/** 对应 contracts/contest_v1.py 及 contest_backend/main.py；UUID/指纹在传输中为字符串。 */
export interface SQLScope { db_id: string; sub_db: string }
export type SQLCell = string | boolean | null
export type ErrorCode = 'PERMISSION_DENIED' | 'SCOPE_INVALID' | 'AUTHORIZATION_REQUIRED'
  | 'BUDGET_EXHAUSTED' | 'MODEL_FAILURE' | 'SQL_REJECTED' | 'SQL_EXECUTION_FAILED'
  | 'EXECUTION_UNKNOWN' | 'DEPENDENCY_UNAVAILABLE' | 'RESULT_INVALID' | 'UNSUPPORTED' | 'INTERNAL_ERROR'
export interface CloudAuthorization {
  allow_cloud: boolean; consent_id?: string | null; provider_id?: string | null
  max_calls: number; max_total_tokens: number; allow_sql_repair: boolean
}
export interface ConsentRequest {
  accept_data_transfer: true; accept_charges: true; provider_id: string
  max_calls: number; max_total_tokens: number; allow_sql_repair: boolean
}
export interface Session {
  session_id: string; session_token: string; scope: SQLScope
  scope_fingerprint: string; expires_in_seconds: number
}
export interface ConfirmedSlot {
  slot: string; value: string; source_request_id: string; clarification_id: string | null; revision: number
}
export interface RunContext {
  request_id: string; client_request_id: string; session_id: string; run_id: string
  trace_id: string; scope_fingerprint: string; confirmed_slots: ConfirmedSlot[]
}
export interface LinkEvidence {
  kind: 'schema' | 'alias' | 'foreign_key' | 'confirmed_slot' | 'value_match'
  reference_id: string; catalog_fingerprint: string
}
export interface SchemaCandidate {
  candidate_id: string; schema_name: string; table: string; column: string | null
  evidence: LinkEvidence[]; score: number | null
}
export interface SchemaLink {
  mention: string; state: 'selected' | 'ambiguous' | 'unmatched'
  candidates: SchemaCandidate[]; selected_id: string | null
}
export interface ClarificationQuestion {
  question_id: string; slot: string; prompt: string
  options: { option_id: string; label: string }[]; allow_text: boolean
}
export interface ClarificationPrompt {
  kind: 'clarification'; clarification_id: string; reply_to_run_id: string; revision: number
  questions: ClarificationQuestion[]; schema_links: SchemaLink[]
}
export interface ClarificationAnswer { question_id: string; option_id?: string; text?: string }
export interface ClarificationReply {
  clarification_id: string; reply_to_run_id: string; revision: number; answers: ClarificationAnswer[]
}
/** AgentRequest 的 SQL 领域投影：不把尚未接入的 rag/formula/auto 发送至领域 API。 */
export interface SQLRequest {
  version: 'contest-agent-v1'; client_request_id: string; session_id: string; question: string
  intent: 'sql'; sql_scope: SQLScope; cloud: CloudAuthorization; clarification?: ClarificationReply
}
export interface SQLAttempt {
  attempt: number; generated_sql: string; validation: 'passed' | 'rejected'
  execution: 'not_run' | 'succeeded' | 'failed' | 'unknown'; executed_sql: string | null
  parameters: Record<string, { data_type: string; value: SQLCell }>; error_code: ErrorCode | null
}
export interface SQLResult {
  kind: 'sql'; status: 'success' | 'empty' | 'failed'; scope: SQLScope; schema_links: SchemaLink[]
  attempts: SQLAttempt[]; columns: { name: string; data_type: string }[]; rows: SQLCell[][]
  returned_rows: number; truncated: boolean; error_code: ErrorCode | null
}
export interface TraceStep {
  step_id: string; tool: 'sql' | 'rag' | 'formula' | 'clarification'
  operation: 'intent' | 'schema_linking' | 'clarification' | 'sql_generate' | 'sql_validate' | 'sql_execute'
    | 'rag_search' | 'rag_synthesize' | 'formula_extract' | 'formula_bind' | 'formula_calculate'
  status: 'succeeded' | 'failed' | 'skipped' | 'unknown'; sql_attempt: number | null
  elapsed_ms: number | null; llm_calls: number | null; error_code: ErrorCode | null
}
/** AgentResponse 的当前 SQL 领域联合，不伪造尚无 HTTP 的 RAG/Formula 适配结果。 */
export interface SQLResponse {
  version: 'contest-agent-v1'; context: RunContext
  status: 'answered' | 'empty' | 'needs_clarification' | 'insufficient_evidence' | 'need_variables' | 'unsupported' | 'failed'
  result: SQLResult | ClarificationPrompt | { kind: 'failure'; code: ErrorCode }
    | { kind: 'unsupported'; capability: 'automatic_cross_source_planning' | 'unified_agent_http' | 'source_resolution_http' }
  trace: TraceStep[]
}
export interface TraceRecord {
  request_id: string; run_id: string; trace_id: string; session_id: string
  reply_to_run_id: string | null; trace: TraceStep[]
}
export interface ForeignKey {
  id: string; from_table: string; to_table: string; pairs: [string, string][]; reference_id: string
}
export interface CatalogSource {
  scope: SQLScope; schema_name: string; tables: { name: string; columns: Record<string, string> }[]
  foreign_keys: ForeignKey[]; aliases: { alias: string; targets: string[]; reference_id: string }[]
  conditions: { slot: string; table: string; column: string; label: string; operator: 'eq' | 'gte' | 'lte'
    value_type: 'text' | 'integer' | 'decimal' | 'date'; triggers: string[]; required: boolean; reference_id: string }[]
  catalog_fingerprint: string
}
export interface ContestHealth {
  status: 'alive'; service: 'contest-sql'; database_verified: false; model_verified: false
}
