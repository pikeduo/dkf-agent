<script setup lang="ts">
import { reactive, ref, watch } from 'vue'
import { credentials, sql } from '../state/workbench.ts'
import type { ClarificationAnswer } from '../types/contest.ts'
import { responseTitle } from '../utils/format.ts'
import { ApiError, safeMessage } from '../api/http.ts'
import TraceTimeline from '../components/TraceTimeline.vue'
import SchemaLinks from '../components/SchemaLinks.vue'
import SQLDetails from '../components/SQLDetails.vue'
import { noCloud } from '../state/sql.ts'
const s = sql.state
const choices = reactive<Record<string, string>>({})
const texts = reactive<Record<string, string>>({})
const consent = reactive({ transfer: false, charges: false, provider: 'contest', calls: 1, tokens: 4096, repair: false })
const localError = ref('')
// 新澄清版本不得继承旧题答案，用户每次看到的都是当前待答版本。
watch(() => s.pending?.clarification_id + ':' + s.pending?.revision, () => {
  // 清除旧题客户端输入；已确认槽位只展示后端 context。
  for (const key of Object.keys(choices)) delete choices[key]
  for (const key of Object.keys(texts)) delete texts[key]
})
watch(() => s.session?.session_id + ':' + s.originalQuestion, () => {
  // 会话或任务改变须重新确认费用，不能继承旧勾选。
  consent.transfer = false; consent.charges = false
})

/** 候选ID或自由文本二选一，允许部分回答，待答状态由后端继续维护。 */
async function reply() {
  localError.value = ''
  if (!s.pending) return
  const answers: ClarificationAnswer[] = []
  for (const q of s.pending.questions) {
    const option = choices[q.question_id]
    if (option && option !== '__text__') answers.push({ question_id: q.question_id, option_id: option })
    else if (q.allow_text && texts[q.question_id]?.trim()) answers.push({ question_id: q.question_id, text: texts[q.question_id] })
  }
  if (!answers.length) { localError.value = '请至少回答一道澄清问题。'; return }
  await sql.submit(answers)
}

/** 许可仅由后端签发，修复调用需用户单独勾选且总调用预算至少两次。 */
async function authorize() {
  localError.value = ''
  if (!consent.transfer || !consent.charges || !consent.provider.trim()
      || consent.repair && consent.calls < 2) { localError.value = '请确认外传和费用，并核对修复预算。'; return }
  await sql.authorize({ accept_data_transfer: true, accept_charges: true, provider_id: consent.provider,
    max_calls: consent.calls, max_total_tokens: consent.tokens, allow_sql_repair: consent.repair })
  consent.transfer = false; consent.charges = false
}

/** 仅显示后端白名单业务错误，不展示上游异常原文。 */
function failure(code: string) { return safeMessage(new ApiError(code)) }

/** 编辑尚未提交的问题后撤销本地旧许可，要求用户按新问题重新明确授权。 */
function editQuestion() { s.cloud = noCloud(); consent.transfer = false; consent.charges = false }
</script>
<template>
  <div class="page stack">
    <header class="page-header"><div><div class="eyebrow">SQL / CLARIFICATION</div><h1 class="page-title">智能问数与多轮澄清</h1><p class="page-subtitle">原问题、范围和确认条件随会话绑定；歧义解决前由后端暂停查询。</p></div></header>
    <section class="card card-pad stack">
      <div class="panel-heading"><h3>真实数据源</h3><button class="btn outline" :disabled="s.busy || !credentials.token" @click="sql.loadCatalog">读取目录</button></div>
      <select class="select" :value="s.selected" :disabled="s.busy" @change="sql.select(Number(($event.target as HTMLSelectElement).value))">
        <option :value="-1">请选择后端授权数据源</option><option v-for="(source, i) in s.sources" :key="source.catalog_fingerprint" :value="i">{{ source.scope.db_id }} / {{ source.scope.sub_db }} · {{ source.schema_name }}</option>
      </select>
      <p v-if="s.catalogLoaded && !s.sources.length" class="notice warn">注册目录为空；请先在后端配置真实授权来源。</p>
      <div class="toolbar"><button class="btn primary" :disabled="s.busy || s.selected < 0" @click="sql.createSession">创建新会话</button><button class="btn outline" :disabled="s.busy || !s.session" @click="sql.reset">修正 / 重置条件</button></div>
      <p v-if="s.session" class="identifier">session_id：{{ s.session.session_id }} · 服务端有效期 {{ s.session.expires_in_seconds }} 秒（不续期）</p>
      <p class="hint">改数据源会清空本地会话；修改原问题或已确认条件须先重置。令牌只驻留内存，刷新后需重新创建。</p>
      <label for="sql-question">原始自然语言问题</label>
      <textarea id="sql-question" v-model="s.draft" class="textarea" rows="4" maxlength="4000" :disabled="s.busy || !!s.originalQuestion" placeholder="描述需要查询的指标、期间和范围" @input="editQuestion"></textarea>
      <button class="btn primary" :disabled="s.busy || !s.session || !s.draft.trim() || !!s.pending || s.uncertain || s.response?.status === 'answered' || s.response?.status === 'empty'" @click="sql.submit()">{{ s.busy ? '请求处理中…' : '提交当前问题（不自动重试）' }}</button>
      <p v-if="s.uncertain" class="notice warn">执行或响应状态未知。请先核对后台及费用；需要再次执行时显式重置并重新授权。</p>
    </section>
    <section class="card card-pad stack">
      <h3>本任务模型外传与费用许可</h3><p class="hint">原问题、已选结构及确认条件将发给后端配置的提供方。默认零调用；登记许可不会自动查询。</p>
      <fieldset :disabled="s.busy || !s.session || s.uncertain">
        <div class="form-grid"><label>提供方标识<input v-model="consent.provider" class="input" maxlength="128"></label><label>总调用上限（1～4）<input v-model.number="consent.calls" type="number" min="1" max="4" class="input"></label><label>总 Token 预算<input v-model.number="consent.tokens" type="number" min="1" max="32768" class="input"></label></div>
        <label class="check"><input v-model="consent.repair" type="checkbox">另外允许一次 SQL 技术修复（总调用上限至少 2 次）</label>
        <label class="check"><input v-model="consent.transfer" type="checkbox">确认本任务数据外传给所配置提供方</label>
        <label class="check"><input v-model="consent.charges" type="checkbox">接受上述预算内可能产生的模型费用</label>
        <button class="btn outline" :disabled="!consent.transfer || !consent.charges || !s.draft.trim()" @click="authorize">向后端申请本任务许可</button>
      </fieldset>
      <p>本地保存的许可：{{ s.cloud.allow_cloud ? `已签发；总调用上限 ${s.cloud.max_calls}，Token上限 ${s.cloud.max_total_tokens}` : '未授权，零调用' }}。剩余额度以服务端为准。</p>
    </section>
    <p v-if="s.error || localError" role="alert" class="notice danger">{{ localError || s.error }}</p>
    <section v-if="s.pending" class="card card-pad stack">
      <div class="panel-heading"><h3>needs_user · 请澄清</h3><span class="pill">版本 {{ s.pending.revision }}</span></div>
      <div v-for="q in s.pending.questions" :key="q.question_id" class="field">
        <label :for="q.question_id">{{ q.prompt }}</label>
        <select :id="q.question_id" v-model="choices[q.question_id]" class="select" :disabled="s.busy">
          <option value="">暂不回答</option><option v-for="option in q.options" :key="option.option_id" :value="option.option_id">{{ option.label }}</option><option v-if="q.allow_text" value="__text__">补充文本</option>
        </select>
        <input v-if="q.allow_text && choices[q.question_id] === '__text__'" v-model="texts[q.question_id]" class="input" :disabled="s.busy" maxlength="1000" placeholder="输入本题条件">
      </div>
      <button class="btn primary" :disabled="s.busy || s.uncertain" @click="reply">确认所答条件并恢复</button>
      <SchemaLinks :links="s.pending.schema_links" :source="s.sources[s.selected]" />
    </section>
    <template v-if="s.response">
      <section class="card card-pad"><h3>{{ responseTitle(s.response) }}</h3><p class="identifier">run_id：{{ s.response.context.run_id }}<br>request_id：{{ s.response.context.request_id }}<br>trace_id：{{ s.response.context.trace_id }}</p>
        <p v-if="s.response.result.kind === 'failure'" class="notice danger">{{ failure(s.response.result.code) }}</p>
        <p v-if="s.response.result.kind === 'unsupported'" class="notice warn">该能力未接入，未运行统一 Agent。</p>
        <h4>后端已确认条件</h4><p v-if="!s.response.context.confirmed_slots.length" class="hint">尚无已确认条件。</p>
        <p v-for="slot in s.response.context.confirmed_slots" :key="slot.slot">{{ slot.slot }} = {{ slot.value }} <small class="identifier">revision {{ slot.revision }} · 来源请求 {{ slot.source_request_id }}</small></p>
      </section>
      <SQLDetails v-if="s.response.result.kind === 'sql'" :result="s.response.result" :source="s.sources[s.selected]" />
      <section class="card card-pad"><TraceTimeline :steps="s.response.trace" :trace-id="s.response.context.trace_id" /></section>
    </template>
    <section v-if="s.history.length" class="card card-pad"><h3>本会话执行记录（仅内存，最多20次）</h3><ol><li v-for="run in s.history" :key="run.context.run_id">{{ responseTitle(run) }} · <span class="identifier">{{ run.context.trace_id }}</span></li></ol><p class="hint">每次澄清具有独立 run/trace；在「执行链路」页可按会话读取记录。</p></section>
  </div>
</template>
