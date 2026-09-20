from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from landrop.network import EndpointObservation
from landrop.service import ServiceController, ServiceError
from landrop.trust import CredentialStore
from tests.support import temporary_directory


class _Interface:
    interface_index = 12
    address = "192.168.50.10"
    alias = "Test WLAN"
    category = "Private"
    connectivity = "Internet"
    has_gateway = True
    description = "Test adapter"


class _StableEndpointChecker:
    def observe(self, *, include_category: bool = False) -> EndpointObservation:
        return EndpointObservation(True, category="Private" if include_category else None)


class _SequenceEndpointChecker:
    def __init__(self, observations: list[EndpointObservation]) -> None:
        self._observations = list(observations)
        self._lock = threading.Lock()

    def observe(self, *, include_category: bool = False) -> EndpointObservation:
        with self._lock:
            if len(self._observations) > 1:
                return self._observations.pop(0)
            return self._observations[0]


def _ready_diagnostics(_port: int, _program: str) -> dict[str, object]:
    return {
        "status": "ready",
        "message": "diagnostics ready",
        "network": {"status": "ready", "adapters": []},
        "firewall": {
            "status": "ready",
            "level": "ok",
            "message": "firewall ready",
            "evidence": [],
        },
    }


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
            endpoint_checker_factory=lambda _baseline: _StableEndpointChecker(),
            diagnostics_factory=_ready_diagnostics,
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
        self.assertEqual(running.interface_index, 12)
        self.assertEqual(running.bound_ipv4, "192.168.50.10")
        self.assertEqual(running.network_name, "Test adapter")
        self.assertEqual(running.endpoint_status, "healthy")

        stopped = self.controller.stop()
        self.assertFalse(stopped.running)
        self.assertEqual(stopped.phase, "stopped")
        self.assertEqual(stopped.lan_url, "")
        self.assertEqual(stopped.network_name, "Test adapter")
        self.assertTrue(self.servers[0].closed.is_set())
        log_path = self.root / "data" / "logs" / "sessions.jsonl"
        record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["stop_reason"], "manual_stop")
        self.assertNotIn("pairing_code", record)

    def test_qr_invitation_is_local_only_cached_and_cleared_on_stop(self) -> None:
        self.controller.start(self.shared, self.received, 32)
        lifecycle = self.controller._lifecycle
        self.assertIsNotNone(lifecycle)
        assert lifecycle is not None
        secret = lifecycle.pairing_invitation()[2]  # type: ignore[index]
        captured: list[str] = []

        def render(url: str) -> str:
            captured.append(url)
            return "data:image/png;base64,test"

        with patch("landrop.service.qr_png_data_uri", side_effect=render):
            first = self.controller.pairing_qr_data_uri()
            second = self.controller.pairing_qr_data_uri()

        self.assertEqual(first, "data:image/png;base64,test")
        self.assertEqual(second, first)
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0],
            f"http://192.168.50.10:8000/pair/qr#{secret}",
        )
        self.assertNotIn(secret, str(self.controller.snapshot().to_dict()))

        self.controller.stop()
        self.assertEqual(self.controller.pairing_qr_data_uri(), "")

    def test_available_interfaces_returns_fresh_diagnostic_view(self) -> None:
        interfaces = self.controller.available_interfaces()

        self.assertEqual(len(interfaces), 1)
        self.assertEqual(interfaces[0]["interface_index"], 12)
        self.assertEqual(interfaces[0]["address"], "192.168.50.10")
        self.assertEqual(interfaces[0]["role"], "lan_candidate")

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
        with self.assertRaisesRegex(ServiceError, "请选择下载来源目录"):
            self.controller.start("", self.received)
        with self.assertRaisesRegex(ServiceError, "下载来源目录不存在"):
            self.controller.start(self.root / "missing", self.received)
        with self.assertRaisesRegex(ServiceError, "上传上限"):
            self.controller.start(self.shared, self.received, 0)

    def test_transient_endpoint_failure_is_confirmed_and_service_continues(self) -> None:
        checker = _SequenceEndpointChecker(
            [
                EndpointObservation(False, category="Private"),
                EndpointObservation(True, category="Private"),
            ]
        )
        self.controller._endpoint_checker_factory = lambda _baseline: checker
        self.controller._endpoint_check_interval = 0.1
        self.controller._endpoint_confirmation_delay = 0.05
        self.controller._category_check_interval = 10

        self.controller.start(self.shared, self.received)
        time.sleep(0.25)

        state = self.controller.snapshot()
        self.assertTrue(state.running)
        self.assertEqual(state.endpoint_status, "healthy")
        self.assertIn("瞬时异常已恢复", state.endpoint_detail)

    def test_repeated_missing_endpoint_stops_with_network_changed(self) -> None:
        checker = _SequenceEndpointChecker(
            [EndpointObservation(False), EndpointObservation(False)]
        )
        self.controller._endpoint_checker_factory = lambda _baseline: checker
        self.controller._endpoint_check_interval = 0.1
        self.controller._endpoint_confirmation_delay = 0.05

        self.controller.start(self.shared, self.received)
        deadline = time.monotonic() + 1.5
        while self.controller.snapshot().running and time.monotonic() < deadline:
            time.sleep(0.02)

        state = self.controller.snapshot()
        self.assertFalse(state.running)
        self.assertEqual(state.stop_reason, "network_changed")
        self.assertEqual(state.endpoint_status, "changed")
        self.assertIn("address_changed", state.endpoint_detail)
        self.assertTrue(self.servers[-1].closed.is_set())

    def test_explicit_public_category_stops_without_second_observation(self) -> None:
        checker = _SequenceEndpointChecker(
            [EndpointObservation(True, category="Public")]
        )
        self.controller._endpoint_checker_factory = lambda _baseline: checker
        self.controller._endpoint_check_interval = 0.1
        self.controller._category_check_interval = 0.1

        self.controller.start(self.shared, self.received)
        deadline = time.monotonic() + 1.5
        while self.controller.snapshot().running and time.monotonic() < deadline:
            time.sleep(0.02)

        state = self.controller.snapshot()
        self.assertFalse(state.running)
        self.assertEqual(state.stop_reason, "network_changed")
        self.assertIn("Public", state.endpoint_detail)

    def test_deep_diagnostics_does_not_block_service_start(self) -> None:
        release = threading.Event()

        def blocking_diagnostics(_port: int, _program: str) -> dict[str, object]:
            release.wait(1)
            return _ready_diagnostics(_port, _program)

        self.controller._diagnostics_factory = blocking_diagnostics
        started = time.monotonic()
        state = self.controller.start(self.shared, self.received)

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(state.diagnostics["status"], "checking")
        release.set()
        deadline = time.monotonic() + 1
        while (
            self.controller.snapshot().diagnostics["status"] == "checking"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertEqual(self.controller.snapshot().diagnostics["status"], "ready")


if __name__ == "__main__":
    unittest.main()
