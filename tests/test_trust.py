from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from support import temporary_directory

from landrop.trust import CREDENTIAL_TTL, CredentialStore, describe_user_agent


class CredentialStoreTests(unittest.TestCase):
    def test_prepared_credential_is_rolled_back_until_explicit_commit(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            prepared = store.prepare("Test Browser")
            self.assertFalse(store.path.exists())

            with store.persist_prepared(prepared):
                self.assertIsNotNone(store.verify(prepared.credential))
            self.assertEqual(store.list_clients(), [])

            with store.persist_prepared(prepared) as persistence:
                persistence.commit()
            self.assertIsNotNone(store.verify(prepared.credential))

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

    def test_server_expiry_rejects_and_lazily_removes_expired_credentials(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            client, credential = store.issue("Test Browser")
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            record = payload["clients"][0]
            record["expires_at"] = "2020-01-01T00:00:00+00:00"
            store.path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(store.verify(credential))
            self.assertEqual(store.list_clients(), [])
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["clients"], [])
            self.assertEqual(client.client_id, record["client_id"])

    def test_legacy_record_expiry_is_derived_from_creation_time(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            _client, credential = store.issue("Test Browser")
            payload = json.loads(store.path.read_text(encoding="utf-8"))
            record = payload["clients"][0]
            record.pop("expires_at")
            record["created_at"] = "2020-01-01T00:00:00+00:00"
            store.path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertIsNone(store.verify(credential))
            self.assertEqual(store.list_clients(), [])

    def test_new_credentials_have_server_expiry(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            _client, _credential = store.issue("Test Browser")
            record = json.loads(store.path.read_text(encoding="utf-8"))["clients"][0]
            created = datetime.fromisoformat(record["created_at"])
            expires = datetime.fromisoformat(record["expires_at"])

            self.assertEqual(expires - created, CREDENTIAL_TTL)

    def test_structures_device_browser_and_custom_name(self) -> None:
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/140.0.0.0 Safari/537.36"
        )
        details = describe_user_agent(user_agent)
        self.assertEqual(details["device_type"], "电脑")
        self.assertEqual(details["operating_system"], "Windows 10/11")
        self.assertEqual(details["browser"], "Chromium 系浏览器（名称未公开） 140")

        client_hints = {
            "brands": '"Chromium";v="140", "Google Chrome";v="140"'
        }
        hinted = describe_user_agent(user_agent, client_hints)
        self.assertEqual(hinted["browser"], "Google Chrome 140")
        self.assertEqual(hinted["browser_engine"], "Blink / Chromium")

        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            client, _credential = store.issue(
                user_agent,
                "  办公电脑  ",
                client_hints,
            )
            self.assertEqual(client.device_name, "办公电脑")
            self.assertEqual(client.label, "办公电脑")
            self.assertEqual(store.list_clients()[0].browser, "Google Chrome 140")

    def test_hides_legacy_android_model_behind_generic_label(self) -> None:
        with temporary_directory() as temporary:
            store = CredentialStore(Path(temporary))
            store.path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "clients": [
                            {
                                "client_id": "legacy-client",
                                "label": "23116PN5BC",
                                "created_at": "2026-09-16T00:00:00+00:00",
                                "device_type": "手机",
                                "operating_system": "Android 16",
                                "browser": "Google Chrome 152",
                                "token_hash": "unused",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            client = store.list_clients()[0]
            self.assertEqual(client.label, "Android 手机")
            self.assertEqual(client.device_model, "23116PN5BC")
            self.assertEqual(client.browser, "Chromium 系浏览器（名称未公开） 152")
            self.assertEqual(client.browser_engine, "Blink / Chromium")

    def test_recognizes_browsers_that_publish_their_name(self) -> None:
        cases = (
            ("Mozilla/5.0 Chrome/140.0 Safari/537.36 Edg/140.0", "Microsoft Edge 140"),
            ("Mozilla/5.0 Firefox/141.0", "Mozilla Firefox 141"),
            ("Mozilla/5.0 Chrome/120.0 Safari/537.36 Via/6.2", "Via 6"),
        )
        for user_agent, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(describe_user_agent(user_agent)["browser"], expected)


if __name__ == "__main__":
    unittest.main()
