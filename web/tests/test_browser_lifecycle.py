import asyncio
import os
import unittest
from unittest.mock import patch

from openeyes_web.browser import BrowserManager, _harden_chrome_command


class FakePage:
    def __init__(self, url="about:blank"):
        self.url = url
        self.closed = False
        self.viewport = None
        self.handlers = {}

    def on(self, event, callback):
        self.handlers[event] = callback

    async def set_viewport_size(self, viewport):
        self.viewport = viewport

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, delay=0):
        self.created = []
        self.delay = delay

    async def new_page(self):
        await asyncio.sleep(self.delay)
        page = FakePage()
        self.created.append(page)
        return page


class FakeBrowser:
    def is_connected(self):
        return True


class BrowserLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def test_managed_chrome_disables_crash_reporters(self):
        command = _harden_chrome_command(["/path/to/chrome", "about:blank"])

        self.assertEqual(command[0], "/path/to/chrome")
        self.assertIn("--disable-breakpad", command)
        self.assertNotIn("--disable-crashpad-for-testing", command)
        self.assertEqual(command[-1], "about:blank")

    async def test_empty_page_list_reuses_initialized_browser(self):
        manager = BrowserManager()
        context = FakeContext()
        manager._browser = FakeBrowser()
        manager._context = context
        manager._pw = object()

        with patch("openeyes_web.browser.async_playwright") as playwright:
            page = await manager._ensure_browser()

        playwright.start.assert_not_called()
        self.assertIs(page, context.created[0])
        self.assertEqual(manager._pages, [page])
        self.assertEqual(page.viewport, {"width": 1280, "height": 900})

    async def test_concurrent_empty_page_recovery_creates_only_one_page(self):
        manager = BrowserManager()
        context = FakeContext(delay=0.01)
        manager._browser = FakeBrowser()
        manager._context = context
        manager._pw = object()

        first, second = await asyncio.gather(
            manager._ensure_browser(), manager._ensure_browser()
        )

        self.assertIs(first, second)
        self.assertEqual(len(context.created), 1)
        self.assertEqual(manager._pages, [first])

    async def test_tab_limit_evicts_oldest_unpinned_background_tab(self):
        with patch.dict(os.environ, {"OPENEYES_WEB_MAX_TABS": "2"}):
            manager = BrowserManager()
        oldest = FakePage("https://old.example")
        pinned = FakePage("https://pinned.example")
        newest = FakePage("https://new.example")
        manager._pages = [oldest, pinned, newest]
        manager._active = 2
        manager._pins[id(pinned)] = "keep"

        await manager._enforce_tab_limit(protected=newest)

        self.assertTrue(oldest.closed)
        self.assertFalse(pinned.closed)
        self.assertFalse(newest.closed)
        self.assertEqual(manager._pages, [pinned, newest])
        self.assertEqual(manager._active, 1)


if __name__ == "__main__":
    unittest.main()
