<template>
  <section class="degrade-worklist">
    <header class="degrade-head">
      <div>
        <h3>{{ title }}</h3>
        <p class="degrade-desc">{{ description }}</p>
      </div>
      <form class="degrade-filter" @submit.prevent="reload">
        <label>
          <span>区域标签</span>
          <input v-model="region" placeholder="按区域筛选" />
        </label>
        <label>
          <span>降级版本</span>
          <input v-model="version" placeholder="按降级版本筛选" />
        </label>
        <button class="btn" type="submit">刷新</button>
      </form>
    </header>

    <div class="degrade-block">
      <h4>降级切换失败核对清单</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in failureColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in failures" :key="`failure-${index}`">
            <td v-for="column in failureColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!failures.length">
            <td :colspan="failureColumns.length" class="empty-state">
              {{ failureState === 'failed' ? `读取失败：${failureMessage}` : (failureMessage || '暂无失败记录') }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="degrade-block">
      <h4>降级切换结果</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="column in resultColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in results" :key="`result-${index}`">
            <td v-for="column in resultColumns" :key="column">{{ row[column] ?? '—' }}</td>
          </tr>
          <tr v-if="!results.length">
            <td :colspan="resultColumns.length" class="empty-state">
              {{ resultState === 'failed' ? `读取失败：${resultMessage}` : (resultMessage || '暂无切换结果') }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Row = Record<string, string | number | null>

type Worklist = {
  items: Row[]
  total: number
  state: 'ok' | 'empty' | 'failed'
  message: string
  summary: Record<string, number>
}

withDefaults(defineProps<{
  title?: string
  description?: string
}>(), {
  title: '代理降级切换协同视图',
  description: '与采集数据源池共用同一份降级切换失败清单与切换结果，避免两个页面各自维护一套口径。',
})

// 统一走数据源模块的接口，不在这里复制业务规则。
const ENDPOINT = '/api/datasource/degrade'
const failureColumns = ["核对编号", "降级版本", "区域标签", "代理配置编号", "失败环节", "失败原因", "复核状态"]
const resultColumns = ["降级版本", "批次状态", "重试次数", "区域标签", "主用线路", "降级状态", "恢复状态", "结果说明"]

const region = ref('')
const version = ref('')
const failures = ref<Row[]>([])
const failureState = ref<'ok' | 'empty' | 'failed'>('ok')
const failureMessage = ref('')
const results = ref<Row[]>([])
const resultState = ref<'ok' | 'empty' | 'failed'>('ok')
const resultMessage = ref('')

function buildQuery(): string {
  const params = new URLSearchParams()
  if (region.value) params.set('region', region.value)
  if (version.value) params.set('version', version.value)
  const query = params.toString()
  return query ? `?${query}` : ''
}

async function loadOne(
  path: string,
  target: typeof failures,
  state: typeof failureState,
  message: typeof failureMessage,
) {
  try {
    const payload = await fetchJson<Worklist>(path)
    target.value = payload.items ?? []
    state.value = payload.state ?? (target.value.length ? 'ok' : 'empty')
    message.value = payload.message ?? ''
  } catch (error) {
    target.value = []
    state.value = 'failed'
    message.value = error instanceof Error ? error.message : '清单读取失败'
  }
}

async function reload() {
  const query = buildQuery()
  await Promise.all([
    loadOne(`${ENDPOINT}/failures${query}`, failures, failureState, failureMessage),
    loadOne(`${ENDPOINT}/results${query}`, results, resultState, resultMessage),
  ])
}

onMounted(reload)
</script>

<style scoped>
.degrade-worklist { margin-top: 20px; }
.degrade-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap; }
.degrade-head h3 { font-size: 15px; margin: 0; }
.degrade-desc { color: var(--muted); font-size: 12px; margin: 4px 0 8px; }
.degrade-filter { display: flex; gap: 10px; align-items: flex-end; }
.degrade-filter span { display: block; font-size: 12px; color: var(--muted); }
.degrade-block { margin-top: 12px; }
.degrade-block h4 { font-size: 13px; margin: 0 0 6px; color: #344054; }
</style>
