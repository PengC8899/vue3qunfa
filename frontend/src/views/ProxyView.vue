<script setup lang="ts">
import { onMounted } from 'vue'
import { ElMessageBox } from 'element-plus'
import { useProxyStore } from '@/stores/proxies'
import { formatProxy, translateText } from '@/utils/format'

const proxies = useProxyStore()

async function confirmUnassign(account: string): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定解绑账号 ${account} 的代理？该账号之后会直连出口，风险更高。`,
      '确认解绑',
      { type: 'warning', confirmButtonText: '解绑', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await proxies.unassign(account)
}

async function confirmUnassignAll(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确定清空全部代理绑定？当前已绑定 ${proxies.bound} 个账号，清空后都会恢复直连。`,
      '确认清空全部绑定',
      { type: 'warning', confirmButtonText: '清空', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  await proxies.unassignAll()
}

onMounted(() => {
  if (!proxies.items.length) void proxies.refresh(false)
})
</script>

<template>
  <div class="stack">
    <div class="page-head">
      <div>
        <h2>代理管理</h2>
        <p>为每个账号分配独立代理，上传 Session 与开始任务时按代理策略校验</p>
      </div>
      <el-button :loading="proxies.loading" @click="proxies.refresh(false)">刷新</el-button>
    </div>

    <div class="kpi-grid">
      <div class="kpi-card">
        <span>账号总数</span>
        <strong>{{ proxies.total }}</strong>
      </div>
      <div class="kpi-card">
        <span>已绑定</span>
        <strong>{{ proxies.bound }}</strong>
      </div>
      <div class="kpi-card" :class="proxies.unbound > 0 ? 'kpi-card--warn' : ''">
        <span>未绑定</span>
        <strong>{{ proxies.unbound }}</strong>
      </div>
    </div>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>强制一账号一代理</span>
            <el-switch v-model="proxies.enforceOneToOne" @change="proxies.toggleEnforce()" />
          </div>
        </template>
        <p class="muted" style="margin: 0">
          开启后，上传 Session 或开始任务时，只要批次里有账号缺少代理，整批都会被拒绝，需要先在这里把代理分配好。
          关闭时没有代理的账号也能上传和开始任务，只是走本机直连出口，风险更高。
        </p>
        <p class="muted" style="margin: 10px 0 0">
          当前状态：{{ proxies.enforceOneToOne ? '已开启，缺代理的账号无法上传或开始任务' : '已关闭，缺代理的账号仍可上传和开始任务' }}
        </p>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>默认代理源</span>
            <span class="muted">保存后上传账号时会自动用它分配</span>
          </div>
        </template>
        <el-input
          v-model="proxies.sourceText"
          type="textarea"
          :rows="6"
          placeholder="支持 账号|代理、纯代理列表、代理 API 地址、711Proxy 旋转网关（自动派生账号级粘性会话）"
        />
        <div class="row-between" style="margin-top: 10px">
          <span class="muted">每行一条；留空表示上传账号时不自动分配代理。</span>
          <el-button type="primary" @click="proxies.saveSource()">保存默认代理源</el-button>
        </div>
        <div class="row-between" style="margin-top: 10px">
          <span class="muted">{{ proxies.topupText || '后台每 10 分钟自动给缺代理的账号补绑一次' }}</span>
          <el-button :loading="proxies.topping" @click="proxies.topup()">立即补绑缺代理的账号</el-button>
        </div>
      </el-card>
    </div>

    <div class="dual">
      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>批量分配代理</span>
            <span class="muted">按下面选择的账号范围逐个绑定</span>
          </div>
        </template>
        <el-input
          v-model="proxies.assignText"
          type="textarea"
          :rows="6"
          placeholder="粘贴要分配的代理：支持 账号|代理、纯代理列表、代理 API 地址、711Proxy 旋转网关"
        />
        <el-radio-group v-model="proxies.assignTarget" style="margin-top: 12px">
          <el-radio-button value="unbound">只给未绑定的账号（{{ proxies.unbound }}）</el-radio-button>
          <el-radio-button value="all">覆盖全部账号（{{ proxies.total }}）</el-radio-button>
        </el-radio-group>
        <p class="muted" style="margin: 12px 0 0">
          本次需要 {{ proxies.assignPreview.targets }} 个代理，已粘贴 {{ proxies.assignPreview.proxies }} 个唯一代理
        </p>
        <el-alert
          v-if="!proxies.assignPreview.enough"
          style="margin-top: 10px"
          type="warning"
          :closable="false"
          show-icon
          title="代理数量不足，仍可点击「开始分配」，后端只会按实际可用的代理数量分配，剩下的账号保持未绑定。"
        />
        <div class="row-between" style="margin-top: 12px">
          <span v-if="proxies.assignedTotal !== null" class="muted">
            分配完成：成功 {{ proxies.assignedTotal }} 个账号{{ proxies.assignLeft ? `，仍有 ${proxies.assignLeft} 个未分配` : '' }}
          </span>
          <span v-else class="muted">选择「覆盖全部账号」时，已绑定的账号会被新代理替换。</span>
          <el-button type="primary" @click="proxies.assign()">开始分配</el-button>
        </div>
      </el-card>

      <el-card shadow="never">
        <template #header>
          <div class="row-between">
            <span>代理连通性测试</span>
            <el-button size="small" type="primary" :loading="proxies.testing" @click="proxies.test()">
              测试出口 IP
            </el-button>
          </div>
        </template>
        <p class="muted" style="margin: 0">
          用上方「批量分配代理」里粘贴的内容测试；那里为空时使用默认代理源。测试可能比较慢，请耐心等待。
        </p>
        <el-alert
          v-if="proxies.probeTotal > proxies.probes.length"
          style="margin-top: 10px"
          type="warning"
          :closable="false"
          show-icon
          :title="`本次解析出 ${proxies.probeTotal} 个代理，按后端限制只探测了前 ${proxies.probes.length} 个`"
        />
        <el-table :data="proxies.probes" size="small" max-height="360" style="margin-top: 12px" empty-text="还没有测试结果">
          <el-table-column label="代理" min-width="200">
            <template #default="{ row }">
              <span class="mono" style="word-break: break-all">{{ row.proxy }}</span>
            </template>
          </el-table-column>
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? '可用' : '不可用' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="出口 IP" width="140">
            <template #default="{ row }">
              <span class="mono">{{ row.exit_ip || '-' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">
              <span class="muted">{{ row.elapsed_ms == null ? '-' : `${row.elapsed_ms} 毫秒` }}</span>
            </template>
          </el-table-column>
          <el-table-column label="错误" min-width="180">
            <template #default="{ row }">
              <span class="muted">{{ translateText(row.error) }}</span>
            </template>
          </el-table-column>
        </el-table>
      </el-card>
    </div>

    <el-card shadow="never">
      <template #header>
        <div class="row-between">
          <span>
            绑定总览
            <span class="muted">（{{ proxies.visibleItems.length }} / {{ proxies.items.length }} 个账号）</span>
          </span>
          <el-space>
            <el-radio-group v-model="proxies.filter">
              <el-radio-button value="all">全部</el-radio-button>
              <el-radio-button value="bound">已绑定</el-radio-button>
              <el-radio-button value="unbound">未绑定</el-radio-button>
            </el-radio-group>
            <el-button type="danger" plain size="small" @click="confirmUnassignAll()">清空全部绑定</el-button>
          </el-space>
        </div>
      </template>
      <el-table :data="proxies.visibleItems" size="small" max-height="480" empty-text="当前筛选下没有账号">
        <el-table-column label="账号" width="180">
          <template #default="{ row }">
            <span class="mono">{{ row.account }}</span>
          </template>
        </el-table-column>
        <el-table-column label="是否绑定" width="110">
          <template #default="{ row }">
            <el-tag :type="row.has_proxy ? 'success' : 'info'" size="small">
              {{ row.has_proxy ? '已绑定' : '未绑定' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="代理" min-width="220">
          <template #default="{ row }">
            <span class="mono" style="word-break: break-all">{{ formatProxy(row.proxy) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="danger" :disabled="!row.has_proxy" @click="confirmUnassign(row.account)">
              解绑
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
