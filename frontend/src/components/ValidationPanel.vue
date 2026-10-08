<script setup lang="ts">
/**
 * SQL 安全校验面板。
 *
 * 这是整个项目里最值得展示的一层，前端必须把它做可见。
 * 尤其是 applied_rules ——"检查了哪 6 条规则"如果只写在后端日志里，
 * 看界面的人根本不知道有这层防护。
 * 把规则逐条列出来，安全机制才从一个说法变成一件看得见的事实。
 */

import { computed } from 'vue'

import type { ValidationPayload } from '@/types/api'

const props = defineProps<{
  validation: ValidationPayload
  running: boolean
  /** 已经跑到过校验阶段，用于区分"还没轮到"和"校验失败" */
  reached: boolean
}>()

/** 是否已经有校验结果可展示 */
const hasResult = computed(() => props.reached)

const status = computed(() => {
  if (!hasResult.value) return { tone: 'muted', text: '未执行' }
  return props.validation.passed
    ? { tone: 'ok', text: '校验通过' }
    : { tone: 'err', text: '校验未通过' }
})
</script>

<template>
  <section class="card">
    <header class="card-head">
      <span class="card-title">安全校验</span>
      <span class="badge" :class="`badge--${status.tone}`">{{ status.text }}</span>
    </header>

    <div class="card-body">
      <div v-if="!hasResult" class="empty">
        {{ running ? '等待进入校验阶段…' : '尚未执行校验。' }}
      </div>

      <template v-else>
        <!-- 拦截原因。只有失败时才有内容，用红色卡片强调 -->
        <ul v-if="validation.errors.length" class="errors">
          <li v-for="error in validation.errors" :key="error" class="errors__item">
            {{ error }}
          </li>
        </ul>

        <div v-if="validation.detected_tables.length" class="block">
          <div class="section-title">SQL 中识别到的表</div>
          <div class="chips">
            <span v-for="table in validation.detected_tables" :key="table" class="chip">
              {{ table }}
            </span>
          </div>
        </div>

        <div v-if="validation.applied_rules.length" class="block">
          <div class="section-title">
            已执行的检查（{{ validation.applied_rules.length }} 项）
          </div>
          <ul class="rules">
            <li v-for="rule in validation.applied_rules" :key="rule" class="rules__item">
              <span class="rules__tick">✓</span>{{ rule }}
            </li>
          </ul>
        </div>

        <p v-if="!validation.applied_rules.length" class="hint">本次没有可展示的检查项记录。</p>
      </template>
    </div>
  </section>
</template>

<style scoped>
.errors {
  margin: 0 0 14px;
  padding: 10px 12px 10px 26px;
  background: var(--err-soft);
  border: 1px solid #f5c6c6;
  border-radius: var(--radius-sm);
  color: var(--err);
  font-size: 12.5px;
}

.errors__item + .errors__item {
  margin-top: 4px;
}

.block + .block {
  margin-top: 14px;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.chip {
  padding: 2px 9px;
  border-radius: var(--radius-sm);
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 11.5px;
  color: var(--text-secondary);
}

.rules {
  margin: 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 5px;
}

.rules__item {
  display: flex;
  gap: 7px;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.rules__tick {
  flex: none;
  color: var(--ok);
  font-weight: 700;
}
</style>
