"""Build LanDrop's multi-resolution product icon from its approved source art."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
SOURCE_PATH = ASSETS / "LanDrop-icon-source.png"
ICON_PATH = ASSETS / "LanDrop.ico"
PREVIEW_PATH = ASSETS / "LanDrop-icon-preview.png"
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _remove_black_matte(source: Image.Image) -> Image.Image:
    """Recover transparency from the black matte around the rounded tile."""
    rgba = source.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            red, green, blue, _alpha = pixels[x, y]
            peak = max(red, green, blue)
            if peak <= 3:
                pixels[x, y] = (0, 0, 0, 0)
                continue
            if peak >= 224:
                pixels[x, y] = (red, green, blue, 255)
                continue

            alpha = round((peak - 3) * 255 / 221)
            pixels[x, y] = (
                min(255, round(red * 255 / alpha)),
                min(255, round(green * 255 / alpha)),
                min(255, round(blue * 255 / alpha)),
                alpha,
            )
    return rgba


def normalized_source(size: int = 512) -> Image.Image:
    """Crop the recovered transparent matte and fill the product-icon canvas."""
    with Image.open(SOURCE_PATH) as opened:
        source = _remove_black_matte(opened)
    # Generated antialiasing can leave nearly invisible alpha noise around the
    # canvas. Ignore it so the visible mark, not the noise, determines scaling.
    visible_alpha = source.getchannel("A").point(
        lambda value: 255 if value >= 24 else 0
    )
    alpha_bounds = visible_alpha.getbbox()
    if alpha_bounds is None:
        raise ValueError(f"图标源图为空：{SOURCE_PATH}")
    source = source.crop(alpha_bounds)

    # The approved source is already a rounded product tile. Additional outer
    # padding makes the taskbar icon undersized and exposes a second blue frame
    # behind the desktop brand button, so the tile should fill this canvas.
    source.thumbnail((size, size), Image.Resampling.LANCZOS)
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
