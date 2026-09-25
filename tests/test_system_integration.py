from __future__ import annotations

import os
from pathlib import Path
import subprocess
import unittest

from landrop.install_contract import (
    APP_USER_MODEL_ID,
    InstallPaths,
    RUN_KEY,
    RUN_VALUE_NAME,
    STARTUP_APPROVED_RUN_KEY,
    UNINSTALL_KEY,
)
from landrop.system_integration import (
    IntegrationPlan,
    PowerShellShortcutBackend,
    ShortcutSpec,
    SystemIntegrationError,
    WindowsFirstInstallIntegration,
)
from tests.support import temporary_directory


class _Key:
    def __init__(self, backend: "_Registry", path: str) -> None:
        self.backend = backend
        self.path = path

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Registry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_WRITE = 2
    KEY_SET_VALUE = 4
    REG_SZ = 1
    REG_BINARY = 3
    REG_DWORD = 4

    def __init__(self) -> None:
        self.keys: dict[str, dict[str, tuple[object, int]]] = {}

    def OpenKey(self, _root: object, path: str, *_args: object) -> _Key:  # noqa: N802
        if path not in self.keys:
            raise FileNotFoundError(path)
        return _Key(self, path)

    def CreateKeyEx(self, _root: object, path: str, *_args: object) -> _Key:  # noqa: N802
        self.keys.setdefault(path, {})
        return _Key(self, path)

    def QueryValueEx(self, key: _Key, name: str):  # noqa: N802
        try:
            return self.keys[key.path][name]
        except KeyError as exc:
            raise FileNotFoundError(name) from exc

    def SetValueEx(self, key: _Key, name: str, _reserved: int, kind: int, value: object):  # noqa: N802
        self.keys[key.path][name] = (value, kind)

    def QueryInfoKey(self, key: _Key):  # noqa: N802
        return 0, len(self.keys[key.path]), 0

    def EnumValue(self, key: _Key, index: int):  # noqa: N802
        name = sorted(self.keys[key.path])[index]
        value, kind = self.keys[key.path][name]
        return name, value, kind

    def DeleteValue(self, key: _Key, name: str):  # noqa: N802
        del self.keys[key.path][name]

    def DeleteKey(self, _root: object, path: str):  # noqa: N802
        if self.keys[path]:
            raise OSError("not empty")
        del self.keys[path]


class _Shortcuts:
    def __init__(self) -> None:
        self.values: dict[Path, ShortcutSpec] = {}

    def read(self, path: Path) -> ShortcutSpec | None:
        return self.values.get(path)

    def write(self, shortcut: ShortcutSpec) -> None:
        if shortcut.path in self.values:
            raise SystemIntegrationError("exists")
        self.values[shortcut.path] = shortcut

    def remove_created(self, path: Path) -> None:
        self.values.pop(path, None)


class SystemIntegrationTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "Local Data",
            root / "Roaming Data",
            root / "Desktop Folder",
            root / "Temp Folder",
        )

    def test_plan_uses_only_stable_paths_commands_and_aumid(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=123,
                desktop_enabled=False,
            )

            self.assertEqual(plan.start_menu_shortcut.target, paths.main_executable)
            self.assertEqual(plan.start_menu_shortcut.app_user_model_id, APP_USER_MODEL_ID)
            self.assertEqual(
                plan.run_command,
                subprocess.list2cmdline([str(paths.main_executable), "--startup"]),
            )
            self.assertEqual(
                plan.registration.uninstall_string,
                subprocess.list2cmdline([str(paths.uninstall_executable)]),
            )
            self.assertEqual(plan.registration.no_modify, 1)
            self.assertEqual(plan.registration.no_repair, 1)
            self.assertFalse(plan.desktop_enabled)

    def test_real_integration_coordinator_does_not_create_cleanup_only_state(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=True,
            )

            backend.assert_absent(plan)
            backend.write(plan)
            backend.verify(plan)

            self.assertEqual(set(shortcuts.values), {paths.start_menu_shortcut, paths.desktop_shortcut})
            self.assertEqual(registry.keys[RUN_KEY][RUN_VALUE_NAME][0], plan.run_command)
            self.assertEqual(
                {name: value for name, (value, _kind) in registry.keys[UNINSTALL_KEY].items()},
                plan.registration.registry_values(),
            )
            self.assertNotIn(STARTUP_APPROVED_RUN_KEY, registry.keys)

            backend.rollback(plan)
            self.assertFalse(shortcuts.values)
            self.assertNotIn(RUN_VALUE_NAME, registry.keys[RUN_KEY])
            self.assertNotIn(UNINSTALL_KEY, registry.keys)
            self.assertNotIn(STARTUP_APPROVED_RUN_KEY, registry.keys)

    def test_optional_desktop_shortcut_is_verified_absent(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )

            backend.write(plan)
            backend.verify(plan)

            self.assertIn(paths.start_menu_shortcut, shortcuts.values)
            self.assertNotIn(paths.desktop_shortcut, shortcuts.values)

    def test_uninstall_removes_only_objects_that_still_match(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=True,
            )
            backend.write(plan)
            registry.SetValueEx(
                _Key(registry, RUN_KEY),
                RUN_VALUE_NAME,
                0,
                registry.REG_SZ,
                "user-modified-command",
            )
            shortcuts.values[paths.desktop_shortcut] = ShortcutSpec(
                path=paths.desktop_shortcut,
                target=paths.main_executable,
                arguments="",
                working_directory=paths.app_directory,
                description="用户修改过的说明",
                icon_location=f"{paths.main_executable},0",
            )

            result = backend.remove_owned(plan)

            self.assertFalse(result.complete)
            self.assertIn("start_menu_shortcut", result.removed)
            self.assertIn("uninstall_key", result.removed)
            self.assertIn(RUN_VALUE_NAME, registry.keys[RUN_KEY])
            self.assertIn(paths.desktop_shortcut, shortcuts.values)
            self.assertTrue(any("run_value" in item for item in result.residuals))
            self.assertTrue(any("desktop_shortcut" in item for item in result.residuals))
            self.assertIn("startup_approved_run_value", result.absent)

    def test_uninstall_removes_only_exact_startup_approved_binary_value(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )
            backend.write(plan)
            startup_values = registry.keys.setdefault(STARTUP_APPROVED_RUN_KEY, {})
            startup_values[RUN_VALUE_NAME] = (b"\x03\x00\x00\x00", registry.REG_BINARY)
            startup_values["OtherProduct"] = (b"\x02\x00\x00\x00", registry.REG_BINARY)

            result = backend.remove_owned(plan)

            self.assertTrue(result.complete)
            self.assertIn("startup_approved_run_value", result.removed)
            self.assertNotIn(RUN_VALUE_NAME, registry.keys[STARTUP_APPROVED_RUN_KEY])
            self.assertEqual(
                registry.keys[STARTUP_APPROVED_RUN_KEY]["OtherProduct"],
                (b"\x02\x00\x00\x00", registry.REG_BINARY),
            )

    def test_uninstall_preserves_unexpected_startup_approved_type_as_residual(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )
            backend.write(plan)
            startup_values = registry.keys.setdefault(STARTUP_APPROVED_RUN_KEY, {})
            startup_values[RUN_VALUE_NAME] = ("unexpected", registry.REG_SZ)

            result = backend.remove_owned(plan)

            self.assertFalse(result.complete)
            self.assertEqual(
                registry.keys[STARTUP_APPROVED_RUN_KEY][RUN_VALUE_NAME],
                ("unexpected", registry.REG_SZ),
            )
            self.assertTrue(
                any("startup_approved_run_value" in item for item in result.residuals)
            )

    def test_foreign_existing_object_blocks_before_write(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            plan = IntegrationPlan.create(
                paths,
                version="0.8.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )
            shortcuts.values[plan.start_menu_shortcut.path] = plan.start_menu_shortcut
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)

            with self.assertRaises(SystemIntegrationError):
                backend.assert_absent(plan)
            self.assertFalse(registry.keys)

    def test_upgrade_changes_only_registration_version_and_size(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan_a = IntegrationPlan.create(
                paths,
                version="1.0.0",
                estimated_size_kib=100,
                desktop_enabled=True,
            )
            backend.write(plan_a)
            registry.keys.setdefault(STARTUP_APPROVED_RUN_KEY, {})[
                RUN_VALUE_NAME
            ] = (b"\x03\x00\x00\x00", registry.REG_BINARY)
            snapshot = backend.snapshot(plan_a)
            shortcuts_before = dict(shortcuts.values)
            run_before = registry.keys[RUN_KEY][RUN_VALUE_NAME]
            startup_approved_before = dict(
                registry.keys[STARTUP_APPROVED_RUN_KEY]
            )

            backend.update_registration(
                snapshot,
                version="2.0.0",
                estimated_size_kib=250,
            )
            backend.verify_upgrade(
                snapshot,
                version="2.0.0",
                estimated_size_kib=250,
            )

            self.assertEqual(shortcuts.values, shortcuts_before)
            self.assertEqual(registry.keys[RUN_KEY][RUN_VALUE_NAME], run_before)
            self.assertEqual(
                registry.keys[STARTUP_APPROVED_RUN_KEY],
                startup_approved_before,
            )
            self.assertEqual(
                registry.keys[UNINSTALL_KEY]["DisplayVersion"][0],
                "2.0.0",
            )
            self.assertEqual(registry.keys[UNINSTALL_KEY]["EstimatedSize"][0], 250)

            backend.restore_upgrade(snapshot)
            self.assertEqual(
                registry.keys[STARTUP_APPROVED_RUN_KEY],
                startup_approved_before,
            )
            self.assertEqual(
                registry.keys[UNINSTALL_KEY]["DisplayVersion"][0],
                "1.0.0",
            )
            self.assertEqual(registry.keys[UNINSTALL_KEY]["EstimatedSize"][0], 100)
            self.assertEqual(
                registry.keys[UNINSTALL_KEY]["EstimatedSize"][1],
                registry.REG_DWORD,
            )

    def test_upgrade_rollback_restores_missing_estimated_size_as_missing(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan_a = IntegrationPlan.create(
                paths,
                version="1.0.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )
            backend.write(plan_a)
            registry.DeleteValue(_Key(registry, UNINSTALL_KEY), "EstimatedSize")

            snapshot = backend.snapshot(plan_a)
            self.assertFalse(snapshot.estimated_size_exists)

            backend.update_registration(
                snapshot,
                version="2.0.0",
                estimated_size_kib=250,
            )
            self.assertEqual(registry.keys[UNINSTALL_KEY]["EstimatedSize"][0], 250)

            backend.restore_upgrade(snapshot)

            self.assertEqual(
                registry.keys[UNINSTALL_KEY]["DisplayVersion"][0],
                "1.0.0",
            )
            self.assertNotIn("EstimatedSize", registry.keys[UNINSTALL_KEY])

    def test_upgrade_preserves_deleted_run_and_absent_desktop(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            shortcuts = _Shortcuts()
            registry = _Registry()
            backend = WindowsFirstInstallIntegration(shortcuts, registry_module=registry)
            plan_a = IntegrationPlan.create(
                paths,
                version="1.0.0",
                estimated_size_kib=100,
                desktop_enabled=False,
            )
            backend.write(plan_a)
            registry.DeleteValue(_Key(registry, RUN_KEY), RUN_VALUE_NAME)
            snapshot = backend.snapshot(plan_a)
            self.assertIsNone(snapshot.run_command)
            self.assertIsNone(snapshot.desktop_shortcut)

            backend.update_registration(
                snapshot,
                version="2.0.0",
                estimated_size_kib=200,
            )
            backend.verify_upgrade(
                snapshot,
                version="2.0.0",
                estimated_size_kib=200,
            )

            self.assertNotIn(RUN_VALUE_NAME, registry.keys[RUN_KEY])
            self.assertNotIn(paths.desktop_shortcut, shortcuts.values)

    @unittest.skipUnless(os.name == "nt", "Windows shortcut integration")
    def test_powershell_shortcut_bridge_writes_reads_aumid_and_removes(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            target = root / "app" / "LanDrop.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"test")
            shortcut_path = root / "Start Menu" / "LanDrop" / "LanDrop.lnk"
            desktop_path = root / "Desktop" / "LanDrop.lnk"
            backend = PowerShellShortcutBackend(
                (shortcut_path, desktop_path),
                Path(__file__).resolve().parent.parent / "scripts" / "shortcut-bridge.ps1",
            )
            spec = ShortcutSpec(
                path=shortcut_path,
                target=target,
                arguments="",
                working_directory=target.parent,
                description="LanDrop test",
                icon_location=f"{target},0",
            )

            backend.write(spec)
            self.assertEqual(backend.read(shortcut_path), spec)
            backend.remove_created(shortcut_path)
            self.assertIsNone(backend.read(shortcut_path))


if __name__ == "__main__":
    unittest.main()
