from __future__ import annotations

from pathlib import Path
import os
import shutil
from typing import Callable
import unittest

from landrop.install_contract import InstallPaths, transaction_directory_name
from landrop.install_state import InstallRecord, InstallationStateStore, TransactionRecord
from landrop.payload_manifest import build_payload_manifest
from landrop.system_integration import (
    IntegrationPlan,
    UpgradeIntegrationSnapshot,
)
from landrop.upgrade import (
    UpgradeError,
    UpgradeService,
    _remove_transaction_tree,
    compare_versions,
    inspect_setup_state,
    retry_pending_cleanup,
)
from tests.support import temporary_directory


class _FakeLock:
    def __init__(self) -> None:
        self.owned = False

    def acquire(self, _timeout: float) -> bool:
        self.owned = True
        return True

    def close(self) -> None:
        self.owned = False


class _FakeUpgradeIntegration:
    def __init__(self, *, fail_at: str = "") -> None:
        self.fail_at = fail_at
        self.version = "1.0.0"
        self.size = 100
        self.update_calls = 0
        self.restore_calls = 0

    def snapshot(self, plan: IntegrationPlan) -> UpgradeIntegrationSnapshot:
        if self.fail_at == "snapshot":
            raise RuntimeError("snapshot")
        registration = plan.registration.__class__(
            display_name=plan.registration.display_name,
            display_version=self.version,
            publisher=plan.registration.publisher,
            display_icon=plan.registration.display_icon,
            install_location=plan.registration.install_location,
            uninstall_string=plan.registration.uninstall_string,
            estimated_size_kib=self.size,
        )
        return UpgradeIntegrationSnapshot(
            start_menu_shortcut=plan.start_menu_shortcut,
            desktop_shortcut_path=plan.desktop_shortcut.path,
            desktop_shortcut=None,
            run_command=None,
            registration=registration,
        )

    def update_registration(
        self,
        _snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None:
        self.update_calls += 1
        self.version = version
        if self.fail_at == "integration_write":
            raise RuntimeError("integration_write")
        self.size = estimated_size_kib

    def verify_upgrade(
        self,
        _snapshot: UpgradeIntegrationSnapshot,
        *,
        version: str,
        estimated_size_kib: int,
    ) -> None:
        if self.fail_at == "integration_read":
            raise RuntimeError("integration_read")
        if self.version != version or self.size != estimated_size_kib:
            raise RuntimeError("registration mismatch")

    def restore_upgrade(self, snapshot: UpgradeIntegrationSnapshot) -> None:
        self.restore_calls += 1
        self.version = snapshot.registration.display_version
        self.size = snapshot.registration.estimated_size_kib


class _FailCommitStore(InstallationStateStore):
    def __init__(self, paths: InstallPaths) -> None:
        super().__init__(paths)
        self.failed = False

    def write_install(self, record: InstallRecord) -> None:
        super().write_install(record)
        if record.version == "2.0.0" and not self.failed:
            self.failed = True
            raise RuntimeError("commit failure after publish")


class UpgradeTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "local_data",
            root / "roaming_data",
            root / "desktop",
            root / "temp",
        )

    def _prepare_a(self, paths: InstallPaths) -> InstallationStateStore:
        (paths.app_directory / "_internal").mkdir(parents=True)
        paths.maintenance_directory.mkdir(parents=True)
        paths.metadata_directory.mkdir(parents=True)
        paths.main_executable.write_bytes(b"app-a")
        (paths.app_directory / "_internal" / "a-only.sentinel").write_bytes(b"old")
        paths.uninstall_executable.write_bytes(b"uninstall-a")
        store = InstallationStateStore(paths)
        store.write_install(
            InstallRecord.create(
                paths,
                version="1.0.0",
                build_id="build-a",
                installed_at="2026-01-01T00:00:00+00:00",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        return store

    def _payload_b(self, root: Path):
        payload = root / "bundle" / "setup_payload"
        (payload / "app" / "_internal").mkdir(parents=True)
        (payload / "maintenance").mkdir(parents=True)
        (payload / "app" / "LanDrop.exe").write_bytes(b"app-b")
        (payload / "app" / "_internal" / "b-only.dat").write_bytes(b"new")
        (payload / "maintenance" / "Uninstall.exe").write_bytes(b"uninstall-b")
        return payload, build_payload_manifest(
            payload,
            version="2.0.0",
            build_id="build-b",
        )

    def _service(
        self,
        root: Path,
        *,
        integration: _FakeUpgradeIntegration | None = None,
        self_check: Callable[[Path], bool] | None = None,
        state_store: InstallationStateStore | None = None,
        checkpoint: Callable[[str], None] | None = None,
        remove_tree=None,
        running: bool = False,
    ):
        paths = self._paths(root)
        default_store = self._prepare_a(paths)
        payload, manifest = self._payload_b(root)
        chosen_store = state_store or default_store
        fake_integration = integration or _FakeUpgradeIntegration()
        service = UpgradeService(
            paths=paths,
            payload_root=payload,
            manifest=manifest,
            integration=fake_integration,
            self_check=self_check or (lambda _path: True),
            state_store=chosen_store,
            lifecycle_lock=_FakeLock(),  # type: ignore[arg-type]
            executable_running=lambda _path: running,
            checkpoint=checkpoint,
            remove_tree=remove_tree,
        )
        return service, paths, chosen_store, fake_integration

    def _assert_a_restored(
        self,
        paths: InstallPaths,
        store: InstallationStateStore,
        integration: _FakeUpgradeIntegration,
    ) -> None:
        record = store.read_install(required=True)
        assert record is not None
        self.assertEqual(record.version, "1.0.0")
        self.assertEqual(paths.main_executable.read_bytes(), b"app-a")
        self.assertTrue((paths.app_directory / "_internal" / "a-only.sentinel").is_file())
        self.assertFalse(paths.transaction_state_path.exists())
        self.assertFalse(
            any(
                child.name.startswith((".staging-", ".rollback-"))
                for child in paths.install_root.iterdir()
            )
        )
        self.assertEqual(integration.version, "1.0.0")

    def _leave_interrupted_upgrade(
        self,
        service: UpgradeService,
        paths: InstallPaths,
        store: InstallationStateStore,
        integration: _FakeUpgradeIntegration,
        *,
        stage: str,
        committed: bool = False,
    ) -> TransactionRecord:
        transaction_id = "a" * 32
        staging_name = transaction_directory_name("staging", "2.0.0", transaction_id)
        rollback_name = transaction_directory_name("rollback", "1.0.0", transaction_id)
        source_plan = IntegrationPlan.create(
            paths,
            version="1.0.0",
            estimated_size_kib=100,
            desktop_enabled=False,
        )
        transaction = TransactionRecord.create(
            transaction_id=transaction_id,
            kind="upgrade",
            source_version="1.0.0",
            source_build_id="build-a",
            target_version="2.0.0",
            target_build_id="build-b",
            staging_directory=staging_name,
            rollback_directory=rollback_name,
            integration_snapshot=integration.snapshot(source_plan).to_json(),
        )
        store.write_transaction(transaction)
        staging = paths.install_root / staging_name
        rollback = paths.install_root / rollback_name
        shutil.copytree(service.payload_root / "app", staging)
        if stage != "prepared":
            os.replace(paths.app_directory, rollback)
            os.replace(staging, paths.app_directory)
            transaction = transaction.advance("app_switched")
            store.write_transaction(transaction)
        if stage in {"integration_written", "integration_verified"}:
            integration.version = "2.0.0"
            integration.size = max(
                1,
                (sum(entry.size for entry in service.manifest.files) + 1023) // 1024,
            )
            transaction = transaction.advance("integration_written")
            store.write_transaction(transaction)
        if stage == "integration_verified":
            transaction = transaction.advance("integration_verified")
            store.write_transaction(transaction)
        if committed:
            current = store.read_install(required=True)
            assert current is not None
            store.write_install(
                InstallRecord.create(
                    paths,
                    version="2.0.0",
                    build_id="build-b",
                    installed_at=current.installed_at,
                )
            )
        return transaction

    def test_success_switches_whole_app_and_removes_a_only_sentinel(self) -> None:
        with temporary_directory() as temporary:
            service, paths, store, integration = self._service(Path(temporary))

            outcome = service.upgrade()

            self.assertTrue(outcome.verified)
            self.assertTrue(outcome.committed)
            self.assertFalse(outcome.cleanup_pending)
            record = store.read_install(required=True)
            assert record is not None
            self.assertEqual((record.version, record.build_id), ("2.0.0", "build-b"))
            self.assertEqual(paths.main_executable.read_bytes(), b"app-b")
            self.assertTrue((paths.app_directory / "_internal" / "b-only.dat").is_file())
            self.assertFalse((paths.app_directory / "_internal" / "a-only.sentinel").exists())
            self.assertEqual(paths.uninstall_executable.read_bytes(), b"uninstall-a")
            self.assertEqual(integration.version, "2.0.0")
            self.assertFalse(paths.transaction_state_path.exists())

    def test_each_precommit_failure_restores_a_and_old_install_json(self) -> None:
        cases = (
            "staging_verify",
            "app_to_rollback",
            "staging_to_app",
            "self_check",
            "integration_write",
            "integration_read",
            "install_commit",
        )
        for case in cases:
            with self.subTest(case=case), temporary_directory() as temporary:
                root = Path(temporary)
                integration = _FakeUpgradeIntegration(
                    fail_at=case if case.startswith("integration_") else ""
                )

                def checkpoint(point: str) -> None:
                    if case == "staging_verify" and point == "before_staging_verify":
                        staging = next(
                            child
                            for child in self._paths(root).install_root.iterdir()
                            if child.name.startswith(".staging-")
                        )
                        (staging / "LanDrop.exe").write_bytes(b"tampered")
                    if case == "app_to_rollback" and point == "before_app_to_rollback":
                        raise RuntimeError(case)
                    if case == "staging_to_app" and point == "before_staging_to_app":
                        raise RuntimeError(case)

                paths = self._paths(root)
                precreated_store = _FailCommitStore(paths) if case == "install_commit" else None
                service, paths, store, integration = self._service(
                    root,
                    integration=integration,
                    self_check=(lambda _path: case != "self_check"),
                    state_store=precreated_store,
                    checkpoint=checkpoint,
                )

                outcome = service.upgrade()

                self.assertFalse(outcome.committed)
                self.assertTrue(outcome.rollback_succeeded, outcome.message)
                self._assert_a_restored(paths, store, integration)

    def test_cleanup_failure_keeps_b_and_records_pending_then_retry_clears_it(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)

            def fail_rollback_cleanup(path: Path, paths: InstallPaths) -> None:
                if path.name.startswith(".rollback-"):
                    raise PermissionError("locked")
                _remove_transaction_tree(path, paths)

            service, paths, store, integration = self._service(
                root,
                remove_tree=fail_rollback_cleanup,
            )

            outcome = service.upgrade()

            self.assertTrue(outcome.committed)
            self.assertTrue(outcome.verified)
            self.assertTrue(outcome.cleanup_pending)
            record = store.read_install(required=True)
            assert record is not None
            self.assertEqual(record.version, "2.0.0")
            self.assertEqual(len(record.pending_cleanup), 1)
            pending_path = paths.install_root / record.pending_cleanup[0]
            self.assertTrue(pending_path.is_dir())
            self.assertEqual(paths.main_executable.read_bytes(), b"app-b")
            self.assertEqual(integration.version, "2.0.0")

            cleanup = retry_pending_cleanup(paths, state_store=store)

            self.assertEqual(cleanup.removed, 1)
            self.assertFalse(cleanup.remaining)
            self.assertFalse(pending_path.exists())
            updated = store.read_install(required=True)
            assert updated is not None
            self.assertFalse(updated.pending_cleanup)
            self.assertEqual(updated.version, "2.0.0")

    def test_same_version_setup_retries_pending_cleanup_before_noop(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            service, paths, store, _integration = self._service(root)
            pending_name = ".rollback-1.0.0-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            pending_path = paths.install_root / pending_name
            pending_path.mkdir()
            (pending_path / "old.dat").write_bytes(b"old")
            current = store.read_install(required=True)
            assert current is not None
            store.write_install(
                InstallRecord.create(
                    paths,
                    version=current.version,
                    build_id=current.build_id,
                    installed_at=current.installed_at,
                    pending_cleanup=(pending_name,),
                )
            )
            same_manifest = build_payload_manifest(
                service.payload_root,
                version="1.0.0",
                build_id="same-build",
            )
            same = UpgradeService(
                paths=paths,
                payload_root=service.payload_root,
                manifest=same_manifest,
                integration=_FakeUpgradeIntegration(),
                state_store=store,
                lifecycle_lock=_FakeLock(),  # type: ignore[arg-type]
                executable_running=lambda _path: False,
            ).upgrade()

            self.assertEqual(same.result, "already-installed")
            self.assertFalse(pending_path.exists())
            cleaned = store.read_install(required=True)
            assert cleaned is not None
            self.assertFalse(cleaned.pending_cleanup)
            self.assertEqual(paths.main_executable.read_bytes(), b"app-a")

    def test_restart_recovers_each_precommit_transaction_stage_then_upgrades(self) -> None:
        for stage in (
            "prepared",
            "app_switched",
            "integration_written",
            "integration_verified",
        ):
            with self.subTest(stage=stage), temporary_directory() as temporary:
                service, paths, store, integration = self._service(Path(temporary))
                self._leave_interrupted_upgrade(
                    service,
                    paths,
                    store,
                    integration,
                    stage=stage,
                )

                outcome = service.upgrade()

                self.assertTrue(outcome.verified, outcome.message)
                self.assertTrue(outcome.committed)
                record = store.read_install(required=True)
                assert record is not None
                self.assertEqual((record.version, record.build_id), ("2.0.0", "build-b"))
                self.assertEqual(paths.main_executable.read_bytes(), b"app-b")
                self.assertFalse(
                    (paths.app_directory / "_internal" / "a-only.sentinel").exists()
                )
                self.assertFalse(paths.transaction_state_path.exists())
                self.assertEqual(integration.version, "2.0.0")

    def test_restart_keeps_b_when_install_commit_won_crash_race(self) -> None:
        with temporary_directory() as temporary:
            service, paths, store, integration = self._service(Path(temporary))
            transaction = self._leave_interrupted_upgrade(
                service,
                paths,
                store,
                integration,
                stage="integration_verified",
                committed=True,
            )
            rollback = paths.install_root / str(transaction.rollback_directory)

            outcome = service.upgrade()

            self.assertTrue(outcome.verified, outcome.message)
            self.assertTrue(outcome.committed)
            self.assertFalse(outcome.cleanup_pending)
            self.assertFalse(rollback.exists())
            self.assertFalse(paths.transaction_state_path.exists())
            record = store.read_install(required=True)
            assert record is not None
            self.assertEqual((record.version, record.build_id), ("2.0.0", "build-b"))
            self.assertEqual(paths.main_executable.read_bytes(), b"app-b")

    def test_running_installed_executable_blocks_before_transaction(self) -> None:
        with temporary_directory() as temporary:
            service, paths, store, integration = self._service(
                Path(temporary),
                running=True,
            )

            outcome = service.upgrade()

            self.assertFalse(outcome.verified)
            self.assertFalse(outcome.rollback_attempted)
            self._assert_a_restored(paths, store, integration)

    def test_same_version_and_downgrade_do_not_overlay(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            service, paths, store, _integration = self._service(root)
            same_manifest = build_payload_manifest(
                service.payload_root,
                version="1.0.0",
                build_id="same-build",
            )
            same = UpgradeService(
                paths=paths,
                payload_root=service.payload_root,
                manifest=same_manifest,
                integration=_FakeUpgradeIntegration(),
                state_store=store,
                lifecycle_lock=_FakeLock(),  # type: ignore[arg-type]
                executable_running=lambda _path: False,
            ).upgrade()
            self.assertEqual(same.result, "already-installed")
            self.assertEqual(paths.main_executable.read_bytes(), b"app-a")

            lower_manifest = build_payload_manifest(
                service.payload_root,
                version="0.9.0",
                build_id="lower-build",
            )
            lower = UpgradeService(
                paths=paths,
                payload_root=service.payload_root,
                manifest=lower_manifest,
                integration=_FakeUpgradeIntegration(),
                state_store=store,
                lifecycle_lock=_FakeLock(),  # type: ignore[arg-type]
                executable_running=lambda _path: False,
            ).upgrade()
            self.assertFalse(lower.verified)
            self.assertIn("不支持", lower.message)
            self.assertEqual(paths.main_executable.read_bytes(), b"app-a")

    def test_inspection_rejects_directory_without_authoritative_install(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            paths = self._paths(root)
            paths.install_root.mkdir(parents=True)
            payload, manifest = self._payload_b(root)

            inspection = inspect_setup_state(paths, manifest)

            self.assertEqual(inspection.disposition, "untrusted")
            self.assertIn("install.json", inspection.message)
            self.assertTrue(payload.is_dir())

    def test_numeric_version_comparison_is_explicit(self) -> None:
        self.assertGreater(compare_versions("1.10.0", "1.9.9"), 0)
        self.assertEqual(compare_versions("1.0", "1.0.0"), 0)
        with self.assertRaises(UpgradeError):
            compare_versions("1.0-beta", "1.0.0")


if __name__ == "__main__":
    unittest.main()
