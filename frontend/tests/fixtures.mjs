// 仅用于用户手动运行的隔离测试，生产页面不导入这些响应，不连接任何服务。
export const ids = {
  session: '11111111-1111-4111-8111-111111111111',
  run: '22222222-2222-4222-8222-222222222222',
  trace: '33333333-3333-4333-8333-333333333333',
  client: '44444444-4444-4444-8444-444444444444',
  request: '55555555-5555-4555-8555-555555555555',
  clarification: '66666666-6666-4666-8666-666666666666',
  kb: '77777777-7777-4777-8777-777777777777',
  doc: '88888888-8888-4888-8888-888888888888',
}
export const scope = { db_id: 'unit', sub_db: 'sales' }
export const fingerprint = 'a'.repeat(64)
export const session = { session_id: ids.session, session_token: 'unit-session-token', scope,
  scope_fingerprint: fingerprint, expires_in_seconds: 60 }
export const source = { scope, schema_name: 'unit', tables: [{ name: 'sales', columns: { amount: 'numeric' } }],
  foreign_keys: [], aliases: [], conditions: [], catalog_fingerprint: fingerprint }

/** 生成纯内存协议响应，client_request_id由用例显式绑定。 */
export function response(status = 'answered', overrides = {}) {
  return {
    version: 'contest-agent-v1', status,
    context: { request_id: ids.request, client_request_id: ids.client, session_id: ids.session,
      run_id: ids.run, trace_id: ids.trace, scope_fingerprint: fingerprint, confirmed_slots: [] },
    result: { kind: 'sql', status: status === 'empty' ? 'empty' : 'success', scope, schema_links: [],
      attempts: [{ attempt: 1, generated_sql: 'SELECT amount FROM sales', validation: 'passed', execution: 'succeeded',
        executed_sql: 'SELECT amount FROM unit.sales LIMIT 501', parameters: {}, error_code: null }],
      columns: [{ name: 'amount', data_type: 'numeric' }], rows: status === 'empty' ? [] : [['9007199254740993.01']],
      returned_rows: status === 'empty' ? 0 : 1, truncated: false, error_code: null }, trace: [], ...overrides,
  }
}

/** 返回服务端问题身份，测试不以 label 伪造槽位值。 */
export function clarification(revision = 1) {
  return { kind: 'clarification', clarification_id: ids.clarification, reply_to_run_id: ids.run, revision,
    questions: [{ question_id: 'period', slot: 'period', prompt: '请选择期间',
      options: [{ option_id: 'year2025', label: '2025年' }], allow_text: false }], schema_links: [] }
}

/** 模拟已入库文档的元数据，不创建或解析任何文档原件。 */
export function document(overrides = {}) {
  return { doc_id: ids.doc, kb_id: ids.kb, file_name: 'unit.txt', file_type: 'txt', file_hash: 'b'.repeat(64),
    source_type: 'uploaded', status: 'UPLOADED', error_message: null, parse_quality: null, task_id: null,
    index_task_id: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', ...overrides }
}

/** 引用字段与原知识服务结构一致，绝不作为生产回退数据。 */
export function evidence(overrides = {}) {
  return { kb_id: ids.kb, doc_id: ids.doc, chunk_id: ids.run, file_name: 'unit.pdf', file_type: 'pdf',
    text: '隔离用例证据', page_start: 2, page_end: 3, section: '章节', metadata: { page_semantics: 'physical_pdf' },
    embedding_identity: null, retrieval_score: null, fusion: null, fusion_score: null,
    rerank_score: null, rerank: null, ...overrides }
}

/** 生成与请求窗口一致的分页响应。 */
export function page(items = [], total = items.length, offset = 0) { return { items, total, limit: 20, offset } }
