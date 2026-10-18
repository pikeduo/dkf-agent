<script setup lang="ts">
import { ref } from 'vue'
import { sql } from '../state/workbench.ts'
import TraceTimeline from '../components/TraceTimeline.vue'
const traceId = ref('')
</script>
<template>
  <div class="page stack"><header class="page-header"><div><div class="eyebrow">SAFE TRACE</div><h1 class="page-title">执行链路追踪</h1><p class="page-subtitle">只读取当前凭据会话的安全摘要；不会从输入输出猜测模型思维或执行步骤。</p></div></header>
    <section class="card card-pad stack"><p class="identifier">session_id：{{ sql.state.session?.session_id || '尚未创建会话' }}</p><label>后端 trace_id<input v-model="traceId" class="input" maxlength="36" :disabled="sql.state.busy"></label><button class="btn primary" :disabled="sql.state.busy || !sql.state.session || !traceId" @click="sql.loadTrace(traceId)">读取真实 Trace</button>
      <div v-if="sql.state.history.length" class="stack"><label>或选择本会话执行</label><button v-for="run in sql.state.history" :key="run.context.trace_id" class="btn outline identifier" :disabled="sql.state.busy" @click="traceId = run.context.trace_id; sql.loadTrace(traceId)">{{ run.status }} · {{ run.context.trace_id }}</button></div>
      <p v-if="sql.state.error" class="notice danger" role="alert">{{ sql.state.error }}</p>
    </section>
    <section v-if="sql.state.trace" class="card card-pad"><p class="identifier">run_id：{{ sql.state.trace.run_id }}<br>request_id：{{ sql.state.trace.request_id }}<br>承接 run：{{ sql.state.trace.reply_to_run_id ?? '未关联前次运行' }}</p><TraceTimeline :steps="sql.state.trace.trace" :trace-id="sql.state.trace.trace_id" /></section>
    <p v-else class="notice warn">未取得 Trace 快照；可能尚未查询、没有记录或无权读取，均不能表示执行成功。</p>
  </div>
</template>
