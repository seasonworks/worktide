# 远程员工管理系统 · Windows 客户端

运行在员工电脑上的后台程序：检测键鼠空闲时间，每 30 秒上报到服务端。

> 合规提示：本程序属于企业监控工具，请仅部署在公司所有的设备上，并事先告知员工、取得同意。

## 目录结构

```
client/
├── main.py                # 入口：日志 + 启动循环
├── config.json            # 运行配置（实际使用）
├── config.example.json    # 配置模板
├── requirements.txt
└── app/
    ├── config.py          # 读取 config.json
    ├── idle.py            # ctypes 检测 Windows 空闲时间
    ├── machine.py         # 机器唯一标识 / 主机名
    ├── reporter.py        # requests 上报到 FastAPI
    ├── screenshot.py      # 预留：未来截图功能
    └── agent.py           # 主循环编排
```

## 配置（config.json）

| 字段 | 说明 |
| --- | --- |
| `server_url` | 服务端地址 |
| `api_path` | 上报接口路径，默认 `/api/v1/activity/report` |
| `report_interval_seconds` | 上报间隔，默认 30 秒 |
| `request_timeout_seconds` | 请求超时 |
| `employee_name` | 员工姓名（留空则服务端先用 machine_id 占位） |
| `screenshot.enabled` | 是否启用截图（预留，默认 false） |
| `screenshot.interval_seconds` | 截图间隔（预留） |

## 运行

```powershell
cd client
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

python main.py
```

启动后每 30 秒向服务端上报一次，日志写入 `client/agent.log`。
按 Ctrl+C 退出。

## 工作原理

- **空闲检测**：调用 `user32.GetLastInputInfo` 取最近一次键鼠输入时间，与
  `GetTickCount` 相减得到空闲秒数（`app/idle.py`）。
- **机器标识**：读取注册表 `MachineGuid` 作为稳定唯一 ID，取不到则用主机名
  （`app/machine.py`）。
- **上报**：`requests` POST 到 `/api/v1/activity/report`；服务端按空闲秒数判定
  online / idle，并自动注册员工（`app/reporter.py`）。

## 后续步骤（尚未实现）

- 系统托盘运行（pystray）
- 开机自启（注册表 Run 项 / 计划任务）
- 静默运行（pythonw / PyInstaller `--windowed`）
- 打包 EXE（PyInstaller）
- 截图上传（mss + Pillow，骨架已在 `app/screenshot.py` 预留）
