"""Safe Phase 8B Uninstall artifact; destructive uninstall is intentionally absent."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

from .platform_checks import webview2_runtime_version
from .resources import resource_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanDrop Uninstall")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt":
        return 1
    runtime_version = webview2_runtime_version()
    if not runtime_version:
        _show_native_error(
            "未检测到 Microsoft Edge WebView2 Runtime；"
            "请重新安装或修复 WebView2 Runtime 后再次卸载 LanDrop。"
        )
        return 2
    entry = resource_path("ui/uninstall/index.html")
    required = (
        entry,
        resource_path("ui/uninstall/js/uninstall.js"),
        resource_path("ui/wizard/js/wizard.js"),
        resource_path("ui/wizard/css/wizard.css"),
    )
    if any(not path.is_file() for path in required):
        _show_native_error("LanDrop Uninstall 界面资源不完整。")
        return 3
    if args.self_check:
        return 0

    try:
        import webview
    except ImportError:
        _show_native_error("LanDrop Uninstall 缺少 pywebview 运行组件。")
        return 3
    webview.create_window(
        "卸载 LanDrop",
        url=entry.as_uri(),
        width=920,
        height=620,
        min_size=(760, 560),
        resizable=True,
        background_color="#eaf5ff",
    )
    webview.start(gui="edgechromium", debug=False, private_mode=True)
    return 0


def _show_native_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "LanDrop Uninstall", 0x10)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        print(message, file=sys.stderr)
