"""Read-only HTTP file server for the first LanDrop milestone."""

from __future__ import annotations

from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
import sys
import threading
from urllib.parse import quote, unquote, urlsplit


class UnsafeRequestPath(ValueError):
    """Raised when a URL could escape the shared directory."""


def resolve_request_path(shared_directory: Path, request_target: str) -> Path:
    """Resolve a request URL beneath the shared root or reject it."""
    try:
        decoded = unquote(urlsplit(request_target).path, errors="strict")
    except UnicodeError as exc:
        raise UnsafeRequestPath("URL 编码无效") from exc
    if "\x00" in decoded or "\\" in decoded:
        raise UnsafeRequestPath("请求路径包含非法字符")

    relative = PurePosixPath(decoded.lstrip("/"))
    if any(part == ".." for part in relative.parts):
        raise UnsafeRequestPath("请求路径不能超出共享目录")

    root = shared_directory.resolve(strict=True)
    candidate = root.joinpath(*relative.parts).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise UnsafeRequestPath("请求路径不能超出共享目录")
    return candidate


class ReadOnlyFileHandler(SimpleHTTPRequestHandler):
    """Serve one directory with downloads and no mutation methods."""

    server_version = "LanDrop/0.1"
    sys_version = ""

    def __init__(self, *args: object, directory: str, **kwargs: object) -> None:
        self.shared_directory = Path(directory)
        self._download_name: str | None = None
        super().__init__(*args, directory=directory, **kwargs)

    def _prepare_request(self) -> bool:
        self._download_name = None
        try:
            target = resolve_request_path(self.shared_directory, self.path)
        except (UnsafeRequestPath, OSError) as exc:
            # HTTP reason phrases must remain Latin-1/ASCII. Keep the readable
            # Chinese explanation in the UTF-8 response body instead.
            self.send_error(
                HTTPStatus.FORBIDDEN,
                "Forbidden",
                explain=str(exc),
            )
            return False
        if target.is_file():
            self._download_name = target.name
        return True

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self._prepare_request():
            super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self._prepare_request():
            super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def end_headers(self) -> None:
        if self._download_name:
            fallback = self._download_name.encode("ascii", "ignore").decode() or "download"
            fallback = fallback.replace("\\", "_").replace('"', "_")
            encoded = quote(self._download_name, safe="")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}',
            )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, message_format: str, *args: object) -> None:
        client = self.client_address[0]
        print(f"[请求] {client} - {message_format % args}")


class LanDropHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request: object, client_address: tuple[str, int]) -> None:
        """Keep routine browser disconnects from producing alarming tracebacks."""
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
    """Run LAN and loopback listeners on the same port."""

    def __init__(self, lan_address: str, port: int, shared_directory: Path) -> None:
        handler = partial(ReadOnlyFileHandler, directory=str(shared_directory))
        self._servers: list[LanDropHTTPServer] = []
        self._threads: list[threading.Thread] = []
        try:
            self._servers.append(LanDropHTTPServer((lan_address, port), handler))
            if lan_address != "127.0.0.1":
                self._servers.append(LanDropHTTPServer(("127.0.0.1", port), handler))
        except OSError:
            self.close()
            raise

    def serve_forever(self) -> None:
        for server in self._servers:
            address, port = server.server_address[:2]
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"LanDrop-{address}:{port}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        stop_check = threading.Event()
        while all(thread.is_alive() for thread in self._threads):
            stop_check.wait(0.5)

    def close(self) -> None:
        for server in self._servers:
            if self._threads:
                server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=3)
        self._threads.clear()
        self._servers.clear()
