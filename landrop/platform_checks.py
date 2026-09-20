"""Small Windows runtime checks needed before the desktop GUI starts."""

from __future__ import annotations

import os


WEBVIEW2_CLIENT_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_runtime_version() -> str:
    """Return the installed Evergreen WebView2 version, or an empty string."""
    if os.name != "nt":
        return ""
    import winreg

    paths = (
        rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
        rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_CLIENT_ID}",
    )
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for path in paths:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _kind = winreg.QueryValueEx(key, "pv")
                text = str(version).strip()
                if text and text != "0":
                    return text
            except OSError:
                continue
    return ""
