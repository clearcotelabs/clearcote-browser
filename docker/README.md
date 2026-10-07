# clearcote — Docker

Run clearcote as a **CDP endpoint** that any Playwright / Puppeteer / browser-use / Crawl4AI /
Stagehand client attaches to over the Chrome DevTools Protocol — keep your automation code, swap
the browser.

## Pull & run

```bash
# the seccomp profile that lets Chrome run with its sandbox (it ships in the image)
docker run --rm --entrypoint cat teamflatearth/clearcote /etc/clearcote/seccomp.json > clearcote-seccomp.json
docker run -d --rm -p 9222:9222 --security-opt seccomp=clearcote-seccomp.json teamflatearth/clearcote
# CDP on http://localhost:9222
```

Without `--security-opt` the container still runs, with Chrome's sandbox off; see
[Chrome's sandbox](#chromes-sandbox).

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
| `CC_PROXY` | `http://user:pass@host:8080` \| `socks5://…` | send the browser's traffic through this proxy (http, https, socks4, socks5; the container refuses to start rather than go direct if it cannot apply it). A password is answered by the engine when it can (the licensed builds), otherwise by a relay on the container's own loopback. Pass it through `CC_SECRETS_FILE` to keep the password out of `docker inspect` |
| `CC_IDLE_EXIT_SECONDS` | `30` | stop once no CDP client has been connected for this long, counted from when the CDP endpoint first answers (default: never). Pair it with `--rm` so an abandoned container also disappears |
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

### Chrome's sandbox

Chrome's Linux sandbox runs every renderer (the process that parses and runs a page) in user, PID and
network namespaces of its own, under a seccomp-bpf filter, so a page that compromises its renderer is
still confined to it. With `--no-sandbox` that renderer has everything the container's user has: its
files, its network and its other processes. (`--no-sandbox` also raises Chrome's "unsupported
command-line flag" warning bar, which the entrypoint then has to keep off the screen with another switch.)

Docker's default seccomp profile refuses the calls that create those namespaces. So the entrypoint
checks first: it makes the same calls Chrome's sandbox makes, and runs Chrome **with** its sandbox
when they work. When they do not, it runs Chrome with `--no-sandbox`, exactly as before, and logs one
line saying why and how to turn the sandbox on, for example:

```
[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: clone(CLONE_NEWUSER) failed (Operation not permitted): the container's seccomp profile blocks the namespaces Chrome's sandbox needs (Docker's default profile does). To turn the sandbox on, start the container with --security-opt seccomp=<profile>; ...
```

With the profile it logs `[clearcote] sandbox: on (...)`, and the `serve-state` line carries
`"sandbox": true`.

**The profile.** [`seccomp.json`](seccomp.json) (also in the image at `/etc/clearcote/seccomp.json`)
is Docker's default profile, unchanged (the one Docker 29.5 to 29.8 build in: moby/profiles seccomp
v0.2.3), with two rules appended. Each carries a `"comment"` starting with `clearcote:`; drop those
two and you have Docker's file again.

| addition | why Chrome needs it |
|---|---|
| `clone` with `CLONE_NEWUSER`, `CLONE_NEWPID`, `CLONE_NEWNET` (mask `0x0E020000` must be clear: mount, cgroup, UTS and IPC namespaces stay refused) | `clone(CLONE_NEWUSER)` checks that user namespaces work, `clone(CLONE_NEWUSER\|CLONE_NEWPID\|CLONE_NEWNET)` starts the zygote, `clone(CLONE_NEWPID)` starts each renderer |
| `unshare(CLONE_NEWUSER)`, that exact flag only | Chrome's check that a user without privileges can create a user namespace calls it inside the first one |

Nothing else is needed: the calls were measured on the engine this image ships (a profile that logs
instead of refusing showed exactly these two; with either rule removed Chrome aborts at start). Do
not use `--privileged`, `--cap-add SYS_ADMIN` or `seccomp=unconfined` instead: they also let the
sandbox run, but turn off far more of the container's protection.

```bash
docker run -d -p 127.0.0.1:9222:9222 --security-opt seccomp=clearcote-seccomp.json teamflatearth/clearcote
```

```yaml
# docker compose
services:
  clearcote:
    image: teamflatearth/clearcote
    ports: ["127.0.0.1:9222:9222"]
    security_opt: ["seccomp=./clearcote-seccomp.json"]
```

The SDKs' macOS `launch()` passes the profile itself to an image that uses it (serve protocol 3).

The sandbox also needs:

- **the image's own user.** Chrome's sandbox does not run as root, so with `--user 0` the container
  falls back (and says so).
- **`CAP_SYS_CHROOT`**, which Docker grants by default; `--cap-drop ALL` removes it (add
  `--cap-add SYS_CHROOT` back).
- **a host that allows unprivileged user namespaces.** Most do. The log line names the setting when
  one refuses them: `kernel.unprivileged_userns_clone=0`, `user.max_user_namespaces=0`, or an AppArmor
  policy that restricts them (`kernel.apparmor_restrict_unprivileged_userns=1`).
- **an amd64 machine.** An emulator running the amd64 image on another CPU may refuse the namespace
  flags; the container then falls back and says so.

To turn the sandbox off on purpose, pass `CC_EXTRA_ARGS=--no-sandbox`. A `--canvas-bridge-url=` in
`CC_EXTRA_ARGS` turns it off too: the [canvas bridge](../docs/CANVAS-BRIDGE.md) opens its socket from the
renderer, which the sandbox does not allow.

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

A secrets file keeps a proxy's password out of the configuration, not out of the process list: the licensed
engine logs in to the proxy itself, so its username and password are on the browser's command line
(`--proxy-auth` for an http or https proxy, `--socks5-credentials` for SOCKS5). Anyone who can list the
container's processes sees them: `docker top`, and `ps` on a Linux machine that runs the container. The open
engine keeps the password in the entrypoint's relay instead, off the command line.

## Notes

- Each image tag is built from one SDK release: `sdk-<version>` (e.g. `sdk-0.30.0`), `<browser>` (the
  open binary it bakes in, e.g. `0.1.0-pre.23`) and `latest`. Rebuild + verify this image yourself:
  `docker build -t clearcote .` — every layer is auditable.
- `--disable-dev-shm-usage` is set; add `--shm-size=1g` on very heavy pages if needed.
- The image carries the label `com.clearcotelabs.serve-protocol` (2: it takes `CC_SECRETS_FILE` and
  `CC_IDLE_EXIT_SECONDS`; 3: it also runs Chrome's sandbox when the container allows it), and its
  entrypoint logs one `[clearcote] serve-state {...}` line saying what it applied: the engine that
  resolved (`licensed` or `open`), the proxy, how a proxy's password is answered (`engine` or `relay`),
  the idle exit, the sandbox. The SDKs' macOS `launch()` reads the label before it starts a
  container (an image without it gets plain variables, with a warning, and is refused a proxy password it
  cannot answer) and refuses a container whose log does not show the licensed engine when a key was
  given, or the proxy (and its login) when one was given.
- The SDKs' macOS `launch()` starts this image with `--rm`, `CC_IDLE_EXIT_SECONDS=30`, the seccomp profile
  (serve protocol 3) and owner labels
  (`com.clearcotelabs.sdk-launch`, `com.clearcotelabs.owner-host`, `com.clearcotelabs.owner-token`), so a
  container whose program was killed stops on its own and is removed; the next launch from the same user
  and machine removes any whose owner process is certainly gone at once (never one from another machine or
  container sharing the Docker daemon). Several licensed launches share the `clearcote-cache` volume; the
  first one downloads the engine and the others wait for it.
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
