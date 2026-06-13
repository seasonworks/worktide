/**
 * 5.3 端到端契约验证：拉起后端 + 注入数据，从前端将要消费的两个端点取数，
 * 用本组件中的相同 reduce 逻辑跑断言。
 *
 * 跑法：
 *   cd admin
 *   node tests/detail_card.mjs
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const REPO_ROOT = path.resolve(__dirname, '../..')
const CLIENT_DIR = path.join(REPO_ROOT, 'client')
const SEEDER = path.join(
  CLIENT_DIR, 'tests', 'integration', 'detail_card_seeder.py',
)

if (!existsSync(SEEDER)) throw new Error(`seeder missing: ${SEEDER}`)
const py = 'python'  // 与 stats_shape.mjs 同因：seeder import requests

let _fail = 0
function ok(m) { console.log(`[ok] ${m}`) }
function check(cond, m) {
  if (cond) ok(m)
  else { console.log(`[FAIL] ${m}`); _fail++ }
}

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
      reject(new Error(`seeder exited rc=${code}\n${stderr}`))
    }
  })
})

const shutdown = async () => {
  try { proc.stdin.end() } catch {}
  await new Promise((r) => proc.once('close', r))
}

// 用与组件一致的 reduce / 排序口径，独立放这里防止双方逻辑漂移
function sortTopAppsByWorking(apps) {
  return [...(apps || [])]
    .sort((a, b) => {
      if (b.working_seconds !== a.working_seconds) {
        return b.working_seconds - a.working_seconds
      }
      return a.process_name.localeCompare(b.process_name)
    })
    .slice(0, 10)
}

try {
  const info = await ready
  ok(`seeder OK port=${info.port} date=${info.date}`)
  console.log(`   alice=${info.alice_id}, bob=${info.bob_id},`
    + ` archived=${info.archived_id}, no_data=${info.no_data_id}`)

  const base = `http://127.0.0.1:${info.port}/api/v1`

  // ── 1) Top Apps 排序：alice 的 chrome 在岗 > vscode 在岗 → chrome 在第 1 位 ──
  // 注意：服务端 top_apps 按 total_seconds DESC；前端再按 working_seconds DESC 重排
  let r = await fetch(`${base}/windows/stats/daily?date=${info.date}&include_inactive=true&top_n=10`)
  check(r.status === 200, `stats/daily 200 (include_inactive=true)`)
  const allStats = await r.json()
  const alice = allStats.find((s) => s.employee_id === info.alice_id)
  check(!!alice, 'alice 在 include_inactive=true 视图中可见')
  if (alice) {
    const top = sortTopAppsByWorking(alice.top_apps)
    check(top.length > 0, `alice top_apps 非空 (got ${top.length})`)
    check(top[0].process_name === 'chrome.exe',
          `alice top#1 = chrome.exe（按 working DESC；got ${top[0]?.process_name}）`)
    // chrome working > vscode working（按 seeder 投喂）
    const vsc = top.find((a) => a.process_name === 'vscode.exe')
    check(vsc && top[0].working_seconds >= vsc.working_seconds,
          `chrome.working >= vscode.working`)
  }

  // ── 2) Recent Sessions 排序：started_at DESC ──
  r = await fetch(`${base}/windows/employees/${info.alice_id}?date=${info.date}&limit=100`)
  check(r.status === 200, `/windows/employees/{alice} 200`)
  const aliceSess = await r.json()
  check(Array.isArray(aliceSess) && aliceSess.length >= 3,
        `alice sessions ≥ 3 (got ${aliceSess.length})`)
  let monotonic = true
  for (let i = 1; i < aliceSess.length; i++) {
    const prev = new Date(aliceSess[i - 1].started_at).getTime()
    const cur = new Date(aliceSess[i].started_at).getTime()
    if (prev < cur) { monotonic = false; break }
  }
  check(monotonic, `sessions started_at DESC（服务端排序兜底）`)

  // ── 3) 空态：从未上传过窗口数据的员工 ──
  r = await fetch(`${base}/windows/employees/${info.no_data_id}?date=${info.date}&limit=100`)
  const emptySess = await r.json()
  check(Array.isArray(emptySess) && emptySess.length === 0,
        `no_data 员工 sessions=[] (got ${emptySess.length})`)
  // 该员工在 stats 里 totals 全 0、top_apps 为空（仍出现在 include_inactive=true 列表中）
  const noData = allStats.find((s) => s.employee_id === info.no_data_id)
  check(!!noData && noData.total_working_seconds === 0 && noData.top_apps.length === 0,
        `no_data 员工 totals 全 0 + top_apps=[]（触发空态）`)

  // ── 4) 离职员工历史可查（端点 /windows/employees/{id} 不做 is_active 过滤）──
  r = await fetch(`${base}/windows/employees/${info.archived_id}?date=${info.date}&limit=100`)
  check(r.status === 200, `archived 员工详情端点 200`)
  const archSess = await r.json()
  check(Array.isArray(archSess) && archSess.length > 0,
        `archived 员工历史 sessions 仍可见 (got ${archSess.length})`)
  // 详情页用 include_inactive=true 取 stats → archived 员工的 totals 也能拿到
  const archStats = allStats.find((s) => s.employee_id === info.archived_id)
  check(!!archStats,
        `archived 员工 totals 仅 include_inactive=true 时可见`)

  // 反例：默认 include_inactive=false 时不可见（5.4 切换前的默认行为）
  r = await fetch(`${base}/windows/stats/daily?date=${info.date}&include_inactive=false&top_n=10`)
  const defStats = await r.json()
  const archInDefault = defStats.some((s) => s.employee_id === info.archived_id)
  check(!archInDefault,
        `默认 include_inactive=false 下 archived 员工不在 daily_stats 中`)

} finally {
  await shutdown()
}

console.log()
if (_fail > 0) {
  console.log(`${_fail} failed`)
  process.exit(1)
}
console.log('5.3 detail card contract: PASS')
