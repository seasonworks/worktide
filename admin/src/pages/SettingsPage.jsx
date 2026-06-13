// Phase 6.5A · 系统设置 / General Settings 页
//
// 设计取舍：
// - 后端只暴露 key/value/value_type/default/group；UI 元数据（label、unit、下拉
//   选项）放在 constants/settingsOptions.js，方便文案改动不需重启后端
// - 每个控件 onChange 立刻 PATCH 一次（无脏数据状态）；右上角"恢复默认值"统一调
//   POST /settings/reset-all（review #9）
// - 单位转换：UI 显示分钟，API 存秒；TimePicker 用 dayjs 实例
// - 所有控件读取时按 value_type 显式映射：bool→Switch / *_seconds→Select(min) /
//   *_reminder_time→TimePicker / *_hours→Select / 其它→Select(min)
import { useEffect, useMemo, useState } from 'react'
import {
  App,
  Alert,
  Button,
  Card,
  Col,
  Divider,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Spin,
  Switch,
  Tag,
  TimePicker,
  Typography,
} from 'antd'
import dayjs from 'dayjs'
import {
  getSettings,
  resetAllSettings,
  updateSetting,
} from '../api/settings.js'
import {
  BREAK_TIMEOUT_MINUTES,
  CLOCK_REMINDER_MODES,
  GROUP_META,
  GROUP_ORDER,
  IDLE_THRESHOLD_MINUTES,
  KEY_META,
  PRE_CLOCK_IN_MINUTES,
  TZ_OFFSET_HOURS,
} from '../constants/settingsOptions.js'

const { Title, Text } = Typography

// 秒 ↔ 分钟（UI 显示分钟，API 用秒）
const secToMin = (s) => Math.round(Number(s) / 60)
const minToSec = (m) => Number(m) * 60

// 单条设置的渲染：按 key / value_type 选控件
function SettingRow({ setting, onChange }) {
  const meta = KEY_META[setting.key] || { label: setting.key }
  const { key, value, value_type, is_overridden } = setting

  const renderControl = () => {
    // bool → Switch
    if (value_type === 'bool') {
      return (
        <Switch
          checked={Boolean(value)}
          onChange={(checked) => onChange(key, checked)}
        />
      )
    }

    // GS-11 · 提醒模式 → Select(group/individual)
    if (key === 'clock_reminder_mode') {
      return (
        <Select
          value={String(value)}
          onChange={(v) => onChange(key, v)}
          style={{ width: 180 }}
          options={CLOCK_REMINDER_MODES}
        />
      )
    }

    // GS-11 · 上班提前提醒分钟 → Select
    if (key === 'pre_clock_in_minutes') {
      return (
        <Space>
          <Select
            value={Number(value)}
            onChange={(v) => onChange(key, v)}
            style={{ width: 100 }}
            options={PRE_CLOCK_IN_MINUTES.map((m) => ({ value: m, label: m }))}
          />
          <Text type="secondary">分钟</Text>
        </Space>
      )
    }

    // Mention · 管理员 TG ID 列表（自由文本，逗号分隔）→ Input
    if (key === 'admin_telegram_user_ids') {
      return (
        <Input.Search
          defaultValue={value == null ? '' : String(value)}
          placeholder="例如 123456,789012"
          enterButton="保存"
          style={{ width: 280 }}
          onSearch={(v) => onChange(key, v.trim())}
        />
      )
    }

    // HH:MM 时间字符串 → TimePicker
    if (key.endsWith('_reminder_time')) {
      const dj = value ? dayjs(value, 'HH:mm') : null
      return (
        <TimePicker
          format="HH:mm"
          minuteStep={5}
          value={dj}
          onChange={(d) => onChange(key, d ? d.format('HH:mm') : '09:00')}
          allowClear={false}
          style={{ width: 120 }}
        />
      )
    }

    // 时区偏移 → Select
    if (key === 'reminders_timezone_offset_hours') {
      return (
        <Select
          value={Number(value)}
          onChange={(v) => onChange(key, v)}
          style={{ width: 140 }}
          options={TZ_OFFSET_HOURS.map((h) => ({
            value: h,
            label: (h >= 0 ? `UTC+${h}` : `UTC${h}`) + (h === 8 ? ' · 中国' : ''),
          }))}
        />
      )
    }

    // 三个 break_*_max_seconds → 分钟下拉（7 档）
    if (key.startsWith('break_') && key.endsWith('_max_seconds')) {
      return (
        <Space>
          <Select
            value={secToMin(value)}
            onChange={(m) => onChange(key, minToSec(m))}
            style={{ width: 100 }}
            options={BREAK_TIMEOUT_MINUTES.map((m) => ({ value: m, label: m }))}
          />
          <Text type="secondary">分钟</Text>
        </Space>
      )
    }

    // 挂机阈值 → 分钟下拉
    if (key === 'idle_threshold_seconds') {
      return (
        <Space>
          <Select
            value={secToMin(value)}
            onChange={(m) => onChange(key, minToSec(m))}
            style={{ width: 100 }}
            options={IDLE_THRESHOLD_MINUTES.map((m) => ({ value: m, label: m }))}
          />
          <Text type="secondary">分钟</Text>
        </Space>
      )
    }

    // 离线阈值（高级）：30~3600 秒，给 7 档预设
    if (key === 'offline_threshold_seconds') {
      const opts = [30, 60, 90, 120, 300, 600, 1800, 3600]
      return (
        <Space>
          <Select
            value={Number(value)}
            onChange={(v) => onChange(key, v)}
            style={{ width: 100 }}
            options={opts.map((s) => ({ value: s, label: s }))}
          />
          <Text type="secondary">秒</Text>
        </Space>
      )
    }

    return <Text code>{JSON.stringify(value)}</Text>
  }

  return (
    <Row align="middle" style={{ padding: '8px 0' }}>
      <Col flex="auto">
        <Space>
          <Text>{meta.label}</Text>
          {is_overridden ? (
            <Tag color="blue">已自定义</Tag>
          ) : (
            <Tag>默认</Tag>
          )}
        </Space>
        {setting.description ? (
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {setting.description}
            </Text>
          </div>
        ) : null}
      </Col>
      <Col>{renderControl()}</Col>
    </Row>
  )
}

export default function SettingsPage() {
  const { message, modal } = App.useApp()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)
  const [data, setData] = useState(null) // { settings, groups }
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await getSettings()
      setData(res)
    } catch (e) {
      setError(e?.message || '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  // key -> setting 索引，PATCH 后局部更新
  const byKey = useMemo(() => {
    const m = new Map()
    ;(data?.settings || []).forEach((s) => m.set(s.key, s))
    return m
  }, [data])

  const onPatch = async (key, value) => {
    setSaving(true)
    try {
      const updated = await updateSetting(key, value)
      // 局部替换避免整页 reload 闪烁
      setData((prev) => {
        if (!prev) return prev
        return {
          ...prev,
          settings: prev.settings.map((s) => (s.key === key ? updated : s)),
        }
      })
      message.success(`已保存：${KEY_META[key]?.label || key}`)
    } catch (e) {
      const detail = e?.response?.data?.detail || e?.message || '保存失败'
      message.error(`保存失败：${detail}`)
    } finally {
      setSaving(false)
    }
  }

  const onResetAll = () => {
    modal.confirm({
      title: '恢复默认值 / Reset to Defaults',
      content: '所有运行期设置将回到内置默认值，无法撤销。继续吗？',
      okText: '确认恢复',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: async () => {
        try {
          const res = await resetAllSettings()
          setData((prev) => prev ? { ...prev, settings: res.settings } : prev)
          message.success(`已恢复默认（清除 ${res.reset} 条覆盖）`)
        } catch (e) {
          message.error(`恢复失败：${e?.message || ''}`)
        }
      },
    })
  }

  if (loading) {
    return (
      <div style={{ textAlign: 'center', padding: 64 }}>
        <Spin tip="加载设置中..." />
      </div>
    )
  }

  if (error) {
    return <Alert type="error" message="加载失败" description={error} showIcon />
  }

  // 分组渲染
  const groupMap = new Map((data?.groups || []).map((g) => [g.id, g]))
  return (
    <div>
      <Row justify="space-between" align="middle" style={{ marginBottom: 16 }}>
        <Col>
          <Title level={3} style={{ margin: 0 }}>
            系统设置 / General Settings
          </Title>
          <Text type="secondary">
            修改立即生效；改了什么会标记 <Tag color="blue">已自定义</Tag>，未改的是
            <Tag>默认</Tag>
          </Text>
        </Col>
        <Col>
          <Space>
            <Button onClick={load} loading={loading}>
              刷新 / Reload
            </Button>
            <Button danger onClick={onResetAll}>
              恢复默认值 / Reset to Defaults
            </Button>
          </Space>
        </Col>
      </Row>

      {saving ? (
        <div style={{ marginBottom: 8 }}>
          <Spin size="small" /> 保存中...
        </div>
      ) : null}

      {GROUP_ORDER.map((gid) => {
        const g = groupMap.get(gid)
        if (!g) return null
        const meta = GROUP_META[gid] || { label_zh: gid, label_en: gid }
        return (
          <Card
            key={gid}
            title={`${meta.label_zh} / ${meta.label_en}`}
            style={{ marginBottom: 16 }}
          >
            {g.keys.map((k, idx) => {
              const s = byKey.get(k)
              if (!s) return null
              return (
                <div key={k}>
                  {idx > 0 ? <Divider style={{ margin: '4px 0' }} /> : null}
                  <SettingRow setting={s} onChange={onPatch} />
                </div>
              )
            })}
          </Card>
        )
      })}
    </div>
  )
}
