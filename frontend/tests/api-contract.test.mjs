import test from 'node:test'
import assert from 'node:assert/strict'
import { createContestApi, contestHealth } from '../src/api/contest.ts'
import { createKnowledgeApi } from '../src/api/knowledge.ts'
import { ApiError, safeMessage } from '../src/api/http.ts'
import { sqlResponse, traceRecord } from '../src/api/guards.ts'
import { cleanCell, pageLabel, responseTitle } from '../src/utils/format.ts'
import { createProxy, rewriteContest } from '../proxy.ts'
import { response, session, ids, evidence } from './fixtures.mjs'

test('SQL成功/空结果/500行截断和无损数值严格按响应展示', () => {
  // 不把空表误判失败，不把大整数字符串转换成Number。
  const success = sqlResponse(response())
  assert.equal(cleanCell(success.result.rows[0][0]), '9007199254740993.01')
  assert.equal(cleanCell(null), 'NULL'); assert.equal(cleanCell(''), '（空字符串）')
  const empty = sqlResponse(response('empty')); assert.equal(empty.result.rows.length, 0)
  const truncated = response(); truncated.result.rows = Array.from({ length: 500 }, () => ['1'])
  truncated.result.returned_rows = 500; truncated.result.truncated = true
  assert.equal(sqlResponse(truncated).result.truncated, true)
  const invalid = response(); invalid.result.rows = [[9007199254740992]]
  assert.throws(() => sqlResponse(invalid), ApiError)
  const unexecuted = response(); unexecuted.result.attempts[0].execution = 'not_run'
  assert.throws(() => sqlResponse(unexecuted), ApiError)
})

test('非2xx领域失败保留真实Trace，普通异常不透传敏感detail', async () => {
  // SQL_REJECTED失败响应仍经过冻结DTO校验。
  const rejected = response('failed', { result: { kind: 'failure', code: 'SQL_REJECTED' } })
  const calls = []
  const api = createContestApi(() => 'unit-token', async (path, init) => {
    // 记录安全断言所需的请求字段，仅在测试内存存在。
    calls.push({ path, init }); return { ok: false, status: 403, body: rejected }
  })
  const req = { client_request_id: ids.client }
  const result = await api.query(req, session)
  assert.equal(result.result.code, 'SQL_REJECTED'); assert.match(responseTitle(result), /blocked/)
  assert.equal(new Headers(calls[0].init.headers).get('Authorization'), 'Bearer unit-token')
  assert.equal(new Headers(calls[0].init.headers).get('X-Session-Token'), session.session_token)
  assert.equal(calls[0].path, '/api/contest/query')
  const broken = createKnowledgeApi(async () => ({ ok: false, status: 503,
    body: { detail: { code: 'DSN=secret', message: 'password=secret' } } })) // 上游敏感内容只在断言中构造。
  await assert.rejects(broken.health(), error => !safeMessage(error).includes('secret'))
})

test('两个服务断开、无入口令牌和坏健康DTO均不能显示已连接', async () => {
  // 隔离传输失败，绝不读取真实后端。
  const disconnected = async () => { throw new ApiError('NETWORK') }
  await assert.rejects(contestHealth(disconnected), ApiError)
  await assert.rejects(createKnowledgeApi(disconnected).health(), ApiError)
  let calls = 0
  const api = createContestApi(() => '', async () => { calls++; throw new Error('不应请求') })
  await assert.rejects(api.catalog(), ApiError); assert.equal(calls, 0)
  await assert.rejects(contestHealth(async () => ({ ok: true, status: 200, body: { status: 'ok' } })), ApiError)
})

test('Trace完整关联与空步骤分开，错会话快照拒绝', async () => {
  // 空步骤是没有记录，不转换为成功步骤。
  const snapshot = { trace_id: ids.trace, session_id: ids.session, run_id: ids.run,
    request_id: ids.request, reply_to_run_id: null, trace: [] }
  assert.equal(traceRecord(snapshot).trace.length, 0)
  const complete = { ...snapshot, trace: [{ step_id: ids.doc, tool: 'sql', operation: 'sql_execute',
    status: 'succeeded', sql_attempt: 1, elapsed_ms: null, llm_calls: 0, error_code: null }] }
  assert.equal(traceRecord(complete).trace[0].elapsed_ms, null)
  assert.equal(traceRecord(complete).trace[0].step_id, ids.doc)
  const api = createContestApi(() => 'unit-token', async () => ({ ok: true, status: 200,
    body: { ...snapshot, session_id: ids.doc } })) // 故意提供错会话，用于验证隔离。
  await assert.rejects(api.trace(ids.trace, session), ApiError)
  const missing = createContestApi(() => 'unit-token', async () => ({ ok: false, status: 403,
    body: { detail: { code: 'PERMISSION_DENIED' } } })) // 缺记录后端真实分类为拒绝读取。
  await assert.rejects(missing.trace(ids.trace, session), ApiError)
})

test('Vite代理只匹配真实路由；双健康检查分别重写到/health', () => {
  // 对实际配置工厂断言，而非维护第二套代理规则。
  const proxy = createProxy()
  assert.equal(proxy['^/health/contest/?$'].target, 'http://127.0.0.1:8001')
  assert.equal(proxy['^/health/knowledge/?$'].target, 'http://127.0.0.1:8000')
  assert.equal(proxy['^/health/contest/?$'].rewrite('/health/contest'), '/health')
  assert.equal(proxy['^/health/knowledge/?$'].rewrite('/health/knowledge'), '/health')
  assert.equal(rewriteContest('/api/contest/query'), '/api/query')
  assert.equal(rewriteContest('/api/contest/traces/id?session_id=s'), '/api/traces/id?session_id=s')
  assert.equal(rewriteContest('/api/contestant/query'), '/api/contestant/query')
  assert.equal(new RegExp('^/api/knowledge(?:/|$)').test('/api/knowledge/query'), true)
  assert.throws(() => createProxy({ FRONTEND_CONTEST_TARGET: 'https://untrusted.example' }))
})

test('知识检索、问答与单/多文件使用原服务字段；许可默认false', async () => {
  // mock响应仅用于协议测试，生产环境无mock分支。
  const calls = []
  const api = createKnowledgeApi(async (path, init) => {
    // 仅按本次请求返回隔离DTO。
    calls.push({ path, init })
    if (path.endsWith('/search')) return { ok: true, status: 200, body: { status: 'success', evidence: [evidence()] } }
    if (path.endsWith('/query')) return { ok: true, status: 200, body: { status: 'success', insufficient_evidence: false,
      answer: '隔离答案[1]', citations: [evidence({ citation_id: 1 })] } }
    return { ok: true, status: 200, body: { total: 2, accepted_count: 1, failed_count: 1, items: [] } }
  })
  const scope = { kb_id: ids.kb, question: '文档问题', doc_ids: [ids.doc], top_k: 30 }
  assert.equal((await api.search(scope)).status, 'success')
  assert.equal((await api.query({ ...scope, allow_cloud: false })).citations[0].citation_id, 1)
  assert.equal(JSON.parse(calls[1].init.body).allow_cloud, false)
  const files = [new File(['a'], 'a.txt'), new File(['b'], 'b.txt')]
  await api.upload(ids.kb, files)
  assert.equal(calls[2].path, `/api/admin/knowledge-bases/${ids.kb}/documents`)
  assert.deepEqual(calls[2].init.body.getAll('file').map(f => f.name), ['a.txt', 'b.txt'])
  assert.equal(calls[2].init.body.get('parse_mode'), 'native')
  assert.equal(calls[2].init.headers, undefined)
  await api.upload(ids.kb, files.slice(0, 1), 'auto')
  assert.equal(calls[3].init.body.getAll('file').length, 1)
  assert.equal(calls[3].init.body.get('parse_mode'), 'auto')
})

test('PDF物理页、Office逻辑页、Sheet与缺失页语义不互相冒充', () => {
  // 来源页语义始终使用后端metadata。
  assert.match(pageLabel(evidence()), /PDF物理页 2–3/)
  assert.match(pageLabel(evidence({ file_type: 'xlsx', metadata: { page_semantics: 'logical' }, section: 'Sheet2' })), /逻辑页 2–3 · Sheet：Sheet2/)
  assert.match(pageLabel(evidence({ metadata: {} })), /页语义未返回/)
})

test('坏管理快照、越界知识引用不能冒充成功', async () => {
  // 仅含READY字样的坏状态响应不能进入管理页。
  const incomplete = createKnowledgeApi(async () => ({ ok: true, status: 200,
    body: { doc_id: ids.doc, kb_id: ids.kb, status: 'READY' } }))
  await assert.rejects(incomplete.status(ids.doc), ApiError)
  const wrongScope = createKnowledgeApi(async () => ({ ok: true, status: 200,
    body: { status: 'success', evidence: [evidence({ kb_id: ids.run })] } })) // 故意提供错库引用。
  await assert.rejects(wrongScope.search({ kb_id: ids.kb, question: '问题', doc_ids: null, top_k: 30 }), ApiError)
})
