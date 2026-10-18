import { reactive } from 'vue'
import type { KnowledgeApi } from '../api/knowledge.ts'
import { safeMessage } from '../api/http.ts'
import { emptyPage } from './knowledge.ts'
import type { Block, Chunk, Document, DocumentStatus, KnowledgeBase, UploadResult } from '../types/knowledge.ts'

/** 管理页只显示服务端状态；上传、重试与状态刷新均由用户显式发起。 */
export function createLibraryWorkspace(api: KnowledgeApi) {
  const state = reactive({
    bases: emptyPage<KnowledgeBase>(), docs: emptyPage<Document>(), kbId: '', kbName: '',
    description: '', selected: null as Document | null, status: null as DocumentStatus | null,
    blocks: emptyPage<Block>(), chunks: emptyPage<Chunk>(), evidenceMode: '' as '' | 'blocks' | 'chunks',
    busy: false, error: '', info: '', upload: null as UploadResult | null,
  })
  /** 单次管理动作互斥；失败不伪造成功消息。 */
  async function action(work: () => Promise<void>) {
    if (state.busy) return
    state.busy = true; state.error = ''; state.info = ''
    try { await work() }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }
  /** 完整分页浏览库目录，无后台轮询。 */
  async function bases(offset = 0) {
    await action(async () => { state.bases = emptyPage(); state.bases = await api.bases(20, offset) })
  }
  /** 当前文档列表刷新先失效旧状态和证据，避免来源错配。 */
  async function documents(offset = 0) {
    if (!state.kbId) return
    await action(async () => {
      state.docs = emptyPage(); state.selected = null; state.status = null; state.evidenceMode = ''
      state.docs = await api.documents(state.kbId, 20, offset)
    })
  }
  /** 库变化只清空本地视图，不删除真实文档。 */
  async function selectBase(kbId: string) {
    if (state.busy) return
    state.kbId = kbId; state.docs = emptyPage(); state.selected = null; state.status = null; state.upload = null
    state.evidenceMode = ''; state.blocks = emptyPage(); state.chunks = emptyPage()
    if (kbId) await documents()
  }
  /** 创建后刷新目录并选择真实新库；名称冲突由后端裁定。 */
  async function createBase() {
    if (!state.kbName.trim()) return
    await action(async () => {
      const created = await api.createBase(state.kbName, state.description || null)
      state.bases = await api.bases(); state.kbId = created.kb_id; state.docs = await api.documents(created.kb_id)
      state.selected = null; state.status = null; state.upload = null; state.evidenceMode = ''
      state.kbName = ''; state.description = ''; state.info = `已创建知识库：${created.name}`
    })
  }
  /** auto 可能外传/收费；界面取得确认后才传 auto，否则显式 native。 */
  async function upload(files: File[], allowCloud: boolean) {
    if (!state.kbId || !files.length || files.length > 50) return
    await action(async () => {
      state.upload = null
      state.upload = await api.upload(state.kbId, files, allowCloud ? 'auto' : 'native')
      state.info = '服务已返回逐文件接收状态；后台处理进度请查询文档，接收不等于 READY。'
      state.docs = await api.documents(state.kbId)
      state.selected = null; state.status = null; state.evidenceMode = ''
    })
    if (state.error) state.error += ' 上传可能已保存原件，请刷新核对 doc_id，勿直接整批重传。'
  }
  /** 读取当前文档实时统计，不拿列表旧状态充当后台完成。 */
  async function inspect(doc: Document) {
    await action(async () => {
      state.selected = doc; state.status = null; state.evidenceMode = ''; state.blocks = emptyPage(); state.chunks = emptyPage()
      state.status = await api.status(doc.doc_id)
    })
  }
  /** 恢复既有阶段，绝不暗中追加PDF新片或Force请求；是否允许由后端判断。 */
  async function retry(allowCloud: boolean) {
    if (!state.selected) return
    const docId = state.selected.doc_id
    await action(async () => {
      const result = await api.retry(docId, allowCloud ? 'auto' : 'native')
      state.info = `恢复请求已接收：${result.management_task_id}；stage=${result.stage}，state=${result.state}。`
      state.status = null; state.status = await api.status(docId)
    })
  }
  /** 分页显示真实Block或Chunk；一次仅读取指定窗口。 */
  async function evidence(mode: 'blocks' | 'chunks', offset = 0) {
    if (!state.selected) return
    const id = state.selected.doc_id
    await action(async () => {
      state.evidenceMode = mode
      if (mode === 'blocks') { state.blocks = emptyPage(); state.blocks = await api.blocks(id, 20, offset) }
      else { state.chunks = emptyPage(); state.chunks = await api.chunks(id, 20, offset) }
    })
  }
  return { state, bases, documents, selectBase, createBase, upload, inspect, retry, evidence }
}
