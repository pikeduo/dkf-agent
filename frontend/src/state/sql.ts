import { reactive } from 'vue'
import { ApiError, safeMessage } from '../api/http.ts'
import type { ContestApi } from '../api/contest.ts'
import type { CatalogSource, ClarificationAnswer, ClarificationPrompt, CloudAuthorization, ConsentRequest, Session, SQLResponse, TraceRecord } from '../types/contest.ts'

/** 每次重置均构造零云预算，不复用旧任务许可。 */
export function noCloud(): CloudAuthorization {
  return { allow_cloud: false, max_calls: 0, max_total_tokens: 0, allow_sql_repair: false }
}

/** 会话状态与 API 分离；凭据、问题和执行结果仅保存在当前页面内存。 */
export function createSQLWorkspace(api: ContestApi) {
  const state = reactive({
    sources: [] as CatalogSource[], selected: -1, catalogLoaded: false,
    session: null as Session | null, originalQuestion: '', draft: '',
    cloud: noCloud(), response: null as SQLResponse | null, history: [] as SQLResponse[],
    pending: null as ClarificationPrompt | null, trace: null as TraceRecord | null,
    busy: false, error: '', uncertain: false,
  })

  /** 清除本地旧范围的全部状态；令牌或数据源变化绝不携带旧确认。 */
  function clear() {
    state.session = null; state.originalQuestion = ''; state.draft = ''; state.cloud = noCloud()
    state.response = null; state.history = []; state.pending = null; state.trace = null
    state.error = ''; state.uncertain = false
  }

  /** 每次目录请求都来自后端；失败清空选项，不能回退到硬编码示例。 */
  async function loadCatalog() {
    if (state.busy) return
    state.busy = true; clear(); state.sources = []; state.selected = -1; state.catalogLoaded = false
    try { state.sources = await api.catalog(); state.catalogLoaded = true }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 切换范围立即废弃旧会话，用户另行点击创建；服务端无删除会话接口。 */
  function select(index: number) {
    if (state.busy) return
    clear(); state.selected = index >= 0 && index < state.sources.length ? index : -1
  }

  /** 明确创建会话后才可问数，UUID不能替代私钥鉴权。 */
  async function createSession() {
    const source = state.sources[state.selected]
    if (state.busy || !source) return
    state.busy = true; clear()
    try { state.session = await api.createSession(source.scope) }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 修正问题或已确认条件先请求后端 reset，成功后才清空本地确认和许可。 */
  async function reset() {
    if (state.busy || !state.session) return
    state.busy = true; state.error = ''
    const draft = state.originalQuestion || state.draft
    try {
      await api.reset(state.session)
      state.originalQuestion = ''; state.draft = draft; state.cloud = noCloud()
      state.pending = null; state.response = null; state.history = []; state.trace = null; state.uncertain = false
    } catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 只有用户单独点击费用授权才签发许可，不自动补充预算或追加 SQL 修复。 */
  async function authorize(body: ConsentRequest) {
    if (state.busy || !state.session || !state.draft.trim() || state.uncertain) return
    state.busy = true; state.error = ''
    try { state.cloud = await api.consent(state.session, body) }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  /** 使用相同原问题及服务端澄清版本恢复，客户端绝不提交 confirmed_slots。 */
  async function submit(answers?: ClarificationAnswer[]) {
    if (state.busy || !state.session || state.uncertain) return
    const question = state.originalQuestion || state.draft
    if (!question.trim() || question.length > 4000 || state.pending && !answers?.length) return
    if (answers && !state.pending) return
    state.busy = true; state.error = ''; state.response = null; state.trace = null
    state.originalQuestion = question
    try {
      const pending = state.pending
      const response = await api.query({
        version: 'contest-agent-v1', client_request_id: crypto.randomUUID(),
        session_id: state.session.session_id, question, intent: 'sql', sql_scope: { ...state.session.scope },
        cloud: { ...state.cloud },
        ...(pending && answers ? { clarification: { clarification_id: pending.clarification_id,
          reply_to_run_id: pending.reply_to_run_id, revision: pending.revision, answers } } : {}),
      }, state.session)
      state.response = response; state.history.push(response)
      if (state.history.length > 20) state.history.shift()
      state.pending = response.result.kind === 'clarification' ? response.result : null
      state.uncertain = response.result.kind === 'failure' && response.result.code === 'EXECUTION_UNKNOWN'
        || response.result.kind === 'sql' && response.result.attempts.some(item => item.execution === 'unknown')
    } catch (error) {
      state.error = safeMessage(error)
      // 传输失败/坏响应可能已经消费澄清或费用；保留身份，禁止重复执行。
      state.uncertain = !(error instanceof ApiError) || ['NETWORK', 'RESULT_INVALID', 'INTERNAL_ERROR'].includes(error.code)
    } finally { state.busy = false }
  }

  /** 只从当前凭据会话查 Trace；失败清空旧记录，避免误示上次成功。 */
  async function loadTrace(traceId: string) {
    if (state.busy || !state.session) return
    state.busy = true; state.error = ''; state.trace = null
    try { state.trace = await api.trace(traceId, state.session) }
    catch (error) { state.error = safeMessage(error) }
    finally { state.busy = false }
  }

  return { state, clear, loadCatalog, select, createSession, reset, authorize, submit, loadTrace }
}
