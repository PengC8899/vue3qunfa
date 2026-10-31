from app.telegram_client import MultiTelegramManager
from sqlalchemy.orm import Session
from app.models import GroupCache, SystemKV
from app.config import CONFIG
from typing import Optional
import json
import time

_GROUP_CACHE: dict[tuple[str, bool], dict] = {}
_CACHE_TTL_SECONDS = getattr(CONFIG, "GROUP_CACHE_TTL_SECONDS", 600)

_PERMANENT_ERROR_MARKERS = (
    "banned from sending messages",
    "user_banned_in_channel",
    "chat_write_forbidden",
    "chat_send_plain_forbidden",
    "chat_send_media_forbidden",
    "chat_send_photos_forbidden",
    "chat_send_videos_forbidden",
    "chat_send_roundvideos_forbidden",
    "chat_send_docs_forbidden",
    "chat_send_gifs_forbidden",
    "chat_send_stickers_forbidden",
    "chat_send_voices_forbidden",
    "chat_send_audios_forbidden",
    "chat_send_polls_forbidden",
    "you cannot send inline results in this chat",
    # 「本群不允许发某类结果」是群自己的限制：转发图集、发链接卡片都会撞上，兜底也过不去
    "results in this chat",
    "chat_forwards_restricted",
    "topic_closed",
    "allow_payment_required",
    "peer error",
    "invalid peer",
    "could not find the input entity",
    "peer_is_user",
    "the chat is restricted and cannot be used in that request",
    "chat restricted",
    "channel_private",
    # Telethon 实际抛出的文本：这个号已经不在群里（被踢/链接已失效），换号补位
    "channel specified is private",
    "you lack permission to access it",
    "session database corrupted",
    "not authorized",
)

_GLOBAL_ERROR_MARKERS = (
    "could not find the input entity",
    "invalid peer",
    "peer error",
    "channel_private",
    "the chat is restricted and cannot be used in that request",
    "chat restricted",
    # 下面这些是「这个群本来就不让发」的属性，不是某个账号的问题：
    # 一个账号撞上就够了，没必要让其余 77 个账号再逐个试一遍
    "results in this chat",
    "chat_forwards_restricted",
    "topic_closed",
    "allow_payment_required",
    "chat_write_forbidden",
    "chat_send_",
)

# 「这个账号在这个群被禁言」是 (账号, 群) 的属性，不是群的属性：一个账号撞上就整群下架，
# 会把绝大多数目标误杀（8003 实测 507 个群被下架 494 个，全库只剩 2 个可发组合，
# 半天只发出 3 条）。但同一个群被好几个账号分别撞上，就说明这个群确实不欢迎我们这批账号，
# 才值得整群下架——所以这类错误改成「先只拉黑当事账号，够票数才整群下架」。
_GLOBAL_BAN_VOTE_MARKERS = (
    "user_banned_in_channel",
)
_BAN_VOTE_PREFIX = "ban_vote:"
# 够几个不同账号在本群撞上「你被禁言」才整群下架。1 就是老行为（一次即下架）。
GLOBAL_BAN_VOTES_REQUIRED = max(1, int(getattr(CONFIG, "GLOBAL_BAN_VOTES_REQUIRED", 3)))

_TEMPORARY_ERROR_MARKERS = (
    "floodwait",
    "a wait of",
    "seconds is required",
    "too many requests",
    "retry after",
    "timeout",
    "network error",
    "connection reset",
    "connection refused",
)

def _normalize_group_items(data: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for item in data or []:
        if not isinstance(item, dict):
            continue
        g = dict(item)
        try:
            gid = int(g.get("id"))
        except Exception:
            normalized.append(g)
            continue
        if "raw_id" not in g and gid > 0:
            g["raw_id"] = gid
            if bool(g.get("is_megagroup")) or bool(g.get("is_channel")):
                g["id"] = -(1000000000000 + gid)
            else:
                g["id"] = -gid
        normalized.append(g)
    return normalized

def should_exclude_group_on_error(error_text: str | None) -> bool:
    if not error_text:
        return False
    err = str(error_text).lower()
    return any(marker in err for marker in _PERMANENT_ERROR_MARKERS)

def classify_error_type(error_text: str | None) -> str:
    """分类错误类型：permanent(永久), temporary(临时), unknown(未知)"""
    if not error_text:
        return "unknown"

    err = str(error_text).lower()
    if any(marker in err for marker in _PERMANENT_ERROR_MARKERS):
        return "permanent"
    if any(marker in err for marker in _TEMPORARY_ERROR_MARKERS):
        return "temporary"
    return "unknown"


def should_add_to_blist(error_text: str | None) -> bool:
    return classify_error_type(error_text) == "permanent"


def should_add_to_global_blist(error_text: str | None) -> bool:
    """要不要直接把这个群拉进全局黑名单（所有账号都不再发它）。

    只看错误文本是不是「群自身属性」（群不存在、群禁言、群禁发链接……）。
    「某个账号在这个群被禁言」不算，那个走 needs_ban_vote 投票。
    """
    if not error_text:
        return False
    err = str(error_text).lower()
    return any(marker in err for marker in _GLOBAL_ERROR_MARKERS)


def needs_ban_vote(error_text: str | None) -> bool:
    """是不是「先记一票、够票数才整群下架」的错误（账号在这个群被禁言）。"""
    if not error_text:
        return False
    err = str(error_text).lower()
    return any(marker in err for marker in _GLOBAL_BAN_VOTE_MARKERS)


def is_mute_error(error_text: str | None) -> bool:
    """这个号在这个群发不了言（禁言 / 禁发），不是群本身不可用。"""
    if needs_ban_vote(error_text):
        return True
    err = str(error_text or "").lower()
    return any(
        marker in err
        for marker in (
            "banned from sending messages",
            "chat_write_forbidden",
            "chatwriteforbidden",
            "you cannot write in this chat",
        )
    )


def is_member_lost_error(error_text: str | None) -> bool:
    """这个号已经不在群里了（被踢 / 群转私有），成员关系要摘掉。"""
    err = str(error_text or "").lower()
    return any(
        marker in err
        for marker in (
            "channel specified is private",
            "you lack permission to access it",
            "channel_private",
            "channelprivate",
        )
    )


def register_ban_vote(db: Session, gid: int, account: str) -> int:
    """给「这个群禁言了某个账号」记一票，返回已投票的账号数。

    按群分开存（`ban_vote:<gid>`），避免一个 key 反复读改写。
    """
    key = _BAN_VOTE_PREFIX + str(int(gid))
    row = db.query(SystemKV).filter(SystemKV.k == key).first()
    voters: set[str] = set()
    if row and row.v:
        try:
            voters = {str(x) for x in json.loads(row.v)}
        except Exception:
            voters = set()
    voters.add(account)
    payload = json.dumps(sorted(voters), ensure_ascii=False)
    if row:
        row.v = payload
    else:
        db.add(SystemKV(k=key, v=payload))
    db.commit()
    return len(voters)


def get_banned_map(db: Session, accounts: list[str]) -> dict[str, set[int]]:
    """一次读出「全局 + 每个账号」的黑名单。

    方案生成时每个群都要按账号过滤一次，逐个查库会变成 账号数 × 2 次查询。
    """
    glob: set[int] = set()
    per: dict[str, set[int]] = {}
    rows = db.query(SystemKV).filter(SystemKV.k.like("banned_groups%")).all()
    for row in rows:
        try:
            ids = {int(x) for x in json.loads(row.v or "[]")}
        except Exception:
            continue
        name = row.k.split(":", 1)[1] if ":" in (row.k or "") else ""
        if name == "global":
            glob |= ids
        else:
            per.setdefault(name, set()).update(ids)
    return {name: per.get(name, set()) | glob for name in accounts}

def get_banned_group_ids(db: Session, account: Optional[str] = None) -> list[int]:
    keys = ["banned_groups:global"]
    if account:
        keys.append(f"banned_groups:{account}")
    collected: set[int] = set()
    for key in keys:
        row = db.query(SystemKV).filter(SystemKV.k == key).first()
        if not row or not (row.v or "").strip():
            continue
        try:
            data = json.loads(row.v)
            if isinstance(data, list):
                for item in data:
                    text = str(item)
                    if text.lstrip("-").isdigit():
                        collected.add(int(text))
        except Exception:
            continue
    return sorted(collected)

def add_banned_group(db: Session, account: str, gid: int, global_scope: bool = False, commit: bool = True):
    key = "banned_groups:global" if global_scope else f"banned_groups:{account}"
    gids = set(get_banned_group_ids(db, None if global_scope else account))
    gids.add(int(gid))
    payload = json.dumps(sorted(gids), ensure_ascii=False)
    row = db.query(SystemKV).filter(SystemKV.k == key).first()
    if row:
        row.v = payload
    else:
        row = SystemKV(k=key, v=payload)
        db.add(row)
    if commit:
        db.commit()
    for k in list(_GROUP_CACHE.keys()):
        acc, og = k
        if global_scope or acc == account:
            _GROUP_CACHE[k]["ts"] = 0.0

async def get_groups(manager: MultiTelegramManager, account: str, only_groups: bool = True, refresh: bool = False, db: Session | None = None):
    key = (account, bool(only_groups))
    if not refresh:
        c = _GROUP_CACHE.get(key)
        if c and (time.monotonic() - c.get("ts", 0)) < _CACHE_TTL_SECONDS:
            data = _normalize_group_items(c.get("data", []))
            if db is not None:
                banned = set(get_banned_group_ids(db, account))
                if banned:
                    data = [g for g in data if int(g.get("id")) not in banned]
            return data
        if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
            row = (
                db.query(GroupCache)
                .filter(GroupCache.account_name == account, GroupCache.only_groups == (1 if only_groups else 0))
                .order_by(GroupCache.updated_at.desc())
                .first()
            )
            if row:
                try:
                    data = _normalize_group_items(json.loads(row.data_json or "[]"))
                    if db is not None:
                        banned = set(get_banned_group_ids(db, account))
                        if banned:
                            data = [g for g in data if int(g.get("id")) not in banned]
                    _GROUP_CACHE[key] = {"data": data, "ts": time.monotonic()}
                    return data
                except Exception:
                    pass
    data = _normalize_group_items(await manager.get_joined_groups(account, only_groups=only_groups))
    if db is not None:
        banned = set(get_banned_group_ids(db, account))
        if banned:
            data = [g for g in data if int(g.get("id")) not in banned]
    _GROUP_CACHE[key] = {"data": data, "ts": time.monotonic()}
    if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
        try:
            payload = json.dumps(data, ensure_ascii=False)
            row = (
                db.query(GroupCache)
                .filter(GroupCache.account_name == account, GroupCache.only_groups == (1 if only_groups else 0))
                .first()
            )
            if row:
                row.data_json = payload
            else:
                row = GroupCache(account_name=account, only_groups=(1 if only_groups else 0), data_json=payload)
                db.add(row)
            db.commit()
        except Exception:
            pass
    return data

def clear_group_cache(account: Optional[str] = None, only_groups: Optional[bool] = None, db: Session | None = None):
    keys = list(_GROUP_CACHE.keys())
    removed = 0
    for k in keys:
        acc, og = k
        if (account is None or acc == account) and (only_groups is None or og == bool(only_groups)):
            if k in _GROUP_CACHE:
                del _GROUP_CACHE[k]
                removed += 1
    if db is not None and getattr(CONFIG, "GROUP_CACHE_ENABLED", 1):
        q = db.query(GroupCache)
        if account is not None:
            q = q.filter(GroupCache.account_name == account)
        if only_groups is not None:
            q = q.filter(GroupCache.only_groups == (1 if only_groups else 0))
        q.delete(synchronize_session=False)
        db.commit()
    return {"removed_memory": removed}
