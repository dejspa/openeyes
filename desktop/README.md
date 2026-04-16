# OpenEyes Desktop

Vision-first desktop controller for AI agents via [MCP](https://modelcontextprotocol.io). Full computer control — mouse, keyboard, screenshots of the entire desktop. No OS APIs, no accessibility layer: the agent sees the screen and acts by coordinates, exactly like a human.

For web-only automation, use [OpenEyes Web](../web/) instead — it's lighter and has session isolation per agent.

## When to use this

- Native desktop apps (Figma, Photoshop, Office, IDE plugins)
- Legacy UIs without accessibility hooks
- Workflows that span browser + OS (download file → open in external editor)
- Anything your agent needs to do that isn't confined to one browser tab

## Quick start

```bash
git clone https://github.com/dejspa/openeyes
cd openeyes/desktop
uv sync
uv run openeyes-desktop
```

Works on Linux (X11 with `xdotool`/`ydotool`, tested on Xvfb too) and on WSL2 (controls the Windows host via PowerShell bridge).

## Prerequisites

- **Python 3.11+**
- **Tesseract** for OCR-based text clicking:
  ```bash
  sudo apt install tesseract-ocr
  ```
- **Linux X11 tools** (if running natively on Linux):
  ```bash
  sudo apt install xdotool scrot
  ```
- **WSL2:** no extra setup — PowerShell is used to drive the Windows desktop.

## Connect to your agent

Add to your MCP config:

```json
{
  "mcpServers": {
    "openeyes-desktop": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/openeyes/desktop", "openeyes-desktop"]
    }
  }
}
```

## Tools

| Tool | Description |
|---|---|
| `screenshot()` | Desktop screenshot with clickable grid overlay (`a0`, `b5`, `f12`, …) |
| `click_label(label)` | Click a grid label — fastest way to hit anything visible |
| `click_text(text, near, index)` | OCR-based — click visible text on screen |
| `click(x, y)` | Click at coordinates |
| `double_click(x, y)` / `right_click(x, y)` | Variants |
| `move(x, y)` | Move mouse, see zoomed view with crosshair (precise aiming) |
| `type_text(text)` | Type on the keyboard |
| `key(combo)` | Key combos: `"Return"`, `"ctrl+c"`, `"alt+Tab"`, `"super"` |
| `scroll(direction, x, y)` | Scroll at position |
| `drag(x1, y1, x2, y2)` | Click-and-drag |
| `navigate(url)` | Open a URL in the default browser (handles focus, address bar, Enter) |

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENEYES_DESKTOP_SESSION` | `default` | Session name for screenshot/history separation. |
| `FASTMCP_PORT` | `6091` | Port for SSE/HTTP transport. |

## License

MIT
