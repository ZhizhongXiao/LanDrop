"""Independent Windows mutex for installation lifecycle mutations."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import threading

from .install_contract import InstallPaths, lifecycle_mutex_name


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF


class InstallLifecycleLockError(RuntimeError):
    """The install lifecycle mutex could not be safely used."""


class InstallLifecycleLock:
    """Own the per-user install/upgrade/uninstall mutex.

    This mutex is intentionally separate from ``DesktopSingleInstance``.
    Ownership is thread-affine because Windows mutex ownership is thread-affine.
    """

    def __init__(
        self,
        paths: InstallPaths | None = None,
        *,
        mutex_name: str | None = None,
    ) -> None:
        resolved_paths = paths or InstallPaths.from_environment()
        self.name = mutex_name or lifecycle_mutex_name(resolved_paths)
        self._handle: int | None = None
        self._kernel32: object | None = None
        self._owned = False
        self._owner_thread_id: int | None = None
        self.was_abandoned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self, timeout_seconds: float | None = 0.0) -> bool:
        if os.name != "nt":
            raise InstallLifecycleLockError("安装生命周期锁目前只支持 Windows。")
        if self._handle is not None:
            raise InstallLifecycleLockError("同一个生命周期锁对象不能重复取得。")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("锁等待时间不能为负数。")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.ReleaseMutex.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise InstallLifecycleLockError(
                f"无法创建安装生命周期锁（Windows 错误 {ctypes.get_last_error()}）。"
            )
        self._kernel32 = kernel32
        self._handle = int(handle)
        milliseconds = (
            INFINITE
            if timeout_seconds is None
            else min(int(timeout_seconds * 1000), INFINITE - 1)
        )
        result = int(kernel32.WaitForSingleObject(wintypes.HANDLE(handle), milliseconds))
        if result in {WAIT_OBJECT_0, WAIT_ABANDONED}:
            self._owned = True
            self._owner_thread_id = threading.get_ident()
            self.was_abandoned = result == WAIT_ABANDONED
            return True
        self._close_handle()
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_FAILED:
            raise InstallLifecycleLockError(
                f"等待安装生命周期锁失败（Windows 错误 {ctypes.get_last_error()}）。"
            )
        raise InstallLifecycleLockError(f"安装生命周期锁返回未知状态：{result}")

    def release(self) -> None:
        if not self._owned or self._handle is None or self._kernel32 is None:
            return
        if self._owner_thread_id != threading.get_ident():
            raise InstallLifecycleLockError("安装生命周期锁必须由取得它的线程释放。")
        if not self._kernel32.ReleaseMutex(wintypes.HANDLE(self._handle)):
            raise InstallLifecycleLockError(
                f"释放安装生命周期锁失败（Windows 错误 {ctypes.get_last_error()}）。"
            )
        self._owned = False
        self._owner_thread_id = None

    def close(self) -> None:
        release_error: Exception | None = None
        try:
            self.release()
        except Exception as exc:
            release_error = exc
        finally:
            self._close_handle()
        if release_error is not None:
            raise release_error

    def _close_handle(self) -> None:
        if self._handle is not None and self._kernel32 is not None:
            self._kernel32.CloseHandle(wintypes.HANDLE(self._handle))
        self._handle = None
        self._kernel32 = None
        self._owned = False
        self._owner_thread_id = None

    def __enter__(self) -> InstallLifecycleLock:
        if not self.acquire(None):
            raise InstallLifecycleLockError("无法取得安装生命周期锁。")
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
