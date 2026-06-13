import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Alert, App, Button, Card, Col, Input, Row, Select, Space, Table, Typography } from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getEmployees } from '../api/employees.js'
import { getWorkStatus } from '../api/work.js'
import StatusTag from '../components/StatusTag.jsx'
import StatCard from '../components/StatCard.jsx'
import WorkStateTag, { BREAK_TYPE_TEXT } from '../components/WorkStateTag.jsx'
import { formatDateTime, formatDuration, formatIdle } from '../utils/format.js'

const { Title } = Typography

const STATUS_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: 'online', label: '在线' },
  { value: 'idle', label: '挂机' },
  { value: 'offline', label: '离线' },
]

const isBreakState = (ws) => typeof ws === 'string' && ws.startsWith('break_')

// 合并取数：活动状态来自 /employees，工时状态来自 /work/status
const fetchEmployeesWithWork = () =>
  Promise.all([getEmployees(), getWorkStatus()]).then(([employees, work]) => ({
    employees,
    work,
  }))

export default function EmployeesPage() {
  const { message } = App.useApp()
  // 15s 轮询提供基准；本地 1s 走表负责平滑（见下）
  const { data, loading, error, refresh } = usePolling(fetchEmployeesWithWork, {
    interval: 15000,
  })

  const [searchText, setSearchText] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')
  const [lastUpdated, setLastUpdated] = useState(null)
  const [, setTick] = useState(0)

  // 每次成功取数后记录刷新时间（同时作为走表基准时刻）
  useEffect(() => {
    if (data) setLastUpdated(new Date())
  }, [data])

  // 本地每秒走表：仅触发重渲染，时长在 render 时按「服务端基准秒 + 已过秒」计算。
  // 单一 interval，卸载时清理；切换路由组件卸载即停止，不会累积。
  useEffect(() => {
    const timer = setInterval(() => setTick((x) => x + 1), 1000)
    return () => clearInterval(timer)
  }, [])

  // 合并行：以 /employees 为基准（保留 machine_id/idle/last_seen），挂上 /work/status 字段
  const rows = useMemo(() => {
    const employees = Array.isArray(data?.employees) ? data.employees : []
    const workMap = new Map(
      (Array.isArray(data?.work) ? data.work : []).map((w) => [w.employee_id, w]),
    )
    return employees.map((e) => ({ ...e, _work: workMap.get(e.id) || null }))
  }, [data])

  // 纯前端过滤：搜索 name/machine_id/hostname + 活动状态筛选
  const filtered = useMemo(() => {
    const kw = searchText.trim().toLowerCase()
    return rows.filter((e) => {
      if (statusFilter !== 'all' && e.status !== statusFilter) return false
      if (!kw) return true
      return [e.name, e.machine_id, e.hostname].some((v) =>
        String(v ?? '').toLowerCase().includes(kw),
      )
    })
  }, [rows, searchText, statusFilter])

  // KPI 概览：活动状态分布 + 在岗/休息计数
  const kpi = useMemo(() => {
    const c = { total: rows.length, online: 0, idle: 0, offline: 0, working: 0, onbreak: 0 }
    for (const e of rows) {
      if (e.status === 'online') c.online += 1
      else if (e.status === 'idle') c.idle += 1
      else if (e.status === 'offline') c.offline += 1
      const ws = e._work?.work_state
      if (ws === 'working') c.working += 1
      else if (isBreakState(ws)) c.onbreak += 1
    }
    return c
  }, [rows])

  const toggleStatus = (s) => setStatusFilter((cur) => (cur === s ? 'all' : s))

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  // 走表：基准时刻 = 最近取数时刻；已过秒数随 1s tick 增长，轮询到点重置基准自动校准
  const baselineMs = lastUpdated ? lastUpdated.getTime() : Date.now()
  const elapsed = Math.max(0, Math.floor((Date.now() - baselineMs) / 1000))
  // 仅对当前确实处于该状态的员工走表；状态结束后下次取数即归位、计时停止
  const liveBreak = (w) =>
    w && isBreakState(w.work_state)
      ? formatDuration((w.current_break_seconds || 0) + elapsed)
      : '—'
  const liveShift = (w) =>
    w && w.work_state && w.work_state !== 'off_shift'
      ? formatDuration((w.current_shift_seconds || 0) + elapsed)
      : '—'

  const columns = [
    { title: '员工名', dataIndex: 'name', ellipsis: true },
    { title: '机器标识', dataIndex: 'machine_id', ellipsis: true, width: 240 },
    { title: '主机名', dataIndex: 'hostname', ellipsis: true, width: 120 },
    {
      title: '活动状态',
      dataIndex: 'status',
      width: 90,
      render: (status) => <StatusTag status={status} />,
    },
    {
      title: '工时状态',
      key: 'work_state',
      width: 90,
      render: (_, r) => <WorkStateTag workState={r._work?.work_state || 'off_shift'} />,
    },
    {
      title: '当前Break',
      key: 'break_type',
      width: 90,
      render: (_, r) =>
        r._work && isBreakState(r._work.work_state)
          ? BREAK_TYPE_TEXT[r._work.break_type] || r._work.break_type
          : '—',
    },
    { title: 'Break时长', key: 'break_seconds', width: 130, render: (_, r) => liveBreak(r._work) },
    { title: '在岗时长', key: 'shift_seconds', width: 130, render: (_, r) => liveShift(r._work) },
    {
      title: '空闲时长',
      dataIndex: 'idle_seconds',
      width: 110,
      sorter: (a, b) => (a.idle_seconds ?? 0) - (b.idle_seconds ?? 0),
      render: (v) => formatIdle(v),
    },
    {
      title: '最近上报',
      dataIndex: 'last_seen',
      width: 170,
      sorter: (a, b) => new Date(a.last_seen ?? 0) - new Date(b.last_seen ?? 0),
      render: (v) => formatDateTime(v),
    },
    {
      title: '操作',
      key: 'action',
      width: 70,
      render: (_, record) => <Link to={`/employees/${record.id}`}>详情</Link>,
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>在线员工</Title>

      <Row gutter={[16, 16]}>
        <Col flex="1 1 150px">
          <StatCard title="员工总数" value={kpi.total} accent="#4f46e5" />
        </Col>
        <Col flex="1 1 150px">
          <StatCard title="在线" value={kpi.online} accent="#52c41a"
            onClick={() => toggleStatus('online')} active={statusFilter === 'online'} />
        </Col>
        <Col flex="1 1 150px">
          <StatCard title="挂机" value={kpi.idle} accent="#faad14"
            onClick={() => toggleStatus('idle')} active={statusFilter === 'idle'} />
        </Col>
        <Col flex="1 1 150px">
          <StatCard title="离线" value={kpi.offline} accent="#8c8c8c"
            onClick={() => toggleStatus('offline')} active={statusFilter === 'offline'} />
        </Col>
        <Col flex="1 1 150px">
          <StatCard title="在岗中" value={kpi.working} accent="#1677ff" />
        </Col>
        <Col flex="1 1 150px">
          <StatCard title="休息中" value={kpi.onbreak} accent="#13c2c2" />
        </Col>
      </Row>

      <Card variant="borderless" styles={{ body: { padding: 16 } }}
        style={{ borderRadius: 14, boxShadow: '0 1px 2px rgba(0,0,0,0.04), 0 6px 16px rgba(0,0,0,0.05)' }}>
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Space wrap>
            <Input
              placeholder="搜索 员工名 / 主机名 / 机器标识"
              allowClear
              value={searchText}
              onChange={(e) => setSearchText(e.target.value)}
              style={{ width: 260 }}
            />
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              options={STATUS_OPTIONS}
              style={{ width: 120 }}
            />
            <Button onClick={onRefresh} loading={loading}>刷新</Button>
            <span style={{ color: '#999' }}>
              最后刷新：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
            </span>
          </Space>

          {error && (
            <Alert
              type="error"
              showIcon
              message="加载员工列表失败"
              description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
            />
          )}

          <Table
            rowKey="id"
            columns={columns}
            dataSource={filtered}
            loading={loading && data == null}
            size="middle"
            scroll={{ x: 'max-content' }}
            pagination={{ defaultPageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
          />
        </Space>
      </Card>
    </Space>
  )
}
