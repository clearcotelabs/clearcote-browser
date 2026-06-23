# Canvas Bridge

Render canvas and WebGL on a **real remote GPU** so the pixels a page reads back are
coherent with the GPU your profile claims.

> **Status:** experimental — ships in `v0.1.0-pre.11`. Opt-in: with no
> `--canvas-bridge-url`, clearcote renders entirely locally, exactly as before.

## Why

clearcote renders canvas/WebGL on whatever GPU the host machine actually has. If a
profile claims a *different* GPU than the host, strict anti-detect / browser-tampering
checks that compare the rendered pixels against the claimed hardware can notice the
mismatch (you cannot make one GPU emit another GPU's exact pixels in software — see
[docs/RESEARCH.md](RESEARCH.md)). The canvas bridge removes the mismatch: instead of
rendering locally and only spoofing the GPU *string*, it **forwards the canvas/WebGL
operations to a remote browser that has the GPU you want to present**, and returns
that browser's real pixels.

Because it forwards the *operations* (not a fixed library of pre-recorded images), it
handles arbitrary and procedurally-generated canvases — not just known probes.

## How it works

```
   clearcote (your automation host)          render browser (real GPU)
   +-----------------------------+           +------------------------+
   | page: getImageData /        |  ops  --> | a real browser renders |
   |   toDataURL / readPixels /  |           |  the ops on its real   |
   |   measureText               |           |  GPU and reads back     |
   | CanvasBridgeClient  <--- pixels ---------|  the pixels            |
   +-----------------------------+           +------------------------+
```

The readback APIs (`getImageData`, `toDataURL`, `readPixels`, `measureText`) return
the render browser's authentic pixels; the local farbling noise is bypassed on the
bridge path (the bridge pixels are ground truth). Transport is a WebSocket carrying a
compact binary message stream.

The render browser can be:

- **a browser you reach over the Chrome DevTools Protocol** (`--backend cdp`) — the
  recommended setup, because you can choose a host whose GPU is the consumer GPU you
  want to present; or
- **a local headless clearcote** (`--backend local`) — simplest, but the canvas
  identity is then your own machine's GPU.

The reference render server lives in
[`tools/canvas-bridge-server/`](../tools/canvas-bridge-server/).

## Choosing where to render — the GPU *is* your canvas identity

Whatever GPU the render browser runs on **becomes the canvas/WebGL fingerprint every
client that uses it presents.** Two things follow:

1. **Pick a real consumer GPU.** A render host with a genuine consumer GPU (a typical
   laptop/desktop integrated or discrete GPU) yields a plausible, common canvas
   identity. Avoid hosts that render with a **software rasterizer** (e.g. a
   "Basic Render Driver" / SwiftShader / llvmpipe) or a **datacenter GPU** — both are
   coherent but stick out as non-consumer hardware. You can tell which you got from
   the GPU string the server logs on connect (step 3 below).
2. **One render host = one canvas identity.** Every profile sharing a render host
   shares its canvas hash, so they are linkable by canvas. For many *unlinkable*
   identities, use **one render host (GPU) per identity group**.

You **bring your own render host.** Anything that exposes a CDP/WebSocket endpoint to
a browser works — a browser-hosting provider, or a browser you run yourself on a spare
machine. clearcote does not ship or endorse a provider; choose one that meets the
criteria above.

## Tutorial — render on a remote real-GPU browser (recommended)

### 1. Get a CDP endpoint to a real-consumer-GPU browser

Obtain a Chrome DevTools Protocol WebSocket URL (`ws://…` or `wss://…`) for a browser
running on the GPU you want to present. Either:

- **From a browser-hosting provider** that gives you a CDP/WebSocket URL for a remote
  browser session. Choose a provider/plan whose nodes run on a **real consumer GPU**
  (verify in step 3). If the provider issues short-lived sessions through an API, see
  *Short-lived sessions* under [Operational notes](#operational-notes--gotchas).
- **From a browser on another machine** you control, started with
  `--remote-debugging-port=9222`. Its CDP URL is the `webSocketDebuggerUrl` field of
  `http://<machine>:9222/json/version`.

### 2. Start the render server pointed at that browser

```bash
pip install playwright            # one-time (no browser download needed for CDP)
python tools/canvas-bridge-server/server.py \
    --backend cdp \
    --cdp-url 'ws(s)://<your-render-browser>/...' \
    --port 9099
```

### 3. Note the render GPU the server reports

On connect the server probes the render browser's **real** WebGL strings and logs
them, e.g.:

```
[backend] connected over CDP; render GPU='ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)'
[server] backend=cdp up; render GPU='ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)'; listening on ws://127.0.0.1:9099
```

Confirm it's a real consumer GPU (not a software renderer or datacenter card). Copy
the vendor + renderer strings — you'll match the client to them next.

### 4. Launch clearcote with the bridge **and a matching GPU** (coherence)

The bridge makes the canvas *pixels* match the render GPU; you must also make
clearcote's WebGL `getParameter(UNMASKED_*)` *strings* match it, or the string and the
pixels disagree. Pass the exact strings from step 3:

```bash
clearcote \
  --canvas-bridge-url=ws://127.0.0.1:9099 \
  --no-sandbox \
  --fingerprint=<seed> \
  --fingerprint-gpu-vendor='Google Inc. (Intel)' \
  --fingerprint-gpu-renderer='ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)'
```

| Flag | Meaning |
|---|---|
| `--canvas-bridge-url=ws://host:port` | Bridge endpoint. **Required** to enable the bridge. |
| `--no-sandbox` | **Required** — the client opens the bridge socket from the renderer process, which the sandbox blocks. |
| `--fingerprint=<seed>` | Your persona seed. |
| `--fingerprint-gpu-vendor` / `--fingerprint-gpu-renderer` | Set to the render GPU from step 3 so the reported GPU string matches the bridged pixels. |
| `--canvas-bridge-auth=user:secret` | Optional HTTP Basic credentials (if your deployment adds auth in front of the server). |

### 5. Verify

- The client log prints `canvas-bridge: connected to <host>:<port>` (run with
  `--enable-logging=stderr --v=1`).
- The server log prints `rendered id=… (N ops)` lines as the page hashes canvases —
  that's your client's canvas work being rendered on the remote GPU.
- In the page, `gl.getParameter(UNMASKED_RENDERER_WEBGL)` equals the render GPU, and a
  `canvas.toDataURL()` hash matches that GPU (differs from a no-bridge run).

## Alternative — render on your own machine

If you have a real-GPU machine to dedicate, skip the CDP host and render locally:

```bash
python tools/canvas-bridge-server/server.py \
    --backend local --chrome /path/to/clearcote/chrome.exe --port 9099
```

The canvas identity is then that machine's GPU.

## Using it from the SDK

Pass the flags through your launch arguments:

```python
import clearcote
browser = clearcote.launch(
    fingerprint="user-1",
    args=[
        "--canvas-bridge-url=ws://127.0.0.1:9099",
        "--no-sandbox",
        "--fingerprint-gpu-vendor=Google Inc. (Intel)",
        "--fingerprint-gpu-renderer=ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
    ],
)
```

```js
// Node
const browser = await clearcote.launch({
  fingerprint: "user-1",
  args: [
    "--canvas-bridge-url=ws://127.0.0.1:9099",
    "--no-sandbox",
    "--fingerprint-gpu-vendor=Google Inc. (Intel)",
    "--fingerprint-gpu-renderer=ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
  ],
});
```

## Operational notes & gotchas

- **The render host never touches your target site.** It only rasterizes canvas
  pixels. Its value is its **GPU**, not its IP — your target-facing IP/proxy is a
  separate concern on the *client*. For a coherent identity, pair a client whose IP
  looks like a consumer connection with a render host whose GPU looks like a consumer
  GPU.
- **Short-lived sessions.** If your provider issues sessions that expire, mint a fresh
  CDP URL when needed: subclass `RemoteCDPBackend` / override `get_cdp_url()` in the
  reference server. **Closing the CDP connection usually does not stop the remote
  session or its billing** — call the provider's stop endpoint explicitly (do it in
  `close()`).
- **Pin the host for a stable identity.** A reconnect can land on a *different* remote
  machine with a *different* GPU, which changes the canvas identity. If your provider
  supports pinning a specific node, use it; the server warns when the GPU drifts.
- **Control-plane bot protection.** If your provider's session API sits behind a bot
  filter, requests from a bare HTTP client may be blocked — send a normal browser
  `User-Agent` (and any required headers) on those API calls.
- **Latency.** Each readback is a blocking network round-trip (5s timeout, then local
  fallback). Keep the render host close (low RTT); avoid the bridge for
  latency-sensitive, canvas-heavy pages.
- **Plaintext transport.** `ws://` is unencrypted — run the bridge over a private
  network or an encrypted tunnel (Tailscale, WireGuard, SSH `-L`). Never expose the
  server's port on the public internet.

## Caveats & limits

- **Canvas2D + measureText** are fully bridged. WebGL op coverage is being expanded
  (clear/readback today; full geometry on the roadmap).
- **`--no-sandbox` is required** on the client.
- Graceful fallback: if the bridge is unreachable, clearcote logs a warning and
  **renders locally** — a misconfigured bridge degrades gracefully, it does not break
  the page.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Canvas still matches the local GPU | Bridge not connected. Check for `canvas-bridge: connected` in the client log; verify `--no-sandbox`, the URL/port, and the tunnel. |
| GPU string and canvas pixels disagree | You didn't set `--fingerprint-gpu-vendor/-renderer` to the render GPU from the server log (step 3/4). |
| Render GPU is a software renderer / datacenter card | Your render host has no suitable consumer GPU — choose a different host/provider/plan. |
| Page stalls briefly, then renders locally | Readback timing out (host slow/unreachable). Check the server and network RTT. |
| `malformed --canvas-bridge-url` in the log | URL must be `ws://host:port[/path]` (or `wss://`, treated as plaintext — tunnel it). |
| Connection refused | Server not listening / firewall / wrong port; confirm the tunnel. |
