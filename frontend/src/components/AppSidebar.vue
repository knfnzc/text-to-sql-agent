<script setup lang="ts">
/**
 * 左侧信息栏：服务状态、模型配置、安全限制、索引管理、示例问题、数据字典。
 *
 * 这里放的都是"不随每次问答变化"的信息，所以和主区域分开。
 * 其中数据字典面板看起来最不起眼，但它解释了整个 Agent 的原理：
 * 系统之所以能写出正确的 SQL，是因为它先知道了"字段的中文业务含义"
 * 和"哪些订单不该被算进销售额"。把这份材料摊开给人看，
 * 比在文档里描述一遍有说服力得多。
 */

import { computed, ref } from 'vue'

import type {
  HealthResponse,
  IndexStatsResponse,
  LLMConfigResponse,
  LLMConfigUpdateRequest,
  MetaResponse,
  TableInfo,
} from '@/types/api'
import { formatCount } from '@/utils/format'

import LLMConfigPanel from '@/components/LLMConfigPanel.vue'

const props = defineProps<{
  health: HealthResponse | null
  meta: MetaResponse | null
  indexStats: IndexStatsResponse | null
  tables: TableInfo[]
  /** 当前生效的模型配置，供「更换模型 / 密钥」面板使用 */
  llmConfig: LLMConfigResponse | null
  /** 是否正在保存模型配置 */
  llmSaving: boolean
  /** 保存模型配置时的错误文案 */
  llmError: string
  /** 是否处于离线演示模式 */
  demoMode: boolean
  /** 是否正在检查后端连通性 */
  loading: boolean
  /** 是否正在重建索引 */
  rebuilding: boolean
  /** 当前问题，用于把已选示例高亮出来 */
  currentQuestion: string
}>()

const emit = defineEmits<{
  (event: 'pick', question: string): void
  (event: 'rebuild'): void
  (event: 'save-llm-config', payload: LLMConfigUpdateRequest): void
  (event: 'reset-llm-config'): void
}>()

// 重建索引在真实模式下会产生向量化费用，所以加一道确认，
// 避免手滑点一下就花钱
const confirmRebuild = ref(false)

const expandedTable = ref<string | null>(null)

/** 索引是否就绪：文档数和预期表数一致且大于 0 */
const indexReady = computed(() => {
  const stats = props.indexStats
  if (!stats) return false
  return stats.ready
})

const backendOnline = computed(() => props.health !== null)

const limits = computed(() => {
  const raw = props.meta?.limits ?? {}
  return [
    { label: '行数上限', value: raw.sql_row_limit ? `${raw.sql_row_limit} 行` : '—' },
    { label: '执行超时', value: raw.sql_timeout_seconds ? `${raw.sql_timeout_seconds} 秒` : '—' },
    { label: '最大尝试', value: raw.sql_max_retries ? `${raw.sql_max_retries} 次` : '—' },
    { label: 'Schema 召回', value: raw.schema_top_k ? `Top-${raw.schema_top_k}` : '—' },
  ]
})

const examples = computed(() => props.meta?.example_questions ?? [])

function toggleTable(name: string): void {
  expandedTable.value = expandedTable.value === name ? null : name
}
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar__brand">
      <div class="sidebar__logo">SQL</div>
      <div>
        <div class="sidebar__name">智能数据分析 Agent</div>
        <div class="sidebar__sub">Text-to-SQL · LangGraph 状态图</div>
      </div>
    </div>

    <!-- ---------- 服务状态 ---------- -->
    <section class="panel">
      <h2 class="panel__title">服务状态</h2>

      <div v-if="loading" class="panel__row panel__row--muted">正在连接后端…</div>

      <template v-else-if="demoMode">
        <div class="panel__row">
          <span class="dot dot--warn" />
          <span>离线演示模式</span>
        </div>
        <p class="panel__hint">
          未连接后端，界面使用内置样例数据渲染。
          启动 <code>uvicorn app.main:app --port 8000</code> 后刷新即可切换到实时结果。
        </p>
      </template>

      <template v-else-if="!backendOnline">
        <div class="panel__row">
          <span class="dot dot--err" />
          <span>后端未连接</span>
        </div>
        <p class="panel__hint">
          请先在 <code>backend/</code> 目录启动服务：
          <code>uvicorn app.main:app --reload --port 8000</code>
        </p>
      </template>

      <template v-else>
        <div class="panel__row">
          <span class="dot" :class="health?.mock_mode ? 'dot--warn' : 'dot--ok'" />
          <span>{{ health?.mock_mode ? '离线模拟模式' : '真实模型模式' }}</span>
        </div>
        <div class="panel__row">
          <span class="dot" :class="health?.database_connected ? 'dot--ok' : 'dot--err'" />
          <span>数据库{{ health?.database_connected ? '连接正常' : '连接失败' }}</span>
        </div>
        <div class="panel__row">
          <span class="dot" :class="health?.index_ready ? 'dot--ok' : 'dot--warn'" />
          <span>Schema 索引{{ health?.index_ready ? '就绪' : '未就绪' }}</span>
        </div>
      </template>
    </section>

    <!-- ---------- 模型与密钥（可在界面上更换） ---------- -->
    <LLMConfigPanel
      :config="llmConfig"
      :disabled="demoMode || !llmConfig"
      :saving="llmSaving"
      :error="llmError"
      @save="emit('save-llm-config', $event)"
      @reset="emit('reset-llm-config')"
    />

    <!-- ---------- 安全限制 ---------- -->
    <section v-if="meta" class="panel">
      <h2 class="panel__title">安全限制</h2>
      <dl class="kv">
        <div v-for="item in limits" :key="item.label" class="kv__row">
          <dt>{{ item.label }}</dt>
          <dd>{{ item.value }}</dd>
        </div>
      </dl>
    </section>

    <!-- ---------- Schema 索引 ---------- -->
    <section class="panel">
      <h2 class="panel__title">Schema 向量索引</h2>

      <div class="panel__row">
        <span class="dot" :class="indexReady ? 'dot--ok' : 'dot--warn'" />
        <span>
          {{
            indexStats
              ? `已索引 ${formatCount(indexStats.document_count)} / ${formatCount(indexStats.expected_count)} 张表`
              : '暂无统计'
          }}
        </span>
      </div>

      <dl v-if="indexStats" class="kv">
        <div class="kv__row">
          <dt>向量模型</dt>
          <dd>{{ indexStats.embedding_model || '—' }}</dd>
        </div>
        <div class="kv__row">
          <dt>持久化目录</dt>
          <dd class="kv__mono">{{ indexStats.persist_dir || '—' }}</dd>
        </div>
      </dl>

      <label class="check">
        <input v-model="confirmRebuild" type="checkbox" />
        <span>我确认要重建索引（真实模式下会产生向量化费用）</span>
      </label>

      <button
        class="btn btn--block sidebar__btn"
        type="button"
        :disabled="!confirmRebuild || rebuilding || demoMode"
        @click="emit('rebuild')"
      >
        <span v-if="rebuilding" class="spinner" />
        {{ rebuilding ? '正在向量化…' : '重建索引' }}
      </button>
    </section>

    <!-- ---------- 示例问题 ---------- -->
    <section class="panel">
      <h2 class="panel__title">示例问题</h2>
      <p class="panel__hint">点一下填入提问框。覆盖单表统计、多表关联、时间趋势、反连接等场景。</p>

      <div class="examples">
        <button
          v-for="example in examples"
          :key="example"
          type="button"
          class="examples__item"
          :class="{ 'examples__item--active': example === currentQuestion }"
          @click="emit('pick', example)"
        >
          {{ example }}
        </button>
      </div>

      <p v-if="!examples.length && !loading" class="panel__hint">
        示例问题需要连接后端获取。
      </p>
    </section>

    <!-- ---------- 数据字典 ---------- -->
    <section class="panel">
      <h2 class="panel__title">
        数据字典
        <span class="panel__count">{{ tables.length }} 张表</span>
      </h2>
      <p class="panel__hint">
        这是交给模型的原始材料：字段的中文含义、取值范围、表之间的关联关系。
      </p>

      <div class="dict">
        <div v-for="table in tables" :key="table.name" class="dict__table">
          <button
            type="button"
            class="dict__head"
            :aria-expanded="expandedTable === table.name"
            @click="toggleTable(table.name)"
          >
            <code class="dict__name">{{ table.name }}</code>
            <span class="dict__caret" :class="{ 'dict__caret--open': expandedTable === table.name }">
              ▾
            </span>
          </button>

          <div v-if="expandedTable === table.name" class="dict__body">
            <p class="dict__desc">{{ table.description }}</p>
            <div class="dict__columns">
              <div v-for="column in table.columns" :key="column.name" class="dict__column">
                <code class="dict__column-name">{{ column.name }}</code>
                <span class="dict__column-desc">{{ column.description }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <p v-if="!tables.length && !loading" class="panel__hint">需要连接后端获取表结构。</p>
    </section>
  </aside>
</template>

<style scoped>
/* 侧栏用深色，和浅色的内容区形成对比。
   深色更适合放"元信息"，也不会和主区域的数据展示抢注意力 */
.sidebar {
  width: var(--sidebar-width);
  flex: none;
  background: var(--nav-bg);
  color: var(--nav-text);
  padding: 18px 16px 40px;
  overflow-y: auto;
  height: 100vh;
  position: sticky;
  top: 0;
}

.sidebar__brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--nav-border);
  margin-bottom: 18px;
}

.sidebar__logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 38px;
  flex: none;
  border-radius: 11px;
  background: linear-gradient(135deg, #3b7bf6, #1d4ed8);
  color: #fff;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.sidebar__name {
  font-size: 14px;
  font-weight: 600;
  color: #f1f5fb;
}

.sidebar__sub {
  font-size: 11.5px;
  color: var(--nav-text-muted);
}

.panel {
  margin-bottom: 20px;
}

.panel__title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--nav-text-muted);
  margin-bottom: 9px;
}

.panel__count {
  font-size: 11px;
  font-weight: 500;
  text-transform: none;
  letter-spacing: 0;
}

.panel__row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--nav-text);
  padding: 3px 0;
}

.panel__row--muted {
  color: var(--nav-text-muted);
}

.panel__hint {
  margin: 7px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--nav-text-muted);
}

.panel__hint code {
  background: var(--nav-bg-soft);
  border: 1px solid var(--nav-border);
  border-radius: 4px;
  padding: 1px 5px;
  font-size: 11px;
  color: #a9c5f5;
}

.dot {
  flex: none;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--nav-text-muted);
}

.dot--ok {
  background: #34d399;
  box-shadow: 0 0 0 3px rgba(52, 211, 153, 0.16);
}

.dot--warn {
  background: #fbbf24;
  box-shadow: 0 0 0 3px rgba(251, 191, 36, 0.16);
}

.dot--err {
  background: #f87171;
  box-shadow: 0 0 0 3px rgba(248, 113, 113, 0.16);
}

.kv {
  margin: 8px 0 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.kv__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
}

.kv__row dt {
  color: var(--nav-text-muted);
}

.kv__row dd {
  margin: 0;
  color: var(--nav-text);
  text-align: right;
  word-break: break-all;
}

.kv__mono {
  font-family: var(--font-mono);
  font-size: 11px;
}

.check {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin: 12px 0 8px;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--nav-text-muted);
  cursor: pointer;
}

.check input {
  margin-top: 3px;
  accent-color: #3b7bf6;
  flex: none;
}

.sidebar__btn {
  background: var(--nav-bg-soft);
  border-color: var(--nav-border);
  color: var(--nav-text);
}

.sidebar__btn:hover:not(:disabled) {
  background: #24314f;
  border-color: #3a4a6d;
}

.examples {
  display: flex;
  flex-direction: column;
  gap: 5px;
  margin-top: 9px;
}

.examples__item {
  padding: 7px 11px;
  border: 1px solid var(--nav-border);
  border-radius: var(--radius-sm);
  background: var(--nav-bg-soft);
  color: var(--nav-text);
  font-family: inherit;
  font-size: 12.5px;
  line-height: 1.5;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.examples__item:hover {
  background: #24314f;
  border-color: #3a4a6d;
}

.examples__item--active {
  background: rgba(59, 123, 246, 0.18);
  border-color: #3b7bf6;
  color: #dbe8ff;
}

.dict {
  margin-top: 9px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.dict__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--nav-border);
  border-radius: var(--radius-sm);
  background: var(--nav-bg-soft);
  cursor: pointer;
  font-family: inherit;
}

.dict__head:hover {
  background: #24314f;
}

.dict__name {
  font-size: 11.5px;
  color: #a9c5f5;
}

.dict__caret {
  font-size: 10px;
  color: var(--nav-text-muted);
  transition: transform 0.15s;
}

.dict__caret--open {
  transform: rotate(180deg);
}

.dict__body {
  padding: 9px 10px 10px;
  border: 1px solid var(--nav-border);
  border-top: none;
  border-radius: 0 0 var(--radius-sm) var(--radius-sm);
  background: rgba(255, 255, 255, 0.02);
}

.dict__desc {
  margin: 0 0 8px;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--nav-text-muted);
}

.dict__columns {
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.dict__column {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.dict__column-name {
  font-size: 11px;
  color: #9fd8b8;
}

.dict__column-desc {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--nav-text);
}

/* 窄屏时侧栏改为全宽并取消吸顶，否则会把主区域挤到只剩一条 */
@media (max-width: 960px) {
  .sidebar {
    width: 100%;
    height: auto;
    position: static;
  }
}
</style>
