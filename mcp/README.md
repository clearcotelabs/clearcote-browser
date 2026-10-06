# Clearcote MCP server

<!-- mcp-name: io.github.clearcotelabs/clearcote-mcp -->

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
The launcher runs the Python server with `uvx` if you have [uv](https://docs.astral.sh/uv/), else with `pipx`, else
installs it with `pip` (a Python whose `pip install` is refused outside a virtual environment, as on Debian 12+,
Ubuntu 23.04+ and Homebrew, needs uv or pipx; the launcher says so).

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
| `CLEARCOTE_CLOUD` | `1` → the shared browser is a **hosted** Clearcote session instead (needs `CLEARCOTE_API_KEY`) |
| `CLEARCOTE_API_KEY` | your `cc_live_...` key: cloud mode, and the `run_task` tool |

Hardening knobs: `CLEARCOTE_MCP_TOOL_TIMEOUT` (s), `CLEARCOTE_MCP_RUN_TIMEOUT` (s, `run_task`; default 900), `CLEARCOTE_MCP_WRITE_DIR` (sandbox for file
writes), `CLEARCOTE_MCP_ALLOW_ANY_PATH=1`, `CLEARCOTE_ALLOW_PRIVATE_EGRESS=1` (allow localhost /
private targets), `CLEARCOTE_MCP_PREWARM=0`, `CLEARCOTE_SERVE_PORT`, `CLEARCOTE_MCP_INLINE_IMAGE_MAX`
(bytes; default 200000: the largest screenshot also returned inline).

## Tools

**Read** · `read_page` (Markdown by default; `format="text"` or `"both"`) · `get_page_html` · `page_elements` (interactive elements +
selectors) · `evaluate_js` · `wait_for` · `current_page` · `get_cookies` · `list_tabs`
**Act** · `navigate` · `click` (selector or visible text) · `fill_field` (selector/label/placeholder/name)
· `press_key` · `new_tab` · `close_tab`
**Capture** · `screenshot_page` (saved under the sandbox dir; up to 200 KB also returned as an image) · `save_page_pdf`

`navigate` and `read_page` also return `http_status` (the main document's HTTP status; `null` for a local file or
`about:blank`) and `page_state`: `blocked` (HTTP 401, 403, 429 or 503), `empty` (under 20 characters of visible
text: still loading, or nothing to read) or `ok`.
**Session** · `save_profile` / `load_profile` (cookies + storage)
**Stealth / infra** · `get_egress_info` (public IP + active persona) · **`get_cdp_endpoint`** (attach any
other CDP client to the same stealth browser)

**Cloud** · `run_task(task, url?, schema_json?)`: a whole task run by the hosted agent (Clearcote
Jet) on a cloud browser, returning its JSON result (`run_status`, `result.output`, `cost_eur`). Listed
only when `CLEARCOTE_API_KEY` is set.

## Cloud mode

Set `CLEARCOTE_CLOUD=1` and `CLEARCOTE_API_KEY` and the shared browser runs on Clearcote's servers
instead of this machine: nothing to download, a residential IP included. Every tool works the same,
and the persona variables above still apply (`CLEARCOTE_BINARY` and `CLEARCOTE_SERVE_PORT` are
local-only and ignored). The one difference: a cloud session's CDP URL is single-use and the server
holds it, so `get_cdp_endpoint` has no endpoint to hand out; start your own hosted browser with
`clearcote.launch(cloud=True)` instead. Needs `clearcote` 0.34 or newer.

```json
{
  "mcpServers": {
    "clearcote": {
      "command": "npx",
      "args": ["-y", "clearcote-mcp"],
      "env": { "CLEARCOTE_CLOUD": "1", "CLEARCOTE_API_KEY": "cc_live_..." }
    }
  }
}
```

## Guardrails (built in)

- Every tool has a wall-clock timeout and returns a **structured error** instead of crashing the server.
- URL args are **SSRF-checked** — localhost / private / cloud-metadata are refused unless you opt in.
- File writes are **confined** to a sandbox dir (no path traversal).
- Oversized text is **capped** so a response never floods the agent's context, with an explicit
  `<field>_truncated` flag.
- Page text, Markdown and HTML are **fenced** as untrusted data (`<untrusted_page_content>`), and a page cannot
  close the fence early.
- The shared browser is **rebuilt** automatically if it dies.

## Just want the raw endpoint?

If you don't need the tools, run the browser as a standing CDP endpoint and attach your existing code:

```bash
clearcote-serve --port 9222 --fingerprint acct-1 --platform windows
# → prints http://127.0.0.1:9222 ; then:  connect_over_cdp / puppeteer.connect({browserURL})
```

See [USAGE.md](USAGE.md) for per-client examples. Part of
[clearcote-browser](https://github.com/clearcotelabs/clearcote-browser) (BSD-3).
