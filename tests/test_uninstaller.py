from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from landrop.install_contract import InstallPaths
from landrop.install_state import InstallRecord, InstallationStateStore, TransactionRecord
from landrop.system_integration import SystemIntegrationRemovalResult
from landrop.uninstaller import (
    UninstallError,
    UninstallLifecycleService,
    UninstallRequest,
    UninstallSelection,
    canonical_request_bytes,
    clean_selected_user_data,
    preflight_install_root_removal,
    read_bound_request,
    safe_remove_install_root,
)
from tests.support import temporary_directory


class _Lock:
    def __init__(self, _paths: InstallPaths) -> None:
        self.closed = False

    def acquire(self, _timeout: float) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _Integration:
    def __init__(self, residuals: tuple[str, ...] = ()) -> None:
        self.residuals = residuals
        self.calls = 0

    def remove_owned(self, _plan) -> SystemIntegrationRemovalResult:
        self.calls += 1
        return SystemIntegrationRemovalResult(
            removed=("run_value", "start_menu_shortcut", "uninstall_key"),
            absent=("desktop_shortcut",),
            residuals=self.residuals,
        )


class UninstallerTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "Local Data",
            root / "Roaming Data",
            root / "Desktop Folder",
            root / "Temp Folder",
        )

    def _installed(self, root: Path, *, build_id: str = "build-a"):
        paths = self._paths(root)
        paths.app_directory.mkdir(parents=True)
        paths.maintenance_directory.mkdir(parents=True)
        paths.metadata_directory.mkdir(parents=True)
        paths.main_executable.write_bytes(b"main-a")
        paths.uninstall_executable.write_bytes(b"uninstall-a")
        record = InstallRecord.create(paths, version="1.0.0", build_id=build_id)
        store = InstallationStateStore(paths)
        store.write_install(record)
        return paths, store, record

    def _service(self, paths: InstallPaths, store: InstallationStateStore, integration=None):
        return UninstallLifecycleService(
            paths=paths,
            integration=integration or _Integration(),
            state_store=store,
            now=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            lock_factory=_Lock,
            process_checker=lambda _path: False,
            pid_waiter=lambda _pid, _timeout: True,
            executable_waiter=lambda _path, _timeout: True,
        )

    def test_handoff_and_full_uninstall_keep_user_transfer_files(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            integration = _Integration()
            service = self._service(paths, store, integration)
            paths.data_root.mkdir(parents=True)
            (paths.data_root / "config.json").write_text("{}", encoding="utf-8")
            (paths.data_root / "credentials.json").write_text("{}", encoding="utf-8")
            paths.log_directory.mkdir()
            (paths.log_directory / "application.log").write_text("log", encoding="utf-8")
            shared = root / "Downloads" / "LanDrop" / "Shared"
            received = root / "Custom Received"
            shared.mkdir(parents=True)
            received.mkdir()
            (shared / "keep.bin").write_bytes(b"shared")
            (received / "keep.bin").write_bytes(b"received")

            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(True, True, True),
                original_pid=321,
            )
            outcome = service.execute_handoff(
                current_executable=handoff.temporary_executable,
                request_path=handoff.request_path,
                nonce=handoff.nonce,
                expected_request_sha256=handoff.expected_request_sha256,
            )

            self.assertTrue(outcome.complete)
            self.assertFalse(paths.install_root.exists())
            self.assertFalse((paths.data_root / "config.json").exists())
            self.assertFalse((paths.data_root / "credentials.json").exists())
            self.assertFalse(paths.log_directory.exists())
            self.assertEqual((shared / "keep.bin").read_bytes(), b"shared")
            self.assertEqual((received / "keep.bin").read_bytes(), b"received")
            self.assertEqual(integration.calls, 1)
            self.assertTrue(handoff.temporary_directory.exists())

    def test_default_selection_preserves_all_user_data_and_records_history(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            service = self._service(paths, store)
            paths.data_root.mkdir(parents=True)
            (paths.data_root / "config.json").write_text("{}", encoding="utf-8")
            (paths.data_root / "credentials.json").write_text("{}", encoding="utf-8")

            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            outcome = service.execute_handoff(
                current_executable=handoff.temporary_executable,
                request_path=handoff.request_path,
                nonce=handoff.nonce,
                expected_request_sha256=handoff.expected_request_sha256,
            )

            self.assertTrue(outcome.complete)
            self.assertTrue((paths.data_root / "config.json").is_file())
            self.assertTrue((paths.data_root / "credentials.json").is_file())
            records = [
                json.loads(line)
                for line in paths.install_history_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [item["event"] for item in records],
                ["uninstall_started", "uninstall_completed"],
            )

    def test_nonce_hash_expiry_and_canonical_encoding_are_bound(self) -> None:
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        request = UninstallRequest(
            nonce="ab" * 32,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(minutes=5)).isoformat(),
            original_pid=100,
            install_root=r"C:\Users\Example\AppData\Local\Programs\LanDrop",
            product_id="LanDrop",
            version="1.0.0",
            build_id="build-a",
            selection=UninstallSelection(),
            source_uninstaller_sha256="cd" * 32,
        )
        with temporary_directory() as temporary:
            path = Path(temporary) / "request.json"
            payload = canonical_request_bytes(request)
            path.write_bytes(payload)
            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(
                read_bound_request(path, nonce=request.nonce, expected_sha256=digest, now=now),
                request,
            )
            with self.assertRaises(UninstallError):
                read_bound_request(path, nonce="ef" * 32, expected_sha256=digest, now=now)
            with self.assertRaises(UninstallError):
                read_bound_request(path, nonce=request.nonce, expected_sha256="00" * 32, now=now)
            with self.assertRaises(UninstallError):
                read_bound_request(
                    path,
                    nonce=request.nonce,
                    expected_sha256=digest,
                    now=now + timedelta(minutes=6),
                )

    def test_source_and_temporary_self_hash_must_match(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            service = self._service(paths, store)
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            handoff.temporary_executable.write_bytes(b"tampered")

            with self.assertRaisesRegex(UninstallError, "哈希不一致"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=handoff.expected_request_sha256,
                )
            self.assertTrue(paths.install_root.exists())

    def test_original_process_must_exit_before_lock_and_delete(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            integration = _Integration()
            service = UninstallLifecycleService(
                paths=paths,
                integration=integration,
                state_store=store,
                now=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
                lock_factory=_Lock,
                process_checker=lambda _path: False,
                pid_waiter=lambda _pid, _timeout: False,
                executable_waiter=lambda _path, _timeout: True,
            )
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            with self.assertRaisesRegex(UninstallError, "仍在运行"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=handoff.expected_request_sha256,
                )
            self.assertEqual(integration.calls, 0)
            self.assertTrue(paths.install_root.exists())

    def test_pyinstaller_source_process_must_fully_exit(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            integration = _Integration()
            service = UninstallLifecycleService(
                paths=paths,
                integration=integration,
                state_store=store,
                now=lambda: datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
                lock_factory=_Lock,
                process_checker=lambda _path: False,
                pid_waiter=lambda _pid, _timeout: True,
                executable_waiter=lambda _path, _timeout: False,
            )
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )

            with self.assertRaisesRegex(UninstallError, "尚未完全退出"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=handoff.expected_request_sha256,
                )
            self.assertEqual(integration.calls, 0)
            self.assertTrue(paths.install_root.exists())

    def test_request_install_root_must_equal_fixed_current_user_root(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            service = self._service(paths, store)
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            request = read_bound_request(
                handoff.request_path,
                nonce=handoff.nonce,
                expected_sha256=handoff.expected_request_sha256,
                now=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
            )
            changed = replace(request, install_root=str(root / "Other Product"))
            payload = canonical_request_bytes(changed)
            handoff.request_path.write_bytes(payload)

            with self.assertRaisesRegex(UninstallError, "固定安装根"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=hashlib.sha256(payload).hexdigest(),
                )
            self.assertTrue(paths.install_root.exists())

    def test_upgrade_while_confirmation_open_rejects_old_request(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, record = self._installed(root)
            integration = _Integration()
            service = self._service(paths, store, integration)
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            store.write_install(replace(record, version="2.0.0", build_id="build-b"))

            with self.assertRaisesRegex(UninstallError, "状态已变化"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=handoff.expected_request_sha256,
                )
            self.assertEqual(integration.calls, 0)
            self.assertTrue(paths.install_root.exists())

    def test_transaction_present_rejects_request(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            service = self._service(paths, store)
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            store.write_transaction(
                TransactionRecord.create(
                    transaction_id="a" * 32,
                    kind="upgrade",
                    source_version="1.0.0",
                    source_build_id="build-a",
                    target_version="2.0.0",
                    target_build_id="build-b",
                    staging_directory=".staging-2.0.0-" + "a" * 32,
                    rollback_directory=".rollback-1.0.0-" + "a" * 32,
                )
            )
            with self.assertRaisesRegex(UninstallError, "未完成的安装事务"):
                service.execute_handoff(
                    current_executable=handoff.temporary_executable,
                    request_path=handoff.request_path,
                    nonce=handoff.nonce,
                    expected_request_sha256=handoff.expected_request_sha256,
                )
            self.assertTrue(paths.install_root.exists())

    def test_reparse_object_aborts_program_root_delete_before_any_removal(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, _store, _record = self._installed(root)
            marker = paths.app_directory / "unsafe-link"
            marker.mkdir()
            original = __import__("landrop.uninstaller", fromlist=["is_reparse_object"]).is_reparse_object

            with mock.patch(
                "landrop.uninstaller.is_reparse_object",
                side_effect=lambda path: _same(path, marker) or original(path),
            ):
                with self.assertRaises(Exception):
                    safe_remove_install_root(paths)

            self.assertTrue(paths.main_executable.exists())
            self.assertTrue(marker.exists())

    def test_reparse_preflight_runs_before_system_integration_removal(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths, store, _record = self._installed(root)
            integration = _Integration()
            service = self._service(paths, store, integration)
            handoff = service.prepare_handoff(
                source_executable=paths.uninstall_executable,
                selection=UninstallSelection(),
                original_pid=321,
            )
            marker = paths.app_directory / "unsafe-link"
            marker.mkdir()
            original = __import__("landrop.uninstaller", fromlist=["is_reparse_object"]).is_reparse_object

            with mock.patch(
                "landrop.uninstaller.is_reparse_object",
                side_effect=lambda path: _same(path, marker) or original(path),
            ):
                with self.assertRaisesRegex(Exception, "reparse object"):
                    service.execute_handoff(
                        current_executable=handoff.temporary_executable,
                        request_path=handoff.request_path,
                        nonce=handoff.nonce,
                        expected_request_sha256=handoff.expected_request_sha256,
                    )

            self.assertEqual(integration.calls, 0)
            self.assertTrue(paths.main_executable.exists())
            self.assertTrue(marker.exists())

    def test_data_whitelist_preserves_unknown_files(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.data_root.mkdir(parents=True)
            (paths.data_root / "config.json").write_text("{}", encoding="utf-8")
            (paths.data_root / "credentials.json").write_text("{}", encoding="utf-8")
            (paths.data_root / "unknown.bin").write_bytes(b"keep")
            paths.log_directory.mkdir()
            (paths.log_directory / "application.log").write_text("known", encoding="utf-8")
            (paths.log_directory / "user-note.txt").write_text("keep", encoding="utf-8")

            deleted = clean_selected_user_data(
                paths,
                UninstallSelection(True, True, True),
            )

            self.assertEqual(set(deleted), {"config", "logs", "trusted_clients"})
            self.assertTrue((paths.data_root / "unknown.bin").is_file())
            self.assertTrue((paths.log_directory / "user-note.txt").is_file())


def _same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


if __name__ == "__main__":
    unittest.main()
