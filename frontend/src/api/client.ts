/**
 * 统一 HTTP 客户端：管理员令牌、超时、错误中文化都在这里收口。
 */
import { translateText } from '@/utils/format'

const TOKEN_KEY = 'adminToken'
const DEFAULT_TIMEOUT_MS = 30_000

export class ApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(value: string): void {
  const next = value.trim()
  if (next) localStorage.setItem(TOKEN_KEY, next)
  else localStorage.removeItem(TOKEN_KEY)
}

export function authHeaders(json = false): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['X-Admin-Token'] = token
  if (json) headers['Content-Type'] = 'application/json'
  return headers
}

export interface RequestOptions {
  method?: string
  /** 传对象自动 JSON 序列化；传 FormData 原样发送 */
  body?: unknown
  timeoutMs?: number
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS } = options
  const isForm = typeof FormData !== 'undefined' && body instanceof FormData
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const res = await fetch(url, {
      method,
      headers: isForm ? authHeaders() : authHeaders(body !== undefined),
      body: body === undefined ? undefined : isForm ? (body as FormData) : JSON.stringify(body),
      signal: controller.signal,
    })
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>
    if (!res.ok) {
      const details = Array.isArray(data.errors) ? (data.errors as string[]).join('\n') : ''
      const raw = (data.message || data.detail || data.error || `HTTP ${res.status}`) as string
      throw new ApiError(translateText(raw) + (details ? `\n${details}` : ''), res.status)
    }
    return data as T
  } catch (error) {
    if (error instanceof ApiError) throw error
    if ((error as Error).name === 'AbortError') {
      throw new ApiError('请求超时，请刷新列表确认结果后再重试', 0)
    }
    throw new ApiError((error as Error).message || '网络请求失败', 0)
  } finally {
    clearTimeout(timer)
  }
}
