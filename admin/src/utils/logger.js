// 前端统一日志封装：控制台输出 + 级别前缀。
// 所有异常都应经此记录，便于排查（满足"所有异常必须 logging"）。
const logger = {
  info: (...args) => console.info('[INFO]', ...args),
  warn: (...args) => console.warn('[WARN]', ...args),
  error: (...args) => console.error('[ERROR]', ...args),
  debug: (...args) => console.debug('[DEBUG]', ...args),
}

export default logger
