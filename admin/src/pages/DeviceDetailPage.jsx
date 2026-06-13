import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Col, Descriptions, Empty, Row, Space, Table, Typography,
} from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getDevice } from '../api/devices.js'
import HealthStatusBadge from '../components/HealthStatusBadge.jsx'
import UpdateStatusBadge from '../components/UpdateStatusBadge.jsx'
import { formatDateTime, formatDuration } from '../utils/format.js'

const { Title, Text } = Typography

export default function DeviceDetailPage() {
  const { id } = useParams()
  const employeeId = Number(id)
  const { message } = App.useApp()

  const fetcher = useMemo(() => () => getDevice(employeeId), [employeeId])
  const { data, loading, error, refresh } = usePolling(fetcher, {
    interval: 15000,
  })

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  if (!employeeId) {
    return <Alert type="warning" message="无效的 employee_id" showIcon />
  }

  if (error) {
    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space>
          <Link to="/devices">← 返回设备列表</Link>
          <Button onClick={onRefresh} loading={loading}>刷新</Button>
        </Space>
        <Alert
          type="error"
          showIcon
          message="加载设备详情失败"
          description={
            error?.response?.status === 404
              ? '该员工还未上报过 device health（agent 可能未启动或版本不含 5.3）'
              : '将在下个周期自动重试。'
          }
        />
      </Space>
    )
  }

  if (!data) {
    return (
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Link to="/devices">← 返回设备列表</Link>
        <Empty description="加载中..." />
      </Space>
    )
  }

  // ── 顶部基本信息卡片 ───────────────────────────────────────────────
  const baseItems = [
    { label: '员工', children: data.employee_name || '未注册' },
    { label: '主机名', children: data.hostname || '—' },
    {
      label: '健康状态',
      children: <HealthStatusBadge status={data.status} />,
    },
    { label: 'Agent 版本', children: data.agent_version || '—' },
    { label: '机器标识', children: <Text copyable>{data.machine_id}</Text> },
    {
      label: '运行时长',
      children: data.uptime_seconds ? formatDuration(data.uptime_seconds) : '—',
    },
    { label: '重启次数', children: data.restart_count ?? 0 },
    { label: '上次退出原因', children: data.last_exit_reason || '—' },
    { label: '待上报', children: data.pending_events ?? 0 },
    { label: '最近上报', children: formatDateTime(data.last_seen) },
    { label: '最近 Upload', children: formatDateTime(data.last_upload) },
    { label: '本次启动', children: formatDateTime(data.process_started_at) },
    { label: '首次启动', children: formatDateTime(data.first_start_at) },
    { label: '上次启动', children: formatDateTime(data.last_start_at) },
    { label: '上次退出', children: formatDateTime(data.last_exit_at) },
    { label: '心跳记录时间', children: formatDateTime(data.last_report_at) },
  ]

  // ── Watchdog 卡片（只在详情显示，列表不显示）───────────────────────
  const watchdog = data.watchdog
  const workerNames = watchdog
    ? Object.keys(watchdog.thresholds_seconds || {})
    : []
  const watchdogRows = workerNames.map((name) => ({
    key: name,
    name,
    threshold: watchdog.thresholds_seconds?.[name],
    age: watchdog.ages_seconds?.[name],
    miss: watchdog.misses?.[name] ?? 0,
    margin: (() => {
      const thr = watchdog.thresholds_seconds?.[name]
      const age = watchdog.ages_seconds?.[name]
      if (!thr || age == null) return '—'
      return `${Math.max(0, Math.round((1 - age / thr) * 100))}%`
    })(),
  }))
  const watchdogColumns = [
    { title: 'Worker', dataIndex: 'name', width: 120 },
    { title: 'Age (s)', dataIndex: 'age', width: 100 },
    { title: 'Threshold (s)', dataIndex: 'threshold', width: 120 },
    {
      title: '余量', dataIndex: 'margin', width: 80,
      render: (v) => <Text style={{ color: v === '—' ? '#999' : undefined }}>{v}</Text>,
    },
    {
      title: 'Miss', dataIndex: 'miss', width: 80,
      render: (v) => (
        <Text style={{ color: (v ?? 0) > 0 ? '#cf1322' : undefined }}>{v ?? 0}</Text>
      ),
    },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Space>
        <Link to="/devices">← 返回设备列表</Link>
        <Button onClick={onRefresh} loading={loading} size="small">刷新</Button>
      </Space>

      <Title level={4} style={{ margin: 0 }}>
        设备详情：{data.employee_name || data.hostname || data.machine_id}
      </Title>

      <Card size="small">
        <Descriptions
          column={{ xs: 1, sm: 2, md: 3 }}
          size="small"
          items={baseItems.map((it, idx) => ({ key: idx, ...it }))}
        />
      </Card>

      <Card
        size="small"
        title="Watchdog 详情"
        extra={
          watchdog?.enabled === false ? (
            <Text type="secondary">未启用</Text>
          ) : (
            <Text type="secondary">每 60s 自检 / 连续 2 次 miss 触发硬退</Text>
          )
        }
      >
        {watchdog && workerNames.length > 0 ? (
          <Table
            rowKey="key"
            size="small"
            pagination={false}
            columns={watchdogColumns}
            dataSource={watchdogRows}
          />
        ) : (
          <Empty description="无 watchdog 数据" />
        )}
      </Card>

      <Card
        size="small"
        title="升级状态"
        extra={<Text type="secondary">Auto Update · 每小时检查一次</Text>}
      >
        <Descriptions
          column={{ xs: 1, sm: 2, md: 3 }}
          size="small"
          items={[
            {
              key: 'us',
              label: '当前状态',
              children: <UpdateStatusBadge status={data.update_status || 'idle'} />,
            },
            {
              key: 'tv',
              label: '目标版本',
              children: data.update_target_version || '—',
            },
            {
              key: 'lc',
              label: '上次检查',
              children: formatDateTime(data.update_last_check_at),
            },
            {
              key: 'le',
              label: '上次错误',
              children: data.update_last_error
                ? <Text type="danger">{data.update_last_error}</Text>
                : '—',
            },
          ]}
        />
      </Card>
    </Space>
  )
}
