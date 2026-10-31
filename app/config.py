import os
from datetime import datetime, timezone as tz
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()


class Settings:
    ADMIN_TOKEN: str
    DB_URL: str
    HOST: str
    PORT: int
    ACCOUNTS: dict
    DEFAULT_ACCOUNT: str
    SESSION_DIR: str
    SEND_RETRY_MAX: int
    SEND_RETRY_DELAY_MS: int
    SEND_MIN_DELAY_MS: int
    SEND_JITTER_PCT: float
    GROUP_CACHE_TTL_SECONDS: int
    GROUP_CACHE_ENABLED: int
    TIMEZONE: str
    TASK_STUCK_TIMEOUT_S: int
    TASK_CLEANUP_INTERVAL_S: int
    TASK_HEARTBEAT_INTERVAL_S: int
    TASK_RUNNER_MISSING_TIMEOUT_S: int
    CONNECTION_CLEANUP_INTERVAL_S: int
    TELEGRAM_MAX_CONCURRENT_CONNECTIONS: int
    TELEGRAM_IDLE_TIMEOUT_S: int
    SQLITE_BUSY_TIMEOUT_MS: int
    SQLITE_HEALTHCHECK_INTERVAL_S: int
    DB_POOL_SIZE: int
    DB_MAX_OVERFLOW: int
    DB_POOL_TIMEOUT_S: int
    DB_POOL_RECYCLE_S: int
    STARTUP_RESUME_MIN_DELAY_S: int
    STARTUP_RESUME_MAX_DELAY_S: int
    # 多账号并发配置
    MULTI_ACCOUNT_ENABLED: int
    MULTI_ACCOUNT_MAX_CONCURRENT: int
    MULTI_ACCOUNT_STAGGER_MS: int
    RESUME_TASKS_ON_STARTUP: int
    # 调度与风控参数
    SCHEDULER_ENABLED: int
    GROUP_RECENT_WINDOW: int
    GROUP_BLACK_FAIL_RATE: float
    GROUP_GREY_FAIL_RATE: float
    GROUP_BLACK_CONSEC_FAILS: int
    ACCOUNT_RECENT_WINDOW: int
    ACCOUNT_SAFE_FAIL_RATE: float
    ACCOUNT_RISK_FAIL_RATE: float
    ACCOUNT_CONSEC_FAILS_PAUSE: int
    MESSAGE_FINGERPRINT_ENABLED: int
    MESSAGE_NUMBER_JITTER_PCT: float
    MESSAGE_EMOJI_TOGGLE_PROB: float
    MESSAGE_WHITESPACE_JITTER_PROB: float
    SPINTAX_ENABLED: int
    # 账号预热与梯队门禁
    WARMUP_ENABLED: int
    WARMUP_AUTO_START: int
    WARMUP_READY_SCORE: int
    WARMUP_SCORE_PER_CYCLE: int
    WARMUP_CYCLE_INTERVAL_S: int
    WARMUP_READ_DIALOGS: int
    WARMUP_READ_MESSAGES: int
    WARMUP_MAX_CONCURRENT: int
    WARMUP_MAX_FAILURES: int
    SEND_RATE_INTERVAL_S: int

    def __init__(self):
        admin_token = os.getenv("ADMIN_TOKEN") or os.getenv("ADMIN_PASSWORD")
        if not admin_token:
            raise RuntimeError("Missing ADMIN_TOKEN in .env")
        self.ADMIN_TOKEN = admin_token
        
        # Support for multiple API keys
        self.TG_API_KEYS = []
        api_keys_str = os.getenv("TG_API_KEYS")
        if api_keys_str:
            try:
                import json
                self.TG_API_KEYS = json.loads(api_keys_str)
            except Exception:
                print("Failed to parse TG_API_KEYS, falling back to single key")
        
        if not self.TG_API_KEYS:
            # Fallback to single key if list is empty or invalid
            api_id = os.getenv("TG_API_ID")
            api_hash = os.getenv("TG_API_HASH")
            if api_id and api_hash:
                self.TG_API_KEYS.append({"api_id": api_id, "api_hash": api_hash})
        
        # Keep these for backward compatibility, but they might be None
        self.TG_API_ID = os.getenv("TG_API_ID")
        self.TG_API_HASH = os.getenv("TG_API_HASH")
        
        self.DB_URL = os.getenv("DB_URL", "sqlite:///./data.db")
        self.HOST = os.getenv("HOST", "0.0.0.0")
        self.PORT = int(os.getenv("PORT", "8000"))
        self.SESSION_DIR = os.getenv("SESSION_DIR", ".")
        self.SEND_RETRY_MAX = int(os.getenv("SEND_RETRY_MAX", "2"))
        self.SEND_RETRY_DELAY_MS = int(os.getenv("SEND_RETRY_DELAY_MS", "1500"))
        self.SEND_MIN_DELAY_MS = int(os.getenv("SEND_MIN_DELAY_MS", "3000"))
        try:
            self.SEND_JITTER_PCT = float(os.getenv("SEND_JITTER_PCT", "0.15"))
        except Exception:
            self.SEND_JITTER_PCT = 0.15
        self.GROUP_CACHE_TTL_SECONDS = int(os.getenv("GROUP_CACHE_TTL_SECONDS", "600"))
        self.GROUP_CACHE_ENABLED = int(os.getenv("GROUP_CACHE_ENABLED", "1"))
        self.TASK_STUCK_TIMEOUT_S = int(os.getenv("TASK_STUCK_TIMEOUT_S", "1800"))
        self.TASK_CLEANUP_INTERVAL_S = int(os.getenv("TASK_CLEANUP_INTERVAL_S", "60"))
        self.TASK_HEARTBEAT_INTERVAL_S = int(os.getenv("TASK_HEARTBEAT_INTERVAL_S", "20"))
        self.TASK_RUNNER_MISSING_TIMEOUT_S = int(os.getenv("TASK_RUNNER_MISSING_TIMEOUT_S", "120"))
        self.CONNECTION_CLEANUP_INTERVAL_S = int(os.getenv("CONNECTION_CLEANUP_INTERVAL_S", "60"))
        self.TELEGRAM_MAX_CONCURRENT_CONNECTIONS = int(os.getenv("TELEGRAM_MAX_CONCURRENT_CONNECTIONS", "5"))
        self.TELEGRAM_IDLE_TIMEOUT_S = int(os.getenv("TELEGRAM_IDLE_TIMEOUT_S", "300"))
        # 同一个群被几个不同账号分别撞上「你被本群禁言」才整群下架。1 就是老行为（一次即下架）。
        self.GLOBAL_BAN_VOTES_REQUIRED = max(1, int(os.getenv("GLOBAL_BAN_VOTES_REQUIRED", "3")))
        # 一批任务跑完后，最多自动换账号补发几轮（补的是「还没成功发出去的群」）。0 = 关掉。
        self.RESEND_MAX_SWEEPS = max(0, int(os.getenv("RESEND_MAX_SWEEPS", "2")))
        self.RESEND_IDLE_POLL_S = max(5, int(os.getenv("RESEND_IDLE_POLL_S", "30")))
        self.SQLITE_BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "15000"))
        self.SQLITE_HEALTHCHECK_INTERVAL_S = int(os.getenv("SQLITE_HEALTHCHECK_INTERVAL_S", "300"))
        self.DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "20"))
        self.DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "40"))
        self.DB_POOL_TIMEOUT_S = int(os.getenv("DB_POOL_TIMEOUT_S", "60"))
        self.DB_POOL_RECYCLE_S = int(os.getenv("DB_POOL_RECYCLE_S", "3600"))
        self.STARTUP_RESUME_MIN_DELAY_S = int(os.getenv("STARTUP_RESUME_MIN_DELAY_S", "5"))
        self.STARTUP_RESUME_MAX_DELAY_S = int(os.getenv("STARTUP_RESUME_MAX_DELAY_S", "45"))
        try:
            self.ACCOUNT_COUNT = int(os.getenv("ACCOUNT_COUNT", "20"))
        except Exception:
            self.ACCOUNT_COUNT = 20
        self.ACCOUNT_PREFIX = os.getenv("ACCOUNT_PREFIX", "account")
        
        # 时区配置 - 默认使用 Asia/Singapore (新加坡) 或 Asia/Kolkata (孟买)
        self.TIMEZONE = os.getenv("TIMEZONE", "Asia/Singapore")
        
        # 多账号并发配置
        self.MULTI_ACCOUNT_ENABLED = int(os.getenv("MULTI_ACCOUNT_ENABLED", "1"))
        self.MULTI_ACCOUNT_MAX_CONCURRENT = int(os.getenv("MULTI_ACCOUNT_MAX_CONCURRENT", "3"))
        # 账号之间的发送间隔 (ms)，防止风控
        self.MULTI_ACCOUNT_STAGGER_MS = int(os.getenv("MULTI_ACCOUNT_STAGGER_MS", "5000"))
        self.RESUME_TASKS_ON_STARTUP = int(os.getenv("RESUME_TASKS_ON_STARTUP", "1"))
        self.SCHEDULER_ENABLED = int(os.getenv("SCHEDULER_ENABLED", "1"))
        self.GROUP_RECENT_WINDOW = int(os.getenv("GROUP_RECENT_WINDOW", "50"))
        try:
            self.GROUP_BLACK_FAIL_RATE = float(os.getenv("GROUP_BLACK_FAIL_RATE", "0.6"))
        except Exception:
            self.GROUP_BLACK_FAIL_RATE = 0.6
        try:
            self.GROUP_GREY_FAIL_RATE = float(os.getenv("GROUP_GREY_FAIL_RATE", "0.3"))
        except Exception:
            self.GROUP_GREY_FAIL_RATE = 0.3
        self.GROUP_BLACK_CONSEC_FAILS = int(os.getenv("GROUP_BLACK_CONSEC_FAILS", "3"))
        self.ACCOUNT_RECENT_WINDOW = int(os.getenv("ACCOUNT_RECENT_WINDOW", "40"))
        try:
            self.ACCOUNT_SAFE_FAIL_RATE = float(os.getenv("ACCOUNT_SAFE_FAIL_RATE", "0.15"))
        except Exception:
            self.ACCOUNT_SAFE_FAIL_RATE = 0.15
        try:
            self.ACCOUNT_RISK_FAIL_RATE = float(os.getenv("ACCOUNT_RISK_FAIL_RATE", "0.35"))
        except Exception:
            self.ACCOUNT_RISK_FAIL_RATE = 0.35
        self.ACCOUNT_CONSEC_FAILS_PAUSE = int(os.getenv("ACCOUNT_CONSEC_FAILS_PAUSE", "3"))
        self.MESSAGE_FINGERPRINT_ENABLED = int(os.getenv("MESSAGE_FINGERPRINT_ENABLED", "1"))
        try:
            self.MESSAGE_NUMBER_JITTER_PCT = float(os.getenv("MESSAGE_NUMBER_JITTER_PCT", "0.03"))
        except Exception:
            self.MESSAGE_NUMBER_JITTER_PCT = 0.03
        try:
            self.MESSAGE_EMOJI_TOGGLE_PROB = float(os.getenv("MESSAGE_EMOJI_TOGGLE_PROB", "0.25"))
        except Exception:
            self.MESSAGE_EMOJI_TOGGLE_PROB = 0.25
        try:
            self.MESSAGE_WHITESPACE_JITTER_PROB = float(os.getenv("MESSAGE_WHITESPACE_JITTER_PROB", "0.4"))
        except Exception:
            self.MESSAGE_WHITESPACE_JITTER_PROB = 0.4
        self.RISK_TRY_GROUPS = int(os.getenv("RISK_TRY_GROUPS", "2"))

        # Spintax 文案变体：文案里写 {你好|您好|hi} 时每条消息随机取一个分支。
        # 只在文案确实带变体标记时才生效，没有标记的文案原样发。
        self.SPINTAX_ENABLED = int(os.getenv("SPINTAX_ENABLED", "1"))
        # 账号预热与梯队门禁。老号（在我们库里成功发过消息的）直接当成熟账号豁免，
        # 只有全新导入、一次都没发成的号才需要先预热，否则上线当天会把存量号全拦住。
        self.WARMUP_ENABLED = int(os.getenv("WARMUP_ENABLED", "1"))
        # 攒够多少分算「已毕业」，可以进群发
        self.WARMUP_READY_SCORE = max(1, int(os.getenv("WARMUP_READY_SCORE", "60")))
        # 每跑一轮只读预热加多少分（默认 10 分 / 5 分钟一轮 → 5 轮约 25 分钟毕业）
        self.WARMUP_SCORE_PER_CYCLE = max(1, int(os.getenv("WARMUP_SCORE_PER_CYCLE", "10")))
        self.WARMUP_CYCLE_INTERVAL_S = max(60, int(os.getenv("WARMUP_CYCLE_INTERVAL_S", "300")))
        # 新号导入后自动进预热队列，容器重启后自动续跑未毕业的号
        self.WARMUP_AUTO_START = int(os.getenv("WARMUP_AUTO_START", "1"))
        # 每轮拉多少个会话、每个群翻多少条历史
        self.WARMUP_READ_DIALOGS = max(1, int(os.getenv("WARMUP_READ_DIALOGS", "15")))
        self.WARMUP_READ_MESSAGES = max(1, int(os.getenv("WARMUP_READ_MESSAGES", "8")))
        self.WARMUP_MAX_CONCURRENT = max(1, int(os.getenv("WARMUP_MAX_CONCURRENT", "5")))
        # 连续失败这么多轮就放弃这个号，别对着掉线的 session 一直空转
        self.WARMUP_MAX_FAILURES = max(1, int(os.getenv("WARMUP_MAX_FAILURES", "6")))
        # 取不到梯队规格时每账号两条消息之间的兜底间隔（秒）
        self.SEND_RATE_INTERVAL_S = max(1, int(os.getenv("SEND_RATE_INTERVAL_S", "20")))

        # Parse Accounts
        accounts = {}
        accounts_list = os.getenv("ACCOUNTS") or os.getenv("TG_ACCOUNTS")
        if accounts_list:
            names = [x.strip() for x in accounts_list.split(",") if x.strip()]
            for name in names:
                api_id = os.getenv(f"TG_{name}_API_ID")
                api_hash = os.getenv(f"TG_{name}_API_HASH")
                session_name = os.getenv(f"TG_{name}_SESSION_NAME") or os.getenv("TG_SESSION_NAME") or name
                
                # 如果没有特定账号的配置，尝试使用全局配置，如果也没有，则留空让 AccountClientManager 自动从池中选择
                if not api_id:
                    api_id = os.getenv("TG_API_ID")
                if not api_hash:
                    api_hash = os.getenv("TG_API_HASH")
                
                # 只有在没有 API Pool 的情况下才强制要求 api_id/hash
                if (not api_id or not api_hash) and not self.TG_API_KEYS:
                    raise RuntimeError(f"Missing TG_{name}_API_ID/TG_API_ID or TG_{name}_API_HASH/TG_API_HASH in .env")
                
                accounts[name] = {
                    "api_id": int(api_id) if api_id else None,
                    "api_hash": api_hash,
                    "session_name": session_name,
                }
            self.DEFAULT_ACCOUNT = names[0]
        else:
            api_id = os.getenv("TG_API_ID")
            api_hash = os.getenv("TG_API_HASH")
            session_name = os.getenv("TG_SESSION_NAME")
            
            # 如果有 API Pool，则允许不配置单个 TG_API_ID
            if (not api_id or not api_hash) and not self.TG_API_KEYS:
                 # 只有当既没有单个配置也没有 Pool 时才报错，但为了兼容性，如果连 session_name 都没有，可能不需要配置 ACCOUNTS
                 if session_name:
                     raise RuntimeError("Missing TG_API_ID, TG_API_HASH in .env")
            
            if session_name:
                accounts[session_name] = {
                    "api_id": int(api_id) if api_id else None,
                    "api_hash": api_hash,
                    "session_name": session_name,
                }
                self.DEFAULT_ACCOUNT = session_name
            else:
                self.DEFAULT_ACCOUNT = "default"

        self.ACCOUNTS = accounts

    def now(self) -> datetime:
        """获取当前时间 (服务器配置的时区)"""
        try:
            tz_info = ZoneInfo(self.TIMEZONE)
            return datetime.now(tz_info)
        except Exception:
            # 如果时区配置无效，回退到 UTC
            return datetime.now(tz.utc)

    def get_timezone(self):
        """获取配置的时区对象"""
        try:
            return ZoneInfo(self.TIMEZONE)
        except Exception:
            return tz.utc


CONFIG = Settings()
