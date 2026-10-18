/** 对应 app/schemas 的原始 HTTP 字段；私有元数据不整体渲染或持久化。 */
export interface Page<T> { items: T[]; total: number; limit: number; offset: number }
export interface KnowledgeBase {
  kb_id: string; name: string; description: string | null; status: 'ACTIVE' | 'DISABLED'
  created_at: string; updated_at: string
}
export type DocumentState = 'UPLOADED' | 'PARSING' | 'OCR_PROCESSING' | 'CHUNKING' | 'EMBEDDING'
  | 'INDEXING' | 'READY' | 'PARTIAL_READY' | 'FAILED'
export type FileType = 'pdf' | 'doc' | 'docx' | 'xls' | 'xlsx' | 'ppt' | 'pptx' | 'csv' | 'txt' | 'md' | 'jpg' | 'jpeg' | 'png'
export interface Document {
  doc_id: string; kb_id: string; file_name: string; file_type: FileType; file_hash: string
  source_type: 'preset' | 'uploaded'; status: DocumentState; error_message: string | null
  parse_quality: Record<string, unknown> | null; task_id: string | null; index_task_id: string | null
  created_at: string; updated_at: string
}
export interface Management {
  doc_id: string; kb_id: string; management_task_id: string
  operation: 'ingest' | 'retry' | 'reindex' | 'reparse' | 'delete'
  stage: 'parse' | 'cloud' | 'chunk' | 'embed' | 'index' | 'delete' | 'done'
  state: 'running' | 'failed' | 'done'; status: string; error_code: string | null; cause_code: string | null
}
export interface DocumentStatus {
  doc_id: string; kb_id: string; status: string; error_message: string | null; task_id: string | null
  chunk_task_id: string | null; embedding_task_id: string | null; index_task_id: string | null
  management: Management | null; block_count: number; chunk_count: number; vector_count: number
  invalid_vector_count: number; original_present: boolean; parse_cache_present: boolean
  updated_at: string; parse_quality: Record<string, unknown> | null
}
export interface Block {
  block_id: string; doc_id: string; block_index: number | null; page: number; section: string | null
  block_type: string; text: string; bbox: number[] | null; source: string; confidence: number | null
  provenance: Record<string, unknown> | null
}
export interface Chunk {
  chunk_id: string; doc_id: string; kb_id: string; text: string; page_start: number; page_end: number
  section: string | null; metadata: Record<string, unknown>; created_at: string
}
export interface ParseJob {
  job_id: string; doc_id: string; provider: string; model_version: string; data_id: string
  batch_id: string | null; task_id: string; trace_id: string | null; state: string; retry_count: number
  error_code: string | null; error_message: string | null; is_active: boolean
  submitted_at: string | null; finished_at: string | null; created_at: string; updated_at: string
}
export interface BatchUploadItem {
  index: number; file_name: string | null; status_code: number; doc_id: string | null
  document: Document | null; parse_job: ParseJob | null; detail: string | Record<string, unknown> | null
}
export interface BatchUpload {
  total: number; accepted_count: number; failed_count: number; items: BatchUploadItem[]
}
export type UploadResult = Document | BatchUpload
export interface FusionProvenance {
  strategy: 'rrf-v1'; rrf_k: number; sources: ('dense' | 'bm25')[]
  dense_rank: number | null; dense_score: number | null; bm25_rank: number | null
  bm25_score: number | null; fusion_score: number
}
export interface RerankProvenance {
  model_name: 'BAAI/bge-reranker-v2-m3'; model_fingerprint: string; version: 'bge-reranker-v2-m3-logit-v1'
  max_length: number; precision: 'fp16' | 'fp32'; score_type: 'logit'; input_rank: number
  retrieval_score: number; fusion_score: number | null; rerank_score: number
}
export interface Evidence {
  kb_id: string; doc_id: string; chunk_id: string; file_name: string; file_type: FileType; text: string
  page_start: number; page_end: number; section: string | null; metadata: Record<string, unknown>
  embedding_identity: Record<string, unknown> | null; retrieval_score: number | null
  fusion: FusionProvenance | null; fusion_score: number | null; rerank_score: number | null
  rerank: RerankProvenance | null
}
export interface Citation extends Evidence { citation_id: number }
export interface SearchRequest { kb_id: string; question: string; doc_ids: string[] | null; top_k: number }
export interface QueryRequest extends SearchRequest { allow_cloud: boolean }
export interface SearchResponse { status: 'success' | 'empty'; evidence: Evidence[] }
export interface RAGAnswer {
  status: 'success' | 'insufficient_evidence'; answer: string; citations: Citation[]; insufficient_evidence: boolean
}
export interface KnowledgeHealth { status: 'ok'; service: 'knowledge-service' }
