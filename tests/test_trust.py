from __future__ import annotations

from pathlib import Path
import unittest

from landrop.trust import CredentialStore
from support import temporary_directory


class CredentialStoreTests(unittest.TestCase):
    def test_stores_hash_verifies_and_revokes(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            client, credential = store.issue("Test Browser")
            stored_text = store.path.read_text(encoding="utf-8")

            self.assertNotIn(credential.split(".", 1)[1], stored_text)
            verified = store.verify(credential)
            self.assertIsNotNone(verified)
            assert verified is not None
            self.assertEqual(verified.client_id, client.client_id)
            self.assertTrue(store.revoke(client.client_id))
            self.assertIsNone(store.verify(credential))

    def test_rejects_modified_token(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            _client, credential = store.issue("Test Browser")
            self.assertIsNone(store.verify(credential + "changed"))


if __name__ == "__main__":
    unittest.main()
