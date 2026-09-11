import asyncio
import unittest

from openeyes_web import devtools as dt_mod
from openeyes_web.devtools import (DevToolsRecorder, redact_headers, redact_body, format_element,
                                   format_storage, format_resources, window_lines)


class FakeRequest:
    def __init__(self, method, url, resource_type="fetch", post_data=None, headers=None,
                 failure=None):
        self.method = method
        self.url = url
        self.resource_type = resource_type
        self.post_data = post_data
        self.headers = headers or {}
        self.failure = failure

    async def all_headers(self):
        return dict(self.headers, cookie="sid=abcdefghijklmnopqrstuvwxyz")


class FakeResponse:
    def __init__(self, request, status=200, headers=None, body=b"", status_text="OK"):
        self.request = request
        self.status = status
        self.status_text = status_text
        self.headers = headers or {}
        self._body = body

    async def all_headers(self):
        return dict(self.headers, **{"set-cookie": "sid=abcdefghijklmnopqrstuvwxyz; Path=/"})

    async def body(self):
        return self._body


TAB = 1
PAGE = "http://localhost:3000/app"


async def _drain():
    # Wait for the fire-and-forget enrichment tasks.
    pending = asyncio.all_tasks() - {asyncio.current_task()}
    if pending:
        await asyncio.gather(*pending)


class RecorderOffTests(unittest.TestCase):
    def test_off_by_default_and_records_nothing(self):
        dt = DevToolsRecorder()
        self.assertFalse(dt.enabled)
        dt.on_console(TAB, "log", "hello")
        dt.on_page_error(TAB, "boom")
        dt.on_request(TAB, FakeRequest("GET", PAGE))
        self.assertEqual(len(dt.console), 0)
        self.assertEqual(len(dt.network), 0)
        self.assertEqual(dt.summary(TAB), "")
        self.assertEqual(dt.status_line(), "DevTools: off")


class RecorderCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dt = DevToolsRecorder()
        self.dt.enable()
        self.dt.on_navigated(TAB, PAGE)

    async def _roundtrip(self, req, status=200, headers=None, body=b""):
        self.dt.on_request(TAB, req)
        resp = FakeResponse(req, status=status, headers=headers, body=body)
        self.dt.on_response(TAB, resp)
        self.dt.on_request_finished(TAB, req)
        await _drain()
        return resp

    async def test_since_action_isolates_the_last_action(self):
        self.dt.on_console(TAB, "log", "before")
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/old"))
        self.dt.mark_action()
        self.dt.on_console(TAB, "error", "after")
        await self._roundtrip(FakeRequest("POST", PAGE + "/api/new"))

        recent = self.dt.console_entries(TAB)
        self.assertEqual([e["text"] for e in recent], ["after"])
        self.assertEqual(len(self.dt.console_entries(TAB, since="all")), 2)
        net = self.dt.network_entries(TAB)
        self.assertEqual([e["url"] for e in net], [PAGE + "/api/new"])
        self.assertEqual(len(self.dt.network_entries(TAB, since="all")), 2)

    async def test_api_filter_hides_assets_and_types_all_shows_them(self):
        await self._roundtrip(FakeRequest("GET", "http://localhost:3000/a.png", "image"))
        await self._roundtrip(FakeRequest("GET", "http://localhost:3000/app.js", "script"))
        await self._roundtrip(FakeRequest("GET", "http://localhost:3000/api/x", "xhr"))
        self.assertEqual([e["type"] for e in self.dt.network_entries(TAB)], ["xhr"])
        self.assertEqual(len(self.dt.network_entries(TAB, types="all")), 3)
        self.assertEqual([e["type"] for e in self.dt.network_entries(TAB, types="image,script")],
                         ["image", "script"])

    async def test_failed_filter_and_contains(self):
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/ok"), status=200)
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/bad"), status=500)
        dead = FakeRequest("GET", PAGE + "/api/dead", failure="net::ERR_CONNECTION_REFUSED")
        self.dt.on_request(TAB, dead)
        self.dt.on_request_failed(TAB, dead)
        failed = self.dt.network_entries(TAB, types="failed")
        self.assertEqual([e["url"].rsplit("/", 1)[-1] for e in failed], ["bad", "dead"])
        self.assertEqual(failed[1]["error"], "net::ERR_CONNECTION_REFUSED")
        only = self.dt.network_entries(TAB, contains="/api/ok")
        self.assertEqual(len(only), 1)

    async def test_bodies_captured_for_api_responses_only(self):
        await self._roundtrip(FakeRequest("POST", PAGE + "/api/save", post_data='{"a":1}'),
                              headers={"content-type": "application/json"}, body=b'{"ok":true}')
        await self._roundtrip(FakeRequest("GET", "http://localhost:3000/a.png", "image"),
                              headers={"content-type": "image/png"}, body=b"\x89PNG")
        api, img = self.dt.network_entries(TAB, types="all", since="all")
        self.assertEqual(api["req_body"], '{"a":1}')
        self.assertEqual(api["resp_body"], '{"ok":true}')
        self.assertIsNone(img["resp_body"])

    async def test_json_document_body_is_captured(self):
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/list.json", "document"),
                              headers={"content-type": "application/json"}, body=b"[1,2]")
        self.assertEqual(self.dt.network_entries(TAB)[0]["resp_body"], "[1,2]")

    async def test_large_bodies_are_truncated(self):
        big = b"x" * (dt_mod.MAX_BODY + 100)
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/big"),
                              headers={"content-type": "text/plain"}, body=big)
        body = self.dt.network_entries(TAB)[0]["resp_body"]
        self.assertTrue(body.startswith("x" * dt_mod.MAX_BODY))
        self.assertIn("truncated", body)

    async def test_full_headers_are_fetched_and_secrets_redacted(self):
        req = FakeRequest("GET", PAGE + "/api/me",
                          headers={"authorization": "Bearer supersecrettoken12345"})
        await self._roundtrip(req, headers={"content-type": "application/json"}, body=b"{}")
        entry = self.dt.network_entries(TAB)[0]
        text = self.dt.format_request(entry)
        self.assertIn("cookie: sid=…(26 chars)", text)
        self.assertIn("set-cookie: sid=…(26 chars); Path=/", text)
        self.assertIn("authorization: Bearer …(28 chars)", text)
        self.assertNotIn("supersecret", text)
        self.assertNotIn("abcdefgh", text)
        revealed = self.dt.format_request(entry, reveal=True)
        self.assertIn("authorization: Bearer supersecrettoken12345", revealed)

    async def test_console_levels_and_page_errors(self):
        self.dt.on_console(TAB, "log", "l")
        self.dt.on_console(TAB, "warning", "w")
        self.dt.on_console(TAB, "error", "e")
        self.dt.on_console(TAB, "assert", "Assertion failed: x")
        self.dt.on_page_error(TAB, "TypeError: x is not a function\n    at app.js:1")
        self.assertEqual([e["level"] for e in self.dt.console_entries(TAB)],
                         ["log", "warn", "error", "error", "error"])
        self.assertEqual([e["text"] for e in self.dt.console_entries(TAB, level="ERROR")],
                         ["e", "Assertion failed: x", "Uncaught TypeError: x is not a function"])
        self.assertEqual(len(self.dt.console_entries(TAB, level="warn")), 4)
        self.assertEqual(len(self.dt.console_entries(TAB, level="log")), 1)

    async def test_active_tab_scoping(self):
        self.dt.on_console(TAB, "log", "mine")
        self.dt.on_console(2, "log", "other")
        self.assertEqual([e["text"] for e in self.dt.console_entries(TAB)], ["mine"])
        self.assertEqual(len(self.dt.console_entries(None)), 2)

    async def test_summary_line(self):
        await self._roundtrip(FakeRequest("POST", PAGE + "/api/cart"), status=500)
        await self._roundtrip(FakeRequest("GET", "http://localhost:3000/a.png", "image"))
        self.dt.on_console(TAB, "error", "Failed to load resource")
        self.dt.on_page_error(TAB, "ReferenceError: foo is not defined")
        s = self.dt.summary(TAB)
        self.assertIn("1 API request (2 total)", s)
        self.assertIn("1 failed: POST /app/api/cart → 500", s)
        self.assertIn("2 console errors: Uncaught ReferenceError", s)

    async def test_clear_and_disable(self):
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/x"))
        self.dt.clear()
        self.assertEqual(len(self.dt.network), 0)
        self.assertTrue(self.dt.enabled)
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/y"))
        self.dt.enable()  # "on" while already on starts fresh
        self.assertEqual(len(self.dt.network), 0)
        self.dt.disable()
        self.assertFalse(self.dt.enabled)
        self.dt.on_request(TAB, FakeRequest("GET", PAGE))
        self.assertEqual(len(self.dt.network), 0)

    async def test_ring_buffer_is_bounded(self):
        for i in range(dt_mod.MAX_ENTRIES + 20):
            self.dt.on_console(TAB, "log", str(i))
        self.assertEqual(len(self.dt.console), dt_mod.MAX_ENTRIES)
        self.assertEqual(self.dt.console[0]["text"], "20")

    async def test_find_request_and_formatting(self):
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/x"),
                              headers={"content-type": "application/json", "content-length": "2048"},
                              body=b"{}")
        entry = self.dt.network_entries(TAB)[0]
        self.assertIs(self.dt.find_request(entry["seq"]), entry)
        self.assertIsNone(self.dt.find_request(9999))
        line = self.dt.format_network([entry])
        self.assertIn(f"#{entry['seq']}", line)
        self.assertIn("GET /app/api/x → 200 (json, 2.0KB,", line)


    async def test_forget_tab_purges_its_entries(self):
        self.dt.on_console(TAB, "log", "mine")
        self.dt.on_console(2, "log", "other")
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/x"))
        self.dt.forget_tab(TAB)
        self.assertEqual([e["text"] for e in self.dt.console_entries(None, since="all")], ["other"])
        self.assertEqual(len(self.dt.network_entries(None, since="all")), 0)
        self.assertEqual(self.dt.console.maxlen, dt_mod.MAX_ENTRIES)

    async def test_response_for_unknown_request_is_ignored(self):
        req = FakeRequest("GET", PAGE + "/api/x")  # started before devtools went on
        self.dt.on_response(TAB, FakeResponse(req))
        self.dt.on_request_finished(TAB, req)
        self.dt.on_request_failed(TAB, req)
        self.assertEqual(len(self.dt.network), 0)

    async def test_by_request_map_is_bounded(self):
        for i in range(2 * dt_mod.MAX_ENTRIES + 5):
            self.dt.on_request(TAB, FakeRequest("GET", f"{PAGE}/ws{i}", "websocket"))
        self.assertLessEqual(len(self.dt._by_request), 2 * dt_mod.MAX_ENTRIES)

    async def test_huge_bodies_and_assets_are_not_fetched(self):
        await self._roundtrip(FakeRequest("GET", PAGE + "/api/huge"),
                              headers={"content-type": "application/json",
                                       "content-length": str(dt_mod.MAX_BODY_FETCH + 1)}, body=b"{}")
        await self._roundtrip(FakeRequest("GET", PAGE + "/a.png", "image"),
                              headers={"content-type": "image/png"}, body=b"x")
        huge, img = self.dt.network_entries(TAB, types="all")
        self.assertIsNone(huge["resp_body"])
        self.assertIsNone(img["resp_body"])
        self.assertNotIn("cookie", img["req_headers"])  # all_headers() skipped for assets

    async def test_filters_are_case_insensitive_and_limit_clamped(self):
        for i in range(5):
            await self._roundtrip(FakeRequest("GET", f"{PAGE}/api/{i}", "xhr"))
        self.assertEqual(len(self.dt.network_entries(TAB, types="API", since="ACTION")), 5)
        self.assertEqual(len(self.dt.network_entries(TAB, limit=0)), 5)
        self.assertEqual(len(self.dt.network_entries(TAB, limit=-2)), 5)
        self.assertEqual([e["url"][-1] for e in self.dt.network_entries(TAB, limit=2)], ["3", "4"])

    async def test_bodies_are_masked_unless_revealed(self):
        await self._roundtrip(FakeRequest("POST", PAGE + "/login", post_data='{"user":"a","password":"hunter2"}'),
                              headers={"content-type": "application/json"},
                              body=b'{"access_token":"eyJabc","user":"a"}')
        entry = self.dt.network_entries(TAB)[0]
        text = self.dt.format_request(entry, include_headers=False)
        self.assertIn('"password":"…(7 chars)"', text)
        self.assertIn('"access_token":"…(6 chars)"', text)
        self.assertIn('"user":"a"', text)
        self.assertNotIn("hunter2", text)
        self.assertIn("hunter2", self.dt.format_request(entry, include_headers=False, reveal=True))


class RedactTests(unittest.TestCase):
    def test_short_values_are_redacted_too(self):
        self.assertEqual(redact_headers({"authorization": "x"}), {"authorization": "…(1 chars)"})
        self.assertEqual(redact_headers({"x-api-key": "abcd1234"}), {"x-api-key": "…(8 chars)"})

    def test_substring_match_and_scheme_kept(self):
        out = redact_headers({"X-Access-Token": "a" * 40, "Accept": "*/*",
                              "Proxy-Authorization": "Basic Zm9v"})
        self.assertEqual(out["X-Access-Token"], "…(40 chars)")
        self.assertEqual(out["Accept"], "*/*")
        self.assertEqual(out["Proxy-Authorization"], "Basic …(10 chars)")

    def test_cookie_headers_keep_names_and_attributes(self):
        out = redact_headers({"cookie": "sid=abc; theme=dark",
                              "set-cookie": "sid=abcdef; Path=/; HttpOnly; SameSite=Lax"})
        self.assertEqual(out["cookie"], "sid=…(3 chars); theme=…(4 chars)")
        self.assertEqual(out["set-cookie"], "sid=…(6 chars); Path=/; HttpOnly; SameSite=Lax")

    def test_reveal_bypasses(self):
        self.assertEqual(redact_headers({"cookie": "sid=abc"}, reveal=True), {"cookie": "sid=abc"})

    def test_redact_body_json_and_form(self):
        self.assertEqual(redact_body('{"ok":true,"Secret_Key":"s3cr3t","n":1}'),
                         '{"ok":true,"Secret_Key":"…(6 chars)","n":1}')
        self.assertEqual(redact_body("username=a&password=hunter2&next=/"),
                         "username=a&password=…(7 chars)&next=/")
        self.assertEqual(redact_body('{"password":"a\\"b"}'), '{"password":"…(4 chars)"}')
        self.assertIsNone(redact_body(None))
        self.assertEqual(redact_body('{"ok":true}'), '{"ok":true}')



class InspectFormattingTests(unittest.TestCase):
    def _info(self, **over):
        base = {
            "label": "button#save.btn", "tag": "button",
            "attrs": {"id": "save", "class": "btn"}, "styles": {"display": "inline-block",
            "position": "static", "visibility": "visible", "opacity": "1", "z-index": "auto",
            "pointer-events": "none"},
            "box": {"x": 100, "y": 50, "w": 40, "h": 20}, "inViewport": True,
            "text": "Save", "children": 0, "covered": None, "ancestors": ["form#login", "body"],
            "html": "<button id=\"save\">Save</button>", "inShadow": False,
        }
        base.update(over)
        return base

    def test_box_is_scaled_to_screenshot_pixels(self):
        out = format_element(self._info(), scale=(0.7, 0.7))
        self.assertIn("Box (screenshot px): x=70 y=35 w=28 h=14", out)

    def test_covered_stack_and_wrapper(self):
        w = self._info(label="a.link", tag="a", href="http://x/")
        out = format_element(self._info(covered="div#overlay", stack=["div#overlay", "button#save.btn"],
                                        wrapper=w, disabled=True, checked=False))
        self.assertIn("⚠ Covered by: div#overlay", out)
        self.assertIn("Stack at point (top → bottom): div#overlay > button#save.btn", out)
        self.assertIn("State: disabled, unchecked", out)
        self.assertIn("Interactive wrapper: a.link", out)
        self.assertIn("Href: http://x/", out)
        self.assertIn("pointer-events: none", out)
        self.assertNotIn("visibility: visible", out)  # boring defaults are dropped

    def test_error_passthrough(self):
        self.assertEqual(format_element({"error": "Nothing at that point"}), "Nothing at that point")

    def test_storage(self):
        out = format_storage({
            "origin": "http://localhost:3000",
            "local": [["token", "abc", 3], ["big", "x" * 300 + "…", 5000]],
            "session": [],
            "cookies": [{"name": "sid", "value": "v" * 100, "domain": "localhost", "path": "/",
                         "httpOnly": True, "secure": False, "sameSite": "Lax", "expires": -1}],
            "indexeddb": ["appdb (v3)"], "cache": {"error": "SecurityError"},
        }, "all")
        self.assertIn("localStorage: 2 items", out)
        self.assertIn("  token = …(3 chars)", out)
        self.assertIn("  big = " + "x" * 300, out)
        self.assertIn("[5000 chars]", out)
        self.assertIn("sessionStorage: 0 items", out)
        self.assertIn("sid = …(100 chars)  [localhost/; HttpOnly, SameSite=Lax, session]", out)
        self.assertIn("(2 secret values hidden — pass reveal=true to show)", out)
        self.assertNotIn("vvvv", out)
        self.assertIn("IndexedDB databases: appdb (v3)", out)
        self.assertIn("Cache Storage: unavailable (SecurityError)", out)
        shown = format_storage({"origin": "o", "local": [["token", "abc", 3]],
                                "cookies": [{"name": "sid", "value": "v" * 100, "domain": "d", "path": "/",
                                             "expires": -1}]}, "all", reveal=True)
        self.assertIn("  token = abc", shown)
        self.assertIn("sid = " + "v" * 100 + "  [d/; session]", shown)
        self.assertNotIn("hidden", shown)

    def test_resources_short_urls(self):
        out = format_resources([{"url": "http://h/app.js", "type": "Script", "size": 2048},
                                {"url": "http://cdn/x.css", "type": "Stylesheet", "size": None}], "http://h/")
        self.assertIn("[Script, 2.0KB] /app.js", out)
        self.assertIn("[Stylesheet] http://cdn/x.css", out)

    def test_window_lines(self):
        content = "\n".join(f"L{i}" for i in range(1, 11))
        out = window_lines(content, start=3, lines=2)
        self.assertIn("    3  L3\n    4  L4\n", out)
        self.assertIn("[lines 3–4 of 10", out)
        self.assertIn("continue with start=5", out)
        tail = window_lines(content, start=9, lines=50)
        self.assertIn("[lines 9–10 of 10", tail)
        self.assertNotIn("continue", tail)
        self.assertIn("beyond the end — the content has 10 lines", window_lines(content, start=50))

    def test_window_lines_caps_minified(self):
        out = window_lines("x" * 50_000, max_chars=100)
        self.assertIn("output capped at 100 chars", out)
        self.assertLess(len(out), 300)


class ListenerPage:
    """Counts listeners per event — what Playwright uses to decide whether the
    Node driver streams console/network events to Python at all."""
    def __init__(self, url="http://localhost:3000/"):
        self.url = url
        self.listeners: dict[str, list] = {}

    def on(self, event, fn):
        self.listeners.setdefault(event, []).append(fn)

    def remove_listener(self, event, fn):
        self.listeners[event].remove(fn)

    def count(self, event):
        return len(self.listeners.get(event, []))


class LazyListenerTests(unittest.TestCase):
    """Off must really be off: no console/request listeners while disabled."""
    RECORDED = ("console", "request", "response", "requestfinished", "requestfailed", "pageerror")

    def _manager(self):
        from openeyes_web.browser import BrowserManager
        m = BrowserManager()
        m._pages = [ListenerPage(), ListenerPage("http://b/")]
        return m

    def test_watch_page_attaches_nothing_while_off(self):
        m = self._manager()
        for page in m._pages:
            m._watch_page(page)
        for page in m._pages:
            for ev in self.RECORDED:
                self.assertEqual(page.count(ev), 0, ev)
            self.assertEqual(page.count("framenavigated"), 1)  # unrelated hooks still wired

    def test_on_attaches_to_every_tab_and_off_detaches(self):
        m = self._manager()
        for page in m._pages:
            m._watch_page(page)
        m.set_devtools(True)
        self.assertTrue(m.devtools.enabled)
        for page in m._pages:
            for ev in self.RECORDED:
                self.assertEqual(page.count(ev), 1, ev)
        m.set_devtools(True)  # idempotent — no duplicate listeners
        self.assertEqual(m._pages[0].count("request"), 1)
        m.set_devtools(False)
        self.assertFalse(m.devtools.enabled)
        for page in m._pages:
            for ev in self.RECORDED:
                self.assertEqual(page.count(ev), 0, ev)

    def test_new_tab_while_on_is_recorded_and_closing_cleans_up(self):
        m = self._manager()
        m.set_devtools(True)
        late = ListenerPage("http://c/")
        m._pages.append(late)
        m._watch_page(late)
        self.assertEqual(late.count("request"), 1)
        m._forget_page(late)
        self.assertNotIn(id(late), m._dt_listeners)
        self.assertNotIn(id(late), m.devtools._page_url)


if __name__ == "__main__":
    unittest.main()
