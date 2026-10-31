<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useAccountsStore } from '@/stores/accounts'
import type { HealthRow } from '@/stores/accounts'
import { statusTagType, translateText } from '@/utils/format'

const accountStore = useAccountsStore()

type FilterMode = 'all' | 'invalid' | 'cannot_send'

const results = ref<HealthRow[]>([])
const checking = ref(false)
const done = ref(0)
const total = ref(0)
const mode = ref<FilterMode>('all')
const keyword = ref('')

const percent = computed(() => (total.value ? Math.round((done.value / total.value) * 100) : 0))

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return results.value.filter((row) => {
    if (mode.value === 'invalid' && row.valid !== false) return false
    if (mode.value === 'cannot_send' && !(row.valid === true && row.can_send_in_groups === false)) return false
    if (kw && !row.account.toLowerCase().includes(kw)) return false
    return true
  })
})

const invalidNames = computed(() => results.value.filter((r) => r.valid === false).map((r) => r.account))
const cannotSendNames = computed(() =>
  results.value.filter((r) => r.valid === true && r.can_send_in_groups === false).map((r) => r.account),
)

async function start(): Promise<void> {
  const names = accountStore.rows.map((r) => r.account)
  if (!names.length) return
  checking.value = true
  total.value = names.length
  done.value = 0
  results.value = names.map((account) => ({ account, status: 'checking' }))

  const queue = names.map((account, index) => ({ account, index }))
  let cursor = 0
  const worker = async (): Promise<void> => {
    while (cursor < queue.length) {
      const item = queue[cursor]
      cursor += 1
      const row = await accountStore.checkOne(item.account)
      results.value[item.index] = row
      done.value += 1
    }
  }

  await Promise.all(Array.from({ length: Math.min(3, queue.length) }, worker))
  checking.value = false
}

async function cleanup(names: string[], label: string): Promise<void> {
  if (!names.length) return
  try {
    await ElMessageBox.confirm(`确定${label}？共 ${names.length} 个账号，删除后不可恢复。`, '批量清理确认', {
      type: 'warning',
    })
  } catch {
    return
  }
  await accountStore.remove(names)
  results.value = results.value.filter((row) => accountStore.rows.some((r) => r.account === row.account))
}

onMounted(() => {
  if (!accountStore.rows.length) void accountStore.refresh()
})
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>健康检查</h2>
        <p>逐个检测账号登录状态与加群/发言权限，检测完成后可一键清理失效账号</p>
      </div>
      <el-space>
        <el-button :loading="accountStore.loading" @click="accountStore.refresh(false)">刷新账号</el-button>
        <el-button type="primary" :loading="checking" :disabled="checking || !accountStore.rows.length" @click="start">
          {{ checking ? '正在检查…' : '开始检查' }}
        </el-button>
      </el-space>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>检查进度 <span class="muted mono">{{ done }} / {{ total }}</span></span>
          <span class="muted">并发 3 个账号，每完成一个立即更新结果</span>
        </div>
      </template>
      <el-progress :percentage="percent" :stroke-width="12" text-inside />
      <p class="muted" style="margin: 10px 0 0">
        账号总数 {{ accountStore.rows.length }} · 已授权 {{ accountStore.stats.authorized }} ·
        已检测 {{ done }} · 失效 {{ invalidNames.length }} · 群内不可发 {{ cannotSendNames.length }}
      </p>
    </el-card>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            检查结果
            <span class="muted">（{{ filtered.length }} / {{ results.length }} 条）</span>
          </span>
          <el-space wrap>
            <el-input v-model="keyword" placeholder="按账号名筛选" clearable style="width: 200px" />
            <el-radio-group v-model="mode">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="invalid">失效账号</el-radio-button>
              <el-radio-button value="cannot_send">不可发群</el-radio-button>
            </el-radio-group>
          </el-space>
        </div>
      </template>

      <el-table :data="filtered" size="small" max-height="460" empty-text="还没有检查结果，点击右上角「开始检查」">
        <el-table-column label="账号" min-width="180">
          <template #default="{ row }"><span class="mono">{{ row.account }}</span></template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="是否可用" width="110">
          <template #default="{ row }">
            <el-tag v-if="row.valid === undefined" type="info" size="small">未检测</el-tag>
            <el-tag v-else :type="row.valid ? 'success' : 'danger'" size="small">{{ row.valid ? '可用' : '不可用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可在群内发言" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.can_send_in_groups === undefined" type="info" size="small">未知</el-tag>
            <el-tag v-else :type="row.can_send_in_groups ? 'success' : 'warning'" size="small">
              {{ row.can_send_in_groups ? '可以' : '不可以' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="可发言群数" width="110">
          <template #default="{ row }">
            <span class="mono">{{ row.sendable_groups ?? '-' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="详情" min-width="200">
          <template #default="{ row }"><span class="muted">{{ translateText(row.detail) }}</span></template>
        </el-table-column>
      </el-table>

      <div class="row-between" style="margin-top: 14px">
        <span class="muted">清理操作会直接从账号库中删除，请确认检查结果无误后再执行。</span>
        <el-space wrap>
          <el-button
            type="danger"
            plain
            :disabled="checking || !invalidNames.length"
            @click="cleanup(invalidNames, '清理全部失效账号')"
          >
            清理失效账号（{{ invalidNames.length }}）
          </el-button>
          <el-button
            type="danger"
            plain
            :disabled="checking || !cannotSendNames.length"
            @click="cleanup(cannotSendNames, '清理群内不可发账号')"
          >
            清理群内不可发账号（{{ cannotSendNames.length }}）
          </el-button>
        </el-space>
      </div>
    </el-card>
  </div>
</template>
