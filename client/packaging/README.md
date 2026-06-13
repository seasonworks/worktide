# EmployeeAgent 打包与部署

> 透明的企业合规监控工具。请仅部署在公司所有的设备上，并事先告知员工。
> 本方案不做隐藏进程、反检测、加壳免杀、权限提升或驱动级功能。

## 一、打包（onedir）

在 `client/` 目录下，使用已安装依赖的虚拟环境：

```powershell
cd client
.\.venv\Scripts\activate
pip install -r requirements.txt      # 含 pyinstaller（仅打包用）

pyinstaller packaging\EmployeeAgent.spec --clean --noconfirm
```

产物：`client\dist\EmployeeAgent\`（整个文件夹一起分发）
入口：`client\dist\EmployeeAgent\EmployeeAgent.exe`

特点：onedir、无控制台窗口（`console=False`）、禁用 UPX、带版本信息、无图标（后续可补 `.ico`）。

## 二、部署目录约定

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| 程序本体 | `C:\Program Files\EmployeeAgent\` | 只读，安装时管理员写入 |
| 配置 | `C:\ProgramData\EmployeeAgent\config.json` | 可写，机器级 |
| 日志 | `C:\ProgramData\EmployeeAgent\logs\agent.log` | 可写，轮转 1MB×3 |

程序通过 `app/config.py` 的 `data_dir()` 自动定位 `%PROGRAMDATA%\EmployeeAgent`；
找不到配置时回退到 EXE 同级目录（便于便携/调试）。

## 三、安装（管理员一次性）

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\install.ps1
```

`install.ps1` 会做：
0. **升级路径预处理**（Phase 5.1）：若已有同名任务/进程在跑，先 `Stop-ScheduledTask` + `Stop-Process` 再拷贝，避免文件锁。in-flight 未持久化的最后一个 flush 周期（默认 15s）样本可能丢失；`window_buffer.db` 设计抗 crash，pending/uploaded 数据保留。
1. `C:\Program Files\EmployeeAgent\`（复制 onedir 程序）
2. `C:\ProgramData\EmployeeAgent\` + `logs\`，并给 **Users 组 Modify** 权限
3. `C:\ProgramData\EmployeeAgent\config.json`（默认配置，已存在则不覆盖）
4. 计划任务 `EmployeeAgent`：**登录触发 + 30 秒延迟启动 + 失败重启（3 次 / 1 分钟）**，以 **Users 组、非提权（Limited）** 运行，`WorkingDirectory` 固定为 InstallDir，无执行时限

安装后修改 `config.json` 的 `server_url` / `employee_name`，下次登录自动启动；
或立即启动：`Start-ScheduledTask -TaskName EmployeeAgent`。

### Phase 5.1 · Single Instance / Startup Hardening

Agent 启动时通过 Win32 命名互斥体 `Global\EmployeeAgent` 强制**整机唯一**：

- Task 已拉起一份 + 用户又手动双击 EXE → 第二份立即退出，日志写 `another instance detected, exiting`
- 进程崩溃 / Kill -9 / 断电不会留死锁（OS 自动释放 mutex handle）
- 实现见 [client/app/single_instance.py](../app/single_instance.py)；单测 `python tests/verify_phase5_1.py`

## 四、卸载

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\uninstall.ps1            # 保留数据
powershell -ExecutionPolicy Bypass -File .\packaging\uninstall.ps1 -RemoveData # 一并删除数据
```

卸载顺序：`Unregister-ScheduledTask` → `Stop-Process` → 删 `C:\Program Files\EmployeeAgent\` →（可选）删 `C:\ProgramData\EmployeeAgent\` → **Phase 5.1 R5**：清扫 HKLM/HKCU\Run 与 Startup folder 中可能的 `EmployeeAgent` 残留（兜底，本项目不使用这些位置）。

## 五、降低杀毒软件误报（合法手段，非免杀对抗）

PyInstaller 程序常被启发式误报。建议按优先级：

1. **代码签名（最有效）**：用 Authenticode 证书 + `signtool` 签名 `EmployeeAgent.exe`。
   - 内部分发：OV 证书通常足够；纯内网可用企业内部 CA。
   - 公网/避免 SmartScreen 警告：EV 证书可获即时信誉。
   - *本阶段仅说明，未实施。*
2. **企业侧显式放行**：通过 Intune / GPO / Microsoft Defender ASR 排除，按
   “发布者 + 安装路径”加白。公司拥有终端，这是正道——而非让程序躲检测。
3. 已在打包层面做的：onedir（非自解压）、**禁用 UPX**、带版本信息、诚实命名。
4. 仍误报时向 Microsoft 提交**误报申诉**（Defender Security Intelligence 提交门户）。
5. 进阶：从源码重新编译 PyInstaller bootloader，改变静态特征。

## 六、明确不做（红线）

- 不隐藏进程/窗口、不伪装系统进程名、不做隐藏自启
- 不做反调试 / 反虚拟机 / 反检测
- 不加壳 / 不加密 payload 免杀
- 不注入进程、不 hook、不装驱动
- **不记录按键内容**（仅采集“空闲时长”）
- 不请求管理员 / 不 UAC 提权运行
- 不偷偷添加 Defender 排除项 / 不篡改杀软
- 不自网络下载并执行代码（更新走企业分发整包替换）

## 七、Windows 10 / 11 与兼容性

- 在 **Win10 x64** 上构建以获得最佳向上兼容；运行支持 Win10/11 x64。
- ARM64（部分 Win11）暂不支持。
- 更新机制（预留）：客户端后续可在上报中带版本号，后台识别待升级机器；
  升级动作交由企业分发工具（Intune/SCCM/GPO）整包替换，客户端不自下载执行。
