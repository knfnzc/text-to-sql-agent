<script setup lang="ts">
/**
 * 执行步骤条。
 *
 * 为什么要有这个组件：
 *    这是前后端分离后前端"最有存在感"的一块。
 *    Streamlit 版本用的是同步 invoke，用户只能盯着一个转圈图标干等十几秒；
 *    这里消费 SSE 事件，每完成一个节点就点亮一格，
 *    用户始终知道系统现在在做什么、还剩几步。
 *
 * 数据来源是后端的 stage 事件，前端不猜进度——
 * 进度如果由前端伪造（比如用定时器按时间预估），一旦某步变慢就会自相矛盾。
 */

import { computed } from 'vue'

import { STAGE_LABELS, STAGE_ORDER } from '@/utils/format'

const props = defineProps<{
  /** 各阶段的当前状态。没有键表示还没执行到 */
  statuses: Record<string, 'running' | 'done'>
  /** 是否处于执行中，用于控制连接线的流动动画 */
  running: boolean
  /** 流程是否以失败告终，失败时最后点亮的那一格标红 */
  failed: boolean
}>()

interface StepView {
  node: string
  label: string
  state: 'pending' | 'running' | 'done' | 'failed'
  index: number
}

/** 最后一个有状态的阶段下标。失败标记要落在这格上，而不是落在任意一格 */
const lastActiveIndex = computed(() => {
  let last = -1
  STAGE_ORDER.forEach((node, index) => {
    if (props.statuses[node]) last = index
  })
  return last
})

const steps = computed<StepView[]>(() =>
  STAGE_ORDER.map((node, index) => {
    const status = props.statuses[node]
    let state: StepView['state'] = 'pending'

    if (status === 'done') {
      state = props.failed && index === lastActiveIndex.value ? 'failed' : 'done'
    } else if (status === 'running') {
      state = 'running'
    }

    return {
      node,
      label: STAGE_LABELS[node] ?? node,
      state,
      index,
    }
  }),
)

/** 已完成的阶段数，用于生成"3/6"这样的进度文案 */
const doneCount = computed(
  () => STAGE_ORDER.filter((node) => props.statuses[node] === 'done').length,
)
</script>

<template>
  <div class="stage-bar" :class="{ 'stage-bar--running': running }">
    <div class="stage-bar__head">
      <span class="stage-bar__title">执行链路</span>
      <span class="stage-bar__count">{{ doneCount }} / {{ STAGE_ORDER.length }}</span>
    </div>

    <ol class="stage-bar__list">
      <li
        v-for="step in steps"
        :key="step.node"
        class="stage"
        :class="`stage--${step.state}`"
      >
        <span class="stage__marker">
          <svg v-if="step.state === 'done'" viewBox="0 0 16 16" class="stage__icon">
            <path
              d="M3.5 8.5l3 3 6-6.5"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
            />
          </svg>
          <svg v-else-if="step.state === 'failed'" viewBox="0 0 16 16" class="stage__icon">
            <path
              d="M4.5 4.5l7 7m0-7l-7 7"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
            />
          </svg>
          <span v-else-if="step.state === 'running'" class="stage__pulse" />
          <span v-else class="stage__index">{{ step.index + 1 }}</span>
        </span>

        <span class="stage__label">{{ step.label }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.stage-bar {
  padding: 14px 18px 16px;
}

.stage-bar__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: 12px;
}

.stage-bar__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-secondary);
}

.stage-bar__count {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-muted);
}

.stage-bar__list {
  display: flex;
  align-items: flex-start;
  gap: 0;
  margin: 0;
  padding: 0;
  list-style: none;
}

.stage {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

/* 连接线：画在每一格的左侧，第一格不画 */
.stage + .stage::before {
  content: '';
  position: absolute;
  top: 13px;
  right: 50%;
  left: -50%;
  height: 2px;
  background: var(--border);
}

.stage--done + .stage::before,
.stage--running + .stage::before {
  background: var(--accent-border);
}

/* 正在执行时，已点亮格子之间的连线做流动效果。
   这是个纯装饰，但它让"还在跑"这件事在视觉上持续可见，
   否则界面长时间没有事件推送时会被误认为卡死 */
.stage-bar--running .stage--done + .stage::before {
  background: linear-gradient(
    90deg,
    var(--accent-border) 0%,
    var(--accent) 50%,
    var(--accent-border) 100%
  );
  background-size: 200% 100%;
  animation: stage-flow 1.6s linear infinite;
}

@keyframes stage-flow {
  from {
    background-position: 100% 0;
  }
  to {
    background-position: -100% 0;
  }
}

.stage__marker {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 27px;
  height: 27px;
  border-radius: 50%;
  border: 2px solid var(--border);
  background: var(--surface);
  color: var(--text-muted);
  font-size: 11.5px;
  font-weight: 600;
  transition: background 0.2s, border-color 0.2s, color 0.2s;
}

.stage__icon {
  width: 14px;
  height: 14px;
}

.stage--done .stage__marker {
  background: var(--accent);
  border-color: var(--accent);
  color: #fff;
}

.stage--running .stage__marker {
  border-color: var(--accent);
  background: var(--accent-soft);
  color: var(--accent);
}

.stage--failed .stage__marker {
  background: var(--err);
  border-color: var(--err);
  color: #fff;
}

.stage--pending .stage__marker {
  background: var(--surface-soft);
}

.stage__pulse {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--accent);
  animation: stage-pulse 1.1s ease-in-out infinite;
}

@keyframes stage-pulse {
  0%,
  100% {
    transform: scale(0.7);
    opacity: 0.5;
  }
  50% {
    transform: scale(1.1);
    opacity: 1;
  }
}

.stage__label {
  font-size: 11.5px;
  line-height: 1.4;
  text-align: center;
  color: var(--text-muted);
  padding: 0 4px;
}

.stage--done .stage__label {
  color: var(--text-secondary);
}

.stage--running .stage__label {
  color: var(--accent-strong);
  font-weight: 600;
}

.stage--failed .stage__label {
  color: var(--err);
  font-weight: 600;
}

/* 窄屏时纵向排列，横向塞 6 格会挤成一团看不清 */
@media (max-width: 860px) {
  .stage-bar__list {
    flex-direction: column;
    gap: 2px;
  }

  .stage {
    flex-direction: row;
    justify-content: flex-start;
    gap: 10px;
    padding: 3px 0;
  }

  .stage + .stage::before {
    top: -8px;
    bottom: 50%;
    left: 13px;
    right: auto;
    width: 2px;
    height: auto;
  }

  .stage__label {
    text-align: left;
  }
}
</style>
