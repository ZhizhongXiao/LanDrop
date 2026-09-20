from __future__ import annotations

import base64
import unittest

from landrop.qr_invite import qr_png_data_uri


class QrInviteTests(unittest.TestCase):
    def test_renders_png_locally_as_data_uri(self) -> None:
        token = "sensitive-one-time-token"
        result = qr_png_data_uri(f"http://192.168.50.1:8000/pair/qr#{token}")

        self.assertTrue(result.startswith("data:image/png;base64,"))
        self.assertNotIn(token, result)
        encoded = result.split(",", 1)[1]
        self.assertTrue(base64.b64decode(encoded).startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
