from __future__ import annotations

from pathlib import Path
from typing import Callable
from unittest.mock import patch
import unittest

from landrop.install_contract import InstallPaths
from landrop.install_state import InstallationStateStore
from landrop.installer import FirstInstallOptions, FirstInstallService
from landrop.payload_manifest import build_payload_manifest
from landrop.system_integration import IntegrationPlan, SystemIntegrationError
from tests.support import temporary_directory


class _FakeLock:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.owned = False
        self.closed = False

    def acquire(self, _timeout: float) -> bool:
        self.owned = self.available
        return self.available

    def close(self) -> None:
        self.owned = False
        self.closed = True


class _FakeIntegration:
    OBJECTS = ("start_menu_shortcut", "desktop_shortcut", "run_value", "uninstall_key")

    def __init__(
        self,
        *,
        fail_at: str = "",
        observe: Callable[[str], None] | None = None,
    ) -> None:
        self.fail_at = fail_at
        self.observe = observe or (lambda _point: None)
        self.objects: set[str] = set()
        self.rollback_called = False

    def assert_absent(self, _plan: IntegrationPlan) -> None:
        self.observe("assert_absent")
        if self.objects:
            raise SystemIntegrationError("objects already exist")

    def write(self, plan: IntegrationPlan) -> None:
        for name in self.OBJECTS:
            if name == "desktop_shortcut" and not plan.desktop_enabled:
                continue
            point = f"write:{name}"
            self.observe(point)
            if self.fail_at == point:
                raise SystemIntegrationError(point)
            self.objects.add(name)

    def verify(self, plan: IntegrationPlan) -> None:
        for name in self.OBJECTS:
            point = f"read:{name}"
            self.observe(point)
            if self.fail_at == point:
                raise SystemIntegrationError(point)
            expected = name != "desktop_shortcut" or plan.desktop_enabled
            self.assertEqualForTest(name in self.objects, expected)

    @staticmethod
    def assertEqualForTest(actual: object, expected: object) -> None:
        if actual != expected:
            raise SystemIntegrationError("fake readback mismatch")

    def rollback(self, _plan: IntegrationPlan) -> None:
        self.rollback_called = True
        self.objects.clear()


class FirstInstallTests(unittest.TestCase):
    def _paths(self, root: Path) -> InstallPaths:
        return InstallPaths(
            root / "local_data",
            root / "roaming_data",
            root / "desktop",
            root / "temp",
        )

    def _payload(self, root: Path):
        payload = root / "bundle" / "setup_payload"
        (payload / "app" / "_internal").mkdir(parents=True)
        (payload / "maintenance").mkdir(parents=True)
        (payload / "app" / "LanDrop.exe").write_bytes(b"main-executable")
        (payload / "app" / "_internal" / "runtime.dat").write_bytes(b"runtime")
        (payload / "maintenance" / "Uninstall.exe").write_bytes(b"uninstaller")
        manifest = build_payload_manifest(payload, version="0.8.0", build_id="build-b")
        return payload, manifest

    def _service(
        self,
        root: Path,
        *,
        integration: _FakeIntegration | None = None,
        self_check: Callable[[Path], bool] | None = None,
        lock: _FakeLock | None = None,
    ) -> tuple[FirstInstallService, InstallPaths, _FakeIntegration, _FakeLock]:
        paths = self._paths(root)
        payload, manifest = self._payload(root)
        fake_integration = integration or _FakeIntegration()
        fake_lock = lock or _FakeLock()
        service = FirstInstallService(
            paths=paths,
            payload_root=payload,
            manifest=manifest,
            integration=fake_integration,
            self_check=self_check or (lambda _path: True),
            lifecycle_lock=fake_lock,  # type: ignore[arg-type]
        )
        return service, paths, fake_integration, fake_lock

    def test_first_install_success_commits_only_after_self_check_and_readback(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            observations: list[str] = []
            paths = self._paths(root)

            def observe(point: str) -> None:
                observations.append(point)
                self.assertFalse(paths.install_state_path.exists())

            integration = _FakeIntegration(observe=observe)
            lock = _FakeLock()

            def self_check(path: Path) -> bool:
                observations.append("self_check")
                self.assertTrue(lock.owned)
                self.assertEqual(path, paths.main_executable)
                self.assertFalse(paths.install_state_path.exists())
                return True

            service, paths, _, _ = self._service(
                root,
                integration=integration,
                self_check=self_check,
                lock=lock,
            )
            stages: list[str] = []
            outcome = service.install(
                FirstInstallOptions(desktop_shortcut=True),
                progress=stages.append,
            )

            self.assertTrue(outcome.verified)
            self.assertEqual(outcome.result, "success")
            self.assertTrue(paths.main_executable.is_file())
            self.assertTrue(paths.uninstall_executable.is_file())
            self.assertFalse(paths.transaction_state_path.exists())
            self.assertEqual(InstallationStateStore(paths).read_install().version, "0.8.0")  # type: ignore[union-attr]
            self.assertFalse(any(path.name.startswith(".staging-") for path in paths.install_root.iterdir()))
            self.assertFalse(lock.owned)
            self.assertIn("self_check", observations)
            self.assertLess(observations.index("self_check"), observations.index("write:start_menu_shortcut"))
            self.assertLess(observations.index("write:uninstall_key"), observations.index("read:start_menu_shortcut"))
            self.assertEqual(stages[-1], "completed")

    def test_payload_tamper_fails_before_product_root_is_created(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            service, paths, integration, _lock = self._service(root)
            (service.payload_root / "app" / "LanDrop.exe").write_bytes(b"tampered")

            outcome = service.install(FirstInstallOptions())

            self.assertFalse(outcome.verified)
            self.assertTrue(outcome.rollback_succeeded)
            self.assertFalse(paths.install_root.exists())
            self.assertFalse(integration.rollback_called)

    def test_self_check_failure_removes_program_root_and_skips_integration(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            service, paths, integration, _lock = self._service(
                root, self_check=lambda _path: False
            )

            outcome = service.install(FirstInstallOptions())

            self.assertFalse(outcome.verified)
            self.assertTrue(outcome.rollback_succeeded)
            self.assertFalse(paths.install_root.exists())
            self.assertFalse(integration.objects)
            self.assertFalse(integration.rollback_called)

    def test_every_system_write_and_readback_failure_rolls_back_cleanly(self) -> None:
        points = [
            *(f"write:{name}" for name in _FakeIntegration.OBJECTS),
            *(f"read:{name}" for name in _FakeIntegration.OBJECTS),
        ]
        for point in points:
            with self.subTest(point=point), temporary_directory() as temporary:
                root = Path(temporary)
                integration = _FakeIntegration(fail_at=point)
                service, paths, _, _lock = self._service(root, integration=integration)

                outcome = service.install(FirstInstallOptions(desktop_shortcut=True))

                self.assertFalse(outcome.verified)
                self.assertTrue(outcome.rollback_attempted)
                self.assertTrue(outcome.rollback_succeeded)
                self.assertFalse(paths.install_root.exists())
                self.assertFalse(integration.objects)
                self.assertTrue(integration.rollback_called)

    def test_existing_install_root_is_never_removed_by_first_install(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            service, paths, integration, _lock = self._service(root)
            paths.install_root.mkdir(parents=True)
            marker = paths.install_root / "foreign.txt"
            marker.write_text("keep", encoding="utf-8")

            outcome = service.install(FirstInstallOptions())

            self.assertFalse(outcome.verified)
            self.assertTrue(marker.is_file())
            self.assertFalse(integration.rollback_called)

    def test_webview2_missing_blocks_before_bundle_or_product_writes(self) -> None:
        from landrop import setup_app

        with (
            patch("landrop.setup_app.webview2_runtime_version", return_value=""),
            patch("landrop.setup_app.SetupBundle") as bundle,
            patch("landrop.setup_app._show_native_error") as native_error,
        ):
            result = setup_app.main([])

        self.assertEqual(result, 2)
        bundle.assert_not_called()
        native_error.assert_called_once()

    def test_setup_logging_is_file_backed_after_preflight(self) -> None:
        from landrop.setup_app import _configure_setup_logging

        with temporary_directory() as temporary:
            data_root = Path(temporary) / "Local Data" / "LanDrop"
            logger = _configure_setup_logging(data_root)
            logger.error("diagnostic-marker")
            for handler in logger.handlers:
                handler.flush()

            log_path = data_root / "logs" / "setup.log"
            self.assertTrue(log_path.is_file())
            self.assertIn("diagnostic-marker", log_path.read_text(encoding="utf-8"))
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
