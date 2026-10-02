<template>
  <section class="page" data-module="maintenance">
    <header class="page-head">
      <div>
        <h2>维保记录管理</h2>
        <p class="page-desc">维护维保记录，围绕维保编号、维保设备、维保单位、维保内容做登记、筛选与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记维保记录</button>
        <button class="btn" type="button" @click="toggleImport">导入维保清单</button>
        <button class="btn" type="button" @click="exportRows">导出维保记录清单</button>
      </div>
    </header>

    <section v-if="importVisible" class="import-panel">
      <p class="page-desc">
        把外委单位交来的表格内容复制粘贴到下面（首行表头，制表符或逗号分隔均可；识别列：{{ importFields.join('、') }}）。
        同一维保编号的多行会合并成一条；缺维保单位的行整行退回并标明行号；维保设备与维保内容对不上时整批拒绝，一行都不写入。
      </p>
      <textarea
        v-model="importText"
        class="import-text"
        rows="6"
        placeholder="维保编号	维保设备	维保单位	维保内容	维保日期	维保人员	更换部件"
      ></textarea>
      <div class="import-actions">
        <button class="btn primary" type="button" :disabled="importing" @click="startImport">
          {{ importing ? '导入中…' : '开始导入' }}
        </button>
        <button v-if="canResume" class="btn" type="button" :disabled="importing" @click="resumeImport">
          从第 {{ nextRow }} 行继续
        </button>
        <button class="btn ghost" type="button" @click="resetImport">清空</button>
      </div>
      <p v-if="importMessage" class="import-message">{{ importMessage }}</p>
      <ul v-if="importErrors.length" class="import-issues">
        <li v-for="(issue, index) in importErrors" :key="index">{{ issue }}</li>
      </ul>
      <ul v-if="rejectedRows.length" class="import-issues">
        <li v-for="item in rejectedRows" :key="item.row">第 {{ item.row }} 行：{{ item.reason }}</li>
      </ul>
    </section>

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
          <td :colspan="columns.length + 1" class="empty-state">暂无维保记录数据，可先登记维保记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条维保记录记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type RejectedRow = { row: number; reason: string }

const ENDPOINT = '/api/maintenance'
const columns = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
const actions = ["安排维保", "开始维保", "返工登记"]
const statuses = ["待维保", "维保中", "已完成", "需返工"]
const stats = [{"label": "待维保设备", "value": 0}, {"label": "维保中设备", "value": 0}, {"label": "需返工设备", "value": 0}]
const importFields = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
const IMPORT_CHUNK = 50

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const importVisible = ref(false)
const importText = ref('')
const importing = ref(false)
const importDone = ref(false)
const importMessage = ref('')
const importErrors = ref<string[]>([])
const rejectedRows = ref<RejectedRow[]>([])
const sessionId = ref('')
const nextRow = ref<number | null>(null)

const canResume = computed(() => Boolean(sessionId.value) && !importDone.value && !importing.value)

function resetFilters() {
  filters.value = {}
  void reload()
}

function currentQuery(): string {
  const params = new URLSearchParams()
  for (const [field, value] of Object.entries(filters.value)) {
    if (value && value.trim()) {
      params.set(field, value.trim())
    }
  }
  return params.toString()
}

function exportRows() {
  // 导出与列表共用同一套筛选条件，保证导出条数与页面上看到的总数一致
  const query = currentQuery()
  window.open(`${ENDPOINT}/export${query ? `?${query}` : ''}`, '_blank')
}

function openCreate() {
  errorMessage.value = '维保记录登记入口尚未接入审批流'
}

function toggleImport() {
  importVisible.value = !importVisible
}

function resetImport() {
  importText.value = ''
  importMessage.value = ''
  importErrors.value = []
  rejectedRows.value = []
  sessionId.value = ''
  nextRow.value = null
  importDone.value = false
}

function parseSheet(text: string): Row[] {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  if (!lines.length) {
    return []
  }
  const delimiter = lines[0].includes('\t') ? '\t' : ','
  const header = lines[0].split(delimiter).map((cell) => cell.trim())
  const headerIndex = importFields.map((field) => header.indexOf(field))
  const hasHeader = headerIndex.some((index) => index >= 0)
  return lines.slice(hasHeader ? 1 : 0).map((line) => {
    const cells = line.split(delimiter).map((cell) => cell.trim())
    const row: Row = {}
    importFields.forEach((field, position) => {
      const index = hasHeader ? headerIndex[position] : position
      if (index >= 0 && index < cells.length && cells[index]) {
        row[field] = cells[index]
      }
    })
    return row
  })
}

async function runImport(firstBody: Record<string, unknown>) {
  importing.value = true
  importMessage.value = ''
  importErrors.value = []
  rejectedRows.value = []
  try {
    let body: Record<string, unknown> = firstBody
    for (let guard = 0; guard < 1000; guard += 1) {
      const response = await request(`${ENDPOINT}/import`, {
        method: 'POST',
        body: JSON.stringify(body),
      })
      const result = await response.json()
      if (!response.ok) {
        throw new Error(result.detail ?? '导入接口返回异常')
      }
      sessionId.value = result.session_id ?? sessionId.value
      rejectedRows.value = result.rejected_rows ?? []
      importErrors.value = result.errors ?? []
      importMessage.value = result.message ?? ''
      nextRow.value = result.next_row ?? null
      importDone.value = Boolean(result.done)
      if (!result.ok || result.done) {
        break
      }
      body = { session_id: sessionId.value, chunk_size: IMPORT_CHUNK }
    }
    await reload()
  } catch (error) {
    const detail = error instanceof Error ? error.message : '导入请求失败'
    importMessage.value = sessionId.value
      ? `导入中断：${detail}；已导入的维保编号不会重复，可点击「从第 ${nextRow.value ?? '?'} 行继续」接着走`
      : `导入失败：${detail}`
  } finally {
    importing.value = false
  }
}

function startImport() {
  const sheetRows = parseSheet(importText.value)
  if (!sheetRows.length) {
    importMessage.value = '没有解析到可导入的数据行，请检查粘贴内容'
    return
  }
  sessionId.value = ''
  importDone.value = false
  void runImport({ rows: sheetRows, chunk_size: IMPORT_CHUNK })
}

function resumeImport() {
  if (!sessionId.value) {
    return
  }
  void runImport({ session_id: sessionId.value, chunk_size: IMPORT_CHUNK })
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('维保记录动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维保记录操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = currentQuery()
  try {
    const response = await request(`${ENDPOINT}${query ? `?${query}` : ''}`)
    if (!response.ok) {
      throw new Error('维保记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维保记录列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.import-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 12px;
}
.import-text {
  width: 100%;
  margin: 8px 0;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
  font-family: inherit;
  font-size: 13px;
  resize: vertical;
}
.import-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.import-message {
  font-size: 13px;
  margin: 8px 0 0;
}
.import-issues {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 13px;
  color: #b42318;
}
</style>
