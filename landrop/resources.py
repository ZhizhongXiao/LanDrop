"""Resolve bundled read-only resources in source and PyInstaller builds."""

from __future__ import annotations

from pathlib import Path
import sys


def resource_path(relative_path: str | Path) -> Path:
    bundle_root = getattr(sys, "_MEIPASS", None)
    root = Path(bundle_root) if bundle_root else Path(__file__).resolve().parents[1]
    return root / Path(relative_path)
