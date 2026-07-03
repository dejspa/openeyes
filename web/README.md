# OpenEyes Web

Vision-first web browser for AI agents via [MCP](https://modelcontextprotocol.io). No DOM parsing, no selectors, no brittle scraping. The agent sees a screenshot and clicks by coordinates — exactly like a human.

## Why

Every web browsing tool for AI agents relies on DOM parsing. That breaks constantly: shadow DOM, iframes, web components, dynamic frameworks. And it's expensive — dumping HTML costs 10,000–50,000 tokens per page.

OpenEyes Web takes a different approach:

| | DOM-based tools | OpenEyes Web |
|---|---|---|
| **How it works** | Parse HTML, extract elements, build text descriptions | Screenshot → agent sees the page → clicks by coordinates |
| **Shadow DOM** | Breaks | Works (elementFromPoint pierces everything) |
| **Tokens per step** | ~2,000–5,000 | ~800 |
| **Cost per task** | ~$0.05–0.10 | ~$0.01–0.04 |
| **Site compatibility** | Needs selectors per site | Any site, any framework |

## How it works

```
Screenshot (PNG) → Resize to 896×630 → JPEG q55 (~68KB) → Agent sees clean page
                                                            Agent says click(x=450, y=300)
                                                            → elementFromPoint snaps to nearest button
                                                            → Click → New screenshot
```

The only DOM interaction is a single `elementFromPoint()` call at click time. It naturally pierces shadow DOM, finds the nearest interactive element, and snaps to its center. No selectors, no tree walking, no element detection.

## Quick start (all-in-one)

Start everything with a single command — MCP server, dashboard, and browser:

```bash
git clone https://github.com/dejspa/openeyes
cd openeyes/web
uv sync
uv run playwright install chromium
uv run openeyes-web-serve
```

`uv run` uses the project's virtualenv without requiring `source .venv/bin/activate` or a global install.

This starts:
- **MCP server** on port 6090 (SSE for remote agents)
- **Dashboard** at http://localhost:6080 (live browser view)
- **Chrome** with CDP on port 9222

All accessible from other machines on the network via the host IP.

## Live monitoring

The dashboard can also be run standalone:

```bash
uv run openeyes-web-dashboard
```

Opens at **http://localhost:6080**. Uses Chrome DevTools Protocol screencast — works on headless servers, no physical display needed.

## Prerequisites

- **Python 3.11+**
- **uv** (recommended) or pip
- **Xvfb** — required for dashboard/screencast (falls back to `--headless=new` without it)

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install Xvfb (Linux)
sudo apt install xvfb        # Debian/Ubuntu
sudo dnf install xorg-x11-server-Xvfb  # Fedora/RHEL
```

## Setup

```bash
git clone https://github.com/dejspa/openeyes
cd openeyes/web
uv sync
uv run playwright install chromium
```

Or with pip:

```bash
git clone https://github.com/dejspa/openeyes
cd openeyes/web
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
```

## Connect to your agent

### Claude Code / Cursor (stdio)

Add to `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "openeyes-web": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/openeyes/web", "openeyes-web"]
    }
  }
}
```

### SSE / remote agents

```bash
# Start the server
uv run openeyes-web sse  # listens on port 6090

# Example: OpenClaw
openclaw mcp set openeyes-web '{"url":"http://localhost:6090/sse"}'
```

### Paperclip (multi-agent)

Each agent gets its own browser with isolated cookies/sessions via separate CDP ports.

**1. Create a working directory per agent with its own `.mcp.json`:**

```bash
mkdir -p /home/user/agents/dejan
cat > /home/user/agents/dejan/.mcp.json << 'EOF'
{
  "mcpServers": {
    "openeyes-web": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/openeyes/web", "openeyes-web"],
      "env": {"OPENEYES_WEB_CDP_PORT": "9222", "OPENEYES_WEB_SESSION": "dejan"}
    }
  }
}
EOF
```

**2. Configure the agent in Paperclip with `cwd` pointing to its directory:**

```yaml
name: dejan
adapter: claude_local
config:
  cwd: /home/user/agents/dejan
  model: sonnet
  dangerouslySkipPermissions: true
```

**3. For a second agent, use a different CDP port:**

```bash
mkdir -p /home/user/agents/anna
cat > /home/user/agents/anna/.mcp.json << 'EOF'
{
  "mcpServers": {
    "openeyes-web": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/openeyes/web", "openeyes-web"],
      "env": {"OPENEYES_WEB_CDP_PORT": "9223", "OPENEYES_WEB_SESSION": "anna"}
    }
  }
}
EOF
```

Each agent gets a completely isolated Chrome instance. Tabs and login sessions persist across heartbeats — Chrome runs independently of the agent process.

### Other platforms

See [SKILL.md](SKILL.md) for integration with Codex CLI, Gemini CLI, and other agent harnesses.

Works with any MCP-compatible agent. The 13 tools appear automatically after connecting.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENEYES_WEB_CDP_PORT` | `9222` | Chrome DevTools Protocol port. Use different ports for multi-agent isolation. |
| `OPENEYES_WEB_SESSION` | `default` | Session name for token tracking. Keeps per-agent stats separate. |
| `OPENEYES_WEB_MODEL` | `unknown` | Model name for cost tracking. Also settable via `set_model()` tool. |
| `FASTMCP_PORT` | `6090` | Port for SSE/HTTP transport (non-stdio mode). |

## Tools

| Tool | Description |
|---|---|
| `navigate(url)` | Go to a URL |
| `click(x, y)` | Click at coordinates — auto-snaps to nearest interactive element |
| `type_text(text)` | Type into focused element. `press_enter=true` to submit, `clear_first=true` to replace |
| `scroll(direction)` | Scroll `"up"` or `"down"` |
| `get_text()` | Extract page text (articles, prices, product details) |
| `go_back()` | Browser back |
| `screenshot()` | Fresh screenshot |
| `set_device(device)` | View the site as `"desktop"` (default) or `"mobile"`/`"android"`/`"ipad"` — switches viewport, touch, and User-Agent, then reloads |
| `set_model(model)` | Set model name for cost tracking |
| `new_tab(url, pin)` | Open a new tab, optionally pin it |
| `switch_tab(index)` | Switch to a tab by index |
| `list_tabs()` | Show all open tabs |
| `close_tab(index)` | Close a tab |

## Device emulation (desktop / mobile)

Desktop by default. The agent can call `set_device("mobile")` (or `"android"`, `"ipad"`) to see how a site renders on a phone, and `set_device("desktop")` to switch back. It changes the viewport size, touch support, the mobile flag, **and** the User-Agent together — then reloads — so sites that serve different HTML to phones show their real mobile version, not just a narrowed desktop layout. The setting applies to every open tab and new tabs inherit it.

Implemented via Chrome DevTools Protocol `Emulation.*` (what Chrome's own device toolbar uses), so it toggles on the existing tab without a new browser context — cookies and logins are preserved. Device-pixel-ratio stays at 1 (Playwright pins it in CDP-attached mode); it only affects raster sharpness, not layout.

## Smart token optimization

Not every action needs a screenshot:

- **`type_text` without Enter** → text-only response (saves ~800 tokens)
- **`click` with no page change** → text-only feedback: "Clicked: \<button> 'Add to cart'"
- **Minor change (modal opened)** → only the changed region is sent, not the full page
- **`scroll` at bottom of page** → text-only: "No new content visible"

## Token benchmark (measured)

Perception tokens — what each tool feeds back into the model's context per action — over the same live pages and the same action sequence: `navigate` + 2× `scroll`. OpenEyes also pays for one explicit `get_text`; a DOM/accessibility-tree tool (measured here with Playwright's a11y snapshot) returns the page's full tree on **every** action, with the text already inside it.

| Page | OpenEyes Web | Playwright a11y tree | |
|---|--:|--:|---|
| example.com (trivial) | 782 | **159** | a11y 4.9× cheaper |
| Hacker News front page | **3,813** | 30,438 | OpenEyes 8.0× cheaper |
| Wikipedia article | **4,607** | 53,505 | OpenEyes 11.6× cheaper |
| Wikipedia Main Page (heavy) | **4,162** | 32,139 | OpenEyes 7.7× cheaper |
| **Total** | **13,364** | **116,241** | **OpenEyes 8.7× cheaper** |

A screenshot is a fixed ~750 tokens no matter how complex the page, and diff-gating drops unchanged frames to zero — whereas an accessibility/DOM snapshot scales with the DOM and is re-sent every action. On content-rich pages OpenEyes runs **8–12× leaner**; the gap only reverses on near-empty pages, where a fixed-size screenshot is overkill and the tiny a11y tree wins. For scale: one a11y snapshot of the Wikipedia article (~17,800 tokens) costs as much as OpenEyes scrolling through **~23 full screens**.

**Method & caveats.** Image tokens use Anthropic's documented `ceil(w×h/750)` on the actual sent JPEG; text uses `tiktoken` (cl100k_base), which undercounts markup and therefore *understates* the tree side — the real gap is likely wider. These are perception tokens only: they exclude the agent's own reasoning, and they don't credit the a11y tree's exact element refs, which can reduce misclick retries. OpenEyes narrows that accuracy gap with click-time snap-to-element.

## License

MIT
