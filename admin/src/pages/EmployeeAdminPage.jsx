import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Collapse,
  Descriptions,
  Drawer,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import StatusTag from '../components/StatusTag.jsx'
import {
  archiveEmployee,
  getEmployees,
  restoreEmployee,
  updateEmployee,
} from '../api/employees.js'
import {
  createBinding,
  deleteBinding,
  getBindings,
  getUnboundUsers,
  rebindBinding,
} from '../api/telegram.js'
import { formatDateTime } from '../utils/format.js'

const { Title } = Typography

// 提取后端 detail / 网络错误 message，统一交给 antd message.error
const extractErr = (err, fallback = '操作失败') =>
  err?.response?.data?.detail || err?.message || fallback

export default function EmployeeAdminPage() {
  const { message } = App.useApp()

  const [employees, setEmployees] = useState([])
  const [bindings, setBindings] = useState([])
  const [unbound, setUnbound] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [searchText, setSearchText] = useState('')
  const [showInactive, setShowInactive] = useState(false)

  // Drawer：仅记 employeeId，详细数据从 rows 派生，便于刷新后自动同步
  const [drawerEmployeeId, setDrawerEmployeeId] = useState(null)
  const [nameDraft, setNameDraft] = useState('')
  const [nameSaving, setNameSaving] = useState(false)

  // 绑定区编辑态（已绑时隐藏到「改绑」按钮后）
  const [showBindForm, setShowBindForm] = useState(false)
  const [tgInput, setTgInput] = useState(null)
  const [tgUsername, setTgUsername] = useState('')
  const [bindLoading, setBindLoading] = useState(false)
  const [unbindLoading, setUnbindLoading] = useState(false)

  const [archiveLoading, setArchiveLoading] = useState(false)
  const [restoreLoading, setRestoreLoading] = useState(false)

  const refresh = useCallback(async () => {
    setLoading(true)
    try {
      const [emps, binds, unb] = await Promise.all([
        getEmployees({ includeInactive: showInactive }),
        getBindings(),
        getUnboundUsers({ limit: 200 }),
      ])
      setEmployees(emps)
      setBindings(binds)
      setUnbound(unb)
      setError(null)
      setLastUpdated(new Date())
    } catch (err) {
      setError(err)
      message.error(extractErr(err, '加载失败，请检查后端连接'))
    } finally {
      setLoading(false)
    }
  }, [message, showInactive])

  // 仅挂载/手动/变更后刷新；无自动轮询，避免编辑被冲掉
  useEffect(() => {
    refresh().catch(() => {})
  }, [refresh])

  const bindingMap = useMemo(() => {
    const m = new Map()
    for (const b of bindings) m.set(b.employee_id, b)
    return m
  }, [bindings])

  const rows = useMemo(
    () => employees.map((e) => ({ ...e, binding: bindingMap.get(e.id) || null })),
    [employees, bindingMap],
  )

  const filtered = useMemo(() => {
    const kw = searchText.trim().toLowerCase()
    if (!kw) return rows
    return rows.filter((e) =>
      [e.name, e.machine_id, e.hostname].some((v) =>
        String(v ?? '').toLowerCase().includes(kw),
      ),
    )
  }, [rows, searchText])

  const current = rows.find((r) => r.id === drawerEmployeeId) || null
  const currentBinding = current?.binding || null

  // Drawer 打开时 seed 一次本地编辑态；后续 refresh 不会冲掉 nameDraft
  useEffect(() => {
    if (drawerEmployeeId == null) return
    const r = rows.find((x) => x.id === drawerEmployeeId)
    if (r) setNameDraft(r.name || '')
    setShowBindForm(false)
    setTgInput(null)
    setTgUsername('')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drawerEmployeeId])

  const openDrawer = (row) => setDrawerEmployeeId(row.id)
  const closeDrawer = () => setDrawerEmployeeId(null)

  const onSaveName = async () => {
    const v = nameDraft.trim()
    if (!v) {
      message.warning('姓名不能为空')
      return
    }
    if (v === current?.name) return
    setNameSaving(true)
    try {
      await updateEmployee(current.id, { name: v })
      message.success('姓名已更新')
      await refresh()
    } catch (err) {
      message.error(extractErr(err))
    } finally {
      setNameSaving(false)
    }
  }

  const onBindSubmit = async () => {
    if (!current) return
    if (tgInput == null || tgInput <= 0) {
      message.warning('请输入或选择 Telegram 用户 id')
      return
    }
    setBindLoading(true)
    try {
      const payload = { telegram_user_id: tgInput }
      if (tgUsername.trim()) payload.telegram_username = tgUsername.trim()
      if (currentBinding) {
        await rebindBinding(current.id, payload)
        message.success('已改绑')
      } else {
        await createBinding({ employee_id: current.id, ...payload })
        message.success('已绑定')
      }
      setShowBindForm(false)
      setTgInput(null)
      setTgUsername('')
      await refresh()
    } catch (err) {
      // 409（tg 已被占用 / 已绑）等冲突直接展示后端中文 detail
      message.error(extractErr(err))
    } finally {
      setBindLoading(false)
    }
  }

  const onUnbind = async () => {
    if (!current) return
    setUnbindLoading(true)
    try {
      const res = await deleteBinding(current.id)
      message.success(res?.unbound ? '已解绑' : '无绑定可解')
      await refresh()
    } catch (err) {
      message.error(extractErr(err))
    } finally {
      setUnbindLoading(false)
    }
  }

  const onArchive = async () => {
    if (!current) return
    setArchiveLoading(true)
    try {
      await archiveEmployee(current.id)
      message.success('已离职')
      await refresh()
    } catch (err) {
      message.error(extractErr(err))
    } finally {
      setArchiveLoading(false)
    }
  }

  const onRestore = async () => {
    if (!current) return
    setRestoreLoading(true)
    try {
      await restoreEmployee(current.id)
      message.success('已恢复员工')
      await refresh()
    } catch (err) {
      message.error(extractErr(err))
    } finally {
      setRestoreLoading(false)
    }
  }

  const onPickUnbound = (tgId) => {
    setTgInput(tgId)
    const u = unbound.find((x) => x.telegram_user_id === tgId)
    setTgUsername(u?.telegram_username || '')
  }

  const columns = [
    { title: '姓名', dataIndex: 'name', ellipsis: true },
    { title: 'machine_id', dataIndex: 'machine_id', ellipsis: true, width: 260 },
    { title: '主机名', dataIndex: 'hostname', ellipsis: true, width: 120 },
    {
      title: '活动状态',
      dataIndex: 'status',
      width: 90,
      render: (s) => <StatusTag status={s} />,
    },
    {
      title: 'Telegram',
      key: 'telegram',
      width: 240,
      render: (_, r) =>
        r.binding ? (
          <Tag color="blue">
            已绑 {r.binding.telegram_username ? `@${r.binding.telegram_username} ` : ''}
            ({r.binding.telegram_user_id})
          </Tag>
        ) : (
          <Tag>未绑</Tag>
        ),
    },
    {
      title: '在职',
      dataIndex: 'is_active',
      width: 100,
      render: (active, r) =>
        active ? (
          <Tag color="green">在职</Tag>
        ) : (
          <Tooltip title={`离职时间：${formatDateTime(r.deleted_at)}`}>
            <Tag>已离职</Tag>
          </Tooltip>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 80,
      render: (_, row) => (
        <Button size="small" onClick={() => openDrawer(row)}>
          管理
        </Button>
      ),
    },
  ]

  const unboundColumns = [
    { title: 'telegram_user_id', dataIndex: 'telegram_user_id', width: 180 },
    {
      title: '用户名',
      dataIndex: 'telegram_username',
      width: 180,
      render: (v) => (v ? `@${v}` : '-'),
    },
    { title: '首次出现', dataIndex: 'first_seen', width: 180, render: (v) => formatDateTime(v) },
    { title: '最近出现', dataIndex: 'last_seen', width: 180, render: (v) => formatDateTime(v) },
  ]

  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Title level={4} style={{ margin: 0 }}>员工管理</Title>

      <Space wrap>
        <Input
          placeholder="搜索 姓名 / 主机名 / machine_id"
          allowClear
          value={searchText}
          onChange={(e) => setSearchText(e.target.value)}
          style={{ width: 280 }}
        />
        <Switch
          checked={showInactive}
          onChange={setShowInactive}
          checkedChildren="含离职"
          unCheckedChildren="仅在职"
        />
        <Button onClick={() => refresh().catch(() => {})} loading={loading}>刷新</Button>
        <span style={{ color: '#999' }}>
          最后刷新：{lastUpdated ? lastUpdated.toLocaleTimeString() : '—'}
        </span>
      </Space>

      {error && (
        <Alert
          type="error"
          showIcon
          message="加载失败"
          description="点「刷新」重试或检查后端连接。"
        />
      )}

      <Table
        rowKey="id"
        columns={columns}
        dataSource={filtered}
        loading={loading && employees.length === 0}
        size="middle"
        scroll={{ x: 'max-content' }}
        pagination={{ defaultPageSize: 10, showSizeChanger: true, showTotal: (t) => `共 ${t} 条` }}
      />

      <Collapse
        items={[
          {
            key: 'unbound',
            label: `未绑定 Telegram 用户（${unbound.length}）`,
            children: (
              <Table
                rowKey="telegram_user_id"
                columns={unboundColumns}
                dataSource={unbound}
                size="small"
                pagination={{ pageSize: 10, showTotal: (t) => `共 ${t} 条` }}
              />
            ),
          },
        ]}
      />

      <Drawer
        title={current ? `管理：${current.name}` : '员工管理'}
        width={520}
        open={drawerEmployeeId != null}
        onClose={closeDrawer}
        destroyOnClose
      >
        {current && (
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions bordered column={1} size="small" title="基本信息">
              <Descriptions.Item label="machine_id">{current.machine_id}</Descriptions.Item>
              <Descriptions.Item label="主机名">{current.hostname || '-'}</Descriptions.Item>
              <Descriptions.Item label="活动状态">
                <StatusTag status={current.status} />
              </Descriptions.Item>
              <Descriptions.Item label="在职状态">
                {current.is_active ? (
                  <Tag color="green">在职</Tag>
                ) : (
                  <Tag>已离职</Tag>
                )}
              </Descriptions.Item>
              {!current.is_active && (
                <Descriptions.Item label="离职时间">
                  {formatDateTime(current.deleted_at)}
                </Descriptions.Item>
              )}
              <Descriptions.Item label="首次注册">{formatDateTime(current.created_at)}</Descriptions.Item>
              <Descriptions.Item label="最近上报">{formatDateTime(current.last_seen)}</Descriptions.Item>
            </Descriptions>

            <div>
              <Title level={5} style={{ marginTop: 0 }}>姓名</Title>
              <Space.Compact style={{ width: '100%' }}>
                <Input
                  value={nameDraft}
                  onChange={(e) => setNameDraft(e.target.value)}
                  placeholder="员工姓名"
                  maxLength={64}
                />
                <Button
                  type="primary"
                  loading={nameSaving}
                  disabled={!nameDraft.trim() || nameDraft.trim() === current.name}
                  onClick={onSaveName}
                >
                  保存
                </Button>
              </Space.Compact>
            </div>

            <div>
              <Title level={5}>Telegram 绑定</Title>
              {currentBinding ? (
                <Space direction="vertical" style={{ width: '100%' }}>
                  <div>
                    已绑定：
                    <Tag color="blue">
                      {currentBinding.telegram_username
                        ? `@${currentBinding.telegram_username} `
                        : ''}
                      ({currentBinding.telegram_user_id})
                    </Tag>
                  </div>
                  <Space>
                    {current.is_active && (
                      <Button onClick={() => setShowBindForm((s) => !s)}>
                        {showBindForm ? '取消改绑' : '改绑'}
                      </Button>
                    )}
                    <Popconfirm
                      title="确认解绑？"
                      description="解绑后该 Telegram 用户将无法操作；可随时重新绑定。"
                      onConfirm={onUnbind}
                      okButtonProps={{ loading: unbindLoading }}
                    >
                      <Button danger>解绑</Button>
                    </Popconfirm>
                  </Space>
                  {showBindForm && current.is_active && (
                    <BindForm
                      tgInput={tgInput}
                      setTgInput={setTgInput}
                      tgUsername={tgUsername}
                      setTgUsername={setTgUsername}
                      unbound={unbound}
                      onPick={onPickUnbound}
                      loading={bindLoading}
                      onSubmit={onBindSubmit}
                      label="确认改绑"
                    />
                  )}
                  {!current.is_active && (
                    <div style={{ color: '#999' }}>
                      员工已离职，bind / rebind 已停用；可解绑。恢复员工后可再次绑定。
                    </div>
                  )}
                </Space>
              ) : current.is_active ? (
                <BindForm
                  tgInput={tgInput}
                  setTgInput={setTgInput}
                  tgUsername={tgUsername}
                  setTgUsername={setTgUsername}
                  unbound={unbound}
                  onPick={onPickUnbound}
                  loading={bindLoading}
                  onSubmit={onBindSubmit}
                  label="绑定"
                />
              ) : (
                <div style={{ color: '#999' }}>员工已离职，恢复后再绑定。</div>
              )}
            </div>

            <div>
              <Title level={5}>状态操作</Title>
              {current.is_active ? (
                <Popconfirm
                  title="确认离职？"
                  description="自动结束当前班次/break、Telegram 立即停用、历史完整保留。可后续恢复。"
                  onConfirm={onArchive}
                  okButtonProps={{ loading: archiveLoading, danger: true }}
                  okText="确认离职"
                  cancelText="取消"
                >
                  <Button danger>离职</Button>
                </Popconfirm>
              ) : (
                <Space>
                  <Button type="primary" onClick={onRestore} loading={restoreLoading}>
                    恢复员工
                  </Button>
                  <Tooltip title="后续超管功能">
                    <Button disabled>永久删除</Button>
                  </Tooltip>
                </Space>
              )}
            </div>
          </Space>
        )}
      </Drawer>
    </Space>
  )
}

// 内联表单：手输 tg_user_id 或从未绑定列表选择；username 可选
function BindForm({
  tgInput,
  setTgInput,
  tgUsername,
  setTgUsername,
  unbound,
  onPick,
  loading,
  onSubmit,
  label,
}) {
  return (
    <Space direction="vertical" style={{ width: '100%' }}>
      <InputNumber
        style={{ width: '100%' }}
        value={tgInput}
        onChange={setTgInput}
        placeholder="telegram_user_id（数字）"
        min={1}
        controls={false}
      />
      <Select
        style={{ width: '100%' }}
        placeholder="或从未绑定列表选择"
        value={null}
        onChange={onPick}
        options={unbound.map((u) => ({
          value: u.telegram_user_id,
          label: `${u.telegram_username ? `@${u.telegram_username} ` : ''}(${u.telegram_user_id})`,
        }))}
        showSearch
        filterOption={(input, option) =>
          (option?.label ?? '').toLowerCase().includes(input.toLowerCase())
        }
      />
      <Input
        value={tgUsername}
        onChange={(e) => setTgUsername(e.target.value)}
        placeholder="username（可选，从列表选会自动填）"
        maxLength={64}
      />
      <Button type="primary" loading={loading} onClick={onSubmit}>
        {label}
      </Button>
    </Space>
  )
}
