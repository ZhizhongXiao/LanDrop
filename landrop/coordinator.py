"""One action boundary for the GUI, tray, and Windows Toast callbacks."""

from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time
from typing import Callable, Protocol

from .service import ServiceController, ServiceSnapshot


AUTOMATIC_STOP_REASONS = frozenset(
    {
        "deadline_no_active",
        "deadline_transfers_completed",
        "grace_timeout",
        "system_resume",
        "network_changed",
    }
)


class WindowActions(Protocol):
    """The sole path that may manipulate the pywebview Window."""

    def show_window(self) -> None: ...

    def hide_window(self) -> None: ...

    def destroy_window(self) -> None: ...


class TrayActions(Protocol):
    def stop(self) -> None: ...


class ToastActions(Protocol):
    def clear_actionable(self) -> None: ...

    def clear_all(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ActionResult:
    applied: bool
    state: ServiceSnapshot
    message: str = ""


class ActionCoordinator:
    """Serialize UI requests while leaving all service authority in the controller."""

    def __init__(
        self,
        controller: ServiceController,
        window: WindowActions,
        *,
        tray: TrayActions | None = None,
        toasts: ToastActions | None = None,
        on_state_changed: Callable[[ServiceSnapshot], None] | None = None,
    ) -> None:
        self._controller = controller
        self._window = window
        self._tray = tray
        self._toasts = toasts
        self._on_state_changed = on_state_changed
        self._exit_requested = threading.Event()
        self._lock = threading.RLock()
        self._window_queue_lock = threading.Lock()
        self._gui_queue: queue.Queue[Callable[[], None] | None] = queue.Queue()
        self._gui_thread = threading.Thread(
            target=self._run_gui_actions,
            name="LanDrop-GUI-Actions",
            daemon=True,
        )
        self._gui_thread.start()
        self._reminder_thread: threading.Thread | None = None

    def bind_tray(self, tray: TrayActions) -> None:
        self._tray = tray

    def bind_toasts(self, toasts: ToastActions) -> None:
        self._toasts = toasts

    def set_state_listener(self, callback: Callable[[ServiceSnapshot], None]) -> None:
        self._on_state_changed = callback

    def start_expiry_monitor(
        self,
        send_reminder: Callable[[str, int], bool],
        send_stopped: Callable[[str], bool] | None = None,
    ) -> None:
        """Observe controller snapshots; the Toast backend never reads timers itself."""
        if self._reminder_thread is not None:
            return

        def monitor() -> None:
            previous_key: tuple[bool, str, int, str] | None = None
            while not self.exiting:
                state = self._controller.snapshot()
                key = (
                    state.running,
                    state.session_id,
                    state.deadline_revision,
                    state.phase,
                )
                if key != previous_key:
                    should_clear_toasts = previous_key is not None and (
                        not state.running
                        or state.session_id != previous_key[1]
                        or state.deadline_revision != previous_key[2]
                    )
                    # Make the tray reflect the authoritative service state
                    # before a potentially slow Windows notification cleanup.
                    self._publish_state(state)
                    if should_clear_toasts:
                        self._clear_toasts()
                    if (
                        send_stopped is not None
                        and previous_key is not None
                        and not state.running
                        and state.phase == "stopped"
                        and state.stop_reason in AUTOMATIC_STOP_REASONS
                    ):
                        try:
                            send_stopped(state.stop_reason)
                        except Exception:
                            pass
                    previous_key = key
                if (
                    state.running
                    and state.phase == "running"
                    and 0 < state.remaining_seconds <= 60
                ):
                    try:
                        send_reminder(state.session_id, state.deadline_revision)
                    except Exception:
                        pass
                time.sleep(0.25)

        self._reminder_thread = threading.Thread(
            target=monitor,
            name="LanDrop-Expiry-Reminder",
            daemon=True,
        )
        self._reminder_thread.start()

    @property
    def exiting(self) -> bool:
        return self._exit_requested.is_set()

    def submit_show_window(self) -> ActionResult:
        """Queue a non-service UI action; old Toasts may always open the window."""
        with self._window_queue_lock:
            accepted = not self._exit_requested.is_set()
            if accepted:
                self._gui_queue.put(self._window.show_window)
        # Queue the presentation action before reading service state: a manual
        # stop may still be releasing sockets, but that must not delay opening
        # the already-live control window.
        return ActionResult(accepted, self._controller.snapshot())

    def submit_hide_window(self) -> None:
        with self._window_queue_lock:
            if not self._exit_requested.is_set():
                self._gui_queue.put(self._window.hide_window)

    def submit_toast_action(
        self,
        action: str,
        *,
        session_id: str = "",
        deadline_revision: int = 0,
    ) -> ActionResult:
        if action == "open_window":
            return self.submit_show_window()
        with self._lock:
            if self._exit_requested.is_set():
                return ActionResult(False, self._controller.snapshot(), "应用正在退出。")
            applied, state = self._controller.apply_expected_action(
                action,
                session_id=session_id,
                deadline_revision=deadline_revision,
            )
            if applied:
                self._clear_toasts()
                self._publish_state(state)
            return ActionResult(
                applied,
                state,
                "" if applied else "通知已过期，未改变当前服务。",
            )

    def start_from_gui(
        self,
        shared_directory: str,
        receive_directory: str,
        max_upload_mb: int,
        interface_selector: str | None = None,
    ) -> ServiceSnapshot:
        with self._lock:
            if self._exit_requested.is_set():
                raise RuntimeError("应用正在退出。")
            state = self._controller.start(
                shared_directory,
                receive_directory,
                max_upload_mb,
                interface_selector,
            )
            self._clear_all_toasts()
            self._publish_state(state)
            return state

    def reset_from_tray(self) -> ActionResult:
        with self._lock:
            state = self._controller.snapshot()
            if not state.running:
                return ActionResult(False, state, "服务尚未启动。")
            applied, state = self._controller.apply_expected_action(
                "reset",
                session_id=state.session_id,
                deadline_revision=state.deadline_revision,
            )
            if applied:
                self._clear_toasts()
                self._publish_state(state)
            return ActionResult(applied, state)

    def stop_from_ui(self) -> ActionResult:
        with self._lock:
            state = self._controller.stop("manual_stop")
            self._clear_toasts()
            self._publish_state(state)
            return ActionResult(True, state)

    def request_exit(self, reason: str = "app_exit") -> None:
        """Perform the documented one-way application shutdown sequence."""
        with self._lock:
            if self._exit_requested.is_set():
                return
            with self._window_queue_lock:
                self._exit_requested.set()
            self._controller.stop(reason)
            self._clear_all_toasts()
            if self._tray is not None:
                self._tray.stop()
            with self._window_queue_lock:
                self._gui_queue.put(self._window.destroy_window)
                self._gui_queue.put(None)

    def close_request(self, *, tray_ready: bool) -> bool:
        """Handle a pywebview closing event; return whether native close may proceed."""
        if tray_ready:
            self.submit_hide_window()
            return False
        self.request_exit("app_exit")
        return True

    def _clear_toasts(self) -> None:
        if self._toasts is not None:
            try:
                self._toasts.clear_actionable()
            except Exception:
                pass

    def _clear_all_toasts(self) -> None:
        if self._toasts is not None:
            try:
                self._toasts.clear_all()
            except Exception:
                pass

    def _publish_state(self, state: ServiceSnapshot) -> None:
        if self._on_state_changed is not None:
            try:
                self._on_state_changed(state)
            except Exception:
                # Presentation failures must not terminate lifecycle monitoring.
                pass

    def _run_gui_actions(self) -> None:
        while True:
            callback = self._gui_queue.get()
            if callback is None:
                return
            try:
                callback()
            except Exception:
                # UI presentation errors must not change the service lifecycle.
                pass
