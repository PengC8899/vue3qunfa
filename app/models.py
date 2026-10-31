from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.sql import func
from app.database import Base


class SendLog(Base):
    __tablename__ = "send_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(64), index=True, nullable=True)
    task_round = Column(Integer, nullable=True)
    account_name = Column(String(64), index=True)
    group_id = Column(Integer, index=True)
    group_title = Column(String(255))
    message_preview = Column(Text)
    status = Column(String(32))
    error = Column(Text, nullable=True)
    message_id = Column(Integer, nullable=True)
    parse_mode = Column(String(16), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class JoinLog(Base):
    """加群日志：和发送日志分开记录，运行日志里可以单独看加群进度。"""
    __tablename__ = "join_logs"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), index=True)
    group_id = Column(Integer, index=True, nullable=True)
    group_username = Column(String(255), nullable=True)
    group_title = Column(String(255), nullable=True)
    status = Column(String(32), index=True)  # joined / skipped / requested / failed / retry / flood_wait
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class GroupCache(Base):
    __tablename__ = "group_caches"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), index=True)
    only_groups = Column(Integer)
    data_json = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String(64), primary_key=True, index=True)
    status = Column(String(32), index=True)
    total = Column(Integer)
    success = Column(Integer)
    failed = Column(Integer)
    account_name = Column(String(64), index=True)
    message = Column(Text)
    parse_mode = Column(String(16))
    disable_web_page_preview = Column(Integer)
    delay_ms = Column(Integer)
    delay_scope = Column(String(32), default="per_account")
    current_index = Column(Integer)
    group_ids_json = Column(Text)
    request_id = Column(String(128), nullable=True)
    paused = Column(Integer, default=0)
    stop_requested = Column(Integer, default=0)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    finished_at = Column(DateTime(timezone=True), nullable=True)
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    rounds = Column(Integer, default=1)
    current_round = Column(Integer, default=1)
    round_interval_s = Column(Integer, default=0)
    next_round_at = Column(DateTime(timezone=True), nullable=True)
    # 同群同文案的最小间隔（秒），跨账号生效：同一批内容不许两个账号在同一群里短时间各发一遍
    group_msg_cooldown_s = Column(Integer, default=180)
    # 1 = 动态任务：每轮从群池实时重算「该账号已加入且可发」的群。
    # 「边加群边群发」用它接住后台新加进来的群，普通任务保持 0 不变。
    dynamic_groups = Column(Integer, default=0)


class TaskEvent(Base):
    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(64), index=True)
    ts = Column(DateTime(timezone=True), server_default=func.now())
    event = Column(String(32))
    detail = Column(Text)
    meta_json = Column(Text)


class AccountHealth(Base):
    """账号健康状态跟踪"""
    __tablename__ = "account_health"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), unique=True, index=True)
    status = Column(String(32), default="pending")  # ok, error, pending, banned
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_join_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccountProxy(Base):
    __tablename__ = "account_proxies"

    id = Column(Integer, primary_key=True, index=True)
    account_name = Column(String(64), unique=True, index=True)
    enabled = Column(Integer, default=1)
    proxy_type = Column(String(16), default="socks5")
    host = Column(String(255))
    port = Column(Integer)
    username = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SystemKV(Base):
    __tablename__ = "system_kv"

    id = Column(Integer, primary_key=True, index=True)
    k = Column(String(64), unique=True, index=True)
    v = Column(Text)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SendPreset(Base):
    """保存下来的发送任务。

    id 就是用户嘴里的「几号任务」：自增、删除后不重用，选中编号点一次就能整条链路开跑。
    """
    __tablename__ = "send_presets"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=True)                 # 可选备注名，方便认出是哪个任务
    message = Column(Text)                                    # 要发送的正文
    parse_mode = Column(String(16), default="plain")
    disable_web_page_preview = Column(Integer, default=1)
    delay_ms = Column(Integer, default=60000)
    rounds = Column(Integer, default=100)
    round_interval_s = Column(Integer, default=600)
    # 同群同文案冷却（秒）：没有账号身份的差异，谁发都算，默认 180
    group_msg_cooldown_s = Column(Integer, default=180)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TargetGroup(Base):
    """目标群组池：用户导入的待投放群组。

    与 group_caches 完全不同：group_caches 存的是「某账号已加入了哪些群」，这里存的是「我们要往哪些群发」。
    """
    __tablename__ = "target_groups"

    id = Column(Integer, primary_key=True, index=True)
    raw = Column(Text)                                        # 用户原始输入，便于回溯
    username = Column(String(255), unique=True, index=True)   # 标准化后的唯一键：@xxx 或 +invite_hash
    link = Column(String(255))                                # https://t.me/...
    kind = Column(String(16), default="public")               # public | invite
    group_id = Column(Integer, nullable=True, index=True)     # 解析得到的 Telegram 数字 id，发送链路只认它
    title = Column(String(255), nullable=True)
    status = Column(String(32), default="pending", index=True)  # pending/available/checking/unreachable/done/failed/skipped
    joined_by_json = Column(Text, nullable=True)  # 已加入该群的账号名 JSON 数组：只有这些人能往这个群发
    last_error = Column(Text, nullable=True)
    last_run_at = Column(DateTime(timezone=True), nullable=True)  # 最后一次被投放到任务的时刻（不代表发送成功）
    created_at = Column(DateTime(timezone=True), server_default=func.now())
