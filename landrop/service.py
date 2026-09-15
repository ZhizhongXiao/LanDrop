"""Thread-safe service lifecycle used by the desktop control window."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import errno
from pathlib import Path
import threading
from typing import Any, Callable

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
    max_upload_mib: int
    local_url: str = ""
    lan_url: str = ""
    interface: str = ""
    network_category: str = ""
    pairing_code: str = ""

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
    ) -> None:
        self._credentials = credentials
        self._port = port
        self._discover = discover
        self._select = select
        self._application_factory = application_factory
        self._server_factory = server_factory
        self._lock = threading.RLock()
        self._server: Any | None = None
        self._worker: threading.Thread | None = None
        self._snapshot = ServiceSnapshot(
            running=False,
            phase="stopped",
            message="服务未启动",
            shared_directory="",
            receive_directory="",
            max_upload_mib=1024,
        )

    def snapshot(self) -> ServiceSnapshot:
        with self._lock:
            return self._snapshot

    def start(
        self,
        shared_directory: str | Path,
        receive_directory: str | Path,
        max_upload_mib: int = 1024,
        interface_selector: str | None = None,
    ) -> ServiceSnapshot:
        with self._lock:
            if self._server is not None:
                raise ServiceError("服务已经在运行。")

            shared = _existing_directory(shared_directory, "共享")
            received = _existing_directory(receive_directory, "接收")
            if not 1 <= max_upload_mib <= 10_240:
                raise ServiceError("上传上限必须在 1 到 10240 MiB 之间。")

            try:
                interface = self._select(self._discover(), interface_selector)
            except Exception as exc:
                raise ServiceError(str(exc)) from exc

            try:
                application, pairing_code = self._application_factory(
                    WebConfig(
                        shared_directory=shared,
                        receive_directory=received,
                        max_upload_bytes=max_upload_mib * 1024 * 1024,
                        credentials=self._credentials,
                    )
                )
                server = self._server_factory(interface.address, self._port, application)
            except OSError as exc:
                raise ServiceError(_format_bind_error(exc, interface.address, self._port)) from exc
            except Exception as exc:
                raise ServiceError(str(exc)) from exc

            self._server = server
            self._snapshot = ServiceSnapshot(
                running=True,
                phase="running",
                message="服务正在运行",
                shared_directory=str(shared),
                receive_directory=str(received),
                max_upload_mib=max_upload_mib,
                local_url=f"http://127.0.0.1:{self._port}/",
                lan_url=f"http://{interface.address}:{self._port}/",
                interface=interface.alias,
                network_category=interface.category,
                pairing_code=pairing_code,
            )
            worker = threading.Thread(
                target=self._serve,
                args=(server,),
                name="LanDrop-GUI-Service",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return self._snapshot

    def stop(self) -> ServiceSnapshot:
        with self._lock:
            server = self._server
            worker = self._worker
            if server is None:
                return self._snapshot
            self._snapshot = _stopped_from(self._snapshot, "正在停止服务……", "stopping")

        server.close()
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)

        with self._lock:
            if self._server is server:
                self._server = None
                self._worker = None
                self._snapshot = _stopped_from(self._snapshot, "服务已停止，端口已关闭。")
            return self._snapshot

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
                    self._server = None
                    self._worker = None
                    self._snapshot = _stopped_from(
                        self._snapshot,
                        error or "服务已停止，端口已关闭。",
                        "error" if error else "stopped",
                    )


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
) -> ServiceSnapshot:
    return ServiceSnapshot(
        running=False,
        phase=phase,
        message=message,
        shared_directory=current.shared_directory,
        receive_directory=current.receive_directory,
        max_upload_mib=current.max_upload_mib,
    )
