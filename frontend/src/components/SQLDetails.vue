<script setup lang="ts">
import type { CatalogSource, SQLResult } from '../types/contest.ts'
import { ApiError } from '../api/http.ts'
import ResultTable from './ResultTable.vue'
import SchemaLinks from './SchemaLinks.vue'
defineProps<{ result: SQLResult; source?: CatalogSource }>()
/** 仅显示已注册的安全错误类别。 */
function errorCode(code: string) { return new ApiError(code).code }
</script>
<template>
  <div class="stack">
    <div v-if="!result.attempts.length" class="notice warn">本次没有 SQL 生成或执行尝试。</div>
    <article v-for="attempt in result.attempts" :key="attempt.attempt" class="card card-pad">
      <div class="panel-heading"><h3>SQL 尝试 {{ attempt.attempt }}</h3><span class="pill">{{ attempt.attempt === 2 ? '授权修复尝试' : '首次生成' }}</span></div>
      <p>校验 {{ attempt.validation }} · 执行 {{ attempt.execution }}<span v-if="attempt.error_code"> · {{ errorCode(attempt.error_code) }}</span></p>
      <h4>模型生成 SQL</h4><pre>{{ attempt.generated_sql }}</pre>
      <h4>实际交给数据库的 SQL</h4><pre v-if="attempt.executed_sql">{{ attempt.executed_sql }}</pre><p v-else class="notice warn">未执行，无实际 SQL 凭证。</p>
      <details v-if="Object.keys(attempt.parameters).length"><summary>实际绑定参数</summary><pre>{{ JSON.stringify(attempt.parameters, null, 2) }}</pre></details>
      <p v-else class="hint">本次后端未返回绑定参数；不声称使用了参数化查询。</p>
    </article>
    <section class="card card-pad"><ResultTable :result="result" /></section>
    <section class="card card-pad"><SchemaLinks :links="result.schema_links" :source="source" /></section>
  </div>
</template>
