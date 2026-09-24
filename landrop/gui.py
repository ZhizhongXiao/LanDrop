"""pywebview desktop control window for LanDrop."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any

from . import __version__
from .app_logging import configure_application_logging, install_exception_hooks
from .install_contract import InstallContractError, InstallPaths
from .install_lock import InstallLifecycleLock, InstallLifecycleLockError
from .install_state import InstallationStateStore, InstallStateError
from .settings import (
    AppSettings,
    DEFAULT_MAX_UPLOAD_MB,
    SettingsError,
    SettingsStore,
    default_data_directory,
)
from .coordinator import ActionCoordinator
from .notifications import WindowsToastBackend
from .platform_checks import webview2_runtime_version
from .qr_invite import qr_png_data_uri
from .resources import resource_path
from .service import ServiceController, ServiceError
from .single_instance import DesktopSingleInstance, SingleInstanceError
from .storage import StorageError, cleanup_orphaned_upload_parts
from .tray import LanDropTray, TrayUnavailableError
from .trust import CredentialStore
from .upgrade import retry_pending_cleanup


class DesktopApi:
    """Small bridge exposed only to the local desktop control page."""

    def __init__(
        self,
        controller: ServiceController,
        credentials: CredentialStore,
        webview_module: Any,
        settings: SettingsStore | None = None,
    ) -> None:
        self._controller = controller
        self._credentials = credentials
        self._webview = webview_module
        self._settings_store = settings or SettingsStore(credentials.data_directory)
        self._settings = self._settings_store.load()
        self._window: Any | None = None
        self._coordinator: ActionCoordinator | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def attach_coordinator(self, coordinator: ActionCoordinator) -> None:
        self._coordinator = coordinator

    def get_state(self) -> dict[str, object]:
        state = self._desktop_state(self._controller.snapshot())
        if not state["running"]:
            state["shared_directory"] = str(self._settings.shared_directory)
            state["receive_directory"] = str(self._settings.receive_directory)
            state["max_upload_mb"] = self._settings.max_upload_mb
        warning = self._settings_store.last_warning
        self._settings_store.last_warning = ""
        return {
            "ok": True,
            "state": state,
            "warning": warning,
        }

    def _desktop_state(self, snapshot: Any) -> dict[str, object]:
        state = snapshot.to_dict()
        qr_provider = getattr(self._controller, "pairing_qr_data_uri", None)
        state["pairing_qr_data_uri"] = qr_provider() if callable(qr_provider) else ""
        return state

    def choose_directory(self, current: str) -> dict[str, object]:
        if self._controller.snapshot().running:
            return {"ok": False, "error": "请先停止服务，再修改目录。"}
        if self._window is None:
            return {"ok": False, "error": "窗口尚未准备完成。"}
        initial = current if current and Path(current).is_dir() else str(Path.home())
        try:
            file_dialog = getattr(self._webview, "FileDialog", None)
            dialog_type = (
                file_dialog.FOLDER
                if file_dialog is not None
                else self._webview.FOLDER_DIALOG
            )
            selected = self._window.create_file_dialog(
                dialog_type,
                directory=initial,
                allow_multiple=False,
            )
        except Exception as exc:
            return {"ok": False, "error": f"无法打开目录选择器：{exc}"}
        if not selected:
            return {"ok": True, "cancelled": True}
        return {"ok": True, "path": str(selected[0])}

    def start_service(self, options: dict[str, object]) -> dict[str, object]:
        try:
            max_upload_mb = int(options.get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB))
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            snapshot = self._coordinator.start_from_gui(
                str(options.get("shared_directory", "")),
                str(options.get("receive_directory", "")),
                max_upload_mb,
                str(options.get("interface_selector") or "") or None,
            )
            warning = ""
            try:
                self._settings = AppSettings(
                    shared_directory=Path(snapshot.shared_directory),
                    receive_directory=Path(snapshot.receive_directory),
                    max_upload_mb=snapshot.max_upload_mb,
                )
                self._settings_store.save(self._settings)
            except SettingsError as exc:
                warning = str(exc)
            return {"ok": True, "state": self._desktop_state(snapshot), "warning": warning}
        except (ServiceError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc), "state": self._desktop_state(self._controller.snapshot())}

    def save_settings(self, options: dict[str, object]) -> dict[str, object]:
        if self._controller.snapshot().running:
            return {"ok": False, "error": "请先停止服务，再保存目录和上传上限。"}
        try:
            shared = _existing_gui_directory(options.get("shared_directory"), "下载来源")
            received = _existing_gui_directory(options.get("receive_directory"), "上传保存")
            max_upload_mb = int(options.get("max_upload_mb", DEFAULT_MAX_UPLOAD_MB))
            settings = AppSettings(shared, received, max_upload_mb)
            self._settings_store.save(settings)
            self._settings = settings
            return {"ok": True}
        except (OSError, SettingsError, TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def list_interfaces(self) -> dict[str, object]:
        try:
            return {"ok": True, "interfaces": self._controller.available_interfaces()}
        except Exception as exc:
            return {"ok": False, "error": f"无法刷新网络接口：{exc}", "interfaces": []}

    def stop_service(self) -> dict[str, object]:
        try:
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            return {"ok": True, "state": self._desktop_state(self._coordinator.stop_from_ui().state)}
        except Exception as exc:
            return {"ok": False, "error": f"停止服务失败：{exc}"}

    def reset_deadline(self) -> dict[str, object]:
        try:
            if self._coordinator is None:
                raise RuntimeError("桌面运行时尚未准备完成。")
            result = self._coordinator.reset_from_tray()
            if not result.applied:
                return {"ok": False, "error": result.message, "state": self._desktop_state(result.state)}
            return {"ok": True, "state": self._desktop_state(result.state)}
        except ServiceError as exc:
            return {"ok": False, "error": str(exc), "state": self._desktop_state(self._controller.snapshot())}

    def refresh_diagnostics(self) -> dict[str, object]:
        try:
            return {"ok": True, "state": self._desktop_state(self._controller.refresh_diagnostics())}
        except Exception as exc:
            return {"ok": False, "error": f"无法刷新诊断：{exc}"}

    def open_windows_settings(self, target: str) -> dict[str, object]:
        targets = {
            "network": "ms-settings:network-status",
            "firewall": "windowsdefender://network/",
            "proxy": "ms-settings:network-proxy",
        }
        uri = targets.get(target)
        if uri is None:
            return {"ok": False, "error": "未知的 Windows 设置入口。"}
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            return {"ok": False, "error": f"无法打开 Windows 设置：{exc}"}
        return {"ok": True}

    def open_log_folder(self) -> dict[str, object]:
        log_directory = self._credentials.data_directory / "logs"
        try:
            log_directory.mkdir(parents=True, exist_ok=True)
            os.startfile(str(log_directory))  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            return {"ok": False, "error": f"无法打开日志文件夹：{exc}"}
        return {"ok": True}

    def get_diagnostic_report(self) -> dict[str, object]:
        try:
            return {"ok": True, "text": _diagnostic_report(self._controller.snapshot())}
        except Exception as exc:
            return {"ok": False, "error": f"无法生成诊断信息：{exc}"}

    def list_trusted_clients(self) -> dict[str, object]:
        try:
            clients = [
                {
                    "client_id": client.client_id,
                    "created_at": client.created_at,
                    "label": client.label,
                    "device_name": client.device_name,
                    "device_type": client.device_type,
                    "operating_system": client.operating_system,
                    "browser": client.browser,
                    "device_model": client.device_model,
                    "browser_engine": client.browser_engine,
                }
                for client in self._credentials.list_clients()
            ]
            return {"ok": True, "clients": clients}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}

    def revoke_trusted_client(self, client_id: str) -> dict[str, object]:
        try:
            revoked = self._credentials.revoke(client_id)
            result = self.list_trusted_clients()
            result["revoked"] = revoked
            if not revoked and result.get("ok"):
                result["ok"] = False
                result["error"] = "该可信客户机已不存在。"
            return result
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}

    def revoke_all_trusted_clients(self) -> dict[str, object]:
        try:
            count = self._credentials.revoke_all()
            return {"ok": True, "count": count, "clients": []}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "clients": []}


class _WindowDispatcher:
    """Centralize every pywebview window mutation away from callback threads."""

    def __init__(self, window: Any) -> None:
        self._window = window

    def hide_window(self) -> None:
        self._window.hide()

    def show_window(self) -> None:
        self._window.show()
        self._window.restore()

    def destroy_window(self) -> None:
        self._window.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="LanDrop Desktop")
    parser.add_argument("--session-seconds", type=int, default=300)
    parser.add_argument("--grace-seconds", type=int, default=60)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 5 <= args.session_seconds <= 86_400 or not 0 <= args.grace_seconds <= 600:
        parser.error("session-seconds 需为 5–86400，grace-seconds 需为 0–600")
    if os.name != "nt":
        print("LanDrop 桌面版目前只支持 Windows。", file=sys.stderr)
        return 1
    data_directory = default_data_directory()
    if args.self_check:
        try:
            logger = configure_application_logging(data_directory, debug=args.debug)
        except OSError as exc:
            print(f"无法创建 LanDrop 日志：{exc}", file=sys.stderr)
            return 1
        restore_exception_hooks = install_exception_hooks(logger)
        result = _portable_self_check(logger)
        restore_exception_hooks()
        return result

    # Ordinary startup enters the maintenance gate before importing the GUI
    # runtime or creating any normal application state.
    try:
        single_instance, is_primary = _acquire_desktop_startup_ownership(data_directory)
    except (
        InstallContractError,
        InstallLifecycleLockError,
        InstallStateError,
        OSError,
        SingleInstanceError,
    ) as exc:
        _show_native_error(f"LanDrop 无法启动：{exc}")
        return 1
    if not is_primary:
        activated = single_instance.notify_existing()
        single_instance.close()
        if not activated:
            _show_native_error("LanDrop 已在运行，但暂时无法唤起现有窗口。")
            return 1
        return 0

    try:
        logger = configure_application_logging(data_directory, debug=args.debug)
    except OSError as exc:
        single_instance.close()
        _show_native_error(f"无法创建 LanDrop 日志：{exc}")
        return 1
    restore_exception_hooks = install_exception_hooks(logger)
    webview2_version = webview2_runtime_version()
    if not webview2_version:
        message = (
            "未检测到 Microsoft Edge WebView2 Runtime。"
            "请先通过 Windows 更新或 Microsoft 官方安装程序安装后再启动 LanDrop。"
        )
        logger.error(message)
        single_instance.close()
        restore_exception_hooks()
        _show_native_error(message)
        return 1
    logger.info("Microsoft Edge WebView2 Runtime %s", webview2_version)
    try:
        import webview
    except ImportError:
        message = "缺少 pywebview。请重新安装 LanDrop 或检查 requirements.txt。"
        logger.error(message)
        single_instance.close()
        restore_exception_hooks()
        _show_native_error(message)
        return 1
    settings_store = SettingsStore(data_directory)
    settings = settings_store.load()
    try:
        settings_store.ensure_default_directories(settings)
    except SettingsError as exc:
        logger.error("无法准备 LanDrop 用户目录：%s", exc)
        single_instance.close()
        restore_exception_hooks()
        return 1
    if settings_store.last_warning:
        logger.warning("[配置] %s", settings_store.last_warning)
    if settings.receive_directory.is_dir():
        try:
            cleanup = cleanup_orphaned_upload_parts(settings.receive_directory)
            if cleanup.removed or cleanup.skipped or cleanup.errors:
                logger.info(
                    "上传临时文件清理：删除 %s，跳过 %s，失败 %s",
                    cleanup.removed,
                    cleanup.skipped,
                    len(cleanup.errors),
                )
                for detail in cleanup.errors:
                    logger.warning("无法清理上传临时文件：%s", detail)
        except StorageError as exc:
            logger.warning("无法检查孤立上传临时文件：%s", exc)
    credentials = CredentialStore(data_directory)
    controller = ServiceController(
        credentials,
        duration_seconds=args.session_seconds,
        grace_seconds=args.grace_seconds,
        emit_performance_timings=args.debug,
    )
    api = DesktopApi(controller, credentials, webview, settings_store)
    entry = _desktop_entry_path()
    window = webview.create_window(
        "LanDrop",
        url=entry.as_uri(),
        js_api=api,
        width=1060,
        height=760,
        min_size=(820, 640),
        **_desktop_window_visibility(args.startup),
        background_color="#eaf2fb",
        text_select=True,
    )
    api.attach_window(window)
    window_dispatcher = _WindowDispatcher(window)
    coordinator = ActionCoordinator(controller, window_dispatcher)
    api.attach_coordinator(coordinator)
    single_instance.set_activate_callback(coordinator.submit_show_window)
    toasts = WindowsToastBackend(
        lambda action, session_id, revision: coordinator.submit_toast_action(
            action,
            session_id=session_id,
            deadline_revision=revision,
        )
    )
    try:
        toasts.start()
    except Exception as exc:
        logger.warning("Windows Toast 不可用：%s", exc)
    else:
        coordinator.bind_toasts(toasts)
        coordinator.start_expiry_monitor(
            toasts.send_expiry_reminder,
            toasts.send_service_stopped,
        )

    tray = LanDropTray(
        state_provider=controller.snapshot,
        open_window=coordinator.submit_show_window,
        reset=coordinator.reset_from_tray,
        stop_service=coordinator.stop_from_ui,
        exit_application=lambda: coordinator.request_exit("app_exit"),
    )
    coordinator.bind_tray(tray)
    coordinator.set_state_listener(lambda _state: tray.refresh())
    try:
        tray.start()
    except TrayUnavailableError as exc:
        logger.warning("%s；关闭窗口将退出 LanDrop。", exc)

    def on_window_closing() -> bool:
        return coordinator.close_request(tray_ready=tray.ready)

    window.events.closing += on_window_closing
    try:
        webview.start(icon=str(resource_path("assets/LanDrop.ico")))
    finally:
        coordinator.request_exit("app_exit")
        single_instance.close()
        restore_exception_hooks()
    return 0


def _diagnostic_report(snapshot: Any) -> str:
    state = snapshot.to_dict()
    diagnostics = state.get("diagnostics") or {}
    network = diagnostics.get("network") or {}
    firewall = diagnostics.get("firewall") or {}
    interfaces = []
    for item in network.get("interfaces") or []:
        interfaces.append(
            {
                "alias": item.get("alias", ""),
                "network_name": item.get("description", ""),
                "interface_index": item.get("interface_index", 0),
                "ipv4": item.get("address", ""),
                "category": item.get("category", "Unknown"),
                "role": item.get("role", ""),
            }
        )
    evidence = []
    for item in firewall.get("evidence") or []:
        program = str(item.get("program") or "Any")
        evidence.append(
            {
                "kind": item.get("kind", ""),
                "name": item.get("name", ""),
                "profile": item.get("profile", ""),
                "action": item.get("action", ""),
                "program": program if program == "Any" else Path(program).name,
                "protocol": item.get("protocol", ""),
                "local_port": item.get("local_port", ""),
            }
        )
    report = {
        "landrop_version": __version__,
        "platform": f"{platform.system()} {platform.release()}",
        "service": {
            "running": state.get("running", False),
            "phase": state.get("phase", ""),
            "message": state.get("message", ""),
            "stop_reason": state.get("stop_reason", ""),
        },
        "endpoint": {
            "interface": state.get("interface", ""),
            "network_name": state.get("network_name", ""),
            "interface_index": state.get("interface_index", 0),
            "ipv4": state.get("bound_ipv4", ""),
            "category": state.get("network_category", ""),
            "status": state.get("endpoint_status", ""),
            "detail": state.get("endpoint_detail", ""),
        },
        "diagnostics": {
            "status": diagnostics.get("status", "idle"),
            "message": diagnostics.get("message", ""),
            "network": {
                "status": network.get("status", "idle"),
                "message": network.get("message", ""),
                "interfaces": interfaces,
            },
            "firewall": {
                "status": firewall.get("status", "idle"),
                "level": firewall.get("level", "unknown"),
                "message": firewall.get("message", ""),
                "exact_port_allow": firewall.get("exact_port_allow", 0),
                "program_allow": firewall.get("program_allow", 0),
                "broad_allow": firewall.get("broad_allow", 0),
                "relevant_blocks": firewall.get("relevant_blocks", 0),
                "evidence": evidence,
            },
        },
    }
    return json.dumps(report, ensure_ascii=False, indent=2)


def _existing_gui_directory(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f"请选择{label}目录。")
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise SettingsError(f"{label}目录不存在：{value}") from exc
    if not path.is_dir():
        raise SettingsError(f"{label}路径不是目录：{path}")
    return path


def _show_native_error(message: str) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "LanDrop", 0x10)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        print(message, file=sys.stderr)


def _desktop_entry_path() -> Path:
    entry = resource_path("ui/desktop/index.html")
    if not entry.is_file():
        raise RuntimeError(f"LanDrop 桌面界面资源不存在：{entry}")
    return entry


def _desktop_window_visibility(startup: bool) -> dict[str, bool]:
    """Keep the only pywebview Window alive but hidden for HKCU Run startup."""
    return {"hidden": startup, "focus": not startup}


def _acquire_desktop_startup_ownership(
    data_directory: Path,
    *,
    install_paths: InstallPaths | None = None,
    lifecycle_lock: InstallLifecycleLock | None = None,
    state_store: InstallationStateStore | None = None,
    single_instance: DesktopSingleInstance | None = None,
    cleanup_retry: Any | None = None,
) -> tuple[DesktopSingleInstance, bool]:
    """Pass the maintenance gate, then establish ordinary desktop ownership.

    ``--self-check`` never calls this function.  The lifecycle lock remains held
    until the desktop single-instance identity has been established, closing the
    startup-versus-maintenance TOCTOU window.
    """
    paths = install_paths or InstallPaths.from_environment()
    maintenance_lock = lifecycle_lock or InstallLifecycleLock(paths)
    state = state_store or InstallationStateStore(paths)
    desktop_instance = single_instance or DesktopSingleInstance(data_directory)
    try:
        acquired = maintenance_lock.acquire(0.0)
        if not acquired:
            raise InstallLifecycleLockError("LanDrop 正在安装、升级或卸载，请稍后重试。")
        state.ensure_normal_start_allowed()
        is_primary = desktop_instance.acquire()
        if is_primary:
            retry = cleanup_retry or (
                lambda: retry_pending_cleanup(paths, state_store=state)
            )
            retry()
        return desktop_instance, is_primary
    except Exception:
        desktop_instance.close()
        raise
    finally:
        maintenance_lock.close()


def _portable_self_check(logger: Any) -> int:
    try:
        from importlib.metadata import version

        runtime_version = webview2_runtime_version()
        if not runtime_version:
            raise RuntimeError("未检测到 Microsoft Edge WebView2 Runtime")
        import pystray  # noqa: F401
        import qrcode  # noqa: F401
        import webview  # noqa: F401
        import windows_toasts  # noqa: F401
        from PIL import Image

        icon_path = resource_path("assets/LanDrop.ico")
        with Image.open(icon_path) as icon:
            if icon.convert("RGBA").getextrema()[3][0] != 0:
                raise RuntimeError("产品图标缺少透明圆角")
        product_preview_path = resource_path("assets/LanDrop-icon-preview.png")
        with Image.open(product_preview_path) as product_preview:
            if product_preview.convert("RGBA").getextrema()[3][0] != 0:
                raise RuntimeError("桌面产品图缺少透明圆角")
        tray_icon_path = resource_path("assets/LanDrop-tray.ico")
        with Image.open(tray_icon_path) as tray_icon:
            if tray_icon.convert("RGBA").getextrema()[3][0] != 0:
                raise RuntimeError("托盘图标缺少透明背景")
        qr_sample = qr_png_data_uri("http://192.168.50.1:8000/pair/qr#self-check")
        if not qr_sample.startswith("data:image/png;base64,"):
            raise RuntimeError("本地二维码渲染自检失败")
        desktop_resources = (
            _desktop_entry_path(),
            resource_path("ui/desktop/css/tokens.css"),
            resource_path("ui/desktop/css/shell.css"),
            resource_path("ui/desktop/css/components.css"),
            resource_path("ui/shared/js/theme.js"),
            resource_path("ui/desktop/js/state.js"),
            resource_path("ui/desktop/js/app.js"),
        )
        missing = [str(path) for path in desktop_resources if not path.is_file()]
        if missing:
            raise RuntimeError(f"桌面界面资源不完整：{', '.join(missing)}")
        logger.info(
            "Portable self-check passed: LanDrop %s; pywebview %s; pystray %s; "
            "Windows-Toasts %s; qrcode %s; WebView2 %s; icons %s/%s; desktop UI %s files",
            __version__,
            version("pywebview"),
            version("pystray"),
            version("windows-toasts"),
            version("qrcode"),
            runtime_version,
            icon_path.name,
            tray_icon_path.name,
            len(desktop_resources),
        )
        return 0
    except Exception:
        logger.exception("Portable self-check failed")
        return 1
