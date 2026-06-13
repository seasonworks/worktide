# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir、无控制台窗口、禁用 UPX、带版本信息。

构建（在 client/ 目录下，已激活含 pyinstaller 的虚拟环境）：
    pyinstaller packaging/EmployeeAgent.spec --clean --noconfirm

产物：client/dist/EmployeeAgent/EmployeeAgent.exe（onedir 整个文件夹分发）
"""
import os

# SPECPATH 由 PyInstaller 注入，为本 spec 所在目录（client/packaging）
_MAIN = os.path.join(SPECPATH, "..", "main.py")
_VERSION = os.path.join(SPECPATH, "version_file.txt")


a = Analysis(
    [_MAIN],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,       # onedir：二进制留给 COLLECT
    name="EmployeeAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # 禁用 UPX，显著降低杀软误报
    console=False,               # 无控制台窗口（后台运行，进程仍可见，非隐藏）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,            # 跟随当前 Python 架构（x64）
    codesign_identity=None,
    entitlements_file=None,
    version=_VERSION,            # 版本资源，诚实元数据有助降误报
    icon=None,                   # 暂不带图标，后续再补 .ico
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EmployeeAgent",
)
