<script setup lang="ts">
/**
 * 「更换模型 / 密钥」面板。
 *
 * 用户改的是**后端进程里当前生效的配置**：保存后对下一次提问立即生效，
 * 不需要改 .env，也不需要重启后端。
 *
 * 三个刻意的设计：
 *   1. 密钥只以脱敏形式回显。后端不会把原文发下来，所以输入框永远是空的——
 *      留空即表示"这把钥匙不动"。这样密钥不会进入浏览器内存、
 *      localStorage 或截图里。
 *   2. 只提交用户真正改过的字段。否则"只改模型名"会把密钥一起写成空值，
 *      下一次真实调用直接 401，而那个报错很难和"改模型"联系起来。
 *   3. 离线模拟开关只读展示。它决定"到底联不联网"，属于部署级别的开关，
 *      做成界面可点会让人在真实密钥下误切到模拟模式，
 *      看到一堆带 MOCK 标记的假数据却以为系统坏了。
 *
 * 表单状态留在组件内部，保存/重置通过事件交给 App 执行——
 * 组件只负责收集输入，不自己发请求，与项目里其余面板保持一致。
 */

import { computed, reactive, ref, watch } from 'vue'

import type { LLMConfigResponse, LLMConfigUpdateRequest } from '@/types/api'

const props = withDefaults(
  defineProps<{
    config: LLMConfigResponse | null
    /** 演示模式或后端不可用时禁止编辑 */
    disabled?: boolean
    /** 正在保存 / 重置 */
    saving?: boolean
    /** 后端返回的错误文案 */
    error?: string
  }>(),
  { disabled: false, saving: false, error: '' },
)

const emit = defineEmits<{
  (event: 'save', payload: LLMConfigUpdateRequest): void
  (event: 'reset'): void
}>()

/** 结构化输出方式的中文说明，纯英文枚举值对使用者没有意义 */
const METHOD_LABELS: Record<string, string> = {
  function_calling: 'function_calling（工具调用，兼容性最好）',
  json_schema: 'json_schema（约束最强，DeepSeek 不支持）',
  json_mode: 'json_mode（要求提示词里出现 json）',
}

/** 字段名 -> 中文名，用于展示"哪些项被界面改过" */
const FIELD_LABELS: Record<string, string> = {
  model: '模型',
  base_url: '接口地址',
  api_key: '密钥',
  structured_method: '结构化输出',
  disable_thinking: '思考模式',
}

/** 表单默认收起：侧栏空间有限，只在真要改配置时才展开 */
const expanded = ref(false)
/** 本地提示（例如"没有检测到改动"），与后端错误分开显示 */
const localHint = ref('')

const form = reactive({
  model: '',
  baseUrl: '',
  apiKey: '',
  structuredMethod: '',
  disableThinking: false,
})

/**
 * 用当前生效配置重置表单。
 *
 * 保存成功后 App 会更新 config，表单随之刷新——
 * 所以这里既是"初始化"，也是"提交后的复位"，
 * 避免输入框里残留已经提交过的明文密钥。
 */
function sync(): void {
  form.model = props.config?.model ?? ''
  form.baseUrl = props.config?.base_url ?? ''
  form.apiKey = ''
  form.structuredMethod = props.config?.structured_method ?? ''
  form.disableThinking = props.config?.disable_thinking ?? false
  localHint.value = ''
}

watch(() => props.config, sync, { immediate: true, deep: true })

const methodOptions = computed(() => props.config?.structured_method_options ?? [])

const overriddenLabels = computed(() =>
  (props.config?.overridden ?? []).map((field) => FIELD_LABELS[field] ?? field),
)

/** 组装这次要提交的字段，只含真正的改动 */
function buildChanges(): LLMConfigUpdateRequest {
  const config = props.config
  const changes: LLMConfigUpdateRequest = {}
  if (!config) return changes

  const model = form.model.trim()
  if (model && model !== config.model) changes.model = model

  // 末尾斜杠由后端统一去掉，这里先归一化，避免"只多了个斜杠"也被当成改动
  const baseUrl = form.baseUrl.trim().replace(/\/+$/, '')
  if (baseUrl && baseUrl !== config.base_url) changes.base_url = baseUrl

  const apiKey = form.apiKey.trim()
  if (apiKey) changes.api_key = apiKey

  if (form.structuredMethod && form.structuredMethod !== config.structured_method) {
    changes.structured_method = form.structuredMethod
  }

  if (form.disableThinking !== config.disable_thinking) {
    changes.disable_thinking = form.disableThinking
  }

  return changes
}

function handleSave(): void {
  const changes = buildChanges()
  if (Object.keys(changes).length === 0) {
    localHint.value = '没有检测到改动。密钥输入框留空表示沿用当前密钥。'
    return
  }
  localHint.value = ''
  emit('save', changes)
}

function handleReset(): void {
  localHint.value = ''
  emit('reset')
}
</script>

<template>
  <section class="panel cfg">
    <h2 class="panel__title">模型与密钥</h2>

    <dl class="kv">
      <div class="kv__row">
        <dt>模型</dt>
        <dd>{{ config?.model || '—' }}</dd>
      </div>
      <div class="kv__row">
        <dt>接口地址</dt>
        <dd class="kv__mono">{{ config?.base_url || '—' }}</dd>
      </div>
      <div class="kv__row">
        <dt>密钥</dt>
        <dd class="kv__mono">{{ config?.api_key_masked || '—' }}</dd>
      </div>
      <div class="kv__row">
        <dt>结构化输出</dt>
        <dd>{{ config?.structured_method || '—' }}</dd>
      </div>
      <div class="kv__row">
        <dt>思考模式</dt>
        <dd>{{ config?.disable_thinking ? '已关闭' : '默认开启' }}</dd>
      </div>
      <div class="kv__row">
        <dt>运行方式</dt>
        <dd>{{ config?.mock_mode ? '离线模拟' : '真实模型' }}</dd>
      </div>
    </dl>

    <p v-if="overriddenLabels.length" class="panel__hint">
      已被界面覆盖（不再是 .env 里的值）：{{ overriddenLabels.join('、') }}
    </p>
    <p v-else class="panel__hint">当前配置全部来自 .env。</p>

    <button
      type="button"
      class="btn btn--block btn--sm sidebar__btn cfg__toggle"
      :disabled="disabled"
      @click="expanded = !expanded"
    >
      {{ expanded ? '收起' : '更换模型 / 密钥' }}
    </button>

    <p v-if="disabled" class="panel__hint">
      {{ config ? '离线演示模式下无法修改配置，请连接后端后重试。' : '尚未取到后端配置，无法修改。' }}
    </p>

    <form v-else-if="expanded" class="cfg__form" @submit.prevent="handleSave">
      <label class="cfg__field">
        <span class="cfg__label">模型名称</span>
        <input
          v-model="form.model"
          class="cfg__input"
          type="text"
          placeholder="例：deepseek-chat"
          :disabled="saving"
        />
      </label>

      <label class="cfg__field">
        <span class="cfg__label">接口地址</span>
        <input
          v-model="form.baseUrl"
          class="cfg__input"
          type="text"
          placeholder="例：https://api.deepseek.com/v1"
          :disabled="saving"
        />
        <span class="cfg__note">必须以 http:// 或 https:// 开头，末尾斜杠会自动去掉。</span>
      </label>

      <label class="cfg__field">
        <span class="cfg__label">API Key</span>
        <input
          v-model="form.apiKey"
          class="cfg__input"
          type="password"
          autocomplete="off"
          :placeholder="`当前 ${config?.api_key_masked || '（未配置）'}，留空表示不修改`"
          :disabled="saving"
        />
      </label>

      <label class="cfg__field">
        <span class="cfg__label">结构化输出方式</span>
        <select v-model="form.structuredMethod" class="cfg__input" :disabled="saving">
          <option v-for="method in methodOptions" :key="method" :value="method">
            {{ METHOD_LABELS[method] ?? method }}
          </option>
        </select>
        <span class="cfg__note">
          生成 SQL 要用结构化输出。接 DeepSeek 必须选 function_calling，
          否则会报 This response_format type is unavailable now。
        </span>
      </label>

      <label class="cfg__check">
        <input v-model="form.disableThinking" type="checkbox" :disabled="saving" />
        <span>
          关闭模型思考模式：接 DeepSeek 这类默认开思考的模型时建议勾选，
          否则温度会被忽略、且结构化输出会被拒绝。
        </span>
      </label>

      <button type="submit" class="btn btn--primary btn--block btn--sm" :disabled="saving">
        {{ saving ? '保存中…' : '保存并立即生效' }}
      </button>
      <button
        type="button"
        class="btn btn--block btn--sm sidebar__btn"
        :disabled="saving"
        @click="handleReset"
      >
        恢复 .env 配置
      </button>

      <p v-if="error" class="cfg__msg cfg__msg--err">{{ error }}</p>
      <p v-else-if="localHint" class="cfg__msg">{{ localHint }}</p>

      <p class="cfg__note">
        配置保存在后端进程内存中，重启后端即回到 .env 的值；密钥不会写入磁盘。
        离线模拟开关由 <code>LLM_MOCK</code> 决定，属于部署级设置，不在此处修改。
      </p>
    </form>
  </section>
</template>

<style scoped>
/*
 * 这个面板挂在深色侧栏里（AppSidebar 的 <aside class="sidebar">），
 * 所以配色一律用 --nav-* 系列变量。
 *
 * 为什么 .panel__title / .panel__hint / .kv 这些"看着应该复用父组件"的样式
 * 要在这里再写一遍：AppSidebar 的样式是 scoped 的，父组件的 scope 属性
 * 只会加到子组件的根元素上，内部元素拿不到——不重写的话这些文字会变成
 * 深色背景上的默认黑字，基本看不见。
 */

.panel__title {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 9px;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--nav-text-muted);
}

.panel__hint {
  margin: 7px 0 0;
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--nav-text-muted);
}

.kv {
  margin: 0;
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
  flex: none;
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

.cfg__toggle {
  margin-top: 10px;
}

.cfg__form {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 12px;
}

.cfg__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.cfg__label {
  font-size: 11.5px;
  color: var(--nav-text-muted);
}

.cfg__input {
  width: 100%;
  padding: 7px 9px;
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: #fff;
  background: var(--nav-bg-soft);
  border: 1px solid var(--nav-border);
  border-radius: var(--radius-sm);
  outline: none;
}

.cfg__input:focus {
  border-color: var(--accent);
}

.cfg__input:disabled {
  opacity: 0.6;
}

.cfg__input::placeholder {
  color: var(--nav-text-muted);
}

.cfg__note {
  font-size: 10.5px;
  line-height: 1.55;
  color: var(--nav-text-muted);
}

.cfg__note code {
  background: var(--nav-bg-soft);
  border: 1px solid var(--nav-border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 10.5px;
  color: #a9c5f5;
}

.cfg__check {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  font-size: 11.5px;
  line-height: 1.55;
  color: var(--nav-text);
  cursor: pointer;
}

.cfg__check input {
  flex: none;
  margin-top: 2px;
  accent-color: #3b7bf6;
}

.cfg__msg {
  margin: 0;
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--nav-text-muted);
}

.cfg__msg--err {
  color: #f0a3a3;
}
</style>
