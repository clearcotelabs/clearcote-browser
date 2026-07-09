# Clearcote MCP server

Drive the open-source **Clearcote stealth Chromium** from any MCP client — Claude Desktop, Cursor,
Cline, Continue, or your own agent. One shared, coherent stealth browser; ~20 tools to navigate,
read, extract, click, fill, screenshot, and persist sessions — plus `get_cdp_endpoint`, which hands
the **same** stealth browser to any Playwright / Puppeteer / browser-use / Crawl4AI client.

The fingerprint is corrected in Chromium's **C++**, so you do **not** add `puppeteer-stealth` /
`undetected-chromedriver` / any JS patching — those self-reveal and undo it. Driving over CDP adds
no automation flags, so `navigator.webdriver` stays `false` and the persona is intact end to end.

---

## Run it

**Node (no install):**
```bash
npx clearcote-mcp
```

**Python:**
```bash
pip install clearcote-mcp
clearcote-mcp            # stdio server
```

Both auto-download + SHA-256-verify the right Clearcote binary per OS on first use (native Windows
x64 + Linux x64).

## Add it to a client

Claude Desktop / Cursor / Cline `mcpServers` config:

```json
{
  "mcpServers": {
    "clearcote": {
      "command": "npx",
      "args": ["-y", "clearcote-mcp"],
      "env": {
        "CLEARCOTE_FINGERPRINT": "acct-1",
        "CLEARCOTE_PLATFORM": "windows",
        "CLEARCOTE_PROXY": "http://user:pass@host:port",
        "CLEARCOTE_GEOIP": "1"
      }
    }
  }
}
```

The **persona lives in the environment**, so the tool surface stays clean:

| env var | meaning |
|---|---|
| `CLEARCOTE_FINGERPRINT` | seed → one stable, coherent identity (same seed = same machine across runs) |
| `CLEARCOTE_PLATFORM` | `windows` \| `linux` \| `macos` \| `android` |
| `CLEARCOTE_BRAND` | `Chrome` \| `Edge` \| `Opera` \| `Vivaldi` |
| `CLEARCOTE_PROXY` | `http://user:pass@host:port` (routes all traffic) |
| `CLEARCOTE_GEOIP` | `1` → derive timezone/locale/WebRTC IP from the proxy exit IP |
| `CLEARCOTE_TIMEZONE` / `CLEARCOTE_ACCEPT_LANGUAGE` | explicit overrides |
| `CLEARCOTE_HEADLESS` | `0` for a visible window (default headless) |
| `CLEARCOTE_BINARY` | path to a specific Clearcote binary (optional) |

Hardening knobs: `CLEARCOTE_MCP_TOOL_TIMEOUT` (s), `CLEARCOTE_MCP_WRITE_DIR` (sandbox for file
writes), `CLEARCOTE_MCP_ALLOW_ANY_PATH=1`, `CLEARCOTE_ALLOW_PRIVATE_EGRESS=1` (allow localhost /
private targets), `CLEARCOTE_MCP_PREWARM=0`, `CLEARCOTE_SERVE_PORT`.

## Tools

**Read** · `read_page` (text + Markdown) · `get_page_html` · `page_elements` (interactive elements +
selectors) · `evaluate_js` · `wait_for` · `current_page` · `get_cookies` · `list_tabs`
**Act** · `navigate` · `click` (selector or visible text) · `fill_field` (selector/label/placeholder/name)
· `press_key` · `new_tab` · `close_tab`
**Capture** · `screenshot_page` · `save_page_pdf`
**Session** · `save_profile` / `load_profile` (cookies + storage)
**Stealth / infra** · `get_egress_info` (public IP + active persona) · **`get_cdp_endpoint`** (attach any
other CDP client to the same stealth browser)

## Guardrails (built in)

- Every tool has a wall-clock timeout and returns a **structured error** instead of crashing the server.
- URL args are **SSRF-checked** — localhost / private / cloud-metadata are refused unless you opt in.
- File writes are **confined** to a sandbox dir (no path traversal).
- Oversized text is **capped** so a response never floods the agent's context.
- The shared browser is **rebuilt** automatically if it dies.

## Just want the raw endpoint?

If you don't need the tools, run the browser as a standing CDP endpoint and attach your existing code:

```bash
clearcote-serve --port 9222 --fingerprint acct-1 --platform windows
# → prints http://127.0.0.1:9222 ; then:  connect_over_cdp / puppeteer.connect({browserURL})
```

See [USAGE.md](USAGE.md) for per-client examples. Part of
[clearcote-browser](https://github.com/clearcotelabs/clearcote-browser) (BSD-3).
