/** 将浏览器问数命名空间转换为已存在的领域 API 路径。 */
export function rewriteContest(path: string): string {
  return path.replace(/^\/api\/contest(?=\/|$)/, '/api')
}

/** 两个进程的存活检查均只转发到各服务真实的 /health。 */
export function rewriteHealth(): string { return '/health' }

/** 限制开发代理目的地为本机端口；这里不接收凭据或远端模型地址。 */
function localTarget(value: string | undefined, port: number): string {
  const target = value || `http://127.0.0.1:${port}`
  const url = new URL(target)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.port !== String(port)
      || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
    throw new Error('开发代理仅允许约定的本机服务地址')
  }
  return url.origin
}

/** 匹配范围互不重叠，健康检查不经通用 /api 重写；不代理未知业务路由。 */
export function createProxy(env: Record<string, string | undefined> = {}) {
  const contest = localTarget(env.FRONTEND_CONTEST_TARGET, 8001)
  const knowledge = localTarget(env.FRONTEND_KNOWLEDGE_TARGET, 8000)
  return {
    '^/health/contest/?$': { target: contest, changeOrigin: true, rewrite: rewriteHealth },
    '^/health/knowledge/?$': { target: knowledge, changeOrigin: true, rewrite: rewriteHealth },
    '^/api/contest(?:/|$)': { target: contest, changeOrigin: true, rewrite: rewriteContest },
    '^/api/knowledge(?:/|$)': { target: knowledge, changeOrigin: true },
    '^/api/admin(?:/|$)': { target: knowledge, changeOrigin: true },
  }
}
