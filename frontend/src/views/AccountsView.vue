<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAccountsStore } from '@/stores/accounts'
import type { UploadSummary } from '@/stores/accounts'
import type { AccountRow, ProfileRow } from '@/api/types'
import { useUiStore } from '@/stores/ui'
import { formatProxy, statusTagType, translateText } from '@/utils/format'

const accountStore = useAccountsStore()
const ui = useUiStore()

onMounted(() => {
  if (!accountStore.rows.length) void accountStore.refresh()
  void accountStore.loadWarmup()
  // 预热是按轮跑的（默认半小时一轮），跑着的时候自动刷新进度，用户不用手动点
  warmupTimer = window.setInterval(() => {
    if (accountStore.warmupProgress?.running) {
      void accountStore.loadWarmup()
      void accountStore.refresh()
    }
  }, 10_000)
})

onUnmounted(() => {
  if (warmupTimer) window.clearInterval(warmupTimer)
})

let warmupTimer: number | undefined

const warmupBusy = ref(false)
/** 预热是按轮跑的（默认半小时一轮），跑着的时候一直轮询进度，用户不用手动刷新 */
const warmupProgress = computed(() => accountStore.warmupProgress)
const warmupSummary = computed(() => {
  const rows = accountStore.rows
  const exempt = rows.filter((r) => r.warmup?.exempt).length
  const graduated = rows.filter((r) => r.warmup?.tier === 'trusted' || r.warmup?.tier === 'mature').length
  const pending = rows.filter((r) => r.warmup && (r.warmup.tier === 'new' || r.warmup.tier === 'warming')).length
  const running = accountStore.warmupProgress?.running
  const current = accountStore.warmupProgress?.current
  const head = running ? `运行中${current ? `（当前 ${current}）` : ''} · ` : ''
  return `${head}已毕业 ${graduated} · 未毕业 ${pending} · 老号豁免 ${exempt}`
})

function tierTagType(tier?: string): 'success' | 'warning' | 'info' | 'primary' {
  if (tier === 'mature') return 'success'
  if (tier === 'trusted') return 'primary'
  if (tier === 'warming') return 'warning'
  return 'info'
}

async function startWarmup(): Promise<void> {
  warmupBusy.value = true
  try {
    await accountStore.startWarmup()
  } finally {
    warmupBusy.value = false
  }
}

const fileInput = ref<HTMLInputElement | null>(null)
const files = ref<File[]>([])
const proxyText = ref('')
const uploading = ref(false)
const uploadSummary = ref<UploadSummary | null>(null)
const purging = ref(false)

const phone = ref('')
const code = ref('')
const password = ref('')
const loggingIn = ref(false)

const profile = ref<ProfileRow | null>(null)
const loadingProfile = ref(false)

const nickname = ref('')
const about = ref('')
const updatingProfiles = ref(false)

const fileNames = computed(() => files.value.map((f) => f.name))

const profileFields = computed<{ label: string; value: string }[]>(() => {
  const p = profile.value
  if (!p) return []
  return [
    { label: '账号', value: accountStore.selected || '-' },
    { label: '用户名', value: translateText(p.username) },
    { label: '昵称', value: translateText(p.first_name) },
    { label: '手机号', value: translateText(p.phone) },
    { label: '简介', value: translateText(p.about) },
  ]
})

function pickFile(): void {
  fileInput.value?.click()
}

function onFileChange(event: Event): void {
  const target = event.target as HTMLInputElement
  files.value = Array.from(target.files ?? [])
  target.value = ''
}

function rowClassName({ row }: { row: AccountRow }): string {
  return row.account === accountStore.selected ? 'row-selected' : ''
}

function proxyLabel(row: AccountRow): string {
  if (!row.has_proxy) return '未绑定'
  return formatProxy(row.proxy)
}

async function doUpload(): Promise<void> {
  if (!files.value.length) {
    ElMessage.warning('请先选择 .session 或 .zip 文件')
    return
  }
  uploading.value = true
  try {
    const summary = await accountStore.upload(files.value, proxyText.value)
    if (summary) {
      uploadSummary.value = summary
      files.value = []
    }
  } finally {
    uploading.value = false
  }
}

async function removeAccount(account: string): Promise<void> {
  try {
    await ElMessageBox.confirm(`确定删除账号 ${account}？此操作不可撤销。`, '删除确认', { type: 'warning' })
  } catch {
    return
  }
  await accountStore.remove([account])
}

async function purgeSessions(): Promise<void> {
  const total = accountStore.stats.total
  if (!total) {
    ElMessage.warning('当前没有账号可清空')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将删除全部 ${total} 个 Session 文件，并清掉它们名下的任务、代理绑定与检测记录。此操作不可撤销，确定继续？`,
      '清空 Session 文件',
      { type: 'warning', confirmButtonText: '确定清空', confirmButtonClass: 'el-button--danger' },
    )
  } catch {
    return
  }
  purging.value = true
  try {
    const summary = await accountStore.purgeAll()
    if (summary) {
      ElMessage.success(
        `已清空 ${summary.accounts} 个账号（删除 ${summary.files_removed} 个文件、${summary.tasks_removed} 条任务记录）`,
      )
    }
  } finally {
    purging.value = false
  }
}

async function sendCode(): Promise<void> {
  if (!accountStore.selected) return
  if (!phone.value.trim()) {
    ElMessage.warning('请先填写手机号')
    return
  }
  loggingIn.value = true
  try {
    await accountStore.sendLoginCode(accountStore.selected, phone.value.trim())
  } finally {
    loggingIn.value = false
  }
}

async function submitLogin(): Promise<void> {
  if (!accountStore.selected) return
  if (!phone.value.trim() || !code.value.trim()) {
    ElMessage.warning('请先填写手机号与验证码')
    return
  }
  loggingIn.value = true
  try {
    await accountStore.submitLoginCode(accountStore.selected, phone.value.trim(), code.value.trim(), password.value)
  } finally {
    loggingIn.value = false
  }
}

async function readProfile(): Promise<void> {
  if (!accountStore.selected) return
  loadingProfile.value = true
  try {
    profile.value = await accountStore.readProfile(accountStore.selected)
  } finally {
    loadingProfile.value = false
  }
}

async function updateAllProfiles(): Promise<void> {
  if (!nickname.value.trim() && !about.value.trim()) {
    ElMessage.warning('请至少填写昵称或简介')
    return
  }
  try {
    await ElMessageBox.confirm(
      '这是批量操作：将对「所有已授权账号」生效，逐个修改它们的昵称与简介，账号较多时耗时较长。确定继续？',
      '批量修改已授权账号资料',
      { type: 'warning' },
    )
  } catch {
    return
  }
  updatingProfiles.value = true
  try {
    await accountStore.updateAllProfiles(nickname.value.trim(), about.value.trim())
  } finally {
    updatingProfiles.value = false
  }
}
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>账号管理</h2>
        <p>上传 Session → 选择账号 → 验证码登录 → 读写资料；登录状态请到健康检查页检测</p>
      </div>
      <el-space>
        <el-button @click="ui.go('health')">去健康检查</el-button>
        <el-button :loading="accountStore.loading" @click="accountStore.refresh(false)">刷新账号</el-button>
        <el-button type="danger" plain :loading="purging" @click="purgeSessions">清空 Session 文件</el-button>
      </el-space>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card"><span>账号总数</span><strong>{{ accountStore.stats.total }}</strong></div>
      <div class="kpi-card kpi-card--ok"><span>已授权</span><strong>{{ accountStore.stats.authorized }}</strong></div>
      <div class="kpi-card kpi-card--bad"><span>未授权</span><strong>{{ accountStore.stats.unauthorized }}</strong></div>
      <div class="kpi-card"><span>已绑代理</span><strong>{{ accountStore.stats.withProxy }}</strong></div>
      <div class="kpi-card kpi-card--warn"><span>未绑代理</span><strong>{{ accountStore.stats.withoutProxy }}</strong></div>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>上传 Session</span>
          <span class="muted">上传是纯本地导入操作，返回成功也可能存在个别失败项，请按明细核对</span>
        </div>
      </template>

      <el-form label-position="top">
        <el-form-item label="可选：上传时自动绑定代理（一账号一代理）；支持 账号|代理、纯代理列表、代理 API 地址">
          <el-input
            v-model="proxyText"
            type="textarea"
            :rows="4"
            placeholder="留空则只导入账号；支持每行 acct|socks5://user:pass@host:port，或纯代理列表，或代理 API 地址"
          />
        </el-form-item>
      </el-form>

      <div class="row-between">
        <span class="muted">
          {{ fileNames.length ? `已选择 ${fileNames.length} 个文件：${fileNames.join('、')}` : '支持 .session 与 .zip 批量上传' }}
        </span>
        <el-space>
          <input ref="fileInput" type="file" multiple accept=".session,.zip" hidden @change="onFileChange" />
          <el-button @click="pickFile">选择 Session 文件</el-button>
          <el-button type="primary" :loading="uploading" @click="doUpload">上传 Session / ZIP</el-button>
        </el-space>
      </div>

      <el-alert
        v-if="uploadSummary"
        style="margin-top: 12px"
        :type="uploadSummary.invalid || (uploadSummary.errors && uploadSummary.errors.length) ? 'warning' : 'success'"
        :closable="false"
        :title="`上传完成 —— 总计 ${uploadSummary.total} · 成功 ${uploadSummary.uploaded} · 重复 ${uploadSummary.duplicate} · 失败 ${uploadSummary.invalid} · 无代理 ${uploadSummary.no_proxy_count} · 粘性会话 ${uploadSummary.proxy_sticky}`"
      >
        <div v-for="item in uploadSummary.errors || []" :key="item" class="muted">失败：{{ item }}</div>
        <div v-for="item in uploadSummary.no_proxy || []" :key="`np-${item}`" class="muted">无代理：{{ item }}</div>
      </el-alert>

      <el-alert
        style="margin-top: 12px"
        type="info"
        :closable="false"
        show-icon
        title="新上传的账号尚未检测登录状态，请到「健康检查」页执行检测后再使用。"
      />
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            账号列表 <span class="muted">（{{ accountStore.rows.length }} 个）</span>
            <span class="muted" style="margin-left: 12px">当前选中：{{ accountStore.selected || '未选择' }}</span>
          </span>
          <span class="muted">
            {{ accountStore.warmupEnabled ? warmupSummary : '预热功能已关闭' }}
            <el-button
              v-if="accountStore.warmupEnabled"
              link
              type="primary"
              :loading="warmupBusy"
              style="margin-left: 8px"
              @click="startWarmup"
            >
              开始预热
            </el-button>
            <el-button v-if="warmupProgress?.running" link type="danger" @click="accountStore.stopWarmup()">
              停止
            </el-button>
          </span>
        </div>
      </template>
      <el-table
        v-loading="accountStore.loading"
        :data="accountStore.rows"
        :row-class-name="rowClassName"
        row-key="account"
        size="small"
        max-height="460"
        empty-text="还没有账号，先在上方上传 Session"
      >
        <el-table-column label="账号" min-width="200">
          <template #default="{ row }">
            <span class="mono">{{ row.account }}</span>
            <el-tag v-if="row.account === accountStore.selected" size="small" effect="plain" style="margin-left: 6px">
              当前
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="已授权" width="100">
          <template #default="{ row }">
            <el-tag :type="row.authorized ? 'success' : 'danger'" size="small">
              {{ row.authorized ? '是' : '否' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="梯队" min-width="160">
          <template #default="{ row }">
            <template v-if="row.warmup">
              <el-tag :type="tierTagType(row.warmup.tier)" size="small">{{ row.warmup.tier_label }}</el-tag>
              <div v-if="row.warmup.exempt" class="muted" style="font-size: 12px">老号豁免，不参与预热</div>
              <div v-else-if="row.warmup.tier !== 'trusted' && row.warmup.tier !== 'mature'" class="muted" style="font-size: 12px">
                预热进度 {{ row.warmup.readiness }}/{{ row.warmup.ready_score }}（{{ row.warmup.cycles }} 轮）
              </div>
              <div v-else class="muted" style="font-size: 12px">
                {{ row.warmup.tier === 'trusted' ? '刚毕业，发送节奏保守' : '成熟账号' }}
              </div>
            </template>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column label="代理" min-width="180">
          <template #default="{ row }">
            <span :class="row.has_proxy ? 'mono' : 'muted'">{{ proxyLabel(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="accountStore.select(row.account)">选择</el-button>
            <el-button link type="danger" @click="removeAccount(row.account)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>验证码登录</span>
            <span class="muted">目标账号：{{ accountStore.selected || '未选择' }}</span>
          </div>
        </template>
        <el-form label-position="top">
          <el-form-item label="手机号">
            <el-input v-model="phone" placeholder="例如 +8613800000000" />
          </el-form-item>
          <el-form-item label="验证码">
            <el-input v-model="code" placeholder="Telegram 收到的登录验证码" />
          </el-form-item>
          <el-form-item label="二步验证密码（可空）">
            <el-input v-model="password" type="password" show-password placeholder="账号开启了两步验证时填写" />
          </el-form-item>
        </el-form>
        <el-space wrap>
          <el-button :disabled="!accountStore.selected || loggingIn" :loading="loggingIn" @click="sendCode">
            发送验证码
          </el-button>
          <el-button
            type="primary"
            :disabled="!accountStore.selected || loggingIn"
            :loading="loggingIn"
            @click="submitLogin"
          >
            提交登录
          </el-button>
        </el-space>
        <p v-if="!accountStore.selected" class="muted" style="margin: 10px 0 0">
          请先在顶部账号列表里点「选择」指定一个账号。
        </p>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>账号资料</span>
            <el-button
              size="small"
              :disabled="!accountStore.selected"
              :loading="loadingProfile"
              @click="readProfile"
            >
              读取资料
            </el-button>
          </div>
        </template>

        <p v-if="!profile" class="muted">选择一个账号后点击「读取资料」查看昵称、用户名与简介。</p>
        <template v-else>
          <el-alert v-if="profile.error" :closable="false" type="error" :title="translateText(profile.error)" />
          <el-descriptions v-else :column="1" border size="small">
            <el-descriptions-item v-for="field in profileFields" :key="field.label" :label="field.label">
              {{ field.value }}
            </el-descriptions-item>
          </el-descriptions>
        </template>

        <el-divider />

        <p class="muted" style="margin: 0 0 10px">
          批量修改资料：以下内容会应用到「所有已授权账号」，不是只改当前选中账号。
        </p>
        <el-form label-position="top">
          <el-form-item label="昵称">
            <el-input v-model="nickname" placeholder="留空则不修改昵称" />
          </el-form-item>
          <el-form-item label="简介（最长 70 字）">
            <el-input
              v-model="about"
              type="textarea"
              :rows="3"
              :maxlength="70"
              show-word-limit
              placeholder="留空则不修改简介"
            />
          </el-form-item>
        </el-form>
        <el-button
          type="warning"
          :disabled="!nickname.trim() && !about.trim()"
          :loading="updatingProfiles"
          @click="updateAllProfiles"
        >
          批量修改已授权账号资料
        </el-button>
      </el-card>
    </div>
  </div>
</template>

<style scoped>
:deep(.el-table__row.row-selected) {
  background: var(--el-color-primary-light-9);
}
</style>
