import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createProxy } from './proxy.ts'

// 只由 Node 读取 FRONTEND_ 地址，浏览器凭据不进入环境文件或构建常量。
export default defineConfig(({ mode }) => {
  // 开发环境监听固定本机端口，不自动寻找其他端口或开放局域网。
  const env = loadEnv(mode, process.cwd(), 'FRONTEND_')
  return {
    plugins: [vue()],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: createProxy(env) },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true },
  }
})
