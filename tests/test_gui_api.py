from __future__ import annotations

from pathlib import Path
import unittest

from landrop.gui import DesktopApi
from landrop.service import ServiceController
from landrop.trust import CredentialStore
from tests.support import temporary_directory


class DesktopTrustApiTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
