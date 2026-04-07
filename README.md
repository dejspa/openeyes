# ViewPort Browser

Vision-first web browser for AI agents via [MCP](https://modelcontextprotocol.io). No DOM parsing, no selectors, no brittle scraping. The agent sees a screenshot and clicks by coordinates — exactly like a human.

## Why

Every web browsing tool for AI agents relies on DOM parsing. That breaks constantly: shadow DOM, iframes, web components, dynamic frameworks. And it's expensive — dumping HTML costs 10,000–50,000 tokens per page.

ViewPort takes a different approach:

| | DOM-based tools | ViewPort |
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

## Live monitoring

ViewPort includes a live dashboard. Watch your agents browse in real-time from any browser:

```bash
# With uv
uv run --directory /path/to/viewport-browser viewport-dashboard

# Or if installed in venv
viewport-dashboard
```

Opens at **http://localhost:6080** — also accessible from other machines on the network via the host IP.

Uses Chrome DevTools Protocol screencast — works on headless servers, no physical display needed.

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
git clone https://github.com/dejspa/viewport-browser
cd viewport-browser
uv sync
uv run playwright install chromium
```

Or with pip:

```bash
git clone https://github.com/dejspa/viewport-browser
cd viewport-browser
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
    "viewport": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/viewport-browser", "viewport-browser"]
    }
  }
}
```

### OpenClaw (SSE)

```bash
# Start the server
viewport-browser sse  # listens on port 6090

# Connect OpenClaw
openclaw mcp set viewport '{"url":"http://localhost:6090/sse"}'
```

### Other platforms

See [SKILL.md](SKILL.md) for integration with Paperclip, Codex CLI, Gemini CLI, and other agent harnesses.

Works with any MCP-compatible agent. The 11 tools appear automatically after connecting.

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

## Smart token optimization

Not every action needs a screenshot:

- **`type_text` without Enter** → text-only response (saves ~800 tokens)
- **`click` with no page change** → text-only feedback: "Clicked: \<button> 'Add to cart'"
- **Minor change (modal opened)** → only the changed region is sent, not the full page
- **`scroll` at bottom of page** → text-only: "No new content visible"

## License

MIT
