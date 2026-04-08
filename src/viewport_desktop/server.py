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
- screenshot() — see the current state of the desktop
- click(x, y) — left-click at coordinates
- double_click(x, y) — double-click at coordinates
- right_click(x, y) — right-click for context menu
- type_text(text, press_enter) — type text on the keyboard
- key(combo) — press keys: "Return", "ctrl+c", "alt+Tab", "super", "ctrl+shift+t"
- scroll(direction, x, y) — scroll up/down at position
- drag(x1, y1, x2, y2) — click-drag from one point to another

HOW IT WORKS:
- You see a screenshot of the desktop (~896px wide).
- Tick marks along the top and left edges at 200px intervals help you estimate coordinates.
- Action tools (click, type, key, scroll, drag) are FAST — they return text-only feedback.
- Only screenshot() returns an image. Call it when you need to see the screen.
- There is NO snap-to-element. Your coordinates must be accurate.
- There is NO get_text. Read text from the screenshot.

SPEED — BE FAST:
- Batch multiple actions before taking a screenshot. Don't screenshot after every action.
- Example: click(x, y) → type_text("hello") → key("Return") → screenshot()
  That's 3 fast actions + 1 screenshot, instead of 3 slow screenshot round-trips.
- Only screenshot when you need to SEE something (verify result, find coordinates, read text).

STRATEGY GUIDE:
1. OPEN AN APP: key("super") → type_text("firefox", press_enter=true) → screenshot()
2. CLICK A BUTTON: click(x, y) → screenshot() to verify
3. TYPE IN A FIELD: click(x, y) → type_text("hello") → screenshot() to verify
4. NAVIGATE MENUS: click menu → screenshot → click menu item → screenshot
5. SWITCH WINDOWS: key("alt+Tab") → screenshot()
6. MULTI-STEP: click → type → key("Tab") → type → key("Return") → screenshot()

RULES:
- Always start with screenshot() to see the current desktop state.
- Be precise with coordinates — there is no auto-snap.
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
    err = desktop.check_tools()
    if err:
        return [f"ERROR: {err}\n\nInstall with: sudo apt install scrot xdotool"]

    img, _, _ = await _capture()
    return [MCPImage(data=img, format="jpeg")]


@mcp.tool()
async def click(x: int, y: int) -> list:
    """Left-click at (x, y) on the screenshot. Returns text-only feedback (fast).
    The screenshot is ~896px wide. Tick marks at 200px intervals help you estimate position.
    There is no auto-snap — aim for the center of what you want to click.
    Use screenshot() after if you need to see the result."""
    desktop = _get_desktop()
    sx, sy = _scale_coords(x, y)
    await desktop.click(sx, sy)
    return [f"Clicked ({x}, {y})"]


@mcp.tool()
async def double_click(x: int, y: int) -> list:
    """Double-click at (x, y). Use for opening files, selecting words, etc.
    Use screenshot() after if you need to see the result."""
    desktop = _get_desktop()
    sx, sy = _scale_coords(x, y)
    await desktop.double_click(sx, sy)
    return [f"Double-clicked ({x}, {y})"]


@mcp.tool()
async def right_click(x: int, y: int) -> list:
    """Right-click at (x, y). Opens context menus.
    Use screenshot() after to see the menu."""
    desktop = _get_desktop()
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
