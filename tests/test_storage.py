from __future__ import annotations

import os
import time
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support import temporary_directory

from landrop.storage import (
    MINIMUM_FREE_SPACE,
    InsufficientSpaceError,
    InvalidFilenameError,
    UploadSpaceBudget,
    UploadTooLargeError,
    cleanup_orphaned_upload_parts,
    resolve_shared_file,
    sanitize_filename,
    save_upload,
)


class FilenameTests(unittest.TestCase):
    def test_preserves_unicode_and_removes_client_path(self) -> None:
        self.assertEqual(sanitize_filename(r"C:\fakepath\中文 文件.txt"), "中文 文件.txt")

    def test_replaces_windows_reserved_characters(self) -> None:
        self.assertEqual(sanitize_filename('a<b>:c?.txt'), "a_b__c_.txt")

    def test_prefixes_reserved_device_name(self) -> None:
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")

    def test_rejects_empty_filename(self) -> None:
        with self.assertRaises(InvalidFilenameError):
            sanitize_filename(" ... ")


class UploadTests(unittest.TestCase):
    def test_space_budget_reserves_concurrent_uploads_and_releases_on_exit(self) -> None:
        budget = UploadSpaceBudget()
        directory = Path(".")
        upload_bytes = 8 * 1024 * 1024
        with (
            patch(
                "landrop.storage.shutil.disk_usage",
                return_value=SimpleNamespace(free=upload_bytes + MINIMUM_FREE_SPACE),
            ),
            budget.reserve(directory, upload_bytes),
            self.assertRaises(InsufficientSpaceError),
            budget.reserve(directory, upload_bytes),
        ):
            self.fail("overcommitted upload reservation was accepted")

        with patch(
            "landrop.storage.shutil.disk_usage",
            return_value=SimpleNamespace(free=upload_bytes + MINIMUM_FREE_SPACE),
        ), budget.reserve(directory, upload_bytes):
            pass

    def test_space_budget_releases_reservation_after_exception(self) -> None:
        budget = UploadSpaceBudget()
        directory = Path(".")
        upload_bytes = 8 * 1024 * 1024
        with (
            patch(
                "landrop.storage.shutil.disk_usage",
                return_value=SimpleNamespace(free=upload_bytes + MINIMUM_FREE_SPACE),
            ),
            self.assertRaisesRegex(RuntimeError, "simulated upload failure"),
            budget.reserve(directory, upload_bytes),
        ):
            raise RuntimeError("simulated upload failure")

        with patch(
            "landrop.storage.shutil.disk_usage",
            return_value=SimpleNamespace(free=upload_bytes + MINIMUM_FREE_SPACE),
        ), budget.reserve(directory, upload_bytes):
            pass

    def test_streams_and_renames_duplicate(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            first = save_upload(BytesIO(b"first"), "same.txt", root, 100)
            second = save_upload(BytesIO(b"second"), "same.txt", root, 100)
            self.assertEqual(first.filename, "same.txt")
            self.assertFalse(first.renamed)
            self.assertEqual(second.filename, "same (1).txt")
            self.assertTrue(second.renamed)
            self.assertEqual((root / first.filename).read_bytes(), b"first")
            self.assertEqual((root / second.filename).read_bytes(), b"second")

    def test_oversize_upload_removes_part_file(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            with self.assertRaises(UploadTooLargeError):
                save_upload(BytesIO(b"too large"), "large.bin", root, 3)
            self.assertEqual(list(root.iterdir()), [])

    def test_shared_path_cannot_escape(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            with self.assertRaises(InvalidFilenameError):
                resolve_shared_file(root, "../secret.txt")

    def test_orphan_cleanup_only_removes_exact_direct_child_pattern(self) -> None:
        with temporary_directory() as temporary:
            root = Path(temporary)
            orphan = root / f".movie.bin.{'a' * 24}.part"
            fresh_matching = root / f".active.bin.{'c' * 24}.part"
            arbitrary = root / "user-download.part"
            wrong_token = root / ".movie.bin.not-a-token.part"
            nested = root / "nested"
            nested.mkdir()
            nested_orphan = nested / f".nested.bin.{'b' * 24}.part"
            for path in (orphan, fresh_matching, arbitrary, wrong_token, nested_orphan):
                path.write_bytes(b"partial")
            old = time.time() - 25 * 60 * 60
            os.utime(orphan, (old, old))

            result = cleanup_orphaned_upload_parts(root)

            self.assertEqual(result.removed, 1)
            self.assertEqual(result.errors, ())
            self.assertFalse(orphan.exists())
            self.assertTrue(fresh_matching.exists())
            self.assertTrue(arbitrary.exists())
            self.assertTrue(wrong_token.exists())
            self.assertTrue(nested_orphan.exists())


if __name__ == "__main__":
    unittest.main()
