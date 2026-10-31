<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAccountsStore } from '@/stores/accounts'
import { useJoinedGroupsStore } from '@/stores/joinedGroups'

const accounts = useAccountsStore()
const joinedGroups = useJoinedGroupsStore()

const keyword = ref('')

const filteredRows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return joinedGroups.rows
  return joinedGroups.rows.filter((row) =>
    String(row.title || '').toLowerCase().includes(kw)
    || String(row.username || '').toLowerCase().includes(kw)
    || String(row.id).includes(kw))
})

const selectedRow = computed(() => accounts.rows.find((r) => r.account === accounts.selected) || null)

const emptyHint = computed(() => {
  if (joinedGroups.error) return joinedGroups.error
  if (!accounts.selected) return '请先在上方选择一个账号'
  if (selectedRow.value && !selectedRow.value.authorized) {
    return `账号 ${accounts.selected} 当前未授权，需要先到「账号管理」登录，或到「健康检查」确认状态`
  }
  if (keyword.value.trim() && joinedGroups.rows.length) return '没有匹配这个关键词的群组'
  return '这个账号还没有可用群组数据，可点击「刷新」重新从 Telegram 拉取'
})

function onAccountChange(value: string): void {
  accounts.select(value)
  keyword.value = ''
  void joinedGroups.refresh(value)
}

function onOnlyGroupsChange(): void {
  void joinedGroups.refresh(accounts.selected)
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
        <h2>已加入群组</h2>
        <p>
          这里展示的是「某个账号自己已经加入了哪些群」，只读查看，与要投放的「目标群组池」无关；
          目标群组池在全局控制台里导入和维护。
        </p>
      </div>
      <el-space>
        <span class="muted">账号</span>
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
            群组列表
            <span class="muted">（{{ filteredRows.length }} / {{ joinedGroups.rows.length }} 条）</span>
          </span>
          <el-space wrap>
            <el-input v-model="keyword" placeholder="筛选群名 / 用户名 / ID" clearable style="width: 220px" />
            <span class="muted">只显示群组（不含频道）</span>
            <el-switch v-model="joinedGroups.onlyGroups" @change="onOnlyGroupsChange" />
            <el-button size="small" type="primary" :loading="joinedGroups.loading" @click="joinedGroups.refresh(accounts.selected, true)">
              刷新
            </el-button>
            <el-button size="small" @click="clearCache()">清理缓存</el-button>
          </el-space>
        </div>
      </template>

      <el-alert
        :type="joinedGroups.error ? 'error' : 'info'"
        :closable="false"
        :title="joinedGroups.summary"
        style="margin-bottom: 12px"
      />

      <el-empty v-if="!joinedGroups.loading && !filteredRows.length" :description="emptyHint" />
      <el-table
        v-else
        v-loading="joinedGroups.loading"
        :data="filteredRows"
        size="small"
        max-height="520"
        row-key="id"
        empty-text="没有数据"
      >
        <el-table-column label="群名" min-width="240">
          <template #default="{ row }">
            <strong>{{ row.title || row.id }}</strong>
            <span v-if="row.title" class="muted"> · ID {{ row.id }}</span>
          </template>
        </el-table-column>
        <el-table-column label="用户名" min-width="160">
          <template #default="{ row }"><span class="mono">{{ row.username || '-' }}</span></template>
        </el-table-column>
        <el-table-column label="人数" width="110">
          <template #default="{ row }"><span class="mono">{{ row.member_count ?? '-' }}</span></template>
        </el-table-column>
        <el-table-column label="可发送" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.can_send === false" type="danger" size="small">不可发送</el-tag>
            <el-tag v-else-if="row.can_send === true" type="success" size="small">可发送</el-tag>
            <el-tag v-else type="info" size="small">未知</el-tag>
          </template>
        </el-table-column>
      </el-table>

      <p class="muted" style="margin: 12px 0 0">
        数据来自该账号自己的会话列表，带有缓存；勾选「只显示群组」会切换数据范围，点「刷新」强制重新拉取。
      </p>
    </el-card>
  </div>
</template>
