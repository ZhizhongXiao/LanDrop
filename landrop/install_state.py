"""Validated, crash-resistant Phase 8 installation state and history."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from typing import Callable, Mapping

from .install_contract import (
    MAIN_EXECUTABLE_RELATIVE,
    PRODUCT_ID,
    InstallContractError,
    InstallPaths,
    UnsafeInstallPathError,
    is_transaction_directory_name,
    transaction_directory_name,
    validate_data_child,
    validate_install_child,
)


INSTALL_SCHEMA_VERSION = 1
TRANSACTION_SCHEMA_VERSION = 1
INSTALL_HISTORY_VERSION = 1
TRANSACTION_KINDS = frozenset({"install", "upgrade"})
TRANSACTION_STAGES = (
    "prepared",
    "app_switched",
    "integration_written",
    "integration_verified",
)
INSTALL_HISTORY_EVENTS = frozenset(
    {
        "installed",
        "upgrade_started",
        "upgrade_committed",
        "rollback_started",
        "rollback_completed",
        "old_payload_removed",
        "cleanup_pending",
        "cleanup_completed",
        "uninstall_started",
        "uninstall_completed",
    }
)
_SENSITIVE_KEY_PARTS = ("token", "credential", "csrf", "pairing", "password", "secret")
_MAX_STATE_FILE_BYTES = 1024 * 1024


class InstallStateError(RuntimeError):
    """An installation state file is missing, invalid, or cannot be persisted."""


class IncompleteInstallationError(InstallStateError):
    """Normal LanDrop startup is blocked by an unfinished/unsafe transaction."""


class InstallHistoryError(RuntimeError):
    """The append-only installation history could not be written."""


@dataclass(frozen=True, slots=True)
class InstallRecord:
    product_id: str
    version: str
    build_id: str
    installed_at: str
    updated_at: str
    install_root: str
    app_relative_path: str = MAIN_EXECUTABLE_RELATIVE.as_posix()
    pending_cleanup: tuple[str, ...] = ()
    schema_version: int = INSTALL_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        paths: InstallPaths,
        *,
        version: str,
        build_id: str,
        installed_at: str | None = None,
        updated_at: str | None = None,
        pending_cleanup: tuple[str, ...] = (),
    ) -> InstallRecord:
        now = _utc_now()
        return cls(
            product_id=PRODUCT_ID,
            version=version,
            build_id=build_id,
            installed_at=installed_at or now,
            updated_at=updated_at or now,
            install_root=str(paths.install_root),
            pending_cleanup=pending_cleanup,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product_id": self.product_id,
            "version": self.version,
            "build_id": self.build_id,
            "installed_at": self.installed_at,
            "updated_at": self.updated_at,
            "install_root": self.install_root,
            "app_relative_path": self.app_relative_path,
            "pending_cleanup": list(self.pending_cleanup),
        }


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    transaction_id: str
    kind: str
    source_version: str | None
    source_build_id: str | None
    target_version: str
    target_build_id: str
    created_at: str
    updated_at: str
    stage: str
    staging_directory: str
    rollback_directory: str | None = None
    integration_snapshot: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = TRANSACTION_SCHEMA_VERSION

    @classmethod
    def create(
        cls,
        *,
        transaction_id: str,
        kind: str,
        target_version: str,
        target_build_id: str,
        staging_directory: str,
        source_version: str | None = None,
        source_build_id: str | None = None,
        rollback_directory: str | None = None,
        integration_snapshot: Mapping[str, object] | None = None,
        created_at: str | None = None,
    ) -> TransactionRecord:
        now = created_at or _utc_now()
        return cls(
            transaction_id=transaction_id,
            kind=kind,
            source_version=source_version,
            source_build_id=source_build_id,
            target_version=target_version,
            target_build_id=target_build_id,
            created_at=now,
            updated_at=now,
            stage="prepared",
            staging_directory=staging_directory,
            rollback_directory=rollback_directory,
            integration_snapshot=dict(integration_snapshot or {}),
        )

    def advance(self, stage: str, *, updated_at: str | None = None) -> TransactionRecord:
        if stage not in TRANSACTION_STAGES:
            raise InstallStateError(f"未知安装事务阶段：{stage}")
        current_index = TRANSACTION_STAGES.index(self.stage)
        target_index = TRANSACTION_STAGES.index(stage)
        if target_index != current_index + 1:
            raise InstallStateError(f"非法安装事务阶段跃迁：{self.stage} → {stage}")
        return replace(self, stage=stage, updated_at=updated_at or _utc_now())

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "kind": self.kind,
            "source_version": self.source_version,
            "source_build_id": self.source_build_id,
            "target_version": self.target_version,
            "target_build_id": self.target_build_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "stage": self.stage,
            "staging_directory": self.staging_directory,
            "rollback_directory": self.rollback_directory,
            "integration_snapshot": dict(self.integration_snapshot),
        }


class InstallationStateStore:
    """Read and atomically publish committed and in-progress install state."""

    def __init__(self, paths: InstallPaths | None = None) -> None:
        self.paths = paths or InstallPaths.from_environment()

    def read_install(self, *, required: bool = False) -> InstallRecord | None:
        raw = self._read_json(self.paths.install_state_path, required=required, label="install.json")
        if raw is None:
            return None
        return _install_record_from_json(raw, self.paths)

    def write_install(self, record: InstallRecord) -> None:
        validated = _install_record_from_json(record.to_json(), self.paths)
        self._write_json(self.paths.install_state_path, validated.to_json())

    def read_transaction(self, *, required: bool = False) -> TransactionRecord | None:
        raw = self._read_json(
            self.paths.transaction_state_path,
            required=required,
            label="transaction.json",
        )
        if raw is None:
            return None
        return _transaction_record_from_json(raw)

    def write_transaction(self, record: TransactionRecord) -> None:
        validated = _transaction_record_from_json(record.to_json())
        if os.path.lexists(self.paths.transaction_state_path):
            current = self.read_transaction(required=True)
            assert current is not None
            _validate_transaction_update(current, validated)
        elif validated.stage != "prepared":
            raise InstallStateError("新 transaction.json 必须从 prepared 阶段开始。")
        self._write_json(self.paths.transaction_state_path, validated.to_json())

    def remove_transaction(self) -> None:
        path = self.paths.transaction_state_path
        self._validate_state_path(path)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise InstallStateError(f"无法删除 transaction.json：{exc}") from exc

    def ensure_normal_start_allowed(self) -> None:
        path = self.paths.transaction_state_path
        if not os.path.lexists(path):
            return
        try:
            transaction = self.read_transaction(required=True)
        except InstallStateError as exc:
            raise IncompleteInstallationError(
                "检测到损坏或无法安全解释的安装事务；请重新运行 LanDrop Setup 完成恢复。"
            ) from exc
        assert transaction is not None
        raise IncompleteInstallationError(
            "检测到未完成的安装/升级"
            f"（{transaction.stage}）；请重新运行 LanDrop Setup 完成恢复。"
        )

    def _read_json(
        self,
        path: Path,
        *,
        required: bool,
        label: str,
    ) -> dict[str, object] | None:
        if not os.path.lexists(path):
            if required:
                raise InstallStateError(f"缺少 {label}。")
            return None
        self._validate_state_path(path)
        try:
            if path.stat().st_size > _MAX_STATE_FILE_BYTES:
                raise InstallStateError(f"{label} 超过 1 MiB 上限。")
            payload = json.loads(path.read_text(encoding="utf-8"))
        except InstallStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallStateError(f"无法读取 {label}：{exc}") from exc
        if not isinstance(payload, dict):
            raise InstallStateError(f"{label} 根节点必须是 JSON 对象。")
        return payload

    def _write_json(self, path: Path, payload: Mapping[str, object]) -> None:
        self._validate_state_path(path)
        try:
            self.paths.metadata_directory.mkdir(parents=True, exist_ok=True)
            self._validate_state_path(path)
            _atomic_write_json(path, payload)
        except (OSError, UnsafeInstallPathError) as exc:
            raise InstallStateError(f"无法原子写入 {path.name}：{exc}") from exc

    def _validate_state_path(self, path: Path) -> None:
        try:
            validate_install_child(path, self.paths)
        except UnsafeInstallPathError as exc:
            raise InstallStateError(f"安装状态路径不安全：{exc}") from exc


class InstallHistoryLog:
    """Append-only diagnostic history; never an authority for current state."""

    def __init__(
        self,
        paths: InstallPaths | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths or InstallPaths.from_environment()
        self.path = self.paths.install_history_path
        self._now = now or (lambda: datetime.now(timezone.utc))

    def append(
        self,
        event: str,
        *,
        version: str,
        result: str,
        from_version: str | None = None,
        to_version: str | None = None,
        transaction_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if event not in INSTALL_HISTORY_EVENTS:
            raise InstallHistoryError(f"未知安装历史事件：{event}")
        try:
            record: dict[str, object] = {
                "schema_version": INSTALL_HISTORY_VERSION,
                "timestamp": self._now().astimezone(timezone.utc).isoformat(),
                "event": event,
                "version": _required_text(version, "version"),
                "result": _required_text(result, "result"),
            }
            if from_version is not None:
                record["from_version"] = _required_text(from_version, "from_version")
            if to_version is not None:
                record["to_version"] = _required_text(to_version, "to_version")
            if transaction_id is not None:
                record["transaction_id"] = _required_transaction_id(transaction_id)
            if details is not None:
                cleaned_details = dict(details)
                _validate_json_mapping(cleaned_details, "details")
                _reject_sensitive_keys(cleaned_details)
                record["details"] = cleaned_details
        except InstallHistoryError:
            raise
        except InstallStateError as exc:
            raise InstallHistoryError(f"安装历史字段无效：{exc}") from exc
        try:
            validate_data_child(self.path, self.paths)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            validate_data_child(self.path, self.paths)
            with self.path.open("a", encoding="utf-8", newline="\n") as output:
                json.dump(record, output, ensure_ascii=False, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except (OSError, UnsafeInstallPathError) as exc:
            raise InstallHistoryError(f"无法写入安装历史：{exc}") from exc


def _install_record_from_json(raw: Mapping[str, object], paths: InstallPaths) -> InstallRecord:
    expected = {
        "schema_version",
        "product_id",
        "version",
        "build_id",
        "installed_at",
        "updated_at",
        "install_root",
        "app_relative_path",
        "pending_cleanup",
    }
    _require_exact_keys(raw, expected, "install.json")
    schema = _required_integer(raw["schema_version"], "schema_version")
    if schema != INSTALL_SCHEMA_VERSION:
        raise InstallStateError(f"不支持的 install.json schema：{schema}")
    product_id = _required_text(raw["product_id"], "product_id")
    if product_id != PRODUCT_ID:
        raise InstallStateError(f"install.json product_id 不匹配：{product_id}")
    install_root = _required_text(raw["install_root"], "install_root")
    if not _same_path(Path(install_root), paths.install_root):
        raise InstallStateError("install.json 安装根与当前固定产品路径不一致。")
    app_relative = _required_text(raw["app_relative_path"], "app_relative_path")
    if app_relative != MAIN_EXECUTABLE_RELATIVE.as_posix():
        raise InstallStateError("install.json 主程序相对路径不符合固定布局。")
    pending_raw = raw["pending_cleanup"]
    if not isinstance(pending_raw, list) or any(not isinstance(item, str) for item in pending_raw):
        raise InstallStateError("pending_cleanup 必须是字符串数组。")
    pending = tuple(pending_raw)
    if len(set(pending)) != len(pending):
        raise InstallStateError("pending_cleanup 不能包含重复目录。")
    if any(not is_transaction_directory_name(item) for item in pending):
        raise InstallStateError("pending_cleanup 包含非受控事务目录名称。")
    return InstallRecord(
        product_id=product_id,
        version=_required_text(raw["version"], "version"),
        build_id=_required_text(raw["build_id"], "build_id"),
        installed_at=_required_timestamp(raw["installed_at"], "installed_at"),
        updated_at=_required_timestamp(raw["updated_at"], "updated_at"),
        install_root=str(paths.install_root),
        app_relative_path=app_relative,
        pending_cleanup=pending,
        schema_version=schema,
    )


def _transaction_record_from_json(raw: Mapping[str, object]) -> TransactionRecord:
    expected = {
        "schema_version",
        "transaction_id",
        "kind",
        "source_version",
        "source_build_id",
        "target_version",
        "target_build_id",
        "created_at",
        "updated_at",
        "stage",
        "staging_directory",
        "rollback_directory",
        "integration_snapshot",
    }
    _require_exact_keys(raw, expected, "transaction.json")
    schema = _required_integer(raw["schema_version"], "schema_version")
    if schema != TRANSACTION_SCHEMA_VERSION:
        raise InstallStateError(f"不支持的 transaction.json schema：{schema}")
    transaction_id = _required_transaction_id(raw["transaction_id"])
    kind = _required_text(raw["kind"], "kind")
    if kind not in TRANSACTION_KINDS:
        raise InstallStateError(f"未知安装事务类型：{kind}")
    source_version = _optional_text(raw["source_version"], "source_version")
    source_build_id = _optional_text(raw["source_build_id"], "source_build_id")
    if kind == "upgrade" and (source_version is None or source_build_id is None):
        raise InstallStateError("升级事务必须记录源 version/build id。")
    if kind == "install" and (source_version is not None or source_build_id is not None):
        raise InstallStateError("首次安装事务不得声明源 version/build id。")
    stage = _required_text(raw["stage"], "stage")
    if stage not in TRANSACTION_STAGES:
        raise InstallStateError(f"未知安装事务阶段：{stage}")
    staging = _required_text(raw["staging_directory"], "staging_directory")
    target_version = _required_text(raw["target_version"], "target_version")
    try:
        expected_staging = transaction_directory_name(
            "staging",
            target_version,
            transaction_id,
        )
    except InstallContractError as exc:
        raise InstallStateError(f"target_version 不能用于事务目录：{exc}") from exc
    if staging != expected_staging:
        raise InstallStateError("staging_directory 不是受控 staging 名称。")
    rollback = _optional_text(raw["rollback_directory"], "rollback_directory")
    if kind == "upgrade" and rollback is None:
        raise InstallStateError("升级事务必须记录 rollback_directory。")
    if rollback is not None:
        assert source_version is not None
        try:
            expected_rollback = transaction_directory_name(
                "rollback",
                source_version,
                transaction_id,
            )
        except InstallContractError as exc:
            raise InstallStateError(f"source_version 不能用于事务目录：{exc}") from exc
        if rollback != expected_rollback:
            raise InstallStateError("rollback_directory 不是受控 rollback 名称。")
    snapshot = raw["integration_snapshot"]
    if not isinstance(snapshot, dict):
        raise InstallStateError("integration_snapshot 必须是 JSON 对象。")
    _validate_json_mapping(snapshot, "integration_snapshot")
    return TransactionRecord(
        transaction_id=transaction_id,
        kind=kind,
        source_version=source_version,
        source_build_id=source_build_id,
        target_version=target_version,
        target_build_id=_required_text(raw["target_build_id"], "target_build_id"),
        created_at=_required_timestamp(raw["created_at"], "created_at"),
        updated_at=_required_timestamp(raw["updated_at"], "updated_at"),
        stage=stage,
        staging_directory=staging,
        rollback_directory=rollback,
        integration_snapshot=dict(snapshot),
        schema_version=schema,
    )


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.parent / f".{path.stem}.{secrets.token_hex(8)}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_transaction_update(
    current: TransactionRecord,
    replacement: TransactionRecord,
) -> None:
    identity_fields = (
        "transaction_id",
        "kind",
        "source_version",
        "source_build_id",
        "target_version",
        "target_build_id",
        "created_at",
        "staging_directory",
        "rollback_directory",
        "integration_snapshot",
    )
    if any(getattr(current, name) != getattr(replacement, name) for name in identity_fields):
        raise InstallStateError("不能在同一 transaction.json 中改变事务身份或路径。")
    current_index = TRANSACTION_STAGES.index(current.stage)
    replacement_index = TRANSACTION_STAGES.index(replacement.stage)
    if replacement_index not in {current_index, current_index + 1}:
        raise InstallStateError(
            f"非法安装事务阶段写入：{current.stage} → {replacement.stage}"
        )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise InstallStateError(f"{label} 必须是长度 1–256 的非空字符串。")
    if any(ord(character) < 32 for character in value):
        raise InstallStateError(f"{label} 不能包含控制字符。")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label)


def _required_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InstallStateError(f"{label} 必须是整数。")
    return value


def _required_timestamp(value: object, label: str) -> str:
    text = _required_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InstallStateError(f"{label} 不是有效 ISO-8601 时间。") from exc
    if parsed.tzinfo is None:
        raise InstallStateError(f"{label} 必须包含时区。")
    return text


def _required_transaction_id(value: object) -> str:
    text = _required_text(value, "transaction_id")
    if len(text) != 32 or any(character not in "0123456789abcdef" for character in text):
        raise InstallStateError("transaction_id 必须是 32 位小写十六进制字符串。")
    return text


def _require_exact_keys(raw: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise InstallStateError(f"{label} 字段不匹配；缺少 {missing}；多出 {extra}。")


def _validate_json_mapping(value: Mapping[str, object], label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InstallStateError(f"{label} 不是可序列化 JSON 对象。") from exc
    if len(encoded.encode("utf-8")) > 65_536:
        raise InstallStateError(f"{label} 超过 64 KiB 上限。")


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).casefold()
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS):
                raise InstallHistoryError(f"安装历史 details 包含敏感字段：{key}")
            _reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
