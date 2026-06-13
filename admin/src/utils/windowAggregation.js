/**
 * Phase 4.3 · 客户端窗口活动 reduce 工具（纯函数，无 UI 依赖）。
 *
 * 抽离的好处：
 * 1. 页面 jsx 不再含逻辑分支，只负责渲染
 * 2. 可以用 node 直接跑单元用例（详见 admin/tests/window_aggregation.test.mjs）
 *
 * 设计取舍：
 *  - 排序键 = working_seconds，而**不是** total_seconds：在岗时间才能真实反映"工作中使用"
 *    的应用，把 off_shift（下班离岗也开着）算进 total 会被员工的电脑使用习惯污染榜单。
 *  - 并列时按 process_name 字母升序兜底，确保 UI 渲染稳定（关键：与表格 rowKey 一致才能避免 React diff 抖动）
 */

/**
 * 把全员 top_apps 聚合为 process_name 维度合计，按 working_seconds DESC 排序，取前 topN。
 *
 * @param {Array<{
 *   top_apps?: Array<{
 *     process_name: string,
 *     working_seconds?: number,
 *     break_seconds?: number,
 *     off_shift_seconds?: number,
 *   }>
 * }>} employeeRows  /windows/stats/daily 返回的数组
 * @param {number} [topN=20]
 * @returns {Array<{
 *   process_name: string,
 *   working_seconds: number,
 *   break_seconds: number,
 *   off_shift_seconds: number,
 *   total_seconds: number,
 * }>}
 */
export function aggregateTopApplications(employeeRows, topN = 20) {
  const byProc = new Map()
  for (const emp of employeeRows || []) {
    if (!Array.isArray(emp?.top_apps)) continue
    for (const app of emp.top_apps) {
      if (!app?.process_name) continue
      const key = app.process_name
      const cur = byProc.get(key) || {
        process_name: key,
        working_seconds: 0,
        break_seconds: 0,
        off_shift_seconds: 0,
      }
      cur.working_seconds += app.working_seconds || 0
      cur.break_seconds += app.break_seconds || 0
      cur.off_shift_seconds += app.off_shift_seconds || 0
      byProc.set(key, cur)
    }
  }
  const list = Array.from(byProc.values())
  for (const r of list) {
    r.total_seconds =
      r.working_seconds + r.break_seconds + r.off_shift_seconds
  }
  list.sort((a, b) => {
    if (b.working_seconds !== a.working_seconds) {
      return b.working_seconds - a.working_seconds
    }
    return a.process_name.localeCompare(b.process_name)
  })
  return list.slice(0, topN)
}

/**
 * 把全员 totals 横向求和成团队合计（用于 KPI 条）。
 */
export function aggregateTeamTotals(employeeRows) {
  return (employeeRows || []).reduce(
    (acc, r) => ({
      working: acc.working + (r.total_working_seconds || 0),
      breaks: acc.breaks + (r.total_break_seconds || 0),
      off: acc.off + (r.total_off_shift_seconds || 0),
    }),
    { working: 0, breaks: 0, off: 0 },
  )
}
