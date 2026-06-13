import axios from 'axios'
import logger from '../utils/logger.js'
import { getToken, clearToken } from '../auth/token.js'

// 开发模式默认 /api/v1（由 Vite 代理转发到后端 9000）；
// 生产模式用 VITE_API_BASE_URL 指向真实服务端。
const baseURL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

const client = axios.create({
  baseURL,
  timeout: 10000,
})

// #4 · 每个请求自动带上后台登录 token（有则带）
client.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 统一记录请求异常，再抛给调用方（页面 catch 后弹提示）
client.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status
    const url = error?.config?.url || ''
    logger.error('API 请求失败', { url, status, message: error?.message })
    // #4 · token 失效/未认证：清登录态并回登录页（登录请求本身除外，让其弹"密码错误"）
    if (status === 401 && !url.includes('/auth/login')) {
      clearToken()
      if (!window.location.pathname.startsWith('/login')) {
        window.location.assign('/login')
      }
    }
    return Promise.reject(error)
  },
)

export default client
