/** 客户端仅显示固定说明，绝不透传异常.message、上游 detail 或代理 HTML。 */
const messages: Record<string, string> = {
  PERMISSION_DENIED: '访问被拒绝，请核对入口令牌及当前会话。',
  SCOPE_INVALID: '数据范围无效或已变化，请重新载入目录并创建会话。',
  AUTHORIZATION_REQUIRED: '缺少本任务的有效云许可；没有自动追加模型请求。',
  BUDGET_EXHAUSTED: '本任务预算不足，请核对已有调用后再显式授权。',
  SQL_REJECTED: 'SQL 被后端安全校验拒绝，未作为成功查询展示。',
  MODEL_FAILURE: '模型调用未完成，请核对服务及费用记录。',
  SQL_EXECUTION_FAILED: '数据库查询失败，未获得成功结果。',
  EXECUTION_UNKNOWN: '执行结果未知，请先核对服务记录，切勿盲目重发。',
  DEPENDENCY_UNAVAILABLE: '依赖或数据源目录不可用，请检查后端配置。',
  RESULT_INVALID: '响应或请求不符合约定，未发布查询结果。',
  UNSUPPORTED: '该能力尚未接入。', INTERNAL_ERROR: '服务未完成，请检查安全日志。',
  KNOWLEDGE_INPUT: '知识库、文档范围或问题参数无效。',
  KNOWLEDGE_RESULT: '知识服务未返回符合契约的证据。',
  KNOWLEDGE_INTERNAL: '知识服务未完成，请检查安全日志。',
  RAG_CLOUD_PERMISSION: '本次未获得云外传及费用许可。',
  LLM_CLOUD_PERMISSION: '本次未获得模型调用许可。',
  RAG_EVIDENCE: '证据当前不可用，请核对文档状态。',
  RAG_CONTEXT_LIMIT: '证据超过模型上下文限制，未生成答案。',
  NETWORK: '服务连接中断或请求超时；执行与费用状态未知，请先核对后端，页面不会自动重试。',
  HTTP_401: '身份验证失败，请核对入口令牌。', HTTP_403: '服务拒绝访问或调用许可。',
  HTTP_404: '请求资源不存在。', HTTP_409: '资源状态冲突，请刷新确认后操作。',
  HTTP_413: '上传或请求大小超限。', HTTP_415: '文件格式不支持或内容不符。',
  HTTP_422: '请求字段不符合服务契约。', HTTP_429: '请求受限，请稍后手动处理。',
  HTTP_500: '服务内部错误，请检查安全日志。', HTTP_502: '上游服务响应不可用。',
  HTTP_503: '服务或依赖不可用。', HTTP_507: '文件存储不可用。',
}
export class ApiError extends Error {
  code: string
  httpStatus: number
  /** 仅保存白名单错误码，丢弃原始响应与异常。 */
  constructor(code: string, status = 0) {
    super(messages[code] || '请求未完成，请检查后端状态。')
    this.name = 'ApiError'
    this.code = Object.hasOwn(messages, code) ? code : 'INTERNAL_ERROR'
    this.httpStatus = status
  }
}

/** 任意捕获值都转换为固定提示，不把未知异常字符串展示给用户。 */
export function safeMessage(error: unknown): string {
  return error instanceof ApiError ? `${error.message} [${error.code}]` : messages.INTERNAL_ERROR
}

/** 防止数组/null被误当响应对象；运行时校验不代替后端鉴权。 */
export function record(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/** 仅提取已注册的安全错误码，其余按 HTTP 状态降级。 */
export function responseError(body: unknown, status: number): ApiError {
  const code = record(body) && record(body.detail) ? body.detail.code : null
  return new ApiError(typeof code === 'string' && Object.hasOwn(messages, code) ? code : `HTTP_${status}`, status)
}

export type Transport = (path: string, init?: RequestInit) => Promise<{ status: number; ok: boolean; body: unknown }>

/** 真实同源请求只发送一次；超时/断网不重放操作，不缓存令牌、SQL或原文。 */
export const transport: Transport = async (path, init = {}) => {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 120000)
  try {
    if (!path.startsWith('/') || path.startsWith('//')) throw new ApiError('RESULT_INVALID')
    const response = await fetch(path, {
      ...init, signal: controller.signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      referrerPolicy: 'no-referrer',
    })
    const body: unknown = await response.json().catch(() => null)
    return { status: response.status, ok: response.ok, body }
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError('NETWORK')
  } finally { clearTimeout(timer) }
}

/** JSON 参数不含客户端自造槽位或数据库配置。 */
export function jsonBody(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

/** 校验身份再放入路由；拒绝把任意字符串当作路径或查询串。 */
export function uuid(value: string): string {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
      || value === '00000000-0000-0000-0000-000000000000') throw new ApiError('RESULT_INVALID')
  return value
}
