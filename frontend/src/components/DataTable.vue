<script setup lang="ts">
/**
 * 结果数据表格。
 *
 * 后端返回的 rows 是二维数组而不是对象数组，展示时要靠 columns 的下标对齐。
 * 这么设计是为了省传输体积：字典数组要为每一行重复一遍列名，
 * 上千行时体积能差好几倍。代价就是前端必须按下标取值，不能拿字段名去取。
 */

import { computed } from 'vue'

import { formatCell } from '@/utils/format'

const props = defineProps<{
  columns: string[]
  rows: unknown[][]
  /** 最多渲染多少行，避免上万行把页面拖死 */
  limit?: number
}>()

const maxRows = computed(() => props.limit ?? 500)

const visibleRows = computed(() => props.rows.slice(0, maxRows.value))

const hiddenCount = computed(() => Math.max(props.rows.length - maxRows.value, 0))

/** 每列是否全为数值，用于决定右对齐。数字右对齐才方便比大小 */
const numericColumns = computed(() =>
  props.columns.map((_, index) =>
    visibleRows.value.length > 0 &&
    visibleRows.value.every((row) => {
      const value = row[index]
      return value === null || value === undefined || typeof value === 'number'
    }),
  ),
)

/**
 * 导出为 CSV。
 *
 * 两个必须处理的细节：
 *   1. 字段里含逗号、引号、换行时要用双引号包裹并把内部引号翻倍，
 *      否则 Excel 打开会串列。这是 CSV 的标准转义规则。
 *   2. 加 UTF-8 BOM。不加的话 Excel 会按本地编码解读，中文全变乱码——
 *      这个坑几乎每个人都会踩一次。
 */
function exportCsv(): void {
  const escapeCell = (value: unknown): string => {
    const text = value === null || value === undefined ? '' : String(value)
    if (/[",\n\r]/.test(text)) {
      return `"${text.replace(/"/g, '""')}"`
    }
    return text
  }

  const lines = [
    props.columns.map(escapeCell).join(','),
    ...props.rows.map((row) => row.map(escapeCell).join(',')),
  ]

  const blob = new Blob([`\uFEFF${lines.join('\r\n')}`], {
    type: 'text/csv;charset=utf-8;',
  })

  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `query_result_${new Date().toISOString().slice(0, 10)}.csv`
  anchor.click()
  // 用完立刻释放，否则 Blob 会一直占着内存直到页面关闭
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="data-table-wrap">
    <div class="data-table-head">
      <span class="section-title" style="margin: 0">
        原始数据（{{ rows.length }} 行）
      </span>
      <button class="btn btn--ghost btn--sm" type="button" @click="exportCsv">
        导出 CSV
      </button>
    </div>

    <div v-if="!rows.length" class="empty">没有数据行。</div>

    <div v-else class="table-wrap">
      <table class="data-table">
        <thead>
          <tr>
            <th v-for="(column, index) in columns" :key="column" :class="{ 'num': numericColumns[index] }">
              {{ column }}
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in visibleRows" :key="rowIndex">
            <td
              v-for="(column, columnIndex) in columns"
              :key="column"
              :class="{ 'num': numericColumns[columnIndex] }"
            >
              {{ formatCell(row[columnIndex]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p v-if="hiddenCount" class="hint">
      为保证渲染性能，只显示了前 {{ maxRows }} 行，另有 {{ hiddenCount }} 行未渲染。
      完整数据请导出 CSV 查看。
    </p>
  </div>
</template>

<style scoped>
.data-table-wrap {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.data-table-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.num {
  text-align: right;
}
</style>
