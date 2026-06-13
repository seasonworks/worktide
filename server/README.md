# 远程员工管理系统 · 服务端

基于 FastAPI + SQLAlchemy + SQLite 的服务端，接收客户端上报、保存员工状态与历史记录，并对外提供 REST API。

## 目录结构

```
server/
├── app/
│   ├── main.py            # FastAPI 应用入口、CORS、建表
│   ├── config.py          # 配置（环境变量 / .env）
│   ├── constants.py       # 状态枚举 EmployeeStatus
│   ├── database.py        # SQLAlchemy 引擎与会话
│   ├── models.py          # ORM 模型：employees / activity_logs
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── crud.py            # 数据库操作与状态判定
│   └── routers/
│       ├── activity.py    # 状态上报接口
│       └── employees.py   # 员工查询接口
├── requirements.txt
├── .env.example
└── README.md
```

## 快速开始

```bash
cd server
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt

# 可选：复制 .env.example 为 .env 并修改配置
copy .env.example .env            # Windows

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动后访问：

- 接口文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

数据库文件默认生成在 `server/data/employees.db`。

## API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/activity/report` | 客户端状态上报（每 30 秒一次，自动注册员工） |
| GET | `/employees` | 员工列表（含实时状态） |
| GET | `/employees/idle` | 当前挂机员工 |
| GET | `/employees/{id}` | 员工详情 |
| GET | `/employees/{id}/logs` | 员工历史上报记录 |

### 上报示例

```bash
curl -X POST http://localhost:8000/api/v1/activity/report \
  -H "Content-Type: application/json" \
  -d '{
        "machine_id": "DESKTOP-ABC123",
        "name": "张三",
        "hostname": "DESKTOP-ABC123",
        "idle_seconds": 120,
        "is_active": true
      }'
```

返回：

```json
{
  "employee_id": 1,
  "status": "online",
  "is_idle_alert": false,
  "server_time": "2026-05-26T10:00:00+00:00"
}
```

## 状态判定规则

- `online`：有上报，且空闲秒数 < `IDLE_THRESHOLD_SECONDS`（默认 900 秒）
- `idle`（挂机）：空闲秒数 ≥ `IDLE_THRESHOLD_SECONDS`
- `offline`：距上次上报超过 `OFFLINE_THRESHOLD_SECONDS`（默认 90 秒），读取时实时计算

## 升级到 PostgreSQL

修改 `.env` 中的 `DATABASE_URL`，例如：

```
DATABASE_URL=postgresql+psycopg://user:password@localhost:5432/employees
```

并安装对应驱动（如 `psycopg[binary]`）。
