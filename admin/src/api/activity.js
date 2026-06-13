import client from './client.js'

// GET /api/v1/activity/recent —— 最近活动记录（含 employee_name/hostname），时间倒序。
// limit：返回条数上限（后端最大 500）；employeeId：可选，传入则服务端只返回该员工的最近记录。
export async function getRecentActivity({ limit = 100, employeeId } = {}) {
  const params = { limit }
  if (employeeId != null) params.employee_id = employeeId
  const res = await client.get('/activity/recent', { params })
  return res.data
}
