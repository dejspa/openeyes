"""MCP server — vision-first browser controller. Clean screenshots + coordinate-based clicking."""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server.fastmcp import FastMCP, Image as MCPImage

from .browser import BrowserManager
from .tracker import PageMemory
from .vision import VisionPipeline

mcp = FastMCP(
    "viewport",
    instructions="""\
Vision-first web browser for navigating websites.

TOOLS:
- navigate(url) — go to a URL in the current tab
- click(x, y) — click at pixel coordinates on the screenshot (auto-snaps to nearest element)
- type_text(text, press_enter, clear_first) — type into focused element
- scroll(direction) — scroll up/down
- get_text() — extract page text (article content, product details, prices)
- go_back() — browser back
- screenshot() — fresh screenshot
- new_tab(url) — open a new tab (keeps existing tabs open)
- switch_tab(index) — switch to a tab by index
- list_tabs() — show all open tabs
- close_tab(index) — close a tab

HOW CLICKING WORKS:
- Look at the screenshot and estimate the (x, y) pixel coordinates of what you want to click.
- The screenshot is ~896 pixels wide and ~630 pixels tall.
- Subtle tick marks along the top and left edges at 200px intervals help you gauge position.
- Your click is automatically snapped to the nearest interactive element (button, link, input).
- After each click you'll see feedback like "Clicked: <button> 'Add to cart'" confirming what was hit.
- To type into a field: click its coordinates first (to focus it), then use type_text().
- To search: click the search field, then type_text(query, press_enter=true, clear_first=true).
- Some actions return text-only feedback (no screenshot) when the page didn't visually change. Use screenshot() if you need to see the current state.

IMPORTANT BEHAVIORS:
- If a cookie banner, ad interstitial, or overlay blocks the page, click its accept/dismiss/close button.
- Popup tabs (ads, new windows) are auto-closed.

STRATEGY GUIDE — follow these patterns for best results:

1. SEARCH & ADD (e.g. "add product X to cart"):
   navigate → click search field → type_text(query, press_enter=true, clear_first=true) → screenshot → click(x, y) on the "add" button.

2. COMPARE & PICK (e.g. "find the cheapest X"):
   navigate → click search → type_text(query) → get_text (read ALL names and prices) → screenshot → click.
   ALWAYS use get_text first to read prices — don't guess prices from screenshots.

3. RESEARCH (e.g. "find info about X"):
   navigate → screenshot → get_text → report.
   Use get_text for article content — don't read long text from screenshots.

4. BROWSE FEED (e.g. "scroll through feed, find articles about X"):
   screenshot → scroll → screenshot → scroll (repeat). Use get_text on interesting items.

PRODUCT SELECTION — think like a human:
- "fryst lax" means salmon fillets, NOT salmon burgers or salmon sausage.
- "potatis" means whole potatoes, NOT potato chips or potato salad.
- "mjölk" means regular milk, NOT oat milk or flavored milk.
- Always prefer the product that matches the NATURAL human intent, not just keyword matches.
- When comparing: first filter to products that genuinely match the request, THEN pick cheapest among those.

RULES:
- Be efficient — never repeat the same action twice.
- Don't scroll unnecessarily — check what's already visible first.
- Don't open product detail modals when the info is already on the product card.
- If an overlay or popup blocks you, take a new screenshot — it may have been auto-dismissed.
""",
)

# Shared state — lazily initialized, persists across tool calls
CDP_PORT = int(os.environ.get("VIEWPORT_CDP_PORT", "9222"))

_browser: BrowserManager | None = None
_vision: VisionPipeline | None = None
_memory: PageMemory | None = None
_page_tokens: dict[int, int] = {}  # id(page) -> cumulative tokens
_current_model: str = os.environ.get("VIEWPORT_MODEL", "unknown")
_session = os.environ.get("VIEWPORT_SESSION", "default")
_TOKEN_FILE = f"/tmp/viewport-tokens-{_session}.json"
_TOKEN_LOG = os.path.expanduser("~/.viewport/token-log.jsonl")


def _track(response: list) -> list:
    """Estimate tokens in response and record for the active tab."""
    tokens = 0
    for part in response:
        if isinstance(part, MCPImage):
            tokens += 1500  # ~896x630 JPEG ≈ 1500 Claude vision tokens
        elif isinstance(part, str):
            tokens += max(1, len(part) // 4)
    if _browser and _browser._pages:
        pid = id(_browser._pages[_browser._active])
        _page_tokens[pid] = _page_tokens.get(pid, 0) + tokens
        _write_token_stats()
        _append_token_log(_browser._pages[_browser._active].url, tokens)
    return response


def _write_token_stats():
    """Write token stats to shared file for dashboard to read."""
    import json
    stats = [
        {"url": page.url, "tokens": _page_tokens.get(id(page), 0), "model": _current_model, "session": _session}
        for page in _browser._pages
    ]
    try:
        with open(_TOKEN_FILE, "w") as f:
            json.dump(stats, f)
    except Exception:
        pass


def _append_token_log(url: str, tokens: int):
    """Append token usage to persistent log for historical tracking."""
    import json
    from datetime import datetime, timezone
    try:
        os.makedirs(os.path.dirname(_TOKEN_LOG), exist_ok=True)
        with open(_TOKEN_LOG, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "url": url,
                "tokens": tokens,
                "model": _current_model,
                "session": _session,
            }) + "\n")
    except Exception:
        pass


def get_token_stats() -> list[dict]:
    """Returns [{url, tokens, session}, ...] for all sessions. Reads all token files."""
    import glob
    import json
    result = []
    for path in glob.glob("/tmp/viewport-tokens-*.json"):
        try:
            with open(path) as f:
                result.extend(json.load(f))
        except Exception:
            pass
    return result


async def _get_browser() -> BrowserManager:
    global _browser
    if _browser is None:
        _browser = BrowserManager(cdp_port=CDP_PORT)
    return _browser


def _get_vision() -> VisionPipeline:
    global _vision
    if _vision is None:
        _vision = VisionPipeline()
    return _vision


def _get_memory() -> PageMemory:
    global _memory
    if _memory is None:
        _memory = PageMemory()
    return _memory


async def _capture() -> tuple[bytes, bytes | None, str, float]:
    """Take screenshot, process it.

    Returns (jpeg_bytes, crop_jpeg_or_none, context, diff_ratio).
    """
    browser = await _get_browser()
    vision = _get_vision()
    memory = _get_memory()

    png = await browser.screenshot_bytes()
    diff_ratio, crop_jpeg = vision.get_change_info(png)
    jpeg_bytes = vision.process(png)

    url = browser.current_url
    title = await browser.get_page_title()
    context = memory.update(url, title, diff_ratio)

    return jpeg_bytes, crop_jpeg, context, diff_ratio


def _build_response(img: bytes, crop: bytes | None, context: str,
                    extra: str = "", diff_ratio: float = 1.0) -> list:
    """Build a tool response — smart about what images to include.

    - Major change (diff > 0.3) or first load: full screenshot
    - Minor change (0.05-0.3): crop only (saves tokens)
    - No change (< 0.02): text only (saves all image tokens)
    """
    parts = []
    if context:
        parts.append(context)
    if extra:
        parts.append(extra)
    text = "\n\n".join(p for p in parts if p)

    # Decide which images to include based on how much changed
    if diff_ratio < 0.02:
        # Nothing changed — text-only response
        return [text] if text else ["Page unchanged"]

    if crop and 0.05 < diff_ratio < 0.3:
        # Minor change — send only the crop (smaller = fewer tokens)
        result = [MCPImage(data=crop, format="jpeg")]
        if text:
            result.append(text + "\n[Showing only the changed area. Use screenshot() for full page.]")
        return result

    # Major change or first load — send full screenshot
    result = [MCPImage(data=img, format="jpeg")]
    if text:
        result.append(text)
    return result


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Model pricing lookup for cost tracking
_MODEL_RATES: dict[str, float] = {
    "haiku": 0.8, "haiku-4.5": 0.8, "claude-haiku-4-5": 0.8,
    "sonnet": 3, "sonnet-4.5": 3, "sonnet-4.6": 3, "claude-sonnet-4-5": 3, "claude-sonnet-4-6": 3,
    "opus": 15, "opus-4": 15, "opus-4.6": 15, "claude-opus-4-6": 15,
    "gpt-4o": 2.5, "gpt-4o-mini": 0.15, "gemini-2.5-pro": 1.25,
}


@mcp.tool()
async def set_model(model: str) -> str:
    """Tell viewport which AI model is using it, for accurate cost tracking.
    Call this once at the start of your session.
    Examples: set_model("sonnet-4.5"), set_model("haiku"), set_model("opus")"""
    global _current_model
    _current_model = model.lower().strip()
    rate = _MODEL_RATES.get(_current_model, None)
    if rate:
        return f"Model set to '{_current_model}' (${rate}/M input tokens)"
    return f"Model set to '{_current_model}' (unknown pricing — add rate via dashboard)"


@mcp.tool()
async def navigate(url: str) -> list:
    """Navigate to a URL. If a tab with that domain is already open, switches to it
    instead of navigating away. Use full URLs (with path) to navigate in the current tab.
    Examples: navigate("linkedin") → switches to LinkedIn tab. navigate("https://di.se/article/...") → opens in current tab."""
    browser = await _get_browser()
    status = await browser.navigate(url)
    img, crop, context, _ = await _capture()
    title = await browser.get_page_title()
    result = [MCPImage(data=img, format="jpeg")]
    tabs = browser.list_tabs()
    tab_info = " | ".join(f"[{t['index']}{'*' if t['active'] else ''}]{' 📌'+t['pin'] if t['pin'] else ''} {t['url'][:30]}" for t in tabs)
    text = f"{status}\n{context}\n\nURL: {browser.current_url}\nTitle: {title}\nTabs: {tab_info}"
    result.append(text)
    return _track(result)


@mcp.tool()
async def click(x: int, y: int) -> list:
    """Click at (x, y) coordinates on the screenshot.
    Look at the screenshot and estimate the pixel position of the element you want to click.
    Your click is automatically snapped to the nearest interactive element (button, link, input).
    The screenshot is ~896px wide and ~630px tall, with tick marks at 200px intervals."""
    browser = await _get_browser()
    vision = _get_vision()

    x = max(0, min(x, vision.actual_width - 1))
    y = max(0, min(y, vision.actual_height - 1))

    vw, vh = browser.viewport_size
    sx = vw / vision.actual_width
    sy = vh / vision.actual_height
    vx, vy = int(x * sx), int(y * sy)

    result = await browser.click_at_point(vx, vy)

    if result["found"]:
        desc = f"Clicked: <{result['tag']}>"
        if result.get("type"):
            desc += f" type={result['type']}"
        if result.get("text"):
            desc += f" '{result['text']}'"
        if result.get("method") == "nearby":
            desc += f" (snapped {result.get('radius', '?')}px)"
    else:
        desc = f"No interactive element at ({x}, {y}) — raw click performed"

    img, crop, context, diff_ratio = await _capture()
    return _track(_build_response(img, crop, context,
                           f"{desc}\nURL: {browser.current_url}",
                           diff_ratio))


@mcp.tool()
async def type_text(text: str, press_enter: bool = False, clear_first: bool = False) -> list:
    """Type text into the currently focused element.
    Set clear_first=true to select-all and replace existing text.
    Set press_enter=true to submit (may navigate to new page)."""
    browser = await _get_browser()
    await browser.type_text(text, press_enter=press_enter, clear_first=clear_first)

    if press_enter:
        # Pressing enter may navigate — return screenshot
        img, crop, context, diff_ratio = await _capture()
        return _track(_build_response(img, crop, context,
                               f"Typed: '{text}' + Enter | URL: {browser.current_url}",
                               diff_ratio))

    # No enter — page barely changed. Text-only response saves ~800 tokens.
    return _track([f"Typed: '{text}' into focused element.\nURL: {browser.current_url}\n\nUse screenshot() to see the current page if needed."])


@mcp.tool()
async def scroll(direction: str = "down") -> list:
    """Scroll the page. Direction: 'up' or 'down'."""
    browser = await _get_browser()
    await browser.scroll(direction)

    img, crop, context, diff_ratio = await _capture()

    if diff_ratio < 0.02:
        # Nothing new appeared — probably at top/bottom of page
        return _track([f"Scrolled {direction} — no new content visible (may have reached the {'bottom' if direction == 'down' else 'top'}).\nURL: {browser.current_url}"])

    return _track(_build_response(img, crop, context,
                           f"Scrolled {direction} | URL: {browser.current_url}",
                           diff_ratio))


@mcp.tool()
async def get_text() -> str:
    """Extract the main text content of the current page (article body, headings, paragraphs).
    Use this to read articles, blog posts, or any page with text content.
    Returns plain text with markdown headings — much faster than reading from screenshots."""
    browser = await _get_browser()
    text = await browser.get_page_text()
    result = f"URL: {browser.current_url}\n\n{text}"
    _track([result])
    return result


@mcp.tool()
async def go_back() -> list:
    """Go back to the previous page."""
    browser = await _get_browser()
    await browser.back()
    img, crop, context, _ = await _capture()
    # Always full screenshot for navigation
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nWent back | URL: {browser.current_url}")
    return _track(result)


@mcp.tool()
async def screenshot() -> list:
    """Take a fresh screenshot of the current page."""
    browser = await _get_browser()
    img, _, context, _ = await _capture()
    # Always full screenshot when explicitly requested
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nURL: {browser.current_url}")
    return _track(result)


@mcp.tool()
async def new_tab(url: str = "about:blank", pin: str = "") -> list:
    """Open a new browser tab. Set pin="name" to make it findable by keyword
    and protect it from being closed. Example: new_tab("https://linkedin.com", pin="linkedin")"""
    browser = await _get_browser()
    index = await browser.new_tab(url, pin=pin)
    img, crop, context, _ = await _capture()
    tabs = browser.list_tabs()
    tab_info = "\n".join(f"  [{t['index']}] {'📌'+t['pin']+' ' if t['pin'] else ''}{'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
    result = [MCPImage(data=img, format="jpeg")]
    pin_msg = f" (pinned as '{pin}')" if pin else ""
    result.append(f"{context}\n\nOpened tab {index}{pin_msg} | URL: {browser.current_url}\n\nAll tabs:\n{tab_info}")
    return _track(result)


@mcp.tool()
async def switch_tab(index: int) -> list:
    """Switch to a different tab by index. Use list_tabs() to see available tabs."""
    browser = await _get_browser()
    await browser.switch_tab(index)
    img, crop, context, _ = await _capture()
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Switched to tab {index} | URL: {browser.current_url}")
    return _track(result)


@mcp.tool()
async def list_tabs() -> str:
    """List all open browser tabs."""
    browser = await _get_browser()
    tabs = browser.list_tabs()
    lines = [f"[{t['index']}] {'→ ' if t['active'] else '  '}{t['url']}" for t in tabs]
    result = f"{len(tabs)} open tabs:\n" + "\n".join(lines)
    _track([result])
    return result


@mcp.tool()
async def close_tab(index: int) -> list:
    """Close a tab by index. Cannot close pinned tabs or the last remaining tab."""
    browser = await _get_browser()
    error = await browser.close_tab(index)
    if error:
        return [f"Cannot close tab {index}: {error}"]
    img, crop, context, _ = await _capture()
    tabs = browser.list_tabs()
    tab_info = "\n".join(f"  [{t['index']}] {'📌'+t['pin']+' ' if t['pin'] else ''}{'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Closed tab {index}\n\nAll tabs:\n{tab_info}")
    return _track(result)


def _start_dashboard():
    """Start dashboard in background threads (non-blocking)."""
    import threading
    from .dashboard import _run_http, _ws_proxy, HTTP_PORT, WS_PORT
    import websockets

    http_thread = threading.Thread(target=_run_http, args=(HTTP_PORT,), daemon=True)
    http_thread.start()

    async def _run_ws():
        async with websockets.serve(_ws_proxy, '0.0.0.0', WS_PORT, max_size=10_000_000):
            await asyncio.Future()

    ws_thread = threading.Thread(
        target=lambda: asyncio.new_event_loop().run_until_complete(_run_ws()),
        daemon=True,
    )
    ws_thread.start()
    print(f"[viewport] Dashboard at http://localhost:{HTTP_PORT}", file=sys.stderr)


async def _warmup_browser():
    """Start browser immediately so CDP is ready for dashboard."""
    await _get_browser()


SSE_PORT = 6090


def main():
    import sys
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"

    if transport != "stdio":
        mcp.settings.port = int(os.environ.get("FASTMCP_PORT", SSE_PORT))
        mcp.settings.host = "0.0.0.0"

    # In server mode: start dashboard + browser automatically
    if transport == "serve":
        transport = "sse"
        mcp.settings.port = int(os.environ.get("FASTMCP_PORT", SSE_PORT))
        mcp.settings.host = "0.0.0.0"
        _start_dashboard()
        asyncio.get_event_loop().run_until_complete(_warmup_browser())
        print(f"[viewport] MCP server at http://localhost:{mcp.settings.port}/sse", file=sys.stderr)
        print("[viewport] Ready.", file=sys.stderr)

    mcp.run(transport=transport)


def serve():
    """All-in-one: MCP server + dashboard + browser. One command to run everything."""
    sys.argv = [sys.argv[0], "serve"]
    main()


if __name__ == "__main__":
    main()
