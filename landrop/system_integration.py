"""Allowlisted current-user Windows integration for a first LanDrop install."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from typing import Any, Mapping, Protocol

from .install_contract import (
    APP_USER_MODEL_ID,
    DISPLAY_NAME,
    PRODUCT_ID,
    PUBLISHER,
    RUN_KEY,
    RUN_VALUE_NAME,
    STARTUP_APPROVED_RUN_KEY,
    UNINSTALL_KEY,
    InstallPaths,
    is_reparse_object,
)


_MAX_SHORTCUT_BYTES = 4 * 1024 * 1024


class SystemIntegrationError(RuntimeError):
    """A frozen Windows integration object could not be safely managed."""


@dataclass(frozen=True, slots=True)
class ShortcutSpec:
    path: Path
    target: Path
    arguments: str
    working_directory: Path
    description: str
    icon_location: str
    app_user_model_id: str = APP_USER_MODEL_ID

    def __post_init__(self) -> None:
        path = _absolute(self.path, "快捷方式路径")
        target = _absolute(self.target, "快捷方式目标")
        working = _absolute(self.working_directory, "快捷方式工作目录")
        if path.name != "LanDrop.lnk" or path.suffix.casefold() != ".lnk":
            raise SystemIntegrationError("快捷方式文件名不属于 LanDrop。")
        if target.name != "LanDrop.exe":
            raise SystemIntegrationError("快捷方式目标必须是 LanDrop.exe。")
        if not _same_path(working, target.parent):
            raise SystemIntegrationError("快捷方式工作目录必须是 app 目录。")
        if self.arguments:
            raise SystemIntegrationError("LanDrop 主快捷方式不得携带参数。")
        if self.app_user_model_id != APP_USER_MODEL_ID:
            raise SystemIntegrationError("快捷方式 AUMID 不符合冻结身份。")
        if self.icon_location != f"{target},0":
            raise SystemIntegrationError("快捷方式图标必须来自正式 LanDrop.exe。")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "working_directory", working)

    def to_bridge_json(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "target": str(self.target),
            "arguments": self.arguments,
            "workingDirectory": str(self.working_directory),
            "description": self.description,
            "iconLocation": self.icon_location,
            "appUserModelId": self.app_user_model_id,
        }

    @classmethod
    def from_bridge_json(cls, raw: Mapping[str, object]) -> ShortcutSpec:
        expected = {
            "path",
            "target",
            "arguments",
            "workingDirectory",
            "description",
            "iconLocation",
            "appUserModelId",
        }
        if set(raw) != expected or any(not isinstance(raw[key], str) for key in expected):
            raise SystemIntegrationError("快捷方式读回数据字段不匹配。")
        return cls(
            path=Path(str(raw["path"])),
            target=Path(str(raw["target"])),
            arguments=str(raw["arguments"]),
            working_directory=Path(str(raw["workingDirectory"])),
            description=str(raw["description"]),
            icon_location=str(raw["iconLocation"]),
            app_user_model_id=str(raw["appUserModelId"]),
        )


@dataclass(frozen=True, slots=True)
class InstalledAppRegistration:
    display_name: str
    display_version: str
    publisher: str
    display_icon: str
    install_location: str
    uninstall_string: str
    estimated_size_kib: int
    no_modify: int = 1
    no_repair: int = 1

    def __post_init__(self) -> None:
        if self.display_name != DISPLAY_NAME or self.publisher != PUBLISHER:
            raise SystemIntegrationError("卸载登记产品身份不匹配。")
        if not self.display_version or len(self.display_version) > 256:
            raise SystemIntegrationError("卸载登记版本无效。")
        if self.no_modify != 1 or self.no_repair != 1:
            raise SystemIntegrationError("卸载登记维护标记无效。")
        if isinstance(self.estimated_size_kib, bool) or self.estimated_size_kib < 1:
            raise SystemIntegrationError("EstimatedSize 必须是正整数 KiB。")

    def registry_values(self) -> dict[str, str | int]:
        return {
            "DisplayName": self.display_name,
            "DisplayVersion": self.display_version,
            "Publisher": self.publisher,
            "DisplayIcon": self.display_icon,
            "InstallLocation": self.install_location,
            "UninstallString": self.uninstall_string,
            "EstimatedSize": self.estimated_size_kib,
            "NoModify": self.no_modify,
            "NoRepair": self.no_repair,
        }


@dataclass(frozen=True, slots=True)
class IntegrationPlan:
    start_menu_shortcut: ShortcutSpec
    desktop_shortcut: ShortcutSpec
    desktop_enabled: bool
    run_command: str
    registration: InstalledAppRegistration

    @classmethod
    def create(
        cls,
        paths: InstallPaths,
        *,
        version: str,
        estimated_size_kib: int,
        desktop_enabled: bool,
    ) -> IntegrationPlan:
        if not isinstance(desktop_enabled, bool):
            raise SystemIntegrationError("桌面快捷方式选项必须是布尔值。")
        target = paths.main_executable
        shortcut_values = {
            "target": target,
            "arguments": "",
            "working_directory": paths.app_directory,
            "description": "LanDrop 局域网文件收发",
            "icon_location": f"{target},0",
        }
        run_command = subprocess.list2cmdline([str(target), "--startup"])
        uninstall_string = subprocess.list2cmdline([str(paths.uninstall_executable)])
        return cls(
            start_menu_shortcut=ShortcutSpec(
                path=paths.start_menu_shortcut,
                **shortcut_values,
            ),
            desktop_shortcut=ShortcutSpec(
                path=paths.desktop_shortcut,
                **shortcut_values,
            ),
            desktop_enabled=desktop_enabled,
            run_command=run_command,
            registration=InstalledAppRegistration(
                display_name=DISPLAY_NAME,
                display_version=version,
                publisher=PUBLISHER,
                display_icon=f"{target},0",
                install_location=str(paths.install_root),
                uninstall_string=uninstall_string,
                estimated_size_kib=max(1, estimated_size_kib),
            ),
        )

    def snapshot_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "objects": {
                "start_menu_shortcut": {"exists": False},
                "desktop_shortcut": {"exists": False},
                "run_value": {"exists": False},
                "uninstall_key": {"exists": False},
            },
        }


class FirstInstallIntegration(Protocol):
    def assert_absent(self, plan: IntegrationPlan) -> None: ...

    def write(self, plan: IntegrationPlan) -> None: ...

    def verify(self, plan: IntegrationPlan) -> None: ...

    def rollback(self, plan: IntegrationPlan) -> None: ...


@dataclass(frozen=True, slots=True)
class SystemIntegrationRemovalResult:
    """Exact allowlisted objects removed, absent, or deliberately preserved."""

    removed: tuple[str, ...]
    absent: tuple[str, ...]
    residuals: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return not self.residuals


@dataclass(frozen=True, slots=True)
class UpgradeIntegrationSnapshot:
    """Exact pre-upgrade values that Setup must preserve or restore."""

    start_menu_shortcut: ShortcutSpec
    desktop_shortcut_path: Path
    desktop_shortcut: ShortcutSpec | None
    run_command: str | None
    registration: InstalledAppRegistration
    estimated_size_exists: bool = True
    estimated_size_value: int | None = None
    estimated_size_type: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.estimated_size_exists, bool):
            raise SystemIntegrationError("升级快照 EstimatedSize 存在状态无效。")
        if self.estimated_size_exists:
            value = (
                self.registration.estimated_size_kib
                if self.estimated_size_value is None
                else self.estimated_size_value
            )
            value_type = 4 if self.estimated_size_type is None else self.estimated_size_type
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise SystemIntegrationError("升级快照 EstimatedSize 值无效。")
            if not isinstance(value_type, int):
                raise SystemIntegrationError("升级快照 EstimatedSize 类型无效。")
            object.__setattr__(self, "estimated_size_value", value)
            object.__setattr__(self, "estimated_size_type", value_type)
        elif self.estimated_size_value is not None or self.estimated_size_type is not None:
            raise SystemIntegrationError("缺失的 EstimatedSize 不得携带值或类型。")

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "start_menu_shortcut": self.start_menu_shortcut.to_bridge_json(),
            "desktop_shortcut_path": str(self.desktop_shortcut_path),
            "desktop_shortcut": (
                None
                if self.desktop_shortcut is None
                else self.desktop_shortcut.to_bridge_json()
            ),
            "run_command": self.run_command,
            "registration": self.registration.registry_values(),
            "estimated_size_state": {
                "exists": self.estimated_size_exists,
                "value": self.estimated_size_value,
                "type": self.estimated_size_type,
            },
        }

    def original_registration_values(self) -> dict[str, str | int]:
        values = self.registration.registry_values()
        if not self.estimated_size_exists:
            values.pop("EstimatedSize")
        elif self.estimated_size_value is not None:
            values["EstimatedSize"] = self.estimated_size_value
        return values

    @classmethod
    def from_json(cls, raw: Mapping[str, object]) -> UpgradeIntegrationSnapshot:
        expected = {
            "schema_version",
            "start_menu_shortcut",
            "desktop_shortcut_path",
            "desktop_shortcut",
            "run_command",
            "registration",
            "estimated_size_state",
        }
        if set(raw) != expected or raw.get("schema_version") != 1:
            raise SystemIntegrationError("升级系统集成快照 schema 不受支持。")
        start_menu_raw = raw["start_menu_shortcut"]
        desktop_raw = raw["desktop_shortcut"]
        registration_raw = raw["registration"]
        estimated_size_raw = raw["estimated_size_state"]
        desktop_path = raw["desktop_shortcut_path"]
        run_command = raw["run_command"]
        if not isinstance(start_menu_raw, Mapping):
            raise SystemIntegrationError("升级快照缺少开始菜单快捷方式。")
        if desktop_raw is not None and not isinstance(desktop_raw, Mapping):
            raise SystemIntegrationError("升级快照桌面快捷方式无效。")
        if not isinstance(registration_raw, Mapping):
            raise SystemIntegrationError("升级快照卸载登记无效。")
        if (
            not isinstance(estimated_size_raw, Mapping)
            or set(estimated_size_raw) != {"exists", "value", "type"}
            or not isinstance(estimated_size_raw["exists"], bool)
        ):
            raise SystemIntegrationError("升级快照 EstimatedSize 状态无效。")
        if not isinstance(desktop_path, str) or not desktop_path:
            raise SystemIntegrationError("升级快照桌面快捷方式路径无效。")
        if run_command is not None and not isinstance(run_command, str):
            raise SystemIntegrationError("升级快照 HKCU Run 值无效。")
        return cls(
            start_menu_shortcut=ShortcutSpec.from_bridge_json(start_menu_raw),
            desktop_shortcut_path=_absolute(Path(desktop_path), "桌面快捷方式路径"),
            desktop_shortcut=(
                None
                if desktop_raw is None
                else ShortcutSpec.from_bridge_json(desktop_raw)
            ),
            run_command=run_command,
            registration=_registration_from_values(registration_raw),
            estimated_size_exists=bool(estimated_size_raw["exists"]),
            estimated_size_value=estimated_size_raw["value"],  # type: ignore[arg-type]
            estimated_size_type=estimated_size_raw["type"],  # type: ignore[arg-type]
        )


class UpgradeIntegration(Protocol):
    def snapshot(self, plan: IntegrationPlan) -> UpgradeIntegrationSnapshot: ...

    def update_registration(
        self,
        snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None: ...

    def verify_upgrade(
        self,
        snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None: ...

    def restore_upgrade(self, snapshot: UpgradeIntegrationSnapshot) -> None: ...


class ShortcutBackend(Protocol):
    def read(self, path: Path) -> ShortcutSpec | None: ...

    def write(self, shortcut: ShortcutSpec) -> None: ...

    def remove_created(self, path: Path) -> None: ...


class PowerShellShortcutBackend:
    """Use the frozen local PowerShell bridge for WSH .lnk + AUMID metadata."""

    TIMEOUT_SECONDS = 30

    def __init__(self, allowed_paths: tuple[Path, ...], bridge_path: Path) -> None:
        if len(allowed_paths) != 2:
            raise SystemIntegrationError("快捷方式白名单必须精确包含两个路径。")
        self._allowed_paths = tuple(_absolute(path, "快捷方式白名单") for path in allowed_paths)
        self._bridge_path = _absolute(bridge_path, "快捷方式桥接脚本")

    def _allowed(self, path: Path) -> Path:
        candidate = _absolute(path, "快捷方式路径")
        if not any(_same_path(candidate, allowed) for allowed in self._allowed_paths):
            raise SystemIntegrationError(f"快捷方式路径不在白名单：{candidate}")
        return candidate

    def _run(self, action: str, request: Mapping[str, object]) -> dict[str, object]:
        if os.name != "nt":
            raise SystemIntegrationError("Windows 快捷方式只支持 Windows。")
        if not self._bridge_path.is_file() or is_reparse_object(self._bridge_path):
            raise SystemIntegrationError("快捷方式桥接脚本缺失或不安全。")
        powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / (
            r"System32\WindowsPowerShell\v1.0\powershell.exe"
        )
        if not powershell.is_file():
            raise SystemIntegrationError("未找到 Windows PowerShell。")
        command = [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._bridge_path),
            "-Action",
            action,
        ]
        try:
            with tempfile.TemporaryDirectory(prefix="landrop-shortcut-") as temporary:
                request_path = Path(temporary) / "request.json"
                request_path.write_text(
                    json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8-sig",
                )
                completed = subprocess.run(
                    [*command, "-RequestPath", str(request_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8-sig",
                    errors="replace",
                    timeout=self.TIMEOUT_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SystemIntegrationError(f"快捷方式桥接执行失败：{exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SystemIntegrationError(
                f"快捷方式桥接返回 {completed.returncode}：{detail or '无输出'}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SystemIntegrationError("快捷方式桥接返回了无效 JSON。") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise SystemIntegrationError(f"快捷方式桥接结果无效：{payload!r}")
        return payload

    def read(self, path: Path) -> ShortcutSpec | None:
        target = self._allowed(path)
        if os.path.lexists(target) and is_reparse_object(target):
            raise SystemIntegrationError("拒绝读取 reparse 快捷方式。")
        payload = self._run("Read", {"path": str(target)})
        if payload.get("exists") is False:
            return None
        if payload.get("readable") is not True or not isinstance(payload.get("shortcut"), dict):
            raise SystemIntegrationError("现有快捷方式无法安全解释。")
        return ShortcutSpec.from_bridge_json(payload["shortcut"])

    def write(self, shortcut: ShortcutSpec) -> None:
        target = self._allowed(shortcut.path)
        if os.path.lexists(target):
            raise SystemIntegrationError(f"首次安装不得覆盖现有快捷方式：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self._run("Write", shortcut.to_bridge_json())
        if payload.get("exists") is not True or self.read(target) != shortcut:
            raise SystemIntegrationError("快捷方式写入后读回不一致。")

    def remove_created(self, path: Path) -> None:
        target = self._allowed(path)
        if not os.path.lexists(target):
            return
        if is_reparse_object(target):
            raise SystemIntegrationError(f"拒绝删除 reparse 快捷方式：{target}")
        details = target.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_SHORTCUT_BYTES:
            raise SystemIntegrationError(f"拒绝删除类型或大小异常的快捷方式：{target}")
        try:
            target.unlink()
            if target.parent.name == PRODUCT_ID:
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
        except OSError as exc:
            raise SystemIntegrationError(f"无法删除快捷方式：{target}：{exc}") from exc


class WindowsFirstInstallIntegration:
    """Manage created objects plus exact cleanup-only Windows-derived state."""

    def __init__(
        self,
        shortcuts: ShortcutBackend,
        *,
        registry_module: Any | None = None,
    ) -> None:
        if registry_module is None:
            try:
                import winreg as registry_module
            except ImportError as exc:
                raise SystemIntegrationError("当前平台不支持 Windows 注册表。") from exc
        self._registry = registry_module
        self._shortcuts = shortcuts

    def assert_absent(self, plan: IntegrationPlan) -> None:
        if self._shortcuts.read(plan.start_menu_shortcut.path) is not None:
            raise SystemIntegrationError("开始菜单快捷方式已存在，拒绝首次安装覆盖。")
        if self._shortcuts.read(plan.desktop_shortcut.path) is not None:
            raise SystemIntegrationError("桌面快捷方式已存在，拒绝首次安装覆盖。")
        if self._read_run() is not None:
            raise SystemIntegrationError("HKCU Run 中已存在 LanDrop，拒绝覆盖。")
        if self._read_uninstall_raw() is not None:
            raise SystemIntegrationError("HKCU Uninstall 中已存在 LanDrop，拒绝覆盖。")

    def write(self, plan: IntegrationPlan) -> None:
        self._shortcuts.write(plan.start_menu_shortcut)
        if plan.desktop_enabled:
            self._shortcuts.write(plan.desktop_shortcut)
        self._write_run(plan.run_command)
        self._write_uninstall(plan.registration)

    def verify(self, plan: IntegrationPlan) -> None:
        if self._shortcuts.read(plan.start_menu_shortcut.path) != plan.start_menu_shortcut:
            raise SystemIntegrationError("开始菜单快捷方式读回不一致。")
        expected_desktop = plan.desktop_shortcut if plan.desktop_enabled else None
        if self._shortcuts.read(plan.desktop_shortcut.path) != expected_desktop:
            raise SystemIntegrationError("桌面快捷方式读回不一致。")
        if self._read_run() != plan.run_command:
            raise SystemIntegrationError("HKCU Run 读回不一致。")
        if self._read_uninstall_raw() != plan.registration.registry_values():
            raise SystemIntegrationError("HKCU Uninstall 读回不一致。")

    def rollback(self, plan: IntegrationPlan) -> None:
        errors: list[str] = []
        for shortcut in (plan.desktop_shortcut, plan.start_menu_shortcut):
            try:
                self._shortcuts.remove_created(shortcut.path)
            except Exception as exc:
                errors.append(str(exc))
        try:
            self._remove_created_run()
        except Exception as exc:
            errors.append(str(exc))
        try:
            self._remove_created_uninstall()
        except Exception as exc:
            errors.append(str(exc))
        if errors:
            raise SystemIntegrationError("系统集成回滚不完整：" + "；".join(errors))

    def remove_owned(self, plan: IntegrationPlan) -> SystemIntegrationRemovalResult:
        """Remove only objects that still exactly belong to this LanDrop install.

        A user-modified object is preserved and reported instead of being guessed
        at or overwritten.  Each allowlisted object is handled independently so
        one residual does not hide the cleanup result of the other objects.
        """
        removed: list[str] = []
        absent: list[str] = []
        residuals: list[str] = []

        def remove_shortcut(object_id: str, expected: ShortcutSpec) -> None:
            try:
                current = self._shortcuts.read(expected.path)
                if current is None:
                    absent.append(object_id)
                elif current != expected:
                    residuals.append(f"{object_id} 已被修改，已保留。")
                else:
                    self._shortcuts.remove_created(expected.path)
                    if self._shortcuts.read(expected.path) is not None:
                        residuals.append(f"{object_id} 删除后仍存在。")
                    else:
                        removed.append(object_id)
            except Exception as exc:
                residuals.append(f"{object_id} 无法安全清理：{exc}")

        try:
            current_run = self._read_run()
            if current_run is None:
                absent.append("run_value")
            elif current_run != plan.run_command:
                residuals.append("run_value 已被修改，已保留。")
            else:
                self._remove_created_run()
                if self._read_run() is None:
                    removed.append("run_value")
                else:
                    residuals.append("run_value 删除后仍存在。")
        except Exception as exc:
            residuals.append(f"run_value 无法安全清理：{exc}")

        try:
            startup_approved = self._read_startup_approved_run()
            if startup_approved is None:
                absent.append("startup_approved_run_value")
            else:
                self._remove_startup_approved_run()
                if self._read_startup_approved_run() is None:
                    removed.append("startup_approved_run_value")
                else:
                    residuals.append("startup_approved_run_value 删除后仍存在。")
        except Exception as exc:
            residuals.append(
                f"startup_approved_run_value 无法安全清理：{exc}"
            )

        remove_shortcut("start_menu_shortcut", plan.start_menu_shortcut)
        remove_shortcut("desktop_shortcut", plan.desktop_shortcut)

        try:
            current_registration = self._read_uninstall_raw()
            if current_registration is None:
                absent.append("uninstall_key")
            elif not _registration_owned_for_uninstall(
                current_registration,
                plan.registration,
            ):
                residuals.append("uninstall_key 已被修改或包含未知字段，已保留。")
            else:
                self._remove_created_uninstall()
                if self._read_uninstall_raw() is None:
                    removed.append("uninstall_key")
                else:
                    residuals.append("uninstall_key 删除后仍存在。")
        except Exception as exc:
            residuals.append(f"uninstall_key 无法安全清理：{exc}")

        return SystemIntegrationRemovalResult(
            removed=tuple(removed),
            absent=tuple(absent),
            residuals=tuple(residuals),
        )

    def snapshot(self, plan: IntegrationPlan) -> UpgradeIntegrationSnapshot:
        """Validate a committed install and capture values without changing them."""
        start_menu = self._shortcuts.read(plan.start_menu_shortcut.path)
        if start_menu != plan.start_menu_shortcut:
            raise SystemIntegrationError("开始菜单快捷方式与当前安装状态不一致。")
        desktop = self._shortcuts.read(plan.desktop_shortcut.path)
        if desktop is not None and desktop != plan.desktop_shortcut:
            raise SystemIntegrationError("桌面快捷方式无法归属当前 LanDrop 安装。")
        run_command = self._read_run()
        if run_command is not None and run_command != plan.run_command:
            raise SystemIntegrationError("HKCU Run 值不属于当前 LanDrop 安装。")
        registration_typed = self._read_uninstall_typed()
        if registration_typed is None:
            raise SystemIntegrationError("缺少当前 LanDrop 卸载登记。")
        registration_raw = {
            name: value for name, (value, _value_type) in registration_typed.items()
        }
        expected_names = set(plan.registration.registry_values())
        actual_names = frozenset(registration_raw)
        if actual_names not in {frozenset(expected_names), frozenset(expected_names - {"EstimatedSize"})}:
            raise SystemIntegrationError("卸载登记字段集合不符合冻结清单。")
        estimated_size_exists = "EstimatedSize" in registration_raw
        estimated_size_value = (
            int(registration_raw["EstimatedSize"])
            if estimated_size_exists
            else None
        )
        estimated_size_type = (
            registration_typed["EstimatedSize"][1]
            if estimated_size_exists
            else None
        )
        registration_raw.setdefault("EstimatedSize", plan.registration.estimated_size_kib)
        registration = _registration_from_values(registration_raw)
        if registration.display_version != plan.registration.display_version:
            raise SystemIntegrationError("卸载登记版本与 install.json 不一致。")
        expected = _registration_with_version_and_size(
            plan.registration,
            version=registration.display_version,
            estimated_size_kib=registration.estimated_size_kib,
        )
        if registration != expected:
            raise SystemIntegrationError("卸载登记稳定字段与当前安装不一致。")
        return UpgradeIntegrationSnapshot(
            start_menu_shortcut=start_menu,
            desktop_shortcut_path=plan.desktop_shortcut.path,
            desktop_shortcut=desktop,
            run_command=run_command,
            registration=registration,
            estimated_size_exists=estimated_size_exists,
            estimated_size_value=estimated_size_value,
            estimated_size_type=estimated_size_type,
        )

    def update_registration(
        self,
        snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None:
        target = _registration_with_version_and_size(
            snapshot.registration,
            version=version,
            estimated_size_kib=estimated_size_kib,
        )
        self._write_upgrade_registration(target)

    def verify_upgrade(
        self,
        snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None:
        if self._shortcuts.read(snapshot.start_menu_shortcut.path) != snapshot.start_menu_shortcut:
            raise SystemIntegrationError("升级后开始菜单快捷方式发生变化。")
        if self._shortcuts.read(snapshot.desktop_shortcut_path) != snapshot.desktop_shortcut:
            raise SystemIntegrationError("升级后桌面快捷方式选择发生变化。")
        if self._read_run() != snapshot.run_command:
            raise SystemIntegrationError("升级后 HKCU Run 状态发生变化。")
        expected_registration = _registration_with_version_and_size(
            snapshot.registration,
            version=version,
            estimated_size_kib=estimated_size_kib,
        )
        if self._read_uninstall_raw() != expected_registration.registry_values():
            raise SystemIntegrationError("升级后卸载登记读回不一致。")

    def restore_upgrade(self, snapshot: UpgradeIntegrationSnapshot) -> None:
        self._restore_upgrade_registration(snapshot)
        if self._shortcuts.read(snapshot.start_menu_shortcut.path) != snapshot.start_menu_shortcut:
            raise SystemIntegrationError("恢复后开始菜单快捷方式不一致。")
        if self._shortcuts.read(snapshot.desktop_shortcut_path) != snapshot.desktop_shortcut:
            raise SystemIntegrationError("恢复后桌面快捷方式不一致。")
        if self._read_run() != snapshot.run_command:
            raise SystemIntegrationError("恢复后 HKCU Run 状态不一致。")
        if self._read_uninstall_raw() != snapshot.original_registration_values():
            raise SystemIntegrationError("恢复后卸载登记不一致。")

    def _read_run(self) -> str | None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                registry.KEY_READ,
            ) as key:
                value, value_type = registry.QueryValueEx(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            return None
        if value_type != registry.REG_SZ or not isinstance(value, str):
            raise SystemIntegrationError("HKCU Run LanDrop 值类型不可解释。")
        return value

    def _write_run(self, value: str) -> None:
        registry = self._registry
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            RUN_KEY,
            0,
            registry.KEY_READ | registry.KEY_WRITE,
        ) as key:
            registry.SetValueEx(key, RUN_VALUE_NAME, 0, registry.REG_SZ, value)

    def _remove_created_run(self) -> None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                RUN_KEY,
                0,
                registry.KEY_SET_VALUE,
            ) as key:
                registry.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            return

    def _read_startup_approved_run(self) -> bytes | None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                STARTUP_APPROVED_RUN_KEY,
                0,
                registry.KEY_READ,
            ) as key:
                value, value_type = registry.QueryValueEx(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            return None
        if value_type != registry.REG_BINARY or not isinstance(value, bytes):
            raise SystemIntegrationError(
                "HKCU StartupApproved Run LanDrop 值类型不可解释。"
            )
        return value

    def _remove_startup_approved_run(self) -> None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                STARTUP_APPROVED_RUN_KEY,
                0,
                registry.KEY_SET_VALUE,
            ) as key:
                registry.DeleteValue(key, RUN_VALUE_NAME)
        except FileNotFoundError:
            return

    def _read_uninstall_raw(self) -> dict[str, str | int] | None:
        typed = self._read_uninstall_typed()
        if typed is None:
            return None
        return {name: value for name, (value, _value_type) in typed.items()}

    def _read_uninstall_typed(self) -> dict[str, tuple[str | int, int]] | None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_KEY,
                0,
                registry.KEY_READ,
            ) as key:
                subkeys, value_count, _modified = registry.QueryInfoKey(key)
                if subkeys:
                    raise SystemIntegrationError("HKCU Uninstall 含未知子键。")
                values: dict[str, tuple[str | int, int]] = {}
                for index in range(value_count):
                    name, value, value_type = registry.EnumValue(key, index)
                    expected_type = registry.REG_DWORD if isinstance(value, int) else registry.REG_SZ
                    if value_type != expected_type or not isinstance(value, (str, int)):
                        raise SystemIntegrationError("HKCU Uninstall 值类型不可解释。")
                    values[name] = (value, value_type)
        except FileNotFoundError:
            return None
        return values

    def _write_uninstall(self, registration: InstalledAppRegistration) -> None:
        registry = self._registry
        with registry.CreateKeyEx(
            registry.HKEY_CURRENT_USER,
            UNINSTALL_KEY,
            0,
            registry.KEY_READ | registry.KEY_WRITE,
        ) as key:
            for name, value in registration.registry_values().items():
                value_type = registry.REG_DWORD if isinstance(value, int) else registry.REG_SZ
                registry.SetValueEx(key, name, 0, value_type, value)

    def _write_upgrade_registration(self, registration: InstalledAppRegistration) -> None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_KEY,
                0,
                registry.KEY_READ | registry.KEY_WRITE,
            ) as key:
                registry.SetValueEx(
                    key,
                    "DisplayVersion",
                    0,
                    registry.REG_SZ,
                    registration.display_version,
                )
                registry.SetValueEx(
                    key,
                    "EstimatedSize",
                    0,
                    registry.REG_DWORD,
                    registration.estimated_size_kib,
                )
        except FileNotFoundError as exc:
            raise SystemIntegrationError("升级期间卸载登记已消失。") from exc

    def _restore_upgrade_registration(
        self,
        snapshot: UpgradeIntegrationSnapshot,
    ) -> None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_KEY,
                0,
                registry.KEY_READ | registry.KEY_WRITE,
            ) as key:
                registry.SetValueEx(
                    key,
                    "DisplayVersion",
                    0,
                    registry.REG_SZ,
                    snapshot.registration.display_version,
                )
                if snapshot.estimated_size_exists:
                    registry.SetValueEx(
                        key,
                        "EstimatedSize",
                        0,
                        int(snapshot.estimated_size_type),
                        snapshot.estimated_size_value,
                    )
                else:
                    try:
                        registry.DeleteValue(key, "EstimatedSize")
                    except FileNotFoundError:
                        pass
        except FileNotFoundError as exc:
            raise SystemIntegrationError("恢复时卸载登记已消失。") from exc

    def _remove_created_uninstall(self) -> None:
        registry = self._registry
        try:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER,
                UNINSTALL_KEY,
                0,
                registry.KEY_READ | registry.KEY_WRITE,
            ) as key:
                subkeys, value_count, _modified = registry.QueryInfoKey(key)
                if subkeys:
                    raise SystemIntegrationError("HKCU Uninstall 键含未知子键。")
                names = [registry.EnumValue(key, index)[0] for index in range(value_count)]
                for name in names:
                    registry.DeleteValue(key, name)
        except FileNotFoundError:
            return
        registry.DeleteKey(registry.HKEY_CURRENT_USER, UNINSTALL_KEY)


def installed_size_kib(root: Path) -> int:
    total = 0
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if is_reparse_object(current):
            raise SystemIntegrationError(f"程序目录包含 reparse object：{current}")
        for name in directory_names:
            if is_reparse_object(current / name):
                raise SystemIntegrationError(f"程序目录包含 reparse object：{current / name}")
        for name in file_names:
            path = current / name
            if is_reparse_object(path):
                raise SystemIntegrationError(f"程序文件是 reparse object：{path}")
            details = path.stat()
            if not stat.S_ISREG(details.st_mode):
                raise SystemIntegrationError(f"程序路径不是普通文件：{path}")
            total += details.st_size
    return max(1, (total + 1023) // 1024)


def _registration_from_values(
    values: Mapping[str, str | int],
) -> InstalledAppRegistration:
    expected = {
        "DisplayName",
        "DisplayVersion",
        "Publisher",
        "DisplayIcon",
        "InstallLocation",
        "UninstallString",
        "EstimatedSize",
        "NoModify",
        "NoRepair",
    }
    if set(values) != expected:
        raise SystemIntegrationError("卸载登记字段集合不符合冻结清单。")
    text_fields = (
        "DisplayName",
        "DisplayVersion",
        "Publisher",
        "DisplayIcon",
        "InstallLocation",
        "UninstallString",
    )
    integer_fields = ("EstimatedSize", "NoModify", "NoRepair")
    if any(not isinstance(values[name], str) for name in text_fields) or any(
        isinstance(values[name], bool) or not isinstance(values[name], int)
        for name in integer_fields
    ):
        raise SystemIntegrationError("卸载登记字段类型不符合冻结清单。")
    return InstalledAppRegistration(
        display_name=str(values["DisplayName"]),
        display_version=str(values["DisplayVersion"]),
        publisher=str(values["Publisher"]),
        display_icon=str(values["DisplayIcon"]),
        install_location=str(values["InstallLocation"]),
        uninstall_string=str(values["UninstallString"]),
        estimated_size_kib=int(values["EstimatedSize"]),
        no_modify=int(values["NoModify"]),
        no_repair=int(values["NoRepair"]),
    )


def _registration_with_version_and_size(
    source: InstalledAppRegistration,
    *,
    version: str,
    estimated_size_kib: int,
) -> InstalledAppRegistration:
    return InstalledAppRegistration(
        display_name=source.display_name,
        display_version=version,
        publisher=source.publisher,
        display_icon=source.display_icon,
        install_location=source.install_location,
        uninstall_string=source.uninstall_string,
        estimated_size_kib=estimated_size_kib,
        no_modify=source.no_modify,
        no_repair=source.no_repair,
    )


def _registration_owned_for_uninstall(
    current: Mapping[str, str | int],
    expected: InstalledAppRegistration,
) -> bool:
    """Accept the exact frozen field set while allowing its positive size value."""
    expected_values = expected.registry_values()
    if set(current) != set(expected_values):
        return False
    for name, value in expected_values.items():
        if name == "EstimatedSize":
            actual = current.get(name)
            if isinstance(actual, bool) or not isinstance(actual, int) or actual < 1:
                return False
        elif current.get(name) != value:
            return False
    return True


def _absolute(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    if not candidate.is_absolute():
        raise SystemIntegrationError(f"{label}必须是绝对路径。")
    return candidate


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))
