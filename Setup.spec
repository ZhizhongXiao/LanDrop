# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH)
payload_root = project_root / "build" / "setup-payload"
manifest_path = project_root / "build" / "payload-manifest.json"
if not payload_root.is_dir() or not manifest_path.is_file():
    raise SystemExit("Setup payload is missing; run scripts/build-installer.ps1.")

analysis = Analysis(
    [str(project_root / "setup.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[
        (str(project_root / "assets" / "LanDrop.ico"), "assets"),
        (str(project_root / "assets" / "LanDrop-icon-preview.png"), "assets"),
        (str(project_root / "ui"), "ui"),
        (str(project_root / "scripts" / "shortcut-bridge.ps1"), "scripts"),
        (str(payload_root), "setup_payload"),
        (str(manifest_path), "."),
    ],
    hiddenimports=[],
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
    analysis.binaries,
    analysis.datas,
    [],
    name="LanDrop-Setup",
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
