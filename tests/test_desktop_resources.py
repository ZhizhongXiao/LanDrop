from __future__ import annotations

from pathlib import Path
import re
import unittest

from PIL import Image

import landrop.gui as gui


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DESKTOP_ROOT = PROJECT_ROOT / "ui" / "desktop"


class DesktopResourceTests(unittest.TestCase):
    def test_desktop_entry_and_assets_are_external_files(self) -> None:
        expected = (
            DESKTOP_ROOT / "index.html",
            DESKTOP_ROOT / "css" / "tokens.css",
            DESKTOP_ROOT / "css" / "shell.css",
            DESKTOP_ROOT / "css" / "components.css",
            PROJECT_ROOT / "ui" / "shared" / "js" / "theme.js",
            DESKTOP_ROOT / "js" / "state.js",
            DESKTOP_ROOT / "js" / "app.js",
        )
        self.assertTrue(all(path.is_file() for path in expected))
        self.assertEqual(gui._desktop_entry_path().resolve(), expected[0].resolve())

        gui_source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertNotIn("DESKTOP_HTML", gui_source)
        self.assertNotIn("<!doctype html>", gui_source.lower())

    def test_entry_links_split_styles_and_scripts_without_inline_blocks(self) -> None:
        html = (DESKTOP_ROOT / "index.html").read_text(encoding="utf-8")
        for relative in (
            "css/tokens.css",
            "css/shell.css",
            "css/components.css",
            "../shared/js/theme.js",
            "js/state.js",
            "js/app.js",
        ):
            self.assertIn(relative, html)
        self.assertNotRegex(html, r"(?is)<style(?:\s|>)")
        self.assertNotRegex(html, r"(?is)<script(?![^>]+\bsrc=)[^>]*>")

    def test_every_javascript_id_contract_exists_in_entry_page(self) -> None:
        html = (DESKTOP_ROOT / "index.html").read_text(encoding="utf-8")
        html_ids = set(re.findall(r'\bid="([^"]+)"', html))
        scripts = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (DESKTOP_ROOT / "js" / "state.js", DESKTOP_ROOT / "js" / "app.js")
        )
        referenced_ids = set(re.findall(r'\$\("([^"]+)"\)', scripts))
        self.assertEqual(referenced_ids - html_ids, set())

    def test_pyinstaller_spec_bundles_the_ui_tree(self) -> None:
        spec = (PROJECT_ROOT / "LanDrop.spec").read_text(encoding="utf-8")
        self.assertIn('project_root / "ui"', spec)
        self.assertIn('"ui"', spec)
        self.assertIn('project_root / "assets" / "LanDrop-icon-preview.png"', spec)
        self.assertIn('project_root / "assets" / "LanDrop-tray.ico"', spec)

        requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("qrcode[pil]==8.2", requirements)
        gui_source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertIn('version("qrcode")', gui_source)
        self.assertIn("qr_png_data_uri", gui_source)

        tray_source = (PROJECT_ROOT / "landrop" / "tray.py").read_text(encoding="utf-8")
        self.assertIn('resource_path("assets/LanDrop-tray.ico")', tray_source)
        self.assertNotIn('resource_path("assets/LanDrop.ico")', tray_source)

    def test_product_icon_assets_have_transparent_rounded_corners(self) -> None:
        for name in ("LanDrop.ico", "LanDrop-icon-preview.png"):
            with Image.open(PROJECT_ROOT / "assets" / name) as icon:
                alpha = icon.convert("RGBA").getchannel("A")
                self.assertEqual(alpha.getpixel((0, 0)), 0, name)
                self.assertEqual(alpha.getextrema()[1], 255, name)

    def test_setup_and_uninstall_mockups_reuse_external_wizard_assets(self) -> None:
        expected = (
            PROJECT_ROOT / "ui" / "wizard" / "css" / "wizard.css",
            PROJECT_ROOT / "ui" / "wizard" / "js" / "wizard.js",
            PROJECT_ROOT / "ui" / "setup" / "js" / "setup.js",
            PROJECT_ROOT / "ui" / "uninstall" / "js" / "uninstall.js",
        )
        self.assertTrue(all(path.is_file() for path in expected))
        for page_name in ("setup", "uninstall"):
            html = (PROJECT_ROOT / "ui" / page_name / "index.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("../wizard/css/wizard.css", html)
            self.assertIn("../wizard/js/wizard.js", html)
            self.assertNotRegex(html, r"(?is)<style(?:\s|>)")
            self.assertNotRegex(html, r"(?is)<script(?![^>]+\bsrc=)[^>]*>")

    def test_all_three_interfaces_share_source_han_and_dark_color_tokens(self) -> None:
        tokens = (DESKTOP_ROOT / "css" / "tokens.css").read_text(encoding="utf-8")
        self.assertIn('"Source Han Sans SC"', tokens)
        self.assertIn("@media (prefers-color-scheme: dark)", tokens)
        self.assertNotIn("#000000", tokens.lower())

        desktop = (DESKTOP_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('<nav class="rail-settings"', desktop)
        self.assertIn('id="transferSettingsPage"', desktop)
        self.assertIn('id="trustedClientsPage"', desktop)
        self.assertNotIn('id="openTransferSettings"', desktop)
        self.assertNotIn('id="localUrl"', desktop)
        self.assertNotIn('id="open"', desktop)
        self.assertIn('id="pairingQr"', desktop)
        self.assertIn("扫码连接（首选）", desktop)
        self.assertIn('id="settingsPreviewNetwork"', desktop)
        self.assertIn('src="../../assets/LanDrop-icon-preview.png"', desktop)
        self.assertNotIn("collapsible-panel", desktop)
        self.assertNotIn('class="rail-foot"', desktop)
        self.assertNotIn('id="returnMain"', desktop)
        self.assertNotIn('class="safety-note"', desktop)

        app_script = (DESKTOP_ROOT / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn("history.pushState", app_script)
        self.assertIn("history.replaceState", app_script)
        self.assertIn('landropIndex: 1', app_script)
        self.assertIn('window.addEventListener("popstate"', app_script)
        self.assertIn("history.back()", app_script)
        self.assertNotIn("open_transfer_page", app_script)

        gui_source = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertNotIn("open_transfer_page", gui_source)
        self.assertNotIn("import webbrowser", gui_source)

        for page_name in ("setup", "uninstall"):
            html = (PROJECT_ROOT / "ui" / page_name / "index.html").read_text(
                encoding="utf-8"
            )
            self.assertIn("../desktop/css/tokens.css", html)
            self.assertIn("../shared/js/theme.js", html)
            self.assertIn('name="color-scheme" content="light dark"', html)


if __name__ == "__main__":
    unittest.main()
