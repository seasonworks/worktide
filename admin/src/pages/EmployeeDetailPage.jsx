import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  Alert,
  App,
  Button,
  Card,
  Col,
  Descriptions,
  Empty,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tag,
  Timeline,
  Typography,
} from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getEmployee, getEmployeeLogs } from '../api/employees.js'
import {
  getEmployeeBreaks,
  getEmployeeShifts,
  getEmployeeShiftStats,
  getWorkStatus,
} from '../api/work.js'
import StatusTag from '../components/StatusTag.jsx'
import WorkStateTag, { BREAK_TYPE_TEXT } from '../components/WorkStateTag.jsx'
import EmployeeWindowActivityCard from '../components/EmployeeWindowActivityCard.jsx'
import AnalyticsErrorBoundary from '../components/AnalyticsErrorBoundary.js'
import { formatDateTime, formatDuration, formatIdle } from '../utils/format.js'

const { Title } = Typography

const LIMIT_OPTIONS = [50, 100, 200, 500].map((n) => ({ value: n, label: `最近 ${n} 条` }))

// 与 StatusTag 一致的颜色映射，供 Timeline 节点使用
const TIMELINE_COLOR = { online: 'green', idle: 'orange', offline: 'gray' }
const STATUS_TEXT = { online: '在线', idle: '挂机', offline: '离线' }
const END_REASON_TEXT = {
  manual_return: '主动回座',
  timeout: '超时',
  shift_end: '下班结束',
  switched: '切换',
  admin_force: '后台强制',
}

const isBreakState = (ws) => typeof ws === 'string' && ws.startsWith('break_')

const fmtLate = (sec) => (sec > 0 ? formatDuration(sec) : '准时')

// 当前/最近班次统计 仪表盘：核心指标卡片 + 时间构成条 + 三类 break
function ShiftStatsPanel({ stats }) {
  const gross = stats.gross_shift_seconds || 0
  const idle = Math.min(stats.idle_seconds_total || 0, gross)
  const brk = stats.break_seconds || 0
  const activeWork = Math.max(0, (stats.net_work_seconds || 0) - idle)
  const denom = gross || 1

  const primary = [
    { label: '净工时', value: formatDuration(stats.net_work_seconds), color: '#4f46e5' },
    { label: '在岗时长', value: formatDuration(gross) },
    { label: 'Break 总时长', value: formatDuration(brk), color: '#8c8c8c' },
    { label: '累计挂机', value: formatDuration(idle), color: idle > 0 ? '#faad14' : undefined },
    {
      label: '迟到',
      value: fmtLate(stats.late_seconds),
      color: stats.late_seconds > 0 ? '#cf1322' : '#52c41a',
    },
  ]
  const segs = [
    { label: '纯工作', sec: activeWork, color: '#52c41a' },
    { label: '挂机', sec: idle, color: '#faad14' },
    { label: '休息', sec: brk, color: '#bfbfbf' },
  ]
  const breaks = [
    { label: '🍚 吃饭', count: stats.meal_count, sec: stats.meal_seconds },
    { label: '🚬 抽烟', count: stats.smoke_count, sec: stats.smoke_seconds },
    { label: '🚻 厕所', count: stats.toilet_count, sec: stats.toilet_seconds },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Row gutter={[16, 16]}>
        {primary.map((p) => (
          <Col key={p.label} flex="1 1 160px">
            <Card size="small" variant="outlined">
              <Statistic
                title={p.label}
                value={p.value}
                valueStyle={{ color: p.color, fontSize: 22 }}
              />
            </Card>
          </Col>
        ))}
      </Row>

      <Card size="small" variant="outlined" title="班次时间构成">
        <div
          style={{
            display: 'flex',
            height: 16,
            borderRadius: 8,
            overflow: 'hidden',
            background: '#f0f0f0',
          }}
        >
          {segs.map(
            (s) =>
              s.sec > 0 && (
                <div
                  key={s.label}
                  style={{ width: `${(s.sec / denom) * 100}%`, background: s.color }}
                  title={`${s.label} ${formatDuration(s.sec)}`}
                />
              ),
          )}
        </div>
        <Space size="large" wrap style={{ marginTop: 12 }}>
          {segs.map((s) => (
            <span key={s.label} style={{ color: '#595959', fontSize: 12 }}>
              <span
                style={{
                  display: 'inline-block',
                  width: 10,
                  height: 10,
                  background: s.color,
                  borderRadius: 2,
                  marginRight: 6,
                }}
              />
              {s.label} · {formatDuration(s.sec)}
            </span>
          ))}
        </Space>
      </Card>

      <Row gutter={[16, 16]}>
        {breaks.map((b) => (
          <Col key={b.label} xs={24} sm={8}>
            <Card size="small" variant="outlined">
              <Statistic title={b.label} value={b.count} suffix="次" />
              <div style={{ color: '#8c8c8c', marginTop: 4 }}>累计 {formatDuration(b.sec)}</div>
            </Card>
          </Col>
        ))}
      </Row>
    </Space>
  )
}

export default function EmployeeDetailPage() {
  const { id } = useParams()
  const empId = Number(id)
  const navigate = useNavigate()
  const { message } = App.useApp()

  const [limit, setLimit] = useState(100)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [, setTick] = useState(0)

  // 合并取数：信息卡 + 历史日志 + 工时状态 + 当前班次统计 + break/shift 历史，一周期一起刷新
  const fetcher = useCallback(
    () =>
      Promise.all([
        getEmployee(id),
        getEmployeeLogs(id, { limit }),
        getWorkStatus(),
        getEmployeeShiftStats(empId),
        getEmployeeBreaks(id, { limit: 50 }),
        getEmployeeShifts(id, { limit: 50 }),
      ]).then(([employee, logs, work, stats, breaks, shifts]) => ({
        employee,
        logs,
        work: (Array.isArray(work) ? work : []).find((w) => w.employee_id === empId) || null,
        stats: stats || null,
        breaks: Array.isArray(breaks) ? breaks : [],
        shifts: Array.isArray(shifts) ? shifts : [],
      })),
    [id, empId, limit],
  )

  const { data, loading, error, refresh } = usePolling(fetcher, {
    interval: 15000,
    immediate: false,
  })

  // 挂载即取 + id/limit 变化立即重取（15s 轮询由 usePolling 负责）
  useEffect(() => {
    refresh().catch(() => {})
  }, [id, limit, refresh])

  useEffect(() => {
    if (data) setLastUpdated(new Date())
  }, [data])

  // 本地每秒走表（单一 interval，卸载清理）
  useEffect(() => {
    const timer = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(timer)
  }, [])

  const employee = data?.employee
  const logs = data?.logs
  const work = data?.work
  const stats = data?.stats
  const breaks = data?.breaks
  const shifts = data?.shifts

  // 走表基准：最近取数时刻
  const baselineMs = lastUpdated ? lastUpdated.getTime() : Date.now()
  const elapsed = Math.max(0, Math.floor((Date.now() - baselineMs) / 1000))
  const inBreak = work && isBreakState(work.work_state)
  const onShift = work && work.work_state && work.work_state !== 'off_shift'
  const liveBreak = inBreak ? formatDuration((work.current_break_seconds || 0) + elapsed) : '—'
  const liveShift = onShift ? formatDuration((work.current_shift_seconds || 0) + elapsed) : '—'

  // Timeline：仅展示状态变更点（折叠连续相同状态），最新在上，最多 50 个
  const timelineItems = useMemo(() => {
    if (!Array.isArray(logs) || logs.length === 0) return []
    const asc = [...logs].sort(
      (a, b) => new Date(a.reported_at).getTime() - new Date(b.reported_at).getTime(),
    )
    const changes = []
    let prev = null
    for (const log of asc) {
      if (log.status !== prev) {
        changes.push(log)
        prev = log.status
      }
    }
    return changes
      .slice(-50)
      .reverse()
      .map((log) => ({
        color: TIMELINE_COLOR[log.status] || 'gray',
        children: (
          <span>
            <span style={{ color: '#999', marginRight: 8 }}>
              {formatDateTime(log.reported_at)}
            </span>
            变为 {STATUS_TEXT[log.status] || log.status}
          </span>
        ),
      }))
  }, [logs])

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  const logColumns = [
    {
      title: '时间',
      dataIndex: 'reported_at',
      width: 180,
      defaultSortOrder: 'descend',
      sorter: (a, b) =>
        new Date(a.reported_at).getTime() - new Date(b.reported_at).getTime(),
      render: (v) => formatDateTime(v),
    },
    { title: '状态', dataIndex: 'status', width: 90, render: (status) => <StatusTag status={status} /> },
    { title: '空闲时长', dataIndex: 'idle_seconds', width: 120, render: (v) => formatIdle(v) },
    {
      title: '活动',
      dataIndex: 'is_active',
      width: 100,
      render: (active) => (active ? <Tag color="blue">有活动</Tag> : <Tag>无活动</Tag>),
    },
  ]

  const breakColumns = [
    {
      title: '类型',
      dataIndex: 'break_type',
      width: 80,
      render: (t) => BREAK_TYPE_TEXT[t] || t,
    },
    { title: '开始', dataIndex: 'started_at', width: 180, render: (v) => formatDateTime(v) },
    {
      title: '结束',
      dataIndex: 'ended_at',
      width: 180,
      render: (v) => (v ? formatDateTime(v) : <Tag color="processing">进行中</Tag>),
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      width: 130,
      render: (v, r) => (r.ended_at ? formatDuration(v) : '—'),
    },
    {
      title: '结束原因',
      dataIndex: 'end_reason',
      width: 100,
      render: (v) => (v ? END_REASON_TEXT[v] || v : '-'),
    },
    {
      title: '超时',
      dataIndex: 'auto_ended',
      width: 70,
      render: (v) => (v ? <Tag color="red">超时</Tag> : '-'),
    },
  ]

  const shiftColumns = [
    { title: '开始', dataIndex: 'started_at', width: 180, render: (v) => formatDateTime(v) },
    {
      title: '结束',
      dataIndex: 'ended_at',
      width: 180,
      render: (v) => (v ? formatDateTime(v) : <Tag color="processing">进行中</Tag>),
    },
    {
      title: '时长',
      dataIndex: 'duration_seconds',
      width: 130,
      render: (v, r) => (r.ended_at ? formatDuration(v) : '—'),
    },
    {
      title: '结束原因',
      dataIndex: 'end_reason',
      width: 120,
      render: (v) => v || '-',
    },
  ]

  const notFound = error?.response?.status === 404

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space>
        <Button onClick={() => navigate('/employees')}>← 返回员工列表</Button>
        <Title level={4} style={{ margin: 0 }}>员工详情</Title>
      </Space>

      {error &&
        (notFound ? (
          <Alert type="warning" showIcon message="该员工不存在" />
        ) : (
          <Alert
            type="error"
            showIcon
            message="加载员工详情失败"
            description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
          />
        ))}

      {/* 首屏加载 */}
      {data == null && loading && (
        <div style={{ textAlign: 'center', padding: 48 }}>
          <Spin />
        </div>
      )}

      {employee && (
        <>
          <Space wrap>
            <Select value={limit} onChange={setLimit} options={LIMIT_OPTIONS} style={{ width: 130 }} />
            <Button onClick={onRefresh} loading={loading}>刷新</Button>
            <span style={{ color: '#999' }}>
              最后刷新：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
            </span>
          </Space>

          <Descriptions title={`员工：${employee.name}`} bordered column={2} size="small">
            <Descriptions.Item label="活动状态">
              <StatusTag status={employee.status} />
            </Descriptions.Item>
            <Descriptions.Item label="工时状态">
              <WorkStateTag workState={work?.work_state || 'off_shift'} />
            </Descriptions.Item>
            <Descriptions.Item label="当前 Break">
              {inBreak ? BREAK_TYPE_TEXT[work.break_type] || work.break_type : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="Break 已持续">{liveBreak}</Descriptions.Item>
            <Descriptions.Item label="在岗已持续">{liveShift}</Descriptions.Item>
            <Descriptions.Item label="当前空闲">{formatIdle(employee.idle_seconds)}</Descriptions.Item>
            <Descriptions.Item label="主机名">{employee.hostname || '-'}</Descriptions.Item>
            <Descriptions.Item label="最近上报">{formatDateTime(employee.last_seen)}</Descriptions.Item>
            <Descriptions.Item label="机器标识" span={2}>{employee.machine_id}</Descriptions.Item>
            <Descriptions.Item label="首次注册" span={2}>{formatDateTime(employee.created_at)}</Descriptions.Item>
          </Descriptions>

          <div>
            <Space align="center" wrap style={{ marginBottom: 12 }}>
              <Title level={5} style={{ margin: 0 }}>当前班次统计</Title>
              {stats &&
                (stats.is_current ? (
                  <Tag color="green">进行中</Tag>
                ) : (
                  <Tag color="default">上次班次</Tag>
                ))}
              {stats && (
                <span style={{ color: '#999' }}>
                  {formatDateTime(stats.shift_started_at)} ~{' '}
                  {stats.shift_ended_at ? formatDateTime(stats.shift_ended_at) : '至今'}
                </span>
              )}
            </Space>
            {stats ? (
              <ShiftStatsPanel stats={stats} />
            ) : (
              <Empty description="暂无班次记录" />
            )}
          </div>

          {/* Phase 4.3 · 5.3 + 5.4：今日窗口活动 Card，独立 ErrorBoundary 失败面，
              不影响 Break / Shift / Timeline / 历史记录 等区块 */}
          <AnalyticsErrorBoundary label="窗口分析模块">
            <EmployeeWindowActivityCard employeeId={empId} />
          </AnalyticsErrorBoundary>

          <div>
            <Title level={5}>Break 历史（最近 50 条）</Title>
            <Table
              rowKey="id"
              columns={breakColumns}
              dataSource={Array.isArray(breaks) ? breaks : []}
              size="small"
              scroll={{ x: 'max-content' }}
              pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
            />
          </div>

          <div>
            <Title level={5}>Shift 历史（最近 50 条）</Title>
            <Table
              rowKey="id"
              columns={shiftColumns}
              dataSource={Array.isArray(shifts) ? shifts : []}
              size="small"
              scroll={{ x: 'max-content' }}
              pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
            />
          </div>

          <div>
            <Title level={5}>状态时间轴（仅 online / idle）</Title>
            {timelineItems.length > 0 ? (
              <Timeline items={timelineItems} />
            ) : (
              <Empty description="暂无记录" />
            )}
          </div>

          <div>
            <Title level={5}>历史记录</Title>
            <Table
              rowKey="id"
              columns={logColumns}
              dataSource={Array.isArray(logs) ? logs : []}
              size="middle"
              pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
            />
          </div>
        </>
      )}
    </Space>
  )
}
