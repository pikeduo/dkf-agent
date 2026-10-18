import { ApiError, jsonBody, record, responseError, transport, uuid } from './http.ts'
import type { Transport } from './http.ts'
import { sqlResponse, traceRecord } from './guards.ts'
import type { CatalogSource, CloudAuthorization, ConsentRequest, ContestHealth, Session, SQLRequest } from '../types/contest.ts'

/** 绑定内存入口令牌；不把令牌放入 URL，不自动重试任何会话/许可/查询操作。 */
export function createContestApi(token: () => string, send: Transport = transport) {
  /** 每次从内存取得入口令牌；会话私钥只用于当前会话请求。 */
  async function request(path: string, init: RequestInit = {}, session?: Session, domain = false) {
    if (!token()) throw new ApiError('PERMISSION_DENIED', 401)
    const headers = new Headers(init.headers)
    headers.set('Authorization', `Bearer ${token()}`)
    if (session) headers.set('X-Session-Token', session.session_token)
    const result = await send(`/api/contest${path}`, { ...init, headers })
    // 领域失败同样带真实执行与 Trace，不能因 HTTP 非2xx直接丢弃。
    if (!result.ok && !(domain && record(result.body) && result.body.version === 'contest-agent-v1')) {
      throw responseError(result.body, result.status)
    }
    return result.body
  }
  return {
    /** 目录不可用时抛错，绝不附加演示源。 */
    async catalog(): Promise<CatalogSource[]> {
      const body = await request('/catalog')
      if (!record(body) || !Array.isArray(body.sources) || !body.sources.every(source => record(source)
          && record(source.scope) && typeof source.scope.db_id === 'string' && typeof source.scope.sub_db === 'string'
          && typeof source.schema_name === 'string' && typeof source.catalog_fingerprint === 'string'
          && Array.isArray(source.tables) && source.tables.every(table => record(table) && typeof table.name === 'string' && record(table.columns))
          && Array.isArray(source.foreign_keys) && source.foreign_keys.every(fk => record(fk)
            && typeof fk.reference_id === 'string' && typeof fk.from_table === 'string' && typeof fk.to_table === 'string'
            && Array.isArray(fk.pairs) && fk.pairs.every(pair => Array.isArray(pair) && pair.length === 2 && pair.every(p => typeof p === 'string')))
          && Array.isArray(source.aliases) && Array.isArray(source.conditions))) throw new ApiError('RESULT_INVALID')
      return body.sources as CatalogSource[]
    },
    /** 用真实授权范围建立会话，服务端发放私钥。 */
    async createSession(scope: CatalogSource['scope']): Promise<Session> {
      const body = await request('/sessions', jsonBody({ scope }))
      if (!record(body) || typeof body.session_id !== 'string' || typeof body.session_token !== 'string'
          || !record(body.scope) || body.scope.db_id !== scope.db_id || body.scope.sub_db !== scope.sub_db
          || typeof body.scope_fingerprint !== 'string' || typeof body.expires_in_seconds !== 'number') throw new ApiError('RESULT_INVALID')
      uuid(body.session_id)
      return body as unknown as Session
    },
    /** 本任务外传及费用许可由后端签发，勾选框不能代替服务端检查。 */
    async consent(session: Session, body: ConsentRequest): Promise<CloudAuthorization> {
      const result = await request(`/sessions/${uuid(session.session_id)}/consents`, jsonBody(body), session)
      if (!record(result) || result.allow_cloud !== true || typeof result.consent_id !== 'string'
          || result.provider_id !== body.provider_id || result.max_calls !== body.max_calls
          || result.max_total_tokens !== body.max_total_tokens || result.allow_sql_repair !== body.allow_sql_repair) throw new ApiError('RESULT_INVALID')
      return result as unknown as CloudAuthorization
    },
    /** 修改条件必须清空后端旧问题/槽位/许可；不虚构删除会话接口。 */
    async reset(session: Session): Promise<void> {
      const result = await request(`/sessions/${uuid(session.session_id)}/reset`, { method: 'POST' }, session)
      if (!record(result) || result.status !== 'reset' || result.session_id !== session.session_id) throw new ApiError('RESULT_INVALID')
    },
    /** 保留原问题和合法澄清身份，仅消费真实领域 DTO。 */
    async query(body: SQLRequest, session: Session) {
      const response = sqlResponse(await request('/query', jsonBody(body), session, true))
      if (response.context.session_id !== session.session_id || response.context.client_request_id !== body.client_request_id
          || response.context.scope_fingerprint !== session.scope_fingerprint) throw new ApiError('RESULT_INVALID')
      if (response.result.kind === 'sql' && (response.result.scope.db_id !== body.sql_scope.db_id
          || response.result.scope.sub_db !== body.sql_scope.sub_db)) throw new ApiError('RESULT_INVALID')
      return response
    },
    /** 只读当前会话安全 Trace；缺记录时不生成空成功快照。 */
    async trace(traceId: string, session: Session) {
      const result = traceRecord(await request(`/traces/${uuid(traceId)}?session_id=${uuid(session.session_id)}`, {}, session))
      if (result.trace_id !== traceId || result.session_id !== session.session_id) throw new ApiError('RESULT_INVALID')
      return result
    },
  }
}
export type ContestApi = ReturnType<typeof createContestApi>

/** 存活状态仅表示进程响应，不能推导数据库、模型或预算可用。 */
export async function contestHealth(send: Transport = transport): Promise<ContestHealth> {
  const result = await send('/health/contest')
  if (!result.ok) throw responseError(result.body, result.status)
  if (!record(result.body) || result.body.status !== 'alive' || result.body.service !== 'contest-sql') throw new ApiError('RESULT_INVALID')
  return result.body as unknown as ContestHealth
}
