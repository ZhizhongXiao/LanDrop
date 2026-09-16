"""Thread-safe service lifecycle used by the desktop control window."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import errno
from pathlib import Path
import threading
from typing import Any, Callable

from .lifecycle import SessionExpiredError, SessionLifecycle
from .events import SessionEventLog
from .network import discover_interfaces, select_interface
from .server import ServerGroup
from .trust import CredentialStore
from .web import WebConfig, create_application


class ServiceError(RuntimeError):
    """A user-facing service lifecycle error."""


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    running: bool
    phase: str
    message: str
    shared_directory: str
    receive_directory: str
    max_upload_mb: int
    local_url: str = ""
    lan_url: str = ""
    interface: str = ""
    network_category: str = ""
    pairing_code: str = ""
    session_id: str = ""
    remaining_seconds: int = 0
    grace_remaining_seconds: int = 0
    remaining_milliseconds: int = 0
    grace_remaining_milliseconds: int = 0
    paired_devices: int = 0
    active_transfers: int = 0
    stop_reason: str = ""
    statistics: dict[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class ServiceController:
    """Own exactly one LanDrop server session at a time."""

    def __init__(
        self,
        credentials: CredentialStore,
        *,
        port: int = 8000,
        discover: Callable[[], list[Any]] = discover_interfaces,
        select: Callable[[list[Any], str | None], Any] = select_interface,
        application_factory: Callable[[WebConfig], tuple[Any, str]] = create_application,
        server_factory: Callable[[str, int, Any], Any] = ServerGroup,
        duration_seconds: float = 300,
        grace_seconds: float = 60,
        lifecycle_factory: Callable[[], SessionLifecycle] | None = None,
    ) -> None:
        self._credentials = credentials
        self._port = port
        self._discover = discover
        self._select = select
        self._application_factory = application_factory
        self._server_factory = server_factory
        self._duration_seconds = duration_seconds
        self._grace_seconds = grace_seconds
        self._lifecycle_factory = lifecycle_factory
        self._event_log = SessionEventLog(credentials.data_directory)
        self._lock = threading.RLock()
        self._server: Any | None = None
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._lifecycle: SessionLifecycle | None = None
        self._started_at = ""
        self._logged_sessions: set[str] = set()
        self._snapshot = ServiceSnapshot(
            running=False,
            phase="stopped",
            message="服务未启动",
            shared_directory="",
            receive_directory="",
            max_upload_mb=1000,
            statistics={},
        )

    def snapshot(self) -> ServiceSnapshot:
        with self._lock:
            return self._current_snapshot_locked()

    def start(
        self,
        shared_directory: str | Path,
        receive_directory: str | Path,
        max_upload_mb: int = 1000,
        interface_selector: str | None = None,
    ) -> ServiceSnapshot:
        with self._lock:
            if self._server is not None:
                raise ServiceError("服务已经在运行。")

            shared = _existing_directory(shared_directory, "共享")
            received = _existing_directory(receive_directory, "接收")
            if not 1 <= max_upload_mb <= 10_000:
                raise ServiceError("上传上限必须在 1 到 10000 MB 之间。")

            try:
                interface = self._select(self._discover(), interface_selector)
            except Exception as exc:
                raise ServiceError(str(exc)) from exc

            try:
                lifecycle = (
                    self._lifecycle_factory()
                    if self._lifecycle_factory is not None
                    else SessionLifecycle(self._duration_seconds, self._grace_seconds)
                )
                application, pairing_code = self._application_factory(
                    WebConfig(
                        shared_directory=shared,
                        receive_directory=received,
                        max_upload_bytes=max_upload_mb * 1_000_000,
                        credentials=self._credentials,
                        lifecycle=lifecycle,
                    )
                )
                server = self._server_factory(interface.address, self._port, application)
                lifecycle.activate()
            except OSError as exc:
                raise ServiceError(_format_bind_error(exc, interface.address, self._port)) from exc
            except Exception as exc:
                raise ServiceError(str(exc)) from exc

            self._server = server
            self._lifecycle = lifecycle
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._snapshot = ServiceSnapshot(
                running=True,
                phase="running",
                message="服务正在运行",
                shared_directory=str(shared),
                receive_directory=str(received),
                max_upload_mb=max_upload_mb,
                local_url=f"http://127.0.0.1:{self._port}/",
                lan_url=f"http://{interface.address}:{self._port}/",
                interface=interface.alias,
                network_category=interface.category,
                pairing_code=pairing_code,
                statistics={},
            )
            worker = threading.Thread(
                target=self._serve,
                args=(server,),
                name="LanDrop-GUI-Service",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            watchdog = threading.Thread(
                target=self._watch_lifecycle,
                args=(server, lifecycle),
                name="LanDrop-Lifecycle",
                daemon=True,
            )
            self._watchdog = watchdog
            watchdog.start()
            return self._current_snapshot_locked()

    def stop(
        self,
        reason: str = "manual_stop",
        *,
        _expected_server: Any | None = None,
        _expected_lifecycle: SessionLifecycle | None = None,
    ) -> ServiceSnapshot:
        with self._lock:
            server = self._server
            worker = self._worker
            watchdog = self._watchdog
            lifecycle = self._lifecycle
            if (
                (_expected_server is not None and server is not _expected_server)
                or (
                    _expected_lifecycle is not None
                    and lifecycle is not _expected_lifecycle
                )
            ):
                return self._current_snapshot_locked()
            if server is None:
                return self._snapshot
            if lifecycle is not None:
                lifecycle.stop(reason, reason)
            self._snapshot = _stopped_from(self._snapshot, "正在停止服务……", "stopping")

        server.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)
        if watchdog is not None and watchdog is not threading.current_thread():
            watchdog.join(timeout=2)

        with self._lock:
            if self._server is server:
                lifecycle_snapshot = lifecycle.snapshot() if lifecycle is not None else None
                if lifecycle_snapshot is not None:
                    self._record_session_locked(lifecycle_snapshot)
                self._server = None
                self._worker = None
                self._watchdog = None
                self._lifecycle = None
                self._snapshot = _stopped_from(
                    self._snapshot,
                    _stop_message(reason),
                    lifecycle_snapshot=lifecycle_snapshot,
                )
            return self._snapshot

    def reset_deadline(self) -> ServiceSnapshot:
        with self._lock:
            if self._lifecycle is None or self._server is None:
                raise ServiceError("服务尚未启动。")
            try:
                self._lifecycle.reset_deadline(300)
            except SessionExpiredError as exc:
                raise ServiceError(str(exc)) from exc
            return self._current_snapshot_locked()

    def _serve(self, server: Any) -> None:
        error = ""
        try:
            server.serve_forever()
        except Exception as exc:
            error = f"服务意外停止：{exc}"
        finally:
            server.close()
            with self._lock:
                if self._server is server:
                    lifecycle = self._lifecycle
                    if lifecycle is not None:
                        lifecycle.stop("server_error" if error else "server_stopped")
                        lifecycle_snapshot = lifecycle.snapshot()
                        self._record_session_locked(lifecycle_snapshot)
                    else:
                        lifecycle_snapshot = None
                    self._server = None
                    self._worker = None
                    self._watchdog = None
                    self._lifecycle = None
                    self._snapshot = _stopped_from(
                        self._snapshot,
                        error or _stop_message(
                            lifecycle_snapshot.stop_reason
                            if lifecycle_snapshot is not None
                            else "server_stopped"
                        ),
                        "error" if error else "stopped",
                        lifecycle_snapshot,
                    )

    def _watch_lifecycle(self, server: Any, lifecycle: SessionLifecycle) -> None:
        stop_check = threading.Event()
        while not stop_check.wait(0.25):
            snapshot = lifecycle.heartbeat()
            if snapshot.should_close:
                self.stop(
                    snapshot.stop_reason or "deadline_no_active",
                    _expected_server=server,
                    _expected_lifecycle=lifecycle,
                )
                return
            with self._lock:
                if self._server is not server or self._lifecycle is not lifecycle:
                    return

    def _current_snapshot_locked(self) -> ServiceSnapshot:
        if self._lifecycle is None or self._server is None:
            return self._snapshot
        lifecycle = self._lifecycle.snapshot()
        phase = lifecycle.phase
        message = "服务正在运行"
        if phase == "grace":
            message = "会话已到期，正在等待现有传输完成"
        return replace(
            self._snapshot,
            running=phase != "stopped",
            phase=phase,
            message=message,
            pairing_code=lifecycle.pairing_code,
            session_id=lifecycle.session_id,
            remaining_seconds=lifecycle.remaining_seconds,
            grace_remaining_seconds=lifecycle.grace_remaining_seconds,
            remaining_milliseconds=lifecycle.remaining_milliseconds,
            grace_remaining_milliseconds=lifecycle.grace_remaining_milliseconds,
            paired_devices=lifecycle.paired_devices,
            active_transfers=lifecycle.active_transfers,
            stop_reason=lifecycle.stop_reason,
            statistics=lifecycle.statistics,
        )

    def _record_session_locked(self, lifecycle_snapshot: Any) -> None:
        if lifecycle_snapshot.session_id in self._logged_sessions:
            return
        self._logged_sessions.add(lifecycle_snapshot.session_id)
        try:
            self._event_log.record(
                lifecycle_snapshot,
                started_at=self._started_at,
                duration_seconds=self._duration_seconds,
                grace_seconds=self._grace_seconds,
            )
        except OSError as exc:
            print(f"[日志] 无法写入会话统计：{exc}")


def _existing_directory(path: str | Path, label: str) -> Path:
    if isinstance(path, str) and not path.strip():
        raise ServiceError(f"请选择{label}目录。")
    source = Path(path).expanduser()
    try:
        resolved = source.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ServiceError(f"{label}目录不存在：{source}") from exc
    except OSError as exc:
        raise ServiceError(f"无法读取{label}目录：{exc}") from exc
    if not resolved.is_dir():
        raise ServiceError(f"{label}路径不是目录：{resolved}")
    return resolved


def _format_bind_error(exc: OSError, address: str, port: int) -> str:
    if exc.errno in {errno.EADDRINUSE, 10048} or getattr(exc, "winerror", None) == 10048:
        return f"无法启动：端口 {port} 已被占用。"
    if getattr(exc, "winerror", None) == 10013:
        return f"无法启动：没有权限监听 {address}:{port}。"
    return f"无法启动服务器：{exc}"


def _stopped_from(
    current: ServiceSnapshot,
    message: str,
    phase: str = "stopped",
    lifecycle_snapshot: Any | None = None,
) -> ServiceSnapshot:
    result = ServiceSnapshot(
        running=False,
        phase=phase,
        message=message,
        shared_directory=current.shared_directory,
        receive_directory=current.receive_directory,
        max_upload_mb=current.max_upload_mb,
        stop_reason=current.stop_reason,
        statistics=current.statistics or {},
    )
    if lifecycle_snapshot is None:
        return result
    return replace(
        result,
        stop_reason=lifecycle_snapshot.stop_reason,
        paired_devices=lifecycle_snapshot.paired_devices,
        statistics=lifecycle_snapshot.statistics,
    )


def _stop_message(reason: str) -> str:
    messages = {
        "manual_stop": "服务已手动停止，端口已关闭。",
        "window_closed": "窗口已关闭，服务和端口已停止。",
        "deadline_no_active": "5 分钟会话已到期，端口已自动关闭。",
        "deadline_transfers_completed": "现有传输已完成，端口已自动关闭。",
        "grace_timeout": "传输宽限时间已结束，端口已强制关闭。",
        "system_resume": "检测到电脑从睡眠恢复，会话已安全终止。",
    }
    return messages.get(reason, "服务已停止，端口已关闭。")
