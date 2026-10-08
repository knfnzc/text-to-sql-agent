<script setup lang="ts">
/**
 * 结果指标卡。
 *
 * 把关键数字从长文本里拎出来单独成块，而不是散落在结论段落中。
 * 这几个数字同时也是"这次运行健康不健康"的信号：
 * 尝试次数大于 1 说明触发过自我修正，token 用量是成本，
 * 耗时能解释为什么这次等得久。
 */

import { computed } from 'vue'

import type { QueryResponse } from '@/types/api'
import { formatCount, formatMs, totalTokens } from '@/utils/format'

const props = defineProps<{
  result: QueryResponse
}>()

interface Metric {
  label: string
  value: string
  tone: 'default' | 'ok' | 'warn' | 'err'
  hint: string
}

const metrics = computed<Metric[]>(() => {
  const { execution, token_usage: usage, sql_history: history, validation } = props.result

  const attempts = history.length
  const tokens = totalTokens(usage)

  return [
    {
      label: '结果行数',
      value: execution.success ? formatCount(execution.row_count) : '—',
      tone: !execution.success ? 'err' : execution.row_count === 0 ? 'warn' : 'ok',
      hint: execution.success ? '查询返回的数据行' : '查询未成功执行',
    },
    {
      label: '查询耗时',
      value: execution.success ? formatMs(execution.elapsed_ms) : '—',
      tone: 'default',
      hint: '仅数据库执行时间，不含模型推理',
    },
    {
      label: '尝试次数',
      value: String(attempts),
      tone: attempts > 1 ? 'warn' : 'ok',
      hint: attempts > 1 ? `触发过 ${attempts - 1} 次自我修正` : '一次生成即通过',
    },
    {
      label: 'token 用量',
      value: tokens ? formatCount(tokens) : '—',
      tone: 'default',
      hint: usage.prompt_tokens
        ? `输入 ${formatCount(usage.prompt_tokens)} + 输出 ${formatCount(usage.completion_tokens)}`
        : '未统计到用量',
    },
    {
      label: '命中表数',
      value: String(validation.detected_tables.length),
      tone: 'default',
      hint: validation.detected_tables.join('、') || '未识别到表',
    },
  ]
})

// 空查询结果是个容易被忽略的状态：SQL 跑通了但一行没有。
// 这不是错误，需要单独给出"放宽条件"的建议，而不是显示一个空白表格
const isEmptyResult = computed(
  () => props.result.execution.success && props.result.execution.row_count === 0,
)
</script>

<template>
  <div class="metrics">
    <div
      v-for="metric in metrics"
      :key="metric.label"
      class="metric"
      :class="`metric--${metric.tone}`"
    >
      <div class="metric__label">{{ metric.label }}</div>
      <div class="metric__value">{{ metric.value }}</div>
      <div class="metric__hint">{{ metric.hint }}</div>
    </div>

    <div v-if="isEmptyResult" class="metrics__notice">
      查询执行成功，但没有符合条件的数据。建议放宽筛选条件，或换一种问法。
    </div>
  </div>
</template>

<style scoped>
.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(128px, 1fr));
  gap: 12px;
}

.metric {
  padding: 12px 14px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}

.metric__label {
  font-size: 12px;
  color: var(--text-muted);
}

.metric__value {
  margin: 3px 0 2px;
  font-size: 22px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  line-height: 1.25;
  color: var(--text);
}

.metric--ok .metric__value {
  color: var(--ok);
}

.metric--warn .metric__value {
  color: var(--warn);
}

.metric--err .metric__value {
  color: var(--err);
}

.metric__hint {
  font-size: 11.5px;
  color: var(--text-muted);
  line-height: 1.5;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metrics__notice {
  grid-column: 1 / -1;
  padding: 10px 14px;
  border-radius: var(--radius);
  background: var(--warn-soft);
  border: 1px solid #f2ddb4;
  color: var(--warn);
  font-size: 12.5px;
}
</style>
