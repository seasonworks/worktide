"""Phase 5.4 · Release manifest 管理。

职责：
- 扫描 ``<release_dir>/<version>/`` 找到所有可用版本
- 维护 ``<release_dir>/latest.json``（指向 channel 最新）
- 为某个 version 计算 / 读取 manifest（含 sha256 + HMAC signature）
- 流式 stream 出 zip 的 BinaryIO（供 router StreamingResponse 用）

设计要点：
- **HMAC signature** 的 canonical 字符串是 ``f"{version}|{sha256}|{size}"``。
  之所以这样组合（而不是签整个 manifest JSON）：客户端只需要拿到这三个字段
  即可独立校验，不依赖 JSON 字段顺序 / whitespace。
- **manifest 缓存**：sha256 计算对 5MB zip ~50ms，每次 check 都算太慢；
  ``<release_dir>/<version>/manifest.json`` 持久化避免重复算。首次访问
  自动生成；release 包变更（mtime 变）时自动重算。
- **latest.json** 只在 publish 时由 ops 脚本写。本模块只读不写
  （v1 不暴露 publish API；release 由管理员手动放置 + 维护 latest.json）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings
from ..schemas import UpdateManifestOut

logger = logging.getLogger(__name__)

MANIFEST_FILE_NAME = "manifest.json"
LATEST_POINTER_NAME = "latest.json"


# ---------------------------------------------------------------------------
# 路径 + HMAC 工具
# ---------------------------------------------------------------------------


def release_root() -> Path:
    """返回 release 根目录的绝对路径，确保存在。"""
    root = Path(settings.update_release_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _zip_name(version: str) -> str:
    return f"EmployeeAgent_v{version}.zip"


def _version_dir(version: str) -> Path:
    return release_root() / version


def _zip_path(version: str) -> Path:
    return _version_dir(version) / _zip_name(version)


def _manifest_path(version: str) -> Path:
    return _version_dir(version) / MANIFEST_FILE_NAME


def compute_hmac_signature(version: str, sha256: str, size: int) -> str:
    """HMAC-SHA256(secret, f"{version}|{sha256}|{size}")。

    与客户端 [client/app/update_downloader.py] 的 verify_hmac 严格对齐。
    任何 canonical 字符串改动都要双向同步并 bump min_compat_version。
    """
    canonical = f"{version}|{sha256}|{size}".encode("utf-8")
    secret = settings.update_hmac_secret.encode("utf-8")
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def compute_sha256_of_file(path: Path) -> str:
    """流式算 sha256；单次读 1 MiB，对 5MB zip ~50 ms 完成。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# manifest 缓存
# ---------------------------------------------------------------------------


def _build_manifest_for_zip(version: str, zip_path: Path) -> dict:
    """从 zip 文件 + 配置构造 manifest dict（未来写入磁盘 / 返回给 client）。"""
    sha256 = compute_sha256_of_file(zip_path)
    size = zip_path.stat().st_size
    signature = compute_hmac_signature(version, sha256, size)
    return {
        "version": version,
        "channel": "stable",
        "sha256": sha256,
        "signature": signature,
        "size": size,
        "released_at": datetime.fromtimestamp(
            zip_path.stat().st_mtime, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_compat_version": "0.5.0",
        "download_url": f"{settings.api_v1_prefix}/agent/updates/download/{version}",
        "changelog": "",
    }


def get_manifest(version: str) -> Optional[dict]:
    """读取或生成 manifest。zip 不存在 → ``None``。

    缓存策略：磁盘 manifest.json 与 zip mtime 比对；
    - manifest 不存在 → 生成
    - zip 比 manifest 新（重新发布）→ 重生成
    - 否则直接读
    """
    zip_path = _zip_path(version)
    if not zip_path.is_file():
        return None
    mf_path = _manifest_path(version)
    if mf_path.is_file():
        try:
            zip_mtime = zip_path.stat().st_mtime
            mf_mtime = mf_path.stat().st_mtime
            if mf_mtime >= zip_mtime:
                data = json.loads(mf_path.read_text(encoding="utf-8"))
                # changelog 等可被人工编辑后保留；其余强一致字段以磁盘为准
                return data
        except Exception:  # noqa: BLE001
            logger.warning("manifest.json 损坏，重新生成 (version=%s)", version)

    data = _build_manifest_for_zip(version, zip_path)
    try:
        mf_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        logger.warning("manifest.json 写盘失败 (version=%s)：%s", version, e)
    return data


# ---------------------------------------------------------------------------
# latest 指针
# ---------------------------------------------------------------------------


def _latest_pointer_path() -> Path:
    return release_root() / LATEST_POINTER_NAME


def get_latest_version(channel: str = "stable") -> Optional[str]:
    """从 latest.json 取 channel 当前最新版本号。

    latest.json schema::

        { "stable": "0.5.4", "beta": "0.5.5-beta1" }

    文件不存在 / 字段缺失 → ``None``（视为"还没发布过任何版本"）
    """
    p = _latest_pointer_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("latest.json 解析失败")
        return None
    ver = data.get(channel)
    return str(ver) if ver else None


# ---------------------------------------------------------------------------
# 对外 service API
# ---------------------------------------------------------------------------


def check_update(
    current_version: str, channel: str = "stable"
) -> tuple[str, bool, Optional[UpdateManifestOut]]:
    """供 router 调用：返回 (latest_version, update_available, manifest)。

    - channel 未支持 → 返回 current 自身，update_available=False
    - 没有任何发布版本 → 同上
    - latest 与 current 相同 → update_available=False
    - **Phase 6.1B**: latest **不严格大于** current（包括降级 / 相等）
      → update_available=False。这是服务端的 anti-downgrade guard，对应
      [client/app/update_pipeline.py] 里同名守卫；两端必须得出**相同结论**。
    - latest > current 但 manifest 缺失 → update_available=False（自我保护）
    """
    if channel not in settings.update_supported_channels:
        return current_version, False, None

    latest = get_latest_version(channel)
    if latest is None:
        return current_version, False, None

    if latest == current_version:
        return latest, False, None

    # ---------------------------------------------------------------------
    # Phase 6.1B · server-side ANTI-DOWNGRADE GUARD
    #
    # 6.0D-V dogfood 实测：测试机 0.6.1 自降级到 0.6.0 — 因为 server 这里
    # 只查 `latest == current` 跳过；不等就发 manifest，client 无脑装。
    # 历史"装 0.6.0 后台显示 0.5.4"也很可能是同源（Cloudflare 缓存窗口里
    # latest.json 短暂滞后到旧版本）。
    #
    # 守卫规则：latest 必须**严格大于** current；同时还要走语义比较，避免
    # 字符串字典序错把 0.10.0 当成 0.9.0 之前的版本。
    # ---------------------------------------------------------------------
    from . import semver
    if not semver.is_upgrade(current_version, latest):
        logger.warning(
            "anti-downgrade · client current=%s latest=%s · returning no-update",
            current_version, latest,
        )
        return latest, False, None

    raw = get_manifest(latest)
    if raw is None:
        logger.warning(
            "latest=%s 在 latest.json 中存在但 zip 缺失，跳过", latest
        )
        return current_version, False, None

    try:
        manifest = UpdateManifestOut(**raw)
    except Exception:  # noqa: BLE001
        logger.exception("manifest 构造失败 (version=%s)", latest)
        return current_version, False, None

    return latest, True, manifest


def get_download_path(version: str) -> Optional[Path]:
    """供 router 流式下载用。版本不存在返回 None；存在返回 zip 的绝对路径。"""
    p = _zip_path(version)
    if not p.is_file():
        return None
    return p
