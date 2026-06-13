/**
 * Phase 4.3 · 5.2 · windowAggregation 纯函数单元用例。
 *
 * 跑法：
 *   cd admin
 *   node tests/windowAggregation.test.mjs
 *
 * 不需要 jest / vitest；node 22 原生 ESM。
 */
import assert from 'node:assert/strict'
import {
  aggregateTeamTotals,
  aggregateTopApplications,
} from '../src/utils/windowAggregation.js'

let _pass = 0
let _fail = 0
function it(name, fn) {
  try {
    fn()
    console.log(`[ok] ${name}`)
    _pass++
  } catch (e) {
    console.log(`[FAIL] ${name}: ${e?.message || e}`)
    _fail++
  }
}

// ─── 真实形态的 fake daily_window_stats 返回 ───
const ROWS = [
  {
    employee_id: 1,
    name: 'Alice',
    total_working_seconds: 10000,
    total_break_seconds: 600,
    total_off_shift_seconds: 0,
    top_apps: [
      { process_name: 'chrome.exe', working_seconds: 6000, break_seconds: 300, off_shift_seconds: 0 },
      { process_name: 'vscode.exe', working_seconds: 4000, break_seconds: 300, off_shift_seconds: 0 },
    ],
  },
  {
    employee_id: 2,
    name: 'Bob',
    total_working_seconds: 7000,
    total_break_seconds: 200,
    total_off_shift_seconds: 9000,
    top_apps: [
      { process_name: 'vscode.exe', working_seconds: 5000, break_seconds: 100, off_shift_seconds: 0 },
      // off_shift 巨长，total 会被拉高，但 working 不变
      { process_name: 'steam.exe', working_seconds: 0, break_seconds: 0, off_shift_seconds: 9000 },
      { process_name: 'chrome.exe', working_seconds: 2000, break_seconds: 100, off_shift_seconds: 0 },
    ],
  },
]

// ─── aggregateTopApplications · 排序逻辑 ───

it('排序键 = working_seconds DESC（**不是 total**），off_shift 不污染榜首', () => {
  const top = aggregateTopApplications(ROWS, 10)
  assert.equal(top[0].process_name, 'vscode.exe')  // 4000+5000=9000
  assert.equal(top[1].process_name, 'chrome.exe')  // 6000+2000=8000
  // steam 的 total=9000 比 chrome (8000+) 都高，但 working=0 → 最后
  assert.equal(top[top.length - 1].process_name, 'steam.exe')
})

it('聚合：进程合计正确', () => {
  const top = aggregateTopApplications(ROWS, 10)
  const byProc = Object.fromEntries(top.map((r) => [r.process_name, r]))
  assert.equal(byProc['vscode.exe'].working_seconds, 9000)
  assert.equal(byProc['vscode.exe'].break_seconds, 400)
  assert.equal(byProc['vscode.exe'].off_shift_seconds, 0)
  assert.equal(byProc['vscode.exe'].total_seconds, 9400)

  assert.equal(byProc['chrome.exe'].working_seconds, 8000)
  assert.equal(byProc['chrome.exe'].break_seconds, 400)
  assert.equal(byProc['chrome.exe'].total_seconds, 8400)

  assert.equal(byProc['steam.exe'].working_seconds, 0)
  assert.equal(byProc['steam.exe'].off_shift_seconds, 9000)
  assert.equal(byProc['steam.exe'].total_seconds, 9000)
})

it('topN 切片', () => {
  const top = aggregateTopApplications(ROWS, 2)
  assert.equal(top.length, 2)
  assert.deepEqual(top.map((r) => r.process_name), ['vscode.exe', 'chrome.exe'])
})

it('并列 working_seconds 用 process_name 字母升序兜底（稳定排序）', () => {
  const tie = [
    { top_apps: [
      { process_name: 'b.exe', working_seconds: 100, break_seconds: 0, off_shift_seconds: 0 },
      { process_name: 'a.exe', working_seconds: 100, break_seconds: 0, off_shift_seconds: 0 },
      { process_name: 'c.exe', working_seconds: 100, break_seconds: 0, off_shift_seconds: 0 },
    ] },
  ]
  const top = aggregateTopApplications(tie, 10)
  assert.deepEqual(top.map((r) => r.process_name), ['a.exe', 'b.exe', 'c.exe'])
})

it('边界：rows 为 null/undefined/空 → 返回 []', () => {
  assert.deepEqual(aggregateTopApplications(null), [])
  assert.deepEqual(aggregateTopApplications(undefined), [])
  assert.deepEqual(aggregateTopApplications([]), [])
})

it('边界：top_apps 缺字段 → 0 兜底', () => {
  const r = [{ top_apps: [{ process_name: 'x.exe' }] }]
  const top = aggregateTopApplications(r)
  assert.equal(top[0].working_seconds, 0)
  assert.equal(top[0].total_seconds, 0)
})

it('边界：app.process_name 为空 → 跳过', () => {
  const r = [{ top_apps: [
    { process_name: '', working_seconds: 100 },
    { process_name: 'real.exe', working_seconds: 50 },
  ]}]
  const top = aggregateTopApplications(r)
  assert.equal(top.length, 1)
  assert.equal(top[0].process_name, 'real.exe')
})

// ─── aggregateTeamTotals ───

it('团队合计 KPI', () => {
  const t = aggregateTeamTotals(ROWS)
  assert.equal(t.working, 17000) // 10000 + 7000
  assert.equal(t.breaks, 800)    // 600 + 200
  assert.equal(t.off, 9000)
})

it('aggregateTeamTotals 空/null 安全', () => {
  assert.deepEqual(aggregateTeamTotals(null), { working: 0, breaks: 0, off: 0 })
  assert.deepEqual(aggregateTeamTotals([]), { working: 0, breaks: 0, off: 0 })
})

// ─── 结果 ───
console.log()
console.log(`${_pass} passed, ${_fail} failed`)
process.exit(_fail ? 1 : 0)
