"""账号预热与梯队门禁。

为什么需要：8003 实测「一批新导入的号直接冲 507 个群」，失败里 96.7% 是
`user_banned_in_channel`，而且只读 `get_permissions()` 查不出禁言（返回 None，
不是 False），要真发那一刻才知道。也就是说新号上量的代价是**不可逆的**——
被禁言之后再补号，也只是拿新的号去送。

做法：新号先只做只读动作（读自己资料、拉会话列表、翻几个已加入群的历史），
攒 readiness 分；分数够了才「毕业」进真实群发。读操作不改账号状态、不引发
反垃圾，但能产生像真人的调用模式。

**老号豁免**：只要这个号在我们库里成功发出过消息，就直接算成熟账号，不进预热。
否则功能一上线，现有 200+ 个一直在用的号会全被拦下来。

预热只针对未毕业的账号，而这些账号本来就被门禁拦着不给发送，
所以预热永远不可能和群发抢同一个账号。
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from datetime import datetime, timezone

from app.config import CONFIG

# 预热进度存 system_kv，和 account_status:* 一样能活过容器重启
WARMUP_KEY_PREFIX = "warmup:"

# 梯队：readiness 和「有没有发送历史」共同决定
TIER_NEW = "new"          # 刚导入，一分没有
TIER_WARMING = "warming"  # 预热中
TIER_TRUSTED = "trusted"  # 已毕业，可以发，但保守
TIER_MATURE = "mature"    # 老号豁免，或养得很足

# 梯队规格：interval_s 是这个账号两条消息之间至少隔多少秒（0 = 不额外限制，
# 直接用任务自己的 delay_ms），daily_max 是日上限（0 = 不限制）。
# 成熟档两项都是 0：老号本来就一直在用，上线这个功能不该改变它们已有的节奏。
TIER_SPECS: dict[str, dict] = {
    TIER_NEW: {"interval_s": 0, "daily_max": 0, "label": "新号"},
    TIER_WARMING: {"interval_s": 0, "daily_max": 0, "label": "预热中"},
    TIER_TRUSTED: {"interval_s": 45, "daily_max": 40, "label": "已毕业"},
    TIER_MATURE: {"interval_s": 0, "daily_max": 0, "label": "成熟"},
}

# 毕业后再多养多少分算「成熟」（不靠豁免的那条路径）
MATURE_SCORE_MULTIPLIER = 2

_EXEMPT_CACHE: dict = {"at": 0.0, "names": set()}
_EXEMPT_TTL_S = 60

# 预热进度：一次性长任务，页面轮询看进度，重启即丢（和 join_service 一致）
_PROGRESS: dict = {"running": False, "started_at": None, "finished_at": None,
                   "accounts": {}, "current": None}
_WARMUP_TASK: dict = {"handle": None, "stop": False, "accounts": [], "wake": asyncio.Event()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ready_score() -> int:
    return max(1, int(getattr(CONFIG, "WARMUP_READY_SCORE", 60)))


def score_per_cycle() -> int:
    return max(1, int(getattr(CONFIG, "WARMUP_SCORE_PER_CYCLE", 10)))


def warmup_enabled() -> bool:
    return bool(int(getattr(CONFIG, "WARMUP_ENABLED", 1)))


# ---------------------------------------------------------------- 状态读写

def _empty_state(account: str) -> dict:
    return {"account": account, "readiness": 0, "cycles": 0, "started_at": None,
            "last_cycle_at": None, "graduated_at": None, "last_error": None}


def load_states(accounts: list[str]) -> dict[str, dict]:
    """一次查出多个账号的预热状态。

    页面每轮轮询几十个号，一个号开一次 DB 会话会把 SQLite 拖住。
    """
    from app.database import SessionLocal
    from app.models import SystemKV

    names = [name for name in dict.fromkeys(accounts) if name]
    states = {name: _empty_state(name) for name in names}
    if not names:
        return states
    db = SessionLocal()
    try:
        keys = [WARMUP_KEY_PREFIX + name for name in names]
        for row in db.query(SystemKV).filter(SystemKV.k.in_(keys)).all():
            name = row.k[len(WARMUP_KEY_PREFIX):]
            if name in states and row.v:
                try:
                    states[name].update(json.loads(row.v))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        db.close()
    return states


def load_state(account: str) -> dict:
    """读某个账号的预热状态；没有记录就是全新账号。"""
    return load_states([account])[account]



def save_state(account: str, state: dict) -> None:
    from app.database import SessionLocal
    from app.models import SystemKV

    payload = json.dumps(state, ensure_ascii=False, default=str)
    db = SessionLocal()
    try:
        key = WARMUP_KEY_PREFIX + account
        row = db.query(SystemKV).filter(SystemKV.k == key).first()
        if row:
            row.v = payload
        else:
            db.add(SystemKV(k=key, v=payload))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def forget_state(account: str) -> None:
    """账号删了就把预热记录一起清掉，别留孤儿行。"""
    from app.database import SessionLocal
    from app.models import SystemKV

    db = SessionLocal()
    try:
        db.query(SystemKV).filter(SystemKV.k == WARMUP_KEY_PREFIX + account).delete(
            synchronize_session=False)
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


# ---------------------------------------------------------------- 老号豁免

def exempt_accounts() -> set[str]:
    """成功发出过消息的账号 —— 这些是已经在用的老号，直接当成熟，不进预热。

    查询结果缓存一分钟：门禁在一次方案生成里会被问几百遍，
    每次都去扫 send_logs 会把 SQLite 拖住。
    """
    now = time.time()
    if now - _EXEMPT_CACHE["at"] < _EXEMPT_TTL_S and _EXEMPT_CACHE["names"]:
        return _EXEMPT_CACHE["names"]
    from app.database import SessionLocal
    from app.models import SendLog

    names: set[str] = set()
    db = SessionLocal()
    try:
        rows = (db.query(SendLog.account_name)
                .filter(SendLog.status == "success")
                .distinct()
                .all())
        names = {str(r[0]) for r in rows if r[0]}
    except Exception:
        # 查不出来时宁可放行（返回空集合 = 不豁免任何人会让所有老号被拦，那是更坏的故障）
        names = set(_EXEMPT_CACHE["names"])
    finally:
        db.close()
    _EXEMPT_CACHE["at"] = now
    _EXEMPT_CACHE["names"] = names
    return names


def invalidate_exempt_cache() -> None:
    _EXEMPT_CACHE["at"] = 0.0


def ensure_state(account: str) -> dict:
    """新上传的账号落一条初始状态，明确标成「新号」。

    这一步是必须的：光靠「有没有预热状态」分不出新老，上传时不写的话，
    重启时的 backfill 会把刚上传的号当成存量账号豁免掉，预热就永远不会生效。
    """
    state = load_state(account)
    if not state.get("started_at"):
        state["started_at"] = _now_iso()
        save_state(account, state)
    return state


def backfill_existing(accounts: list[str]) -> dict:
    """把「这个功能上线之前就已经在用的账号」一次性标成存量账号，直接算成熟。

    为什么必须有这一步：8003 上线时 send_logs 是空的（发送历史被清过），
    单靠「成功发送记录」判老号会得到空集，74 个一直在用的号会全部被拦去预热，
    等于上线瞬间把发送能力清零。所以判据是「有没有预热状态」：
    没有状态的，就是上线前就存在的账号。
    """
    if not warmup_enabled():
        return {"ok": False, "message": "预热未启用，不需要打标", "marked": 0}
    from app.database import SessionLocal
    from app.models import SystemKV

    threshold = ready_score()
    db = SessionLocal()
    marked: list[str] = []
    try:
        existing = {
            row[0][len(WARMUP_KEY_PREFIX):]
            for row in db.query(SystemKV.k).filter(SystemKV.k.like(f"{WARMUP_KEY_PREFIX}%")).all()
        }
        for name in accounts:
            if not name or name in existing:
                continue
            db.add(SystemKV(k=WARMUP_KEY_PREFIX + name, v=json.dumps({
                "account": name,
                "readiness": threshold * MATURE_SCORE_MULTIPLIER,
                "cycles": 0,
                "started_at": _now_iso(),
                "graduated_at": _now_iso(),
                "backfilled": True,
            }, ensure_ascii=False)))
            marked.append(name)
        if marked:
            db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "message": f"打标失败: {exc}", "marked": 0}
    finally:
        db.close()
    if marked:
        invalidate_exempt_cache()
        print(f"[WARMUP] 存量账号打标 {len(marked)} 个（直接算成熟，不进预热）")
    return {"ok": True, "marked": len(marked), "accounts": marked}


# ---------------------------------------------------------------- 梯队判定

def tier_of(state: dict, exempt: bool = False) -> str:
    """这个账号现在算哪一档。"""
    if exempt or state.get("backfilled"):
        return TIER_MATURE
    score = int(state.get("readiness") or 0)
    threshold = ready_score()
    if score >= threshold * MATURE_SCORE_MULTIPLIER:
        return TIER_MATURE
    if score >= threshold:
        return TIER_TRUSTED
    # 跑过至少一轮才算「预热中」；只是上传时落了个 started_at 的还属于新号
    if score > 0 or int(state.get("cycles") or 0) > 0:
        return TIER_WARMING
    return TIER_NEW


def tier_for(account: str, exempt: set[str] | None = None) -> str:
    pool = exempt_accounts() if exempt is None else exempt
    return tier_of(load_state(account), account in pool)


def interval_for(account: str, exempt: set[str] | None = None) -> int:
    """这个账号两条消息至少隔几秒。0 = 不放行。"""
    return int(TIER_SPECS.get(tier_for(account, exempt), TIER_SPECS[TIER_NEW])["interval_s"])


def gate_state(state: dict, exempt: bool = False) -> tuple[bool, str]:
    """给了状态就不用再查库；批量判定时用它。"""
    if not warmup_enabled():
        return True, ""
    if tier_of(state, exempt) in (TIER_NEW, TIER_WARMING):
        return False, "账号预热中"
    return True, ""


def gate(account: str, exempt: set[str] | None = None) -> tuple[bool, str]:
    """能不能让它进群发。返回 (是否放行, 不放行的原因)。"""
    pool = exempt_accounts() if exempt is None else exempt
    return gate_state(load_state(account), account in pool)


def gate_many(accounts: list[str], exempt: set[str] | None = None) -> set[str]:
    """一次查库判定一批账号，返回能进群发的那些。"""
    if not warmup_enabled():
        return set(accounts)
    pool = exempt_accounts() if exempt is None else exempt
    states = load_states(accounts)
    return {name for name in accounts if gate_state(states[name], name in pool)[0]}


def pending_accounts(accounts: list[str]) -> list[str]:
    """还没毕业的账号（新号 + 预热中），自动预热和「开始预热」都用它。"""
    if not warmup_enabled():
        return []
    pool = exempt_accounts()
    states = load_states(accounts)
    return [name for name in dict.fromkeys(accounts)
            if tier_of(states[name], name in pool) in (TIER_NEW, TIER_WARMING)]



# ---------------------------------------------------------------- 只读预热动作

async def _read_cycle(manager, account: str) -> dict:
    """跑一轮只读动作，返回这一轮实际做了什么。

    全是读操作：取自己资料、拉会话列表、翻几个已加入群的历史。
    不改账号状态、不加群、不发消息、不点反应。
    """
    mgr = manager.get(account)
    await asyncio.wait_for(mgr.ensure_connected(), timeout=30)
    client = mgr.client
    if client is None:
        raise RuntimeError("客户端未就绪")

    did: list[str] = []

    me = await asyncio.wait_for(client.get_me(), timeout=15)
    if me:
        did.append("get_me")

    limit = max(1, int(getattr(CONFIG, "WARMUP_READ_DIALOGS", 15)))
    dialogs = await asyncio.wait_for(client.get_dialogs(limit=limit), timeout=30) or []
    did.append(f"get_dialogs:{len(dialogs)}")

    msg_limit = max(1, int(getattr(CONFIG, "WARMUP_READ_MESSAGES", 8)))
    picks = [d for d in dialogs if getattr(d, "entity", None) is not None]
    random.shuffle(picks)
    read = 0
    for dialog in picks[:3]:
        try:
            await asyncio.wait_for(client.get_messages(dialog.entity, limit=msg_limit), timeout=20)
            read += 1
            # 真人是隔一会儿翻一段，不是一口气全拉完
            await asyncio.sleep(random.uniform(1.5, 4.0))
        except Exception:
            continue
    if read:
        did.append(f"read_history:{read}")

    return {"actions": did, "dialogs": len(dialogs)}


async def run_cycle(manager, account: str) -> dict:
    """给账号加一轮预热分并落库。失败只记错误，不扣分、不打断整个预热批次。"""
    state = load_state(account)
    if not state.get("started_at"):
        state["started_at"] = _now_iso()
    try:
        detail = await _read_cycle(manager, account)
        state["readiness"] = int(state.get("readiness") or 0) + score_per_cycle()
        state["cycles"] = int(state.get("cycles") or 0) + 1
        state["last_cycle_at"] = _now_iso()
        state["last_error"] = None
        state["fail_streak"] = 0
        # 读得通就证明登录有效：把状态刷成可用，边加群边群发不用再手动检测一遍
        try:
            from app.services.account_service import account_service
            account_service.mark_account_alive(account)
        except Exception:
            pass
        if int(state["readiness"]) >= ready_score() and not state.get("graduated_at"):
            state["graduated_at"] = _now_iso()
        ok = True
    except Exception as exc:
        # TimeoutError 这类异常的 str() 是空串，直接记下来界面上的失败原因就是一片空白，
        # 至少要把异常类型名留下（实测 916239381011 就只显示了一个空的 last_error）。
        state["last_error"] = (str(exc) or type(exc).__name__)[:160]
        state["last_cycle_at"] = _now_iso()
        state["fail_streak"] = int(state.get("fail_streak") or 0) + 1
        detail = {"actions": [], "error": state["last_error"]}
        ok = False
    save_state(account, state)
    return {"account": account, "ok": ok, "readiness": state["readiness"],
            "cycles": state["cycles"], "detail": detail}


# ---------------------------------------------------------------- 后台预热循环

def progress() -> dict:
    return {
        "running": bool(_PROGRESS["running"]),
        "started_at": _PROGRESS["started_at"],
        "finished_at": _PROGRESS["finished_at"],
        "current": _PROGRESS["current"],
        "accounts": _PROGRESS["accounts"],
    }


def is_running() -> bool:
    return bool(_PROGRESS["running"])


def stop() -> dict:
    _WARMUP_TASK["stop"] = True
    _WARMUP_TASK["wake"].set()
    return {"ok": True, "message": "已请求停止，当前这一轮跑完就停"}


async def _warm_loop(manager, accounts: list[str]) -> None:
    interval = max(60, int(getattr(CONFIG, "WARMUP_CYCLE_INTERVAL_S", 300)))
    concurrency = max(1, int(getattr(CONFIG, "WARMUP_MAX_CONCURRENT", 5)))
    max_failures = max(1, int(getattr(CONFIG, "WARMUP_MAX_FAILURES", 6)))
    sem = asyncio.Semaphore(concurrency)

    while not _WARMUP_TASK["stop"]:
        # 队列可能在中途被 start() 追加新号，每轮都重新读一遍
        targets = [name for name in dict.fromkeys(accounts) if name]
        states = load_states(targets)
        pending = []
        for name in targets:
            # 账号可能在预热跑到一半时被删了（purge-sessions / 单个删除）。
            # 不校验的话，这个循环会一直对着已经不存在的 session 空转：每轮全部失败、
            # 分数永远到不了毕业线，于是永远不退出（8002 实测踩过这个坑）。
            if not os.path.exists(os.path.join(CONFIG.SESSION_DIR, f"{name}.session")):
                _PROGRESS["accounts"].setdefault(name, {})["removed"] = True
                continue
            readiness = int(states[name].get("readiness") or 0)
            if readiness >= ready_score():
                _PROGRESS["accounts"].setdefault(name, {})["done"] = True
                _PROGRESS["accounts"][name]["readiness"] = readiness
                continue
            # 连续失败到上限就别再空转了（掉线的号每 5 分钟重连一次纯属浪费）
            if int(states[name].get("fail_streak") or 0) >= max_failures:
                _PROGRESS["accounts"].setdefault(name, {})["failed_out"] = True
                continue
            pending.append(name)
        if not pending:
            break

        results: list[dict] = []

        async def one(name: str):
            async with sem:
                _PROGRESS["current"] = name
                try:
                    result = await run_cycle(manager, name)
                    results.append(result)
                    _PROGRESS["accounts"][name] = {
                        "readiness": result["readiness"],
                        "cycles": result["cycles"],
                        "ok": result["ok"],
                        "detail": result.get("detail", {}),
                    }
                    print(f"[WARMUP] {name}: readiness={result['readiness']} "
                          f"cycles={result['cycles']} ok={result['ok']}")
                except Exception as exc:
                    _PROGRESS["accounts"][name] = {"ok": False, "error": str(exc)[:120]}
                    print(f"[WARMUP] {name} 失败: {exc}")

        await asyncio.gather(*(one(name) for name in pending))
        if _WARMUP_TASK["stop"]:
            break
        # 这一轮全部毕业就没必要再空等一个周期
        if results and all(int(r["readiness"]) >= ready_score() for r in results):
            break
        print(f"[WARMUP] 本轮结束，{interval}s 后跑下一轮")
        try:
            # 用 Event 而不是 sleep：新号入队 / 点停止可以立刻打断等待
            await asyncio.wait_for(_WARMUP_TASK["wake"].wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
        _WARMUP_TASK["wake"].clear()

    _PROGRESS["running"] = False
    _PROGRESS["finished_at"] = _now_iso()
    _PROGRESS["current"] = None
    print("[WARMUP] 预热结束")
    invalidate_exempt_cache()


def start(manager, accounts: list[str]) -> dict:
    """开始预热一批账号；已经在跑就追加到队列，不用等这一批结束。"""
    if not warmup_enabled():
        return {"ok": False, "message": "预热功能未启用（WARMUP_ENABLED=0）"}
    targets = [name for name in dict.fromkeys(accounts) if name]
    if not targets:
        return {"ok": False, "message": "没有选中账号"}

    exempt = exempt_accounts()
    if _PROGRESS["running"]:
        new = [name for name in targets if name not in _WARMUP_TASK["accounts"]]
        if not new:
            return {"ok": True, "accounts": [], "message": "这些账号已经在预热队列里"}
        _WARMUP_TASK["accounts"].extend(new)
        for name in new:
            _PROGRESS["accounts"][name] = {"exempt": name in exempt}
        _WARMUP_TASK["wake"].set()
        return {"ok": True, "accounts": new, "message": f"已加入预热队列 {len(new)} 个账号"}

    _PROGRESS.update({
        "running": True,
        "started_at": _now_iso(),
        "finished_at": None,
        "current": None,
        "accounts": {name: {"exempt": name in exempt} for name in targets},
    })
    _WARMUP_TASK["stop"] = False
    _WARMUP_TASK["accounts"] = targets
    _WARMUP_TASK["wake"] = asyncio.Event()
    _WARMUP_TASK["handle"] = asyncio.create_task(_warm_loop(manager, targets))
    return {"ok": True, "accounts": targets, "message": f"已开始预热 {len(targets)} 个账号"}


# ---------------------------------------------------------------- 给 UI 的快照

def snapshot(accounts: list[str]) -> list[dict]:
    """账号列表页用：梯队 + readiness + 预热进度，一次查完不 N+1。"""
    exempt = exempt_accounts()
    states = load_states(accounts)
    out: list[dict] = []
    for name in accounts:
        state = states[name]
        tier = tier_of(state, name in exempt)
        spec = TIER_SPECS.get(tier, TIER_SPECS[TIER_NEW])
        out.append({
            "account": name,
            "tier": tier,
            "tier_label": spec["label"],
            "readiness": int(state.get("readiness") or 0),
            "ready_score": ready_score(),
            "cycles": int(state.get("cycles") or 0),
            "exempt": name in exempt or bool(state.get("backfilled")),
            "backfilled": bool(state.get("backfilled")),
            "interval_s": spec["interval_s"],
            "daily_max": spec["daily_max"],
            "graduated_at": state.get("graduated_at"),
            "last_cycle_at": state.get("last_cycle_at"),
            "last_error": state.get("last_error"),
            "warmup_running": name in _PROGRESS["accounts"] and _PROGRESS["running"],
        })
    return out


__all__ = [
    "TIER_NEW", "TIER_WARMING", "TIER_TRUSTED", "TIER_MATURE", "TIER_SPECS",
    "WARMUP_KEY_PREFIX", "load_state", "load_states", "save_state", "forget_state",
    "ensure_state", "backfill_existing",
    "exempt_accounts", "invalidate_exempt_cache", "tier_of", "tier_for",
    "interval_for", "gate", "gate_state", "gate_many", "pending_accounts",
    "ready_score", "run_cycle", "start", "stop", "progress",
    "is_running", "snapshot", "warmup_enabled",
]

