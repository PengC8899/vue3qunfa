from starlette.applications import Starlette
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.middleware.cors import CORSMiddleware
from app.config import CONFIG
from app.database import (
    Base,
    engine,
    export_task_migration_manifest,
    SessionLocal,
    export_task_runtime_snapshot,
    is_database_malformed_error,
    is_sqlite_enabled,
    sqlite_checkpoint,
    sqlite_ensure_task_runtime_indexes,
    sqlite_health_check,
)
from sqlalchemy import text, func
from sqlalchemy.exc import DatabaseError
from app.telegram_client import append_send_suffix, multi_manager, set_copy_receiver, set_on_private_message
from sqlalchemy.orm import Session
from app.models import AccountProxy, JoinLog, SendLog, SendPreset, TargetGroup, Task, TaskEvent, SystemKV
from app.services.send_service import send_to_groups
from app.services.group_service import (
    get_groups,
    clear_group_cache,
    add_banned_group,
    get_banned_group_ids,
    needs_ban_vote,
    is_mute_error,
    is_member_lost_error,
    register_ban_vote,
    should_add_to_blist,
    should_add_to_global_blist,
    GLOBAL_BAN_VOTES_REQUIRED,
)
from app.routers.accounts import (
    auto_assign_account_proxies,
    check_single_account,
    get_default_account_proxy_source,
    get_account_profile,
    get_account_proxy,
    get_authorized_profiles,
    get_warmup_overview,
    start_warmup,
    stop_warmup,
    update_default_account_proxy_source,
    update_account_proxy,
    update_account_profile,
    validate_spintax_message,
)
from app.routers.system import reset_system
from app.routers.group_pool import (
    import_target_groups,
    list_target_groups,
    delete_target_groups,
)
from app.services import account_service as account_service_module
from app.services.account_service import account_service
from app.services.plan_service import (
    classify_ready_accounts,
    recheck_pool,
)
from app.services import join_service, warmup
from app.services.dispatch_layer import classify_groups, classify_account, select_groups_for_account, dynamic_delay_ms, randomize_message, recent_fail_rate, unique_group_ids, sort_groups_for_account
from app.services.proxy_service import (
    assign_proxies_from_source,
    get_default_proxy_source,
    get_proxy_enforce,
    set_proxy_enforce,
    assign_proxy_pool_to_accounts,
    delete_proxy_binding,
    list_proxy_bindings,
    parse_proxy_input,
    resolve_proxy_source_text,
    upsert_proxy_binding,
)
import json
import time
import uuid
import asyncio
import os
import random
import shutil
import logging
import io
import zipfile
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
import tempfile

from app.services.session_import import (
    validate_session as validate_session_file, account_from_filename, MAX_SESSION_BYTES, MAX_BATCH_BYTES, MAX_SESSION_COUNT,
)

app = Starlette()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")
# 本地开发先跑后端、还没构建前端时 static/ 不存在：
# 建个空目录让 StaticFiles 不报错直接启动（页面会 404 到构建完成）。
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

def _normalize_account_names(accounts) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for account in accounts or []:
        name = str(account or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _discover_session_accounts() -> list[str]:
    session_dir = CONFIG.SESSION_DIR
    try:
        items = os.listdir(session_dir)
    except Exception:
        return []
    names: list[str] = []
    for filename in items:
        if not filename.endswith(".session"):
            continue
        if filename.endswith(".session-journal"):
            continue
        name = filename[:-8]
        if not name:
            continue
        names.append(name)
    return sorted(set(names))


def _candidate_accounts(accounts=None) -> list[str]:
    if accounts:
        return _normalize_account_names(accounts)
    session_dir = CONFIG.SESSION_DIR
    names: list[str] = []
    count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
    prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
    for i in range(1, count + 1):
        name = f"{prefix}_{i:02d}"
        if os.path.exists(os.path.join(session_dir, f"{name}.session")):
            names.append(name)
    names.extend(_discover_session_accounts())
    return _normalize_account_names(names)


async def _is_authorized_account(account: str, timeout_s: float | None = None) -> bool:
    # 不存在的 session 绝不要去连：Telethon 连接时会顺手新建一个空 session 文件，污染账号列表
    if not account or not os.path.exists(os.path.join(CONFIG.SESSION_DIR, f"{account}.session")):
        return False
    timeout = max(float(timeout_s or getattr(CONFIG, "ACCOUNT_AUTH_CHECK_TIMEOUT_S", 12)), 3.0)
    try:
        return bool(await asyncio.wait_for(multi_manager.is_authorized(account), timeout=timeout))
    except Exception:
        return False


async def _split_authorized_accounts(accounts, timeout_s: float | None = None, concurrency: int = 4) -> tuple[list[str], list[str]]:
    names = _normalize_account_names(accounts)
    if not names:
        return [], []
    sem = asyncio.Semaphore(max(1, int(concurrency or 1)))

    async def check(account: str) -> tuple[str, bool]:
        async with sem:
            ok = await _is_authorized_account(account, timeout_s=timeout_s)
            return account, ok

    results = await asyncio.gather(*(check(account) for account in names))
    authorized = [account for account, ok in results if ok]
    rejected = [account for account, ok in results if not ok]
    return authorized, rejected


def _self_check_account_helpers() -> None:
    assert _normalize_account_names(["acc1", "", "acc1", None, " acc2 "]) == ["acc1", "acc2"]


_self_check_account_helpers()


def _purge_account_tasks(db: Session, account_name: str) -> dict:
    task_ids = [row[0] for row in db.query(Task.id).filter(Task.account_name == account_name).all()]
    active_tasks = db.query(Task).filter(Task.account_name == account_name, Task.status == "running").count()
    events_deleted = 0
    tasks_deleted = 0
    if task_ids:
        events_deleted = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        tasks_deleted = (
            db.query(Task)
            .filter(Task.id.in_(task_ids))
            .delete(synchronize_session=False)
        )
        db.commit()
    return {
        "active_tasks_deleted": int(active_tasks or 0),
        "task_records_deleted": int(tasks_deleted or 0),
        "task_events_deleted": int(events_deleted or 0),
    }

PUBLIC_PATHS = ("/",)
PUBLIC_PATH_PREFIXES = ("/static",)


@app.middleware("http")
async def admin_token_middleware(request, call_next):
    path = request.url.path
    if (
        request.method != "OPTIONS"
        and path not in PUBLIC_PATHS
        and not path.startswith(PUBLIC_PATH_PREFIXES)
    ):
        if request.headers.get("X-Admin-Token") != CONFIG.ADMIN_TOKEN:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


@app.route("/")
async def index(request: Request):
    # 外壳页必须每次回源：否则浏览器缓存住旧 HTML，?v= 也就跟着停在旧值，页面一直跑旧前端
    return templates.TemplateResponse(
        "vue_index.html",
        {"request": request, "build_id": _frontend_build_id()},
        headers={"Cache-Control": "no-store"},
    )


def _frontend_build_id() -> str:
    """用打包产物的 mtime 当版本号，避免浏览器缓存到旧的前端。"""
    try:
        return str(int(os.path.getmtime(os.path.join("static", "dist", "app.js"))))
    except OSError:
        return "dev"

@app.route("/api/accounts/status")
async def list_accounts_status(request: Request):
    session_dir = CONFIG.SESSION_DIR
    discovered = _discover_session_accounts()
    names: list[str] = list(discovered)
    seen = set(names)
    db: Session = SessionLocal()
    try:
        running_rows = db.query(Task.account_name).filter(Task.status == "running").all()
    finally:
        db.close()
    for row in running_rows:
        acc = (row[0] or "").strip()
        if acc and acc not in seen:
            names.append(acc)
            seen.add(acc)
    if not names:
        for name in list(getattr(CONFIG, "ACCOUNTS", {}).keys()):
            n = (name or "").strip()
            if n and n not in seen:
                names.append(n)
                seen.add(n)
    db = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, names)
    finally:
        db.close()
    # 梯队/预热进度跟列表一起返回，前端不用为每个账号再发一次请求
    warmup_map = {item["account"]: item for item in warmup.snapshot(names)}

    async def inspect_one(name: str) -> dict:
        session_path = os.path.join(session_dir, f"{name}.session")
        session_exists = os.path.exists(session_path)
        if not session_exists:
            return {
                "account": name,
                "authorized": False,
                "status": "missing_file",
                "detail": "Session 文件不存在",
                "proxy": proxy_map.get(name),
                "has_proxy": bool(proxy_map.get(name)),
                "warmup": warmup_map.get(name),
            }
        result = account_service.cached_status(name)
        status = result.get("status") or "unknown"
        detail = result.get("detail")
        if not detail:
            if status == "ok":
                detail = "账号可用"
            elif status == "unauthorized":
                detail = "Telegram session 未授权"
            elif status == "banned":
                detail = "账号已被 Telegram 封禁"
            elif status == "connect_failed":
                detail = "连接 Telegram 失败"
        return {
            "account": name,
            "authorized": bool(result.get("valid")) and status not in {"cannot_send", "unauthorized", "banned", "auth_error", "connect_failed", "error", "unknown", "missing_file"},
            "status": status,
            "detail": detail,
            "phone": result.get("phone"),
            "checked_at": result.get("checked_at"),
            "stale": result.get("stale", False),
            "proxy": proxy_map.get(name),
            "has_proxy": bool(proxy_map.get(name)),
            "warmup": warmup_map.get(name),
        }

    data = await asyncio.gather(*(inspect_one(name) for name in names))
    return JSONResponse(data)


@app.route("/api/dashboard/overview", methods=["GET"])
async def dashboard_overview(request: Request):
    """首页聚合接口：一次返回 KPI，避免打开首页连打十几个接口。只聚合，不复制业务逻辑。"""
    accounts = _discover_session_accounts()
    db: Session = SessionLocal()
    try:
        proxy_map = list_proxy_bindings(db, accounts)
        task_rows = db.query(Task.status, func.count(Task.id)).group_by(Task.status).all()
        log_rows = db.query(SendLog.status, func.count(SendLog.id)).group_by(SendLog.status).all()
        pool_rows = db.query(TargetGroup.status, func.count(TargetGroup.id)).group_by(TargetGroup.status).all()
        pool_total = db.query(func.count(TargetGroup.id)).scalar() or 0
    finally:
        db.close()

    ready = error = unchecked = 0
    for name in accounts:
        result = account_service.cached_status(name)
        status = result.get("status") or "unchecked"
        healthy = bool(result.get("valid")) and status not in {
            "cannot_send", "unauthorized", "banned", "auth_error", "connect_failed", "error",
            "missing_file",
        }
        if status in ("unchecked", "unknown", "pending"):
            unchecked += 1
        elif healthy:
            ready += 1
        else:
            error += 1

    by_status = {str(status or "unknown"): int(count) for status, count in task_rows}
    # 与前端 runningTaskCount 保持同一口径，不要另立一套「运行中」定义
    running = sum(by_status.get(k, 0) for k in ("running", "waiting_next_round", "scheduled"))

    return JSONResponse({
        "accounts": {
            "total": len(accounts),
            "ready": ready,
            "error": error,
            "unchecked": unchecked,
            "no_proxy": sum(1 for name in accounts if name not in proxy_map),
        },
        "tasks": {
            "running": running,
            "by_status": by_status,
        },
        "logs": {
            "by_status": {str(status or "unknown"): int(count) for status, count in log_rows},
        },
        "groups": {
            "total": int(pool_total),
            "by_status": {str(status or "pending"): int(count) for status, count in pool_rows},
        },
        "scheduler": {"status": "RUNNING" if running else "IDLE"},
    })


app.add_route("/api/accounts/check-single", check_single_account, methods=["POST"])
app.add_route("/api/accounts/profile", get_account_profile, methods=["GET"])
app.add_route("/api/accounts/profiles-authorized", get_authorized_profiles, methods=["GET"])
app.add_route("/api/accounts/profile", update_account_profile, methods=["POST"])
app.add_route("/api/accounts/proxy", get_account_proxy, methods=["GET"])
app.add_route("/api/accounts/proxy", update_account_proxy, methods=["POST"])
app.add_route("/api/accounts/proxy/default-source", get_default_account_proxy_source, methods=["GET"])
app.add_route("/api/accounts/proxy/default-source", update_default_account_proxy_source, methods=["POST"])
app.add_route("/api/accounts/proxy/auto-assign", auto_assign_account_proxies, methods=["POST"])
app.add_route("/api/accounts/warmup", get_warmup_overview, methods=["GET"])
app.add_route("/api/accounts/warmup/start", start_warmup, methods=["POST"])
app.add_route("/api/accounts/warmup/stop", stop_warmup, methods=["POST"])
app.add_route("/api/messages/spintax/validate", validate_spintax_message, methods=["POST"])
app.add_route("/api/groups/import", import_target_groups, methods=["POST"])
app.add_route("/api/groups/pool", list_target_groups, methods=["GET"])
app.add_route("/api/groups/pool/delete", delete_target_groups, methods=["POST"])


def _probe_proxy(raw_value: str, timeout: float = 12.0) -> dict:
    """通过指定代理请求一次 IP 回显接口，返回真实出口 IP。"""
    try:
        parsed = parse_proxy_input(raw_value)
    except Exception as exc:
        return {"proxy": raw_value, "ok": False, "error": f"代理格式错误: {exc}"}
    if not parsed:
        return {"proxy": raw_value, "ok": False, "error": "代理格式错误"}
    try:
        import socks as _socks
    except Exception:
        return {"proxy": raw_value, "ok": False, "error": "缺少 PySocks 依赖"}

    proxy_type = parsed["proxy_type"]
    if proxy_type in {"http", "https"}:
        sock_type = _socks.HTTP
    elif proxy_type == "socks4":
        sock_type = _socks.SOCKS4
    else:
        sock_type = _socks.SOCKS5

    sock = None
    try:
        sock = _socks.socksocket()
        sock.set_proxy(
            sock_type,
            parsed["host"],
            parsed["port"],
            username=parsed["username"] or None,
            password=parsed["password"] or None,
        )
        sock.settimeout(timeout)
        sock.connect(("api.ipify.org", 80))
        sock.sendall(
            b"GET /?format=text HTTP/1.1\r\n"
            b"Host: api.ipify.org\r\n"
            b"User-Agent: proxy-check/1.0\r\n"
            b"Connection: close\r\n\r\n"
        )
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", "ignore")
        body_text = raw.split("\r\n\r\n", 1)[-1].strip()
        exit_ip = body_text.splitlines()[-1].strip() if body_text else ""
        if exit_ip:
            return {"proxy": raw_value, "ok": True, "exit_ip": exit_ip, "error": None}
        return {"proxy": raw_value, "ok": False, "error": "代理无有效响应"}
    except Exception as exc:
        return {"proxy": raw_value, "ok": False, "error": f"{type(exc).__name__}: {str(exc)[:80]}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _proxy_policy_missing(db: Session, accounts: list[str]) -> list[str]:
    """「强制一账号一代理」开启时返回缺代理的账号；开关关闭时永远返回空列表。"""
    if not get_proxy_enforce(db):
        return []
    bound = set(list_proxy_bindings(db, accounts))
    return [name for name in accounts if name not in bound]


@app.route("/api/settings/proxy-policy", methods=["GET"])
async def get_proxy_policy(request: Request):
    db: Session = SessionLocal()
    try:
        return JSONResponse({"enforce_one_to_one": get_proxy_enforce(db)})
    finally:
        db.close()


@app.route("/api/settings/proxy-policy", methods=["POST"])
async def update_proxy_policy(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    db: Session = SessionLocal()
    try:
        enforce = set_proxy_enforce(db, bool(body.get("enforce_one_to_one")))
    finally:
        db.close()
    if enforce:
        # 已经连着的账号要断开：否则它们会继续以「策略打开前的旧连接」跑，
        # 新策略对它们等于没生效。
        await multi_manager.disconnect_all()
    return JSONResponse({"ok": True, "enforce_one_to_one": enforce})


@app.route("/api/proxies/overview", methods=["GET"])
async def proxies_overview(request: Request):
    """代理配置总览：默认代理源 + 每个账号的绑定与覆盖率。"""

    accounts = _discover_session_accounts()
    db: Session = SessionLocal()
    try:
        binding_map = list_proxy_bindings(db, accounts)
        default_source = get_default_proxy_source(db)
        enforce_one_to_one = get_proxy_enforce(db)
    finally:
        db.close()

    items = []
    unbound_accounts: list[str] = []
    for name in accounts:
        proxy = binding_map.get(name)
        if proxy:
            items.append({
                "account": name,
                "has_proxy": True,
                "enabled": bool(proxy.get("enabled", True)),
                "proxy": proxy,
            })
        else:
            items.append({"account": name, "has_proxy": False, "enabled": False, "proxy": None})
            unbound_accounts.append(name)

    return JSONResponse({
        "default_proxy_source": default_source,
        "enforce_one_to_one": enforce_one_to_one,
        "items": items,
        "total": len(accounts),
        "bound": len(accounts) - len(unbound_accounts),
        "unbound": len(unbound_accounts),
        "unbound_accounts": unbound_accounts,
        "topup_last": _load_topup_summary(),
    })


def _load_topup_summary() -> dict | None:
    raw = _kv_get(PROXY_TOPUP_KEY)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


@app.route("/api/proxies/topup", methods=["POST"])
async def proxies_topup(request: Request):
    """立即给缺代理的账号补绑（定时任务做的是同一件事）。"""
    summary = await _proxy_topup_once()
    return JSONResponse({"ok": True, **summary})


@app.route("/api/proxies/unassign", methods=["POST"])
async def proxies_unassign(request: Request):
    """解绑代理：指定账号列表，或用 all=true 清空全部绑定。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    db: Session = SessionLocal()
    removed_accounts: list[str] = []
    try:
        if bool(body.get("all")):
            rows = db.query(AccountProxy).all()
            removed_accounts = [row.account_name for row in rows]
            for row in rows:
                db.delete(row)
            db.commit()
        else:
            targets: list[str] = []
            seen: set[str] = set()
            for item in body.get("accounts") or []:
                name = str(item or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    targets.append(name)
            if not targets:
                return JSONResponse({"detail": "accounts required"}, status_code=400)
            for name in targets:
                if delete_proxy_binding(db, name):
                    removed_accounts.append(name)
    finally:
        db.close()

    for name in removed_accounts:
        try:
            await multi_manager.refresh_account_client(name)
        except Exception:
            pass

    return JSONResponse({
        "ok": True,
        "removed": len(removed_accounts),
        "accounts": removed_accounts,
    })


@app.route("/api/proxies/test", methods=["POST"])
async def proxies_test(request: Request):
    """批量测试代理连通性，返回每个代理的真实出口 IP。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    raw_list = body.get("proxy_urls")
    if not isinstance(raw_list, list):
        raw_list = [body.get("proxy_url")]
    targets: list[str] = []
    seen: set[str] = set()
    for item in raw_list:
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            targets.append(value)

    source_meta = None
    source_text = str(body.get("proxy_source_text") or "").strip()
    if source_text:
        source_errors: list[str] = []
        explicit_map, pool, source_meta = resolve_proxy_source_text(source_text, source_errors)
        for value in list(explicit_map.values()) + list(pool):
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                targets.append(cleaned)
        if source_errors:
            source_meta = {**(source_meta or {}), "errors": source_errors}

    if not targets:
        return JSONResponse({"detail": "proxy_url required"}, status_code=400)

    pool_total = len(targets)
    targets = targets[:50]

    loop = asyncio.get_running_loop()
    results = []
    for value in targets:
        results.append(await loop.run_in_executor(None, _probe_proxy, value))

    reachable = sum(1 for item in results if item.get("ok"))
    exit_ips = sorted({item["exit_ip"] for item in results if item.get("exit_ip")})
    return JSONResponse({
        "ok": reachable > 0,
        "total": len(results),
        "pool_total": pool_total,
        "reachable": reachable,
        "unique_exit_ips": len(exit_ips),
        "source_meta": source_meta,
        "results": results,
    })
app.add_route("/api/system/reset", reset_system, methods=["POST"])


def _normalize_session_schema(session_path: str) -> bool:
    """Use Telethon's own migration rather than forcing a schema version."""
    try:
        validate_session_file(session_path)
        return True
    except Exception as exc:
        logging.warning("Session schema validation failed: %s", type(exc).__name__)
        return False


@app.route("/api/accounts/upload-sessions", methods=["POST"])
async def upload_sessions(request: Request):
    """上传 session 文件"""
    
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"detail": "上传表单解析失败，请减少文件数量或检查文件格式"}, status_code=400)
    files = form.getlist("files")
    
    if not files:
        return JSONResponse({"detail": "No files uploaded"}, status_code=400)
    
    uploaded = 0
    duplicate = 0
    invalid = 0
    errors = []
    validated_accounts = []
    no_proxy_accounts: list[str] = []
    uploaded_accounts: list[str] = []
    pending_sessions: list[tuple[str, bytes]] = []
    proxy_bindings_raw = form.get("proxy_bindings")
    proxy_source_text = form.get("proxy_source_text")
    resolved_proxy_source_text = str(proxy_source_text or "").strip()
    proxy_bindings: dict[str, str] = {}
    proxy_pool: list[str] = []
    proxy_meta: dict = {}
    if proxy_bindings_raw:
        try:
            parsed_bindings = json.loads(str(proxy_bindings_raw))
            if isinstance(parsed_bindings, dict):
                proxy_bindings = {
                    str(k).strip(): (str(v).strip() if v is not None else "")
                    for k, v in parsed_bindings.items()
                    if str(k).strip()
                }
        except Exception:
            errors.append("proxy_bindings: JSON 格式无效，已忽略代理绑定")
    if not resolved_proxy_source_text:
        db = SessionLocal()
        try:
            resolved_proxy_source_text = get_default_proxy_source(db)
        finally:
            db.close()
    extra_map, proxy_pool, proxy_meta = await asyncio.to_thread(resolve_proxy_source_text, resolved_proxy_source_text, errors)
    proxy_bindings.update(extra_map)
    
    os.makedirs(CONFIG.SESSION_DIR, exist_ok=True)

    async def persist_session(account_name: str, content: bytes):
        nonlocal uploaded, duplicate, invalid
        target_path = os.path.join(CONFIG.SESSION_DIR, f"{account_name}.session")
        # Never replace a live session or silently discard an existing login.
        staging_dir = tempfile.mkdtemp(prefix=".import-", dir=CONFIG.SESSION_DIR)
        fd, staging = tempfile.mkstemp(suffix=".session", dir=staging_dir)
        binding = preview_bindings.get(account_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            await asyncio.to_thread(validate_session_file, staging)
            if os.path.lexists(target_path):
                raise FileExistsError(target_path)
            # Save the binding before making the account discoverable.
            with SessionLocal() as db:
                if binding:
                    upsert_proxy_binding(db, account_name, binding, enabled=True, commit=False)
                os.link(staging, target_path)  # atomic publication; fails if target exists
                try:
                    db.commit()
                except Exception:
                    os.unlink(target_path)
                    raise
            uploaded_accounts.append(account_name)
            validated_accounts.append(account_name)
            # 新号先记一条预热状态（readiness=0）。不写的话，重启时的存量打标
            # 会把它当成「上线前就存在的老号」豁免掉，预热就永远轮不到它。
            try:
                warmup.ensure_state(account_name)
            except Exception:
                pass
            if not binding:
                no_proxy_accounts.append(account_name)
            uploaded += 1
        except FileExistsError:
            duplicate += 1
            errors.append(f"{account_name}: 已存在，已保留原账号；如需替换请先停止任务并删除旧账号")
        except Exception as exc:
            invalid += 1
            errors.append(f"{account_name}: 导入失败 - {str(exc)[:160]}")
        finally:
            shutil.rmtree(staging_dir)

    batch_bytes = 0

    for file in files:
        try:
            filename = (file.filename or "").strip()
            lower_name = filename.lower()
            content = await file.read(MAX_BATCH_BYTES + 1)
            if len(content) > MAX_BATCH_BYTES:
                errors.append(f"{filename}: 上传文件超过 128 MiB")
                continue

            if lower_name.endswith(".session"):
                account_name = account_from_filename(filename)
                if not account_name:
                    errors.append(f"{filename}: 文件名无效")
                    continue
                if len(content) > MAX_SESSION_BYTES or batch_bytes + len(content) > MAX_BATCH_BYTES or len(pending_sessions) >= MAX_SESSION_COUNT:
                    errors.append(f"{filename}: 超过导入大小或数量限制")
                    continue
                batch_bytes += len(content)
                pending_sessions.append((account_name, content))
                continue

            if lower_name.endswith(".zip"):
                try:
                    archive = zipfile.ZipFile(io.BytesIO(content))
                except zipfile.BadZipFile:
                    errors.append(f"{filename}: ZIP 文件损坏或格式不正确")
                    continue

                members = [
                    info for info in archive.infolist()
                    if not info.is_dir() and info.filename.lower().endswith(".session")
                ]
                if not members:
                    errors.append(f"{filename}: ZIP 中未找到 .session 文件")
                    continue

                for info in members:
                    account_name = account_from_filename(info.filename)
                    if not account_name:
                        errors.append(f"{filename}: {info.filename} 文件名无效")
                        continue
                    try:
                        if info.file_size > MAX_SESSION_BYTES or batch_bytes + info.file_size > MAX_BATCH_BYTES or len(pending_sessions) >= MAX_SESSION_COUNT:
                            errors.append(f"{filename}: {info.filename} 超过导入大小或数量限制")
                            continue
                        batch_bytes += info.file_size
                        pending_sessions.append((account_name, archive.read(info)))
                    except Exception as e:
                        errors.append(f"{filename}: {info.filename} - {str(e)[:80]}")
                continue

            errors.append(f"{filename}: 仅支持 .session 或 .zip 文件")
        except Exception as e:
            errors.append(f"{getattr(file, 'filename', '文件')}: {str(e)}")

    await form.close()

    # 上传是纯本地操作：代理缺失只标记 NO_PROXY，绝不阻止账号入库，也不连接 Telegram
    pending_accounts: list[str] = []
    seen_pending: set[str] = set()
    for account_name, _content in pending_sessions:
        if account_name not in seen_pending:
            seen_pending.add(account_name)
            pending_accounts.append(account_name)

    assignment_meta: dict = {}
    preview_bindings: dict[str, str] = {}
    if pending_accounts:
        # 轮换型网关（如 711Proxy）按账号派生粘性会话，把 1 个网关拆成 N 个固定出口 IP
        preview_bindings, assignment_meta = assign_proxy_pool_to_accounts(
            pending_accounts,
            proxy_bindings,
            proxy_pool,
            allow_sticky=True,
        )
        missing = [name for name in pending_accounts if name not in preview_bindings]
        if missing:
            db = SessionLocal()
            try:
                enforce = get_proxy_enforce(db)
            finally:
                db.close()
            if enforce:
                return JSONResponse(
                    {
                        "detail": "proxy_required_per_account",
                        "message": (
                            f"已开启「强制一账号一代理」：本次 {len(pending_accounts)} 个账号里有 {len(missing)} 个"
                            f"没有可用代理，已整批拒绝（未写入任何账号）。可在「代理 IP 配置」关闭该开关后重试。"
                        ),
                        "accounts_total": len(pending_accounts),
                        "proxies_missing": len(missing),
                        "missing_accounts": missing[:20],
                        "errors": errors if errors else None,
                    },
                    status_code=400,
                )
        for account_name, content in pending_sessions:
            await persist_session(account_name, content)

    account_service.clear_cache()
    if warmup.warmup_enabled() and int(getattr(CONFIG, "WARMUP_AUTO_START", 1)):
        try:
            pending = warmup.pending_accounts(_discover_session_accounts())
            if pending:
                warmup.start(multi_manager, pending)
        except Exception:
            pass
    return JSONResponse({
        "ok": True,
        "total": len(pending_accounts),
        "uploaded": uploaded,
        "duplicate": duplicate,
        "invalid": invalid,
        "validated": len(validated_accounts),
        "validated_accounts": validated_accounts,
        "no_proxy": no_proxy_accounts,
        "no_proxy_count": len(no_proxy_accounts),
        "proxy_assigned": sum(1 for name in validated_accounts if preview_bindings.get(name)),
        "proxy_accounts_total": len(validated_accounts),
        "proxy_sticky": sum(1 for name in validated_accounts if "-session-" in preview_bindings.get(name, "")),
        "proxy_source_meta": proxy_meta,
        "proxy_assignment_meta": assignment_meta,
        "errors": errors if errors else None,
    }, status_code=200)


@app.route("/api/accounts/bulk-delete", methods=["POST"])
async def bulk_delete_accounts(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    accounts = body.get("accounts") or []
    if not isinstance(accounts, list):
        return JSONResponse({"detail": "accounts must be a list"}, status_code=400)
    cleaned: list[str] = []
    for a in accounts:
        if not isinstance(a, str):
            a = str(a)
        name = a.strip()
        if name and name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        return JSONResponse({"detail": "no_accounts"}, status_code=400)
    db: Session = SessionLocal()
    results = []
    try:
        for name in cleaned:
            task_cleanup = {
                "active_tasks_deleted": 0,
                "task_records_deleted": 0,
                "task_events_deleted": 0,
            }
            try:
                task_cleanup = _purge_account_tasks(db, name)
            except Exception:
                db.rollback()
            deleted = False
            error = None
            try:
                if name in multi_manager.managers:
                    try:
                        await multi_manager.managers[name].disconnect()
                    except Exception:
                        pass
                deleted = await account_service.delete_session(name)
                delete_proxy_binding(db, name)
            except Exception as e:
                error = str(e)[:200]
            results.append(
                {
                    "account": name,
                    "deleted": bool(deleted),
                    "tasks_stopped": task_cleanup["active_tasks_deleted"],
                    "task_records_deleted": task_cleanup["task_records_deleted"],
                    "task_events_deleted": task_cleanup["task_events_deleted"],
                    "error": error,
                }
            )
        return JSONResponse({"results": results})
    finally:
        db.close()


# session 家族的所有后缀：主库 + SQLite 的 journal/wal/shm 副产物
_SESSION_FILE_SUFFIXES = (".session", ".session-journal", ".session-wal", ".session-shm")


def _purge_session_files() -> tuple[list[str], int]:
    """删光 session 目录下的账号文件，返回 (账号名, 删掉的文件数)。

    先按 .session 主文件认出有哪些账号，再连带 journal/wal/shm 一起删：
    只删主文件会留下副产物，下次上传同一个号时 SQLite 会拿旧日志回滚出新状态。
    """
    session_dir = CONFIG.SESSION_DIR
    try:
        items = os.listdir(session_dir)
    except Exception:
        return [], 0
    accounts = _discover_session_accounts()
    removed = 0
    for filename in items:
        if not filename.endswith(_SESSION_FILE_SUFFIXES):
            continue
        path = os.path.join(session_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            os.remove(path)
            removed += 1
        except Exception as exc:
            print(f"[ACCOUNT] 删除 {path} 失败: {exc}")
    return accounts, removed


@app.route("/api/accounts/purge-sessions", methods=["POST"])
async def purge_all_sessions(request: Request):
    """清空全部 session 文件：账号、它们的任务、代理绑定与检测记录一起清掉。

    不可逆操作，前端会二次确认。删之前必须先断开全部连接——连接还挂着的时候删文件，
    Telethon 会在下一次写入时把文件连同旧 auth key 一起建回来。
    """
    try:
        await multi_manager.disconnect_all()
    except Exception as exc:
        print(f"[ACCOUNT] 清空前断开连接失败（继续删文件）: {exc}")

    db: Session = SessionLocal()
    tasks_removed = 0
    try:
        for name in _discover_session_accounts():
            try:
                tasks_removed += int((_purge_account_tasks(db, name) or {}).get("task_records_deleted") or 0)
            except Exception:
                db.rollback()
            try:
                delete_proxy_binding(db, name)
            except Exception:
                db.rollback()
    finally:
        db.close()

    accounts, files_removed = _purge_session_files()
    for name in accounts:
        try:
            await account_service.delete_session(name)
        except Exception as exc:
            print(f"[ACCOUNT] 清理 {name} 残留记录失败: {exc}")
        # 连接管理器也要丢掉：留着它，之后谁去连一次就会用一个空 auth key
        # 把 session 文件重新建出来（账号列表里就会多出一个假账号）
        multi_manager.managers.pop(name, None)
    account_service.clear_cache()

    print(f"[ACCOUNT] 已清空 session：{len(accounts)} 个账号、{files_removed} 个文件、{tasks_removed} 条任务记录")
    return JSONResponse({
        "ok": True,
        "accounts": len(accounts),
        "files_removed": files_removed,
        "tasks_removed": tasks_removed,
    })


@app.route("/api/task-control", methods=["POST"])
async def task_control(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    task_id = body.get("task_id")
    action = (body.get("action") or "").strip().lower()
    if not task_id or action not in ("pause", "resume", "stop"):
        return JSONResponse({"detail": "bad_request"}, status_code=400)
    db: Session = SessionLocal()
    try:
        t = db.query(Task).filter(Task.id == task_id).first()
        if not t:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if action == "pause":
            t.paused = 1
            db.add(TaskEvent(task_id=task_id, event="paused", detail="task_paused", meta_json=json.dumps({}, ensure_ascii=False)))
        elif action == "resume":
            t.paused = 0
            db.add(TaskEvent(task_id=task_id, event="resumed", detail="task_resumed", meta_json=json.dumps({}, ensure_ascii=False)))
        elif action == "stop":
            t.stop_requested = 1
            db.add(TaskEvent(task_id=task_id, event="stop_requested", detail="task_stop_requested", meta_json=json.dumps({}, ensure_ascii=False)))
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()

@app.route("/api/groups")
async def list_groups(request: Request):
    only_groups = request.query_params.get("only_groups", "true").lower() != "false"
    account = request.query_params.get("account") or CONFIG.DEFAULT_ACCOUNT
    refresh = request.query_params.get("refresh", "false").lower() in ("1", "true", "yes")
    if getattr(CONFIG, "GROUP_CACHE_ENABLED", 1) == 0:
        refresh = True
    authorized = await _is_authorized_account(account)
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    db: Session = SessionLocal()
    try:
        data = await get_groups(multi_manager, account=account, only_groups=only_groups, refresh=refresh, db=db)
        return JSONResponse(data)
    except asyncio.CancelledError:
        return JSONResponse({"detail": "request_cancelled"}, status_code=499)
    except BaseException as e:
        msg = str(e).lower()
        if "not authorized" in msg or "session" in msg:
            return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
        return JSONResponse({"detail": "internal_error"}, status_code=500)
    finally:
        db.close()

@app.route("/api/groups/cache/clear")
async def clear_groups_cache(request: Request):
    account = request.query_params.get("account")
    only_groups_param = request.query_params.get("only_groups")
    only_groups = None
    if only_groups_param is not None:
        only_groups = only_groups_param.lower() != "false"
    db: Session = SessionLocal()
    try:
        resp = clear_group_cache(account=account, only_groups=only_groups, db=db)
        return JSONResponse({"ok": True, **resp})
    finally:
        db.close()


@app.route("/api/test-send", methods=["POST"])
async def test_send(request: Request):
    token = request.headers.get("X-Admin-Token")
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    retry_max = int(body.get("retry_max", getattr(CONFIG, "SEND_RETRY_MAX", 0)))
    retry_delay_ms = int(body.get("retry_delay_ms", getattr(CONFIG, "SEND_RETRY_DELAY_MS", 1500)))
    request_id = body.get("request_id")
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    authorized = await _is_authorized_account(account)
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        resp = await send_to_groups(multi_manager, db, account, group_ids, message, parse_mode, disable_web_page_preview, 0, retry_max, retry_delay_ms)
        return JSONResponse(resp)
    finally:
        db.close()


@app.route("/api/logs")
async def recent_logs(request: Request):
    limit = int(request.query_params.get("limit", 50))
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(SendLog)
            .order_by(SendLog.created_at.desc())
            .limit(limit)
            .all()
        )
        data = [
            {
                "id": r.id,
                "task_id": getattr(r, "task_id", None),
                "task_round": getattr(r, "task_round", None),
                "account_name": getattr(r, "account_name", None),
                "group_id": r.group_id,
                "group_title": r.group_title,
                "message_preview": r.message_preview,
                "status": r.status,
                "error": r.error,
                "message_id": getattr(r, "message_id", None),
                "parse_mode": getattr(r, "parse_mode", None),
                "created_at": _serialize_log_created_at(r.created_at),
            }
            for r in rows
        ]
        return JSONResponse(data)
    finally:
        db.close()


@app.route("/api/join-logs")
async def recent_join_logs(request: Request):
    """加群日志：和发送日志分开返回，前端单独展示。"""
    limit = int(request.query_params.get("limit", 50))
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(JoinLog)
            .order_by(JoinLog.created_at.desc())
            .limit(limit)
            .all()
        )
        data = [
            {
                "id": r.id,
                "account_name": r.account_name,
                "group_id": r.group_id,
                "group_username": r.group_username,
                "group_title": r.group_title,
                "status": r.status,
                "error": r.error,
                "created_at": _serialize_log_created_at(r.created_at),
            }
            for r in rows
        ]
        return JSONResponse(data)
    finally:
        db.close()


@app.route("/api/logs/clear", methods=["POST"])
async def clear_logs(request: Request):
    db: Session = SessionLocal()
    try:
        deleted = db.query(SendLog).delete()
        join_deleted = db.query(JoinLog).delete()
        db.commit()
        return JSONResponse({"deleted": int(deleted), "join_deleted": int(join_deleted)})
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        return JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        db.close()

async def startup_event():
    print("[STARTUP] Event triggered")
    Base.metadata.create_all(bind=engine)
    try:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            cols = conn.execute(text("PRAGMA table_info('send_logs')")).fetchall()
            names = {c[1] for c in cols}
            if 'task_id' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN task_id VARCHAR(64)"))
            if 'task_round' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN task_round INTEGER"))
            if 'account_name' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN account_name VARCHAR(64)"))
            if 'message_id' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN message_id INTEGER"))
            if 'parse_mode' not in names:
                conn.execute(text("ALTER TABLE send_logs ADD COLUMN parse_mode VARCHAR(16)"))

            task_cols = conn.execute(text("PRAGMA table_info('tasks')")).fetchall()
            task_names = {c[1] for c in task_cols}
            if 'rounds' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN rounds INTEGER DEFAULT 1"))
            if 'current_round' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN current_round INTEGER DEFAULT 1"))
            if 'round_interval_s' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN round_interval_s INTEGER DEFAULT 0"))
            if 'next_round_at' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN next_round_at DATETIME"))
            if 'delay_scope' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN delay_scope VARCHAR(32) DEFAULT 'per_account'"))
            if 'group_msg_cooldown_s' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN group_msg_cooldown_s INTEGER DEFAULT 180"))
            if 'dynamic_groups' not in task_names:
                conn.execute(text("ALTER TABLE tasks ADD COLUMN dynamic_groups INTEGER DEFAULT 0"))

            preset_cols = conn.execute(text("PRAGMA table_info('send_presets')")).fetchall()
            preset_names = {c[1] for c in preset_cols}
            if preset_names and 'group_msg_cooldown_s' not in preset_names:
                conn.execute(text("ALTER TABLE send_presets ADD COLUMN group_msg_cooldown_s INTEGER DEFAULT 180"))

            group_cols = conn.execute(text("PRAGMA table_info('target_groups')")).fetchall()
            group_names = {c[1] for c in group_cols}
            if group_names and 'joined_by_json' not in group_names:
                conn.execute(text("ALTER TABLE target_groups ADD COLUMN joined_by_json TEXT"))
            conn.commit()
    except Exception:
        pass
    try:
        import os as _os
        _os.makedirs(CONFIG.SESSION_DIR, exist_ok=True)
        for _name, _cfg in CONFIG.ACCOUNTS.items():
            _sn = _cfg.get("session_name")
            if not _sn:
                continue
            _target = _os.path.join(CONFIG.SESSION_DIR, _sn + ".session")
            if not _os.path.exists(_target):
                _src = _os.path.join(".", _sn + ".session")
                if _os.path.exists(_src):
                    try:
                        shutil.copy2(_src, _target)
                    except Exception:
                        pass
    except Exception:
        pass
    try:
        db: Session = SessionLocal()
        try:
            row = db.query(SystemKV).filter(SystemKV.k == "copy_receiver").first()
            if row and row.v:
                try:
                    d = json.loads(row.v)
                    acc = d.get("account")
                    enabled = bool(d.get("enabled", 0))
                    if acc:
                        set_copy_receiver(acc, enabled)
                except Exception:
                    pass
            dp = db.query(SystemKV).filter(SystemKV.k == "login_phone:main_account").first()
            if not dp:
                dp = SystemKV(k="login_phone:main_account", v=json.dumps({"phone": "+1 548 520 7895"}, ensure_ascii=False))
                db.add(dp)
                db.commit()
        finally:
            db.close()
    except Exception:
        pass

    # 设置 account_service 的 manager 引用（用于健康检查）
    account_service.set_manager(multi_manager)

    # 把上次的账号检测结果读回来，避免每次重建容器全部账号都变回「未检测」
    try:
        loaded = account_service.load_persisted_statuses()
        print(f"[STARTUP] 已恢复 {loaded} 个账号的上次检测结果")
    except Exception as exc:
        print(f"[STARTUP] 恢复账号检测结果失败（不影响启动）: {exc}")

    # 预热上线时把「已经在用的账号」一次性标成存量账号，直接算成熟。
    # 不这么做的话：判老号靠的是成功发送记录，而这批库里的 send_logs 可能是空的
    # （8003 实测 0 行），全部账号会被判成新号、集体拦去预热，发送能力等于清零。
    # 幂等：只给没有预热状态的账号打标，所以每次启动跑都安全。
    try:
        backfill = warmup.backfill_existing(_discover_session_accounts())
        if backfill.get("marked"):
            print(f"[STARTUP] 存量账号打标完成：{backfill['marked']} 个")
    except Exception as exc:
        print(f"[STARTUP] 存量账号打标失败（不影响启动）: {exc}")

    # 预热自动续跑：重启前没毕业的号接着养，不用手动再点「开始预热」
    if warmup.warmup_enabled() and int(getattr(CONFIG, "WARMUP_AUTO_START", 1)):
        try:
            pending = warmup.pending_accounts(_discover_session_accounts())
            if pending:
                warmup.start(multi_manager, pending)
                print(f"[STARTUP] 自动预热 {len(pending)} 个未毕业账号")
        except Exception as exc:
            print(f"[STARTUP] 自动预热失败（不影响启动）: {exc}")

    if is_sqlite_enabled():
        try:
            prepare_result = sqlite_ensure_task_runtime_indexes()
            manifest_path = export_task_migration_manifest("startup")
            health = sqlite_health_check("quick_check")
            if not health.get("ok", False):
                snapshot_path = _capture_task_db_snapshot("startup_healthcheck_failed")
                print(f"[STARTUP][TASK_DB] health_check failed: {health.get('result')} snapshot={snapshot_path}")
            else:
                snapshot_path = _capture_task_db_snapshot("startup")
                sqlite_checkpoint("PASSIVE")
                if snapshot_path:
                    print(f"[STARTUP][TASK_DB] startup snapshot saved: {snapshot_path}")
            if prepare_result.get("indexes") or manifest_path:
                print(
                    "[STARTUP][TASK_DB] prepared "
                    f"indexes={len(prepare_result.get('indexes', []))} "
                    f"manifest={manifest_path}"
                )
        except Exception as exc:
            print(f"[STARTUP][TASK_DB] health bootstrap failed: {exc}")
    
    # 启动时串行把旧版 session 升级到当前表结构，避免客户端并发打开时报 database is locked
    try:
        normalized = 0
        for filename in sorted(os.listdir(CONFIG.SESSION_DIR)):
            if filename.endswith(".session"):
                path = os.path.join(CONFIG.SESSION_DIR, filename)
                if _normalize_session_schema(path):
                    normalized += 1
        if normalized:
            print(f"[STARTUP][SESSION] normalized {normalized} session file(s)")
    except Exception as exc:
        print(f"[STARTUP][SESSION] normalize failed: {exc}")

    # 启动定期清理空闲连接的后台任务
    asyncio.create_task(_periodic_connection_cleanup())
    asyncio.create_task(_periodic_task_cleanup())
    asyncio.create_task(_periodic_sqlite_maintenance())
    asyncio.create_task(_periodic_proxy_topup())

    db: Session = SessionLocal()
    try:
        rows = db.query(Task).filter(Task.status == "running").limit(100).all()
        # 边加群边群发是常驻模式：重启后自动接着跑，否则每次部署都要手动点一次
        live_resumed = False
        try:
            live_no = _kv_get(LIVE_PIPELINE_KV_KEY)
            if live_no:
                preset_row = db.query(SendPreset).filter(SendPreset.id == int(live_no)).first()
                if preset_row is not None:
                    _start_live_pipeline(_preset_payload(preset_row))
                    live_resumed = True
                    print(f"[STARTUP] 恢复边加群边群发（#{live_no}）")
        except Exception as exc:
            print(f"[STARTUP] 恢复边加群边群发失败：{exc}")
        if live_resumed:
            # 旧任务由常驻模式自己重建，避免同一账号被两个 runner 同时发
            now = CONFIG.now()
            for t in rows:
                t.status = "stopped"
                t.stop_requested = 1
                t.finished_at = now
            db.commit()
            rows = []
        if int(getattr(CONFIG, "RESUME_TASKS_ON_STARTUP", 0)) != 1:
            now = CONFIG.now()
            for t in rows:
                t.status = "stopped"
                t.stop_requested = 1
                t.finished_at = now
                db.add(TaskEvent(
                    task_id=t.id,
                    event="startup_resume_skipped",
                    detail="resume_disabled_on_startup",
                    meta_json=json.dumps({}, ensure_ascii=False),
                ))
            db.commit()
            print(f"[STARTUP] Resume disabled, stopped {len(rows)} running task(s)")
            return
        for t in rows:
            try:
                start_delay = random.uniform(
                    max(int(getattr(CONFIG, "STARTUP_RESUME_MIN_DELAY_S", 5)), 0),
                    max(int(getattr(CONFIG, "STARTUP_RESUME_MAX_DELAY_S", 45)), 0),
                )
                print(
                    f"[STARTUP] Resuming task {t.id} for {t.account_name} "
                    f"at round {t.current_round} index {t.current_index} "
                    f"with delay {start_delay:.1f}s"
                )
                await _resume_existing_task(
                    t,
                    reason="startup_resume",
                    start_delay=start_delay,
                )
            except Exception:
                pass
    finally:
        db.close()


_REQ_IDS: dict[str, float] = {}
_LAST_TS: dict[str, float] = {}
_GLOBAL_SEND_NEXT_AT_BY_BUCKET: dict[str, float] = {}
_GLOBAL_SEND_SLOT_LOCK = asyncio.Lock()


async def _periodic_connection_cleanup():
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "CONNECTION_CLEANUP_INTERVAL_S", 60)), 10))
            await multi_manager._cleanup_idle_connections()
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")


PROXY_TOPUP_KEY = "proxy_topup_last"
# 缺代理的账号会被定时补绑。裸奔连 Telegram = 多账号共用出口 IP = 批量掉号，
# 8002/8004 那次 100 个号掉 78 个就是这么来的。
PROXY_TOPUP_INTERVAL_S = max(60, int(os.getenv("PROXY_TOPUP_INTERVAL_S", "600")))


def _kv_set(key: str, value: str) -> None:
    db: Session = SessionLocal()
    try:
        row = db.query(SystemKV).filter(SystemKV.k == key).first()
        if row:
            row.v = value
        else:
            db.add(SystemKV(k=key, v=value))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _kv_get(key: str) -> str | None:
    db: Session = SessionLocal()
    try:
        row = db.query(SystemKV).filter(SystemKV.k == key).first()
        return row.v if row else None
    finally:
        db.close()


async def _proxy_topup_once() -> dict:
    """给还没有代理的账号补绑定：拉默认代理源 → 一号一代理分配 → 落库。

    已有绑定的账号一律跳过：粘性会话的出口 IP 一换，对 Telegram 就是换了张脸，
    比没代理还危险。池子不够时只补能补的，剩下的留给下一轮。
    """
    accounts = _discover_session_accounts()
    db: Session = SessionLocal()
    try:
        bound_map = list_proxy_bindings(db, accounts)
        source = get_default_proxy_source(db)
    finally:
        db.close()

    missing = [name for name in accounts if name not in bound_map]
    # 已经绑出去的代理不能再给第二个号：多号共用出口 IP 才是被封的根因
    used_proxies = [
        item.get("proxy_url") for item in bound_map.values() if item.get("proxy_url")
    ]
    if not missing or not source:
        summary = {
            "at": CONFIG.now().isoformat(),
            "accounts": len(accounts),
            "candidates": len(missing),
            "assigned_total": 0,
            "reason": "no_source" if not source else "all_bound",
        }
        _kv_set(PROXY_TOPUP_KEY, json.dumps(summary, ensure_ascii=False))
        return summary

    result = await asyncio.to_thread(
        assign_proxies_from_source, missing, source, used_proxies)
    summary = {
        "at": CONFIG.now().isoformat(),
        "accounts": len(accounts),
        "candidates": len(missing),
        "assigned_total": result["assigned_total"],
        "errors": result["errors"],
        "reason": "assigned" if result["assigned_total"] else "no_pool_left",
    }
    _kv_set(PROXY_TOPUP_KEY, json.dumps(summary, ensure_ascii=False))
    print(f"[PROXY] 自动补绑：{len(missing)} 个账号缺代理，补上 {result['assigned_total']} 个")
    return summary


async def _periodic_proxy_topup():
    """定时给缺代理的账号补绑；没配默认代理源时什么都不做。"""
    while True:
        await asyncio.sleep(PROXY_TOPUP_INTERVAL_S)
        try:
            await _proxy_topup_once()
        except Exception as exc:
            print(f"[PROXY] 自动补绑失败: {exc}")


async def _periodic_task_cleanup():
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "TASK_CLEANUP_INTERVAL_S", 60)), 10))
            db: Session = SessionLocal()
            try:
                now = CONFIG.now()
                timeout_s = int(getattr(CONFIG, "TASK_STUCK_TIMEOUT_S", 1800))
                missing_runner_timeout_s = int(getattr(CONFIG, "TASK_RUNNER_MISSING_TIMEOUT_S", 120))
                rows = db.query(Task).filter(Task.status == "running").limit(500).all()
                for t in rows:
                    if int(t.paused or 0) == 1 or int(t.stop_requested or 0) == 1:
                        continue
                    hb = t.heartbeat_at or t.started_at
                    if not hb:
                        continue
                    heartbeat_age_s = _seconds_since_task_timestamp(hb)
                    if not _task_runner_active(t.id):
                        if (
                            int(getattr(CONFIG, "RESUME_TASKS_ON_STARTUP", 1)) == 1
                            and heartbeat_age_s >= missing_runner_timeout_s
                        ):
                            if await _resume_existing_task(
                                t,
                                reason="watchdog_runner_missing",
                                start_delay=0.0,
                            ):
                                continue
                    if heartbeat_age_s > timeout_s:
                        t.status = "error"
                        t.stop_requested = 1
                        t.finished_at = now
                        db.add(TaskEvent(
                            task_id=t.id,
                            event="stuck_timeout",
                            detail=str(timeout_s),
                            meta_json=json.dumps({"heartbeat_age_s": int(heartbeat_age_s)}, ensure_ascii=False),
                        ))
                try:
                    db.commit()
                except DatabaseError as exc:
                    db.rollback()
                    _handle_task_db_error("periodic_task_cleanup_commit", exc)
            finally:
                db.close()
        except DatabaseError as exc:
            _handle_task_db_error("periodic_task_cleanup", exc)
        except Exception as e:
            print(f"[CLEANUP] Error: {e}")


async def _periodic_sqlite_maintenance():
    if not is_sqlite_enabled():
        return
    while True:
        try:
            await asyncio.sleep(max(int(getattr(CONFIG, "SQLITE_HEALTHCHECK_INTERVAL_S", 300)), 60))
            health = sqlite_health_check("quick_check")
            if not health.get("ok", False):
                snapshot_path = _capture_task_db_snapshot("sqlite_healthcheck_failed")
                print(f"[TASK_DB] quick_check failed: {health.get('result')} snapshot={snapshot_path}")
                continue
            sqlite_ensure_task_runtime_indexes()
            sqlite_checkpoint("PASSIVE")
        except Exception as e:
            print(f"[TASK_DB] maintenance error: {e}")


def _check_request_guard(token: str, request_id: str | None, window_ms: int = 500):
    now = time.monotonic()
    # prune old ids
    for k, ts in list(_REQ_IDS.items()):
        if now - ts > 60:
            del _REQ_IDS[k]
    # duplicate id guard
    if request_id:
        if request_id in _REQ_IDS:
            return False, "duplicate"
        _REQ_IDS[request_id] = now
    # throttle by token
    last = _LAST_TS.get(token, 0.0)
    if now - last < (window_ms / 1000.0):
        _LAST_TS[token] = now
        return False, "too_frequent"
    _LAST_TS[token] = now
    return True, None


def _normalize_delay_scope(raw_scope: str | None, default: str = "per_account") -> str:
    scope = str(raw_scope or default).strip().lower()
    if scope not in {"per_account", "global"}:
        return default
    return scope


def _prefer_per_account_delay_scope(
    raw_scope: str | None,
    request_id: str | None,
    has_sibling_tasks: bool,
) -> str:
    scope = _normalize_delay_scope(raw_scope, "per_account")
    if scope == "global" and str(request_id or "").strip() and has_sibling_tasks:
        return "per_account"
    return scope


def _prefer_batch_delay_ms(delay_ms: int, has_sibling_tasks: bool) -> int:
    value = max(0, int(delay_ms or 0))
    if has_sibling_tasks and value >= 11000:
        return 3000
    return value


def _task_has_sibling_tasks(db: Session, task: Task | None, task_id: str) -> tuple[str, bool]:
    if task is None:
        return "", False
    request_id = str(getattr(task, "request_id", "") or "").strip()
    if not request_id:
        return "", False
    has_sibling_tasks = (
        db.query(Task.id)
        .filter(Task.request_id == request_id, Task.id != task_id)
        .first()
        is not None
    )
    return request_id, has_sibling_tasks


def _effective_task_timing(db: Session, task: Task | None, task_id: str, delay_ms: int) -> tuple[str, int]:
    if task is None:
        return "per_account", max(0, int(delay_ms or 0))
    request_id, has_sibling_tasks = _task_has_sibling_tasks(db, task, task_id)
    scope = _prefer_per_account_delay_scope(getattr(task, "delay_scope", None), request_id, has_sibling_tasks)
    effective_delay_ms = _prefer_batch_delay_ms(delay_ms, has_sibling_tasks)
    return scope, effective_delay_ms


def _self_check_delay_scope_helpers() -> None:
    assert _prefer_per_account_delay_scope("global", "req-1", True) == "per_account"
    assert _prefer_per_account_delay_scope("global", "req-1", False) == "global"
    assert _prefer_per_account_delay_scope("per_account", "req-1", True) == "per_account"
    assert _prefer_batch_delay_ms(11000, True) == 3000
    assert _prefer_batch_delay_ms(3000, True) == 3000


_self_check_delay_scope_helpers()


def _compute_effective_task_delay_ms(account: str, delay_ms: int) -> int:
    base_ms = max(int(delay_ms or 0), int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500)))
    if int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) != 1:
        return base_ms
    with SessionLocal() as db:
        rate = recent_fail_rate(db, account, int(getattr(CONFIG, "ACCOUNT_RECENT_WINDOW_N", 50)))
    adjusted_ms = dynamic_delay_ms(base_ms, rate)
    jitter_pct = float(getattr(CONFIG, "SEND_JITTER_PCT", 0.15))
    jitter = random.uniform(-jitter_pct, jitter_pct) * adjusted_ms
    return max(0, int(adjusted_ms + jitter))


def _global_send_bucket_key(task: Task | None, task_id: str) -> str:
    if task is None:
        return task_id
    request_id = str(getattr(task, "request_id", "") or "").strip()
    if request_id:
        return f"request:{request_id}"
    return f"task:{task_id}"


async def _reserve_global_send_slot(task_id: str, bucket_key: str, wait_s: float) -> bool:
    """在全局发送 bucket 上预留一个槽位，并等待到槽位到期再返回。

    每次调用都会把 bucket 的“下次可用时间”往后推 wait_s，并真实等待到
    自己这轮槽位到期，从而保证同一个 bucket（同一个 request/task）内相邻
    两次发送之间有至少 wait_s 的间隔，真正起到防风控作用。
    """
    delay_s = max(0.0, float(wait_s))
    async with _GLOBAL_SEND_SLOT_LOCK:
        now = time.monotonic()
        last_at = _GLOBAL_SEND_NEXT_AT_BY_BUCKET.get(bucket_key, 0.0)
        ready_at = max(now, last_at)
        _GLOBAL_SEND_NEXT_AT_BY_BUCKET[bucket_key] = ready_at + delay_s
    wait_needed_s = ready_at - now
    if wait_needed_s > 0:
        # 等待期间保持心跳，并响应暂停/停止请求
        if not await _sleep_with_task_checks(task_id, wait_needed_s, refresh_heartbeat=True):
            return False
    return True


def _get_existing_tasks_by_request_id(db: Session, request_id: str | None) -> list[Task]:
    if not request_id:
        return []
    return (
        db.query(Task)
        .filter(Task.request_id == request_id)
        .order_by(Task.started_at.desc())
        .all()
    )


def _planned_group_count(group_ids_json: str | None) -> int:
    try:
        group_ids = json.loads(group_ids_json or "[]")
    except Exception:
        return 0
    return len(group_ids) if isinstance(group_ids, list) else 0


def _task_round_total(task) -> int:
    """当前轮的计划群数。

    一轮跑完会把工作列表写成 []，轮次间隔里只看 group_ids_json 会把
    「等待下一轮」误报成「无可发群」，所以退回用任务上记录的 total。
    """
    total = _planned_group_count(getattr(task, "group_ids_json", None))
    if total == 0 and getattr(task, "next_round_at", None):
        total = int(getattr(task, "total", 0) or 0)
    return total


def _filter_account_banned_group_ids(db: Session, account: str, group_ids: list[int]) -> list[int]:
    banned = set(get_banned_group_ids(db, account))
    if not banned:
        return unique_group_ids(group_ids)
    return [gid for gid in unique_group_ids(group_ids) if int(gid) not in banned]


_SEND_BLOCKED_GROUP_STATUSES = ("approval_required", "unreachable")


def _account_pool_group_ids(db: Session, account: str) -> list[int]:
    """群池里这个账号当前真实可发的群：已加入 + 未拉黑 + 群状态可加。

    动态任务每轮都调用它，后台加群新落库的群下一轮就会自动进入发送列表。
    """
    banned = set(get_banned_group_ids(db, account))
    ids: list[int] = []
    for group in db.query(TargetGroup).all():
        if str(group.status or "") in _SEND_BLOCKED_GROUP_STATUSES:
            continue
        if group.group_id is None:
            continue
        try:
            members = json.loads(group.joined_by_json or "[]")
        except Exception:
            members = []
        gid = int(group.group_id)
        if account in members and gid not in banned:
            ids.append(gid)
    return unique_group_ids(ids)


def _remove_group_id_once(group_ids: list[int], gid: int) -> list[int]:
    target = int(gid)
    return [item for item in group_ids if int(item) != target]


def _self_check_group_skip_helpers() -> None:
    assert _remove_group_id_once([1, 2, 3], 2) == [1, 3]


_self_check_group_skip_helpers()


def _collect_task_log_counters(
    db: Session,
    task_ids: list[str],
    current_round_by_task: dict[str, int] | None = None,
    task_rows_by_id: dict[str, Task] | None = None,
) -> dict[str, dict[str, int]]:
    counters: dict[str, dict[str, int]] = {
        task_id: {
            "success": 0,
            "failed": 0,
            "overall_completed": 0,
            "current_round_completed": 0,
            "current_round_success": 0,
            "current_round_failed": 0,
        }
        for task_id in task_ids
        if task_id
    }
    if not counters:
        return {}

    rows = (
        db.query(
            SendLog.task_id,
            SendLog.task_round,
            SendLog.status,
            func.count(SendLog.id),
        )
        .filter(
            SendLog.task_id.in_(list(counters.keys())),
            SendLog.status.in_(("success", "failed")),
        )
        .group_by(SendLog.task_id, SendLog.task_round, SendLog.status)
        .all()
    )

    current_round_by_task = current_round_by_task or {}
    for task_id, task_round, status, count in rows:
        bucket = counters.get(task_id)
        if not bucket:
            continue
        n = int(count or 0)
        if status == "success":
            bucket["success"] += n
        elif status == "failed":
            bucket["failed"] += n
        bucket["overall_completed"] += n

        if current_round_by_task.get(task_id) == int(task_round or 0):
            bucket["current_round_completed"] += n
            if status == "success":
                bucket["current_round_success"] += n
            elif status == "failed":
                bucket["current_round_failed"] += n

    # 兼容历史日志：旧版本 send_logs 没有 task_id/task_round，
    # 回退到“账号 + 任务开始时间”聚合累计成功/失败。
    task_rows_by_id = task_rows_by_id or {}
    for task_id, bucket in counters.items():
        if bucket["overall_completed"] > 0:
            continue
        task = task_rows_by_id.get(task_id)
        if not task or not task.started_at or not task.account_name:
            continue
        legacy_rows = (
            db.query(SendLog.status, func.count(SendLog.id))
            .filter(
                SendLog.task_id.is_(None),
                SendLog.account_name == task.account_name,
                SendLog.created_at >= task.started_at,
                SendLog.status.in_(("success", "failed")),
            )
            .group_by(SendLog.status)
            .all()
        )
        for status, count in legacy_rows:
            n = int(count or 0)
            if status == "success":
                bucket["success"] += n
            elif status == "failed":
                bucket["failed"] += n
            bucket["overall_completed"] += n

    return counters


def _serialize_log_created_at(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        target_tz = CONFIG.get_timezone()
    except Exception:
        target_tz = timezone.utc
    dt = value
    if dt.tzinfo is None:
        # SQLite often returns naive datetimes even when the original value was
        # written in the configured local timezone. Treat naive log timestamps
        # as local configured time to avoid shifting them by the timezone offset.
        dt = dt.replace(tzinfo=target_tz)
    try:
        dt = dt.astimezone(target_tz)
    except Exception:
        pass
    return dt.isoformat()


def _mark_task_stopped_if_requested(db: Session, task_id: str, detail: str) -> bool:
    t = db.query(Task).filter(Task.id == task_id).first()
    if not t:
        return True
    if not t.stop_requested:
        return False
    if t.status == "error":
        return True
    if t.status != "stopped":
        t.status = "stopped"
        t.finished_at = CONFIG.now()
        db.add(TaskEvent(task_id=task_id, event="stopped", detail=detail, meta_json=json.dumps({}, ensure_ascii=False)))
        db.commit()
    return True


async def _sleep_with_task_checks(
    task_id: str,
    seconds: float,
    step_s: float = 1.0,
    refresh_heartbeat: bool = True,
    heartbeat_interval_s: float | None = None,
) -> bool:
    remaining = max(0.0, float(seconds))
    if heartbeat_interval_s is None:
        heartbeat_interval_s = float(getattr(CONFIG, "TASK_HEARTBEAT_INTERVAL_S", 20))
    next_heartbeat_at = time.monotonic()
    while remaining > 0:
        db: Session | None = None
        try:
            db = SessionLocal()
            if _mark_task_stopped_if_requested(db, task_id, "task_stopped_during_wait"):
                return False
            if refresh_heartbeat and time.monotonic() >= next_heartbeat_at:
                t = db.query(Task).filter(Task.id == task_id).first()
                if not t:
                    return False
                if t.status == "running":
                    t.heartbeat_at = CONFIG.now()
                    db.commit()
                next_heartbeat_at = time.monotonic() + max(1.0, float(heartbeat_interval_s))
        except DatabaseError as exc:
            if db is not None:
                db.rollback()
            _handle_task_db_error("sleep_with_task_checks", exc)
            if refresh_heartbeat and time.monotonic() >= next_heartbeat_at:
                _update_runtime_task_state(
                    task_id,
                    status=TASKS.get(task_id, {}).get("status") or "running",
                    db_warning="task_db_unavailable",
                    heartbeat_at=_runtime_state_iso_now(),
                )
                next_heartbeat_at = time.monotonic() + max(1.0, float(heartbeat_interval_s))
        finally:
            if db is not None:
                db.close()
        wait_s = min(step_s, remaining)
        await asyncio.sleep(wait_s)
        remaining -= wait_s
    return True

app.add_event_handler("startup", startup_event)

TASKS: dict[str, dict] = {}
_LAST_TASK_DB_SNAPSHOT_TS = 0.0

LAST_COPY_MESSAGE: dict = {}


def _runtime_state_iso_now() -> str:
    try:
        return CONFIG.now().isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()


def _update_runtime_task_state(task_id: str, **fields) -> None:
    state = TASKS.get(task_id)
    if not state:
        state = {}
        TASKS[task_id] = state
    state.update(fields)
    state["last_updated_at"] = _runtime_state_iso_now()


def _capture_task_db_snapshot(reason: str) -> str | None:
    global _LAST_TASK_DB_SNAPSHOT_TS
    now = time.monotonic()
    if now - _LAST_TASK_DB_SNAPSHOT_TS < 60:
        return None
    _LAST_TASK_DB_SNAPSHOT_TS = now
    try:
        return export_task_runtime_snapshot(reason)
    except Exception as exc:
        print(f"[TASK_DB] snapshot failed ({reason}): {exc}")
        return None


def _runtime_task_list_rows() -> list[dict]:
    rows: list[dict] = []
    for task_id, state in TASKS.items():
        account = state.get("account")
        if not account:
            continue
        rows.append(
            {
                "task_id": task_id,
                "status": state.get("status") or ("running" if _task_runner_active(task_id) else "unknown"),
                "total": int(state.get("total") or 0),
                "success": int(state.get("success") or 0),
                "failed": int(state.get("failed") or 0),
                "current_index": int(state.get("current_index") or 0),
                "account": account,
                "delay_scope": _normalize_delay_scope(state.get("delay_scope"), "per_account"),
                "started_at": state.get("started_at"),
                "finished_at": state.get("finished_at"),
                "rounds": int(state.get("rounds") or 0),
                "current_round": int(state.get("current_round") or 0),
                "next_round_at": state.get("next_round_at"),
                "source": "runtime_fallback",
                "db_warning": "task_db_unavailable",
            }
        )
    rows.sort(key=lambda item: item.get("started_at") or "", reverse=True)
    return rows


def _runtime_task_summary_rows() -> list[dict]:
    selected: dict[str, dict] = {}
    for item in _runtime_task_list_rows():
        account = item.get("account")
        if not account:
            continue
        existing = selected.get(account)
        if not existing:
            selected[account] = item
            continue
        if existing.get("status") != "running" and item.get("status") == "running":
            selected[account] = item
            continue
        if (item.get("started_at") or "") > (existing.get("started_at") or ""):
            selected[account] = item

    data: list[dict] = []
    for item in selected.values():
        completed = int(item.get("current_index") or 0)
        total = int(item.get("total") or 0)
        data.append(
            {
                "account": item.get("account"),
                "status": item.get("status"),
                "task_id": item.get("task_id"),
                "delay_scope": item.get("delay_scope"),
                "tasks_count": 1,
                "total": total,
                "success": int(item.get("success") or 0),
                "failed": int(item.get("failed") or 0),
                "completed": completed,
                "progress_total": total,
                "progress_completed": completed,
                "current_round": int(item.get("current_round") or 0),
                "rounds": int(item.get("rounds") or 0),
                "current_round_planned": total,
                "current_round_sent": completed,
                "overall_planned": total,
                "overall_completed": completed,
                "last_updated_at": item.get("finished_at") or item.get("started_at") or item.get("last_updated_at"),
                "source": "runtime_fallback",
                "db_warning": "task_db_unavailable",
            }
        )
    data.sort(key=lambda item: item.get("last_updated_at") or "", reverse=True)
    return data


def _handle_task_db_error(context: str, exc: Exception) -> None:
    snapshot_path = _capture_task_db_snapshot(f"{context}_db_error")
    suffix = f", snapshot={snapshot_path}" if snapshot_path else ""
    malformed = " malformed" if is_database_malformed_error(exc) else ""
    print(f"[TASK_DB]{malformed} {context} degraded: {exc}{suffix}")


def _task_runner_active(task_id: str) -> bool:
    state = TASKS.get(task_id)
    if not state:
        return False
    runner = state.get("runner")
    if runner is None or runner.done():
        return False
    return True


def _register_task_runner(task_id: str, runner: asyncio.Task, meta: dict | None = None) -> None:
    payload = {"runner": runner}
    if meta:
        payload.update(meta)
    TASKS[task_id] = payload
    _update_runtime_task_state(task_id, runner_done=False)

    def _cleanup(done_task: asyncio.Task, tid: str = task_id):
        current = TASKS.get(tid)
        if current and current.get("runner") is done_task:
            current["runner_done"] = True
            current["last_updated_at"] = _runtime_state_iso_now()
        try:
            done_task.result()
        except asyncio.CancelledError:
            _update_runtime_task_state(tid, status=current.get("status") or "stopped", finished_at=_runtime_state_iso_now())
        except Exception as exc:
            _update_runtime_task_state(tid, status="error", last_error=str(exc)[:200], finished_at=_runtime_state_iso_now())
            print(f"[TASK] runner crashed for {tid}: {exc}")

    runner.add_done_callback(_cleanup)


def _seconds_since_task_timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    try:
        target_tz = CONFIG.get_timezone()
    except Exception:
        target_tz = timezone.utc
    ts = value
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=target_tz)
    now = CONFIG.now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=target_tz)
    try:
        delta = (now.astimezone(target_tz) - ts.astimezone(target_tz)).total_seconds()
    except Exception:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        ts_naive = value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)
        delta = (now_naive - ts_naive).total_seconds()
    return max(0.0, float(delta))


_JOINED_GROUP_CACHE = {"at": 0.0, "value": 0}


def _joined_group_total() -> int:
    """全池去重后的群数量：自适应冷却的分母。

    每轮每个账号都会问一次，缓存 60 秒，别为了同一个数字把表扫 18 遍。
    """
    now = time.monotonic()
    if now - _JOINED_GROUP_CACHE["at"] < 60:
        return int(_JOINED_GROUP_CACHE["value"])
    total = 0
    with SessionLocal() as db:
        for gid, members_json in db.query(TargetGroup.group_id, TargetGroup.joined_by_json).all():
            if gid is None:
                continue
            try:
                members = json.loads(members_json or "[]")
            except Exception:
                members = []
            if members:
                total += 1
    _JOINED_GROUP_CACHE.update(at=now, value=total)
    return total


def _effective_group_cooldown_s(configured_s: int) -> int:
    """按群池规模把「每小时目标条数」摊到每个群上。

    全池 218 个群、目标 240 条/小时 → 每个群大约 55 分钟才能再发一次。
    群池越小间隔越短，但不会低于 GROUP_MIN_GAP_S；预设里的冷却是防封上限。
    """
    configured = max(0, int(configured_s or 0))
    if SEND_TARGET_PER_HOUR <= 0:
        return configured
    total = _joined_group_total()
    if total <= 0:
        return configured
    adaptive = max(GROUP_MIN_GAP_S, int(round(total / float(SEND_TARGET_PER_HOUR) * 3600)))
    return min(adaptive, configured) if configured > 0 else adaptive


def _group_msg_cooldown_map(
    group_ids: list[int],
    account: str,
    cooldown_s: int,
    min_gap_s: int | None = None,
) -> dict[int, float]:
    """一次查出这批群里还发不出去的，返回 {群: 剩余秒}。

    两个维度同时生效：
    - 本账号在这个群的冷却（cooldown_s）：同一个号别反复刷同一个群；
    - 任何账号在这个群的最近一次成功（min_gap_s）：换号也别紧挨着连发。
    """
    if not group_ids:
        return {}
    gap_s = GROUP_MIN_GAP_S if min_gap_s is None else max(0, int(min_gap_s))
    if cooldown_s <= 0 and gap_s <= 0:
        return {}
    with SessionLocal() as db:
        rows = (
            db.query(SendLog.group_id, SendLog.account_name, func.max(SendLog.created_at))
            .filter(SendLog.group_id.in_(group_ids), SendLog.status == "success")
            .group_by(SendLog.group_id, SendLog.account_name)
            .all()
        )
    latest_any: dict[int, datetime] = {}
    latest_self: dict[int, datetime] = {}
    for gid, acc, ts in rows:
        if ts is None:
            continue
        gid = int(gid)
        if gid not in latest_any or ts > latest_any[gid]:
            latest_any[gid] = ts
        if acc == account and (gid not in latest_self or ts > latest_self[gid]):
            latest_self[gid] = ts
    cooling: dict[int, float] = {}
    for gid, ts in latest_any.items():
        left = gap_s - _seconds_since_task_timestamp(ts) if gap_s > 0 else 0.0
        self_ts = latest_self.get(gid)
        if cooldown_s > 0 and self_ts is not None:
            left = max(left, cooldown_s - _seconds_since_task_timestamp(self_ts))
        if left > 0:
            cooling[gid] = left
    return cooling


def _group_msg_cooldown_remaining(
    group_id: int,
    account: str,
    cooldown_s: int,
    min_gap_s: int | None = None,
) -> float:
    """这个号现在能不能往这个群发，不能的话还差多少秒。

    只看成功投递；被禁言/失败的记录不拦着别的号补发。
    """
    cooling = _group_msg_cooldown_map([group_id], account, cooldown_s, min_gap_s)
    return float(cooling.get(int(group_id), 0.0))


def _start_send_task_runner(
    *,
    task_id: str,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    rounds: int,
    round_interval_s: int,
    start_delay: float = 0.0,
    start_round_idx: int = 0,
    start_group_idx: int = 0,
    delay_scope: str = "per_account",
) -> bool:
    if _task_runner_active(task_id):
        return False

    runner = asyncio.create_task(
        _run_send_task_with_delay(
            task_id=task_id,
            account=account,
            group_ids=group_ids,
            message=message,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
            delay_ms=delay_ms,
            rounds=rounds,
            round_interval_s=round_interval_s,
            start_delay=start_delay,
            start_round_idx=start_round_idx,
            start_group_idx=start_group_idx,
        )
    )
    _register_task_runner(
        task_id,
        runner,
        {
            "account": account,
            "status": "running",
            "started_at": _runtime_state_iso_now(),
            "delay_scope": delay_scope,
            "total": len(group_ids),
            "success": 0,
            "failed": 0,
            "current_index": max(0, int(start_group_idx or 0)),
            "rounds": int(rounds or 0),
            "current_round": max(1, int(start_round_idx or 0) + 1),
            "round_interval_s": int(round_interval_s or 0),
        },
    )
    return True


async def _resume_existing_task(t: Task, reason: str, start_delay: float = 0.0) -> bool:
    if _task_runner_active(t.id):
        return False
    try:
        gids = json.loads(t.group_ids_json or "[]")
    except Exception:
        gids = []
    if not gids:
        return False
    started = _start_send_task_runner(
        task_id=t.id,
        account=t.account_name,
        group_ids=gids,
        message=t.message,
        parse_mode=t.parse_mode,
        disable_web_page_preview=bool(t.disable_web_page_preview),
        delay_ms=t.delay_ms,
        rounds=t.rounds or 1,
        round_interval_s=t.round_interval_s or 0,
        start_delay=max(0.0, float(start_delay or 0.0)),
        start_round_idx=max(0, (t.current_round or 1) - 1),
        start_group_idx=max(0, (t.current_index or 0)),
        delay_scope=_normalize_delay_scope(getattr(t, "delay_scope", None), "per_account"),
    )
    if not started:
        return False

    with SessionLocal() as db:
        fresh = db.query(Task).filter(Task.id == t.id).first()
        if fresh and fresh.status == "running":
            fresh.heartbeat_at = CONFIG.now()
            db.add(TaskEvent(
                task_id=t.id,
                event="runner_started",
                detail=reason,
                meta_json=json.dumps(
                    {
                        "round": int(fresh.current_round or 1),
                        "index": int(fresh.current_index or 0),
                    },
                    ensure_ascii=False,
                ),
            ))
            try:
                db.commit()
            except DatabaseError as exc:
                db.rollback()
                _handle_task_db_error("resume_existing_task_commit", exc)
    return True


async def _handle_private_copy(account: str, event):
    try:
        msg_id = event.message.id
        chat_id = event.message.chat_id
        text = getattr(event.message, "message", "") or ""
        media_path = None
        if getattr(event.message, "media", None):
            tmp = tempfile.NamedTemporaryFile(delete=False)
            try:
                await event.message.download_media(file=tmp.name)
                media_path = tmp.name
            finally:
                try:
                    tmp.close()
                except Exception:
                    pass
        session = SessionLocal()
        try:
            LAST_COPY_MESSAGE["account"] = account
            LAST_COPY_MESSAGE["chat_id"] = chat_id
            LAST_COPY_MESSAGE["message_id"] = msg_id
        finally:
            session.close()
        accounts = list(CONFIG.ACCOUNTS.keys())
        authorized, _skipped = await _split_authorized_accounts(accounts)
        for acc in authorized:
            try:
                gids = []
                try:
                    groups = await multi_manager.get_joined_groups(acc, only_groups=True)
                    for g in groups:
                        gid = g.get("id")
                        if gid:
                            gids.append(gid)
                except Exception:
                    pass
                base = int(getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
                jitter_pct = float(getattr(CONFIG, "SEND_JITTER_PCT", 0.15))
                db = SessionLocal()
                try:
                    for gid in gids:
                        ok = False
                        err = None
                        mid = None
                        try:
                            await multi_manager.ensure_connected(acc)
                            if media_path:
                                msg = await multi_manager.get(acc).client.send_file(entity=gid, file=media_path, caption=append_send_suffix(text))
                                mid = getattr(msg, "id", None)
                                ok = True
                            else:
                                ok, err, mid = await multi_manager.send_message_to_group(acc, gid, text=text, parse_mode=None, disable_web_page_preview=True)
                        except Exception as e:
                            err = str(e)
                            ok = False
                        title = await multi_manager.get_group_title(acc, gid)
                        db.add(SendLog(
                            account_name=acc,
                            group_id=gid,
                            group_title=title,
                            message_preview=(text or "")[:200],
                            status="success" if ok else "failed",
                            error=None if ok else err,
                            message_id=mid,
                            parse_mode=None,
                        ))
                        db.commit()
                        jitter = random.uniform(-jitter_pct, jitter_pct) * base
                        wait_ms = max(0, base + jitter)
                        await asyncio.sleep(wait_ms / 1000.0)
                finally:
                    db.close()
            except Exception:
                pass
        if media_path:
            try:
                os.unlink(media_path)
            except Exception:
                pass
    except Exception:
        pass


set_on_private_message(_handle_private_copy)


@app.route("/api/groups/pool/check", methods=["POST"])
async def check_target_groups(request: Request):
    """重新检测目标群组可达性（§10「重新检测」）。只更新群组池，不建任务。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_ids = body.get("ids") or []
    ids = [int(i) for i in raw_ids if str(i).strip().lstrip("-").isdigit()] or None

    db: Session = SessionLocal()
    try:
        result = await recheck_pool(db, _discover_session_accounts(), ids)
    finally:
        db.close()
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


def _pool_join_groups(db: Session, ids) -> list[TargetGroup]:
    query = db.query(TargetGroup).order_by(TargetGroup.id.asc())
    if ids:
        query = query.filter(TargetGroup.id.in_([int(i) for i in ids]))
    return query.all()


@app.route("/api/groups/pool/join", methods=["POST"])
async def pool_join(request: Request):
    """批量加群：把目标群组池平均分给可用账号。

    mode=preview 只回分配预览；mode=start 起后台任务（逐个群真加，带延迟）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    accounts = _candidate_accounts(body.get("accounts") or [])
    if not accounts:
        return JSONResponse({"detail": "no_accounts_available"}, status_code=400)

    ready, blocked = join_service.classify_joinable_accounts(accounts)
    if not ready:
        return JSONResponse({
            "detail": "no_ready_accounts",
            "message": "没有可用账号，无法加群。请先到「健康检查」把账号检测成可用状态。",
            "blocked_reasons": blocked,
        }, status_code=400)

    db: Session = SessionLocal()
    try:
        groups = _pool_join_groups(db, body.get("ids") or [])
    finally:
        db.close()
    if not groups:
        return JSONResponse({"detail": "pool_empty", "message": "目标群组池是空的，先导入群组。"}, status_code=400)

    # 一个群只要有一个账号进去就能发，所以已经有账号的群不再重复加：
    # 否则每次点一次批量加群，就往同一个群里多塞一个账号（8002 实测平均每群 4.2 个账号）。
    selected = join_service.uncovered_groups(groups, ready)
    skipped_covered = len(groups) - len(selected)
    if not selected:
        return JSONResponse({
            "detail": "all_groups_covered",
            "message": f"这 {len(groups)} 个群里都已有账号加入，没有需要新加的群。"
                       "想重新加请先把群从群组池删掉再导入。",
            "groups": len(groups),
        }, status_code=400)

    assignment = join_service.build_assignment(selected, ready)
    result = {
        "ok": True,
        **join_service.preview(assignment),
        "groups_covered_skipped": skipped_covered,
        "accounts_ready": len(ready),
        "accounts_blocked": blocked,
    }

    if str(body.get("mode") or "preview") != "start":
        return JSONResponse({**result, "started": False})

    running = join_service.current_job()
    if running and running.get("running"):
        return JSONResponse({"detail": "join_running", "message": "上一批加群还没跑完，先等它结束或点停止。"}, status_code=409)

    delay_ms = max(int(body.get("delay_ms", 4000)), 1500)
    # 一个 worker = 一个账号，账号之间一号一代理、限流额度互不相干。
    # 串行跑 400 多个群要几个小时，所以并发上限放到账号数量级。
    workers = max(1, min(int(body.get("workers", 12)), 40))
    job = join_service.start_job(assignment, delay_ms, workers, accounts=ready)
    return JSONResponse({**result, "started": True, "job": job})


@app.route("/api/groups/pool/join", methods=["GET"])
async def pool_join_status(request: Request):
    """批量加群的实时进度（内存态，重启即丢）。"""
    return JSONResponse({"ok": True, "job": join_service.current_job()})


@app.route("/api/groups/pool/join/stop", methods=["POST"])
async def pool_join_stop(request: Request):
    if not join_service.request_stop():
        return JSONResponse({"detail": "join_not_running", "message": "当前没有在跑的加群任务。"}, status_code=400)
    return JSONResponse({"ok": True, "stopping": True})


GROUP_MSG_COOLDOWN_DEFAULT_S = 180


def _preset_payload(row: SendPreset) -> dict:
    return {
        "no": row.id,
        "name": row.name or "",
        "message": row.message or "",
        "parse_mode": row.parse_mode or "plain",
        "disable_web_page_preview": bool(row.disable_web_page_preview),
        "delay_ms": row.delay_ms or 60000,
        "rounds": row.rounds or 1,
        "round_interval_s": row.round_interval_s or 0,
        "group_msg_cooldown_s": 180 if row.group_msg_cooldown_s is None else row.group_msg_cooldown_s,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _preset_fields(body: dict) -> dict:
    return {
        "name": (body.get("name") or "").strip() or None,
        "message": body.get("message") or "",
        "parse_mode": body.get("parse_mode") or "plain",
        "disable_web_page_preview": 1 if body.get("disable_web_page_preview", True) else 0,
        "delay_ms": max(int(body.get("delay_ms", 60000)), getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500)),
        "rounds": max(1, int(body.get("rounds", 100))),
        "round_interval_s": max(0, int(body.get("round_interval_s", 600))),
        "group_msg_cooldown_s": max(0, int(body.get("group_msg_cooldown_s", GROUP_MSG_COOLDOWN_DEFAULT_S))),
    }


@app.route("/api/send-presets", methods=["GET"])
async def send_presets_list(request: Request):
    """保存下来的发送任务列表，编号就是 id。"""
    db: Session = SessionLocal()
    try:
        rows = db.query(SendPreset).order_by(SendPreset.id.asc()).all()
        return JSONResponse({"ok": True, "items": [_preset_payload(row) for row in rows]})
    finally:
        db.close()


@app.route("/api/send-presets/save", methods=["POST"])
async def send_presets_save(request: Request):
    """不带 no 就是新建一个编号；带 no 就是覆盖那个编号。"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    fields = _preset_fields(body)
    if not str(fields["message"]).strip():
        return JSONResponse({"detail": "message_required", "message": "任务正文不能为空。"}, status_code=400)

    no = body.get("no")
    db: Session = SessionLocal()
    try:
        row = db.query(SendPreset).filter(SendPreset.id == int(no)).first() if no else None
        if no and row is None:
            return JSONResponse({"detail": "preset_not_found", "message": f"没有 #{no} 号任务。"}, status_code=404)
        if row is None:
            row = SendPreset(**fields)
            db.add(row)
        else:
            for key, value in fields.items():
                setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return JSONResponse({"ok": True, "preset": _preset_payload(row)})
    finally:
        db.close()


@app.route("/api/send-presets/delete", methods=["POST"])
async def send_presets_delete(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    no = body.get("no")
    if not no:
        return JSONResponse({"detail": "no required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        deleted = db.query(SendPreset).filter(SendPreset.id == int(no)).delete(synchronize_session=False)
        db.commit()
        return JSONResponse({"ok": True, "deleted": deleted})
    finally:
        db.close()


# ---------------- 一键链路：补加群 → 生成方案 → 开始发送 ----------------
# 进度是进程内存态（跟批量加群一样，重启即丢），只用于给页面看，不伪造统计。

_pipeline: dict | None = None


def _pipeline_stage(run: dict, key: str) -> None:
    """进入下一个阶段：上一个还在跑的算跑完，这一个算开始。"""
    run["stage"] = key
    run["detail"] = ""
    for step in run["steps"]:
        if step["key"] == key:
            step["status"] = "running"
        elif step["status"] == "running":
            step["status"] = "done"


def _pipeline_detail(run: dict, text: str) -> None:
    run["detail"] = text
    for step in run["steps"]:
        if step["key"] == run["stage"]:
            step["detail"] = text


def _blocked_account_names(accounts: list[str]) -> list[str]:
    """没进发送池的账号名（多半是健康检查判的「无群发言权限」）。"""
    ready, _blocked = classify_ready_accounts(accounts)
    ok = set(ready)
    return [name for name in accounts if name not in ok]


async def _pipeline_recheck_step(run: dict) -> None:
    """只重检被挡的账号，不做全量检测。

    绝大多数「无群发言权限」是当时一个群都没加、没有群可以实测，加完群就该放回发送池。
    inspect 只读群权限，不会真的发消息。

    「预热未毕业」是另一种被挡，重检修不了：门禁看的是 readiness 分数，跟登录状态无关。
    这类账号必须先摘出去，否则用户会反复点重检、每次都「救回 0 个」，还看不出为什么。
    """
    accounts = _candidate_accounts([])
    pending = _blocked_account_names(accounts)
    # 被平台限制发言的号重检也查不出来（只读权限看不出封禁），别让它把状态刷回「正常」
    pending = [name for name in pending if not _account_send_blocked(name)]
    if not pending:
        _pipeline_detail(run, "没有被挡账号，无需重检")
        return

    warmed = warmup.gate_many(pending)
    warming = [name for name in pending if name not in warmed]
    pending = [name for name in pending if name not in warming]
    if not pending:
        _pipeline_detail(run, f"{len(warming)} 个账号预热未毕业，重检无效，得等预热跑完才会放行")
        return

    sem = asyncio.Semaphore(3)
    done = 0

    async def one(name: str) -> None:
        nonlocal done
        if run["stop"]:
            return
        async with sem:
            try:
                await account_service.check_account(name, use_cache=False, include_group_send_check=True)
            except Exception as exc:
                print(f"[PIPELINE] 重检 {name} 失败: {exc}")
        done += 1
        _pipeline_detail(run, f"已重检 {done}/{len(pending)} 个被挡账号…")

    await asyncio.gather(*(one(name) for name in pending))
    still = [name for name in _blocked_account_names(accounts) if name not in warming]
    summary = f"重检 {len(pending)} 个被挡账号，救回 {len(pending) - len(still)} 个，仍被挡 {len(still)} 个"
    if warming:
        summary += f"；另有 {len(warming)} 个账号预热未毕业，重检对它们无效"
    _pipeline_detail(run, summary)


LIVE_JOIN_DELAY_MS = max(1500, int(os.getenv("LIVE_JOIN_DELAY_MS", "8000")))
# 每个群希望保有几个「没被禁言」的成员账号。没显式配 LIVE_COVERAGE_TARGET 时
# 跟随 JOIN_ACCOUNTS_PER_GROUP：那边设成 1 就不用为了备胎把所有群多加一遍，
# 等真的被禁言了再按缺口补人（_coverage_join_assignment 会自己算）。
LIVE_COVERAGE_TARGET = max(
    1,
    int(os.getenv("LIVE_COVERAGE_TARGET") or os.getenv("JOIN_ACCOUNTS_PER_GROUP", "2")),
)
# 检测结果过期后，常驻链路自己补检的最小间隔：不等用户手点，也不会每 30 秒狂打 Telegram。
LIVE_AUTO_RECHECK_S = max(60, int(os.getenv("LIVE_AUTO_RECHECK_S", "300")))
# 控制循环的节拍：检查覆盖缺口、补任务、推进加群。
LIVE_CONTROL_INTERVAL_S = max(10, int(os.getenv("LIVE_CONTROL_INTERVAL_S", "30")))
# 同时加群的账号数上限。20 个号一起对着 Telegram 加群会立刻吃 FloodWait，
# 不如少开几个、稳稳地加；账号多了再往上调。
LIVE_JOIN_WORKERS = max(1, int(os.getenv("LIVE_JOIN_WORKERS", "8")))
# 每个账号多久从 Telegram 重新拉一次真实群列表（轮流刷新，不会同时打满）。
LIVE_MEMBERSHIP_REFRESH_S = max(300, int(os.getenv("LIVE_MEMBERSHIP_REFRESH_S", "900")))
# 刚加进去的群多久内不允许被「会话列表里没有它」摘掉成员关系：
# 拉列表和加群是并发的，列表可能比加群早，直接删会把刚加成功的群判成没加。
LIVE_FRESH_JOIN_GRACE_S = max(60, int(os.getenv("LIVE_FRESH_JOIN_GRACE_S", "600")))
# 一个账号在一轮里被几个群禁言，就先停手冷静多久。被禁言说明这个号已经被盯上，
# 继续往剩下的群硬发只会换来更多禁言甚至封号；停下来让别的号去发。
MUTE_CIRCUIT_THRESHOLD = max(1, int(os.getenv("MUTE_CIRCUIT_THRESHOLD", "3")))
MUTE_CIRCUIT_COOLDOWN_S = max(60, int(os.getenv("MUTE_CIRCUIT_COOLDOWN_S", "1800")))

# 群发节奏改成「按群池规模反推」：全池每小时想发多少条，就决定了每个群该隔多久再发。
# 群池越大同一群可以隔得越久（更安全），群池小则自动收紧。0 = 关闭，回到预设里的固定冷却。
SEND_TARGET_PER_HOUR = max(0, int(os.getenv("LIVE_SEND_TARGET_PER_HOUR", "240")))
# 同一个群不管换几个账号，两次投递之间至少隔这么久，避免两个号把同一个群连发。
GROUP_MIN_GAP_S = max(0, int(os.getenv("GROUP_MIN_GAP_S", "1800")))


# 发送前查发言权限的并发：每个账号一轮查几十个群，串行会拖慢整轮
PROBE_CONCURRENCY = max(1, int(os.getenv("PROBE_CONCURRENCY", "5")))
# 单个群最多等多久：账号同时还在加群时会被 Telegram 限流，不能让它把整轮发送卡死
PROBE_TIMEOUT_S = max(2.0, float(os.getenv("PROBE_TIMEOUT_S", "6")))
# 一轮最多预检几个群：剩下的直接发，失败自然会记进黑名单
PROBE_MAX_GROUPS = max(0, int(os.getenv("PROBE_MAX_GROUPS", "20")))


async def _probe_send_permissions(account: str, group_ids: list[int]) -> tuple[list[int], list[int], list[int], list[int]]:
    """发送前逐群查一次发言权限，返回（可发、本号被禁言、只读群、已不在群）。

    以前是「发一条才知道被禁言」：每撞一次都要记一条失败，群里还会留下痕迹。
    禁言状态本来就能只读查出来，先查再发能把这类失败从「发送失败」变成「不发送」。
    只查前 PROBE_MAX_GROUPS 个，且单个群超时就当成未知（照常发），
    避免账号同时在加群、被限流时把整轮卡住。
    """
    sem = asyncio.Semaphore(PROBE_CONCURRENCY)
    targets = group_ids[:PROBE_MAX_GROUPS]
    rest = group_ids[len(targets):]

    async def one(gid: int) -> tuple[int, str]:
        async with sem:
            try:
                return gid, await asyncio.wait_for(
                    multi_manager.get_send_permission(account, gid), PROBE_TIMEOUT_S)
            except Exception:
                return gid, "unknown"

    usable: list[int] = []
    muted: list[int] = []
    readonly: list[int] = []
    gone: list[int] = []
    for gid, state in await asyncio.gather(*(one(gid) for gid in targets)):
        if state == "blocked":
            muted.append(gid)
        elif state == "group_readonly":
            readonly.append(gid)
        elif state == "not_member":
            gone.append(gid)
        else:
            usable.append(gid)
    return usable + rest, muted, readonly, gone


def _forget_group_membership(db: Session, account: str, group_id: int) -> None:
    """账号已经不在这个群了：把成员关系摘掉，让加群进程换号补上。"""
    row = db.query(TargetGroup).filter(TargetGroup.group_id == group_id).first()
    if not row:
        return
    try:
        members = set(json.loads(row.joined_by_json or "[]"))
    except Exception:
        members = set()
    if account in members:
        members.discard(account)
        row.joined_by_json = json.dumps(sorted(members))


# 账号被 Telegram 限制在群组发言后，先停多久再让它试
ACCOUNT_RESTRICT_COOLDOWN_S = max(600, int(os.getenv("ACCOUNT_RESTRICT_COOLDOWN_S", "86400")))
# 连续多少个群都是「群没禁言我、我还是发不出去」，才判定账号被平台限制
# （单个群可能是群里的机器人/审核，误报一次不能把好号停掉）
ACCOUNT_SPAM_BAN_STREAK = max(2, int(os.getenv("ACCOUNT_SPAM_BAN_STREAK", "3")))
# account -> 解禁时间点（epoch 秒）：这段时间内边加群边群发不再给它派任务、也不再让它加群
_ACCOUNT_SEND_BLOCK: dict[str, float] = {}
_SEND_BLOCK_KV_KEY = "send_blocked_accounts"
_SEND_BLOCK_LOADED = False


def _load_send_blocks() -> None:
    """重启后把「被平台限制发言」的账号读回来，别让它们又去撞一轮。"""
    global _SEND_BLOCK_LOADED
    if _SEND_BLOCK_LOADED:
        return
    _SEND_BLOCK_LOADED = True
    try:
        with SessionLocal() as db:
            row = db.query(SystemKV).filter(SystemKV.k == _SEND_BLOCK_KV_KEY).first()
            data = json.loads(row.v or "{}") if row else {}
    except Exception:
        return
    now = time.time()
    for name, until in (data or {}).items():
        try:
            if float(until) > now:
                _ACCOUNT_SEND_BLOCK[name] = float(until)
        except Exception:
            continue


def _expire_send_blocks() -> None:
    """限制发言到期就把账号放回池子。

    必须主动清：被 blocked 的账号不在 ready 里，`_account_send_blocked` 根本没机会
    被调用，状态就会一直停在 send_banned —— 表现出来就是「账号自动停止后再也不动」。
    """
    _load_send_blocks()
    now = time.time()
    for name, until in list(_ACCOUNT_SEND_BLOCK.items()):
        if until > now:
            continue
        _ACCOUNT_SEND_BLOCK.pop(name, None)
        account_service.clear_send_block(name)


def _mark_account_send_blocked(account: str, detail: str) -> None:
    """账号被平台限制发言：当场标记，别让它继续拿剩下的群一条条去撞。"""
    until = time.time() + ACCOUNT_RESTRICT_COOLDOWN_S
    _ACCOUNT_SEND_BLOCK[account] = until
    try:
        with SessionLocal() as db:
            row = db.query(SystemKV).filter(SystemKV.k == _SEND_BLOCK_KV_KEY).first()
            data = json.loads(row.v or "{}") if row else {}
            data[account] = until
            payload = json.dumps(data, ensure_ascii=False)
            if row:
                row.v = payload
            else:
                db.add(SystemKV(k=_SEND_BLOCK_KV_KEY, v=payload))
            db.commit()
    except Exception:
        pass
    account_service.mark_account_send_blocked(account, detail)
    print(f"[LIVE] {account} 已被 Telegram 限制在群组发言，暂停 {ACCOUNT_RESTRICT_COOLDOWN_S // 60} 分钟")


def _account_send_blocked(account: str) -> bool:
    _load_send_blocks()
    until = _ACCOUNT_SEND_BLOCK.get(account)
    if not until:
        return False
    if time.time() >= until:
        _ACCOUNT_SEND_BLOCK.pop(account, None)
        account_service.clear_send_block(account)
        return False
    return True


# 体检不许把「限制发言」刷成正常：把冻结判断注入给 account_service
account_service_module.SEND_BLOCK_CHECK = _account_send_blocked


def _stop_account_running_tasks(db: Session, account: str) -> int:
    """停掉该账号还在跑的旧任务，避免新模式和旧任务同时往同一个群发两遍。"""
    now = CONFIG.now()
    rows = (
        db.query(Task)
        .filter(Task.account_name == account, Task.status.in_(("running", "scheduled")))
        .all()
    )
    for task in rows:
        task.stop_requested = 1
        task.status = "stopped"
        task.finished_at = now
        db.add(TaskEvent(task_id=task.id, event="stopped", detail="superseded_by_live_mode",
                         meta_json=json.dumps({"account": account}, ensure_ascii=False)))
    return len(rows)


def _create_live_task(db: Session, account: str, group_ids: list[int], preset: dict) -> str:
    """创建动态群池任务：每轮自己从群池重算「已加入且可发」的群。"""
    task_id = uuid.uuid4().hex[:24]
    task = Task(
        id=task_id,
        status="running",
        total=len(group_ids),
        success=0,
        failed=0,
        account_name=account,
        message=preset["message"],
        parse_mode=preset["parse_mode"],
        disable_web_page_preview=1 if preset["disable_web_page_preview"] else 0,
        delay_ms=preset["delay_ms"],
        delay_scope="per_account",
        rounds=preset["rounds"],
        round_interval_s=preset["round_interval_s"],
        current_index=0,
        group_ids_json=json.dumps(group_ids),
        heartbeat_at=CONFIG.now(),
        # 冷却按预设走：用户设 180 秒就 180 秒，不再偷偷改成一天。
        # 想更保守就把预设里的「同群同文案冷却」调大（最大 86400）。
        group_msg_cooldown_s=max(
            0, int(preset.get("group_msg_cooldown_s", GROUP_MSG_COOLDOWN_DEFAULT_S))),
        dynamic_groups=1,
    )
    db.add(task)
    db.add(TaskEvent(task_id=task_id, event="created", detail="live_task_created",
                     meta_json=json.dumps({"count": len(group_ids)}, ensure_ascii=False)))
    return task_id


def _coverage_join_assignment(db: Session, accounts: list[str], target: int) -> dict[str, list]:
    """按「每个群至少要有 target 个可发成员」算缺口，并把缺口分给最闲的账号。

    只补缺：已经有足够成员、且这些成员没被禁言的群不会再塞账号。
    """
    ready = set(accounts)
    groups = _pool_join_groups(db, None)
    # 群池是按导入顺序返回的；先打乱，让同一优先级里各账号从不同位置开始，
    # 否则所有账号都从头加，前几个群被反复抢、后面的群迟迟没人加。
    random.shuffle(groups)
    # 优先级：先加「一个账号都没有」的群，再补有成员但覆盖不足的群。
    # 稳定排序，所以同一档里仍然是随机的。
    groups.sort(key=lambda group: len(join_service.members_of(group)))
    banned = {name: set(get_banned_group_ids(db, name)) for name in accounts}
    assignment: dict[str, list] = {name: [] for name in accounts}
    load = {name: 0 for name in accounts}

    for group in groups:
        if str(group.status or "") in _SEND_BLOCKED_GROUP_STATUSES:
            continue
        members = set(join_service.members_of(group)) & ready
        gid = int(group.group_id) if group.group_id is not None else None
        usable = [
            name for name in members
            if gid is None or gid not in banned.get(name, set())
        ]
        need = target - len(usable)
        if need <= 0:
            continue
        candidates = [
            name for name in accounts
            if name not in members and (gid is None or gid not in banned.get(name, set()))
        ]
        candidates.sort(key=lambda name: load.get(name, 0))
        for name in candidates[:need]:
            assignment[name].append(group)
            load[name] = load.get(name, 0) + 1
    # 分配时就是按「空群优先」的顺序 append 的，所以每个账号自己的队列天然有序。
    return {name: items for name, items in assignment.items() if items}


async def _refresh_account_membership(account: str) -> int:
    """从 Telegram 重拉这个账号的群列表，修正群池里的 joined_by_json。

    本地记录会过时（被踢、自己退群、在别处加了群）。不刷新就会把
    「其实已经不在群里」的账号当成可发，或者漏掉新加的群。
    """
    # 拉列表前先记下「谁已经在群里」：拉取期间刚加进去的群不会出现在这次列表里，
    # 不能因为「列表里没有」就把刚写好的成员关系删掉。
    before: dict[int, set[str]] = {}
    snapshot_db: Session = SessionLocal()
    try:
        for row_id, joined_json in snapshot_db.query(
                TargetGroup.id, TargetGroup.joined_by_json).all():
            try:
                before[int(row_id)] = set(json.loads(joined_json or "[]"))
            except Exception:
                before[int(row_id)] = set()
    finally:
        snapshot_db.close()

    try:
        rows = await asyncio.wait_for(
            multi_manager.get_joined_groups(account, only_groups=False), timeout=90)
    except Exception as exc:
        print(f"[LIVE] 刷新 {account} 群列表失败：{type(exc).__name__}")
        return 0
    if not rows:
        # 网络抖动也会返回空，宁可这轮不更新，也不能把成员关系清空
        return 0

    ids: set[int] = set()
    names: set[str] = set()
    for row in rows:
        for key in ("id", "raw_id"):
            if row.get(key) is not None:
                try:
                    ids.add(int(row[key]))
                except Exception:
                    pass
        if row.get("username"):
            names.add(str(row["username"]).strip().lstrip("@").lower())

    db: Session = SessionLocal()
    changed = 0
    try:
        for group in db.query(TargetGroup).all():
            try:
                members = set(json.loads(group.joined_by_json or "[]"))
            except Exception:
                members = set()
            present = False
            if group.group_id is not None:
                try:
                    present = int(group.group_id) in ids
                except Exception:
                    present = False
            if not present and group.username:
                uname = str(group.username).strip().lstrip("@").lower()
                present = bool(uname) and uname in names
            if present and account not in members:
                members.add(account)
                group.joined_by_json = json.dumps(sorted(members))
                changed += 1
            elif not present and account in members:
                # 加群和刷新是并发的：刚加进去的群可能还没出现在这次拉到的列表里，
                # 直接删会把刚写好的成员关系冲掉（实测同一批群反复「加成功 → 没人」）。
                if account not in before.get(int(group.id), set()):
                    continue
                # 时间保护只兜「加群早于本次拉取、但服务端列表还是旧的」这一种
                if (group.last_run_at is not None
                        and _seconds_since_task_timestamp(group.last_run_at) < LIVE_FRESH_JOIN_GRACE_S):
                    continue
                members.discard(account)
                group.joined_by_json = json.dumps(sorted(members))
                changed += 1
        if changed:
            db.commit()
    except Exception as exc:
        db.rollback()
        print(f"[LIVE] 写入 {account} 群列表失败：{type(exc).__name__}")
    finally:
        db.close()
    return changed


def _live_task_needs_rebuild(status: str | None) -> bool:
    """常驻模式里这条任务还能不能接着跑。

    done/completed 是自然结束；stopped/error 是账号被平台限制发言或连接失败后自己停的。
    这些都必须丢掉旧任务、按当前群池重建：否则会把一条 `stop_requested=1` 的旧任务
    反复拉起来，runner 一进循环就自己停，账号看起来永远不再发消息。
    """
    return status is None or status in ("done", "completed", "stopped", "error")


async def _run_live_pipeline(run: dict, preset: dict) -> None:
    """边加群边群发（常驻）：刷新真实成员关系 → 补覆盖缺口 → 动态任务持续群发。

    和普通链路的区别：它不会「启动任务就结束」，而是一直循环：
    - 轮流从 Telegram 重拉每个账号的真实群列表，修正 joined_by_json
    - 按「每个群至少 target 个可发成员」补缺口，加群慢但不停
    - 发送任务每轮从群池重算，新加入的群自动进入发送列表
    """
    started: dict[str, str] = {}
    last_refresh: dict[str, float] = {}
    last_auto_recheck = 0.0
    try:
        _pipeline_stage(run, "recheck")
        await _pipeline_recheck_step(run)
        if run["stop"]:
            run["status"] = "stopped"
            return

        _pipeline_stage(run, "live")
        while not run["stop"]:
            # 限制发言到期先放回池子，否则这些账号永远进不了 ready
            _expire_send_blocks()
            # 每轮重算：预热读通会把「未检测」刷成可用，不用重启或手动点检测
            accounts = _candidate_accounts([])
            ready, blocked = join_service.classify_joinable_accounts(accounts)
            if not ready:
                reasons = "、".join(f"{k} {v}" for k, v in (blocked or {}).items()) or "先去健康检查检测账号"
                if time.monotonic() - last_auto_recheck >= LIVE_AUTO_RECHECK_S:
                    # 检测结果只有 30 分钟有效期。以前过期后链路就一直挂着等用户手点，
                    # 期间新导入的群也不会被加。这里自己补检，检完下一轮自动继续。
                    last_auto_recheck = time.monotonic()
                    _pipeline_detail(run, f"暂无可加群账号（{reasons}），正在自动重检…")
                    await _pipeline_recheck_step(run)
                else:
                    _pipeline_detail(run, f"暂无可加群账号（{reasons}），恢复后自动继续")
                await asyncio.sleep(LIVE_CONTROL_INTERVAL_S)
                continue
            # 被 Telegram 限制发言的号先不进池子：既不发也不加群
            active = [name for name in ready if not _account_send_blocked(name)]
            if not active:
                # 一个能发的号都没有：加群也没意义，先把加群停掉并说清楚原因
                job = join_service.current_job()
                if job and job.get("running"):
                    join_service.request_stop()
                _pipeline_detail(
                    run,
                    f"{len(ready)} 个账号全部被 Telegram 限制在群组发言，已暂停；"
                    f"限制到期后会自动恢复，或先导入新账号",
                )
                await asyncio.sleep(LIVE_CONTROL_INTERVAL_S)
                continue
            # 1) 给还没有任务的账号建动态任务（有群才建，避免空任务）
            #    预热没毕业的号可以照常加群，但先别发：新号一上来就群发最容易吃 spam ban。
            states = warmup.load_states(active)
            exempt = warmup.exempt_accounts()
            warmed = {name for name in active if warmup.gate_state(states[name], name in exempt)[0]}
            db: Session = SessionLocal()
            try:
                for account in active:
                    if account in started or account not in warmed:
                        continue
                    ids = _account_pool_group_ids(db, account)
                    if not ids:
                        continue
                    _stop_account_running_tasks(db, account)
                    started[account] = _create_live_task(db, account, ids, preset)
                db.commit()
            finally:
                db.close()

            for account, task_id in list(started.items()):
                if _account_send_blocked(account):
                    continue
                if _task_runner_active(task_id):
                    continue
                # 用户在面板里清过任务：旧 id 已经不存在，重建一条，别在空 id 上空转
                db = SessionLocal()
                try:
                    row = db.query(Task.id, Task.status).filter(Task.id == task_id).first()
                finally:
                    db.close()
                if row is None or _live_task_needs_rebuild(row[1]):
                    # 任务跑完 / 被停掉（含被平台限制发言后自己停的）：丢掉旧 id，
                    # 下一轮按当前群池重建一条新的。
                    # 以前只认 done/completed，被限制发言停掉的任务会一直被重新拉起，
                    # 而它的 stop_requested 还是 1，一进循环就自己停——表现出来就是
                    # 「账号冻结到期了，却再也没发过消息」。
                    started.pop(account, None)
                    continue
                _start_send_task_runner(
                    task_id=task_id,
                    account=account,
                    # 动态任务每轮会自己从群池重算，这里不需要初始群列表
                    group_ids=[],
                    message=preset["message"],
                    parse_mode=preset["parse_mode"],
                    disable_web_page_preview=preset["disable_web_page_preview"],
                    delay_ms=preset["delay_ms"],
                    rounds=preset["rounds"],
                    round_interval_s=preset["round_interval_s"],
                )

            # 2) 没人在加群时，按覆盖缺口继续加
            job = join_service.current_job()
            if not (job and job.get("running")):
                db = SessionLocal()
                try:
                    assignment = _coverage_join_assignment(db, active, LIVE_COVERAGE_TARGET)
                finally:
                    db.close()
                if assignment:
                    total = sum(len(items) for items in assignment.values())
                    join_service.start_job(
                        assignment,
                        LIVE_JOIN_DELAY_MS,
                        workers=max(1, min(len(active), LIVE_JOIN_WORKERS)),
                        accounts=active,
                    )
                    print(f"[LIVE] 覆盖缺口加群：{total} 个群")

            # 3) 每轮只刷新一个账号的真实群列表，轮流刷新
            now = time.monotonic()
            for account in active:
                if now - last_refresh.get(account, 0.0) < LIVE_MEMBERSHIP_REFRESH_S:
                    continue
                changed = await _refresh_account_membership(account)
                last_refresh[account] = time.monotonic()
                if changed:
                    print(f"[LIVE] {account} 群成员关系已刷新：{changed} 处")
                break

            job = join_service.current_job()
            # 「被平台限制发言」的号不在 ready 里，得单独数出来，不然界面上会凭空少几个号
            platform_blocked = [name for name in accounts if _account_send_blocked(name)]
            paused = len(platform_blocked)
            warming = len(active) - len(warmed)
            warming_best = max(
                (int(states[name].get("readiness") or 0) for name in active if name not in warmed),
                default=0,
            )
            join_state = "准备下一批"
            if job and job.get("running"):
                join_state = f"进行中 {job.get('done', 0)}/{job.get('total', 0)}"
                if job.get("redispatches"):
                    join_state += f" · 换号 {job['redispatches']}"
            _pipeline_detail(
                run,
                f"群发任务 {len(started)}/{len(active)} 个账号"
                + (f" · {paused} 个账号被平台限制发言，到期自动恢复" if paused > 0 else "")
                + (f" · {warming} 个账号预热中（最高 {warming_best}/{warmup.ready_score()} 分）" if warming > 0 else "")
                + f" · 加群 {join_state}"
                + f" · 群成员每 {LIVE_MEMBERSHIP_REFRESH_S // 60} 分钟刷新一次",
            )
            await asyncio.sleep(LIVE_CONTROL_INTERVAL_S)

        join_service.request_stop()
        db = SessionLocal()
        try:
            for task_id in started.values():
                task = db.query(Task).filter(Task.id == task_id).first()
                if task and task.status == "running":
                    task.stop_requested = 1
            db.commit()
        finally:
            db.close()
        run["status"] = "stopped"
    except Exception as exc:
        run["status"] = "failed"
        run["error"] = str(exc)[:400]
        for step in run["steps"]:
            if step["key"] == run["stage"]:
                step["status"] = "failed"
    finally:
        run["running"] = False
        run["finished_at"] = CONFIG.now().isoformat()
        run["result"] = {
            "ok": run["status"] == "stopped",
            "mode": "live",
            "accounts_count": len(started),
            "tasks": [{"account": a, "task_id": t} for a, t in started.items()],
        }


@app.route("/api/pipeline/start-live", methods=["POST"])
async def pipeline_start_live(request: Request):
    """边加群边群发：加群在后台持续跑，发送任务每轮从群池重算可发群。"""
    global _pipeline
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    if _pipeline and _pipeline.get("running"):
        return JSONResponse({"detail": "pipeline_running", "message": "上一次链路还在跑，先等它结束或点停止。"}, status_code=400)
    no = body.get("preset_no")
    if not no:
        return JSONResponse({"detail": "preset_no_required", "message": "先选择要发送的几号任务。"}, status_code=400)

    db: Session = SessionLocal()
    try:
        row = db.query(SendPreset).filter(SendPreset.id == int(no)).first()
        if row is None:
            return JSONResponse({"detail": "preset_not_found", "message": f"没有 #{no} 号任务。"}, status_code=404)
        preset = _preset_payload(row)
    finally:
        db.close()

    _start_live_pipeline(preset)
    return JSONResponse({"ok": True, "pipeline": _pipeline})


# 记住正在跑的「边加群边群发」预设：容器重启是常驻模式最怕的事，重启后自动接着跑
LIVE_PIPELINE_KV_KEY = "live_pipeline_preset"


def _start_live_pipeline(preset: dict) -> dict:
    global _pipeline
    _pipeline = {
        "running": True,
        "stop": False,
        "status": "running",
        "stage": "",
        "detail": "",
        "error": None,
        "mode": "live",
        "preset_no": preset["no"],
        "preset_name": preset["name"],
        "join_delay_ms": LIVE_JOIN_DELAY_MS,
        "steps": [
            {"key": "recheck", "label": "自动重检", "status": "pending", "detail": ""},
            {"key": "live", "label": "边加群边群发", "status": "pending", "detail": ""},
        ],
        "result": None,
        "started_at": CONFIG.now().isoformat(),
        "finished_at": None,
    }
    _kv_set(LIVE_PIPELINE_KV_KEY, str(preset["no"]))
    asyncio.create_task(_run_live_pipeline(_pipeline, preset))
    return _pipeline


def _app_time_naive(value) -> datetime | None:
    """把带时区的时间/ISO 串换成「应用时区的裸时间」，好和库里存的日志时间比。"""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except Exception:
            return None
    if value is None:
        return None
    if value.tzinfo is not None:
        try:
            value = value.astimezone(CONFIG.get_timezone())
        except Exception:
            pass
        value = value.replace(tzinfo=None)
    return value


def _pipeline_run_stats(run: dict | None) -> dict:
    """本次执行（点开始 → 现在/停止）总共发出去多少条、加进多少个新群。

    直接按时间区间数日志，不做内存计数器：停止时间一确定区间就封口，
    统计自己就停了；下次点开始 started_at 换新，统计自动归零。
    """
    empty = {"send_success": 0, "send_failed": 0, "join_success": 0}
    window = _pipeline_run_window(run)
    if window is None:
        return empty
    start, end = window
    db = SessionLocal()
    try:
        send_q = db.query(func.count(SendLog.id)).filter(SendLog.created_at >= start)
        join_q = db.query(func.count(JoinLog.id)).filter(
            JoinLog.status == "joined", JoinLog.created_at >= start)
        if end is not None:
            send_q = send_q.filter(SendLog.created_at <= end)
            join_q = join_q.filter(JoinLog.created_at <= end)
        return {
            "send_success": int(send_q.filter(SendLog.status == "success").scalar() or 0),
            "send_failed": int(send_q.filter(SendLog.status != "success").scalar() or 0),
            "join_success": int(join_q.scalar() or 0),
        }
    except Exception:
        return empty
    finally:
        db.close()


def _pipeline_run_window(run: dict | None) -> tuple[datetime, datetime | None] | None:
    """本次执行的日志时间区间：[点开始, 停止]；还在跑就只有起点。"""
    if not run or not run.get("started_at"):
        return None
    start = _app_time_naive(run.get("started_at"))
    if start is None:
        return None
    end = _app_time_naive(run.get("finished_at")) if run.get("finished_at") else None
    return start, end


@app.route("/api/pipeline/logs", methods=["GET"])
async def pipeline_logs(request: Request):
    """本次执行的完整日志（群发 / 加群）。

    日志一直留在库里不清空，只是按本次执行的时间区间取，所以每启动一次任务
    都会从 0 开始统计，而之前几次的日志仍然完整保存、可回查。
    """
    kind = (request.query_params.get("kind") or "send").lower()
    try:
        limit = max(1, min(int(request.query_params.get("limit", 300)), 1000))
        offset = max(0, int(request.query_params.get("offset", 0)))
    except Exception:
        limit, offset = 300, 0

    window = _pipeline_run_window(_pipeline)
    if window is None:
        return JSONResponse({"ok": True, "items": [], "total": 0,
                             "started_at": None, "finished_at": None})
    start, end = window
    model = JoinLog if kind == "join" else SendLog
    db: Session = SessionLocal()
    try:
        q = db.query(model).filter(model.created_at >= start)
        if end is not None:
            q = q.filter(model.created_at <= end)
        total = q.count()
        rows = q.order_by(model.created_at.desc()).offset(offset).limit(limit).all()
        items = [
            {
                "id": r.id,
                "account_name": r.account_name,
                "group_id": r.group_id,
                "group_title": getattr(r, "group_title", None),
                "status": r.status,
                "error": r.error,
                "created_at": _serialize_log_created_at(r.created_at),
            }
            for r in rows
        ]
        return JSONResponse({
            "ok": True,
            "items": items,
            "total": int(total),
            "started_at": _pipeline["started_at"] if _pipeline else None,
            "finished_at": _pipeline.get("finished_at") if _pipeline else None,
            "frozen": bool(_pipeline and _pipeline.get("finished_at")),
        })
    finally:
        db.close()


@app.route("/api/pipeline/status", methods=["GET"])
async def pipeline_status(request: Request):
    run = {**_pipeline, "stats": _pipeline_run_stats(_pipeline)} if _pipeline else None
    return JSONResponse({"ok": True, "pipeline": run})


@app.route("/api/pipeline/stop", methods=["POST"])
async def pipeline_stop(request: Request):
    if not _pipeline or not _pipeline.get("running"):
        return JSONResponse({"detail": "pipeline_not_running", "message": "当前没有在跑的链路。"}, status_code=400)
    _pipeline["stop"] = True
    if _pipeline.get("mode") == "live":
        # 用户明确点了停止：重启后别再自己拉起来
        _kv_set(LIVE_PIPELINE_KV_KEY, "")
    return JSONResponse({"ok": True, "stopping": True})


@app.route("/api/send-async", methods=["POST"])
async def send_async(request: Request):
    token = request.headers.get("X-Admin-Token")
    body = await request.json()
    group_ids = body.get("group_ids") or []
    message = (body.get("message") or "").strip()
    parse_mode = body.get("parse_mode") or "plain"
    disable_web_page_preview = bool(body.get("disable_web_page_preview", True))
    delay_ms = int(body.get("delay_ms", 11000))  # 默认 11 秒
    delay_ms = max(delay_ms, getattr(CONFIG, "SEND_MIN_DELAY_MS", 1500))
    delay_scope = _normalize_delay_scope(body.get("delay_scope"), "per_account")
    rounds = max(1, int(body.get("rounds", 100)))
    round_interval_s = int(body.get("round_interval_s", 600))
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    request_id = body.get("request_id")
    ok, _reason = _check_request_guard(token, request_id)
    if not ok:
        return JSONResponse({"detail": "Too Many Requests"}, status_code=429, headers={"Retry-After": "1"})
    authorized = await _is_authorized_account(account)
    if not authorized:
        return JSONResponse({"detail": "session_not_authorized"}, status_code=403)
    policy_db = SessionLocal()
    try:
        blocked = _proxy_policy_missing(policy_db, [account])
    finally:
        policy_db.close()
    if blocked:
        return JSONResponse({
            "detail": "proxy_required_per_account",
            "message": f"已开启「强制一账号一代理」：账号 {account} 没有绑定代理，无法开始任务。可在「代理 IP 配置」绑定代理，或关闭该开关。",
            "accounts": blocked,
        }, status_code=400)
    group_ids = unique_group_ids(group_ids)
    if not group_ids or not message:
        return JSONResponse({"detail": "group_ids and message required"}, status_code=400)
    task_id = uuid.uuid4().hex[:24]
    db: Session = SessionLocal()
    try:
        existing = _get_existing_tasks_by_request_id(db, request_id)
        if existing:
            return JSONResponse({
                "task_id": existing[0].id,
                "duplicate": True,
                "delay_scope": _normalize_delay_scope(getattr(existing[0], "delay_scope", None), delay_scope),
            })
        t = Task(
            id=task_id,
            status="running",
            total=len(group_ids),
            success=0,
            failed=0,
            account_name=account,
            message=message,
            parse_mode=parse_mode,
            disable_web_page_preview=1 if disable_web_page_preview else 0,
            delay_ms=delay_ms,
            delay_scope=delay_scope,
            rounds=rounds,
            round_interval_s=round_interval_s,
            current_index=0,
            group_ids_json=json.dumps(group_ids),
            request_id=request_id,
            heartbeat_at=CONFIG.now(),
        )
        db.add(t)
        db.add(TaskEvent(task_id=task_id, event="created", detail="task_created", meta_json=json.dumps({"count": len(group_ids)}, ensure_ascii=False)))
        try:
            db.commit()
        except DatabaseError as exc:
            db.rollback()
            _handle_task_db_error("send_async_create", exc)
            return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("send_async_query", exc)
        return JSONResponse({"detail": "task_db_unavailable"}, status_code=503)
    finally:
        db.close()
    _start_send_task_runner(
        task_id=task_id,
        account=account,
        group_ids=group_ids,
        message=message,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
        delay_ms=delay_ms,
        rounds=rounds,
        round_interval_s=round_interval_s,
        delay_scope=delay_scope,
    )
    return JSONResponse({"task_id": task_id, "delay_scope": delay_scope})


@app.route("/api/tasks/summary")
async def tasks_summary(request: Request):
    db: Session = SessionLocal()
    try:
        rows = (
            db.query(Task)
            .order_by(Task.started_at.desc())
            .limit(300)
            .all()
        )
        selected_tasks: dict[str, Task] = {}
        for t in rows:
            k = t.account_name
            existing = selected_tasks.get(k)
            current_ts = t.heartbeat_at or t.finished_at or t.started_at
            existing_ts = None
            if existing:
                existing_ts = existing.heartbeat_at or existing.finished_at or existing.started_at

            # Running tasks always win. Otherwise keep the most recent task
            # so the monitor panel can still show failed/stopped/done tasks.
            should_replace = False
            if not existing:
                should_replace = True
            elif existing.status != "running" and t.status == "running":
                should_replace = True
            elif existing.status != "running" and t.status != "running":
                if current_ts and ((not existing_ts) or current_ts > existing_ts):
                    should_replace = True

            if not should_replace:
                continue
            selected_tasks[k] = t

        current_round_by_task = {
            t.id: int(t.current_round or 0)
            for t in selected_tasks.values()
            if t and t.id
        }
        counters_by_task = _collect_task_log_counters(
            db,
            list(current_round_by_task.keys()),
            current_round_by_task,
            {t.id: t for t in selected_tasks.values() if t and t.id},
        )

        data = []
        for account, t in selected_tasks.items():
            current_ts = t.heartbeat_at or t.finished_at or t.started_at
            round_total = _task_round_total(t)
            counters = counters_by_task.get(t.id, {})
            round_completed = min(
                max(0, int(counters.get("current_round_completed", 0))),
                round_total,
            )
            overall_completed = int(counters.get("overall_completed", 0))
            previous_rounds_sent = max(0, overall_completed - round_completed)
            overall_planned = previous_rounds_sent + round_total
            next_round_at = t.next_round_at.isoformat() if t.next_round_at else None
            wait_seconds = 0
            status = t.status
            if t.status == "running" and t.next_round_at:
                # 下一轮时间已经排好 = 当前轮已结束、正在睡轮次间隔。
                # 不能再用「本轮计划数是否为 0」判「无可发群」：工作列表在
                # 轮末会被清空成 []，会把正常的等待误报成没有可发的群。
                target = t.next_round_at
                now = CONFIG.now()
                if target.tzinfo is None and now.tzinfo is not None:
                    target = target.replace(tzinfo=now.tzinfo)
                wait_seconds = max(0, int((target - now).total_seconds()))
                if wait_seconds > 0:
                    status = "waiting_next_round"
            elif t.status == "running" and round_total == 0:
                status = "no_sendable_groups"

            data.append({
                "account": account,
                "status": status,
                "task_id": t.id,
                "delay_scope": _normalize_delay_scope(getattr(t, "delay_scope", None), "per_account"),
                "tasks_count": 1,
                "total": round_total,
                "success": int(counters.get("success", 0)),
                "failed": int(counters.get("failed", 0)),
                "completed": round_completed,
                "progress_total": round_total,
                "progress_completed": round_completed,
                "current_round": int(t.current_round or 0),
                "rounds": int(t.rounds or 0),
                "current_round_planned": round_total,
                "current_round_sent": round_completed,
                "overall_planned": overall_planned,
                "overall_completed": overall_completed,
                "next_round_at": next_round_at,
                "wait_seconds": wait_seconds,
                "last_updated_at": current_ts.isoformat() if current_ts else None,
            })
        data.sort(key=lambda x: x.get("last_updated_at") or "", reverse=True)
        return JSONResponse(data)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("tasks_summary", exc)
        return JSONResponse(_runtime_task_summary_rows())
    finally:
        db.close()

@app.route("/api/tasks")
async def list_tasks(request: Request):
    db: Session = SessionLocal()
    try:
        rows = db.query(Task).order_by(Task.started_at.desc()).limit(100).all()
        data = [
            {
                "task_id": r.id,
                "status": r.status,
                "total": r.total,
                "success": r.success,
                "failed": r.failed,
                "current_index": r.current_index,
                "account": r.account_name,
                "delay_scope": _normalize_delay_scope(getattr(r, "delay_scope", None), "per_account"),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "rounds": getattr(r, "rounds", None),
                "current_round": getattr(r, "current_round", None),
                "next_round_at": r.next_round_at.isoformat() if getattr(r, "next_round_at", None) else None,
            }
            for r in rows
        ]
        return JSONResponse(data)
    except DatabaseError as exc:
        db.rollback()
        _handle_task_db_error("list_tasks", exc)
        return JSONResponse(_runtime_task_list_rows())
    finally:
        db.close()

@app.route("/api/tasks/delete", methods=["POST"])
async def delete_task(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    task_id = (body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"detail": "task_id required"}, status_code=400)
    db: Session = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        events_deleted = (
            db.query(TaskEvent)
            .filter(TaskEvent.task_id == task_id)
            .delete(synchronize_session=False)
        )
        db.delete(task)
        db.commit()
        return JSONResponse({
            "ok": True,
            "deleted_task_id": task_id,
            "events_deleted": int(events_deleted or 0),
        })
    finally:
        db.close()


@app.route("/api/tasks/clear", methods=["POST"])
async def clear_all_tasks(request: Request):
    db: Session = SessionLocal()
    try:
        task_ids = [row[0] for row in db.query(Task.id).all()]
        events_deleted = 0
        if task_ids:
            events_deleted = (
                db.query(TaskEvent)
                .filter(TaskEvent.task_id.in_(task_ids))
                .delete(synchronize_session=False)
            )
        tasks_deleted = db.query(Task).delete(synchronize_session=False)
        db.commit()
        return JSONResponse({
            "ok": True,
            "tasks_deleted": int(tasks_deleted or 0),
            "events_deleted": int(events_deleted or 0),
        })
    finally:
        db.close()


async def _run_send_task_with_delay(
    task_id: str,
    account: str,
    group_ids: list[int],
    message: str,
    parse_mode: str,
    disable_web_page_preview: bool,
    delay_ms: int,
    rounds: int,
    round_interval_s: int,
    start_delay: float = 0,
    start_round_idx: int = 0,
    start_group_idx: int = 0,
):
    """带延迟启动的发送任务包装器"""
    if start_delay > 0:
        _update_runtime_task_state(task_id, status="scheduled")
        print(f"[TASK] {account}: waiting {start_delay:.1f}s before starting...")
        if not await _sleep_with_task_checks(task_id, start_delay, refresh_heartbeat=True):
            return
    print(f"[TASK] {account}: starting send task (task_id={task_id[:8]}...)")
    _update_runtime_task_state(task_id, status="running")
    await _run_send_task(task_id, account, group_ids, message, parse_mode, disable_web_page_preview, delay_ms, rounds, round_interval_s, start_round_idx, start_group_idx)


async def _run_send_task(task_id: str, account: str, group_ids: list[int], message: str, parse_mode: str, disable_web_page_preview: bool, delay_ms: int, rounds: int, round_interval_s: int, start_round_idx: int = 0, start_group_idx: int = 0):
    consecutive_failures = 0
    # 连续几次「群里没禁言我，却发不出去」= 账号被 Telegram 全局限制。
    spam_ban_streak = 0
    if _account_send_blocked(account):
        # 容器重启会把 running 任务恢复回来，但被平台限制发言的号不该跟着恢复
        with SessionLocal() as db:
            t = db.query(Task).filter(Task.id == task_id).first()
            if t:
                t.status = "stopped"
                t.finished_at = CONFIG.now()
                t.stop_requested = 1
                db.add(TaskEvent(task_id=task_id, event="stopped", detail="account_send_blocked",
                                 meta_json=json.dumps({"account": account}, ensure_ascii=False)))
                db.commit()
        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
        print(f"[TASK] {account}: 账号被限制群组发言，跳过发送")
        return
    MAX_CONSECUTIVE_FAILURES = int(getattr(CONFIG, "MAX_CONSECUTIVE_FAILURES", 10))
    active_group_ids = unique_group_ids(group_ids)
    # 同群同文案冷却按建任务时定的值走，之后改设置也不影响已经在跑的任务
    message_preview = message[:200]
    with SessionLocal() as db:
        cooldown_row = db.query(Task.group_msg_cooldown_s).filter(Task.id == task_id).first()
    cooldown_s = GROUP_MSG_COOLDOWN_DEFAULT_S if not cooldown_row or cooldown_row[0] is None else int(cooldown_row[0])

    def _should_defer_group(error_text: str | None) -> bool:
        if not error_text:
            return True
        err = str(error_text).lower()
        if should_add_to_blist(err):
            return False
        hard_fail_markers = (
            "peer error",
            "invalid peer",
            "could not find the input entity",
            "channel_private",
            "session database corrupted",
            "not authorized",
        )
        return not any(marker in err for marker in hard_fail_markers)
    
    try:
        for i in range(start_round_idx, rounds):
            current_round = i + 1
            round_started_mono = time.monotonic()
            
            # --- Round Setup ---
            resume_current_round = (i == start_round_idx and start_group_idx > 0)
            with SessionLocal() as db:
                t = db.query(Task).filter(Task.id == task_id).first()
                if not t:
                    print(f"[TASK] {account}: Task record missing, terminating task loop")
                    _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                    return
                else:
                    t.current_round = current_round
                    # 新一轮真的开始了：清掉上一轮排的等待时间，界面才不会一直挂着
                    # 「等待下一轮」（等待结束但字段没清，看起来就像卡住不动）。
                    t.next_round_at = None
                    # 每轮开始时重置索引（无论是新轮次还是从恢复点继续）
                    if i == start_round_idx and start_group_idx > 0:
                        # 恢复任务，从断点继续
                        pass  # 保留 current_index 用于恢复逻辑
                    else:
                        # 新轮次开始，重置索引
                        t.current_index = 0

                dynamic = int(getattr(t, "dynamic_groups", 0) or 0) == 1
                dynamic_pool_ids: list[int] = []
                cooling_remaining: dict[int, float] = {}
                if dynamic:
                    # 动态任务：每轮都从群池重算，后台加群新落库的群下一轮自动可发
                    dynamic_pool_ids = _account_pool_group_ids(db, account)
                    # 冷却每轮按当前群池规模重算：群越多同一群可以隔得越久，
                    # 群越少自动收紧，吞吐不再被预设里的固定小时数焊死。
                    round_cooldown_s = _effective_group_cooldown_s(cooldown_s)
                    # 跨账号同群最小间隔只用于常驻群发；手动任务还是按预设冷却走，
                    # 不然一个 30 分钟的下限会把「按群组直接发」拖得很慢。
                    round_min_gap_s = GROUP_MIN_GAP_S
                    # 还在冷却里的先整批剔掉：这些群这一轮不可能发，
                    # 再拿去做权限探测只是白烧 Telegram 请求（每轮几十个群）。
                    cooling_remaining = _group_msg_cooldown_map(
                        dynamic_pool_ids, account, round_cooldown_s, round_min_gap_s)
                    active_group_ids = [gid for gid in dynamic_pool_ids if gid not in cooling_remaining]
                else:
                    round_cooldown_s = cooldown_s
                    round_min_gap_s = 0
                    active_group_ids = _filter_account_banned_group_ids(db, account, active_group_ids)

                if dynamic and active_group_ids:
                    # 先查发言权限再发：被禁言的群直接剔掉，不再靠「发一条失败一条」去发现
                    probe_started = time.monotonic()
                    usable, muted, readonly, gone = await _probe_send_permissions(account, active_group_ids)
                    for gid in muted:
                        add_banned_group(db, account, gid, global_scope=False, commit=False)
                    for gid in readonly:
                        # 群默认只允许管理员发言：整群下架，换哪个号都一样
                        add_banned_group(db, account, gid, global_scope=True, commit=False)
                    for gid in gone:
                        _forget_group_membership(db, account, gid)
                    if muted or readonly or gone:
                        active_group_ids = usable
                        db.add(TaskEvent(
                            task_id=task_id,
                            event="probe",
                            detail=f"muted_{len(muted)}_readonly_{len(readonly)}_gone_{len(gone)}",
                            meta_json=json.dumps(
                                {"account": account, "muted": muted[:50], "readonly": readonly[:50],
                                 "gone": gone[:50], "usable": len(usable),
                                 "elapsed_s": round(time.monotonic() - probe_started, 1)},
                                ensure_ascii=False,
                            ),
                        ))
                        db.commit()
                    print(
                        f"[PROBE] {account}: 可发 {len(usable)} · 禁言 {len(muted)}"
                        f" · 只读群 {len(readonly)} · 已不在群 {len(gone)}"
                        f" · {time.monotonic() - probe_started:.1f}s"
                    )

                wait_for_groups = False
                wait_for_groups_s = float(max(round_interval_s, 60))
                if not active_group_ids:
                    if dynamic:
                        # 动态任务永远不判完成：群被拉黑/冷却只是这一轮没得发，
                        # 后台还在加群，下一轮从群池重算又会拿到新群。
                        wait_for_groups = True
                        if not dynamic_pool_ids:
                            # 这个号现在一个群都没有：加群随时会补进来，1 分钟看一次
                            wait_for_groups_s = 60.0
                        elif cooling_remaining:
                            # 全在冷却里就别硬睡一整个轮间隔：等到最早解锁的那个群
                            wait_for_groups_s = min(
                                wait_for_groups_s,
                                max(15.0, min(cooling_remaining.values())),
                            )
                        t.next_round_at = CONFIG.now() + timedelta(seconds=int(wait_for_groups_s))
                        db.commit()
                    else:
                        t.status = "completed"
                        t.finished_at = CONFIG.now()
                        t.next_round_at = None
                        t.group_ids_json = "[]"
                        db.add(TaskEvent(task_id=task_id, event="completed", detail="no_sendable_groups", meta_json=json.dumps({"account": account}, ensure_ascii=False)))
                        db.commit()
                        _update_runtime_task_state(task_id, status="completed", total=0, finished_at=_runtime_state_iso_now())
                        return

                if not wait_for_groups:
                    if resume_current_round:
                        ids = list(active_group_ids)
                    elif int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) == 1:
                        grades = classify_groups(db, account, active_group_ids)
                        role = classify_account(db, account)
                        ids = select_groups_for_account(role, active_group_ids, grades)
                    else:
                        ids = list(active_group_ids)
                        random.shuffle(ids)
                    ids = unique_group_ids(ids)
                    if not resume_current_round:
                        ids = sort_groups_for_account(db, account, ids)
                    t.group_ids_json = json.dumps(ids)
                    db.commit()

            if wait_for_groups:
                wait_s = wait_for_groups_s
                _update_runtime_task_state(
                    task_id,
                    status="running",
                    current_round=int(current_round),
                    total=0,
                    next_round_at=(CONFIG.now() + timedelta(seconds=wait_s)).isoformat(),
                )
                if not await _sleep_with_task_checks(task_id, wait_s, refresh_heartbeat=True):
                    return
                continue

            _update_runtime_task_state(
                task_id,
                status="running",
                current_round=int(current_round),
                total=len(ids),
                next_round_at=None,
            )
            deferred_once: set[int] = set()
            # 本轮被禁言了几次：够了就停手，别让这个号把剩下的群挨个撞一遍
            mute_hits = 0
            
            # Resumption logic: derive the real resume point from send_logs.
            # Because deferred/blacklisted groups reorder the working list during
            # a round, the persisted current_index no longer matches the array
            # position. So on resume we skip any group that already has a
            # "success" SendLog for this round and continue from the first one
            # that does not, avoiding duplicate sends and skipped groups.
            start_pos = 0
            if i == start_round_idx and start_group_idx > 0:
                print(f"[TASK] {account}: Resuming round {current_round} from index {start_group_idx}")
                if not ids:
                    ids = []
                else:
                    with SessionLocal() as db:
                        sent_success_in_round = {
                            row[0]
                            for row in db.query(SendLog.group_id)
                            .filter(
                                SendLog.task_id == task_id,
                                SendLog.task_round == current_round,
                                SendLog.status == "success",
                            )
                            .all()
                        }
                    start_pos = next(
                        (pos for pos, gid in enumerate(ids) if gid not in sent_success_in_round),
                        len(ids),
                    )
                    if start_pos >= len(ids):
                        # 本轮所有群都已成功发送，直接结束当前轮
                        ids = []
                        start_pos = 0
            
            print(f"[TASK] {account}: Processing round {current_round}, {max(0, len(ids) - start_pos)} groups remaining")

            # --- Process Groups ---
            pos = start_pos
            sent_this_round = 0
            while pos < len(ids):
                gid = ids[pos]
                task_delay_scope = "per_account"

                # 冷却（本账号维度 + 跨账号同群最小间隔）
                remaining = _group_msg_cooldown_remaining(gid, account, round_cooldown_s, round_min_gap_s)
                if remaining > 0:
                    if dynamic:
                        # 动态任务里同一个群可能有多个成员账号：这一轮已经有人发进去了，
                        # 第二个账号直接跳过；干等的话会把整个队列卡住，还会在冷却结束后重发一遍。
                        ids.pop(pos)
                        with SessionLocal() as log_db:
                            row = log_db.query(Task).filter(Task.id == task_id).first()
                            if row:
                                row.group_ids_json = json.dumps(ids)
                            log_db.add(TaskEvent(
                                task_id=task_id,
                                event="skip",
                                detail="same_group_message_claimed",
                                meta_json=json.dumps({"gid": gid, "remaining_s": round(remaining)}, ensure_ascii=False),
                            ))
                            log_db.commit()
                        continue
                    print(f"[TASK] {account}: group {gid} 同群同文案冷却中，等 {remaining:.0f}s")
                    with SessionLocal() as log_db:
                        log_db.add(TaskEvent(
                            task_id=task_id,
                            event="cooldown_wait",
                            detail=f"same_group_message_{int(remaining)}s",
                            meta_json=json.dumps({"gid": gid, "remaining_s": round(remaining)}, ensure_ascii=False),
                        ))
                        log_db.commit()
                    if not await _sleep_with_task_checks(task_id, remaining, refresh_heartbeat=True):
                        return

                # Use a fresh session for each message to avoid long-lived session issues
                with SessionLocal() as db:
                    t = db.query(Task).filter(Task.id == task_id).first()
                    
                    # If task row has been deleted (e.g., system reset), terminate immediately
                    if not t:
                        print(f"[TASK] {account}: Task record deleted, stopping immediately")
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return
                    
                    # Check stop/pause
                    if t and t.stop_requested:
                        print(f"[TASK] {account}: Stop requested")
                        t.status = "stopped"
                        t.finished_at = CONFIG.now()
                        db.add(TaskEvent(task_id=task_id, event="stopped", detail="task_stopped", meta_json=json.dumps({}, ensure_ascii=False)))
                        db.commit()
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return

                    while t and t.paused:
                        print(f"[TASK] {account}: Paused")
                        db.commit() # Commit any pending
                        db.close() # Close while sleeping
                        await asyncio.sleep(1)
                        db = SessionLocal() # Re-open
                        t = db.query(Task).filter(Task.id == task_id).first()
                    
                    # Check again after pause loop
                    if not t or (t and t.stop_requested):
                        print(f"[TASK] {account}: Stop requested after pause")
                        if t:
                            t.status = "stopped"
                            t.finished_at = CONFIG.now()
                            db.add(TaskEvent(task_id=task_id, event="stopped", detail="task_stopped", meta_json=json.dumps({}, ensure_ascii=False)))
                            db.commit()
                        _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                        return

                    task_delay_scope, task_delay_ms = _effective_task_timing(db, t, task_id, delay_ms)
                    if task_delay_scope == "global":
                        wait_ms = _compute_effective_task_delay_ms(account, task_delay_ms)
                        bucket_key = _global_send_bucket_key(t, task_id)
                        if not await _reserve_global_send_slot(task_id, bucket_key, wait_ms / 1000.0):
                            return

                    # Send Logic
                    send_text = message
                    if int(getattr(CONFIG, "SMART_SCHEDULER_ENABLED", 1)) == 1:
                        send_text = randomize_message(message)
                    
                    # Call async send (no DB involved here)
                    # print(f"[TASK] {account}: Sending to group {gid}")
                    ok, err, msg_id = await multi_manager.send_message_to_group(
                        account,
                        group_id=gid,
                        text=send_text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=disable_web_page_preview,
                    )
                    
                    # Logging
                    sent_this_round += 1
                    status = "success" if ok else "failed"
                    if not ok:
                        print(f"[TASK] {account}: Failed to send to {gid}: {err}")
                    title = await multi_manager.get_group_title(account, gid)

                    log = SendLog(
                        task_id=task_id,
                        task_round=current_round,
                        account_name=account,
                        group_id=gid,
                        group_title=title,
                        message_preview=message_preview,
                        status=status,
                        error=None if ok else (err or "send_failed"),
                        message_id=msg_id,
                        parse_mode=parse_mode,
                        created_at=CONFIG.now(),
                    )
                    db.add(log)
                    
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        finalized = False
                        advance_pos = True
                        if ok:
                            t.success = (t.success or 0) + 1
                            consecutive_failures = 0
                            spam_ban_streak = 0
                            finalized = True
                        else:
                            consecutive_failures += 1
                            if should_add_to_blist(err):
                                account_restricted = False
                                try:
                                    global_scope = should_add_to_global_blist(err)
                                    if is_mute_error(err) and dynamic:
                                        # 先分清是谁的问题：群本身允许发言、这个号却被挡，
                                        # 可能是账号被平台限制。但单个群会有误报（群里的机器人 /
                                        # 审核），所以连续几次都这样才判定为账号级限制。
                                        state = await multi_manager.get_send_permission(account, gid)
                                        global_scope = False
                                        mute_hits += 1
                                        if state == "ok":
                                            spam_ban_streak += 1
                                            if spam_ban_streak >= ACCOUNT_SPAM_BAN_STREAK:
                                                account_restricted = True
                                    elif not global_scope and needs_ban_vote(err):
                                        # 「你被本群禁言」只先拉黑这个账号；同一个群被
                                        # 够多个账号分别撞上，才认定是群不欢迎我们，整群下架。
                                        votes = register_ban_vote(db, gid, account)
                                        global_scope = votes >= GLOBAL_BAN_VOTES_REQUIRED
                                    if not account_restricted:
                                        add_banned_group(db, account, gid, global_scope=global_scope)
                                        if is_member_lost_error(err):
                                            # 已经被踢出去了：成员关系摘掉，加群进程会换号补位
                                            _forget_group_membership(db, account, gid)
                                except Exception:
                                    pass
                                if account_restricted:
                                    # 这个号发不出去了：停掉整条任务，剩下的群留给别的号
                                    db.add(TaskEvent(
                                        task_id=task_id,
                                        event="stopped",
                                        detail="account_restricted_from_groups",
                                        meta_json=json.dumps({"account": account, "gid": gid, "error": err},
                                                             ensure_ascii=False),
                                    ))
                                    t.status = "stopped"
                                    t.finished_at = CONFIG.now()
                                    t.stop_requested = 1
                                    try:
                                        db.commit()
                                    except DatabaseError as exc:
                                        db.rollback()
                                        _handle_task_db_error("task_runner_account_restricted", exc)
                                    _update_runtime_task_state(task_id, status="stopped", finished_at=_runtime_state_iso_now())
                                    _mark_account_send_blocked(
                                        account, "账号被 Telegram 限制在群组发言（spam ban），暂不发送")
                                    return
                                active_group_ids = _remove_group_id_once(active_group_ids, gid)
                                ids.pop(pos)
                                t.group_ids_json = json.dumps(ids)
                                advance_pos = False
                                db.add(TaskEvent(
                                    task_id=task_id,
                                    event="skip",
                                    detail="group_blocked_for_account",
                                    meta_json=json.dumps({"gid": gid, "error": err, "account": account}, ensure_ascii=False),
                                ))
                            if gid not in deferred_once and _should_defer_group(err):
                                deferred_once.add(gid)
                                ids.pop(pos)
                                ids.append(gid)
                                t.group_ids_json = json.dumps(ids)
                                db.add(TaskEvent(
                                    task_id=task_id,
                                    event="deferred",
                                    detail="group_deferred_to_tail",
                                    meta_json=json.dumps({"gid": gid, "error": err}, ensure_ascii=False),
                                ))
                            else:
                                t.failed = (t.failed or 0) + 1
                                finalized = True

                        if finalized:
                            t.current_index = (t.current_index or 0) + 1
                            if advance_pos:
                                pos += 1
                        t.heartbeat_at = CONFIG.now()
                        # 获取当前轮次的实际计划数
                        current_round_planned = _planned_group_count(t.group_ids_json)
                        # 计算当前轮次已完成数
                        current_round_completed = min(int(t.current_index or 0), current_round_planned)
                        # 计算前面轮次的累计完成数
                        previous_rounds_completed = max(0, int((t.success or 0) + (t.failed or 0)) - current_round_completed)
                        # 总体进度：前面轮次累计 + 当前轮次实际计划数
                        overall_total = previous_rounds_completed + current_round_planned
                        overall_completed = previous_rounds_completed + current_round_completed
                        _update_runtime_task_state(
                            task_id,
                            status="running",
                            success=int(t.success or 0),
                            failed=int(t.failed or 0),
                            current_index=int(t.current_index or 0),
                            current_round=int(t.current_round or current_round),
                            total=int(current_round_planned or len(ids)),
                        )
                        db.add(TaskEvent(task_id=task_id, event="progress", detail=f"{overall_completed}/{overall_total}", meta_json=json.dumps({"gid": gid, "round": t.current_round, "round_completed": current_round_completed, "round_planned": current_round_planned}, ensure_ascii=False)))
                    
                    # Auto-pause logic
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                         db.add(TaskEvent(
                            task_id=task_id, 
                            event="auto_pause", 
                            detail=f"sleeping_10m_failures_{consecutive_failures}",
                            meta_json=json.dumps({"consecutive_failures": consecutive_failures, "last_error": err}, ensure_ascii=False)
                        ))
                    
                    try:
                        db.commit()
                    except DatabaseError as exc:
                        db.rollback()
                        _handle_task_db_error("task_runner_commit", exc)
                        _update_runtime_task_state(
                            task_id,
                            status="error",
                            last_error=f"task_db_write_error: {str(exc)[:160]}",
                            finished_at=_runtime_state_iso_now(),
                        )
                        return
                
                # Handle auto-pause outside of DB session
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    if not await _sleep_with_task_checks(task_id, 600, refresh_heartbeat=True):
                        return
                    consecutive_failures = 0
                    continue

                # 被禁言熔断：这个号已经在好几个群被禁言，先停手冷静一段时间。
                # 剩下没发的群留着下一轮由别的号去发（后台加群会补新号进来）。
                if dynamic and mute_hits >= MUTE_CIRCUIT_THRESHOLD:
                    with SessionLocal() as log_db:
                        log_db.add(TaskEvent(
                            task_id=task_id,
                            event="auto_pause",
                            detail=f"muted_in_{mute_hits}_groups",
                            meta_json=json.dumps(
                                {"account": account, "cooldown_s": MUTE_CIRCUIT_COOLDOWN_S},
                                ensure_ascii=False,
                            ),
                        ))
                        log_db.commit()
                    if not await _sleep_with_task_checks(task_id, MUTE_CIRCUIT_COOLDOWN_S, refresh_heartbeat=True):
                        return
                    mute_hits = 0
                    continue

                # Sleep between messages
                if task_delay_scope != "global":
                    wait_ms = _compute_effective_task_delay_ms(account, task_delay_ms)
                    if wait_ms > 0:
                        if not await _sleep_with_task_checks(task_id, wait_ms / 1000.0, refresh_heartbeat=True):
                            return

            # --- End of Round ---
            if not active_group_ids:
                # 动态任务不判完成：这一轮可用群被拉黑/冷却清空了，下一轮从群池重算
                # 又会拿到新群（后台还在加群）。判完成会被常驻模式立刻重建，来回空转。
                if dynamic:
                    # 这一轮把可发群撞空了（全被拉黑/踢出）：60 秒后从群池重算，
                    # 不要睡满一整个轮间隔，后台加群随时会补进新群。
                    wait_s = 60.0
                    with SessionLocal() as db:
                        t = db.query(Task).filter(Task.id == task_id).first()
                        if t:
                            t.next_round_at = CONFIG.now() + timedelta(seconds=wait_s)
                            t.group_ids_json = "[]"
                            try:
                                db.commit()
                            except DatabaseError as exc:
                                db.rollback()
                                _handle_task_db_error("task_wait_no_groups", exc)
                    _update_runtime_task_state(
                        task_id,
                        status="running",
                        next_round_at=(CONFIG.now() + timedelta(seconds=wait_s)).isoformat(),
                    )
                    if not await _sleep_with_task_checks(task_id, wait_s, refresh_heartbeat=True):
                        return
                    continue
                with SessionLocal() as db:
                    t = db.query(Task).filter(Task.id == task_id).first()
                    if t:
                        t.status = "completed"
                        t.finished_at = CONFIG.now()
                        t.next_round_at = None
                        t.group_ids_json = "[]"
                        db.add(TaskEvent(
                            task_id=task_id,
                            event="completed",
                            detail="no_sendable_groups",
                            meta_json=json.dumps({"account": account, "round": current_round}, ensure_ascii=False),
                        ))
                        try:
                            db.commit()
                        except DatabaseError as exc:
                            db.rollback()
                            _handle_task_db_error("task_complete_no_groups", exc)
                _update_runtime_task_state(task_id, status="completed", total=0, finished_at=_runtime_state_iso_now(), next_round_at=None)
                return

            if current_round < rounds:
                # 轮间隔 = 「两轮开始时间的最小间隔」，不是「每轮结束后硬睡满」。
                # 一轮本身发了几十分钟时再睡 10 分钟纯空转，界面就一直停在「等待下一轮」。
                elapsed_s = time.monotonic() - round_started_mono
                sleep_time = max(0.0, float(round_interval_s) - elapsed_s)
                if sent_this_round <= 0:
                    # 这一轮一条都没发出去（群被清空/全跳过）：别硬等整个轮间隔
                    sleep_time = min(sleep_time, 60.0)
                update_db_time = True
                
                # Resuming a finished round?
                if i == start_round_idx and len(ids) == 0:
                    with SessionLocal() as db:
                        t = db.query(Task).filter(Task.id == task_id).first()
                        if t and t.next_round_at:
                            target = t.next_round_at
                            if target.tzinfo is None:
                                # Assume it's in the configured timezone
                                try:
                                    tz = ZoneInfo(getattr(CONFIG, "TIMEZONE", "UTC"))
                                except Exception:
                                    tz = timezone.utc
                                target = target.replace(tzinfo=tz)
                            
                            now = datetime.now(target.tzinfo)
                            remaining = (target - now).total_seconds()
                            
                            if remaining <= 0:
                                sleep_time = 0
                                update_db_time = False
                                print(f"[TASK] {account}: Skipping sleep, next round was due at {target}")
                            else:
                                sleep_time = remaining
                                update_db_time = False
                                print(f"[TASK] {account}: Sleeping remaining {remaining:.1f}s until {target}")

                if update_db_time:
                    with SessionLocal() as db:
                        t = db.query(Task).filter(Task.id == task_id).first()
                        if t:
                            t.next_round_at = (
                                CONFIG.now() + timedelta(seconds=int(sleep_time))
                            ) if sleep_time > 0 else None
                            try:
                                db.commit()
                            except DatabaseError as exc:
                                db.rollback()
                                _handle_task_db_error("task_runner_next_round_commit", exc)
                                _update_runtime_task_state(task_id, next_round_at=t.next_round_at.isoformat() if t.next_round_at else None)
                
                if sleep_time > 0:
                    _update_runtime_task_state(task_id, next_round_at=(CONFIG.now() + timedelta(seconds=sleep_time)).isoformat())
                    if not await _sleep_with_task_checks(task_id, sleep_time, refresh_heartbeat=True):
                        return
                else:
                    # 一轮已经跑够轮间隔：直接开下一轮，不空转
                    _update_runtime_task_state(task_id, next_round_at=None)

        # --- Task Done ---
        with SessionLocal() as db:
            t = db.query(Task).filter(Task.id == task_id).first()
            if t and t.status not in ("stopped", "error"):
                t.status = "done"
                t.finished_at = CONFIG.now()
                db.add(TaskEvent(task_id=task_id, event="finished", detail="task_done", meta_json=json.dumps({}, ensure_ascii=False)))
                try:
                    db.commit()
                except DatabaseError as exc:
                    db.rollback()
                    _handle_task_db_error("task_runner_finish_commit", exc)
        _update_runtime_task_state(task_id, status="done", finished_at=_runtime_state_iso_now())

    except Exception as e:
        _update_runtime_task_state(task_id, status="error", last_error=str(e)[:200], finished_at=_runtime_state_iso_now())
        try:
            with SessionLocal() as db:
                t = db.query(Task).filter(Task.id == task_id).first()
                if t:
                    t.status = "error"
                    t.finished_at = CONFIG.now()
                    db.add(TaskEvent(task_id=task_id, event="error", detail=f"task_error: {e}", meta_json=json.dumps({}, ensure_ascii=False)))
                    db.commit()
        except Exception:
            print(f"CRITICAL: Failed to log error to DB for task {task_id}: {e}")

@app.route("/api/login/send-code", methods=["POST"])
async def login_send_code(request: Request):
    body = await request.json()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    phone = body.get("phone")
    force_sms = body.get("force_sms", False)
    force_new_session = body.get("force_new_session", False)
    if not phone:
        try:
            db: Session = SessionLocal()
            try:
                row = db.query(SystemKV).filter(SystemKV.k == f"login_phone:{account}").first()
                if row and row.v:
                    try:
                        d = json.loads(row.v)
                        ph = (d.get("phone") or "").strip()
                        if ph:
                            phone = ph
                    except Exception:
                        pass
            finally:
                db.close()
        except Exception:
            pass
        if not phone:
            return JSONResponse({"detail": "phone required"}, status_code=400)
    try:
        resp = await multi_manager.send_login_code(account, phone, force_sms=force_sms, force_new_session=force_new_session)
        try:
            if isinstance(resp, dict) and not resp.get("ok", True):
                if "retry_after" in resp:
                    return JSONResponse(resp, status_code=429, headers={"Retry-After": str(resp.get("retry_after", 60))})
                err = str(resp.get("error", "")).lower()
                if "phone_invalid" in err or "phone" in err:
                    return JSONResponse(resp, status_code=400)
                return JSONResponse(resp, status_code=500)
        except Exception:
            pass
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.route("/api/login/submit-code", methods=["POST"])
async def login_submit_code(request: Request):
    body = await request.json()
    code = body.get("code")
    phone = (body.get("phone") or "").strip()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    if not phone or not code:
        return JSONResponse({"detail": "phone and code required"}, status_code=400)
    try:
        resp = await multi_manager.confirm_login(account, phone, code, body.get("password") or None)
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.route("/api/groups/join", methods=["POST"])
async def join_group(request: Request):
    """
    使用指定账号加入群组
    
    请求体:
    {
        "account": "account_01",
        "invite_link": "https://t.me/+xxxxx" 或 "@groupname"
    """
    
    body = await request.json()
    account = body.get("account") or CONFIG.DEFAULT_ACCOUNT
    invite_link = (body.get("invite_link") or "").strip()
    
    if not invite_link:
        return JSONResponse({"detail": "invite_link required"}, status_code=400)
    
    policy_db = SessionLocal()
    try:
        blocked = _proxy_policy_missing(policy_db, [account])
    finally:
        policy_db.close()
    if blocked:
        return JSONResponse({
            "detail": "proxy_required_per_account",
            "message": f"已开启「强制一账号一代理」：账号 {account} 没有绑定代理，无法加群。可在「代理 IP 配置」绑定代理，或关闭该开关。",
            "accounts": blocked,
        }, status_code=400)
    try:
        if not await _is_authorized_account(account):
            return JSONResponse({"detail": "account_not_authorized"}, status_code=403)
        result = await multi_manager.join_group(account, invite_link)
        return JSONResponse({"account": account, **result})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.on_event("shutdown")
async def shutdown_event():
    print("[SHUTDOWN] Closing all Telegram connections...")
    await multi_manager.disconnect_all()
    print("[SHUTDOWN] Cleanup complete.")


@app.route("/api/groups/join-all-accounts", methods=["POST"])
async def join_group_all_accounts(request: Request):
    """
    让所有账号加入同一个群组
    
    请求体:
    {
        "accounts": ["account_01", ...],  // 可选
        "invite_link": "https://t.me/+xxxxx",
        "delay_ms": 3000  // 每个账号之间的延迟 (防风控)
    """
    
    body = await request.json()
    invite_link = (body.get("invite_link") or "").strip()
    delay_ms = int(body.get("delay_ms", 3000))
    
    if not invite_link:
        return JSONResponse({"detail": "invite_link required"}, status_code=400)
    
    # 获取账号列表
    accounts = _candidate_accounts(body.get("accounts") or [])
    
    results = []
    _authorized_accounts, unauthorized_accounts = await _split_authorized_accounts(accounts)
    unauthorized_set = set(unauthorized_accounts)

    policy_db = SessionLocal()
    try:
        blocked = _proxy_policy_missing(policy_db, _authorized_accounts)
    finally:
        policy_db.close()
    if blocked:
        return JSONResponse({
            "detail": "proxy_required_per_account",
            "message": (
                f"已开启「强制一账号一代理」：{len(blocked)} 个账号没有绑定代理，无法开始加群"
                f"（{'、'.join(blocked[:5])}{'…' if len(blocked) > 5 else ''}）。"
                "可在「代理 IP 配置」绑定代理，或关闭该开关。"
            ),
            "accounts": blocked,
        }, status_code=400)
    
    for i, acc in enumerate(accounts):
        try:
            if acc in unauthorized_set:
                results.append({"account": acc, "ok": False, "error": "not_authorized"})
                continue
            
            result = await multi_manager.join_group(acc, invite_link)
            results.append({"account": acc, **result})
            
            # 延迟 (防风控)
            if delay_ms > 0 and i < len(accounts) - 1:
                # 添加随机抖动
                jitter = random.uniform(-0.2, 0.2) * delay_ms
                await asyncio.sleep((delay_ms + jitter) / 1000.0)
                
        except Exception as e:
            results.append({"account": acc, "ok": False, "error": str(e)})
    
    summary = {
        "total": len(results),
        "success": sum(1 for r in results if r.get("ok")),
        "already_joined": sum(1 for r in results if r.get("already_joined")),
        "failed": sum(1 for r in results if not r.get("ok") and not r.get("already_joined")),
    }
    
    return JSONResponse({"summary": summary, "results": results})
