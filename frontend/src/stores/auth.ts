import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { getToken, setToken } from '@/api/client'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(getToken())
  const saved = computed(() => Boolean(token.value))
  const statusText = computed(() => (token.value ? '令牌已加载' : '请先保存管理员令牌'))

  function save(value: string): void {
    setToken(value)
    token.value = getToken()
  }

  return { token, saved, statusText, save }
})
