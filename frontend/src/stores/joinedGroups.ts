import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { JoinedGroup } from '@/api/types'

/** 只读展示「某账号已经加入了哪些群」，与目标群组池是两回事。 */
export const useJoinedGroupsStore = defineStore('joinedGroups', () => {
  const rows = ref<JoinedGroup[]>([])
  const loading = ref(false)
  const error = ref('')
  const onlyGroups = ref(localStorage.getItem('includeChannels') !== '1')

  const summary = computed(() => {
    if (loading.value) return '群组加载中…'
    if (error.value) return error.value
    if (!rows.value.length) return '当前账号还没有可用群组数据'
    return `已加载 ${rows.value.length} 个群组`
  })

  async function refresh(account: string, force = false): Promise<void> {
    if (!account) {
      error.value = '请先选择账号'
      return
    }
    loading.value = true
    error.value = ''
    try {
      // 后端 /api/groups 返回的是**顶层数组**。这里以前按 {items|groups} 包裹解析，
      // 数组上这两个属性都是 undefined，rows 恒为空，页面永远显示「还没有可用群组数据」，
      // 而且 HTTP 200、控制台不报错，从表面完全看不出问题。
      const data = await request<JoinedGroup[] | { items?: JoinedGroup[]; groups?: JoinedGroup[] }>(
        `/api/groups?only_groups=${onlyGroups.value ? 'true' : 'false'}&account=${encodeURIComponent(account)}&refresh=${force ? 'true' : 'false'}`,
        { timeoutMs: 120_000 },
      )
      rows.value = Array.isArray(data) ? data : (data.items || data.groups || [])
    } catch (err) {
      error.value = `群组加载失败: ${(err as ApiError).message}`
      rows.value = []
    } finally {
      loading.value = false
    }
  }

  async function clearCache(account: string): Promise<void> {
    try {
      await request(`/api/groups/cache/clear?account=${encodeURIComponent(account)}`, { method: 'GET' })
      ElMessage.success('群组缓存已清理')
      await refresh(account, true)
    } catch (err) {
      ElMessage.error(`清理缓存失败: ${(err as ApiError).message}`)
    }
  }

  return { rows, loading, error, onlyGroups, summary, refresh, clearCache }
})
