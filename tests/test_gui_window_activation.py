from __future__ import annotations

import unittest
from unittest.mock import patch

from landrop import gui


class _FakeReadyEvent:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.events.append("shown.wait")
        return True


class _FakeWindowEvents:
    def __init__(self, events: list[str]) -> None:
        self.shown = _FakeReadyEvent(events)


class _FakeHandle:
    def ToInt32(self) -> int:
        return 123


class _FakeNativeWindow:
    Handle = _FakeHandle()


class _FakeDesktopWindow:
    def __init__(self, events: list[str]) -> None:
        self.events = _FakeWindowEvents(events)
        self.native = _FakeNativeWindow()
        self._focus = False
        self.calls = events

    @property
    def focus(self) -> bool:
        return self._focus

    @focus.setter
    def focus(self, value: bool) -> None:
        self.calls.append(f"focus:{value}")
        self._focus = value

    def hide(self) -> None:
        self.calls.append("hide")

    def show(self) -> None:
        self.calls.append("show")

    def restore(self) -> None:
        self.calls.append("restore")

    def destroy(self) -> None:
        self.calls.append("destroy")

    def create_file_dialog(
        self,
        dialog_type: object,
        *,
        directory: str,
        allow_multiple: bool,
    ) -> tuple[str, ...]:
        del dialog_type, directory, allow_multiple
        return ()


class StartupWindowActivationTests(unittest.TestCase):
    def test_noactivate_bit_is_cleared_and_other_extended_styles_are_preserved(self) -> None:
        styles = {123: gui._WS_EX_NOACTIVATE | 0x00040000 | 0x00000008}
        set_calls: list[tuple[int, int, int]] = []
        position_calls: list[tuple[int, int, int, int, int, int, int]] = []

        def get_extended_style(handle: int, index: int) -> int:
            self.assertEqual(index, gui._GWL_EXSTYLE)
            return styles[handle]

        def set_extended_style(handle: int, index: int, value: int) -> int:
            self.assertEqual(index, gui._GWL_EXSTYLE)
            previous = styles[handle]
            styles[handle] = value
            set_calls.append((handle, index, value))
            return previous

        gui._clear_window_noactivate_style(
            123,
            get_extended_style=get_extended_style,
            set_extended_style=set_extended_style,
            set_window_pos=lambda *args: position_calls.append(args) or 1,
        )

        expected_style = 0x00040000 | 0x00000008
        self.assertEqual(styles[123], expected_style)
        self.assertEqual(set_calls, [(123, gui._GWL_EXSTYLE, expected_style)])
        self.assertEqual(
            position_calls,
            [
                (
                    123,
                    0,
                    0,
                    0,
                    0,
                    0,
                    gui._SWP_NOSIZE
                    | gui._SWP_NOMOVE
                    | gui._SWP_NOZORDER
                    | gui._SWP_NOACTIVATE
                    | gui._SWP_FRAMECHANGED,
                )
            ],
        )

    def test_existing_noactivate_clear_still_refreshes_cached_window_styles(self) -> None:
        position_calls: list[tuple[int, int, int, int, int, int, int]] = []

        gui._clear_window_noactivate_style(
            123,
            get_extended_style=lambda _handle, _index: 0x00040000,
            set_extended_style=lambda *_args: self.fail(
                "样式位已清除，不应再次调用 SetWindowLongW。"
            ),
            set_window_pos=lambda *args: position_calls.append(args) or 1,
        )

        self.assertEqual(len(position_calls), 1)

    def test_startup_activation_style_is_cleared_before_show_and_restore(self) -> None:
        events: list[str] = []
        window = _FakeDesktopWindow(events)
        style_calls: list[int] = []
        clear_style_helper = gui._clear_window_noactivate_style

        def clear_style(handle: int) -> None:
            self.assertEqual(handle, 123)
            style_calls.append(handle)
            events.append("style.clear")
            styles = {handle: gui._WS_EX_NOACTIVATE | 0x00040000}

            def set_style(_handle: int, _index: int, value: int) -> int:
                previous = styles[handle]
                styles[handle] = value
                return previous

            clear_style_helper(
                handle,
                get_extended_style=lambda _handle, _index: styles[handle],
                set_extended_style=set_style,
                set_window_pos=lambda *_args: events.append("style.refresh") or 1,
            )
            self.assertEqual(styles[handle], 0x00040000)

        dispatcher = gui._WindowDispatcher(window, startup_hidden=True)
        with (
            patch("landrop.gui._clear_window_noactivate_style", side_effect=clear_style),
            patch(
                "landrop.gui._invoke_winforms_thread",
                side_effect=lambda _native, action: (events.append("ui.invoke"), action()),
            ),
        ):
            dispatcher.show_window()
            dispatcher.show_window()

        self.assertEqual(
            events,
            [
                "shown.wait",
                "ui.invoke",
                "style.clear",
                "style.refresh",
                "focus:True",
                "show",
                "restore",
                "show",
                "restore",
            ],
        )
        self.assertEqual(style_calls, [123])
        self.assertTrue(window.focus)

    def test_normal_startup_does_not_change_focus_or_window_styles(self) -> None:
        events: list[str] = []
        window = _FakeDesktopWindow(events)
        dispatcher = gui._WindowDispatcher(window, startup_hidden=False)

        with (
            patch("landrop.gui._clear_window_noactivate_style") as clear_style,
            patch("landrop.gui._invoke_winforms_thread") as invoke,
        ):
            dispatcher.show_window()

        clear_style.assert_not_called()
        invoke.assert_not_called()
        self.assertFalse(window.focus)
        self.assertEqual(events, ["show", "restore"])


if __name__ == "__main__":
    unittest.main()
