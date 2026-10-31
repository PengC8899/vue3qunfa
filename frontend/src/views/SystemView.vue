<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ApiError, request } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const tokenInput = ref(auth.token)
const resetSessions = ref(false)
const resetting = ref(false)
const deletedSessions = ref<number | null>(null)

const mode = import.meta.env.MODE
const baseUrl = import.meta.env.BASE_URL

function saveToken(): void {
  auth.save(tokenInput.value)
  ElMessage.success('管理员令牌已保存')
}

async function resetSystem(): Promise<void> {
  const sessions = resetSessions.value
  try {
    await ElMessageBox.confirm(
      `确定重置系统？这会清空任务记录与统计数据${sessions
        ? '，并同时删除所有 Session 文件（删除后所有账号都需要重新导入）'
        : '，但保留所有 Session 文件'}。操作不可撤销。`,
      '确认重置系统',
      { type: 'warning', confirmButtonText: '确认重置', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  resetting.value = true
  try {
    const data = await request<{ status?: string; deleted_sessions?: number }>(
      `/api/system/reset?sessions=${sessions ? 'true' : 'false'}`,
      { method: 'POST', timeoutMs: 120_000 },
    )
    deletedSessions.value = data.deleted_sessions ?? 0
    ElMessage.success(`系统已重置，删除 Session ${deletedSessions.value} 个`)
  } catch (error) {
    ElMessage.error(`重置失败: ${(error as ApiError).message}`)
  } finally {
    resetting.value = false
  }
}
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>系统设置</h2>
        <p>管理员令牌与系统重置，两个操作都会直接影响整站的接口访问与数据。</p>
      </div>
      <el-tag :type="auth.saved ? 'success' : 'warning'" effect="dark" round>{{ auth.statusText }}</el-tag>
    </div>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>管理员令牌</span>
            <span class="muted">{{ auth.statusText }}</span>
          </div>
        </template>
        <el-input
          v-model="tokenInput"
          type="password"
          show-password
          placeholder="粘贴 ADMIN_TOKEN"
          clearable
        />
        <div class="row-between" style="margin-top: 10px">
          <span class="muted">令牌只存在浏览器 localStorage，不会上传到服务端。</span>
          <el-button type="primary" :disabled="!tokenInput.trim()" @click="saveToken">
            保存令牌
          </el-button>
        </div>
        <p class="muted" style="margin: 10px 0 0">
          所有 /api 请求都会自动带上 <span class="mono">X-Admin-Token</span> 请求头，令牌不对时会返回未授权，需要重新保存。
        </p>
        <el-alert
          style="margin-top: 10px"
          type="info"
          :closable="false"
          show-icon
          title="令牌值来自服务端 .env 里的 ADMIN_TOKEN，请向部署者索取，不要随意填写。"
        />
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>系统重置</span>
            <span class="muted">危险操作，不可撤销</span>
          </div>
        </template>
        <el-alert
          type="error"
          :closable="false"
          show-icon
          title="重置会清空任务记录与统计数据，正在运行的任务也会被停止。"
        />
        <div class="row-between" style="margin-top: 12px">
          <el-checkbox v-model="resetSessions">同时删除所有 Session 文件</el-checkbox>
          <el-button type="danger" :loading="resetting" @click="resetSystem">重置系统</el-button>
        </div>
        <p class="muted" style="margin: 10px 0 0">
          删除 Session 后所有账号都需要重新导入并重新登录；不勾选时只清空任务与统计数据，账号文件保持不动。
        </p>
        <p v-if="deletedSessions !== null" class="muted" style="margin: 10px 0 0">
          上次重置已删除 Session {{ deletedSessions }} 个。
        </p>
      </el-card>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>前端版本信息</span>
          <span class="muted">由构建时注入，刷新页面即可看到最新值</span>
        </div>
      </template>
      <div class="kpi-grid">
        <div class="kpi-card">
          <span>运行模式（MODE）</span>
          <strong>{{ mode }}</strong>
        </div>
        <div class="kpi-card">
          <span>静态资源基路径（BASE_URL）</span>
          <strong>{{ baseUrl }}</strong>
        </div>
      </div>
      <p class="muted" style="margin: 12px 0 0">
        MODE 为 development 时走 Vite 开发服务器并把 /api 代理到本地后端；为 production 时由后端 /static 目录提供构建产物。
      </p>
    </el-card>
  </div>
</template>
