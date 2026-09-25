from __future__ import annotations

import os
from pathlib import Path
import time
import unittest
from unittest import mock

from landrop.uninstall_app import UninstallApi, main
from landrop.uninstaller import UninstallOutcome


class _Service:
    def __init__(self, root: Path) -> None:
        self.paths = mock.Mock()
        self.paths.install_root = root / "install"
        self.executions = 0

    def execute_handoff(self, **_kwargs) -> UninstallOutcome:
        self.executions += 1
        return UninstallOutcome(complete=True, message="done")


class UninstallAppTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows WebView2 entry check")
    def test_missing_webview2_stops_before_path_or_product_initialization(self) -> None:
        with (
            mock.patch("landrop.uninstall_app.webview2_runtime_version", return_value=None),
            mock.patch("landrop.uninstall_app._show_native_error") as show,
            mock.patch(
                "landrop.uninstall_app.install_paths_for_current_windows_user"
            ) as paths,
        ):
            result = main([])
        self.assertEqual(result, 2)
        show.assert_called_once()
        paths.assert_not_called()

    def test_temporary_execution_schedules_self_cleanup_before_reporting_complete(self) -> None:
        service = _Service(Path("C:/test"))
        api = UninstallApi(
            service,  # type: ignore[arg-type]
            current_executable=Path("C:/Temp/LanDrop/uninstall-a/Uninstall.exe"),
            request_path=Path("C:/Temp/LanDrop/uninstall-a/request.json"),
            nonce="ab" * 32,
            expected_request_sha256="cd" * 32,
        )
        with mock.patch("landrop.uninstall_app.schedule_temp_self_cleanup") as cleanup:
            self.assertTrue(api.start_execution()["ok"])
            deadline = time.monotonic() + 2
            while api.get_uninstall_status()["phase"] != "finished":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        outcome = api.get_uninstall_status()["outcome"]
        self.assertTrue(outcome["complete"])
        cleanup.assert_called_once()
        self.assertEqual(service.executions, 1)

    def test_self_cleanup_schedule_failure_is_reported_as_residual(self) -> None:
        service = _Service(Path("C:/test"))
        api = UninstallApi(
            service,  # type: ignore[arg-type]
            current_executable=Path("C:/Temp/LanDrop/uninstall-a/Uninstall.exe"),
            request_path=Path("C:/Temp/LanDrop/uninstall-a/request.json"),
            nonce="ab" * 32,
            expected_request_sha256="cd" * 32,
        )
        with mock.patch(
            "landrop.uninstall_app.schedule_temp_self_cleanup",
            side_effect=OSError("blocked"),
        ):
            self.assertTrue(api.start_execution()["ok"])
            deadline = time.monotonic() + 2
            while api.get_uninstall_status()["phase"] != "finished":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
        outcome = api.get_uninstall_status()["outcome"]
        self.assertFalse(outcome["complete"])
        self.assertTrue(any("自清理" in item for item in outcome["residuals"]))


if __name__ == "__main__":
    unittest.main()
