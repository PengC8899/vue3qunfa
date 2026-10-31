"""目标群组池 → 多账号批量加群（平均分配 + 不重复加入）。

分配：轮询平均分配，**每个目标群只会落到一个账号名下**，不会两个账号加同一个群。
换号：某个账号加不上（被这个群封了、账号加群额度满了、莫名报错），这个群不会被判死，
     而是换一个「没试过它」的账号接着加，最多换 JOIN_MAX_ACCOUNTS_PER_GROUP 个账号。
     群是主角、账号是资源：只要有一个账号进得去，这个群就能用来群发。
去重：每个账号动手前先拉一次自己的会话列表，已经在群里的直接记「已跳过」；
     真正发起加入时把 Telegram 的 UserAlreadyParticipantError 也当「已加入」，
     所以同一批重跑不会重复加。
进度：状态放在进程内存里（一次性长任务，页面轮询看进度，重启即丢）。
     真正落库的是「最后执行」时间、群成员关系与账号群组缓存的失效，不伪造任何统计。
"""
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timezone

from app.config import CONFIG
from app.database import SessionLocal
from app.models import GroupCache, JoinLog, TargetGroup, Task
from app.services.account_service import account_service
from app.services.plan_service import ACCOUNT_BLOCK_LABELS, STATUS_TRUST_SECONDS
from app.telegram_client import multi_manager

# cannot_send 绝大多数是「一个群都没加，没有群可以测发送」，不影响加群；
# 真正发不出消息才该被发送链路挡掉，加群这条链路不该跟着一起挡。
JOINABLE_STATUSES = {"ok", "slow", "cannot_send"}

# 一个目标群最多因为限流被推迟几次：Telegram 的 JoinChannelRequest 限流经常几百秒，
# 推迟几次还解不开说明这个账号已经被盯上了，判失败比无限循环好。
FLOOD_MAX_RESUMES = 3

# 一个群最多换几个账号去加。Telegram 对「谁能进这个群」是按账号判的：A 号被这个群
# 封了，B 号照样进得去。换到第 N 个还进不去，基本可以断定是群/链接本身的问题，
# 再换下去只是烧掉更多账号的加群额度。
JOIN_MAX_ACCOUNTS_PER_GROUP = max(1, int(os.getenv("JOIN_MAX_ACCOUNTS_PER_GROUP", "3")))

# 每个群要配几个成员账号。1 就是「一个群一个号」的老行为：那个号一旦被这个群禁言，
# 这个群就彻底废了（发送时只读权限查不出禁言，要真发才知道）。留一个备胎才能换号补发。
JOIN_ACCOUNTS_PER_GROUP = max(1, int(os.getenv("JOIN_ACCOUNTS_PER_GROUP", "2")))

# 全局加群节流：账号内部的 delay 管不住「18 个号在同一秒各加一个群」这种突发，
# 而 Telegram 是按出口 IP 限速的，实测整批吃 FloodWait（一次 255~900 秒）。
# 用令牌桶摊平：允许一个小突发，之后每 JOIN_REFILL_S 秒放一个。
# 一号一代理后出口 IP 不同，把 JOIN_BURST 设 0 就能关掉。
JOIN_BURST = max(0, int(os.getenv("JOIN_BURST", "3")))
JOIN_REFILL_S = max(0.1, float(os.getenv("JOIN_REFILL_S", "3")))
_JOIN_TOKENS = float(JOIN_BURST)
_JOIN_REFILL_AT = time.monotonic()
_JOIN_TOKEN_LOCK = asyncio.Lock()


async def _wait_join_slot() -> None:
    """跨账号限速：拿到令牌才发起加群。"""
    global _JOIN_TOKENS, _JOIN_REFILL_AT
    if JOIN_BURST <= 0:
        return
    async with _JOIN_TOKEN_LOCK:
        while True:
            now = time.monotonic()
            _JOIN_TOKENS = min(JOIN_BURST, _JOIN_TOKENS + (now - _JOIN_REFILL_AT) / JOIN_REFILL_S)
            _JOIN_REFILL_AT = now
            if _JOIN_TOKENS >= 1:
                _JOIN_TOKENS -= 1
                return
            await asyncio.sleep((1 - _JOIN_TOKENS) * JOIN_REFILL_S)

# 「需要管理员审核才能进」的群：申请发出去了也进不去，而且申请记录不落库的话
# 下一轮又会换别的账号再申请一遍。所以一旦撞上就写进群状态，之后加群和发送都绕开它。
APPROVAL_REQUIRED_STATUS = "approval_required"
# 这些状态下的群不该再拿去加群：不是进不去（需审核），就是压根不存在（不可访问）。
_UNJOINABLE_STATUSES = (APPROVAL_REQUIRED_STATUS, "unreachable")

# 这些错是账号自己的登录状态出了问题，不是群的问题：撞上就不用再拿剩下的群去试了。
_AUTH_DEAD_MARKERS = (
    "key is not registered",
    "not logged in",
    "authkeyunregistered",
    "authkeyduplicated",
    "authkeyinvalid",
    "sessionrevoked",
    "session revoked",
    "userdeactivated",
)

# 这些错说明「这个群/邀请链接本身已经没用了」：换多少个账号都是同一句，不用再换号。
_GROUP_DEAD_MARKERS = (
    "cannot find any entity",
    "could not find the input entity",
    "specified is private",
    "channel private",
    "channelprivate",
    "invite_expired",
    "invitehash",
    "invite link has expired",
    "invite link is invalid",
    "invalid_invite_link",
    "unknown_invite_type",
    "channel_invalid",
    "channelinvalid",
    "peer_id_invalid",
    "peeridinvalid",
    "not in use",  # UsernameNotOccupiedError：@username 已经没人用了
    "no user has",  # 「No user has "xxx" as username」：这个 @username 根本不存在
)

# 账号加群额度满了：它退出这一轮加群，剩余的群换号加。
# 注意这只是「加群」额度，账号照样能发消息，绝不能像掉线那样判死。
_ACCOUNT_FULL_MARKERS = (
    "channelstoomuch",
    "channels_too_much",
    "too many channels",
    "too many supergroups",
    "too many groups",
    "chatlists_too_much",
    "toomanychatlists",
)

_job: dict | None = None


def _auth_dead(error_text: str | None) -> bool:
    """这句话说明账号已经掉线（401），换任何一个群都会报同一句。"""
    err = str(error_text or "").lower()
    return any(marker in err for marker in _AUTH_DEAD_MARKERS)


def _group_dead(error_text: str | None) -> bool:
    """这句话说明群/链接本身已经没用了：换账号重试是白费。"""
    err = str(error_text or "").lower()
    return any(marker in err for marker in _GROUP_DEAD_MARKERS)


def _account_full(error_text: str | None) -> bool:
    """这个账号加的群已经到上限：它自己退出加群，但还能发消息。"""
    err = str(error_text or "").lower()
    return any(marker in err for marker in _ACCOUNT_FULL_MARKERS)


def _still_joinable(account: str) -> bool:
    """换号之前确认这个账号还在：掉线的账号接过去只会白跑一趟。"""
    result = account_service.cached_status(account)
    status = str(result.get("status") or "")
    return bool(result.get("valid")) and status in JOINABLE_STATUSES


def classify_joinable_accounts(accounts: list[str]) -> tuple[list[str], dict[str, int]]:
    """挑出能用来加群的账号，其余按原因归类给用户看。

    检测结果太旧的账号不算可用：加群一次就是几百个群，拿半小时前的快照去赌，
    账号要是已经掉线就是整批秒挂（8002 实测 41/41 都是 AUTH_KEY_UNREGISTERED）。
    """
    joinable: list[str] = []
    blocked: dict[str, int] = {}
    now = time.time()
    for name in accounts:
        result = account_service.cached_status(name)
        status = str(result.get("status") or "unchecked")
        if result.get("valid") and status in JOINABLE_STATUSES:
            checked_at = result.get("checked_at")
            if checked_at is not None and now - float(checked_at) < STATUS_TRUST_SECONDS:
                joinable.append(name)
                continue
            blocked[ACCOUNT_BLOCK_LABELS["stale"]] = blocked.get(ACCOUNT_BLOCK_LABELS["stale"], 0) + 1
            continue
        label = ACCOUNT_BLOCK_LABELS.get(status, status)
        blocked[label] = blocked.get(label, 0) + 1
    return joinable, blocked


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def members_of(group: TargetGroup) -> set[str]:
    """这个群已知的成员账号（加群结果写回）。"""
    try:
        return {str(name) for name in json.loads(group.joined_by_json or "[]")}
    except Exception:
        return set()


def build_assignment(groups: list[TargetGroup], accounts: list[str]) -> dict[str, list[TargetGroup]]:
    """轮询平均分配：第 i 个群给第 i % n 个账号。

    已经在这个群里的账号要跳过 —— 现在每个群要配 JOIN_ACCOUNTS_PER_GROUP 个成员，
    把群又分给已有成员等于白跑一趟（加群服务只会把它记成「已跳过」）。
    """
    assignment: dict[str, list[TargetGroup]] = {name: [] for name in accounts}
    if not accounts:
        return assignment
    # 打乱后再轮询分配：每个账号拿到的群在列表里的位置不同，
    # 不会所有账号都从群组列表最前面开始加同一批群。
    shuffled = list(groups)
    random.shuffle(shuffled)
    for index, group in enumerate(shuffled):
        members = members_of(group)
        pool = [name for name in accounts if name not in members] or accounts
        assignment[pool[index % len(pool)]].append(group)
    return {name: items for name, items in assignment.items() if items}


def uncovered_groups(groups: list[TargetGroup], accounts: list[str]) -> list[TargetGroup]:
    """挑出「可用成员账号还没配够」的群。

    加群步骤只该处理这些：已经在里面的账号能直接发，让别的账号再进同一个群是白烧加群额度，
    还会把账号推向 Telegram 的「加群太多」限制。8002 实测：手动批量加群已经覆盖 128 个群，
    一键链路却把整池 155 个从头跑了一遍。

    但要给每个群留一个**备胎**：账号被某个群禁言时（只读权限查不出来，只能真发才知道），
    同一个群里还有别的成员才能接手。只配 1 个成员的话，那个号一被禁言这个群就废了
    （8003 实测 96.7% 的发送失败是「你被本群禁言」，309 个群只有 1 个成员、没有备胎）。

    需要管理员审核、以及压根不存在的群直接跳过：申请一万次也进不去，只会白费时间。
    """
    ready = set(accounts)
    pending: list[TargetGroup] = []
    for group in groups:
        if str(group.status or "") in _UNJOINABLE_STATUSES:
            continue
        if len(members_of(group) & ready) < JOIN_ACCOUNTS_PER_GROUP:
            pending.append(group)
    return pending


def preview(assignment: dict[str, list[TargetGroup]]) -> dict:
    per_account = [
        {
            "account": name,
            "count": len(items),
            "sample": [g.username for g in items[:5]],
        }
        for name, items in assignment.items()
    ]
    return {
        "accounts_used": len(assignment),
        "groups": sum(len(items) for items in assignment.values()),
        "per_account": per_account,
    }


def current_job() -> dict | None:
    return _job


def request_stop() -> bool:
    if not _job or not _job.get("running"):
        return False
    _job["stop"] = True
    return True


def _create_job(assignment: dict[str, list[TargetGroup]], delay_ms: int, workers: int,
                accounts: list[str] | None = None) -> dict:
    items = [
        {
            "account": name,
            "row_id": group.id,
            "peer_id": group.group_id,
            "username": group.username,
            "title": group.title,
            "status": "pending",
            "error": None,
            # 这个群已经被哪些账号试过：换号时靠它避免让同一个人再试一遍
            "tried": [name],
        }
        for name, groups in assignment.items()
        for group in groups
    ]
    return {
        "running": True,
        "stop": False,
        "started_at": _now(),
        "finished_at": None,
        # 多账号并发时这里是 {账号: 正在加的群}，前端直接照着显示
        "current": {},
        "delay_ms": delay_ms,
        "workers": workers,
        # 能接手的账号全集（含这轮一个群都没分到的）：换号时从这里挑人
        "accounts": list(accounts or assignment.keys()),
        "total": len(items),
        "done": 0,
        "joined": 0,
        "skipped": 0,
        "requested": 0,
        "failed": 0,
        "pending": len(items),
        "redispatches": 0,
        "flood_waits": 0,
        "items": items,
    }


async def _joined_snapshot(account: str) -> tuple[set[int], set[str]]:
    """拿这个账号当前已加入的群（含频道），用于跳过重复加群。

    同时收 peer id 与 raw id：群组池里存的是 peer id，而 join_group 内部比的是 raw id。
    """
    # 拉全量会话列表可能要翻很多页；卡住会把整个 worker 一起拖死，超时就当没拿到
    rows = await asyncio.wait_for(
        multi_manager.get_joined_groups(account, only_groups=False), timeout=90)
    ids: set[int] = set()
    names: set[str] = set()
    for row in rows:
        for key in ("id", "raw_id"):
            if row.get(key) is not None:
                ids.add(int(row[key]))
        if row.get("username"):
            names.add(str(row["username"]).lower())
    return ids, names


def _already_joined(item: dict, joined_ids: set[int], joined_names: set[str]) -> bool:
    # 群组池里存的 group_id 与账号会话列表的 id 都是 peer id，同一个口径
    peer_id = item.get("peer_id")
    if peer_id and joined_ids and int(peer_id) in joined_ids:
        return True
    name = str(item.get("username") or "").lstrip("@").lower()
    return bool(name) and name in joined_names


def _append_group_to_running_tasks(db, account: str, group_id: int) -> int:
    """新加入的群立刻追加到该账号正在跑的发送任务里，待发送群组马上更新。"""
    changed = 0
    tasks = (
        db.query(Task)
        .filter(
            Task.account_name == account,
            Task.status.in_(("running", "scheduled")),
            # 动态任务每轮自己从群池重算，不用逐条追加（450 个群就是 450 次多余写）
            Task.dynamic_groups == 0,
        )
        .all()
    )
    for task in tasks:
        try:
            ids = [int(x) for x in json.loads(task.group_ids_json or "[]")]
        except Exception:
            ids = []
        if group_id in ids:
            continue
        ids.append(group_id)
        task.group_ids_json = json.dumps(ids)
        changed += 1
    return changed


def _flush(pending: dict[str, dict[str, list]]) -> None:
    """把这一批结果写回库。

    - `joined`：这次真的加进去了 → 更新「最后执行」+ 记成员
    - `member`：这次确认账号本来就在群里（跳过）→ 只记成员
    - `resolved`：加群时拿到了群的真实 id → 回填 group_id，群组池里刚导入的链接
      （group_id 为空）只有回填之后才会进入发送列表
    - `approval`：该群要管理员审核才能进 → 标成需审核，之后加群/发送都绕开
    成员关系是发送链路的硬前提：只有已加入该群的账号才能往这个群发消息，
    所以这里必须落库，调度方案才能据此分配。
    """
    if not pending:
        return
    db = SessionLocal()
    try:
        # 统一用应用时区写时间：其他地方按 CONFIG 时区读，写 UTC 会差出 8 小时，
        # 「刚加进去的群」保护期会直接失效（实测刚加成功就被当成几小时前）。
        now = CONFIG.now()
        for account, buckets in pending.items():
            joined_rows = buckets.get("joined") or []
            member_rows = list({*joined_rows, *(buckets.get("member") or [])})
            approval_rows = buckets.get("approval") or []
            dead_rows = buckets.get("dead") or []
            resolved_rows = buckets.get("resolved") or []
            if resolved_rows:
                # 先回填 id：下面的「追加到正在跑的任务」和发送链路都只认 group_id
                for row_id, gid in resolved_rows:
                    row = db.query(TargetGroup).filter(TargetGroup.id == row_id).first()
                    if row is None:
                        continue
                    if row.group_id is None:
                        row.group_id = int(gid)
                    if str(row.status or "") in ("pending", "checking"):
                        row.status = "available"
                        row.last_error = None
            if joined_rows:
                db.query(TargetGroup).filter(TargetGroup.id.in_(joined_rows)).update(
                    {TargetGroup.last_run_at: now}, synchronize_session=False)
            if approval_rows:
                db.query(TargetGroup).filter(TargetGroup.id.in_(approval_rows)).update(
                    {TargetGroup.status: APPROVAL_REQUIRED_STATUS,
                     TargetGroup.last_error: "该群需要管理员审核才能进，已从加群/发送里剔除"},
                    synchronize_session=False)
            if dead_rows:
                # 群已经不可访问（转私有/链接失效）：换哪个账号都是同一句，
                # 不落状态的话链路每 30 秒就会拿这两个群重试一次，永远失败。
                db.query(TargetGroup).filter(TargetGroup.id.in_(dead_rows)).update(
                    {TargetGroup.status: "unreachable",
                     TargetGroup.last_error: "群已不可访问（私有/无权限），已从加群/发送里剔除"},
                    synchronize_session=False)
            if member_rows:
                rows = db.query(TargetGroup).filter(TargetGroup.id.in_(member_rows)).all()
                for row in rows:
                    try:
                        names = json.loads(row.joined_by_json or "[]")
                    except Exception:
                        names = []
                    if account not in names:
                        names.append(account)
                        row.joined_by_json = json.dumps(names, ensure_ascii=False)
            # 这次真正新加入的群，立刻写进该账号正在跑的发送任务，界面上的
            # 「待发送群组」不用等下一轮刷新就能看到。
            if joined_rows:
                for row in db.query(TargetGroup).filter(TargetGroup.id.in_(joined_rows)).all():
                    if row.group_id is not None:
                        _append_group_to_running_tasks(db, account, int(row.group_id))
            # 群组缓存作废：下次看「已加入群组」时重新拉
            db.query(GroupCache).filter(GroupCache.account_name == account).delete(synchronize_session=False)
        db.commit()
    except Exception as exc:
        db.rollback()
        # 写不进去要留痕：成员关系落不了库，发送链路就会把群分给没进群的账号
        print(f"[JOIN] 回写成员关系失败: {exc}")
    finally:
        db.close()
    pending.clear()


_FLOOD_RE = re.compile(r"wait of (\d+) seconds", re.I)
# 「You have successfully requested to join this chat or channel」= 已提交申请，等管理员审批。
# 这不是失败，但也不算已加入，所以单列一种结果。
_JOIN_REQUEST_RE = re.compile(r"successfully requested to join", re.I)


async def _try_join(account: str, item: dict, known_ids: set[int], job: dict) -> dict:
    """加一次群；被限流就把 FloodWait 原样带回去，由调度层决定等还是先做别的账号。

    Telegram 对 JoinChannelRequest 的限流动辄几百秒（实测 255~900s），
    如果在账号内部就地 sleep，一个 worker 就被顶住十几分钟什么都干不了。
    """
    await _wait_join_slot()
    result = await multi_manager.join_group(account, item["username"], known_joined_ids=known_ids)
    if result.get("ok"):
        return result
    match = _FLOOD_RE.search(str(result.get("error") or ""))
    if not match:
        return result
    wait_s = int(match.group(1)) + 2
    job["flood_waits"] += 1
    print(f"[JOIN] {account} 被限流 {wait_s}s：{item['username']}，先让出去做别的账号")
    return {**result, "flood_wait": wait_s}


def _tally(job: dict) -> None:
    items = job["items"]
    job["joined"] = sum(1 for it in items if it["status"] == "joined")
    job["skipped"] = sum(1 for it in items if it["status"] == "skipped")
    job["requested"] = sum(1 for it in items if it["status"] == "requested")
    job["failed"] = sum(1 for it in items if it["status"] == "failed")
    job["pending"] = sum(1 for it in items if it["status"] == "pending")
    job["done"] = job["joined"] + job["skipped"] + job["requested"] + job["failed"]


def _hand_over(item: dict, account: str, reason: str, redispatch: list[dict], job: dict) -> None:
    """这个群这个账号加不上：交给别的账号接手，不当失败处理。

    状态保持 pending —— 它还没定生死，只是换个人再试。
    「试过谁」在分派的时候就记进 item["tried"] 了，这里不重复记。
    """
    item["error"] = f"{account} 加不上，换账号重试：{reason[:120]}"
    redispatch.append(item)
    _tally(job)


def _record_join_log(account: str, item: dict, status: str, error: str | None = None) -> None:
    """落一条加群日志；写失败不能影响加群本身。"""
    gid = item.get("peer_id")
    try:
        gid = int(gid) if gid is not None else None
    except Exception:
        gid = None
    db = SessionLocal()
    try:
        db.add(JoinLog(
            account_name=account,
            group_id=gid,
            group_username=(item.get("username") or None),
            group_title=(item.get("title") or None),
            status=status,
            error=(str(error)[:500] if error else None),
            # 必须显式写应用时区的当前时间：列上的 server_default 走 SQLite
            # CURRENT_TIMESTAMP，那是 UTC，会比群发日志整整差 8 小时。
            created_at=CONFIG.now(),
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


async def _run_account(
    account: str,
    items: list[dict],
    job: dict,
    delay_s: float,
    snapshots: dict[str, tuple[set[int], set[str]]],
    redispatch: list[dict],
) -> tuple[list[dict], float]:
    """一个账号把它名下的群挨个加完（账号内部严格串行，防风控）。

    加不上的群不在这里定生死，而是塞进 `redispatch` 交给别的账号接着加
    （限流除外：等一等就能过，换号反而白烧一个账号的加群额度）。

    返回 (还没加完的群, 多少秒后可以再来)：撞上限流就整批交还给调度层，
    让这个账号去冷却，worker 转手去做别的账号，而不是在这里干等。
    """
    pending: dict[str, dict[str, list]] = {}
    buckets: dict[str, list] = {"joined": [], "member": [], "approval": [], "dead": [], "resolved": []}
    pending[account] = buckets
    try:
        if account in snapshots:
            joined_ids, joined_names = snapshots[account]
        else:
            job["current"][account] = "读取该账号已加入的群…"
            try:
                joined_ids, joined_names = await _joined_snapshot(account)
            except Exception:
                joined_ids, joined_names = set(), set()  # 拉不到就照常尝试，join 内部还会再判一次
            snapshots[account] = (joined_ids, joined_names)

        for index, item in enumerate(items):
            if job["stop"]:
                return [], 0.0
            job["current"][account] = item["username"]
            if _already_joined(item, joined_ids, joined_names):
                item["status"] = "skipped"
                item["error"] = "已在群内，未重复加入"
                buckets["member"].append(item["row_id"])
                _record_join_log(account, item, "skipped", item["error"])
                _tally(job)
                continue  # 没碰 Telegram，不用等间隔
            try:
                result = await _try_join(account, item, joined_ids, job)
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:200]}

            if result.get("flood_wait"):
                item["resumes"] = int(item.get("resumes") or 0) + 1
                if item["resumes"] >= FLOOD_MAX_RESUMES:
                    # 一直解除不了就别无限循环，明确判失败让用户看见
                    item["status"] = "failed"
                    item["error"] = f"Telegram 限流，推迟 {item['resumes']} 次仍未解除"
                    _record_join_log(account, item, "failed", item["error"])
                    _tally(job)
                    continue
                item["error"] = f"被 Telegram 限流，推迟 {result['flood_wait']}s 后再试"
                _record_join_log(account, item, "flood_wait", item["error"])
                return items[index:], float(result["flood_wait"])

            if result.get("ok"):
                if result.get("group_id") is not None:
                    # 加群成功时 Telegram 把群的真实 id 带回来了：回填到群组池，
                    # 刚导入的链接（group_id 为空）才能进发送列表。
                    gid = int(result["group_id"])
                    buckets["resolved"].append((item["row_id"], gid))
                    # 同步写进 item，紧接着的加群日志才有群 id（新导入的链接原本是 None）
                    item["peer_id"] = gid
                if result.get("already_joined"):
                    item["status"] = "skipped"
                    item["error"] = "已在群内，未重复加入"
                    buckets["member"].append(item["row_id"])
                    _record_join_log(account, item, "skipped", item["error"])
                else:
                    item["status"] = "joined"
                    buckets["joined"].append(item["row_id"])
                    buckets["member"].append(item["row_id"])
                    _record_join_log(account, item, "joined", None)
                    if result.get("group_id") is not None:
                        joined_ids.add(int(result["group_id"]))
            else:
                error_text = str(result.get("error") or "join_failed")
                if _JOIN_REQUEST_RE.search(error_text):
                    item["status"] = "requested"
                    item["error"] = "该群需要管理员审批，已从加群/发送里剔除"
                    buckets["approval"].append(item["row_id"])
                    _record_join_log(account, item, "requested", item["error"])
                elif _auth_dead(error_text):
                    # 账号已经掉线：它名下的每个群都会报同一句 401，剩下的群换号加
                    account_service.mark_account_dead(
                        account, f"加群时 Telegram 判定登录已失效：{error_text[:120]}")
                    _record_join_log(account, item, "failed", f"账号登录已失效：{error_text[:120]}")
                    for rest in items[index:]:
                        _hand_over(rest, account, "账号登录已失效", redispatch, job)
                    print(f"[JOIN] {account} 登录已失效，名下剩余 {len(items) - index} 个群转给别的账号")
                    return [], 0.0
                elif _account_full(error_text):
                    # 账号加群额度满了：它退出加群，但账号本身没坏，还能发消息，不许判死
                    _record_join_log(account, item, "retry", "该账号加群额度已满，交给别的账号")
                    for rest in items[index:]:
                        _hand_over(rest, account, "该账号加群额度已满", redispatch, job)
                    print(f"[JOIN] {account} 加群额度已满，退出加群，剩余 {len(items) - index} 个群转给别的账号")
                    return [], 0.0
                elif _group_dead(error_text):
                    # 群/链接本身没用了：换号也是同一句，直接判死，别再拿别的账号去试
                    item["status"] = "failed"
                    item["error"] = f"群不可用，换账号也加不了：{error_text[:160]}"
                    buckets["dead"].append(item["row_id"])
                    _record_join_log(account, item, "failed", item["error"])
                else:
                    _record_join_log(account, item, "retry", error_text)
                    _hand_over(item, account, error_text, redispatch, job)
            _tally(job)
            await asyncio.sleep(delay_s + random.uniform(0, delay_s * 0.4))
        return [], 0.0
    finally:
        _flush(pending)
        job["current"].pop(account, None)


def _give_up(item: dict, reason: str) -> None:
    """这个群到此为止：把换过几个账号、最后一次报什么错一起写清楚。"""
    item["status"] = "failed"
    detail = str(item.get("error") or "").strip()
    item["error"] = f"{reason}：{detail}" if detail else reason
    _record_join_log(str(item.get("account") or ""), item, "failed", item["error"])


def _redistribute(
    items: list[dict],
    accounts: list[str],
    load: dict[str, int],
    job: dict,
) -> list[tuple[str, list[dict]]]:
    """把加不上的群分给「没试过它、而且现在活最少」的账号，接手继续加。

    群是主角：A 号被这个群封了，不代表 B 号进不去；只要有一个账号进得去，
    这个群就能用来群发。每个群最多试 JOIN_MAX_ACCOUNTS_PER_GROUP 个账号，
    到顶就判失败——继续换只会烧掉更多账号的加群额度，还拖住整条链路。
    """
    batches: dict[str, list[dict]] = {}
    for item in items:
        tried = item.get("tried") or []
        if len(tried) >= JOIN_MAX_ACCOUNTS_PER_GROUP:
            _give_up(item, f"换过 {len(tried)} 个账号都没能加进去")
            continue
        candidates = [name for name in accounts if name not in tried and _still_joinable(name)]
        if not candidates:
            _give_up(item, "没有别的可用账号能接手")
            continue
        target = min(candidates, key=lambda name: load.get(name, 0))
        item["account"] = target
        item.setdefault("tried", []).append(target)
        batches.setdefault(target, []).append(item)
        load[target] = load.get(target, 0) + 1
        job["redispatches"] += 1
    _tally(job)
    return list(batches.items())


async def _run_job(job: dict) -> None:
    """把账号分给若干个 worker 并发跑；加不上的群换账号接着加。

    一个账号内部严格串行 + 间隔，多个账号之间并发：用户是一号一代理，
    不同账号出口 IP 不同，所以并发不会互相踩；串行跑 400 多个群要几个小时。

    撞上 FloodWait 的账号会被整条放回队尾，冷却到点再上；这样 worker 永远在
    干能干的活，不会因为一个账号被限流 900 秒就整条流水线停摆。

    一轮跑完还有没加上的群，就换号再来一轮（见 `_redistribute`）；换不出接手人
    或者都试满次数时收工，绝不停在那里等。
    """
    delay_s = max(job["delay_ms"], 0) / 1000.0
    by_account: dict[str, list[dict]] = {}
    for item in job["items"]:
        by_account.setdefault(item["account"], []).append(item)

    accounts = list(job.get("accounts") or by_account)
    load = {name: len(items) for name, items in by_account.items()}
    batch: list[tuple[str, list[dict]]] = list(by_account.items())
    snapshots: dict[str, tuple[set[int], set[str]]] = {}
    redispatch: list[dict] = []
    lock = asyncio.Lock()

    try:
        while batch and not job["stop"]:
            # 单轮队列：[账号, 还没加的群, 可以开工的时间(monotonic)]
            queue: list[list] = [[name, items, 0.0] for name, items in batch]

            async def take() -> list | None:
                """拿一个现在就能开工的账号；剩下全在冷却就等到最早那个解禁。"""
                while not job["stop"]:
                    async with lock:
                        if not queue:
                            return None
                        now = time.monotonic()
                        for index, entry in enumerate(queue):
                            if entry[2] <= now:
                                return queue.pop(index)
                        soonest = min(entry[2] for entry in queue)
                    await asyncio.sleep(min(soonest - now, 5.0))
                return None

            async def worker() -> None:
                while not job["stop"]:
                    entry = await take()
                    if entry is None:
                        return
                    account, items, _ = entry
                    remaining, resume_in = await _run_account(
                        account, items, job, delay_s, snapshots, redispatch)
                    if remaining and not job["stop"]:
                        async with lock:
                            queue.append([account, remaining, time.monotonic() + max(resume_in, 1.0)])

            await asyncio.gather(*(worker() for _ in range(max(1, min(job["workers"], len(queue))))))
            if job["stop"] or not redispatch:
                break
            waiting, redispatch = redispatch, []
            batch = _redistribute(waiting, accounts, load, job)
    finally:
        job["running"] = False
        job["stop"] = False
        job["current"] = {}
        job["finished_at"] = _now()


def start_job(assignment: dict[str, list[TargetGroup]], delay_ms: int, workers: int = 12,
              accounts: list[str] | None = None) -> dict:
    """启动后台加群任务；同一时间只允许一个在跑。

    accounts 传「这一轮能用的账号全集」：某个群加不上时要换号接手，
    只从分配结果里挑人会漏掉那些一个群都没分到的账号。
    """
    global _job
    if _job and _job.get("running"):
        return _job
    _job = _create_job(assignment, delay_ms, workers, accounts=accounts)
    asyncio.create_task(_run_job(_job))
    return _job
