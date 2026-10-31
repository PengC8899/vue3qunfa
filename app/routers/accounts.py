from starlette.responses import JSONResponse
from starlette.requests import Request
from telethon.errors.rpcerrorlist import AboutTooLongError, FirstNameInvalidError
import asyncio
from app.config import CONFIG
from app.services.account_service import account_service
from app.services.proxy_service import (
    assign_proxies_from_source,
    DEFAULT_PROXY_SOURCE,
    get_default_proxy_source,
    list_proxy_bindings,
    set_default_proxy_source,
    upsert_proxy_binding,
)
from app.database import SessionLocal
from app.telegram_client import multi_manager
from app.services import spintax, warmup
import os
import glob


def _is_authorized_token(request: Request) -> bool:
    return request.headers.get("X-Admin-Token") == CONFIG.ADMIN_TOKEN


def _translate_profile_update_error(exc: Exception) -> str:
    err_text = str(exc)
    if "FROZEN_METHOD_INVALID" in err_text:
        return "该账号当前被 Telegram 限制，暂时不能修改昵称或简介"
    if isinstance(exc, AboutTooLongError):
        return "账号简介过长，请控制在 70 个字符以内"
    if isinstance(exc, FirstNameInvalidError):
        return "账号昵称不合法，请换一个昵称再试"
    return str(exc)


def _discover_authorized_accounts() -> list[str]:
    session_dir = CONFIG.SESSION_DIR
    files = glob.glob(os.path.join(session_dir, "*.session"))
    names = []
    for path in files:
        name = os.path.basename(path)[:-8]
        if name and name not in names:
            names.append(name)
    return names


async def get_account_profile(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    session_path = os.path.join(CONFIG.SESSION_DIR, f"{account}.session")
    if not os.path.exists(session_path):
        return JSONResponse({"detail": "session_not_found"}, status_code=404)

    try:
        profile = await multi_manager.get_profile(account)
        return JSONResponse({"ok": True, **profile})
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=400)


async def get_authorized_profiles(request: Request):
    """Read cached profiles; explicit health checks perform network operations."""
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    accounts = _discover_authorized_accounts()
    with SessionLocal() as db:
        proxy_map = list_proxy_bindings(db, accounts)
    # 梯队/预热进度跟着账号列表一起返回，前端不用为每个账号再发一次请求
    warmup_map = {item["account"]: item for item in warmup.snapshot(accounts)}
    profiles, failed_profiles = [], []
    for account in accounts:
        result = account_service.cached_status(account)
        item = {
            "account": account, "ok": bool(result.get("valid")),
            "phone": result.get("phone"),
            "nickname": result.get("first_name") or result.get("username") or "",
            "about": result.get("about") or "", "proxy": proxy_map.get(account),
            "status": result.get("status"), "checked_at": result.get("checked_at"),
            "stale": result.get("stale", False), "error": result.get("detail"),
            "warmup": warmup_map.get(account),
        }
        (profiles if item["ok"] else failed_profiles).append(item)
    return JSONResponse({"ok": True, "accounts_total": len(accounts),
                         "profiles": profiles, "failed_profiles": failed_profiles})


async def update_account_profile(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    nickname = (body.get("nickname") or "").strip()
    about = body.get("about")

    if not nickname:
        return JSONResponse({"detail": "Missing nickname"}, status_code=400)
    if len(nickname) > 64:
        return JSONResponse({"detail": "nickname_too_long"}, status_code=400)
    if about is not None:
        about = str(about).strip()
        if len(about) > 70:
            return JSONResponse({"detail": "about_too_long"}, status_code=400)

    accounts = _discover_authorized_accounts()
    if not accounts:
        return JSONResponse({"detail": "no_authorized_accounts"}, status_code=400)

    results = {}
    success_count = 0
    failed_count = 0
    sample_profile = None

    for account in accounts:
        try:
            profile = await multi_manager.update_text_profile(account, nickname, about)
            results[account] = {"ok": True, "profile": profile}
            success_count += 1
            if sample_profile is None:
                sample_profile = profile
        except Exception as e:
            results[account] = {"ok": False, "error": _translate_profile_update_error(e)}
            failed_count += 1

    return JSONResponse(
        {
            "ok": success_count > 0,
            "nickname": nickname,
            "about": about or "",
            "accounts_total": len(accounts),
            "success_count": success_count,
            "failed_count": failed_count,
            "results": results,
            "sample_profile": sample_profile,
        }
    )


async def get_account_proxy(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    account = (request.query_params.get("account") or "").strip()
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    db = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, [account])
        return JSONResponse({"ok": True, "account": account, "proxy": proxy_map.get(account)})
    finally:
        db.close()


async def update_account_proxy(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    account = (body.get("account") or "").strip()
    proxy_url = (body.get("proxy_url") or "").strip()
    enabled = bool(body.get("enabled", True))
    if not account:
        return JSONResponse({"detail": "Missing account"}, status_code=400)

    db = SessionLocal()
    try:
        try:
            proxy_data = upsert_proxy_binding(db, account, proxy_url, enabled=enabled)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
    finally:
        db.close()

    try:
        await multi_manager.refresh_account_client(account)
    except Exception:
        pass

    return JSONResponse({"ok": True, "account": account, "proxy": proxy_data})


async def get_default_account_proxy_source(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    db = SessionLocal()
    try:
        source = get_default_proxy_source(db)
        return JSONResponse({"ok": True, "proxy_source_text": source, "default_proxy_source": DEFAULT_PROXY_SOURCE})
    finally:
        db.close()


async def update_default_account_proxy_source(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    source_value = body.get("proxy_source_text")
    db = SessionLocal()
    try:
        saved = set_default_proxy_source(db, source_value)
        return JSONResponse({"ok": True, "proxy_source_text": saved, "default_proxy_source": DEFAULT_PROXY_SOURCE})
    finally:
        db.close()


async def auto_assign_account_proxies(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    raw_accounts = body.get("accounts") or []
    proxy_source_text = body.get("proxy_source_text") or body.get("proxy_source") or ""
    if not str(proxy_source_text).strip():
        return JSONResponse({"detail": "proxy_source_text required"}, status_code=400)

    accounts: list[str] = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            name = str(item or "").strip()
            if name and name not in accounts:
                accounts.append(name)
    if not accounts:
        accounts = _discover_authorized_accounts()
    if not accounts:
        return JSONResponse({"detail": "no_authorized_accounts"}, status_code=400)

    result = await asyncio.to_thread(assign_proxies_from_source, accounts, str(proxy_source_text))
    for account in result["assigned_accounts"]:
        try:
            await multi_manager.refresh_account_client(account)
        except Exception:
            pass

    return JSONResponse(
        {
            "ok": result["assigned_total"] > 0,
            "accounts": accounts,
            **result,
        }
    )

async def check_single_account(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
        include_group_send_check = bool(body.get("include_group_send_check", True))
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    
    try:
        # #region debug-point C:check-single-route-start
        route_started_at = asyncio.get_running_loop().time()
        account_service_module = __import__("app.services.account_service", fromlist=["_report_health_check_debug"])
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:start", "check_single_account.start", {
            "account": account,
            "include_group_send_check": include_group_send_check,
        })
        # #endregion
        result = await account_service.check_account(account, include_group_send_check=include_group_send_check)
        # #region debug-point C:check-single-route-finish
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:finish", "check_single_account.finish", {
            "account": account,
            "elapsedMs": int((asyncio.get_running_loop().time() - route_started_at) * 1000),
            "status": result.get("status"),
            "valid": result.get("valid"),
        })
        # #endregion
        return JSONResponse(result)
    except Exception as e:
        # #region debug-point C:check-single-route-error
        account_service_module._report_health_check_debug("C", "app/routers/accounts.py:check_single_account:error", "check_single_account.error", {
            "account": account,
            "error": str(e)[:120],
        })
        # #endregion
        return JSONResponse(
            {"account": account, "status": "error", "valid": False, "detail": str(e)[:120]},
            status_code=200,
        )


async def get_warmup_overview(request: Request):
    """预热总览：每个账号的梯队 + readiness，以及当前批次跑到哪了。"""
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    accounts = _discover_authorized_accounts()
    return JSONResponse({
        "ok": True,
        "enabled": warmup.warmup_enabled(),
        "ready_score": max(1, int(getattr(CONFIG, "WARMUP_READY_SCORE", 60))),
        "tiers": warmup.TIER_SPECS,
        "progress": warmup.progress(),
        "accounts": warmup.snapshot(accounts),
    })


async def start_warmup(request: Request):
    """开始预热。body 里给 accounts 就只预热这些，不给就是全部未毕业的账号。"""
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    accounts = body.get("accounts")
    if not accounts:
        # 不指定就是「还没毕业的那些」，已经毕业/豁免的账号没必要再养
        accounts = warmup.pending_accounts(_discover_authorized_accounts())
    result = warmup.start(multi_manager, accounts)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


async def stop_warmup(request: Request):
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return JSONResponse(warmup.stop())


async def validate_spintax_message(request: Request):
    """文案变体预览：告诉用户这条文案能生成多少种不同文本。"""
    if not _is_authorized_token(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    message = body.get("message") or ""
    result = spintax.validate(message)
    result["enabled"] = bool(int(getattr(CONFIG, "SPINTAX_ENABLED", 1)))
    return JSONResponse(result)
