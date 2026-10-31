import random
import re
from datetime import datetime, timezone
from typing import List, Dict, Tuple
from sqlalchemy.orm import Session
from app.models import SendLog
from app.config import CONFIG
from app.telegram_client import _extract_forward_source, _extract_inline_bot_query

def _recent_group_stats_many(
    db: Session, account: str, group_ids: List[int], window: int
) -> Dict[int, Tuple[int, int, int]]:
    """一次查完这批群的最近发送记录，返回 {群: (总数, 成功, 失败)}。

    原来是每个群一条 SQL，60 个群 × 每轮 = 几十次查询；动态任务每 10 分钟跑一轮，
    这些查询全是重复功。
    """
    stats: Dict[int, list] = {int(gid): [0, 0, 0] for gid in group_ids}
    if not stats:
        return {}
    rows = (
        db.query(SendLog.group_id, SendLog.status)
        .filter(SendLog.account_name == account, SendLog.group_id.in_(list(stats)))
        .order_by(SendLog.created_at.desc())
        # ponytail: 全局上限，极端情况下某个群记录特别多会让别的群少统计几条；
        # 误差只会让分类偏保守，不影响发送
        .limit(window * len(stats) * 2)
        .all()
    )
    for gid, status in rows:
        bucket = stats.get(int(gid))
        if bucket is None or bucket[0] >= window:
            continue
        bucket[0] += 1
        st = (status or "").lower()
        if st == "success":
            bucket[1] += 1
        elif st == "failed":
            bucket[2] += 1
    return {gid: (v[0], v[1], v[2]) for gid, v in stats.items()}


def classify_groups(db: Session, account: str, group_ids: List[int]) -> Dict[int, str]:
    window = int(getattr(CONFIG, "GROUP_RECENT_WINDOW_N", 20))
    fail_thr = float(getattr(CONFIG, "GROUP_BLACK_FAIL_RATE", 0.75))
    grey_low = float(getattr(CONFIG, "GROUP_GREY_LOW_FAIL_RATE", 0.3))
    black_min = int(getattr(CONFIG, "GROUP_BLACK_MIN_FAILS", 3))
    stats = _recent_group_stats_many(db, account, group_ids, window)
    res: Dict[int, str] = {}
    for gid in group_ids:
        n, s, f = stats.get(int(gid), (0, 0, 0))
        rate = (f / n) if n > 0 else 0.0
        if f >= black_min and rate >= fail_thr:
            res[gid] = "BLACK"
        elif n == 0:
            res[gid] = "GREY"
        elif rate >= grey_low:
            res[gid] = "GREY"
        else:
            res[gid] = "WHITE"
    return res

def _recent_account_stats(db: Session, account: str, window: int) -> Tuple[int, int]:
    rows = (
        db.query(SendLog)
        .filter(SendLog.account_name == account)
        .order_by(SendLog.created_at.desc())
        .limit(window)
        .all()
    )
    s = 0
    f = 0
    for r in rows:
        st = (r.status or "").lower()
        if st == "success":
            s += 1
        elif st == "failed":
            f += 1
    return s, f

def classify_account(db: Session, account: str) -> str:
    window = int(getattr(CONFIG, "ACCOUNT_RECENT_WINDOW_N", 50))
    safe_max_fail_rate = float(getattr(CONFIG, "ACCOUNT_SAFE_MAX_FAIL_RATE", 0.2))
    risk_min_fail_rate = float(getattr(CONFIG, "ACCOUNT_RISK_MIN_FAIL_RATE", 0.5))
    s, f = _recent_account_stats(db, account, window)
    n = max(1, s + f)
    rate = f / n
    if rate >= risk_min_fail_rate:
        return "RISK"
    if rate <= safe_max_fail_rate:
        return "SAFE"
    return "CORE"

def select_groups_for_account(role: str, group_ids: List[int], grades: Dict[int, str]) -> List[int]:
    whites = [g for g in group_ids if grades.get(g) == "WHITE"]
    greys = [g for g in group_ids if grades.get(g) == "GREY"]
    blacks = [g for g in group_ids if grades.get(g) == "BLACK"]
    if role == "SAFE":
        res = whites
        if not res:
            res = whites + greys
    elif role == "CORE":
        res = whites + greys
    else:
        max_probe = int(getattr(CONFIG, "RISK_MAX_GREY_PROBES", 2))
        random.shuffle(greys)
        res = whites + greys[:max_probe]
    if not res:
        res = [g for g in group_ids if g not in blacks]
    random.shuffle(res)
    return res

def dynamic_delay_ms(base_ms: int, recent_fail_rate: float) -> int:
    min_delay = int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    max_multiplier = float(getattr(CONFIG, "DELAY_MAX_MULTIPLIER", 3.0))
    factor = 1.0 + min(max(recent_fail_rate, 0.0), 1.0) * (max_multiplier - 1.0)
    return int(max(min_delay, base_ms) * factor)

def randomize_message(text: str) -> str:
    if int(getattr(CONFIG, "CONTENT_FINGERPRINT_ENABLED", 1)) == 0:
        return text
    if _extract_inline_bot_query(text)[0] or _extract_forward_source(text)[0] is not None:
        return text
    t = text
    
    # 1. 基础替换
    if random.random() < 0.6:
        t = re.sub(r"[ \t]{2,}", " ", t)
    if random.random() < 0.5:
        t = re.sub(r"\n{2,}", "\n", t)
        
    # 2. 零宽字符注入 (Zero-width injection) - 最有效的反 Hash
    # \u200b (Zero Width Space), \u200c (Zero Width Non-Joiner), \u200d (Zero Width Joiner), \u2060 (Word Joiner)
    zw_chars = ["\u200b", "\u200c", "\u200d", "\u2060"]
    if random.random() < 0.8:  # 80% 概率启用
        chars = list(t)
        # 随机插入 1-3 个零宽字符
        num_inserts = random.randint(1, 3)
        for _ in range(num_inserts):
            if len(chars) > 0:
                pos = random.randint(0, len(chars))
                chars.insert(pos, random.choice(zw_chars))
        t = "".join(chars)

    # 3. 尾部 Emoji 统一由 telegram_client.append_send_suffix 在真正发送时追加，
    #    这里不再重复加，否则一条消息结尾会挂两个 emoji。

    return t

def recent_fail_rate(db: Session, account: str, window: int) -> float:
    s, f = _recent_account_stats(db, account, window)
    n = max(1, s + f)
    return f / n


def unique_group_ids(group_ids: List[int]) -> List[int]:
    seen = set()
    res: List[int] = []
    for gid in group_ids:
        try:
            value = int(gid)
        except Exception:
            continue
        if value in seen:
            continue
        seen.add(value)
        res.append(value)
    return res


def _latest_group_activity_map(db: Session, group_ids: List[int]) -> Dict[int, datetime]:
    rows = (
        db.query(SendLog)
        .filter(SendLog.group_id.in_(group_ids))
        .order_by(SendLog.created_at.desc())
        .all()
    )
    latest: Dict[int, datetime] = {}
    for row in rows:
        gid = int(row.group_id)
        if gid not in latest and row.created_at is not None:
            latest[gid] = row.created_at
    return latest


def _group_outcome_score_map(rows) -> Dict[int, int]:
    scores: Dict[int, int] = {}
    for row in rows:
        try:
            gid = int(row.group_id)
        except Exception:
            continue
        status = (row.status or "").lower()
        if status == "success":
            scores[gid] = scores.get(gid, 0) + 2
        elif status == "failed":
            scores[gid] = scores.get(gid, 0) - 1
    return scores


def _self_check_group_outcome_scores() -> None:
    class _Row:
        def __init__(self, group_id, status):
            self.group_id = group_id
            self.status = status

    scores = _group_outcome_score_map([
        _Row(1, "success"),
        _Row(1, "failed"),
        _Row(2, "failed"),
    ])
    assert scores[1] > scores[2]


_self_check_group_outcome_scores()


def sort_groups_for_account(db: Session, account: str, group_ids: List[int]) -> List[int]:
    ids = unique_group_ids(group_ids)
    if not ids:
        return []
    account_rows = (
        db.query(SendLog)
        .filter(SendLog.account_name == account, SendLog.group_id.in_(ids))
        .order_by(SendLog.created_at.desc())
        .all()
    )
    global_rows = (
        db.query(SendLog)
        .filter(SendLog.group_id.in_(ids))
        .order_by(SendLog.created_at.desc())
        .limit(max(len(ids) * 8, 200))
        .all()
    )
    account_latest: Dict[int, datetime] = {}
    for row in account_rows:
        gid = int(row.group_id)
        if gid not in account_latest and row.created_at is not None:
            account_latest[gid] = row.created_at
    account_scores = _group_outcome_score_map(account_rows)
    global_latest = _latest_group_activity_map(db, ids)
    global_scores = _group_outcome_score_map(global_rows)
    rng = random.Random(f"{account}:{len(ids)}")
    jitter = {gid: rng.random() for gid in ids}

    def tier(gid: int) -> int:
        account_score = account_scores.get(gid, 0)
        global_score = global_scores.get(gid, 0)
        if account_score > 0:
            return 0
        if global_score > 0:
            return 1
        if account_score < 0 or global_score < 0:
            return 3
        return 2

    return sorted(
        ids,
        key=lambda gid: (
            tier(gid),
            -account_scores.get(gid, 0),
            -global_scores.get(gid, 0),
            account_latest.get(gid) is not None,
            account_latest.get(gid) or datetime.min,
            global_latest.get(gid) is not None,
            global_latest.get(gid) or datetime.min,
            jitter[gid],
        ),
    )


