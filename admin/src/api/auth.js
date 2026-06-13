// #4 · 后台登录 API。
import client from './client.js'
import { setToken, clearToken } from '../auth/token.js'

// 登录成功 → 存 token 并返回；失败抛出（页面 catch 弹提示）。
export async function login(password) {
  const { data } = await client.post('/auth/login', { password })
  if (data?.token) setToken(data.token)
  return data
}

// 退出登录：清 token 后回登录页。
export function logout() {
  clearToken()
  window.location.assign('/login')
}
