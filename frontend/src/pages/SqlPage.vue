<script setup lang="ts">
import { sql } from '../state/workbench.ts'
import SQLDetails from '../components/SQLDetails.vue'
import { responseTitle } from '../utils/format.ts'
defineEmits<{ navigate: [page: string] }>()
</script>
<template>
  <div class="page stack"><header class="page-header"><div><div class="eyebrow">EXECUTION EVIDENCE</div><h1 class="page-title">NL2SQL 查询凭证</h1><p class="page-subtitle">与智能问数共享当前会话；本页显示最后一次响应，不另外生成或执行 SQL。</p></div><button class="btn primary" @click="$emit('navigate', 'chat')">前往问数与澄清</button></header>
    <p v-if="!sql.state.response" class="empty card">尚未收到本次结构化结果。</p>
    <template v-else><p class="notice">{{ responseTitle(sql.state.response) }} · {{ sql.state.response.context.trace_id }}</p>
      <SQLDetails v-if="sql.state.response.result.kind === 'sql'" :result="sql.state.response.result" :source="sql.state.sources[sql.state.selected]" />
      <p v-else class="notice warn">本次未返回 SQL 结果；请在问数页处理澄清、授权或服务失败。</p>
    </template>
  </div>
</template>
