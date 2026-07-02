"""Browser management via Playwright — headless Chromium with viewport-only screenshots."""

from __future__ import annotations

import asyncio
import os
import random
import shutil
import subprocess
import sys
import time
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

# OPENEYES_WEB_FAST=1 removes the human-mimicking delays (pure latency win on
# sites that don't fingerprint timing).
_FAST = os.environ.get("OPENEYES_WEB_FAST", "") == "1"


async def _human_delay(min_ms: int = 100, max_ms: int = 400) -> None:
    """Random delay to mimic a fast human — not instant, not slow."""
    if _FAST:
        return
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)


async def _settle(page: Page, timeout_ms: int = 2000) -> None:
    """Best-effort wait for the network to go quiet so screenshots don't capture
    half-rendered pages. Swallows the timeout — JS-heavy sites with polling
    never reach networkidle and that's fine."""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass

# Click-time snap: find nearest interactive element at (x, y), piercing shadow DOM.
# Called with {x, y} in viewport coordinates. Returns element info + click point.
# On a direct hit the click point is the REQUESTED point (it's already inside the
# element — clicking the element's center instead can hit a different item when
# the matched element is a large card/container). Only nearby snaps move the
# point, and then to the nearest point on the element's rect (verified hittable,
# falling back to the center).
_CLICK_SNAP_JS = """
({x, y}) => {
    const INTERACTIVE_TAGS = new Set([
        'A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'SUMMARY', 'DETAILS'
    ]);
    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'tab', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'textbox', 'combobox',
        'searchbox', 'checkbox', 'radio'
    ]);

    function deepElementFromPoint(px, py) {
        let el = document.elementFromPoint(px, py);
        while (el && el.shadowRoot) {
            const inner = el.shadowRoot.elementFromPoint(px, py);
            if (!inner || inner === el) break;
            el = inner;
        }
        return el;
    }

    // SVG subtree elements — never return these as the "interactive" hit.
    // They often inherit cursor:pointer from a parent <button>/<a>, so we
    // must keep climbing to find the semantic wrapper.
    const SVG_SUBTREE_TAGS = new Set([
        'SVG', 'PATH', 'G', 'CIRCLE', 'RECT', 'LINE', 'POLYGON', 'POLYLINE',
        'ELLIPSE', 'USE', 'DEFS', 'SYMBOL', 'TEXT', 'TSPAN'
    ]);

    function isInteractive(el) {
        if (!el || el === document.body || el === document.documentElement) return false;
        // SVG children are never "the" interactive element — climb past them.
        if (SVG_SUBTREE_TAGS.has(el.tagName)) return false;
        if (INTERACTIVE_TAGS.has(el.tagName)) return true;
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role)) return true;
        if (el.getAttribute('contenteditable') === 'true') return true;
        if (el.onclick || el.getAttribute('onclick')) return true;
        const ti = el.getAttribute('tabindex');
        if (ti && ti !== '-1') return true;
        try { if (window.getComputedStyle(el).cursor === 'pointer') return true; } catch(e) {}
        return false;
    }

    // "Strong" interactive = has semantic meaning (button/link/input), not
    // just a cursor:pointer div. We prefer these when climbing.
    function isStronglyInteractive(el) {
        if (!el) return false;
        if (SVG_SUBTREE_TAGS.has(el.tagName)) return false;
        if (INTERACTIVE_TAGS.has(el.tagName)) return true;
        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role)) return true;
        return false;
    }

    function findInteractive(startEl) {
        let el = startEl;
        let firstWeak = null;
        // Climb up to 10 parents looking for the semantic wrapper.
        for (let i = 0; i < 10 && el && el !== document.body; i++) {
            if (isStronglyInteractive(el)) return el;
            if (!firstWeak && isInteractive(el)) firstWeak = el;
            if (el.parentElement) { el = el.parentElement; }
            else { const r = el.getRootNode(); el = r && r.host ? r.host : null; }
        }
        // No semantic button/link found — fall back to the first weak hit
        // (cursor:pointer div, onclick handler, etc).
        return firstWeak;
    }

    function describe(el, cx, cy) {
        const tag = el.tagName.toLowerCase();
        let type = el.getAttribute('type') || '';
        if (!type && el.getAttribute('contenteditable') === 'true') type = 'contenteditable';
        if (!type && el.getAttribute('role') === 'textbox') type = 'textbox';
        let text = '';
        if (el.getAttribute('contenteditable') === 'true' || el.getAttribute('role') === 'textbox') {
            text = el.getAttribute('aria-placeholder') || el.getAttribute('data-placeholder') || el.getAttribute('aria-label') || '';
            if (!text && el.nextElementSibling) {
                text = el.nextElementSibling.getAttribute('data-placeholder') || '';
            }
        }
        if (!text) {
            text = (el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 60);
        }
        return {
            found: true, tag, type, text: text.trim().slice(0, 60),
            cx: Math.round(cx), cy: Math.round(cy),
        };
    }

    // Click point for a nearby snap: nearest point on the element's rect
    // (inset 2px), verified to actually hit the element; else its center.
    function snapPoint(el, px, py) {
        const rect = el.getBoundingClientRect();
        const nx = Math.min(Math.max(px, rect.left + 2), rect.right - 2);
        const ny = Math.min(Math.max(py, rect.top + 2), rect.bottom - 2);
        const test = deepElementFromPoint(nx, ny);
        if (test && (test === el || el.contains(test) || findInteractive(test) === el)) {
            return [nx, ny];
        }
        return [rect.x + rect.width / 2, rect.y + rect.height / 2];
    }

    // 1. Direct hit — click exactly where asked; the point is inside the element.
    const hit = deepElementFromPoint(x, y);
    if (hit) {
        const el = findInteractive(hit);
        if (el) return {...describe(el, x, y), method: 'direct'};
    }

    // 2. Spiral search — 8 directions, expanding radius
    const dirs = [[0,-1],[1,-1],[1,0],[1,1],[0,1],[-1,1],[-1,0],[-1,-1]];
    for (let r = 10; r <= 50; r += 10) {
        for (const [dx, dy] of dirs) {
            const px = x + dx * r, py = y + dy * r;
            if (px < 0 || py < 0 || px >= window.innerWidth || py >= window.innerHeight) continue;
            const el2 = deepElementFromPoint(px, py);
            if (el2) {
                const interactive = findInteractive(el2);
                if (interactive) {
                    const [sx, sy] = snapPoint(interactive, x, y);
                    return {...describe(interactive, sx, sy), method: 'nearby', radius: r};
                }
            }
        }
    }

    // 3. No interactive element — raw click
    return {
        found: false, method: 'raw',
        tag: hit ? hit.tagName.toLowerCase() : 'unknown',
        type: '', text: hit ? (hit.textContent || '').trim().slice(0, 60) : '',
        cx: x, cy: y,
    };
}
"""

_GET_TEXT_JS = """
() => {
    // Try to find the main article content
    const selectors = ['article', '[role="main"]', 'main', '.article-body',
                       '.article-content', '.story-body', '.post-content'];
    let root = null;
    for (const sel of selectors) {
        root = document.querySelector(sel);
        if (root) break;
    }
    if (!root) root = document.body;

    // Structured pass: headings become markdown, text-bearing blocks follow.
    const parts = [];
    const els = root.querySelectorAll('h1, h2, h3, h4, h5, h6, p, li, td, th, dt, dd, figcaption, blockquote');
    for (const el of els) {
        const text = el.textContent.trim();
        if (!text || text.length < 5) continue;
        const tag = el.tagName.toLowerCase();
        if (tag.startsWith('h')) {
            parts.push('\\n## ' + text);
        } else {
            parts.push(text);
        }
    }
    let out = parts.join('\\n');

    // Product grids and app-like pages render names/prices in divs/spans that
    // the structured pass misses entirely. If it came back thin, fall back to
    // the page's visible text (innerText respects CSS visibility).
    if (out.length < 200) {
        out = (root.innerText || document.body.innerText || '').trim();
    }

    const LIMIT = 8000;
    if (out.length > LIMIT) {
        return out.slice(0, LIMIT) + '\\n\\n[Truncated — showing ' + LIMIT + ' of ' + out.length + ' characters. Scroll or navigate to a more specific page for the rest.]';
    }
    return out;
}
"""


class BrowserManager:
    """Manages a Chromium instance with multiple tabs.

    Environment variables:
        HEADED=1              — show the browser window on the desktop
        CDP_PORT=9222         — expose Chrome DevTools Protocol on this port;
                                connect from any browser to see/interact live
        SLOW_MO=300           — delay (ms) between Playwright actions
        OPENEYES_WEB_FAST=1   — skip human-mimicking delays
    """

    def __init__(self, viewport_width: int = 1280, viewport_height: int = 900,
                 cdp_port: int = 0, headed: bool = False, slow_mo: int = 0):
        self._pw = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: list[Page] = []
        self._active: int = 0
        self._expect_new_page = False
        self._pins: dict[int, str] = {}  # page id -> pin name
        self._vw = viewport_width
        self._vh = viewport_height
        self._cdp_port = cdp_port
        self._headed = headed
        self._slow_mo = slow_mo
        self._chrome_pid: int | None = None  # PID of Chrome we launched (None if we reused an existing one)

    async def _ensure_browser(self) -> Page:
        if self._pages:
            return self._pages[self._active]

        headed = self._headed
        slow_mo = self._slow_mo
        cdp_port = self._cdp_port

        self._pw = await async_playwright().start()

        if cdp_port:
            port = cdp_port
            import urllib.request

            # Check if Chrome is already running on this port (survives MCP reconnects)
            chrome_running = False
            try:
                urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
                chrome_running = True
                print(f"[openeyes-web] Reusing existing Chrome on port {port}", file=sys.stderr)
            except Exception:
                pass

            if not chrome_running:
                # Launch new Chrome with CDP port
                import glob
                candidates = sorted(
                    glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
                    reverse=True,
                )
                chrome_path = candidates[0] if candidates else self._pw.chromium.executable_path
                # Per-port user-data-dir keeps sessions' cookies/storage isolated
                # and lets multiple Chrome instances coexist (Chrome refuses to
                # launch a second instance sharing a profile directory).
                user_data_dir = f"/tmp/openeyes-web-chrome-{port}"
                os.makedirs(user_data_dir, exist_ok=True)
                chrome_args = [
                    chrome_path,
                    f"--remote-debugging-port={port}",
                    "--remote-debugging-address=0.0.0.0",
                    f"--user-data-dir={user_data_dir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    f"--window-size={self._vw},{self._vh}",
                    "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ]
                chrome_args.append("about:blank")

                env = os.environ.copy()
                use_xvfb = shutil.which("Xvfb")

                if use_xvfb:
                    self._xvfb_display = ":99"
                    # Xvfb is shared across all sessions — only launch if not already running.
                    # (X lock file is the standard way to detect a running display.)
                    if not os.path.exists(f"/tmp/.X99-lock"):
                        subprocess.Popen(
                            ["Xvfb", self._xvfb_display, "-screen", "0",
                             f"{self._vw}x{self._vh}x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,  # survive MCP shutdown
                        )
                        time.sleep(0.5)
                        print(f"[openeyes-web] Started Xvfb on {self._xvfb_display}", file=sys.stderr)
                    else:
                        print(f"[openeyes-web] Reusing Xvfb on {self._xvfb_display}", file=sys.stderr)
                    env["DISPLAY"] = self._xvfb_display
                else:
                    chrome_args.insert(1, "--headless=new")
                    print("[openeyes-web] No Xvfb — using --headless=new for CDP", file=sys.stderr)

                # Chrome survives this process, but we track the PID so TTL
                # cleanup can terminate it when the session expires.
                proc = subprocess.Popen(
                    chrome_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,  # survive agent/MCP shutdown
                )
                self._chrome_pid = proc.pid
                for _ in range(30):
                    try:
                        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=1)
                        break
                    except Exception:
                        time.sleep(0.2)

            self._browser = await self._pw.chromium.connect_over_cdp(
                f"http://localhost:{port}",
                slow_mo=slow_mo,
            )
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": self._vw, "height": self._vh},
                )
            # Reattach to all existing pages (preserves tabs across reconnects)
            pages = self._context.pages
            if pages:
                self._pages = list(pages)
                self._active = len(pages) - 1
                for page in self._pages:
                    await page.set_viewport_size({"width": self._vw, "height": self._vh})
                    self._watch_page(page)
            else:
                page = await self._context.new_page()
                await page.set_viewport_size({"width": self._vw, "height": self._vh})
                self._pages = [page]
                self._active = 0
                self._watch_page(page)

            self._context.on("page", self._on_new_page)

            print(f"[openeyes-web] Live browser at http://localhost:{port} ({len(self._pages)} tab(s))", file=sys.stderr)
        else:
            # Standard Playwright launch (no remote viewing)
            self._browser = await self._pw.chromium.launch(
                headless=not headed,
                slow_mo=slow_mo,
            )
            self._context = await self._browser.new_context(
                viewport={"width": self._vw, "height": self._vh},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = await self._context.new_page()
            self._pages = [page]
            self._active = 0
            self._watch_page(page)
            self._context.on("page", self._on_new_page)

        return self._pages[self._active]

    def _watch_page(self, page: Page) -> None:
        """Keep _pages in sync when a tab is closed externally (dashboard close
        button, window.close(), crash) — otherwise the next screenshot targets
        a dead page and throws a raw Playwright error at the agent."""
        page.on("close", lambda: self._forget_page(page))

    def _forget_page(self, page: Page) -> None:
        if page not in self._pages:
            return  # already removed by close_tab()
        i = self._pages.index(page)
        self._pages.pop(i)
        self._pins.pop(id(page), None)
        if i < self._active:
            self._active -= 1
        if self._active >= len(self._pages):
            self._active = max(0, len(self._pages) - 1)

    def _on_new_page(self, page) -> None:
        """Auto-close popup tabs unless opened intentionally via new_tab()."""
        if self._expect_new_page:
            return  # Intentional — will be managed by new_tab()
        async def _close():
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            url = page.url
            print(f"[openeyes-web] Auto-closing popup tab: {url[:80]}", file=sys.stderr)
            try:
                await page.close()
            except Exception:
                pass
        asyncio.ensure_future(_close())

    @property
    def current_url(self) -> str:
        if not self._pages:
            return "about:blank"
        return self._pages[self._active].url

    @property
    def viewport_size(self) -> tuple[int, int]:
        return (self._vw, self._vh)

    @property
    def active_tab_key(self) -> int:
        """Opaque key for the active tab — used for per-tab vision/memory state."""
        if not self._pages:
            return 0
        return id(self._pages[self._active])

    @property
    def live_tab_keys(self) -> set[int]:
        return {id(p) for p in self._pages}

    # --- Tab management ---

    def _find_tab_by_keyword(self, keyword: str) -> int | None:
        """Find an open tab whose URL or pin name matches a keyword."""
        kw = keyword.lower()
        # Check pinned names first (exact match)
        for i, page in enumerate(self._pages):
            pin = self._pins.get(id(page), "")
            if pin and kw == pin.lower():
                return i
        # Then check URL domain/path contains keyword
        for i, page in enumerate(self._pages):
            if kw in page.url.lower():
                return i
        return None

    async def new_tab(self, url: str = "about:blank", pin: str = "", force_new: bool = False) -> int:
        """Open a new tab. Optional pin name makes it findable by keyword
        and protects it from close_tab. Returns the new tab's index.
        If a tab with the same domain is already open, switches to it instead
        — unless force_new=True, which always opens a fresh tab."""
        await self._ensure_browser()

        # Reuse existing tab with same domain unless caller explicitly forces new
        if not force_new and url and url != "about:blank":
            from urllib.parse import urlparse
            try:
                domain = urlparse(url if "://" in url else f"https://{url}").netloc.lower().replace("www.", "")
            except Exception:
                domain = ""
            if domain:
                for i, page in enumerate(self._pages):
                    try:
                        existing = urlparse(page.url).netloc.lower().replace("www.", "")
                    except Exception:
                        continue
                    if existing == domain:
                        self._active = i
                        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        await _settle(page)
                        await _human_delay(300, 700)
                        return self._active

        self._expect_new_page = True
        page = await self._context.new_page()
        self._expect_new_page = False
        await page.set_viewport_size({"width": self._vw, "height": self._vh})
        self._pages.append(page)
        self._active = len(self._pages) - 1
        self._watch_page(page)
        if pin:
            self._pins[id(page)] = pin
        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _settle(page)
            await _human_delay(300, 700)
        return self._active

    async def switch_tab(self, index: int) -> str | None:
        """Switch to tab by index. Returns error message if the index is invalid."""
        if not (0 <= index < len(self._pages)):
            return f"No tab at index {index} — valid indices: 0..{len(self._pages) - 1}"
        self._active = index
        await self._pages[index].bring_to_front()
        return None

    def list_tabs(self) -> list[dict]:
        """List all open tabs with index, url, pin name, and active status."""
        return [
            {
                "index": i,
                "url": p.url,
                "pin": self._pins.get(id(p), ""),
                "active": i == self._active,
            }
            for i, p in enumerate(self._pages)
        ]

    async def close_tab(self, index: int) -> str | None:
        """Close a tab. Pinned tabs are protected. Closing the last tab leaves
        no pages — the caller is expected to tear down the session.
        Returns error message if refused, None on success."""
        if not (0 <= index < len(self._pages)):
            return f"No tab at index {index} — valid indices: 0..{len(self._pages) - 1}"
        page = self._pages[index]
        pin = self._pins.get(id(page))
        if pin:
            return f"Tab '{pin}' is pinned — unpin it first or use navigate() to change its page"
        self._pages.pop(index)
        self._pins.pop(id(page), None)
        await page.close()
        if self._active >= len(self._pages):
            self._active = max(0, len(self._pages) - 1)
        return None

    # --- Navigation (tab-aware) ---

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> str:
        """Navigate to a URL. If a tab with a matching domain is already open,
        switches to it instead of navigating away from the current tab.
        Full URLs (with path) always navigate in the current tab.
        Returns a status string describing what happened."""
        await self._ensure_browser()

        # Short keyword or domain-only? Try to find existing tab.
        is_full_url = "/" in url.split("//", 1)[-1]  # has a path beyond domain
        keyword = url.replace("https://", "").replace("http://", "").replace("www.", "").rstrip("/")

        if not is_full_url:
            existing = self._find_tab_by_keyword(keyword)
            if existing is not None:
                self._active = existing
                await self._pages[existing].bring_to_front()
                return f"Switched to existing tab {existing}"

        # Ensure url has protocol
        if not url.startswith("http"):
            url = f"https://{url}"

        page = self._pages[self._active]
        await page.goto(url, wait_until=wait_until, timeout=30000)
        await _settle(page)
        await _human_delay(300, 700)
        return "Navigated"

    async def screenshot_bytes(self) -> bytes:
        """Capture viewport-only screenshot as PNG bytes."""
        page = await self._ensure_browser()
        try:
            return await page.screenshot(type="png", full_page=False, timeout=10000)
        except Exception:
            # Fallback: skip animations/fonts that may hang
            return await page.screenshot(
                type="png", full_page=False, timeout=10000,
                animations="disabled",
            )

    async def get_page_text(self) -> str:
        """Extract the visible text content of the page. Structured (headings as
        markdown) when the page has article-style markup; falls back to visible
        innerText for app-like pages (product grids, dashboards)."""
        page = await self._ensure_browser()
        return await page.evaluate(_GET_TEXT_JS)

    async def click_at_point(self, x: int, y: int) -> dict:
        """Click near (x, y) with snap-to-interactive-element.

        Uses elementFromPoint (pierces shadow DOM) to find the nearest
        interactive element. Direct hits click the requested point; nearby
        snaps click the nearest point on the element. Returns info about
        what was clicked: {found, cx, cy, tag, type, text, method}.
        """
        page = await self._ensure_browser()
        result = await page.evaluate(_CLICK_SNAP_JS, {"x": x, "y": y})
        await page.mouse.click(result["cx"], result["cy"])
        await _human_delay(150, 400)
        return result

    async def type_text(self, text: str, press_enter: bool = False,
                        clear_first: bool = False) -> None:
        page = await self._ensure_browser()
        if clear_first:
            await page.keyboard.press("Control+a")
            await _human_delay(50, 150)
        await page.keyboard.type(text, delay=0 if _FAST else random.randint(20, 50))
        if press_enter:
            await _human_delay(100, 300)
            await page.keyboard.press("Enter")
            await _settle(page, timeout_ms=1500)
            await _human_delay(300, 600)

    async def scroll(self, direction: str = "down", amount: int = 3) -> None:
        page = await self._ensure_browser()
        delta = amount * 300 * (1 if direction == "down" else -1)
        await page.mouse.wheel(0, delta)
        await _human_delay(200, 500)

    async def back(self) -> None:
        page = await self._ensure_browser()
        await page.go_back(timeout=10000)
        await _settle(page)
        await _human_delay(200, 500)

    async def get_page_title(self) -> str:
        page = await self._ensure_browser()
        return await page.title()

    async def close(self, kill_chrome: bool = False) -> None:
        """Disconnect from browser. By default Chrome/Xvfb persist; pass
        kill_chrome=True to also terminate the Chrome process we launched
        (used by TTL cleanup for expired sessions)."""
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        if kill_chrome and self._chrome_pid:
            import signal
            try:
                os.kill(self._chrome_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except Exception as e:
                print(f"[openeyes-web] Failed to kill Chrome pid={self._chrome_pid}: {e}", file=sys.stderr)
            self._chrome_pid = None
        self._pages = []
        self._active = 0
        self._pins = {}
        self._context = None
        self._browser = None
        self._pw = None
