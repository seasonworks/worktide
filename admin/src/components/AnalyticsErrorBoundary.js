/**
 * Phase 4.3 · 5.4 · 分析模块错误边界。
 *
 * 为什么用 .js（React.createElement）而不是 .jsx：
 *  - 错误边界本质是 catch-only 容器，fallback 只是一段简单 DOM；JSX 不必要
 *  - 没有 JSX → node 可直接 import 跑 SSR 单测（renderToString fault injection），
 *    省得为单一测试拉 vitest / esbuild / tsx 任何额外构建链
 *
 * 用法：
 *   <AnalyticsErrorBoundary label="窗口分析模块">
 *     <SomeChartThatMightThrow />
 *   </AnalyticsErrorBoundary>
 *
 * 关键性质：
 *  - **只捕获子树渲染期 / 生命周期 / 构造函数中的同步异常**（React 错误边界标准语义）
 *  - 不捕获 useEffect / Promise / 事件回调里的异步异常 —— hook 自身的 error state
 *    才是正确处理路径（usePolling 已落实）
 *  - 「重试」按钮只清自己 state；不重新挂载父页面，**不影响其它区块**
 *  - 多个 boundary 平铺时**相互隔离**：一块炸不带垮另一块
 */
import React from 'react'

const wrapperStyle = {
  padding: 16,
  border: '1px solid #f0f0f0',
  borderRadius: 8,
  background: '#fafafa',
  color: '#595959',
}

const titleStyle = { fontWeight: 600, marginBottom: 8 }
const subStyle = { color: '#8c8c8c', fontSize: 12, marginBottom: 12 }
const buttonStyle = {
  padding: '4px 14px',
  border: '1px solid #d9d9d9',
  borderRadius: 6,
  background: '#fff',
  cursor: 'pointer',
  fontSize: 13,
}

export default class AnalyticsErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, error: null }
    this.handleRetry = this.handleRetry.bind(this)
  }

  static getDerivedStateFromError(error) {
    // React 18+ 静态钩子：React 在 commit 前调用，用返回值合并 state
    return { hasError: true, error }
  }

  componentDidCatch(error, info) {
    // 不抑制错误：开发态控制台可见，方便定位
    if (typeof console !== 'undefined' && console.warn) {
      console.warn(
        '[AnalyticsErrorBoundary] caught:',
        error && error.message,
        info && info.componentStack,
      )
    }
  }

  handleRetry() {
    this.setState({ hasError: false, error: null })
  }

  render() {
    if (!this.state.hasError) return this.props.children

    const label = this.props.label || '窗口分析模块'
    return React.createElement(
      'div',
      { style: wrapperStyle, role: 'alert' },
      React.createElement('div', { style: titleStyle }, `${label}暂时不可用`),
      React.createElement(
        'div',
        { style: subStyle },
        '本块内部出错；其它页面区块不受影响。可点击重试或等待下一轮自动刷新。',
      ),
      React.createElement(
        'button',
        { type: 'button', onClick: this.handleRetry, style: buttonStyle },
        '重试',
      ),
    )
  }
}
