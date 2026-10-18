<script setup lang="ts">
import type { SQLResult } from '../types/contest.ts'
import { cleanCell } from '../utils/format.ts'
defineProps<{ result: SQLResult }>()
</script>
<template>
  <section>
    <p v-if="result.status === 'failed'" class="notice danger">SQL 未成功执行，没有可发布的结果表格。</p>
    <template v-else>
      <div class="panel-heading"><h3>查询结果</h3><span class="pill">{{ result.returned_rows }} 行 · {{ result.status }}</span></div>
      <div class="table-scroll">
        <table class="data-table"><thead><tr><th v-for="(col, i) in result.columns" :key="i">{{ col.name }}<small>{{ col.data_type }}</small></th></tr></thead>
          <tbody><tr v-for="(row, i) in result.rows" :key="i"><td v-for="(cell, j) in row" :key="j" :class="{ 'null-cell': cell === null }">{{ cleanCell(cell) }}</td></tr></tbody>
        </table>
      </div>
      <p v-if="result.status === 'empty'" class="empty">本次查询成功，结果为空；没有以示例数据填充。</p>
      <p v-if="result.truncated" class="notice warn">后端已确认结果截断，仅返回前 {{ result.returned_rows }} 行。此页展示全部已返回行。</p>
      <p v-else class="hint">后端截断标记为 false；数值与日期按无损字符串显示，NULL 与空字符串分开显示。</p>
    </template>
  </section>
</template>
