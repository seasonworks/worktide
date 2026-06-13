import { Tag } from 'antd'

// 员工状态 → 统一的 Tag 颜色与中文文案
const STATUS_MAP = {
  online: { color: 'green', text: '在线' },
  idle: { color: 'orange', text: '挂机' },
  offline: { color: 'default', text: '离线' },
}

export default function StatusTag({ status }) {
  const cfg = STATUS_MAP[status] || { color: 'default', text: status || '未知' }
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}
