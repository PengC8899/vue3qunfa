import { ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { JoinLogRow, LogRow } from '@/api/types'

export const useLogsStore = defineStore('logs', () => {
  const rows = ref<LogRow[]>([])
  /** 加群日志单独一份：和群发日志分开展示，互不干扰。 */
  const joinRows = ref<JoinLogRow[]>([])
  const loading = ref(false)
  /** §19：默认「普通」模式，不显示 TASK_ID / ROUND 等调试字段。 */
  const detailed = ref(false)

  async function refresh(silent = true): Promise<void> {
    if (loading.value) return
    loading.value = true
    try {
      const [sendData, joinData] = await Promise.all([
        request<LogRow[]>('/api/logs?limit=50'),
        request<JoinLogRow[]>('/api/join-logs?limit=50'),
      ])
      rows.value = sendData
      joinRows.value = joinData
    } catch (error) {
      if (!silent) ElMessage.error(`读取日志失败: ${(error as ApiError).message}`)
    } finally {
      loading.value = false
    }
  }

  async function clear(): Promise<void> {
    try {
      await request('/api/logs/clear', { method: 'POST' })
      ElMessage.success('日志已清空')
      await refresh()
    } catch (error) {
      ElMessage.error(`清空日志失败: ${(error as ApiError).message}`)
    }
  }

  return { rows, joinRows, loading, detailed, refresh, clear }
})
