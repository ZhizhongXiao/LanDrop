from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import unittest

import landrop.gui as gui
from landrop.install_contract import InstallPaths, transaction_directory_name
from landrop.install_lock import InstallLifecycleLockError
from landrop.install_state import InstallationStateStore, TransactionRecord
from tests.support import temporary_directory


class _FakeLifecycleLock:
    def __init__(self, events: list[str], *, available: bool = True) -> None:
        self.events = events
        self.available = available

    def acquire(self, _timeout: float) -> bool:
        self.events.append("lifecycle.acquire")
        return self.available

    def close(self) -> None:
        self.events.append("lifecycle.close")


class _FakeDesktopInstance:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def acquire(self) -> bool:
        self.events.append("desktop.acquire")
        return True

    def close(self) -> None:
        self.events.append("desktop.close")


class _RecordingStateStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def ensure_normal_start_allowed(self) -> None:
        self.events.append("transaction.check")


class DesktopStartupGateTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "local_data",
            root / "roaming_data",
            root / "desktop",
            root / "temp",
        )

    def test_lifecycle_lock_covers_transaction_check_and_single_instance_creation(self) -> None:
        with temporary_directory() as temporary:
            events: list[str] = []
            desktop = _FakeDesktopInstance(events)
            instance, primary = gui._acquire_desktop_startup_ownership(
                Path(temporary) / "data",
                install_paths=self._paths(Path(temporary)),
                lifecycle_lock=_FakeLifecycleLock(events),  # type: ignore[arg-type]
                state_store=_RecordingStateStore(events),  # type: ignore[arg-type]
                single_instance=desktop,  # type: ignore[arg-type]
            )
            self.assertIs(instance, desktop)
            self.assertTrue(primary)
            self.assertEqual(
                events,
                ["lifecycle.acquire", "transaction.check", "desktop.acquire", "lifecycle.close"],
            )

    def test_windows_startup_keeps_the_single_window_hidden_and_unfocused(self) -> None:
        self.assertEqual(
            gui._desktop_window_visibility(True),
            {"hidden": True, "focus": False},
        )
        self.assertEqual(
            gui._desktop_window_visibility(False),
            {"hidden": False, "focus": True},
        )

    def test_transaction_residue_blocks_before_desktop_single_instance(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            transaction_id = "f" * 32
            transaction = TransactionRecord.create(
                transaction_id=transaction_id,
                kind="install",
                target_version="0.8.0",
                target_build_id="build-b",
                staging_directory=transaction_directory_name(
                    "staging", "0.8.0", transaction_id
                ),
            )
            InstallationStateStore(paths).write_transaction(transaction)
            events: list[str] = []

            with self.assertRaisesRegex(RuntimeError, "未完成"):
                gui._acquire_desktop_startup_ownership(
                    root / "data",
                    install_paths=paths,
                    lifecycle_lock=_FakeLifecycleLock(events),  # type: ignore[arg-type]
                    state_store=InstallationStateStore(paths),
                    single_instance=_FakeDesktopInstance(events),  # type: ignore[arg-type]
                )
            self.assertEqual(events, ["lifecycle.acquire", "desktop.close", "lifecycle.close"])

    def test_malformed_transaction_blocks_before_desktop_single_instance(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.metadata_directory.mkdir(parents=True)
            paths.transaction_state_path.write_text("{broken", encoding="utf-8")
            events: list[str] = []

            with self.assertRaisesRegex(RuntimeError, "损坏"):
                gui._acquire_desktop_startup_ownership(
                    root / "data",
                    install_paths=paths,
                    lifecycle_lock=_FakeLifecycleLock(events),  # type: ignore[arg-type]
                    state_store=InstallationStateStore(paths),
                    single_instance=_FakeDesktopInstance(events),  # type: ignore[arg-type]
                )
            self.assertEqual(events, ["lifecycle.acquire", "desktop.close", "lifecycle.close"])

    def test_maintenance_lock_contention_blocks_before_transaction_check(self) -> None:
        with temporary_directory() as temporary:
            events: list[str] = []
            with self.assertRaisesRegex(InstallLifecycleLockError, "安装"):
                gui._acquire_desktop_startup_ownership(
                    Path(temporary) / "data",
                    install_paths=self._paths(Path(temporary)),
                    lifecycle_lock=_FakeLifecycleLock(events, available=False),  # type: ignore[arg-type]
                    state_store=_RecordingStateStore(events),  # type: ignore[arg-type]
                    single_instance=_FakeDesktopInstance(events),  # type: ignore[arg-type]
                )
            self.assertEqual(events, ["lifecycle.acquire", "desktop.close", "lifecycle.close"])

    @unittest.skipUnless(os.name == "nt", "Desktop self-check is Windows-only")
    def test_self_check_bypasses_both_locks_and_runtime_objects(self) -> None:
        fake_logger = MagicMock()
        fake_webview = object()
        with (
            patch.dict(sys.modules, {"webview": fake_webview}),
            patch("landrop.gui.configure_application_logging", return_value=fake_logger),
            patch("landrop.gui.install_exception_hooks", return_value=lambda: None),
            patch("landrop.gui._portable_self_check", return_value=0) as self_check,
            patch("landrop.gui.InstallLifecycleLock") as lifecycle_lock,
            patch("landrop.gui.DesktopSingleInstance") as desktop_lock,
            patch("landrop.gui.ServiceController") as service,
            patch("landrop.gui.LanDropTray") as tray,
        ):
            result = gui.main(["--self-check"])

        self.assertEqual(result, 0)
        self_check.assert_called_once_with(fake_logger)
        lifecycle_lock.assert_not_called()
        desktop_lock.assert_not_called()
        service.assert_not_called()
        tray.assert_not_called()


if __name__ == "__main__":
    unittest.main()
