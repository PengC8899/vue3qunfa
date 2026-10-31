import { computed, reactive } from 'vue'
import { defineStore } from 'pinia'

/** 任务模式：发一轮就结束，还是同一批群反复发。 */
export type SendMode = 'single' | 'loop'

export interface SendConfig {
  message: string
  parse_mode: 'plain' | 'markdown' | 'html'
  /** 任务模式是给人看的，落到任务里会变成 rounds / round_interval_s 两个具体数字 */
  mode: SendMode
  delay_ms: number
  rounds: number
  round_interval_s: number
  /** 同群同文案的最小间隔（秒）：同一批内容不许两个账号在同一群里短时间各发一遍 */
  group_msg_cooldown_s: number
  disable_web_page_preview: boolean
}

/** 控制台的「开始全部任务」和手动群发页共用同一份发送参数。 */
export const useSendStore = defineStore('send', () => {
  const config = reactive<SendConfig>({
    message: '',
    parse_mode: 'plain',
    mode: 'loop',
    // 默认节奏：单群每条间隔 1 分钟、100 轮、每轮间隔 10 分钟
    delay_ms: 60000,
    rounds: 100,
    round_interval_s: 600,
    group_msg_cooldown_s: 180,
    disable_web_page_preview: true,
  })

  /** 单轮模式就是 1 轮、0 间隔；这样任务里永远只有一种解释，不会偷偷循环。 */
  const taskRounds = computed(() => (config.mode === 'single' ? 1 : Math.max(1, config.rounds)))
  const taskRoundIntervalS = computed(() => (config.mode === 'single' ? 0 : Math.max(0, config.round_interval_s)))

  return { config, taskRounds, taskRoundIntervalS }
})
