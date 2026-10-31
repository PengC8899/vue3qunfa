import os
import asyncio
import json
import time
import urllib.request
from telethon.errors import (
    UserDeactivatedError,
    UserDeactivatedBanError,
)
from app.config import CONFIG
from app.services import warmup

# 账号检测结果落库用的 key 前缀（system_kv），让检测结果能活过容器重启
STATUS_KEY_PREFIX = "account_status:"

# 由发送链路（main）注入：判断这个账号是否还在「限制群组发言」冻结期。
# 冻结名单和到期时间在发送链路里，这里只调用，避免循环 import。
SEND_BLOCK_CHECK = None

# 连接阶段拿到的异常里，哪些说明是「账号自己的登录状态坏了」而不是网络问题。
# 8004 实测：42 个已掉线的号被归成 connect_failed，detail 只有一句英文，
# 用户既不知道该补号还是该查代理。这里把桶分开。
_DEAD_SESSION_MARKERS = (
    "not authorized",
    "authkey",
    "key is not registered",
    "authorization key",
    "session revoked",
    "sessionrevoked",
    "not logged in",
)
_BANNED_MARKERS = (
    "phone_number_banned",
    "userdeactivated",
    "user is deleted",
    "deactivated",
)


def _classify_failure(session_name: str, exc: Exception) -> dict:
    """连接阶段失败归类：封号 / 登录失效 / 真的连不上，三者要分得开。"""
    text = str(exc)
    low = text.lower()
    if isinstance(exc, (UserDeactivatedError, UserDeactivatedBanError)) or any(m in low for m in _BANNED_MARKERS):
        return {"account": session_name, "status": "banned", "valid": False, "detail": text[:100]}
    if any(m in low for m in _DEAD_SESSION_MARKERS):
        return {"account": session_name, "status": "unauthorized", "valid": False, "detail": text[:100]}
    return {"account": session_name, "status": "connect_failed", "valid": False, "detail": text[:100]}


async def _raw_auth_error(client) -> str | None:
    """问 Telethon 要 Telegram 的原始 401 原文。

    `is_user_authorized()` 把 AUTH_KEY_UNREGISTERED 之类的错误吞成了 False，
    只剩一句「未授权」，看不出是掉线、被注销还是会话冲突。再发一次请求就能拿到原文。
    """
    from telethon.tl.functions.users import GetUsersRequest
    from telethon.tl.types import InputUserSelf

    try:
        await client(GetUsersRequest([InputUserSelf()]))
    except Exception as exc:
        return str(exc)[:160]
    return None


# #region debug-point C:health-check-server-report
def _report_health_check_debug(hypothesis_id: str, location: str, msg: str, data: dict | None = None):
    if os.getenv("HEALTH_CHECK_DEBUG", "").lower() not in ("1", "true", "yes"):
        return
    try:
        debug_url = "http://127.0.0.1:7777/event"
        session_id = "health-check-stall"
        try:
            with open(".dbg/health-check-stall.env", "r", encoding="utf-8") as fh:
                content = fh.read().splitlines()
            for line in content:
                if line.startswith("DEBUG_SERVER_URL="):
                    debug_url = line.split("=", 1)[1].strip() or debug_url
                elif line.startswith("DEBUG_SESSION_ID="):
                    session_id = line.split("=", 1)[1].strip() or session_id
        except Exception:
            pass
        payload = json.dumps({
            "sessionId": session_id,
            "runId": "post-fix",
            "hypothesisId": hypothesis_id,
            "location": location,
            "msg": f"[DEBUG] {msg}",
            "data": data or {},
            "ts": int(time.time() * 1000),
        }).encode("utf-8")
        urllib.request.urlopen(
            urllib.request.Request(
                debug_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            ),
            timeout=1,
        ).read()
    except Exception:
        pass
# #endregion

class AccountService:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(3)  # 减少并发，避免网络拥塞
        self._manager = None
        self.check_timeout = 10  # 减少超时到 10 秒
        self._cache = {}  # 缓存检查结果
        self._cache_ttl = 60  # 缓存 60 秒
    
    def set_manager(self, manager):
        """Set the multi_manager reference"""
        self._manager = manager

    def cached_status(self, session_name: str) -> dict:
        """Read the last check without starting any Telegram connection.

        两份缓存（带/不带群发言检测）里取**更新时间更晚**的那份：只认固定的
        一份会让「重检被挡账号」这种带 include_group_send_check 的结果白跑。
        """
        entries = [
            self._cache[key]
            for key in ((session_name, False), (session_name, True))
            if key in self._cache
        ]
        if entries:
            checked_at, result = max(entries, key=lambda item: item[0])
            return {**result, "checked_at": checked_at, "stale": time.time() - checked_at >= self._cache_ttl}
        return {"account": session_name, "status": "unchecked", "valid": False,
                "detail": "已导入，尚未检测登录状态", "checked_at": None, "stale": False}

    def load_persisted_statuses(self) -> int:
        """启动时把上次的检测结果读回内存。

        否则每次 `up -d --build` 都会把全部账号打回「未检测」，用户得重跑一遍全量检测
        才能加群/调度。检测结果本身仍带 checked_at，前端照旧能看出是不是旧的。
        """
        from app.database import SessionLocal
        from app.models import SystemKV

        db = SessionLocal()
        try:
            rows = db.query(SystemKV).filter(SystemKV.k.like(f"{STATUS_KEY_PREFIX}%")).all()
        except Exception:
            return 0
        finally:
            db.close()
        loaded = 0
        for row in rows:
            name = row.k[len(STATUS_KEY_PREFIX):]
            try:
                result = json.loads(row.v or "{}")
                checked_at = float(result.pop("_checked_at", 0)) or time.time()
            except Exception:
                continue
            if not result:
                continue
            self._cache[(name, False)] = (checked_at, result)
            loaded += 1
        return loaded

    def mark_account_dead(self, session_name: str, detail: str) -> dict:
        """把账号当场判为「登录已失效」并落库。

        加群/发送撞到 401 时用：那一瞬间拿到的才是真的，比几十小时前的快照可信，
        而且当场落库之后，下一轮就不会再把它放进池子里。
        """
        result = {"account": session_name, "status": "unauthorized", "valid": False,
                  "detail": detail}
        self._cache[(session_name, False)] = (time.time(), result)
        self._persist_status(session_name, result)
        return result

    def mark_account_send_blocked(self, session_name: str, detail: str) -> dict:
        """账号被 Telegram 限制在群组发言（spam ban）：当场标记，别让它挨个群去撞。

        和「某个群禁言了这个号」不同：群本身是好的、账号也还是成员，是账号自己被
        平台限制了。继续换群发只会多几条失败记录，还可能加重处罚。
        """
        result = {"account": session_name, "status": "send_banned", "valid": True,
                  "can_send_in_groups": False, "detail": detail}
        self._cache[(session_name, False)] = (time.time(), result)
        self._persist_status(session_name, result)
        return result

    def clear_send_block(self, session_name: str) -> dict:
        """限制发言的冷却到期：把账号放回池子，别让它永远停在「已停止」。

        只清 send_banned：这中间要是又检测出了别的状态，说明有更新的结论，不覆盖。
        放回去之后第一条发不出去就会重新撞上标记，不需要额外探测。
        """
        current = self.cached_status(session_name)
        if current.get("status") != "send_banned":
            return current
        result = {"account": session_name, "status": "ok", "valid": True,
                  "detail": "限制发言冷却已到期，重新放回发送池"}
        self._cache[(session_name, False)] = (time.time(), result)
        self._persist_status(session_name, result)
        return result

    def mark_account_alive(self, session_name: str, detail: str = "预热只读动作成功") -> dict:
        """预热能读通就说明 session 还活着：顺手把「未检测」刷成可用，省一次全量检测。

        只升不降：已经判定不能发言 / 被封的号，不因为能读就覆盖掉。
        """
        current = self.cached_status(session_name)
        if current.get("status") in ("cannot_send", "banned", "send_banned"):
            return current
        result = {"account": session_name, "status": "ok", "valid": True, "detail": detail}
        self._cache[(session_name, False)] = (time.time(), result)
        self._persist_status(session_name, result)
        return result

    def _persist_status(self, session_name: str, result: dict) -> None:
        """落库这次检测结果；纯顺手保存，失败不影响检测本身。"""
        from app.database import SessionLocal
        from app.models import SystemKV

        if not result or result.get("status") in (None, "unchecked"):
            return
        payload = json.dumps({**result, "_checked_at": time.time()}, ensure_ascii=False)
        db = SessionLocal()
        try:
            key = STATUS_KEY_PREFIX + session_name
            row = db.query(SystemKV).filter(SystemKV.k == key).first()
            if row:
                row.v = payload
            else:
                db.add(SystemKV(k=key, v=payload))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    async def check_account(self, session_name: str, use_cache: bool = True, include_group_send_check: bool = False):
        result = await self._check_account(session_name, use_cache=use_cache,
                                          include_group_send_check=include_group_send_check)
        # 体检只证明「登录是活的」，不证明「能在群里发言」。被平台限制发言的账号
        # 一样能连上、能读消息，检测结果不能把它刷回可用——否则界面显示正常、
        # 发送池却一直不派活，看起来就像「检测 10 个正常，只有 2 个能跑」。
        # 是否仍在冻结由发送链路注入的 SEND_BLOCK_CHECK 判断（它带着 24 小时的到期时间）。
        blocked = bool(SEND_BLOCK_CHECK and SEND_BLOCK_CHECK(session_name))
        if blocked and str(result.get("status") or "") != "send_banned":
            result = {
                **result,
                "status": "send_banned",
                "valid": True,
                "can_send_in_groups": False,
                "detail": "账号被 Telegram 限制在群组发言（spam ban），冻结还没到期",
            }
            self._cache[(session_name, False)] = (time.time(), result)
        self._persist_status(session_name, result)
        return result

    async def _check_account(self, session_name: str, use_cache: bool = True, include_group_send_check: bool = False):
        """检查账号状态，支持缓存"""
        # #region debug-point C:check-account-start
        started_at = time.time()
        _report_health_check_debug("C", "app/services/account_service.py:check_account:start", "check_account.start", {
            "account": session_name,
            "use_cache": bool(use_cache),
            "include_group_send_check": bool(include_group_send_check),
        })
        # #endregion
        cache_key = (session_name, bool(include_group_send_check))
        # 检查缓存
        if use_cache and cache_key in self._cache:
            cached_time, cached_result = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                # #region debug-point C:check-account-cache-hit
                _report_health_check_debug("C", "app/services/account_service.py:check_account:cache-hit", "check_account.cache-hit", {
                    "account": session_name,
                    "elapsedMs": int((time.time() - started_at) * 1000),
                    "status": cached_result.get("status"),
                })
                # #endregion
                return cached_result
        
        async with self.semaphore:
            timeout_s = max(self.check_timeout, 20) if include_group_send_check else self.check_timeout
            try:
                result = await asyncio.wait_for(
                    self._do_check(session_name, include_group_send_check=include_group_send_check),
                    timeout=timeout_s
                )
            except asyncio.TimeoutError:
                result = {"account": session_name, "status": "timeout", "valid": False, "detail": f"连接超时 ({timeout_s}s)"}
            
            # 缓存结果
            self._cache[cache_key] = (time.time(), result)
            # #region debug-point C:check-account-finish
            _report_health_check_debug("C", "app/services/account_service.py:check_account:finish", "check_account.finish", {
                "account": session_name,
                "elapsedMs": int((time.time() - started_at) * 1000),
                "status": result.get("status"),
                "valid": result.get("valid"),
                "detail": result.get("detail"),
            })
            # #endregion
            return result

    async def _do_check(self, session_name: str, include_group_send_check: bool = False):
        """实际的检查逻辑 - 优化版"""
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        last_error = None
        
        # 1. 检查 session 文件是否存在
        if not os.path.exists(f"{session_base}.session"):
            return {"account": session_name, "status": "missing_file", "valid": False}
        
        if not self._manager:
            return {"account": session_name, "status": "file_exists", "valid": True, "detail": "Session 文件存在"}
        
        try:
            # 2. 检查是否已在 manager 中加载
            mgr = self._manager.get(session_name)
            
            # 3. 快速检查：如果客户端已连接，直接用缓存的信息
            if mgr.client and mgr.client.is_connected():
                try:
                    # 使用超短超时获取用户信息
                    me = await asyncio.wait_for(mgr.client.get_me(), timeout=5)
                    if me:
                        result = {
                            "account": session_name,
                            "status": "ok",
                            "valid": True,
                            "phone": me.phone,
                            "id": me.id,
                            "username": getattr(me, 'username', None),
                            "first_name": getattr(me, 'first_name', None),
                        }
                        if include_group_send_check:
                            result.update(await self._inspect_group_send_capability(session_name))
                            if not result.get("can_send_in_groups", True):
                                result["status"] = "cannot_send"
                        return result
                except asyncio.TimeoutError:
                    return {"account": session_name, "status": "slow", "valid": True, "detail": "连接慢但可用"}
                except (UserDeactivatedError, UserDeactivatedBanError):
                    return {"account": session_name, "status": "banned", "valid": False}
                except Exception as e:
                    last_error = e
            
            # 4. 如果未连接，尝试连接（可能较慢）
            try:
                await asyncio.wait_for(mgr.ensure_connected(), timeout=8)
            except asyncio.TimeoutError:
                return {"account": session_name, "status": "connect_slow", "valid": False, "detail": "连接超时"}
            except Exception as e:
                return _classify_failure(session_name, e)
            
            # 5. 检查授权
            try:
                authorized = await asyncio.wait_for(mgr.client.is_user_authorized(), timeout=5)
                if not authorized:
                    detail = await _raw_auth_error(mgr.client) or "Telegram session 未授权"
                    return {"account": session_name, "status": "unauthorized", "valid": False, "detail": detail}
            except Exception as e:
                return {"account": session_name, "status": "auth_error", "valid": False, "detail": str(e)[:100]}
            
            # 6. 获取用户信息
            try:
                me = await asyncio.wait_for(mgr.client.get_me(), timeout=5)
                if me:
                    result = {
                        "account": session_name,
                        "status": "ok",
                        "valid": True,
                        "phone": me.phone,
                        "id": me.id,
                        "username": getattr(me, 'username', None),
                        "first_name": getattr(me, 'first_name', None),
                    }
                    if include_group_send_check:
                        result.update(await self._inspect_group_send_capability(session_name))
                        if not result.get("can_send_in_groups", True):
                            result["status"] = "cannot_send"
                    return result
            except (UserDeactivatedError, UserDeactivatedBanError):
                return {"account": session_name, "status": "banned", "valid": False}
            except Exception as e:
                last_error = e
            
            if last_error is not None:
                return {
                    "account": session_name,
                    "status": "error",
                    "valid": False,
                    "detail": str(last_error)[:120],
                }
            return {"account": session_name, "status": "unknown", "valid": False, "detail": "无法读取账号资料"}
            
        except Exception as e:
            return {"account": session_name, "status": "error", "valid": False, "detail": str(e)[:50]}

    def clear_cache(self):
        """清除缓存"""
        self._cache.clear()

    async def _inspect_group_send_capability(self, session_name: str):
        if not self._manager:
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": "管理器未初始化，无法检测群发言权限",
            }
        try:
            # #region debug-point D:group-send-check-start
            started_at = time.time()
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:start", "group_send_check.start", {
                "account": session_name,
            })
            # #endregion
            result = await asyncio.wait_for(
                self._manager.inspect_group_send_capability(session_name),
                timeout=max(self.check_timeout, 15),
            )
            # #region debug-point D:group-send-check-finish
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:finish", "group_send_check.finish", {
                "account": session_name,
                "elapsedMs": int((time.time() - started_at) * 1000),
                "checked_groups": result.get("checked_groups"),
                "can_send_in_groups": result.get("can_send_in_groups"),
            })
            # #endregion
            return result
        except asyncio.TimeoutError:
            # #region debug-point D:group-send-check-timeout
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:timeout", "group_send_check.timeout", {
                "account": session_name,
                "timeoutS": max(self.check_timeout, 15),
            })
            # #endregion
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": f"群发言权限检查超时 ({self.check_timeout}s)",
            }
        except Exception as exc:
            # #region debug-point D:group-send-check-error
            _report_health_check_debug("D", "app/services/account_service.py:_inspect_group_send_capability:error", "group_send_check.error", {
                "account": session_name,
                "error": str(exc)[:120],
            })
            # #endregion
            return {
                "can_send_in_groups": False,
                "checked_groups": 0,
                "sendable_groups": 0,
                "detail": f"群发言权限检查失败: {str(exc)[:80]}",
            }

    async def check_all_accounts(self):
        count = getattr(CONFIG, "ACCOUNT_COUNT", 100)
        prefix = getattr(CONFIG, "ACCOUNT_PREFIX", "account")
        names = [f"{prefix}_{i:02d}" for i in range(1, count + 1)]
        
        tasks = [self.check_account(name) for name in names]
        results = await asyncio.gather(*tasks)
        return results

    async def delete_session(self, session_name: str):
        session_base = os.path.join(CONFIG.SESSION_DIR, session_name)
        deleted = False
        # journal/wal/shm 是 Telethon 的 SQLite 副产物，账号删了它们就是垃圾文件
        for ext in [".session", ".session-journal", ".session-wal", ".session-shm"]:
            path = f"{session_base}{ext}"
            if os.path.exists(path):
                try:
                    os.remove(path)
                    deleted = True
                except Exception as e:
                    print(f"Error deleting {path}: {e}")
        # 清除缓存
        for key in list(self._cache.keys()):
            if isinstance(key, tuple):
                if key[0] == session_name:
                    del self._cache[key]
            elif key == session_name:
                del self._cache[key]
        self.forget_persisted(session_name)
        return deleted

    def forget_persisted(self, session_name: str) -> None:
        """把落库的检测结果与群组缓存一起清掉。

        账号都删了，这些行只会变成孤儿：重新上传同一个号时，
        上一次的「登录已失效」和旧的已加入群缓存会被读回来当成现状。
        """
        from app.database import SessionLocal
        from app.models import GroupCache, SystemKV

        try:
            warmup.forget_state(session_name)
        except Exception:
            pass

        db = SessionLocal()
        try:
            db.query(SystemKV).filter(SystemKV.k == STATUS_KEY_PREFIX + session_name).delete(
                synchronize_session=False)
            db.query(GroupCache).filter(GroupCache.account_name == session_name).delete(
                synchronize_session=False)
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"[ACCOUNT] 清理 {session_name} 的历史记录失败: {exc}")
        finally:
            db.close()

account_service = AccountService()
