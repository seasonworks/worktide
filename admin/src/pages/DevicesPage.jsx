import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Alert, App, Button, Input, Segmented, Space, Table, Typography,
} from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getDevices } from '../api/devices.js'
import HealthStatusBadge from '../components/HealthStatusBadge.jsx'
import UpdateStatusBadge from '../components/UpdateStatusBadge.jsx'
import { formatDateTime, formatDuration } from '../utils/format.js'

const { Title } = Typography

const STATUS_FILTERS = [
  { value: 'all',      label: '全部' },
  { value: 'healthy',  label: '健康' },
  { value: 'degraded', label: '退化' },
  { value: 'unstable', label: '不稳定' },
  { value: 'stale',    label: '停滞' },
  { value: 'offline',  label: '离线' },
]

const fetchDevices = () => getDevices()

export default function DevicesPage() {
  const { message } = App.useApp()
  const { data, loading, error, refresh } = usePolling(fetchDevices, {
    interval: 15000,
  })

  const [searchText, setSearchText] = useState('')
  const [statusFilter, setStatusFilter] = useState('all')

  const rows = useMemo(() => (Array.isArray(data) ? data : []), [data])

  const filtered = useMemo(() => {
    const kw = searchText.trim().toLowerCase()
    return rows.filter((d) => {
      if (statusFilter !== 'all' && d.status !== statusFilter) return false
      if (!kw) return true
      return [d.employee_name, d.hostname, d.machine_id, d.agent_version].some(
        (v) => String(v ?? '').toLowerCase().includes(kw),
      )
    })
  }, [rows, searchText, statusFilter])

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  const columns = [
    { title: '员工', dataIndex: 'employee_name', ellipsis: true, width: 120,
      render: (v) => v || <span style={{ color: '#999' }}>未注册</span> },
    { title: '主机', dataIndex: 'hostname', ellipsis: true, width: 120 },
    { title: '版本', dataIndex: 'agent_version', width: 80 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (s) => <HealthStatusBadge status={s} />,
      sorter: (a, b) => String(a.status).localeCompare(String(b.status)),
    },
    {
      title: '最近上报',
      dataIndex: 'last_seen',
      width: 170,
      sorter: (a, b) => new Date(a.last_seen ?? 0) - new Date(b.last_seen ?? 0),
      defaultSortOrder: 'descend',
      render: (v) => formatDateTime(v),
    },
    {
      title: '运行时长',
      dataIndex: 'uptime_seconds',
      width: 110,
      sorter: (a, b) => (a.uptime_seconds ?? 0) - (b.uptime_seconds ?? 0),
      render: (v) => (v ? formatDuration(v) : '—'),
    },
    {
      title: '重启次数',
      dataIndex: 'restart_count',
      width: 90,
      sorter: (a, b) => (a.restart_count ?? 0) - (b.restart_count ?? 0),
    },
    {
      title: '上次退出原因',
      dataIndex: 'last_exit_reason',
      width: 200,
      ellipsis: true,
      render: (v) => v || '—',
    },
    {
      title: '待上报',
      dataIndex: 'pending_events',
      width: 80,
      sorter: (a, b) => (a.pending_events ?? 0) - (b.pending_events ?? 0),
      // > 1000 显眼标黄；纯展示，不进 status 判定
      render: (v) => (
        <span style={{ color: (v ?? 0) > 1000 ? '#d48806' : undefined }}>
          {v ?? 0}
        </span>
      ),
    },
    {
      // Phase 5.4 · 升级状态列；idle 时不显示徽章保持列简洁
      title: '更新',
      dataIndex: 'update_status',
      width: 100,
      render: (status, r) => {
        const tag = <UpdateStatusBadge status={status} hideIdle />
        if (!tag) return <span style={{ color: '#bbb' }}>—</span>
        // 显示目标版本，让管理员一眼知道在升到哪个版本
        return (
          <span>
            {tag}
            {r.update_target_version && (
              <span style={{ marginLeft: 4, color: '#999', fontSize: 12 }}>
                →{r.update_target_version}
              </span>
            )}
          </span>
        )
      },
    },
    {
      title: '机器标识',
      dataIndex: 'machine_id',
      ellipsis: true,
      width: 220,
    },
    {
      title: '操作',
      key: 'action',
      width: 70,
      render: (_, r) =>
        r.employee_id ? (
          <Link to={`/devices/${r.employee_id}`}>详情</Link>
        ) : (
          <span style={{ color: '#999' }}>—</span>
        ),
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>设备健康</Title>

      <Space wrap>
        <Input
          placeholder="搜索 员工 / 主机 / machine_id / 版本"
          allowClear
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          style={{ width: 320 }}
        />
        <Segmented
          options={STATUS_FILTERS}
          value={statusFilter}
          onChange={setStatusFilter}
        />
        <Button onClick={onRefresh} loading={loading}>刷新</Button>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message="加载设备列表失败"
          description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
        />
      )}

      <Table
        rowKey="machine_id"
        columns={columns}
        dataSource={filtered}
        loading={loading && data == null}
        size="middle"
        scroll={{ x: 'max-content' }}
        pagination={{
          defaultPageSize: 10,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
        }}
      />
    </Space>
  )
}
