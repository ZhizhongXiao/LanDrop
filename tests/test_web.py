from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import re
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlencode
from wsgiref.util import setup_testing_defaults

from landrop.lifecycle import SessionLifecycle
from landrop.trust import CredentialStore
from landrop.web import WebConfig, _TrackedIterable, create_application
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
        self.lifecycle = SessionLifecycle()
        self.app, self.code = create_application(
            WebConfig(self.shared, self.received, 1024, self.store, self.lifecycle)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_pair_download_and_upload(self) -> None:
        status, headers, body = wsgi_request(self.app, "/")
        self.assertTrue(status.startswith("200"))
        pairing_page = body.decode()
        self.assertIn("连接 LanDrop", pairing_page)
        self.assertIn('class="pairing-form"', pairing_page)
        self.assertIn('class="pairing-field"', pairing_page)

        form = urlencode({"code": f" {self.code} ", "device_name": "测试手机"}).encode()
        status, headers, _body = wsgi_request(
            self.app,
            "/pair",
            method="POST",
            body=form,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("303"), status)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertNotEqual(self.lifecycle.pairing_code, self.code)
        self.assertEqual(self.lifecycle.snapshot().paired_devices, 1)
        self.assertEqual(self.store.list_clients()[0].device_name, "测试手机")

        status, _headers, body = wsgi_request(self.app, "/", cookie=cookie)
        self.assertTrue(status.startswith("200"))
        decoded = body.decode()
        self.assertIn("hello.txt", decoded)
        match = re.search(r'name="csrf" value="([^"]+)"', decoded)
        self.assertIsNotNone(match)
        assert match is not None
        csrf = match.group(1)
        self.assertIn("/upload/raw", decoded)
        self.assertIn("xhr.upload.addEventListener('progress'", decoded)
        self.assertIn('type="file" name="file" multiple', decoded)
        self.assertIn('class="download-choice"', decoded)
        self.assertIn('id="downloadSelected"', decoded)
        self.assertIn('id="showDownloads"', decoded)
        self.assertIn('id="showUploads"', decoded)
        self.assertIn('id="downloadPanel"', decoded)
        self.assertIn('id="uploadPanel"', decoded)
        self.assertIn("setTransferMode", decoded)
        self.assertNotIn("取消信任此浏览器", decoded)
        self.assertNotIn('action="/unpair"', decoded)
        self.assertIn("runUploadQueue", decoded)
        self.assertNotIn("new Blob", decoded)

        status, headers, body = wsgi_request(
            self.app, "/download/hello.txt", cookie=cookie
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(body, b"hello")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.lifecycle.snapshot().statistics["downloaded_mb"], 0.000005)

        status, headers, body = wsgi_request(
            self.app,
            "/download/hello.txt",
            cookie=cookie,
            range_header="bytes=2-4",
        )
        self.assertTrue(status.startswith("206"), status)
        self.assertEqual(body, b"llo")
        self.assertEqual(headers["Content-Range"], "bytes 2-4/5")

        status, _headers, body = wsgi_request(
            self.app, "/prepare-download/hello.txt", cookie=cookie
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn("下载可能显示为", body.decode())

        status, _headers, body = wsgi_request(
            self.app, "/session-status", cookie=cookie
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn('"phase": "running"', body.decode())

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
        self.assertEqual(self.lifecycle.snapshot().statistics["uploaded_mb"], 0.000008)

        status, _headers, body = wsgi_request(
            self.app,
            "/upload/raw",
            method="POST",
            body=b"raw-data",
            content_type="application/octet-stream",
            cookie=cookie,
            extra_headers={
                "HTTP_X_LANDROP_CSRF": csrf,
                "HTTP_X_LANDROP_FILENAME": quote("流式 文件.txt", safe=""),
            },
        )
        self.assertTrue(status.startswith("201"), body.decode())
        self.assertEqual((self.received / "流式 文件.txt").read_bytes(), b"raw-data")
        self.assertEqual(self.lifecycle.snapshot().statistics["uploaded_mb"], 0.000016)

        status, _headers, _body = wsgi_request(
            self.app,
            "/unpair",
            method="POST",
            body=urlencode({"csrf": csrf}).encode(),
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        )
        self.assertTrue(status.startswith("404"), status)
        self.assertEqual(len(self.store.list_clients()), 1)

    def test_upload_requires_authentication(self) -> None:
        status, _headers, _body = wsgi_request(self.app, "/upload", method="POST")
        self.assertTrue(status.startswith("401"))
        status, _headers, _body = wsgi_request(self.app, "/upload/raw", method="POST")
        self.assertTrue(status.startswith("401"))

    def test_raw_upload_rejects_wrong_csrf_without_creating_files(self) -> None:
        cookie, _csrf = self._trusted_session()

        status, _headers, _body = wsgi_request(
            self.app,
            "/upload/raw",
            method="POST",
            body=b"content",
            content_type="application/octet-stream",
            cookie=cookie,
            extra_headers={
                "HTTP_X_LANDROP_CSRF": "wrong-token",
                "HTTP_X_LANDROP_FILENAME": "blocked.txt",
            },
        )

        self.assertTrue(status.startswith("403"), status)
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.active_transfers, 0)
        self.assertEqual(snapshot.statistics["rejections"], {"csrf": 1})
        self.assertEqual(list(self.received.iterdir()), [])

    def test_raw_upload_rejects_declared_oversize_before_transfer(self) -> None:
        cookie, csrf = self._trusted_session()

        status, _headers, _body = wsgi_request(
            self.app,
            "/upload/raw",
            method="POST",
            body=b"x" * 1025,
            content_type="application/octet-stream",
            cookie=cookie,
            extra_headers={
                "HTTP_X_LANDROP_CSRF": csrf,
                "HTTP_X_LANDROP_FILENAME": "too-large.bin",
            },
        )

        self.assertTrue(status.startswith("413"), status)
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.active_transfers, 0)
        self.assertEqual(snapshot.statistics["rejections"], {"size_limit": 1})
        self.assertEqual(snapshot.statistics["failed_uploads"], 0)
        self.assertEqual(list(self.received.iterdir()), [])

    def test_raw_upload_cleans_partial_file_after_early_disconnect(self) -> None:
        cookie, csrf = self._trusted_session()

        status, _headers, body = wsgi_request(
            self.app,
            "/upload/raw",
            method="POST",
            body=b"short",
            content_length=10,
            content_type="application/octet-stream",
            cookie=cookie,
            extra_headers={
                "HTTP_X_LANDROP_CSRF": csrf,
                "HTTP_X_LANDROP_FILENAME": "interrupted.bin",
            },
        )

        self.assertTrue(status.startswith("400"), body.decode())
        snapshot = self.lifecycle.snapshot()
        self.assertEqual(snapshot.active_transfers, 0)
        self.assertEqual(snapshot.statistics["failed_uploads"], 1)
        self.assertEqual(snapshot.statistics["failed_upload_mb"], 0.000005)
        self.assertEqual(snapshot.statistics["failures"], {"client_disconnect": 1})
        self.assertEqual(list(self.received.iterdir()), [])

    def _trusted_session(self) -> tuple[str, str]:
        _client, credential = self.store.issue("Test Browser")
        cookie = f"landrop_trust={credential}"
        status, _headers, body = wsgi_request(self.app, "/", cookie=cookie)
        self.assertTrue(status.startswith("200"), status)
        match = re.search(r'name="csrf" value="([^"]+)"', body.decode())
        self.assertIsNotNone(match)
        assert match is not None
        return cookie, match.group(1)

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

    def test_qr_landing_clears_fragment_before_posting_in_memory_token(self) -> None:
        status, headers, body = wsgi_request(self.app, "/pair/qr")
        decoded = body.decode()
        self.assertTrue(status.startswith("200"), status)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        read_index = decoded.index("window.location.hash")
        save_index = decoded.index("const qrToken")
        clear_index = decoded.index("history.replaceState")
        post_index = decoded.index("fetch('/pair/qr'")
        self.assertLess(read_index, save_index)
        self.assertLess(save_index, clear_index)
        self.assertLess(clear_index, post_index)

    def test_qr_pairing_sets_trust_and_rotates_qr_and_code_together(self) -> None:
        original = self.lifecycle.snapshot()
        invitation = self.lifecycle.pairing_invitation()
        self.assertIsNotNone(invitation)
        assert invitation is not None
        old_qr = invitation[2]
        form = urlencode({"token": old_qr}).encode()

        status, headers, _body = wsgi_request(
            self.app,
            "/pair/qr",
            method="POST",
            body=form,
            content_type="application/x-www-form-urlencoded",
        )

        self.assertTrue(status.startswith("303"), status)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        current = self.lifecycle.snapshot()
        self.assertEqual(current.pairing_revision, original.pairing_revision + 1)
        self.assertNotEqual(current.pairing_code, original.pairing_code)
        self.assertIsNone(self.lifecycle.prepare_pairing("qr", old_qr))
        self.assertEqual(len(self.store.list_clients()), 1)

    def test_manual_pairing_invalidates_qr_and_qr_pairing_invalidates_code(self) -> None:
        invitation = self.lifecycle.pairing_invitation()
        self.assertIsNotNone(invitation)
        assert invitation is not None
        old_qr = invitation[2]
        old_code = self.lifecycle.pairing_code
        form = urlencode({"code": old_code}).encode()
        status, _headers, _body = wsgi_request(
            self.app,
            "/pair",
            method="POST",
            body=form,
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("303"), status)
        status, _headers, _body = wsgi_request(
            self.app,
            "/pair/qr",
            method="POST",
            body=urlencode({"token": old_qr}).encode(),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("403"), status)

        second_code = self.lifecycle.pairing_code
        second_qr = self.lifecycle.pairing_invitation()[2]  # type: ignore[index]
        status, _headers, _body = wsgi_request(
            self.app,
            "/pair/qr",
            method="POST",
            body=urlencode({"token": second_qr}).encode(),
            content_type="application/x-www-form-urlencoded",
        )
        self.assertTrue(status.startswith("303"), status)
        self.assertIsNone(self.lifecycle.prepare_pairing("code", second_code))

    def test_trusted_client_qr_visit_does_not_consume_invitation(self) -> None:
        _client, credential = self.store.issue("Already trusted")
        cookie = f"landrop_trust={credential}"
        before = self.lifecycle.pairing_invitation()

        for method in ("GET", "POST"):
            status, _headers, _body = wsgi_request(
                self.app,
                "/pair/qr",
                method=method,
                body=b"token=wrong" if method == "POST" else b"",
                content_type="application/x-www-form-urlencoded" if method == "POST" else "",
                cookie=cookie,
            )
            self.assertTrue(status.startswith("303"), status)
        self.assertEqual(self.lifecycle.pairing_invitation(), before)
        self.assertEqual(self.lifecycle.snapshot().paired_devices, 0)

    def test_concurrent_qr_use_allows_exactly_one_new_client(self) -> None:
        invitation = self.lifecycle.pairing_invitation()
        self.assertIsNotNone(invitation)
        assert invitation is not None
        body = urlencode({"token": invitation[2]}).encode()
        barrier = threading.Barrier(3)
        statuses: list[str] = []
        status_lock = threading.Lock()

        def submit() -> None:
            barrier.wait()
            status, _headers, _body = wsgi_request(
                self.app,
                "/pair/qr",
                method="POST",
                body=body,
                content_type="application/x-www-form-urlencoded",
            )
            with status_lock:
                statuses.append(status)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(status.startswith("303") for status in statuses), 1)
        self.assertEqual(sum(status.startswith("403") for status in statuses), 1)
        self.assertEqual(len(self.store.list_clients()), 1)

    def test_failed_final_commit_rolls_back_trust_and_sets_no_cookie(self) -> None:
        code = self.lifecycle.pairing_code
        with patch.object(self.lifecycle, "commit_pairing", return_value=False):
            status, headers, _body = wsgi_request(
                self.app,
                "/pair",
                method="POST",
                body=urlencode({"code": code}).encode(),
                content_type="application/x-www-form-urlencoded",
            )
        self.assertTrue(status.startswith("503"), status)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual(self.store.list_clients(), [])
        self.assertEqual(self.lifecycle.pairing_code, code)

    def test_expired_session_rejects_new_request(self) -> None:
        now = [10.0]
        lifecycle = SessionLifecycle(5, 1, clock=lambda: now[0])
        app, _code = create_application(
            WebConfig(self.shared, self.received, 1024, self.store, lifecycle)
        )
        now[0] = 15.0
        status, _headers, body = wsgi_request(app, "/")
        self.assertTrue(status.startswith("503"))
        self.assertIn("会话已到期", body.decode())
        self.assertEqual(lifecycle.snapshot().statistics["rejected_expired_requests"], 1)

    def test_expected_download_cancellation_ends_iteration_cleanly(self) -> None:
        lifecycle = SessionLifecycle()
        transfer = lifecycle.begin_transfer("download")
        response = _TrackedIterable([b"content"], transfer)

        lifecycle.stop("manual_stop", "manual_stop")

        self.assertEqual(list(response), [])
        statistics = lifecycle.snapshot().statistics
        self.assertEqual(statistics["failed_downloads"], 1)
        self.assertEqual(statistics["failures"], {"manual_stop": 1})

    def test_each_start_groups_its_ranges_as_one_logical_download(self) -> None:
        _client, credential = self.store.issue("Test Browser")
        cookie = f"landrop_trust={credential}"
        status, _headers, body = wsgi_request(
            self.app,
            "/prepare-download/hello.txt",
            cookie=cookie,
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn("/start-download/hello.txt", body.decode())

        status, _headers, ticket_body = wsgi_request(
            self.app,
            "/start-download/hello.txt",
            cookie=cookie,
        )
        self.assertTrue(status.startswith("200"))
        match = re.search(r"download_id=([A-Za-z0-9_-]+)", ticket_body.decode())
        self.assertIsNotNone(match)
        assert match is not None
        query = f"download_id={match.group(1)}"

        for byte_range in ("bytes=0-1", "bytes=2-4"):
            status, _headers, _body = wsgi_request(
                self.app,
                "/download/hello.txt",
                cookie=cookie,
                range_header=byte_range,
                query_string=query,
            )
            self.assertTrue(status.startswith("206"), status)

        statistics = self.lifecycle.snapshot().statistics
        self.assertEqual(statistics["completed_downloads"], 1)
        self.assertEqual(statistics["completed_download_streams"], 2)
        self.assertEqual(statistics["downloaded_mb"], 0.000005)

        status, _headers, ticket_body = wsgi_request(
            self.app,
            "/start-download/hello.txt",
            cookie=cookie,
        )
        second_match = re.search(r"download_id=([A-Za-z0-9_-]+)", ticket_body.decode())
        self.assertIsNotNone(second_match)
        assert second_match is not None
        status, _headers, _body = wsgi_request(
            self.app,
            "/download/hello.txt",
            cookie=cookie,
            query_string=f"download_id={second_match.group(1)}",
        )
        self.assertTrue(status.startswith("200"))
        statistics = self.lifecycle.snapshot().statistics
        self.assertEqual(statistics["completed_downloads"], 2)
        self.assertEqual(statistics["completed_download_streams"], 3)
        self.assertEqual(statistics["downloaded_mb"], 0.00001)

    def test_batch_ticket_preserves_valid_serialization_key(self) -> None:
        _client, credential = self.store.issue("Test Browser")
        cookie = f"landrop_trust={credential}"
        status, _headers, ticket_body = wsgi_request(
            self.app,
            "/start-download/hello.txt",
            cookie=cookie,
            query_string="batch_id=0123456789abcdef",
        )
        self.assertTrue(status.startswith("200"))
        self.assertIn("batch_id=0123456789abcdef", ticket_body.decode())
        ticket = json.loads(ticket_body)
        status, _headers, body = wsgi_request(
            self.app,
            ticket["status_url"],
            cookie=cookie,
        )
        self.assertTrue(status.startswith("200"))
        self.assertEqual(json.loads(body)["status"], "pending")


def wsgi_request(
    app: object,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    content_type: str = "",
    cookie: str = "",
    range_header: str = "",
    query_string: str = "",
    extra_headers: dict[str, str] | None = None,
    content_length: int | None = None,
) -> tuple[str, dict[str, str], bytes]:
    environ: dict[str, object] = {}
    setup_testing_defaults(environ)
    environ["REQUEST_METHOD"] = method
    environ["PATH_INFO"] = path
    environ["QUERY_STRING"] = query_string
    environ["wsgi.input"] = BytesIO(body)
    environ["CONTENT_LENGTH"] = str(len(body) if content_length is None else content_length)
    if content_type:
        environ["CONTENT_TYPE"] = content_type
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    if range_header:
        environ["HTTP_RANGE"] = range_header
    if extra_headers:
        environ.update(extra_headers)
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
