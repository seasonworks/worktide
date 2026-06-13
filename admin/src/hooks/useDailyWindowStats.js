import { useCallback } from 'react'
import { getDailyWindowStats } from '../api/windows.js'
import usePolling from './usePolling.js'

/**
 * 30s 轮询 `/windows/stats/daily`，date / includeInactive 变化时自动换 fetcher。
 *
 * 注意 polling 节奏与服务端 aggregator 节奏对齐：每次 /windows/report 即触发
 * 同员工聚合，30s 足够拿到 t-30s 内的新 session。
 *
 * @param {Object}  options
 * @param {string}  options.date              'YYYY-MM-DD'（必填）
 * @param {boolean} [options.includeInactive=false]
 * @param {number}  [options.topN=20]
 * @param {number}  [options.interval=30000]
 * @param {boolean} [options.enabled=true]    date 为空时建议传 false
 * @returns {{ data, loading, error, refresh }}
 */
export default function useDailyWindowStats({
  date,
  includeInactive = false,
  topN = 20,
  interval = 30000,
  enabled = true,
} = {}) {
  const fetcher = useCallback(
    () => getDailyWindowStats({ date, includeInactive, topN }),
    [date, includeInactive, topN],
  )
  return usePolling(fetcher, {
    interval,
    enabled: Boolean(enabled && date),
    immediate: true,
  })
}
