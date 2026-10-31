"""目标群组池：导入 / 列表 / 批量删除。

与账号已加入的群缓存（group_caches）无关，这里只维护「我们要往哪些群发」。
"""
from starlette.requests import Request
from starlette.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
import json
import re

from app.database import SessionLocal
from app.models import TargetGroup

_TG_HOSTS = {"t.me", "telegram.me", "telegram.dog"}
_BARE_USERNAME = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_PUBLIC_SEGMENT = re.compile(r"^[A-Za-z0-9_]{4,32}$")
_INVITE_SEGMENT = re.compile(r"^\+[A-Za-z0-9_-]{8,64}$")
_STRIP_CHARS = " \t\r\n\u3000\"',;|<>"

MAX_INVALID_SAMPLES = 20


def normalize_group_line(line: str) -> dict | None:
    """把一行用户输入标准化为 {raw, username, link, kind}，无法识别返回 None。"""
    raw = (line or "").strip().lstrip("\ufeff").strip(_STRIP_CHARS)
    if not raw or raw.startswith("#"):
        return None

    text = raw
    lower = text.lower()
    for scheme in ("https://", "http://"):
        if lower.startswith(scheme):
            text = text[len(scheme):]
            break

    host, _, rest = text.partition("/")
    host = host.lower().lstrip("www.")
    if host in _TG_HOSTS:
        segments = [seg for seg in rest.split("/") if seg]
        username = segments[0] if segments else ""
        if username.lower() == "joinchat" and len(segments) > 1:
            username = f"+{segments[1]}"
        elif username.lower() in ("c", "s"):
            return None  # t.me/c/<内部id> 没有可用邀请链接，无法投放
        elif not username.startswith("+"):
            username = f"@{username.lstrip('@')}"
    else:
        username = text.lstrip("@").strip(_STRIP_CHARS)
        if not username.startswith("+"):
            username = f"@{username}"

    if username.startswith("+"):
        if not _INVITE_SEGMENT.match(username):
            return None
        return {"raw": raw, "username": username, "link": f"https://t.me/{username}", "kind": "invite"}

    if not _PUBLIC_SEGMENT.match(username[1:]):
        return None
    return {"raw": raw, "username": username, "link": f"https://t.me/{username[1:]}", "kind": "public"}


def parse_group_lines(text: str) -> tuple[list[dict], list[str], int, int]:
    """解析粘贴文本/文件内容。

    返回 (待导入条目, 无效样本, 批内重复条数, 无效条数)。批内重复只保留第一条。
    """
    entries: list[dict] = []
    invalid_samples: list[str] = []
    seen: set[str] = set()
    duplicate = 0
    invalid = 0

    for line in (text or "").splitlines():
        # ponytail: 只按逗号切分取首个可解析单元格，覆盖单列/首列 CSV；多列复杂 CSV 需要时再上 csv 模块
        for cell in line.split(","):
            cell = cell.strip()
            if not cell:
                continue
            entry = normalize_group_line(cell)
            if not entry:
                invalid += 1
                if len(invalid_samples) < MAX_INVALID_SAMPLES:
                    invalid_samples.append(cell[:120])
                continue
            if entry["username"] in seen:
                duplicate += 1
                continue
            seen.add(entry["username"])
            entries.append(entry)

    if invalid > len(invalid_samples):
        invalid_samples.append(f"...另有 {invalid - len(invalid_samples)} 条无效")
    return entries, invalid_samples, duplicate, invalid


async def _read_import_text(request: Request) -> str:
    content_type = (request.headers.get("content-type") or "").lower()
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is not None and getattr(upload, "filename", None):
            raw = await upload.read()
            for encoding in ("utf-8-sig", "gbk"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
        return str(form.get("text") or "")
    try:
        body = await request.json()
    except Exception:
        return ""
    return str(body.get("text") or "")


async def import_target_groups(request: Request):
    """POST /api/groups/import —— 部分成功，绝不因为个别坏行整批失败。"""
    text = await _read_import_text(request)
    entries, invalid_samples, duplicate, invalid = parse_group_lines(text)

    imported = 0
    db = SessionLocal()
    try:
        candidates = [e["username"] for e in entries]
        existing: set[str] = set()
        if candidates:
            existing = {
                row[0]
                for row in db.query(TargetGroup.username).filter(TargetGroup.username.in_(candidates)).all()
            }
        new_entries = []
        for entry in entries:
            if entry["username"] in existing:
                duplicate += 1
                continue
            new_entries.append(entry)

        for entry in new_entries:
            db.add(TargetGroup(
                raw=entry["raw"], username=entry["username"], link=entry["link"],
                kind=entry["kind"], status="pending",
            ))
        try:
            db.commit()
            imported = len(new_entries)
        except IntegrityError:
            # 并发导入撞唯一键：逐条落库，跳过已存在的
            db.rollback()
            imported = 0
            for entry in new_entries:
                db.add(TargetGroup(
                    raw=entry["raw"], username=entry["username"], link=entry["link"],
                    kind=entry["kind"], status="pending",
                ))
                try:
                    db.commit()
                    imported += 1
                except IntegrityError:
                    db.rollback()
                    duplicate += 1
    finally:
        db.close()

    return JSONResponse({
        "ok": True,
        "total": imported + duplicate + invalid,
        "imported": imported,
        "duplicate": duplicate,
        "invalid": invalid,
        "invalid_samples": invalid_samples,
    })


def _count_invalid(text: str) -> int:
    count = 0
    for line in (text or "").splitlines():
        for cell in line.split(","):
            cell = cell.strip()
            if cell and not normalize_group_line(cell):
                count += 1
    return count


def _member_names(row: TargetGroup) -> list[str]:
    """已加入该群的账号（来自加群结果落库），只有这些账号能往这个群发消息。"""
    try:
        return sorted({str(name) for name in json.loads(row.joined_by_json or "[]")})
    except Exception:
        return []


async def list_target_groups(request: Request):
    """GET /api/groups/pool —— 全量返回（用户明确要求不要人为上限）。"""
    db = SessionLocal()
    try:
        rows = db.query(TargetGroup).order_by(TargetGroup.id.asc()).all()
        items = []
        for row in rows:
            members = _member_names(row)
            items.append({
                "id": row.id,
                "username": row.username,
                "link": row.link,
                "kind": row.kind,
                "status": row.status,
                "joined_accounts": len(members),
                "joined_sample": members[:5],
                "last_error": row.last_error,
                "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
            })
    finally:
        db.close()

    by_status: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
    return JSONResponse({"ok": True, "total": len(items), "by_status": by_status, "items": items})


async def delete_target_groups(request: Request):
    """POST /api/groups/pool/delete —— {ids:[...]} 或 {scope:"invalid"|"all"}。

    只删目标记录，不碰账号、任务历史、Session。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    ids = [int(i) for i in (body.get("ids") or []) if str(i).strip().lstrip("-").isdigit()]
    scope = str(body.get("scope") or "").strip().lower()
    if not ids and scope not in ("invalid", "all"):
        return JSONResponse({"detail": "ids or scope required"}, status_code=400)

    db = SessionLocal()
    try:
        query = db.query(TargetGroup)
        if ids:
            query = query.filter(TargetGroup.id.in_(ids))
        elif scope == "invalid":
            query = query.filter(TargetGroup.status.in_(["unreachable", "failed"]))
        deleted = query.delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    return JSONResponse({"ok": True, "deleted": int(deleted or 0)})
