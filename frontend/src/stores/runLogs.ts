import { reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { request } from '@/api/client'
import type { LogRow, RunLogPage } from '@/api/types'

/**
 * 「运行日志」页的数据源（左侧菜单项）。
 *
 * 日志全部落库、一条不删；这里只按「本次执行」的时间区间取（点开始 → 停止），
 * 所以每启动一次任务都从 0 开始统计，之前几次的日志仍然完整保存在库里。
 */
export const useRunLogsStore = defineStore('runLogs', () => {
  const kind = ref<'send' | 'join'>('send')
  const send = reactive({ items: [] as LogRow[], total: 0 })
  const join = reactive({ items: [] as LogRow[], total: 0 })
  const meta = reactive({
    started_at: null as string | null,
    finished_at: null as string | null,
    frozen: false,
    loading: false,
  })

  async function refresh(): Promise<void> {
    if (meta.loading) return
    meta.loading = true
    try {
      const [s, j] = await Promise.all([
        request<RunLogPage>('/api/pipeline/logs?kind=send&limit=300'),
        request<RunLogPage>('/api/pipeline/logs?kind=join&limit=300'),
      ])
      send.items = s.items || []
      send.total = s.total || 0
      join.items = j.items || []
      join.total = j.total || 0
      meta.started_at = s.started_at ?? null
      meta.finished_at = s.finished_at ?? null
      meta.frozen = Boolean(s.frozen)
    } catch {
      // 侧边栏是辅助信息，读失败不打扰用户
    } finally {
      meta.loading = false
    }
  }

  return { kind, send, join, meta, refresh }
})
