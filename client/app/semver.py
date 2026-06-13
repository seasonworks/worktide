"""Phase 6.1B · 语义版本比较 (semver)

为什么单写一个？现有 update_pipeline 隐式用 ``!=`` 字符串比较版本，
这次 dogfood 实测发现：

  current=0.6.1, latest=0.6.0  →  字符串 0.6.1 != 0.6.0 → 触发"升级" → 实
  际是降级，把 0.6.1 装回 0.6.0。

字符串比较还有更隐蔽的雷：

  "0.10.0" < "0.9.0"   # 字符串字典序：'1' < '9'，会把 0.10.0 当成更老的
  "0.6.10" < "0.6.2"   # 同理：'1' < '2'，0.6.10 反而小

任何只看 ``!=`` / 字典序的比较都会出错。本模块把版本解析成
``(major, minor, patch)`` 三元组进行**整数比较**，彻底消除这两类雷。

本模块**独立于** stdlib / pydantic，可在 Python 3.10+ 直接 import；
设计上准备在客户端 + 服务端各自 import 同一份代码逻辑（服务端有镜像副本）。

只支持 ``MAJOR.MINOR.PATCH`` 三段；这是当前项目唯一在用的格式
（见 [client/app/version.py] / [server/app/services/release_manager.py]）。
不支持 pre-release / build-metadata 后缀（``-rc.1`` / ``+sha.abc``）— 加上
反而让默认行为更难预测。如果以后需要，专门加 ``parse_extended()``。
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

#: 三元组形如 (0, 6, 2)
SemverTuple = Tuple[int, int, int]


def parse(version: str) -> SemverTuple:
    """``"0.6.2"`` → ``(0, 6, 2)``。

    宽容策略：
    - 允许前导 ``v``（``"v0.6.2"`` OK）— 与 git tag 习惯对齐
    - 允许尾随空白
    - 字段必须能解析成 *非负* 整数；任何字段失败 → ``ValueError``

    严格策略：
    - **只接受 3 段**。``"0.6"`` / ``"0.6.2.0"`` 都拒绝 — 多出一段静默
      丢弃会掩盖 PE 资源（``"0.6.2.0"``）和源码常量（``"0.6.2"``）的不一致。
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
    """返回 ``-1`` 若 ``a < b``；``0`` 若 ``a == b``；``+1`` 若 ``a > b``。

    解析失败一律返回 ``0`` 并打 WARNING — 把"我不知道"映射成"相等"
    比映射成"小于/大于"更安全：调用方（``is_upgrade`` 等）会拒绝任何
    "相等"以外的行为，遇到坏版本号时 fail-safe 站住不动。
    """
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
    """``target`` 是否**严格大于** ``current``。

    只有这个返回 ``True`` 时，调用方才允许进入"下载 + 安装"。
    任何"相等"或"降级"路径都返回 ``False`` —— 这就是 Phase 6.1B
    的核心 guard：**没有合法降级路径**。
    """
    return compare(target, current) > 0
