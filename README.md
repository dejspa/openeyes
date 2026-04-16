# OpenEyes

Vision-first automation for AI agents via [MCP](https://modelcontextprotocol.io). No DOM parsing, no accessibility APIs, no selectors — the agent sees a screenshot and acts by coordinates, like a human.

This repo contains two MCP servers in one family:

| | Package | Scope |
|---|---|---|
| **[OpenEyes Web](web/)** | `openeyes-web` | Headless Chromium with per-agent session isolation. For browsing, scraping, filling forms. |
| **[OpenEyes Desktop](desktop/)** | `openeyes-desktop` | Full OS control — mouse, keyboard, screenshots of the whole desktop. For native apps, legacy UIs, anything outside the browser. |

Both share the same vision-first philosophy: screenshot → the agent looks → click/type by coordinates → next screenshot.

## Why

DOM-based tools break on shadow DOM, iframes, web components, and dynamic frameworks. They also cost 2–5× more in tokens per step because HTML dumps are huge. Screenshots are ~800 tokens each regardless of site complexity, and `elementFromPoint` + coordinate clicks naturally pierce every kind of DOM boundary.

## Layout

```
openeyes/
├── web/        # OpenEyes Web  — browser MCP (Playwright + CDP)
│   ├── pyproject.toml
│   ├── README.md
│   └── src/openeyes_web/
└── desktop/    # OpenEyes Desktop — OS-level control MCP
    ├── pyproject.toml
    ├── README.md
    └── src/openeyes_desktop/
```

Each subproject is its own Python package with its own dependencies, versioning, and entry points — install only what you need.

## Quick start

```bash
git clone https://github.com/dejspa/openeyes
cd openeyes/web   # or cd openeyes/desktop
uv sync
uv run playwright install chromium  # web only
uv run openeyes-web-serve            # or: uv run openeyes-desktop
```

See each subproject's README for detailed setup, MCP client config, and tool reference.

## License

MIT
