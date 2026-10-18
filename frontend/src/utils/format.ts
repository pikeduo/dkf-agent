import type { Evidence, Document } from '../types/knowledge.ts'
import type { SQLCell, SQLResponse } from '../types/contest.ts'

/** 原样呈现无损SQL值，NULL和空字符串明确区分。 */
export function cleanCell(value: SQLCell): string {
  return value === null ? 'NULL' : value === '' ? '（空字符串）' : String(value)
}

/** 只使用后端页语义；Sheet取真实section，不把逻辑页冒充PDF物理页。 */
export function pageLabel(source: Pick<Evidence, 'metadata' | 'file_type' | 'page_start' | 'page_end' | 'section'>): string {
  const semantics = source.metadata.page_semantics
  const pages = source.page_start === source.page_end ? String(source.page_start) : `${source.page_start}–${source.page_end}`
  const title = semantics === 'physical_pdf' ? 'PDF物理页' : semantics === 'logical' ? '逻辑页'
    : semantics === 'image_page' ? '图片页' : '页语义未返回，原页码'
  return `${title} ${pages}${['xls', 'xlsx'].includes(source.file_type) && source.section ? ` · Sheet：${source.section}` : ''}`
}

/** 原始状态保留，同时解释 PARTIAL_READY/FAILED；READY仅表示已入库。 */
export function statusTitle(value: Document['status'] | string): string {
  const labels: Record<string, string> = { UPLOADED: '已接收，待处理', PARSING: '解析中', OCR_PROCESSING: '云解析处理中',
    CHUNKING: '切片阶段', EMBEDDING: '向量化阶段', INDEXING: '索引阶段', READY: '已入库',
    PARTIAL_READY: '部分内容已入库（存在质量缺口）', FAILED: '处理失败' }
  return labels[value] || '未知状态'
}

/** 页面业务状态来自 DTO；安全拒绝与普通服务失败分别展示。 */
export function responseTitle(response: SQLResponse): string {
  const code = response.result.kind === 'failure' ? response.result.code
    : response.result.kind === 'sql' ? response.result.error_code : null
  if (code && ['PERMISSION_DENIED', 'SCOPE_INVALID', 'AUTHORIZATION_REQUIRED', 'BUDGET_EXHAUSTED', 'SQL_REJECTED'].includes(code)) return 'blocked · 后端拒绝执行'
  return ({ answered: 'success · 已返回真实结果', empty: 'empty · 查询为空', needs_clarification: 'needs_user · 等待澄清',
    insufficient_evidence: 'insufficient_evidence · 证据不足', need_variables: 'need_variables · 缺少参数',
    unsupported: 'unsupported · 尚未接入', failed: 'error · 执行未完成' })[response.status]
}
