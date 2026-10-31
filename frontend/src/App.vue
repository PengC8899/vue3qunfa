<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, watch } from 'vue'
import { NAV_ITEMS, useUiStore } from '@/stores/ui'
import { useAuthStore } from '@/stores/auth'
import { useConsoleStore } from '@/stores/console'
import { useJoinStore } from '@/stores/join'
import { usePipelineStore } from '@/stores/pipeline'
import { useTasksStore } from '@/stores/tasks'
import { useLogsStore } from '@/stores/logs'
import { useRunLogsStore } from '@/stores/runLogs'

import ConsoleView from '@/views/ConsoleView.vue'
import RunLogsView from '@/views/RunLogsView.vue'
import AccountsView from '@/views/AccountsView.vue'
import ProxyView from '@/views/ProxyView.vue'
import SendView from '@/views/SendView.vue'
import HealthView from '@/views/HealthView.vue'
import JoinedGroupsView from '@/views/JoinedGroupsView.vue'
import SystemView from '@/views/SystemView.vue'

const ui = useUiStore()
const auth = useAuthStore()
const consoleStore = useConsoleStore()
const join = useJoinStore()
const pipeline = usePipelineStore()
const tasks = useTasksStore()
const logs = useLogsStore()
const runLogs = useRunLogsStore()

const VIEWS = {
  console: ConsoleView,
  runlogs: RunLogsView,
  accounts: AccountsView,
  proxies: ProxyView,
  send: SendView,
  health: HealthView,
  groups: JoinedGroupsView,
  system: SystemView,
} as const

const currentView = computed(() => VIEWS[ui.activeView as keyof typeof VIEWS] ?? ConsoleView)

let timer: ReturnType<typeof setTimeout> | null = null
let inFlight = false

/** 一个统一的轮询循环：只有停在控制台/运行日志时才轮询，页面不可见时跳过。 */
function schedule(delayMs = 5000): void {
  if (timer) clearTimeout(timer)
  timer = setTimeout(async () => {
    const view = ui.activeView
    const autoRefresh = view === 'console' || view === 'runlogs'
    if (auth.saved && autoRefresh && document.visibilityState === 'visible' && !inFlight) {
      inFlight = true
      try {
        // 并行拉取：串行会让 5 个接口的 RTT 叠加，弱网下一轮轮询超过 5 秒
        await Promise.all([
          tasks.refresh(),
          logs.refresh(),
          consoleStore.refresh(true, false),
          join.refresh(),
          pipeline.refresh(),
          ...(view === 'runlogs' ? [runLogs.refresh()] : []),
        ])
      } finally {
        inFlight = false
      }
    }
    schedule(5000)
  }, delayMs)
}

onMounted(async () => {
  if (auth.saved) {
    await Promise.all([tasks.refresh(), logs.refresh(), consoleStore.refresh(true, true)])
  }
  schedule(3000)
})

// 切回控制台时立刻拉一次全量（含群组池），不用等下一次轮询
watch(() => ui.activeView, (view) => {
  if (!auth.saved) return
  if (view === 'console') void consoleStore.refresh(true, true)
  if (view === 'runlogs') void runLogs.refresh()
})

onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
})
</script>

<template>
  <el-container class="layout">
    <el-aside class="layout__aside" width="228px">
      <div class="layout__brand">
        <el-icon :size="22" color="#4f8cff"><Promotion /></el-icon>
        <div>
          <h1>TG 群发控制台</h1>
          <small>{{ auth.statusText }}</small>
        </div>
      </div>
      <el-menu :default-active="ui.activeView" @select="ui.go">
        <el-menu-item v-for="item in NAV_ITEMS" :key="item.key" :index="item.key">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <div class="layout__topbar">
        <el-button :icon="'Menu'" circle @click="ui.sidebarOpen = true" />
        <strong>{{ NAV_ITEMS.find((i) => i.key === ui.activeView)?.label }}</strong>
      </div>

      <el-main class="layout__main">
        <component :is="currentView" />
      </el-main>
    </el-container>
  </el-container>

  <el-drawer v-model="ui.sidebarOpen" direction="ltr" size="248px" :with-header="false">
    <el-menu :default-active="ui.activeView" @select="ui.go">
      <el-menu-item v-for="item in NAV_ITEMS" :key="item.key" :index="item.key">
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </el-menu-item>
    </el-menu>
  </el-drawer>
</template>
