<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { usePipelineStore } from '@/stores/pipeline'
import { useRunLogsStore } from '@/stores/runLogs'
import { formatTime, statusTagType, translateText } from '@/utils/format'

const pipeline = usePipelineStore()
const runLogs = useRunLogsStore()

onMounted(() => {
  void runLogs.refresh()
})

/** 本次执行的时间区间：点开始 → 停止；日志全部落库，这里只做区间统计。 */
const windowText = computed(() => {
  if (!runLogs.meta.started_at) return '还没有执行过任务'
  const start = formatTime(runLogs.meta.started_at)
  if (!runLogs.meta.finished_at) return `${start} 开始 · 进行中`
  return `${start} → ${formatTime(runLogs.meta.finished_at)} · 已停止`
})
</script>

<template>
  <div class="page-head">
    <div>
      <h2>运行日志</h2>
      <p>本次任务的群发日志与加群日志 · {{ windowText }}</p>
    </div>
    <el-button :loading="runLogs.meta.loading" @click="runLogs.refresh()">刷新</el-button>
  </div>

  <div class="stack">
    <div class="kpi-grid">
      <div class="kpi-card kpi-card--ok"><span>群发成功</span><strong>{{ pipeline.run?.stats?.send_success ?? 0 }}</strong></div>
      <div class="kpi-card kpi-card--bad"><span>群发失败</span><strong>{{ pipeline.run?.stats?.send_failed ?? 0 }}</strong></div>
      <div class="kpi-card"><span>新加群</span><strong>{{ pipeline.run?.stats?.join_success ?? 0 }}</strong></div>
    </div>

    <el-card shadow="never">
      <el-tabs v-model="runLogs.kind">
        <el-tab-pane :label="`群发日志（${runLogs.send.total}）`" name="send">
          <el-table :data="runLogs.send.items" size="small" max-height="620" empty-text="本次任务还没有群发日志">
            <el-table-column label="账号" width="130">
              <template #default="{ row }"><span class="mono">{{ row.account_name || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="目标" min-width="200">
              <template #default="{ row }">{{ row.group_title || row.group_id || '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="失败原因" min-width="200">
              <template #default="{ row }"><span class="muted">{{ row.error || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="时间" width="170">
              <template #default="{ row }"><span class="mono">{{ formatTime(row.created_at) }}</span></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
        <el-tab-pane :label="`加群日志（${runLogs.join.total}）`" name="join">
          <el-table :data="runLogs.join.items" size="small" max-height="620" empty-text="本次任务还没有加群日志">
            <el-table-column label="账号" width="130">
              <template #default="{ row }"><span class="mono">{{ row.account_name || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="群组" min-width="200">
              <template #default="{ row }">{{ row.group_title || row.group_id || '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="statusTagType(row.status)" size="small">{{ translateText(row.status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="说明" min-width="200">
              <template #default="{ row }"><span class="muted">{{ row.error || '-' }}</span></template>
            </el-table-column>
            <el-table-column label="时间" width="170">
              <template #default="{ row }"><span class="mono">{{ formatTime(row.created_at) }}</span></template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>

      <p class="muted" style="margin-top: 12px">
        日志全部保存在数据库里、一条不删。这里只按「本次执行」的时间区间统计，
        所以每启动一次任务都从 0 开始，之前几次的日志仍可回查。
      </p>
    </el-card>
  </div>
</template>
