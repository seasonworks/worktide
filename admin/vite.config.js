import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发代理：前端调用 /api/* 同源转发到后端 9000，开发期免 CORS。
// 生产构建时改用 VITE_API_BASE_URL 指向真实服务端。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:9000',
        changeOrigin: true,
      },
    },
  },
})
