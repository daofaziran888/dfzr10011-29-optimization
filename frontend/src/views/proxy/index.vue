<template>
  <section class="page" data-module="proxy">
    <header class="page-head">
      <div>
        <h2>采集代理路由管理</h2>
        <p class="page-desc">
          维护采集代理的地址配置、区域标签与可用性监控；降级动作会把结果写入采集数据源池的降级切换结果与失败核对清单。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记代理配置</button>
        <button class="btn" type="button" @click="exportRows">导出采集代理路由清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>区域标签</span>
        <input v-model="regionFilter" placeholder="按区域筛选健康采样" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="row.status === '已关闭'"
              :title="row.status === '已关闭' ? '已关闭代理不能再次执行动作' : action"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集代理路由数据，可先登记代理配置</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集代理路由记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="sub-panel">
      <header class="sub-panel-head">
        <h3>代理健康采样</h3>
        <span v-if="healthState === 'ok'" class="sub-note">共 {{ healthTotal }} 条，低于阈值 {{ belowThreshold }} 条</span>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in healthColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in healthRows" :key="`health-${index}`">
            <td v-for="column in healthColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!healthRows.length">
            <td :colspan="healthColumns.length" class="empty-state">
              {{ healthState === 'failed' ? `读取失败：${healthMessage}` : (healthMessage || '暂无代理健康采样') }}
            </td>
          </tr>
        </tbody>
      </table>
      <p v-if="actionMessage" :class="actionOk ? 'ok-text' : 'error-text'" class="sub-note">{{ actionMessage }}</p>
    </section>

    <DegradeWorklist
      title="代理降级切换协同视图"
      description="与采集数据源池共用同一份降级切换失败核对清单与切换结果，降级动作完成后这里同步刷新。"
      :key="worklistKey"
    />
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'
import DegradeWorklist from '@/components/DegradeWorklist.vue'

type Row = Record<string, string | number | null>

type Worklist = {
  items: Row[]
  total: number
  state: 'ok' | 'empty' | 'failed'
  message: string
  summary: Record<string, number>
}

const ENDPOINT = '/api/proxy'
const columns = ["配置编号", "代理地址", "代理协议", "区域标签", "可用比例", "切换阈值", "响应延迟", "配置状态"]
const actions = ["启用代理", "降级代理", "关闭代理"]
const filterFields = columns.slice(0, 3)
const healthColumns = ["区域标签", "代理配置编号", "采样时间", "可用比例", "响应延迟", "切换阈值", "采样结论"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const regionFilter = ref('')
const stats = ref([
  { label: '代理配置总数', value: 0 },
  { label: '已启用代理', value: 0 },
  { label: '已降级代理', value: 0 },
  { label: '已关闭代理', value: 0 },
])

const healthRows = ref<Row[]>([])
const healthTotal = ref(0)
const healthState = ref<'ok' | 'empty' | 'failed'>('ok')
const healthMessage = ref('')
const belowThreshold = ref(0)
const actionMessage = ref('')
const actionOk = ref(true)
const worklistKey = ref(0)

function resetFilters() {
  filters.value = {}
  regionFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '代理配置登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  actionMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    actionOk.value = Boolean(payload?.ok)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '采集代理路由动作未生效，请稍后重试')
    }
    actionMessage.value = payload.message
    worklistKey.value += 1
    await reload()
  } catch (error) {
    actionOk.value = false
    errorMessage.value = error instanceof Error ? error.message : '采集代理路由操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('代理配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => row.status === '已启用').length
    stats.value[2].value = rows.value.filter((row) => row.status === '已降级').length
    stats.value[3].value = rows.value.filter((row) => row.status === '已关闭').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集代理路由列表读取失败'
  }
  await reloadHealth()
}

async function reloadHealth() {
  healthMessage.value = ''
  healthState.value = 'ok'
  const params = new URLSearchParams()
  if (regionFilter.value) params.set('region', regionFilter.value)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  try {
    const payload = await fetchJson<Worklist>(`${ENDPOINT}/health-samples${suffix}`)
    healthRows.value = payload.items ?? []
    healthTotal.value = payload.total ?? healthRows.value.length
    healthState.value = payload.state ?? (healthRows.value.length ? 'ok' : 'empty')
    healthMessage.value = payload.message ?? ''
    belowThreshold.value = payload.summary?.['低于阈值'] ?? 0
  } catch (error) {
    healthRows.value = []
    healthTotal.value = 0
    belowThreshold.value = 0
    healthState.value = 'failed'
    healthMessage.value = error instanceof Error ? error.message : '健康采样读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.sub-panel { margin-top: 20px; }
.sub-panel-head { display: flex; align-items: baseline; gap: 12px; }
.sub-panel-head h3 { font-size: 15px; margin: 0 0 8px; }
.sub-note { color: var(--muted); font-size: 12px; }
.ok-text { color: #067647; }
.link:disabled { color: #9aa4b2; cursor: not-allowed; }
</style>
