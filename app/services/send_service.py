import asyncio
import time
import hashlib
import random
import re
from sqlalchemy.orm import Session
from app.telegram_client import MultiTelegramManager
from app.models import SendLog
from app.config import CONFIG
from app.services import warmup
from app.services.ratelimit import RateLimiter, human_delay
from app.services.send_scheduler import SendScheduler
from app.services.group_service import (
    get_banned_group_ids,
    add_banned_group,
    needs_ban_vote,
    register_ban_vote,
    should_exclude_group_on_error,
    should_add_to_global_blist,
    GLOBAL_BAN_VOTES_REQUIRED,
)


_SEND_CACHE: dict[str, float] = {}

# 令牌桶按账号隔离、且跨任务共享：同一个账号即使被两个任务同时发，
# 走的也是同一个桶；各自 sleep 做不到这一点。
_LIMITER = RateLimiter(default_rate=1.0 / max(int(getattr(CONFIG, "SEND_RATE_INTERVAL_S", 20)), 1))


def _msg_key(account: str, gid: int, message: str, parse_mode: str, disable_web_page_preview: bool) -> str:
    h = hashlib.sha256((parse_mode or "")
                       .encode("utf-8") + b"|" +
                       (b"1" if disable_web_page_preview else b"0") + b"|" +
                       message.encode("utf-8")).hexdigest()[:16]
    return f"{account}:{gid}:{h}"


def _should_skip(account: str, gid: int, message: str, parse_mode: str, disable_web_page_preview: bool, window_s: int = 120) -> bool:
    now = time.monotonic()
    key = _msg_key(account, gid, message, parse_mode, disable_web_page_preview)
    ts = _SEND_CACHE.get(key)
    for k, t in list(_SEND_CACHE.items()):
        if now - t > window_s * 2:
            del _SEND_CACHE[k]
    if ts and (now - ts) < window_s:
        return True
    _SEND_CACHE[key] = now
    return False


def _flood_wait_seconds(err) -> int | None:
    """从 Telegram 的错误文本里解析出要等多少秒；不是限流就返回 None。

    Telegram 的原文是「A wait of 2956 seconds is required」，里面**没有 "FloodWait"
    这个词**。只认 "FloodWait" 会让这个分支永远进不去，于是 1.5 秒后就撞回去重试
    （实测 04:13 被要求等 2956 秒，04:16 就重试了，等于白撞）。
    """
    text = str(err or "")
    if "FloodWait" not in text and "A wait of" not in text:
        return None
    match = re.search(r"(\d+)\s*seconds?", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if ":" in text:
        try:
            return int(text.split(":")[1])
        except Exception:
            return None
    return 60


async def send_to_groups(
    manager: MultiTelegramManager,
    db: Session,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    retry_max: int = 0,
    retry_delay_ms: int = 1500,
):
    use_sched = bool(getattr(CONFIG, "SCHEDULER_ENABLED", 1))
    ids = list(group_ids)
    try:
        banned = set(get_banned_group_ids(db, account))
        if banned:
            ids = [g for g in ids if g not in banned]
    except Exception:
        pass
    if use_sched:
        sched = SendScheduler(db)
        grading = sched.grade_groups(ids)
        role = sched.account_role(account)
        ids = sched.select_groups_for_account(role, grading.get("WHITE", []), grading.get("GREY", []))
        black = set(grading.get("BLACK", []))
        ids = [g for g in ids if g not in black]
    random.shuffle(ids)
    total = len(ids)
    success = 0
    failed = 0
    base_delay_ms = max(delay_ms, 0)
    min_delay_ms = max(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500), 0)
    jitter_pct = max(0.0, min(getattr(CONFIG, "SEND_JITTER_PCT", 0.15), 0.5))
    base = max(base_delay_ms, min_delay_ms)
    # 梯队要求的最小间隔（0 = 不额外限制）。刚毕业的新号比老号慢，
    # 成熟档是 0，老号仍然只听任务自己的 delay_ms，存量任务节奏不变。
    try:
        tier_interval_s = float(warmup.interval_for(account))
    except Exception:
        tier_interval_s = 0.0
    gap_s = max(base / 1000.0, tier_interval_s)
    rate = (1.0 / gap_s) if gap_s > 0 else 0.0
    for gid in ids:
        skipped = _should_skip(account, gid, message, parse_mode, disable_web_page_preview)
        msg_id = None
        err = None
        if skipped:
            status = "skipped"
        else:
            if use_sched and sched.should_pause_account(account):
                status = "skipped"
                err = "account_paused"
                msg_id = None
                preview = message[:200]
                title = await manager.get_group_title(account, gid)
                db.add(
                    SendLog(
                        account_name=account,
                        group_id=gid,
                        group_title=title,
                        message_preview=preview,
                        status=status,
                        error=None if status == "success" else (err or ("" if status == "skipped" else "send_failed")),
                        message_id=msg_id,
                        parse_mode=parse_mode,
                        created_at=CONFIG.now(),
                    )
                )
                db.commit()
                continue
            attempt = 0
            flood_retry = 0
            max_flood_retry = max(int(getattr(CONFIG, "MAX_FLOOD_RETRY", 5)), 0)
            ok = False
            while attempt <= max(0, retry_max):
                send_text = message
                if use_sched:
                    send_text = sched.fingerprint_message(message, parse_mode if parse_mode != "plain" else None)
                ok, err, msg_id = await manager.send_message_to_group(
                    account,
                    group_id=gid,
                    text=send_text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=disable_web_page_preview,
                )
                if ok:
                    break
                
                # 处理 FloodWait 错误
                wait_seconds = _flood_wait_seconds(err)
                if wait_seconds is not None:
                    # 需要等太久就别干等，这一轮先放过这个群
                    if wait_seconds > 300:
                        break
                    # 限制连续 FloodWait 等待次数，避免该分支永不递增
                    # attempt 导致 retry_max=0 时无限循环卡住
                    flood_retry += 1
                    if flood_retry > max_flood_retry:
                        break
                    await asyncio.sleep(wait_seconds)
                else:
                    attempt += 1
                    if attempt <= retry_max:
                        await asyncio.sleep(max(retry_delay_ms, 0) / 1000.0)
                        
            status = "success" if ok else "failed"
            if not ok and err:
                if should_exclude_group_on_error(err):
                    try:
                        global_scope = should_add_to_global_blist(err)
                        if not global_scope and needs_ban_vote(err):
                            # 「你被本群禁言」只先拉黑这个账号；同一个群被够多个账号
                            # 分别撞上，才认定是群不欢迎我们，整群下架。
                            votes = register_ban_vote(db, gid, account)
                            global_scope = votes >= GLOBAL_BAN_VOTES_REQUIRED
                        add_banned_group(db, account, gid, global_scope=global_scope)
                    except Exception:
                        pass
            if status == "success":
                success += 1
            elif status == "failed":
                failed += 1
        preview = message[:200]
        title = await manager.get_group_title(account, gid)
        db.add(
            SendLog(
                account_name=account,
                group_id=gid,
                group_title=title,
                message_preview=preview,
                status=status,
                error=None if status == "success" else (err or ("" if status == "skipped" else "send_failed")),
                message_id=msg_id,
                parse_mode=parse_mode,
                created_at=CONFIG.now(),
            )
        )
        db.commit()
        if rate > 0:
            # 令牌桶管住这个账号的稳态速率，再叠一层正向随机让节奏不像机器。
            # 随机只往上加（不会比 gap_s 更快），所以只会更保守。
            await _LIMITER.wait(account, rate=rate)
            if jitter_pct > 0:
                await human_delay(0.0, jitter_pct * gap_s)
    return {"total": total, "success": success, "failed": failed}
