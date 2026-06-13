// #4 · 后台登录 token 的本地存取（localStorage）。
const KEY = 'worktide_admin_token'

export function getToken() {
  try {
    return localStorage.getItem(KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token) {
  try {
    localStorage.setItem(KEY, token)
  } catch {
    /* localStorage 不可用时静默：登录态退化为本次会话内存 */
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}
