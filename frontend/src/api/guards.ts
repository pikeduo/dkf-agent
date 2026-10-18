import { ApiError, record } from './http.ts'
import type { SQLResponse, TraceStep, TraceRecord, SQLCell, SchemaLink } from '../types/contest.ts'
import type { Evidence, RAGAnswer, SearchResponse } from '../types/knowledge.ts'

/** 将当前服务安全步骤按真实字段校验，未知耗时保留 null。 */
function traceSteps(value: unknown): value is TraceStep[] {
  return Array.isArray(value) && value.every(step => record(step) && typeof step.step_id === 'string'
    && ['sql', 'rag', 'formula', 'clarification'].includes(String(step.tool))
    && ['intent', 'schema_linking', 'clarification', 'sql_generate', 'sql_validate', 'sql_execute',
      'rag_search', 'rag_synthesize', 'formula_extract', 'formula_bind', 'formula_calculate'].includes(String(step.operation))
    && ['succeeded', 'failed', 'skipped', 'unknown'].includes(String(step.status))
    && (step.elapsed_ms === null || typeof step.elapsed_ms === 'number' && Number.isFinite(step.elapsed_ms) && step.elapsed_ms >= 0)
    && (step.llm_calls === null || typeof step.llm_calls === 'number' && Number.isInteger(step.llm_calls) && step.llm_calls >= 0 && step.llm_calls <= 4)
    && (step.sql_attempt === null || step.sql_attempt === 1 || step.sql_attempt === 2)
    && (step.error_code === null || typeof step.error_code === 'string'))
}

/** SQLCell 不接受 JS 数字，防止把已损失精度的数值冒充无损结果。 */
function cell(value: unknown): value is SQLCell {
  return value === null || typeof value === 'string' || typeof value === 'boolean'
}

/** Linking界面只消费真实目录候选和结构化依据，坏结构不进入展示层。 */
function schemaLinks(value: unknown): value is SchemaLink[] {
  return Array.isArray(value) && value.every(link => record(link) && typeof link.mention === 'string'
    && ['selected', 'ambiguous', 'unmatched'].includes(String(link.state)) && Array.isArray(link.candidates)
    && link.candidates.every(c => record(c) && ['candidate_id', 'schema_name', 'table'].every(key => typeof c[key] === 'string')
      && (c.column === null || typeof c.column === 'string') && (c.score === null || typeof c.score === 'number' && Number.isFinite(c.score))
      && Array.isArray(c.evidence) && c.evidence.length > 0 && c.evidence.every(e => record(e)
        && ['schema', 'alias', 'foreign_key', 'confirmed_slot', 'value_match'].includes(String(e.kind))
        && typeof e.reference_id === 'string' && typeof e.catalog_fingerprint === 'string'))
    && (link.state === 'selected' ? link.candidates.some(c => c.candidate_id === link.selected_id) : link.selected_id === null))
}

/** 尝试必须含SQL文本、校验/执行状态和真实参数对象，禁止未执行却带凭证。 */
function attempts(value: unknown): boolean {
  return Array.isArray(value) && value.length <= 2 && value.every((a, i) => record(a) && a.attempt === i + 1
    && typeof a.generated_sql === 'string' && ['passed', 'rejected'].includes(String(a.validation))
    && ['not_run', 'succeeded', 'failed', 'unknown'].includes(String(a.execution)) && record(a.parameters)
    && Object.values(a.parameters).every(p => record(p) && typeof p.data_type === 'string' && cell(p.value))
    && (a.execution === 'not_run' ? a.executed_sql === null && !Object.keys(a.parameters).length
      : a.validation === 'passed' && typeof a.executed_sql === 'string')
    && (a.validation !== 'rejected' || a.execution === 'not_run'))
}

/** 只接收阶段2可产生的响应联合，并验证成功表格与执行证据一致。 */
export function sqlResponse(value: unknown): SQLResponse {
  if (!record(value) || value.version !== 'contest-agent-v1' || !record(value.context)
      || !record(value.result) || !traceSteps(value.trace)
      || !['answered', 'empty', 'needs_clarification', 'insufficient_evidence', 'need_variables', 'unsupported', 'failed'].includes(String(value.status))) {
    throw new ApiError('RESULT_INVALID')
  }
  const ctx = value.context
  if (!['request_id', 'client_request_id', 'session_id', 'run_id', 'trace_id', 'scope_fingerprint']
    .every(key => typeof ctx[key] === 'string') || !Array.isArray(ctx.confirmed_slots)
    || !ctx.confirmed_slots.every(s => record(s) && typeof s.slot === 'string' && typeof s.value === 'string'
      && typeof s.source_request_id === 'string' && Number.isInteger(s.revision))) throw new ApiError('RESULT_INVALID')
  const result = value.result
  if (result.kind === 'sql') {
    if (!record(result.scope) || !schemaLinks(result.schema_links) || !attempts(result.attempts)
        || !Array.isArray(result.attempts)
        || !Array.isArray(result.columns) || !result.columns.every(c => record(c) && typeof c.name === 'string' && typeof c.data_type === 'string')
        || !Array.isArray(result.rows) || result.rows.length !== result.returned_rows || result.rows.length > 500
        || !result.rows.every(row => Array.isArray(row) && row.length === (result.columns as unknown[]).length && row.every(cell))
        || typeof result.truncated !== 'boolean' || result.truncated && result.returned_rows !== 500) throw new ApiError('RESULT_INVALID')
    if (!['success', 'empty', 'failed'].includes(String(result.status))) throw new ApiError('RESULT_INVALID')
    const last = result.attempts.at(-1)
    if (result.status !== 'failed' && (!record(last) || last.execution !== 'succeeded'
        || last.validation !== 'passed' || typeof last.executed_sql !== 'string' || !result.columns.length
        || (result.status === 'success') !== !!result.rows.length
        || value.status !== (result.status === 'success' ? 'answered' : 'empty'))) throw new ApiError('RESULT_INVALID')
    if (result.status === 'failed' && (value.status !== 'failed' || result.rows.length || result.columns.length)) throw new ApiError('RESULT_INVALID')
  } else if (result.kind === 'clarification') {
    if (value.status !== 'needs_clarification' || result.reply_to_run_id !== ctx.run_id
        || !Number.isInteger(result.revision) || !Array.isArray(result.questions) || !result.questions.length
        || !schemaLinks(result.schema_links) || typeof result.clarification_id !== 'string'
        || !result.questions.every(q => record(q) && typeof q.question_id === 'string'
          && typeof q.prompt === 'string' && Array.isArray(q.options) && typeof q.allow_text === 'boolean'
          && q.options.every(o => record(o) && typeof o.option_id === 'string' && typeof o.label === 'string'))) throw new ApiError('RESULT_INVALID')
  } else if (result.kind === 'failure') {
    if (value.status !== 'failed' || typeof result.code !== 'string') throw new ApiError('RESULT_INVALID')
  } else if (result.kind !== 'unsupported' || value.status !== 'unsupported') throw new ApiError('RESULT_INVALID')
  return value as unknown as SQLResponse
}

/** Trace 快照必须具有服务端关联身份，空数组明确表示没有步骤。 */
export function traceRecord(value: unknown): TraceRecord {
  if (!record(value) || !traceSteps(value.trace)
      || !['request_id', 'run_id', 'trace_id', 'session_id'].every(key => typeof value[key] === 'string')) throw new ApiError('RESULT_INVALID')
  return value as unknown as TraceRecord
}

/** 引用必须有真实文档、正文及页范围，不接受旧前端 quote 或假页码回退。 */
function evidence(value: unknown): value is Evidence {
  return record(value) && ['kb_id', 'doc_id', 'chunk_id', 'file_name', 'file_type', 'text'].every(key => typeof value[key] === 'string')
    && typeof value.text === 'string' && !!value.text.trim() && record(value.metadata)
    && typeof value.page_start === 'number' && Number.isInteger(value.page_start) && value.page_start >= 1
    && typeof value.page_end === 'number' && value.page_end >= value.page_start
}

/** 证据获取成功不等于答案生成成功；保持原 search 状态。 */
export function searchResponse(value: unknown): SearchResponse {
  if (!record(value) || !Array.isArray(value.evidence) || !value.evidence.every(evidence)
      || !['success', 'empty'].includes(String(value.status))
      || value.evidence.length > 5 || new Set(value.evidence.map(item => item.chunk_id)).size !== value.evidence.length
      || (value.status === 'success') !== !!value.evidence.length) throw new ApiError('KNOWLEDGE_RESULT')
  return value as unknown as SearchResponse
}

/** 只发布引用完整的 RAG 回答，异常不能降级成证据不足。 */
export function ragAnswer(value: unknown): RAGAnswer {
  if (!record(value) || typeof value.answer !== 'string' || !Array.isArray(value.citations)
      || typeof value.insufficient_evidence !== 'boolean') throw new ApiError('KNOWLEDGE_RESULT')
  if (value.status === 'success' && value.insufficient_evidence === false && value.citations.length
      && value.citations.length <= 5 && value.citations.every((c, i) => evidence(c) && record(c) && c.citation_id === i + 1)
      && new Set(value.citations.map(c => c.chunk_id)).size === value.citations.length
      && value.citations.every(c => (value.answer as string).includes(`[${c.citation_id}]`))) return value as unknown as RAGAnswer
  if (value.status === 'insufficient_evidence' && value.insufficient_evidence === true && !value.citations.length
      && value.answer === '当前检索到的证据不足以回答该问题。') return value as unknown as RAGAnswer
  throw new ApiError('KNOWLEDGE_RESULT')
}
