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

Works with any MCP-compatible agent. The 12 tools appear automatically after connecting.

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
| `set_model(model)` | Set model name for cost tracking |
| `new_tab(url, pin)` | Open a new tab, optionally pin it |
| `switch_tab(index)` | Switch to a tab by index |
| `list_tabs()` | Show all open tabs |
| `close_tab(index)` | Close a tab |

## Smart token optimization

Not every action needs a screenshot:

- **`type_text` without Enter** → text-only response (saves ~800 tokens)
- **`click` with no page change** → text-only feedback: "Clicked: \<button> 'Add to cart'"
- **Minor change (modal opened)** → only the changed region is sent, not the full page
- **`scroll` at bottom of page** → text-only: "No new content visible"

## License

MIT
