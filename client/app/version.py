"""Phase 5.3 · Agent 版本号（D5 决策：常量为真值来源，version_file.txt 作交叉校验）。

为什么不直接从 PyInstaller 嵌入的 PE 资源读？
- 源码运行时根本没有 PE 资源，会 fallback 到默认值，难以区分"未配置"和"被改"
- 这里**常量是真值来源**；version_file.txt（PyInstaller 打包元数据）应当
  与之保持一致，打包时通过 packaging/EmployeeAgent.spec → version_file.txt
  脚本手动同步（README 已注明）

字符串格式：``"<major>.<minor>.<patch>"`` 不带 v 前缀。与 git tag 用法对齐：
  - 代码常量：``0.5.3``
  - git tag：``v0.5.3-health``
"""
from __future__ import annotations

#: 唯一真值来源。改这里 → 改 packaging/version_file.txt → 再 git tag。
# Phase 6.0D · 设备身份硬化（machine.json + hw_fingerprint）— semver minor bump
# 表示语义破坏：machine_id 不再来自 MachineGuid，禁止回滚到 0.5.x。
# Phase 6.1A · Agent Reliability Phase 1（自启动加固 + 外部 Recovery Task +
# Defender 排除 + firstboot retry）— 纯客户端可靠性增强，patch 级，
# 不破坏 6.0D 身份与上报路径，0.6.0 可平滑 in-place 升级到 0.6.1。
# Phase 6.1B · Auto Update Hardening（anti-downgrade guard 双端 + semver
# 比较 + update_history.jsonl audit log）。Phase 6.4A · Device Enrollment
# Lite（Inno wizard 收 employee_name + 服务端 name 保护）。0.6.1 可 in-place
# 升级到 0.6.2，且 0.6.2 拒绝降级回 0.6.x 或 0.5.x。
AGENT_VERSION = "0.6.2"
