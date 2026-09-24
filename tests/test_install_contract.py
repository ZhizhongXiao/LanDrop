from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import unittest

from landrop.install_contract import (
    APP_USER_MODEL_ID,
    InstallContractError,
    InstallPaths,
    UnsafeInstallPathError,
    is_transaction_directory_name,
    is_uninstall_directory_name,
    lifecycle_mutex_name,
    transaction_directory_name,
    uninstall_directory_name,
    validate_install_child,
    validate_transaction_directory,
    validate_uninstall_temp_directory,
)
from tests.support import temporary_directory


class InstallContractTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "local_data",
            root / "roaming_data",
            root / "desktop",
            root / "temp",
        )

    def test_fixed_current_user_layout_and_system_object_allowlist(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))

            self.assertEqual(paths.install_root, paths.local_app_data / "Programs" / "LanDrop")
            self.assertEqual(paths.main_executable, paths.install_root / "app" / "LanDrop.exe")
            self.assertEqual(
                paths.uninstall_executable,
                paths.install_root / "maintenance" / "Uninstall.exe",
            )
            self.assertEqual(paths.data_root, paths.local_app_data / "LanDrop")
            self.assertEqual(paths.install_history_path.name, "install-history.jsonl")
            self.assertEqual(APP_USER_MODEL_ID, "LanDrop.Desktop")
            objects = paths.system_integration_objects()
            self.assertEqual(
                {item.object_id for item in objects},
                {
                    "start_menu_shortcut",
                    "desktop_shortcut",
                    "run_value",
                    "uninstall_key",
                },
            )
            self.assertTrue(next(item for item in objects if item.object_id == "desktop_shortcut").optional)
            self.assertFalse(any("firewall" in item.identifier.casefold() for item in objects))

    def test_environment_paths_must_be_absolute(self) -> None:
        with self.assertRaises(InstallContractError):
            InstallPaths.from_environment(
                {
                    "LOCALAPPDATA": "relative",
                    "APPDATA": r"C:\Users\Test\AppData\Roaming",
                    "USERPROFILE": r"C:\Users\Test",
                    "TEMP": r"C:\Temp",
                }
            )

    def test_transaction_and_uninstall_names_are_strict(self) -> None:
        transaction_id = "a" * 32
        staging = transaction_directory_name("staging", "0.8.0", transaction_id)
        rollback = transaction_directory_name("rollback", "0.7.0", transaction_id)
        uninstall = uninstall_directory_name(transaction_id)

        self.assertEqual(staging, f".staging-0.8.0-{transaction_id}")
        self.assertTrue(is_transaction_directory_name(staging))
        self.assertTrue(is_transaction_directory_name(rollback))
        self.assertTrue(is_uninstall_directory_name(uninstall))
        self.assertFalse(is_transaction_directory_name(".staging-../../outside"))
        self.assertFalse(is_uninstall_directory_name("uninstall-../outside"))
        with self.assertRaises(InstallContractError):
            transaction_directory_name("staging", "../0.8", transaction_id)
        with self.assertRaises(InstallContractError):
            uninstall_directory_name("ABC")

    def test_path_validation_rejects_escape_nested_target_and_wrong_names(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            transaction_id = "b" * 32
            controlled = paths.install_root / transaction_directory_name(
                "staging", "0.8.0", transaction_id
            )
            self.assertEqual(validate_transaction_directory(controlled, paths), controlled)
            self.assertEqual(
                validate_uninstall_temp_directory(
                    paths.uninstall_temp_root / uninstall_directory_name(transaction_id),
                    paths,
                ),
                paths.uninstall_temp_root / uninstall_directory_name(transaction_id),
            )
            with self.assertRaises(UnsafeInstallPathError):
                validate_install_child(root / "outside", paths)
            with self.assertRaises(UnsafeInstallPathError):
                validate_transaction_directory(paths.install_root / "nested" / controlled.name, paths)
            with self.assertRaises(UnsafeInstallPathError):
                validate_transaction_directory(paths.install_root / "ordinary", paths)

    def test_existing_reparse_component_is_rejected_fail_closed(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            trap = paths.install_root / "trap"
            trap.mkdir(parents=True)
            target = trap / "child"

            with patch(
                "landrop.install_contract.is_reparse_object",
                side_effect=lambda path: path.name == "trap",
            ):
                with self.assertRaises(UnsafeInstallPathError):
                    validate_install_child(target, paths)

    def test_reparse_between_system_anchor_and_product_root_is_rejected(self) -> None:
        with temporary_directory() as temporary:
            paths = self._paths(Path(temporary))
            paths.install_root.parent.mkdir(parents=True)
            target = paths.install_root / "metadata" / "install.json"

            with patch(
                "landrop.install_contract.is_reparse_object",
                side_effect=lambda path: path.name == "Programs",
            ):
                with self.assertRaises(UnsafeInstallPathError):
                    validate_install_child(target, paths)

    def test_lifecycle_mutex_identity_is_stable_and_user_path_scoped(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            first = self._paths(root / "one")
            second = self._paths(root / "two")
            self.assertEqual(lifecycle_mutex_name(first), lifecycle_mutex_name(first))
            self.assertNotEqual(lifecycle_mutex_name(first), lifecycle_mutex_name(second))
            self.assertTrue(lifecycle_mutex_name(first).startswith("Global\\LanDrop.InstallLifecycle."))
            self.assertNotIn(str(first.local_app_data), lifecycle_mutex_name(first))


if __name__ == "__main__":
    unittest.main()
