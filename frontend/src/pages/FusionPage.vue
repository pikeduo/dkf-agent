<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { sql, knowledgeApi, unifiedAgent } from '../state/workbench.ts'
import { createKnowledgeWorkspace } from '../state/knowledge.ts'
import ResultTable from '../components/ResultTable.vue'
import KnowledgeScope from '../components/KnowledgeScope.vue'
import KnowledgeAnswer from '../components/KnowledgeAnswer.vue'
import { cleanCell } from '../utils/format.ts'
defineEmits<{ navigate: [page: string] }>()
const model = createKnowledgeWorkspace(knowledgeApi)
const k = model.state
const rowIndex = ref(-1)
const colIndex = ref(-1)
const confirmed = ref(false)
const confirmation = ref('')
const result = computed(() => sql.state.response?.result.kind === 'sql' && sql.state.response.result.status === 'success' ? sql.state.response.result : null)
const entity = computed(() => result.value?.rows[rowIndex.value]?.[colIndex.value] ?? null)
const eligible = computed(() => entity.value !== null
  && (typeof entity.value !== 'string' || !!entity.value.trim()) && cleanCell(entity.value).length <= 160)
const binding = computed(() => `${sql.state.response?.context.run_id ?? ''}:${rowIndex.value}:${colIndex.value}:${entity.value ?? ''}`)
// 源运行或单元格改变后使人工确认及所有文档结果失效。
watch(binding, () => {
  // 旧实体不可继续触发文档检索。
  confirmed.value = false; confirmation.value = ''; model.invalidate(); k.question = ''
})
onMounted(() => { void model.bases() })

/** 用户明确确认本次真实单元格，再将该实体带入可编辑的文档问题。 */
function confirmEntity() {
  if (!result.value || !eligible.value || entity.value === null) return
  confirmed.value = true; confirmation.value = binding.value
  model.invalidate(); k.question = `请根据文档提供与“${cleanCell(entity.value)}”有关的证据。`
}
</script>
<template>
  <div class="page stack"><header class="page-header"><div><div class="eyebrow">HUMAN CONFIRMED CROSS-SOURCE DEMO</div><h1 class="page-title">人工确认的跨源联动</h1><p class="page-subtitle">SQL查询 → 人工确认实体 → 文档检索，保留两个服务各自的真实结果。</p></div></header>
    <p class="notice warn">本页为人工两阶段演示，未实现自动跨源规划。统一 Agent：{{ unifiedAgent.enabled ? '已接入' : '未接入，仅冻结契约' }}。</p>
    <div class="split"><section class="card card-pad stack"><div class="panel-heading"><h3>01 · 真实 SQL 结果</h3><button class="btn outline" @click="$emit('navigate', 'chat')">前往问数</button></div>
      <template v-if="result"><p class="identifier">run_id：{{ sql.state.response?.context.run_id }} · trace_id：{{ sql.state.response?.context.trace_id }}</p><ResultTable :result="result" />
        <label>选择实体行<select v-model.number="rowIndex" class="select" :disabled="k.busy"><option :value="-1">请明确选择</option><option v-for="(_, i) in result.rows" :key="i" :value="i">第 {{ i + 1 }} 行</option></select></label>
        <label>选择实体列<select v-model.number="colIndex" class="select" :disabled="k.busy"><option :value="-1">请明确选择</option><option v-for="(col, i) in result.columns" :key="i" :value="i">{{ col.name }}</option></select></label>
        <p>选定实体：{{ entity === null ? '未选择或 NULL，不可联动' : cleanCell(entity) }}</p><button class="btn primary" :disabled="k.busy || !eligible" @click="confirmEntity">人工确认此实体并准备文档检索</button>
      </template><p v-else class="empty">需先在问数页取得非空的真实 SQL 结果，当前没有可联动实体。</p>
    </section>
    <section class="card card-pad stack"><h3>02 · 文档范围及问题</h3><KnowledgeScope :model="model" /><p v-if="confirmed" class="identifier">人工确认绑定：{{ confirmation }}</p>
      <textarea v-model="k.question" class="textarea" rows="4" maxlength="4000" :disabled="k.busy || !confirmed" @input="model.invalidate"></textarea>
      <p class="hint">可补充文档问题；必须保留已确认实体。仅检索不调用云端合成。</p>
      <label class="check"><input v-model="k.allowCloud" type="checkbox" :disabled="k.busy || !confirmed">仅本次允许文档问题及证据外传，并接受生成费用</label>
      <div class="toolbar"><button class="btn outline" :disabled="k.busy || !confirmed || confirmation !== binding || !k.docsLoaded || entity === null || !k.question.includes(String(entity))" @click="model.run('search')">检索实体文档证据</button><button class="btn primary" :disabled="k.busy || !confirmed || confirmation !== binding || !k.docsLoaded || entity === null || !k.question.includes(String(entity))" @click="model.run('query')">请求文档回答</button></div>
      <p v-if="k.error" class="notice danger" role="alert">{{ k.error }}</p>
      <KnowledgeAnswer :search="k.search" :answer="k.answer" :question="k.submittedQuestion" />
    </section></div>
  </div>
</template>
