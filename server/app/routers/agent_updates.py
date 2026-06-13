"""Phase 5.4 · Auto Update API。

3 个 endpoint:
- ``GET /api/v1/agent/updates/check``        客户端轮询有没有新版
- ``GET /api/v1/agent/updates/manifest/{ver}`` 独立取 manifest（用于校验）
- ``GET /api/v1/agent/updates/download/{ver}`` 流式下载 zip

与 [device_health.py] 同口径：D2 无鉴权；schema 在 server 单点维护。
"""
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..schemas import UpdateCheckOut, UpdateManifestOut
from ..services import device_health as device_health_service
from ..services import release_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent/updates", tags=["agent-updates"])

# version 仅允许 semver x.y.z，杜绝把 "../" 等带进文件路径（manifest/download
# 用 version 拼 <release_dir>/<version>/...）。免鉴权端点的防御性校验。
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _validate_version(version: str) -> None:
    if not _VERSION_RE.fullmatch(version):
        raise HTTPException(status_code=400, detail="非法版本号")


@router.get(
    "/check",
    response_model=UpdateCheckOut,
    summary="客户端轮询：有没有比 current_version 更新的版本",
)
def check_update(
    current_version: str = Query(..., description="客户端当前版本，如 0.5.3"),
    channel: str = Query("stable", description="发布通道；v1 仅 stable"),
    machine_id: str | None = Query(None, description="可选；服务端可用于审计"),
    db: Session = Depends(get_db),
) -> UpdateCheckOut:
    """返回 latest_version + manifest（如果有 update_available）。

    Side effect：如果带了 machine_id 且能匹配到 device_health row，
    顺手更新 ``update_last_check_at``（client 不必额外 POST）。
    """
    latest, available, manifest = release_manager.check_update(
        current_version, channel=channel
    )

    if machine_id:
        try:
            device_health_service.touch_update_last_check(db, machine_id)
        except Exception:  # noqa: BLE001  辅助字段，不影响主响应
            logger.exception(
                "touch_update_last_check 失败 machine_id=%s", machine_id
            )

    return UpdateCheckOut(
        current_version=current_version,
        latest_version=latest,
        update_available=available,
        manifest=manifest,
    )


@router.get(
    "/manifest/{version}",
    response_model=UpdateManifestOut,
    summary="单独取某版本的 manifest（含 sha256 + HMAC signature）",
)
def get_manifest(version: str) -> UpdateManifestOut:
    _validate_version(version)
    raw = release_manager.get_manifest(version)
    if raw is None:
        raise HTTPException(
            status_code=404, detail=f"version {version} not found"
        )
    try:
        return UpdateManifestOut(**raw)
    except Exception as e:  # noqa: BLE001
        logger.exception("manifest 构造失败 version=%s", version)
        raise HTTPException(status_code=500, detail=f"bad manifest: {e}")


@router.get(
    "/download/{version}",
    summary="流式下载升级包（zip）",
)
def download_zip(version: str) -> FileResponse:
    _validate_version(version)
    path: Path | None = release_manager.get_download_path(version)
    if path is None:
        raise HTTPException(
            status_code=404, detail=f"download not found for {version}"
        )
    # FileResponse 自带 Content-Length + sendfile 优化
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )
