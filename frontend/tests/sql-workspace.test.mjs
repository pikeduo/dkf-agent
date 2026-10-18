import test from 'node:test'
import assert from 'node:assert/strict'
import { createSQLWorkspace } from '../src/state/sql.ts'
import { ApiError } from '../src/api/http.ts'
import { response, clarification, session, source, ids, scope } from './fixtures.mjs'

/** 用依赖注入记录调用，不产生网络、模型、数据库或磁盘操作。 */
function setup(query) {
  const calls = []
  const api = {
    // 目录只有测试显式提供的来源。
    async catalog() { return [source] },
    // 创建响应保留服务端凭据字段。
    async createSession(value) { calls.push(['create', value]); return structuredClone(session) },
    // 许可由测试服务签发，页面不得自行构造consent_id。
    async consent(current, body) { calls.push(['consent', body]); return { allow_cloud: true,
      consent_id: ids.clarification, provider_id: body.provider_id, max_calls: body.max_calls,
      max_total_tokens: body.max_total_tokens, allow_sql_repair: body.allow_sql_repair } },
    // 记录显式重置次数。
    async reset(current) { calls.push(['reset', current.session_id]) },
    // 查询仅消费测试函数返回值。
    async query(body, current) { calls.push(['query', structuredClone(body), current.session_id]);
      return query ? query(body, calls) : response('answered', { context: { ...response().context, client_request_id: body.client_request_id } }) },
    // Trace仅返回纯内存安全快照。
    async trace(id) { return { trace_id: id, session_id: ids.session, run_id: ids.run,
      request_id: ids.request, reply_to_run_id: null, trace: [] } },
  }
  return { model: createSQLWorkspace(api), calls, api }
}

/** 创建真实目录范围的隔离会话，并设置尚未提交的原问题。 */
async function ready(query) {
  const value = setup(query)
  await value.model.loadCatalog(); value.model.select(0); await value.model.createSession()
  value.model.state.draft = '按年度查询销售金额'
  return value
}

test('会话/澄清/部分追问保留原问题、范围、版本和选项身份', async () => {
  // 三次请求分别取得初始问题、部分待答和最终结果。
  let index = 0
  const { model, calls } = await ready(body => {
    // 后端回显当前请求身份；前两次保持待答状态。
    const ctx = { ...response().context, client_request_id: body.client_request_id }
    return ++index < 3 ? response('needs_clarification', { context: ctx, result: clarification(index) })
      : response('answered', { context: { ...ctx, confirmed_slots: [{ slot: 'period', value: '2025',
        source_request_id: body.client_request_id, clarification_id: ids.clarification, revision: 2 }] } })
  })
  await model.submit()
  assert.equal(model.state.pending.revision, 1)
  const answer = [{ question_id: 'period', option_id: 'year2025' }]
  await model.submit(answer); assert.equal(model.state.pending.revision, 2)
  await model.submit(answer); assert.equal(model.state.pending, null)
  const requests = calls.filter(c => c[0] === 'query').map(c => c[1])
  assert.equal(requests.length, 3)
  assert.equal(new Set(requests.map(r => r.client_request_id)).size, 3)
  requests.forEach(r => { assert.equal(r.question, '按年度查询销售金额'); assert.deepEqual(r.sql_scope, scope); assert.equal('confirmed_slots' in r, false) })
  assert.equal(requests[1].clarification.revision, 1)
  assert.equal(requests[2].clarification.revision, 2)
  assert.deepEqual(requests[2].clarification.answers, answer)
  assert.equal(model.state.response.context.confirmed_slots[0].value, '2025')
})

test('未回答澄清不执行，重置后旧槽位/许可/版本全部清空', async () => {
  // 待答状态不发送没有answers的恢复请求。
  const { model, calls } = await ready(() => response('needs_clarification', { result: clarification() }))
  await model.submit(); await model.submit()
  assert.equal(calls.filter(c => c[0] === 'query').length, 1)
  model.state.cloud = { allow_cloud: true, max_calls: 2, max_total_tokens: 4000, allow_sql_repair: true }
  await model.reset()
  assert.equal(model.state.pending, null); assert.equal(model.state.response, null)
  assert.equal(model.state.originalQuestion, ''); assert.equal(model.state.cloud.allow_cloud, false)
  assert.equal(model.state.session.session_id, ids.session)
  assert.equal(calls.filter(c => c[0] === 'reset').length, 1)
  model.select(-1); assert.equal(model.state.session, null)
})

test('默认云许可零调用；新许可只能由显式授权动作取得', async () => {
  // 提交时没有隐式consent调用。
  const { model, calls } = await ready()
  await model.submit()
  assert.equal(calls.filter(c => c[0] === 'consent').length, 0)
  assert.equal(calls.find(c => c[0] === 'query')[1].cloud.allow_cloud, false)
  await model.reset()
  await model.authorize({ accept_data_transfer: true, accept_charges: true, provider_id: 'contest', max_calls: 1,
    max_total_tokens: 4096, allow_sql_repair: false })
  assert.equal(model.state.cloud.consent_id, ids.clarification)
})

test('断网状态未知不自动重放；目录不可用不保留演示源', async () => {
  // 模拟传输故障，不触发任何真实请求。
  const { model, calls, api } = await ready(() => { throw new ApiError('NETWORK') })
  await model.submit(); await model.submit()
  assert.equal(calls.filter(c => c[0] === 'query').length, 1)
  assert.equal(model.state.uncertain, true); assert.equal(model.state.response, null)
  api.catalog = async () => { throw new ApiError('DEPENDENCY_UNAVAILABLE') } // 目录失败安全关闭。
  await model.loadCatalog()
  assert.deepEqual([...model.state.sources], []); assert.equal(model.state.selected, -1)
  assert.equal(model.state.catalogLoaded, false)
})
