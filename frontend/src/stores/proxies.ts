import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { AccountRow } from '@/api/types'

export interface ProxyProbe {
  proxy: string
  ok: boolean
  exit_ip?: string
  error?: string
  elapsed_ms?: number
}

export interface TopupSummary {
  at: string
  accounts: number
  candidates: number
  assigned_total: number
  reason: 'assigned' | 'all_bound' | 'no_source' | 'no_pool_left'
  errors?: string[] | null
}

interface ProxyOverviewResponse {
  default_proxy_source: string
  enforce_one_to_one: boolean
  items: AccountRow[]
  total: number
  bound: number
  unbound: number
  unbound_accounts: string[]
  topup_last: TopupSummary | null
}

export const useProxyStore = defineStore('proxies', () => {
  const items = ref<AccountRow[]>([])
  const total = ref(0)
  const bound = ref(0)
  const unbound = ref(0)
  const unboundAccounts = ref<string[]>([])
  const sourceText = ref('')
  const enforceOneToOne = ref(false)
  const loading = ref(false)

  const assignText = ref('')
  const assignTarget = ref<'unbound' | 'all'>('unbound')
  const assignedTotal = ref<number | null>(null)
  const assignLeft = ref(0)

  const probes = ref<ProxyProbe[]>([])
  const probeTotal = ref(0)
  const testing = ref(false)
  const filter = ref<'all' | 'bound' | 'unbound'>('all')

  const topupLast = ref<TopupSummary | null>(null)
  const topping = ref(false)

  /** 上次自动补绑的结果，用人话显示在按钮旁边 */
  const topupText = computed(() => {
    const last = topupLast.value
    if (!last) return ''
    const when = last.at ? new Date(last.at).toLocaleTimeString() : ''
    if (last.reason === 'all_bound') return `${when} 检查过：没有缺代理的账号`
    if (last.reason === 'no_source') return `${when} 检查过：还没配默认代理源`
    if (last.reason === 'no_pool_left') return `${when} 检查过：还缺 ${last.candidates} 个账号没代理`
    return `${when} 自动补绑了 ${last.assigned_total} 个账号`
  })

  const visibleItems = computed(() => {
    if (filter.value === 'bound') return items.value.filter((item) => item.has_proxy)
    if (filter.value === 'unbound') return items.value.filter((item) => !item.has_proxy)
    return items.value
  })

  /** 代理池是否够这次分配用：给用户一个提前预警，不要等后端报错 */
  const assignPreview = computed(() => {
    const unique = new Set(
      assignText.value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean),
    )
    const targets = assignTarget.value === 'unbound' ? unbound.value : total.value
    return { proxies: unique.size, targets, enough: unique.size >= targets }
  })

  async function refresh(silent = true): Promise<void> {
    if (loading.value) return
    loading.value = true
    try {
      const data = await request<ProxyOverviewResponse>('/api/proxies/overview')
      items.value = data.items || []
      total.value = data.total || 0
      bound.value = data.bound || 0
      unbound.value = data.unbound || 0
      unboundAccounts.value = data.unbound_accounts || []
      sourceText.value = data.default_proxy_source || ''
      enforceOneToOne.value = Boolean(data.enforce_one_to_one)
      topupLast.value = data.topup_last || null
      if (!assignText.value.trim()) assignText.value = sourceText.value
    } catch (error) {
      if (!silent) ElMessage.error(`读取代理总览失败: ${(error as ApiError).message}`)
    } finally {
      loading.value = false
    }
  }

  async function saveSource(): Promise<void> {
    try {
      const data = await request<{ proxy_source_text: string }>('/api/accounts/proxy/default-source', {
        method: 'POST',
        body: { proxy_source_text: sourceText.value },
      })
      sourceText.value = data.proxy_source_text || ''
      ElMessage.success('默认代理源已保存，上传账号时会自动用它分配')
    } catch (error) {
      ElMessage.error(`保存默认代理源失败: ${(error as ApiError).message}`)
    }
  }

  async function toggleEnforce(): Promise<void> {
    try {
      const data = await request<{ enforce_one_to_one: boolean }>('/api/settings/proxy-policy', {
        method: 'POST',
        body: { enforce_one_to_one: !enforceOneToOne.value },
      })
      enforceOneToOne.value = Boolean(data.enforce_one_to_one)
      ElMessage.success(enforceOneToOne.value ? '已开启「强制一账号一代理」' : '已关闭「强制一账号一代理」')
    } catch (error) {
      ElMessage.error(`切换开关失败: ${(error as ApiError).message}`)
    }
  }

  async function assign(): Promise<void> {
    if (!assignText.value.trim()) {
      ElMessage.warning('请先粘贴代理列表或代理 API 地址')
      return
    }
    const targets = assignTarget.value === 'unbound' ? unboundAccounts.value.slice() : items.value.map((i) => i.account)
    if (!targets.length) {
      ElMessage.warning('当前没有可分配的账号')
      return
    }
    try {
      const data = await request<{ assigned_total?: number; assignment_meta?: { unassigned_accounts?: string[] } }>(
        '/api/accounts/proxy/auto-assign',
        { method: 'POST', body: { accounts: targets, proxy_source_text: assignText.value }, timeoutMs: 120_000 },
      )
      assignedTotal.value = data.assigned_total || 0
      assignLeft.value = data.assignment_meta?.unassigned_accounts?.length || 0
      ElMessage.success(`分配完成：成功 ${assignedTotal.value} 个账号${assignLeft.value ? `，仍有 ${assignLeft.value} 个未分配` : ''}`)
      await refresh()
    } catch (error) {
      ElMessage.error(`分配代理失败: ${(error as ApiError).message}`)
    }
  }

  async function test(): Promise<void> {
    const text = assignText.value.trim() || sourceText.value.trim()
    if (!text) {
      ElMessage.warning('请先粘贴代理 IP 或代理 API 地址')
      return
    }
    testing.value = true
    try {
      const data = await request<{ results?: ProxyProbe[]; pool_total?: number }>('/api/proxies/test', {
        method: 'POST',
        body: { proxy_source_text: text },
        timeoutMs: 300_000,
      })
      probes.value = data.results || []
      probeTotal.value = data.pool_total || probes.value.length
    } catch (error) {
      ElMessage.error(`测试代理失败: ${(error as ApiError).message}`)
    } finally {
      testing.value = false
    }
  }

  async function topup(): Promise<void> {
    topping.value = true
    try {
      const data = await request<TopupSummary>('/api/proxies/topup', { method: 'POST', timeoutMs: 120_000 })
      topupLast.value = data
      if (data.reason === 'no_source') ElMessage.warning('还没保存默认代理源，没有地方可以拉代理')
      else if (data.reason === 'all_bound') ElMessage.success('所有账号都已经有代理了')
      else if (data.reason === 'no_pool_left') ElMessage.warning(`池子里的代理不够：还有 ${data.candidates} 个账号没补上`)
      else ElMessage.success(`已给 ${data.assigned_total} 个账号补上代理`)
      await refresh()
    } catch (error) {
      ElMessage.error(`补绑失败: ${(error as ApiError).message}`)
    } finally {
      topping.value = false
    }
  }

  async function unassign(account: string): Promise<void> {
    try {
      await request('/api/proxies/unassign', { method: 'POST', body: { accounts: [account] } })
      ElMessage.success(`已解绑 ${account} 的代理`)
      await refresh()
    } catch (error) {
      ElMessage.error(`解绑失败: ${(error as ApiError).message}`)
    }
  }

  async function unassignAll(): Promise<void> {
    try {
      const data = await request<{ removed?: number }>('/api/proxies/unassign', { method: 'POST', body: { all: true } })
      ElMessage.success(`已清空全部代理绑定（${data.removed ?? 0} 个）`)
      await refresh()
    } catch (error) {
      ElMessage.error(`清空绑定失败: ${(error as ApiError).message}`)
    }
  }

  return {
    items, total, bound, unbound, unboundAccounts, sourceText, enforceOneToOne, loading,
    assignText, assignTarget, assignedTotal, assignLeft, probes, probeTotal, testing, filter,
    topupLast, topping, topupText,
    visibleItems, assignPreview,
    refresh, saveSource, toggleEnforce, assign, test, unassign, unassignAll, topup,
  }
})
