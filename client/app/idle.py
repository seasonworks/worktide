"""通过 Windows API 检测键盘/鼠标空闲时间。

使用 user32.GetLastInputInfo 获取最近一次输入的系统滴答时间，
与当前滴答相减得到空闲时长。dwTime 基于 32 位 GetTickCount，
因此用 GetTickCount 相减并做 32 位掩码以正确处理回绕。
"""
import ctypes
from ctypes import wintypes


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_seconds() -> float:
    """返回当前已连续空闲的秒数（无键鼠输入的时长）。"""
    info = _LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0

    tick = ctypes.windll.kernel32.GetTickCount()
    elapsed_ms = (tick - info.dwTime) & 0xFFFFFFFF
    return elapsed_ms / 1000.0
