from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from typing import TypeGuard
from unittest import mock

from landrop.install_contract import InstallPaths
from landrop.uninstall_app import UninstallApi, main, uninstall_ui_url
from landrop.uninstaller import (
    UninstallHandoff,
    UninstallOutcome,
    UninstallSelection,
)


class _Service:
    def __init__(self, root: Path) -> None:
        self.paths = InstallPaths(
            root / "local",
            root / "roaming",
            root / "desktop",
            root / "temp",
        )
        self.executions = 0

    def prepare_handoff(
        self,
        *,
        source_executable: Path,
        selection: UninstallSelection,
        original_pid: int,
    ) -> UninstallHandoff:
        del source_executable, selection, original_pid
        raise NotImplementedError

    def execute_handoff(
        self,
        *,
        current_executable: Path,
        request_path: Path,
        nonce: str,
        expected_request_sha256: str,
    ) -> UninstallOutcome:
        del current_executable, request_path, nonce, expected_request_sha256
        self.executions += 1
        return UninstallOutcome(complete=True, message="done")


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _outcome(status: dict[str, object]) -> dict[str, object]:
    value = status.get("outcome")
    if not _is_object_dict(value):
        raise TypeError("uninstall status does not contain an outcome")
    return value


class UninstallAppTests(unittest.TestCase):
    def test_temporary_ui_uses_dedicated_resource_before_webview_context(self) -> None:
        root = Path.cwd().resolve()

        def resolve_resource(relative: str) -> Path:
            return root / relative

        with mock.patch(
            "landrop.uninstall_app.resource_path",
            side_effect=resolve_resource,
        ):
            self.assertEqual(
                uninstall_ui_url(temporary_mode=True),
                (root / "ui" / "uninstall" / "execute.html").as_uri(),
            )
            self.assertEqual(
                uninstall_ui_url(temporary_mode=False),
                (root / "ui" / "uninstall" / "index.html").as_uri(),
            )

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

    def test_temporary_execution_stays_pending_until_process_exit_cleanup(self) -> None:
        service = _Service(Path("C:/test"))
        api = UninstallApi(
            service,
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
        outcome = _outcome(api.get_uninstall_status())
        self.assertFalse(outcome["complete"])
        self.assertTrue(outcome["finalization_pending"])
        self.assertIn("关闭此窗口后", str(outcome["message"]))
        cleanup.assert_called_once()
        self.assertEqual(service.executions, 1)

    def test_self_cleanup_schedule_failure_is_reported_as_residual(self) -> None:
        service = _Service(Path("C:/test"))
        api = UninstallApi(
            service,
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
        outcome = _outcome(api.get_uninstall_status())
        self.assertFalse(outcome["complete"])
        self.assertFalse(outcome["finalization_pending"])
        residuals = outcome["residuals"]
        assert _is_object_list(residuals)
        self.assertTrue(any("自清理" in str(item) for item in residuals))

    def test_temporary_cleanup_schedule_is_idempotent_before_execution(self) -> None:
        service = _Service(Path("C:/test"))
        api = UninstallApi(
            service,
            current_executable=Path("C:/Temp/LanDrop/uninstall-a/Uninstall.exe"),
            request_path=Path("C:/Temp/LanDrop/uninstall-a/request.json"),
            nonce="ab" * 32,
            expected_request_sha256="cd" * 32,
        )
        with mock.patch("landrop.uninstall_app.schedule_temp_self_cleanup") as cleanup:
            api.ensure_temp_cleanup_scheduled()
            api.ensure_temp_cleanup_scheduled()
        cleanup.assert_called_once_with(
            Path("C:/Temp/LanDrop/uninstall-a"),
            service.paths,
        )


if __name__ == "__main__":
    unittest.main()
