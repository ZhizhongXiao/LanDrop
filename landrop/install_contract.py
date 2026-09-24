"""Frozen Phase 8 installation identity, paths, and deletion boundaries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Mapping


PRODUCT_ID = "LanDrop"
DISPLAY_NAME = "LanDrop"
PUBLISHER = "LanDrop"
APP_USER_MODEL_ID = "LanDrop.Desktop"

MAIN_EXECUTABLE_RELATIVE = Path("app") / "LanDrop.exe"
UNINSTALL_EXECUTABLE_RELATIVE = Path("maintenance") / "Uninstall.exe"
INSTALL_STATE_RELATIVE = Path("metadata") / "install.json"
TRANSACTION_STATE_RELATIVE = Path("metadata") / "transaction.json"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "LanDrop"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LanDrop"

_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_TRANSACTION_ID_PATTERN = re.compile(r"[0-9a-f]{32}\Z")
_TRANSACTION_DIRECTORY_PATTERN = re.compile(
    r"\.(staging|rollback)-([A-Za-z0-9][A-Za-z0-9._-]{0,63})-([0-9a-f]{32})\Z"
)
_UNINSTALL_DIRECTORY_PATTERN = re.compile(r"uninstall-([0-9a-f]{32})\Z")
_REPARSE_POINT_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class InstallContractError(RuntimeError):
    """The requested install path or identity violates the frozen contract."""


class UnsafeInstallPathError(InstallContractError):
    """A path escapes an owned root or crosses a link/reparse object."""


@dataclass(frozen=True, slots=True)
class SystemIntegrationObject:
    """One exact Windows object that Setup may own and Uninstall may remove."""

    object_id: str
    kind: str
    identifier: str
    optional: bool = False


@dataclass(frozen=True, slots=True)
class InstallPaths:
    """All fixed current-user locations used by the Phase 8 lifecycle."""

    local_app_data: Path
    roaming_app_data: Path
    desktop_directory: Path
    temp_directory: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> InstallPaths:
        values = os.environ if environment is None else environment
        local = _required_absolute_environment_path(
            values,
            "LOCALAPPDATA",
            Path.home() / "AppData" / "Local",
        )
        roaming = _required_absolute_environment_path(
            values,
            "APPDATA",
            Path.home() / "AppData" / "Roaming",
        )
        profile = _required_absolute_environment_path(values, "USERPROFILE", Path.home())
        temporary = _required_absolute_environment_path(
            values,
            "TEMP",
            local / "Temp",
        )
        return cls(local, roaming, profile / "Desktop", temporary)

    @property
    def install_root(self) -> Path:
        return self.local_app_data / "Programs" / PRODUCT_ID

    @property
    def app_directory(self) -> Path:
        return self.install_root / "app"

    @property
    def main_executable(self) -> Path:
        return self.install_root / MAIN_EXECUTABLE_RELATIVE

    @property
    def maintenance_directory(self) -> Path:
        return self.install_root / "maintenance"

    @property
    def uninstall_executable(self) -> Path:
        return self.install_root / UNINSTALL_EXECUTABLE_RELATIVE

    @property
    def metadata_directory(self) -> Path:
        return self.install_root / "metadata"

    @property
    def install_state_path(self) -> Path:
        return self.install_root / INSTALL_STATE_RELATIVE

    @property
    def transaction_state_path(self) -> Path:
        return self.install_root / TRANSACTION_STATE_RELATIVE

    @property
    def data_root(self) -> Path:
        return self.local_app_data / PRODUCT_ID

    @property
    def log_directory(self) -> Path:
        return self.data_root / "logs"

    @property
    def install_history_path(self) -> Path:
        return self.log_directory / "install-history.jsonl"

    @property
    def start_menu_shortcut(self) -> Path:
        return (
            self.roaming_app_data
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / PRODUCT_ID
            / "LanDrop.lnk"
        )

    @property
    def desktop_shortcut(self) -> Path:
        return self.desktop_directory / "LanDrop.lnk"

    @property
    def uninstall_temp_root(self) -> Path:
        return self.temp_directory / PRODUCT_ID

    def system_integration_objects(self) -> tuple[SystemIntegrationObject, ...]:
        """Return the exhaustive pre-implementation ownership allowlist."""
        return (
            SystemIntegrationObject(
                "start_menu_shortcut",
                "shortcut",
                str(self.start_menu_shortcut),
            ),
            SystemIntegrationObject(
                "desktop_shortcut",
                "shortcut",
                str(self.desktop_shortcut),
                optional=True,
            ),
            SystemIntegrationObject(
                "run_value",
                "registry_value",
                f"HKCU\\{RUN_KEY}\\{RUN_VALUE_NAME}",
            ),
            SystemIntegrationObject(
                "uninstall_key",
                "registry_key",
                f"HKCU\\{UNINSTALL_KEY}",
            ),
        )


def lifecycle_mutex_name(paths: InstallPaths) -> str:
    """Return a stable per-user mutex name without exposing the user path."""
    identity = os.path.normcase(os.path.abspath(paths.local_app_data))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return rf"Global\LanDrop.InstallLifecycle.{digest}"


def new_transaction_id() -> str:
    return secrets.token_hex(16)


def transaction_directory_name(kind: str, version: str, transaction_id: str) -> str:
    if kind not in {"staging", "rollback"}:
        raise InstallContractError(f"未知事务目录类型：{kind}")
    _validate_component(version, "版本")
    _validate_transaction_id(transaction_id)
    return f".{kind}-{version}-{transaction_id}"


def uninstall_directory_name(transaction_id: str) -> str:
    _validate_transaction_id(transaction_id)
    return f"uninstall-{transaction_id}"


def is_transaction_directory_name(name: str) -> bool:
    return _TRANSACTION_DIRECTORY_PATTERN.fullmatch(name) is not None


def is_uninstall_directory_name(name: str) -> bool:
    return _UNINSTALL_DIRECTORY_PATTERN.fullmatch(name) is not None


def validate_install_child(
    candidate: Path,
    paths: InstallPaths,
    *,
    direct_child: bool = False,
    allow_root: bool = False,
) -> Path:
    _validate_owned_root(paths.install_root, paths.local_app_data)
    return validate_controlled_path(
        candidate,
        paths.install_root,
        direct_child=direct_child,
        allow_root=allow_root,
    )


def validate_data_child(
    candidate: Path,
    paths: InstallPaths,
    *,
    direct_child: bool = False,
    allow_root: bool = False,
) -> Path:
    _validate_owned_root(paths.data_root, paths.local_app_data)
    return validate_controlled_path(
        candidate,
        paths.data_root,
        direct_child=direct_child,
        allow_root=allow_root,
    )


def validate_transaction_directory(candidate: Path, paths: InstallPaths) -> Path:
    validated = validate_install_child(candidate, paths, direct_child=True)
    if not is_transaction_directory_name(validated.name):
        raise UnsafeInstallPathError(f"不是受控事务目录：{validated.name}")
    return validated


def validate_uninstall_temp_directory(candidate: Path, paths: InstallPaths) -> Path:
    _validate_owned_root(paths.uninstall_temp_root, paths.temp_directory)
    validated = validate_controlled_path(
        candidate,
        paths.uninstall_temp_root,
        direct_child=True,
    )
    if not is_uninstall_directory_name(validated.name):
        raise UnsafeInstallPathError(f"不是受控临时卸载目录：{validated.name}")
    return validated


def validate_controlled_path(
    candidate: Path,
    root: Path,
    *,
    direct_child: bool = False,
    allow_root: bool = False,
) -> Path:
    """Validate containment and reject every existing reparse/link component."""
    candidate_absolute = Path(os.path.abspath(candidate))
    root_absolute = Path(os.path.abspath(root))
    try:
        common = Path(os.path.commonpath((root_absolute, candidate_absolute)))
    except ValueError as exc:
        raise UnsafeInstallPathError("路径不在同一卷，拒绝操作。") from exc
    if not _same_path(common, root_absolute):
        raise UnsafeInstallPathError(f"路径超出受控根：{candidate_absolute}")
    if _same_path(candidate_absolute, root_absolute):
        if not allow_root:
            raise UnsafeInstallPathError("不允许把受控根本身作为此操作目标。")
        relative_parts: tuple[str, ...] = ()
    else:
        relative_parts = candidate_absolute.relative_to(root_absolute).parts
    if direct_child and len(relative_parts) != 1:
        raise UnsafeInstallPathError("目标必须是受控根的直接子项。")

    current = root_absolute
    for part in relative_parts:
        current /= part
        if os.path.lexists(current) and is_reparse_object(current):
            raise UnsafeInstallPathError(f"路径包含链接或 reparse object：{current}")
    if os.path.lexists(root_absolute) and is_reparse_object(root_absolute):
        raise UnsafeInstallPathError(f"受控根是链接或 reparse object：{root_absolute}")

    canonical_root = root_absolute.resolve(strict=False)
    canonical_candidate = candidate_absolute.resolve(strict=False)
    try:
        canonical_common = Path(os.path.commonpath((canonical_root, canonical_candidate)))
    except ValueError as exc:
        raise UnsafeInstallPathError("规范化路径不在同一卷，拒绝操作。") from exc
    if not _same_path(canonical_common, canonical_root):
        raise UnsafeInstallPathError(f"规范化路径超出受控根：{candidate_absolute}")
    return candidate_absolute


def is_reparse_object(path: Path) -> bool:
    """Return whether an existing path is a symlink/junction/reparse object."""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    return path.is_symlink() or bool(attributes & _REPARSE_POINT_ATTRIBUTE)


def _validate_owned_root(root: Path, anchor: Path) -> None:
    """Reject an owned product root redirected through an intermediate reparse point."""
    root_absolute = Path(os.path.abspath(root))
    anchor_absolute = Path(os.path.abspath(anchor))
    try:
        common = Path(os.path.commonpath((anchor_absolute, root_absolute)))
    except ValueError as exc:
        raise UnsafeInstallPathError("产品根与系统锚点不在同一卷。") from exc
    if not _same_path(common, anchor_absolute) or _same_path(root_absolute, anchor_absolute):
        raise UnsafeInstallPathError("产品根不在预期系统锚点下。")

    current = anchor_absolute
    for part in root_absolute.relative_to(anchor_absolute).parts:
        current /= part
        if os.path.lexists(current) and is_reparse_object(current):
            raise UnsafeInstallPathError(f"产品根经过链接或 reparse object：{current}")

    canonical_anchor = anchor_absolute.resolve(strict=False)
    canonical_root = root_absolute.resolve(strict=False)
    try:
        canonical_common = Path(os.path.commonpath((canonical_anchor, canonical_root)))
    except ValueError as exc:
        raise UnsafeInstallPathError("规范化产品根与系统锚点不在同一卷。") from exc
    if not _same_path(canonical_common, canonical_anchor):
        raise UnsafeInstallPathError("规范化产品根超出预期系统锚点。")


def _required_absolute_environment_path(
    environment: Mapping[str, str],
    name: str,
    fallback: Path,
) -> Path:
    raw = environment.get(name)
    candidate = Path(raw) if raw else fallback
    if not candidate.is_absolute():
        raise InstallContractError(f"{name} 必须是绝对路径：{candidate}")
    return Path(os.path.abspath(candidate))


def _validate_component(value: str, label: str) -> None:
    if not isinstance(value, str) or _COMPONENT_PATTERN.fullmatch(value) is None:
        raise InstallContractError(f"{label}不能用于事务目录名称：{value!r}")


def _validate_transaction_id(value: str) -> None:
    if not isinstance(value, str) or _TRANSACTION_ID_PATTERN.fullmatch(value) is None:
        raise InstallContractError("事务 ID 必须是 32 位小写十六进制字符串。")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
