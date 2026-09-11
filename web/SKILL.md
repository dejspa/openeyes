---
name: openeyes-web
description: Vision-first web browser — navigate websites, click by coordinates, fill forms, extract text. Uses screenshots + coordinate-based clicking with auto-snap to nearest interactive element.
version: 1.0.0
requires:
  env: []
  bins: []
---

# OpenEyes Web — Vision-First Web Navigation

You have access to a browser that lets you navigate websites, click elements, type text, and extract content. Everything is vision-based: you see screenshots and click by (x, y) pixel coordinates.

## Connection

The browser runs as an MCP server. Connect using one of these methods:

### Stdio (Claude Code, Cursor, local agents)

```json
{
  "mcpServers": {
    "openeyes-web": {
      "command": "openeyes-web",
      "transport": "stdio"
    }
  }
}
```

### SSE / HTTP (remote agents)

Start the server first, then connect:

```bash
# Start OpenEyes Web in SSE mode (default port 6090)
openeyes-web sse
```

```bash
# Example: OpenClaw
openclaw mcp set openeyes-web '{"url":"http://localhost:6090/sse"}'
```

### Stdio with Python (if openeyes-web is not in PATH)

```json
{
  "mcpServers": {
    "openeyes-web": {
      "command": "python",
      "args": ["-m", "openeyes_web.server"]
    }
  }
}
```

## Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `navigate` | `url` | Go to a URL. If a tab with that domain is already open, switches to it. |
| `click` | `x`, `y` | Click at pixel coordinates on the screenshot. Auto-snaps to nearest interactive element. |
| `type_text` | `text`, `press_enter`, `clear_first` | Type into the currently focused element. |
| `scroll` | `direction` ("up"/"down") | Scroll the page. |
| `get_text` | — | Extract the page's main text content (articles, prices, product details). |
| `screenshot` | — | Take a fresh screenshot of the current page. |
| `go_back` | — | Browser back button. |
| `new_tab` | `url`, `pin` | Open a new tab. Set `pin="name"` to protect it from closing. |
| `switch_tab` | `index` | Switch to a tab by index. |
| `list_tabs` | — | Show all open tabs. |
| `close_tab` | `index` | Close a tab by index (cannot close pinned tabs). |
| `devtools` | `mode` | `"on"` / `"off"` / `"clear"` / `"status"`. Opt-in console + network recording. **Off by default.** |
| `get_console` | `level`, `since`, `limit`, `all_tabs` | Console output and uncaught JS errors. Needs `devtools("on")`. |
| `get_network` | `types`, `since`, `contains`, `limit`, `all_tabs` | HTTP requests with method, URL, status, size, timing. Needs `devtools("on")`. |
| `get_request` | `id`, `headers`, `reveal` | One request in full: headers, request body, response body. Needs `devtools("on")`. |
| `inspect_element` | `x`, `y` or `selector`, `html_chars` | Elements panel: attributes, box, computed styles, ancestors, outerHTML, and what covers it. Works anytime. |
| `get_storage` | `kind`, `contains`, `reveal` | Application panel: localStorage, sessionStorage, cookies, IndexedDB, Cache Storage. Works anytime. |
| `get_source` | `url`, `start`, `lines` | Sources panel: live page HTML (`""`), resource list (`"list"`), or a loaded script/stylesheet by URL substring. Works anytime. |

## How Clicking Works

1. Look at the screenshot and estimate the **(x, y) pixel coordinates** of what you want to click.
2. The screenshot is **~896 pixels wide** and **~630 pixels tall**.
3. Subtle **tick marks** along the top and left edges at **200px intervals** help you gauge position.
4. Your click is **automatically snapped** to the nearest interactive element (button, link, input).
5. After each click you get feedback like `Clicked: <button> 'Add to cart'` confirming what was hit.
6. To type into a field: **click its coordinates first** (to focus it), then use `type_text()`.
7. To search: click the search field, then `type_text(query, press_enter=true, clear_first=true)`.
8. Some actions return **text-only feedback** (no screenshot) when the page didn't visually change. Use `screenshot()` if you need to see the current state.

## Strategy Guide

### 1. SEARCH & ADD (e.g. "add product X to cart")
```
navigate → click search field → type_text(query, press_enter=true, clear_first=true) → screenshot → click "add" button
```

### 2. COMPARE & PICK (e.g. "find the cheapest X")
```
navigate → click search → type_text(query) → get_text (read ALL names and prices) → screenshot → click
```
ALWAYS use `get_text` first to read prices — don't guess prices from screenshots.

### 3. RESEARCH (e.g. "find info about X")
```
navigate → screenshot → get_text → report
```
Use `get_text` for article content — don't read long text from screenshots.

### 4. BROWSE FEED (e.g. "scroll through feed, find articles about X")
```
screenshot → scroll → screenshot → scroll (repeat)
```
Use `get_text` on interesting items.

### 5. DEBUG A SITE (e.g. "why does the login form do nothing", "what does the app call when I click save", "check localhost:3000 for console errors")
```
devtools("on") → navigate/click as usual → read the "DevTools:" summary line on each reply
→ get_network() / get_console() for what that action caused → get_request(id) for headers + bodies
→ devtools("off") when done
```
DevTools is **off by default** because it costs tokens. Turn it on only when you need to see console output or network traffic — a page misbehaves, you're developing a site, or you must know what an action sent and received. By default `get_console`/`get_network` show only what your **last action** caused; use `since="all"` for the whole buffer, `types="all"` to include images/CSS/scripts, `types="failed"` for errors only.

`inspect_element`, `get_storage` and `get_source` need no recording — call them whenever you need them:
- A click "does nothing"? `inspect_element(x, y)` shows what is really at that point, the stack of elements under it, and whether an overlay covers the button.
- Wrong login state, stale data, a feature flag? `get_storage()` shows localStorage, sessionStorage, cookies and IndexedDB for the page's origin.
- Need to read the page's actual HTML or a loaded script? `get_source()` / `get_source(url="app.js")` — page through with `start`/`lines`.

Cookies, tokens, passwords and similar secrets are masked as `…(N chars)` in headers, bodies and storage. You still see *that* they were sent and how long they are; pass `reveal=true` only when the actual value is needed for the bug.

## Important Behaviors

- If a **cookie banner**, ad interstitial, or overlay blocks the page, click its accept/dismiss/close button.
- **Popup tabs** (ads, new windows) are auto-closed.
- When comparing products: first filter to products that genuinely match the request, THEN pick cheapest among those. Think like a human — "milk" means regular milk, not oat milk.

## Rules

- Be efficient — never repeat the same action twice.
- Don't scroll unnecessarily — check what's already visible first.
- Don't open product detail pages when the info is already visible on the card.
- If an overlay or popup blocks you, take a new screenshot — it may have been auto-dismissed.
