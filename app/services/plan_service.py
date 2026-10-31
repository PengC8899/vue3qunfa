"""账号可用性判定 + 群池重检。

发送链路只认数字 group_id，所以重检时顺带把目标群组池里的 @username 解析成数字 id 并缓存回库，
解析失败即「群组不可访问」。
"""
import asyncio
import os
import time

from sqlalchemy.orm import Session

from app.models import TargetGroup
from app.services.account_service import account_service
from app.services import warmup
from app.telegram_client import multi_manager

READY_STATUSES = {"ok", "slow"}
# 群池导入后每个群都要发一次真实 Telegram RPC 才能拿到数字 id（含代理握手，
# 冷连接实测单群平均 4.7s、最慢 23s）。并发写死 4 时，507 个群 = 127 个串行波次
# ≈ 10 分钟，前端 5 分钟超时后还报「生成方案失败」——用户看到的就是「卡死」。
# 一个群只落在一个账号上、账号之间一号一代理，所以并发可以直接对齐可用账号规模。
RESOLVE_CONCURRENCY = max(1, int(os.getenv("RESOLVE_CONCURRENCY", "24")))
# 冷连接实测最慢 13.3s，20s 会把慢群直接判成「不可访问」并清空 group_id，
# 于是这些群每跑一次方案就要重试一次，永远好不了。
RESOLVE_TIMEOUT_S = max(5.0, float(os.getenv("RESOLVE_TIMEOUT_S", "45")))

# 检测结果超过这么久就不再采信。cached_status 里的 stale 只是 60 秒的内存缓存标志，
# 任何超过 1 分钟的快照都算 stale，拿它当门槛会把全部账号挡光；这里要的是一个
# 真能反映「账号现在还在不在」的时间窗。一次加群/发送动辄几百个目标，账号掉线
# 就是整批秒挂（8002 实测拿 16 分钟前的快照跑了 155 个群，41/41 报 AUTH_KEY_UNREGISTERED）。
STATUS_TRUST_SECONDS = 30 * 60

ACCOUNT_BLOCK_LABELS = {
    "unchecked": "尚未检测登录状态",
    "unknown": "状态未知",
    "pending": "等待检测",
    "missing_file": "Session 文件缺失",
    "banned": "已被封禁",
    "unauthorized": "登录已失效",
    "auth_error": "鉴权失败",
    "connect_failed": "连接失败",
    "connect_slow": "连接超时",
    "timeout": "检测超时",
    "error": "检测异常",
    "cannot_send": "无群发言权限",
    "send_banned": "被限制群组发言",
    "stale": "检测结果已过期，需重新检测",
    "warmup": "账号预热中，尚未毕业",
}

GROUP_INVITE_PENDING = "邀请链接需先加入"


def classify_ready_accounts(accounts: list[str]) -> tuple[list[str], dict[str, int]]:
    """只放行 READY 账号；其余按原因归类，方便前端直接显示「不可执行」明细。

    检测结果过期的账号不算可用，归类成「检测结果已过期」而不是「状态未知」，
    让用户知道该去点重新检测，而不是以为账号坏了。
    """
    ready: list[str] = []
    blocked: dict[str, int] = {}
    now = time.time()
    exempt = warmup.exempt_accounts()
    states = warmup.load_states(accounts)
    for name in accounts:
        result = account_service.cached_status(name)
        status = str(result.get("status") or "unchecked")
        if result.get("valid") and status in READY_STATUSES:
            checked_at = result.get("checked_at")
            if checked_at is not None and now - float(checked_at) < STATUS_TRUST_SECONDS:
                # 检测通过还不算数：新导入的号要先预热毕业才准进群发。
                # 有发送历史的老号走豁免，不受这条影响。
                allowed, _reason = warmup.gate_state(states[name], name in exempt)
                if not allowed:
                    label = ACCOUNT_BLOCK_LABELS["warmup"]
                else:
                    ready.append(name)
                    continue
            else:
                label = ACCOUNT_BLOCK_LABELS["stale"]
        else:
            label = ACCOUNT_BLOCK_LABELS.get(status, status)
        blocked[label] = blocked.get(label, 0) + 1
    return ready, blocked


async def resolve_groups(db: Session, groups: list[TargetGroup], accounts: list[str], force: bool = False) -> None:
    """把公开群解析成数字 id。多账号轮询分摊 FloodWait 风险。

    force=True 时忽略已缓存的 group_id，重新向 Telegram 确认可达性（「重新检测」用）。
    """
    pending = list(groups) if force else [g for g in groups if g.group_id is None]
    if not pending or not accounts:
        return
    sem = asyncio.Semaphore(RESOLVE_CONCURRENCY)

    async def one(index: int, group: TargetGroup):
        if str(group.status or "") == "approval_required":
            # 「需管理员审核」是群自己的设置，重新解析可达性改变不了它，
            # 不能因为一次重检就把标记冲掉、又拿账号去申请一遍。
            return
        if group.kind == "invite":
            group.status = "unreachable"
            group.last_error = GROUP_INVITE_PENDING
            return
        account = accounts[index % len(accounts)]
        async with sem:
            try:
                result = await asyncio.wait_for(
                    multi_manager.resolve_group_ref(account, group.username),
                    timeout=RESOLVE_TIMEOUT_S,
                )
            except Exception as exc:
                result = {"ok": False, "error": str(exc)[:120]}
        if result.get("ok"):
            group.group_id = int(result["id"])
            group.title = (result.get("title") or "")[:255] or None
            group.status = "available"
            group.last_error = None
        else:
            group.group_id = None
            group.status = "unreachable"
            group.last_error = result.get("error") or "resolve_failed"

    await asyncio.gather(*(one(i, g) for i, g in enumerate(pending)))
    db.commit()


async def recheck_pool(db: Session, accounts: list[str], ids: list[int] | None = None) -> dict:
    """重新检测目标群组的可达性。只更新群组池，不碰任务与账号。"""
    ready, _blocked = classify_ready_accounts(accounts)
    query = db.query(TargetGroup)
    if ids:
        query = query.filter(TargetGroup.id.in_([int(i) for i in ids]))
    groups = query.order_by(TargetGroup.id.asc()).all()
    if not groups:
        return {"ok": True, "checked": 0, "available": 0, "unreachable": 0, "accounts_ready": len(ready)}
    if not ready:
        return {
            "ok": False,
            "detail": "no_ready_accounts",
            "message": "没有可用账号，无法检测群组。请先到「健康检查」把账号检测成可用状态。",
            "checked": 0,
        }
    await resolve_groups(db, groups, ready, force=True)
    available = sum(1 for g in groups if g.group_id is not None)
    return {
        "ok": True,
        "checked": len(groups),
        "available": available,
        "unreachable": len(groups) - available,
        "accounts_ready": len(ready),
    }
