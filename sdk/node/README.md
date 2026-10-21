# clearcote (Node / TypeScript SDK)

A **Playwright drop-in** for [Clearcote](https://github.com/clearcotelabs/clearcote-browser) — the
open, reproducible, anti-fingerprint Chromium build. `launch()` returns a standard Playwright
`Browser`, so migrating is a one-line import change.

The verified Clearcote binary is **auto-downloaded and SHA-256 checked** on first use, then cached —
you don't manage zips or paths. Or run the same code on a hosted Clearcote browser with
`launch({ cloud: true })`: see [Local or cloud](#local-or-cloud).

> **Platform:** Clearcote ships **Windows x64** and **Linux x64** binaries, plus a **macOS arm64** (Apple
> Silicon) PRO build; `launch()` runs on all of them and the SDK auto-downloads the right one for your OS. On Linux the persona is Linux-native (Linux
> GPU/voices/audio-device values) and DRM uses the Linux CDM. On an Apple Silicon Mac
> with a licence key, `launch()` runs the native macOS PRO build (a `Chromium.app`, unsigned: the SDK clears its
> quarantine flag and passes `--use-mock-keychain`). Its fonts and canvas text metrics stay the Mac's own, so keep
> `platform: "macos"` (the default there); another platform warns unless a `fingerprint_profile` captured on that OS
> supplies them. Without a licence key (the free build has no macOS binary) or on an Intel Mac, `launch()` runs the
> [Docker image](../../docker/README.md) through Docker Desktop instead (SDK 0.40.0+; `docker: true` or
> `CLEARCOTE_DOCKER=1` does the same on any OS, a licensed Mac included). On a minimal Linux host, install `xz-utils` (the Node SDK unpacks the
> `.tar.xz` with the system `tar`) plus the browser's runtime libs (e.g.
> `apt-get install -y xz-utils libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2
> libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libpango-1.0-0
> libcairo2 libx11-6 libxcb1 libexpat1 libdbus-1-3`) and pass `args: ["--no-sandbox"]` (or
> `chown root:root chrome-sandbox && chmod 4755 chrome-sandbox`) in containers.
> Running as **root** in a container? Run as a normal user (the official Docker image does) or add
> `--cap-add=SYS_NICE`: the open build is compiled with debug assertions, and a root container that
> refuses Chromium's process-priority call stops it at start.

## Install

```bash
npm install clearcote
```

`playwright-core` comes as a dependency — you do **not** need to install Playwright separately or
run `playwright install` (Clearcote uses its own browser binary).

## Usage

```ts
import { launch } from "clearcote";

const browser = await launch({
  fingerprint: "user-7423",        // per-eTLD+1 seed: same seed => same identity, different => unlinkable
  platform: "windows",
  timezone: "America/New_York",
  headless: false,
});

const page = await browser.newPage();
await page.goto("https://abrahamjuliot.github.io/creepjs/");
// ... standard Playwright from here ...
await browser.close();
```

Already using Playwright? Swap `chromium.launch(...)` for `launch(...)` from `clearcote` — the
returned object is a normal Playwright `Browser`.

### Light stealth (`lightStealth: true`)

When the full seed-derived persona is more than a target needs, `lightStealth` spoofs only a
coherent, seed-derived bundle of the *safe* metadata axes — `hardwareConcurrency`, `deviceMemory`,
`colorDepth`, `devicePixelRatio`, `maxTouchPoints` — via native override switches, leaving rendering
(canvas/WebGL/audio/fonts), TLS and the real browser version untouched. Screen dimensions stay real by
default (opt-in), since a faked screen that can't be reconciled with the real render surface is a
reliable block trigger. It never engages the `--fingerprint` persona machinery.

```javascript
const browser = await launch({ lightStealth: true, fingerprint: "my-seed" });
```

Or set any native override directly (no seed needed) — an explicit value always wins over the persona:

```javascript
const browser = await launch({ hardwareConcurrency: 8, deviceMemory: 8, devicePixelRatio: 1.25, maxTouchPoints: 0 });
```

Native overrides: `hardwareConcurrency`, `deviceMemory`, `colorDepth`, `devicePixelRatio`,
`maxTouchPoints`, and (opt-in) `screenWidth` / `screenHeight` / `availWidth` / `availHeight`. Needs the
Clearcote 149.0.7827.114 (v0.1.0-pre.22) build or newer.

### Standing CDP endpoint (`serve()`)

Run Clearcote as a **stealthy CDP endpoint** any existing automation attaches to unchanged —
Playwright's `connectOverCDP`, `puppeteer.connect`, or browser-use / Crawl4AI / Stagehand. Where
`launch()` spawns a Playwright-owned browser, `serve()` launches the binary **directly** — so
`--enable-automation` is never added and `navigator.webdriver` stays `false`; the port binds to
loopback (`127.0.0.1`) behind an origin allowlist.

```js
import { serve } from "clearcote";
import { chromium } from "playwright";

const srv = await serve({ fingerprint: "seed-123", platform: "windows" });  // same options as launch()
console.log(srv.cdpUrl);                                                    // http://127.0.0.1:<port>

const browser = await chromium.connectOverCDP(srv.cdpUrl);                  // your code, unchanged
// ... or puppeteer.connect({ browserURL: srv.cdpUrl }) / browser-use / Crawl4AI / Stagehand ...
await srv.close();
```

The returned `Server` exposes `.cdpUrl`, `.wsUrl()`, and `.close()`. (For a no-code CDP endpoint, the
official Docker image also works: `docker run -d --rm --shm-size=1g -p 127.0.0.1:9222:9222 teamflatearth/clearcote`.)

Headless, `serve()` gives the browser a real-size display (the persona's, or one drawn from real
desktops) and maximizes its window onto the work area before any client attaches, so every page,
tab and popup reports a window that fits its screen. Pass `windowSize: { width, height }` for a
smaller window (clamped to the work area), or your own `--window-size` / `--screen-info` in `args` to
opt out. On an engine with persona windows a seeded `serve()` opens the persona's own window instead
(see [Window geometry](#window-geometry-the-personas-own-window)); `windowSize` still wins.

**Drive it from an AI agent (MCP).** Point Claude Desktop / Cursor / Cline at the
[`clearcote-mcp`](https://github.com/clearcotelabs/clearcote-browser/tree/main/mcp) server
(`npx -y clearcote-mcp` or `pip install clearcote-mcp`) — ~20 tools over one shared stealth browser.

### Many identities on one endpoint (`serveMultiplex`, `clearcote serve`)

One port, one browser per identity, chosen by the connection URL:

```ts
import { chromium } from "playwright-core";
// in another process:  npx clearcote serve --port 9222 --idle-timeout 300
const a = await chromium.connectOverCDP("http://127.0.0.1:9222?fingerprint=acct-1&timezone=Europe/Berlin");
const b = await chromium.connectOverCDP(`http://127.0.0.1:9222?fingerprint=acct-2&proxy=${encodeURIComponent("socks5://u:p@host:1080")}&geoip=true`);
```

- Query options: `fingerprint`, `timezone`, `locale`, `proxy`, `geoip`, plus any fingerprint option in kebab-case (`platform`, `hardware-concurrency`, `allow-third-party-cookies`, ...). Unknown options are rejected (400).
- The same identity reuses its browser. Asking for a running identity with *different* options returns 409 — close it first.
- `GET /` lists running browsers; `POST /fingerprint/<id>/close` stops one; `--idle-timeout <s>` closes a browser after its last client disconnects; `--max-browsers` caps them (default 16).
- Behind a reverse proxy, WebSocket URLs honour `X-Forwarded-Host` / `X-Forwarded-Proto`; add the public name with `--allow-host`.
- Binds `127.0.0.1` by default. Requests a web page could make (cross-site fetches, foreign `Origin`, a `Host` that isn't an IP / `localhost` / allowed) are refused.

In code: `serveMultiplex({ port: 9222, idleTimeoutSec: 300, dataDir: "./profiles" })`.

### `clearcote` command line

```bash
npx clearcote install [--version 152] [--channel preview]
npx clearcote info [--quick] [--json] [--proxy <url>]   # alias: doctor
npx clearcote bench [--concurrency 3] [--url <url>] [--proxy <url>] [--duration 30] [--json]
npx clearcote login [key]      # validates the key, saves it to ~/.clearcote/license.key
npx clearcote logout
npx clearcote clear-cache
npx clearcote serve [--port 9222] ...
npx clearcote cloud run|sessions|stop|events|recording|profile sync|webhooks ...   # the hosted API, see Local or cloud
```

`info` never downloads: it reports the SDK, this machine (CPU, RAM, VM or not, and whether there is a GPU Chromium can draw with), the cached builds, which engine features the binary supports, a launch test (skipped with `--quick`), licence seats, fonts and missing system libraries.

`bench` answers "why is my CPU at 100 %" and "how many browsers can I run": it starts `--concurrency` browsers (headless, temporary profiles, deleted afterwards), loads `--url` in each for `--duration` seconds, through `--proxy` if you give one, and prints what each browser costs in CPU threads and RAM on this machine, page timings (DOMContentLoaded, load, network idle), errors, about how many browsers the machine runs at once, and what to change. It runs fewer browsers than asked when free RAM looks too small (`--force` runs them all). Each browser holds a licence seat while it runs. Paste the output into a support request; proxy credentials are never printed.

Windows: when a cached build cannot start from the cache (`spawn UNKNOWN`, "the side-by-side configuration is incorrect"; this happens when the SDK runs inside an MSIX-packaged app, whose writes to `%LOCALAPPDATA%` Windows redirects), launches use one copy of that build in `~/.clearcote/recovered/` instead, made once and reused by every later launch from Python or Node. `clear-cache` removes those copies too.

### Engine options (PRO 152 r22+)

| Option | Effect |
|---|---|
| `fingerprint: "off"` | No persona at all — for telling whether a problem comes from the spoofing or from your environment. |
| `fingerprintVoices: false` | Keep the host's own `speechSynthesis` voices under a persona. |
| `allowThirdPartyCookies: false` | Block third-party cookies. Since 0.45.0 they are allowed by default, as in Chrome; embedded sign-in, payment and captcha frames rely on them. |
| `transparentProxy: true` | With a proxy: send the headers a direct connection sends, and report connection timing as a reused connection. |

On an older engine each of these is skipped with a warning; the launch still works.

`stockRuntime: true` (or `CLEARCOTE_STOCK_RUNTIME=1`) gives Playwright the browser's stock DevTools behaviour back: `page.on("console")` and `page.on("pageerror")` receive events, `page.setContent()` works, and `exposeFunction()` / `exposeBinding()` keep working after a navigation. It is off by default because pages can observe some of what it restores. It needs an engine from r32 on; an older engine launches without it and the SDK warns once. It applies to local and Docker launches, not to cloud browsers.

### On engines from r32 (`personaEnv`, light or dark)

Two more things change on an engine that supports them (the licensed build from r32). The SDK checks the binary
for each one first, so an older engine launches exactly as before.

- **The persona stays off the command line** (`personaEnv`, on by default). The seed, every persona override,
  proxy credentials and the canvas-bridge token reach the browser in the `CLEARCOTE_PERSONA_ARGS` environment
  variable instead of on its command line, which any local user can read (`/proc/<pid>/cmdline` on Linux, the
  process details on Windows). `personaEnv: false` or `CLEARCOTE_PERSONA_ENV=0` keeps them on the command line. A
  Docker launch hands the choice to the container.
- **Light or dark comes from the persona.** The engine picks the persona's colour scheme (about one persona in
  three is dark; `--fingerprint-color-scheme=light|dark` in `args` forces one), and CSS, `matchMedia`, the
  `Sec-CH-Prefers-Color-Scheme` header and system colours follow it. Playwright emulates light in every context it
  creates, which a dark persona would contradict, so on such an engine `launch()` and
  `launchPersistentContext()` leave the colour scheme unemulated (`colorScheme: null`). Pass `colorScheme`
  yourself to emulate one anyway. Docker and cloud launches are unchanged.

Also: `licenseThroughProxy: true` (or `CLEARCOTE_LICENSE_THROUGH_PROXY=1`) sends the licence calls through the launch proxy; `releaseChannel: "preview"` (or `CLEARCOTE_RELEASE_CHANNEL`) picks up PRO preview builds; `getSessionSeats()` reports seats in use.

### Through a proxy (report the proxy's IP, not your host's)

```ts
const browser = await launch({
  fingerprint: "user-7423",
  proxy: { server: "http://host:8080", username: "u", password: "p" }, // standard Playwright option
  timezone: "America/New_York",
  webrtcIp: "203.0.113.10",        // make WebRTC report the proxy egress IP, not your host's
});
```

**WebRTC won't leak your real IP.** The engine *fabricates* the WebRTC server-reflexive (`srflx`) candidate at `webrtcIp` and sends **no real STUN** from your host — so WebRTC reports the proxy IP and your real IP never leaks at the packet level. A plain candidate "relabel" doesn't stop the leak (the real STUN packet still goes out from your host); Clearcote sends none. Raw host candidates are suppressed, and the candidate set stays coherent (not empty/disabled).

### Auto geo-match (`geoip`)

Set `geoip: true` and Clearcote resolves the **proxy's exit IP** (looked up *through* the proxy) and auto-fills any unset `timezone`, `acceptLanguage`, `location`, **and `webrtcIp`** so the whole identity — clock, language, geo, and WebRTC IP — matches the proxy's region:

```ts
const browser = await launch({
  fingerprint: "user-7423",
  proxy: { server: "http://host:8080", username: "u", password: "p" },
  geoip: true,              // timezone, languages, location, AND WebRTC IP all auto-set to the proxy's geo
});
```

Anything you set explicitly wins over `geoip`. With no proxy it uses your direct connection's IP. The lookup goes through the proxy — http(s) and SOCKS5 (with username/password) alike.

The whole lookup has one deadline, `CLEARCOTE_GEOIP_TIMEOUT_SECONDS` (default 20). If the region can't be resolved in time, `launch()` throws `GeoipError` (code `GEOIP_UNRESOLVED`) **before** any browser starts, instead of launching with this machine's clock and language. If you set both `timezone` and `acceptLanguage` yourself, it warns and launches anyway.

Geo data comes from the offline [geoip-all-in-one](https://github.com/daijro/geoip-all-in-one) MaxMind database (downloaded + cached on first use, ~52 MB; GPL-3.0 data, the same source Camoufox uses) — more accurate than a single online API. The exit IP comes from a small IP echo through the proxy (`api.ipify.org`, then `icanhazip.com`).

- `CLEARCOTE_GEOIP_ECHO_URL`: your own IP-echo endpoint(s), comma-separated, used instead of the public ones, so no third party learns that the proxy is in use. It should answer with the IP as plain text, or JSON with an `ip` field.
- `CLEARCOTE_GEOIP_IPAPI=1`: also ask `ip-api.com` when the database has no answer, e.g. while it is still downloading on first use. Off by default: ip-api.com's free endpoint is for non-commercial use only and allows 45 requests a minute per IP.

### In Docker (`docker: true`)

`geoip: true` works for a Docker launch exactly as for a native one: the SDK resolves the proxy's exit IP before the container is started and hands the container the timezone and Accept-Language it resolved (any `timezone` / `acceptLanguage` you set yourself still wins). Without `geoip` (and no `timezone` / `acceptLanguage` of your own) the container's entrypoint builds the persona the way a native launch does: Accept-Language defaults to `en-US` and the timezone is derived from its primary tag (`en-US` -> `America/New_York`), so every such container is New York, English.

A container has no WebRTC ICE candidates at all unless `webrtcIp` is given. The value to give is the proxy's exit IP, which `geoip: true` fills in as it does for a native launch.

### Humanized input (`humanize`, `showCursor`)

`humanize: true` installs **one consistent human-input standard** across every Playwright surface (`page.click`/`hover`/`dblclick`/`type`/`fill`/`press`/`selectOption`/`check`/`uncheck`/`setChecked`, `page.mouse.*`, `page.keyboard.type`/`press`, and the `Locator` equivalents incl. `dragTo`), dispatched as **native trusted input** (`isTrusted === true`, `navigator.webdriver` stays `false`). Mouse paths start from the *last* cursor position (no snap to the corner) and are walked as a **sum of sub-movements with a min-jerk velocity profile**: an arcing ballistic primary that reaches its top speed before the halfway point and either stops a little short of the target or, in a share of moves fixed per identity (15–38 %), passes it and comes back, then a short corrective move; a held `mouse.down()` stays pressed across the move so `down → move → up` is a real drag, planned as a flat leg within a few pixels of the line from grab to drop (a slider handle stays on its track), with the occasional small overshoot and pull-back. `page.mouse.click(x, y)` is walked by the engine's own pointer path when the engine has one and knows where the pointer is (nothing else moved it since its last walk); `CLEARCOTE_HUMANIZE_ENGINE_CLICKS=off` keeps every click on the SDK's path, `=all` also sends `page.click(selector)` / `locator.click()` there (plain left single clicks on a point the hit test proved). Typing is key-by-key, each key held for the identity's dwell, with a randomized 45–150 ms gap between keys + occasional thinking pauses + word-boundary pauses + the occasional fat-finger correction + **key rollover** (on some fast pairs the next key goes down before the previous one comes up, at a rate fixed per identity; a `fill` over 200 chars glides to the field and clicks into it, then inserts the value in one go). Every press the SDK makes itself (a selector click, the click into a field, `mouse.down()` after a move, a drag's grab, a drop-down or list-box click) comes after a short pause once the pointer has arrived (40–250 ms, right-skewed, typical length fixed per identity). A `page.mouse.click(x, y)` that the engine's pointer path walks is pressed by the engine, not the SDK: engine builds up to r39 press about 10 ms after their last move, with no such pause (`CLEARCOTE_HUMANIZE_ENGINE_CLICKS=off` keeps those clicks on the SDK's path, pause included). Scrolling moves in **whole wheel notches** (100 px on a Windows identity, 120 on Linux, 40 on macOS; the requested distance is rounded to the nearest notch, at least one), because a browser reports every wheel event as one full notch, in short flicks with the occasional reading pause, and moves the pointer over the content first when it isn't already there (a wheel event carries the cursor position). Click options are honoured (`button`, `clickCount`, `delay` as the hold — `0`, Playwright's default, gets the identity's hold —, `modifiers` held around the press (down 80–360 ms before it, up 20–80 ms after), `position`, `force`; `trial` performs no action: the SDK runs Playwright's checks itself — visible, enabled, stable, the click point receives the press — and rejects with Playwright's `TimeoutError` when they never pass, without moving the pointer or pressing, which Playwright's own trial does). Humanized clicks run no Playwright `trial` first: whether the click point is covered comes from the browser's own hit test, read in an isolated world, so the pointer never jumps onto the target and back. A layer with `pointer-events: none` over the target is skipped, as a real press skips it, and a click point inside a child frame is checked against the `<iframe>` element that holds it. `fill` follows the field: text is typed (cleared with held keys only when it has a value, then checked; a value a formatting mask only re-punctuates counts as typed, and a mask that then stops taking keys (an `MM/YY` expiry, a dashed phone number) is typed once, never wiped and retyped; a `maxlength` field gets only the characters it can hold, as with Playwright's own `fill`), a range slider is dragged or clicked and then nudged with the arrow keys to the exact value (a value off the step or out of range raises Playwright's "Malformed value", as without humanize), and a date / time / month / week / datetime-local field is typed segment by segment in the order the browser shows them, with the same key timing as text; colour and non-text inputs keep Playwright's behaviour. `type`/`press` click into a text field first; keys for a button, a link or a hidden field go through Playwright's own focus, so nothing is activated twice. A `<select>` drop-down is opened with a click, its option is found by typing the first letters of its label (spaces included, so `Item 371` of 400 is typed, not arrowed to; a list with nothing to type is crossed by holding the arrow key) and committed with Enter, so the browser fires one trusted `change`; a list box is clicked row by row, scrolled with the wheel until the row shows, with Ctrl (Cmd on a Mac engine) held for the extra rows of a multi-select. Typos are never made in password, e-mail, number, phone, URL or one-time-code fields, nor in a field inside an iframe, and only what actually landed is deleted; `CLEARCOTE_HUMANIZE_TYPO_RATE` sets the per-key rate (default `0.02`, `0` turns typos off). Waits use the caller's `timeout` (or the page default) and a `TimeoutError` is raised after one wait, never a second native one; `force` skips the waits, as in Playwright. Errors are Playwright's own class, worded for the method you called (`page.click: Timeout 800ms exceeded.`, `locator.check: Error: Not a checkbox or radio button`), and `locator.tap()` in a context without `hasTouch` rejects with Playwright's own error instead of clicking. Frame locators and anything humanize cannot verify (a covered field, a value it cannot read back) fall back to Playwright's own action. Calls on one page run one at a time, so a `Promise.all` of fills cannot mix their keys. `showCursor: true` injects a cursor dot for headed runs. Both default off.

**Press and hold** (`pressAndHold`) is one long press for controls that act only while the button stays down: hold-to-confirm buttons, long-press menus, press-and-hold verification buttons.

```ts
import { pressAndHold } from "clearcote";

await page.pressAndHold("#hold", { duration: 3000 });          // humanize: true; hold for 3 s
await page.pressAndHold("#hold", { until: async () => (await page.locator("#hold[data-state=done]").count()) > 0 });
await page.frameLocator("iframe").locator("button").pressAndHold();   // frames work too

await pressAndHold(page.locator("#hold"), { duration: 2500, position: { x: 40, y: 20 } }); // any page, humanize on or off
```

It waits for the element and scrolls it into view, glides to a point inside it, pauses, presses and holds: `duration` ms (a fresh 2.8–3.8 s when not given), or until `until()` returns true (asked every 100 ms, may be async; `duration` is then the longest it may last, 15 s by default) — and lets go 160–350 ms after that, as a hand reacts. While the button is down the pointer drifts by a pixel now and then, never more than 2 px from the press, every move reporting the button held (`tremor: false` keeps it still). It resolves `true`, or `false` when `until` never became true, and the button is always released, also when `until` throws. Options: `position`, `button` (`"left"` / `"right"` / `"middle"`), `timeout` (for the element to show); a bad `duration` or `button` throws. `page.pressAndHold` / `locator.pressAndHold` exist once a page has `humanize: true` (they are not in Playwright's types: cast, or call the exported `pressAndHold(locator, opts)`), and run one at a time with the page's other input.

### Render-backend coherence check (`checkRenderCoherence`)

A persona can claim a GPU, but if the page is actually painted by a software rasterizer (SwiftShader/llvmpipe — common headless with no GPU) a strict detector can tell:

```ts
import { launch, checkRenderCoherence } from "clearcote";

const br = await launch({ fingerprint: "user-7423" });
const page = await br.newPage(); await page.goto("about:blank");
const verdict = await checkRenderCoherence(page); // { renderer, softwareSuspected, coherent, warnings, ... }
if (!verdict.coherent) console.warn(verdict.warnings);
```

It reads the (unmasked) WebGL vendor/renderer the page sees, flags a software rasterizer (a fatal headless tell — enable the canvas bridge or run headed on a real GPU) and an incoherent vendor/renderer pair. Pass a second arg (`claimedGpu`) to also assert the rendered family.

The renderer *string* alone is not enough: a persona renames the backend, so a SwiftShader fallback stops looking like one. The check therefore also measures limits the string cannot move — `MAX_TEXTURE_SIZE`, the vertex/fragment uniform-vector pair, and an actual 16384-wide texture allocation — and reports them as `maxTextureSize`, `maxVertexUniformVectors`, `maxFragmentUniformVectors` and `canAllocate16kTexture`. A renderer naming a desktop GPU while `MAX_TEXTURE_SIZE` is below 16384 is a spoof over a software rasterizer and comes back `coherent: false`. **Headless on Linux with no GPU hits exactly this** when Chromium renders through SwiftShader (8192): a Windows persona always does, and a Linux persona does on a host without Mesa's EGL (`libegl1`). Run headed under Xvfb, or use the canvas bridge.

On a Linux host the SDK picks the software backend that matches the GPU the persona names: a Linux persona (Mesa/OpenGL) renders through Mesa's own GL — over the X display when one is reachable (headed, or headless with `DISPLAY` pointing at an Xvfb), otherwise over EGL when the host has Mesa's EGL (`libegl1` on Debian/Ubuntu) — and a Windows persona (Direct3D11) renders through SwiftShader, whose limits match Direct3D11. Your own `--use-angle=` / `--use-gl=` in `args` wins.

### Hardened launch defaults

Every `launch()` already, with no extra options: **drops Playwright's `--enable-automation`** (so the engine's `AutomationControlled` feature stays off — pass your own `ignoreDefaultArgs` to override); **disables QUIC/HTTP-3 when a proxy is set** (a SOCKS5/HTTP proxy carries only TCP, so no UDP egresses around it); and prints a one-line **coherence warning** to stderr for incoherent option combos (silence with `quiet: true` or `CLEARCOTE_NO_WARN=1`). That includes a one-time warning when a launch claims Linux on a Windows host: Direct3D caps the WebGL and WebGPU limits there, and a persona can lower a limit but never raise one past the driver's, so a Linux persona reports limits no Linux machine has (claim `platform: "windows"` on Windows, or run Linux personas on Linux or in Docker with `docker: true`). On Linux a headless launch's display is sized to the persona's screen (`--screen-info`; your own wins), so full-screen, pop-ups and moved windows stay inside the screen the page is told about.

### Your own fonts on Linux (`fontDirs`)

On Linux the browser sees only the fonts the SDK points it at: the release's bundle of Windows metric-compatible
lookalikes (Arial→Arimo, Segoe UI→Selawik, Times New Roman→Tinos, …). Fonts installed on the host never reach it,
so nothing on the machine changes what a page measures. Two settings add directories (`launch()`, persistent
contexts and `serve()`):

- `fontDirs: [...]`, or `CLEARCOTE_FONT_DIRS` (directories separated by `:`): your own fonts, typically a copy of a
  Windows machine's `C:\Windows\Fonts`, listed ahead of the bundle. On an engine that lets a genuine face win over
  its lookalike (the licensed build from r32), every family they provide renders as itself, and the names that
  stand for it on Windows follow: Helvetica, Times and Courier, and the CSS generics (`sans-serif` → Arial, `serif` →
  Times New Roman, `monospace` → Consolas, `system-ui` → Segoe UI). An older engine keeps its lookalikes for the
  families a persona lists and uses the directories only for characters nothing else covers. A path that is not a
  directory throws before the licence lease.
- `CLEARCOTE_FALLBACK_FONT_DIRS`: fonts used only for characters nothing else covers (CJK, Indic scripts, emoji,
  …), listed after the bundle; they take no family over. The Docker image sets it to the script fonts it ships.
  A persona hides a family's name from CSS, but the browser can still draw characters that only that font covers,
  so put only fonts in these directories that you are happy for a page to see.

The font configuration is written next to the browser binary. When that directory is not writable (a binary
unpacked by another user, a read-only image layer), it goes to `CLEARCOTE_CACHE/fontconfig` or
`~/.cache/clearcote/fontconfig` instead, then to a private temp directory; if none can be written, the SDK warns
once, because the browser would then see this machine's own fonts.

```js
import { launch } from "clearcote";

// a copy of a Windows machine's C:\Windows\Fonts on the Linux host
const browser = await launch({ fingerprint: "acct-1", platform: "windows", fontDirs: ["/srv/windows-fonts"] });
```

`npx clearcote info` reports which of 29 writing systems the fonts a launch would see can draw, your font directories,
and which of the key Windows families they provide for real. Never point either setting at all of
`/usr/share/fonts`: the host's Latin families then compete with the bundle and change the widths a Windows persona
reports. `--disable-genuine-font-faces` in `args` (r32+) keeps the lookalikes even with your fonts listed, and the
SDK then leaves its font rules alone. Ignored on Windows and macOS, which use their own fonts; refused for cloud and
Docker launches (in your own container, set `CLEARCOTE_FONT_DIRS` there). Only use fonts you are licensed to use.

### Persistent profile

```ts
import { launchPersistentContext } from "clearcote";

const context = await launchPersistentContext("./profile-7423", {
  fingerprint: "user-7423",
  platform: "windows",
});
```

### Widevine / DRM (`widevine`)

clearcote ships the **EME/Widevine plumbing** compiled in, but — being 100% open source — it does
**not** bundle Google's proprietary CDM. The SDK fetches that CDM from Google's own component server
(same as a real Chrome receives it), caches it, and enables it — so
`navigator.requestMediaKeySystemAccess('com.widevine.alpha')` resolves and DRM streams play, instead of
EME being a "no-Widevine" tell.

**On by default for PRO launches (since 0.48.0).** A licensed launch (a licence key set: the PRO build,
which claims the Google Chrome brand) on an engine from r32 loads the CDM by default, in `launch()`,
`launchPersistentContext()` and `serve()` alike:

- The first such launch **downloads Google's CDM from Google** (`update.googleapis.com`), checks its
  SHA-256 and caches it under `~/.clearcote/WidevineCdm` (`CLEARCOTE_WIDEVINE_DIR` overrides). Later launches
  use the cache **at once, with no network**; once a day a background check asks Google for a newer CDM
  (used from the next launch on). A failed check counts as that day's check: the cached CDM keeps working.
  The check gives up after 10 s, so it can hold a finishing script at most that long, once a day.
- **Fail-soft**: only a launch with nothing cached waits for the download, at most
  `CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS` (default 15, clamped to 1-600). If it fails — offline, a firewall,
  Google refusing the request — the launch goes on without Widevine and prints one warning per process
  (`quiet: true` or `CLEARCOTE_NO_WARN=1` silences it); the unfinished download is cancelled, and that process
  retries it after 10 minutes.
- **Opt out** with `CLEARCOTE_WIDEVINE=0` (every launch), or `widevine: false`. An explicit `widevine` wins
  over the environment; `widevine: false` is also accepted (and ignored) by `cloud: true` and Docker launches.
- **Free (open) builds stay opt-in**: pass `widevine: true` (or set `CLEARCOTE_WIDEVINE=1`). On a
  persistent context that also seeds the CDM into the profile, so an engine before r32 loads it too.

```ts
import { launchPersistentContext } from "clearcote";

const ctx = await launchPersistentContext("./profile-drm", { widevine: true }); // free build: fetch + seed + enable
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto("https://example.com");
const ok = await page.evaluate(async () => {
  const a = await navigator.requestMediaKeySystemAccess("com.widevine.alpha", [
    { initDataTypes: ["cenc"], videoCapabilities: [
      { contentType: 'video/mp4;codecs="avc1.42E01E"', robustness: "SW_SECURE_DECODE" }] },
  ]);
  await a.createMediaKeys();
  return true;
});
console.log("Widevine:", ok); // true
await ctx.close();
```

- On an engine from r32 the SDK passes `--widevine-cdm-path`, pointing at the cached CDM, so the browser
  registers it at startup in every profile — incognito (`ephemeralProfile: false`) included — without waiting
  for the component updater. Older engines need `widevine: true` on a **persistent** context (the
  seeded-profile route). Your own `--widevine-cdm-path=<dir>` in `args` is left as it is.
- Fetch the CDM ahead of time (e.g. in a Docker build step) with `fetchWidevine()`.
- The clearcote package never distributes Google's CDM: it is downloaded from Google, on your machine.
- Cloud launches (`cloud: true`) are unchanged: hosted browsers load Widevine on the server.
- Software-secure (L3) playback. Hardware-secure (L1) paths are out of scope.

### AI agent (OpenRouter)

Drive a page with an **in-browser AI agent** — it perceives the live page, asks an LLM what to do,
and executes the steps as real, trusted input through Chrome's Actor framework. Defaults to
[OpenRouter](https://openrouter.ai); switch models with a single slug.

```ts
import { launchAgent, runAgentTask } from "clearcote";

const ctx = await launchAgent({
  agentLlmKey: process.env.OPENROUTER_API_KEY,   // turns the agent on
  agentModel: "openai/gpt-4o-mini",              // any provider/model slug
});
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto("https://example.com");

const result = await runAgentTask(page, "Click the 'More information...' link.", { maxSteps: 8 });
console.log(result.success, result.finalText, result.steps);
await ctx.close();
```

- `agentLlmKey` is all you need — the engine auto-enables Chrome's Actor framework (no extra flags).
- `agentLlmUrl` points at any OpenAI-compatible endpoint (default OpenRouter); `agentToolMode` is `"tools"` (function-calling) or `"json"`.
- Override the model per task: `runAgentTask(page, goal, { model: "anthropic/claude-3.5-sonnet" })`.
- The agent needs a **regular profile** — use `launchAgent` / `launchPersistentContext`, not the incognito `launch()`.
- Without `userDataDir`, `launchAgent` uses a temp profile and deletes it when the context closes; pass `userDataDir` to keep logins between runs.

### Capture or import a profile

Make Clearcote present a **real machine's** identity instead of the synthetic seed-derived one. Pass
a captured fingerprint as `fingerprintProfile` — a file path, a plain object, or a JSON string (the
SDK gzip+base64-encodes it for the engine):

```ts
const browser = await launch({
  fingerprint: "seed-1",                 // still the farbling root / fallback for absent fields
  fingerprintProfile: "profile.json",    // a real machine's captured identity
});
```

Get a profile two ways:

- **Capture** from a donor Chrome — open
  [`tools/fingerprint-collect/collect.html`](../../tools/fingerprint-collect/README.md) and click
  Capture, or paste `collect.js` + `snippet.js` in DevTools. Either downloads the JSON.
- **Convert** the open-source [chrome-fingerprints](https://github.com/Vinyzu/chrome-fingerprints)
  dataset: `pip install chrome-fingerprints && python tools/fingerprint-collect/convert_dataset.py
  --out ./profiles --count 100`.

**Override / fallback semantics:** fields present in the profile **override** the `fingerprint`
seed-derived persona; **absent** fields **fall back** to the seed, so partial profiles stay
coherent. The SDK also derives `acceptLanguage` from the profile's `navigator.languages` when you
don't set `acceptLanguage` explicitly. See the
[collector README](../../tools/fingerprint-collect/README.md) for the full schema and what each
field drives.

## Local or cloud

The same `launch()` runs the browser on this machine or on Clearcote's servers. One flag picks which,
and both resolve to the same Playwright `Browser`:

```ts
import { launch } from "clearcote";

const browser = await launch({ cloud: true, country: "us", identity: "acct-1" });
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();               // disconnects and ends the hosted session
```

Leave `cloud` unset and set `CLEARCOTE_CLOUD=1` to move existing code to the cloud without editing
it. The API key comes from `apiKey` or `CLEARCOTE_API_KEY`, and `CLEARCOTE_API_URL` points the SDK at
another server (https only: plain `http://` is accepted for `127.0.0.1`, `::1` and `localhost` alone,
so the key never crosses a network unencrypted).

What changes in the cloud:

- **Options** keep the API's names: `fingerprint`, `identity`, `platform`, `brand`, `timezone`,
  `locale` (or `acceptLanguage`), `geoip`, `headless`, `lightStealth`, `proxy` (`"managed"`, a URL, or
  `{ server, username, password }`), `country`/`state`/`city`, `proxySession`, `timeoutSec`,
  `idleTimeoutSec`, `maxGb`, `version`, `profile`, `url`, `adblock`, `solveSliders`, `solveCheckboxes`,
  `solvePressHold`, `challengeService`, `keepAlive`, `record`, `note`, `worker`. `humanize` and `showCursor` run in the SDK, exactly as for a local browser.
  `timeout` and `slowMo` go to the CDP connect, where `timeout` defaults to 120 s (the browser starts as you connect).
- **Input is humanized by default.** A cloud launch types, clicks and scrolls the way `humanize: true`
  does locally, and switches the hosted browser's own mouse humanization off so the two never fight
  over the cursor. `humanize: false` turns humanizing off altogether.
- **Slider challenges are solved for you.** A cloud browser drags slide-to-verify challenges (a handle
  to the end of a bar, or a puzzle piece into its gap) by itself, in any tab or frame. Pass
  `solveSliders: false` when your script handles them.
- **Human-check checkboxes are clicked for you.** A separate option: a cloud browser clicks "verify you
  are human" boxes by itself, in any tab or frame. Pass `solveCheckboxes: false` when your script handles
  them.
- **Press-and-hold buttons are held for you.** A third option: a cloud browser presses and holds a
  "press and hold" verification button by itself, in any tab or frame, until it reports done. Pass
  `solvePressHold: false` when your script handles them (for example with `pressAndHold`). Local
  browsers never do this on their own.
- **Harder challenges can go to a solving service (off by default).** `challengeService: true`, or
  `{ categories, sites, key, apiKey, mode, maxSolves, maxSpendEur }`: what the free actions cannot clear is
  solved with your own solving-service key (`key: "own"`, stored in the dashboard or passed as `apiKey`) or
  ours (`key: "managed"`, billed per solve). `mode: "report"` only reports what the pages show. See the
  [hosted browsers docs](https://www.clearcotelabs.com/docs/hosted-browsers#challenge-service).
- **Local-only options are refused, by name.** `executablePath`, `args`, `userDataDir`, `extensions`,
  `ignoreDefaultArgs`, the finer persona switches (`gpuVendor`, `webrtcIp`, ...) and the licence
  options throw `<name> is not available for cloud browsers` before anything starts.
- **Profiles are cloud profiles.** `profile: "acct-1"` loads the cookies of a named cloud profile, and
  `launchPersistentContext({ cloud: true, profile: "acct-1" })` resolves to a context that also saves
  them back when it closes. Fill one from a browser you are logged in to with `profiles.sync` (below).
- **The session.** `cloudSessionOf(browser)` (also `browser.cloudSession`) holds its `id`, `worker` and
  `expiresAt`. `close()` disconnects and ends the session; a `keepAlive: true` session is left running
  (stop it with `cloud.browsers.stop(id)`).

### The Cloud client

Everything else the hosted API does is on `Cloud`:

```ts
import { Cloud } from "clearcote";

const cloud = new Cloud();                        // CLEARCOTE_API_KEY

// An agent run: a task in, JSON out
const run = await cloud.runs.create("Log in with {{password}} and read the current plan's price", {
  url: "https://example.com/login",
  schema: { type: "object", properties: { price: { type: "string" } } },
  secrets: { password: { value: "s3cret", domains: ["example.com"] } },   // the model never sees it
});
console.log(run.status, run.result.output, run.costEur.total);

// Copy a logged-in state into a cloud profile: only the cookies a browser would use on the domains you
// name (theirs, their subdomains', and parent-domain ones such as .example.com for www.example.com)
await cloud.profiles.sync("acct-1", { loginUrl: "https://example.com/login", domains: ["example.com"] });

// Recordings, the event timeline, hand-off to a person
const s = await cloud.browsers.create({ record: true });
await cloud.browsers.events(s.id);                // { events: [...], next }
await cloud.browsers.downloadRecording(s.id, "session.mp4");
```

- `browsers`: `create(options)`, `get(id)`, `list({ status, note, limit, before })`, `stop(id)`,
  `live(id, { control })`, `share(id, { control, minutes, recording })`,
  `handoff(id, { reason, timeoutSec })`, `handoffDone(id)`, `waitHandoff(id, { timeout, poll })`,
  `events(id, { after, limit })`, `recordingUrl(id)`, `downloadRecording(id, path)`.
  `waitHandoff` on a session with no hand-off rejects with `CloudError` (code `NO_HANDOFF`).
- `runs`: `create(task, { url, schema, secrets, wait = true, timeout, poll = 1.5, onUpdate, ...options })`
  (the browser options above plus `maxSteps`, `handoff`, `handoffTimeoutSec`), `get(id)`,
  `list({ limit, before })`, `cancel(id)`, `wait(id, { timeout, poll, onUpdate })`. A run that pauses
  for a person (`waiting_for_human`) is not finished: `onUpdate` hears about it, and without one the
  live link is printed to stderr. `timeout` and `poll` are in seconds, as in the Python SDK.
- `profiles`: `list()`, `get(name)`, `delete(name)`, `importCookies(name, cookies, { mode })`,
  `sync(name, { fromProfile | fromCdp | fromFile | loginUrl, domains | allDomains, replace })`. `sync`
  refuses to run without `domains` or `allDomains: true`.
- `webhooks`: `create(url, { events, description })` (the answer carries the `secret`, shown once),
  `list()`, `delete(id)`, `test(id)`

Every method resolves to the parsed JSON of its endpoint. An API refusal rejects with `CloudError`
(`status`, `code` such as `"PROFILE_IN_USE"`, and the server's own `message`); a wait that runs out
rejects with `CloudTimeoutError`, whose `last` holds the last view (the run carries on on the server).

Check a webhook delivery with the raw request body and the `Clearcote-Signature` header:

```ts
import { verifyWebhook } from "clearcote";

const event = verifyWebhook(rawBody, req.headers["clearcote-signature"], "whsec_..."); // throws if forged or stale
```

The same from the command line (`--json` for machine-readable output):

```bash
npx clearcote cloud run "Read the price of the Pro plan" --url https://example.com/pricing --schema price.json
npx clearcote cloud run "Open my account page" --country us --profile acct-1 --persist-profile --max-steps 20
npx clearcote cloud sessions
npx clearcote cloud events bs_123
npx clearcote cloud recording bs_123 -o session.mp4
npx clearcote cloud profile sync acct-1 --login https://example.com/login --domain example.com
npx clearcote cloud webhooks add https://hooks.example.com/clearcote --event run.finished
```

## Fingerprint options

All optional. Anything not listed here is passed straight through to Playwright
(`headless`, `proxy`, `args`, `timeout`, `slowMo`, …).

| Option | Switch | Meaning |
|---|---|---|
| `fingerprint` | `--fingerprint` | Master seed (per-eTLD+1 farbling root). String or number. |
| `platform` | `--fingerprint-platform` | `windows` \| `linux` \| `macos` \| `android` (best-effort mobile). |
| `platformVersion` | `--fingerprint-platform-version` | UA-CH platform version. |
| `brand` | `--fingerprint-brand` | `Chrome` \| `Edge` \| `Opera` \| `Vivaldi`. |
| `brandVersion` | `--fingerprint-brand-version` | Brand version. |
| `gpuVendor` | `--fingerprint-gpu-vendor` | WebGL UNMASKED vendor. |
| `gpuRenderer` | `--fingerprint-gpu-renderer` | WebGL UNMASKED renderer. |
| `hardwareConcurrency` | `--fingerprint-hardware-concurrency` | `navigator.hardwareConcurrency`. |
| `location` | `--fingerprint-location` | `"lat,lng"` (only when geo permission is granted). |
| `timezone` | `--timezone` | IANA timezone, e.g. `America/New_York`. |
| `acceptLanguage` | `--accept-lang` | `navigator.languages` + `Accept-Language` header, e.g. `en-US,en`. |
| `webrtcIp` | `--webrtc-ip` | WebRTC IP to report. The engine **fabricates** the `srflx` candidate at this IP and sends **no real STUN** from the host, so the real IP never leaks (not merely relabeled). |
| `disableGpuFingerprint` | `--disable-gpu-fingerprint` | Turn off GPU/WebGL spoofing. |
| `geoip` | _(directive)_ | `true` → resolve the proxy's exit-IP geo and auto-fill timezone/acceptLanguage/location/**webrtcIp**. |
| `fingerprintProfile` | `--fingerprint-profile` | A real machine's captured fingerprint (file path / object / JSON string; the SDK gzip+base64-encodes it). Fields present override the seed-derived persona; absent fields fall back to `fingerprint`. |
| `canvasBridge` | `--canvas-bridge-*` | Forward canvas/WebGL readbacks to a remote real-GPU host so the pixels a page hashes match the GPU your persona claims. `{ url, auth?, mode?, allow?, deny?, fallback? }`; setting `url` auto-adds `--no-sandbox`. See [docs/CANVAS-BRIDGE.md](../../docs/CANVAS-BRIDGE.md). |
| `extensions` | `--load-extension` + `--disable-extensions-except` | Unpacked-extension directory paths to load (Chromium forces headed when extensions are present). |

> **Headed launches** default to `viewport: null` (no emulated viewport) so `window.innerWidth` tracks the real OS window — an emulated `1280×720` on a real window is an impossible-window tell. Pass an explicit `viewport` to override.

> **Headless launches** (0.24.0+) get a coherent window geometry by default, so `screen`, `availWidth/Height`,
> `innerWidth/Height` and `outerWidth/Height` agree with each other the way a real window's do. With a
> `fingerprint` seed the engine's own screen and work area are used and the window is sized to them; without a
> seed the SDK sets the headless display to a screen size drawn from real captured desktops (with a
> taskbar on Windows) and sizes the window to its work area the same way. It is applied at launch,
> before your first navigation. Pass an explicit `viewport` or `screen` to opt out entirely. On an engine with
> persona windows (below) a seeded launch is handled by the engine instead.

### Window geometry: the persona's own window

On an engine with persona window support (licensed builds from r39), a `fingerprint` seed decides the
browser window too, not only the screen. The engine derives the window that machine has open on its
screen — maximized for most seeds, a restored window of a seed-derived size and position for the rest,
always inside the work area — and creates the **real** window with those bounds and that state before the
first page exists. In headless mode it also makes the headless display the persona's display (screen size
plus taskbar). `outerWidth`/`outerHeight`/`screenX`/`screenY` are then the real window's, so they agree with
`innerWidth`/`innerHeight`, `visualViewport`, screenshots, mouse-event coordinates, pop-up placement and
`device-width` media queries without any of them being patched. The same seed opens the same window in
every process and on every launch path (`launch()`, `launchPersistentContext()`, `serve()` /
`clearcote serve`, raw CLI and CDP, Docker); different seeds open different windows. The SDK then sends no
display switch and runs no window fit for that launch (older engines keep the fit). A seedless launch has
no persona and keeps the behaviour described above.

Where several things decide the window, the first one that applies wins:

1. **Your explicit configuration.** `viewport` (including `viewport: null`) / `screen`, `serve({ windowSize })`,
   and the switches `--window-size`, `--window-position`, `--start-maximized`. An explicit `viewport` is
   honoured as given: the SDK makes the real window the viewport plus the browser's own frame, at the
   persona's position, moved back onto the work area if it would hang over an edge, so outer and inner sizes
   still agree.
2. **The placement this identity saved in its profile.** A `userDataDir` reused with the same seed restores
   the window it closed with, as Chrome does. A profile last used with another seed (or by an older engine)
   gets the new seed's window instead.
3. **The persona's window**, derived from the seed and its final screen and work area (an explicit
   `screenWidth`/`availWidth`/... included).
4. **No persona** (no seed, or `lightStealth`): the SDK's display and a window maximized onto its work area.

`args: ["--fingerprint-window=off"]` keeps a seeded launch on the previous behaviour. A pinned persona
schema (`--fingerprint-schema=1`..`3`) keeps its previous window too, unless you add
`--fingerprint-window=on`.

>
> **Proxies:** a SOCKS5 proxy with credentials — written in the URL (`socks5://user:pass@host:port`) or passed as `username`/`password` — is routed via `--proxy-server` (Playwright rejects credentials in its SOCKS descriptor), and the credentials are passed to the engine, which implements SOCKS5 username/password authentication (RFC 1929). Stock Chromium does not, so no local relay is needed.

## Personas & Client Hints coherence

A persona's Client Hints are coherent across **JavaScript and HTTP by construction** — clearcote rewrites a single `blink::UserAgentMetadata` (brand list, full-version list, platform, mobile, arch/bitness) that feeds *both* the browser path that attaches the `Sec-CH-UA*` request headers **and** the renderer path that builds `navigator.userAgentData`. There aren't two sides to keep in sync; they read the same source. So `getHighEntropyValues(['fullVersionList', …])` in JS matches `Sec-CH-UA-Full-Version-List` on the wire, `userAgentData.platform` matches `Sec-CH-UA-Platform`, and so on.

**Chrome — the default and the recommendation** (most coherent: clearcote *is* Chromium, so the claim matches the engine's real behavior):

```ts
const browser = await launch({ fingerprint: "p1" });  // brand defaults to Chrome; platform defaults to the host OS
// HTTP:  sec-ch-ua: "Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="…"
//        sec-ch-ua-mobile: ?0    sec-ch-ua-platform: "Windows"
// JS:    navigator.userAgentData.brands == that same list; mobile=false; platform="Windows"
```

**Edge** — a coherent Edge string surface (UA + UA-CH), for targets that specifically expect the Edge brand:

```ts
const browser = await launch({ fingerprint: "p1", brand: "Edge" });
// UA:    …Chrome/149.0.0.0 Safari/537.36 Edg/149.0.0.0
// HTTP:  sec-ch-ua: "Microsoft Edge";v="149", "Chromium";v="149", "Not)A;Brand";v="…"
//        sec-ch-ua-full-version-list: "Microsoft Edge";v="149.0.3650.65", "Chromium";v="149.0.7827.114", …
// JS:    userAgentData.brands include "Microsoft Edge"; getHighEntropyValues(['fullVersionList'])
//        carries that same distinct Edge build — JS and HTTP identical, from one metadata.
```

`brand` (`"Chrome"` | `"Edge"` | `"Opera"` | `"Vivaldi"`) is a **string-level** persona: it changes the UA + UA-CH brand, but the engine still behaves like Chromium. Chrome is the most coherent default (nothing to contradict); reach for `brand: "Edge"` only when a target expects it. If you also set `brandVersion` to an older major, the network `tlsProfile` (default `match-persona`) shifts the TLS shape to that major while the JS engine stays at the build version — so **Chrome ≈ the build version, everything aligned** is the strongest persona.

**Android** — a best-effort mobile persona (seed-selected Pixel/Galaxy):

```ts
const browser = await launch({ fingerprint: "p1", platform: "android" });  // auto-sets a phone window-size
// UA:    …(Linux; Android 10; K) … Chrome/149.0.0.0 Mobile Safari/537.36
// JS/HTTP: sec-ch-ua-mobile: ?1, platform="Android", model + platformVersion; maxTouchPoints=5,
//          pointer:coarse / hover:none, mobile screen + DPR, Mali/Adreno WebGL, plugins=0.
```

Android is **best-effort on a desktop engine**: the JS/header surface is coherent, but the GPU *render* and fine page geometry (`innerWidth` floors at ~500px) stay desktop — documented residual tells. Pair with `canvasBridge` for render coherence.

## Saved profiles (`Profile`)

A `Profile` bundles a persona (seed, GPU, brand, …) **and** its `canvasBridge` config under one
name you can persist and re-launch — so the claimed GPU, the bridge endpoint, and the bridge's
GPU-keyed cache stay coherent because they travel together.

```ts
import { Profile, launch } from "clearcote";

// save once
await new Profile("acct-1", {
  fingerprint: "acct-1",
  gpuVendor: "Google Inc. (Intel)",
  gpuRenderer: "ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
  canvasBridge: { url: "ws://127.0.0.1:9099", auth: "user:secret" },
}).save();

// re-launch anywhere (explicit options override the saved ones)
const browser = await Profile.load("acct-1").launch({ headless: false });
// equivalently: await launch({ profile: "acct-1" });
```

Profiles are JSON at `~/.clearcote/profiles/<name>.json` (set `CLEARCOTE_PROFILE_DIR` to relocate).

## API

- `launch(options?)` → `Promise<Browser>` — launch and get a Playwright `Browser`. Pass `profile` (a name, path, or `Profile`) to launch a saved persona; `cloud: true` for a hosted browser ([Local or cloud](#local-or-cloud)).
- `launchPersistentContext(userDataDir, options?)` → `Promise<BrowserContext>`; `launchPersistentContext({ cloud: true, profile: "name" })` for a cloud profile.
- `new Cloud({ apiKey, baseUrl })` — the hosted API (`browsers`, `runs`, `profiles`, `webhooks`); `CloudError`, `CloudTimeoutError`, `verifyWebhook(rawBody, signatureHeader, secret, toleranceSec = 300)`, `cloudSessionOf(browser)`.
- `serve(options?)` → `Promise<Server>` — a standing, stealthy CDP endpoint (`.cdpUrl` / `.wsUrl()` / `.close()`) any Playwright/Puppeteer/CDP client attaches to via `connectOverCDP`.
- `executablePath(options?)` → `Promise<string>` — resolve (download/verify if needed) the chrome.exe path, e.g. for raw `chromium.launch({ executablePath })`.
- `download(options?)` → `Promise<string>` — pre-fetch + verify the binary without launching.
- `Profile` — `new Profile(name, options)`, `.save(path?)`, `Profile.load(name)`, `.launch(overrides?)`, `.launchPersistentContext(dir, overrides?)`.
- `listProfiles()` → `string[]`, `loadProfile(name)` → `Profile`.
- `RELEASE` — the pinned release metadata (tag, version, sha256).

## Binary resolution & verification

`launch()` resolves the browser in this order:

1. `executablePath` option, if given;
2. `CLEARCOTE_BINARY` environment variable, if set;
3. otherwise **download** the pinned release, **verify its SHA-256** (the hash is baked into this
   package — it's the trust anchor), extract to a per-version cache, and verify the extracted
   `chrome.exe` hash too.

Cache location (override with `CLEARCOTE_CACHE`):
- Windows: `%LOCALAPPDATA%\clearcote\Cache\<tag>`
- macOS: `~/Library/Caches/clearcote/<tag>`
- Linux: `${XDG_CACHE_HOME:-~/.cache}/clearcote/<tag>`

Processes that share this cache take turns installing a build: the first one downloads it, the others
wait and then use it, whichever SDK (Python, Node or .NET) each one runs. That needs every process
sharing the cache to run an SDK release with this install lock (any release after 0.40.1); earlier
releases do not wait for it.

A SHA-256 mismatch is a hard error — the SDK refuses to run an unverified binary. You can
independently confirm the published checksums and GPG signatures on the
[release page](https://github.com/clearcotelabs/clearcote-browser/releases).

### Stay on the latest build (`autoUpdate`)

By default the SDK installs the **exact browser build pinned into this package** — reproducible,
and the baked-in SHA-256 is the trust anchor. To follow new browser releases **without upgrading
the package every time**, opt in:

```ts
const browser = await launch({ fingerprint: "seed-123", autoUpdate: true });
```

or set the environment variable globally:

```bash
CLEARCOTE_AUTO_UPDATE=1
```

With `autoUpdate`, the SDK resolves the **newest GitHub release**, downloads its zip, and verifies
it against that release's published `SHA256SUMS.txt`. When a **`gpg`** binary is available it
additionally imports the release's public key, confirms its fingerprint equals the pinned
`CA96F185 F96A693A EDB3AC1F CB00D851 B7A86B0F`, and verifies the signed checksum — so an
auto-resolved build is cryptographically authenticated, not just downloaded. If GitHub is
unreachable it falls back to the pinned release; if the latest release *is* the pinned one, the
audited baked-in hashes are used. Each build is cached per tag, so this only downloads when a new
version actually ships. (For locked-down/reproducible deployments, leave `autoUpdate` off and bump
the package deliberately.)

## Choosing a browser version

By default the SDK downloads the exact build pinned into this package. To pick a specific browser
major/version instead, pass `version` (or set `CLEARCOTE_BROWSER_VERSION`):

```ts
await launch({ fingerprint: "seed-1", version: "150" });     // newest 150.x (free)
await launch({ version: "150.0.7871.114" });                 // an exact build
await launch({ version: "latest" });                         // newest you can access
await launch({ version: "150", licenseKey: "cc_lic_..." });  // a PRO-tier version (see below)
```

The request is resolved against a public catalog (`GET /api/v1/versions`) and **validated to exist
before anything downloads**, so a bad request fails fast instead of getting stuck:

- unknown version → `No Clearcote build matches version '151'. Available: 149.0.7827.114 (free).`
- a PRO-tier version without a key → `Clearcote 150… is a PRO build … set a license key (CLEARCOTE_LICENSE_KEY).`

Free versions download from GitHub (no key needed); PRO versions download via the authenticated route.
Each build is cached per version, so different majors coexist. Binary resolution order:
`executablePath` > `CLEARCOTE_BINARY` > `version` > pinned default. Also on `launchPersistentContext`,
`serve`, `executablePath()`, and `download()`.

## PRO tier (license key)

Everything above is the **free** build. A PRO license adds **floating-concurrency licensing** and
pulls a separate, license-gated browser build. It's opt-in and entirely additive — **with no
license key the SDK is byte-for-byte the free client** (free binary from GitHub, and it never
contacts the license backend).

### What's in each tier

<!-- The rows below mirror site/lib/tiers.ts, which is the single source of truth for the
     free/PRO split. If you change one, change the other (and the sibling SDK README). -->

The identity surface is **free in full**. A licence does not unlock "more spoofing" — every fingerprint
control below is in the open build. The licensed build adds behavioural realism that needs recorded
data or engine work held out of the public tree. **It is free with GitHub for one browser at a time**
(sign in at [clearcotelabs.com](https://clearcotelabs.com/pricing#free), needs SDK 0.29.0+); **PRO**
runs up to 250 browsers at once and adds email support.

| Capability | Open source | Free with GitHub | PRO |
|---|:---:|:---:|:---:|
| Seeded personas (`fingerprint`), per-site farbling | ✅ | ✅ | ✅ |
| Canvas / WebGL / audio / font identity controls | ✅ | ✅ | ✅ |
| All 18 metadata overrides (`screenWidth`, `deviceMemory`, `gpuVendor`, …) | ✅ | ✅ | ✅ |
| `light_stealth` preset | ✅ | ✅ | ✅ |
| TLS ClientHello profile (`tlsProfile`) | ✅ | ✅ | ✅ |
| Proxy + `geoip` locale/timezone coherence | ✅ | ✅ | ✅ |
| Humanized input (`humanize`) — synthetic bézier paths | ✅ | ✅ | ✅ |
| **Humanized input — real recorded human trajectories** | — | ✅ | ✅ |
| **Coalesced pointer samples** (`getCoalescedEvents` realism) | — | ✅ | ✅ |
| **Coherent WebRTC srflx fabrication** (`webrtcIp`) | — | ✅ | ✅ |
| **WebRTC host-candidate concealment** (`.local` names) | — | ✅ | ✅ |
| **Request-header hygiene** on revalidation | — | ✅ | ✅ |
| Floating-concurrency licensing + run-token gate | — | 1 browser | ✅ up to 250 |
| Email support from the owner | — | — | ✅ |

The mouse tier is decided **at runtime** from a signed claim in the run-token — same SDK call,
same `humanize: true`. With a valid PRO lease the motion comes from recorded human trajectories;
without one it falls back to the synthetic bézier path. Nothing in your code changes.

Pass a `licenseKey` (or set `CLEARCOTE_LICENSE_KEY`, or drop it in `~/.clearcote/license.key`):

```ts
const browser = await launch({ fingerprint: "seed-123", licenseKey: "cc_lic_..." });
```

When a key is present the SDK:

1. **downloads the PRO binary** via the site's authenticated `GET /api/v1/download/pro` route
   (short-lived signed URL), verified against its SHA-256 exactly like the free pin, then cached;
2. **checks out one concurrency slot for each browser** from the licence lease server
   (`lease.clearcotelabs.com`, with `www.clearcotelabs.com` as its fallback) — a background heartbeat
   keeps it alive and rotates the run-token, and the slot is released when the browser closes. A
   paid licence still launches on its last run-token while the lease server cannot be reached.

The PRO engine refuses to launch without a valid run-token, so a copied binary alone won't run.
Resolution order for the binary is **`executablePath` → `CLEARCOTE_BINARY` → PRO (licensed) →
free** — an explicit binary always wins. A revoked/expired key raises (`ConcurrencyLimitError` /
`LicenseRevokedError` / `LicenseError`); it never silently downgrades to the free binary. Override
the backend with `licenseApiBase` or `CLEARCOTE_LICENSE_API` (every licence call), or only the lease
calls with `CLEARCOTE_LEASE_API`.

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
