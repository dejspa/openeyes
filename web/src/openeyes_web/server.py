"""MCP server — vision-first browser controller. Clean screenshots + coordinate-based clicking.

Multi-session isolation: each MCP client gets its own BrowserManager on a dedicated
CDP port. Session ID comes from ctx.client_id (SSE) or OPENEYES_WEB_SESSION env var (stdio).
Sessions inactive > 48h are auto-cleaned (Chrome killed, state kept).
"""

from __future__ import annotations

import asyncio
import fcntl
import functools
import hashlib
import json
import os
import select
import signal
import socket
import stat
import sys
import tempfile
import time
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP, Context, Image as MCPImage

from .browser import BrowserManager
from .tracker import PageMemory
from .vision import VisionPipeline, estimate_image_tokens

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
- set_device(device) — view the site as "desktop" (default) or "mobile"/"android"/"ipad"
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
- If your click misses (no interactive element there), the response lists the nearest clickable elements with their coordinates — use those to retry precisely instead of guessing again.
- To type into a field: click its coordinates first (to focus it), then use type_text().
- To search: click the search field, then type_text(query, press_enter=true, clear_first=true).
- Some actions return text-only feedback (no screenshot) when the page didn't visually change. Use screenshot() if you need to see the current state.

IMPORTANT BEHAVIORS:
- If a cookie banner, ad interstitial, or overlay blocks the page, click its accept/dismiss/close button.
- When a site opens content in a new tab (e.g. a report or checkout via target=_blank), it's adopted and becomes the active tab; blank ad-popups are auto-closed. Use list_tabs() to see what's open.
- If navigate() reports a page "did not fully load", the site was slow or unreachable — the screenshot shows whatever rendered; retry or try a different URL.

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
_HISTORY_RETENTION_DAYS = 14
_PROCESS_EXIT_TIMEOUT = 5.0
_SESSION_FILE = "/tmp/openeyes-web-sessions.json"

_browsers: dict[str, BrowserManager] = {}
_vision: dict[str, VisionPipeline] = {}
_memory: dict[str, PageMemory] = {}
_page_tokens: dict[int, int] = {}  # id(page) -> cumulative tokens
_current_model: str = os.environ.get("OPENEYES_WEB_MODEL", "unknown")
_session_ports: dict[str, int] = {}  # session_id -> CDP port
_last_active: dict[str, float] = {}  # session_id -> unix timestamp
_locks: dict[str, asyncio.Lock] = {}  # session_id -> serialize tool calls
_cleanup_started = False

_TOKEN_LOG = os.path.expanduser("~/.openeyes/web/token-log.jsonl")
_HISTORY_ROOT = os.path.expanduser("~/.openeyes/web/history")

# In stdio mode every client gets its own server process, so the client is
# identifiable — but not by our own cwd: the launcher is typically
# "uv run --directory <openeyes>", which puts every instance in the SAME
# directory. Walk up to the process that actually spawned us (the agent) and
# key on ITS working directory instead. Without this, all Claude Code agents
# on a machine report the same clientInfo.name ("claude-code"), land on one
# session and share ONE Chrome — their calls then serialize behind each other
# until they time out (two QA runs lost this way on 2026-08-04).
# The agent's cwd is stable across reconnections, so it keeps its own cookies
# and logged-in state. SSE mode keeps the old behaviour: one shared server
# process means the parent says nothing about who is calling.
_SHARED_TRANSPORT = (len(sys.argv) > 1 and sys.argv[1] in ("sse", "serve", "http"))
_LAUNCHER_COMMS = {"uv", "uvx", "python", "python3", "sh", "bash", "zsh", "dash", "env"}


def _client_key() -> str:
    """Identify the process that launched this server: its cwd, else its pid."""
    try:
        pid = os.getppid()
        for _ in range(6):
            if pid <= 1:
                break
            with open(f"/proc/{pid}/comm") as f:
                comm = f.read().strip()
            if comm not in _LAUNCHER_COMMS:
                try:
                    return os.readlink(f"/proc/{pid}/cwd")
                except OSError:
                    return f"pid{pid}"
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])  # ppid
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.getcwd()
    except OSError:
        return ""


def _instance_suffix() -> str:
    if _SHARED_TRANSPORT:
        return ""
    key = _client_key()
    return "-" + hashlib.sha1(key.encode()).hexdigest()[:8] if key else ""


def _bg(fn) -> None:
    """Run a small blocking I/O job off the event loop (fire and forget)."""
    try:
        asyncio.get_running_loop().run_in_executor(None, fn)
    except RuntimeError:
        fn()  # no running loop (sync context) — just do it


def _session_id(ctx: Context | None) -> str:
    """Resolve session ID from MCP context.

    Uses the client-provided name from InitializeRequest (e.g. 'claude-code')
    plus, in stdio mode, a short hash of the working directory — the client
    name alone is the same for every Claude Code agent on the machine, so it
    identifies the *program*, not the caller. The pair is stable across
    reconnections, so an agent keeps its own Chrome, cookies and logged-in
    state, while two agents never land on the same browser.
    Set OPENEYES_WEB_SESSION to pin a session explicitly (amux does this per
    tmux session, which also gives sibling agents in one worktree their own).
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
                        return name + _instance_suffix()
    return "default" + _instance_suffix()


def _lock_for(sid: str) -> asyncio.Lock:
    lock = _locks.get(sid)
    if lock is None:
        lock = asyncio.Lock()
        _locks[sid] = lock
    return lock


def _guard(fn):
    """Serialize a tool's calls per session so overlapping requests (e.g. two SSE
    calls to the same session) can't interleave awaits on the one shared page."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        ctx = kwargs.get("ctx") or next((a for a in args if isinstance(a, Context)), None)
        async with _lock_for(_session_id(ctx)):
            return await fn(*args, **kwargs)
    return wrapper


# --- Session file: shared across processes, so guard read-modify-write with flock ---

def _open_owned_regular(path: str, flags: int, mode: int = 0o600) -> int:
    """Open a private state file without following a predictable /tmp symlink."""
    fd = os.open(path, flags | os.O_CLOEXEC | os.O_NOFOLLOW, mode)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError(f"unsafe OpenEyes state file: {path}")
    os.fchmod(fd, mode)
    return fd


@contextmanager
def _session_lock():
    fd = _open_owned_regular(_SESSION_FILE + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_sessions() -> dict[str, dict]:
    try:
        fd = _open_owned_regular(_SESSION_FILE, os.O_RDONLY)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("OpenEyes session registry must contain a JSON object")
    return data


def _save_sessions(data: dict[str, dict]) -> None:
    """Atomically write private state without a predictable temporary path."""
    tmp = None
    try:
        directory = os.path.dirname(_SESSION_FILE) or "."
        fd, tmp = tempfile.mkstemp(prefix=".openeyes-sessions-", dir=directory)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _SESSION_FILE)
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def _update_session_record(session_id: str, port: int, chrome_pid: int | None = None) -> None:
    with _session_lock():
        data = _load_sessions()
        rec = data.get(session_id, {})
        rec["port"] = port
        rec["last_active"] = time.time()
        if chrome_pid is not None:
            rec["chrome_pid"] = chrome_pid
        data[session_id] = rec
        _save_sessions(data)


def _remove_session_record(session_id: str) -> None:
    with _session_lock():
        data = _load_sessions()
        if data.pop(session_id, None) is not None:
            _save_sessions(data)


def _port_listening(port: int) -> bool:
    """Return whether Chrome's wildcard bind would conflict on this port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("0.0.0.0", port))
        return False
    except OSError:
        # Fail closed: an existing listener or another bind restriction both
        # mean this port is unsafe to allocate or declare process-free.
        return True
    finally:
        probe.close()


def _allocate_port(session_id: str) -> int:
    """Return a CDP port for this session, allocating a fresh one if needed.

    The reservation is persisted immediately under the file lock so two MCP
    processes can't hand out the same port concurrently."""
    if session_id in _session_ports:
        return _session_ports[session_id]
    with _session_lock():
        persisted = _load_sessions()
        if session_id in persisted:
            # Refresh the lease while still holding the allocation lock. A
            # detached Chrome may be reconnecting with a new PID; cleanup must
            # not reclaim it between allocation and the first browser call.
            persisted[session_id]["last_active"] = time.time()
            _save_sessions(persisted)
            port = persisted[session_id]["port"]
            _session_ports[session_id] = port
            return port
        used = set(_session_ports.values()) | {r["port"] for r in persisted.values()}
        # Prefer the legacy base port for "default", but never reserve a port
        # already owned by an unregistered listener.
        port = _BASE_CDP_PORT
        while port in used or _port_listening(port):
            port += 1
        persisted[session_id] = {"port": port, "last_active": time.time()}
        _save_sessions(persisted)
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
            # Claude vision cost scales with actual image size — crops are far
            # cheaper than full screenshots, so size each image individually.
            tokens += estimate_image_tokens(part.data)
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
    payload = json.dumps(stats)
    path = _token_file(session_id)

    def _write():
        try:
            with open(path, "w") as f:
                f.write(payload)
        except Exception:
            pass
    _bg(_write)


def _append_token_log(session_id: str, url: str, tokens: int) -> None:
    """Append token usage to persistent log (never rotated — kept for historical reporting)."""
    from datetime import datetime, timezone
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "url": url,
        "tokens": tokens,
        "model": _current_model,
        "session": session_id,
    }) + "\n"

    def _write():
        try:
            os.makedirs(os.path.dirname(_TOKEN_LOG), exist_ok=True)
            with open(_TOKEN_LOG, "a") as f:
                f.write(line)
        except Exception:
            pass
    _bg(_write)


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


_port_alive_at: dict[int, float] = {}  # port -> monotonic time last confirmed alive
_PORT_ALIVE_TTL = 3.0


def _port_alive(port: int) -> bool:
    """Quick check — is Chrome still listening on this CDP port?"""
    import urllib.request
    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=0.3)
        return True
    except Exception:
        return False


async def _port_alive_async(port: int) -> bool:
    """Cached, off-loop aliveness check. Positive results are cached briefly so
    every tool call doesn't pay a blocking HTTP round-trip; negatives are never
    cached (Chrome may be mid-launch)."""
    now = time.monotonic()
    if now - _port_alive_at.get(port, float("-inf")) < _PORT_ALIVE_TTL:
        return True
    alive = await asyncio.to_thread(_port_alive, port)
    if alive:
        _port_alive_at[port] = now
    return alive


def _drop_session_state(session_id: str) -> None:
    _browsers.pop(session_id, None)
    _vision.pop(session_id, None)
    _memory.pop(session_id, None)
    _session_ports.pop(session_id, None)
    _last_active.pop(session_id, None)


async def _get_browser(session_id: str) -> BrowserManager:
    """Return (creating if needed) the BrowserManager for this session."""
    global _cleanup_started
    _last_active[session_id] = time.time()

    if session_id in _browsers:
        port = _session_ports.get(session_id)
        # Chrome may have died since last call (e.g. user closed all tabs via dashboard).
        # Drop the stale BrowserManager and fall through to re-launch a fresh Chrome.
        if port is None or not await _port_alive_async(port):
            stale = _browsers.get(session_id)
            _drop_session_state(session_id)
            if stale:
                try:
                    await stale.close()
                except Exception:
                    pass
            # Also reap the persisted record — port stays the same on recreate.
            _remove_session_record(session_id)
        else:
            # Persist the heartbeat before handing out the existing browser so
            # a cleanup process cannot act on stale activity after this returns.
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


def _get_vision(session_id: str) -> VisionPipeline:
    if session_id not in _vision:
        _vision[session_id] = VisionPipeline()
    return _vision[session_id]


def _get_memory(session_id: str) -> PageMemory:
    if session_id not in _memory:
        _memory[session_id] = PageMemory()
    return _memory[session_id]


async def _capture(session_id: str) -> tuple[bytes, bytes | None, str, float]:
    """Take screenshot, process it.

    Returns (jpeg_bytes, crop_jpeg_or_none, context, diff_ratio).
    """
    browser = await _get_browser(session_id)
    vision = _get_vision(session_id)
    memory = _get_memory(session_id)

    png = await browser.screenshot_bytes()
    tab_key = browser.active_tab_key
    # Decode/diff/encode is ~50ms of CPU — keep it off the event loop.
    jpeg_bytes, crop_jpeg, diff_ratio = await asyncio.to_thread(vision.analyze, png, tab_key)

    url = browser.current_url
    title = await browser.get_page_title()
    context = memory.update(tab_key, url, diff_ratio)

    live = browser.live_tab_keys
    vision.prune(live)
    memory.prune(live)

    _save_screenshot(session_id, jpeg_bytes, url, title)

    return jpeg_bytes, crop_jpeg, context, diff_ratio


def _save_screenshot(session_id: str, jpeg_bytes: bytes, url: str, title: str) -> None:
    """Save screenshot to disk for history browsing (per-session dir, off-loop)."""
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc)
    hist = _history_dir(session_id)
    filename = f"{ts.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    line = json.dumps({
        "ts": ts.isoformat(),
        "file": filename,
        "url": url,
        "title": title,
        "session": session_id,
    }) + "\n"

    def _write():
        try:
            os.makedirs(hist, exist_ok=True)
            with open(os.path.join(hist, filename), "wb") as f:
                f.write(jpeg_bytes)
            with open(os.path.join(hist, "index.jsonl"), "a") as f:
                f.write(line)
        except Exception:
            pass
    _bg(_write)


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
# TTL cleanup + history retention
# ---------------------------------------------------------------------------

async def _cleanup_loop() -> None:
    """Background task: every _CLEANUP_INTERVAL seconds, close sessions idle > _TTL_SECONDS
    and sweep screenshot history older than _HISTORY_RETENTION_DAYS."""
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL)
            await _cleanup_expired()
            await asyncio.to_thread(_sweep_history)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[openeyes-web] Cleanup error: {e}", file=sys.stderr)


def _managed_root_port(pid: int, expected_port: int | None = None) -> int | None:
    """Return the port only for an exact OpenEyes Chromium root command line."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            parts = [part.decode(errors="surrogateescape") for part in f.read().split(b"\0") if part]
        executable = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None
    # Chromium may rewrite the root process's entire argv block into one
    # space-separated process title. Support both that production form and
    # the normal NUL-separated procfs representation.
    args = parts[0].split() if len(parts) == 1 else parts
    allowed_names = {
        "chrome", "chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
    }
    if (not args or os.path.basename(args[0]) not in allowed_names
            or os.path.basename(executable) not in allowed_names):
        return None
    if any(arg == "--type" or arg.startswith("--type=") for arg in args[1:]):
        return None
    port_args = [arg for arg in args[1:] if arg.startswith("--remote-debugging-port=")]
    if len(port_args) != 1:
        return None
    try:
        port = int(port_args[0].split("=", 1)[1])
    except ValueError:
        return None
    if not 1 <= port <= 65535 or (expected_port is not None and port != expected_port):
        return None
    profile = f"--user-data-dir=/tmp/openeyes-web-chrome-{port}"
    profile_args = [arg for arg in args[1:] if arg.startswith("--user-data-dir=")]
    if profile_args != [profile]:
        return None
    return port


def _process_age(pid: int) -> float | None:
    """Read process age from Linux procfs without adding a psutil dependency."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        started = int(fields[19]) / os.sysconf("SC_CLK_TCK")
        return max(0.0, uptime - started)
    except (OSError, ValueError, IndexError):
        return None


def _managed_roots() -> list[dict]:
    roots = []
    try:
        pids = (int(name) for name in os.listdir("/proc") if name.isdigit())
        for pid in pids:
            port = _managed_root_port(pid)
            if port is not None:
                roots.append({"pid": pid, "port": port, "age": _process_age(pid)})
    except OSError:
        pass
    return roots


def _wait_pidfd_exit(pidfd: int, timeout: float) -> bool:
    """Wait boundedly for a pidfd to report process exit."""
    try:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                events = poller.poll(max(1, int(remaining * 1000)))
            except InterruptedError:
                continue
            if events:
                # Linux pidfds become readable (POLLIN) when the process exits.
                # Treat error-only events as failures, not proof of exit.
                return any(mask & select.POLLIN for _, mask in events)
    except (OSError, ValueError):
        return False


def _signal_managed_root(pid: int, port: int) -> str:
    """SIGTERM an exact managed root by pidfd and confirm that it exits."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if not callable(pidfd_open) or not callable(pidfd_send_signal):
        return "failed"
    try:
        pidfd = pidfd_open(pid, 0)
    except OSError:
        return "failed"
    try:
        # The pidfd pins process identity.  Validate the exact command line only
        # after acquiring it so PID reuse cannot redirect the subsequent signal.
        if _managed_root_port(pid, port) != port:
            return "mismatch"
        try:
            pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
        except OSError:
            return "failed"
        if not _wait_pidfd_exit(pidfd, _PROCESS_EXIT_TIMEOUT):
            return "timeout"
        return "exited"
    finally:
        try:
            os.close(pidfd)
        except OSError:
            pass


def _cleanup_managed_processes(now: float | None = None) -> list[str]:
    """Reclaim expired tracked and old untracked managed Chromium roots."""
    now = time.time() if now is None else now
    reclaimed_sessions: list[str] = []
    with _session_lock():
        data = _load_sessions()
        roots = _managed_roots()
        tracked_ports = {rec.get("port") for rec in data.values()}
        processed_pids: set[int] = set()
        changed = False

        for sid, rec in list(data.items()):
            last_active = rec.get("last_active", 0)
            if not last_active or now - last_active <= _TTL_SECONDS:
                continue
            port = rec.get("port")
            pid = rec.get("chrome_pid")
            status = "mismatch"
            if isinstance(port, int):
                candidates = [root for root in roots if root["port"] == port]
                matching = next((root for root in candidates if root["pid"] == pid), None)
                target = matching or (candidates[0] if len(candidates) == 1 else None)
                if target is not None:
                    processed_pids.add(target["pid"])
                    status = _signal_managed_root(target["pid"], port)
                elif not candidates and not _port_listening(port):
                    status = "gone"
            if status in {"exited", "gone"}:
                data.pop(sid, None)
                reclaimed_sessions.append(sid)
                changed = True
                print(f"[openeyes-web] Reclaimed idle session '{sid}'", file=sys.stderr)

        for root in roots:
            pid, port, age = root["pid"], root["port"], root["age"]
            if pid in processed_pids or port in tracked_ports:
                continue
            if age is not None and age > _TTL_SECONDS:
                status = _signal_managed_root(pid, port)
                if status == "exited":
                    print(f"[openeyes-web] Reclaimed untracked Chrome pid={pid} port={port}", file=sys.stderr)

        if changed:
            _save_sessions(data)

    return reclaimed_sessions


async def _cleanup_expired() -> None:
    reclaimed = await asyncio.to_thread(_cleanup_managed_processes)
    for sid in reclaimed:
        browser = _browsers.get(sid)
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        _drop_session_state(sid)


def _sweep_history() -> None:
    """Delete screenshot history older than the retention window and prune the
    per-session index files. Screenshots pile up at ~30-80KB per agent action."""
    cutoff = time.time() - _HISTORY_RETENTION_DAYS * 86400
    if not os.path.isdir(_HISTORY_ROOT):
        return
    for name in os.listdir(_HISTORY_ROOT):
        sess_dir = os.path.join(_HISTORY_ROOT, name)
        if not os.path.isdir(sess_dir):
            continue
        removed: set[str] = set()
        for fname in os.listdir(sess_dir):
            if not fname.endswith(".jpg"):
                continue
            path = os.path.join(sess_dir, fname)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed.add(fname)
            except OSError:
                pass
        if not removed:
            continue
        idx = os.path.join(sess_dir, "index.jsonl")
        try:
            with open(idx) as f:
                lines = f.readlines()
            kept = []
            for line in lines:
                try:
                    if json.loads(line).get("file") not in removed:
                        kept.append(line)
                except Exception:
                    pass
            with open(idx, "w") as f:
                f.writelines(kept)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Model pricing lookup for cost tracking — input $/M tokens.
# Single source of truth: the dashboard reads this via /api/models.
_MODEL_RATES: dict[str, float] = {
    # Anthropic (current generation)
    "fable": 10, "fable-5": 10, "claude-fable-5": 10,
    "opus": 5, "opus-4.8": 5, "opus-4.7": 5, "opus-4.6": 5,
    "claude-opus-4-8": 5, "claude-opus-4-7": 5, "claude-opus-4-6": 5,
    "sonnet": 3, "sonnet-5": 3, "sonnet-4.6": 3, "sonnet-4.5": 3,
    "claude-sonnet-5": 3, "claude-sonnet-4-6": 3, "claude-sonnet-4-5": 3,
    "haiku": 1, "haiku-4.5": 1, "claude-haiku-4-5": 1,
    # Non-Anthropic (edit via dashboard if stale)
    "gpt-4o": 2.5, "gpt-4o-mini": 0.15, "gemini-2.5-pro": 1.25,
}


def get_model_rates() -> dict[str, float]:
    """Model → input $/M tokens (for the dashboard's /api/models endpoint)."""
    return dict(_MODEL_RATES)


# Curated display list for the dashboard's model picker (the raw rates table
# above contains one entry per alias; this is one row per model).
_MODEL_DISPLAY: list[dict] = [
    {"name": "Fable 5", "rate": 10},
    {"name": "Opus 4.8", "rate": 5},
    {"name": "Sonnet 5", "rate": 3},
    {"name": "Haiku 4.5", "rate": 1},
    {"name": "GPT-4o", "rate": 2.5},
    {"name": "GPT-4o mini", "rate": 0.15},
    {"name": "Gemini 2.5 Pro", "rate": 1.25},
]


def get_model_display_list() -> list[dict]:
    return [dict(m) for m in _MODEL_DISPLAY]


@mcp.tool()
async def set_model(model: str) -> str:
    """Tell OpenEyes Web which AI model is using it, for accurate cost tracking.
    Call this once at the start of your session.
    Examples: set_model("sonnet-5"), set_model("haiku"), set_model("opus")"""
    global _current_model
    _current_model = model.lower().strip()
    rate = _MODEL_RATES.get(_current_model, None)
    if rate:
        return f"Model set to '{_current_model}' (${rate}/M input tokens)"
    return f"Model set to '{_current_model}' (unknown pricing — add rate via dashboard)"


@mcp.tool()
@_guard
async def set_device(device: str, ctx: Context) -> list:
    """Switch the emulated device for viewing sites, then reload so the page renders for it.
    Desktop is the default. Use this to check how a site looks on mobile vs desktop.
    device: "desktop" | "mobile" (=iphone) | "android" | "ipad" (aliases: phone, pixel, tablet).
    Changes viewport size, touch, device-pixel-ratio, AND the User-Agent (so sites that
    serve different HTML to phones render their real mobile version). Applies to every
    open tab; new tabs inherit it. Example: set_device("mobile") then screenshot()."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    res = await browser.set_device(device)
    if not res.get("ok"):
        return _track([res.get("error", "Failed to set device.")], sid)
    img, crop, context, _ = await _capture(sid)
    result = [MCPImage(data=img, format="jpeg")]
    kind = "mobile" if res["mobile"] else "desktop"
    result.append(f"Device set to '{res['device']}' ({res['w']}×{res['h']}, {kind}) | "
                  f"URL: {browser.current_url}\n{context}")
    return _track(result, sid)


@mcp.tool()
@_guard
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
    dev = f"\nDevice: {browser.current_device}" if browser.current_device != "desktop" else ""
    text = f"{status}\n{context}\n\nURL: {browser.current_url}\nTitle: {title}{dev}\nTabs: {tab_info}"
    result.append(text)
    return _track(result, sid)


@mcp.tool()
@_guard
async def click(x: int, y: int, ctx: Context) -> list:
    """Click at (x, y) coordinates on the screenshot.
    Look at the screenshot and estimate the pixel position of the element you want to click.
    Your click is automatically snapped to the nearest interactive element (button, link, input).
    The screenshot is ~896px wide and ~630px tall, with tick marks at 200px intervals."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    vision = _get_vision(sid)

    vw, vh = browser.viewport_size
    aw, ah = vision.display_size(vw, vh)
    x = max(0, min(x, aw - 1))
    y = max(0, min(y, ah - 1))
    vx, vy = int(x * vw / aw), int(y * vh / ah)

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
        nearby = result.get("nearby") or []
        if nearby:
            # Hand back the nearest clickables in DISPLAY coords so the agent can
            # retry precisely instead of guessing again.
            sx, sy = aw / vw, ah / vh
            items = "; ".join(
                f"<{n['tag']}>" + (f" '{n['text']}'" if n.get('text') else "")
                + f" at ({int(n['cx'] * sx)}, {int(n['cy'] * sy)})"
                for n in nearby
            )
            desc += f"\nNearest clickable elements: {items}"

    img, crop, context, diff_ratio = await _capture(sid)
    return _track(_build_response(img, crop, context,
                           f"{desc}\nURL: {browser.current_url}",
                           diff_ratio, show_tiny_changes=True), sid)


@mcp.tool()
@_guard
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
@_guard
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
@_guard
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
@_guard
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
@_guard
async def screenshot(ctx: Context) -> list:
    """Take a fresh screenshot of the current page."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    img, _, context, _ = await _capture(sid)
    # Always full screenshot when explicitly requested
    result = [MCPImage(data=img, format="jpeg")]
    dev = f" | Device: {browser.current_device}" if browser.current_device != "desktop" else ""
    result.append(f"{context}\n\nURL: {browser.current_url}{dev}")
    return _track(result, sid)


@mcp.tool()
@_guard
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
@_guard
async def switch_tab(index: int, ctx: Context) -> list:
    """Switch to a different tab by index. Use list_tabs() to see available tabs."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    error = await browser.switch_tab(index)
    if error:
        tabs = browser.list_tabs()
        tab_info = "\n".join(f"  [{t['index']}] {'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
        return _track([f"Cannot switch: {error}\n\nOpen tabs:\n{tab_info}"], sid)
    img, crop, context, _ = await _capture(sid)
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Switched to tab {index} | URL: {browser.current_url}")
    return _track(result, sid)


@mcp.tool()
@_guard
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
@_guard
async def close_tab(index: int, ctx: Context) -> list:
    """Close a tab by index. Pinned tabs cannot be closed.
    Closing the last tab ends the session (Chrome exits, browser state discarded)."""
    sid = _session_id(ctx)
    browser = await _get_browser(sid)
    key = None
    if 0 <= index < len(browser._pages):
        key = id(browser._pages[index])
    error = await browser.close_tab(index)
    if error:
        return [f"Cannot close tab {index}: {error}"]
    if key is not None:
        _get_vision(sid).forget(key)
        _get_memory(sid).forget(key)

    # Last tab closed — tear down the session entirely.
    if not browser._pages:
        try:
            await browser.close()  # Chrome exits on its own once all pages are gone
        except Exception:
            pass
        _drop_session_state(sid)
        _remove_session_record(sid)
        return [f"Closed tab {index} — last tab, session '{sid}' ended."]

    img, crop, context, _ = await _capture(sid)
    tabs = browser.list_tabs()
    tab_info = "\n".join(f"  [{t['index']}] {'📌'+t['pin']+' ' if t['pin'] else ''}{'→ ' if t['active'] else '  '}{t['url']}" for t in tabs)
    result = [MCPImage(data=img, format="jpeg")]
    result.append(f"Closed tab {index}\n\nAll tabs:\n{tab_info}")
    return _track(result, sid)


def _start_dashboard():
    """Start dashboard in background threads (non-blocking).

    Safe to call from every transport: if another instance already owns the
    dashboard port, we silently skip — the session file is shared, so the
    running dashboard already sees everyone's sessions.
    """
    import threading
    import urllib.request
    from .dashboard import _run_http, _ws_proxy, HTTP_PORT, WS_PORT
    import websockets

    try:
        urllib.request.urlopen(f"http://localhost:{HTTP_PORT}/api/sessions", timeout=0.3)
        return  # Another instance is already serving the dashboard.
    except Exception:
        pass

    def _run_http_safe():
        try:
            _run_http(HTTP_PORT)
        except OSError:
            pass  # Lost the race to another instance — fine.

    http_thread = threading.Thread(target=_run_http_safe, daemon=True)
    http_thread.start()

    async def _run_ws():
        try:
            async with websockets.serve(_ws_proxy, '0.0.0.0', WS_PORT, max_size=10_000_000):
                await asyncio.Future()
        except OSError:
            pass

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

    # Try to bring up the dashboard regardless of transport — _start_dashboard
    # skips itself if another instance already owns the port.
    _start_dashboard()

    # In server mode: also warm up the browser
    if transport == "serve":
        transport = "sse"
        mcp.settings.port = int(os.environ.get("FASTMCP_PORT", SSE_PORT))
        mcp.settings.host = "0.0.0.0"
        asyncio.get_event_loop().run_until_complete(_warmup_browser())
        print(f"[openeyes-web] MCP server at http://localhost:{mcp.settings.port}/sse", file=sys.stderr)
        print("[openeyes-web] Ready.", file=sys.stderr)

    mcp.run(transport=transport)


def serve():
    """All-in-one: MCP server + dashboard + browser. One command to run everything."""
    sys.argv = [sys.argv[0], "serve"]
    main()


def cleanup():
    """Run one serialized browser and screenshot-history cleanup pass."""
    _cleanup_managed_processes()
    _sweep_history()


if __name__ == "__main__":
    main()
