"""Single-file LanDrop Uninstall UI and temporary executor entry point."""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys
import threading
from typing import Any

from .install_contract import install_paths_for_current_windows_user
from .platform_checks import webview2_runtime_version
from .resources import resource_path
from .system_integration import PowerShellShortcutBackend, WindowsFirstInstallIntegration
from .uninstaller import (
    UninstallLifecycleService,
    UninstallOutcome,
    UninstallSelection,
    launch_temporary_uninstaller,
    schedule_temp_self_cleanup,
)


class UninstallApi:
    """Allowlisted local bridge for either the installed or temporary process."""

    def __init__(
        self,
        service: UninstallLifecycleService,
        *,
        current_executable: Path,
        request_path: Path | None = None,
        nonce: str | None = None,
        expected_request_sha256: str | None = None,
        installed_version: str = "",
    ) -> None:
        self.service = service
        self.current_executable = current_executable
        self.request_path = request_path
        self.nonce = nonce
        self.expected_request_sha256 = expected_request_sha256
        self.installed_version = installed_version
        self._window: Any | None = None
        self._guard = threading.Lock()
        self._state_lock = threading.Lock()
        self._operation: dict[str, object] | None = None

    @property
    def temporary_mode(self) -> bool:
        return self.request_path is not None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def get_context(self) -> dict[str, object]:
        return {
            "ok": True,
            "mode": "temporary" if self.temporary_mode else "installed",
            "version": self.installed_version,
            "install_root": str(self.service.paths.install_root),
        }

    def start_uninstall(self, options: dict[str, object]) -> dict[str, object]:
        if self.temporary_mode:
            return {"ok": False, "error": "临时卸载器不能生成新的卸载请求。"}
        try:
            selection = UninstallSelection.from_mapping(options)
            handoff = self.service.prepare_handoff(
                source_executable=self.current_executable,
                selection=selection,
                original_pid=os.getpid(),
            )
            launch_temporary_uninstaller(handoff)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if self._window is not None:
            threading.Timer(0.25, self._window.destroy).start()
        return {"ok": True, "launched": True}

    def start_execution(self) -> dict[str, object]:
        if not self.temporary_mode:
            return {"ok": False, "error": "当前不是临时卸载执行模式。"}
        if not self._guard.acquire(blocking=False):
            return {"ok": False, "error": "卸载已经在进行中。"}
        with self._state_lock:
            self._operation = {"phase": "running", "outcome": None}
        threading.Thread(
            target=self._execute,
            name="LanDropUninstall",
            daemon=False,
        ).start()
        return {"ok": True}

    def _execute(self) -> None:
        try:
            assert self.request_path is not None
            assert self.nonce is not None
            assert self.expected_request_sha256 is not None
            outcome = self.service.execute_handoff(
                current_executable=self.current_executable,
                request_path=self.request_path,
                nonce=self.nonce,
                expected_request_sha256=self.expected_request_sha256,
            )
            try:
                schedule_temp_self_cleanup(
                    self.current_executable.parent,
                    self.service.paths,
                )
                if outcome.complete:
                    outcome = UninstallOutcome(
                        complete=False,
                        finalization_pending=True,
                        message=(
                            "LanDrop 程序与所选数据已移除。"
                            "关闭此窗口后将完成临时卸载文件清理。"
                        ),
                        removed_integration=outcome.removed_integration,
                        absent_integration=outcome.absent_integration,
                        residuals=outcome.residuals,
                        deleted_data=outcome.deleted_data,
                    )
            except Exception as exc:
                outcome = UninstallOutcome(
                    complete=False,
                    message="卸载未完全完成，请查看残留项。",
                    removed_integration=outcome.removed_integration,
                    absent_integration=outcome.absent_integration,
                    residuals=(
                        *outcome.residuals,
                        f"临时卸载目录自清理无法调度：{exc}"[:1000],
                    ),
                    deleted_data=outcome.deleted_data,
                )
        except Exception as exc:
            outcome = UninstallOutcome(
                complete=False,
                message="卸载未执行或未完全完成。",
                residuals=(f"{type(exc).__name__}: {exc}"[:1000],),
            )
        with self._state_lock:
            assert self._operation is not None
            self._operation = {
                "phase": "finished",
                "outcome": outcome.to_dict(),
            }
        self._guard.release()

    def get_uninstall_status(self) -> dict[str, object]:
        with self._state_lock:
            if self._operation is None:
                return {"ok": True, "phase": "idle", "outcome": None}
            return {"ok": True, **self._operation}

    def close_window(self) -> dict[str, object]:
        with self._state_lock:
            running = self._operation is not None and self._operation["phase"] == "running"
        if running:
            return {"ok": False, "error": "卸载执行完成前不能关闭窗口。"}
        if self._window is None:
            return {"ok": False, "error": "卸载窗口尚未就绪。"}
        self._window.destroy()
        return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanDrop Uninstall")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--execute-request", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--nonce", help=argparse.SUPPRESS)
    parser.add_argument("--expected-request-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt":
        return 1

    runtime_version = webview2_runtime_version()
    if not runtime_version:
        if args.self_check:
            return 2
        _show_native_error(
            "未检测到 Microsoft Edge WebView2 Runtime；"
            "请重新安装或修复 WebView2 Runtime 后再次卸载 LanDrop。"
        )
        return 2
    required = _required_resources()
    if any(not path.is_file() for path in required):
        if args.self_check:
            return 3
        _show_native_error("LanDrop Uninstall 界面资源不完整。")
        return 3
    if args.self_check:
        return 0

    if bool(args.execute_request) != bool(args.nonce and args.expected_request_sha256):
        _show_native_error("临时卸载参数不完整。")
        return 3

    paths = install_paths_for_current_windows_user()
    shortcut_backend = PowerShellShortcutBackend(
        (paths.start_menu_shortcut, paths.desktop_shortcut),
        resource_path("scripts/shortcut-bridge.ps1"),
    )
    integration = WindowsFirstInstallIntegration(shortcut_backend)
    service = UninstallLifecycleService(paths=paths, integration=integration)
    current_executable = Path(sys.executable)
    temporary_mode = args.execute_request is not None
    installed_version = ""
    try:
        if temporary_mode:
            # Full binding is repeated inside execute_handoff while holding the
            # lifecycle lock; the UI starts without mutating product state.
            installed_version = ""
        else:
            installed_version = service.inspect_installed(current_executable).version
    except Exception as exc:
        _show_native_error(str(exc))
        return 4

    try:
        import webview
    except ImportError:
        _show_native_error("LanDrop Uninstall 缺少 pywebview 运行组件。")
        return 3

    api = UninstallApi(
        service,
        current_executable=current_executable,
        request_path=args.execute_request,
        nonce=args.nonce,
        expected_request_sha256=args.expected_request_sha256,
        installed_version=installed_version,
    )
    window = webview.create_window(
        "卸载 LanDrop",
        url=_uninstall_ui_url(temporary_mode=temporary_mode),
        js_api=api,
        width=840,
        height=560,
        min_size=(720, 500),
        resizable=True,
        background_color="#eaf5ff",
    )
    api.attach_window(window)
    window.events.closing += lambda: bool(
        api.get_uninstall_status().get("phase") != "running"
    )
    try:
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except Exception as exc:
        _show_native_error(f"LanDrop Uninstall 无法继续：{exc}")
        return 4


def _required_resources() -> tuple[Path, ...]:
    return (
        resource_path("ui/uninstall/index.html"),
        resource_path("ui/uninstall/js/mode.js"),
        resource_path("ui/uninstall/js/uninstall.js"),
        resource_path("ui/uninstall/css/uninstall.css"),
        resource_path("ui/wizard/js/wizard.js"),
        resource_path("ui/wizard/css/wizard.css"),
        resource_path("scripts/shortcut-bridge.ps1"),
    )


def _uninstall_ui_url(*, temporary_mode: bool) -> str:
    url = resource_path("ui/uninstall/index.html").as_uri()
    return f"{url}?mode=temporary" if temporary_mode else url


def _show_native_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None,
            message,
            "LanDrop Uninstall",
            0x10,
        )
    except (AttributeError, OSError):
        print(message, file=sys.stderr)
