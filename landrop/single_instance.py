"""Windows single-instance ownership and existing-window activation."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import time
from typing import Callable


ERROR_ALREADY_EXISTS = 183


class SingleInstanceError(RuntimeError):
    """Single-instance coordination could not be initialized."""


class DesktopSingleInstance:
    """Own a per-session mutex and a token-protected loopback activation channel."""

    def __init__(
        self,
        data_directory: Path,
        *,
        mutex_name: str = r"Local\LanDrop.Desktop",
    ) -> None:
        self._data_directory = Path(data_directory)
        self._state_path = self._data_directory / "desktop-instance.json"
        self._mutex_name = mutex_name
        self._mutex: int | None = None
        self._kernel32: object | None = None
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._token = ""
        self._activate: Callable[[], object] | None = None
        self._activation_pending = False
        self._callback_lock = threading.Lock()

    def acquire(self) -> bool:
        if os.name != "nt":
            raise SingleInstanceError("单实例控制目前只支持 Windows。")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateMutexW(None, False, self._mutex_name)
        if not handle:
            raise SingleInstanceError(f"无法创建单实例锁（Windows 错误 {ctypes.get_last_error()}）。")
        self._kernel32 = kernel32
        self._mutex = int(handle)
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            return False
        self._start_activation_listener()
        return True

    def notify_existing(self, timeout_seconds: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while time.monotonic() < deadline:
            try:
                state = json.loads(self._state_path.read_text(encoding="utf-8"))
                port = int(state["port"])
                token = str(state["token"])
                with socket.create_connection(("127.0.0.1", port), timeout=0.4) as connection:
                    payload = json.dumps(
                        {"token": token, "action": "show"}, ensure_ascii=True
                    ).encode("ascii")
                    connection.sendall(payload + b"\n")
                    return connection.recv(16).strip() == b"ok"
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                time.sleep(0.05)
        return False

    def set_activate_callback(self, callback: Callable[[], object]) -> None:
        with self._callback_lock:
            self._activate = callback
            pending = self._activation_pending
            self._activation_pending = False
        if pending:
            callback()

    def close(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)
        self._listener = None
        self._thread = None
        try:
            state = json.loads(self._state_path.read_text(encoding="utf-8"))
            if state.get("token") == self._token:
                self._state_path.unlink(missing_ok=True)
        except (OSError, ValueError, AttributeError, json.JSONDecodeError):
            pass
        if self._mutex is not None and self._kernel32 is not None:
            try:
                self._kernel32.CloseHandle(wintypes.HANDLE(self._mutex))
            finally:
                self._mutex = None
                self._kernel32 = None

    def _start_activation_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", 0))
            listener.listen(4)
            listener.settimeout(0.25)
            port = int(listener.getsockname()[1])
            self._token = secrets.token_urlsafe(24)
            self._data_directory.mkdir(parents=True, exist_ok=True)
            temporary = self._data_directory / f".desktop-instance.{secrets.token_hex(6)}.tmp"
            try:
                temporary.write_text(
                    json.dumps(
                        {"version": 1, "pid": os.getpid(), "port": port, "token": self._token},
                        ensure_ascii=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self._state_path)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception:
            listener.close()
            raise
        self._listener = listener
        self._thread = threading.Thread(
            target=self._listen,
            name="landrop-instance-activation",
            daemon=True,
        )
        self._thread.start()

    def _listen(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.5)
                try:
                    raw = connection.recv(4096)
                    request = json.loads(raw.split(b"\n", 1)[0].decode("ascii"))
                    accepted = secrets.compare_digest(
                        str(request.get("token") or ""), self._token
                    ) and request.get("action") == "show"
                    if accepted:
                        self._request_activation()
                    connection.sendall(b"ok\n" if accepted else b"no\n")
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    continue

    def _request_activation(self) -> None:
        with self._callback_lock:
            callback = self._activate
            if callback is None:
                self._activation_pending = True
                return
        callback()

    def __enter__(self) -> DesktopSingleInstance:
        if not self.acquire():
            raise SingleInstanceError("LanDrop 已在运行。")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
