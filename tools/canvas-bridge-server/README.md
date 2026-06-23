# Canvas Bridge — render server

The server side of the [canvas bridge](../../docs/CANVAS-BRIDGE.md). It replays the
canvas/WebGL operations a client forwards on a **real browser** and returns that
browser's authentic pixels (and `measureText` metrics). Whatever GPU the render
browser runs on becomes the canvas identity your clients present.

It has two backends:

| `--backend` | Renders on | Use when |
|---|---|---|
| `local` | a local headless clearcote (this host's GPU) | you have a real-GPU machine to render on |
| `cdp` | **any browser you reach over CDP** | you want the canvas identity to be some *other* host's GPU — e.g. a real consumer GPU from a browser-hosting provider, or a browser on a spare PC |

## Run it

```bash
pip install playwright

# A) render on this host's GPU
python server.py --backend local --chrome /path/to/clearcote/chrome.exe --port 9099

# B) render on a browser you reach over the DevTools Protocol (bring your own host)
python server.py --backend cdp --cdp-url 'ws(s)://<your-render-browser>/...' --port 9099
```

| Flag | Meaning |
|---|---|
| `--backend` | `local` or `cdp` (default `local`). |
| `--port` / `--host` | Where to listen (default `127.0.0.1:9099`). |
| `--chrome` | `local`: path to the clearcote `chrome.exe`. |
| `--fingerprint` | `local`: optional persona seed for the render browser. |
| `--cdp-url` | `cdp`: CDP/WebSocket endpoint of the render browser. |

On startup the server probes the render browser's **real** WebGL vendor/renderer and
logs it — use those exact strings for the client's `--fingerprint-gpu-vendor` /
`--fingerprint-gpu-renderer` so the client's reported GPU matches the pixels.

Then point clients at it (over a private network or tunnel — see the
[main doc](../../docs/CANVAS-BRIDGE.md)):

```
clearcote --canvas-bridge-url=ws://<host>:9099 --no-sandbox --fingerprint=<seed>
```

## Bringing your own CDP render host

`--cdp-url` accepts any CDP/WebSocket endpoint. Two common sources:

- **A browser-hosting provider.** Many services hand you a CDP/WebSocket URL to a
  remote browser. Pick a provider/plan whose nodes run on a **real consumer GPU**
  (not a datacenter GPU or a software rasterizer) if you want a plausible consumer
  canvas identity — verify it from the GPU string the server logs on connect.
- **A browser on another machine** started with `--remote-debugging-port=9222`; use
  its `webSocketDebuggerUrl` (from `http://<machine>:9222/json/version`).

If your provider issues **short-lived sessions via an API**, subclass
`RemoteCDPBackend` / override `get_cdp_url()` to create a session and return its CDP
URL, and stop the session in `close()` — closing the CDP connection usually does
**not** end the remote session or its billing. Keep any credentials in the
environment; never hard-code them.

## How it works

1. Speaks the clearcote bridge protocol (a compact little-endian binary message
   stream over a WebSocket).
2. Buffers the Canvas2D / WebGL op stream per server-side canvas id.
3. On a readback (`getImageData` / `toDataURL` / `readPixels`) or `measureText`,
   replays the ops on a real `OffscreenCanvas` via `page.evaluate` and returns the
   resulting bytes / metrics.

## Status / limits

- **Canvas2D + measureText** are fully supported. WebGL op coverage is being
  expanded (clear/readback today; full geometry on the roadmap).
- Access control currently relies on the network/tunnel — the server does not yet
  enforce `--canvas-bridge-auth`. **Never expose its port to the public internet.**
- One render host = one GPU identity; run one host per identity group for unlinkable
  canvas fingerprints. A remote host can change between reconnects, which changes the
  identity — the server logs a warning when the GPU drifts.
