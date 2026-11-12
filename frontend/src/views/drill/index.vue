<template>
  <section class="page" data-module="drill">
    <header class="page-head">
      <div>
        <h2>多维下钻管理</h2>
        <p class="page-desc">维护多维下钻路径的输入、执行约束与结果记录，为数据探查提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记下钻路径</button>
        <button class="btn" type="button" @click="exportRows">导出多维下钻清单</button>
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
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <p v-if="notice" class="notice" :class="notice.ok ? 'notice-ok' : 'notice-bad'">
      {{ notice.message }}
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行性</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ displayValue(row, column) }}</td>
          <td>
            <span :class="row.executable ? 'badge badge-ok' : 'badge badge-bad'">
              {{ row.executable ? '可执行' : '不可执行' }}
            </span>
          </td>
          <td class="row-actions">
            <button v-for="action in actions" :key="action" class="link" type="button" @click="runAction(action, row)">
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无多维下钻数据，可先登记下钻路径</td>
        </tr>
      </tbody>
    </table>

    <section v-if="detail" class="detail-panel">
      <header class="detail-head">
        <h3>路径详情 #{{ detail.id }}（版本 {{ detail.version }}）</h3>
        <button class="btn ghost" type="button" @click="closeDetail">关闭</button>
      </header>

      <p v-if="detail.last_action" class="detail-action" :class="detail.last_action.ok ? 'notice-ok' : 'notice-bad'">
        最近动作：{{ detail.last_action.action }} · {{ detail.last_action.message }}
      </p>

      <table class="data-table">
        <thead>
          <tr><th>字段</th><th>实际接纳值</th></tr>
        </thead>
        <tbody>
          <tr>
            <td>层级维度列表</td>
            <td>
              <span v-if="detail['层级维度列表'] === undefined || detail['层级维度列表'] === null">
                字段缺失（存量记录从未填写，不会自动补造层级）
              </span>
              <span v-else-if="!Array.isArray(detail['层级维度列表']) || detail['层级维度列表'].length === 0">
                显式空列表（已确认没有层级，不能启用）
              </span>
              <span v-else>{{ displayValue(detail, '层级维度列表') }}</span>
            </td>
          </tr>
          <tr v-for="field in columns.filter((item) => item !== '层级维度列表')" :key="field">
            <td>{{ field }}</td>
            <td>{{ displayValue(detail, field) }}</td>
          </tr>
          <tr>
            <td>状态</td>
            <td>{{ detail.status }}</td>
          </tr>
          <tr>
            <td>可执行性</td>
            <td>
              {{ detail.executable ? '可执行（层级与聚合指标均已确认）' : '不可执行' }}
              <span v-if="!detail.executable && detail.validation?.enable_ready" class="hint-text">
                （字段已齐全但尚未启用）
              </span>
            </td>
          </tr>
          <tr>
            <td>层级证据</td>
            <td>{{ levelEvidenceText(detail.validation) }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="detail.validation?.errors?.length" class="reject-block">
        <h4>当前会被拒绝的具体原因</h4>
        <ul>
          <li v-for="issue in detail.validation.errors" :key="`${issue.field}-${issue.code}-${issue.message}`">
            <strong>{{ issue.field }}</strong>：{{ issue.message }}（{{ issue.code }}）
          </li>
        </ul>
      </div>

      <div v-if="detail.validation?.enable_blockers?.length" class="reject-block">
        <h4>启用前必须先补全的证据（启用会被整体拒绝）</h4>
        <ul>
          <li v-for="issue in detail.validation.enable_blockers" :key="`${issue.field}-${issue.code}-${issue.message}`">
            <strong>{{ issue.field }}</strong>：{{ issue.message }}（{{ issue.code }}）
          </li>
        </ul>
      </div>

      <div v-if="detail.validation?.warnings?.length" class="warn-block">
        <h4>待补全提示（不会自动补造层级）</h4>
        <ul>
          <li v-for="issue in detail.validation.warnings" :key="`${issue.field}-${issue.code}-${issue.message}`">
            <strong>{{ issue.field }}</strong>：{{ issue.message }}
          </li>
        </ul>
      </div>

      <div v-if="detail.validation?.聚合指标解析" class="warn-block">
        <h4>聚合指标解析</h4>
        <p>
          形式：{{ detail.validation['聚合指标解析']['形式'] ?? '—' }} · 方案：
          {{ detail.validation['聚合指标解析']['方案编号'] ?? '—' }} · 函数：
          {{ detail.validation['聚合指标解析']['聚合函数'] ?? '—' }} · 字段：
          {{ detail.validation['聚合指标解析']['目标字段'] ?? '—' }} · 状态：
          {{ detail.validation['聚合指标解析']['状态'] ?? '—' }}
        </p>
        <p v-if="detail.validation['聚合指标解析']['reason']">
          {{ detail.validation['聚合指标解析']['reason'] }}
        </p>
      </div>

      <div class="page-actions">
        <button class="btn" type="button" @click="startEdit(detail)">编辑路径（层级/指标）</button>
        <button class="btn ghost" type="button" @click="reload">刷新实际接纳值</button>
      </div>
    </section>

    <section v-if="editorOpen" class="editor-panel">
      <header class="detail-head">
        <h3>{{ editingId === null ? '登记下钻路径' : `编辑下钻路径 #${editingId}` }}</h3>
        <button class="btn ghost" type="button" @click="closeEditor">取消</button>
      </header>
      <p v-if="editingId !== null" class="page-desc">
        基于版本 {{ editingVersion }} 提交；若层级在此期间被他人修改，整份修改会被拒绝，不会只写一半维度。
      </p>

      <form class="editor-form" @submit.prevent="submitEditor">
        <label v-for="field in editorFields" :key="field.key" class="filter-item">
          <span>{{ field.label }}<em v-if="field.required" class="required-mark">*</em></span>
          <select v-if="field.kind === 'direction'" v-model="form[field.key]">
            <option value="">（未填写）</option>
            <option value="升序">升序</option>
            <option value="降序">降序</option>
          </select>
          <input
            v-else
            v-model="form[field.key]"
            :type="field.kind === 'number' ? 'number' : 'text'"
            :placeholder="field.placeholder"
          />
        </label>

        <p class="page-desc">
          层级维度列表按顺序填写，多个维度用逗号分隔。留空视为“从未填写”（不会补造层级）；
          只有显式提交空列表才表示“已确认没有层级”。两种情况都不能启用。
        </p>

        <div class="page-actions">
          <button class="btn primary" type="submit">{{ editingId === null ? '登记' : '保存修改' }}</button>
        </div>
      </form>

      <div v-if="editorErrors.length" class="reject-block">
        <h4>提交被拒绝的具体原因</h4>
        <ul>
          <li v-for="issue in editorErrors" :key="`${issue.field}-${issue.code}-${issue.message}`">
            <strong>{{ issue.field }}</strong>：{{ issue.message }}
          </li>
        </ul>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条多维下钻记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type ValidationIssue = { field: string; code: string; message: string }
type IndicatorParse = {
  形式?: string | null
  方案编号?: string | null
  聚合函数?: string | null
  目标字段?: string | null
  状态?: string | null
  reason?: string | null
}
type Validation = {
  ok: boolean
  executable: boolean
  enable_ready: boolean
  missing_fields: string[]
  levels: string
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  enable_blockers?: ValidationIssue[]
  聚合指标解析?: IndicatorParse | null
}
type Row = {
  id: number
  version: number
  status: string
  executable?: boolean
  validation?: Validation
  last_action?: { action: string; ok: boolean; message: string } | null
  [key: string]: unknown
}
type ActionResult = {
  ok: boolean
  message: string
  entry?: Row | null
  errors?: ValidationIssue[]
}

const ENDPOINT = '/api/drill'
const columns = ["路径编号", "起点维度", "下钻层数", "层级维度列表", "聚合指标", "默认展开层", "排序方向", "最多展示行数"]
const actions = ["提交校验", "启用路径", "停用路径"]
const filterFields = ["路径编号", "起点维度", "下钻层数"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const notice = ref<{ ok: boolean; message: string } | null>(null)

const detail = ref<Row | null>(null)
const editorOpen = ref(false)
const editingId = ref<number | null>(null)
const editingVersion = ref(0)
const editorErrors = ref<ValidationIssue[]>([])
const form = reactive<Record<string, string>>({})

const editorFields = [
  { key: "路径编号", label: "路径编号", required: true, kind: "text", placeholder: "如 DRIL-0004" },
  { key: "起点维度", label: "起点维度", required: true, kind: "text", placeholder: "如 区域" },
  { key: "下钻层数", label: "下钻层数", required: true, kind: "number", placeholder: "如 3" },
  { key: "层级维度列表", label: "层级维度列表", required: false, kind: "text", placeholder: "如 区域，省份，城市" },
  { key: "聚合指标", label: "聚合指标", required: false, kind: "text", placeholder: "如 AGGR-0003 或 SUM(销售额)" },
  { key: "默认展开层", label: "默认展开层", required: false, kind: "number", placeholder: "不得超过已确认层级数" },
  { key: "排序方向", label: "排序方向", required: false, kind: "direction", placeholder: "" },
  { key: "最多展示行数", label: "最多展示行数", required: false, kind: "number", placeholder: "如 20" },
]

const stats = computed(() => [
  { label: "下钻路径总数", value: total.value },
  { label: "已启用路径", value: rows.value.filter((row) => row.status === "已启用").length },
  { label: "待校验路径", value: rows.value.filter((row) => row.status === "待校验").length },
])

function displayValue(row: Record<string, unknown>, column: string): string {
  const value = row[column]
  if (value === undefined || value === null || value === "") return '—'
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => String(item)).join(' → ') : '（显式空列表）'
  }
  return String(value)
}

function levelEvidenceText(validation?: Validation): string {
  if (!validation) return '—'
  if (validation.levels === 'missing') return '字段缺失：历史记录从未填写，不会自动补造层级'
  if (validation.levels === 'empty') return '显式空列表：已确认没有层级，不能启用'
  return '已提供层级维度列表'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function readPayload(response: Response, fallback: string): Promise<ActionResult> {
  let payload: Partial<ActionResult> = {}
  try {
    payload = (await response.json()) as Partial<ActionResult>
  } catch {
    // 后端没有返回 JSON 时保留 HTTP 状态说明。
  }
  if (!response.ok) {
    const detailText = typeof payload.message === 'string' && payload.message ? payload.message : `接口返回 ${response.status}`
    throw new Error(`${fallback}：${detailText}`)
  }
  return {
    ok: Boolean(payload.ok),
    message: typeof payload.message === 'string' && payload.message ? payload.message : fallback,
    entry: (payload.entry as Row | null) ?? null,
    errors: Array.isArray(payload.errors) ? payload.errors : [],
  }
}

function setNotice(ok: boolean, message: string) {
  notice.value = { ok, message }
}

function openCreate() {
  editingId.value = null
  editingVersion.value = 0
  editorErrors.value = []
  Object.keys(form).forEach((key) => delete form[key])
  editorFields.forEach((field) => { form[field.key] = '' })
  editorOpen.value = true
}

function startEdit(entry: Row) {
  editingId.value = entry.id
  editingVersion.value = entry.version
  editorErrors.value = []
  editorFields.forEach((field) => {
    const value = entry[field.key]
    if (Array.isArray(value)) {
      form[field.key] = value.join('，')
    } else {
      form[field.key] = value === undefined || value === null ? '' : String(value)
    }
  })
  editorOpen.value = true
}

function closeEditor() {
  editorOpen.value = false
  editorErrors.value = []
}

function buildValues(): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  values["路径编号"] = form["路径编号"].trim()
  values["起点维度"] = form["起点维度"].trim()
  values["下钻层数"] = form["下钻层数"].trim()
  const levelsText = form["层级维度列表"].trim()
  if (levelsText) {
    // 空输入表示“从未填写”，直接省略字段；只有明确提交 [] 才代表“已确认没有层级”。
    values["层级维度列表"] = levelsText.split(/[,，、;；\n\r]+/).map((item) => item.trim()).filter(Boolean)
  }
  // 聚合指标始终提交：清空输入表示解除绑定，由后端拒绝启用而不是静默保留旧指标。
  values["聚合指标"] = form["聚合指标"].trim()
  if (form["默认展开层"].trim()) values["默认展开层"] = Number(form["默认展开层"].trim())
  if (form["排序方向"].trim()) values["排序方向"] = form["排序方向"].trim()
  if (form["最多展示行数"].trim()) values["最多展示行数"] = Number(form["最多展示行数"].trim())
  return values
}

async function submitEditor() {
  editorErrors.value = []
  errorMessage.value = ''
  const values = buildValues()
  const path = editingId.value === null ? ENDPOINT : `${ENDPOINT}/${editingId.value}`
  const body = editingId.value === null
    ? values
    : { ...values, expected_version: editingVersion.value }

  try {
    const response = await request(path, {
      method: editingId.value === null ? 'POST' : 'PUT',
      body: JSON.stringify(body),
    })
    const payload = await readPayload(response, editingId.value === null ? '下钻路径登记失败' : '下钻路径修改失败')
    editorErrors.value = payload.errors ?? []
    if (!payload.ok) {
      // 被拒绝时保留表单内容，同时回显后端实际接纳值，避免把未保存的值当成已保存。
      setNotice(false, payload.message)
      if (payload.entry) {
        detail.value = payload.entry
      }
      return
    }
    setNotice(true, payload.message)
    editorOpen.value = false
    if (payload.entry) {
      detail.value = payload.entry
    }
    await reload()
  } catch (error) {
    const message = error instanceof Error ? error.message : '下钻路径提交失败'
    errorMessage.value = message
    setNotice(false, message)
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, expected_version: row.version }),
    })
    const payload = await readPayload(response, '多维下钻动作未生效')
    // 后端用 ActionResult.ok 表达业务结果：HTTP 200 也可能是“整体拒绝”。
    setNotice(payload.ok, payload.message)
    if (payload.entry) {
      detail.value = payload.entry
    }
    if (!payload.ok) {
      errorMessage.value = payload.message
    }
    await reload()
  } catch (error) {
    const message = error instanceof Error ? error.message : '多维下钻操作失败'
    errorMessage.value = message
    setNotice(false, message)
  }
}

async function openDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error(`路径详情读取失败：接口返回 ${response.status}`)
    }
    detail.value = (await response.json()) as Row
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '路径详情读取失败'
  }
}

function closeDetail() {
  detail.value = null
}

async function reload() {
  errorMessage.value = ''
  const params: Record<string, string> = {}
  if (filters.value["路径编号"]) params.keyword = filters.value["路径编号"]
  if (filters.value["起点维度"]) params.start_dimension = filters.value["起点维度"]
  if (filters.value["下钻层数"]) params.layer_count = filters.value["下钻层数"]
  const query = new URLSearchParams(params).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('下钻路径列表读取失败')
    }
    const payload = await response.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '多维下钻列表读取失败'
  }
}

onMounted(reload)
</script>
