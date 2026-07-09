# clearcote — Docker

Run clearcote as a **CDP endpoint** that any Playwright / Puppeteer / browser-use / Crawl4AI /
Stagehand client attaches to over the Chrome DevTools Protocol — keep your automation code, swap
the browser.

## Pull & run

```bash
docker run -d --rm -p 9222:9222 teamflatearth/clearcote      # CDP on http://localhost:9222
```

```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.new_page(); page.goto("https://example.com"); print(page.title())
```

The image bakes in the signed clearcote Linux binary (SHA-256 verified), a base font set **and**
the Windows metric-clone fonts, and defaults to a coherent **native Linux** persona.

## Configure the persona (env vars)

| env | example | meaning |
|---|---|---|
| `CC_PLATFORM` | `windows` \| `linux` \| `macos` \| `android` | spoofed OS |
| `CC_FINGERPRINT` | `user-7423` | seed → stable, unlinkable identity |
| `CC_BRAND` | `Edge` | brand (UA + UA-CH) |
| `CC_BRAND_VERSION` | `149.0.3650.65` | brand/version (drives TLS via `match-persona`) |
| `CC_ACCEPT_LANGUAGE` | `de-DE,de` | locale |
| `CC_TIMEZONE` | `Europe/Berlin` | IANA timezone |
| `CC_HARDWARE_CONCURRENCY` | `8` | `navigator.hardwareConcurrency` |
| `CC_GPU_VENDOR` / `CC_GPU_RENDERER` | `Google Inc. (NVIDIA)` / `ANGLE (NVIDIA …)` | WebGL strings |
| `CC_TLS_PROFILE` | `match-persona` | TLS ClientHello follows the claimed Chrome major |
| `CC_PORT` | `9222` | exposed CDP port |

```bash
docker run -d -p 9222:9222 \
  -e CC_PLATFORM=windows -e CC_FINGERPRINT=user-7423 -e CC_BRAND=Edge \
  teamflatearth/clearcote
```

## Security

The CDP endpoint is **full browser control**. Publish it only to trusted networks — bind it
host-local with `-p 127.0.0.1:9222:9222`, or keep it on an internal Docker network. Never expose
`:9222` to the public internet.

## Notes

- Requires the clearcote SDK **≥ 0.13.1** (pins the `v0.1.0-pre.19` binary). Rebuild + verify this
  image yourself: `docker build -t clearcote .` — every layer is auditable.
- `--disable-dev-shm-usage` is set; add `--shm-size=1g` on very heavy pages if needed.
- WebGL/WebGPU render via ANGLE/SwiftShader (no GPU in the container); pair with the
  [canvas bridge](../docs/CANVAS-BRIDGE.md) for real-GPU pixel coherence.
