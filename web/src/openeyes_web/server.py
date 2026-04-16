"""MCP server — vision-first browser controller. Clean screenshots + coordinate-based clicking.

Multi-session isolation: each MCP client gets its own BrowserManager on a dedicated
CDP port. Session ID comes from ctx.client_id (SSE) or OPENEYES_WEB_SESSION env var (stdio).
Sessions inactive > 48h are auto-cleaned (Chrome killed, state kept).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from mcp.server.fastmcp import FastMCP, Context, Image as MCPImage

from .browser import BrowserManager
from .tracker import PageMemory
from .vision import VisionPipeline

mcp = FastMCP(
    "openeyes-web",
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
- new_tab(url) — open a tab for a different site (keeps existing tabs open; reuses the tab if that domain is already open)
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

5. PARALLEL WORK (e.g. "compare X on site A vs site B", "research multiple topics",
   "keep gp.se open while also checking willys.se"):
   new_tab("https://a.com") → work there → new_tab("https://b.com") → work there →
   switch_tab(0) to return to the first. Tabs persist across calls — open as many as
   you need, one per site/topic. Use list_tabs() to see what's already open.

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
- Use new_tab whenever you start work on a different site or topic — don't navigate away
  from a useful tab. You can have many tabs open simultaneously and switch between them.
""",
)

# ---------------------------------------------------------------------------
# Session state — per-client isolation
# ---------------------------------------------------------------------------

_BASE_CDP_PORT = int(os.environ.get("OPENEYES_WEB_CDP_PORT", "9222"))
_TTL_SECONDS = 48 * 3600
_CLEANUP_INTERVAL = 1800  # 30 min
_SESSION_FILE = "/tmp/openeyes-web-sessions.json"

_browsers: dict[str, BrowserManager] = {}
_vision: VisionPipeline | None = None
_memory: PageMemory | None = None
_page_tokens: dict[int, int] = {}  # id(page) -> cumulative tokens
_current_model: str = os.environ.get("OPENEYES_WEB_MODEL", "unknown")
_session_ports: dict[str, int] = {}  # session_id -> CDP port
_last_active: dict[str, float] = {}  # session_id -> unix timestamp
_cleanup_started = False

_TOKEN_LOG = os.path.expanduser("~/.openeyes/web/token-log.jsonl")
_HISTORY_ROOT = os.path.expanduser("~/.openeyes/web/history")


def _session_id(ctx: Context | None) -> str:
    """Resolve session ID from MCP context.

    Uses the client-provided name from InitializeRequest (e.g. 'claude-code')
    so the same agent gets the same session — and the same Chrome, cookies,
    logged-in state — across reconnections. If two parallel instances of the
    same client need isolation, set OPENEYES_WEB_SESSION in each one's
    environment to override.
    """
    override = os.environ.get("OPENEYES_WEB_SESSION")
    if override:
        return override
    if ctx is not None:
        sess = getattr(ctx, "session", None)
        if sess is not None:
            cp = getattr(sess, "client_params", None)
            if cp is not None:
                ci = getattr(cp, "clientInfo", None)
                if ci is not None and getattr(ci, "name", None):
                    raw = str(ci.name).strip()
                    name = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw)[:32]
                    if name:
                        return name
    return "default"


def _load_sessions() -> dict[str, dict]:
    try:
        with open(_SESSION_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sessions(data: dict[str, dict]) -> None:
    try:
        with open(_SESSION_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _update_session_record(session_id: str, port: int, chrome_pid: int | None = None) -> None:
    data = _load_sessions()
    rec = data.get(session_id, {})
    rec["port"] = port
    rec["last_active"] = time.time()
    if chrome_pid is not None:
        rec["chrome_pid"] = chrome_pid
    data[session_id] = rec
    _save_sessions(data)


def _allocate_port(session_id: str) -> int:
    """Return a CDP port for this session, allocating a fresh one if needed."""
    if session_id in _session_ports:
        return _session_ports[session_id]
    persisted = _load_sessions()
    if session_id in persisted:
        port = persisted[session_id]["port"]
        _session_ports[session_id] = port
        return port
    used = set(_session_ports.values()) | {r["port"] for r in persisted.values()}
    # "default" always gets the base port (back-compat with single-session deployments)
    if session_id == "default" and _BASE_CDP_PORT not in used:
        port = _BASE_CDP_PORT
    else:
        port = _BASE_CDP_PORT
        while port in used:
            port += 1
    _session_ports[session_id] = port
    return port


def _token_file(session_id: str) -> str:
    return f"/tmp/openeyes-web-tokens-{session_id}.json"


def _history_dir(session_id: str) -> str:
    return os.path.join(_HISTORY_ROOT, session_id)


def _track(response: list, session_id: str) -> list:
    """Estimate tokens in response and record for the active tab of this session."""
    tokens = 0
    for part in response:
        if isinstance(part, MCPImage):
            tokens += 1500  # ~896x630 JPEG ≈ 1500 Claude vision tokens
        elif isinstance(part, str):
            tokens += max(1, len(part) // 4)
    browser = _browsers.get(session_id)
    if browser and browser._pages:
        pid = id(browser._pages[browser._active])
        _page_tokens[pid] = _page_tokens.get(pid, 0) + tokens
        _write_token_stats(session_id)
        _append_token_log(session_id, browser._pages[browser._active].url, tokens)
    return response


def _write_token_stats(session_id: str) -> None:
    """Write token stats for this session to shared file for dashboard to read."""
    browser = _browsers.get(session_id)
    if not browser:
        return
    stats = [
        {"url": page.url, "tokens": _page_tokens.get(id(page), 0),
         "model": _current_model, "session": session_id}
        for page in browser._pages
    ]
    try:
        with open(_token_file(session_id), "w") as f:
            json.dump(stats, f)
    except Exception:
        pass


def _append_token_log(session_id: str, url: str, tokens: int) -> None:
    """Append token usage to persistent log (never rotated — kept for historical reporting)."""
    from datetime import datetime, timezone
    try:
        os.makedirs(os.path.dirname(_TOKEN_LOG), exist_ok=True)
        with open(_TOKEN_LOG, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "url": url,
                "tokens": tokens,
                "model": _current_model,
                "session": session_id,
            }) + "\n")
    except Exception:
        pass


def get_token_stats() -> list[dict]:
    """Returns [{url, tokens, model, session}, ...] across all sessions."""
    import glob
    result = []
    for path in glob.glob("/tmp/openeyes-web-tokens-*.json"):
        try:
            with open(path) as f:
                result.extend(json.load(f))
        except Exception:
            pass
    return result


def get_sessions() -> list[dict]:
    """Return all known sessions with their port and last_active (for dashboard)."""
    data = _load_sessions()
    now = time.time()
    result = []
    for sid, rec in data.items():
        last = rec.get("last_active", 0)
        result.append({
            "id": sid,
            "port": rec.get("port"),
            "last_active": last,
            "idle_seconds": int(now - last) if last else None,
            "active": sid in _browsers,
        })
    result.sort(key=lambda r: -(r["last_active"] or 0))
    return result


def _port_alive(port: int) -> bool:
    """Quick check — is Chrome still listening on this CDP port?"""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=0.3)
        return True
    except Exception:
        return False


async def _get_browser(session_id: str) -> BrowserManager:
    """Return (creating if needed) the BrowserManager for this session."""
    global _cleanup_started
    _last_active[session_id] = time.time()

    if session_id in _browsers:
        port = _session_ports.get(session_id)
        # Chrome may have died since last call (e.g. user closed all tabs via dashboard).
        # Drop the stale BrowserManager and fall through to re-launch a fresh Chrome.
        if port is None or not _port_alive(port):
            stale = _browsers.pop(session_id, None)
            if stale:
                try:
                    await stale.close()
                except Exception:
                    pass
            # Also reap the persisted record — port stays the same on recreate.
            data = _load_sessions()
            data.pop(session_id, None)
            _save_sessions(data)
            _session_ports.pop(session_id, None)
        else:
            _update_session_record(session_id, port)
            if not _cleanup_started:
                asyncio.create_task(_cleanup_loop())
                _cleanup_started = True
            return _browsers[session_id]

    port = _allocate_port(session_id)
    browser = BrowserManager(cdp_port=port)
    _browsers[session_id] = browser
    # Trigger actual Chrome launch so we can capture the PID.
    await browser._ensure_browser()
    _update_session_record(session_id, port, chrome_pid=browser._chrome_pid)
    print(f"[openeyes-web] Session '{session_id}' → CDP port {port} (pid={browser._chrome_pid})", file=sys.stderr)

    if not _cleanup_started:
        asyncio.create_task(_cleanup_loop())
        _cleanup_started = True

    return browser


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


async def _capture(session_id: str) -> tuple[bytes, bytes | None, str, float]:
    """Take screenshot, process it.

    Returns (jpeg_bytes, crop_jpeg_or_none, context, diff_ratio).
    """
    browser = await _get_browser(session_id)
    vision = _get_vision()
    memory = _get_memory()

    png = await browser.screenshot_bytes()
    diff_ratio, crop_jpeg = vision.get_change_info(png)
    jpeg_bytes = vision.process(png)

    url = browser.current_url
    title = await browser.get_page_title()
    context = memory.update(url, title, diff_ratio)

    _save_screenshot(session_id, jpeg_bytes, url, title)

    return jpeg_bytes, crop_jpeg, context, diff_ratio


def _save_screenshot(session_id: str, jpeg_bytes: bytes, url: str, title: str) -> None:
    """Save screenshot to disk for history browsing (per-session dir)."""
    from datetime import datetime, timezone
    try:
        hist = _history_dir(session_id)
        os.makedirs(hist, exist_ok=True)
        ts = datetime.now(timezone.utc)
        filename = f"{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        filepath = os.path.join(hist, filename)
        with open(filepath, "wb") as f:
            f.write(jpeg_bytes)
        with open(os.path.join(hist, "index.jsonl"), "a") as f:
            f.write(json.dumps({
                "ts": ts.isoformat(),
                "file": filename,
                "url": url,
                "title": title,
                "session": session_id,
            }) + "\n")
    except Exception:
        pass


def _build_response(img: bytes, crop: bytes | None, context: str,
                    extra: str = "", diff_ratio: float = 1.0,
                    show_tiny_changes: bool = False) -> list:
    """Build a tool response — smart about what images to include.

    - Major change (diff > 0.3) or first load: full screenshot
    - Moderate change (0.05-0.3): crop only (saves tokens)
    - Tiny change (< 0.05): text only by default, crop if show_tiny_changes=True
      (clicks pass show_tiny_changes=True since UI feedback is often subtle:
      cart badges, button state flips, toasts — all ~1-3% of the page)
    """
    parts = []
    if context:
        parts.append(context)
    if extra:
        parts.append(extra)
    text = "\n\n".join(p for p in parts if p)

    # Tiny / no detectable change
    if diff_ratio < 0.05:
        if show_tiny_changes and crop and diff_ratio > 0.0:
            # Subtle UI feedback — show the crop so the model can see
            # what actually changed.
            result = [MCPImage(data=crop, format="jpeg")]
            if text:
                result.append(
                    f"{text}\n[Tiny visual change ({diff_ratio:.1%}) — "
                    f"showing only the changed region.]"
                )
            return result
        # Text-only: no visible change detected (but don't claim "unchanged"
        # — the page may have changed in ways our diff didn't catch).
        return [text] if text else [""]

    if crop and diff_ratio < 0.3:
        # Moderate change — send only the crop (smaller = fewer tokens)
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
# TTL cleanup
# ---------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    """Background task: every _CLEANUP_INTERVAL seconds, close sessions idle > _TTL_SECONDS."""
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            await _cleanup_expired()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[openeyes-web] Cleanup error: {e}", file=sys.stderr)


async def _cleanup_expired() -> None:
    """Close any session whose last_active is older than TTL. Chrome killed, logs kept."""
    now = time.time()
    data = _load_sessions()
    changed = False

    for sid, rec in list(data.items()):
        last = rec.get("last_active", 0)
        if last and (now - last) > _TTL_SECONDS:
            browser = _browsers.pop(sid, None)
            pid = rec.get("chrome_pid")
            if browser:
                try:
                    await browser.close(kill_chrome=True)
                except Exception as e:
                    print(f"[openeyes-web] Failed to close session {sid}: {e}", file=sys.stderr)
            elif pid:
                # Session known from persisted state but no BrowserManager in memory
                # (e.g., server restarted). Kill Chrome directly.
                import signal
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except Exception as e:
                    print(f"[openeyes-web] Failed to kill pid {pid} for session {sid}: {e}", file=sys.stderr)
            _last_active.pop(sid, None)
            _session_ports.pop(sid, None)
            data.pop(sid, None)
            changed = True
            print(f"[openeyes-web] Reclaimed idle session '{sid}' (idle {int(now - last)}s)", file=sys.stderr)

    if changed:
        _save_sessions(data)


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
    """Tell OpenEyes Web which AI model is using it, for accurate cost tracking.
    Call this once at the start of your session.
    Examples: set_model("sonnet-4.5"), set_model("haiku"), set_model("opus")"""
    global _current_model
    _current_model = model.lower().strip()
    rate = _MODEL_RATES.get(_current_model, None)
    if rate:
        return f"Model set to '{_current_model}' (${rate}/M input tokens)"
    return f"Model set to '{_current_model}' (unknown pricing — add rate via dashboard)"


@mcp.tool()
async def navigate(url: str, ctx: Context) -> list:
    """Navigate to a URL. If a tab with that domain is already open, switches to it
    instead of navigating away. Use full URLs (with path) to navigate in the current tab.
    Examples: navigate("linkedin") → switches to LinkedIn tab. navigate("https://di.se/article/...") → opens in current tab."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    status = await browser.navigate(url)
    img, crop, context, _ = await _capture(sid)
    title = await browser.get_page_title()
    result = [MCPImage(data=img, format="jpeg")]
    tabs = browser.list_tabs()
    tab_info = " | ".join(f"[{t['index']}{'*' if t['active'] else ''}]{' 📌'+t['pin'] if t['pin'] else ''} {t['url'][:30]}" for t in tabs)
    text = f"{status}\n{context}\n\nURL: {browser.current_url}\nTitle: {title}\nTabs: {tab_info}"
    result.append(text)
    return _track(result, sid)


@mcp.tool()
async def click(x: int, y: int, ctx: Context) -> list:
    """Click at (x, y) coordinates on the screenshot.
    Look at the screenshot and estimate the pixel position of the element you want to click.
    Your click is automatically snapped to the nearest interactive element (button, link, input).
    The screenshot is ~896px wide and ~630px tall, with tick marks at 200px intervals."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
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

    img, crop, context, diff_ratio = await _capture(sid)
    return _track(_build_response(img, crop, context,
                           f"{desc}\nURL: {browser.current_url}",
                           diff_ratio, show_tiny_changes=True), sid)


@mcp.tool()
async def type_text(text: str, ctx: Context, press_enter: bool = False, clear_first: bool = False) -> list:
    """Type text into the currently focused element.
    Set clear_first=true to select-all and replace existing text.
    Set press_enter=true to submit (may navigate to new page)."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    await browser.type_text(text, press_enter=press_enter, clear_first=clear_first)

    if press_enter:
        # Pressing enter may navigate — return screenshot
        img, crop, context, diff_ratio = await _capture(sid)
        return _track(_build_response(img, crop, context,
                               f"Typed: '{text}' + Enter | URL: {browser.current_url}",
                               diff_ratio), sid)

    # No enter — page barely changed. Text-only response saves ~800 tokens.
    return _track([f"Typed: '{text}' into focused element.\nURL: {browser.current_url}\n\nUse screenshot() to see the current page if needed."], sid)


@mcp.tool()
async def scroll(ctx: Context, direction: str = "down") -> list:
    """Scroll the page. Direction: 'up' or 'down'."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    await browser.scroll(direction)

    img, crop, context, diff_ratio = await _capture(sid)

    if diff_ratio < 0.02:
        # Nothing new appeared — probably at top/bottom of page
        return _track([f"Scrolled {direction} — no new content visible (may have reached the {'bottom' if direction == 'down' else 'top'}).\nURL: {browser.current_url}"], sid)

    return _track(_build_response(img, crop, context,
                           f"Scrolled {direction} | URL: {browser.current_url}",
                           diff_ratio), sid)


@mcp.tool()
async def get_text(ctx: Context) -> str:
    """Extract the main text content of the current page (article body, headings, paragraphs).
    Use this to read articles, blog posts, or any page with text content.
    Returns plain text with markdown headings — much faster than reading from screenshots."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    text = await browser.get_page_text()
    result = f"URL: {browser.current_url}\n\n{text}"
    _track([result], sid)
    return result


@mcp.tool()
async def go_back(ctx: Context) -> list:
    """Go back to the previous page."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    await browser.back()
    img, crop, context, _ = await _capture(sid)
    # Always full screenshot for navigation
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nWent back | URL: {browser.current_url}")
    return _track(result, sid)


@mcp.tool()
async def screenshot(ctx: Context) -> list:
    """Take a fresh screenshot of the current page."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    img, _, context, _ = await _capture(sid)
    # Always full screenshot when explicitly requested
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nURL: {browser.current_url}")
    return _track(result, sid)


@mcp.tool()
async def new_tab(ctx: Context, url: str = "about:blank", pin: str = "", force_new: bool = False) -> list:
    """Open a new browser tab.

    Two different domains → two tabs. For example, if willys.se is already
    open, new_tab("https://gp.se") opens a second tab; you can switch_tab(0)
    to return to willys.

    Same-domain default: if a tab for that domain is already open, the call
    navigates the existing tab instead of opening a duplicate. This prevents
    agents from accumulating duplicate tabs by accident.

    force_new=True overrides the dedup — use it when you deliberately want a
    second tab for the same site (e.g. comparing two product pages on willys
    side by side). Example: new_tab("https://willys.se/choklad", force_new=True).

    Set pin="name" to tag a tab so you can tell duplicates apart in list_tabs()
    and protect it from close_tab. Example: new_tab("https://linkedin.com", pin="linkedin")"""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    index = await browser.new_tab(url, pin=pin, force_new=force_new)
    img, crop, context, _ = await _capture(sid)
    tabs = browser.list_tabs()
    tab_info = "\n".join(f"  [{t['index']}] {'📌'+t['pin']+' ' if t['pin'] else ''}{'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
    result = [MCPImage(data=img, format="jpeg")]
    pin_msg = f" (pinned as '{pin}')" if pin else ""
    result.append(f"{context}\n\nOpened tab {index}{pin_msg} | URL: {browser.current_url}\n\nAll tabs:\n{tab_info}")
    return _track(result, sid)


@mcp.tool()
async def switch_tab(index: int, ctx: Context) -> list:
    """Switch to a different tab by index. Use list_tabs() to see available tabs."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    await browser.switch_tab(index)
    img, crop, context, _ = await _capture(sid)
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Switched to tab {index} | URL: {browser.current_url}")
    return _track(result, sid)


@mcp.tool()
async def list_tabs(ctx: Context) -> str:
    """List all open browser tabs."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    tabs = browser.list_tabs()
    lines = [f"[{t['index']}] {'→ ' if t['active'] else '  '}{t['url']}" for t in tabs]
    result = f"{len(tabs)} open tabs:\n" + "\n".join(lines)
    _track([result], sid)
    return result


@mcp.tool()
async def close_tab(index: int, ctx: Context) -> list:
    """Close a tab by index. Pinned tabs cannot be closed.
    Closing the last tab ends the session (Chrome exits, browser state discarded)."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    error = await browser.close_tab(index)
    if error:
        return [f"Cannot close tab {index}: {error}"]

    # Last tab closed — tear down the session entirely.
    if not browser._pages:
        try:
            await browser.close()  # Chrome exits on its own once all pages are gone
        except Exception:
            pass
        _browsers.pop(sid, None)
        _session_ports.pop(sid, None)
        _last_active.pop(sid, None)
        data = _load_sessions()
        data.pop(sid, None)
        _save_sessions(data)
        return [f"Closed tab {index} — last tab, session '{sid}' ended."]

    img, crop, context, _ = await _capture(sid)
    tabs = browser.list_tabs()
    tab_info = "\n".join(f"  [{t['index']}] {'📌'+t['pin']+' ' if t['pin'] else ''}{'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Closed tab {index}\n\nAll tabs:\n{tab_info}")
    return _track(result, sid)


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
    print(f"[openeyes-web] Dashboard at http://localhost:{HTTP_PORT}", file=sys.stderr)


async def _warmup_browser():
    """Start the default session's browser immediately so CDP is ready for dashboard."""
    await _get_browser("default")


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
        print(f"[openeyes-web] MCP server at http://localhost:{mcp.settings.port}/sse", file=sys.stderr)
        print("[openeyes-web] Ready.", file=sys.stderr)

    mcp.run(transport=transport)


def serve():
    """All-in-one: MCP server + dashboard + browser. One command to run everything."""
    sys.argv = [sys.argv[0], "serve"]
    main()


if __name__ == "__main__":
    main()
