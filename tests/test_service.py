from __future__ import annotations

from pathlib import Path
import threading
import unittest

from landrop.service import ServiceController, ServiceError
from landrop.trust import CredentialStore
from tests.support import temporary_directory


class _Interface:
    address = "192.168.50.10"
    alias = "Test WLAN"
    category = "Private"


class _FakeServer:
    def __init__(self, address: str, port: int, application: object) -> None:
        self.address = address
        self.port = port
        self.application = application
        self.closed = threading.Event()

    def serve_forever(self) -> None:
        self.closed.wait(5)

    def close(self) -> None:
        self.closed.set()


class ServiceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_directory()
        self.root = Path(self.temporary.name)
        self.shared = self.root / "shared"
        self.received = self.root / "received"
        self.shared.mkdir()
        self.received.mkdir()
        self.servers: list[_FakeServer] = []

        def server_factory(address: str, port: int, application: object) -> _FakeServer:
            server = _FakeServer(address, port, application)
            self.servers.append(server)
            return server

        self.controller = ServiceController(
            CredentialStore(self.root / "data"),
            discover=lambda: [_Interface()],
            select=lambda interfaces, selector: interfaces[0],
            application_factory=lambda config: (object(), "12345678"),
            server_factory=server_factory,
        )

    def tearDown(self) -> None:
        self.controller.stop()
        self.temporary.cleanup()

    def test_start_and_stop_expose_expected_state(self) -> None:
        running = self.controller.start(self.shared, self.received, 32)

        self.assertTrue(running.running)
        self.assertEqual(running.lan_url, "http://192.168.50.10:8000/")
        self.assertEqual(running.local_url, "http://127.0.0.1:8000/")
        self.assertEqual(running.pairing_code, "12345678")
        self.assertEqual(running.max_upload_mib, 32)

        stopped = self.controller.stop()
        self.assertFalse(stopped.running)
        self.assertEqual(stopped.phase, "stopped")
        self.assertEqual(stopped.lan_url, "")
        self.assertTrue(self.servers[0].closed.is_set())

    def test_rejects_second_start(self) -> None:
        self.controller.start(self.shared, self.received)
        with self.assertRaisesRegex(ServiceError, "已经在运行"):
            self.controller.start(self.shared, self.received)

    def test_rejects_missing_directory_and_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ServiceError, "请选择共享目录"):
            self.controller.start("", self.received)
        with self.assertRaisesRegex(ServiceError, "共享目录不存在"):
            self.controller.start(self.root / "missing", self.received)
        with self.assertRaisesRegex(ServiceError, "上传上限"):
            self.controller.start(self.shared, self.received, 0)


if __name__ == "__main__":
    unittest.main()
