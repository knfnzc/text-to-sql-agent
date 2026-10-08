<script setup lang="ts">
/**
 * 结论解读面板。
 *
 * 结论文本由后端调模型生成，是 Markdown 格式。
 * 这里必须先渲染再净化才能插入页面——直接把模型输出塞进 v-html
 * 等于留了一条从输入框直通页面的脚本注入通道。净化逻辑在 utils/markdown.ts。
 */

import { computed } from 'vue'

import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  answer: string
  /** 执行失败的原始原因；为空表示没有失败 */
  error: string
  running: boolean
  /** 是否已经有最终结果 */
  hasResult: boolean
}>()

const html = computed(() => renderMarkdown(props.answer))

const failed = computed(() => Boolean(props.error) && !props.answer)
</script>

<template>
  <div class="answer">
    <!-- 三种状态分开处理：失败、生成中、正常。
         把"失败"和"还没生成"混在一起会让用户不知道要不要重试 -->
    <div v-if="failed" class="answer__failure">
      <div class="answer__failure-title">查询执行失败</div>
      <p class="answer__failure-detail">{{ error }}</p>
      <p class="answer__failure-tip">
        系统会自动带着这条报错重新生成 SQL（最多若干次）。如果仍然失败，
        可以换一种问法，或在问题里补充明确的字段口径与时间范围。
      </p>
    </div>

    <div v-else-if="!answer && running" class="answer__loading">
      <span class="answer__dots"><i /><i /><i /></span>
      正在生成结论与图表配置…
    </div>

    <div v-else-if="!answer && !hasResult" class="empty">
      提问后这里会显示对结果的解读。
    </div>

    <div v-else-if="!answer" class="empty">本次没有生成结论解读。</div>

    <!-- eslint-disable-next-line vue/no-v-html -->
    <div v-else class="markdown-body" v-html="html" />
  </div>
</template>

<style scoped>
.answer {
  font-size: 13.5px;
}

.answer__failure {
  padding: 12px 14px;
  border-radius: var(--radius);
  background: var(--err-soft);
  border: 1px solid #f5c6c6;
}

.answer__failure-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--err);
  margin-bottom: 5px;
}

.answer__failure-detail {
  margin: 0 0 8px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--err);
  word-break: break-all;
}

.answer__failure-tip {
  margin: 0;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.7;
}

.answer__loading {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-muted);
  font-size: 13px;
}

.answer__dots {
  display: inline-flex;
  gap: 3px;
}

.answer__dots i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--accent);
  animation: dot-bounce 1.1s ease-in-out infinite;
}

.answer__dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.answer__dots i:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes dot-bounce {
  0%,
  100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  50% {
    transform: translateY(-4px);
    opacity: 1;
  }
}
</style>
