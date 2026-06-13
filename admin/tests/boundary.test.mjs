/**
 * 5.4 · AnalyticsErrorBoundary 单元 + 故障注入测试。
 *
 * 跑法：
 *   cd admin
 *   node tests/boundary.test.mjs
 *
 * 不需要 jest / vitest / jsdom：
 *  - boundary 用 React.createElement（无 JSX）写在 .js 里，node 可直接 import
 *  - 用 react-dom/server 的 renderToString 渲染最终输出
 *
 * 重要前提（React 18 官方文档）：
 *   "Error boundaries do not catch errors for: ... Server side rendering."
 *   ─ https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary
 *
 * 因此 fault injection 不能"在 SSR 期间抛错指望 boundary 接住" —— 客户端才 catch。
 * 但 boundary 的 **catch 后行为** 完全可以离线验证：
 *   1) 静态 getDerivedStateFromError(err) → { hasError: true, error } —— React catch 后第一步
 *   2) 把 boundary 实例的 state 设为 hasError → 调 render() → SSR 渲染 fallback DOM
 *   3) componentDidCatch(err, info) → console.warn 出错信息（不静默吞错）
 *
 * 三步组合 = React catch → 状态切换 → fallback 渲染的全链路；
 * 真正的"运行时 catch"由 React 自身保证，不在本组件可控范围。
 *
 * 用例：
 *  1) 正常 props → 子树渲染，无 fallback
 *  2) hasError=true → fallback 含设计文案"窗口分析模块暂时不可用" + 重试按钮
 *  3) 平铺两实例：一个 error 状态、一个正常 → 互不影响（局部崩溃验证）
 *  4) 静态 getDerivedStateFromError → 返回 { hasError: true, error }
 *  5) componentDidCatch 走 console.warn
 *  6) handleRetry 清掉 hasError，恢复正常子树
 */
import assert from 'node:assert/strict'
import React from 'react'
import { renderToString } from 'react-dom/server'
import AnalyticsErrorBoundary from '../src/components/AnalyticsErrorBoundary.js'

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

const FALLBACK_MARK = '暂时不可用'
const RETRY_MARK = '重试'

function Ok({ msg = 'OK' } = {}) {
  return React.createElement('span', { 'data-ok': 'true' }, msg)
}

// 把一个 boundary instance 当作离线模拟：直接设 state（等价于 React 已 catch 完）。
function instanceInErrorState(label = '窗口分析模块', err = new Error('synthetic-boom')) {
  const inst = new AnalyticsErrorBoundary({ label })
  // 模拟 React 的"调静态钩子 → 合 state"两步
  inst.state = { ...inst.state, ...AnalyticsErrorBoundary.getDerivedStateFromError(err) }
  return inst
}

// ─────────────────────────────────────────────────────────────────────────────
// 1) 正常 props → 渲染儿子
// ─────────────────────────────────────────────────────────────────────────────
it('正常 props：渲染儿子，无 fallback', () => {
  const html = renderToString(
    React.createElement(
      AnalyticsErrorBoundary,
      null,
      React.createElement(Ok, { msg: 'CHILD-RENDERED' }),
    ),
  )
  assert.match(html, /CHILD-RENDERED/)
  assert.doesNotMatch(html, new RegExp(FALLBACK_MARK))
})

// ─────────────────────────────────────────────────────────────────────────────
// 2) 错误状态：fallback 含设计文案 + 重试按钮
// ─────────────────────────────────────────────────────────────────────────────
it('hasError=true → fallback 含设计文案 + 重试按钮', () => {
  const inst = instanceInErrorState('窗口分析模块')
  const html = renderToString(inst.render())
  assert.match(html, new RegExp(`窗口分析模块${FALLBACK_MARK}`))
  assert.match(html, new RegExp(RETRY_MARK))
})

// ─────────────────────────────────────────────────────────────────────────────
// 3) 平铺两实例：一个 error 状态、一个正常 → 互不影响（局部崩溃验证）
// ─────────────────────────────────────────────────────────────────────────────
it('平铺：A error / B 正常 → fallback 仅在 A，B 仍渲染', () => {
  const errInst = instanceInErrorState('A 块')
  // B 块"正常"：直接拿一个非 error 实例，把儿子塞进 props
  const okEl = React.createElement(
    AnalyticsErrorBoundary,
    { label: 'B 块' },
    React.createElement(Ok, { msg: 'B-STILL-RENDERED' }),
  )

  const html = renderToString(
    React.createElement('div', null, errInst.render(), okEl),
  )
  assert.match(html, /A 块[\s\S]*?暂时不可用/, 'A 块 fallback 出现')
  assert.match(html, /B-STILL-RENDERED/, 'B 块儿子正常渲染')
  assert.doesNotMatch(html, /B 块[\s\S]*?暂时不可用/, 'B 块不应进入 fallback')
})

// ─────────────────────────────────────────────────────────────────────────────
// 4) 静态 hook：getDerivedStateFromError
// ─────────────────────────────────────────────────────────────────────────────
it('getDerivedStateFromError 返回 { hasError: true, error }', () => {
  const err = new Error('unit')
  const next = AnalyticsErrorBoundary.getDerivedStateFromError(err)
  assert.equal(next.hasError, true)
  assert.equal(next.error, err)
})

// ─────────────────────────────────────────────────────────────────────────────
// 5) componentDidCatch 走 console.warn（直接调实例）
// ─────────────────────────────────────────────────────────────────────────────
it('componentDidCatch 调用 console.warn（不静默吞错）', () => {
  const orig = console.warn
  let captured = ''
  console.warn = (...args) => { captured = args.join(' ') }
  try {
    const inst = new AnalyticsErrorBoundary({})
    inst.componentDidCatch(
      new Error('hello-from-test'),
      { componentStack: 'at <Synthetic>' },
    )
    assert.match(captured, /AnalyticsErrorBoundary/)
    assert.match(captured, /hello-from-test/)
  } finally {
    console.warn = orig
  }
})

// ─────────────────────────────────────────────────────────────────────────────
// 6) handleRetry：清掉 hasError → 重新渲染儿子
// ─────────────────────────────────────────────────────────────────────────────
it('handleRetry 清掉 hasError，render() 回到儿子', () => {
  const inst = instanceInErrorState()
  // 让 inst 有真实的 children 准备给恢复后渲染
  inst.props = {
    ...inst.props,
    children: React.createElement(Ok, { msg: 'AFTER-RETRY' }),
  }
  // setState 在脱离 React tree 时不会触发渲染，这里手动模拟其结果
  // （等价：retry → React 调度下一次 render）
  const mockSet = (next) => { inst.state = { ...inst.state, ...next } }
  inst.setState = mockSet
  inst.handleRetry()
  assert.equal(inst.state.hasError, false)
  assert.equal(inst.state.error, null)
  const html = renderToString(inst.render())
  assert.match(html, /AFTER-RETRY/)
  assert.doesNotMatch(html, /暂时不可用/)
})

// ─────────────────────────────────────────────────────────────────────────────
// 入口
// ─────────────────────────────────────────────────────────────────────────────
console.log()
console.log(`${_pass} passed, ${_fail} failed`)
process.exit(_fail ? 1 : 0)
