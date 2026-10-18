<script setup lang="ts">
import type { TraceStep } from '../types/contest.ts'
import { ApiError } from '../api/http.ts'
defineProps<{ steps: TraceStep[]; traceId?: string }>()
const operations: Record<TraceStep['operation'], string> = { intent: '意图识别', schema_linking: 'Schema Linking',
  clarification: '主动澄清', sql_generate: 'SQL生成', sql_validate: 'SQL校验', sql_execute: 'SQL执行及结果处理',
  rag_search: '文档检索', rag_synthesize: '答案生成', formula_extract: '公式提取', formula_bind: '参数绑定', formula_calculate: '公式计算' }
const states = { succeeded: '成功', failed: '失败', skipped: '跳过', unknown: '未知' }
/** 只显示白名单安全码，不输出任何未知上游原文。 */
function safeCode(code: string) { return new ApiError(code).code }
</script>
<template>
  <section class="timeline-section">
    <div class="panel-heading"><h3>真实工具步骤</h3><span class="pill">{{ steps.length }} 步</span></div>
    <p v-if="traceId" class="identifier">trace_id：{{ traceId }}</p>
    <p v-if="!steps.length" class="notice warn">当前没有步骤记录；没有记录不代表执行成功。</p>
    <ol v-else class="timeline">
      <li v-for="(step, i) in steps" :key="step.step_id" class="trace-step">
        <span class="step-marker">{{ i + 1 }}</span><div class="step-content">
          <div class="panel-heading"><strong>{{ operations[step.operation] }}</strong><span class="pill">{{ states[step.status] }}</span></div>
          <p>{{ step.tool }} · {{ step.operation }}<span v-if="step.sql_attempt !== null"> · SQL尝试 {{ step.sql_attempt }}</span></p>
          <p>耗时：{{ step.elapsed_ms === null ? '未统计' : `${step.elapsed_ms} ms` }} · 模型请求：{{ step.llm_calls ?? '未统计' }}</p>
          <p v-if="step.error_code" class="notice danger">{{ safeCode(step.error_code) }}</p>
          <small class="identifier">step_id：{{ step.step_id }}</small>
        </div>
      </li>
    </ol>
  </section>
</template>
