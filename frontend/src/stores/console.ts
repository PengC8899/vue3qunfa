import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { DashboardOverview, GroupPool, ImportResult, RecheckResult, TargetGroup } from '@/api/types'
import { useUiStore } from '@/stores/ui'

const EMPTY_OVERVIEW: DashboardOverview = {
  accounts: { total: 0, ready: 0, error: 0, unchecked: 0, no_proxy: 0 },
  tasks: { running: 0, by_status: {} },
  logs: { by_status: {} },
  groups: { total: 0, by_status: {} },
  scheduler: { status: 'IDLE' },
}

const EMPTY_POOL: GroupPool = { ok: true, total: 0, by_status: {}, items: [] }

export const useConsoleStore = defineStore('console', () => {
  const ui = useUiStore()

  const overview = ref<DashboardOverview>({ ...EMPTY_OVERVIEW })
  const pool = ref<GroupPool>({ ...EMPTY_POOL })
  const importResult = ref<ImportResult | null>(null)
  const importText = ref('')
  const poolFilter = ref('')
  const poolStatusFilter = ref('')
  const onlyUnjoined = ref(false)
  const selectedGroupIds = ref<number[]>([])
  const errorDrawerOpen = ref(false)

  const loading = reactive({ overview: false, importing: false, rechecking: false })

  const kpis = computed(() => {
    const acc = overview.value.accounts
    const tasks = overview.value.tasks
    return [
      { label: '账号', value: acc.total, tone: '' },
      { label: '可用账号', value: acc.ready, tone: 'ok' },
      { label: '异常账号', value: acc.error, tone: acc.error ? 'bad' : '' },
      { label: '未检测', value: acc.unchecked, tone: acc.unchecked ? 'warn' : '' },
      { label: '目标群组', value: pool.value.total, tone: '' },
      { label: '运行任务', value: tasks.running, tone: tasks.running ? 'ok' : '' },
    ]
  })

  const filteredGroups = computed<TargetGroup[]>(() => {
    const q = poolFilter.value.trim().toLowerCase()
    return pool.value.items.filter((item) => {
      if (poolStatusFilter.value && item.status !== poolStatusFilter.value) return false
      if (onlyUnjoined.value && item.joined_accounts) return false
      if (!q) return true
      return String(item.username || '').toLowerCase().includes(q) ||
        String(item.title || '').toLowerCase().includes(q)
    })
  })

  /** 没人加入的群发不出去，得先加群：单独筛出来方便去补加群。 */
  const unjoinedCount = computed(() => pool.value.items.filter((item) => !item.joined_accounts).length)

  /** §20 错误中心：只统计后端真的给出的字段，不虚构「Worker 异常」这类没有数据源的分类。 */
  const errors = computed(() => {
    const acc = overview.value.accounts
    const taskStatus = overview.value.tasks.by_status || {}
    const groupStatus = pool.value.by_status || {}
    const groupBad = (groupStatus.unreachable || 0) + (groupStatus.failed || 0)
    return [
      { key: 'account', label: '账号异常', count: acc.error, view: 'health', hint: '登录失效 / 被封禁 / 无发言权限，去「健康检查」重新检测' },
      { key: 'proxy', label: '代理异常', count: acc.no_proxy, view: 'proxies', hint: '没有绑定代理的账号，开启强制一账号一代理时会被拒绝执行' },
      { key: 'group', label: '群组异常', count: groupBad, view: 'console', hint: '不可访问 / 失败的群组，可在「群组列表」筛选出来清理' },
      { key: 'task', label: '任务异常', count: taskStatus.failed || 0, view: 'console', hint: '发送失败的任务，看下面「最近任务」与运行日志的详情' },
    ]
  })

  const errorTotal = computed(() => errors.value.reduce((sum, item) => sum + item.count, 0))

  /** §20「查看」：能筛的就地筛选，不能筛的就跳到对应页面。 */
  function openErrors(): void {
    errorDrawerOpen.value = true
  }

  function focusError(key: string): void {
    errorDrawerOpen.value = false
    if (key === 'group') {
      poolStatusFilter.value = 'unreachable'
      poolFilter.value = ''
      ui.go('console')
      return
    }
    const target = errors.value.find((item) => item.key === key)
    if (target) ui.go(target.view)
  }

  /**
   * includePool=false 用于 5 秒轮询：群组池可能几千行，计数从 overview 里取就够了。
   */
  async function refresh(silent = true, includePool = true): Promise<void> {
    if (loading.overview) return
    loading.overview = true
    try {
      const overviewPromise = request<DashboardOverview>('/api/dashboard/overview')
      const poolPromise = includePool ? request<GroupPool>('/api/groups/pool') : null
      const [nextOverview, nextPool] = await Promise.all([overviewPromise, poolPromise])
      overview.value = nextOverview
      if (nextPool) {
        pool.value = nextPool
      } else {
        pool.value.total = nextOverview.groups?.total ?? pool.value.total
        pool.value.by_status = nextOverview.groups?.by_status ?? pool.value.by_status
      }
    } catch (error) {
      if (!silent) ElMessage.error(`读取控制台数据失败: ${(error as ApiError).message}`)
    } finally {
      loading.overview = false
    }
  }

  async function importGroups(file: File | null): Promise<void> {
    const text = importText.value.trim()
    if (!text && !file) {
      ElMessage.warning('请粘贴群组链接，或选择 TXT / CSV 文件')
      return
    }
    loading.importing = true
    try {
      let data: ImportResult
      if (file) {
        const form = new FormData()
        form.append('file', file)
        data = await request<ImportResult>('/api/groups/import', { method: 'POST', body: form, timeoutMs: 120_000 })
      } else {
        data = await request<ImportResult>('/api/groups/import', { method: 'POST', body: { text } })
      }
      importResult.value = data
      importText.value = ''
      await refresh(true, true)
      ElMessage({
        type: data.imported ? 'success' : 'warning',
        message: `群组导入完成：成功 ${data.imported}，重复 ${data.duplicate}，无效 ${data.invalid}`,
      })
    } catch (error) {
      ElMessage.error(`导入群组失败: ${(error as ApiError).message}`)
    } finally {
      loading.importing = false
    }
  }

  async function removeGroups(payload: { ids?: number[]; scope?: string }, confirmText?: string): Promise<void> {
    if (confirmText) {
      try {
        await ElMessageBox.confirm(confirmText, '确认删除', { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' })
      } catch {
        return
      }
    }
    try {
      const data = await request<{ deleted: number }>('/api/groups/pool/delete', { method: 'POST', body: payload })
      ElMessage.success(`已删除 ${data.deleted} 个目标群组`)
      selectedGroupIds.value = []
      await refresh(true, true)
    } catch (error) {
      ElMessage.error(`删除目标群组失败: ${(error as ApiError).message}`)
    }
  }

  /** §10「重新检测」：逐个群向 Telegram 确认可达性，群多时很慢，超时给到 5 分钟。 */
  async function recheckGroups(ids?: number[]): Promise<void> {
    loading.rechecking = true
    try {
      const data = await request<RecheckResult>('/api/groups/pool/check', {
        method: 'POST',
        body: ids?.length ? { ids } : {},
        timeoutMs: 300_000,
      })
      ElMessage({
        type: data.unreachable ? 'warning' : 'success',
        message: `检测完成：可访问 ${data.available}，不可访问 ${data.unreachable}`,
      })
      selectedGroupIds.value = []
      await refresh(true, true)
    } catch (error) {
      ElMessage.error(`重新检测失败: ${(error as ApiError).message}`)
    } finally {
      loading.rechecking = false
    }
  }

  return {
    overview, pool, importResult, importText, poolFilter, poolStatusFilter, onlyUnjoined,
    selectedGroupIds, errorDrawerOpen, loading,
    kpis, filteredGroups, unjoinedCount, errors, errorTotal,
    refresh, importGroups, removeGroups, recheckGroups, openErrors, focusError,
  }
})
