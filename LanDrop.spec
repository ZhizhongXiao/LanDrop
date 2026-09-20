# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project_root = Path(SPECPATH)
datas = [
    (str(project_root / "assets" / "LanDrop.ico"), "assets"),
    (str(project_root / "assets" / "LanDrop-icon-preview.png"), "assets"),
    (str(project_root / "assets" / "LanDrop-tray.ico"), "assets"),
    (str(project_root / "ui"), "ui"),
]
binaries = []
hiddenimports = collect_submodules("windows_toasts") + collect_submodules("winrt")

analysis = Analysis(
    [str(project_root / "desktop.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="LanDrop",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "assets" / "LanDrop.ico"),
)

collection = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LanDrop",
)
