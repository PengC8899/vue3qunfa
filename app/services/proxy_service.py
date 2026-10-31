from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse, quote, unquote
from urllib.request import Request as UrlRequest, urlopen
from urllib.error import HTTPError

from sqlalchemy.orm import Session

from app.models import AccountProxy, SystemKV


SUPPORTED_PROXY_TYPES = {"socks5", "http"}
DEFAULT_PROXY_SOURCE = ""


def _normalize_proxy_type(value: str | None) -> str:
    proxy_type = (value or "socks5").strip().lower()
    if proxy_type not in SUPPORTED_PROXY_TYPES:
        raise ValueError("unsupported_proxy_type")
    return proxy_type


def _clean_raw_proxy_value(raw_value: str | None) -> str:
    value = (raw_value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"`", '"', "'"}:
        value = value[1:-1].strip()
    return value


def infer_proxy_type_from_source(source_value: str | None, fallback: str = "socks5") -> str:
    value = _clean_raw_proxy_value(source_value)
    if not value:
        return _normalize_proxy_type(fallback)
    try:
        parsed = urlparse(value)
    except Exception:
        return _normalize_proxy_type(fallback)

    query = parse_qs(parsed.query or "", keep_blank_values=True)
    for key in ("proto", "protocol", "proxy_type", "ptype_name", "type"):
        raw = (query.get(key) or [None])[0]
        if raw in SUPPORTED_PROXY_TYPES:
            return _normalize_proxy_type(raw)

    lowered = value.lower()
    if "socks5" in lowered:
        return "socks5"
    if "proto=http" in lowered:
        return "http"
    return _normalize_proxy_type(fallback)


def parse_proxy_input(raw_value: str | None, default_proxy_type: str = "socks5") -> dict | None:
    value = _clean_raw_proxy_value(raw_value)
    if not value:
        return None

    proxy_type = _normalize_proxy_type(default_proxy_type)
    username = None
    password = None
    host = None
    port = None

    if "://" in value:
        parsed = urlparse(value)
        proxy_type = _normalize_proxy_type(parsed.scheme)
        # A usable proxy must be just scheme://host:port; source URLs with path/query
        # are proxy providers' APIs and must not be stored as account bindings.
        if parsed.path not in ("", "/") or parsed.query or parsed.params or parsed.fragment:
            raise ValueError("invalid_proxy_endpoint")
        host = parsed.hostname
        port = parsed.port
        username = unquote(parsed.username) if parsed.username else None
        password = unquote(parsed.password) if parsed.password else None
    else:
        parts = [part.strip() for part in value.split(":", 3)]
        if len(parts) == 2:
            host, port = parts
        elif len(parts) == 4:
            host, port, username, password = parts
        else:
            raise ValueError("invalid_proxy_format")

    host = (host or "").strip()
    if not host:
        raise ValueError("missing_proxy_host")

    try:
        port = int(port)
    except Exception as exc:
        raise ValueError("invalid_proxy_port") from exc

    if port <= 0 or port > 65535:
        raise ValueError("invalid_proxy_port")

    return {
        "proxy_type": proxy_type,
        "host": host,
        "port": port,
        "username": username or None,
        "password": password or None,
        "raw_input": value,
        "enabled": 1,
    }


def normalize_proxy_input(raw_value: str | None, default_proxy_type: str = "socks5") -> str | None:
    parsed = parse_proxy_input(raw_value, default_proxy_type=default_proxy_type)
    if parsed is None:
        return None
    return format_proxy_url(parsed, mask_password=False)


def fetch_proxy_lines_from_url(
    source_url: str,
    *,
    timeout: int = 20,
    default_proxy_type: str = "socks5",
) -> list[str]:
    cleaned_url = _clean_raw_proxy_value(source_url)
    req = UrlRequest(cleaned_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="ignore")
    except HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            body = ""
        if body:
            raise RuntimeError(f"http_{exc.code}: {body}") from exc
        raise
    lines: list[str] = []
    for item in payload.splitlines():
        normalized = normalize_proxy_input(item, default_proxy_type=default_proxy_type)
        if normalized:
            lines.append(normalized)
    return lines


def resolve_proxy_source_text(
    raw_text: str | None,
    errors: list[str] | None = None,
) -> tuple[dict[str, str], list[str], dict]:
    issues = errors if errors is not None else []
    text = _clean_raw_proxy_value(raw_text)
    explicit_map: dict[str, str] = {}
    proxy_pool: list[str] = []
    meta = {
        "source_lines": 0,
        "remote_sources": 0,
        "pool_total": 0,
        "explicit_total": 0,
    }
    if not text:
        return explicit_map, proxy_pool, meta

    expanded_lines: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = _clean_raw_proxy_value(raw_line)
        if not line:
            continue
        meta["source_lines"] += 1
        if "|" not in line and line.startswith(("http://", "https://")) and (
            urlparse(line).path not in ("", "/") or urlparse(line).query
        ):
            default_proxy_type = infer_proxy_type_from_source(line)
            try:
                remote_lines = fetch_proxy_lines_from_url(
                    line,
                    timeout=20,
                    default_proxy_type=default_proxy_type,
                )
                if not remote_lines:
                    issues.append(f"代理源为空: {line}")
                    continue
                meta["remote_sources"] += 1
                expanded_lines.extend((item, default_proxy_type) for item in remote_lines)
            except Exception as exc:
                err_text = str(exc)[:180]
                if "ip whitelist check failed" in err_text.lower():
                    issues.append(f"代理源拉取失败: 711Proxy 白名单校验失败，请把当前出口 IP 加入 711Proxy Whitelist 后重试")
                else:
                    issues.append(f"代理源拉取失败: {line} - {err_text}")
            continue
        expanded_lines.append((line, "socks5"))

    seen_pool: set[str] = set()
    for line, default_proxy_type in expanded_lines:
        if "|" in line:
            account, proxy_value = [part.strip() for part in line.split("|", 1)]
            if not account or not proxy_value:
                issues.append(f"代理映射格式错误: {line}")
                continue
            try:
                normalized = normalize_proxy_input(proxy_value, default_proxy_type=default_proxy_type)
            except ValueError as exc:
                issues.append(f"{account}: 代理格式错误 - {str(exc)}")
                continue
            if normalized:
                explicit_map[account] = normalized
            continue

        try:
            normalized = normalize_proxy_input(line, default_proxy_type=default_proxy_type)
        except ValueError as exc:
            issues.append(f"代理格式错误: {line} - {str(exc)}")
            continue
        if not normalized or normalized in seen_pool:
            continue
        seen_pool.add(normalized)
        proxy_pool.append(normalized)

    meta["pool_total"] = len(proxy_pool)
    meta["explicit_total"] = len(explicit_map)
    return explicit_map, proxy_pool, meta


STICKY_SESSION_SUFFIX = "-session-"


def _sticky_token(account: str, used: set[str]) -> str:
    base = re.sub(r"[^0-9A-Za-z]", "", account) or "acct"
    token = base
    index = 2
    while token in used:
        token = f"{base}{index}"
        index += 1
    used.add(token)
    return token


def derive_sticky_binding(base_proxy: str, token: str) -> str | None:
    """把轮换型网关派生成账号级粘性会话（711Proxy 约定：用户名追加 -session-<id>）。

    轮换型网关每次连接都换出口 IP，同一账号重连就会漂移；追加固定会话 ID 后每个
    账号会拿到固定且唯一的出口 IP，等价于真正的一号一代理。
    """
    try:
        parsed = parse_proxy_input(base_proxy)
    except ValueError:
        return None
    if not parsed or not parsed.get("username"):
        return None
    username = str(parsed["username"])
    if STICKY_SESSION_SUFFIX in username:
        return None
    derived = dict(parsed)
    derived["username"] = f"{username}{STICKY_SESSION_SUFFIX}{token}"
    return format_proxy_url(derived, mask_password=False)


def assign_proxy_pool_to_accounts(
    accounts: list[str],
    explicit_map: dict[str, str] | None,
    proxy_pool: list[str] | None,
    allow_sticky: bool = False,
) -> tuple[dict[str, str], dict]:
    ordered_accounts: list[str] = []
    seen_accounts: set[str] = set()
    for item in accounts or []:
        account = (item or "").strip()
        if not account or account in seen_accounts:
            continue
        seen_accounts.add(account)
        ordered_accounts.append(account)

    bindings: dict[str, str] = {}
    remaining_pool: list[str] = []
    seen_pool: set[str] = set()
    for item in proxy_pool or []:
        try:
            value = normalize_proxy_input(item)
        except ValueError:
            continue
        if not value or value in seen_pool:
            continue
        seen_pool.add(value)
        remaining_pool.append(value)

    pool_bases = list(remaining_pool)

    normalized_map = {}
    for account, value in (explicit_map or {}).items():
        try:
            normalized = normalize_proxy_input(value)
        except ValueError:
            continue
        if normalized:
            normalized_map[account] = normalized
    explicit_map = normalized_map
    reserved = set(explicit_map.values())
    remaining_pool = [value for value in remaining_pool if value not in reserved]
    pool_bases = list(remaining_pool)
    used_bindings = set()
    needs_pool = [
        account for account in ordered_accounts if not _clean_raw_proxy_value(explicit_map.get(account))
    ]
    # 代理池不够时整批改用派生粘性会话：裸网关是轮换型，同一账号重连就会漂移 IP。
    # 但粘性会话只能靠「用户名追加 -session-」派生，无认证的固定 IP 代理派生不出来：
    # 那种池子不够就该老老实实一一对应，别把账号全判成「没代理」。
    can_sticky = False
    for base in pool_bases:
        try:
            parsed_base = parse_proxy_input(base)
        except ValueError:
            continue
        if parsed_base and parsed_base.get("username") \
                and STICKY_SESSION_SUFFIX not in str(parsed_base["username"]):
            can_sticky = True
            break
    use_sticky = bool(allow_sticky and pool_bases and can_sticky
                      and len(pool_bases) < len(needs_pool))

    assigned_from_explicit = 0
    assigned_from_pool = 0
    assigned_from_sticky = 0
    sticky_used: set[str] = set()
    unassigned_accounts: list[str] = []
    for account in ordered_accounts:
        explicit_value = _clean_raw_proxy_value(explicit_map.get(account))
        if explicit_value and explicit_value in used_bindings:
            unassigned_accounts.append(account)
            continue
        if explicit_value:
            used_bindings.add(explicit_value)
            bindings[account] = explicit_value
            assigned_from_explicit += 1
            continue
        if not use_sticky and remaining_pool:
            bindings[account] = remaining_pool.pop(0)
            assigned_from_pool += 1
            continue
        if use_sticky:
            base = pool_bases[assigned_from_sticky % len(pool_bases)]
            derived = derive_sticky_binding(base, _sticky_token(account, sticky_used))
            if derived:
                bindings[account] = derived
                assigned_from_sticky += 1
                continue
        unassigned_accounts.append(account)

    return bindings, {
        "accounts_total": len(ordered_accounts),
        "assigned_total": len(bindings),
        "assigned_from_explicit": assigned_from_explicit,
        "assigned_from_pool": assigned_from_pool,
        "assigned_from_sticky": assigned_from_sticky,
        "unassigned_accounts": unassigned_accounts,
        "remaining_proxy_pool": len(remaining_pool),
    }


def format_proxy_url(proxy: AccountProxy | dict | None, mask_password: bool = True) -> str | None:
    if not proxy:
        return None

    proxy_type = (proxy.get("proxy_type") if isinstance(proxy, dict) else proxy.proxy_type) or "socks5"
    host = proxy.get("host") if isinstance(proxy, dict) else proxy.host
    port = proxy.get("port") if isinstance(proxy, dict) else proxy.port
    username = proxy.get("username") if isinstance(proxy, dict) else proxy.username
    password = proxy.get("password") if isinstance(proxy, dict) else proxy.password

    auth = ""
    if username:
        auth = quote(str(username), safe="")
        if password:
            auth += ":" + ("******" if mask_password else quote(str(password), safe=""))
        auth += "@"
    host = f"[{host}]" if ":" in str(host) and not str(host).startswith("[") else host
    return f"{proxy_type}://{auth}{host}:{port}"


def serialize_proxy(proxy: AccountProxy | None) -> dict | None:
    if not proxy:
        return None
    return {
        "account": proxy.account_name,
        "enabled": bool(proxy.enabled),
        "proxy_type": proxy.proxy_type,
        "host": proxy.host,
        "port": proxy.port,
        "username": proxy.username,
        "proxy_url": format_proxy_url(proxy, mask_password=False),
        "proxy_url_masked": format_proxy_url(proxy, mask_password=True),
        "updated_at": proxy.updated_at.isoformat() if proxy.updated_at else None,
    }


def get_proxy_binding(db: Session, account: str) -> AccountProxy | None:
    return db.query(AccountProxy).filter(AccountProxy.account_name == account).first()


def get_default_proxy_source(db: Session) -> str:
    row = db.query(SystemKV).filter(SystemKV.k == "default_proxy_source").first()
    value = (row.v if row and row.v is not None else DEFAULT_PROXY_SOURCE) or DEFAULT_PROXY_SOURCE
    return _clean_raw_proxy_value(value) or DEFAULT_PROXY_SOURCE


def set_default_proxy_source(db: Session, source_value: str | None) -> str:
    normalized = _clean_raw_proxy_value(source_value) or DEFAULT_PROXY_SOURCE
    row = db.query(SystemKV).filter(SystemKV.k == "default_proxy_source").first()
    if row:
        row.v = normalized
    else:
        row = SystemKV(k="default_proxy_source", v=normalized)
        db.add(row)
    db.commit()
    return normalized


PROXY_POLICY_KEY = "proxy_enforce_one_to_one"


def get_proxy_enforce(db: Session) -> bool:
    """是否强制「一账号一代理」。默认关闭：没有代理也能上传、也能开始任务。"""
    row = db.query(SystemKV).filter(SystemKV.k == PROXY_POLICY_KEY).first()
    return (row.v if row and row.v is not None else "").strip().lower() in {"1", "true", "yes"}


def set_proxy_enforce(db: Session, enforce: bool) -> bool:
    value = "1" if enforce else "0"
    row = db.query(SystemKV).filter(SystemKV.k == PROXY_POLICY_KEY).first()
    if row:
        row.v = value
    else:
        db.add(SystemKV(k=PROXY_POLICY_KEY, v=value))
    db.commit()
    return bool(enforce)



def delete_proxy_binding(db: Session, account: str) -> bool:
    row = get_proxy_binding(db, account)
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def assign_proxies_from_source(
    accounts: list[str],
    source_text: str,
    exclude_proxies: list[str] | None = None,
) -> dict:
    """拉取/解析代理源 → 一号一代理分配 → 落库。

    上传账号、手动分配、定时自动补绑共用这一份，别再各写一遍。
    allow_sticky 必须是 True：轮换型网关（711Proxy 之类）不派生粘性会话的话，
    N 个账号会共用会漂移的出口 IP —— 这正是 Telegram 批量注销 auth key 的触发条件。

    exclude_proxies 是已经绑在别的账号上的代理：代理池比账号少时，补绑绝不能拿
    同一个 IP 再绑第二个号，否则又回到「多号共用出口 IP」。
    """
    from app.database import SessionLocal

    errors: list[str] = []
    explicit_map, pool, source_meta = resolve_proxy_source_text(source_text, errors)
    if exclude_proxies:
        used: set[str] = set()
        for item in exclude_proxies:
            try:
                normalized_used = normalize_proxy_input(item)
            except ValueError:
                continue
            if normalized_used:
                used.add(normalized_used)
        pool = [value for value in pool if value not in used]
    bindings, assignment_meta = assign_proxy_pool_to_accounts(
        accounts, explicit_map, pool, allow_sticky=True)

    saved: list[str] = []
    with SessionLocal() as db:
        for account in accounts:
            proxy_url = bindings.get(account)
            if not proxy_url:
                continue
            try:
                upsert_proxy_binding(db, account, proxy_url, enabled=True)
                saved.append(account)
            except ValueError as exc:
                errors.append(f"{account}: 代理格式错误 - {exc}")
    return {
        "assigned_accounts": saved,
        "assigned_total": len(saved),
        "source_meta": source_meta,
        "assignment_meta": assignment_meta,
        "errors": errors or None,
    }


def list_proxy_bindings(db: Session, accounts: list[str] | None = None) -> dict[str, dict]:
    q = db.query(AccountProxy)
    if accounts:
        q = q.filter(AccountProxy.account_name.in_(accounts))
    rows = q.all()
    return {row.account_name: serialize_proxy(row) for row in rows}


def upsert_proxy_binding(db: Session, account: str, proxy_input: str | None, enabled: bool = True, *, commit: bool = True) -> dict | None:
    account_name = (account or "").strip()
    if not account_name:
        raise ValueError("missing_account")

    existing = get_proxy_binding(db, account_name)
    value = (proxy_input or "").strip()
    if not value:
        if existing:
            db.delete(existing)
            db.commit()
        return None

    parsed = parse_proxy_input(value)
    if parsed is None:
        return None

    if existing is None:
        existing = AccountProxy(account_name=account_name)
        db.add(existing)

    existing.enabled = 1 if enabled else 0
    existing.proxy_type = parsed["proxy_type"]
    existing.host = parsed["host"]
    existing.port = parsed["port"]
    existing.username = parsed["username"]
    existing.password = parsed["password"]
    if commit:
        db.commit()
    else:
        db.flush()
    db.refresh(existing)
    return serialize_proxy(existing)
