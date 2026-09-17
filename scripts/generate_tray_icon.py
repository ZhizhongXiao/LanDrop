"""Build LanDrop's multi-resolution tray icon from its approved source art."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE_PATH = ASSETS / "LanDrop-icon-source.png"
ICON_PATH = ASSETS / "LanDrop.ico"
PREVIEW_PATH = ASSETS / "LanDrop-icon-preview.png"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def normalized_source(size: int = 512) -> Image.Image:
    """Crop transparent margins, then centre the mark with tray-safe padding."""
    with Image.open(SOURCE_PATH) as opened:
        source = opened.convert("RGBA")
    # Generated antialiasing can leave nearly invisible alpha noise around the
    # canvas. Ignore it so the visible mark, not the noise, determines scaling.
    visible_alpha = source.getchannel("A").point(
        lambda value: 255 if value >= 24 else 0
    )
    alpha_bounds = visible_alpha.getbbox()
    if alpha_bounds is None:
        raise ValueError(f"图标源图为空：{SOURCE_PATH}")
    source = source.crop(alpha_bounds)

    # Ten-percent padding keeps the outer broadcast curve clear of the tray
    # cell boundary after Windows scales the icon down.
    usable = round(size * 0.80)
    source.thumbnail((usable, usable), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        source,
        ((size - source.width) // 2, (size - source.height) // 2),
    )
    return canvas


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    master = normalized_source()
    master.save(PREVIEW_PATH)
    master.save(ICON_PATH, sizes=[(size, size) for size in ICON_SIZES])
    print(ICON_PATH)
    print(PREVIEW_PATH)


if __name__ == "__main__":
    main()
