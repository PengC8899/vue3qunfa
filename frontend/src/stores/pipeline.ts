import { computed, reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { PipelineRun } from '@/api/types'

/** 边加群边群发：后台持续补加群，发送任务每轮自动接住新加入的群。 */
export const usePipelineStore = defineStore('pipeline', () => {
  const run = ref<PipelineRun | null>(null)
  const loading = reactive({ starting: false, stopping: false, loaded: false })

  const running = computed(() => Boolean(run.value?.running))
  const activeStep = computed(() => {
    const steps = run.value?.steps || []
    const index = steps.findIndex((step) => step.status === 'running')
    if (index >= 0) return index
    return steps.every((step) => step.status === 'done') ? steps.length : 0
  })

  function stepStatus(status: string): 'wait' | 'process' | 'finish' | 'error' {
    if (status === 'running') return 'process'
    if (status === 'done') return 'finish'
    if (status === 'failed') return 'error'
    return 'wait'
  }

  async function startLive(presetNo: number | null): Promise<void> {
    if (!presetNo) {
      ElMessage.warning('先选择要发送的几号任务')
      return
    }
    loading.starting = true
    try {
      const data = await request<{ ok: boolean; pipeline: PipelineRun }>('/api/pipeline/start-live', {
        method: 'POST',
        body: { preset_no: presetNo },
        timeoutMs: 60_000,
      })
      run.value = data.pipeline
      loading.loaded = true
      ElMessage.success(`#${presetNo} 号任务已启动「边加群边群发」，进度自动刷新`)
    } catch (error) {
      ElMessage.error(`启动失败: ${(error as ApiError).message}`)
    } finally {
      loading.starting = false
    }
  }

  async function stop(): Promise<void> {
    loading.stopping = true
    try {
      await request('/api/pipeline/stop', { method: 'POST' })
      ElMessage.warning('已请求停止，当前这一步做完就收工')
    } catch (error) {
      ElMessage.error(`停止失败: ${(error as ApiError).message}`)
    } finally {
      loading.stopping = false
    }
  }

  async function refresh(silent = true): Promise<void> {
    if (loading.loaded && !running.value) return
    try {
      const data = await request<{ ok: boolean; pipeline: PipelineRun | null }>('/api/pipeline/status')
      run.value = data.pipeline
      loading.loaded = true
    } catch (error) {
      if (!silent) ElMessage.error(`读取链路进度失败: ${(error as ApiError).message}`)
    }
  }

  return { run, loading, running, activeStep, stepStatus, startLive, stop, refresh }
})
