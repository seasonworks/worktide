import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { App as AntdApp, ConfigProvider } from 'antd'
import zhCN from 'antd/locale/zh_CN'
import App from './App.jsx'

// 全局主题：靛蓝主色 + 更大圆角 + 柔和背景，整体观感更现代、不廉价
const theme = {
  token: {
    colorPrimary: '#4f46e5',
    colorInfo: '#4f46e5',
    borderRadius: 8,
    colorBgLayout: '#f4f5fb',
    fontSize: 14,
    wireframe: false,
  },
  components: {
    Card: { borderRadiusLG: 14, paddingLG: 20 },
    Statistic: { titleFontSize: 13 },
    Layout: { headerBg: '#ffffff', siderBg: '#1f2233', triggerBg: '#1f2233' },
    Menu: { darkItemBg: '#1f2233', darkItemSelectedBg: '#4f46e5', darkSubMenuItemBg: '#1f2233' },
    Table: { headerBg: '#f4f5fb', borderRadius: 10 },
  },
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={theme}>
      {/* AntdApp 提供 message/notification 的上下文，页面里用 App.useApp() 取用 */}
      <AntdApp>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
)
