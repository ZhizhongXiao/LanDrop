from __future__ import annotations

import json
from pathlib import Path
import unittest

from landrop.settings import AppSettings, SettingsStore
from support import temporary_directory


class SettingsStoreTests(unittest.TestCase):
    def test_defaults_use_app_owned_download_subdirectories(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = SettingsStore(root / "data", downloads=root / "Downloads")

            settings = store.load()

            self.assertEqual(settings.shared_directory, root / "Downloads" / "LanDrop" / "Shared")
            self.assertEqual(settings.receive_directory, root / "Downloads" / "LanDrop" / "Received")
            self.assertEqual(settings.max_upload_mb, 1000)
            self.assertFalse(settings.shared_directory.exists())
            store.ensure_default_directories(settings)
            self.assertTrue(settings.shared_directory.is_dir())
            self.assertTrue(settings.receive_directory.is_dir())

    def test_round_trip_uses_small_versioned_schema(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = SettingsStore(root / "data", downloads=root / "Downloads")
            expected = AppSettings(root / "share", root / "receive", 2048)

            store.save(expected)

            self.assertEqual(store.load(), expected)
            raw = json.loads(store.path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(raw),
                {"version", "shared_directory", "receive_directory", "max_upload_mb"},
            )
            self.assertNotIn("session", store.path.read_text(encoding="utf-8"))

    def test_invalid_json_falls_back_without_overwriting_evidence(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = SettingsStore(root / "data", downloads=root / "Downloads")
            store.data_directory.mkdir(parents=True)
            store.path.write_text("{broken", encoding="utf-8")

            settings = store.load()

            self.assertEqual(settings, store.defaults)
            self.assertIn("已回退到安全默认值", store.last_warning)
            self.assertEqual(store.path.read_text(encoding="utf-8"), "{broken")

    def test_missing_fields_use_defaults_but_transient_fields_are_ignored(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            store = SettingsStore(root / "data", downloads=root / "Downloads")
            store.data_directory.mkdir(parents=True)
            custom = root / "custom-share"
            store.path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "shared_directory": str(custom),
                        "session_id": "must-not-load",
                        "pairing_code": "12345678",
                    }
                ),
                encoding="utf-8",
            )

            settings = store.load()

            self.assertEqual(settings.shared_directory, custom)
            self.assertEqual(settings.receive_directory, store.defaults.receive_directory)
            self.assertEqual(settings.max_upload_mb, 1000)


if __name__ == "__main__":
    unittest.main()
