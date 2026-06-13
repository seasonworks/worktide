import { useMemo } from 'react'
import { Button, Layout, Menu, Popconfirm, theme } from 'antd'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { logout } from '../api/auth.js'

const { Header, Sider, Content } = Layout

// 菜单项（S1 暂不使用图标，避免额外依赖 @ant-design/icons）
const MENU_ITEMS = [
  { key: '/employees', label: '在线员工' },
  { key: '/employee-admin', label: '员工管理' },
  { key: '/work-stats', label: '工时统计' },
  { key: '/windows-analytics', label: '窗口分析' },
  { key: '/devices', label: '设备健康' },   // Phase 5.3
  { key: '/activity', label: '最近活动' },
  { key: '/settings', label: '系统设置' },  // Phase 6.5A
]

export default function MainLayout() {
  const navigate = useNavigate()
  const location = useLocation()
  const { token } = theme.useToken()

  // 详情页 /employees/:id 也应高亮"在线员工"；/devices/:id 同理
  const selectedKey = useMemo(() => {
    const p = location.pathname
    if (p.startsWith('/activity')) return '/activity'
    if (p.startsWith('/work-stats')) return '/work-stats'
    if (p.startsWith('/windows-analytics')) return '/windows-analytics'
    if (p.startsWith('/devices')) return '/devices'   // Phase 5.3
    if (p.startsWith('/employee-admin')) return '/employee-admin'
    if (p.startsWith('/settings')) return '/settings'  // Phase 6.5A
    return '/employees'
  }, [location.pathname])

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Sider breakpoint="lg" collapsedWidth="0">
        <div
          style={{
            height: 56,
            margin: '16px 16px 8px',
            display: 'flex',
            alignItems: 'center',
            gap: 10,
          }}
        >
          <div
            style={{
              width: 34,
              height: 34,
              borderRadius: 9,
              background: 'linear-gradient(135deg, #6366f1, #4f46e5)',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#fff',
              fontWeight: 700,
              fontSize: 14,
              flex: '0 0 auto',
            }}
          >
            FA
          </div>
          <span style={{ color: '#fff', fontWeight: 600, fontSize: 15 }}>员工管理后台</span>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[selectedKey]}
          items={MENU_ITEMS}
          onClick={({ key }) => navigate(key)}
        />
      </Sider>
      <Layout>
        <Header
          style={{
            background: token.colorBgContainer,
            paddingInline: 24,
            display: 'flex',
            alignItems: 'center',
            boxShadow: '0 1px 4px rgba(0,0,0,0.06)',
            position: 'sticky',
            top: 0,
            zIndex: 10,
          }}
        >
          <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600 }}>远程员工管理系统</h1>
          <div style={{ marginLeft: 'auto' }}>
            <Popconfirm
              title="退出登录？"
              okText="退出"
              cancelText="取消"
              onConfirm={logout}
            >
              <Button size="small">退出登录</Button>
            </Popconfirm>
          </div>
        </Header>
        <Content style={{ minHeight: 360, padding: 24 }}>
          <Outlet />
        </Content>
      </Layout>
    </Layout>
  )
}
