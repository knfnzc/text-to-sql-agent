<script setup lang="ts">
/**
 * SQL 展示面板。
 *
 * 展示的是"真正执行的那条 SQL"（safe_sql）而不是模型原始输出（sql），
 * 因为两者可能不同——安全校验层会强制补上 LIMIT。
 * 把这个差别显式呈现出来（下方标注"已自动补 LIMIT"），
 * 比给一段漂亮的 SQL 更能说明系统里有一层真实的保护在起作用。
 *
 * 语法高亮没用 CodeMirror / highlight.js 这类库：
 *   它们单独就有几百 KB，而 SQL 只需要区分关键字、字符串、数字、注释四类。
 *   这里用一条正则搞定，体积为零。
 *   代价是不支持复杂语法（比如方言关键字），对展示场景完全够用。
 */

import { computed, ref } from 'vue'

const props = defineProps<{
  /** 模型原始输出 */
  sql: string
  /** 实际执行的 SQL（可能被校验层改写过） */
  safeSql: string
  /** 每次尝试生成的 SQL，长度 > 1 说明发生过重试 */
  history: string[]
  running: boolean
}>()

const showHistory = ref(false)
const copied = ref(false)

/** 真正执行的语句。safe_sql 为空时退回原始 SQL */
const executed = computed(() => props.safeSql || props.sql || '')

/** 校验层是否改写过 SQL */
const rewritten = computed(
  () => Boolean(props.safeSql) && Boolean(props.sql) && props.safeSql.trim() !== props.sql.trim(),
)

/** 超出一次尝试就问一句"为什么重试"，这是自我修正机制最直观的证据 */
const retried = computed(() => props.history.length > 1)

const KEYWORDS =
  'SELECT|FROM|WHERE|GROUP|ORDER|HAVING|LIMIT|OFFSET|DISTINCT|AS|ON|JOIN|LEFT|RIGHT|INNER|OUTER|FULL|UNION|ALL|AND|OR|NOT|IN|IS|NULL|LIKE|BETWEEN|EXISTS|CASE|WHEN|THEN|ELSE|END|DESC|ASC|BY|WITH|COUNT|SUM|AVG|MIN|MAX|ROUND|CAST|COALESCE|IFNULL|DATE|DATETIME|SUBSTR|STRFTIME|ABS|UPPER|LOWER'

const TOKEN_RE = new RegExp(
  `(--[^\\n]*)|('(?:[^']|'')*')|(\\b\\d+(?:\\.\\d+)?\\b)|(\\b(?:${KEYWORDS})\\b)`,
  'gi',
)

/** 转义 HTML 特殊字符，防止 SQL 里的 < > & 破坏页面结构 */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

/**
 * 给 SQL 加上高亮标记。
 *
 * 注意这里是先分词再逐段转义，而不是先转义再匹配——
 * 反过来的话引号会变成 &quot;，字符串字面量的正则就匹配不到了。
 * 因为每一段都手动转义过，最终结果插进 v-html 是安全的。
 */
function highlight(sql: string): string {
  let output = ''
  let cursor = 0

  for (const match of sql.matchAll(TOKEN_RE)) {
    const index = match.index ?? 0
    output += escapeHtml(sql.slice(cursor, index))

    const [full, comment, literal, number, keyword] = match

    if (comment) {
      output += `<span class="tok-comment">${escapeHtml(full)}</span>`
    } else if (literal) {
      output += `<span class="tok-string">${escapeHtml(full)}</span>`
    } else if (number) {
      output += `<span class="tok-number">${escapeHtml(full)}</span>`
    } else if (keyword) {
      output += `<span class="tok-keyword">${escapeHtml(full)}</span>`
    } else {
      output += escapeHtml(full)
    }

    cursor = index + full.length
  }

  output += escapeHtml(sql.slice(cursor))
  return output
}

const highlighted = computed(() => highlight(executed.value))

async function copySql(): Promise<void> {
  try {
    await navigator.clipboard.writeText(executed.value)
    copied.value = true
    window.setTimeout(() => {
      copied.value = false
    }, 1600)
  } catch {
    // 剪贴板权限被浏览器拒绝（非 HTTPS 环境常见）。
    // 不弹提示打扰用户，选中文本让他们自己复制即可
    copied.value = false
  }
}
</script>

<template>
  <section class="card">
    <header class="card-head">
      <span class="card-title">
        生成的 SQL
        <span v-if="retried" class="badge badge--warn">重试 {{ history.length - 1 }} 次</span>
      </span>

      <button
        v-if="executed"
        class="btn btn--ghost btn--sm"
        type="button"
        @click="copySql"
      >
        {{ copied ? '已复制' : '复制' }}
      </button>
    </header>

    <div class="card-body">
      <div v-if="running && !executed" class="skeleton">
        <span class="skeleton__line" />
        <span class="skeleton__line skeleton__line--short" />
        <span class="skeleton__line" />
      </div>

      <div v-else-if="!executed" class="empty">尚未生成 SQL。</div>

      <template v-else>
        <!-- eslint-disable-next-line vue/no-v-html -->
        <pre class="code-block" v-html="highlighted" />

        <p v-if="rewritten" class="sql-note">
          执行前 SQL 被安全层改写过（通常是自动补上行数上限），上方展示的是真正执行的那条。
        </p>

        <div v-if="retried" class="history">
          <button class="history__toggle" type="button" @click="showHistory = !showHistory">
            {{ showHistory ? '收起' : '查看' }}全部 {{ history.length }} 次尝试
            <span class="history__caret" :class="{ 'history__caret--open': showHistory }">▾</span>
          </button>

          <div v-if="showHistory" class="history__list">
            <div
              v-for="(attempt, index) in history"
              :key="index"
              class="history__item"
              :class="{ 'history__item--last': index === history.length - 1 }"
            >
              <div class="history__label">
                第 {{ index + 1 }} 次
                <span v-if="index === history.length - 1" class="badge badge--info">最终采用</span>
              </div>
              <!-- eslint-disable-next-line vue/no-v-html -->
              <pre class="code-block code-block--plain" v-html="highlight(attempt)" />
            </div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.sql-note {
  margin: 10px 0 0;
  padding: 8px 12px;
  background: var(--warn-soft);
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  color: var(--warn);
}

.history {
  margin-top: 12px;
}

.history__toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 0;
  border: none;
  background: none;
  color: var(--accent);
  font-family: inherit;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
}

.history__caret {
  transition: transform 0.15s;
}

.history__caret--open {
  transform: rotate(180deg);
}

.history__list {
  margin-top: 6px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.history__item--last .code-block {
  outline: 2px solid var(--accent-border);
  outline-offset: -2px;
}

.history__label {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 5px;
  font-size: 12px;
  color: var(--text-muted);
}

/* 加载骨架。比转圈图标更能说明"这里将会出现一段 SQL" */
.skeleton {
  display: flex;
  flex-direction: column;
  gap: 9px;
  padding: 18px 16px;
  border-radius: var(--radius);
  background: var(--surface-sunken);
}

.skeleton__line {
  height: 10px;
  border-radius: 999px;
  background: linear-gradient(90deg, #dde4ef 25%, #eef2f8 50%, #dde4ef 75%);
  background-size: 200% 100%;
  animation: shimmer 1.4s linear infinite;
}

.skeleton__line--short {
  width: 55%;
}

@keyframes shimmer {
  from {
    background-position: 100% 0;
  }
  to {
    background-position: -100% 0;
  }
}
</style>
