from __future__ import annotations

from io import BytesIO
from pathlib import Path
import unittest

from landrop.storage import (
    InvalidFilenameError,
    UploadTooLargeError,
    resolve_shared_file,
    sanitize_filename,
    save_upload,
)
from support import temporary_directory


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


if __name__ == "__main__":
    unittest.main()
