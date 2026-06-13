import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { App, Button, Card, Form, Input, Typography } from 'antd'
import { login } from '../api/auth.js'
import { getToken } from '../auth/token.js'

const { Title, Text } = Typography

export default function LoginPage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  const [loading, setLoading] = useState(false)

  // 已登录直接进后台（避免重复登录）
  if (getToken()) {
    navigate('/employees', { replace: true })
  }

  const onFinish = async ({ password }) => {
    setLoading(true)
    try {
      await login(password)
      message.success('登录成功')
      navigate('/employees', { replace: true })
    } catch (e) {
      const status = e?.response?.status
      const detail = e?.response?.data?.detail
      if (status === 429) message.error(detail || '登录失败过多，请稍后再试')
      else if (status === 503) message.error(detail || '后台鉴权未配置，请联系管理员')
      else message.error(detail || '密码错误')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        background: 'linear-gradient(135deg,#1f2233 0%,#4f46e5 100%)',
        padding: 24,
      }}
    >
      <Card style={{ width: 360, boxShadow: '0 12px 40px rgba(0,0,0,0.25)' }}>
        <div style={{ textAlign: 'center', marginBottom: 20 }}>
          <div style={{ fontSize: 32 }}>🛡️</div>
          <Title level={4} style={{ margin: '8px 0 0' }}>员工管理后台</Title>
          <Text type="secondary">请输入管理员密码登录</Text>
        </div>
        <Form layout="vertical" onFinish={onFinish} disabled={loading}>
          <Form.Item
            label="密码"
            name="password"
            rules={[{ required: true, message: '请输入密码' }]}
          >
            <Input.Password
              size="large"
              autoFocus
              placeholder="管理员密码"
              onPressEnter={() => {}}
            />
          </Form.Item>
          <Form.Item style={{ marginBottom: 0 }}>
            <Button type="primary" htmlType="submit" block size="large" loading={loading}>
              登 录
            </Button>
          </Form.Item>
        </Form>
      </Card>
    </div>
  )
}
