from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import unittest

from landrop.gui import DesktopApi
from landrop.service import ServiceController
from landrop.service import ServiceSnapshot
from landrop.settings import SettingsStore
from landrop.trust import CredentialStore
from tests.support import temporary_directory


class DesktopTrustApiTests(unittest.TestCase):
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
                    _interface_selector: str | None,
                ) -> ServiceSnapshot:
                    return ServiceSnapshot(
                        running=True,
                        phase="running",
                        message="服务正在运行",
                        shared_directory=str(Path(shared_directory).resolve()),
                        receive_directory=str(Path(receive_directory).resolve()),
                        max_upload_mb=max_upload_mb,
                    )

            api.attach_coordinator(Coordinator())  # type: ignore[arg-type]
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
            self.assertEqual(
                {item["client_id"] for item in listed["clients"]},
                {first.client_id, second.client_id},
            )
            self.assertTrue(
                all(
                    "browser" in item
                    and "operating_system" in item
                    and "device_model" in item
                    and "browser_engine" in item
                    for item in listed["clients"]
                )
            )
            self.assertNotIn("credential", str(listed))

            revoked = api.revoke_trusted_client(first.client_id)
            self.assertTrue(revoked["revoked"])
            self.assertEqual(len(revoked["clients"]), 1)

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

            class Controller:
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
            self.assertEqual(result["state"]["diagnostics"]["status"], "checking")

    def test_lists_interfaces_from_controller(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))

            class Controller:
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
            self.assertEqual(result["interfaces"][0]["address"], "192.168.50.1")

    def test_diagnostic_report_omits_credentials_and_user_directories(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = CredentialStore(root / "data")

            class Controller:
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
