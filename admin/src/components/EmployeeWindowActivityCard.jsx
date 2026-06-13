import { useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Col,
  Empty,
  Progress,
  Row,
  Space,
  Statistic,
  Table,
  Tooltip,
  Typography,
} from 'antd'
import useDailyWindowStats from '../hooks/useDailyWindowStats.js'
import useEmployeeWindowSessions from '../hooks/useEmployeeWindowSessions.js'
import WorkStateTag from './WorkStateTag.jsx'
import { formatDateTime, formatDuration, localDateString } from '../utils/format.js'

const { Title, Text } = Typography

const TITLE_DISPLAY_MAX = 60
const SESSIONS_DEFAULT_LIMIT = 25
const SESSIONS_EXPANDED_LIMIT = 100

/**
 * 把超过 TITLE_DISPLAY_MAX 字符的标题截断 + 末尾省略号，hover 看完整。
 *
 * 注意：服务端 window_sessions.window_title 已截断到 120；前端再 ellipsis 到 60
 * 只为视觉密度。
 */
function truncateTitle(raw) {
  const s = typeof raw === 'string' ? raw : ''
  if (s.length <= TITLE_DISPLAY_MAX) return s
  return s.slice(0, TITLE_DISPLAY_MAX) + '…'
}

/**
 * Employee Detail 的「今日窗口活动」Card。
 *
 * 自带两条独立 polling 轨：
 *  - useDailyWindowStats(today)：取当员工的 totals + top_apps（top_n=10）
 *  - useEmployeeWindowSessions(empId, today, limit)：取 Recent Sessions
 *
 * 设计要点：
 *  - 自己处理 loading / error / 空态，**不向父页面冒泡**：5.4 将外包一层 ErrorBoundary
 *    后，本块失败也不会带垮员工详情页其它区块。
 *  - 默认 include_inactive=false 不影响这里：详情页是"明确指定 id"语境，离职员工历史
 *    必须能看；好在 /windows/employees/{id} 服务端**不过滤 is_active**（设计如此），
 *    所以本组件无需任何额外回退逻辑。totals/top_apps 拿不到就让 KPI 显示「—」。
 */
export default function EmployeeWindowActivityCard({ employeeId }) {
  const today = localDateString()
  const [limit, setLimit] = useState(SESSIONS_DEFAULT_LIMIT)

  // 1) 当日全员 stats（topN=10），抽出本员工那一行
  const {
    data: statsList,
    loading: statsLoading,
    error: statsError,
  } = useDailyWindowStats({
    date: today,
    includeInactive: true,    // 详情页语境：archived 员工 totals 也要能查到
    topN: 10,
    interval: 30000,
    enabled: Boolean(employeeId),
  })

  const myStats = useMemo(() => {
    if (!Array.isArray(statsList)) return null
    return statsList.find((r) => r.employee_id === employeeId) || null
  }, [statsList, employeeId])

  // Top Apps：按设计 5.2 同款排序——working_seconds DESC（不是 total）
  const topApps = useMemo(() => {
    const apps = myStats?.top_apps
    if (!Array.isArray(apps)) return []
    const sorted = [...apps].sort((a, b) => {
      if (b.working_seconds !== a.working_seconds) {
        return b.working_seconds - a.working_seconds
      }
      return a.process_name.localeCompare(b.process_name)
    })
    return sorted.slice(0, 10)
  }, [myStats])

  const topWorkingMax = topApps[0]?.working_seconds || 1

  // 2) 当员工 Recent Sessions（服务端已 started_at DESC）
  const {
    data: sessions,
    loading: sessionsLoading,
    error: sessionsError,
  } = useEmployeeWindowSessions(employeeId, {
    date: today,
    limit,
    interval: 30000,
    enabled: Boolean(employeeId),
  })

  const sessionRows = Array.isArray(sessions) ? sessions : []

  // ───────── 列定义 · Top Apps ─────────
  const topAppsColumns = [
    {
      title: '#',
      key: 'rank',
      width: 50,
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
            style={{ minWidth: 100 }}
          />
        </Space>
      ),
    },
    {
      title: '休息',
      dataIndex: 'break_seconds',
      width: 120,
      sorter: (a, b) => a.break_seconds - b.break_seconds,
      render: (v) => formatDuration(v),
    },
    {
      title: '离岗',
      dataIndex: 'off_shift_seconds',
      width: 120,
      sorter: (a, b) => a.off_shift_seconds - b.off_shift_seconds,
      render: (v) => formatDuration(v),
    },
  ]

  // ───────── 列定义 · Recent Sessions ─────────
  const sessionColumns = [
    {
      title: '工时状态',
      dataIndex: 'work_state',
      width: 110,
      render: (v) => <WorkStateTag workState={v || 'off_shift'} />,
    },
    {
      title: '进程',
      dataIndex: 'process_name',
      width: 160,
      ellipsis: true,
    },
    {
      title: '窗口标题',
      dataIndex: 'window_title',
      ellipsis: true,  // antd 自带列截断 + tooltip 已能 cover 一部分，但中文环境下我们手动加 tooltip 更稳
      render: (v) => (
        <Tooltip title={v || ''}>
          <span>{truncateTitle(v)}</span>
        </Tooltip>
      ),
    },
    {
      title: '开始时间',
      dataIndex: 'started_at',
      width: 180,
      defaultSortOrder: 'descend',
      sorter: (a, b) =>
        new Date(a.started_at).getTime() - new Date(b.started_at).getTime(),
      render: (v) => formatDateTime(v),
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      width: 130,
      render: (v) => formatDuration(v),
    },
  ]

  // ───────── 顶部 KPI 三桶 ─────────
  const kpis = (
    <Row gutter={32}>
      <Col>
        <Statistic
          title="Working"
          value={myStats ? formatDuration(myStats.total_working_seconds) : '—'}
        />
      </Col>
      <Col>
        <Statistic
          title="Break"
          value={myStats ? formatDuration(myStats.total_break_seconds) : '—'}
        />
      </Col>
      <Col>
        <Statistic
          title="Off Shift"
          value={myStats ? formatDuration(myStats.total_off_shift_seconds) : '—'}
        />
      </Col>
    </Row>
  )

  // ───────── 空态（统一文案，按设计稿）─────────
  // 触发条件：stats 已加载完成（loading=false）AND 取不到本员工的行 AND 没有 session 数据
  const isEmpty =
    !statsLoading &&
    !sessionsLoading &&
    !myStats &&
    sessionRows.length === 0

  if (isEmpty) {
    return (
      <div>
        <Title level={5}>今日窗口活动（{today}，UTC 日）</Title>
        <Empty description="今日还未采集到窗口活动（客户端可能未启用 window.enabled）" />
      </div>
    )
  }

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={5} style={{ margin: 0 }}>
        今日窗口活动（{today}，UTC 日）
      </Title>

      {(statsError || sessionsError) && (
        <Alert
          type="warning"
          showIcon
          message="窗口活动数据部分加载失败"
          description="本块下次轮询会自动重试，不影响员工详情其它区块。"
        />
      )}

      {kpis}

      <div>
        <Text strong>Top Apps（按在岗时长排序 · 前 10）</Text>
        <Table
          rowKey="process_name"
          columns={topAppsColumns}
          dataSource={topApps}
          loading={statsLoading && !myStats}
          pagination={false}
          size="small"
          scroll={{ x: 'max-content' }}
          locale={{ emptyText: '本日无 Top Apps 数据' }}
          style={{ marginTop: 8 }}
        />
      </div>

      <div>
        <Space align="center" style={{ marginBottom: 8 }}>
          <Text strong>Recent Sessions（最近 {limit} 条 · 时间倒序）</Text>
          {limit === SESSIONS_DEFAULT_LIMIT && sessionRows.length >= SESSIONS_DEFAULT_LIMIT && (
            <Button
              type="link"
              size="small"
              onClick={() => setLimit(SESSIONS_EXPANDED_LIMIT)}
            >
              查看更多 ↓
            </Button>
          )}
          {limit === SESSIONS_EXPANDED_LIMIT && (
            <Button
              type="link"
              size="small"
              onClick={() => setLimit(SESSIONS_DEFAULT_LIMIT)}
            >
              收起
            </Button>
          )}
        </Space>
        <Table
          rowKey="id"
          columns={sessionColumns}
          dataSource={sessionRows}
          loading={sessionsLoading && sessionRows.length === 0}
          size="small"
          scroll={{ x: 'max-content' }}
          pagination={{
            pageSize: 25,
            showTotal: (t) => `共 ${t} 条（上限 ${limit}）`,
          }}
          locale={{ emptyText: '本日暂无窗口会话' }}
        />
      </div>
    </Space>
  )
}
