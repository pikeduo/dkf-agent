import test from 'node:test'
import assert from 'node:assert/strict'
import { createKnowledgeWorkspace } from '../src/state/knowledge.ts'
import { createLibraryWorkspace } from '../src/state/library.ts'
import { ApiError } from '../src/api/http.ts'
import { ragAnswer, searchResponse } from '../src/api/guards.ts'
import { document, evidence, ids, page } from './fixtures.mjs'

/** 提供最小注入服务，所有调用仅记录到隔离内存。 */
function knowledgeApi() {
  const calls = []
  const api = {
    // 真实分页行为由用例提供offset并核对。
    async bases(limit = 20, offset = 0) { calls.push(['bases', offset]); return page([], 40, offset) },
    // 第二页仍属于相同知识库。
    async documents(kbId, limit = 20, offset = 0) { calls.push(['docs', kbId, offset]);
      return page([document({ status: 'READY', doc_id: offset ? ids.run : ids.doc })], 40, offset) },
    // 未调用任何嵌入或检索模型。
    async search(body) { calls.push(['search', body]); return { status: 'success', evidence: [evidence()] } },
    // 无许可的生成请求由服务端拒绝。
    async query(body) { calls.push(['query', body]); if (!body.allow_cloud) throw new ApiError('RAG_CLOUD_PERMISSION', 403)
      return { status: 'success', answer: '引用事实[1]', citations: [evidence({ citation_id: 1 })], insufficient_evidence: false } },
  }
  return { api, calls }
}

test('跨页选文档、原请求范围和每次云许可互不混用', async () => {
  // 首次问答默认false，勾选后仅下一次生成生效。
  const { api, calls } = knowledgeApi()
  const model = createKnowledgeWorkspace(api)
  await model.bases(); await model.selectBase(ids.kb)
  model.state.allDocuments = false
  model.toggleDocument(model.state.docs.items[0], true)
  await model.documents(20); model.toggleDocument(model.state.docs.items[0], true)
  assert.equal(model.state.docIds.length, 2)
  model.state.question = '文档问题'
  await model.run('query')
  assert.equal(model.state.answer, null); assert.match(model.state.error, /RAG_CLOUD_PERMISSION/)
  model.state.allowCloud = true; await model.run('query')
  assert.equal(model.state.answer.status, 'success'); assert.equal(model.state.allowCloud, false)
  assert.deepEqual(calls.filter(c => c[0] === 'query')[1][1].doc_ids, [ids.doc, ids.run])
  await model.selectBase(ids.request)
  assert.equal(model.state.docIds.length, 0); assert.equal(model.state.answer, null)
})

test('证据、空检索、证据不足、服务异常具有不同状态', async () => {
  // 空证据与生成失败不互相替代。
  assert.equal(searchResponse({ status: 'empty', evidence: [] }).status, 'empty')
  assert.throws(() => searchResponse({ status: 'success', evidence: [] }), ApiError)
  assert.equal(ragAnswer({ status: 'insufficient_evidence', answer: '当前检索到的证据不足以回答该问题。',
    insufficient_evidence: true, citations: [] }).status, 'insufficient_evidence')
  assert.throws(() => ragAnswer({ status: 'success', answer: '无引用文本', citations: [], insufficient_evidence: false }), ApiError)
  const { api } = knowledgeApi(); const model = createKnowledgeWorkspace(api)
  await model.selectBase(ids.kb); model.state.question = '问题'
  await model.run('search'); assert.equal(model.state.search.status, 'success'); assert.equal(model.state.answer, null)
  api.search = async () => { throw new ApiError('NETWORK') } // 断网不能伪装证据不足。
  await model.run('search'); assert.equal(model.state.search, null); assert.equal(model.state.answer, null)
  assert.match(model.state.error, /NETWORK/)
})

test('空的显式文档范围不扩大为全库，来源失效不发布旧响应', async () => {
  // 没有明确选文档时不触发查询。
  const { api, calls } = knowledgeApi(); const model = createKnowledgeWorkspace(api)
  await model.selectBase(ids.kb); model.state.question = '问题'; model.state.allDocuments = false
  await model.run('search'); assert.equal(calls.filter(c => c[0] === 'search').length, 0)
  model.state.allDocuments = true
  let finish
  api.search = () => new Promise(resolve => { finish = resolve }) // 只延迟内存响应，不阻塞外部服务。
  const pending = model.run('search')
  model.invalidate(); finish({ status: 'success', evidence: [evidence()] })
  await pending; assert.equal(model.state.search, null)
})

test('上传逐项报告和异步状态不伪造READY，重试不隐式授权新片', async () => {
  // 按时间返回UPLOADED、PARTIAL_READY和FAILED，不从HTTP202推断状态。
  const { api } = knowledgeApi(); const events = []
  let state = 'UPLOADED'
  api.upload = async (kbId, files, mode) => { events.push(['upload', mode, files.length]);
    return { total: 2, accepted_count: 1, failed_count: 1, items: [
      { index: 0, file_name: 'a.txt', status_code: 202, doc_id: ids.doc, document: document(), parse_job: null, detail: null },
      { index: 1, file_name: 'b.txt', status_code: 415, doc_id: null, document: null, parse_job: null, detail: null }] } } // 纯响应，不处理文件。
  api.status = async () => ({ doc_id: ids.doc, kb_id: ids.kb, status: state,
    block_count: 3, chunk_count: 2, vector_count: state === 'PARTIAL_READY' ? 2 : 0, invalid_vector_count: 0,
    parse_quality: { content: 'PARTIAL' }, management: null }) // 状态明确来自注入服务。
  api.retry = async (docId, mode) => { events.push(['retry', mode]);
    return { management_task_id: ids.run, stage: 'chunk', state: 'running' } } // 接收不表示完成。
  const model = createLibraryWorkspace(api); model.state.kbId = ids.kb
  await model.upload([new File(['a'], 'a.txt'), new File(['b'], 'b.txt')], false)
  assert.equal(model.state.upload.failed_count, 1); assert.equal(model.state.upload.items[0].document.status, 'UPLOADED')
  assert.equal(events[0][1], 'native')
  await model.inspect(document()); assert.equal(model.state.status.status, 'UPLOADED')
  state = 'PARTIAL_READY'; await model.inspect(document()); assert.equal(model.state.status.vector_count, 2)
  state = 'FAILED'; await model.inspect(document()); assert.equal(model.state.status.status, 'FAILED')
  await model.retry(false); assert.deepEqual(events.at(-1), ['retry', 'native'])
  assert.equal(model.state.status.status, 'FAILED')
})
