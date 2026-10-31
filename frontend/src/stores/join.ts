import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { JoinItem, JoinJob, JoinPreview } from '@/api/types'

const STATUS_LABEL: Record<JoinItem['status'], string> = {
  pending: '等待中',
  joined: '已加入',
  skipped: '已跳过',
  requested: '待审批',
  failed: '失败',
}

export const useJoinStore = defineStore('join', () => {
  const preview = ref<JoinPreview | null>(null)
  const job = ref<JoinJob | null>(null)
  const delayMs = ref(4000)
  const workers = ref(12)
  const filter = ref<'all' | JoinItem['status']>('all')
  const loading = reactive({ preview: false, starting: false, stopping: false })
  /** 进度是进程内存态，只有真的跑过或刚加载过才去问，避免空轮询 */
  const loaded = ref(false)

  const running = computed(() => Boolean(job.value?.running))
  const percent = computed(() => {
    const current = job.value
    if (!current?.total) return 0
    return Math.min(100, Math.round((current.done / current.total) * 100))
  })
  /** 速度 + 预计剩余：进度条不动时用户至少知道还要等多久，不会以为卡死 */
  const speed = computed(() => {
    const current = job.value
    if (!current?.started_at || !current.done) return 0
    const elapsedMin = (Date.now() - Date.parse(current.started_at)) / 60000
    if (elapsedMin <= 0) return 0
    return current.done / elapsedMin
  })
  const etaText = computed(() => {
    const current = job.value
    if (!current || !current.running) return ''
    if (!speed.value) return '正在估算…'
    const left = (current.total - current.done) / speed.value
    if (left < 1) return '不到 1 分钟'
    if (left < 60) return `约 ${Math.round(left)} 分钟`
    return `约 ${(left / 60).toFixed(1)} 小时`
  })
  const currentText = computed(() => {
    const entries = Object.entries(job.value?.current || {})
    if (!entries.length) return '准备中'
    return entries.slice(0, 8).map(([account, group]) => `${account} → ${group}`).join('；')
      + (entries.length > 8 ? ` 等 ${entries.length} 个账号` : '')
  })
  const items = computed<JoinItem[]>(() => {
    const rows = job.value?.items || []
    if (filter.value === 'all') return rows
    return rows.filter((row) => row.status === filter.value)
  })

  function statusLabel(status: JoinItem['status']): string {
    return STATUS_LABEL[status] || status
  }

  async function loadPreview(ids?: number[]): Promise<void> {
    loading.preview = true
    try {
      preview.value = await request<JoinPreview>('/api/groups/pool/join', {
        method: 'POST',
        body: { mode: 'preview', ids: ids?.length ? ids : undefined },
        timeoutMs: 60_000,
      })
      const blocked = Object.entries(preview.value.accounts_blocked || {})
        .map(([reason, count]) => `${reason} ${count}`)
        .join('、')
      const skipped = preview.value.groups_covered_skipped
      ElMessage({
        type: preview.value.accounts_used ? 'success' : 'warning',
        message: preview.value.accounts_used
          ? `${preview.value.groups} 个群 → ${preview.value.accounts_used} 个账号`
            + `${skipped ? `（另有 ${skipped} 个群已有账号，跳过不重复加）` : ''}`
            + `${blocked ? `（未参与：${blocked}）` : ''}`
          : '没有可用账号，先到「健康检查」检测账号',
      })
    } catch (error) {
      ElMessage.error(`预览分配失败: ${(error as ApiError).message}`)
    } finally {
      loading.preview = false
    }
  }

  async function start(ids?: number[]): Promise<void> {
    loading.starting = true
    try {
      const data = await request<JoinPreview>('/api/groups/pool/join', {
        method: 'POST',
        body: { mode: 'start', delay_ms: delayMs.value, workers: workers.value, ids: ids?.length ? ids : undefined },
        timeoutMs: 120_000,
      })
      preview.value = data
      job.value = data.job || null
      loaded.value = true
      const skipped = data.groups_covered_skipped
      ElMessage.success(
        `已开始加群：${data.groups} 个群分给 ${data.accounts_used} 个账号`
        + `${skipped ? `（${skipped} 个群已有账号，跳过）` : ''}，进度会实时刷新`,
      )
    } catch (error) {
      ElMessage.error(`开始加群失败: ${(error as ApiError).message}`)
    } finally {
      loading.starting = false
    }
  }

  async function stop(): Promise<void> {
    loading.stopping = true
    try {
      await request('/api/groups/pool/join/stop', { method: 'POST' })
      ElMessage.warning('已请求停止，当前这一条加完就收工')
    } catch (error) {
      ElMessage.error(`停止失败: ${(error as ApiError).message}`)
    } finally {
      loading.stopping = false
    }
  }

  async function refresh(silent = true, force = false): Promise<void> {
    if (!force && loaded.value && !running.value) return
    try {
      const data = await request<{ ok: boolean; job: JoinJob | null }>('/api/groups/pool/join')
      job.value = data.job
      // 输入框跟正在跑的任务对齐，别显示一个和实际不一样的并发数
      if (data.job?.workers) workers.value = data.job.workers
      loaded.value = true
    } catch (error) {
      if (!silent) ElMessage.error(`读取加群进度失败: ${(error as ApiError).message}`)
    }
  }

  return {
    preview, job, delayMs, workers, filter, loading, running, percent, speed, etaText, currentText, items,
    statusLabel, loadPreview, start, stop, refresh,
  }
})
