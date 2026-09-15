from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re
import unittest
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

from landrop.trust import CredentialStore
from landrop.web import WebConfig, create_application
from support import temporary_directory


class BottleApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_directory()
        root = Path(self.temporary.name)
        self.shared = root / "shared"
        self.received = root / "received"
        self.shared.mkdir()
        self.received.mkdir()
        (self.shared / "hello.txt").write_text("hello", encoding="utf-8")
        self.store = CredentialStore(root / "data")
        self.app, self.code = create_application(
            WebConfig(self.shared, self.received, 1024, self.store)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pair_download_upload_and_unpair(self) -> None:
        status, headers, body = wsgi_request(self.app, "/")
        self.assertTrue(status.startswith("200"))
        self.assertIn("连接 LanDrop", body.decode())

        form = urlencode({"code": self.code}).encode()
        status, headers, _body = wsgi_request(
            self.app,
            "/pair",
            method="POST",
            body=form,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("303"), status)
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        status, _headers, body = wsgi_request(self.app, "/", cookie=cookie)
        self.assertTrue(status.startswith("200"))
        decoded = body.decode()
        self.assertIn("hello.txt", decoded)
        match = re.search(r'name="csrf" value="([^"]+)"', decoded)
        self.assertIsNotNone(match)
        assert match is not None
        csrf = match.group(1)

        status, headers, body = wsgi_request(
            self.app, "/download/hello.txt", cookie=cookie
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"hello")
        self.assertIn("attachment", headers["Content-Disposition"])

        upload_body, content_type = multipart_upload(csrf, "中文.txt", b"uploaded")
        status, _headers, body = wsgi_request(
            self.app,
            "/upload",
            method="POST",
            body=upload_body,
            content_type=content_type,
            cookie=cookie,
        )
        self.assertTrue(status.startswith("201"), body.decode())
        self.assertEqual((self.received / "中文.txt").read_bytes(), b"uploaded")

        unpair_form = urlencode({"csrf": csrf}).encode()
        status, _headers, _body = wsgi_request(
            self.app,
            "/unpair",
            method="POST",
            body=unpair_form,
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        )
        self.assertTrue(status.startswith("303"), status)
        self.assertEqual(self.store.list_clients(), [])

    def test_upload_requires_authentication(self) -> None:
        status, _headers, _body = wsgi_request(self.app, "/upload", method="POST")
        self.assertTrue(status.startswith("401"))

    def test_rejects_wrong_pairing_code(self) -> None:
        form = urlencode({"code": "00000000"}).encode()
        status, _headers, _body = wsgi_request(
            self.app,
            "/pair",
            method="POST",
            body=form,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("403"))


def wsgi_request(
    app: object,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    content_type: str = "",
    cookie: str = "",
) -> tuple[str, dict[str, str], bytes]:
    environ: dict[str, object] = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    environ["wsgi.input"] = BytesIO(body)
    environ["CONTENT_LENGTH"] = str(len(body))
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    captured: dict[str, object] = {}

    def start_response(status: str, headers: list[tuple[str, str]], _exc_info=None) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    iterable = app(environ, start_response)
    try:
        response_body = b"".join(iterable)
    finally:
        close = getattr(iterable, "close", None)
        if close is not None:
            close()
    return str(captured["status"]), captured["headers"], response_body


def multipart_upload(csrf: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "----LanDropUnitTestBoundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="csrf"\r\n\r\n'
        f"{csrf}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


if __name__ == "__main__":
    unittest.main()
