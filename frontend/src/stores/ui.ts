import { ref } from 'vue'
import { defineStore } from 'pinia'

export interface NavItem {
  key: string
  label: string
  desc: string
  icon: string
}

/** 菜单只保留控制台 + 维护页 + 运行日志，其余能力都收进控制台。 */
export const NAV_ITEMS: NavItem[] = [
  { key: 'console', label: '全局控制台', desc: '上传、调度、进度', icon: 'Odometer' },
  { key: 'runlogs', label: '运行日志', desc: '本次任务群发 / 加群', icon: 'Document' },
  { key: 'accounts', label: '账号管理', desc: '登录、Session、资料', icon: 'User' },
  { key: 'proxies', label: '代理 IP', desc: '分配、测试、开关', icon: 'Connection' },
  { key: 'send', label: '手动群发', desc: '按群组直接发', icon: 'Promotion' },
  { key: 'health', label: '健康检查', desc: '检测并清理', icon: 'FirstAidKit' },
  { key: 'groups', label: '已加入群组', desc: '账号自己的群', icon: 'ChatDotRound' },
  { key: 'system', label: '系统设置', desc: '令牌 / 重置', icon: 'Setting' },
]

export const useUiStore = defineStore('ui', () => {
  const activeView = ref<string>('console')
  const sidebarOpen = ref(false)

  function go(key: string): void {
    activeView.value = key
    sidebarOpen.value = false
  }

  return { activeView, sidebarOpen, go }
})
