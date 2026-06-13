# Worktide · Remote Team Activity Analytics & Attendance

> **隐私优先的远程团队活动分析与考勤平台** — a privacy-first, full-stack system that turns raw desktop activity into team-running analytics and Telegram-driven attendance, designed to run unattended in production.

<p>
<img alt="Python" src="https://img.shields.io/badge/Python-3.10-3776AB?logo=python&logoColor=white">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi&logoColor=white">
<img alt="SQLAlchemy" src="https://img.shields.io/badge/SQLAlchemy-2.0-d71f00">
<img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black">
<img alt="Vite" src="https://img.shields.io/badge/Vite-5-646CFF?logo=vite&logoColor=white">
<img alt="Ant Design" src="https://img.shields.io/badge/Ant%20Design-5-0170FE?logo=antdesign&logoColor=white">
<img alt="SQLite" src="https://img.shields.io/badge/SQLite-WAL-003B57?logo=sqlite&logoColor=white">
<img alt="License" src="https://img.shields.io/badge/license-AGPL--3.0-blue">
<img alt="CI" src="https://github.com/seasonworks/worktide/actions/workflows/ci.yml/badge.svg">
</p>

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="Worktide admin — live roster" width="880">
</p>

---

## Overview / 简介

**Worktide** is a three-tier system, designed and built end-to-end: a **Windows agent** that observes desktop activity, a **FastAPI service** that interprets it into a work-state machine + attendance records, and a **React admin console** for analytics — wired together with a **Telegram bot** for self-service punch-in/out and bilingual notifications.

它由三层加一个机器人组成:

- **客户端 Agent**(Windows):只观察,不监视——采集前台窗口、空闲秒数,本地 SQLite WAL 缓冲;已提交事件在普通崩溃/重启/断网后仍可恢复续传。
- **服务端 Service**(FastAPI):把原始活动解释成工时状态机与考勤事件,跑后台清扫、提醒调度、设置中心、Telegram 机器人。
- **管理后台 Admin**(React + AntD):设备、员工、工时、窗口分析、实时活动、运行时设置等可视化面板。
- **Telegram 机器人**:员工在群里自助打卡(上/下班、用餐/抽烟/如厕、回座),系统双语通知、@提醒、群发上下班提醒。

> **Privacy by design / 隐私优先**: the agent collects *only* process name, window title, timestamps, and idle seconds. It **never** captures screenshots, keystrokes, URLs, clipboard, or file contents. Deploy only on company-owned devices with informed consent.

---

## Architecture / 架构

```mermaid
flowchart LR
    subgraph Desktop["Employee Desktop (Windows)"]
        A1["window_tracker<br/>(2s sample)"]
        A2["idle detector<br/>(GetLastInputInfo)"]
        A3["SQLite WAL buffer"]
        A4["batched uploader<br/>(30s, backoff retry)"]
        A5["watchdog · single-instance<br/>· auto-update"]
        A1 --> A3
        A2 --> A3
        A3 --> A4
    end

    subgraph Server["Server (Linux / systemd)"]
        S1["FastAPI routers<br/>activity · windows · work<br/>employees · devices · settings"]
        S2["work_state<br/>(state machine)"]
        S3["window_aggregator<br/>→ sessions"]
        S4["sweeper · clock-reminder<br/>· cleanup daemon"]
        S5["telegram_poller<br/>/ handlers / notifier"]
        DB[("SQLite (WAL)")]
        S1 --> S2 --> DB
        S1 --> S3 --> DB
        S4 --> DB
        S5 --> DB
    end

    subgraph Clients
        ADMIN["Admin Console<br/>(React + AntD)"]
        TG["Telegram Bot"]
    end

    A4 -- "HTTPS POST /activity /windows" --> S1
    A5 -- "GET /agent/updates" --> S1
    ADMIN -- "REST /api/v1 (Bearer token)" --> S1
    TG <-- "long-poll getUpdates" --> S5
```

### Punch-in flow / 打卡时序

```mermaid
sequenceDiagram
    participant E as Employee (Telegram)
    participant B as Bot Poller
    participant W as work_state
    participant DB as SQLite
    E->>B: taps "上班 / Clock In"
    B->>W: clock_in(employee)
    W->>DB: UPDATE ... WHERE ended_at IS NULL (rowcount-guarded)
    W-->>B: ActionResult(changed, state, code, msg)
    B-->>E: bilingual confirmation (idempotent on double-tap)
```

---

## Engineering Highlights / 工程亮点

The system is built to survive networks, restarts, and double-clicks without losing data or spamming users.

| # | Capability | What makes it interesting |
|---|---|---|
| 1 | **Crash-resilient offline buffering** | The agent persists every window event to a local **SQLite WAL** buffer before upload, then a batched uploader drains it. Server down? The agent backs off (5/30/120/300s) and keeps buffering; **committed events survive ordinary application crashes, restarts, and network outages**, and are replayed once connectivity recovers. |
| 2 | **Idempotent work-state machine** | `clock_in / clock_out / start_break / return_to_work / expire_overdue_break` each return an `ActionResult(changed, work_state, code, message)`. Re-running an action is a safe no-op; concurrent break closing uses a **rowcount-guarded `UPDATE ... WHERE ended_at IS NULL`** so the sweeper and a manual return can't double-close the same session. |
| 3 | **Process-local suppression patterns** | Three small, lock-guarded dedup layers solve real production UX bugs: suppress idle-exit noise after a manual punch, suppress idle-enter right after an auto-return, and swallow the duplicate no-op a double-tapped button produces. Worst case after a restart is one extra message. |
| 4 | **Runtime-configurable settings** | A `settings_service` layers DB overrides on top of pydantic `Settings`, with a typed whitelist + cache, so operators change thresholds, break limits, reminder times, and notification toggles **live from the admin UI** — no redeploy. |
| 5 | **Bilingual notifications with graceful degradation** | Notifications are EN/中文, **mention by immutable `tg://user?id=`** (survives username changes) with HTML `parse_mode`, falling back to `@username` then **auto-degrading to plain text** if a user is unbound or Telegram rejects the markup — notifications are never dropped. |
| 6 | **Single-process poller discipline** | Telegram `getUpdates` is mutually exclusive per token, so the design runs exactly one poller per process with a persisted offset for restart-safe resume. The current deployment model therefore requires a single application worker when polling is enabled; cross-process leader election is not yet implemented (see [Known Limitations](#known-limitations)). |
| 7 | **Dry-run smoke suite** | A one-command runner drives **18 smoke scripts**, every one monkey-patching `urlopen` to physically forbid real network sends — the notification + state-machine + auth surface is regression-tested with **0 real Telegram messages**. |
| 8 | **Hardened client lifecycle** | Single-instance mutex, scheduled-task autostart, watchdog, and an **auto-update pipeline** (whole-package swap with semver + SHA-256 integrity / shared-secret HMAC gating) — all designed around Windows quirks (BOM configs, sharing violations on upgrade, fast user switching). |
| 9 | **App-layer auth, zero new deps** | A single HTTP middleware gates the whole admin/API surface behind an **HMAC-signed token** (stdlib only), with **PBKDF2-hashed** password, per-IP login throttling, and a precise allowlist that keeps agent-ingest endpoints open — so monitoring data isn't world-readable while agents keep reporting untouched. |
| 10 | **Tuned for the long haul** | SQLite is treated as a real production store under concurrent agents: `busy_timeout` + `journal_size_limit` + a daily `wal_checkpoint(TRUNCATE)` keep write contention and WAL growth in check; production hides `/openapi.json` + `/docs`, and the unauthenticated update endpoints regex-validate the version to close path traversal. |
| 11 | **Observable & self-healing** | Rotating file logs + journald, a device-health channel (heartbeat / lifecycle / update state), and background daemons (break sweeper, clock-reminder scheduler, retention cleanup) that each isolate failures so one bad task never takes down the API. |

---

## Tech Stack

| Layer | Stack |
|---|---|
| **Agent** | Python 3.10, Win32 APIs (`GetForegroundWindow` / `GetLastInputInfo`), SQLite WAL, PyInstaller, Inno Setup |
| **Server** | FastAPI, SQLAlchemy 2.0, Pydantic v2, SQLite (WAL), uvicorn, stdlib HMAC/PBKDF2 auth |
| **Admin** | React 18, Vite 5, Ant Design 5, Axios |
| **Bot** | Telegram Bot API (long-poll), bilingual templating |
| **Ops** | systemd, nginx, rotating logs |

---

## Repository Layout

```
worktide/
├── client/      Windows agent (sampler, WAL buffer, uploader, watchdog, updater, installer)
├── server/      FastAPI service (routers, work-state machine, services, smoke suite)
├── admin/       React + Vite + Ant Design admin console
└── README.md
```

---

## Quickstart

> Requires Python 3.10+ and Node 18+.

### 1. Server

```bash
cd server
python -m venv .venv
. .venv/Scripts/activate        # Windows
# source .venv/bin/activate      # macOS/Linux
pip install -r requirements.txt
cp .env.example .env             # then fill in the values (see comments in the file)
uvicorn app.main:app --host 127.0.0.1 --port 9000 --workers 1
```

The server auto-creates the SQLite schema on first run. Generate the admin password hash and session secret with the helpers in [`server/app/auth.py`](server/app/auth.py).

> ⚠️ **Run a single worker.** The in-process background services (cleanup, break sweeper, clock-reminder scheduler) and the Telegram poller assume one process — see [Known Limitations](#known-limitations).

### 2. Admin console

```bash
cd admin
npm install
npm run dev        # dev server on http://localhost:5173
npm run build      # production build → dist/
```

### 3. Agent (Windows only)

```powershell
cd client
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.json config.json   # then set "server_url" and "employee_name"
python main.py
```

For production, build a signed installer with the scripts in [`client/installer`](client/installer) / [`client/packaging`](client/packaging).

---

## Testing

```bash
cd server
python scripts/run_all_smokes.py
```

The suite contains **18 dry-run smoke scripts** that monkey-patch `urlopen` to forbid real network calls. Most run standalone; the notification/integration scripts read flags from your `.env` (set `TELEGRAM_ENABLED=true` with a placeholder token — nothing is ever actually sent), so configure `.env` and start the server once to initialize the schema before running the full suite.

---

## Screenshots

> All captures use demo / placeholder data — no real employee information.

**Live roster / 在线员工** — real-time status, work-state and idle/break at a glance
![Live roster](docs/screenshots/dashboard.png)

**Employee detail · shift breakdown / 员工详情 · 当前班次统计** — net work, idle, break types, and top apps
![Employee detail](docs/screenshots/employee-detail.png)

**Work hours & attendance / 工时统计** — per-day team totals and a net-hours leaderboard
![Work hours](docs/screenshots/work-hours.png)

**Window analytics / 窗口分析** — per-app on-shift vs break vs off-shift time, top apps ranked
![Window analytics](docs/screenshots/window-analytics.png)

**Device health / 设备健康** — agent heartbeat, version, uptime, restart count and update state
![Device health](docs/screenshots/device-health.png)

The Mermaid diagrams above render natively on GitHub as the architectural reference.

---

## Security & Privacy Notes

- The agent collects only window titles, process names, timestamps, and idle seconds — never screenshots, keystrokes, URLs, clipboard, or file contents.
- The admin/API surface is gated by an HMAC-signed bearer token; passwords are PBKDF2-hashed; login is rate-limited per IP.
- Secrets live only in environment files (see `server/.env.example`) — never in the repo.
- Deploy strictly on company-owned devices, with employees informed and consenting, in line with local labor and privacy law.

### Current security limitations / 当前安全限制

- Agent ingest endpoints (`/api/v1/activity/report`, `/api/v1/windows/report`, `/api/v1/agent/*`) do **not** yet use per-device credentials, and an unknown `machine_id` reporting activity may self-register as a new employee.
- Until device enrollment and signed requests are implemented, deploy the ingest API behind trusted network controls — a private tunnel, VPN, or an authenticated reverse-proxy boundary — rather than exposing it directly to the public internet.
- 上报接口尚无每设备凭证、未知 `machine_id` 可能自动注册;在设备注册与请求签名落地前,应通过可信网络 / VPN / 私有隧道 / 反向代理鉴权来保护上报接口。

---

## Known Limitations

An honest engineering showcase — the boundaries below are intentional trade-offs for the current scale, not defects.

- **Agent ingest is unauthenticated.** No per-device credentials yet; unknown devices may self-register. Per-device `device_id`/`device_secret`, admin-approved enrollment, and HMAC-signed requests (timestamp + nonce + body hash, with replay protection and revocation) are planned. Until then, keep the ingest API behind a trusted network boundary.
- **Single-worker deployment.** The Telegram poller and the in-process background services (cleanup, break sweeper, clock-reminder scheduler) assume one application worker. Run uvicorn with `--workers 1` when polling is enabled; horizontal scaling needs dedicated worker processes with leader election.
- **Auto-update uses a shared-secret HMAC** for SHA-256 integrity gating, not asymmetric package signing. A future release should move to Ed25519 signatures with the private key kept only in the release environment and only the public key embedded in agents.
- **Admin auth is a single shared password**, not multi-user RBAC. The admin console stores its bearer token in `localStorage`; a hardened deployment should move to Secure / HttpOnly / SameSite cookies with server-side session revocation and CSRF protection.
- **SQLite (WAL)** targets single-node small/medium deployments. PostgreSQL is the intended migration path for larger scale.
- **Schema management** currently relies on `create_all` plus small idempotent startup migrations. Alembic-based versioned migrations are planned before broader multi-environment deployment.

---

## License

[AGPL-3.0](LICENSE) © 2026 seasonworks

Worktide is licensed under the **GNU Affero General Public License v3.0**. Network use counts as distribution: if you run a modified version as a network-accessible service, you must make the complete corresponding source available to its users.
