<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { TableInstance } from 'element-plus'
import { ApiError, request } from '@/api/client'
import type { JoinedGroup } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { useJoinedGroupsStore } from '@/stores/joinedGroups'
import { useSendStore } from '@/stores/send'
import { useTasksStore } from '@/stores/tasks'
import { translateText } from '@/utils/format'

/** 单账号加群返回：{ account, ok, group_id, title, error, already_joined? } */
interface JoinResult {
  account?: string
  ok?: boolean
  group_id?: number | null
  title?: string | null
  error?: string | null
  already_joined?: boolean
}

/** 全部账号加群返回：{ summary: { total, success, already_joined, failed }, results: [...] } */
interface JoinAllResult {
  summary?: {
    total?: number
    success?: number
    already_joined?: number
    failed?: number
  }
  results?: JoinResult[]
}

const accounts = useAccountsStore()
const joinedGroups = useJoinedGroupsStore()
const send = useSendStore()
const tasks = useTasksStore()

const tableRef = ref<TableInstance | null>(null)
const selectedGroups = ref<JoinedGroup[]>([])
interface SpintaxInfo {
  ok: boolean
  variants: number
  preview: string
  warning?: string | null
  error?: string
  enabled?: boolean
}

const spintaxInfo = ref<SpintaxInfo | null>(null)
let spintaxTimer: number | undefined

/** 变体提示输入即算，但防抖一下，别每敲一个字就打一次接口 */
watch(
  () => send.config.message,
  (value: string) => {
    if (spintaxTimer) window.clearTimeout(spintaxTimer)
    spintaxTimer = window.setTimeout(() => void checkSpintax(value), 700)
  },
)

async function checkSpintax(message: string): Promise<void> {
  if (!message || !message.trim()) {
    spintaxInfo.value = null
    return
  }
  try {
    spintaxInfo.value = await request<SpintaxInfo>('/api/messages/spintax/validate', {
      method: 'POST',
      body: { message },
    })
  } catch {
    // 这一步只是提示，取不到就不显示，不能影响发送本身
    spintaxInfo.value = null
  }
}

const sending = ref(false)
const testing = ref(false)
/** 每次提交后重新生成，避免连续两次手动群发被后端当成重复请求 */
const requestId = ref<string>(crypto.randomUUID())

const joinLink = ref('')
const joinAllLink = ref('')
const joinAllDelayS = ref(3)
const joiningSingle = ref(false)
const joiningAll = ref(false)
const singleJoinResult = ref<JoinResult | null>(null)
const allJoinResult = ref<JoinAllResult | null>(null)

const selectedIds = computed(() => selectedGroups.value.map((row) => row.id))

const blockedReason = computed(() => {
  if (!accounts.selected) return '请先选择发送账号'
  if (!selectedIds.value.length) return '请先在群组列表里勾选至少一个群组'
  if (!send.config.message.trim()) return '请填写要发送的消息内容'
  return ''
})

const allJoinSuccess = computed(() => allJoinResult.value?.summary?.success ?? 0)
const allJoinAlready = computed(() => allJoinResult.value?.summary?.already_joined ?? 0)
const allJoinFailed = computed(() => allJoinResult.value?.summary?.failed ?? 0)

function onAccountChange(value: string): void {
  accounts.select(value)
  onSelectionChange([])
  tableRef.value?.clearSelection()
  void joinedGroups.refresh(value)
}

function onOnlyGroupsChange(): void {
  onSelectionChange([])
  tableRef.value?.clearSelection()
  void joinedGroups.refresh(accounts.selected)
}

function onSelectionChange(rows: JoinedGroup[]): void {
  selectedGroups.value = rows
}

function selectAllRows(): void {
  joinedGroups.rows.forEach((row) => tableRef.value?.toggleRowSelection(row, true))
}

function clearSelection(): void {
  tableRef.value?.clearSelection()
  onSelectionChange([])
}

async function clearCache(): Promise<void> {
  if (!accounts.selected) {
    ElMessage.warning('请先选择账号')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定清理账号 ${accounts.selected} 的群组缓存？下次读取会重新向 Telegram 拉取，可能比较慢。`,
      '确认清理缓存',
      { type: 'warning', confirmButtonText: '清理', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await joinedGroups.clearCache(accounts.selected)
}

async function testSend(): Promise<void> {
  const groupId = selectedIds.value[0]
  if (!groupId) {
    ElMessage.warning('请先在群组列表里勾选一个群组')
    return
  }
  testing.value = true
  try {
    const data = await request<{ total?: number; success?: number; failed?: number }>('/api/test-send', {
      method: 'POST',
      body: {
        account: accounts.selected,
        group_ids: [groupId],
        message: send.config.message,
        parse_mode: send.config.parse_mode,
        disable_web_page_preview: send.config.disable_web_page_preview,
      },
      timeoutMs: 180_000,
    })
    ElMessage.success(`测试发送完成：成功 ${data.success ?? 0}，失败 ${data.failed ?? 0}`)
  } catch (error) {
    ElMessage.error(`测试发送失败: ${(error as ApiError).message}`)
  } finally {
    testing.value = false
  }
}

async function submitSend(): Promise<void> {
  sending.value = true
  try {
    const data = await request<{ task_id?: string; duplicate?: boolean }>('/api/send-async', {
      method: 'POST',
      body: {
        account: accounts.selected,
        group_ids: selectedIds.value,
        message: send.config.message,
        parse_mode: send.config.parse_mode,
        disable_web_page_preview: send.config.disable_web_page_preview,
        delay_ms: send.config.delay_ms,
        rounds: send.config.rounds,
        round_interval_s: send.config.round_interval_s,
        request_id: requestId.value,
      },
      timeoutMs: 60_000,
    })
    if (data.duplicate) {
      ElMessage.warning('该请求已提交过，未重复创建任务')
    } else {
      ElMessage.success(`任务已创建：${data.task_id ?? '-'}`)
    }
    requestId.value = crypto.randomUUID()
    await tasks.refresh(false)
  } catch (error) {
    ElMessage.error(`创建任务失败: ${(error as ApiError).message}`)
  } finally {
    sending.value = false
  }
}

async function joinSingle(): Promise<void> {
  if (!accounts.selected) {
    ElMessage.warning('请先选择账号')
    return
  }
  const link = joinLink.value.trim()
  if (!link) {
    ElMessage.warning('请填写邀请链接或 @username')
    return
  }
  joiningSingle.value = true
  try {
    const data = await request<JoinResult>('/api/groups/join', {
      method: 'POST',
      body: { account: accounts.selected, invite_link: link },
      timeoutMs: 180_000,
    })
    singleJoinResult.value = data
    if (data.ok) ElMessage.success(`加群成功：${data.title || data.group_id || link}`)
    else ElMessage.error(`加群失败: ${translateText(data.error)}`)
  } catch (error) {
    ElMessage.error(`加群失败: ${(error as ApiError).message}`)
  } finally {
    joiningSingle.value = false
  }
}

async function joinAll(): Promise<void> {
  const link = joinAllLink.value.trim()
  if (!link) {
    ElMessage.warning('请填写邀请链接或 @username')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定让全部已授权账号加入 ${link}？账号之间间隔 ${joinAllDelayS.value} 秒，账号较多时会比较慢。`,
      '确认全部账号加群',
      { type: 'warning', confirmButtonText: '开始加群', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  joiningAll.value = true
  try {
    const data = await request<JoinAllResult>('/api/groups/join-all-accounts', {
      method: 'POST',
      body: { invite_link: link, delay_ms: Math.max(0, Number(joinAllDelayS.value) || 0) * 1000 },
      timeoutMs: 180_000,
    })
    allJoinResult.value = data
    ElMessage.success(`加群已执行：成功 ${data.summary?.success ?? 0}，失败 ${data.summary?.failed ?? 0}`)
  } catch (error) {
    ElMessage.error(`批量加群失败: ${(error as ApiError).message}`)
  } finally {
    joiningAll.value = false
  }
}

onMounted(async () => {
  if (!accounts.rows.length) await accounts.refresh(false)
  // 未授权的账号去拉群组只会拿到 403，先让用户选一个能用的账号
  const row = accounts.rows.find((r) => r.account === accounts.selected)
  if (row?.authorized) await joinedGroups.refresh(accounts.selected)
})
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>手动群发</h2>
        <p>
          兜底入口：手动指定「一个账号 + 若干已加入群组」直接发送。主流程请走全局控制台的智能调度，
          那里会按账号状态、代理与冷却自动分配目标。
        </p>
      </div>
      <el-space>
        <span class="muted">发送账号</span>
        <el-select
          v-model="accounts.selected"
          placeholder="请选择账号"
          filterable
          style="width: 220px"
          @change="onAccountChange"
        >
          <el-option v-for="row in accounts.rows" :key="row.account" :label="row.account" :value="row.account">
            <span>{{ row.account }}</span>
            <span class="muted"> · {{ row.authorized ? '已授权' : '未授权' }}</span>
          </el-option>
        </el-select>
        <el-button :loading="accounts.loading" @click="accounts.refresh(false)">刷新账号</el-button>
      </el-space>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            群组选择
            <span class="muted">（{{ joinedGroups.summary }}）</span>
          </span>
          <el-space wrap>
            <span class="muted">只显示群组（不含频道）</span>
            <el-switch v-model="joinedGroups.onlyGroups" @change="onOnlyGroupsChange" />
            <el-button size="small" type="primary" :loading="joinedGroups.loading" @click="joinedGroups.refresh(accounts.selected, true)">
              加载 / 刷新群组
            </el-button>
            <el-button size="small" @click="clearCache()">清理缓存</el-button>
          </el-space>
        </div>
      </template>

      <el-table
        ref="tableRef"
        v-loading="joinedGroups.loading"
        :data="joinedGroups.rows"
        size="small"
        height="360"
        row-key="id"
        empty-text="当前账号还没有可用群组数据，点右上角「加载 / 刷新群组」"
        @selection-change="onSelectionChange"
      >
        <el-table-column type="selection" width="46" />
        <el-table-column label="群名" min-width="220">
          <template #default="{ row }">
            <strong>{{ row.title || row.id }}</strong>
            <span v-if="row.title" class="muted"> · ID {{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="用户名" min-width="150">
          <template #default="{ row }"><span class="mono">{{ row.username || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="人数" width="100">
          <template #default="{ row }"><span class="mono">{{ row.member_count ?? '-' }}</span></template>
        </el-table-column>
        <el-table-column label="可发送" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.can_send === false" type="danger" size="small">不可发送</el-tag>
            <el-tag v-else-if="row.can_send === true" type="success" size="small">可发送</el-tag>
            <el-tag v-else type="info" size="small">未知</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <div class="row-between" style="margin-top: 12px">
        <span class="muted">
          已选 {{ selectedIds.length }} 个群组
          <template v-if="selectedIds.length">
            · ID：{{ selectedIds.slice(0, 8).join('、') }}{{ selectedIds.length > 8 ? ' …' : '' }}
          </template>
        </span>
        <el-space>
          <el-button size="small" :disabled="!joinedGroups.rows.length" @click="selectAllRows">全选本页</el-button>
          <el-button size="small" :disabled="!selectedIds.length" @click="clearSelection">清空选择</el-button>
        </el-space>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>消息与发送参数</span>
          <span class="muted">与全局控制台「智能调度」共用同一份参数</span>
        </div>
      </template>

      <el-form label-position="top">
        <el-form-item label="要发送的消息内容">
          <el-input
            v-model="send.config.message"
            type="textarea"
            :rows="4"
            placeholder="发送给选中群组的内容（支持内联机器人 @PostBot 与消息链接转发）"
          />
          <!-- 变体提示：几百个群发同一句话是明显的垃圾特征，这里提前告诉用户 -->
          <div v-if="spintaxInfo" style="font-size: 12px; margin-top: 4px">
            <span v-if="spintaxInfo.error" style="color: var(--el-color-danger)">
              变体语法有问题：{{ spintaxInfo.error }}
            </span>
            <span v-else-if="spintaxInfo.variants > 1">
              可生成 <b>{{ spintaxInfo.variants }}</b> 种不同文本，每条消息随机取一种 —— 例如「{{ spintaxInfo.preview }}」
              <span v-if="spintaxInfo.warning" style="color: var(--el-color-warning)">（{{ spintaxInfo.warning }}）</span>
            </span>
            <span v-else class="muted">
              这条文案每次发出去都完全一样。写成 <code>{你好|您好|hi}</code> 就能让每条消息各不相同，变体越多越安全。
            </span>
          </div>
        </el-form-item>
        <el-space wrap style="align-items: flex-end">
          <el-form-item label="解析模式" style="margin-bottom: 0">
            <el-select v-model="send.config.parse_mode" style="width: 140px">
              <el-option label="纯文本" value="plain" />
              <el-option label="Markdown" value="markdown" />
              <el-option label="HTML" value="html" />
            </el-select>
          </el-form-item>
          <el-form-item label="每群间隔（毫秒）" style="margin-bottom: 0">
            <el-input-number v-model="send.config.delay_ms" :min="1500" :step="500" />
          </el-form-item>
          <el-form-item label="每号轮数" style="margin-bottom: 0">
            <el-input-number v-model="send.config.rounds" :min="1" />
          </el-form-item>
          <el-form-item label="轮次间隔（秒）" style="margin-bottom: 0">
            <el-input-number v-model="send.config.round_interval_s" :min="0" />
          </el-form-item>
          <el-form-item label="预览链接" style="margin-bottom: 0">
            <el-switch v-model="send.config.disable_web_page_preview" active-text="关闭" inactive-text="开启" />
          </el-form-item>
        </el-space>
      </el-form>

      <div class="row-between" style="margin-top: 6px">
        <el-space>
          <el-button
            plain
            :loading="testing"
            :disabled="!selectedIds.length || !send.config.message.trim()"
            @click="testSend"
          >
            测试发送
          </el-button>
          <el-button type="primary" :loading="sending" :disabled="Boolean(blockedReason)" @click="submitSend">
            发送到选中群组（{{ selectedIds.length }}）
          </el-button>
        </el-space>
        <span v-if="blockedReason" class="muted">{{ blockedReason }}</span>
        <span v-else class="muted">测试发送只对勾选的第一个群组发一次，用于验证文案与权限。</span>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>加群</span>
          <span class="muted">加群与发消息相互独立，可先用这里把账号加进目标群</span>
        </div>
      </template>

      <div class="dual">
        <el-card shadow="never">
          <template #header>
            <div class="row-between">
              <span>单账号加群</span>
              <span class="muted mono">{{ accounts.selected || '未选择账号' }}</span>
            </div>
          </template>
          <el-input v-model="joinLink" placeholder="邀请链接或 @username，如 https://t.me/+xxxxx 或 @xxxxx" clearable />
          <div class="row-between" style="margin-top: 10px">
            <span class="muted">强制一账号一代理开启时，没有代理的账号会被拒绝。</span>
            <el-button type="primary" :loading="joiningSingle" :disabled="!accounts.selected" @click="joinSingle">
              加入这个群
            </el-button>
          </div>
          <el-alert
            v-if="singleJoinResult"
            style="margin-top: 10px"
            :type="singleJoinResult.ok ? 'success' : 'error'"
            :closable="false"
            :title="singleJoinResult.ok ? '加群成功' : '加群失败'"
          >
            <div class="muted">账号：{{ singleJoinResult.account || accounts.selected }}</div>
            <div class="muted">群名：{{ singleJoinResult.title || '-' }}</div>
            <div class="muted">群 ID：{{ singleJoinResult.group_id ?? '-' }}</div>
            <div class="muted">是否已在群里：{{ singleJoinResult.already_joined ? '是' : '否' }}</div>
            <div v-if="singleJoinResult.error" class="muted">原因：{{ translateText(singleJoinResult.error) }}</div>
          </el-alert>
        </el-card>

        <el-card shadow="never">
          <template #header>
            <div class="row-between">
              <span>所有账号加同一个群</span>
              <span class="muted">会操作全部已授权账号</span>
            </div>
          </template>
          <el-input v-model="joinAllLink" placeholder="邀请链接或 @username，如 https://t.me/+xxxxx" clearable />
          <el-space wrap style="margin-top: 10px">
            <span class="muted">账号间隔（秒）</span>
            <el-input-number v-model="joinAllDelayS" :min="0" :max="600" />
            <el-button type="warning" :loading="joiningAll" @click="joinAll">全部账号加群</el-button>
          </el-space>
          <p class="muted" style="margin: 10px 0 0">账号数量越多耗时越长，请耐心等待，中途不要刷新页面。</p>
          <template v-if="allJoinResult">
            <el-alert style="margin-top: 10px" type="success" :closable="false" title="加群已执行完毕">
              <div class="muted">
                总计 {{ allJoinResult.summary?.total ?? allJoinResult.results?.length ?? 0 }} ·
                成功 {{ allJoinSuccess }} · 已加入 {{ allJoinAlready }} · 失败 {{ allJoinFailed }}
              </div>
            </el-alert>
            <el-table :data="allJoinResult.results || []" size="small" max-height="240" style="margin-top: 10px">
              <el-table-column label="账号" width="130">
                <template #default="{ row }"><span class="mono">{{ row.account }}</span></template>
              </el-table-column>
              <el-table-column label="结果" width="100">
                <template #default="{ row }">
                  <el-tag v-if="row.already_joined" type="info" size="small">已加入</el-tag>
                  <el-tag v-else :type="row.ok ? 'success' : 'danger'" size="small">
                    {{ row.ok ? '成功' : '失败' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="群 / 原因" min-width="180">
                <template #default="{ row }">
                  <span class="muted">{{ row.ok ? (row.title || '-') : translateText(row.error) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </template>
        </el-card>
      </div>
    </el-card>
  </div>
</template>
