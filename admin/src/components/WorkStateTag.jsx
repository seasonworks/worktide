import { Tag } from 'antd'

// work_state → Tag 颜色与中文。与活动状态（StatusTag）正交，独立展示，不复用 StatusTag。
const WORK_STATE_MAP = {
  off_shift: { color: 'default', text: '下班' },
  working: { color: 'green', text: '工作中' },
  break_meal: { color: 'gold', text: '吃饭' },
  break_toilet: { color: 'orange', text: '厕所' },
  break_smoke: { color: 'volcano', text: '抽烟' },
}

// break_type → 中文，供列表/详情展示当前 break 类型
export const BREAK_TYPE_TEXT = { meal: '吃饭', toilet: '厕所', smoke: '抽烟' }

export default function WorkStateTag({ workState }) {
  const cfg = WORK_STATE_MAP[workState] || { color: 'default', text: workState || '未知' }
  return <Tag color={cfg.color}>{cfg.text}</Tag>
}
