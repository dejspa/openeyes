"""Vision pipeline — screenshot preprocessing, coordinate reference, diffing."""

from __future__ import annotations

import io
import math
from PIL import Image, ImageChops, ImageDraw, ImageFont


_TICK_FONT: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None


def _get_tick_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _TICK_FONT
    if _TICK_FONT is None:
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            try:
                _TICK_FONT = ImageFont.truetype(path, 10)
                break
            except (OSError, IOError):
                continue
        if _TICK_FONT is None:
            _TICK_FONT = ImageFont.load_default()
    return _TICK_FONT


def overlay_coordinate_reference(img: Image.Image) -> Image.Image:
    """Draw subtle tick marks along top and left edges for spatial reference."""
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    font = _get_tick_font()
    fg = (160, 160, 160)
    bg = (40, 40, 40)

    for x in range(0, img.width, 200):
        draw.line([(x, 0), (x, 6)], fill=fg, width=1)
        if x > 0:
            label = str(x)
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((x + 2 + dx, dy), label, fill=bg, font=font)
            draw.text((x + 2, 0), label, fill=fg, font=font)

    for y in range(0, img.height, 200):
        draw.line([(0, y), (6, y)], fill=fg, width=1)
        if y > 0:
            label = str(y)
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((1 + dx, y + 1 + dy), label, fill=bg, font=font)
            draw.text((1, y + 1), label, fill=fg, font=font)

    return annotated


def image_to_bytes(img: Image.Image, format: str = "JPEG", quality: int = 55) -> bytes:
    """Encode image to raw bytes."""
    buf = io.BytesIO()
    if format == "JPEG":
        img = img.convert("RGB")
    img.save(buf, format=format, quality=quality)
    return buf.getvalue()


def estimate_image_tokens(jpeg_bytes: bytes) -> int:
    """Claude vision cost: ~(width * height) / 750 tokens for the actual image size."""
    try:
        with Image.open(io.BytesIO(jpeg_bytes)) as im:
            w, h = im.size
        return max(1, math.ceil(w * h / 750))
    except Exception:
        return 750


def find_changed_region(img_a: Image.Image, img_b: Image.Image,
                        grid_size: int = 64, threshold: int = 30) -> tuple[float, dict | None]:
    """Compare two screenshots using RGB channels. Returns (diff_ratio, changed_bbox or None).

    Uses max RGB channel difference instead of grayscale — catches color changes
    (e.g. red→green button) that grayscale would miss since they have similar luminance.
    Implemented with PIL C-level ops (ImageChops + BOX downscale) — ~4ms per call
    vs ~500ms for the equivalent per-pixel Python loop.
    """
    if img_a.size != img_b.size:
        return 1.0, None

    a = img_a.convert("RGB")
    b = img_b.convert("RGB")
    w, h = a.size

    diff = ImageChops.difference(a, b)
    r, g, bl = diff.split()
    max_diff = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    mask = max_diff.point(lambda v: 255 if v > threshold else 0)

    # BOX-resize of the 0/255 mask computes each grid cell's mean = 255 * fraction
    # of changed pixels. A cell counts as changed above 10%, matching the old loop.
    gw, gh = math.ceil(w / grid_size), math.ceil(h / grid_size)
    cells = list(mask.resize((gw, gh), Image.Resampling.BOX).getdata())

    min_x = min_y = 10 ** 9
    max_x = max_y = 0
    changed = 0
    for i, v in enumerate(cells):
        if v > 255 * 0.1:
            changed += 1
            gx, gy = (i % gw) * grid_size, (i // gw) * grid_size
            min_x = min(min_x, gx)
            min_y = min(min_y, gy)
            max_x = max(max_x, gx + grid_size)
            max_y = max(max_y, gy + grid_size)

    ratio = changed / len(cells) if cells else 1.0
    if changed == 0:
        return 0.0, None

    bbox = {
        "x": min_x, "y": min_y,
        "w": min(max_x, w) - min_x,
        "h": min(max_y, h) - min_y,
    }
    return ratio, bbox


def crop_region(img: Image.Image, bbox: dict, padding: int = 50) -> Image.Image:
    """Crop a region of interest with padding."""
    left = max(bbox["x"] - padding, 0)
    top = max(bbox["y"] - padding, 0)
    right = min(bbox["x"] + bbox["w"] + padding, img.width)
    bottom = min(bbox["y"] + bbox["h"] + padding, img.height)
    return img.crop((left, top, right, bottom))


class VisionPipeline:
    """Processes screenshots for efficient LLM consumption.

    One instance per session. Downscales to 896x672 max (typically 896x630 for a
    1280x900 viewport), JPEG quality 55, subtle coordinate tick marks along edges.
    Diff baselines are kept per tab (keyed by an opaque tab key) so switching tabs
    or running sessions concurrently never diffs against the wrong page.
    """

    def __init__(self, max_width: int = 896, max_height: int = 672):
        self.max_width = max_width
        self.max_height = max_height
        self._last: dict[int, Image.Image] = {}  # tab_key -> last downscaled screenshot
        self.actual_width: int | None = None
        self.actual_height: int | None = None

    def display_size(self, viewport_w: int, viewport_h: int) -> tuple[int, int]:
        """Size of the screenshot as the model sees it. Falls back to the thumbnail
        math when no screenshot has been processed yet (e.g. click before screenshot)."""
        if self.actual_width and self.actual_height:
            return self.actual_width, self.actual_height
        scale = min(self.max_width / viewport_w, self.max_height / viewport_h, 1.0)
        return round(viewport_w * scale), round(viewport_h * scale)

    def analyze(self, png_bytes: bytes, tab_key: int) -> tuple[bytes, bytes | None, float]:
        """Decode once, diff against this tab's previous screenshot, produce outputs.

        Returns (full_jpeg_with_ticks, crop_jpeg_of_changed_region_or_None, diff_ratio).
        """
        img = Image.open(io.BytesIO(png_bytes))
        img.thumbnail((self.max_width, self.max_height), Image.Resampling.LANCZOS)
        img = img.convert("RGB")
        self.actual_width = img.width
        self.actual_height = img.height

        prev = self._last.get(tab_key)
        self._last[tab_key] = img

        crop_jpeg = None
        if prev is None:
            ratio = 1.0
        else:
            ratio, bbox = find_changed_region(prev, img)
            # Generate a crop for any non-trivial change up to 70% of the page.
            # Even tiny changes (cart badges, button state flips, toasts) are
            # worth showing — that's the whole point of vision-first feedback.
            if bbox and 0.0 < ratio < 0.7:
                crop_jpeg = image_to_bytes(crop_region(img, bbox, padding=50))

        jpeg = image_to_bytes(overlay_coordinate_reference(img))
        return jpeg, crop_jpeg, ratio

    def forget(self, tab_key: int) -> None:
        """Drop the diff baseline for a closed tab."""
        self._last.pop(tab_key, None)

    def prune(self, live_keys: set[int]) -> None:
        """Drop baselines for tabs that no longer exist (each is ~1.7MB of RAM)."""
        for key in list(self._last):
            if key not in live_keys:
                del self._last[key]
