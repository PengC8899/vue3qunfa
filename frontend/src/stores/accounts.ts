import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { ElMessage } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { AccountRow, ProfileRow, WarmupProgress } from '@/api/types'

export interface UploadSummary {
  ok: boolean
  total: number
  uploaded: number
  duplicate: number
  invalid: number
  no_proxy: string[]
  no_proxy_count: number
  proxy_assigned: number
  proxy_sticky: number
  validated_accounts: string[]
  errors?: string[] | null
}

export interface HealthRow {
  account: string
  status: string
  detail?: string
  valid?: boolean
  can_send_in_groups?: boolean
  sendable_groups?: number
}

export const useAccountsStore = defineStore('accounts', () => {
  const rows = ref<AccountRow[]>([])
  const loading = ref(false)
  const selected = ref(localStorage.getItem('selectedAccount') || '')
  const warmupEnabled = ref(true)
  const warmupProgress = ref<WarmupProgress | null>(null)

  const stats = computed(() => {
    const total = rows.value.length
    const authorized = rows.value.filter((r) => r.authorized).length
    const withProxy = rows.value.filter((r) => r.has_proxy).length
    return { total, authorized, unauthorized: total - authorized, withProxy, withoutProxy: total - withProxy }
  })

  const selectedRow = computed(() => rows.value.find((r) => r.account === selected.value) || null)

  function select(account: string): void {
    selected.value = account
    if (account) localStorage.setItem('selectedAccount', account)
    else localStorage.removeItem('selectedAccount')
  }

  async function refresh(silent = true): Promise<void> {
    if (loading.value) return
    loading.value = true
    try {
      rows.value = await request<AccountRow[]>('/api/accounts/status')
      if (!selected.value || !rows.value.some((r) => r.account === selected.value)) {
        select(rows.value[0]?.account || '')
      }
    } catch (error) {
      if (!silent) ElMessage.error(`读取账号失败: ${(error as ApiError).message}`)
    } finally {
      loading.value = false
    }
  }

  /** 上传是纯本地操作：代理缺失只标记 NO_PROXY，不会阻止账号入库 */
  async function upload(files: File[], proxySourceText: string): Promise<UploadSummary | null> {
    if (!files.length) {
      ElMessage.warning('请先选择 .session 或 .zip 文件')
      return null
    }
    const form = new FormData()
    files.forEach((file) => form.append('files', file))
    if (proxySourceText.trim()) form.append('proxy_source_text', proxySourceText.trim())
    try {
      const data = await request<UploadSummary>('/api/accounts/upload-sessions', {
        method: 'POST',
        body: form,
        timeoutMs: 120_000,
      })
      await refresh()
      return data
    } catch (error) {
      ElMessage.error(`上传失败: ${(error as ApiError).message}`)
      return null
    }
  }

  async function remove(accounts: string[]): Promise<void> {
    if (!accounts.length) return
    try {
      await request('/api/accounts/bulk-delete', { method: 'POST', body: { accounts } })
      ElMessage.success(`已删除 ${accounts.length} 个账号`)
      if (accounts.includes(selected.value)) select('')
      await refresh()
    } catch (error) {
      ElMessage.error(`删除账号失败: ${(error as ApiError).message}`)
    }
  }

  /** 清空全部 session 文件：账号、它们名下的任务、代理绑定、检测记录一起清掉。不可逆 */
  async function purgeAll(): Promise<{ accounts: number; files_removed: number; tasks_removed: number } | null> {
    try {
      const data = await request<{ ok: boolean; accounts: number; files_removed: number; tasks_removed: number }>(
        '/api/accounts/purge-sessions',
        { method: 'POST', timeoutMs: 180_000 },
      )
      select('')
      await refresh()
      return data
    } catch (error) {
      ElMessage.error(`清空 Session 文件失败: ${(error as ApiError).message}`)
      return null
    }
  }

  async function sendLoginCode(account: string, phone: string, forceSms = false): Promise<boolean> {
    try {
      await request('/api/login/send-code', {
        method: 'POST',
        body: { account, phone, force_sms: forceSms },
        timeoutMs: 60_000,
      })
      ElMessage.success('验证码已发送')
      return true
    } catch (error) {
      ElMessage.error(`发送验证码失败: ${(error as ApiError).message}`)
      return false
    }
  }

  async function submitLoginCode(account: string, phone: string, code: string, password: string): Promise<boolean> {
    try {
      await request('/api/login/submit-code', {
        method: 'POST',
        body: { account, phone, code, password },
        timeoutMs: 60_000,
      })
      ElMessage.success('登录成功')
      await refresh()
      return true
    } catch (error) {
      ElMessage.error(`登录失败: ${(error as ApiError).message}`)
      return false
    }
  }

  async function readProfile(account: string): Promise<ProfileRow | null> {
    try {
      return await request<ProfileRow>(`/api/accounts/profile?account=${encodeURIComponent(account)}`)
    } catch (error) {
      ElMessage.error(`读取资料失败: ${(error as ApiError).message}`)
      return null
    }
  }

  /** 后端这个接口是按「所有已授权账号」批量改的，不是单账号 */
  async function updateAllProfiles(nickname: string, about: string): Promise<void> {
    try {
      const data = await request<{ success_count?: number; failed_count?: number }>('/api/accounts/profile', {
        method: 'POST',
        body: { nickname, about },
        timeoutMs: 180_000,
      })
      ElMessage.success(`资料已更新：成功 ${data.success_count || 0}，失败 ${data.failed_count || 0}`)
    } catch (error) {
      ElMessage.error(`更新资料失败: ${(error as ApiError).message}`)
    }
  }

  async function checkOne(account: string, includeGroupSendCheck = true): Promise<HealthRow> {
    try {
      const data = await request<HealthRow & { detail?: string }>('/api/accounts/check-single', {
        method: 'POST',
        body: { account, include_group_send_check: includeGroupSendCheck },
        timeoutMs: 60_000,
      })
      return { ...data, account }
    } catch (error) {
      return { account, status: 'error', detail: (error as ApiError).message, valid: false }
    }
  }

  /** 预热总览：只为了拿「当前批次跑到哪了」，梯队本身跟着账号列表一起回来 */
  async function loadWarmup(): Promise<void> {
    try {
      const data = await request<{ enabled: boolean; progress: WarmupProgress }>('/api/accounts/warmup')
      warmupEnabled.value = data.enabled
      warmupProgress.value = data.progress
    } catch {
      // 总览读不到不该把账号列表也拖挂，静默即可
    }
  }

  /** 不传 accounts 就是预热所有「还没毕业」的账号（已豁免的老号不会被拉进来） */
  async function startWarmup(accounts: string[] = []): Promise<boolean> {
    try {
      const data = await request<{ ok: boolean; message?: string }>('/api/accounts/warmup/start', {
        method: 'POST',
        body: { accounts },
        timeoutMs: 60_000,
      })
      ElMessage.success(data.message || '已开始预热')
      await Promise.all([refresh(), loadWarmup()])
      return true
    } catch (error) {
      ElMessage.error(`开始预热失败: ${(error as ApiError).message}`)
      return false
    }
  }

  async function stopWarmup(): Promise<void> {
    try {
      const data = await request<{ ok: boolean; message?: string }>('/api/accounts/warmup/stop', {
        method: 'POST',
        body: {},
      })
      ElMessage.success(data.message || '已请求停止')
      await loadWarmup()
    } catch (error) {
      ElMessage.error(`停止预热失败: ${(error as ApiError).message}`)
    }
  }

  return {
    rows, loading, selected, stats, selectedRow, select,
    warmupEnabled, warmupProgress, loadWarmup, startWarmup, stopWarmup,
    refresh, upload, remove, purgeAll, sendLoginCode, submitLoginCode,
    readProfile, updateAllProfiles, checkOne,
  }
})
