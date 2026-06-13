// 时间与空闲秒数格式化（原生 Date，不依赖 dayjs）

// 后端 SQLite 取回的时间多为 UTC 但不带时区标识，补 Z 以便正确解析为 UTC，
// 再以浏览器本地时区展示。
function normalizeIso(value) {
  let s = String(value)
  const hasTz = /Z$/.test(s) || /[+-]\d{2}:\d{2}$/.test(s)
  if (/\dT\d/.test(s) && !hasTz) s += 'Z'
  return s
}

export function formatDateTime(value) {
  if (!value) return '-'
  const d = new Date(normalizeIso(value))
  if (Number.isNaN(d.getTime())) return String(value)
  const pad = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

export function formatIdle(seconds) {
  if (seconds == null) return '-'
  const s = Number(seconds)
  if (Number.isNaN(s)) return '-'
  if (s < 60) return `${s} 秒`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m} 分 ${s % 60} 秒`
  const h = Math.floor(m / 60)
  return `${h} 时 ${m % 60} 分`
}

// 通用时长格式化：秒 → "X 秒" / "X 分 Y 秒" / "X 时 Y 分 Z 秒"。
// 统一 break / shift / 统计 / 走表计时的时长显示，避免各页面自行拼接。
export function formatDuration(seconds) {
  if (seconds == null) return '-'
  let s = Number(seconds)
  if (Number.isNaN(s)) return '-'
  if (s < 0) s = 0
  s = Math.floor(s)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h} 时 ${m} 分 ${sec} 秒`
  if (m > 0) return `${m} 分 ${sec} 秒`
  return `${sec} 秒`
}

// 本地时区的 YYYY-MM-DD（统计页默认日期用）。注意：后端按 UTC 日聚合。
export function localDateString(d = new Date()) {
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}
