/**
 * Phase 4.3 · 5.5 · Analytics UI End-to-End Validation。
 *
 * 跑法：
 *   cd admin
 *   node tests/e2e/e2e.test.mjs
 *
 * 流程：
 *   1. 起隔离测试 server（detail_card_seeder：2 在职 + 1 archived + 1 no_data）
 *   2. 起 vite dev（独立 config，proxy 指向那个 server）
 *   3. 跑 12 项 E2E 程序化断言（A/B/C/D/F/G/J/L 数据层 + 5 项源码静态检查）
 *   4. 关 vite 关 server，输出报告
 *
 * 不引入 Playwright / Cypress / Puppeteer / 截图回归（按 5.5 明确要求）。
 * 真正的 UI 交互（E/H/I/K）由文末 Manual Smoke Checklist 真人验证。
 */
import { spawn } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const ADMIN_DIR = path.resolve(__dirname, '../..')
const REPO_ROOT = path.resolve(ADMIN_DIR, '..')
const CLIENT_DIR = path.join(REPO_ROOT, 'client')
const SEEDER = path.join(
  CLIENT_DIR, 'tests', 'integration', 'detail_card_seeder.py',
)
const VITE_BIN = path.join(ADMIN_DIR, 'node_modules', 'vite', 'bin', 'vite.js')
const E2E_VITE_CFG = path.join(__dirname, 'vite.config.e2e.js')
const VITE_PORT = Number(process.env.E2E_VITE_PORT) || 5273

if (!existsSync(SEEDER)) throw new Error(`seeder missing: ${SEEDER}`)
if (!existsSync(VITE_BIN)) throw new Error(`vite not installed: ${VITE_BIN}`)

let _pass = 0
let _fail = 0
const _failures = []

function ok(m) { console.log(`[ok] ${m}`); _pass++ }
function fail(m) { console.log(`[FAIL] ${m}`); _failures.push(m); _fail++ }
function check(cond, m) { (cond ? ok : fail)(m) }

// ─────────────────────────────────────────────────────────────────────────────
// 启停辅助
// ─────────────────────────────────────────────────────────────────────────────

async function startSeeder() {
  const proc = spawn('python', [SEEDER], {
    cwd: CLIENT_DIR,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  })
  let stdout = ''
  let stderr = ''
  const info = await new Promise((resolve, reject) => {
    proc.stdout.on('data', (b) => {
      stdout += b.toString('utf-8')
      const m = stdout.match(/\{"port":\s*\d[^\n]+\}/)
      if (m) { try { resolve(JSON.parse(m[0])) } catch (e) { reject(e) } }
    })
    proc.stderr.on('data', (b) => { stderr += b.toString('utf-8') })
    proc.on('error', reject)
    proc.on('exit', (code) => {
      if (code !== 0 && !stdout.includes('"port"')) {
        reject(new Error(`seeder exited rc=${code}\n${stderr}`))
      }
    })
  })
  return { proc, info }
}

async function stopSeeder({ proc }) {
  try { proc.stdin.end() } catch {}
  await new Promise((r) => proc.once('close', r))
}

async function startVite(apiTarget) {
  const proc = spawn(process.execPath, [
    VITE_BIN,
    '--config', E2E_VITE_CFG,
  ], {
    cwd: ADMIN_DIR,
    env: {
      ...process.env,
      VITE_DEV_API_TARGET: apiTarget,
      E2E_VITE_PORT: String(VITE_PORT),
    },
  })

  let stderr = ''
  proc.stderr.on('data', (b) => { stderr += b.toString('utf-8') })
  proc.stdout.on('data', () => {})  // 静音 vite 自身的日志

  // 探活 GET /，最长 30s
  const deadline = Date.now() + 30000
  while (Date.now() < deadline) {
    if (proc.exitCode != null) {
      throw new Error(`vite exited prematurely:\n${stderr}`)
    }
    try {
      const r = await fetch(`http://127.0.0.1:${VITE_PORT}/`, {
        signal: AbortSignal.timeout(800),
      })
      if (r.status === 200) return proc
    } catch {}
    await new Promise((r) => setTimeout(r, 300))
  }
  throw new Error(`vite did not become ready within 30s:\n${stderr}`)
}

async function stopVite(proc) {
  if (!proc) return
  if (process.platform === 'win32') {
    spawn('taskkill', ['/PID', String(proc.pid), '/F', '/T'])
  } else {
    proc.kill('SIGTERM')
  }
  await new Promise((r) => proc.once('close', r))
}

// ─────────────────────────────────────────────────────────────────────────────
// 源码静态检查（替代 UI 交互断言）
// ─────────────────────────────────────────────────────────────────────────────

function staticChecks() {
  console.log('\n── 源码静态检查（覆盖 E / F / H / I / K 代码路径） ──')
  const wap = readFileSync(
    path.join(ADMIN_DIR, 'src', 'pages', 'WindowAnalyticsPage.jsx'),
    'utf-8',
  )
  // E: date / scope 变化立即 refresh —— 依赖数组里同时含 date / includeInactive / refresh
  check(
    wap.includes('[date, includeInactive, refresh]') && /refresh\(\)/.test(wap),
    'E · WindowAnalyticsPage 在 [date, includeInactive] 变化时显式 refresh',
  )
  // F: Segmented 含 SCOPE_ACTIVE / SCOPE_ALL
  check(
    wap.includes('SCOPE_ACTIVE') && wap.includes('SCOPE_ALL')
      && /options=\{\[SCOPE_ACTIVE, SCOPE_ALL\]\}/.test(wap),
    'F · 顶栏 Segmented 含 [在职 / 全部含离职]',
  )
  // K: 三处包 ErrorBoundary
  const wrapMatches = wap.match(/<AnalyticsErrorBoundary\b/g) || []
  check(
    wrapMatches.length >= 3,
    `K · WindowAnalyticsPage 含 ${wrapMatches.length} 个 AnalyticsErrorBoundary（≥3：KPI / Top / Daily）`,
  )

  const card = readFileSync(
    path.join(ADMIN_DIR, 'src', 'components', 'EmployeeWindowActivityCard.jsx'),
    'utf-8',
  )
  // H: 默认 25 / 展开 100 + 查看更多 / 收起
  check(
    /SESSIONS_DEFAULT_LIMIT\s*=\s*25/.test(card)
      && /SESSIONS_EXPANDED_LIMIT\s*=\s*100/.test(card)
      && card.includes('查看更多'),
    'H · Card 含 25/100 二段式 + 「查看更多」按钮',
  )
  // I: Tooltip 包装 + truncateTitle 60
  check(
    card.includes('truncateTitle') && card.includes('TITLE_DISPLAY_MAX = 60')
      && card.includes('<Tooltip'),
    'I · Card title 用 Tooltip + truncateTitle(60 字符)',
  )

  const dp = readFileSync(
    path.join(ADMIN_DIR, 'src', 'pages', 'EmployeeDetailPage.jsx'),
    'utf-8',
  )
  // K: 详情页 Card 外裹 ErrorBoundary
  check(
    /<AnalyticsErrorBoundary[^>]*>[\s\S]*?<EmployeeWindowActivityCard/.test(dp),
    'K · EmployeeDetailPage 把 Card 包在 AnalyticsErrorBoundary',
  )
  // L: include_inactive=true 让详情页对 archived 也能取数
  check(
    card.includes('includeInactive: true'),
    'L · Card 用 includeInactive=true（详情页对 archived 不另做回退）',
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// 主流程
// ─────────────────────────────────────────────────────────────────────────────

let seederHandle
let viteProc

try {
  console.log('── 阶段 1: 起隔离 server ──')
  seederHandle = await startSeeder()
  const info = seederHandle.info
  const backendUrl = `http://127.0.0.1:${info.port}`
  ok(`server up @ ${backendUrl}`)
  console.log(
    `   alice=${info.alice_id} bob=${info.bob_id}`
    + ` archived=${info.archived_id} no_data=${info.no_data_id}`,
  )

  console.log('\n── 阶段 2: 起 vite dev ──')
  viteProc = await startVite(backendUrl)
  const baseUrl = `http://127.0.0.1:${VITE_PORT}`
  ok(`A · vite dev up @ ${baseUrl}`)

  // ─────────── B · 路由可达（SPA fallback：所有未命中的路径回 index.html） ───────────
  console.log('\n── 阶段 3: B · 路由可达 ──')
  for (const route of ['/', '/windows-analytics', `/employees/${info.alice_id}`, `/employees/${info.archived_id}`]) {
    const r = await fetch(`${baseUrl}${route}`)
    const text = await r.text()
    const okStatus = r.status === 200
    const okShape = text.includes('id="root"')  // index.html 标志
    check(okStatus && okShape, `B · GET ${route} → 200 + SPA index.html（status=${r.status}）`)
  }

  // ─────────── C · API Proxy 通到后端 ───────────
  console.log('\n── 阶段 4: C · API Proxy ──')
  const dateStr = info.date
  let r = await fetch(
    `${baseUrl}/api/v1/windows/stats/daily?date=${dateStr}&include_inactive=false&top_n=20`,
  )
  check(r.status === 200, `C · GET /api/v1/windows/stats/daily 通过 proxy 拿到 200`)
  const defStats = await r.json()
  check(Array.isArray(defStats), 'C · proxy 返回 JSON 数组')

  r = await fetch(`${baseUrl}/api/v1/windows/employees/${info.alice_id}?date=${dateStr}&limit=200`)
  check(r.status === 200, 'C · GET /api/v1/windows/employees/{id} 通过 proxy 拿到 200')
  const aliceSessions = await r.json()
  check(Array.isArray(aliceSessions) && aliceSessions.length >= 3,
    `C · alice sessions ≥ 3（got ${aliceSessions.length}）`)

  // ─────────── D · Admin Analytics 数据层 ───────────
  console.log('\n── 阶段 5: D · Top Applications / Daily Usage 数据 ──')
  check(defStats.length >= 2, `D · Daily Usage 至少含 2 名在职员工（got ${defStats.length}）`)
  // 验证 reduce 排序：working_seconds DESC
  const byProc = new Map()
  for (const e of defStats) {
    for (const a of e.top_apps || []) {
      const c = byProc.get(a.process_name) || { working: 0 }
      c.working += a.working_seconds || 0
      byProc.set(a.process_name, c)
    }
  }
  const sortedProcs = [...byProc.entries()].sort((a, b) => b[1].working - a[1].working)
  check(sortedProcs.length > 0, `D · Top Applications 聚合非空（${sortedProcs.length} 进程）`)
  if (sortedProcs.length >= 2) {
    check(
      sortedProcs[0][1].working >= sortedProcs[1][1].working,
      `D · Top Applications 排序 working DESC：${sortedProcs[0][0]} ≥ ${sortedProcs[1][0]}`,
    )
  }

  // ─────────── F · archived 切换 ───────────
  console.log('\n── 阶段 6: F · archived 切换 ──')
  const defIds = new Set(defStats.map((r) => r.employee_id))
  check(!defIds.has(info.archived_id),
    `F · 默认 include_inactive=false：archived emp_id=${info.archived_id} 不显示`)

  r = await fetch(
    `${baseUrl}/api/v1/windows/stats/daily?date=${dateStr}&include_inactive=true&top_n=20`,
  )
  const allStats = await r.json()
  const allIds = new Set(allStats.map((r) => r.employee_id))
  check(allIds.has(info.archived_id),
    `F · 切到 include_inactive=true：archived emp_id=${info.archived_id} 出现`)

  // ─────────── G · Employee Detail 数据层 ───────────
  console.log('\n── 阶段 7: G · Employee Detail ──')
  // 详情页 Card 用 include_inactive=true，找本员工那一行
  const aliceStat = allStats.find((r) => r.employee_id === info.alice_id)
  check(!!aliceStat, 'G · alice 在 include_inactive=true 视图中可见（用于 KPI 三桶）')
  check(
    aliceStat && Array.isArray(aliceStat.top_apps) && aliceStat.top_apps.length > 0,
    'G · alice top_apps 非空（用于 Top Apps 表）',
  )
  check(aliceSessions.length > 0, 'G · alice Recent Sessions 非空')

  // ─────────── J · 空态 ───────────
  console.log('\n── 阶段 8: J · 空态 ──')
  r = await fetch(`${baseUrl}/api/v1/windows/employees/${info.no_data_id}?date=${dateStr}&limit=200`)
  const noDataSess = await r.json()
  check(Array.isArray(noDataSess) && noDataSess.length === 0,
    `J · no_data 员工 sessions=[] → Card 显示空态文案`)
  const noDataStat = allStats.find((r) => r.employee_id === info.no_data_id)
  check(
    noDataStat && noDataStat.total_working_seconds === 0
      && (!noDataStat.top_apps || noDataStat.top_apps.length === 0),
    'J · no_data 员工 totals 全 0 + top_apps=[]（触发完整空态分支）',
  )

  // ─────────── L · 离职员工详情可达 ───────────
  console.log('\n── 阶段 9: L · 离职员工详情 ──')
  r = await fetch(`${baseUrl}/employees/${info.archived_id}`)
  check(r.status === 200, `L · /employees/{archivedId} SPA 路由 200`)
  r = await fetch(`${baseUrl}/api/v1/windows/employees/${info.archived_id}?date=${dateStr}&limit=200`)
  const archSess = await r.json()
  check(Array.isArray(archSess) && archSess.length > 0,
    `L · archived 员工历史 sessions 可见（got ${archSess.length}）`)
  const archStat = allStats.find((r) => r.employee_id === info.archived_id)
  check(!!archStat,
    `L · archived 员工 KPI（include_inactive=true 下）可见`)

  // ─────────── 静态源码检查 · E/F/H/I/K ───────────
  staticChecks()

} finally {
  console.log('\n── 收尾 ──')
  if (viteProc) await stopVite(viteProc).catch(() => {})
  if (seederHandle) await stopSeeder(seederHandle).catch(() => {})
}

console.log()
console.log(`${_pass} passed, ${_fail} failed`)
if (_failures.length) {
  for (const f of _failures) console.log(`  - ${f}`)
}
process.exit(_fail ? 1 : 0)
