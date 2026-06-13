import { Tag } from 'antd'

/**
 * Phase 5.4 · 6 档升级状态徽章。
 *
 * 状态来源：client/app/update_state.py（与 server schemas.HealthUpdateIn.status 一致）
 *
 *   idle         灰    空闲（无升级活动；列表里默认不显示，DOM 简洁）
 *   checking     蓝    检查中
 *   downloading  蓝    下载中
 *   staged       青    已下载（等待安装；安装窗口内的瞬态）
 *   installing   橙    安装中（agent 已 exit，updater 在跑）
 *   failed       红    失败（last_error 有内容）
 */
const STATUS_MAP = {
  idle:        { color: 'default', text: '空闲' },
  checking:    { color: 'blue',    text: '检查中' },
  downloading: { color: 'blue',    text: '下载中' },
  staged:      { color: 'cyan',    text: '已下载' },
  installing:  { color: 'orange',  text: '安装中' },
  failed:      { color: 'red',     text: '失败' },
}

export default function UpdateStatusBadge({ status, hideIdle = false }) {
  const cfg = STATUS_MAP[status] || { color: 'default', text: status || '未知' }
  // hideIdle: 列表里 idle 不展示，减少视觉噪音；详情页 hideIdle=false 总是显示
  if (hideIdle && status === 'idle') return null
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}

export { STATUS_MAP as UPDATE_STATUS_MAP }
