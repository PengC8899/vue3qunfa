import { reactive, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { SendPreset } from '@/api/types'
import { useSendStore } from '@/stores/send'

/**
 * 保存下来的发送任务：保存一次拿一个编号，之后选编号点「开始全部链路」就能整条跑。
 * 编号就是后端表的主键，不会因为删除而重用。
 */
export const usePresetsStore = defineStore('presets', () => {
  const send = useSendStore()
  const rows = ref<SendPreset[]>([])
  const selectedNo = ref<number | null>(null)
  const loading = reactive({ list: false, saving: false })
  // 记住上次选的编号：刷新页面后不用重新选，否则「边加群边群发」按钮会一直是灰的
  const STORAGE_KEY = 'live_selected_preset'

  function remember(no: number | null): void {
    try {
      if (no) localStorage.setItem(STORAGE_KEY, String(no))
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // 隐私模式下 localStorage 不可用，忽略即可
    }
  }

  async function refresh(): Promise<void> {
    loading.list = true
    try {
      const data = await request<{ ok: boolean; items: SendPreset[] }>('/api/send-presets')
      rows.value = data.items || []
      if (selectedNo.value && !rows.value.some((row) => row.no === selectedNo.value)) {
        selectedNo.value = null
      }
      if (!selectedNo.value) {
        let saved = 0
        try {
          saved = Number(localStorage.getItem(STORAGE_KEY) || 0)
        } catch {
          saved = 0
        }
        if (saved && rows.value.some((row) => row.no === saved)) select(saved)
      }
    } catch (error) {
      ElMessage.error(`读取发送任务失败: ${(error as ApiError).message}`)
    } finally {
      loading.list = false
    }
  }

  function label(preset: SendPreset): string {
    const preview = preset.message.replace(/\s+/g, ' ').slice(0, 24)
    return `#${preset.no}　${preset.name || preview || '（空正文）'}`
  }
  /** 选中编号就把这个任务的参数填回发送表单，用户点开始前能看清要发什么。 */
  function select(no: number | null): void {
    selectedNo.value = no
    remember(no)
    const preset = rows.value.find((row) => row.no === no)
    if (!preset) return
    send.config.message = preset.message
    send.config.parse_mode = preset.parse_mode
    send.config.disable_web_page_preview = preset.disable_web_page_preview
    send.config.delay_ms = preset.delay_ms
    send.config.rounds = preset.rounds
    send.config.round_interval_s = preset.round_interval_s
    send.config.group_msg_cooldown_s = preset.group_msg_cooldown_s
    // 任务里 rounds=1 就是「发一轮就结束」，多轮才回到循环模式
    send.config.mode = preset.rounds > 1 ? 'loop' : 'single'
  }

  function currentFields(): Record<string, unknown> {
    return {
      message: send.config.message,
      parse_mode: send.config.parse_mode,
      disable_web_page_preview: send.config.disable_web_page_preview,
      delay_ms: send.config.delay_ms,
      rounds: send.taskRounds,
      round_interval_s: send.taskRoundIntervalS,
      group_msg_cooldown_s: send.config.group_msg_cooldown_s,
    }
  }

  /** asNew=true 新建一个编号；false 覆盖当前选中的编号。 */
  async function save(asNew: boolean): Promise<SendPreset | null> {
    if (!send.config.message.trim()) {
      ElMessage.warning('先填写要发送的消息内容，再保存为任务')
      return null
    }
    if (!asNew && !selectedNo.value) {
      ElMessage.warning('先选择一个编号，或者用「保存为新任务」')
      return null
    }
    loading.saving = true
    try {
      const body = { ...currentFields(), ...(asNew ? {} : { no: selectedNo.value }) }
      const data = await request<{ ok: boolean; preset: SendPreset }>('/api/send-presets/save', {
        method: 'POST',
        body,
      })
      await refresh()
      selectedNo.value = data.preset.no
      ElMessage.success(asNew ? `已保存为 #${data.preset.no} 号任务` : `已更新 #${data.preset.no} 号任务`)
      return data.preset
    } catch (error) {
      ElMessage.error(`保存发送任务失败: ${(error as ApiError).message}`)
      return null
    } finally {
      loading.saving = false
    }
  }

  async function remove(no: number): Promise<void> {
    try {
      await request('/api/send-presets/delete', { method: 'POST', body: { no } })
      if (selectedNo.value === no) selectedNo.value = null
      await refresh()
      ElMessage.success(`已删除 #${no} 号任务`)
    } catch (error) {
      ElMessage.error(`删除发送任务失败: ${(error as ApiError).message}`)
    }
  }

  return { rows, selectedNo, loading, refresh, select, save, remove, label, currentFields }
})
