import { Tag } from 'antd'

/**
 * Phase 5.3 · 5 档设备健康状态徽章。
 *
 * 状态来源：server/app/services/device_health.compute_status
 * 颜色约定：与 StatusTag / WorkStateTag 同口径（antd 内置语义色），不引图标依赖。
 *
 *   healthy  绿  健康     last_seen<60s 且无任何 watchdog 异常信号
 *   degraded 橙  退化     active 但有 watchdog miss 或最近退过 watchdog_timeout
 *   unstable 红  不稳定   最近 10min 内被 watchdog 杀过
 *   stale    黄  停滞     last_seen 在 [60s, 5min)
 *   offline  灰  离线     last_seen ≥ 5min
 */
const STATUS_MAP = {
  healthy:  { color: 'green',   text: '健康' },
  degraded: { color: 'orange',  text: '退化' },
  unstable: { color: 'red',     text: '不稳定' },
  stale:    { color: 'gold',    text: '停滞' },
  offline:  { color: 'default', text: '离线' },
}

export default function HealthStatusBadge({ status }) {
  const cfg = STATUS_MAP[status] || { color: 'default', text: status || '未知' }
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}

export { STATUS_MAP as HEALTH_STATUS_MAP }
