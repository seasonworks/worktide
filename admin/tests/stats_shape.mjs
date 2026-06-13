/**
 * 5.2 端到端 shape 校验：用 client 联调 harness 拉起后端 + 注入数据，
 * 然后 GET /windows/stats/daily 并断言 WindowAnalyticsPage 消费的字段都在。
 *
 * 跑法：
 *   cd admin
 *   node tests/stats_shape.mjs
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  aggregateTeamTotals,
  aggregateTopApplications,
} from '../src/utils/windowAggregation.js'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const REPO_ROOT = path.resolve(__dirname, '../..')
const CLIENT_DIR = path.join(REPO_ROOT, 'client')
const SEEDER = path.join(
  CLIENT_DIR, 'tests', 'integration', 'stats_shape_seeder.py',
)
if (!existsSync(SEEDER)) throw new Error(`seeder missing: ${SEEDER}`)
// seeder 自身要 import `app.window_uploader` -> `requests`，server venv 没装 requests，
// 必须用系统 python；harness.IsolatedServer 内部已用 server venv 起 uvicorn。
const py = 'python'

let _fail = 0
function ok(m) { console.log(`[ok] ${m}`) }
function assertOrFail(cond, m) {
  if (cond) ok(m)
  else { console.log(`[FAIL] ${m}`); _fail++ }
}

console.log(`[harness] spawn seeder via ${py}`)
const proc = spawn(py, [SEEDER], {
  cwd: CLIENT_DIR,
  env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
})

let stdout = ''
let stderr = ''
const ready = new Promise((resolve, reject) => {
  proc.stdout.on('data', (b) => {
    stdout += b.toString('utf-8')
    const m = stdout.match(/\{"port":\s*\d[^\n]+\}/)
    if (m) {
      try { resolve(JSON.parse(m[0])) } catch (e) { reject(e) }
    }
  })
  proc.stderr.on('data', (b) => { stderr += b.toString('utf-8') })
  proc.on('error', reject)
  proc.on('exit', (code) => {
    if (code !== 0 && !stdout.includes('"port"')) {
      reject(new Error(`seeder exited rc=${code} before JSON:\n${stderr}`))
    }
  })
})

const shutdown = async () => {
  try { proc.stdin.end() } catch {}
  await new Promise((r) => proc.once('close', r))
}

try {
  const info = await ready
  ok(`seeder OK port=${info.port} employees=${info.employees.length} date=${info.date}`)

  // GET /api/v1/windows/stats/daily（前端 5.1 hook 调用的同款 URL）
  const url = `http://127.0.0.1:${info.port}/api/v1/windows/stats/daily`
    + `?date=${info.date}&include_inactive=false&top_n=20`
  const r = await fetch(url)
  assertOrFail(r.status === 200, `HTTP 200 from ${url}（got ${r.status}）`)
  const rows = await r.json()
  assertOrFail(Array.isArray(rows), '返回数组')
  assertOrFail(rows.length >= 2, `至少 2 行（archived 应被过滤）实得 ${rows.length}`)

  const REQUIRED = [
    'employee_id', 'name', 'date',
    'total_working_seconds', 'total_break_seconds', 'total_off_shift_seconds',
    'top_apps',
  ]
  const have = Object.keys(rows[0] || {})
  for (const k of REQUIRED) {
    assertOrFail(have.includes(k), `员工行含字段 ${k}`)
  }
  assertOrFail(Array.isArray(rows[0].top_apps), 'top_apps 是数组')

  const APP_REQ = [
    'process_name', 'working_seconds', 'break_seconds',
    'off_shift_seconds', 'total_seconds',
  ]
  const appHave = Object.keys(rows[0].top_apps[0] || {})
  for (const k of APP_REQ) {
    assertOrFail(appHave.includes(k), `top_apps[0] 含字段 ${k}`)
  }

  const top = aggregateTopApplications(rows, 20)
  const team = aggregateTeamTotals(rows)
  assertOrFail(top.length > 0, `reduce 输出 top.length=${top.length}`)
  ok(`reduce: top=[${top.map((t) => t.process_name).join(',')}] team.working=${team.working}s`)

  // 默认 include_inactive=false → archived 员工应不在结果里
  const archivedFound = rows.some((r) => r.employee_id === info.archived_employee_id)
  assertOrFail(
    !archivedFound,
    `默认 include_inactive=false 过滤 archived emp_id=${info.archived_employee_id}`,
  )

  // 额外：include_inactive=true 时 archived 出现
  const url2 = `http://127.0.0.1:${info.port}/api/v1/windows/stats/daily`
    + `?date=${info.date}&include_inactive=true&top_n=20`
  const r2 = await fetch(url2)
  const rows2 = await r2.json()
  const archivedFound2 = rows2.some((r) => r.employee_id === info.archived_employee_id)
  assertOrFail(archivedFound2, `include_inactive=true 时 archived 可见`)
} finally {
  await shutdown()
}

console.log()
if (_fail > 0) {
  console.log(`${_fail} failed`)
  process.exit(1)
}
console.log('5.2 shape contract: PASS')
