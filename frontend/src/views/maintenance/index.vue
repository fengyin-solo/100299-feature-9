<template>
  <section class="page" data-module="maintenance">
    <header class="page-head">
      <div>
        <h2>维保记录管理</h2>
        <p class="page-desc">维护维保记录，围绕维保编号、维保设备、维保单位、维保内容做登记、筛选、批量导入导出与状态流转。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openImport">导入维保清单</button>
        <button class="btn" type="button" @click="exportRows">导出维保记录清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>维保编号</span>
        <input v-model="keyword" placeholder="按维保编号检索" />
      </label>
      <label class="filter-item">
        <span>维保单位</span>
        <input v-model="unit" placeholder="按维保单位检索" />
      </label>
      <label class="filter-item">
        <span>维保状态</span>
        <select v-model="status">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <div v-if="importPanel" class="import-panel">
      <div class="import-head">
        <strong>导入外委单位维保清单</strong>
        <button class="link" type="button" @click="closeImport">收起</button>
      </div>
      <p class="import-hint">
        支持 CSV（首行表头：维保编号、维保设备、维保单位、维保内容、维保日期、维保人员、更换部件、维保状态）。
        同一维保编号的多行会合并成一条；缺维保单位的整行退回并标注行号；维保设备与维保内容对不上时整批拒绝。
      </p>
      <div class="import-controls">
        <input ref="fileInput" type="file" accept=".csv,.txt,.json" @change="onFileChange" />
        <button class="btn primary" type="button" :disabled="!importContent || importing" @click="startImport">
          {{ importing ? '导入中…' : '开始导入' }}
        </button>
        <button
          v-if="resumeSessionId"
          class="btn"
          type="button"
          :disabled="importing"
          @click="resumeImport"
        >
          从第 {{ resumeNextRow }} 行续导
        </button>
        <button
          v-if="resumeSessionId"
          class="btn ghost" type="button" @click="discardSession"
        >放弃续导</button>
      </div>
      <div v-if="importContent" class="import-preview">已读取 {{ importContent.split(/\r?\n/).filter(Boolean).length - 1 }} 行数据（不含表头）</div>
      <ul v-if="importErrors.length" class="import-errors">
        <li v-for="(item, index) in importErrors" :key="index">{{ item }}</li>
      </ul>
      <p v-if="importMessage" class="import-message" :class="{ 'error-text': !importOk }">{{ importMessage }}</p>
    </div>

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
          <td :colspan="columns.length + 1" class="empty-state">暂无维保记录数据，可先导入或登记维保记录</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条维保记录</span>
      <span v-if="infoMessage" class="info-text">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
interface ImportResponse {
  ok: boolean
  message: string
  session_id: string | null
  done: boolean
  next_row: number
  processed_rows: number
  total_rows: number
  added: number
  updated: number
  rejected_rows: number
  batch_rejected: boolean
  reject_errors: string[]
}

const ENDPOINT = '/api/maintenance'
const columns = ["维保编号", "维保设备", "维保单位", "维保内容", "维保日期", "维保人员", "更换部件", "维保状态"]
const actions = ["安排维保", "开始维保", "返工登记"]
const statuses = ["待维保", "维保中", "已完成", "需返工"]
const stats = [{"label": "待维保设备", "value": 0}, {"label": "维保中设备", "value": 0}, {"label": "需返工设备", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const keyword = ref('')
const unit = ref('')
const status = ref('')

const importPanel = ref(false)
const importContent = ref('')
const importing = ref(false)
const importMessage = ref('')
const importOk = ref(true)
const importErrors = ref<string[]>([])
const resumeSessionId = ref('')
const resumeNextRow = ref(0)
const fileInput = ref<HTMLInputElement | null>(null)

function currentParams(): URLSearchParams {
  const params = new URLSearchParams()
  if (keyword.value.trim()) params.set('keyword', keyword.value.trim())
  if (unit.value.trim()) params.set('unit', unit.value.trim())
  if (status.value) params.set('status', status.value)
  return params
}

function resetFilters() {
  keyword.value = ''
  unit.value = ''
  status.value = ''
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const query = currentParams().toString()
    const response = await request(`${ENDPOINT}/export${query ? `?${query}` : ''}`)
    if (!response.ok) {
      throw new Error('维保清单导出失败')
    }
    const payload = await response.json()
    // 导出条数与页面筛选口径一致，并在文件名上带上维保单位，避免导错对象
    const blob = new Blob([JSON.stringify(payload.items, null, 2)], { type: 'application/json;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const scope = unit.value.trim() ? `_${unit.value.trim()}` : '全部单位'
    link.href = url
    link.download = `维保清单${scope}_${payload.total}条.json`
    link.click()
    URL.revokeObjectURL(url)
    infoMessage.value = `已按当前筛选导出 ${payload.total} 条（与页面一致）`
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维保清单导出失败'
  }
}

function openImport() {
  importPanel.value = true
}

function closeImport() {
  importPanel.value = false
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) {
    return
  }
  const reader = new FileReader()
  reader.onload = () => {
    importContent.value = String(reader.result ?? '')
    importMessage.value = ''
    importErrors.value = []
  }
  reader.readAsText(file, 'utf-8')
}

async function postImport(body: Record<string, unknown>): Promise<ImportResponse> {
  const response = await request(`${ENDPOINT}/import`, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) {
    throw new Error('维保清单导入失败')
  }
  return (await response.json()) as ImportResponse
}

function applyImportResult(result: ImportResponse) {
  importOk.value = result.ok
  importMessage.value = result.message
  importErrors.value = result.reject_errors ?? []
  if (result.done) {
    resumeSessionId.value = ''
    resumeNextRow.value = 0
    importContent.value = ''
    if (fileInput.value) {
      fileInput.value.value = ''
    }
    void reload()
  } else {
    resumeSessionId.value = result.session_id ?? ''
    resumeNextRow.value = result.next_row
  }
}

async function startImport() {
  importing.value = true
  importMessage.value = ''
  importErrors.value = []
  resumeSessionId.value = ''
  try {
    applyImportResult(await postImport({ content: importContent.value }))
  } catch (error) {
    importOk.value = false
    importMessage.value = error instanceof Error ? error.message : '维保清单导入失败'
  } finally {
    importing.value = false
  }
}

async function resumeImport() {
  if (!resumeSessionId.value) {
    return
  }
  importing.value = true
  try {
    const response = await request(`${ENDPOINT}/import/resume`, {
      method: 'POST',
      body: JSON.stringify({ session_id: resumeSessionId.value }),
    })
    if (!response.ok) {
      throw new Error('续导失败，请重新上传维保清单')
    }
    applyImportResult((await response.json()) as ImportResponse)
  } catch (error) {
    importOk.value = false
    importMessage.value = error instanceof Error ? error.message : '续导失败'
  } finally {
    importing.value = false
  }
}

function discardSession() {
  resumeSessionId.value = ''
  resumeNextRow.value = 0
  importMessage.value = ''
  importErrors.value = []
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
  infoMessage.value = ''
  const query = currentParams().toString()
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
