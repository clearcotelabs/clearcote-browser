# Installing the Clearcote MCP server (for AI agents)

These steps are for an agent such as Cline that sets the server up for a user. The human-facing guide is
[README.md](README.md); the full docs are at https://www.clearcotelabs.com/docs/mcp.

## 1. Check the prerequisites

- **Node.js 18+** (for `npx`) and **Python 3.10+**.
- One of [uv](https://docs.astral.sh/uv/) (`uvx`) or `pipx` is recommended. Without either, the launcher falls back to
  `pip install`, which some Pythons refuse outside a virtual environment (Debian 12+, Ubuntu 23.04+, Homebrew). If the
  launcher says so, install uv (`pip install uv` or the uv installer) and retry.
- Windows x64 and Linux x64 run the browser natively. For macOS, see https://www.clearcotelabs.com/docs/installation.

No API key is needed. On first use the server downloads the Clearcote browser for the OS and checks its SHA-256, so
the first tool call can take a minute.

## 2. Add the server to the MCP settings

Add this entry to the client's `mcpServers` config (for Cline: `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "clearcote": {
      "command": "npx",
      "args": ["-y", "clearcote-mcp"]
    }
  }
}
```

If the user prefers Python, use `"command": "uvx", "args": ["clearcote-mcp"]` instead, or run
`pip install clearcote-mcp` and use `"command": "clearcote-mcp", "args": []`.

## 3. Optional settings (only when the user asks)

Put these under `"env"` in the same entry. Leave them out by default.

| env var | use |
|---|---|
| `CLEARCOTE_FINGERPRINT` | a seed such as `acct-1`: the same seed gives the same browser identity every run |
| `CLEARCOTE_PLATFORM` | `windows`, `linux`, `macos` or `android` |
| `CLEARCOTE_PROXY` | `http://user:pass@host:port`; ask the user for it, never invent one |
| `CLEARCOTE_GEOIP` | `1` to match timezone, locale and WebRTC IP to the proxy exit |
| `CLEARCOTE_HEADLESS` | `0` to show the browser window (default is headless) |
| `CLEARCOTE_LICENSE_KEY` | a Clearcote licence key, only if the user has one |

## 4. Verify

Restart the MCP server in the client, then call the `navigate` tool with `https://example.com` and `read_page`. The
page text should come back as Markdown. About 20 tools are listed (`navigate`, `read_page`, `click`, `fill_field`,
`screenshot_page`, `get_cdp_endpoint`, ...).

If the first call times out, call it again: the browser download on first use is the usual cause.
