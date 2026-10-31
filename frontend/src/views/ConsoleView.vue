<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useConsoleStore } from '@/stores/console'
import { useJoinStore } from '@/stores/join'
import { usePipelineStore } from '@/stores/pipeline'
import { usePresetsStore } from '@/stores/presets'
import { useSendStore } from '@/stores/send'
import { useTasksStore } from '@/stores/tasks'
import { useLogsStore } from '@/stores/logs'
import { useUiStore } from '@/stores/ui'
import { formatTime, statusTagType, translateText } from '@/utils/format'

const consoleStore = useConsoleStore()
const join = useJoinStore()
const pipeline = usePipelineStore()
const presets = usePresetsStore()
const send = useSendStore()
const tasks = useTasksStore()
const logs = useLogsStore()
const ui = useUiStore()

onMounted(() => {
  void presets.refresh()
})

function onPresetChange(no: number | null): void {
  presets.select(no)
}

/** 把任务模式翻译成一句人话：点开始前必须一眼看清每个群会收到几条。 */
const modeHint = computed(() => {
  const cooldown = send.config.group_msg_cooldown_s
  const cool = cooldown > 0
    ? `同一个群收到同一批内容的间隔不会小于 ${cooldown} 秒（不同账号也一样）。`
    : '同群同文案冷却已关闭。'
  if (send.config.mode === 'single') {
    return `每个群只发 1 条，发完任务自动结束，不会重复。${cool}`
  }
  const minutes = Math.round(send.taskRoundIntervalS / 60)
  const span = minutes >= 60 ? `${(minutes / 60).toFixed(1)} 小时` : `${minutes} 分钟`
  const floor = cooldown > send.taskRoundIntervalS
    ? `轮次间隔比冷却短，实际会按 ${cooldown} 秒一轮走。`
    : ''
  return `每个群一共收到 ${send.taskRounds} 条消息：发完一轮后每 ${span} 再发一轮，共 ${send.taskRounds} 轮。`
    + `${cool}${floor}`
})

/** 边加群边群发：后台持续加群，发送任务每轮自动接住新加入的群。 */
async function confirmLive(): Promise<void> {
  const no = presets.selectedNo
  if (!no) return
  try {
    await ElMessageBox.confirm(
      '这是一个常驻模式，点「停止」才会结束，后台会一直循环：\n'
        + '1. 按「每个群至少 2 个可发成员」补加群，慢但不停\n'
        + '2. 定期从 Telegram 刷新每个账号真实加入的群\n'
        + `3. 用 #${no} 号任务持续群发，新加入的群下一轮自动生效\n\n`
        + modeHint.value,
      `确认开始 #${no} 号任务的「边加群边群发」`,
      { type: 'warning', confirmButtonText: '开始', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await pipeline.startLive(no)
}

const fileInput = ref<HTMLInputElement | null>(null)
const logTab = ref<'send' | 'join'>('send')
const poolKeyword = ref('')
const controlling = ref(false)

function pickFile(): void {
  fileInput.value?.click()
}

function onFileChange(event: Event): void {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0] ?? null
  target.value = ''
  void consoleStore.importGroups(file)
}

async function controlAll(action: 'pause' | 'resume' | 'stop'): Promise<void> {
  controlling.value = true
  try {
    await tasks.controlAll(action)
  } finally {
    controlling.value = false
  }
}

function progressOf(row: { current_round_sent: number; current_round_planned: number }): number {
  if (!row.current_round_planned) return 0
  return Math.min(100, Math.round((row.current_round_sent / row.current_round_planned) * 100))
}

function onGroupSelectionChange(rows: { id: number }[]): void {
  consoleStore.selectedGroupIds = rows.map((row) => row.id)
}

/** 加群结果的标签配色：待审批是「没失败但也没成」，单独用警告色。 */
function joinStatusType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (status === 'joined') return 'success'
  if (status === 'requested') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

async function confirmJoin(): Promise<void> {
  const count = consoleStore.selectedGroupIds.length || consoleStore.pool.total
  const scope = consoleStore.selectedGroupIds.length
    ? `选中的 ${count} 个群`
    : `全部 ${count} 个群`
  try {
    await ElMessageBox.confirm(
      `会把${scope}按轮询平均分给可用账号，然后逐个真实加入（每群间隔 ${join.delayMs / 1000} 秒，会很慢）。已在群内的会自动跳过。`,
      '确认开始加群',
      { type: 'warning', confirmButtonText: '开始加群', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await join.start(consoleStore.selectedGroupIds)
}
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>全局调度中心</h2>
        <p>上传账号 → 导入目标群组 → 检测账号 → 生成智能方案 → 开始全部任务</p>
      </div>
      <el-space>
        <el-tag :type="tasks.progress.running ? 'success' : 'info'" effect="dark" round>
          {{ tasks.progress.running ? '调度运行中' : '调度空闲' }}
        </el-tag>
        <el-button
          :type="consoleStore.errorTotal ? 'danger' : 'default'"
          :plain="Boolean(consoleStore.errorTotal)"
          @click="consoleStore.openErrors()"
        >
          异常 {{ consoleStore.errorTotal }}
        </el-button>
        <el-button @click="ui.go('accounts')">上传账号</el-button>
        <el-button @click="ui.go('health')">检测账号</el-button>
        <el-button :loading="consoleStore.loading.overview" @click="consoleStore.refresh(false, true)">刷新</el-button>
      </el-space>
    </div>

    <div class="kpi-grid">
      <div
        v-for="kpi in consoleStore.kpis"
        :key="kpi.label"
        class="kpi-card"
        :class="kpi.tone ? `kpi-card--${kpi.tone}` : ''"
      >
        <span>{{ kpi.label }}</span>
        <strong>{{ kpi.value }}</strong>
      </div>
    </div>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>账号资源池</span>
            <span class="muted">
              可用 {{ consoleStore.overview.accounts.ready }} / 共 {{ consoleStore.overview.accounts.total }}
            </span>
          </div>
        </template>
        <el-progress
          :percentage="consoleStore.overview.accounts.total
            ? Math.round((consoleStore.overview.accounts.ready / consoleStore.overview.accounts.total) * 100)
            : 0"
          :stroke-width="8"
        />
        <p class="muted" style="margin: 10px 0 0">
          可用 {{ consoleStore.overview.accounts.ready }} ·
          异常 {{ consoleStore.overview.accounts.error }} ·
          未检测 {{ consoleStore.overview.accounts.unchecked }} ·
          无代理 {{ consoleStore.overview.accounts.no_proxy }}
        </p>
        <el-alert
          v-if="consoleStore.overview.accounts.unchecked > 0"
          style="margin-top: 10px"
          type="warning"
          :closable="false"
          show-icon
          :title="`还有 ${consoleStore.overview.accounts.unchecked} 个账号没检测登录状态，未检测的账号不会进入调度方案。`"
        />
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>目标群组池</span>
            <el-space>
              <el-button
                size="small"
                @click="consoleStore.removeGroups({ scope: 'invalid' }, '确定删除所有不可访问 / 失败的群组？只删目标记录，不影响账号与任务历史。')"
              >
                删除无效
              </el-button>
              <el-button
                size="small"
                type="danger"
                @click="consoleStore.removeGroups({ scope: 'all' }, '确定清空整个目标群组池？只删目标记录，不影响账号与任务历史。')"
              >
                清空全部
              </el-button>
            </el-space>
          </div>
        </template>
        <el-input
          v-model="consoleStore.importText"
          type="textarea"
          :rows="5"
          placeholder="每行一个：https://t.me/xxxxx / @xxxxx / xxxxx"
        />
        <div class="row-between" style="margin-top: 10px">
          <span class="muted">自动去空格、去重复、格式校验；个别坏行不会导致整批失败。</span>
          <el-space>
            <input ref="fileInput" type="file" accept=".txt,.csv" hidden @change="onFileChange" />
            <el-button @click="pickFile">选择文件</el-button>
            <el-button type="primary" :loading="consoleStore.loading.importing" @click="consoleStore.importGroups(null)">
              导入群组
            </el-button>
          </el-space>
        </div>
        <el-alert
          v-if="consoleStore.importResult"
          style="margin-top: 10px"
          :type="consoleStore.importResult.imported ? 'success' : 'warning'"
          :closable="false"
          :title="`导入完成 —— 总计 ${consoleStore.importResult.total} · 成功 ${consoleStore.importResult.imported} · 重复 ${consoleStore.importResult.duplicate} · 无效 ${consoleStore.importResult.invalid}`"
        >
          <div v-for="sample in consoleStore.importResult.invalid_samples" :key="sample" class="muted">
            无效：{{ sample }}
          </div>
        </el-alert>
      </el-card>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            群组列表
            <span class="muted">（{{ consoleStore.filteredGroups.length }} / {{ consoleStore.pool.total }} 条）</span>
          </span>
          <el-space wrap>
            <el-select v-model="consoleStore.poolStatusFilter" placeholder="全部状态" clearable style="width: 130px">
              <el-option
                v-for="(count, status) in consoleStore.pool.by_status"
                :key="status"
                :label="`${translateText(status)}（${count}）`"
                :value="status"
              />
            </el-select>
            <el-input v-model="poolKeyword" placeholder="筛选群名 / 用户名" clearable style="max-width: 220px" />
            <el-checkbox v-model="consoleStore.onlyUnjoined" :label="`只看无人加入（${consoleStore.unjoinedCount}）`" />
            <el-button
              size="small"
              :loading="consoleStore.loading.rechecking"
              :disabled="!consoleStore.pool.total"
              @click="consoleStore.recheckGroups()"
            >
              重新检测
            </el-button>
            <el-button
              size="small"
              :loading="consoleStore.loading.rechecking"
              :disabled="!consoleStore.selectedGroupIds.length"
              @click="consoleStore.recheckGroups(consoleStore.selectedGroupIds)"
            >
              检测选中（{{ consoleStore.selectedGroupIds.length }}）
            </el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!consoleStore.selectedGroupIds.length"
              @click="consoleStore.removeGroups({ ids: consoleStore.selectedGroupIds }, `确定删除选中的 ${consoleStore.selectedGroupIds.length} 个目标群组？只删目标记录，不影响账号与任务历史。`)"
            >
              删除选中
            </el-button>
          </el-space>
        </div>
      </template>
      <div class="scroll-box">
        <el-table
          :data="consoleStore.filteredGroups"
          size="small"
          height="420"
          row-key="id"
          empty-text="还没有目标群组，先在上面导入"
          @selection-change="onGroupSelectionChange"
        >
          <el-table-column type="selection" width="44" :reserve-selection="true" />
          <el-table-column label="群组" min-width="200">
            <template #default="{ row }">
              <strong>{{ row.title || row.username }}</strong>
              <span class="muted"> · {{ row.username }}</span>
            </template>
          </el-table-column>
          <el-table-column label="链接" min-width="200">
            <template #default="{ row }">
              <span class="mono">{{ row.link }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="已加入账号" width="120">
            <template #default="{ row }">
              <el-tooltip
                v-if="row.joined_accounts"
                :content="(row.joined_sample || []).join('、')"
                placement="top"
              >
                <el-tag type="success" size="small">{{ row.joined_accounts }} 个</el-tag>
              </el-tooltip>
              <el-tag v-else type="danger" size="small">无人加入</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="备注" min-width="160">
            <template #default="{ row }">
              <span class="muted">{{ row.last_error || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="最后执行" width="160">
            <template #default="{ row }">
              <span class="muted">{{ row.last_run_at ? formatTime(row.last_run_at) : '未执行' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="danger"
                @click="consoleStore.removeGroups({ ids: [row.id] }, `删除目标群组 ${row.username}？`)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            批量加群
            <span class="muted">（目标群组平均分给可用账号）</span>
          </span>
          <el-space wrap>
            <span class="muted">并发账号</span>
            <el-input-number
              v-model="join.workers"
              :min="1"
              :max="40"
              :disabled="join.running"
              size="small"
              controls-position="right"
              style="width: 100px"
            />
            <span class="muted">每群间隔（秒）</span>
            <el-input-number
              v-model="join.delayMs"
              :min="1500"
              :max="120000"
              :step="500"
              size="small"
              controls-position="right"
            />
            <el-button size="small" :loading="join.loading.preview" @click="join.loadPreview(consoleStore.selectedGroupIds)">
              预览分配
            </el-button>
            <el-button
              size="small"
              type="primary"
              :loading="join.loading.starting"
              :disabled="join.running || !consoleStore.pool.total"
              @click="confirmJoin()"
            >
              {{ consoleStore.selectedGroupIds.length ? `加群选中（${consoleStore.selectedGroupIds.length}）` : '开始批量加群' }}
            </el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :disabled="!join.running"
              :loading="join.loading.stopping"
              @click="join.stop()"
            >
              停止
            </el-button>
          </el-space>
        </div>
      </template>

      <p class="muted" style="margin-top: 0">
        每个目标群只会分给一个账号（轮询平均分配），不会两个账号加同一个群；
        账号已经在群里的目标记「已跳过」，所以同一批重跑不会重复加入。
      </p>

      <div v-if="join.preview" class="kpi-grid" style="margin-bottom: 10px">
        <div class="kpi-card"><span>待分配群组</span><strong>{{ join.preview.groups }}</strong></div>
        <div class="kpi-card kpi-card--ok"><span>参与账号</span><strong>{{ join.preview.accounts_used }}</strong></div>
        <div class="kpi-card"><span>可用账号</span><strong>{{ join.preview.accounts_ready }}</strong></div>
        <div class="kpi-card kpi-card--warn">
          <span>未参与账号</span>
          <strong>{{ Object.values(join.preview.accounts_blocked || {}).reduce((a, b) => a + b, 0) }}</strong>
        </div>
      </div>
      <p v-if="join.preview && Object.keys(join.preview.accounts_blocked || {}).length" class="muted">
        未参与原因：{{ Object.entries(join.preview.accounts_blocked).map(([k, v]) => `${k} ${v}`).join('、') }}
      </p>

      <template v-if="join.job">
        <div class="row-between" style="margin: 6px 0">
          <span>
            进度 {{ join.job.done }} / {{ join.job.total }}
            <span class="muted">
              · 已加入 {{ join.job.joined }} · 已跳过 {{ join.job.skipped }} · 待审批 {{ join.job.requested }} · 失败 {{ join.job.failed }}
              <template v-if="join.job.redispatches"> · 换号重试 {{ join.job.redispatches }} 次</template>
              <template v-if="join.job.flood_waits"> · 限流等待 {{ join.job.flood_waits }} 次</template>
            </span>
          </span>
          <span class="muted">
            {{ join.running
              ? `${join.etaText}${join.speed ? ` · ${join.speed.toFixed(1)} 条/分钟` : ''} · 并发 ${join.job.workers} 个账号`
              : `已结束 ${formatTime(join.job.finished_at)}` }}
          </span>
        </div>
        <el-progress :percentage="join.percent" :stroke-width="8" />
        <p v-if="join.running" class="muted" style="margin: 8px 0 0">
          正在处理：{{ join.currentText }}
        </p>

        <div class="row-between" style="margin: 10px 0 6px">
          <span class="muted">明细（{{ join.items.length }} 条）</span>
          <el-select v-model="join.filter" size="small" style="width: 130px">
            <el-option label="全部" value="all" />
            <el-option label="已加入" value="joined" />
            <el-option label="已跳过" value="skipped" />
            <el-option label="待审批" value="requested" />
            <el-option label="失败" value="failed" />
            <el-option label="等待中" value="pending" />
          </el-select>
        </div>
        <el-table :data="join.items" size="small" max-height="320" empty-text="暂无明细">
          <el-table-column label="账号" width="140">
            <template #default="{ row }"><span class="mono">{{ row.account }}</span></template>
          </el-table-column>
          <el-table-column label="目标群组" min-width="200">
            <template #default="{ row }">{{ row.title || row.username }}</template>
          </el-table-column>
          <el-table-column label="结果" width="100">
            <template #default="{ row }">
              <el-tag :type="joinStatusType(row.status)" size="small">
                {{ join.statusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="换过账号" width="150">
            <template #default="{ row }">
              <span class="muted mono">{{ (row.tried?.length || 0) > 1 ? row.tried.join(' → ') : '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="说明" min-width="180">
            <template #default="{ row }">
              <span class="muted">{{ row.error ? translateText(row.error) : '-' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            智能调度 · 一键群发
            <span class="muted">（选编号 → 点一次：生成方案 → 开始发送）</span>
          </span>
          <span class="muted">按账号状态、负载与冷却自动分配，不需要手工指定「账号 → 群」</span>
        </div>
      </template>

      <div class="row-between" style="margin-bottom: 12px">
        <el-space wrap>
          <span class="muted">发送任务</span>
          <el-select
            :model-value="presets.selectedNo"
            placeholder="选择几号任务"
            style="width: 260px"
            :loading="presets.loading.list"
            @change="onPresetChange"
          >
            <el-option v-for="row in presets.rows" :key="row.no" :label="presets.label(row)" :value="row.no" />
          </el-select>
          <el-button :loading="presets.loading.saving" @click="presets.save(true)">保存为新任务</el-button>
          <el-button
            :disabled="!presets.selectedNo"
            :loading="presets.loading.saving"
            @click="presets.save(false)"
          >
            {{ presets.selectedNo ? `覆盖 #${presets.selectedNo}` : '覆盖编号' }}
          </el-button>
          <el-button
            v-if="presets.selectedNo"
            type="danger"
            plain
            @click="presets.remove(presets.selectedNo)"
          >
            删除
          </el-button>
        </el-space>
        <span class="muted">
          {{ presets.rows.length }} 个已保存任务{{ presets.rows.length ? '' : '，填好内容点「保存为新任务」拿编号' }}
        </span>
      </div>

      <el-form label-position="top">
        <el-form-item label="要发送的消息内容">
          <el-input
            v-model="send.config.message"
            type="textarea"
            :rows="4"
            placeholder="发送给目标群组的内容（支持内联机器人 @PostBot 与消息链接转发）"
          />
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
          <el-form-item label="预览链接" style="margin-bottom: 0">
            <el-switch v-model="send.config.disable_web_page_preview" active-text="关闭" inactive-text="开启" />
          </el-form-item>
        </el-space>
        <el-space wrap style="align-items: flex-end; margin-top: 6px">
          <el-form-item label="任务模式" style="margin-bottom: 0">
            <el-radio-group v-model="send.config.mode">
              <el-radio-button value="single">发一轮就结束</el-radio-button>
              <el-radio-button value="loop">多轮循环</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <template v-if="send.config.mode === 'loop'">
            <el-form-item label="轮数" style="margin-bottom: 0">
              <el-input-number v-model="send.config.rounds" :min="2" :max="200" />
            </el-form-item>
            <el-form-item label="每轮间隔（秒）" style="margin-bottom: 0">
              <el-input-number v-model="send.config.round_interval_s" :min="60" :step="300" />
            </el-form-item>
          </template>
          <el-form-item label="同群同文案冷却（秒）" style="margin-bottom: 0">
            <el-input-number v-model="send.config.group_msg_cooldown_s" :min="0" :max="86400" :step="30" />
          </el-form-item>
        </el-space>
        <p class="muted" style="margin: 10px 0 0">{{ modeHint }}</p>
      </el-form>

      <div class="row-between" style="margin-top: 6px">
        <el-space wrap>
          <el-button
            type="primary"
            plain
            size="large"
            :disabled="pipeline.running || !presets.selectedNo"
            :loading="pipeline.loading.starting"
            @click="confirmLive()"
          >
            {{ presets.selectedNo ? `边加群边群发（#${presets.selectedNo}）` : '边加群边群发' }}
          </el-button>
          <el-button
            v-if="pipeline.running"
            type="danger"
            plain
            :loading="pipeline.loading.stopping"
            @click="pipeline.stop()"
          >
            停止
          </el-button>
        </el-space>
      </div>
      <p v-if="!presets.selectedNo" class="muted" style="margin: 8px 0 0">
        先在右上角选一个任务编号，「边加群边群发」按钮才能点。
      </p>
      <p v-if="!send.config.message.trim()" class="muted" style="margin: 8px 0 0">
        开始任务前必须填写消息内容（或先选中一个已保存的编号自动填入）。
      </p>

      <template v-if="pipeline.run">
        <el-steps :active="pipeline.activeStep" align-center style="margin-top: 18px">
          <el-step
            v-for="step in pipeline.run.steps"
            :key="step.key"
            :title="step.label"
            :description="step.detail || '等待中'"
            :status="pipeline.stepStatus(step.status)"
          />
        </el-steps>
        <el-alert
          v-if="pipeline.run.status === 'failed'"
          style="margin-top: 14px"
          type="error"
          :closable="false"
          show-icon
          :title="`#${pipeline.run.preset_no} 号任务链路中断：${pipeline.run.error || '未知原因'}`"
        />
        <el-alert
          v-else-if="pipeline.run.status === 'done'"
          style="margin-top: 14px"
          type="success"
          :closable="false"
          show-icon
          :title="`#${pipeline.run.preset_no} 号任务已开始发送：${pipeline.run.result?.accounts_count || 0} 个账号 / ${pipeline.run.result?.planned_groups || 0} 个目标群组`"
        />
        <el-alert
          v-else-if="pipeline.run.status === 'stopped'"
          style="margin-top: 14px"
          type="warning"
          :closable="false"
          show-icon
          title="链路已停止"
        />
        <el-alert
          v-else
          style="margin-top: 14px"
          type="info"
          :closable="false"
          :title="pipeline.run.detail || '正在准备…'"
        />
      </template>
    </el-card>

    <el-card v-if="tasks.progress.planned" shadow="never">
      <template #header>
        <div class="row-between">
          <span>总体进度</span>
          <span class="muted mono">{{ tasks.progress.done }} / {{ tasks.progress.planned }}</span>
        </div>
      </template>
      <el-progress :percentage="tasks.progress.percent" :stroke-width="14" text-inside />
      <div class="kpi-grid" style="margin-top: 16px">
        <div class="kpi-card kpi-card--ok"><span>成功</span><strong>{{ tasks.progress.success }}</strong></div>
        <div class="kpi-card kpi-card--bad"><span>失败</span><strong>{{ tasks.progress.failed }}</strong></div>
        <div class="kpi-card"><span>执行中账号</span><strong>{{ tasks.progress.running }}</strong></div>
        <div class="kpi-card"><span>等待中账号</span><strong>{{ tasks.progress.waiting }}</strong></div>
        <div class="kpi-card"><span>剩余</span><strong>{{ tasks.progress.remaining }}</strong></div>
      </div>
      <div class="row-between" style="margin-top: 16px">
        <el-space>
          <el-button :disabled="controlling" @click="controlAll('pause')">全部暂停</el-button>
          <el-button :disabled="controlling" @click="controlAll('resume')">全部恢复</el-button>
          <el-button type="danger" :disabled="controlling" @click="controlAll('stop')">全部停止</el-button>
          <el-button type="danger" plain @click="tasks.clearAll()">清空任务记录</el-button>
        </el-space>
        <span class="muted">停止为 Graceful Stop：正在执行的 Telegram 操作会安全结束，新的不再领取。</span>
      </div>
    </el-card>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>最近任务 <span class="muted">（{{ tasks.rows.length }} 个账号，{{ tasks.running }} 个运行中）</span></span>
            <el-button size="small" :loading="tasks.loading" @click="tasks.refresh(false)">刷新</el-button>
          </div>
        </template>
        <el-table :data="tasks.rows" size="small" max-height="420" empty-text="当前没有任务">
          <el-table-column label="账号" width="150">
            <template #default="{ row }"><span class="mono">{{ row.account }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="轮次进度" min-width="180">
            <template #default="{ row }">
              <el-progress :percentage="progressOf(row)" :stroke-width="6" />
              <span class="muted mono">
                {{ row.current_round_sent }} / {{ row.current_round_planned }} · 轮 {{ row.current_round }}/{{ row.rounds }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="180" fixed="right">
            <template #default="{ row }">
              <el-button link @click="tasks.control(row.task_id, 'pause')">暂停</el-button>
              <el-button link @click="tasks.control(row.task_id, 'resume')">恢复</el-button>
              <el-button link type="danger" @click="tasks.control(row.task_id, 'stop')">停止</el-button>
              <el-button link type="danger" @click="tasks.remove(row.task_id)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <div>
              <div>运行日志 <span class="muted">（每 5 秒自动刷新）</span></div>
              <div class="muted mono" style="font-size: 12px; margin-top: 2px">
                本次执行 · 已群发 {{ pipeline.run?.stats?.send_success ?? 0 }} 条
                · 已加群 {{ pipeline.run?.stats?.join_success ?? 0 }} 个
                <span v-if="pipeline.run && !pipeline.run.running">（已停止，不再累计）</span>
              </div>
            </div>
            <el-space>
              <el-radio-group v-model="logs.detailed" size="small">
                <el-radio-button :value="false">普通</el-radio-button>
                <el-radio-button :value="true">详细</el-radio-button>
              </el-radio-group>
              <el-button size="small" :loading="logs.loading" @click="logs.refresh(false)">刷新</el-button>
              <el-button size="small" type="danger" plain @click="logs.clear()">清空</el-button>
            </el-space>
          </div>
        </template>
        <el-tabs v-model="logTab">
          <el-tab-pane :label="`群发日志（${logs.rows.length}）`" name="send">
            <el-table :data="logs.rows" size="small" max-height="420" empty-text="暂无群发日志">
              <el-table-column v-if="logs.detailed" label="TASK_ID" width="130">
                <template #default="{ row }"><span class="mono">{{ row.task_id || '-' }}</span></template>
              </el-table-column>
              <el-table-column v-if="logs.detailed" label="ROUND" width="70">
                <template #default="{ row }">{{ row.task_round ?? '-' }}</template>
              </el-table-column>
              <el-table-column label="账号" width="130">
                <template #default="{ row }"><span class="mono">{{ row.account_name || '-' }}</span></template>
              </el-table-column>
              <el-table-column label="目标" min-width="150">
                <template #default="{ row }">{{ row.group_title || row.group_id || '-' }}</template>
              </el-table-column>
              <el-table-column label="状态" width="100">
                <template #default="{ row }">
                  <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column v-if="logs.detailed" label="解析" width="90">
                <template #default="{ row }">{{ row.parse_mode || 'plain' }}</template>
              </el-table-column>
              <el-table-column label="时间" width="160">
                <template #default="{ row }"><span class="muted">{{ formatTime(row.created_at) }}</span></template>
              </el-table-column>
              <el-table-column label="详情" min-width="150">
                <template #default="{ row }">
                  <span class="muted">{{ row.error ? translateText(row.error) : (row.message_id ? `消息 ID：${row.message_id}` : '-') }}</span>
                </template>
              </el-table-column>
              <el-table-column v-if="logs.detailed" label="内容预览" min-width="200">
                <template #default="{ row }"><span class="muted">{{ row.message_preview || '-' }}</span></template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
          <el-tab-pane :label="`加群日志（${logs.joinRows.length}）`" name="join">
            <el-table :data="logs.joinRows" size="small" max-height="420" empty-text="暂无加群日志">
              <el-table-column label="账号" width="130">
                <template #default="{ row }"><span class="mono">{{ row.account_name || '-' }}</span></template>
              </el-table-column>
              <el-table-column label="目标群" min-width="200">
                <template #default="{ row }">
                  {{ row.group_title || row.group_username || row.group_id || '-' }}
                </template>
              </el-table-column>
              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="时间" width="160">
                <template #default="{ row }"><span class="muted">{{ formatTime(row.created_at) }}</span></template>
              </el-table-column>
              <el-table-column label="详情" min-width="200">
                <template #default="{ row }">
                  <span class="muted">{{ row.error ? translateText(row.error) : '-' }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </div>

    <el-drawer v-model="consoleStore.errorDrawerOpen" title="错误中心" size="420px">
      <p class="muted" style="margin-top: 0">
        只统计后端真实汇报的异常。点「查看」会跳去对应页面，或把群组列表就地筛成异常项。
      </p>
      <div
        v-for="item in consoleStore.errors"
        :key="item.key"
        class="row-between"
        style="padding: 12px 0; border-bottom: 1px solid var(--el-border-color-lighter)"
      >
        <div>
          <strong>{{ item.label }}</strong>
          <div class="muted">{{ item.hint }}</div>
        </div>
        <el-space>
          <el-tag :type="item.count ? 'danger' : 'success'" size="small">{{ item.count }}</el-tag>
          <el-button size="small" :disabled="!item.count" @click="consoleStore.focusError(item.key)">查看</el-button>
        </el-space>
      </div>
    </el-drawer>
  </div>
</template>
