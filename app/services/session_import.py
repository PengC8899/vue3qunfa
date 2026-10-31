"""Offline validation of uploaded Telethon sessions; never contacts Telegram."""
import os
import sqlite3
from telethon.sessions import SQLiteSession
from telethon.sessions.sqlite import CURRENT_VERSION

MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
MAX_SESSION_COUNT = 1000


def account_from_filename(filename: str) -> str:
    basename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    name = basename[:-8] if basename.lower().endswith('.session') else ''
    if not name or name in {'.', '..'} or any(ord(c) < 32 for c in name) or len(name.encode()) > 200:
        raise ValueError('无效的 session 文件名')
    return name


def validate_session(path: str) -> None:
    if not 0 < os.path.getsize(path) <= MAX_SESSION_BYTES:
        raise ValueError('session 为空或超过 16 MiB')
    try:
        with open(path, 'rb') as stream:
            if stream.read(16) != b'SQLite format 3\x00':
                raise ValueError('不是 SQLite session；不支持 StringSession 或 tdata')
        conn = sqlite3.connect(path)
        try:
            if conn.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise ValueError('SQLite 文件损坏')
            columns = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
            if not {'dc_id', 'server_address', 'port', 'auth_key'} <= columns:
                raise ValueError('不是 Telethon session（Pyrogram session 不能直接使用）')
            version = conn.execute('SELECT version FROM version').fetchone()
            if not version or not 1 <= version[0] <= CURRENT_VERSION:
                raise ValueError('session 版本不兼容，请使用匹配的 Telethon 版本导出')
            row = conn.execute('SELECT dc_id, server_address, port, auth_key FROM sessions').fetchone()
            if not row or not row[0] or not row[1] or not row[2] or not isinstance(row[3], bytes) or len(row[3]) != 256:
                raise ValueError('session 缺少完整登录密钥，请登录后重新导出')
        finally:
            conn.close()
        # Let the installed library perform its complete, version-specific migration.
        session = SQLiteSession(path)
        try:
            session.save()
        finally:
            session.close()
    except sqlite3.Error as exc:
        raise ValueError('session 数据库无效或表结构不兼容') from exc
