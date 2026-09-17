from __future__ import annotations

import threading
import time
import unittest

from landrop.coordinator import ActionCoordinator
from landrop.service import ServiceSnapshot


class _Window:
    def __init__(self) -> None:
        self.shown = threading.Event()
        self.hidden = threading.Event()
        self.destroyed = threading.Event()

    def show_window(self) -> None:
        self.shown.set()

    def hide_window(self) -> None:
        self.hidden.set()

    def destroy_window(self) -> None:
        self.destroyed.set()


class _Toasts:
    def __init__(self) -> None:
        self.cleared = 0
        self.cleared_all = 0

    def clear_actionable(self) -> None:
        self.cleared += 1

    def clear_all(self) -> None:
        self.cleared_all += 1


class _BlockingToasts(_Toasts):
    def __init__(self) -> None:
        super().__init__()
        self.clear_started = threading.Event()
        self.allow_clear = threading.Event()

    def clear_actionable(self) -> None:
        self.clear_started.set()
        self.allow_clear.wait(1)
        super().clear_actionable()


class _FailingToasts(_Toasts):
    def clear_actionable(self) -> None:
        raise RuntimeError("simulated notification cleanup failure")


class _Controller:
    def __init__(self) -> None:
        self.state = ServiceSnapshot(
            running=True,
            phase="running",
            message="服务正在运行",
            shared_directory="",
            receive_directory="",
            max_upload_mb=1,
            session_id="session-a",
            deadline_revision=1,
        )
        self.actions: list[str] = []

    def snapshot(self) -> ServiceSnapshot:
        return self.state

    def apply_expected_action(self, action: str, *, session_id: str, deadline_revision: int):
        if (
            not self.state.running
            or self.state.session_id != session_id
            or self.state.deadline_revision != deadline_revision
        ):
            return False, self.state
        self.actions.append(action)
        if action == "reset":
            self.state = ServiceSnapshot(
                **{**self.state.to_dict(), "deadline_revision": self.state.deadline_revision + 1}
            )
        elif action == "stop":
            self.state = ServiceSnapshot(
                **{**self.state.to_dict(), "running": False, "phase": "stopped"}
            )
        return True, self.state

    def stop(self, reason: str) -> ServiceSnapshot:
        self.actions.append(reason)
        self.state = ServiceSnapshot(
            **{**self.state.to_dict(), "running": False, "phase": "stopped", "stop_reason": reason}
        )
        return self.state

    def start(self, *_args):
        return self.state


class _BlockingStopController(_Controller):
    def __init__(self) -> None:
        super().__init__()
        self.stop_entered = threading.Event()
        self.allow_stop = threading.Event()

    def apply_expected_action(self, action: str, *, session_id: str, deadline_revision: int):
        if action == "stop":
            self.stop_entered.set()
            self.allow_stop.wait(2)
        return super().apply_expected_action(
            action,
            session_id=session_id,
            deadline_revision=deadline_revision,
        )


class ActionCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = _Window()
        self.controller = _Controller()
        self.toasts = _Toasts()
        self.coordinator = ActionCoordinator(self.controller, self.window, toasts=self.toasts)

    def test_stale_toast_cannot_modify_new_revision_but_can_open_window(self) -> None:
        result = self.coordinator.submit_toast_action(
            "reset", session_id="session-a", deadline_revision=1
        )
        self.assertTrue(result.applied)
        stale = self.coordinator.submit_toast_action(
            "stop", session_id="session-a", deadline_revision=1
        )
        self.assertFalse(stale.applied)
        self.assertTrue(self.controller.snapshot().running)

        self.coordinator.submit_toast_action("open_window")
        self.assertTrue(self.window.shown.wait(1))

    def test_stale_action_remains_safe_when_toast_cleanup_fails(self) -> None:
        coordinator = ActionCoordinator(
            self.controller,
            self.window,
            toasts=_FailingToasts(),
        )
        reset = coordinator.submit_toast_action(
            "reset", session_id="session-a", deadline_revision=1
        )
        stale = coordinator.submit_toast_action(
            "stop", session_id="session-a", deadline_revision=1
        )

        self.assertTrue(reset.applied)
        self.assertFalse(stale.applied)
        self.assertTrue(self.controller.snapshot().running)
        coordinator.request_exit()

    def test_tray_ready_close_hides_but_unavailable_tray_exits(self) -> None:
        self.assertFalse(self.coordinator.close_request(tray_ready=True))
        self.assertTrue(self.window.hidden.wait(1))
        self.assertTrue(self.controller.snapshot().running)

        other = ActionCoordinator(_Controller(), _Window())
        self.assertTrue(other.close_request(tray_ready=False))
        deadline = time.monotonic() + 1
        while not other.exiting and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(other.exiting)

    def test_natural_stop_publishes_tray_state_before_toast_cleanup(self) -> None:
        blocking_toasts = _BlockingToasts()
        stopped_published = threading.Event()
        stopped_notice_sent = threading.Event()
        coordinator = ActionCoordinator(
            self.controller,
            self.window,
            toasts=blocking_toasts,
            on_state_changed=lambda state: (
                stopped_published.set() if not state.running else None
            ),
        )
        coordinator.start_expiry_monitor(
            lambda _session, _revision: False,
            lambda reason: stopped_notice_sent.set()
            if reason == "deadline_no_active"
            else False,
        )
        time.sleep(0.3)

        self.controller.state = ServiceSnapshot(
            **{
                **self.controller.state.to_dict(),
                "running": False,
                "phase": "stopped",
                "stop_reason": "deadline_no_active",
            }
        )
        try:
            self.assertTrue(blocking_toasts.clear_started.wait(1))
            self.assertTrue(stopped_published.is_set())
            self.assertFalse(stopped_notice_sent.is_set())
        finally:
            blocking_toasts.allow_clear.set()
            self.assertTrue(stopped_notice_sent.wait(1))
            coordinator.request_exit()

    def test_manual_stop_does_not_send_automatic_stop_notice(self) -> None:
        stopped_notice_sent = threading.Event()
        coordinator = ActionCoordinator(self.controller, self.window, toasts=self.toasts)
        coordinator.start_expiry_monitor(
            lambda _session, _revision: False,
            lambda _reason: stopped_notice_sent.set() or True,
        )
        time.sleep(0.3)

        self.controller.state = ServiceSnapshot(
            **{
                **self.controller.state.to_dict(),
                "running": False,
                "phase": "stopped",
                "stop_reason": "manual_stop",
            }
        )
        time.sleep(0.4)
        coordinator.request_exit()
        self.assertFalse(stopped_notice_sent.is_set())

    def test_window_can_reopen_while_toast_stop_is_still_finishing(self) -> None:
        controller = _BlockingStopController()
        window = _Window()
        coordinator = ActionCoordinator(controller, window, toasts=_Toasts())
        stop_thread = threading.Thread(
            target=lambda: coordinator.submit_toast_action(
                "stop",
                session_id="session-a",
                deadline_revision=1,
            )
        )
        stop_thread.start()
        self.assertTrue(controller.stop_entered.wait(1))

        show_thread = threading.Thread(target=coordinator.submit_show_window)
        show_thread.start()
        try:
            self.assertTrue(window.shown.wait(0.5))
        finally:
            controller.allow_stop.set()
            stop_thread.join(2)
            show_thread.join(2)
            coordinator.request_exit()


if __name__ == "__main__":
    unittest.main()
