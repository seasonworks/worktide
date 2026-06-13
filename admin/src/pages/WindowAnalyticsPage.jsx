import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Col,
  Progress,
  Row,
  Segmented,
  Space,
  Statistic,
  Table,
  Tag,
  Typography,
} from 'antd'
import useDailyWindowStats from '../hooks/useDailyWindowStats.js'
import AnalyticsErrorBoundary from '../components/AnalyticsErrorBoundary.js'
import { formatDuration, localDateString } from '../utils/format.js'
import {
  aggregateTeamTotals,
  aggregateTopApplications,
} from '../utils/windowAggregation.js'

const { Title, Text } = Typography

// ─────────────────────────────────────────────────────────────────────────────
// 日期 chip：只允许"今日 / 昨日"（按设计禁止 DatePicker，避免诱导超出 retention 的查询）
// 注意：服务端按 UTC 日聚合；前端用 localDateString 与现有 WorkStatsPage 保持一致，
// 顶栏会提示"按 UTC 日聚合"以消除歧义。
// ─────────────────────────────────────────────────────────────────────────────

const TODAY = '今日'
const YESTERDAY = '昨日'

// 员工范围 chip。注意：**不持久化**到 localStorage / URL，
// 切回页面 / F5 刷新都回到默认"在职"。
const SCOPE_ACTIVE = '在职'
const SCOPE_ALL = '全部（含离职）'

function resolveDate(chip) {
  if (chip === YESTERDAY) {
    const d = new Date(Date.now() - 24 * 60 * 60 * 1000)
    return localDateString(d)
  }
  return localDateString()
}

// ─────────────────────────────────────────────────────────────────────────────
// 页面
// ─────────────────────────────────────────────────────────────────────────────

export default function WindowAnalyticsPage() {
  const { message } = App.useApp()
  const [chip, setChip] = useState(TODAY)
  const [scope, setScope] = useState(SCOPE_ACTIVE)
  const [lastUpdated, setLastUpdated] = useState(null)

  const date = useMemo(() => resolveDate(chip), [chip])
  const includeInactive = scope === SCOPE_ALL

  // 唯一数据源：5.1 已实现的 hook；本页只走它一条线，不新增请求
  const { data, loading, error, refresh } = useDailyWindowStats({
    date,
    includeInactive,
    topN: 20,
    interval: 30000,
  })

  // 修复 5.2 遗留：fetcher 引用变了（date / includeInactive 改），但 usePolling 不
  // 会立即重拉（refresh 是稳定回调）。这里**显式触发**一次 refresh，使切换立即生效。
  useEffect(() => {
    refresh().catch(() => {})
  }, [date, includeInactive, refresh])

  useEffect(() => {
    if (data) setLastUpdated(new Date())
  }, [data])

  const rows = Array.isArray(data) ? data : []

  // 团队合计 KPI
  const totals = useMemo(() => aggregateTeamTotals(rows), [rows])

  // Top Applications：前端 reduce，working_seconds DESC，取前 20
  const topApps = useMemo(() => aggregateTopApplications(rows, 20), [rows])

  // Progress 用最高一项的 working 做基准（避免低 working 的 app 进度条难看清）
  const topWorkingMax = topApps[0]?.working_seconds || 1

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  // ───────── 表格列 · Top Applications ─────────
  const topAppsColumns = [
    {
      title: '#',
      key: 'rank',
      width: 56,
      render: (_, __, idx) => <Text type="secondary">{idx + 1}</Text>,
    },
    {
      title: '进程',
      dataIndex: 'process_name',
      ellipsis: true,
    },
    {
      title: '在岗时长',
      dataIndex: 'working_seconds',
      width: 280,
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.working_seconds - b.working_seconds,
      render: (v) => (
        <Space size="small" style={{ width: '100%' }}>
          <span style={{ minWidth: 110, display: 'inline-block' }}>
            {formatDuration(v)}
          </span>
          <Progress
            percent={Math.round((v / topWorkingMax) * 100)}
            showInfo={false}
            size="small"
            style={{ minWidth: 120 }}
          />
        </Space>
      ),
    },
    {
      title: '休息时长',
      dataIndex: 'break_seconds',
      width: 130,
      sorter: (a, b) => a.break_seconds - b.break_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: '离岗时长',
      dataIndex: 'off_shift_seconds',
      width: 130,
      sorter: (a, b) => a.off_shift_seconds - b.off_shift_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: '合计',
      dataIndex: 'total_seconds',
      width: 130,
      sorter: (a, b) => a.total_seconds - b.total_seconds,
      render: (v) => formatDuration(v),
    },
  ]

  // ───────── 表格列 · Daily Usage by Employee ─────────
  const renderTopApp = (idx) => (_, r) => {
    const app = r?.top_apps?.[idx]
    if (!app) return <Text type="secondary">—</Text>
    return (
      <Space direction="vertical" size={0}>
        <Text>{app.process_name}</Text>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {formatDuration(app.working_seconds || 0)}
        </Text>
      </Space>
    )
  }

  const dailyUsageColumns = [
    { title: '员工', dataIndex: 'name', ellipsis: true, fixed: 'left', width: 160 },
    {
      title: '在岗',
      dataIndex: 'total_working_seconds',
      width: 140,
      defaultSortOrder: 'descend',
      sorter: (a, b) => a.total_working_seconds - b.total_working_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: 'Break',
      dataIndex: 'total_break_seconds',
      width: 140,
      sorter: (a, b) => a.total_break_seconds - b.total_break_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: '离岗',
      dataIndex: 'total_off_shift_seconds',
      width: 140,
      sorter: (a, b) =>
        a.total_off_shift_seconds - b.total_off_shift_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: 'Top App #1（在岗最长）',
      key: 'top_app_1',
      width: 200,
      render: renderTopApp(0),
    },
    {
      title: 'Top App #2',
      key: 'top_app_2',
      width: 200,
      render: renderTopApp(1),
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>
        窗口分析 · 每日
      </Title>

      {/* 控制条：日期 chip + 员工范围 chip + 刷新 + 时间戳 + UTC 提示 */}
      <Space wrap align="center">
        <Segmented
          options={[TODAY, YESTERDAY]}
          value={chip}
          onChange={(v) => setChip(v)}
        />
        <Segmented
          options={[SCOPE_ACTIVE, SCOPE_ALL]}
          value={scope}
          onChange={(v) => setScope(v)}
        />
        <Button onClick={onRefresh} loading={loading}>
          刷新
        </Button>
        <Text type="secondary">
          刷新于：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'} · auto 30s
        </Text>
        <Tag>查询日（UTC）：{date}</Tag>
        {includeInactive && (
          <Tag color="orange">已包含离职员工历史</Tag>
        )}
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message="加载窗口活动统计失败"
          description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
        />
      )}

      {/* 团队合计 KPI —— 包 ErrorBoundary（独立失败面） */}
      <AnalyticsErrorBoundary label="团队合计 KPI">
        <Row gutter={32}>
          <Col>
            <Statistic
              title="团队在岗合计"
              value={formatDuration(totals.working)}
            />
          </Col>
          <Col>
            <Statistic
              title="团队 Break 合计"
              value={formatDuration(totals.breaks)}
            />
          </Col>
          <Col>
            <Statistic
              title="团队离岗合计"
              value={formatDuration(totals.off)}
            />
          </Col>
          <Col>
            <Statistic
              title={includeInactive ? '员工数（含离职）' : '员工数（在职）'}
              value={rows.length}
            />
          </Col>
        </Row>
      </AnalyticsErrorBoundary>

      {/* Top Applications（全员前 20，working_seconds DESC）—— 独立 ErrorBoundary */}
      <AnalyticsErrorBoundary label="Top Applications">
        <div>
          <Title level={5} style={{ margin: '0 0 8px' }}>
            Top Applications（全员合并 · 按在岗时长排序 · 前 20）
          </Title>
          <Table
            rowKey="process_name"
            size="middle"
            columns={topAppsColumns}
            dataSource={topApps}
            loading={loading && data == null}
            pagination={false}
            scroll={{ x: 'max-content' }}
            locale={{ emptyText: '本日无窗口活动数据' }}
          />
        </div>
      </AnalyticsErrorBoundary>

      {/* Daily Usage by Employee —— 独立 ErrorBoundary */}
      <AnalyticsErrorBoundary label="Daily Usage">
        <div>
          <Title level={5} style={{ margin: '0 0 8px' }}>
            Daily Usage by Employee
          </Title>
          <Table
            rowKey="employee_id"
            size="middle"
            columns={dailyUsageColumns}
            dataSource={rows}
            loading={loading && data == null}
            scroll={{ x: 'max-content' }}
            pagination={{ pageSize: 50, showTotal: (t) => `共 ${t} 名员工` }}
            locale={{
              emptyText: includeInactive
                ? '本日无员工窗口数据（已含离职）'
                : '本日无在职员工窗口数据',
            }}
          />
        </div>
      </AnalyticsErrorBoundary>
    </Space>
  )
}
