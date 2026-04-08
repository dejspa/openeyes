"""Vision pipeline — screenshot preprocessing, coordinate reference, diffing.

Identical approach to viewport_browser/vision.py but standalone
so desktop-nav has no dependency on Playwright/browser code.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont


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


def image_to_bytes(img: Image.Image, fmt: str = "JPEG", quality: int = 75) -> bytes:
    """Encode image to raw bytes."""
    buf = io.BytesIO()
    if fmt == "JPEG":
        img = img.convert("RGB")
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def find_changed_region(
    img_a: Image.Image,
    img_b: Image.Image,
    grid_size: int = 64,
    threshold: int = 30,
) -> tuple[float, dict | None]:
    """Compare two screenshots. Returns (diff_ratio, changed_bbox or None)."""
    if img_a.size != img_b.size:
        return 1.0, None

    a = img_a.convert("L")
    b = img_b.convert("L")
    w, h = a.size
    pixels_a = list(a.getdata())
    pixels_b = list(b.getdata())

    min_x, min_y = w, h
    max_x, max_y = 0, 0
    total_cells = 0
    changed_cells = 0

    for gy in range(0, h, grid_size):
        for gx in range(0, w, grid_size):
            total_cells += 1
            cell_changed = 0
            cell_total = 0
            for cy in range(gy, min(gy + grid_size, h)):
                for cx in range(gx, min(gx + grid_size, w)):
                    idx = cy * w + cx
                    cell_total += 1
                    if abs(pixels_a[idx] - pixels_b[idx]) > threshold:
                        cell_changed += 1
            if cell_changed > cell_total * 0.1:
                changed_cells += 1
                min_x = min(min_x, gx)
                min_y = min(min_y, gy)
                max_x = max(max_x, gx + grid_size)
                max_y = max(max_y, gy + grid_size)

    ratio = changed_cells / total_cells if total_cells > 0 else 1.0

    if changed_cells == 0:
        return 0.0, None

    bbox = {
        "x": min_x,
        "y": min_y,
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
    """Processes desktop screenshots for efficient LLM consumption.

    Downscales to max 896x672, JPEG quality 75.
    Adds coordinate tick marks along edges.
    Tracks changes between screenshots.
    """

    def __init__(self, max_width: int = 896, max_height: int = 672):
        self.max_width = max_width
        self.max_height = max_height
        self._last_screenshot: Image.Image | None = None
        self.actual_width: int = max_width
        self.actual_height: int = max_height

    def process(self, png_bytes: bytes) -> bytes:
        """Process a raw screenshot into a JPEG with coordinate reference.

        Returns JPEG bytes. Sets actual_width/actual_height for coordinate scaling.
        """
        img = Image.open(io.BytesIO(png_bytes))
        img.thumbnail((self.max_width, self.max_height), Image.Resampling.LANCZOS)
        self.actual_width = img.width
        self.actual_height = img.height

        img = overlay_coordinate_reference(img)
        return image_to_bytes(img)

    def get_change_info(self, png_bytes: bytes) -> tuple[float, bytes | None]:
        """Compare new screenshot against previous one.

        Returns (diff_ratio, cropped_jpeg_of_changed_region or None).
        """
        img = Image.open(io.BytesIO(png_bytes))
        img.thumbnail((self.max_width, self.max_height), Image.Resampling.LANCZOS)

        if self._last_screenshot is None:
            self._last_screenshot = img
            return 1.0, None

        ratio, bbox = find_changed_region(self._last_screenshot, img)
        self._last_screenshot = img

        if bbox and 0.05 < ratio < 0.7:
            cropped = crop_region(img, bbox, padding=50)
            return ratio, image_to_bytes(cropped)

        return ratio, None
