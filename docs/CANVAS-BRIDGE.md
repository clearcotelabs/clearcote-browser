# Canvas Bridge

Render canvas and WebGL on a **real remote GPU** so the pixels a page reads back
are coherent with the GPU your profile claims.

> **Status:** experimental — ships in `v0.1.0-pre.11`. Opt-in: with no
> `--canvas-bridge-url`, clearcote renders entirely locally, exactly as before.

## Why

clearcote renders canvas/WebGL on whatever GPU the host machine actually has. If
a profile claims a *different* GPU than the host, strict anti-detect /
browser-tampering checks that compare the rendered pixels against the claimed
hardware can notice the mismatch (you cannot make one GPU emit another GPU's
exact pixels in software — see [docs/RESEARCH.md](RESEARCH.md)). The canvas
bridge removes the mismatch: instead of rendering locally and only spoofing the
GPU *string*, it **forwards the canvas/WebGL operations to a remote host that has
the GPU you want to present**, and returns that host's real pixels.

Because it forwards the *operations* (not a fixed library of pre-recorded
images), it handles arbitrary and procedurally-generated canvases — not just
known probes.

## How it works

```
   clearcote (your automation host)          bridge host (real GPU)
   +-----------------------------+           +------------------------+
   | page: getImageData /        |  ops  --> | headless clearcote      |
   |       toDataURL / readPixels |          | renders on the real GPU,|
   | CanvasBridgeClient  <--- pixels ---------|  reads back the pixels  |
   +-----------------------------+           +------------------------+
```

The readback APIs (`getImageData`, `toDataURL`, `readPixels`, `measureText`)
return the bridge host's authentic pixels; the local farbling noise is bypassed
on the bridge path (the bridge pixels are ground truth). Transport is a
WebSocket carrying a compact binary message stream; the persona seed
(`--fingerprint`) is sent to the server so it selects a matching GPU profile.

## What you need

1. **A bridge host** — a machine (Windows, real GPU) that does the rendering.
   *Its GPU becomes the canvas identity your profiles present,* so pick hardware
   matching the persona you want to show (an NVIDIA box to present NVIDIA, etc.).
2. **A private network path** between your automation host and the bridge host.
   The bridge speaks plaintext WebSocket (`ws://`) — **always run it over a
   private network or an encrypted tunnel** (Tailscale, WireGuard, or SSH port
   forwarding). Never expose the bridge port on the public internet.

## Setup

### 1. Start the bridge server (on the real-GPU host)

The server is a small Python coordinator (in
[`tools/canvas-bridge-server/`](../tools/canvas-bridge-server/)) that launches a
**headless clearcote** and replays the forwarded ops on its real canvas via the
DevTools protocol — so the pixels it returns are exactly what a real clearcote on
that GPU produces, no approximation.

```bash
pip install playwright    # one-time
python tools/canvas-bridge-server/server.py \
    --chrome /path/to/clearcote/chrome.exe \
    --port 8443 \
    --fingerprint <seed>      # the persona/GPU this host presents
```

The `--fingerprint` seed selects the persona (and thus the GPU strings) the
server presents; run it with the **same seed as your clients** so the whole
identity stays coherent. The server currently relies on the private network /
tunnel below for access control (it does not yet enforce `--canvas-bridge-auth`),
so never expose its port publicly.

### 2. Tunnel it (recommended)

`ws://` is unencrypted. Put both hosts on the same Tailscale/WireGuard network,
or forward the port over SSH:

```bash
# on the automation host:
ssh -N -L 8443:localhost:8443 user@bridge-host
# now the bridge is reachable at ws://127.0.0.1:8443
```

### 3. Launch the client (your automation clearcote) pointing at the bridge

```bash
clearcote --canvas-bridge-url=ws://127.0.0.1:8443 \
          --canvas-bridge-auth=user:secret \
          --no-sandbox \
          --fingerprint=<seed>
```

| Flag | Meaning |
|---|---|
| `--canvas-bridge-url=ws://host:port` | Bridge endpoint. **Required** to enable the bridge. |
| `--canvas-bridge-auth=user:secret` | HTTP Basic credentials; must match the server. |
| `--no-sandbox` | **Required** — the client opens the bridge socket from the renderer process, which the sandbox blocks. |
| `--fingerprint=<seed>` | Your persona; the seed is sent to the server to pick a matching GPU profile. |

### Using it from the SDK

Pass the flags through your launch arguments:

```python
import clearcote
browser = clearcote.launch(
    fingerprint="user-1",
    args=[
        "--canvas-bridge-url=ws://127.0.0.1:8443",
        "--canvas-bridge-auth=user:secret",
        "--no-sandbox",
    ],
)
```

```js
// Node
const browser = await clearcote.launch({
  fingerprint: "user-1",
  args: [
    "--canvas-bridge-url=ws://127.0.0.1:8443",
    "--canvas-bridge-auth=user:secret",
    "--no-sandbox",
  ],
});
```

## Verify it's working

1. The client log prints `canvas-bridge: connected to <host>:<port>` on a
   successful connection (run with `--enable-logging=stderr --v=1` to see it).
2. Load a page that hashes a canvas/WebGL surface. With the bridge connected, the
   hashes match the **bridge host's** GPU, not your automation host's. A quick
   check: run the same `canvas.toDataURL()` with and without the bridge — the
   results differ (the bridge result equals the bridge host's render).
3. If the bridge is unreachable, clearcote logs a warning and **falls back to
   local rendering** — a misconfigured bridge degrades gracefully, it does not
   break the page.

## Caveats & limits

- **Canvas identity = the bridge host, not the seed.** Every profile that shares
  one bridge host shares that host's canvas/WebGL identity, so they are linkable
  by canvas hash. For many *unlinkable* identities, run **one bridge host (GPU)
  per identity group**.
- **Latency.** Each readback is a blocking network round-trip (5s timeout, then
  local fallback). Keep the bridge host on the same LAN/datacenter; avoid the
  bridge for latency-sensitive, canvas-heavy pages.
- **`--no-sandbox` is required** on the client.
- **Plaintext transport** — always tunnel; never expose the bridge port publicly.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Canvas still matches the local GPU | Bridge not connected. Check for `canvas-bridge: connected` in the log; verify `--no-sandbox`, the URL/port, that `--canvas-bridge-auth` matches the server, and the tunnel is up. |
| Page stalls briefly, then renders locally | Readback timing out (server slow/unreachable). Check the server is running and network latency. |
| `malformed --canvas-bridge-url` in the log | URL must be `ws://host:port[/path]` (or `wss://`, treated as plaintext — tunnel it). |
| Connection refused | Server not listening / firewall / wrong port; confirm the SSH or Tailscale tunnel. |
