<script setup lang="ts">
/**
 * 根组件：状态编排与布局。
 *
 * 组件划分原则是"谁拥有状态，谁负责传递"：
 *     App 持有全部运行状态（后端元信息、当前结果、执行进度），
 *     子组件只通过 props 收数据、通过 emit 发意图，自己不留业务状态。
 * 这样数据流向是单向的，出问题时只需要在这一个文件里追。
 *
 * 全部网络交互都在这里发起，子组件不直接调接口——
 * 否则一个页面里会有七八处 fetch，加个统一的错误处理都不知道往哪加。
 */

import { computed, onBeforeUnmount, ref } from 'vue'

import {
  FORCE_DEMO,
  fetchHealth,
  fetchIndexStats,
  fetchLLMConfig,
  fetchMeta,
  fetchTables,
  rebuildIndex,
  resetLLMConfig,
  streamQuestion,
  updateLLMConfig,
  type StreamController,
} from '@/api/client'
import {
  DEMO_QUESTION,
  demoHealth,
  demoIndexStats,
  demoLLMConfig,
  demoMeta,
  demoQueryResponse,
  demoTables,
} from '@/api/demo'
import AnswerPanel from '@/components/AnswerPanel.vue'
import AppSidebar from '@/components/AppSidebar.vue'
import ChartView from '@/components/ChartView.vue'
import DataTable from '@/components/DataTable.vue'
import IntentPanel from '@/components/IntentPanel.vue'
import MetricCards from '@/components/MetricCards.vue'
import SqlPanel from '@/components/SqlPanel.vue'
import StageBar from '@/components/StageBar.vue'
import TraceTimeline from '@/components/TraceTimeline.vue'
import ValidationPanel from '@/components/ValidationPanel.vue'
import type {
  HealthResponse,
  IndexStatsResponse,
  LLMConfigResponse,
  LLMConfigUpdateRequest,
  MetaResponse,
  QueryResponse,
  TableInfo,
  TraceItem,
} from '@/types/api'

// ===============================================================
// 状态
// ===============================================================

const demoMode = ref(FORCE_DEMO)
const loading = ref(true)
const backendUnavailable = ref(false)
const backendError = ref('')

const health = ref<HealthResponse | null>(null)
const meta = ref<MetaResponse | null>(null)
const indexStats = ref<IndexStatsResponse | null>(null)
const tables = ref<TableInfo[]>([])

const question = ref('')
const withAnswer = ref(true)
const inputError = ref('')

const running = ref(false)
const stageStatuses = ref<Record<string, 'running' | 'done'>>({})
const trace = ref<TraceItem[]>([])
const result = ref<QueryResponse | null>(null)
const runError = ref('')

const rebuilding = ref(false)
const rebuildMessage = ref('')

const llmConfig = ref<LLMConfigResponse | null>(null)
const llmSaving = ref(false)
const llmError = ref('')

let controller: StreamController | null = null
/** 演示模式的运行令牌，用来让上一次未跑完的演示流程自动作废 */
let demoToken = 0

// ===============================================================
// 派生状态
// ===============================================================

/** 已经跑到了校验阶段 */
const reachedValidation = computed(
  () => stageStatuses.value.validate !== undefined || Boolean(result.value),
)

/** 流程以失败告终 */
const failed = computed(() => Boolean(result.value) && !result.value?.succeeded)

/** 触发过终止节点（重试次数用尽） */
const gaveUp = computed(() => stageStatuses.value.give_up !== undefined)

/** 是否已经开始过一次执行 */
const hasRun = computed(() => Object.keys(stageStatuses.value).length > 0 || Boolean(result.value))

const showWorkspace = computed(() => hasRun.value || running.value)

/** 执行失败的提示文案 */
const failureMessage = computed(() => {
  if (runError.value) return runError.value
  if (result.value && !result.value.succeeded) {
    return result.value.execution.error || '查询未能成功执行。'
  }
  return ''
})

// ===============================================================
// 初始化
// ===============================================================

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms)
  })
}

/** 加载后端的元信息。任何一项失败都不影响其余部分 */
async function bootstrap(): Promise<void> {
  if (FORCE_DEMO) {
    loadDemoData()
    loading.value = false
    return
  }

  loading.value = true
  backendUnavailable.value = false
  backendError.value = ''

  try {
    // 这几个接口互不依赖，并发拉取比串行快得多
    const [healthData, metaData, statsData, tablesData, llmConfigData] = await Promise.all([
      fetchHealth(),
      fetchMeta(),
      fetchIndexStats(),
      fetchTables(),
      fetchLLMConfig(),
    ])

    health.value = healthData
    meta.value = metaData
    indexStats.value = statsData
    tables.value = tablesData
    llmConfig.value = llmConfigData
  } catch (error) {
    backendUnavailable.value = true
    backendError.value = (error as Error).message
  } finally {
    loading.value = false
  }
}

/** 切换到离线演示模式，用内置样例数据把界面撑起来 */
function loadDemoData(): void {
  demoMode.value = true
  health.value = demoHealth
  meta.value = demoMeta
  indexStats.value = demoIndexStats
  tables.value = demoTables
  llmConfig.value = demoLLMConfig
  backendUnavailable.value = false
}

function enterDemoMode(): void {
  loadDemoData()
  question.value = DEMO_QUESTION
  void runDemo()
}

// ===============================================================
// 问答
// ===============================================================

function resetRun(): void {
  stageStatuses.value = {}
  trace.value = []
  result.value = null
  runError.value = ''
}

/**
 * 发起提问。
 *
 * @param text 指定要问的问题；不传则用输入框里的内容
 */
function ask(text?: string): void {
  const target = (text ?? question.value).trim()

  if (!target) {
    inputError.value = '请先输入问题，或从左侧示例中选一个。'
    return
  }

  if (running.value) return

  inputError.value = ''
  question.value = target
  resetRun()

  if (demoMode.value) {
    void runDemo()
    return
  }

  running.value = true

  controller = streamQuestion(target, withAnswer.value, {
    onStage: (event) => {
      stageStatuses.value = { ...stageStatuses.value, [event.node]: event.status }
    },
    onTrace: (item) => {
      trace.value.push(item)
    },
    onResult: (payload) => {
      result.value = payload
    },
    onError: (event) => {
      runError.value = event.message
    },
    onTransportError: (error) => {
      runError.value = error.message
    },
    onDone: () => {
      running.value = false
      controller = null
    },
  })
}

/** 主动中止。用户点了"停止"时不该继续占着连接等结果 */
function stop(): void {
  controller?.close()
  controller = null
  running.value = false

  if (!result.value && !runError.value) {
    runError.value = '已手动停止本次执行。'
  }
}

/**
 * 演示模式下的模拟执行。
 *
 * 为什么要做这个：
 *    演示模式下没有后端，如果点"开始分析"什么都不发生，
 *    界面会显得像是坏了。这里按后端真实的事件顺序把步骤条和轨迹走一遍，
 *    数据和轨迹都来自内置样例，行为与连后端时一致。
 */
async function runDemo(): Promise<void> {
  const token = ++demoToken
  resetRun()
  running.value = true

  const stageSequence = ['parse_intent', 'retrieve_schema', 'generate_sql', 'validate', 'execute']

  for (const node of stageSequence) {
    await sleep(430)
    if (token !== demoToken) return

    stageStatuses.value = { ...stageStatuses.value, [node]: 'done' }

    for (const item of demoQueryResponse.trace.filter((entry) => entry.step === node)) {
      trace.value.push(item)
    }
  }

  await sleep(320)
  if (token !== demoToken) return
  stageStatuses.value = { ...stageStatuses.value, render: 'running' }

  await sleep(900)
  if (token !== demoToken) return
  stageStatuses.value = { ...stageStatuses.value, render: 'done' }

  // 用深拷贝交付，避免结果对象被后续操作意外改动
  result.value = JSON.parse(JSON.stringify(demoQueryResponse)) as QueryResponse
  running.value = false
}

// ===============================================================
// 索引重建
// ===============================================================

async function handleRebuild(): Promise<void> {
  if (rebuilding.value) return

  rebuilding.value = true
  rebuildMessage.value = ''

  try {
    const response = await rebuildIndex(true)
    rebuildMessage.value = response.message
    // 重建后统计会变，必须重新拉一次，否则侧栏还显示旧的就绪状态
    indexStats.value = await fetchIndexStats()
    health.value = await fetchHealth()
  } catch (error) {
    rebuildMessage.value = (error as Error).message
  } finally {
    rebuilding.value = false
  }
}

// ===============================================================
// 模型配置
// ===============================================================

/**
 * 保存模型配置。
 *
 * 保存成功后不再刷新 meta / health：这两个接口会反映配置变化的是
 * 模型摘要与模拟开关，而侧栏已经改由 llmConfig 直接渲染，
 * 多打两个请求只会让"保存中"多转一会儿。
 */
async function handleSaveLLMConfig(payload: LLMConfigUpdateRequest): Promise<void> {
  if (llmSaving.value) return

  llmSaving.value = true
  llmError.value = ''

  try {
    llmConfig.value = await updateLLMConfig(payload)
  } catch (error) {
    llmError.value = (error as Error).message
  } finally {
    llmSaving.value = false
  }
}

/** 恢复为 .env 里的配置 */
async function handleResetLLMConfig(): Promise<void> {
  if (llmSaving.value) return

  llmSaving.value = true
  llmError.value = ''

  try {
    llmConfig.value = await resetLLMConfig()
  } catch (error) {
    llmError.value = (error as Error).message
  } finally {
    llmSaving.value = false
  }
}

// ===============================================================
// 生命周期
// ===============================================================

void bootstrap()

onBeforeUnmount(() => {
  // 组件卸载时必须断开 SSE，否则连接会挂到后端超时为止。
  // 在单页应用里切换路由时这类泄漏很常见
  controller?.close()
  controller = null
})
</script>

<template>
  <div class="layout">
    <AppSidebar
      :health="health"
      :meta="meta"
      :index-stats="indexStats"
      :tables="tables"
      :llm-config="llmConfig"
      :llm-saving="llmSaving"
      :llm-error="llmError"
      :demo-mode="demoMode"
      :loading="loading"
      :rebuilding="rebuilding"
      :current-question="question"
      @pick="ask"
      @rebuild="handleRebuild"
      @save-llm-config="handleSaveLLMConfig"
      @reset-llm-config="handleResetLLMConfig"
    />

    <main class="main">
      <!-- ---------- 标题区 ---------- -->
      <header class="hero">
        <div class="hero__text">
          <h1 class="hero__title">智能数据分析 Agent</h1>
          <p class="hero__desc">
            用中文提问，自动完成<strong>意图解析 → 表结构检索 → SQL 生成 → 安全校验 → 执行查询 → 结论与图表</strong>。
            底层是 LangGraph 状态图，校验或执行失败时会带着原始报错自动重写 SQL。
          </p>
        </div>

        <div class="hero__badges">
          <span v-if="demoMode" class="badge badge--warn">离线演示数据</span>
          <span v-else-if="health?.mock_mode" class="badge badge--info">模拟模型模式</span>
          <span v-else-if="health" class="badge badge--ok">真实模型模式</span>
          <span v-if="backendUnavailable" class="badge badge--err">后端未连接</span>
        </div>
      </header>

      <!-- ---------- 后端不可用提示 ---------- -->
      <div v-if="backendUnavailable && !demoMode" class="alert alert--warn">
        <div class="alert__title">未连接到后端服务</div>
        <p class="alert__body">{{ backendError }}</p>
        <div class="alert__actions">
          <button class="btn btn--sm" type="button" @click="bootstrap">重试连接</button>
          <button class="btn btn--sm btn--primary" type="button" @click="enterDemoMode">
            用内置演示数据预览界面
          </button>
        </div>
      </div>

      <!-- ---------- 提问区 ---------- -->
      <section class="card ask">
        <div class="card-body">
          <label class="ask__label" for="question-input">你的问题</label>

          <textarea
            id="question-input"
            v-model="question"
            class="ask__input"
            rows="2"
            maxlength="500"
            placeholder="例如：最近六个月每个月的销售额是多少？"
            @keydown.enter.exact.prevent="ask()"
          />

          <div class="ask__footer">
            <label class="ask__switch">
              <input v-model="withAnswer" type="checkbox" />
              <span>
                生成结论与图表
                <span class="ask__switch-hint">（关闭可省两次模型调用，只看 SQL 与数据）</span>
              </span>
            </label>

            <div class="ask__actions">
              <span class="ask__counter">{{ question.length }} / 500</span>
              <button v-if="running" class="btn btn--danger" type="button" @click="stop">
                停止
              </button>
              <button
                class="btn btn--primary"
                type="button"
                :disabled="running || loading"
                @click="ask()"
              >
                <span v-if="running" class="spinner" />
                {{ running ? '分析中…' : '开始分析' }}
              </button>
            </div>
          </div>

          <p v-if="inputError" class="ask__error">{{ inputError }}</p>
        </div>
      </section>

      <!-- ---------- 失败提示 ---------- -->
      <div v-if="failureMessage" class="alert alert--err">
        <div class="alert__title">{{ gaveUp ? '重试次数用尽，已终止' : '执行未成功' }}</div>
        <p class="alert__body">{{ failureMessage }}</p>
        <p class="alert__hint">
          系统会自动带着这条报错重新生成 SQL。若仍失败，可在问题里补充明确的指标口径和时间范围。
        </p>
      </div>

      <!-- ---------- 工作区 ---------- -->
      <template v-if="showWorkspace">
        <section class="card">
          <StageBar :statuses="stageStatuses" :running="running" :failed="failed" />

          <div class="trace-block">
            <div class="trace-block__head">
              <span class="section-title" style="margin: 0">执行轨迹</span>
              <span class="hint">实时推送，逐条追加</span>
            </div>
            <TraceTimeline :items="trace" :running="running" />
          </div>
        </section>

        <div class="grid grid--2">
          <IntentPanel :intent="result?.intent ?? {}" :running="running" />
          <ValidationPanel
            :validation="result?.validation ?? { passed: false, errors: [], warnings: [], applied_rules: [], detected_tables: [] }"
            :running="running"
            :reached="reachedValidation"
          />
        </div>

        <SqlPanel
          :sql="result?.sql ?? ''"
          :safe-sql="result?.safe_sql ?? ''"
          :history="result?.sql_history ?? []"
          :running="running"
        />

        <template v-if="result">
          <MetricCards :result="result" />

          <div class="grid grid--wide">
            <section class="card">
              <header class="card-head">
                <span class="card-title">可视化</span>
                <span class="hint">后端只给配置，图形由前端渲染</span>
              </header>
              <div class="card-body">
                <ChartView :chart="result.chart" :execution="result.execution" />
              </div>
            </section>

            <section class="card">
              <header class="card-head">
                <span class="card-title">分析结论</span>
              </header>
              <div class="card-body">
                <AnswerPanel
                  :answer="result.answer"
                  :error="result.execution.error"
                  :running="running"
                  :has-result="true"
                />
              </div>
            </section>
          </div>

          <section v-if="result.execution.columns.length" class="card">
            <div class="card-body">
              <DataTable :columns="result.execution.columns" :rows="result.execution.rows" />
            </div>
          </section>
        </template>
      </template>

      <!-- ---------- 首次进入的引导 ---------- -->
      <section v-else-if="!loading" class="card card--flat">
        <div class="card-body">
          <p class="intro__title">还没有执行过查询</p>
          <p class="intro__text">
            从左侧挑一个示例问题，或直接在上方输入框提问。
            建议先用「一共有多少个客户？」这类单表统计确认链路通畅，
            再试「退款率最高的商品类目是哪个？」这类需要四表关联、还要判断退款口径的问题。
          </p>
        </div>
      </section>

      <footer class="footer">
        <span>
          前端 Vue 3 + TypeScript + Vite ｜ 后端 FastAPI + LangGraph ｜ 通过 REST 与 SSE 通信
        </span>
        <span v-if="rebuildMessage" class="footer__message">{{ rebuildMessage }}</span>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  align-items: flex-start;
  min-height: 100vh;
}

.main {
  flex: 1;
  min-width: 0;
  padding: 26px 28px 48px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 1400px;
}

/* ---------- 标题 ---------- */

.hero {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.hero__title {
  font-size: 22px;
  letter-spacing: -0.01em;
}

.hero__desc {
  margin: 6px 0 0;
  max-width: 760px;
  font-size: 13.5px;
  line-height: 1.7;
  color: var(--text-secondary);
}

.hero__desc strong {
  color: var(--text);
  font-weight: 600;
}

.hero__badges {
  display: flex;
  gap: 6px;
  padding-top: 4px;
}

/* ---------- 提示条 ---------- */

.alert {
  padding: 13px 16px;
  border-radius: var(--radius-lg);
  border: 1px solid;
}

.alert--warn {
  background: var(--warn-soft);
  border-color: #f2ddb4;
}

.alert--err {
  background: var(--err-soft);
  border-color: #f5c6c6;
}

.alert__title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 4px;
}

.alert--warn .alert__title {
  color: var(--warn);
}

.alert--err .alert__title {
  color: var(--err);
}

.alert__body {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-secondary);
  word-break: break-word;
}

.alert__hint {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}

.alert__actions {
  display: flex;
  gap: 8px;
  margin-top: 11px;
}

/* ---------- 提问区 ---------- */

.ask__label {
  display: block;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 7px;
}

.ask__input {
  width: 100%;
  padding: 11px 13px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  font-family: inherit;
  font-size: 14px;
  line-height: 1.6;
  color: var(--text);
  resize: vertical;
  transition: border-color 0.15s, box-shadow 0.15s;
}

.ask__input:focus {
  outline: none;
  border-color: var(--accent);
  box-shadow: 0 0 0 3px rgba(47, 107, 237, 0.12);
}

.ask__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  margin-top: 11px;
}

.ask__switch {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--text-secondary);
  cursor: pointer;
}

.ask__switch input {
  accent-color: var(--accent);
}

.ask__switch-hint {
  color: var(--text-muted);
}

.ask__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ask__counter {
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-muted);
}

.ask__error {
  margin: 9px 0 0;
  font-size: 12.5px;
  color: var(--err);
}

/* ---------- 轨迹区 ---------- */

.trace-block {
  padding: 0 18px 16px;
  border-top: 1px solid var(--border);
  padding-top: 14px;
}

.trace-block__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 8px;
}

/* ---------- 栅格 ---------- */

.grid {
  display: grid;
  gap: 16px;
}

.grid--2 {
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
}

.grid--wide {
  grid-template-columns: minmax(0, 3fr) minmax(0, 2fr);
}

@media (max-width: 1100px) {
  .grid--2,
  .grid--wide {
    grid-template-columns: minmax(0, 1fr);
  }
}

/* ---------- 引导与页脚 ---------- */

.card--flat {
  box-shadow: none;
  background: var(--surface-soft);
}

.intro__title {
  margin: 0 0 5px;
  font-size: 13.5px;
  font-weight: 600;
}

.intro__text {
  margin: 0;
  font-size: 13px;
  line-height: 1.75;
  color: var(--text-secondary);
}

.footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  padding-top: 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.footer__message {
  color: var(--accent);
}

/* ---------- 窄屏 ---------- */

@media (max-width: 960px) {
  .layout {
    flex-direction: column;
  }

  .main {
    padding: 20px 16px 40px;
  }
}
</style>
