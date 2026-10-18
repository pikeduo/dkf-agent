import { reactive } from 'vue'
import type { KnowledgeApi } from '../api/knowledge.ts'
import { ApiError, safeMessage } from '../api/http.ts'
import type { Document, KnowledgeBase, Page, RAGAnswer, SearchRequest, SearchResponse } from '../types/knowledge.ts'

/** 初始分页不声称已读取目录；loaded 由页面状态单独记录。 */
export function emptyPage<T>(): Page<T> { return { items: [], total: 0, limit: 20, offset: 0 } }

/** 文档问答保持独立范围，证据与生成回答分别保存且不串用。 */
export function createKnowledgeWorkspace(api: KnowledgeApi) {
  let revision = 0
  const state = reactive({
    bases: emptyPage<KnowledgeBase>(), docs: emptyPage<Document>(), basesLoaded: false, docsLoaded: false,
    kbId: '', allDocuments: true, docIds: [] as string[], question: '', topK: 30, allowCloud: false,
    search: null as SearchResponse | null, answer: null as RAGAnswer | null,
    submittedQuestion: '', submittedScope: null as SearchRequest | null,
    busy: false, error: '',
  })

  /** 输入范围或问题变化即撤销本次许可及旧结果，避免证据误配。 */
  function invalidate() {
    revision += 1
    state.search = null; state.answer = null; state.submittedQuestion = ''; state.submittedScope = null
    state.allowCloud = false; state.error = ''
  }

  /** 每页显式请求知识库目录，失败不保留伪造来源。 */
  async function bases(offset = 0) {
    if (state.busy) return
    state.busy = true; state.error = ''; state.basesLoaded = false; state.bases = emptyPage()
    try { state.bases = await api.bases(20, offset); state.basesLoaded = true }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 读取当前库文档，支持跨页勾选，失败关闭本次查询入口。 */
  async function documents(offset = 0) {
    if (state.busy || !state.kbId) return
    state.busy = true; state.error = ''; state.docsLoaded = false; state.docs = emptyPage()
    try { state.docs = await api.documents(state.kbId, 20, offset); state.docsLoaded = true }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 选择库后清除所有旧文档范围，不能将空数组解释成全库。 */
  async function selectBase(kbId: string) {
    if (state.busy) return
    state.kbId = kbId; state.docIds = []; state.allDocuments = true; state.docs = emptyPage(); state.docsLoaded = false
    invalidate()
    if (kbId) await documents()
  }

  /** 在本次真实列表中核对文档身份及 READY/PARTIAL_READY，再接受勾选。 */
  function toggleDocument(doc: Document, checked: boolean) {
    if (state.busy || doc.kb_id !== state.kbId || !['READY', 'PARTIAL_READY'].includes(doc.status)) return
    invalidate()
    state.docIds = state.docIds.filter(id => id !== doc.doc_id)
    if (checked) state.docIds.push(doc.doc_id)
  }

  /** 调用原知识服务；默认 allow_cloud=false 仍交由后端验证，不在客户端假成功。 */
  async function run(mode: 'search' | 'query') {
    if (state.busy || !state.kbId || !state.docsLoaded || !state.question.trim()) return
    if (!state.allDocuments && (!state.docIds.length || state.docIds.length > 100)) {
      state.error = safeMessage(new ApiError('KNOWLEDGE_INPUT')); return
    }
    const body: SearchRequest = { kb_id: state.kbId, question: state.question,
      doc_ids: state.allDocuments ? null : [...state.docIds], top_k: state.topK }
    const allowed = state.allowCloud
    const startedRevision = revision
    state.busy = true; state.error = ''; state.search = null; state.answer = null
    state.submittedQuestion = body.question; state.submittedScope = body
    state.allowCloud = false // 每次生成后需重新明确许可，不继承至后续问题。
    try {
      if (mode === 'search') {
        const result = await api.search(body)
        if (startedRevision === revision) state.search = result
      } else {
        const result = await api.query({ ...body, allow_cloud: allowed })
        if (startedRevision === revision) state.answer = result
      }
    } catch (error) { if (startedRevision === revision) state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  return { state, invalidate, bases, documents, selectBase, toggleDocument, run }
}
