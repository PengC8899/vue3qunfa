/** 后端英文状态 → 中文展示。i18n 收口在这里，视图里不要各写一套。 */

const EXACT: Record<string, string> = {
  success: '成功',
  ok: '成功',
  done: '已完成',
  running: '运行中',
  paused: '已暂停',
  pending: '等待中',
  waiting: '等待中',
  waiting_next_round: '等待下一轮',
  queued: '排队中',
  scheduled: '已排期',
  stopped: '已停止',
  processing: '处理中',
  sending: '发送中',
  active: '运行中',
  failed: '失败',
  error: '异常',
  normal: '正常',
  authorized: '已授权',
  unauthorized: '未授权',
  invalid: '无效',
  checking: '检查中',
  unchecked: '未检测',
  unknown: '未知',
  available: '可用',
  unreachable: '不可访问',
  approval_required: '需管理员审核',
  skipped: '已跳过',
  cannot_send: '不可发送',
  send_banned: '被限制群组发言',
  no_sendable_groups: '无可发群',
  missing_file: '缺少文件',
  connect_failed: '连接失败',
  connect_slow: '连接超时',
  auth_error: '鉴权失败',
  banned: '已封禁',
  slow: '连接慢',
  public: '公开群',
  invite: '邀请链接',
  session_not_authorized: '账号 Session 未授权',
  account_not_authorized: '账号未授权',
  no_authorized_accounts: '当前没有已授权账号',
  no_accounts_available: '当前没有可用账号',
  no_sendable_groups_for_accounts: '当前账号都没有可发群',
  no_groups_available_after_planning: '没有可投放的目标',
  no_groups_available_for_distributed_join_mode: '没有可用于分布式加群的群组',
  task_db_unavailable: '任务数据库暂时不可用',
  chat_write_forbidden: '群内禁止发言',
  user_banned_in_channel: '账号在群组中被禁言',
  peer_is_user: '目标不是群组',
  proxy_required_per_account: '已开启强制一账号一代理，且有账号缺少代理',
  plan_not_found: '方案已过期，请重新生成',
  plan_has_no_tasks: '该方案没有可执行任务',
  already_joined: '已加入',
  joined: '已加入',
  requested: '待审批',
  retry: '换号重试',
  flood_wait: '限流等待',
  forbidden: '被拒绝',
  not_found: '未找到',
  timeout: '超时',
}

const RULES: [RegExp, string][] = [
  [/http\s+(\d+)/gi, '请求失败（HTTP $1）'],
  [/Could not find the input entity for PeerChannel\(channel_id=(\d+)\).*$/gi, '找不到目标频道实体（ID: $1），可能该群组未缓存或当前账号无访问权限'],
  [/inline_send_failed:\s*You cannot send inline results in this chat.*$/gi, '内联发送失败：当前聊天不允许发送内联结果'],
  [/inline_send_failed:\s*The chat is restricted and cannot be used in that request.*$/gi, '内联发送失败：该聊天受限，当前请求无法使用'],
  [/A wait of (\d+) seconds is required before sending another message in this chat.*$/gi, '当前聊天触发频率限制，需等待 $1 秒后再发送'],
  [/Connection to Telegram failed (\d+) time\(s\)/gi, '连接 Telegram 失败（已重试 $1 次）'],
  [/RPCError 400:\s*TOPIC_CLOSED.*$/gi, '话题已关闭，无法发送消息'],
  [/session[_ ]not[_ ]authorized/gi, '账号 Session 未授权'],
  [/account[_ ]not[_ ]authorized/gi, '账号未授权'],
  [/no[_ ]authorized[_ ]accounts/gi, '当前没有已授权账号'],
  [/no[_ ]accounts[_ ]available/gi, '当前没有可用账号'],
  [/task[_ ]db[_ ]unavailable/gi, '任务数据库暂时不可用'],
  [/database disk image is malformed/gi, '数据库文件异常'],
  [/chat[_ ]write[_ ]forbidden/gi, '群内禁止发言'],
  [/user[_ ]banned[_ ]in[_ ]channel/gi, '账号在群组中被禁言'],
  [/already joined/gi, '已加入'],
  [/connection to telegram failed/gi, '连接 Telegram 失败'],
  [/telegram session not authorized/gi, 'Telegram Session 未授权'],
  [/phone code invalid/gi, '验证码错误'],
  [/phone code expired/gi, '验证码已过期'],
  [/flood wait/gi, '触发频率限制'],
  [/timed out/gi, '超时'],
  [/\btimeout\b/gi, '超时'],
  [/\bforbidden\b/gi, '被拒绝'],
  [/\bunauthorized\b/gi, '未授权'],
  [/\bnot found\b/gi, '未找到'],
  [/\binvalid\b/gi, '无效'],
  [/\bfailed\b/gi, '失败'],
  [/\berror\b/gi, '异常'],
]

const ACTION: Record<string, string> = { pause: '暂停', resume: '恢复', stop: '停止' }

export function translateText(value: unknown, fallback = '-'): string {
  if (value === null || value === undefined) return fallback
  const text = String(value).trim()
  if (!text) return fallback
  const exact = EXACT[text.toLowerCase()]
  if (exact) return exact
  let out = text
  for (const [pattern, replacement] of RULES) out = out.replace(pattern, replacement)
  return out
}

export type TagType = 'success' | 'warning' | 'danger' | 'info' | 'primary'

const SUCCESS = ['success', 'done', 'ok', 'normal', 'authorized', 'available', 'joined', '成功', '正常', '已授权', '可用', '已加入']
const RUNNING = ['running', 'sending', 'processing', 'active', '进行中', '检查中', '运行中']
const WARN = ['paused', 'pending', 'waiting', 'waiting_next_round', 'queued', 'scheduled', 'stopped',
  'unchecked', 'checking', 'unknown', 'slow', 'requested', 'retry', 'flood_wait',
  '未检测', '等待', '不可发送', '无可发群', '待审批', '限流',
  'approval_required', '需管理员审核']
const DANGER = ['failed', 'error', 'banned', 'unauthorized', 'invalid', 'unreachable', 'cannot_send',
  'send_banned', 'missing_file', 'connect_failed', 'auth_error', '失败', '异常', '未授权', '失效', '不可访问']

/** 直接给 el-tag 用的类型，不要再在视图里手写 statusClass 判断 */
export function statusTagType(value: unknown): TagType {
  const raw = String(value ?? '').trim().toLowerCase()
  if (!raw) return 'info'
  if (DANGER.some((k) => raw.includes(k))) return 'danger'
  if (SUCCESS.some((k) => raw.includes(k))) return 'success'
  if (RUNNING.some((k) => raw.includes(k))) return 'primary'
  if (WARN.some((k) => raw.includes(k))) return 'warning'
  return 'info'
}

export function actionLabel(action: string): string {
  return ACTION[String(action || '').trim().toLowerCase()] || action
}

export function formatTime(value: unknown): string {
  if (!value) return '-'
  const dt = new Date(String(value))
  if (Number.isNaN(dt.getTime())) return String(value)
  return dt.toLocaleString('zh-CN', { hour12: false })
}

export function formatProxy(proxy?: { proxy_url_masked?: string; proxy_url?: string } | null): string {
  if (!proxy) return '-'
  return proxy.proxy_url_masked || proxy.proxy_url || '-'
}
