"""Bound, temporary-copy Uninstall transaction for the Phase 8 lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Protocol

from .install_contract import (
    PRODUCT_ID,
    InstallPaths,
    UnsafeInstallPathError,
    is_reparse_object,
    new_transaction_id,
    uninstall_directory_name,
    validate_data_child,
    validate_install_child,
    validate_uninstall_temp_directory,
)
from .install_lock import InstallLifecycleLock
from .install_state import InstallHistoryLog, InstallRecord, InstallationStateStore
from .system_integration import (
    IntegrationPlan,
    SystemIntegrationRemovalResult,
    installed_size_kib,
)
from .upgrade import is_executable_running


UNINSTALL_REQUEST_SCHEMA_VERSION = 1
UNINSTALL_REQUEST_FILENAME = "request.json"
UNINSTALL_TEMP_LOG_FILENAME = "uninstall.log"
UNINSTALL_REQUEST_TTL_SECONDS = 300
_MAX_REQUEST_BYTES = 1024 * 1024
_SHA256_HEX_LENGTH = 64
_PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


class UninstallError(RuntimeError):
    """The uninstall request or cleanup boundary could not be trusted."""


@dataclass(frozen=True, slots=True)
class UninstallSelection:
    delete_config: bool = False
    delete_logs: bool = False
    delete_trusted_clients: bool = False

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> UninstallSelection:
        expected = {"delete_config", "delete_logs", "delete_trusted_clients"}
        if set(raw) != expected or any(not isinstance(raw[key], bool) for key in expected):
            raise UninstallError("卸载数据选项无效。")
        return cls(**{key: bool(raw[key]) for key in expected})

    def to_json(self) -> dict[str, bool]:
        return {
            "delete_config": self.delete_config,
            "delete_logs": self.delete_logs,
            "delete_trusted_clients": self.delete_trusted_clients,
        }


@dataclass(frozen=True, slots=True)
class UninstallRequest:
    nonce: str
    created_at: str
    expires_at: str
    original_pid: int
    install_root: str
    product_id: str
    version: str
    build_id: str
    selection: UninstallSelection
    source_uninstaller_sha256: str
    schema_version: int = UNINSTALL_REQUEST_SCHEMA_VERSION

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product_id": self.product_id,
            "nonce": self.nonce,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "original_pid": self.original_pid,
            "install_root": self.install_root,
            "version": self.version,
            "build_id": self.build_id,
            "selection": self.selection.to_json(),
            "source_uninstaller_sha256": self.source_uninstaller_sha256,
        }


@dataclass(frozen=True, slots=True)
class UninstallHandoff:
    temporary_directory: Path
    temporary_executable: Path
    request_path: Path
    nonce: str
    expected_request_sha256: str
    original_pid: int

    def command(self) -> list[str]:
        return [
            str(self.temporary_executable),
            "--execute-request",
            str(self.request_path),
            "--nonce",
            self.nonce,
            "--expected-request-sha256",
            self.expected_request_sha256,
        ]


@dataclass(frozen=True, slots=True)
class UninstallOutcome:
    complete: bool
    message: str
    finalization_pending: bool = False
    removed_integration: tuple[str, ...] = ()
    absent_integration: tuple[str, ...] = ()
    residuals: tuple[str, ...] = ()
    deleted_data: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "complete": self.complete,
            "message": self.message,
            "finalization_pending": self.finalization_pending,
            "removed_integration": list(self.removed_integration),
            "absent_integration": list(self.absent_integration),
            "residuals": list(self.residuals),
            "deleted_data": list(self.deleted_data),
        }


class UninstallIntegration(Protocol):
    def remove_owned(self, plan: IntegrationPlan) -> SystemIntegrationRemovalResult: ...


class UninstallLifecycleService:
    """Prepare a bound handoff and execute it only from the controlled TEMP copy."""

    def __init__(
        self,
        *,
        paths: InstallPaths,
        integration: UninstallIntegration,
        state_store: InstallationStateStore | None = None,
        history: InstallHistoryLog | None = None,
        now: Callable[[], datetime] | None = None,
        lock_factory: Callable[[InstallPaths], object] | None = None,
        process_checker: Callable[[Path], bool] | None = None,
        pid_waiter: Callable[[int, float], bool] | None = None,
        executable_waiter: Callable[[Path, float], bool] | None = None,
    ) -> None:
        self.paths = paths
        self.integration = integration
        self.state_store = state_store or InstallationStateStore(paths)
        self.history = history or InstallHistoryLog(paths)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock_factory = lock_factory or (lambda target: InstallLifecycleLock(target))
        self._process_checker = process_checker or is_executable_running
        self._pid_waiter = pid_waiter or wait_for_process_exit
        self._executable_waiter = executable_waiter or wait_for_executable_exit

    def inspect_installed(self, source_executable: Path) -> InstallRecord:
        source = _absolute(source_executable)
        if not _same_path(source, self.paths.uninstall_executable):
            raise UninstallError("卸载器不在当前固定安装结构中。")
        if not source.is_file() or is_reparse_object(source):
            raise UninstallError("安装目录中的卸载器缺失或不安全。")
        record = self.state_store.read_install(required=True)
        assert record is not None
        self._assert_install_identity(record)
        if os.path.lexists(self.paths.transaction_state_path):
            # Reading forces damaged state to fail closed as well.
            transaction = self.state_store.read_transaction(required=True)
            raise UninstallError(
                f"检测到未完成的安装事务（{transaction.stage}），请先重新运行 Setup。"
            )
        if self._process_checker(self.paths.main_executable):
            raise UninstallError("LanDrop 仍在运行，请先从系统托盘退出后重试。")
        return record

    def prepare_handoff(
        self,
        *,
        source_executable: Path,
        selection: UninstallSelection,
        original_pid: int,
    ) -> UninstallHandoff:
        record = self.inspect_installed(source_executable)
        if isinstance(original_pid, bool) or original_pid <= 0:
            raise UninstallError("原卸载器 PID 无效。")
        source = _absolute(source_executable)
        source_hash = sha256_file(source)
        nonce = secrets.token_hex(32)
        transaction_id = new_transaction_id()
        temporary_directory = self.paths.uninstall_temp_root / uninstall_directory_name(
            transaction_id
        )
        validate_uninstall_temp_directory(temporary_directory, self.paths)
        if os.path.lexists(temporary_directory):
            raise UninstallError("临时卸载目录已存在，拒绝覆盖。")

        request_path = temporary_directory / UNINSTALL_REQUEST_FILENAME
        temporary_executable = temporary_directory / "Uninstall.exe"
        created = self._now().astimezone(timezone.utc)
        request = UninstallRequest(
            nonce=nonce,
            created_at=created.isoformat(),
            expires_at=(created + timedelta(seconds=UNINSTALL_REQUEST_TTL_SECONDS)).isoformat(),
            original_pid=original_pid,
            install_root=str(self.paths.install_root),
            product_id=PRODUCT_ID,
            version=record.version,
            build_id=record.build_id,
            selection=selection,
            source_uninstaller_sha256=source_hash,
        )
        try:
            self.paths.uninstall_temp_root.mkdir(parents=True, exist_ok=True)
            validate_uninstall_temp_directory(temporary_directory, self.paths)
            temporary_directory.mkdir()
            shutil.copyfile(source, temporary_executable)
            if sha256_file(temporary_executable) != source_hash:
                raise UninstallError("临时卸载器复制后哈希不一致。")
            request_bytes = canonical_request_bytes(request)
            _write_new_file(request_path, request_bytes)
            expected_hash = hashlib.sha256(request_bytes).hexdigest()
            _write_temp_log(temporary_directory, "handoff_prepared")
            return UninstallHandoff(
                temporary_directory=temporary_directory,
                temporary_executable=temporary_executable,
                request_path=request_path,
                nonce=nonce,
                expected_request_sha256=expected_hash,
                original_pid=original_pid,
            )
        except Exception:
            try:
                safe_remove_temp_tree(temporary_directory, self.paths)
            except Exception:
                pass
            raise

    def execute_handoff(
        self,
        *,
        current_executable: Path,
        request_path: Path,
        nonce: str,
        expected_request_sha256: str,
    ) -> UninstallOutcome:
        temporary_directory = validate_uninstall_temp_directory(
            _absolute(current_executable).parent,
            self.paths,
        )
        current = temporary_directory / "Uninstall.exe"
        if not _same_path(current_executable, current):
            raise UninstallError("临时卸载器路径不符合冻结结构。")
        expected_request_path = temporary_directory / UNINSTALL_REQUEST_FILENAME
        if not _same_path(request_path, expected_request_path):
            raise UninstallError("卸载 request 路径不符合冻结结构。")
        request = read_bound_request(
            expected_request_path,
            nonce=nonce,
            expected_sha256=expected_request_sha256,
            now=self._now(),
        )
        if not _same_path(Path(request.install_root), self.paths.install_root):
            raise UninstallError("request 安装根不是当前用户固定安装根。")
        if sha256_file(current) != request.source_uninstaller_sha256:
            raise UninstallError("临时卸载器与安装目录源文件哈希不一致。")
        if not self._pid_waiter(request.original_pid, 30.0):
            raise UninstallError("原安装目录卸载器仍在运行，拒绝删除程序根。")
        if not self._executable_waiter(self.paths.uninstall_executable, 30.0):
            raise UninstallError("安装目录中的卸载器进程尚未完全退出，拒绝删除程序根。")

        lock = self._lock_factory(self.paths)
        acquired = False
        try:
            acquired = bool(lock.acquire(30.0))  # type: ignore[attr-defined]
            if not acquired:
                raise UninstallError("LanDrop 正在安装、升级或卸载，请稍后重试。")
            record = self.state_store.read_install(required=True)
            assert record is not None
            self._assert_request_still_current(request, record)
            if os.path.lexists(self.paths.transaction_state_path):
                self.state_store.read_transaction(required=True)
                raise UninstallError("检测到未完成的安装事务，旧卸载请求已拒绝。")
            if self._process_checker(self.paths.main_executable):
                raise UninstallError("LanDrop 仍在运行，请先从系统托盘退出后重试。")
            return self._remove_committed_install(request, record, temporary_directory)
        finally:
            if acquired:
                lock.close()  # type: ignore[attr-defined]
            else:
                try:
                    lock.close()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _remove_committed_install(
        self,
        request: UninstallRequest,
        record: InstallRecord,
        temporary_directory: Path,
    ) -> UninstallOutcome:
        preflight_install_root_removal(self.paths)
        residuals: list[str] = []
        deleted_data: list[str] = []
        _write_temp_log(temporary_directory, "uninstall_started")
        if not request.selection.delete_logs:
            try:
                self.history.append(
                    "uninstall_started",
                    version=record.version,
                    result="started",
                )
            except Exception as exc:
                residuals.append(f"无法记录 uninstall_started：{exc}")

        estimated_size = _registered_size_fallback(self.paths)
        plan = IntegrationPlan.create(
            self.paths,
            version=record.version,
            estimated_size_kib=estimated_size,
            desktop_enabled=False,
        )
        integration_result = self.integration.remove_owned(plan)
        residuals.extend(integration_result.residuals)

        try:
            safe_remove_install_root(self.paths)
            _write_temp_log(temporary_directory, "program_root_removed")
        except Exception as exc:
            residuals.append(f"程序根未完整删除：{exc}")

        try:
            deleted_data.extend(clean_selected_user_data(self.paths, request.selection))
        except Exception as exc:
            residuals.append(f"用户数据清理未完成：{exc}")

        complete = not residuals
        message = "LanDrop 已完整卸载。" if complete else "卸载未完全完成，请查看残留项。"
        if not request.selection.delete_logs:
            try:
                self.history.append(
                    "uninstall_completed",
                    version=record.version,
                    result="success" if complete else "partial",
                    details={"residual_count": len(residuals)},
                )
            except Exception as exc:
                residuals.append(f"无法记录 uninstall_completed：{exc}")
                complete = False
                message = "卸载未完全完成，请查看残留项。"
        _write_temp_log(
            temporary_directory,
            "uninstall_completed" if complete else "uninstall_partial",
        )
        return UninstallOutcome(
            complete=complete,
            message=message,
            removed_integration=integration_result.removed,
            absent_integration=integration_result.absent,
            residuals=tuple(residuals),
            deleted_data=tuple(deleted_data),
        )

    def _assert_install_identity(self, record: InstallRecord) -> None:
        if record.product_id != PRODUCT_ID or not _same_path(
            Path(record.install_root), self.paths.install_root
        ):
            raise UninstallError("install.json 产品身份或固定安装根不匹配。")
        for directory in (
            self.paths.install_root,
            self.paths.app_directory,
            self.paths.maintenance_directory,
            self.paths.metadata_directory,
        ):
            validate_install_child(
                directory,
                self.paths,
                allow_root=_same_path(directory, self.paths.install_root),
            )
            if not directory.is_dir() or is_reparse_object(directory):
                raise UninstallError(f"安装结构缺失或不安全：{directory}")
        if not self.paths.main_executable.is_file() or is_reparse_object(
            self.paths.main_executable
        ):
            raise UninstallError("正式 LanDrop.exe 缺失或不安全。")

    def _assert_request_still_current(
        self,
        request: UninstallRequest,
        record: InstallRecord,
    ) -> None:
        self._assert_install_identity(record)
        if (
            request.product_id != record.product_id
            or request.version != record.version
            or request.build_id != record.build_id
            or not _same_path(Path(request.install_root), Path(record.install_root))
        ):
            raise UninstallError(
                "安装状态已变化，旧卸载请求已拒绝；请从当前 Windows 卸载入口重试。"
            )


def canonical_request_bytes(request: UninstallRequest) -> bytes:
    validated = _request_from_json(request.to_json())
    return (
        json.dumps(
            validated.to_json(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def read_bound_request(
    path: Path,
    *,
    nonce: str,
    expected_sha256: str,
    now: datetime,
) -> UninstallRequest:
    source = _absolute(path)
    if not source.is_file() or is_reparse_object(source):
        raise UninstallError("卸载 request 缺失、不是普通文件或是 reparse object。")
    if source.stat().st_size > _MAX_REQUEST_BYTES:
        raise UninstallError("卸载 request 超过 1 MiB 上限。")
    try:
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != _required_sha256(expected_sha256):
            raise UninstallError("卸载 request SHA-256 不匹配。")
        raw = json.loads(payload.decode("utf-8"))
    except UninstallError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UninstallError(f"无法读取卸载 request：{exc}") from exc
    if not isinstance(raw, dict):
        raise UninstallError("卸载 request 根节点必须是 JSON 对象。")
    request = _request_from_json(raw)
    if payload != canonical_request_bytes(request):
        raise UninstallError("卸载 request 不是规范化编码。")
    if not secrets.compare_digest(request.nonce, _required_nonce(nonce)):
        raise UninstallError("卸载 request nonce 不匹配。")
    current = now.astimezone(timezone.utc)
    created = _timestamp(request.created_at)
    expires = _timestamp(request.expires_at)
    if expires <= created or expires - created > timedelta(seconds=UNINSTALL_REQUEST_TTL_SECONDS):
        raise UninstallError("卸载 request 有效期无效。")
    if current < created - timedelta(seconds=5) or current > expires:
        raise UninstallError("卸载 request 已过期或尚未生效。")
    return request


def safe_remove_install_root(paths: InstallPaths) -> None:
    root = preflight_install_root_removal(paths)
    _remove_tree_entries(root)
    root.rmdir()


def preflight_install_root_removal(paths: InstallPaths) -> Path:
    root = validate_install_child(paths.install_root, paths, allow_root=True)
    if not os.path.lexists(root):
        raise UninstallError("固定程序根不存在，拒绝执行不完整卸载。")
    _preflight_tree(root)
    return root


def safe_remove_temp_tree(target: Path, paths: InstallPaths) -> None:
    validated = validate_uninstall_temp_directory(target, paths)
    if os.path.lexists(validated):
        _safe_remove_tree(validated)


def clean_selected_user_data(
    paths: InstallPaths,
    selection: UninstallSelection,
) -> tuple[str, ...]:
    deleted: list[str] = []
    if selection.delete_config:
        if _remove_allowlisted_file(paths.data_root / "config.json", paths):
            deleted.append("config")
    if selection.delete_trusted_clients:
        if _remove_allowlisted_file(paths.data_root / "credentials.json", paths):
            deleted.append("trusted_clients")
    if selection.delete_logs:
        log_directory = validate_data_child(paths.log_directory, paths)
        if os.path.lexists(log_directory):
            if is_reparse_object(log_directory) or not log_directory.is_dir():
                raise UninstallError("日志目录不是安全的普通目录。")
            for entry in tuple(log_directory.iterdir()):
                if _is_allowlisted_log_name(entry.name):
                    if entry.is_dir() or is_reparse_object(entry):
                        raise UninstallError(f"日志白名单对象类型不安全：{entry.name}")
                    entry.unlink()
            try:
                log_directory.rmdir()
            except OSError:
                pass
        deleted.append("logs")
    try:
        validate_data_child(paths.data_root, paths, allow_root=True).rmdir()
    except OSError:
        pass
    return tuple(deleted)


def sha256_file(path: Path) -> str:
    source = _absolute(path)
    if not source.is_file() or is_reparse_object(source):
        raise UninstallError(f"哈希目标不是安全的普通文件：{source}")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_file:
            while chunk := input_file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise UninstallError(f"无法计算文件哈希：{exc}") from exc
    return digest.hexdigest()


def wait_for_process_exit(pid: int, timeout_seconds: float) -> bool:
    if os.name != "nt":
        return pid != os.getpid()
    if isinstance(pid, bool) or pid <= 0:
        raise UninstallError("原卸载器 PID 无效。")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        result = int(kernel32.WaitForSingleObject(handle, max(0, int(timeout_seconds * 1000))))
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise UninstallError(f"等待原卸载器退出失败（Windows 错误 {ctypes.get_last_error()}）。")
    finally:
        kernel32.CloseHandle(handle)


def wait_for_executable_exit(executable: Path, timeout_seconds: float) -> bool:
    """Wait for every PyInstaller bootloader/child using the installed path."""
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while is_executable_running(executable):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def launch_temporary_uninstaller(handoff: UninstallHandoff) -> None:
    try:
        subprocess.Popen(
            handoff.command(),
            cwd=str(handoff.temporary_directory),
            close_fds=True,
        )
    except OSError as exc:
        raise UninstallError(f"无法启动临时卸载器：{exc}") from exc


def schedule_temp_self_cleanup(temporary_directory: Path, paths: InstallPaths) -> None:
    target = validate_uninstall_temp_directory(temporary_directory, paths)
    _preflight_tree(target)
    cleanup_cwd = paths.temp_directory.resolve(strict=True)
    if _is_same_or_child(cleanup_cwd, target):
        raise UninstallError("临时卸载清理器工作目录不得位于待删除目录内。")
    powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / (
        r"System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    escaped = str(target).replace("'", "''")
    process_ids = [os.getpid()]
    if getattr(sys, "frozen", False):
        parent_pid = os.getppid()
        if parent_pid > 0 and parent_pid not in process_ids:
            process_ids.append(parent_pid)
    process_id_list = ", ".join(str(process_id) for process_id in process_ids)
    script = (
        f"$processIds = @({process_id_list}); "
        "foreach ($processId in $processIds) { "
        "Wait-Process -Id $processId -ErrorAction SilentlyContinue }; "
        "$deleted = $false; "
        "for ($attempt = 0; $attempt -lt 30; $attempt++) { "
        "try { "
        f"if (Test-Path -LiteralPath '{escaped}') {{ "
        f"Remove-Item -LiteralPath '{escaped}' -Recurse -Force -ErrorAction Stop "
        "} "
        "} catch { }; "
        f"if (-not (Test-Path -LiteralPath '{escaped}')) {{ "
        "$deleted = $true; break }; "
        "$delay = if ($attempt -lt 8) { 250 } else { 500 }; "
        "Start-Sleep -Milliseconds $delay "
        "}; "
        "if ($deleted) { exit 0 }; "
        "try { "
        "Add-Type -AssemblyName PresentationFramework -ErrorAction Stop; "
        "[System.Windows.MessageBox]::Show("
        "'LanDrop 临时卸载文件未能自动清理。请稍后删除对应的 uninstall-* 临时目录。', "
        "'LanDrop Uninstall') | Out-Null "
        "} catch { }; "
        "exit 1"
    )
    encoded = __import__("base64").b64encode(script.encode("utf-16le")).decode("ascii")
    subprocess.Popen(
        [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ],
        cwd=str(cleanup_cwd),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _request_from_json(raw: Mapping[str, object]) -> UninstallRequest:
    expected = {
        "schema_version",
        "product_id",
        "nonce",
        "created_at",
        "expires_at",
        "original_pid",
        "install_root",
        "version",
        "build_id",
        "selection",
        "source_uninstaller_sha256",
    }
    if set(raw) != expected or raw.get("schema_version") != UNINSTALL_REQUEST_SCHEMA_VERSION:
        raise UninstallError("卸载 request schema 或字段集合不受支持。")
    if raw.get("product_id") != PRODUCT_ID:
        raise UninstallError("卸载 request 产品身份不匹配。")
    original_pid = raw.get("original_pid")
    selection = raw.get("selection")
    if isinstance(original_pid, bool) or not isinstance(original_pid, int) or original_pid <= 0:
        raise UninstallError("卸载 request PID 无效。")
    if not isinstance(selection, Mapping):
        raise UninstallError("卸载 request 数据选项无效。")
    text_fields = ("created_at", "expires_at", "install_root", "version", "build_id")
    if any(not isinstance(raw.get(name), str) or not raw.get(name) for name in text_fields):
        raise UninstallError("卸载 request 文本字段无效。")
    install_root = Path(str(raw["install_root"]))
    if not install_root.is_absolute():
        raise UninstallError("卸载 request 安装根必须是绝对路径。")
    return UninstallRequest(
        nonce=_required_nonce(raw.get("nonce")),
        created_at=_timestamp(str(raw["created_at"])).isoformat(),
        expires_at=_timestamp(str(raw["expires_at"])).isoformat(),
        original_pid=original_pid,
        install_root=str(_absolute(install_root)),
        product_id=PRODUCT_ID,
        version=str(raw["version"]),
        build_id=str(raw["build_id"]),
        selection=UninstallSelection.from_mapping(selection),
        source_uninstaller_sha256=_required_sha256(raw.get("source_uninstaller_sha256")),
    )


def _safe_remove_tree(root: Path) -> None:
    target = _absolute(root)
    if not os.path.lexists(target):
        return
    _preflight_tree(target)
    _remove_tree_entries(target)
    target.rmdir()


def _preflight_tree(root: Path) -> None:
    if is_reparse_object(root) or not root.is_dir():
        raise UnsafeInstallPathError(f"递归删除目标不是安全普通目录：{root}")
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if is_reparse_object(current):
            raise UnsafeInstallPathError(f"递归删除遇到 reparse object：{current}")
        for name in (*directory_names, *file_names):
            child = current / name
            if is_reparse_object(child):
                raise UnsafeInstallPathError(f"递归删除遇到 reparse object：{child}")
            mode = child.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise UnsafeInstallPathError(f"递归删除遇到未知文件类型：{child}")


def _remove_tree_entries(root: Path) -> None:
    for entry in tuple(os.scandir(root)):
        child = Path(entry.path)
        if is_reparse_object(child):
            raise UnsafeInstallPathError(f"删除期间遇到 reparse object：{child}")
        if entry.is_dir(follow_symlinks=False):
            _remove_tree_entries(child)
            child.rmdir()
        elif entry.is_file(follow_symlinks=False):
            child.unlink()
        else:
            raise UnsafeInstallPathError(f"删除期间遇到未知文件类型：{child}")


def _remove_allowlisted_file(path: Path, paths: InstallPaths) -> bool:
    target = validate_data_child(path, paths)
    if not os.path.lexists(target):
        return False
    if is_reparse_object(target) or not target.is_file():
        raise UninstallError(f"数据白名单对象类型不安全：{target.name}")
    target.unlink()
    return True


def _is_allowlisted_log_name(name: str) -> bool:
    bases = ("application.log", "sessions.jsonl", "install-history.jsonl", "setup.log")
    for base in bases:
        if name == base:
            return True
        if name.startswith(base + ".") and name[len(base) + 1 :].isdigit():
            return True
    return False


def _write_new_file(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise UninstallError(f"无法安全写入卸载 request：{exc}") from exc


def _write_temp_log(directory: Path, event: str) -> None:
    try:
        with (directory / UNINSTALL_TEMP_LOG_FILENAME).open(
            "a", encoding="utf-8", newline="\n"
        ) as output:
            output.write(
                json.dumps(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": event,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        pass


def _registered_size_fallback(paths: InstallPaths) -> int:
    try:
        return installed_size_kib(paths.install_root)
    except Exception:
        return 1


def _required_nonce(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise UninstallError("卸载 request nonce 无效。")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise UninstallError("卸载 request nonce 无效。") from exc
    return value.lower()


def _required_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH:
        raise UninstallError("SHA-256 值无效。")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise UninstallError("SHA-256 值无效。") from exc
    return value.lower()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise UninstallError("卸载 request 时间格式无效。") from exc
    if parsed.tzinfo is None:
        raise UninstallError("卸载 request 时间必须包含时区。")
    return parsed.astimezone(timezone.utc)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _is_same_or_child(candidate: Path, root: Path) -> bool:
    candidate_text = os.path.normcase(os.path.abspath(candidate))
    root_text = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath((candidate_text, root_text)) == root_text
    except ValueError:
        return False
