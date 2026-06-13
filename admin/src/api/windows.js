import client from './client.js'

/**
 * GET /api/v1/windows/stats/daily —— 单日全员窗口活动统计（按员工 + 按 process 聚合）。
 *
 * 与服务端 `daily_window_stats` 路由一致：
 * - 默认 include_inactive=false：archived 员工**完全隐身**
 * - top_n 默认 20，单员工返回的 top_apps 最多 top_n 条
 *
 * @param {Object}  params
 * @param {string}  params.date            'YYYY-MM-DD' UTC 单日（必填）
 * @param {boolean} [params.includeInactive=false]
 * @param {number}  [params.topN=20]       每员工 top_apps 长度上限
 * @returns {Promise<Array<{
 *   employee_id: number,
 *   name: string,
 *   date: string,
 *   total_working_seconds: number,
 *   total_break_seconds: number,
 *   total_off_shift_seconds: number,
 *   top_apps: Array<{
 *     process_name: string,
 *     working_seconds: number,
 *     break_seconds: number,
 *     off_shift_seconds: number,
 *     total_seconds: number,
 *   }>,
 * }>>}
 */
export async function getDailyWindowStats({
  date,
  includeInactive = false,
  topN = 20,
} = {}) {
  if (!date) throw new Error('getDailyWindowStats: date is required')
  const res = await client.get('/windows/stats/daily', {
    params: {
      date,
      include_inactive: includeInactive,
      top_n: topN,
    },
  })
  return res.data
}

/**
 * GET /api/v1/windows/employees/{id} —— 某员工某 UTC 日的 window_sessions（时间倒序）。
 *
 * 历史端点：**不过滤 is_active**，archived 员工的历史可查（与设计一致）。
 *
 * @param {number} id
 * @param {Object} params
 * @param {string} params.date          'YYYY-MM-DD' UTC 单日
 * @param {number} [params.limit=200]   单次返回上限（服务端 max_query_limit 兜底）
 */
export async function getEmployeeWindowSessions(id, { date, limit = 200 } = {}) {
  if (!id) throw new Error('getEmployeeWindowSessions: id is required')
  if (!date) throw new Error('getEmployeeWindowSessions: date is required')
  const res = await client.get(`/windows/employees/${id}`, {
    params: { date, limit },
  })
  return res.data
}
