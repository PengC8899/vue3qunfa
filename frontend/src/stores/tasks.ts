import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { TaskSummaryRow } from '@/api/types'
import { actionLabel } from '@/utils/format'

const ACTIVE_STATUSES = ['running', 'sending', 'processing', 'waiting_next_round', 'scheduled', 'paused']

export const useTasksStore = defineStore('tasks', () => {
  const rows = ref<TaskSummaryRow[]>([])
  const loading = ref(false)

  const running = computed(() => rows.value.filter((r) => ['running', 'sending', 'processing'].includes(r.status)).length)

  const progress = computed(() => {
    let planned = 0
    let done = 0
    let success = 0
    let failed = 0
    let active = 0
    let waiting = 0
    for (const row of rows.value) {
      planned += Number(row.overall_planned || 0)
      done += Number(row.overall_completed || 0)
      success += Number(row.success || 0)
      failed += Number(row.failed || 0)
      if (['running', 'sending', 'processing'].includes(row.status)) active += 1
      else if (['waiting_next_round', 'scheduled', 'paused'].includes(row.status)) waiting += 1
    }
    return {
      planned,
      done,
      success,
      failed,
      running: active,
      waiting,
      remaining: Math.max(0, planned - done),
      percent: planned ? Math.round((done / planned) * 1000) / 10 : 0,
    }
  })

  async function refresh(silent = true): Promise<void> {
    if (loading.value) return
    loading.value = true
    try {
      rows.value = await request<TaskSummaryRow[]>('/api/tasks/summary')
    } catch (error) {
      if (!silent) ElMessage.error(`读取任务失败: ${(error as ApiError).message}`)
    } finally {
      loading.value = false
    }
  }

  async function control(taskId: string, action: 'pause' | 'resume' | 'stop'): Promise<void> {
    try {
      await request('/api/task-control', { method: 'POST', body: { task_id: taskId, action } })
      ElMessage.success(`任务${actionLabel(action)}已提交`)
      await refresh()
    } catch (error) {
      ElMessage.error(`任务操作失败: ${(error as ApiError).message}`)
    }
  }

  /** 全局暂停/恢复/停止：逐任务复用既有 /api/task-control，不新增全局接口。 */
  async function controlAll(action: 'pause' | 'resume' | 'stop'): Promise<void> {
    const targets = rows.value.filter((row) => ACTIVE_STATUSES.includes(row.status))
    if (!targets.length) {
      ElMessage.warning('当前没有可控制的任务')
      return
    }
    try {
      await Promise.all(targets.map((row) =>
        request('/api/task-control', { method: 'POST', body: { task_id: row.task_id, action } })))
      ElMessage.success(`已对 ${targets.length} 个任务提交「${actionLabel(action)}」`)
      await refresh()
    } catch (error) {
      ElMessage.error(`批量${actionLabel(action)}失败: ${(error as ApiError).message}`)
    }
  }

  async function remove(taskId: string): Promise<void> {
    try {
      await request('/api/tasks/delete', { method: 'POST', body: { task_id: taskId } })
      ElMessage.success('任务已删除')
      await refresh()
    } catch (error) {
      ElMessage.error(`删除任务失败: ${(error as ApiError).message}`)
    }
  }

  async function clearAll(): Promise<void> {
    try {
      const data = await request<{ tasks_deleted?: number; events_deleted?: number }>(
        '/api/tasks/clear', { method: 'POST' })
      ElMessage.success(`已清空任务 ${data.tasks_deleted || 0} 条，事件 ${data.events_deleted || 0} 条`)
      await refresh()
    } catch (error) {
      ElMessage.error(`清空任务失败: ${(error as ApiError).message}`)
    }
  }

  return { rows, loading, running, progress, refresh, control, controlAll, remove, clearAll }
})
