import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, App, Button, Card, Col, Empty, Row, Space, Table, Typography } from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getDailyStats } from '../api/work.js'
import StatCard from '../components/StatCard.jsx'
import { formatDuration, localDateString } from '../utils/format.js'

const { Title } = Typography
const CARD_STYLE = {
  borderRadius: 14,
  boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 6px 16px rgba(0,0,0,0.05)',
}

export default function WorkStatsPage() {
  const { message } = App.useApp()
  const [date, setDate] = useState(localDateString())
  const [lastUpdated, setLastUpdated] = useState(null)

  // 日期变化即重取；统计变化较慢，30s 轮询足够
  const fetcher = useCallback(() => getDailyStats(date), [date])
  const { data, loading, error, refresh } = usePolling(fetcher, {
    interval: 30000,
    immediate: false,
  })

  useEffect(() => {
    refresh().catch(() => {})
  }, [date, refresh])

  useEffect(() => {
    if (data) setLastUpdated(new Date())
  }, [data])

  const rows = Array.isArray(data) ? data : []

  const totals = useMemo(
    () =>
      rows.reduce(
        (acc, r) => ({
          gross: acc.gross + (r.gross_shift_seconds || 0),
          brk: acc.brk + (r.break_seconds || 0),
          net: acc.net + (r.net_work_seconds || 0),
        }),
        { gross: 0, brk: 0, net: 0 },
      ),
    [rows],
  )

  const topNet = useMemo(
    () =>
      [...rows]
        .sort((a, b) => (b.net_work_seconds || 0) - (a.net_work_seconds || 0))
        .slice(0, 10),
    [rows],
  )
  const maxNet = topNet.length ? (topNet[0].net_work_seconds || 0) || 1 : 1

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  const columns = [
    { title: '员工', dataIndex: 'name', ellipsis: true },
    {
      title: '在岗时长',
      dataIndex: 'gross_shift_seconds',
      width: 150,
      sorter: (a, b) => a.gross_shift_seconds - b.gross_shift_seconds,
      render: (v) => formatDuration(v),
    },
    { title: 'Break 总时长', dataIndex: 'break_seconds', width: 150, render: (v) => formatDuration(v) },
    {
      title: '净工时',
      dataIndex: 'net_work_seconds',
      width: 150,
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.net_work_seconds - b.net_work_seconds,
      render: (v) => formatDuration(v),
    },
    { title: '厕所', key: 'toilet', width: 160, render: (_, r) => `${r.toilet_count} 次 / ${formatDuration(r.toilet_seconds)}` },
    { title: '抽烟', key: 'smoke', width: 160, render: (_, r) => `${r.smoke_count} 次 / ${formatDuration(r.smoke_seconds)}` },
    { title: '吃饭', key: 'meal', width: 160, render: (_, r) => `${r.meal_count} 次 / ${formatDuration(r.meal_seconds)}` },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>工时统计</Title>

      <Space wrap>
        <span>日期：</span>
        <input
          type="date"
          value={date}
          max={localDateString()}
          onChange={(e) => setDate(e.target.value)}
          style={{ padding: '4px 8px', borderRadius: 6, border: '1px solid #d9d9d9' }}
        />
        <Button onClick={onRefresh} loading={loading}>刷新</Button>
        <span style={{ color: '#999' }}>
          最后刷新：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
        </span>
        <span style={{ color: '#999' }}>（按 UTC 日聚合）</span>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message="加载工时统计失败"
          description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
        />
      )}

      <Row gutter={[16, 16]}>
        <Col flex="1 1 180px">
          <StatCard title="团队净工时合计" value={formatDuration(totals.net)} accent="#4f46e5" />
        </Col>
        <Col flex="1 1 180px">
          <StatCard title="团队在岗合计" value={formatDuration(totals.gross)} accent="#1677ff" />
        </Col>
        <Col flex="1 1 180px">
          <StatCard title="团队 Break 合计" value={formatDuration(totals.brk)} accent="#13c2c2" />
        </Col>
        <Col flex="1 1 180px">
          <StatCard title="统计人数" value={rows.length} suffix="人" accent="#722ed1" />
        </Col>
      </Row>

      <Card title="净工时排行 · Top 10" variant="borderless" style={CARD_STYLE}>
        {topNet.length ? (
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            {topNet.map((r, i) => (
              <div key={r.employee_id} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <span style={{ width: 20, color: '#bfbfbf', fontSize: 12, textAlign: 'right' }}>{i + 1}</span>
                <span style={{ width: 96, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.name}</span>
                <div style={{ flex: 1, height: 16, background: '#f0f0f5', borderRadius: 8, overflow: 'hidden' }}>
                  <div
                    style={{
                      width: `${((r.net_work_seconds || 0) / maxNet) * 100}%`,
                      height: '100%',
                      borderRadius: 8,
                      background: 'linear-gradient(90deg, #6366f1, #4f46e5)',
                      minWidth: r.net_work_seconds ? 4 : 0,
                    }}
                  />
                </div>
                <span style={{ width: 110, textAlign: 'right', color: '#595959', fontVariantNumeric: 'tabular-nums' }}>
                  {formatDuration(r.net_work_seconds)}
                </span>
              </div>
            ))}
          </Space>
        ) : (
          <Empty description="暂无数据" />
        )}
      </Card>

      <Card variant="borderless" styles={{ body: { padding: 16 } }} style={CARD_STYLE}>
        <Table
          rowKey="employee_id"
          columns={columns}
          dataSource={rows}
          loading={loading && data == null}
          size="middle"
          scroll={{ x: 'max-content' }}
          pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
        />
      </Card>
    </Space>
  )
}
