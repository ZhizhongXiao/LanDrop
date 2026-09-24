from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch
import unittest

from landrop.payload_manifest import (
    PayloadFile,
    PayloadManifestError,
    build_payload_manifest,
    read_payload_manifest,
    verify_payload,
    write_payload_manifest,
)
from tests.support import temporary_directory


class PayloadManifestTests(unittest.TestCase):
    def _payload(self, root: Path) -> Path:
        payload = root / "payload"
        (payload / "_internal" / "资源").mkdir(parents=True)
        (payload / "LanDrop.exe").write_bytes(b"executable")
        (payload / "_internal" / "资源" / "ui.txt").write_text(
            "界面资源", encoding="utf-8"
        )
        return payload

    def test_build_write_read_and_verify_exact_payload(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            payload = self._payload(root)
            manifest = build_payload_manifest(
                payload,
                version="0.8.0",
                build_id="build-20260924",
            )
            manifest_path = root / "payload-manifest.json"

            write_payload_manifest(manifest_path, manifest)
            loaded = read_payload_manifest(manifest_path)
            verify_payload(payload, loaded)

            self.assertEqual(loaded, manifest)
            self.assertEqual(
                [entry.relative_path for entry in loaded.files],
                ["_internal/资源/ui.txt", "LanDrop.exe"],
            )
            self.assertFalse(list(root.glob(".payload-manifest.*.tmp")))

    def test_missing_extra_size_and_hash_changes_are_rejected(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            payload = self._payload(root)
            manifest = build_payload_manifest(payload, version="0.8.0", build_id="b")

            (payload / "LanDrop.exe").unlink()
            with self.assertRaisesRegex(PayloadManifestError, "缺少"):
                verify_payload(payload, manifest)

            (payload / "LanDrop.exe").write_bytes(b"executable")
            (payload / "extra.bin").write_bytes(b"extra")
            with self.assertRaisesRegex(PayloadManifestError, "多出"):
                verify_payload(payload, manifest)

            (payload / "extra.bin").unlink()
            (payload / "LanDrop.exe").write_bytes(b"same-size!")
            with self.assertRaisesRegex(PayloadManifestError, "大小|SHA-256"):
                verify_payload(payload, manifest)

    def test_manifest_rejects_path_escape_duplicate_and_unknown_fields(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            payload = self._payload(root)
            manifest = build_payload_manifest(payload, version="0.8.0", build_id="b")
            base = manifest.to_json()
            manifest_path = root / "payload-manifest.json"

            cases: list[dict[str, object]] = []
            escaped = json.loads(json.dumps(base))
            escaped["files"][0]["path"] = "../outside"  # type: ignore[index]
            cases.append(escaped)
            duplicate = json.loads(json.dumps(base))
            duplicate["files"].append(dict(duplicate["files"][0]))  # type: ignore[union-attr,index]
            cases.append(duplicate)
            unknown = json.loads(json.dumps(base))
            unknown["files"][0]["mode"] = "executable"  # type: ignore[index]
            cases.append(unknown)

            for data in cases:
                with self.subTest(data=data):
                    manifest_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(PayloadManifestError):
                        read_payload_manifest(manifest_path)

    def test_reparse_file_or_directory_is_rejected(self) -> None:
        with temporary_directory() as temporary:
            payload = self._payload(Path(temporary))
            with patch(
                "landrop.payload_manifest.is_reparse_object",
                side_effect=lambda path: path.name == "资源",
            ):
                with self.assertRaisesRegex(PayloadManifestError, "reparse"):
                    build_payload_manifest(payload, version="0.8.0", build_id="b")

    def test_manifest_identity_and_sorted_entries_are_strict(self) -> None:
        with temporary_directory() as temporary:
            payload = self._payload(Path(temporary))
            manifest = build_payload_manifest(payload, version="0.8.0", build_id="b")
            with self.assertRaises(PayloadManifestError):
                verify_payload(payload, replace(manifest, product_id="Other"))
            with self.assertRaises(PayloadManifestError):
                verify_payload(payload, replace(manifest, files=tuple(reversed(manifest.files))))
            bad_hash = replace(manifest.files[0], sha256="A" * 64)
            with self.assertRaises(PayloadManifestError):
                verify_payload(payload, replace(manifest, files=(bad_hash, *manifest.files[1:])))



if __name__ == "__main__":
    unittest.main()
