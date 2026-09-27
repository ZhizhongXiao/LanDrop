"""Minimal pystray adapter; it never owns service or window state."""

from __future__ import annotations

import threading
from collections.abc import Callable
from importlib import import_module
from typing import Protocol, cast

from .resources import resource_path


class TrayUnavailableError(RuntimeError):
    """Raised when the Windows notification area cannot be initialized."""


class _TrayState(Protocol):
    @property
    def running(self) -> bool: ...


class _TrayIcon(Protocol):
    visible: bool

    def run_detached(self, setup: Callable[[_TrayIcon], None]) -> None: ...

    def update_menu(self) -> None: ...

    def stop(self) -> None: ...


class _MenuFactory(Protocol):
    SEPARATOR: object

    def __call__(self, *items: object) -> object: ...


class _MenuItemFactory(Protocol):
    def __call__(
        self,
        text: str | Callable[[object], str],
        action: Callable[[object, object], None] | None,
        *,
        default: bool = False,
        enabled: bool | Callable[[object], bool] = True,
    ) -> object: ...


class _IconFactory(Protocol):
    def __call__(self, name: str, image: object, title: str, menu: object) -> _TrayIcon: ...


class _PystrayModule(Protocol):
    Menu: _MenuFactory
    MenuItem: _MenuItemFactory
    Icon: _IconFactory


class LanDropTray:
    def __init__(
        self,
        *,
        state_provider: Callable[[], _TrayState],
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
        self._icon: _TrayIcon | None = None
        self.ready = False

    def start(self) -> None:
        try:
            from PIL import Image

            pystray = cast(_PystrayModule, import_module("pystray"))
        except ImportError as exc:
            raise TrayUnavailableError("缺少 pystray 或 Pillow。") from exc
        icon_path = resource_path("assets/LanDrop-tray.ico")
        try:
            with Image.open(icon_path) as opened:
                image = opened.convert("RGBA")
        except OSError as exc:
            raise TrayUnavailableError(f"无法读取托盘图标：{icon_path}") from exc

        def running(_item: object) -> bool:
            return bool(self._state_provider().running)

        def status(item: object) -> str:
            return "状态：运行中" if running(item) else "状态：已停止"

        def open_window(_icon: object, _item: object) -> None:
            self._open_window()

        def reset(_icon: object, _item: object) -> None:
            self._reset()

        def stop_service(_icon: object, _item: object) -> None:
            self._stop_service()

        def exit_application(_icon: object, _item: object) -> None:
            self._exit_application()

        menu = pystray.Menu(
            pystray.MenuItem("打开窗口", open_window, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(status, None, enabled=False),
            pystray.MenuItem("重置为 5 分钟", reset, enabled=running),
            pystray.MenuItem("停止服务", stop_service, enabled=running),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出 LanDrop", exit_application),
        )
        self._icon = pystray.Icon("LanDrop", image, "LanDrop", menu)
        ready = threading.Event()

        def setup(icon: _TrayIcon) -> None:
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
