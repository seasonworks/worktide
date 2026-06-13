"""Phase 6.0D-R · build_release.py — package + sign + manifest.

Run from client/ with .venv active:

    python packaging/build_release.py 0.6.0

Produces under client/dist/:
    EmployeeAgent_v<version>.zip       (contents of dist/EmployeeAgent/ zipped)
    manifest_v<version>.json            (matches server UpdateManifestOut schema)

Computes:
    sha256(zip)
    HMAC-SHA256(secret, f"{version}|{sha256}|{size}")  signature

Uses HMAC secret from env UPDATE_HMAC_SECRET; defaults to the project's
shared dev value (matches both server/.env and client config.json default).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HMAC_SECRET = "change-me-in-production"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: build_release.py <version>")
        return 2
    version = sys.argv[1]

    client_dir = Path(__file__).resolve().parents[1]
    src = client_dir / "dist" / "EmployeeAgent"
    if not src.is_dir():
        print(f"ERROR: source dir not found: {src}")
        return 1

    zip_path = client_dir / "dist" / f"EmployeeAgent_v{version}.zip"
    manifest_path = client_dir / "dist" / f"manifest_v{version}.json"

    # 1) zip — entries keyed as EmployeeAgent/...  (matches 0.5.4 layout)
    n = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(src):
            for f in files:
                full = Path(root) / f
                arc = full.relative_to(client_dir / "dist").as_posix()
                z.write(full, arc)
                n += 1
    size = zip_path.stat().st_size

    # 2) sha256
    h = hashlib.sha256()
    with zip_path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()

    # 3) HMAC signature: canonical = "version|sha256|size"
    secret = os.environ.get("UPDATE_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    canonical = f"{version}|{sha}|{size}"
    sig = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"),
                   hashlib.sha256).hexdigest()

    # 4) manifest — schema mirrors server UpdateManifestOut
    manifest = {
        "version": version,
        "channel": "stable",
        "sha256": sha,
        "signature": sig,
        "size": size,
        "released_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_compat_version": "0.5.0",
        "download_url": f"/api/v1/agent/updates/download/{version}",
        "changelog": (
            "Phase 6.1B + 6.4A. "
            "6.1B Auto Update Hardening: client + server semver anti-downgrade "
            "guard（0.6.2 拒装任何 < 0.6.2，0.10.0 > 0.9.0 正确）+ update_history "
            ".jsonl audit log。修复 6.0D-V 期间被 dogfood 抓到的自降级 bug。 "
            "6.4A Device Enrollment Lite: Inno installer 新增 Employee Name "
            "向导页（必填、双语 UI、UTF-8 文件传递），post-install 写入 "
            "config.json 并保护已有 admin 改名（不覆盖）。Auto Update 路径 "
            "保持向兼容，旧机不弹向导。machine.json / hw_fingerprint / 状态机 / "
            "Telegram / Admin / 数据库 schema 完全不变。"
        ),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    print(f"zip      : {zip_path}")
    print(f"  size   : {size} bytes ({size/1024/1024:.2f} MiB)")
    print(f"  files  : {n}")
    print(f"  sha256 : {sha}")
    print(f"  sig    : {sig}")
    print(f"manifest : {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
