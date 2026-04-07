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


async def _human_delay(min_ms: int = 100, max_ms: int = 400) -> None:
    """Random delay to mimic a fast human — not instant, not slow."""
    await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)

# Click-time snap: find nearest interactive element at (x, y), piercing shadow DOM.
# Called with {x, y} in viewport coordinates. Returns element info + snapped center.
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

    function isInteractive(el) {
        if (!el || el === document.body || el === document.documentElement) return false;
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

    function findInteractive(startEl) {
        let el = startEl;
        for (let i = 0; i < 8 && el && el !== document.body; i++) {
            if (isInteractive(el)) return el;
            if (el.parentElement) { el = el.parentElement; }
            else { const r = el.getRootNode(); el = r && r.host ? r.host : null; }
        }
        return null;
    }

    function describe(el) {
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
        const rect = el.getBoundingClientRect();
        return {
            found: true, tag, type, text: text.trim().slice(0, 60),
            cx: Math.round(rect.x + rect.width / 2),
            cy: Math.round(rect.y + rect.height / 2),
        };
    }

    // 1. Direct hit
    const hit = deepElementFromPoint(x, y);
    if (hit) {
        const el = findInteractive(hit);
        if (el) return {...describe(el), method: 'direct'};
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
                if (interactive) return {...describe(interactive), method: 'nearby', radius: r};
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


class BrowserManager:
    """Manages a Chromium instance with multiple tabs.

    Environment variables:
        HEADED=1        — show the browser window on the desktop
        CDP_PORT=9222   — expose Chrome DevTools Protocol on this port;
                          connect from any browser to see/interact live
        SLOW_MO=300     — delay (ms) between Playwright actions
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
        self._chrome_proc: subprocess.Popen | None = None
        self._vw = viewport_width
        self._vh = viewport_height
        self._cdp_port = cdp_port
        self._headed = headed
        self._slow_mo = slow_mo

    async def _ensure_browser(self) -> Page:
        if self._pages:
            return self._pages[self._active]

        headed = self._headed
        slow_mo = self._slow_mo
        cdp_port = self._cdp_port

        self._pw = await async_playwright().start()

        # CDP mode requires Xvfb — skip if not available (e.g. remote/cloud hosts)
        if cdp_port and not shutil.which("Xvfb"):
            print("[viewport] Xvfb not found — falling back to headless mode (no dashboard)", file=sys.stderr)
            cdp_port = None

        if cdp_port:
            # Launch full Chromium (not headless shell) with CDP port, then connect
            # Playwright to it. The headless shell doesn't support CDP port.
            port = cdp_port
            # Find the full Chromium binary (not the headless shell)
            import glob
            candidates = sorted(
                glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome")),
                reverse=True,  # newest version first
            )
            chrome_path = candidates[0] if candidates else self._pw.chromium.executable_path
            chrome_args = [
                chrome_path,
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=0.0.0.0",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                f"--window-size={self._vw},{self._vh}",
                "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            ]
            # Screencast requires visual rendering — can't use headless.
            # Use Xvfb (virtual framebuffer) so Chrome renders but no window appears.
            chrome_args.append("about:blank")

            # Start Xvfb on a private display
            self._xvfb_display = ":99"
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", self._xvfb_display, "-screen", "0",
                 f"{self._vw}x{self._vh}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)  # let Xvfb start

            env = os.environ.copy()
            env["DISPLAY"] = self._xvfb_display

            self._chrome_proc = subprocess.Popen(
                chrome_args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
            # Wait for CDP to be ready
            import urllib.request
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
            # Use the default context created by Chrome
            contexts = self._browser.contexts
            if contexts:
                self._context = contexts[0]
            else:
                self._context = await self._browser.new_context(
                    viewport={"width": self._vw, "height": self._vh},
                )
            # Use existing page or create one
            pages = self._context.pages
            page = pages[0] if pages else await self._context.new_page()
            await page.set_viewport_size({"width": self._vw, "height": self._vh})
            self._pages = [page]
            self._active = 0

            # Auto-close popup tabs (ads, new windows from link clicks)
            self._context.on("page", self._on_new_page)

            print(f"[viewport] Live browser at http://localhost:{port}", file=sys.stderr)
            print(f"[viewport] Open chrome://inspect and add localhost:{port} to see/interact", file=sys.stderr)
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
            self._context.on("page", self._on_new_page)

        return self._pages[self._active]

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
            print(f"[viewport] Auto-closing popup tab: {url[:80]}", file=sys.stderr)
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

    async def new_tab(self, url: str = "about:blank", pin: str = "") -> int:
        """Open a new tab. Optional pin name makes it findable by keyword
        and protects it from close_tab. Returns the new tab's index."""
        await self._ensure_browser()
        self._expect_new_page = True
        page = await self._context.new_page()
        self._expect_new_page = False
        await page.set_viewport_size({"width": self._vw, "height": self._vh})
        self._pages.append(page)
        self._active = len(self._pages) - 1
        if pin:
            self._pins[id(page)] = pin
        if url and url != "about:blank":
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await _human_delay(300, 700)
        return self._active

    async def switch_tab(self, index: int) -> None:
        """Switch to tab by index."""
        if 0 <= index < len(self._pages):
            self._active = index
            await self._pages[index].bring_to_front()

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
        """Close a tab. Won't close pinned tabs or the last remaining tab.
        Returns error message if refused, None on success."""
        if len(self._pages) <= 1:
            return "Cannot close the last tab"
        page = self._pages[index]
        pin = self._pins.get(id(page))
        if pin:
            return f"Tab '{pin}' is pinned — unpin it first or use navigate() to change its page"
        self._pages.pop(index)
        self._pins.pop(id(page), None)
        await page.close()
        if self._active >= len(self._pages):
            self._active = len(self._pages) - 1
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
        """Extract the main text content of the page (article body, headings, paragraphs)."""
        page = await self._ensure_browser()
        return await page.evaluate("""
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

            const parts = [];
            const els = root.querySelectorAll('h1, h2, h3, h4, p, li, figcaption, blockquote');
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
            return parts.join('\\n').slice(0, 8000);
        }
        """)

    async def click_at_point(self, x: int, y: int) -> dict:
        """Click near (x, y) with snap-to-interactive-element.

        Uses elementFromPoint (pierces shadow DOM) to find the nearest
        interactive element and clicks its center. Returns info about
        what was clicked: {found, cx, cy, tag, type, text, method}.
        """
        page = await self._ensure_browser()
        result = await page.evaluate(_CLICK_SNAP_JS, {"x": x, "y": y})
        await page.mouse.click(result["cx"], result["cy"])
        await _human_delay(150, 400)
        return result

    async def click(self, x: int, y: int) -> None:
        """Raw click at exact viewport coordinates (no snap)."""
        page = await self._ensure_browser()
        await page.mouse.click(x, y)
        await _human_delay(150, 400)

    async def type_text(self, text: str, press_enter: bool = False,
                        clear_first: bool = False) -> None:
        page = await self._ensure_browser()
        if clear_first:
            await page.keyboard.press("Control+a")
            await _human_delay(50, 150)
        await page.keyboard.type(text, delay=random.randint(20, 50))
        if press_enter:
            await _human_delay(100, 300)
            await page.keyboard.press("Enter")
            await _human_delay(300, 600)

    async def press_key(self, key: str) -> None:
        page = await self._ensure_browser()
        await page.keyboard.press(key)
        await _human_delay(100, 300)

    async def scroll(self, direction: str = "down", amount: int = 3) -> None:
        page = await self._ensure_browser()
        delta = amount * 300 * (1 if direction == "down" else -1)
        await page.mouse.wheel(0, delta)
        await _human_delay(200, 500)

    async def back(self) -> None:
        page = await self._ensure_browser()
        await page.go_back(timeout=10000)
        await _human_delay(200, 500)

    async def get_page_title(self) -> str:
        page = await self._ensure_browser()
        return await page.title()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._chrome_proc:
            self._chrome_proc.terminate()
            self._chrome_proc.wait(timeout=5)
            self._chrome_proc = None
        if hasattr(self, '_xvfb_proc') and self._xvfb_proc:
            self._xvfb_proc.terminate()
            self._xvfb_proc.wait(timeout=5)
            self._xvfb_proc = None
        if self._pw:
            await self._pw.stop()
        self._pages = []
        self._active = 0
        self._pins = {}
        self._context = None
        self._browser = None
        self._pw = None
