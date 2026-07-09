# Clearcote MCP — usage

Two ways to use Clearcote from an agent stack:

1. **MCP server** — the agent calls tools (`read_page`, `click`, …). Best for Claude Desktop / Cursor / Cline.
2. **`clearcote-serve`** — a standing raw CDP endpoint your existing Playwright/Puppeteer/browser-use code attaches to unchanged. Best when you already have automation code.

Both drive the **same stealth engine**; `get_cdp_endpoint` bridges (1)→(2) at runtime.

---

## 1. MCP server — client configs

All expose the same `stdio` server; the persona is set via `env`.

**Claude Desktop** (`claude_desktop_config.json`) / **Cursor** (`~/.cursor/mcp.json`) / **Cline**:
```json
{
  "mcpServers": {
    "clearcote": {
      "command": "npx",
      "args": ["-y", "clearcote-mcp"],
      "env": { "CLEARCOTE_FINGERPRINT": "acct-1", "CLEARCOTE_PLATFORM": "windows" }
    }
  }
}
```

If you prefer the Python entry point instead of `npx`:
```json
{ "mcpServers": { "clearcote": { "command": "clearcote-mcp",
    "env": { "CLEARCOTE_FINGERPRINT": "acct-1", "CLEARCOTE_PROXY": "http://user:pass@host:port", "CLEARCOTE_GEOIP": "1" } } } }
```

Then just ask the agent to browse. A typical loop the model runs:
`navigate` → `read_page` / `page_elements` → `click` / `fill_field` → `read_page` … and
`save_profile` to keep a logged-in session for next time.

## 2. `clearcote-serve` — attach your existing code

```bash
clearcote-serve --port 9222 --fingerprint acct-1 --platform windows --proxy http://user:pass@host:port
# stdout: http://127.0.0.1:9222
```

```python
# Playwright (Python) — nothing else in your script changes
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    page = browser.contexts[0].pages[0] if browser.contexts[0].pages else browser.contexts[0].new_page()
    page.goto("https://example.com")
```

```js
// Puppeteer / Playwright (Node)
const browser = await puppeteer.connect({ browserURL: "http://127.0.0.1:9222" });
// or: await chromium.connectOverCDP("http://127.0.0.1:9222")
```

```python
# browser-use / Crawl4AI / Stagehand: point the CDP endpoint at http://127.0.0.1:9222
```

Programmatic (no separate process):
```python
from clearcote import serve
srv = serve(fingerprint="acct-1", platform="windows")   # Server handle
print(srv.cdp_url)                                       # http://127.0.0.1:<port>
# ... attach any CDP client to srv.cdp_url ...
srv.close()
```

## Rules an agent MUST follow (stealth)

- **Do NOT** add `puppeteer-stealth`, `undetected-chromedriver`, or any JS fingerprint patching — the spoofing is in the engine's C++; JS patches self-reveal and undo it.
- **Do NOT** pass `--user-agent` / `--enable-automation` or set a custom UA over CDP — it desyncs UA vs UA-CH. Set the persona via env instead.
- Keep `timezone` / `accept_language` coherent with your proxy's geography (use `CLEARCOTE_GEOIP=1`).
- Clearcote hardens the **browser fingerprint**. **IP reputation and behavior are still yours** — a residential/mobile proxy and human-like pacing matter as much as the fingerprint.

## Stealth, verified

The served/MCP-driven browser is launched **directly** (no automation framework), so `--enable-automation`
is never present and `navigator.webdriver` stays `false`; the engine's `Runtime.enable` neutralization
keeps the attached CDP client undetectable to the page; the debug port binds to loopback with an origin
allowlist. Confirm with `evaluate_js("navigator.webdriver")` → `false`.
