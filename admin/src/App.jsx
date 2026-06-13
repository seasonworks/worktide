import { Navigate, Route, Routes } from 'react-router-dom'
import MainLayout from './layouts/MainLayout.jsx'
import LoginPage from './pages/LoginPage.jsx'
import EmployeesPage from './pages/EmployeesPage.jsx'
import RecentActivityPage from './pages/RecentActivityPage.jsx'
import EmployeeDetailPage from './pages/EmployeeDetailPage.jsx'
import WorkStatsPage from './pages/WorkStatsPage.jsx'
import EmployeeAdminPage from './pages/EmployeeAdminPage.jsx'
import WindowAnalyticsPage from './pages/WindowAnalyticsPage.jsx'
// Phase 5.3 · Device Health 页面
import DevicesPage from './pages/DevicesPage.jsx'
import DeviceDetailPage from './pages/DeviceDetailPage.jsx'
// Phase 6.5A · 系统设置
import SettingsPage from './pages/SettingsPage.jsx'
// #4 · 后台登录守卫
import { getToken } from './auth/token.js'

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/login" replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/" element={<RequireAuth><MainLayout /></RequireAuth>}>
        <Route index element={<Navigate to="/employees" replace />} />
        <Route path="employees" element={<EmployeesPage />} />
        <Route path="employees/:id" element={<EmployeeDetailPage />} />
        <Route path="employee-admin" element={<EmployeeAdminPage />} />
        <Route path="work-stats" element={<WorkStatsPage />} />
        <Route path="windows-analytics" element={<WindowAnalyticsPage />} />
        <Route path="devices" element={<DevicesPage />} />
        <Route path="devices/:id" element={<DeviceDetailPage />} />
        <Route path="activity" element={<RecentActivityPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="*" element={<Navigate to="/employees" replace />} />
      </Route>
    </Routes>
  )
}
