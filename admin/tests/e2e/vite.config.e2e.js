/**
 * Phase 4.3 · 5.5 · E2E 专用 vite 配置。
 *
 * 与项目根 vite.config.js 的差异：
 *  - proxy target 不硬编码 9000，改读 env：VITE_DEV_API_TARGET
 *    （e2e.test.mjs 注入隔离后端的实际端口）
 *  - 监听端口由 env：E2E_VITE_PORT 指定（默认 5273，避开默认 5173）
 *
 * 用法：
 *   VITE_DEV_API_TARGET=http://127.0.0.1:9160 \
 *   E2E_VITE_PORT=5273 \
 *   node node_modules/vite/bin/vite.js \
 *     --config tests/e2e/vite.config.e2e.js
 */
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const target = process.env.VITE_DEV_API_TARGET || 'http://localhost:9000'
const port = Number(process.env.E2E_VITE_PORT) || 5273

export default defineConfig({
  plugins: [react()],
  server: {
    port,
    strictPort: true,        // 端口冲突直接报错，不偷偷换端口
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target,
        changeOrigin: true,
      },
    },
  },
})
