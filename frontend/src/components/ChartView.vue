<script setup lang="ts">
/**
 * 图表渲染。
 *
 * 后端只给"画什么类型、用哪两列"的配置，图形由前端自己画。
 * 这么分工的理由：
 *   如果后端直接把 Plotly 的 Figure 对象序列化过来，
 *   传输体积大（几百 KB 起）、前端也改不动它的样式，
 *   而且图和数据是两套坐标，想加个提示框都要回到后端改代码。
 *   给出配置之后，前端用 ECharts 渲染只有几十 KB，还能自由做交互。
 *
 * ECharts 用按需引入而不是 `import * as echarts from 'echarts'`：
 *   全量引入会把所有图表类型和组件（地图、关系图、3D 等）都打进来，
 *   体积接近 1MB。这里只用到 4 种图表 + 4 个组件，体积能降到三分之一。
 */

import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { EChartsOption } from 'echarts'

import type { ChartPayload, ExecutionPayload } from '@/types/api'
import { formatCell } from '@/utils/format'

echarts.use([
  BarChart,
  LineChart,
  PieChart,
  ScatterChart,
  GridComponent,
  LegendComponent,
  TitleComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = defineProps<{
  chart: ChartPayload
  execution: ExecutionPayload
}>()

const container = ref<HTMLElement | null>(null)

// 用 ReturnType<typeof echarts.init> 而不是显式写类型名：
// ECharts 的实例类型在不同小版本里叫法变过（ECharts / EChartsType），
// 这样写不会因为升级而编译失败
let instance: ReturnType<typeof echarts.init> | null = null
let observer: ResizeObserver | null = null

/** 类目太多时图会挤成一团，超过这个数量就只画前面的部分 */
const MAX_SERIES_ITEMS = 40

const chartType = computed(() => (props.chart.chart_type || 'table').toLowerCase())

/** table 类型不画图，由表格组件负责展示 */
const needsChart = computed(() => chartType.value !== 'table' && chartType.value !== 'metric')

const rows = computed(() => props.execution.rows ?? [])
const columns = computed(() => props.execution.columns ?? [])

/**
 * 定位 x / y 对应的列下标。
 *
 * 为什么不能直接信任 x_field / y_field：
 *   这两个字段是模型挑的，偶尔会挑到不存在的列名（比如把别名的中文写成英文）。
 *   一旦 indexOf 返回 -1，图表就会画成空白，而用户完全看不出原因。
 *   所以这里做两级兜底：先用字段名匹配，匹配不到再从数值列里推断。
 */
const indexes = computed(() => {
  const columnList = columns.value
  const rowList = rows.value

  if (!columnList.length) return { xIndex: -1, yIndex: -1 }

  const isNumericColumn = (index: number): boolean =>
    rowList.length > 0 &&
    rowList.every((row) => {
      const value = row[index]
      return value === null || value === undefined || typeof value === 'number'
    })

  const declaredX = columnList.indexOf(props.chart.x_field)
  const declaredY = columnList.indexOf(props.chart.y_field)

  let xIndex = declaredX
  let yIndex = declaredY

  if (yIndex < 0) {
    yIndex = columnList.findIndex((_, index) => index !== xIndex && isNumericColumn(index))
  }

  // metric 是"一个大数字卡片"，只要定位到那个数值就行，横轴没有意义。
  // 必须在这里提前返回：单值结果只有一列，下面的 x 兜底会让 x 落到同一列，
  // 接着触发"x === y"修正，反而把 yIndex 改回 -1，卡片就显示成"没有可展示的数值"
  if (chartType.value === 'metric') {
    return { xIndex: -1, yIndex }
  }

  if (xIndex < 0) {
    // x 兜底：优先选第一个非数值列（通常是类目/时间），没有就退回第一列
    xIndex = columnList.findIndex((_, index) => index !== yIndex && !isNumericColumn(index))
    if (xIndex < 0) xIndex = 0
  }

  // 兜底之后再校验一次，避免出现 x === y 把图画成一条直线
  if (yIndex < 0 || yIndex === xIndex) {
    yIndex = columnList.findIndex((_, index) => index !== xIndex && isNumericColumn(index))
  }

  return { xIndex, yIndex }
})

/** 通用提示框：数量级大的数字加上千分位更好读 */
const PALETTE = ['#2f6bed', '#15925a', '#b8730a', '#8b5cf6', '#0ea5e9', '#cc3b3b', '#64748b']

function buildOption(): EChartsOption {
  const { xIndex, yIndex } = indexes.value
  const rowList = rows.value.slice(0, MAX_SERIES_ITEMS)
  const chart = chartType.value

  const base: EChartsOption = {
    color: PALETTE,
    textStyle: {
      fontFamily:
        "Inter, system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif",
      fontSize: 12,
    },
    grid: { left: 12, right: 20, top: 42, bottom: 8, containLabel: true },
    tooltip: {
      trigger: chart === 'pie' ? 'item' : 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (value) =>
        typeof value === 'number' ? value.toLocaleString('zh-CN') : String(value ?? ''),
    },
    title: {
      text: props.chart.title || '查询结果可视化',
      left: 0,
      top: 0,
      textStyle: { fontSize: 13, fontWeight: 600, color: '#16202f' },
    },
  }

  if (xIndex < 0 || yIndex < 0) {
    return base
  }

  const categories = rowList.map((row) => formatCell(row[xIndex]))
  const values = rowList.map((row) => {
    const value = row[yIndex]
    return typeof value === 'number' ? value : Number(value) || 0
  })

  if (chart === 'pie') {
    return {
      ...base,
      tooltip: { trigger: 'item' },
      legend: { type: 'scroll', orient: 'vertical', right: 0, top: 30, bottom: 0, itemWidth: 9, itemHeight: 9 },
      series: [
        {
          type: 'pie',
          radius: ['42%', '68%'],
          center: ['38%', '56%'],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: '#fff', borderWidth: 2 },
          label: { formatter: '{b}\n{d}%', fontSize: 11, lineHeight: 15 },
          data: categories.map((name, index) => ({ name, value: values[index] })),
        },
      ],
    }
  }

  if (chart === 'scatter') {
    return {
      ...base,
      xAxis: {
        type: 'value',
        name: columns.value[xIndex] ?? '',
        nameLocation: 'middle',
        nameGap: 26,
        axisLine: { lineStyle: { color: '#cbd5e1' } },
        splitLine: { lineStyle: { color: '#eef2f8' } },
      },
      yAxis: {
        type: 'value',
        name: columns.value[yIndex] ?? '',
        axisLine: { lineStyle: { color: '#cbd5e1' } },
        splitLine: { lineStyle: { color: '#eef2f8' } },
      },
      series: [
        {
          type: 'scatter',
          symbolSize: 10,
          data: rowList.map((row) => [
            Number(row[xIndex]) || 0,
            typeof row[yIndex] === 'number' ? (row[yIndex] as number) : 0,
          ]),
        },
      ],
    }
  }

  const isLine = chart === 'line'

  return {
    ...base,
    xAxis: {
      type: 'category',
      data: categories,
      // 类目多时倾斜标签，否则会重叠成一团黑
      axisLabel: { rotate: categories.length > 8 ? 30 : 0, interval: 0, hideOverlap: true },
      axisLine: { lineStyle: { color: '#cbd5e1' } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      name: columns.value[yIndex] ?? '',
      nameTextStyle: { color: '#8894ab' },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: '#eef2f8' } },
      axisLabel: {
        formatter: (value: string | number) => {
          const numeric = Number(value)
          return Math.abs(numeric) >= 10000 ? `${(numeric / 10000).toFixed(0)}万` : String(value)
        },
      },
    },
    series: [
      {
        type: isLine ? 'line' : 'bar',
        data: values,
        smooth: isLine,
        symbolSize: 6,
        barMaxWidth: 46,
        itemStyle: { borderRadius: isLine ? 0 : [5, 5, 0, 0] },
        areaStyle: isLine ? { opacity: 0.12 } : undefined,
      },
    ],
  }
}

function render(): void {
  if (!container.value) return
  if (!needsChart.value) return

  if (!instance) {
    instance = echarts.init(container.value)
  }

  instance.setOption(buildOption(), true)
  instance.resize()
}

onMounted(() => {
  render()

  // 容器尺寸变化时（侧栏折叠、窗口缩放）必须显式 resize，
  // ECharts 内部用 canvas 绘制，不会跟着 CSS 自动重排
  if (container.value) {
    observer = new ResizeObserver(() => instance?.resize())
    observer.observe(container.value)
  }
})

watch(() => [props.chart, props.execution], render, { deep: true })

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  // 不 dispose 会留下悬空的 canvas 和事件监听，反复问答后内存持续上涨
  instance?.dispose()
  instance = null
})

/** metric 类型：只有一个关键数字，用大字号直接呈现 */
const metricView = computed(() => {
  const { yIndex, xIndex } = indexes.value
  const firstRow = rows.value[0]

  if (!firstRow || yIndex < 0) return null

  return {
    label: columns.value[xIndex] && rows.value.length === 1 ? formatCell(firstRow[xIndex]) : '',
    value: formatCell(firstRow[yIndex]),
    suffix: columns.value[yIndex] ?? '',
  }
})
</script>

<template>
  <div class="chart-view">
    <div v-if="chartType === 'metric'" class="metric-view">
      <template v-if="metricView">
        <div class="metric-view__label">{{ metricView.label || metricView.suffix }}</div>
        <div class="metric-view__value">{{ metricView.value }}</div>
      </template>
      <div v-else class="empty">没有可展示的数值。</div>
    </div>

    <div v-else-if="chartType === 'table'" class="empty">
      本次结果适合用表格呈现，图表配置选择了 table 类型。
    </div>

    <div v-else ref="container" class="chart-view__canvas" />

    <div class="chart-view__meta">
      <p v-if="chart.reason" class="chart-view__reason">
        <span class="chart-view__tag">{{ chartType }}</span>{{ chart.reason }}
      </p>
      <p v-for="note in chart.notes" :key="note" class="chart-view__note">已自动修正：{{ note }}</p>
      <p v-if="rows.length > MAX_SERIES_ITEMS" class="chart-view__note">
        为保证可读性，图表只画了前 {{ MAX_SERIES_ITEMS }} 条记录，完整数据见下方表格。
      </p>
    </div>
  </div>
</template>

<style scoped>
.chart-view {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.chart-view__canvas {
  width: 100%;
  height: 320px;
}

.metric-view {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 34px 16px;
  border-radius: var(--radius);
  background: linear-gradient(180deg, var(--accent-soft), var(--surface));
  border: 1px solid var(--accent-border);
}

.metric-view__label {
  font-size: 12.5px;
  color: var(--text-secondary);
}

.metric-view__value {
  font-size: 40px;
  font-weight: 650;
  line-height: 1.2;
  letter-spacing: -0.02em;
  color: var(--accent-strong);
  font-variant-numeric: tabular-nums;
}

.chart-view__meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.chart-view__reason {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0;
  font-size: 12.5px;
  color: var(--text-secondary);
  line-height: 1.6;
}

.chart-view__tag {
  flex: none;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--surface-sunken);
  border: 1px solid var(--border);
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-muted);
}

.chart-view__note {
  margin: 0;
  font-size: 12px;
  color: var(--warn);
}
</style>
