"""Phase 5.1 · 单实例锁

通过 Win32 命名互斥体 ``Global\\EmployeeAgent`` 保证整机只跑一份 agent。

主要应对场景：
- Scheduled Task 已经把 agent 拉起，用户又手动双击 EXE → 第二份必须立即退
- 否则两个进程会同时写 ``window_buffer.db``，SQLite WAL 在多写者下会竞态

实现选择：
- 用 ``ctypes.windll.kernel32`` 直调 ``CreateMutexW``，和 [idle.py] /
  [window_tracker.py] 风格一致；不引入 pywin32（项目原本不依赖）
- ``Global\\`` 前缀让所有 Terminal Services 会话共享同一命名空间 → 真正"整机一份"
- 决策：不带 SID 后缀（不区分用户），快速用户切换 / RDP 场景里只跑第一个 acquire 成功的那份

进程退出时 OS 自动释放 handle，断电 / Kill -9 / TerminateProcess 都不会留死锁。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import Optional

# CreateMutexW 在 mutex 已存在时返回的 GetLastError 值
_ERROR_ALREADY_EXISTS = 183

#: 命名 mutex 名。"Global\\" 前缀让所有会话共享。
MUTEX_NAME = "Global\\EmployeeAgent"

# 非 Windows 平台（开发机 / CI 上跑单元测试）时 ``ctypes.windll`` 不存在；
# 用 getattr 兜底，让 import 不挂，acquire() 时再判断。
_kernel32 = getattr(ctypes, "windll", None) and ctypes.windll.kernel32

if _kernel32 is not None:
    # 不显式声明 argtypes/restype 时，Python 会按 int 处理 64-bit handle，
    # 在 x64 上会截断；这里照 window_tracker.py 的做法把签名钉死。
    _kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,    # lpMutexAttributes (NULL)
        wintypes.BOOL,      # bInitialOwner
        wintypes.LPCWSTR,   # lpName
    ]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE

    _kernel32.GetLastError.argtypes = []
    _kernel32.GetLastError.restype = wintypes.DWORD

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL


class SingleInstance:
    """命名互斥体的薄包装。

    用法::

        lock = SingleInstance()
        if not lock.acquire():
            return  # 已有实例在跑
        # ... 主循环 ...

    **重要**：``lock`` 对象的引用必须保留到进程结束。一旦 GC 把它收掉，
    ``CloseHandle`` 会被调用，mutex 的内核引用计数归零后 OS 销毁它，
    下一个 agent 启动就能 acquire 成功——出现两份 agent 并存。
    在 [main.py] 里直接绑到 main() 的本地变量足够，因为 Agent.run()
    阻塞到进程退出，本地变量全程在栈上。
    """

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._name = name
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        """尝试拿锁。

        :return: ``True`` = 拿到了（我是这台机器唯一的 agent）；
                 ``False`` = 已经有人占着，调用方应当退出
        """
        if _kernel32 is None:
            # 非 Windows：单实例语义不适用，直接放行让单测能跑
            return True

        # bInitialOwner=False —— 不需要 mutex 的所有权（我们只用它作存在性标志），
        # 这样进程崩溃时 OS 自动释放就不必处理"abandoned mutex"信号。
        handle = _kernel32.CreateMutexW(None, False, self._name)
        last_err = _kernel32.GetLastError()

        # 关键 Windows 行为：CreateMutexW 在 ERROR_ALREADY_EXISTS 时
        # 仍然返回一个**有效** handle（指向已存在的那个 mutex）。
        # 这个 handle 也会延长 mutex 寿命，所以这条路径必须显式 CloseHandle，
        # 否则即使第二实例自己退出，第一份释放后 mutex 也不会被清——
        # 直接造成"第二实例死了还把锁留下"的诡异 bug。
        if last_err == _ERROR_ALREADY_EXISTS:
            if handle:
                _kernel32.CloseHandle(handle)
            return False

        if not handle:
            # 创建失败（权限缺失 / 句柄耗尽，罕见）。保守放行，
            # 让 agent 继续起来——单实例保护失效好过完全起不来。
            return True

        self._handle = handle
        return True

    def release(self) -> None:
        """显式释放。

        正常进程退出时 OS 会兜底，所以业务代码通常**不需要**调；
        单元测试要在一个进程内做"acquire → release → reacquire"才用得着。
        """
        if self._handle is not None and _kernel32 is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None
