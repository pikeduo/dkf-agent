import { ApiError, jsonBody, record, responseError, transport, uuid } from './http.ts'
import type { Transport } from './http.ts'
import { ragAnswer, searchResponse } from './guards.ts'
import type { Block, Chunk, Document, DocumentStatus, KnowledgeBase, Management, Page, QueryRequest, SearchRequest, UploadResult } from '../types/knowledge.ts'

/** 原知识服务适配，不附带竞赛令牌，不复制 RAG/解析/索引能力。 */
export function createKnowledgeApi(send: Transport = transport) {
  /** 原管理异常也只显示固定说明，丢弃自由文本 detail 和异常原文。 */
  async function request(path: string, init?: RequestInit) {
    const result = await send(path, init)
    if (!result.ok) throw responseError(result.body, result.status)
    return result.body
  }
  /** 对分页窗口作结构校验，不把缺失 items 当成真实空集。 */
  async function page<T>(path: string, limit: number, offset: number): Promise<Page<T>> {
    const result = await request(`${path}?limit=${limit}&offset=${offset}`)
    if (!record(result) || !Array.isArray(result.items) || !Number.isInteger(result.total)
        || result.limit !== limit || result.offset !== offset) throw new ApiError('RESULT_INVALID')
    return result as unknown as Page<T>
  }
  /** 文档状态必须由当前文档完整快照提供；坏响应不能仅凭READY字样冒充入库。 */
  function documentStatus(body: unknown, docId: string): DocumentStatus {
    if (!record(body) || body.doc_id !== docId || typeof body.kb_id !== 'string'
        || !['UPLOADED', 'PARSING', 'OCR_PROCESSING', 'CHUNKING', 'EMBEDDING', 'INDEXING', 'READY', 'PARTIAL_READY', 'FAILED'].includes(String(body.status))
        || !['block_count', 'chunk_count', 'vector_count', 'invalid_vector_count'].every(key => typeof body[key] === 'number'
          && Number.isInteger(body[key]) && Number(body[key]) >= 0)
        || typeof body.updated_at !== 'string' || body.parse_quality !== null && !record(body.parse_quality)
        || body.management !== null && !record(body.management)) throw new ApiError('RESULT_INVALID')
    return body as unknown as DocumentStatus
  }
  return {
    /** 分页查询知识库，不只取第一屏。 */
    bases(limit = 20, offset = 0) { return page<KnowledgeBase>('/api/admin/knowledge-bases', limit, offset) },
    /** 原创建接口只接收名称及可选说明。 */
    async createBase(name: string, description: string | null): Promise<KnowledgeBase> {
      return await request('/api/admin/knowledge-bases', jsonBody({ name, description })) as KnowledgeBase
    },
    /** 文档范围限定当前知识库，页大小与偏移显式传递。 */
    documents(kbId: string, limit = 20, offset = 0) {
      return page<Document>(`/api/admin/knowledge-bases/${uuid(kbId)}/documents`, limit, offset)
    },
    /** 单/多文件使用同名 file；不发送假完成状态或手工拼接 Content-Type。 */
    async upload(kbId: string, files: File[], mode: 'auto' | 'native' = 'native'): Promise<UploadResult> {
      if (!files.length || files.length > 50) throw new ApiError('RESULT_INVALID')
      const data = new FormData()
      files.forEach(file => data.append('file', file))
      data.append('parse_mode', mode)
      const body = await request(`/api/admin/knowledge-bases/${uuid(kbId)}/documents`, { method: 'POST', body: data })
      if (!record(body) || (!Array.isArray(body.items) && typeof body.doc_id !== 'string')) throw new ApiError('RESULT_INVALID')
      return body as unknown as UploadResult
    },
    /** 真实数据库快照给出处理状态及数量，202接收不等于 READY。 */
    async status(docId: string): Promise<DocumentStatus> {
      return documentStatus(await request(`/api/admin/documents/${uuid(docId)}/status`), docId)
    },
    /** 原证据按阅读序号分页，历史空序号不补造。 */
    blocks(docId: string, limit = 20, offset = 0) { return page<Block>(`/api/admin/documents/${uuid(docId)}/blocks`, limit, offset) },
    /** 原切片含页范围/来源，不获取或显示向量正文。 */
    chunks(docId: string, limit = 20, offset = 0) { return page<Chunk>(`/api/admin/documents/${uuid(docId)}/chunks`, limit, offset) },
    /** 仅用户显式恢复；不授权新增PDF分片、不触发Force Reparse。 */
    async retry(docId: string, mode: 'auto' | 'native' = 'native'): Promise<Management> {
      return await request(`/api/admin/documents/${uuid(docId)}/retry?parse_mode=${mode}&allow_new_split_parts=false`, { method: 'POST' }) as Management
    },
    /** 本地检索只返回证据，不冒充已生成回答。 */
    async search(body: SearchRequest) {
      const result = searchResponse(await request('/api/knowledge/search', jsonBody(body)))
      if (result.evidence.some(item => item.kb_id !== body.kb_id || body.doc_ids && !body.doc_ids.includes(item.doc_id))) throw new ApiError('KNOWLEDGE_RESULT')
      return result
    },
    /** 每次使用明确 allow_cloud，由后端继续核对许可与模型配置。 */
    async query(body: QueryRequest) {
      const result = ragAnswer(await request('/api/knowledge/query', jsonBody(body)))
      if (result.citations.some(item => item.kb_id !== body.kb_id || body.doc_ids && !body.doc_ids.includes(item.doc_id))) throw new ApiError('KNOWLEDGE_RESULT')
      return result
    },
    /** 原 /health 仅检查知识进程存活。 */
    async health() {
      const body = await request('/health/knowledge')
      if (!record(body) || body.status !== 'ok' || body.service !== 'knowledge-service') throw new ApiError('RESULT_INVALID')
      return body
    },
  }
}
export type KnowledgeApi = ReturnType<typeof createKnowledgeApi>
