/**
 * 界面用到的常量与格式化工具。
 */

/** 执行链路的固定顺序，用来渲染步骤条 */
export const STAGE_ORDER: string[] = [
  'parse_intent',
  'retrieve_schema',
  'generate_sql',
  'validate',
  'execute',
  'render',
]

/**
 * 节点名到中文说明的映射。
 *
 * 后端的 SSE 事件里已经带了一份 label，正常情况下直接用后端给的就行。
 * 这里再保留一份是为了两个场景：
 *   1. 步骤条需要在还没收到事件时就把 6 个阶段全部画出来（占位待点亮）；
 *   2. 演示模式下没有后端，得靠本地这份文案。
 * 内容与 backend/app/api/service.py 的 STAGE_LABELS 保持一致。
 */
export const STAGE_LABELS: Record<string, string> = {
  parse_intent: '解析问题意图',
  retrieve_schema: '检索相关表结构',
  generate_sql: '生成 SQL',
  validate: 'SQL 安全校验',
  execute: '执行查询',
  render: '生成结论与图表',
  give_up: '放弃并说明原因',
}

/** 轨迹级别的展示样式：图标 + 颜色语义 */
export const LEVEL_STYLES: Record<string, { icon: string; tone: string }> = {
  info: { icon: '·', tone: 'info' },
  warning: { icon: '!', tone: 'warning' },
  error: { icon: '×', tone: 'error' },
}

/**
 * 把数值格式化成便于阅读的形式。
 *
 * 处理逻辑：
 *   整数 -> 原样输出（不加小数点，避免「1,200 行」看起来很怪）
 *   小数 -> 最多保留两位
 *   非数字 -> 转成字符串原样输出
 * 金额类字段后端已经 ROUND 过，这里不再做四舍五入的二次处理。
 */
export function formatCell(value: unknown): string {
  if (value === null || value === undefined) {
    // 空值显式显示成 (空)，而不是留白。
    // 留白的话没法区分"这一格是 0"和"这一格没数据"
    return '(空)'
  }

  if (typeof value === 'number') {
    if (Number.isInteger(value)) {
      return value.toLocaleString('zh-CN')
    }
    return value.toLocaleString('zh-CN', {
      minimumFractionDigits: 0,
      maximumFractionDigits: 2,
    })
  }

  if (typeof value === 'boolean') {
    return value ? '是' : '否'
  }

  return String(value)
}

/** 毫秒数格式化成可读文本 */
export function formatMs(ms: number | undefined): string {
  if (!ms || ms <= 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

/** 大数加千分位 */
export function formatCount(value: number | undefined): string {
  if (value === undefined || value === null) return '—'
  return value.toLocaleString('zh-CN')
}

/** 按 token 用量字典算出总量 */
export function totalTokens(usage: Record<string, number | undefined> | undefined): number {
  if (!usage) return 0
  return (usage.prompt_tokens ?? 0) + (usage.completion_tokens ?? 0)
}
