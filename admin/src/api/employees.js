import client from './client.js'

// GET /api/v1/employees —— 员工列表（默认仅在职；include_inactive=true 含已离职）
export async function getEmployees({ includeInactive = false } = {}) {
  const res = await client.get('/employees', {
    params: { include_inactive: includeInactive },
  })
  return res.data
}

// GET /api/v1/employees/{id} —— 单个员工详情（用于详情页信息卡）
export async function getEmployee(id) {
  const res = await client.get(`/employees/${id}`)
  return res.data
}

// GET /api/v1/employees/{id}/logs —— 该员工历史上报记录（时间倒序）
export async function getEmployeeLogs(id, { limit = 100 } = {}) {
  const res = await client.get(`/employees/${id}/logs`, { params: { limit } })
  return res.data
}

// PATCH /api/v1/employees/{id} —— 修改员工姓名（后台权威，覆盖客户端默认 None 上报）
export async function updateEmployee(id, payload) {
  const res = await client.patch(`/employees/${id}`, payload)
  return res.data
}

// PATCH /api/v1/employees/{id}/archive —— 软删除（离职）：自动 clock_out + is_active=false
export async function archiveEmployee(id) {
  const res = await client.patch(`/employees/${id}/archive`)
  return res.data
}

// PATCH /api/v1/employees/{id}/restore —— 恢复员工：is_active=true、deleted_at=null
export async function restoreEmployee(id) {
  const res = await client.patch(`/employees/${id}/restore`)
  return res.data
}
