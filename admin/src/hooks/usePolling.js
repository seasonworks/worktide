import { useCallback, useEffect, useRef, useState } from 'react'
import logger from '../utils/logger.js'

/**
 * 周期性执行异步取数函数（用于列表自动刷新）。
 *
 * @param {Function} fetcher 返回 Promise 的取数函数
 * @param {Object}   options
 * @param {number}   options.interval  轮询间隔（毫秒），默认 30000
 * @param {boolean}  options.immediate 挂载时是否立即取一次，默认 true
 * @param {boolean}  options.enabled   是否启用轮询，默认 true
 * @returns {{ data, loading, error, refresh }}
 */
export default function usePolling(
  fetcher,
  { interval = 30000, immediate = true, enabled = true } = {},
) {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)

  // 用 ref 持有最新 fetcher，避免因函数引用变化重建定时器
  const fetcherRef = useRef(fetcher)
  fetcherRef.current = fetcher

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const result = await fetcherRef.current()
      setData(result)
      setError(null)
      return result
    } catch (err) {
      logger.error('轮询取数失败', err?.message)
      setError(err)
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return undefined
    if (immediate) refresh().catch(() => {})
    const timer = setInterval(() => refresh().catch(() => {}), interval)
    return () => clearInterval(timer)
  }, [enabled, interval, immediate, refresh])

  return { data, loading, error, refresh }
}
