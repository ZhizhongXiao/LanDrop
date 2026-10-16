from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import TypeGuard
from unittest.mock import call, patch

from landrop.coordinator import ActionResult
from landrop.gui import DesktopApi
from landrop.service import ServiceController, ServiceSnapshot
from landrop.settings import AppSettings, SettingsStore
from landrop.trust import CredentialStore
from tests.support import temporary_directory


class _ControllerStub:
    def snapshot(self) -> ServiceSnapshot:
        raise NotImplementedError

    def available_interfaces(self) -> list[dict[str, object]]:
        raise NotImplementedError

    def refresh_diagnostics(self) -> ServiceSnapshot:
        raise NotImplementedError


class _SnapshotController(_ControllerStub):
    def __init__(self, snapshot: ServiceSnapshot) -> None:
        self.current_snapshot = snapshot

    def snapshot(self) -> ServiceSnapshot:
        return self.current_snapshot


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _object_dict(value: object) -> dict[str, object]:
    if not _is_object_dict(value):
        raise TypeError(f"expected string-keyed dict, got {type(value).__name__}")
    return value


def _is_object_dict_list(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list)


def _object_dicts(value: object) -> list[dict[str, object]]:
    if not _is_object_dict_list(value):
        raise TypeError("expected list of string-keyed dicts")
    return value


class DesktopTrustApiTests(unittest.TestCase):
    def test_get_state_counts_active_authorizations_across_issue_and_revoke(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary) / "data")
            api = DesktopApi(ServiceController(store), store, object())

            def count() -> object:
                state = _object_dict(api.get_state()["state"])
                trusted_count = state["trusted_client_count"]
                listed = api.list_trusted_clients()
                self.assertTrue(listed["ok"])
                self.assertEqual(
                    trusted_count,
                    len(_object_dicts(listed["clients"])),
                )
                return trusted_count

            self.assertEqual(count(), 0)
            clients = []
            for expected_count in (1, 2, 3, 3):
                client, _credential = store.issue("Same Browser User-Agent")
                clients.append(client)
                self.assertEqual(count(), expected_count)

            active_ids = {client.client_id for client in store.list_clients()}
            self.assertNotIn(clients[0].client_id, active_ids)
            self.assertEqual(
                active_ids,
                {client.client_id for client in clients[1:]},
            )

            revoked = api.revoke_trusted_client(clients[1].client_id)
            self.assertTrue(revoked["revoked"])
            self.assertEqual(count(), 2)

            all_revoked = api.revoke_all_trusted_clients()
            self.assertEqual(all_revoked["count"], 2)
            self.assertEqual(count(), 0)

    def test_get_state_counts_separate_records_for_the_same_user_agent(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary) / "data")
            api = DesktopApi(ServiceController(store), store, object())
            user_agent = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"

            first, _first_credential = store.issue(user_agent)
            second, _second_credential = store.issue(user_agent)

            self.assertNotEqual(first.client_id, second.client_id)
            state = _object_dict(api.get_state()["state"])
            self.assertEqual(state["trusted_client_count"], 2)

    def test_get_state_excludes_expired_trusted_records(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary) / "data")
            store.issue("Expired Browser")
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            payload["clients"][0]["expires_at"] = "2020-01-01T00:00:00+00:00"
            store.path.write_text(json.dumps(payload), encoding="utf-8")
            api = DesktopApi(ServiceController(store), store, object())

            state = _object_dict(api.get_state()["state"])

            self.assertEqual(state["trusted_client_count"], 0)
            self.assertEqual(store.list_clients(), [])

    def test_get_state_uses_none_when_trusted_store_cannot_be_read(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary) / "data")
            store.issue("Unreadable Browser")
            store.path.write_text("{invalid json", encoding="utf-8")
            api = DesktopApi(ServiceController(store), store, object())

            state = _object_dict(api.get_state()["state"])

            self.assertIsNone(state["trusted_client_count"])

    def test_open_transfer_directories_uses_saved_settings_when_stopped(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            shared = root / "saved-shared"
            received = root / "saved-received"
            shared.mkdir()
            received.mkdir()
            store = CredentialStore(root / "data")
            settings = SettingsStore(root / "data", downloads=root / "Downloads")
            settings.save(AppSettings(shared, received, 100))
            snapshot = ServiceSnapshot(
                running=False,
                phase="stopped",
                message="服务未启动",
                shared_directory=str(root / "stale-shared"),
                receive_directory=str(root / "stale-received"),
                max_upload_mb=100,
            )
            api = DesktopApi(_SnapshotController(snapshot), store, object(), settings)

            with patch("landrop.gui.os.startfile", create=True) as startfile:
                shared_result = api.open_transfer_directory("shared")
                received_result = api.open_transfer_directory("received")

            self.assertTrue(shared_result["ok"])
            self.assertTrue(received_result["ok"])
            self.assertEqual(
                startfile.call_args_list,
                [call(str(shared)), call(str(received))],
            )

    def test_open_transfer_directories_uses_running_snapshot_paths(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            saved_shared = root / "saved-shared"
            saved_received = root / "saved-received"
            frozen_shared = root / "frozen-shared"
            frozen_received = root / "frozen-received"
            for directory in (saved_shared, saved_received, frozen_shared, frozen_received):
                directory.mkdir()
            store = CredentialStore(root / "data")
            settings = SettingsStore(root / "data", downloads=root / "Downloads")
            settings.save(AppSettings(saved_shared, saved_received, 100))
            snapshot = ServiceSnapshot(
                running=True,
                phase="running",
                message="服务正在运行",
                shared_directory=str(frozen_shared),
                receive_directory=str(frozen_received),
                max_upload_mb=100,
            )
            api = DesktopApi(_SnapshotController(snapshot), store, object(), settings)

            with patch("landrop.gui.os.startfile", create=True) as startfile:
                shared_result = api.open_transfer_directory("shared")
                received_result = api.open_transfer_directory("received")

            self.assertTrue(shared_result["ok"])
            self.assertTrue(received_result["ok"])
            self.assertEqual(
                startfile.call_args_list,
                [call(str(frozen_shared)), call(str(frozen_received))],
            )

    def test_open_transfer_directory_rejects_invalid_target_and_missing_path(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            missing_shared = root / "missing-shared"
            received = root / "received"
            received.mkdir()
            store = CredentialStore(root / "data")
            settings = SettingsStore(root / "data", downloads=root / "Downloads")
            settings.save(AppSettings(missing_shared, received, 100))
            snapshot = ServiceSnapshot(
                running=False,
                phase="stopped",
                message="服务未启动",
                shared_directory="",
                receive_directory="",
                max_upload_mb=100,
            )
            api = DesktopApi(_SnapshotController(snapshot), store, object(), settings)

            with patch("landrop.gui.os.startfile", create=True) as startfile:
                invalid = api.open_transfer_directory("command")
                missing = api.open_transfer_directory("shared")

            self.assertFalse(invalid["ok"])
            self.assertFalse(missing["ok"])
            self.assertIn("不存在或不可访问", str(missing["error"]))
            startfile.assert_not_called()

    def test_successful_start_persists_only_validated_user_settings(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            shared = root / "shared"
            received = root / "received"
            shared.mkdir()
            received.mkdir()
            credential_store = CredentialStore(root / "data")
            settings_store = SettingsStore(root / "data", downloads=root / "Downloads")
            controller = ServiceController(credential_store)
            api = DesktopApi(controller, credential_store, object(), settings_store)

            class Coordinator:
                def start_from_gui(
                    self,
                    shared_directory: str,
                    receive_directory: str,
                    max_upload_mb: int,
                    interface_selector: str | None,
                ) -> ServiceSnapshot:
                    del interface_selector
                    return ServiceSnapshot(
                        running=True,
                        phase="running",
                        message="服务正在运行",
                        shared_directory=str(Path(shared_directory).resolve()),
                        receive_directory=str(Path(receive_directory).resolve()),
                        max_upload_mb=max_upload_mb,
                    )

                def stop_from_ui(self) -> ActionResult:
                    raise NotImplementedError

                def reset_from_tray(self) -> ActionResult:
                    raise NotImplementedError

            api.attach_coordinator(Coordinator())
            result = api.start_service(
                {
                    "shared_directory": str(shared),
                    "receive_directory": str(received),
                    "max_upload_mb": 2048,
                    "interface_selector": "",
                }
            )

            self.assertTrue(result["ok"])
            persisted = settings_store.load()
            self.assertEqual(persisted.shared_directory, shared.resolve())
            self.assertEqual(persisted.receive_directory, received.resolve())
            self.assertEqual(persisted.max_upload_mb, 2048)
            raw = settings_store.path.read_text(encoding="utf-8")
            self.assertNotIn("session_id", raw)
            self.assertNotIn("pairing_code", raw)

    def test_list_and_revoke_trusted_clients(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            first, _credential = store.issue("Test Browser")
            second, _credential = store.issue("Phone Browser")
            controller = ServiceController(store)
            api = DesktopApi(controller, store, object())

            listed = api.list_trusted_clients()
            self.assertTrue(listed["ok"])
            clients = _object_dicts(listed["clients"])
            self.assertEqual(
                {item["client_id"] for item in clients},
                {first.client_id, second.client_id},
            )
            self.assertTrue(
                all(
                    "browser" in item
                    and "operating_system" in item
                    and "device_model" in item
                    and "browser_engine" in item
                    for item in clients
                )
            )
            self.assertNotIn("credential", str(listed))

            revoked = api.revoke_trusted_client(first.client_id)
            self.assertTrue(revoked["revoked"])
            self.assertEqual(len(_object_dicts(revoked["clients"])), 1)

            all_revoked = api.revoke_all_trusted_clients()
            self.assertEqual(all_revoked["count"], 1)
            self.assertEqual(store.list_clients(), [])

    def test_settings_entry_is_whitelisted(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            api = DesktopApi(ServiceController(store), store, object())

            rejected = api.open_windows_settings("command")
            self.assertFalse(rejected["ok"])

            with patch("landrop.gui.os.startfile", create=True) as startfile:
                accepted = api.open_windows_settings("network")
            self.assertTrue(accepted["ok"])
            startfile.assert_called_once_with("ms-settings:network-status")

    def test_refresh_diagnostics_returns_controller_snapshot(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))

            class Controller(_ControllerStub):
                def refresh_diagnostics(self) -> ServiceSnapshot:
                    return ServiceSnapshot(
                        running=False,
                        phase="stopped",
                        message="服务未启动",
                        shared_directory="",
                        receive_directory="",
                        max_upload_mb=1000,
                        diagnostics={"status": "checking"},
                    )

            api = DesktopApi(Controller(), store, object())
            result = api.refresh_diagnostics()

            self.assertTrue(result["ok"])
            state = _object_dict(result["state"])
            diagnostics = _object_dict(state["diagnostics"])
            self.assertEqual(diagnostics["status"], "checking")

    def test_lists_interfaces_from_controller(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))

            class Controller(_ControllerStub):
                def available_interfaces(self) -> list[dict[str, object]]:
                    return [
                        {
                            "alias": "Ethernet",
                            "interface_index": 7,
                            "address": "192.168.50.1",
                            "category": "Private",
                            "role": "lan_candidate",
                        }
                    ]

            api = DesktopApi(Controller(), store, object())
            result = api.list_interfaces()

            self.assertTrue(result["ok"])
            interfaces = _object_dicts(result["interfaces"])
            self.assertEqual(interfaces[0]["address"], "192.168.50.1")

    def test_diagnostic_report_omits_credentials_and_user_directories(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = CredentialStore(root / "data")

            class Controller(_ControllerStub):
                def snapshot(self) -> ServiceSnapshot:
                    return ServiceSnapshot(
                        running=True,
                        phase="running",
                        message="服务正在运行",
                        shared_directory=str(root / "private" / "shared"),
                        receive_directory=str(root / "private" / "received"),
                        max_upload_mb=1000,
                        pairing_code="87654321",
                        interface="WLAN",
                        network_name="Home",
                        interface_index=4,
                        bound_ipv4="192.168.1.10",
                        network_category="Private",
                        diagnostics={
                            "status": "ready",
                            "message": "ok",
                            "network": {"status": "ready", "interfaces": []},
                            "firewall": {
                                "status": "ready",
                                "level": "ok",
                                "evidence": [
                                    {
                                        "program": str(root / "secret" / "LanDrop.exe"),
                                        "name": "LanDrop",
                                    }
                                ],
                            },
                        },
                    )

            api = DesktopApi(Controller(), store, object())
            result = api.get_diagnostic_report()

            self.assertTrue(result["ok"])
            report = str(result["text"])
            self.assertIn("LanDrop.exe", report)
            self.assertNotIn("87654321", report)
            self.assertNotIn(str(root / "private"), report)
            self.assertNotIn(str(root / "secret"), report)


if __name__ == "__main__":
    unittest.main()
