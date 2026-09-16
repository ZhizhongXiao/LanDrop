"""Threaded WSGI servers for LAN and loopback Bottle access."""

from __future__ import annotations

import sys
import socket
import threading
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


class LanDropRequestHandler(WSGIRequestHandler):
    """Readable request logging without reverse DNS lookups."""

    server_version = "LanDrop/0.4"

    def address_string(self) -> str:
        return self.client_address[0]

    def log_message(self, message_format: str, *args: object) -> None:
        print(f"[请求] {self.client_address[0]} - {message_format % args}")


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._connections: set[Any] = set()
        self._connections_lock = threading.Lock()

    def process_request(self, request: Any, client_address: tuple[str, int]) -> None:
        with self._connections_lock:
            self._connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        try:
            super().shutdown_request(request)
        finally:
            with self._connections_lock:
                self._connections.discard(request)

    def close_active_connections(self) -> None:
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            print(
                f"[连接结束] {client_address[0]} 主动关闭了连接；"
                "浏览器取消或结束请求时可能出现。"
            )
            return
        super().handle_error(request, client_address)


class ServerGroup:
    """Run the same WSGI app on the selected LAN address and loopback."""

    def __init__(self, lan_address: str, port: int, application: Any) -> None:
        self._servers: list[WSGIServer] = []
        self._threads: list[threading.Thread] = []
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        try:
            self._servers.append(
                make_server(
                    lan_address,
                    port,
                    application,
                    server_class=ThreadedWSGIServer,
                    handler_class=LanDropRequestHandler,
                )
            )
            if lan_address != "127.0.0.1":
                self._servers.append(
                    make_server(
                        "127.0.0.1",
                        port,
                        application,
                        server_class=ThreadedWSGIServer,
                        handler_class=LanDropRequestHandler,
                    )
                )
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
                if isinstance(server, ThreadedWSGIServer):
                    server.close_active_connections()
                server.server_close()
            for thread in self._threads:
                thread.join(timeout=3)
            self._threads.clear()
            self._servers.clear()
