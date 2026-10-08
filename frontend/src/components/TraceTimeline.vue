<script setup lang="ts">
/**
 * 执行轨迹时间线。
 *
 * 为什么值得单独做一个组件：
 *    轨迹是这个 Agent 的"思考过程"，也是排查问题的主要依据。
 *    它由 SSE 的 trace 事件逐条推送而来，是真实的实时追加，
 *    而不是等结果回来后一次性渲染的静态列表。
 *
 * 一个细节：自动滚动只在用户本来就停在底部时才执行。
 *    如果用户已经往上翻去看前面的步骤，新事件把它拽回底部是非常烦人的体验。
 */

import { nextTick, ref, watch } from 'vue'

import type { TraceItem } from '@/types/api'
import { LEVEL_STYLES, formatMs } from '@/utils/format'

const props = defineProps<{
  items: TraceItem[]
  /** 是否正在执行，用于显示"等待下一步"的占位 */
  running: boolean
}>()

const scroller = ref<HTMLElement | null>(null)

/** 用户是否停留在底部附近 */
function isAtBottom(): boolean {
  const element = scroller.value
  if (!element) return true
  return element.scrollHeight - element.scrollTop - element.clientHeight < 40
}

watch(
  () => props.items.length,
  async (_length, previous) => {
    // 首条记录出现时直接滚到底，不需要先判断位置
    if (previous === 0) {
      await nextTick()
      scroller.value?.scrollTo({ top: scroller.value.scrollHeight })
      return
    }

    const shouldFollow = isAtBottom()
    await nextTick()
    if (shouldFollow) {
      scroller.value?.scrollTo({ top: scroller.value.scrollHeight, behavior: 'smooth' })
    }
  },
)

function levelStyle(level: string) {
  return LEVEL_STYLES[level] ?? LEVEL_STYLES.info
}
</script>

<template>
  <div ref="scroller" class="timeline">
    <div v-if="!items.length && !running" class="empty">还没有执行记录。</div>

    <div
      v-for="(item, index) in items"
      :key="`${index}-${item.node}`"
      class="entry fade-in"
      :class="`entry--${levelStyle(item.level).tone}`"
    >
      <span class="entry__dot">{{ levelStyle(item.level).icon }}</span>

      <div class="entry__content">
        <div class="entry__head">
          <code class="entry__node">{{ item.node }}</code>
          <span v-if="item.elapsed_ms" class="entry__time">{{ formatMs(item.elapsed_ms) }}</span>
        </div>
        <p class="entry__message">{{ item.message }}</p>
      </div>
    </div>

    <div v-if="running" class="entry entry--pending">
      <span class="entry__dot"><span class="entry__blink" /></span>
      <div class="entry__content">
        <p class="entry__message">等待下一步…</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.timeline {
  max-height: 300px;
  overflow-y: auto;
  padding-right: 4px;
}

.entry {
  display: flex;
  gap: 10px;
  padding: 7px 0;
  border-bottom: 1px dashed var(--border);
}

.entry:last-child {
  border-bottom: none;
}

.entry__dot {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  margin-top: 2px;
  border-radius: 50%;
  font-size: 11px;
  font-weight: 700;
  background: var(--surface-sunken);
  color: var(--text-muted);
}

.entry--info .entry__dot {
  background: var(--accent-soft);
  color: var(--accent);
}

.entry--warning .entry__dot {
  background: var(--warn-soft);
  color: var(--warn);
}

.entry--error .entry__dot {
  background: var(--err-soft);
  color: var(--err);
}

.entry__content {
  min-width: 0;
  flex: 1;
}

.entry__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.entry__node {
  font-size: 11.5px;
  color: var(--text-secondary);
  background: var(--surface-sunken);
  padding: 1px 6px;
  border-radius: 4px;
}

.entry--warning .entry__node {
  background: var(--warn-soft);
  color: var(--warn);
}

.entry--error .entry__node {
  background: var(--err-soft);
  color: var(--err);
}

.entry__time {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

.entry__message {
  margin: 3px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text);
  word-break: break-word;
}

.entry--error .entry__message {
  color: var(--err);
}

.entry--warning .entry__message {
  color: var(--warn);
}

.entry--pending .entry__message {
  color: var(--text-muted);
}

.entry__blink {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  animation: blink 1s ease-in-out infinite;
}

@keyframes blink {
  0%,
  100% {
    opacity: 0.25;
  }
  50% {
    opacity: 1;
  }
}
</style>
