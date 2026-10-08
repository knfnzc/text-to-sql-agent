<script setup lang="ts">
/**
 * 查询意图面板。
 *
 * 这个面板的存在意义常被低估：它把"模型理解成了什么"摊开给人看。
 * 用户看到"指标=总销售额，维度=商品类目"之后，如果发现理解偏了，
 * 就知道该换个说法重新提问，而不是对着一个错误结果发懵。
 * 可解释性在数据分析类产品里不是加分项，是必须项。
 */

import { computed } from 'vue'

import type { IntentPayload } from '@/types/api'

const props = defineProps<{
  intent: IntentPayload
  running: boolean
}>()

interface Row {
  key: string
  label: string
  values: string[]
  /** 空值时是否仍然显示（前两个字段即使为空也值得显示，让结构完整） */
  alwaysShow: boolean
}

const rows = computed<Row[]>(() => {
  const list: Row[] = [
    { key: 'metric', label: '核心指标', values: toList(props.intent.metric), alwaysShow: true },
    {
      key: 'dimensions',
      label: '分析维度',
      values: toList(props.intent.dimensions),
      alwaysShow: true,
    },
    { key: 'filters', label: '筛选条件', values: toList(props.intent.filters), alwaysShow: false },
    { key: 'time_range', label: '时间范围', values: toList(props.intent.time_range), alwaysShow: false },
    {
      key: 'sort_and_limit',
      label: '排序与限制',
      values: toList(props.intent.sort_and_limit),
      alwaysShow: false,
    },
  ]

  return list.filter((row) => row.alwaysShow || row.values.length > 0)
})

/**
 * 统一成数组。
 *
 * 后端 intent 是 dict[str, Any]，同一字段可能是字符串也可能是字符串数组：
 * 提示词里让模型输出「分析维度」，有的模型给 ["商品类目"]，有的给 "商品类目"。
 * 前端不该假设某一种，这里统一摊平成数组再渲染。
 */
function toList(value: unknown): string[] {
  if (value === null || value === undefined || value === '') return []
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).filter((item) => item.trim() !== '')
  }
  const text = String(value).trim()
  // 模型有时会把多个值拼成一个字符串，用顿号或逗号分隔，这里顺便拆开
  if (text.includes('、') || text.includes(',')) {
    return text
      .split(/[、,]/)
      .map((part) => part.trim())
      .filter(Boolean)
  }
  return text ? [text] : []
}
</script>

<template>
  <section class="card">
    <header class="card-head">
      <span class="card-title">查询意图</span>
      <span class="hint">模型对问题的结构化理解</span>
    </header>

    <div class="card-body">
      <div v-if="running && !rows.length" class="intent__loading">正在解析问题…</div>

      <div v-else-if="!rows.length" class="empty">尚未解析出意图。</div>

      <dl v-else class="intent">
        <div v-for="row in rows" :key="row.key" class="intent__row">
          <dt class="intent__label">{{ row.label }}</dt>
          <dd class="intent__value">
            <template v-if="row.values.length">
              <span v-for="value in row.values" :key="value" class="intent__chip">{{ value }}</span>
            </template>
            <span v-else class="intent__empty">不限</span>
          </dd>
        </div>
      </dl>
    </div>
  </section>
</template>

<style scoped>
.intent {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.intent__row {
  display: grid;
  grid-template-columns: 78px 1fr;
  gap: 10px;
  align-items: start;
}

.intent__label {
  font-size: 12.5px;
  color: var(--text-muted);
  padding-top: 3px;
}

.intent__value {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.intent__chip {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  background: var(--accent-soft);
  border: 1px solid var(--accent-border);
  color: var(--accent-strong);
  font-size: 12px;
  line-height: 1.7;
}

.intent__empty {
  font-size: 12.5px;
  color: var(--text-muted);
  padding-top: 3px;
}

.intent__loading {
  font-size: 13px;
  color: var(--text-muted);
}
</style>
