"""Vision pipeline — screenshot preprocessing, coordinate reference, diffing.

Identical approach to openeyes_web/vision.py but standalone
so OpenEyes Desktop has no dependency on Playwright/browser code.
"""

from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

try:
    import pytesseract
    _HAS_TESSERACT = True
except ImportError:
    _HAS_TESSERACT = False


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


_GRID_FONT: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None


def _get_grid_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _GRID_FONT
    if _GRID_FONT is None:
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            try:
                _GRID_FONT = ImageFont.truetype(path, 9)
                break
            except (OSError, IOError):
                continue
        if _GRID_FONT is None:
            _GRID_FONT = ImageFont.load_default()
    return _GRID_FONT


def overlay_grid_labels(
    img: Image.Image, cols: int = 24, rows: int = 12
) -> tuple[Image.Image, dict[str, tuple[int, int]]]:
    """Overlay a grid of unique labels on the screenshot.

    Each cell gets a label like "a0", "a1", ..., "b0", "b1", etc.
    Row = letter (a-z), Column = number (0-99).

    Returns (annotated_image, label_map) where label_map maps
    label string -> (center_x, center_y) in image pixel coordinates.
    """
    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    font = _get_grid_font()

    cell_w = img.width / cols
    cell_h = img.height / rows
    label_map: dict[str, tuple[int, int]] = {}

    for r in range(rows):
        for c in range(cols):
            label = f"{chr(97 + r)}{c}"
            cx = int(c * cell_w + cell_w / 2)
            cy = int(r * cell_h + cell_h / 2)
            label_map[label] = (cx, cy)

            # Draw label with dark outline for readability on any background
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                draw.text((cx - 7 + dx, cy - 5 + dy), label, fill=(0, 0, 0), font=font)
            draw.text((cx - 7, cy - 5), label, fill=(255, 80, 80), font=font)

    return annotated, label_map


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

    def process_with_grid(self, png_bytes: bytes, cols: int = 24, rows: int = 12) -> tuple[bytes, dict[str, tuple[int, int]]]:
        """Process screenshot with clickable grid labels instead of tick marks.

        Returns (jpeg_bytes, label_map) where label_map maps "a0" -> (cx, cy)
        in the downscaled image coordinates. Use with click_label().
        """
        img = Image.open(io.BytesIO(png_bytes))
        img.thumbnail((self.max_width, self.max_height), Image.Resampling.LANCZOS)
        self.actual_width = img.width
        self.actual_height = img.height

        img, label_map = overlay_grid_labels(img, cols, rows)
        return image_to_bytes(img), label_map

    def cursor_crop(self, png_bytes: bytes, cx: int, cy: int, size: int = 400) -> bytes:
        """Crop a region around (cx, cy) at native resolution with a red crosshair.

        Returns JPEG bytes of the cropped region.
        """
        img = Image.open(io.BytesIO(png_bytes))
        w, h = img.size

        # Clamp crop box to image bounds
        left = max(cx - size // 2, 0)
        top = max(cy - size // 2, 0)
        right = min(left + size, w)
        bottom = min(top + size, h)
        # Adjust if we hit the right/bottom edge
        if right - left < size:
            left = max(right - size, 0)
        if bottom - top < size:
            top = max(bottom - size, 0)

        crop = img.crop((left, top, right, bottom))

        # Draw red crosshair at the center of the crop
        draw = ImageDraw.Draw(crop)
        ccx = cx - left
        ccy = cy - top
        arm = 10
        draw.line([(ccx - arm, ccy), (ccx + arm, ccy)], fill=(255, 0, 0), width=2)
        draw.line([(ccx, ccy - arm), (ccx, ccy + arm)], fill=(255, 0, 0), width=2)

        return image_to_bytes(crop, fmt="JPEG", quality=85)

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

    # --- OCR text finding ---

    def _ocr_lines(self, png_bytes: bytes) -> dict[tuple, list[dict]]:
        """Run OCR and cache results. Returns lines grouped by (block, par, line)."""
        # Cache key: id of the bytes object
        cache_key = id(png_bytes)
        if hasattr(self, "_ocr_cache_key") and self._ocr_cache_key == cache_key:
            return self._ocr_cache

        img = Image.open(io.BytesIO(png_bytes))
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        lines: dict[tuple[int, int, int], list[dict]] = {}
        n_boxes = len(data["text"])
        for i in range(n_boxes):
            word = data["text"][i].strip()
            if not word:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append({
                "text": word,
                "x": data["left"][i],
                "y": data["top"][i],
                "w": data["width"][i],
                "h": data["height"][i],
            })

        self._ocr_cache_key = cache_key
        self._ocr_cache = lines
        return lines

    def find_text(self, png_bytes: bytes, query: str) -> list[dict]:
        """Find all occurrences of *query* (case-insensitive substring) on screen.

        Uses pytesseract OCR at native resolution.  Returns a list of match
        dicts: ``[{"text": str, "cx": int, "cy": int, "x": int, "y": int,
        "w": int, "h": int}]`` where cx/cy are center coordinates in native
        screen pixels.
        """
        if not _HAS_TESSERACT:
            print("[vision] pytesseract not installed — OCR disabled. "
                  "Install with: pip install pytesseract", flush=True)
            return []

        lines = self._ocr_lines(png_bytes)

        query_lower = query.lower()
        matches: list[dict] = []

        for _key, words in lines.items():
            line_text = " ".join(w["text"] for w in words)
            line_lower = line_text.lower()

            # Search for all occurrences of query within this line
            start = 0
            while True:
                idx = line_lower.find(query_lower, start)
                if idx == -1:
                    break

                # Map character offset back to word indices.  Walk the
                # joined string tracking cumulative character positions.
                char_pos = 0
                first_word_idx: int | None = None
                last_word_idx: int | None = None
                match_end = idx + len(query_lower)

                for wi, w in enumerate(words):
                    word_start = char_pos
                    word_end = char_pos + len(w["text"])
                    # Does this word overlap the match span?
                    if word_end > idx and word_start < match_end:
                        if first_word_idx is None:
                            first_word_idx = wi
                        last_word_idx = wi
                    char_pos = word_end + 1  # +1 for the joining space

                if first_word_idx is not None and last_word_idx is not None:
                    # Compute bounding box covering the matched words
                    mw = words[first_word_idx:last_word_idx + 1]
                    bx = min(w["x"] for w in mw)
                    by = min(w["y"] for w in mw)
                    bx2 = max(w["x"] + w["w"] for w in mw)
                    by2 = max(w["y"] + w["h"] for w in mw)
                    bw = bx2 - bx
                    bh = by2 - by
                    matches.append({
                        "text": line_text[idx:idx + len(query_lower)],
                        "cx": bx + bw // 2,
                        "cy": by + bh // 2,
                        "x": bx,
                        "y": by,
                        "w": bw,
                        "h": bh,
                    })

                start = idx + 1  # continue searching for more in this line

        return matches

    def annotate_matches(self, png_bytes: bytes, matches: list[dict]) -> bytes:
        """Draw numbered red badges at each match position and return JPEG.

        Opens the PNG at native resolution, draws badges, then downscales to
        the normal screenshot size and adds coordinate tick marks.
        """
        img = Image.open(io.BytesIO(png_bytes))

        draw = ImageDraw.Draw(img)

        # Choose badge radius and font size based on image size
        badge_r = max(16, img.width // 80)
        try:
            badge_font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                badge_r,
            )
        except (OSError, IOError):
            badge_font = ImageFont.load_default()

        for i, m in enumerate(matches, 1):
            cx, cy = m["cx"], m["cy"]

            # Draw red rectangle around match
            draw.rectangle(
                [m["x"] - 2, m["y"] - 2, m["x"] + m["w"] + 2, m["y"] + m["h"] + 2],
                outline=(255, 0, 0),
                width=2,
            )

            # Draw numbered red circle badge above-right of match
            bx = m["x"] + m["w"] + badge_r
            by = m["y"] - badge_r
            # Clamp to image bounds
            bx = max(badge_r, min(bx, img.width - badge_r))
            by = max(badge_r, min(by, img.height - badge_r))

            draw.ellipse(
                [bx - badge_r, by - badge_r, bx + badge_r, by + badge_r],
                fill=(220, 30, 30),
                outline=(255, 255, 255),
                width=2,
            )

            # Draw number centered in badge
            label = str(i)
            bbox = draw.textbbox((0, 0), label, font=badge_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            draw.text(
                (bx - tw // 2, by - th // 2 - 1),
                label,
                fill=(255, 255, 255),
                font=badge_font,
            )

        # Downscale to normal screenshot size
        img.thumbnail((self.max_width, self.max_height), Image.Resampling.LANCZOS)
        img = overlay_coordinate_reference(img)
        return image_to_bytes(img)
