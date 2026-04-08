"""MCP server — vision-first desktop controller.

Pure vision, no OS APIs. The AI sees the screen and uses mouse/keyboard.
Just like a human sitting at the computer.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP, Image as MCPImage

from .desktop import DesktopController
from .vision import VisionPipeline

mcp = FastMCP(
    "desktop",
    instructions="""\
Vision-first desktop controller. Full computer control through screenshots and input simulation.
No OS APIs — pure vision, just like a human.

TOOLS:
- screenshot() — see the full desktop
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

STRATEGY GUIDE:
1. OPEN AN APP: key("super") → type_text("firefox", press_enter=true) → screenshot()
2. PRECISE CLICK: move(x, y) → verify target in zoom → click() → screenshot()
3. QUICK CLICK: click(x, y) → screenshot()
4. TYPE IN A FIELD: click(x, y) → type_text("hello") → screenshot()
5. NAVIGATE MENUS: click menu → screenshot → move to item → click() → screenshot
6. MULTI-STEP: click → type → key("Tab") → type → key("Return") → screenshot()

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


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def screenshot() -> list:
    """Take a screenshot of the entire desktop. Always call this first to see what's on screen."""
    desktop = _get_desktop()
    vision = _get_vision()
    err = desktop.check_tools()
    if err:
        return [f"ERROR: {err}\n\nInstall with: sudo apt install scrot xdotool"]

    png = await desktop.screenshot()
    img = vision.process(png)
    return [MCPImage(data=img, format="jpeg")]


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
