"""Minimal pystray adapter; it never owns service or window state."""

from __future__ import annotations

import threading
from typing import Any, Callable

from .resources import resource_path


class TrayUnavailableError(RuntimeError):
    """Raised when the Windows notification area cannot be initialized."""


class LanDropTray:
    def __init__(
        self,
        *,
        state_provider: Callable[[], Any],
        open_window: Callable[[], None],
        reset: Callable[[], None],
        stop_service: Callable[[], None],
        exit_application: Callable[[], None],
    ) -> None:
        self._state_provider = state_provider
        self._open_window = open_window
        self._reset = reset
        self._stop_service = stop_service
        self._exit_application = exit_application
        self._icon: Any | None = None
        self.ready = False

    def start(self) -> None:
        try:
            import pystray
            from PIL import Image
        except ImportError as exc:
            raise TrayUnavailableError("缺少 pystray 或 Pillow。") from exc
        icon_path = resource_path("assets/LanDrop-tray.ico")
        try:
            with Image.open(icon_path) as opened:
                image = opened.convert("RGBA")
        except OSError as exc:
            raise TrayUnavailableError(f"无法读取托盘图标：{icon_path}") from exc

        def running(_: Any) -> bool:
            return bool(self._state_provider().running)

        def status(_: Any) -> str:
            return "状态：运行中" if running(_) else "状态：已停止"

        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", lambda *_: self._open_window(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(status, None, enabled=False),
            pystray.MenuItem("重置为 5 分钟", lambda *_: self._reset(), enabled=running),
            pystray.MenuItem("停止服务", lambda *_: self._stop_service(), enabled=running),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 LanDrop", lambda *_: self._exit_application()),
        )
        self._icon = pystray.Icon("LanDrop", image, "LanDrop", menu)
        ready = threading.Event()

        def setup(icon: Any) -> None:
            # pystray icons start hidden.  A custom setup callback replaces its
            # default ``visible = True`` callback, so make the icon visible
            # before declaring the tray safe for close-to-tray behaviour.
            icon.visible = True
            ready.set()

        try:
            self._icon.run_detached(setup=setup)
            if not ready.wait(5):
                raise TimeoutError("系统托盘在 5 秒内未就绪。")
        except Exception as exc:
            self._icon = None
            raise TrayUnavailableError(f"无法初始化系统托盘：{exc}") from exc
        self.ready = True

    def refresh(self) -> None:
        if self._icon is not None:
            self._icon.update_menu()

    def stop(self) -> None:
        self.ready = False
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
