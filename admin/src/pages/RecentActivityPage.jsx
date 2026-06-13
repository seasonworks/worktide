import { useCallback, useEffect, useState } from 'react'
import { Alert, App, Button, Select, Space, Table, Typography } from 'antd'
import usePolling from '../hooks/usePolling.js'
import { getRecentActivity } from '../api/activity.js'
import { getEmployees } from '../api/employees.js'
import StatusTag from '../components/StatusTag.jsx'
import logger from '../utils/logger.js'
import { formatDateTime, formatIdle } from '../utils/format.js'

const { Title } = Typography

const LIMIT_OPTIONS = [50, 100, 200, 500].map((n) => ({ value: n, label: `最近 ${n} 条` }))

export default function RecentActivityPage() {
  const { message } = App.useApp()

  const [limit, setLimit] = useState(100)
  const [employeeId, setEmployeeId] = useState(undefined) // undefined = 全部员工
  const [employeeOptions, setEmployeeOptions] = useState([])
  const [lastUpdated, setLastUpdated] = useState(null)

  // 取数函数随 limit / employeeId 变化
  const fetcher = useCallback(
    () => getRecentActivity({ limit, employeeId }),
    [limit, employeeId],
  )

  // immediate:false：首取与"切换参数即取"交给下面的 effect，避免重复请求
  const { data, loading, error, refresh } = usePolling(fetcher, {
    interval: 30000,
    immediate: false,
  })

  // 挂载即取 + limit/employeeId 变化立即重取（30s 轮询由 usePolling 负责）
  useEffect(() => {
    refresh().catch(() => {})
  }, [limit, employeeId, refresh])

  useEffect(() => {
    if (data) setLastUpdated(new Date())
  }, [data])

  // 员工下拉选项：挂载时一次性取，失败仅记录、不阻塞主表格
  useEffect(() => {
    let active = true
    getEmployees()
      .then((list) => {
        if (!active) return
        const opts = (Array.isArray(list) ? list : []).map((e) => ({
          value: e.id,
          label: `${e.name || e.machine_id}${e.hostname ? ` (${e.hostname})` : ''}`,
        }))
        setEmployeeOptions(opts)
      })
      .catch((err) => logger.error('加载员工下拉失败', err?.message))
    return () => {
      active = false
    }
  }, [])

  const onRefresh = () => {
    refresh().catch(() => message.error('刷新失败，请检查后端连接'))
  }

  const columns = [
    { title: '员工名', dataIndex: 'employee_name', ellipsis: true },
    { title: '主机名', dataIndex: 'hostname', ellipsis: true, width: 140 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (status) => <StatusTag status={status} />,
    },
    {
      title: '空闲时长',
      dataIndex: 'idle_seconds',
      width: 120,
      render: (v) => formatIdle(v),
    },
    {
      title: '时间',
      dataIndex: 'reported_at',
      width: 180,
      defaultSortOrder: 'descend',
      sorter: (a, b) => new Date(a.reported_at).getTime() - new Date(b.reported_at).getTime(),
      render: (v) => formatDateTime(v),
    },
  ]

  const dataSource = Array.isArray(data) ? data : []

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>最近活动</Title>

      <Space wrap>
        <Select
          value={employeeId ?? 'all'}
          onChange={(v) => setEmployeeId(v === 'all' ? undefined : v)}
          options={[{ value: 'all', label: '全部员工' }, ...employeeOptions]}
          style={{ width: 220 }}
          showSearch
          optionFilterProp="label"
        />
        <Select value={limit} onChange={setLimit} options={LIMIT_OPTIONS} style={{ width: 130 }} />
        <Button onClick={onRefresh} loading={loading}>刷新</Button>
        <span style={{ color: '#999' }}>
          最后刷新：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
        </span>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message="加载最近活动失败"
          description="将在下个周期自动重试；也可点「刷新」或检查后端连接。"
        />
      )}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={dataSource}
        loading={loading && data == null}
        size="middle"
        pagination={{ defaultPageSize: 20, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
      />
    </Space>
  )
}
