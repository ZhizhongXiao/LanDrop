from __future__ import annotations

import json
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
        self.assertRegex(running.pairing_code, r"^\d{8}$")
        self.assertEqual(running.max_upload_mb, 32)

        stopped = self.controller.stop()
        self.assertFalse(stopped.running)
        self.assertEqual(stopped.phase, "stopped")
        self.assertEqual(stopped.lan_url, "")
        self.assertTrue(self.servers[0].closed.is_set())
        log_path = self.root / "data" / "logs" / "sessions.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["stop_reason"], "manual_stop")
        self.assertNotIn("pairing_code", record)

    def test_rejects_second_start(self) -> None:
        self.controller.start(self.shared, self.received)
        with self.assertRaisesRegex(ServiceError, "已经在运行"):
            self.controller.start(self.shared, self.received)

    def test_old_session_cannot_stop_restarted_session(self) -> None:
        self.controller.start(self.shared, self.received)
        old_server = self.servers[-1]
        old_lifecycle = self.controller._lifecycle
        self.controller.stop()
        self.controller.start(self.shared, self.received)

        state = self.controller.stop(
            "deadline_no_active",
            _expected_server=old_server,
            _expected_lifecycle=old_lifecycle,
        )

        self.assertTrue(state.running)
        self.assertIsNot(self.servers[-1], old_server)

    def test_window_close_cancels_active_transfer_and_records_reason(self) -> None:
        self.controller.start(self.shared, self.received)
        assert self.controller._lifecycle is not None
        transfer = self.controller._lifecycle.begin_transfer("download")
        transfer.add_bytes(250_000)

        stopped = self.controller.stop("window_closed")

        self.assertFalse(stopped.running)
        self.assertEqual(stopped.stop_reason, "window_closed")
        self.assertEqual(stopped.statistics["failed_downloads"], 1)
        self.assertEqual(stopped.statistics["failures"], {"window_closed": 1})
        self.assertTrue(self.servers[-1].closed.is_set())

    def test_expected_action_rejects_old_revision_and_old_session(self) -> None:
        initial = self.controller.start(self.shared, self.received)
        reset, current = self.controller.apply_expected_action(
            "reset",
            session_id=initial.session_id,
            deadline_revision=initial.deadline_revision,
        )
        self.assertTrue(reset)
        self.assertEqual(current.deadline_revision, initial.deadline_revision + 1)

        stale, unchanged = self.controller.apply_expected_action(
            "stop",
            session_id=initial.session_id,
            deadline_revision=initial.deadline_revision,
        )
        self.assertFalse(stale)
        self.assertTrue(unchanged.running)

        self.controller.stop()
        restarted = self.controller.start(self.shared, self.received)
        old_session, state = self.controller.apply_expected_action(
            "stop",
            session_id=initial.session_id,
            deadline_revision=current.deadline_revision,
        )
        self.assertFalse(old_session)
        self.assertTrue(state.running)
        self.assertEqual(state.session_id, restarted.session_id)

    def test_expected_reset_and_stop_are_atomic_under_race(self) -> None:
        initial = self.controller.start(self.shared, self.received)
        barrier = threading.Barrier(3)
        results: list[tuple[str, bool]] = []

        def apply(action: str) -> None:
            barrier.wait()
            applied, _state = self.controller.apply_expected_action(
                action,
                session_id=initial.session_id,
                deadline_revision=initial.deadline_revision,
            )
            results.append((action, applied))

        workers = [
            threading.Thread(target=apply, args=("reset",)),
            threading.Thread(target=apply, args=("stop",)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(8)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(applied for _action, applied in results), 1)
        current = self.controller.snapshot()
        if current.running:
            self.assertEqual(current.deadline_revision, initial.deadline_revision + 1)
        else:
            self.assertEqual(current.stop_reason, "manual_stop")

    def test_rejects_missing_directory_and_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ServiceError, "请选择共享目录"):
            self.controller.start("", self.received)
        with self.assertRaisesRegex(ServiceError, "共享目录不存在"):
            self.controller.start(self.root / "missing", self.received)
        with self.assertRaisesRegex(ServiceError, "上传上限"):
            self.controller.start(self.shared, self.received, 0)


if __name__ == "__main__":
    unittest.main()
