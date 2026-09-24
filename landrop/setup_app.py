"""Single-file LanDrop Setup entry and minimal pywebview API."""

from __future__ import annotations

import argparse
import ctypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from typing import Any

from .app_logging import install_exception_hooks
from .install_contract import InstallPaths
from .installer import FirstInstallOptions, FirstInstallOutcome, FirstInstallService
from .payload_manifest import PayloadManifest, read_payload_manifest, verify_payload
from .platform_checks import webview2_runtime_version
from .resources import resource_path
from .system_integration import PowerShellShortcutBackend, WindowsFirstInstallIntegration


class SetupBundleError(RuntimeError):
    """The onefile Setup bundle is incomplete or inconsistent."""


class SetupBundle:
    def __init__(self) -> None:
        self.payload_root = resource_path("setup_payload")
        self.manifest_path = resource_path("payload-manifest.json")
        self.ui_entry = resource_path("ui/setup/index.html")
        self.shortcut_bridge = resource_path("scripts/shortcut-bridge.ps1")

    def validate(self) -> PayloadManifest:
        required_resources = (self.ui_entry, self.shortcut_bridge, self.manifest_path)
        if any(not path.is_file() for path in required_resources):
            missing = [str(path) for path in required_resources if not path.is_file()]
            raise SetupBundleError(f"Setup 资源不完整：{missing}")
        manifest = read_payload_manifest(self.manifest_path)
        verify_payload(self.payload_root, manifest)
        required_payload = (
            self.payload_root / "app" / "LanDrop.exe",
            self.payload_root / "maintenance" / "Uninstall.exe",
        )
        if any(not path.is_file() for path in required_payload):
            raise SetupBundleError("Setup payload 缺少 LanDrop.exe 或 Uninstall.exe。")
        return manifest


class SetupRuntime:
    def __init__(
        self,
        *,
        bundle: SetupBundle | None = None,
        paths: InstallPaths | None = None,
    ) -> None:
        self.bundle = bundle or SetupBundle()
        self.paths = paths or _install_paths_with_windows_desktop()
        self.manifest = self.bundle.validate()

    def create_service(self) -> FirstInstallService:
        shortcut_backend = PowerShellShortcutBackend(
            (
                self.paths.start_menu_shortcut,
                self.paths.desktop_shortcut,
            ),
            self.bundle.shortcut_bridge,
        )
        integration = WindowsFirstInstallIntegration(shortcut_backend)
        return FirstInstallService(
            paths=self.paths,
            payload_root=self.bundle.payload_root,
            manifest=self.manifest,
            integration=integration,
        )


class SetupApi:
    """Small allowlisted bridge used only by the bundled local Setup page."""

    def __init__(self, runtime: SetupRuntime, *, logger: logging.Logger | None = None) -> None:
        self.runtime = runtime
        self._logger = logger or logging.getLogger("landrop.setup")
        self._guard = threading.Lock()
        self._state_lock = threading.Lock()
        self._operation: dict[str, Any] | None = None
        self._window: Any | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def get_status(self) -> dict[str, object]:
        return {
            "ok": True,
            "install_root": str(self.runtime.paths.install_root),
            "version": self.runtime.manifest.version,
            "desktop_shortcut_default": False,
        }

    def start_install(self, options: dict[str, object]) -> dict[str, object]:
        if set(options) != {"desktop_shortcut"} or not isinstance(
            options.get("desktop_shortcut"), bool
        ):
            return {"ok": False, "error": "安装选项无效。"}
        if not self._guard.acquire(blocking=False):
            return {"ok": False, "error": "安装已经在进行中。"}
        with self._state_lock:
            self._operation = {
                "phase": "running",
                "stage": "starting",
                "outcome": None,
            }
        worker = threading.Thread(
            target=self._run_install,
            args=(bool(options["desktop_shortcut"]),),
            name="LanDropSetup",
            daemon=False,
        )
        worker.start()
        return {"ok": True}

    def _run_install(self, desktop_shortcut: bool) -> None:
        try:
            service = self.runtime.create_service()
            outcome = service.install(
                FirstInstallOptions(desktop_shortcut=desktop_shortcut),
                progress=self._set_stage,
            )
        except Exception as exc:
            self._logger.exception("Unhandled first-install worker failure")
            outcome = FirstInstallOutcome(
                result="failed",
                verified=False,
                rollback_attempted=False,
                rollback_succeeded=None,
                message=f"{type(exc).__name__}: {exc}"[:1000],
            )
        with self._state_lock:
            assert self._operation is not None
            self._operation["phase"] = "finished"
            self._operation["outcome"] = outcome.to_dict()
        if not outcome.verified:
            self._logger.error(
                "First install failed: result=%s rollback_attempted=%s "
                "rollback_succeeded=%s message=%s",
                outcome.result,
                outcome.rollback_attempted,
                outcome.rollback_succeeded,
                outcome.message,
            )
        elif outcome.warning:
            self._logger.warning("First install completed with warning: %s", outcome.warning)
        else:
            self._logger.info("First install completed and verified")
        self._guard.release()

    def _set_stage(self, stage: str) -> None:
        with self._state_lock:
            if self._operation is not None:
                self._operation["stage"] = stage

    def get_install_status(self) -> dict[str, object]:
        with self._state_lock:
            if self._operation is None:
                return {"ok": True, "phase": "idle", "stage": "", "outcome": None}
            return {"ok": True, **self._operation}

    def can_close(self) -> dict[str, object]:
        with self._state_lock:
            running = self._operation is not None and self._operation["phase"] == "running"
        return {"ok": True, "allowed": not running}

    def close_window(self) -> dict[str, object]:
        if not self.can_close()["allowed"]:
            return {"ok": False, "error": "安装事务完成前不能关闭窗口。"}
        if self._window is None:
            return {"ok": False, "error": "安装窗口尚未就绪。"}
        self._window.destroy()
        return {"ok": True}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanDrop Setup")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.name != "nt":
        return 1

    runtime_version = webview2_runtime_version()
    if not runtime_version:
        _show_native_error(
            "未检测到 Microsoft Edge WebView2 Runtime；"
            "请安装或修复 Runtime 后再次运行 LanDrop Setup。"
        )
        return 2
    if args.self_check:
        try:
            manifest = SetupBundle().validate()
        except Exception as exc:
            _show_native_error(f"LanDrop Setup 自检失败：{exc}")
            return 3
        return 0 if manifest.files else 3

    try:
        paths = _install_paths_with_windows_desktop()
        logger = _configure_setup_logging(paths.data_root)
    except Exception as exc:
        _show_native_error(f"LanDrop Setup 无法初始化诊断日志：{exc}")
        return 3
    restore_exception_hooks = install_exception_hooks(logger)
    try:
        bundle = SetupBundle()
        bundle.validate()
    except Exception as exc:
        logger.exception("Setup bundle validation failed")
        _show_native_error(f"LanDrop Setup 自检失败：{exc}")
        restore_exception_hooks()
        return 3

    # Delayed import preserves the native WebView2-missing path with zero product writes.
    try:
        import webview
    except ImportError:
        logger.exception("pywebview import failed")
        _show_native_error("LanDrop Setup 缺少 pywebview 运行组件。")
        restore_exception_hooks()
        return 3
    try:
        runtime = SetupRuntime(bundle=bundle, paths=paths)
        api = SetupApi(runtime, logger=logger)
        window = webview.create_window(
            "安装 LanDrop",
            url=bundle.ui_entry.as_uri(),
            js_api=api,
            width=920,
            height=620,
            min_size=(760, 560),
            resizable=True,
            background_color="#eaf5ff",
        )
        api.attach_window(window)
        window.events.closing += lambda: bool(api.can_close()["allowed"])
        webview.start(gui="edgechromium", debug=False, private_mode=True)
        return 0
    except Exception as exc:
        logger.exception("Unhandled Setup UI failure")
        _show_native_error(f"LanDrop Setup 无法继续：{exc}")
        return 3
    finally:
        restore_exception_hooks()


def _configure_setup_logging(data_directory: Path) -> logging.Logger:
    log_directory = Path(data_directory) / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("landrop.setup")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = RotatingFileHandler(
        log_directory / "setup.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.info("LanDrop Setup logging initialized")
    return logger


def _install_paths_with_windows_desktop() -> InstallPaths:
    paths = InstallPaths.from_environment()
    if os.name != "nt":
        return paths
    try:
        import winreg

        shell_folders = (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, shell_folders) as key:
            raw, value_type = winreg.QueryValueEx(key, "Desktop")
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(raw, str):
            return paths
        desktop = Path(os.path.expandvars(raw))
        if desktop.is_absolute():
            return InstallPaths(
                paths.local_app_data,
                paths.roaming_app_data,
                desktop,
                paths.temp_directory,
            )
    except OSError:
        pass
    return paths


def _show_native_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "LanDrop Setup", 0x10)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        print(message, file=sys.stderr)
