import client from './client.js'

// GET /api/v1/telegram/bindings —— 所有 Telegram 绑定（含 employee_name）
export async function getBindings() {
  const res = await client.get('/telegram/bindings')
  return res.data
}

// GET /api/v1/telegram/unbound-users —— 最近未绑定 Telegram 用户
export async function getUnboundUsers({ limit = 200 } = {}) {
  const res = await client.get('/telegram/unbound-users', { params: { limit } })
  return res.data
}

// POST /api/v1/telegram/bindings —— 新增绑定（员工已绑或 tg 已占用返回 409）
export async function createBinding(payload) {
  // payload: { employee_id, telegram_user_id, telegram_username? }
  const res = await client.post('/telegram/bindings', payload)
  return res.data
}

// PUT /api/v1/telegram/bindings/{employee_id} —— 改绑（换 Telegram 账号）
export async function rebindBinding(employeeId, payload) {
  // payload: { telegram_user_id, telegram_username? }
  const res = await client.put(`/telegram/bindings/${employeeId}`, payload)
  return res.data
}

// DELETE /api/v1/telegram/bindings/{employee_id} —— 解绑（幂等：未绑定返回 unbound=false）
export async function deleteBinding(employeeId) {
  const res = await client.delete(`/telegram/bindings/${employeeId}`)
  return res.data
}
