from __future__ import annotations

import socket
import threading
import unittest
from socketserver import ThreadingMixIn
from unittest.mock import patch

from landrop.server import (
    MAX_HTTP_WORKERS,
    SOCKET_IDLE_TIMEOUT_SECONDS,
    LanDropRequestHandler,
    ServerGroup,
    ThreadedWSGIServer,
)
from landrop.web import StartResponse


class HttpWorkerLimitTests(unittest.TestCase):
    def test_seventeenth_request_is_closed_before_worker_creation_and_slot_reuses(self) -> None:
        server = ThreadedWSGIServer(
            ("127.0.0.1", 0),
            LanDropRequestHandler,
            socket_idle_timeout_seconds=0.25,
        )
        accepted: list[tuple[socket.socket, socket.socket]] = []
        try:
            with patch.object(ThreadingMixIn, "process_request", autospec=True) as worker_start:
                for _index in range(MAX_HTTP_WORKERS):
                    server_socket, client_socket = socket.socketpair()
                    accepted.append((server_socket, client_socket))
                    server.process_request(server_socket, ("127.0.0.1", 0))
                    self.assertEqual(server_socket.gettimeout(), 0.25)

                rejected_server, rejected_client = socket.socketpair()
                server.process_request(rejected_server, ("127.0.0.1", 0))
                self.assertEqual(worker_start.call_count, MAX_HTTP_WORKERS)
                self.assertEqual(rejected_client.recv(1), b"")

                server.shutdown_request(accepted[0][0])
                recycled_server, recycled_client = socket.socketpair()
                accepted.append((recycled_server, recycled_client))
                server.process_request(recycled_server, ("127.0.0.1", 0))
                self.assertEqual(worker_start.call_count, MAX_HTTP_WORKERS + 1)
                self.assertEqual(recycled_server.gettimeout(), 0.25)

                rejected_client.close()
                for server_socket, client_socket in accepted:
                    if server_socket.fileno() != -1:
                        server.shutdown_request(server_socket)
                    client_socket.close()
        finally:
            server.server_close()

    def test_idle_socket_timeout_closes_stalled_header_and_releases_worker_slot(self) -> None:
        request_slots = threading.BoundedSemaphore(1)
        server = ThreadedWSGIServer(
            ("127.0.0.1", 0),
            LanDropRequestHandler,
            request_slots=request_slots,
            socket_idle_timeout_seconds=0.05,
        )
        server.set_app(_empty_application)
        serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
        serve_thread.start()
        address, port = server.server_address[:2]
        client = socket.create_connection((str(address), int(port)), timeout=2)
        try:
            client.sendall(b"GET / HTTP/1.1\r\nHost:")
            self.assertTrue(request_slots.acquire(timeout=2))
            request_slots.release()
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            serve_thread.join(timeout=2)

    def test_lan_and_loopback_listeners_share_one_request_semaphore(self) -> None:
        shared_slots = threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        for _index in range(MAX_HTTP_WORKERS - 1):
            self.assertTrue(shared_slots.acquire(blocking=False))
        lan_started = threading.Event()
        release_lan = threading.Event()

        def blocking_application(
            _environ: dict[str, object],
            start_response: StartResponse,
        ) -> list[bytes]:
            start_response("200 OK", [("Content-Length", "0")])
            lan_started.set()
            release_lan.wait(timeout=3)
            return [b""]

        with socket.socket() as probe:
            probe.bind(("127.0.0.2", 0))
            port = probe.getsockname()[1]
        group = ServerGroup(
            "127.0.0.2",
            port,
            blocking_application,
            request_slots=shared_slots,
        )
        serve_thread = threading.Thread(target=group.serve_forever, daemon=True)
        serve_thread.start()
        lan_client = socket.create_connection(("127.0.0.2", port), timeout=2)
        loopback_client: socket.socket | None = None
        try:
            lan_client.sendall(b"GET / HTTP/1.1\r\nHost: lan\r\n\r\n")
            self.assertTrue(lan_started.wait(timeout=2))
            loopback_client = socket.create_connection(("127.0.0.1", port), timeout=2)
            loopback_client.settimeout(2)
            loopback_client.sendall(b"GET / HTTP/1.1\r\nHost: loopback\r\n\r\n")
            try:
                received = loopback_client.recv(1)
            except (ConnectionAbortedError, ConnectionResetError):
                received = b""
            self.assertEqual(received, b"")
            self.assertEqual(SOCKET_IDLE_TIMEOUT_SECONDS, 30.0)
        finally:
            release_lan.set()
            lan_client.close()
            if loopback_client is not None:
                loopback_client.close()
            group.close()
            serve_thread.join(timeout=3)
            for _index in range(MAX_HTTP_WORKERS - 1):
                shared_slots.release()


def _empty_application(_environ: object, _start_response: object) -> list[bytes]:
    return []


if __name__ == "__main__":
    unittest.main()
