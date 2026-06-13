import client from './client.js'

/**
 * Phase 5.3 · 设备健康 API。
 *
 * 后端路由：
 *   POST /api/v1/agent/health           (client 上报，Admin UI 不调)
 *   GET  /api/v1/devices                (列表，本文件 getDevices)
 *   GET  /api/v1/devices/{employee_id}  (详情，本文件 getDevice)
 *
 * 字段口径与 server/app/schemas.py 的 DeviceListItemOut / DeviceDetailOut 一致；
 * status 是 5 档枚举：healthy / degraded / unstable / stale / offline
 */

/**
 * GET /api/v1/devices —— 全员设备健康列表。
 *
 * @param {Object}  [opts]
 * @param {boolean} [opts.includeInactive=false]  与其它列表端点同义：归档员工是否可见
 * @returns {Promise<Array<{
 *   employee_id: number|null,
 *   employee_name: string|null,
 *   machine_id: string,
 *   hostname: string|null,
 *   agent_version: string,
 *   status: 'healthy'|'degraded'|'unstable'|'stale'|'offline',
 *   last_seen: string|null,
 *   last_upload: string|null,
 *   uptime_seconds: number,
 *   restart_count: number,
 *   last_exit_reason: string|null,
 *   pending_events: number,
 * }>>}
 */
export async function getDevices({ includeInactive = false } = {}) {
  const res = await client.get('/devices', {
    params: { include_inactive: includeInactive },
  })
  return res.data
}

/**
 * GET /api/v1/devices/{employee_id} —— 设备详情。
 *
 * 比列表多 process_started_at / first_start_at / last_start_at / last_exit_at /
 * last_report_at + 完整 watchdog 子结构（ages_seconds / misses / thresholds_seconds / enabled）。
 *
 * @param {number} employeeId
 */
export async function getDevice(employeeId) {
  if (!employeeId) throw new Error('getDevice: employeeId is required')
  const res = await client.get(`/devices/${employeeId}`)
  return res.data
}
