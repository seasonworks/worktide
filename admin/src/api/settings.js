import client from './client.js'

// Phase 6.5A · 系统设置 / General Settings 后端 4 个端点
// 不暴露 axios 给上层；统一返回 res.data，错误由 client.js interceptor 记日志

// GET /api/v1/settings —— 列出所有运行期可配置项（含当前值/默认/分组）
export async function getSettings() {
  const res = await client.get('/settings')
  return res.data  // { settings: [...], groups: [...] }
}

// PATCH /api/v1/settings/{key} —— 改单条
// value 类型按 setting.value_type 决定（int / bool / str）
export async function updateSetting(key, value) {
  const res = await client.patch(`/settings/${key}`, { value })
  return res.data  // 单条 SettingOut
}

// POST /api/v1/settings/{key}/reset —— 单条恢复默认（删 DB 覆盖行）
export async function resetSetting(key) {
  const res = await client.post(`/settings/${key}/reset`)
  return res.data  // { reset: 0|1, settings: [...] }
}

// POST /api/v1/settings/reset-all —— 一键恢复全部默认（review #9）
export async function resetAllSettings() {
  const res = await client.post('/settings/reset-all')
  return res.data
}
