import { useCallback } from 'react'
import { getEmployeeWindowSessions } from '../api/windows.js'
import usePolling from './usePolling.js'

/**
 * 30s 轮询某员工某日的 window_sessions。
 *
 * 该端点服务端**不过滤 is_active**，archived 员工历史也可查（与设计一致）。
 *
 * @param {number}  employeeId
 * @param {Object}  options
 * @param {string}  options.date             'YYYY-MM-DD'
 * @param {number}  [options.limit=200]
 * @param {number}  [options.interval=30000]
 * @param {boolean} [options.enabled=true]   id 或 date 为空时建议传 false
 * @returns {{ data, loading, error, refresh }}
 */
export default function useEmployeeWindowSessions(
  employeeId,
  { date, limit = 200, interval = 30000, enabled = true } = {},
) {
  const fetcher = useCallback(
    () => getEmployeeWindowSessions(employeeId, { date, limit }),
    [employeeId, date, limit],
  )
  return usePolling(fetcher, {
    interval,
    enabled: Boolean(enabled && employeeId && date),
    immediate: true,
  })
}
