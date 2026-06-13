# 远程员工管理后台（前端）

React + Vite + Ant Design 管理后台。前后端分离，通过 REST API 消费服务端数据，
开发期用 Vite 代理转发到后端 9000，免 CORS。

## 技术栈

React 18 · Vite 5 · Ant Design 5 · React Router 6 · axios（JavaScript/JSX）

## 开发

```bash
cd admin
npm install
npm run dev        # http://localhost:5173
```

需要后端已在 `http://localhost:9000` 运行：
```bash
cd ../server
uvicorn app.main:app --host 0.0.0.0 --port 9000
```

## 构建

```bash
npm run build      # 产物在 dist/
npm run preview    # 本地预览构建产物
```

## API 基址

- 开发：axios `baseURL = /api/v1`，由 [vite.config.js](vite.config.js) 代理到 `http://localhost:9000`。
- 生产：设置 `VITE_API_BASE_URL`（含 `/api/v1`）指向真实服务端（需后端 CORS 放行该来源）。

## 目录

```
src/
├── main.jsx              入口（Router + AntD ConfigProvider）
├── App.jsx               路由表
├── layouts/MainLayout    Layout：左侧菜单 + Header + Content
├── pages/                EmployeesPage / RecentActivityPage / EmployeeDetailPage
├── api/                  client(axios 实例) + 各资源接口封装
├── components/           StatusTag 等复用组件
├── hooks/                usePolling（30s 轮询）
└── utils/                logger / format
```

## 页面（开发顺序）

- S1 地基（当前）：Layout / 路由 / 菜单 / api / hooks，页面为占位
- S2 在线员工列表 `/employees`
- S3 最近活动 `/activity`
- S4 单员工详情 `/employees/:id`
