import type { SQLRequest, SQLResponse } from './contest.ts'
import type { QueryRequest, SearchResponse } from './knowledge.ts'

// 只在用户手动 typecheck 时检查契约，运行时无HTTP、模型、数据库副作用。
/** 确保客户端请求不能注入自称已确认的槽位。 */
function checkRequest(request: SQLRequest) {
  // @ts-expect-error confirmed_slots只允许出现在服务端RunContext。
  request.confirmed_slots = []
  // @ts-expect-error SQL权限不能由客户端任意表允许集授予。
  request.sql_scope.tables = ['private']
}
/** 确保无损结果不接收JavaScript number或旧rows/answer_ready接口。 */
function checkResponse(response: SQLResponse) {
  if (response.result.kind === 'sql') {
    // @ts-expect-error 数值必须由服务端返回无损字符串。
    response.result.rows = [[9007199254740992]]
  }
  // @ts-expect-error 冻结契约不存在answer_ready。
  response.answer_ready = true
}
/** 确保RAG许可是布尔值且检索证据不冒充RAGAnswer。 */
function checkKnowledge(request: QueryRequest, search: SearchResponse) {
  // @ts-expect-error 费用许可不能用非空字符串冒充true。
  request.allow_cloud = 'true'
  // @ts-expect-error 仅检索接口不返回生成答案。
  search.answer = '伪造'
}
void checkRequest; void checkResponse; void checkKnowledge
