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
the Windows metric-clone fonts, and defaults to a coherent **native Linux** persona. The browser
runs **headful on a virtual X display (Xvfb) by default** — a real headed Chrome avoids the
headless-mode tells some detectors probe. Set `CC_HEADLESS=1` for the old pure-headless mode.

## Configure the persona (env vars)

| env | example | meaning |
|---|---|---|
| `CC_PLATFORM` | `windows` \| `linux` \| `macos` \| `android` | spoofed OS |
| `CC_FINGERPRINT` | `user-7423` | seed → stable, unlinkable identity. Unset: a random seed per container, kept in the profile directory (printed at start); set but empty: no seed |
| `CC_BRAND` | `Edge` | brand (UA + UA-CH) |
| `CC_BRAND_VERSION` | `150.0.4078.65` | brand/version (drives TLS via `match-persona`) |
| `CC_ACCEPT_LANGUAGE` | `de-DE,de` | locale |
| `CC_TIMEZONE` | `Europe/Berlin` | IANA timezone |
| `CC_HARDWARE_CONCURRENCY` | `8` | `navigator.hardwareConcurrency` |
| `CC_GPU_VENDOR` / `CC_GPU_RENDERER` | `Google Inc. (NVIDIA)` / `ANGLE (NVIDIA …)` | WebGL strings |
| `CC_TLS_PROFILE` | `match-persona` | TLS ClientHello follows the claimed Chrome major |
| `CC_HEADLESS` | `1` | force pure-headless (default is **headful on Xvfb**) |
| `CC_SCREEN` | `1920x1080x24` | Xvfb virtual screen geometry (headful mode) |
| `CC_PORT` | `9222` | exposed CDP port |
| `CC_WIDEVINE` | `1` \| `0` | seed the Widevine CDM — **auto-on for `CC_PLATFORM=windows`** |
| `CC_SHADER_DIALECT` | `hlsl` \| `0` | report ANGLE's translated shader as HLSL — **auto-on for `CC_PLATFORM=windows`** |
| `CLEARCOTE_LICENSE_KEY` | `cc_lic_...` | use the licensed engine instead of the bundled open one — see below |
| `CC_VERSION` | `152` \| `152.0.7977.82` \| `r24` | with a **Pro** key: pin a major, an exact build or a revision (default: the newest your key allows). Free keys always get the latest build and are refused a pin |
| `CC_IDLE_EXIT_SECONDS` | `30` | stop once no CDP client has been connected for this long (default: never). Pair it with `--rm` so an abandoned container also disappears |
| `CC_SECRETS_FILE` | `/tmp/clearcote-secrets.json` | a JSON file holding `CLEARCOTE_LICENSE_KEY` and/or `CC_PROXY`, read once at start and deleted — see [Secrets](#secrets-and-docker-inspect) |

```bash
docker run -d -p 9222:9222 \
  -e CC_PLATFORM=windows -e CC_FINGERPRINT=user-7423 -e CC_BRAND=Edge \
  teamflatearth/clearcote
```

### Windows persona on this Linux host

Two things switch on automatically when `CC_PLATFORM=windows`, because they are exactly the places
where running a Windows persona on a Linux host is readable as a contradiction:

* **Widevine CDM** — a build branded Google Chrome that claims Windows and carries no CDM is
  readable by any page.
* **Shader dialect** — the persona advertises a Direct3D renderer, but ANGLE's Vulkan backend
  answers `getTranslatedShaderSource()` with a SPIR-V dump, so the renderer string and the dialect
  beside it name two different graphics backends.

Neither applies to a Linux persona, so the default container is unchanged. Override either with
`CC_WIDEVINE=0` / `CC_SHADER_DIALECT=0`. Rendering is unaffected by the dialect setting — only the
debug-extension query changes — and engines older than **151 r15** ignore it. See
[/docs/shader-dialect](https://www.clearcotelabs.com/docs/shader-dialect).

## Licensed engine (Free with GitHub or Pro)

The image bakes in the **open** engine. Set `CLEARCOTE_LICENSE_KEY` and the container resolves the
**licensed** engine instead (Chromium 152 today), downloading it once into the cache volume:

```bash
docker run -d -p 127.0.0.1:9222:9222 \
  -e CLEARCOTE_LICENSE_KEY=cc_lic_... \
  -v clearcote-cache:/opt/xdg-cache \
  teamflatearth/clearcote
```

- **Mount the cache volume.** Without it every new container downloads the engine again.
- **One licence seat per running container.** The container takes a seat when its browser starts,
  keeps it while the browser runs, and gives it back on `docker stop`. If the seat can't be taken
  (limit reached, key revoked, no network), the container exits with the reason instead of starting
  a browser that can't run.
- **Free keys** (sign in with GitHub at [clearcotelabs.com](https://clearcotelabs.com/pricing#free))
  run **one browser at a time** across all your containers: a second container exits with
  `The free tier runs one browser at a time` until the first is stopped. They need an image built
  from **SDK 0.30.0 or newer** — `docker pull teamflatearth/clearcote` to refresh. An older image is
  refused outright on a free key, because the licensed browser expects the container to keep its
  licence current while it runs; that is also what stops a running free container within a couple of
  minutes if the key is revoked, checked in elsewhere, or over its limit.
- **Pro keys** have no cap on containers during the beta.
- The container needs outbound HTTPS to `clearcotelabs.com` for the licence.

## Security

The CDP endpoint is **full browser control**. Publish it only to trusted networks — bind it
host-local with `-p 127.0.0.1:9222:9222`, or keep it on an internal Docker network. Never expose
`:9222` to the public internet.

### Secrets and `docker inspect`

Every variable passed with `-e` is part of the container's configuration: anyone who can run
`docker inspect` on it reads `CLEARCOTE_LICENSE_KEY`, and the password inside a `CC_PROXY` URL, in
plain text (as do `docker compose config` and most container dashboards). To keep them out of the
configuration, put them in a JSON file inside the container instead and point `CC_SECRETS_FILE` at it;
the entrypoint reads the file once and deletes it:

```bash
id=$(docker create -p 127.0.0.1:9222:9222 -e CC_SECRETS_FILE=/tmp/clearcote-secrets.json \
  -v clearcote-cache:/opt/xdg-cache teamflatearth/clearcote)
# a file owned by the image's user (uid 10001), e.g. built with: tar --owner=10001 --group=10001 --mode=600
docker cp - "$id:/tmp" < clearcote-secrets.tar
docker start "$id"
```

The SDKs' macOS `launch()` (which runs this image because there is no native macOS build) does exactly
that. Anyone with access to the Docker daemon can still read a running container's memory and files, so
treat daemon access as access to the key.

## Notes

- Each image tag is built from one SDK release: `sdk-<version>` (e.g. `sdk-0.30.0`), `<browser>` (the
  open binary it bakes in, e.g. `0.1.0-pre.23`) and `latest`. Rebuild + verify this image yourself:
  `docker build -t clearcote .` — every layer is auditable.
- `--disable-dev-shm-usage` is set; add `--shm-size=1g` on very heavy pages if needed.
- The SDKs' macOS `launch()` starts this image with `--rm`, `CC_IDLE_EXIT_SECONDS=30` and owner labels
  (`com.clearcotelabs.sdk-launch`, `com.clearcotelabs.owner-host`, `com.clearcotelabs.owner-pid`), so a
  container whose program was killed stops on its own and is removed; the next launch on that machine
  removes any left over at once. Several licensed launches share the `clearcote-cache` volume; the first
  one downloads the engine and the others wait for it.
- `tini` is PID 1, so browser helper processes that exit inside the container (for example from
  scripts you `docker exec` that launch their own browsers) are reaped instead of piling up as
  `<defunct>` entries. `docker stop` still reaches the browser and releases the licence seat.
- The image runs as a **non-root** user (`cc`). If you run the browser in your own container as
  **root**, add `--cap-add=SYS_NICE` (or run as a normal user): the open build is compiled with
  DCHECKs, and Chromium's process-priority call aborts it when a root container refuses the call.
- There is no GPU in the container, so WebGL renders in software through the backend that matches
  the GPU the persona names: a Linux persona (Mesa/OpenGL) through Mesa's own GL on the Xvfb display
  (headful, the default), a Windows persona (Direct3D11) through SwiftShader. Under `CC_HEADLESS=1`
  there is no display, so a Linux persona renders through Mesa over EGL instead (same limits).
  A `--use-angle=` in `CC_EXTRA_ARGS` wins.
  Pair with the [canvas bridge](../docs/CANVAS-BRIDGE.md) for real-GPU pixel coherence.
