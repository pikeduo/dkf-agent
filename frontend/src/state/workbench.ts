import { reactive } from 'vue'
import { createContestApi } from '../api/contest.ts'
import { createKnowledgeApi } from '../api/knowledge.ts'
import { createSQLWorkspace } from './sql.ts'

// 入口令牌只保存在内存；不写入 localStorage/sessionStorage/URL/环境变量。
export const credentials = reactive({ token: '' })
export const contestApi = createContestApi(() => credentials.token)
export const knowledgeApi = createKnowledgeApi()
export const sql = createSQLWorkspace(contestApi)

/** 改换入口身份立即清除旧目录、会话私钥和执行证据，阻止跨身份复用。 */
export function setToken(value: string) {
  if (sql.state.busy) return
  credentials.token = value.trim(); sql.clear(); sql.state.sources = []; sql.state.selected = -1
  sql.state.catalogLoaded = false
}

/** 统一Agent接入保留设计位置；未实现的端点不创建可点击调用或假返回。 */
export const unifiedAgent = Object.freeze({ enabled: false, contract: 'contest-agent-v1', route: null })
