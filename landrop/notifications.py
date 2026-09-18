"""Windows-Toasts adapter with bounded, session-safe actionable reminders."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol


APP_USER_MODEL_ID = "LanDrop.Desktop"
TOAST_GROUP = "LanDrop.Actionable"
STATUS_TOAST_GROUP = "LanDrop.Status"

AUTOMATIC_STOP_MESSAGES = {
    "deadline_no_active": "5 分钟会话已到期，端口 8000 已关闭。",
    "deadline_transfers_completed": "现有传输已经完成，端口 8000 已关闭。",
    "grace_timeout": "传输宽限时间已结束，端口 8000 已关闭。",
    "system_resume": "检测到服务机从睡眠恢复，服务和端口已安全关闭。",
    "network_changed": "网络环境已变化，服务和端口已安全关闭。",
}


class ToastActionHandler(Protocol):
    def __call__(self, action: str, session_id: str, deadline_revision: int) -> None: ...


class WindowsToastBackend:
    def __init__(self, handler: ToastActionHandler) -> None:
        self._handler = handler
        self._toaster = None
        self._sent: set[tuple[str, int]] = set()

    def start(self) -> None:
        from windows_toasts import InteractableWindowsToaster

        self._toaster = InteractableWindowsToaster("LanDrop", APP_USER_MODEL_ID)
        self.clear_all()

    def send_expiry_reminder(self, session_id: str, deadline_revision: int) -> bool:
        key = (session_id, deadline_revision)
        if key in self._sent:
            return False
        if self._toaster is None:
            return False
        from windows_toasts import Toast, ToastButton

        toast = Toast(
            ["LanDrop 即将关闭服务", "剩余约 60 秒；可重置倒计时或立即停止服务。"],
            group=TOAST_GROUP,
            expiration_time=datetime.now(timezone.utc) + timedelta(minutes=2),
        )
        toast.tag = f"{session_id[:24]}-{deadline_revision}"
        toast.AddAction(ToastButton("重置为 5 分钟", "reset"))
        toast.AddAction(ToastButton("立即关闭", "stop"))
        toast.AddAction(ToastButton("打开窗口", "open_window"))
        toast.on_activated = lambda event: self._handler(
            str(event.arguments), session_id, deadline_revision
        )
        self._toaster.show_toast(toast)
        self._sent.add(key)
        return True

    def clear_actionable(self) -> None:
        if self._toaster is not None:
            self._toaster.remove_toast_group(TOAST_GROUP)
        self._sent.clear()

    def clear_all(self) -> None:
        if self._toaster is not None:
            self._toaster.clear_toasts()
        self._sent.clear()

    def send_service_stopped(self, reason: str) -> bool:
        """Show a buttonless automatic-stop notice whose body opens the GUI."""
        if self._toaster is None or reason not in AUTOMATIC_STOP_MESSAGES:
            return False
        from windows_toasts import Toast

        toast = Toast(
            ["LanDrop 服务已自动关闭", AUTOMATIC_STOP_MESSAGES[reason]],
            group=STATUS_TOAST_GROUP,
            expiration_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        toast.tag = "service-stopped"
        toast.on_activated = lambda _event: self._handler("open_window", "", 0)
        self._toaster.show_toast(toast)
        return True
