"""DevTools recorder — opt-in capture of console output and network traffic.

Off by default: a browsing agent never pays for this. When an agent turns it
on (to debug a site it is developing) every tab's console messages, uncaught
errors and HTTP requests/responses are kept in bounded ring buffers, and the
agent reads them back on demand with compact, filterable views.

Only the metadata is captured eagerly. Response bodies are fetched in the
background for API-shaped responses (xhr/fetch, or anything JSON) and capped,
so a busy page can't balloon memory or block the event loop.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

# Bounds — generous enough for a debugging session, small enough to be harmless.
MAX_ENTRIES = 500          # per buffer (console / network), oldest dropped first
MAX_TEXT = 500             # chars kept per console message
MAX_BODY = 16_000          # chars of request/response body kept
BODY_FETCH_TIMEOUT = 5.0   # seconds to wait for a response body
MAX_BODY_FETCH = 2_000_000 # skip bodies advertised larger than this

# Names (headers, cookies, storage keys, JSON/form fields) whose values are
# session secrets. Matched by substring, case-insensitive. Values are replaced
# by "…(N chars)" — keeping the scheme word ("Bearer …") — so the agent still
# sees *whether* auth was sent and how long it is, but the secret never lands
# in the model context or the logs. reveal=True on the tools bypasses this.
_SECRET_NAMES = ("cookie", "auth", "token", "secret", "passw", "session", "credential",
                 "api-key", "apikey", "api_key", "jwt", "csrf", "xsrf")

# "API" traffic = what a developer usually means by "which requests fired".
# preflight/ping: a failed CORS preflight is exactly why "the form does nothing".
API_TYPES = {"xhr", "fetch", "eventsource", "websocket", "document", "other", "preflight", "ping"}
# Static assets are noise unless explicitly asked for.
ASSET_TYPES = {"stylesheet", "image", "media", "font", "script", "texttrack", "manifest"}

_TEXTUAL_CT = ("json", "text/", "xml", "javascript", "x-www-form-urlencoded", "graphql")


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _short_url(url: str, page_url: str) -> str:
    """Drop the origin when it matches the page's own — keeps API lists readable."""
    try:
        u, p = urlparse(url), urlparse(page_url)
        if u.scheme == p.scheme and u.netloc == p.netloc:
            path = u.path or "/"
            return path + (f"?{u.query}" if u.query else "")
    except Exception:
        pass
    return url


def _fmt_size(n: int | None) -> str:
    if n is None:
        return "?"
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _ct_short(ct: str) -> str:
    """'application/json; charset=utf-8' -> 'json'."""
    ct = (ct or "").split(";", 1)[0].strip().lower()
    if not ct:
        return "-"
    sub = ct.split("/", 1)[-1]
    for k in ("json", "html", "javascript", "css", "xml", "plain", "form"):
        if k in sub:
            return k
    return sub[:12]


def _clip(s: str | None, limit: int) -> str | None:
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n…[truncated — {len(s)} chars total]"


def is_secret_name(name: str) -> bool:
    n = (name or "").lower()
    return any(k in n for k in _SECRET_NAMES)


def redact_value(v: str) -> str:
    """'Bearer eyJ…' -> 'Bearer …(28 chars)'; anything else -> '…(N chars)'."""
    v = "" if v is None else str(v)
    scheme = v.split(" ", 1)[0] if " " in v and v.split(" ", 1)[0].isalpha() else ""
    return (f"{scheme} " if scheme else "") + f"…({len(v)} chars)"


def _redact_cookie_pairs(v: str) -> str:
    """'sid=abc; theme=dark' -> 'sid=…(3 chars); theme=…(4 chars)' (names kept)."""
    parts = []
    for pair in v.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        if "=" in pair:
            name, val = pair.split("=", 1)
            parts.append(f"{name.strip()}={redact_value(val)}")
        else:
            parts.append(pair)
    return "; ".join(parts)


def redact_headers(headers: dict, reveal: bool = False) -> dict:
    if reveal:
        return dict(headers or {})
    out = {}
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk == "cookie":
            out[k] = _redact_cookie_pairs(str(v))
        elif lk == "set-cookie":
            # Keep the attributes (Path, HttpOnly, …) — only the value is secret.
            first, _, attrs = str(v).partition(";")
            out[k] = _redact_cookie_pairs(first) + (f";{attrs}" if attrs else "")
        elif is_secret_name(lk):
            out[k] = redact_value(str(v))
        else:
            out[k] = v
    return out


# JSON  "password": "hunter2"   and   form  password=hunter2&next=/
_JSON_SECRET_RE = re.compile(r'("[^"]*?(?:%s)[^"]*?"\s*:\s*")((?:[^"\\]|\\.)*)(")' % "|".join(_SECRET_NAMES), re.I)
_FORM_SECRET_RE = re.compile(r'(^|[&\n])([^=&\n]*(?:%s)[^=&\n]*=)([^&\n]*)' % "|".join(_SECRET_NAMES), re.I)


def redact_body(body: str | None, reveal: bool = False) -> str | None:
    """Mask values of secret-looking fields in a JSON or form-encoded body."""
    if body is None or reveal:
        return body
    body = _JSON_SECRET_RE.sub(lambda m: m.group(1) + redact_value(m.group(2)) + m.group(3), body)
    body = _FORM_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + redact_value(m.group(3)), body)
    return body


class DevToolsRecorder:
    """Session-wide buffers keyed by tab. Entries carry a monotonically
    increasing ``seq`` so "since my last action" is a cheap comparison."""

    def __init__(self):
        self.enabled = False
        self.enabled_at: float | None = None
        self._seq = 0
        self._action_seq = 0          # seq watermark at the start of the last action
        self.console: deque[dict] = deque(maxlen=MAX_ENTRIES)
        self.network: deque[dict] = deque(maxlen=MAX_ENTRIES)
        self._by_request: dict[int, dict] = {}  # id(request) -> network entry
        self._page_url: dict[int, str] = {}     # tab_key -> last known page URL
        self._enrich_slots = asyncio.Semaphore(8)  # concurrent driver round-trips

    # --- lifecycle ---

    def enable(self) -> None:
        """Start (or restart) recording with empty buffers."""
        self.enabled = True
        self.enabled_at = time.time()
        self.clear()

    def disable(self) -> None:
        self.enabled = False
        self.enabled_at = None
        self.clear()

    def clear(self) -> None:
        self.console.clear()
        self.network.clear()
        self._by_request.clear()
        self._action_seq = self._seq

    def mark_action(self) -> None:
        """Called at the start of every agent action (click, navigate, …) so
        the default views show only what that action caused."""
        self._action_seq = self._seq

    def forget_tab(self, tab_key: int) -> None:
        """Drop everything about a closed tab — CPython reuses ids, so a later
        tab must not inherit its buffered lines."""
        self._page_url.pop(tab_key, None)
        if any(e["tab"] == tab_key for e in self.console):
            self.console = deque((e for e in self.console if e["tab"] != tab_key), maxlen=MAX_ENTRIES)
        if any(e["tab"] == tab_key for e in self.network):
            self.network = deque((e for e in self.network if e["tab"] != tab_key), maxlen=MAX_ENTRIES)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # --- capture (called from Playwright event handlers) ---

    def on_console(self, tab_key: int, level: str, text: str, location: str = "") -> None:
        if not self.enabled:
            return
        level = {"warning": "warn", "assert": "error"}.get(level, level or "log")
        self.console.append({
            "seq": self._next_seq(), "tab": tab_key, "ts": _now_hms(),
            "level": level, "text": _clip(text, MAX_TEXT) or "", "loc": location,
        })

    def on_page_error(self, tab_key: int, message: str) -> None:
        if not self.enabled:
            return
        first = (message or "").strip().split("\n", 1)[0]
        self.console.append({
            "seq": self._next_seq(), "tab": tab_key, "ts": _now_hms(),
            "level": "error", "text": _clip("Uncaught " + first, MAX_TEXT) or "", "loc": "",
        })

    def on_request(self, tab_key: int, request) -> None:
        if not self.enabled:
            return
        try:
            post = request.post_data
        except Exception:
            post = None
        entry = {
            "seq": self._next_seq(), "tab": tab_key, "ts": _now_hms(),
            "t0": time.monotonic(),
            "method": request.method, "url": request.url,
            "type": request.resource_type, "page": self._page_url.get(tab_key, ""),
            "req_headers": dict(request.headers or {}),
            "req_body": _clip(post, MAX_BODY),
            "status": None, "status_text": "", "mime": "", "size": None,
            "ms": None, "resp_headers": {}, "resp_body": None, "error": None,
        }
        self.network.append(entry)
        self._by_request[id(request)] = entry
        if len(self._by_request) > 2 * MAX_ENTRIES:
            # Requests that never reported finished/failed — drop the oldest.
            for k in list(self._by_request)[:MAX_ENTRIES]:
                self._by_request.pop(k, None)

    def on_response(self, tab_key: int, response) -> None:
        if not self.enabled:
            return
        entry = self._by_request.get(id(response.request))
        if entry is None:
            return
        entry["status"] = response.status
        entry["status_text"] = response.status_text
        headers = dict(response.headers or {})
        entry["resp_headers"] = headers
        entry["mime"] = headers.get("content-type", "")
        try:
            entry["size"] = int(headers.get("content-length")) if headers.get("content-length") else None
        except ValueError:
            entry["size"] = None
        entry["ms"] = int((time.monotonic() - entry["t0"]) * 1000)
        asyncio.ensure_future(self._enrich(entry, response))

    def on_request_finished(self, tab_key: int, request) -> None:
        entry = self._by_request.pop(id(request), None)
        if entry is not None and entry["ms"] is None:
            entry["ms"] = int((time.monotonic() - entry["t0"]) * 1000)

    def on_request_failed(self, tab_key: int, request) -> None:
        entry = self._by_request.pop(id(request), None)
        if entry is None:
            return
        try:
            failure = request.failure
        except Exception:
            failure = None
        entry["error"] = failure or "failed"
        entry["ms"] = int((time.monotonic() - entry["t0"]) * 1000)

    def on_navigated(self, tab_key: int, url: str) -> None:
        self._page_url[tab_key] = url

    @staticmethod
    def _wants_body(entry: dict) -> bool:
        ct = (entry["mime"] or "").lower()
        if entry["size"] is not None and entry["size"] > MAX_BODY_FETCH:
            return False
        if entry["type"] in ("xhr", "fetch", "eventsource"):
            return not ct or any(k in ct for k in _TEXTUAL_CT)
        return "json" in ct  # e.g. a JSON document loaded via navigate()

    async def _enrich(self, entry: dict, response) -> None:
        """Background: complete headers (the sync ``.headers`` omit cookie/auth
        ones) and, for API-shaped responses, the body. Assets get neither —
        nobody inspects an image's headers, and a page load can fire hundreds."""
        if entry["type"] in ASSET_TYPES:
            return
        async with self._enrich_slots:
            for key, fn in (("req_headers", response.request.all_headers),
                            ("resp_headers", response.all_headers)):
                try:
                    entry[key] = dict(await asyncio.wait_for(fn(), BODY_FETCH_TIMEOUT))
                except Exception:
                    pass  # keep the partial sync headers
            if not self._wants_body(entry):
                return
            try:
                raw = await asyncio.wait_for(response.body(), BODY_FETCH_TIMEOUT)
            except Exception as e:
                entry["resp_body"] = f"[body unavailable: {str(e).split(chr(10), 1)[0][:80]}]"
                return
            if entry["size"] is None:
                entry["size"] = len(raw)
            entry["resp_body"] = _clip(raw.decode("utf-8", errors="replace"), MAX_BODY)

    # --- queries ---

    def _since(self, since: str) -> int:
        return self._action_seq if (since or "action").lower() == "action" else 0

    @staticmethod
    def _cap(out: list, limit: int) -> list:
        """Newest ``limit`` entries; limit <= 0 means all."""
        return out[-limit:] if limit and limit > 0 else out

    def console_entries(self, tab_key: int | None, level: str = "all",
                        since: str = "action", limit: int = 50) -> list[dict]:
        floor = self._since(since)
        level = (level or "all").lower()
        want = None
        if level != "all":
            want = {"error": {"error"}, "warn": {"warn", "error"},
                    "log": {"log", "info", "debug", "trace"}}.get(level, {level})
        out = [e for e in self.console
               if e["seq"] > floor and (tab_key is None or e["tab"] == tab_key)
               and (want is None or e["level"] in want)]
        return self._cap(out, limit)

    def network_entries(self, tab_key: int | None, types: str = "api",
                        since: str = "action", contains: str = "",
                        limit: int = 50) -> list[dict]:
        floor = self._since(since)
        types = (types or "api").lower()
        if types == "all":
            allowed = None
        elif types == "api":
            allowed = API_TYPES
        elif types == "failed":
            allowed = None
        else:
            allowed = {t.strip().lower() for t in types.split(",") if t.strip()}
        needle = (contains or "").lower()
        out = []
        for e in self.network:
            if e["seq"] <= floor or (tab_key is not None and e["tab"] != tab_key):
                continue
            if allowed is not None and e["type"] not in allowed:
                continue
            if types == "failed" and not self._is_failed(e):
                continue
            if needle and needle not in e["url"].lower():
                continue
            out.append(e)
        return self._cap(out, limit)

    def find_request(self, seq: int) -> dict | None:
        for e in self.network:
            if e["seq"] == seq:
                return e
        return None

    @staticmethod
    def _is_failed(e: dict) -> bool:
        return bool(e["error"]) or (e["status"] is not None and e["status"] >= 400)

    def summary(self, tab_key: int | None) -> str:
        """One line for action responses: what the last action caused."""
        if not self.enabled:
            return ""
        floor = self._action_seq
        reqs = [e for e in self.network if e["seq"] > floor and (tab_key is None or e["tab"] == tab_key)]
        cons = [e for e in self.console if e["seq"] > floor and (tab_key is None or e["tab"] == tab_key)]
        api = [e for e in reqs if e["type"] in API_TYPES]
        failed = [e for e in reqs if self._is_failed(e)]
        errors = [e for e in cons if e["level"] == "error"]
        warns = [e for e in cons if e["level"] == "warn"]
        pending = [e for e in reqs if e["status"] is None and not e["error"]]

        parts = [f"{len(api)} API request{'s' if len(api) != 1 else ''}"
                 + (f" ({len(reqs)} total)" if len(reqs) != len(api) else "")]
        if failed:
            head = ", ".join(self._fail_label(e) for e in failed[:3])
            parts.append(f"{len(failed)} failed: {head}" + (" …" if len(failed) > 3 else ""))
        if pending:
            parts.append(f"{len(pending)} pending")
        if errors:
            first = next((e for e in errors if e["text"].startswith("Uncaught")), errors[0])
            parts.append(f"{len(errors)} console error{'s' if len(errors) != 1 else ''}: "
                         + first["text"][:80].replace("\n", " "))
        elif warns:
            parts.append(f"{len(warns)} console warning{'s' if len(warns) != 1 else ''}")
        elif cons:
            parts.append(f"{len(cons)} console line{'s' if len(cons) != 1 else ''}")
        return "DevTools: " + "; ".join(parts) + ". (get_network / get_console for details)"

    def _fail_label(self, e: dict) -> str:
        what = f"{e['method']} {_short_url(e['url'], e['page'])[:60]}"
        return f"{what} → {e['error']}" if e["error"] else f"{what} → {e['status']}"

    # --- formatting ---

    def format_console(self, entries: list[dict]) -> str:
        if not entries:
            return "(no console output)"
        lines = []
        for e in entries:
            loc = f"  ({e['loc']})" if e["loc"] else ""
            text = e["text"].replace("\n", "\n    ")
            lines.append(f"[{e['level']}] {e['ts']} {text}{loc}")
        return "\n".join(lines)

    def format_network(self, entries: list[dict]) -> str:
        if not entries:
            return "(no matching requests)"
        lines = []
        for e in entries:
            url = _short_url(e["url"], e["page"])
            if len(url) > 100:
                url = url[:97] + "…"
            if e["error"]:
                result = f"✗ {e['error']}"
            elif e["status"] is None:
                result = "… pending"
            else:
                result = f"{e['status']}"
                extra = [x for x in (_ct_short(e["mime"]) if e["mime"] else "",
                                     _fmt_size(e["size"]) if e["size"] is not None else "",
                                     f"{e['ms']}ms" if e["ms"] is not None else "") if x]
                if extra:
                    result += f" ({', '.join(extra)})"
            tag = "" if e["type"] in ("xhr", "fetch") else f" [{e['type']}]"
            lines.append(f"#{e['seq']} {e['ts']} {e['method']} {url}{tag} → {result}")
        return "\n".join(lines)

    def format_request(self, e: dict, include_headers: bool = True, reveal: bool = False) -> str:
        out = [f"#{e['seq']} {e['method']} {e['url']}",
               f"Type: {e['type']}  Time: {e['ts']}" + (f"  Duration: {e['ms']}ms" if e["ms"] is not None else "")]
        if e["error"]:
            out.append(f"Result: FAILED — {e['error']}")
        elif e["status"] is None:
            out.append("Result: pending (no response yet)")
        else:
            out.append(f"Result: {e['status']} {e['status_text']}".rstrip()
                       + (f"  {e['mime']}" if e["mime"] else "")
                       + (f"  {_fmt_size(e['size'])}" if e["size"] is not None else ""))
        if include_headers:
            out.append("\nRequest headers:")
            out += [f"  {k}: {v}" for k, v in redact_headers(e["req_headers"], reveal).items()]
        if e["req_body"]:
            out.append(f"\nRequest body:\n{redact_body(e['req_body'], reveal)}")
        if include_headers and e["resp_headers"]:
            out.append("\nResponse headers:")
            out += [f"  {k}: {v}" for k, v in redact_headers(e["resp_headers"], reveal).items()]
        if e["resp_body"] is not None:
            out.append(f"\nResponse body:\n{redact_body(e['resp_body'], reveal)}")
        elif e["status"] is not None and not e["error"]:
            out.append("\nResponse body: (not captured — only API/JSON bodies are kept)")
        return "\n".join(out)

    def status_line(self) -> str:
        if not self.enabled:
            return "DevTools: off"
        age = int(time.time() - (self.enabled_at or time.time()))
        return (f"DevTools: on for {age}s — {len(self.network)} requests, "
                f"{len(self.console)} console lines buffered (max {MAX_ENTRIES} each)")



# ---------------------------------------------------------------------------
# Elements / Sources / Application formatting (pull-based — no recording needed)
# ---------------------------------------------------------------------------

# Computed-style values that are the browser default and therefore say nothing.
# display/position are always shown; "none" is never boring (display:none,
# pointer-events:none are exactly what a debugger looks for).
_BORING_STYLE = {
    "visibility": ("visible",), "opacity": ("1",), "z-index": ("auto",),
    "pointer-events": ("auto",), "overflow": ("visible",), "cursor": ("auto", "default"),
}


def format_element(info: dict, scale: tuple[float, float] = (1.0, 1.0), heading: str = "") -> str:
    """Render an inspect_element() result. ``scale`` converts viewport px to
    screenshot px so the box matches what the agent sees."""
    if info.get("error"):
        return info["error"]
    sx, sy = scale
    b = info["box"]
    lines = [heading or f"Element: {info['label']}"]
    if info.get("matches", 1) > 1:
        lines[0] += f"  (first of {info['matches']} matches)"
    box = f"x={int(b['x'] * sx)} y={int(b['y'] * sy)} w={int(b['w'] * sx)} h={int(b['h'] * sy)}"
    lines.append(f"Box (screenshot px): {box}" + ("" if info.get("inViewport") else "  — OUTSIDE viewport"))
    if info.get("covered"):
        lines.append(f"⚠ Covered by: {info['covered']} — clicks at its center hit that element instead")
    if info.get("inShadow"):
        lines.append("In shadow DOM")
    if len(info.get("stack") or []) > 1:
        lines.append("Stack at point (top → bottom): " + " > ".join(info["stack"]))
    flags = []
    if info.get("disabled"):
        flags.append("disabled")
    if "checked" in info:
        flags.append("checked" if info["checked"] else "unchecked")
    if flags:
        lines.append("State: " + ", ".join(flags))
    if info.get("value") not in (None, ""):
        lines.append(f"Value: {info['value']}")
    if info.get("href"):
        lines.append(f"Href: {info['href']}")
    if info.get("form"):
        lines.append(f"Form: {info['form']['method'].upper()} {info['form']['action']}")
    if info.get("inlineHandlers"):
        lines.append("Inline handlers: " + ", ".join(info["inlineHandlers"]))
    if info.get("text"):
        lines.append(f"Text: {info['text'][:200]!r}")
    if info.get("attrs"):
        lines.append("Attributes: " + "  ".join(f'{k}="{v}"' for k, v in info["attrs"].items()))
    st = info.get("styles") or {}
    interesting = {k: v for k, v in st.items() if v and v not in _BORING_STYLE.get(k, ())}
    lines.append("Styles: " + "; ".join(f"{k}: {v}" for k, v in interesting.items()))
    if info.get("ancestors"):
        lines.append("Ancestors: " + " < ".join(info["ancestors"]))
    lines.append(f"Children: {info.get('children', 0)}")
    if info.get("html"):
        lines.append(f"HTML:\n{info['html']}")
    if info.get("wrapper"):
        w = info["wrapper"]
        lines.append("")
        lines.append(format_element(w, scale, heading=f"Interactive wrapper: {w['label']}")
                     .split("HTML:\n", 1)[0].rstrip())
    return "\n".join(lines)


def format_storage(res: dict, kind: str, reveal: bool = False) -> str:
    lines = [f"Storage for {res.get('origin', '?')} (kind={kind})"]
    hidden = 0

    def area(name, items):
        nonlocal hidden
        if isinstance(items, dict) and items.get("error"):
            lines.append(f"\n{name}: unavailable ({items['error']})")
            return
        lines.append(f"\n{name}: {len(items)} item{'s' if len(items) != 1 else ''}")
        for k, v, n in items:
            if not reveal and is_secret_name(k):
                lines.append(f"  {k} = …({n} chars)")
                hidden += 1
                continue
            size = f"  [{n} chars]" if n > len(v) else ""
            lines.append(f"  {k} = {v}{size}")

    if "local" in res:
        area("localStorage", res["local"])
    if "session" in res:
        area("sessionStorage", res["session"])
    if "cookies" in res:
        cs = res["cookies"]
        lines.append(f"\nCookies: {len(cs)}" + (f"  (error: {res['cookies_error']})" if res.get("cookies_error") else ""))
        for c in cs:
            flags = [f for f, on in (("HttpOnly", c.get("httpOnly")), ("Secure", c.get("secure"))) if on]
            if c.get("sameSite"):
                flags.append(f"SameSite={c['sameSite']}")
            exp = c.get("expires", -1)
            if exp and exp > 0:
                flags.append("expires " + datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M"))
            else:
                flags.append("session")
            val = c.get("value", "")
            if not reveal:
                val = redact_value(val)
                hidden += 1
            elif len(val) > 200:
                val = val[:200] + f"…({len(val)} chars)"
            lines.append(f"  {c.get('name')} = {val}  [{c.get('domain')}{c.get('path', '/')}; {', '.join(flags)}]")
    if "indexeddb" in res:
        idb = res["indexeddb"]
        if isinstance(idb, dict):
            lines.append(f"\nIndexedDB: unavailable ({idb.get('error')})")
        else:
            lines.append(f"\nIndexedDB databases: {', '.join(idb) if idb else 'none'}")
    if "cache" in res:
        c = res["cache"]
        if isinstance(c, dict):
            lines.append(f"\nCache Storage: unavailable ({c.get('error')})")
        else:
            lines.append(f"\nCache Storage: {', '.join(c) if c else 'none'}")
    if hidden:
        lines.append(f"\n({hidden} secret value{'s' if hidden != 1 else ''} hidden — pass reveal=true to show)")
    return "\n".join(lines)


def format_resources(resources: list[dict], page_url: str) -> str:
    if not resources:
        return "(no resources loaded)"
    lines = []
    for r in resources:
        size = f", {_fmt_size(int(r['size']))}" if r.get("size") else ""
        lines.append(f"  [{r['type']}{size}] {_short_url(r['url'], page_url)[:120]}")
    return "\n".join(lines)


def window_lines(content: str, start: int = 1, lines: int = 150, max_chars: int = 12_000) -> str:
    """Lines [start, start+lines) of ``content``, 1-based, with a footer saying
    what was omitted. Minified files (one huge line) are capped by characters."""
    all_lines = content.split("\n")
    total = len(all_lines)
    start = max(1, start)
    if start > total:
        return f"[start={start} is beyond the end — the content has {total} lines, {len(content)} chars]"
    chunk = all_lines[start - 1:start - 1 + max(1, lines)]
    body = "\n".join(f"{start + i:>5}  {l}" for i, l in enumerate(chunk))
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars] + "…"
    end = start + len(chunk) - 1
    foot = f"[lines {start}–{end} of {total}, {len(content)} chars"
    if truncated:
        foot += f"; output capped at {max_chars} chars — minified? use a smaller `lines` window"
    elif end < total:
        foot += f"; continue with start={end + 1}"
    return body + "\n" + foot + "]"
