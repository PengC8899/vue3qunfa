/** 后端返回的基础类型定义。只声明前端真正读到的字段。 */

export interface ProxyBinding {
  account: string
  enabled: boolean
  proxy_type: string
  host: string
  port: number
  username?: string | null
  password?: string | null
  proxy_url?: string
  proxy_url_masked?: string
  updated_at?: string | null
}

/** 账号梯队与预热进度。exempt=true 表示这个号发过成功消息，直接算成熟账号 */
export interface WarmupInfo {
  account: string
  tier: string
  tier_label: string
  readiness: number
  ready_score: number
  cycles: number
  exempt: boolean
  interval_s: number
  daily_max: number
  graduated_at?: string | null
  last_cycle_at?: string | null
  last_error?: string | null
  warmup_running?: boolean
}

export interface WarmupProgress {
  running: boolean
  started_at?: string | null
  finished_at?: string | null
  current?: string | null
  accounts: Record<string, {
    readiness?: number
    cycles?: number
    ok?: boolean
    done?: boolean
    exempt?: boolean
    error?: string
  }>
}

export interface AccountRow {
  account: string
  authorized: boolean
  status: string
  detail?: string | null
  phone?: string | null
  username?: string | null
  first_name?: string | null
  id?: number | null
  can_send_in_groups?: boolean
  sendable_groups?: number
  checked_groups?: number
  proxy?: ProxyBinding | null
  has_proxy: boolean
  checked_at?: number | null
  stale?: boolean
  warmup?: WarmupInfo | null
}

export interface DashboardOverview {
  accounts: {
    total: number
    ready: number
    error: number
    unchecked: number
    no_proxy: number
  }
  tasks: { running: number; by_status: Record<string, number> }
  logs: { by_status: Record<string, number> }
  groups: { total: number; by_status: Record<string, number> }
  scheduler: { status: string }
}

export interface TargetGroup {
  id: number
  username: string
  link: string
  kind: string
  status: string
  title?: string | null
  group_id?: number | null
  /** 已加入该群的账号数（加群结果落库），只有这些账号能往这个群发 */
  joined_accounts: number
  joined_sample?: string[]
  last_error?: string | null
  last_run_at?: string | null
}

export interface GroupPool {
  ok: boolean
  total: number
  by_status: Record<string, number>
  items: TargetGroup[]
}

export interface ImportResult {
  ok: boolean
  total: number
  imported: number
  duplicate: number
  invalid: number
  invalid_samples: string[]
}

export interface TaskSummaryRow {
  account: string
  status: string
  task_id: string
  delay_scope: string
  total: number
  success: number
  failed: number
  completed: number
  progress_total: number
  progress_completed: number
  current_round: number
  rounds: number
  current_round_planned: number
  current_round_sent: number
  overall_planned: number
  overall_completed: number
  next_round_at?: string | null
  wait_seconds: number
  last_updated_at?: string | null
}

export interface LogRow {
  id: number
  task_id?: string | null
  task_round?: number | null
  account_name?: string | null
  group_id?: number | null
  group_title?: string | null
  status?: string | null
  error?: string | null
  message_id?: number | null
  message_preview?: string | null
  parse_mode?: string | null
  created_at?: string | null
}

export interface RunLogPage {
  ok: boolean
  items: LogRow[]
  total: number
  started_at?: string | null
  finished_at?: string | null
  frozen?: boolean
}

export interface JoinLogRow {
  id: number
  account_name?: string | null
  group_id?: number | null
  group_username?: string | null
  group_title?: string | null
  status?: string | null
  error?: string | null
  created_at?: string | null
}

export interface RecheckResult {
  ok: boolean
  detail?: string
  message?: string
  checked: number
  available: number
  unreachable: number
  accounts_ready?: number
}

export interface JoinItem {
  account: string
  row_id: number
  peer_id?: number | null
  username: string
  title?: string | null
  status: 'pending' | 'joined' | 'skipped' | 'requested' | 'failed'
  error?: string | null
  /** 已经试过这个群的账号：加不上会换人接手，这里是换过谁 */
  tried?: string[]
}

export interface JoinAccountPlan {
  account: string
  count: number
  sample: string[]
}

export interface JoinJob {
  running: boolean
  started_at: string
  finished_at?: string | null
  /** 多账号并发，这里是 {账号: 正在加的群} */
  current?: Record<string, string>
  delay_ms: number
  workers: number
  total: number
  done: number
  joined: number
  skipped: number
  requested: number
  failed: number
  /** 还没定生死的（在等限流冷却，或正在换账号接手） */
  pending?: number
  /** 换账号接手的次数：一个群加不上就换人，不是直接判失败 */
  redispatches?: number
  /** 被 Telegram 限流（FloodWait）的次数：进度卡住基本都是它在等 */
  flood_waits?: number
  items: JoinItem[]
}

export interface JoinPreview {
  ok: boolean
  started: boolean
  groups: number
  accounts_used: number
  accounts_ready: number
  accounts_blocked: Record<string, number>
  /** 因为已经有账号在里面而没重复加的群数 */
  groups_covered_skipped?: number
  per_account: JoinAccountPlan[]
  job?: JoinJob | null
}

export interface JoinedGroup {
  id: number
  title?: string | null
  username?: string | null
  member_count?: number | null
  can_send?: boolean
  [key: string]: unknown
}

export interface ProxyOverviewItem extends AccountRow {}

export interface ProxyOverview {
  ok?: boolean
  total: number
  bound: number
  unbound: number
  items: ProxyOverviewItem[]
  policy?: { enforce_one_to_one: boolean }
}

export interface ProfileRow {
  ok: boolean
  about?: string | null
  error?: string | null
  [key: string]: unknown
}

export interface ProxyPolicy {
  enforce_one_to_one: boolean
}

/** 保存下来的发送任务，no 就是用户嘴里的「几号任务」。 */
export interface SendPreset {
  no: number
  name: string
  message: string
  parse_mode: 'plain' | 'markdown' | 'html'
  disable_web_page_preview: boolean
  delay_ms: number
  rounds: number
  round_interval_s: number
  /** 同群同文案的最小间隔（秒），跨账号生效 */
  group_msg_cooldown_s: number
  created_at?: string | null
}

export interface PipelineStep {
  key: 'recheck' | 'join' | 'plan' | 'send'
  label: string
  status: 'pending' | 'running' | 'done' | 'failed'
  detail: string
}

/** 一键链路：重检被挡账号 → 补加群 → 生成方案 → 开始发送，进度是后端进程内存态。 */
export interface PipelineRun {
  running: boolean
  status: 'running' | 'done' | 'failed' | 'stopped'
  stage: string
  detail: string
  error?: string | null
  preset_no: number
  preset_name: string
  run_recheck: boolean
  run_join: boolean
  steps: PipelineStep[]
  result?: { accounts_count?: number; planned_groups?: number } | null
  /** 加群被 Telegram 拦住、到上限先停加群了；发送仍照跑 */
  join_incomplete?: boolean
  /** 本次执行的累计：从点开始算起，停止后封口不再增长 */
  stats?: { send_success: number; send_failed?: number; join_success: number }
  started_at: string
  finished_at?: string | null
}
