"""Phase 6.0D · Device Identity Hardening.

Pre-6.0D 行为：``get_machine_id()`` 直接读 ``HKLM\\SOFTWARE\\Microsoft\\
Cryptography\\MachineGuid``。RCA (6.0C) 结论：未 sysprep 的 Ghost 克隆会
共用同一 MachineGuid，服务端 ``employees.machine_id UNIQUE`` 让所有克隆机
挤进同一行，时间线相互污染。

6.0D 行为：UUID4 持久化在 ``C:\\ProgramData\\EmployeeAgent\\machine.json``
（位于卸载保留目录，Agent 升级/重装 / Windows 重启都保留），并在首启时
通过 WMI 读取硬件指纹做克隆侦测。文件契约见 §1，迁移时序见 §2。

§1 machine.json 契约（schema=1）::

    {
      "machine_id":   "<uuid4 hex, 32 lowercase chars>",
      "generated_at": "<UTC ISO 8601 (秒精度，'Z' 后缀)>",
      "schema":       1
    }

§2 首启迁移时序（见 6.0D-Design §8）：首次启动时本模块返回的
``MachineIdentity.legacy_machine_id`` 是旧 MachineGuid（若可读到），
Reporter 把它放进 ``ActivityReport.legacy_machine_id`` 字段上报；服务端
``crud._try_migrate_legacy`` 据此把旧员工行的 ``machine_id`` 改写为本机
新的 uuid4，保留所有历史关联（activity_logs、windows、device_health）。

§3 hw_fingerprint 算法：``sha256(CSP.UUID|BIOS.SN|BaseBoard.SN)[:32]``。
所有源都为空（非 Windows / WMI 不可达）→ 空串，server 当作"无法采集"
而非"克隆"。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

logger = logging.getLogger(__name__)

_MACHINE_JSON_SCHEMA = 1
_HW_FP_LEN = 32
# 三次 WMI 调用合并成一个 PowerShell 进程；冷启 ~300ms，比 3 次串行省 ~600ms
_HW_FP_TIMEOUT_SECONDS = 10.0
# CREATE_NO_WINDOW = 0x08000000；旧 Python 没有这个常量时 fallback 到 0
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class MachineIdentity:
    """本机身份四件套，由 Agent.__init__ 注入到所有 Reporter。

    machine_id        本机唯一标识（uuid4 hex）。所有上报都用它作主键。
    hw_fingerprint    硬件指纹（sha256 截断 32 char）。空串=无法采集。
    legacy_machine_id 仅在 machine.json 刚被本进程生成时为旧 MachineGuid，
                      否则为 None。Reporter 据此决定是否随 payload 上报。
    source            "cache" | "generated"，仅供日志/排查用。
    """

    machine_id: str
    hw_fingerprint: str
    legacy_machine_id: str | None
    source: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_hostname() -> str:
    """读取主机名，OSError 时返回 "unknown"。"""
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


def get_machine_identity() -> MachineIdentity:
    """Phase 6.0D 主入口。Agent.__init__ 调用一次。

    缓存命中 → 返回 (cached_uuid, hw_fp, None, "cache")
    首启 / 文件损坏 → 生成新 uuid4，写文件，附 legacy_machine_id 用于迁移
    """
    path = _machine_json_path()
    hw_fp = _compute_hw_fingerprint()
    cached = _read_machine_json(path)
    if cached is not None:
        return MachineIdentity(
            machine_id=cached,
            hw_fingerprint=hw_fp,
            legacy_machine_id=None,
            source="cache",
        )
    legacy = _read_windows_machineguid()
    new_id = uuid.uuid4().hex
    try:
        _write_machine_json_atomic(path, new_id)
    except OSError as e:
        # 写失败：本进程继续用刚生成的 uuid4，下次启动会重生（可能拿到第二个 uuid4）。
        # 罕见且容易诊断；不阻塞 Agent 启动。
        logger.error(
            "写入 %s 失败：%s。本次进程内使用 uuid=%s，下次启动可能重生。",
            path, e, new_id,
        )
    return MachineIdentity(
        machine_id=new_id,
        hw_fingerprint=hw_fp,
        legacy_machine_id=legacy,
        source="generated",
    )


def get_machine_id() -> str:
    """向后兼容 wrapper：返回 machine_id 字符串。

    新代码（Agent / Reporter / WindowUploader / HealthReporter）请改调
    ``get_machine_identity()``。此 wrapper 保留：
      - 旧 import 路径不破坏
      - 仍然走 machine.json 缓存与 uuid4 生成逻辑（**不再**直接返回 MachineGuid）
    """
    return get_machine_identity().machine_id


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _machine_json_path() -> Path:
    """``C:\\ProgramData\\EmployeeAgent\\machine.json``。

    用 %PROGRAMDATA% env 取根，避免硬编码 C 盘；非 Windows fallback 到
    /tmp，方便源码运行/单测在 Linux/macOS 下也走通（hw_fp 会是空串）。
    """
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "EmployeeAgent" / "machine.json"
    # 源码运行 / 非 Windows
    return Path.home() / ".employee_agent" / "machine.json"


def _is_valid_uuid_hex(s: object) -> bool:
    if not isinstance(s, str) or len(s) != 32:
        return False
    try:
        uuid.UUID(hex=s)
    except (ValueError, TypeError):
        return False
    return True


def _read_machine_json(path: Path) -> str | None:
    """文件不存在 / schema 不符 / 解析失败 → None（调用方会重生）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (PermissionError, OSError) as e:
        logger.warning("读取 %s 失败：%s，将重生", path, e)
        return None
    try:
        data = json.loads(raw)
    except ValueError as e:
        logger.warning("%s 解析失败 (%s)，将重生", path, e)
        return None
    schema = data.get("schema") if isinstance(data, dict) else None
    mid = data.get("machine_id") if isinstance(data, dict) else None
    if schema != _MACHINE_JSON_SCHEMA:
        logger.warning(
            "%s schema=%r 不支持（仅 %d），将重生",
            path, schema, _MACHINE_JSON_SCHEMA,
        )
        return None
    if not _is_valid_uuid_hex(mid):
        logger.warning("%s 的 machine_id 不是合法 uuid4 hex，将重生", path)
        return None
    return mid


def _write_machine_json_atomic(path: Path, machine_id: str) -> None:
    """原子写：先写 .tmp 再 os.replace，避免半写损坏文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "machine_id": machine_id,
        "generated_at": _iso_utc_now(),
        "schema": _MACHINE_JSON_SCHEMA,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _iso_utc_now() -> str:
    """UTC ISO 8601 秒精度 'Z' 后缀；与 health_reporter._iso 口径一致。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_windows_machineguid() -> str | None:
    """读取 ``HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid``。

    仅 Windows；非 Windows / ACL 拒绝 → None。
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
        ) as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value) if value else None
    except OSError:
        return None


def _compute_hw_fingerprint() -> str:
    """``sha256(CSP.UUID|BIOS.SN|BaseBoard.SN)[:32]``。

    三源任一非空 → 返回 sha256 截断结果。全空（非 Windows / WMI 不可达）→
    返回空串，server 当作"无法采集"，不计入克隆判定。
    """
    if sys.platform != "win32":
        return ""
    # 合并 1 个 PowerShell 进程取 3 个字段：相比串行 3 次省 ~600ms 冷启
    expr = (
        "$ErrorActionPreference = 'SilentlyContinue';"
        "$csp = (Get-CimInstance Win32_ComputerSystemProduct).UUID;"
        "$bios = (Get-CimInstance Win32_BIOS).SerialNumber;"
        "$bd = (Get-CimInstance Win32_BaseBoard).SerialNumber;"
        "Write-Output (\"{0}|{1}|{2}\" -f $csp, $bios, $bd)"
    )
    out = _query_powershell(expr)
    if not out:
        return ""
    parts = out.splitlines()[-1].strip()  # 取最后一非空行，防 PS 警告噪音
    if not parts or parts == "||":
        return ""
    return sha256(parts.encode("utf-8")).hexdigest()[:_HW_FP_LEN]


def _query_powershell(expr: str, timeout: float = _HW_FP_TIMEOUT_SECONDS) -> str:
    """运行 PowerShell 表达式，返回 stdout（已 strip）。失败返回 ""。

    - ``-NoProfile -NonInteractive`` 避免加载 profile / 等待交互
    - ``CREATE_NO_WINDOW`` 不弹控制台
    - timeout/异常 → 空串，调用方自行降级
    """
    if sys.platform != "win32":
        return ""
    cmd = [
        "powershell.exe", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-Command", expr,
    ]
    try:
        result = subprocess.run(  # noqa: S603 cmd 来自常量
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.warning("hw_fingerprint PowerShell 超时（%.1fs）", timeout)
        return ""
    except (FileNotFoundError, OSError) as e:
        logger.warning("hw_fingerprint PowerShell 调用失败：%s", e)
        return ""
    if result.returncode != 0:
        logger.warning(
            "hw_fingerprint PowerShell 退出码 %d：%s",
            result.returncode, (result.stderr or "")[:200],
        )
        return ""
    return (result.stdout or "").strip()
