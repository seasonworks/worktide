import { Card, Statistic } from 'antd'

/**
 * 统一的 KPI 卡片：淡色渐变底 + 顶部强调色描边 + 柔和阴影 + 粗体彩色数值。
 * 可点击（onClick）时带 hover 效果，active 时高亮描边——用于「点卡片筛选」。
 */
export default function StatCard({
  title,
  value,
  suffix,
  accent = '#4f46e5',
  sub,
  onClick,
  active = false,
}) {
  return (
    <Card
      size="small"
      hoverable={!!onClick}
      onClick={onClick}
      variant="borderless"
      styles={{ body: { padding: 16 } }}
      style={{
        borderRadius: 14,
        borderTop: `3px solid ${accent}`,
        background: `linear-gradient(180deg, ${accent}0d 0%, #ffffff 60%)`,
        boxShadow: active
          ? `0 0 0 2px ${accent}, 0 6px 16px rgba(0,0,0,0.08)`
          : '0 1px 2px rgba(0,0,0,0.04), 0 6px 16px rgba(0,0,0,0.05)',
        cursor: onClick ? 'pointer' : 'default',
        height: '100%',
      }}
    >
      <Statistic
        title={title}
        value={value}
        suffix={suffix}
        valueStyle={{ color: accent, fontSize: 24, fontWeight: 600, lineHeight: 1.2 }}
      />
      {sub != null && (
        <div style={{ color: '#8c8c8c', fontSize: 12, marginTop: 4 }}>{sub}</div>
      )}
    </Card>
  )
}
