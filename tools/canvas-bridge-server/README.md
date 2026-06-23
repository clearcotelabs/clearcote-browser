# Canvas Bridge — render server

The server side of the [canvas bridge](../../docs/CANVAS-BRIDGE.md). It launches a
**headless clearcote** on the host's real GPU and replays the canvas/WebGL
operations a client forwards, returning that GPU's authentic pixels (and
`measureText` metrics). Because it drives a real clearcote, the readbacks are
exactly what a real browser on this hardware produces — no approximation.

## Run it

```bash
pip install playwright
python server.py \
    --chrome /path/to/clearcote/chrome.exe \
    --port 8443 \
    --fingerprint <seed>
```

| Flag | Meaning |
|---|---|
| `--chrome` | Path to the clearcote `chrome.exe` (this host renders with it). |
| `--port` | TCP port to listen on (default `9099`). |
| `--fingerprint` | Persona seed — selects the GPU/identity this host presents. **Use the same seed as your clients** so the whole identity stays coherent. |

Then point clients at it (over a private network or tunnel — see the
[main doc](../../docs/CANVAS-BRIDGE.md)):

```
clearcote --canvas-bridge-url=ws://<host>:8443 --no-sandbox --fingerprint=<seed>
```

## How it works

1. Speaks the clearcote bridge protocol (a compact little-endian binary message
   stream over a WebSocket).
2. Buffers the Canvas2D op stream per server-side canvas id.
3. On a readback (`getImageData` / `toDataURL`) or `measureText`, replays the ops
   on a real `OffscreenCanvas` via `page.evaluate` and returns the resulting
   bytes / metrics.

## Status / limits

- **Canvas2D + measureText** are fully supported. WebGL is on the roadmap.
- Access control currently relies on the network/tunnel — the server does not yet
  enforce `--canvas-bridge-auth`. **Never expose its port to the public internet.**
- One bridge host = one GPU identity; run one host per identity group for
  unlinkable canvas fingerprints.
