"""Threaded WSGI servers for LAN and loopback Bottle access."""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
import threading
from socketserver import TCPServer, ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer

logger = logging.getLogger("landrop.server")


SHUTDOWN_POLL_INTERVAL_SECONDS = 0.1
MAX_HTTP_WORKERS = 16
SOCKET_IDLE_TIMEOUT_SECONDS = 30.0
type ServerRequest = socket.socket | tuple[bytes, socket.socket]


class LanDropRequestHandler(WSGIRequestHandler):
    """Readable request logging without reverse DNS lookups."""

    server_version = "LanDrop/0.6"

    def address_string(self) -> str:
        return self.client_address[0]

    def log_message(self, format: str, *args: object) -> None:
        logger.info("[请求] %s - %s", self.client_address[0], format % args)


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        *args: Any,
        request_slots: threading.BoundedSemaphore | None = None,
        socket_idle_timeout_seconds: float = SOCKET_IDLE_TIMEOUT_SECONDS,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._connections: set[ServerRequest] = set()
        self._connections_lock = threading.Lock()
        self._request_slots = (
            request_slots
            if request_slots is not None
            else threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        )
        self._socket_idle_timeout_seconds = socket_idle_timeout_seconds

    def server_bind(self) -> None:
        """Bind without HTTPServer's blocking reverse-DNS lookup.

        LanDrop only needs the numeric bind address in the WSGI environment.
        ``HTTPServer.server_bind`` calls ``socket.getfqdn`` and can stall for
        about five seconds on phone hotspots without reverse DNS.
        """
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)
        self.setup_environ()

    def process_request(self, request: ServerRequest, client_address: tuple[str, int]) -> None:
        if not self._request_slots.acquire(blocking=False):
            logger.info("[连接限额] %s 超出 HTTP 并发上限。", client_address[0])
            self.shutdown_request(request)
            return
        with self._connections_lock:
            self._connections.add(request)
        try:
            connection = request if isinstance(request, socket.socket) else request[1]
            connection.settimeout(self._socket_idle_timeout_seconds)
            super().process_request(request, client_address)
        except BaseException:
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request: ServerRequest) -> None:
        try:
            super().shutdown_request(request)
        finally:
            release_slot = False
            with self._connections_lock:
                if request in self._connections:
                    self._connections.remove(request)
                    release_slot = True
            if release_slot:
                self._request_slots.release()

    def close_active_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for request in connections:
            connection = request if isinstance(request, socket.socket) else request[1]
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                connection.close()

    def handle_error(self, request: ServerRequest, client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            logger.info(
                "[连接结束] %s 主动关闭了连接；浏览器取消或结束请求时可能出现。",
                client_address[0],
            )
            return
        super().handle_error(request, client_address)


class ServerGroup:
    """Run the same WSGI app on the selected LAN address and loopback."""

    def __init__(
        self,
        lan_address: str,
        port: int,
        application: Any,
        *,
        request_slots: threading.BoundedSemaphore | None = None,
    ) -> None:
        self._servers: list[ThreadedWSGIServer] = []
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        shared_request_slots = (
            request_slots
            if request_slots is not None
            else threading.BoundedSemaphore(MAX_HTTP_WORKERS)
        )
        try:
            lan_server = ThreadedWSGIServer(
                (lan_address, port),
                LanDropRequestHandler,
                request_slots=shared_request_slots,
            )
            lan_server.set_app(application)
            self._servers.append(lan_server)
            if lan_address != "127.0.0.1":
                loopback_server = ThreadedWSGIServer(
                    ("127.0.0.1", port),
                    LanDropRequestHandler,
                    request_slots=shared_request_slots,
                )
                loopback_server.set_app(application)
                self._servers.append(loopback_server)
        except OSError:
            self.close()
            raise

    def serve_forever(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            for server in self._servers:
                address, port = server.server_address[:2]
                thread = threading.Thread(
                    target=server.serve_forever,
                    kwargs={"poll_interval": SHUTDOWN_POLL_INTERVAL_SECONDS},
                    name=f"LanDrop-{address}:{port}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
        while not self._stopping.wait(0.5):
            if not all(thread.is_alive() for thread in self._threads):
                return

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            for server in self._servers:
                if self._threads:
                    server.shutdown()
                server.close_active_connections()
                server.server_close()
            for thread in self._threads:
                thread.join(timeout=3)
            self._threads.clear()
            self._servers.clear()
