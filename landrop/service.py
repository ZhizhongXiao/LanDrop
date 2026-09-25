"""Thread-safe service lifecycle used by the desktop control window."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import errno
import logging
from pathlib import Path
import sys
import threading
from time import monotonic, perf_counter
from typing import Any, Callable

from .diagnostics import inspect_system
from .lifecycle import SessionExpiredError, SessionLifecycle
from .events import SessionEventLog
from .network import (
    EndpointBaseline,
    EndpointChecker,
    discover_interfaces,
    endpoint_baseline,
    select_interface,
)
from .qr_invite import qr_png_data_uri
from .server import ServerGroup
from .trust import CredentialStore
from .web import WebConfig, create_application


logger = logging.getLogger("landrop.service")


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
    network_name: str = ""
    interface_index: int = 0
    bound_ipv4: str = ""
    network_category: str = ""
    endpoint_status: str = "inactive"
    endpoint_detail: str = ""
    diagnostics: dict[str, object] | None = None
    pairing_code: str = ""
    session_id: str = ""
    deadline_revision: int = 0
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


@dataclass(frozen=True, slots=True)
class _StopContext:
    server: Any
    worker: threading.Thread | None
    watchdog: threading.Thread | None
    network_monitor: threading.Thread | None
    lifecycle: SessionLifecycle | None


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
        endpoint_checker_factory: Callable[[EndpointBaseline], Any] = EndpointChecker,
        diagnostics_factory: Callable[[int, str], dict[str, object]] = inspect_system,
        endpoint_check_interval: float = 3.0,
        category_check_interval: float = 15.0,
        endpoint_confirmation_delay: float = 0.75,
        emit_performance_timings: bool = False,
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
        self._endpoint_checker_factory = endpoint_checker_factory
        self._diagnostics_factory = diagnostics_factory
        self._endpoint_check_interval = max(0.1, float(endpoint_check_interval))
        self._category_check_interval = max(
            self._endpoint_check_interval, float(category_check_interval)
        )
        self._endpoint_confirmation_delay = max(
            0.05, float(endpoint_confirmation_delay)
        )
        self._emit_performance_timings = emit_performance_timings
        self._event_log = SessionEventLog(credentials.data_directory)
        self._lock = threading.RLock()
        self._server: Any | None = None
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop: threading.Event | None = None
        self._network_monitor: threading.Thread | None = None
        self._network_stop: threading.Event | None = None
        self._diagnostics_generation = 0
        self._lifecycle: SessionLifecycle | None = None
        self._started_at = ""
        self._logged_sessions: set[str] = set()
        self._qr_cache_key: tuple[str, int, str] | None = None
        self._qr_cache_data_uri = ""
        self._snapshot = ServiceSnapshot(
            running=False,
            phase="stopped",
            message="服务未启动",
            shared_directory="",
            receive_directory="",
            max_upload_mb=1000,
            diagnostics=_empty_diagnostics(),
            statistics={},
        )

    def snapshot(self) -> ServiceSnapshot:
        with self._lock:
            return self._current_snapshot_locked()

    def available_interfaces(self) -> list[dict[str, object]]:
        """Return a fresh, read-only interface list for explicit GUI selection."""
        interfaces = self._discover()
        return [_interface_diagnostic(item) for item in interfaces]

    def pairing_qr_data_uri(self) -> str:
        """Render the current invitation for the local desktop UI only."""
        with self._lock:
            if self._lifecycle is None or self._server is None:
                self._qr_cache_key = None
                self._qr_cache_data_uri = ""
                return ""
            invitation = self._lifecycle.pairing_invitation()
            if invitation is None:
                return ""
            session_id, revision, token = invitation
            key = (session_id, revision, self._snapshot.lan_url)
            if key == self._qr_cache_key:
                return self._qr_cache_data_uri
            invitation_url = f"{self._snapshot.lan_url.rstrip('/')}/pair/qr#{token}"
            data_uri = qr_png_data_uri(invitation_url)
            self._qr_cache_key = key
            self._qr_cache_data_uri = data_uri
            return data_uri

    def start(
        self,
        shared_directory: str | Path,
        receive_directory: str | Path,
        max_upload_mb: int = 1000,
        interface_selector: str | None = None,
    ) -> ServiceSnapshot:
        started_at = perf_counter()
        with self._lock:
            if self._server is not None:
                raise ServiceError("服务已经在运行。")

            shared = _existing_directory(shared_directory, "下载来源")
            received = _existing_directory(receive_directory, "上传保存")
            if not 1 <= max_upload_mb <= 10_000:
                raise ServiceError("上传上限必须在 1 到 10000 MB 之间。")
            directories_ready_at = perf_counter()

            try:
                interfaces = self._discover()
                interface = self._select(interfaces, interface_selector)
            except Exception as exc:
                raise ServiceError(str(exc)) from exc
            network_ready_at = perf_counter()

            server = None
            try:
                lifecycle = (
                    self._lifecycle_factory()
                    if self._lifecycle_factory is not None
                    else SessionLifecycle(self._duration_seconds, self._grace_seconds)
                )
                lifecycle_ready_at = perf_counter()
                application, pairing_code = self._application_factory(
                    WebConfig(
                        shared_directory=shared,
                        receive_directory=received,
                        max_upload_bytes=max_upload_mb * 1_000_000,
                        credentials=self._credentials,
                        lifecycle=lifecycle,
                    )
                )
                application_ready_at = perf_counter()
                server = self._server_factory(interface.address, self._port, application)
                server_ready_at = perf_counter()
                baseline = endpoint_baseline(interface)
                endpoint_checker = self._endpoint_checker_factory(baseline)
                lifecycle.activate()
                lifecycle_activated_at = perf_counter()
            except OSError as exc:
                if server is not None:
                    server.close()
                raise ServiceError(_format_bind_error(exc, interface.address, self._port)) from exc
            except Exception as exc:
                if server is not None:
                    server.close()
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
                network_name=interface.description,
                interface_index=interface.interface_index,
                bound_ipv4=interface.address,
                network_category=interface.category,
                endpoint_status="healthy",
                endpoint_detail="启动时已确认 Private endpoint。",
                diagnostics=_checking_diagnostics(interfaces),
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
            worker_started_at = perf_counter()
            watchdog_stop = threading.Event()
            watchdog = threading.Thread(
                target=self._watch_lifecycle,
                args=(server, lifecycle, watchdog_stop),
                name="LanDrop-Lifecycle",
                daemon=True,
            )
            self._watchdog = watchdog
            self._watchdog_stop = watchdog_stop
            watchdog.start()
            watchdog_started_at = perf_counter()
            network_stop = threading.Event()
            network_monitor = threading.Thread(
                target=self._watch_network,
                args=(server, lifecycle, endpoint_checker, network_stop),
                name="LanDrop-Network-Monitor",
                daemon=True,
            )
            self._network_monitor = network_monitor
            self._network_stop = network_stop
            network_monitor.start()
            network_monitor_started_at = perf_counter()
            self._start_diagnostics_locked(interfaces)
            diagnostics_started_at = perf_counter()
            state = self._current_snapshot_locked()
            ready_at = perf_counter()
            if self._emit_performance_timings:
                logger.debug(
                    "[启动耗时] "
                    f"目录 {directories_ready_at - started_at:.3f}s；"
                    f"网络检测 {network_ready_at - directories_ready_at:.3f}s；"
                    f"会话 {lifecycle_ready_at - network_ready_at:.3f}s；"
                    f"Web 应用 {application_ready_at - lifecycle_ready_at:.3f}s；"
                    f"端口绑定 {server_ready_at - application_ready_at:.3f}s；"
                    f"会话激活 {lifecycle_activated_at - server_ready_at:.3f}s；"
                    f"服务线程 {worker_started_at - lifecycle_activated_at:.3f}s；"
                    f"生命周期监控 {watchdog_started_at - worker_started_at:.3f}s；"
                    f"网络监控 {network_monitor_started_at - watchdog_started_at:.3f}s；"
                    f"诊断调度 {diagnostics_started_at - network_monitor_started_at:.3f}s；"
                    f"状态快照 {ready_at - diagnostics_started_at:.3f}s；"
                    f"总计 {ready_at - started_at:.3f}s"
                )
            return state

    def stop(
        self,
        reason: str = "manual_stop",
        *,
        _expected_server: Any | None = None,
        _expected_lifecycle: SessionLifecycle | None = None,
    ) -> ServiceSnapshot:
        stop_started_at = perf_counter()
        with self._lock:
            context = self._begin_stop_locked(
                reason,
                expected_server=_expected_server,
                expected_lifecycle=_expected_lifecycle,
            )
            if context is None:
                return self._current_snapshot_locked()
        return self._finish_stop(context, reason, stop_started_at)

    def _begin_stop_locked(
        self,
        reason: str,
        *,
        expected_server: Any | None = None,
        expected_lifecycle: SessionLifecycle | None = None,
    ) -> _StopContext | None:
        """Atomically mark the current service as stopping; caller holds ``_lock``."""
        server = self._server
        lifecycle = self._lifecycle
        if (
            (expected_server is not None and server is not expected_server)
            or (expected_lifecycle is not None and lifecycle is not expected_lifecycle)
            or server is None
        ):
            return None
        if lifecycle is not None:
            lifecycle.stop(reason, reason)
        if self._watchdog_stop is not None:
            self._watchdog_stop.set()
        if self._network_stop is not None:
            self._network_stop.set()
        self._snapshot = _stopped_from(self._snapshot, "正在停止服务……", "stopping")
        return _StopContext(
            server,
            self._worker,
            self._watchdog,
            self._network_monitor,
            lifecycle,
        )

    def _finish_stop(
        self,
        context: _StopContext,
        reason: str,
        stop_started_at: float,
    ) -> ServiceSnapshot:
        """Release sockets and join threads after the atomic state transition."""
        server = context.server
        lifecycle = context.lifecycle
        server.close()
        server_closed_at = perf_counter()
        for thread, timeout in (
            (context.worker, 5),
            (context.watchdog, 2),
            (context.network_monitor, 2),
        ):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=timeout)
        threads_joined_at = perf_counter()

        with self._lock:
            if self._server is server:
                lifecycle_snapshot = lifecycle.snapshot() if lifecycle is not None else None
                if lifecycle_snapshot is not None:
                    self._record_session_locked(lifecycle_snapshot)
                self._server = None
                self._worker = None
                self._watchdog = None
                self._watchdog_stop = None
                self._network_monitor = None
                self._network_stop = None
                self._lifecycle = None
                self._snapshot = _stopped_from(
                    self._snapshot,
                    _stop_message(reason),
                    lifecycle_snapshot=lifecycle_snapshot,
                )
            if self._emit_performance_timings:
                logger.debug(
                    "[停止耗时] "
                    f"服务器 {server_closed_at - stop_started_at:.3f}s；"
                    f"线程回收 {threads_joined_at - server_closed_at:.3f}s；"
                    f"总计 {perf_counter() - stop_started_at:.3f}s"
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

    def refresh_diagnostics(self) -> ServiceSnapshot:
        """Queue a full read-only diagnostic refresh without blocking the GUI."""
        with self._lock:
            if (self._snapshot.diagnostics or {}).get("status") == "checking":
                return self._current_snapshot_locked()
            self._diagnostics_generation += 1
            generation = self._diagnostics_generation
            current = dict(self._snapshot.diagnostics or _empty_diagnostics())
            current["status"] = "checking"
            current["message"] = "正在重新检测网络与防火墙……"
            firewall = dict(current.get("firewall") or {})
            firewall.update({"status": "checking", "message": "检测中"})
            current["firewall"] = firewall
            self._snapshot = replace(self._snapshot, diagnostics=current)
            thread = threading.Thread(
                target=self._run_diagnostics,
                args=(generation, None, True),
                name="LanDrop-Deep-Diagnostics",
                daemon=True,
            )
            thread.start()
            return self._current_snapshot_locked()

    def apply_expected_action(
        self,
        action: str,
        *,
        session_id: str,
        deadline_revision: int,
    ) -> tuple[bool, ServiceSnapshot]:
        """Apply a Toast action only if its session state is still current.

        The comparison and the service state transition share this controller's
        lock, so an old notification cannot validate one revision and mutate a
        newer one after a concurrent reset or restart.
        """
        if action not in {"reset", "stop"}:
            raise ValueError(f"未知的受保护动作：{action}")
        with self._lock:
            current = self._current_snapshot_locked()
            if (
                not current.running
                or current.session_id != session_id
                or current.deadline_revision != deadline_revision
            ):
                return False, current
            if action == "reset":
                try:
                    self._lifecycle.reset_deadline(300)  # type: ignore[union-attr]
                except SessionExpiredError:
                    return False, self._current_snapshot_locked()
                return True, self._current_snapshot_locked()
            context = self._begin_stop_locked("manual_stop")
            if context is None:
                return False, self._current_snapshot_locked()
        return True, self._finish_stop(context, "manual_stop", perf_counter())

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
                    if self._watchdog_stop is not None:
                        self._watchdog_stop.set()
                    self._watchdog_stop = None
                    self._network_monitor = None
                    if self._network_stop is not None:
                        self._network_stop.set()
                    self._network_stop = None
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

    def _watch_lifecycle(
        self,
        server: Any,
        lifecycle: SessionLifecycle,
        stop_check: threading.Event,
    ) -> None:
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

    def _watch_network(
        self,
        server: Any,
        lifecycle: SessionLifecycle,
        checker: Any,
        stop_check: threading.Event,
    ) -> None:
        # Startup discovery has just confirmed the category.  Do not launch a
        # redundant PowerShell profile query while the initial background
        # diagnostics are also warming up; address checks still start at the
        # normal lightweight interval.
        next_category_check = monotonic() + self._category_check_interval
        while not stop_check.wait(self._endpoint_check_interval):
            now = monotonic()
            include_category = now >= next_category_check
            observation = checker.observe(include_category=include_category)
            if include_category:
                next_category_check = now + self._category_check_interval

            if observation.explicitly_public:
                self._stop_for_network_change(
                    server,
                    lifecycle,
                    "category_changed：当前 endpoint 已明确变为 Public",
                )
                return
            if _endpoint_observation_matches(observation, include_category):
                self._set_endpoint_status(server, "healthy", "运行期 endpoint 校验正常。")
                continue

            self._set_endpoint_status(
                server,
                "confirming",
                _observation_detail(observation, include_category=include_category),
            )
            if stop_check.wait(self._endpoint_confirmation_delay):
                return
            confirmed = checker.observe(include_category=include_category)
            if confirmed.explicitly_public:
                self._stop_for_network_change(
                    server,
                    lifecycle,
                    "category_changed：当前 endpoint 已明确变为 Public",
                )
                return
            if _endpoint_observation_matches(confirmed, include_category):
                self._set_endpoint_status(server, "healthy", "瞬时异常已恢复，服务继续。")
                continue
            stop_reason = _observation_stop_reason(
                confirmed,
                include_category=include_category,
            )
            self._stop_for_network_issue(
                server,
                lifecycle,
                stop_reason,
                _observation_detail(confirmed, include_category=include_category),
            )
            return

    def _set_endpoint_status(self, server: Any, status: str, detail: str) -> None:
        with self._lock:
            if self._server is server:
                self._snapshot = replace(
                    self._snapshot,
                    endpoint_status=status,
                    endpoint_detail=detail,
                )

    def _stop_for_network_change(
        self,
        server: Any,
        lifecycle: SessionLifecycle,
        detail: str,
    ) -> None:
        self._stop_for_network_issue(server, lifecycle, "network_changed", detail)

    def _stop_for_network_issue(
        self,
        server: Any,
        lifecycle: SessionLifecycle,
        reason: str,
        detail: str,
    ) -> None:
        endpoint_status = "unavailable" if reason == "network_category_unavailable" else "changed"
        with self._lock:
            if self._server is not server or self._lifecycle is not lifecycle:
                return
            self._snapshot = replace(
                self._snapshot,
                endpoint_status=endpoint_status,
                endpoint_detail=detail,
            )
        label = "网络类别无法确认" if reason == "network_category_unavailable" else "网络变化"
        logger.warning("[%s] %s", label, detail)
        self.stop(
            reason,
            _expected_server=server,
            _expected_lifecycle=lifecycle,
        )

    def _start_diagnostics_locked(self, interfaces: list[Any]) -> None:
        self._diagnostics_generation += 1
        generation = self._diagnostics_generation
        thread = threading.Thread(
            target=self._run_diagnostics,
            args=(generation, interfaces, False),
            name="LanDrop-Deep-Diagnostics",
            daemon=True,
        )
        thread.start()

    def _run_diagnostics(
        self,
        generation: int,
        interfaces: list[Any] | None,
        refresh_interfaces: bool,
    ) -> None:
        discovery_error = ""
        if refresh_interfaces:
            try:
                interfaces = self._discover()
            except Exception as exc:
                interfaces = None
                discovery_error = str(exc)
        try:
            result = self._diagnostics_factory(self._port, sys.executable)
        except Exception as exc:
            result = {
                "status": "unknown",
                "message": f"深度诊断失败：{exc}",
                "network": {"status": "unknown", "adapters": []},
                "firewall": {
                    "status": "unknown",
                    "level": "unknown",
                    "message": f"防火墙诊断失败：{exc}",
                    "evidence": [],
                },
            }
        diagnostics = dict(result)
        network = dict(diagnostics.get("network") or {})
        if interfaces is not None:
            network["interfaces"] = [_interface_diagnostic(item) for item in interfaces]
        if discovery_error:
            network["discovery_error"] = discovery_error
            if network.get("status") != "ready":
                network["message"] = discovery_error
        diagnostics["network"] = network
        diagnostics.setdefault("message", "网络与防火墙诊断已更新。")
        with self._lock:
            if generation != self._diagnostics_generation:
                return
            self._snapshot = replace(self._snapshot, diagnostics=diagnostics)

    def _current_snapshot_locked(self) -> ServiceSnapshot:
        if self._lifecycle is None or self._server is None:
            return self._snapshot
        if self._snapshot.phase == "stopping":
            # Do not expose the lifecycle's stopped phase until server.close()
            # has returned and the listening port is actually released.
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
            deadline_revision=lifecycle.deadline_revision,
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
            logger.error("无法写入会话统计：%s", exc)


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
        interface=current.interface,
        network_name=current.network_name,
        interface_index=current.interface_index,
        bound_ipv4=current.bound_ipv4,
        network_category=current.network_category,
        endpoint_status=current.endpoint_status,
        endpoint_detail=current.endpoint_detail,
        diagnostics=current.diagnostics,
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
        "app_exit": "LanDrop 已退出，服务和端口已关闭。",
        "deadline_no_active": "5 分钟会话已到期，端口已自动关闭。",
        "deadline_transfers_completed": "现有传输已完成，端口已自动关闭。",
        "grace_timeout": "传输宽限时间已结束，端口已强制关闭。",
        "system_resume": "检测到服务机从睡眠恢复，会话已安全终止。",
        "network_changed": "网络环境已变化，传输服务已安全停止，请重新开启。",
        "network_category_unavailable": (
            "无法确认当前网络仍为 Private，传输服务已安全停止，请检查网络后重新开启。"
        ),
    }
    return messages.get(reason, "服务已停止，端口已关闭。")


def _endpoint_observation_matches(observation: Any, include_category: bool) -> bool:
    if observation.error or observation.address_present is not True:
        return False
    return not include_category or observation.category_private


def _observation_detail(observation: Any, *, include_category: bool = False) -> str:
    if include_category and observation.address_present is True:
        category = (observation.category or "").strip()
        if observation.error:
            return f"category_unavailable：{observation.error}"
        if not category or category.casefold() == "unknown":
            return "category_unavailable：Windows 返回 Unknown，无法确认当前网络仍为 Private"
        if category.casefold() != "private":
            return f"category_changed：当前网络类别为 {category}"
    if observation.error:
        return f"monitor_error：{observation.error}"
    if observation.address_present is not True:
        return "address_changed：启动时绑定的 InterfaceIndex + IPv4 已不存在"
    if observation.category and not observation.category_private:
        return f"category_changed：当前网络类别为 {observation.category}"
    return "endpoint_changed：当前 endpoint 与启动基线不一致"


def _observation_stop_reason(observation: Any, *, include_category: bool) -> str:
    if include_category and observation.address_present is True:
        category = (observation.category or "").strip().casefold()
        if observation.error or not category or category == "unknown":
            return "network_category_unavailable"
    return "network_changed"


def _interface_diagnostic(interface: Any) -> dict[str, object]:
    converter = getattr(interface, "to_diagnostic_dict", None)
    if callable(converter):
        return converter()
    return {
        "alias": str(getattr(interface, "alias", "")),
        "interface_index": int(getattr(interface, "interface_index", 0)),
        "address": str(getattr(interface, "address", "")),
        "category": str(getattr(interface, "category", "Unknown")),
        "connectivity": str(getattr(interface, "connectivity", "Unknown")),
        "has_gateway": getattr(interface, "has_gateway", None),
        "description": str(getattr(interface, "description", "")),
        "role": "lan_candidate",
    }


def _empty_diagnostics() -> dict[str, object]:
    return {
        "status": "idle",
        "message": "尚未执行深度诊断。",
        "network": {"status": "idle", "interfaces": [], "adapters": []},
        "firewall": {
            "status": "idle",
            "level": "unknown",
            "message": "尚未检测。",
            "evidence": [],
        },
    }


def _checking_diagnostics(interfaces: list[Any]) -> dict[str, object]:
    return {
        "status": "checking",
        "message": "服务已启动，正在异步读取网络与防火墙详细信息……",
        "network": {
            "status": "checking",
            "message": "正在读取详细信息。",
            "interfaces": [_interface_diagnostic(item) for item in interfaces],
            "adapters": [],
        },
        "firewall": {
            "status": "checking",
            "level": "unknown",
            "message": "检测中",
            "evidence": [],
        },
    }
