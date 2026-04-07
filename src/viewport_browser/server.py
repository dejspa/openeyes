"""MCP server — vision-first browser controller. Clean screenshots + coordinate-based clicking."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP, Image as MCPImage

from .browser import BrowserManager
from .tracker import PageMemory
from .vision import VisionPipeline

mcp = FastMCP(
    "viewport",
    instructions="""\
Vision-first web browser for navigating websites.

TOOLS:
- navigate(url) — go to a URL
- click(x, y) — click at pixel coordinates on the screenshot (auto-snaps to nearest element)
- type_text(text, press_enter, clear_first) — type into focused element
- scroll(direction) — scroll up/down
- get_text() — extract page text (article content, product details, prices)
- go_back() — browser back
- screenshot() — fresh screenshot

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
CDP_PORT = 9222

_browser: BrowserManager | None = None
_vision: VisionPipeline | None = None
_memory: PageMemory | None = None


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

@mcp.tool()
async def navigate(url: str) -> list:
    """Navigate to a URL. Returns a screenshot."""
    browser = await _get_browser()
    await browser.navigate(url)
    img, crop, context, _ = await _capture()
    title = await browser.get_page_title()
    # Always send full screenshot for new page
    result = [MCPImage(data=img, format="jpeg")]
    text = f"{context}\n\nURL: {browser.current_url}\nTitle: {title}"
    result.append(text)
    return result


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
    return _build_response(img, crop, context,
                           f"{desc}\nURL: {browser.current_url}",
                           diff_ratio)


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
        return _build_response(img, crop, context,
                               f"Typed: '{text}' + Enter | URL: {browser.current_url}",
                               diff_ratio)

    # No enter — page barely changed. Text-only response saves ~800 tokens.
    return [f"Typed: '{text}' into focused element.\nURL: {browser.current_url}\n\nUse screenshot() to see the current page if needed."]


@mcp.tool()
async def scroll(direction: str = "down") -> list:
    """Scroll the page. Direction: 'up' or 'down'."""
    browser = await _get_browser()
    await browser.scroll(direction)

    img, crop, context, diff_ratio = await _capture()

    if diff_ratio < 0.02:
        # Nothing new appeared — probably at top/bottom of page
        return [f"Scrolled {direction} — no new content visible (may have reached the {'bottom' if direction == 'down' else 'top'}).\nURL: {browser.current_url}"]

    return _build_response(img, crop, context,
                           f"Scrolled {direction} | URL: {browser.current_url}",
                           diff_ratio)


@mcp.tool()
async def get_text() -> str:
    """Extract the main text content of the current page (article body, headings, paragraphs).
    Use this to read articles, blog posts, or any page with text content.
    Returns plain text with markdown headings — much faster than reading from screenshots."""
    browser = await _get_browser()
    text = await browser.get_page_text()
    return f"URL: {browser.current_url}\n\n{text}"


@mcp.tool()
async def go_back() -> list:
    """Go back to the previous page."""
    browser = await _get_browser()
    await browser.back()
    img, crop, context, _ = await _capture()
    # Always full screenshot for navigation
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nWent back | URL: {browser.current_url}")
    return result


@mcp.tool()
async def screenshot() -> list:
    """Take a fresh screenshot of the current page."""
    browser = await _get_browser()
    img, _, context, _ = await _capture()
    # Always full screenshot when explicitly requested
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"{context}\n\nURL: {browser.current_url}")
    return result


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
