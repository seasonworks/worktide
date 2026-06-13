"""Phase 6.1B · 语义版本比较 (semver) — 服务端镜像。

与 [client/app/semver.py] **必须保持算法一致**：

  parse / compare / is_upgrade

服务端要单独维护一份，是因为 server 与 client 跑在不同 Python 进程
（Linux VPS / Windows agent），没有共享 import 路径。这份逻辑 30 行，
重复成本远低于"用 git submodule 共享一个 package"那种复杂度。

任何改 client semver 的，要同步改本文件，否则 server 的 anti-downgrade
guard 与 client 的 guard 会判定不一致 — 6.1B 防降级的核心就是两端
得出相同结论。
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

#: 三元组形如 (0, 6, 2)
SemverTuple = Tuple[int, int, int]


def parse(version: str) -> SemverTuple:
    """``"0.6.2"`` → ``(0, 6, 2)``。

    宽容：允许前导 ``v``、首尾空白。
    严格：只接受 3 段；非整数 / 负数 / 段数错 → ``ValueError``。
    """
    if not isinstance(version, str):
        raise ValueError(f"version must be str, got {type(version).__name__}")
    raw = version.strip()
    if raw.startswith("v") or raw.startswith("V"):
        raw = raw[1:]
    parts = raw.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"version must be MAJOR.MINOR.PATCH (3 parts), got {version!r}"
        )
    try:
        triple = tuple(int(p) for p in parts)
    except ValueError as e:
        raise ValueError(f"version parts must be integers: {version!r}") from e
    if any(p < 0 for p in triple):
        raise ValueError(f"version parts must be non-negative: {version!r}")
    return triple  # type: ignore[return-value]


def compare(a: str, b: str) -> int:
    """``a<b → -1`` / ``a==b → 0`` / ``a>b → +1``；解析失败一律返回 0
    （fail-safe — 调用方拒绝 "0" 以外的操作）。"""
    try:
        ta = parse(a)
        tb = parse(b)
    except ValueError as exc:
        logger.warning("semver.compare 解析失败 a=%r b=%r err=%s", a, b, exc)
        return 0
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def is_upgrade(current: str, target: str) -> bool:
    """``target`` 严格 > ``current`` 才返回 True。降级 / 相等都返回 False。"""
    return compare(target, current) > 0
