import client from './client.js'

// GET /api/v1/work/status —— 全员实时工时状态（activity_status + work_state + 实时秒数）
export async function getWorkStatus() {
  const res = await client.get('/work/status')
  return res.data
}

// GET /api/v1/work/stats/daily?date=YYYY-MM-DD —— 按员工聚合的单日工时统计（UTC 日）
export async function getDailyStats(date) {
  const res = await client.get('/work/stats/daily', { params: { date } })
  return res.data
}

// GET /api/v1/work/employees/{id}/shift-stats —— 当前/最近一次班次统计（迟到/累计挂机等）
export async function getEmployeeShiftStats(id) {
  const res = await client.get(`/work/employees/${id}/shift-stats`)
  return res.data
}

// GET /api/v1/work/employees/{id}/breaks —— 某员工 break 历史（时间倒序）
export async function getEmployeeBreaks(id, { limit = 100, breakType, date } = {}) {
  const params = { limit }
  if (breakType) params.break_type = breakType
  if (date) params.date = date
  const res = await client.get(`/work/employees/${id}/breaks`, { params })
  return res.data
}

// GET /api/v1/work/employees/{id}/shifts —— 某员工 shift 历史（时间倒序）
export async function getEmployeeShifts(id, { limit = 100 } = {}) {
  const res = await client.get(`/work/employees/${id}/shifts`, { params: { limit } })
  return res.data
}
