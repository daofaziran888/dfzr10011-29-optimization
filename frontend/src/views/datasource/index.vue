<template>
  <section class="page" data-module="datasource">
    <header class="page-head">
      <div>
        <h2>采集数据源池管理</h2>
        <p class="page-desc">
          维护采集数据源的协议配置、优先级排序与主备流量；代理降级切换的结果、失败核对与主备清单都在这里统一出数。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记数据源条目</button>
        <button class="btn" type="button" @click="exportRows">导出采集数据源池清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <nav class="tab-bar" aria-label="采集数据源池视图">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <template v-if="activeTab === 'entries'">
      <form class="filter-bar" @submit.prevent="reloadEntries">
        <label v-for="field in filterFields" :key="field" class="filter-item">
          <span>{{ field }}</span>
          <input v-model="filters[field]" :placeholder="`按${field}检索`" />
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
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无采集数据源池数据，可先登记数据源条目</td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span>共 {{ total }} 条采集数据源池记录</span>
        <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      </footer>
    </template>

    <template v-else>
      <form class="filter-bar" @submit.prevent="reloadWorklist">
        <label class="filter-item">
          <span>区域标签</span>
          <input v-model="workFilters.region" placeholder="按区域标签检索" />
        </label>
        <label v-if="supportsVersion" class="filter-item">
          <span>降级版本</span>
          <input v-model="workFilters.version" placeholder="按降级版本检索" />
        </label>
        <label v-if="activeTab === 'failures'" class="filter-item">
          <span>复核状态</span>
          <select v-model="workFilters.reviewStatus">
            <option value="">全部</option>
            <option value="待复核">待复核</option>
            <option value="已复核">已复核</option>
            <option value="已排除">已排除</option>
          </select>
        </label>
        <button class="btn" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="resetWorkFilters">重置条件</button>
        <button
          v-if="activeTab === 'failures' || activeTab === 'results'"
          class="btn"
          type="button"
          @click="triggerRetry"
        >
          按版本重试续做
        </button>
      </form>

      <div v-if="workSummary" class="summary-row">
        <span v-for="(value, label) in workSummary" :key="label" class="summary-chip">
          {{ label }}：{{ value }}
        </span>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in activeColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in workRows" :key="`${row['区域标签'] || row['降级版本'] || 'row'}-${index}`">
            <td v-for="column in activeColumns" :key="column">{{ display(row[column]) }}</td>
          </tr>
          <tr v-if="!workRows.length">
            <td :colspan="activeColumns.length || 1" class="empty-state">
              {{ workState === 'failed' ? `读取失败：${workMessage}` : (workMessage || '暂无数据') }}
            </td>
          </tr>
        </tbody>
      </table>

      <footer class="page-foot">
        <span v-if="workState === 'ok'">共 {{ workTotal }} 条记录</span>
        <span v-else-if="workState === 'empty'">空态：{{ workMessage }}</span>
        <span v-else>失败：{{ workMessage }}</span>
        <span v-if="actionMessage" :class="actionOk ? 'ok-text' : 'error-text'">{{ actionMessage }}</span>
      </footer>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>

type Worklist = {
  items: Row[]
  total: number
  state: 'ok' | 'empty' | 'failed'
  message: string
  summary: Record<string, number>
}

const ENDPOINT = '/api/datasource'
const columns = ["条目编号", "数据源名称", "源类型", "接入协议", "认证方式", "优先级", "区域标签", "主备角色", "当前流量", "最近采集", "条目状态"]
const actions = ["接入源", "暂停源", "移除源"]
const stats = ref([
  { label: '数据源条目总数', value: 0 },
  { label: '已接入源', value: 0 },
  { label: '已暂停源', value: 0 },
])

const tabs = [
  { key: 'entries', label: '数据源条目' },
  { key: 'standby', label: '数据源主备清单' },
  { key: 'results', label: '降级切换结果' },
  { key: 'failures', label: '降级切换失败核对' },
  { key: 'diff', label: '代理路由差异' },
]

const WORK_COLUMNS: Record<string, string[]> = {
  standby: ["区域标签", "代理配置编号", "代理状态", "数据源条目编号", "主备角色", "当前流量", "主用线路", "降级版本", "降级状态", "恢复状态", "最近事件"],
  results: ["降级版本", "批次状态", "重试次数", "区域标签", "代理配置编号", "数据源条目编号", "主用线路", "降级状态", "恢复状态", "结果说明", "更新时间"],
  failures: ["核对编号", "降级版本", "区域标签", "代理配置编号", "失败环节", "失败原因", "空路由说明", "复核状态", "更新时间"],
  diff: ["区域标签", "代理配置编号", "代理状态", "数据源条目编号", "数据源状态", "路由结论", "差异说明"],
}

const activeTab = ref('entries')
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const workRows = ref<Row[]>([])
const workTotal = ref(0)
const workState = ref<'ok' | 'empty' | 'failed'>('ok')
const workMessage = ref('')
const workSummary = ref<Record<string, number> | null>(null)
const workFilters = ref({ region: '', version: '', reviewStatus: '' })
const actionMessage = ref('')
const actionOk = ref(true)

const supportsVersion = computed(() => ['results', 'failures'].includes(activeTab.value))
const activeColumns = computed(() => WORK_COLUMNS[activeTab.value] ?? [])

function display(value: Row[string] | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function resetFilters() {
  filters.value = {}
  void reloadEntries()
}

function resetWorkFilters() {
  workFilters.value = { region: '', version: '', reviewStatus: '' }
  void reloadWorklist()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '数据源条目登记入口尚未接入审批流'
}

function switchTab(key: string) {
  activeTab.value = key
  actionMessage.value = ''
  if (key === 'entries') {
    void reloadEntries()
  } else {
    void reloadWorklist()
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '采集数据源池动作未生效，请稍后重试')
    }
    await reloadEntries()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集数据源池操作失败'
  }
}

async function reloadEntries() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('数据源条目列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row) => (row['条目状态'] ?? row.status) === '已接入').length
    stats.value[2].value = rows.value.filter((row) => (row['条目状态'] ?? row.status) === '已暂停').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集数据源池列表读取失败'
  }
}

async function reloadWorklist() {
  workMessage.value = ''
  workState.value = 'ok'
  workSummary.value = null
  const params = new URLSearchParams()
  if (workFilters.value.region) params.set('region', workFilters.value.region)
  if (supportsVersion.value && workFilters.value.version) params.set('version', workFilters.value.version)
  if (activeTab.value === 'failures' && workFilters.value.reviewStatus) {
    params.set('review_status', workFilters.value.reviewStatus)
  }
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const path = activeTab.value === 'diff' ? `${ENDPOINT}/degrade/route-diff` : `${ENDPOINT}/degrade/${activeTab.value}${suffix}`
  try {
    const payload = await fetchJson<Worklist>(path)
    workRows.value = payload.items ?? []
    workTotal.value = payload.total ?? workRows.value.length
    workState.value = payload.state ?? (workRows.value.length ? 'ok' : 'empty')
    workMessage.value = payload.message ?? ''
    workSummary.value = payload.summary && Object.keys(payload.summary).length ? payload.summary : null
  } catch (error) {
    workRows.value = []
    workTotal.value = 0
    workState.value = 'failed'
    workMessage.value = error instanceof Error ? error.message : '清单读取失败'
  }
}

async function triggerRetry() {
  actionMessage.value = ''
  if (!workFilters.value.version) {
    actionOk.value = false
    actionMessage.value = '请先填写降级版本，重试需要按版本确定续做范围'
    return
  }
  try {
    const response = await request(`${ENDPOINT}/degrade/retry`, {
      method: 'POST',
      body: JSON.stringify({ 降级版本: workFilters.value.version }),
    })
    const payload = await response.json().catch(() => null)
    actionOk.value = Boolean(payload?.ok)
    actionMessage.value = payload?.message || (response.ok ? '重试已提交' : '重试中断，原因未返回')
    await reloadWorklist()
  } catch (error) {
    actionOk.value = false
    actionMessage.value = error instanceof Error ? error.message : '重试中断，请稍后再试'
  }
}

onMounted(reloadEntries)
</script>

<style scoped>
.tab-bar { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.tab.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.summary-row { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }
.summary-chip { background: #eef4ff; color: #1f3f77; border-radius: 999px; padding: 3px 10px; font-size: 12px; }
.ok-text { color: #067647; }
.filter-item select { padding: 4px 6px; border: 1px solid var(--border); border-radius: 4px; }
</style>
