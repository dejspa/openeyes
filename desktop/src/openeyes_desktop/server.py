"""MCP server — vision-first desktop controller.

Pure vision, no OS APIs. The AI sees the screen and uses mouse/keyboard.
Just like a human sitting at the computer.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import sys
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP, Image as MCPImage

from PIL import Image

from .desktop import DesktopController
from .vision import VisionPipeline, find_changed_region

mcp = FastMCP(
    "openeyes-desktop",
    instructions="""\
Vision-first desktop controller. Full computer control through screenshots and input simulation.
No OS APIs — pure vision, just like a human.

TOOLS:
- screenshot() — see the desktop with a clickable GRID overlay (labels like 'a0', 'b5', 'f12')
- click_label(label) — click a grid label from the screenshot. THE FASTEST way to click anything!
- navigate(url) — open a URL in the browser (handles everything: focus, address bar, typing, Enter)
- click_text(text, near, index) — click visible text on screen using OCR
- move(x, y) — move mouse and see zoomed view around cursor (for precise targeting)
- click(x, y) — quick click at coordinates (or omit x,y to click at current position after move)
- double_click(x, y) — double-click (or omit x,y for current position)
- right_click(x, y) — right-click (or omit x,y for current position)
- type_text(text, press_enter) — type text on the keyboard
- key(combo) — press keys: "Return", "ctrl+c", "alt+Tab", "super", "ctrl+shift+t"
- scroll(direction, x, y) — scroll up/down at position
- drag(x1, y1, x2, y2) — click-drag from one point to another

HOW IT WORKS:
- You see a screenshot of the desktop (~896px wide).
- Tick marks along the top and left edges at 200px intervals help you estimate coordinates.
- move() shows a ~400px zoomed crop around the cursor at native resolution — use it for precise clicks.
- Action tools (click, type, key, scroll, drag) return text-only feedback (fast).
- Only screenshot() and move() return images.

PRECISE CLICKING — use move() to aim:
- move(x, y) → see zoomed view with crosshair → adjust if needed → click() to confirm
- This is like a human: move cursor, look, adjust, click.
- A red crosshair in the zoom shows exactly where the cursor is.

FAST MODE — skip move() when precision isn't needed:
- click(x, y) → type_text("hello") → key("Return") → screenshot()
- Use this for large buttons, text fields, or known positions.

SMART CLICKING — use click_text() for text targets:
- click_text("Sign in") → finds and clicks "Sign in" button
- click_text("Close", near="Chrome") → clicks Close nearest to Chrome
- Best for: buttons, links, menu items, labels — anything with readable text.
- Falls back to numbered annotation if multiple matches.

STRATEGY GUIDE:
1. OPEN A WEBSITE: navigate("linkedin.com") → done in one call
2. OPEN AN APP: key("super") → type_text("firefox", press_enter=true) → screenshot()
3. PRECISE CLICK: move(x, y) → verify target in zoom → click() → screenshot()
4. QUICK CLICK: click(x, y) → screenshot()
5. TEXT CLICK: click_text("Submit") → screenshot()
6. TYPE IN A FIELD: click(x, y) → type_text("hello") → screenshot()
7. NAVIGATE MENUS: click menu → screenshot → move to item → click() → screenshot
8. MULTI-STEP: click → type → key("Tab") → type → key("Return") → screenshot()

RULES:
- Always start with screenshot() to see the current desktop state.
- Use move() for small targets (close buttons, icons, menu items).
- Use click(x,y) for large targets (text areas, big buttons).
- Use key("super") to open the app launcher/start menu.
- Use key("alt+F4") to close windows.
- Use key("ctrl+c")/key("ctrl+v") for copy/paste.
""",
)

_desktop: DesktopController | None = None
_vision: VisionPipeline | None = None
_grid_labels: dict[str, tuple[int, int]] = {}  # label -> (img_x, img_y)
_session = os.environ.get("OPENEYES_DESKTOP_SESSION", "default")
_OPENEYES_DIR = os.path.expanduser("~/.openeyes/desktop")
_HISTORY_DIR = os.path.expanduser(f"~/.openeyes/desktop/history/{_session}")


def _get_desktop() -> DesktopController:
    global _desktop
    if _desktop is None:
        _desktop = DesktopController()
    return _desktop


def _get_vision() -> VisionPipeline:
    global _vision
    if _vision is None:
        _vision = VisionPipeline()
    return _vision


def _scale_coords(x: int, y: int) -> tuple[int, int]:
    """Scale from screenshot coordinates to actual screen coordinates."""
    desktop = _get_desktop()
    vision = _get_vision()

    sw, sh = desktop.screen_size
    if sw == 0 or sh == 0:
        # Screen size unknown yet — take a screenshot first
        return x, y

    sx = sw / vision.actual_width
    sy = sh / vision.actual_height
    return int(x * sx), int(y * sy)


def _save_screenshot(jpeg_bytes: bytes):
    """Save screenshot to history and write latest for dashboard live view."""
    try:
        os.makedirs(_HISTORY_DIR, exist_ok=True)
        ts = datetime.now(timezone.utc)
        filename = f"{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        filepath = os.path.join(_HISTORY_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(jpeg_bytes)
        with open(os.path.join(_HISTORY_DIR, "index.jsonl"), "a") as f:
            f.write(json.dumps({
                "ts": ts.isoformat(),
                "file": filename,
                "url": "desktop://screen",
                "title": f"Desktop ({_session})" if _session != "default" else "Desktop",
                "session": _session,
            }) + "\n")
    except Exception:
        pass

    # Write latest screenshot + metadata for dashboard polling
    try:
        os.makedirs(_OPENEYES_DIR, exist_ok=True)
        with open(os.path.join(_OPENEYES_DIR, f"desktop-{_session}.jpg"), "wb") as f:
            f.write(jpeg_bytes)
        with open(os.path.join(_OPENEYES_DIR, f"desktop-{_session}.json"), "w") as f:
            json.dump({
                "session": _session,
                "ts": datetime.now(timezone.utc).isoformat(),
                "label": f"Desktop ({_session})" if _session != "default" else "Desktop",
            }, f)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def screenshot() -> list:
    """Take a screenshot with a clickable grid overlay.
    Each cell has a label like 'a0', 'b5', 'f12' etc.
    To click any spot: find the label nearest your target, then call click_label('b5').
    No coordinate guessing needed — just read the label text."""
    global _grid_labels
    desktop = _get_desktop()
    vision = _get_vision()
    err = desktop.check_tools()
    if err:
        return [f"ERROR: {err}\n\nInstall with: sudo apt install scrot xdotool"]

    png = await desktop.screenshot()
    img, _grid_labels = vision.process_with_grid(png)
    _save_screenshot(img)
    return [MCPImage(data=img, format="jpeg")]


@mcp.tool()
async def click_label(label: str) -> list:
    """Click on a grid label from the screenshot. Just read the label near your target and click it.
    Examples: click_label('a5'), click_label('d12'), click_label('g3')
    Labels are row letter (a-z) + column number (0-23). Instant, no coordinate math."""
    global _grid_labels
    label = label.lower().strip()
    if label not in _grid_labels:
        return [f"Label '{label}' not found. Take a screenshot() first, then use a label from the grid."]

    desktop = _get_desktop()
    ix, iy = _grid_labels[label]
    sx, sy = _scale_coords(ix, iy)
    await desktop.click(sx, sy)
    return [f"Clicked label '{label}' at screen position ({sx}, {sy})"]


@mcp.tool()
async def navigate(url: str) -> list:
    """Open a URL in the browser. Handles finding the browser, focusing the address bar, typing the URL, and pressing Enter.
    Works with any browser (Chrome, Firefox, Edge). Returns a screenshot after the page loads.

    Examples: navigate("linkedin.com"), navigate("google.com/search?q=hello")"""
    desktop = _get_desktop()
    vision = _get_vision()

    # Ensure URL has protocol for actual navigation
    display_url = url
    if not url.startswith("http"):
        url = "https://" + url

    # Ctrl+L focuses the address bar in any browser (universal shortcut)
    await desktop.key("ctrl+l")
    await asyncio.sleep(0.3)
    await desktop.type_text(display_url)
    await desktop.key("Return")
    await asyncio.sleep(3)

    # Take screenshot and return
    png = await desktop.screenshot()
    img = vision.process(png)
    _save_screenshot(img)
    return [MCPImage(data=img, format="jpeg"), f"Navigated to {url}"]


async def _verify_click(
    desktop: DesktopController,
    png_before: bytes,
    m: dict,
    label: str,
) -> str:
    """Click a match, verify the screen changed, retry with offsets if needed.

    Returns a status message describing what happened.
    """
    await desktop.click(m["cx"], m["cy"])
    await asyncio.sleep(0.5)
    png_after = await desktop.screenshot()

    img_before = Image.open(io.BytesIO(png_before))
    img_after = Image.open(io.BytesIO(png_after))
    diff_ratio, _ = find_changed_region(img_before, img_after)

    if diff_ratio >= 0.02:
        return label

    # Screen didn't change — try offset positions
    offsets = [
        (m["cx"], m["cy"] - m["h"] // 2, "above"),
        (m["cx"], m["cy"] + m["h"] // 2, "below"),
        (m["cx"] - m["w"] // 4, m["cy"], "left of"),
    ]
    for ox, oy, direction in offsets:
        await desktop.click(ox, oy)
        await asyncio.sleep(0.5)
        png_retry = await desktop.screenshot()
        img_retry = Image.open(io.BytesIO(png_retry))
        diff_ratio, _ = find_changed_region(img_before, img_retry)
        if diff_ratio >= 0.02:
            return f"{label} (retried {direction} text)"

    return f"{label} — clicked but screen didn't change"


@mcp.tool()
async def click_text(text: str, near: str = "", index: int = 0) -> list:
    """Click on visible text on the screen. Uses OCR to find text — no coordinate guessing needed.

    - click_text("Submit") — clicks the text "Submit" (must be unique on screen)
    - click_text("Close", near="Settings") — clicks "Close" nearest to "Settings"
    - click_text("OK", index=2) — clicks the 2nd "OK" on screen

    If multiple matches and no disambiguator, returns an annotated screenshot showing all matches.
    Verifies the click worked by checking if the screen changed, and retries with offsets if needed."""
    desktop = _get_desktop()
    vision = _get_vision()

    png = await desktop.screenshot()
    matches = vision.find_text(png, text)

    if not matches:
        return [f"Text '{text}' not found on screen. Try screenshot() to see what's visible."]

    # Single match — click it directly with verification
    if len(matches) == 1:
        m = matches[0]
        msg = await _verify_click(
            desktop, png, m,
            f"Clicked text '{m['text']}' at ({m['cx']}, {m['cy']})",
        )
        return [msg]

    # Multiple matches — disambiguate
    if near:
        # Find the reference text and pick the closest match to it
        ref_matches = vision.find_text(png, near)
        if ref_matches:
            ref = ref_matches[0]
            rx, ry = ref["cx"], ref["cy"]
            best = min(
                matches,
                key=lambda m: math.hypot(m["cx"] - rx, m["cy"] - ry),
            )
            msg = await _verify_click(
                desktop, png, best,
                f"Found {len(matches)} matches for '{text}'. "
                f"Clicked the one nearest '{near}': '{best['text']}' at ({best['cx']}, {best['cy']})",
            )
            return [msg]
        # near text not found — fall through to annotation
        return [
            f"Found {len(matches)} matches for '{text}' but reference text '{near}' not found. "
            f"Try click_text(\"{text}\", index=N) with one of the numbers below.",
        ]

    if index > 0:
        if index > len(matches):
            return [f"Index {index} out of range — only {len(matches)} matches found for '{text}'."]
        m = matches[index - 1]
        msg = await _verify_click(
            desktop, png, m,
            f"Clicked match #{index}: '{m['text']}' at ({m['cx']}, {m['cy']})",
        )
        return [msg]

    # No disambiguator — show annotated screenshot with all matches
    annotated = vision.annotate_matches(png, matches)
    _save_screenshot(annotated)
    listing = "\n".join(
        f"  {i}. '{m['text']}' at ({m['cx']}, {m['cy']})"
        for i, m in enumerate(matches, 1)
    )
    return [
        MCPImage(data=annotated, format="jpeg"),
        f"Found {len(matches)} matches for '{text}':\n{listing}\n\n"
        f"Call click_text(\"{text}\", index=N) to click the one you want.",
    ]


@mcp.tool()
async def move(x: int, y: int) -> list:
    """Move the mouse to (x, y) and see a zoomed view around the cursor.
    Use this to verify position before clicking. Returns a ~400x400 crop at native resolution.
    After verifying, call click() to click at the current position."""
    desktop = _get_desktop()
    vision = _get_vision()
    sx, sy = _scale_coords(x, y)
    await desktop.move_mouse(sx, sy)
    png = await desktop.screenshot()
    full_img = vision.process(png)
    _save_screenshot(full_img)
    crop_img = vision.cursor_crop(png, sx, sy)
    return [
        MCPImage(data=full_img, format="jpeg"),
        MCPImage(data=crop_img, format="jpeg"),
        f"Moved to ({x}, {y}) — zoomed crop shows cursor area. Call click() to click here.",
    ]


@mcp.tool()
async def click(x: int = -1, y: int = -1) -> list:
    """Left-click at (x, y) on the screenshot, or omit coordinates to click at current position.
    The screenshot is ~896px wide. Tick marks at 200px intervals help you estimate position.
    Use screenshot() after if you need to see the result."""
    desktop = _get_desktop()
    if x == -1 and y == -1:
        await desktop.click_here()
        return ["Clicked at current position"]
    sx, sy = _scale_coords(x, y)
    await desktop.click(sx, sy)
    return [f"Clicked ({x}, {y})"]


@mcp.tool()
async def double_click(x: int = -1, y: int = -1) -> list:
    """Double-click at (x, y), or omit coordinates for current position.
    Use for opening files, selecting words, etc.
    Use screenshot() after if you need to see the result."""
    desktop = _get_desktop()
    if x == -1 and y == -1:
        await desktop.click_here()
        await asyncio.sleep(0.06)
        await desktop.click_here()
        return ["Double-clicked at current position"]
    sx, sy = _scale_coords(x, y)
    await desktop.double_click(sx, sy)
    return [f"Double-clicked ({x}, {y})"]


@mcp.tool()
async def right_click(x: int = -1, y: int = -1) -> list:
    """Right-click at (x, y), or omit coordinates for current position. Opens context menus.
    Use screenshot() after to see the menu."""
    desktop = _get_desktop()
    if x == -1 and y == -1:
        await desktop.click_here(button=3)
        return ["Right-clicked at current position"]
    sx, sy = _scale_coords(x, y)
    await desktop.right_click(sx, sy)
    return [f"Right-clicked ({x}, {y})"]


@mcp.tool()
async def type_text(text: str, press_enter: bool = False) -> list:
    """Type text on the keyboard. Set press_enter=true to press Enter after typing.
    Click a text field first to focus it before typing.
    Returns text-only feedback (fast). Use screenshot() to verify."""
    desktop = _get_desktop()
    await desktop.type_text(text)
    if press_enter:
        await asyncio.sleep(0.05)
        await desktop.key("Return")
    msg = f"Typed: '{text}'"
    if press_enter:
        msg += " + Enter"
    return [msg]


@mcp.tool()
async def key(combo: str) -> list:
    """Press a key or key combination. Returns text-only feedback (fast).
    Examples: 'Return', 'Escape', 'Tab', 'BackSpace', 'Delete',
              'ctrl+c', 'ctrl+v', 'ctrl+z', 'alt+Tab', 'alt+F4',
              'super' (opens app launcher), 'ctrl+shift+t', 'space'
    Use screenshot() after if you need to see the result."""
    desktop = _get_desktop()
    await desktop.key(combo)
    return [f"Pressed: {combo}"]


@mcp.tool()
async def scroll(direction: str = "down", x: int = 0, y: int = 0) -> list:
    """Scroll up or down. Optionally at a specific position (x, y).
    If x and y are 0, scrolls at current mouse position.
    Use screenshot() after to see the result."""
    desktop = _get_desktop()
    if x > 0 or y > 0:
        sx, sy = _scale_coords(x, y)
    else:
        sx, sy = 0, 0
    await desktop.scroll(direction, sx, sy)
    return [f"Scrolled {direction}"]


@mcp.tool()
async def drag(x1: int, y1: int, x2: int, y2: int) -> list:
    """Drag from (x1, y1) to (x2, y2). For moving windows, selecting text, resizing, etc.
    Use screenshot() after to see the result."""
    desktop = _get_desktop()
    sx1, sy1 = _scale_coords(x1, y1)
    sx2, sy2 = _scale_coords(x2, y2)
    await desktop.drag(sx1, sy1, sx2, sy2)
    return [f"Dragged ({x1},{y1}) -> ({x2},{y2})"]


def main():
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport != "stdio":
        mcp.settings.port = int(os.environ.get("FASTMCP_PORT", "6091"))
        mcp.settings.host = "0.0.0.0"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
