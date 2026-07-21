# clearcote (Node / TypeScript SDK)

A **Playwright drop-in** for [Clearcote](https://github.com/clearcotelabs/clearcote-browser) — the
open, reproducible, anti-fingerprint Chromium build. `launch()` returns a standard Playwright
`Browser`, so migrating is a one-line import change.

The verified Clearcote binary is **auto-downloaded and SHA-256 checked** on first use, then cached —
you don't manage zips or paths.

> **Platform:** Clearcote ships **Windows x64** and **Linux x64** binaries; `launch()` runs on both
> and the SDK auto-downloads the right one for your OS. On Linux the persona is Linux-native (Linux
> GPU/voices/audio-device values) and DRM uses the Linux CDM. macOS is on the
> [roadmap](../../ROADMAP.md). On a minimal Linux host, install `xz-utils` (the Node SDK unpacks the
> `.tar.xz` with the system `tar`) plus the browser's runtime libs (e.g.
> `apt-get install -y xz-utils libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2
> libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libpango-1.0-0
> libcairo2 libx11-6 libxcb1 libexpat1 libdbus-1-3`) and pass `args: ["--no-sandbox"]` (or
> `chown root:root chrome-sandbox && chmod 4755 chrome-sandbox`) in containers.

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
official Docker image also works: `docker run -d --rm -p 9222:9222 teamflatearth/clearcote`.)

**Drive it from an AI agent (MCP).** Point Claude Desktop / Cursor / Cline at the
[`clearcote-mcp`](https://github.com/clearcotelabs/clearcote-browser/tree/main/mcp) server
(`npx -y clearcote-mcp` or `pip install clearcote-mcp`) — ~20 tools over one shared stealth browser.

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

Anything you set explicitly wins over `geoip`. With no proxy it uses your direct connection's IP. The lookup needs an **http(s) proxy** — SOCKS proxies are skipped (set `timezone`/`acceptLanguage` yourself).

Geo data comes from the offline [geoip-all-in-one](https://github.com/daijro/geoip-all-in-one) MaxMind database (downloaded + cached on first use; GPL-3.0 data, the same source Camoufox uses) — more accurate than a single online API — with `ip-api.com` as a fallback.

### Humanized input (`humanize`, `showCursor`)

`humanize: true` installs **one consistent human-input standard** across every Playwright surface (`page.click`/`hover`/`dblclick`/`type`/`fill`/`press`, `page.mouse.*`, `page.keyboard.type`, and the `Locator` equivalents incl. `dragTo`), dispatched as **native trusted input** (`isTrusted === true`, `navigator.webdriver` stays `false`). Mouse paths are slightly bowed cubic-beziers from the *last* cursor position (no snap to the corner), walked as a **sum of sub-movements with a min-jerk velocity profile** (a ballistic primary + corrective move — multi-peak velocity, not one symmetric bell); a held `mouse.down()` stays pressed across the move so `down → move → up` is a real drag. Typing is key-by-key with **gaussian inter-key timing** + word-boundary pauses + the occasional fat-finger correction (bulk values over 200 chars stay atomic). Scrolling uses **ease-out inertia** with the occasional reading pause. `showCursor: true` injects a cursor dot for headed runs. Both default off.

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

### Hardened launch defaults

Every `launch()` already, with no extra options: **drops Playwright's `--enable-automation`** (so the engine's `AutomationControlled` feature stays off — pass your own `ignoreDefaultArgs` to override); **disables QUIC/HTTP-3 when a proxy is set** (a SOCKS5/HTTP proxy carries only TCP, so no UDP egresses around it); and prints a one-line **coherence warning** to stderr for incoherent option combos (silence with `quiet: true` or `CLEARCOTE_NO_WARN=1`).

### Persistent profile

```ts
import { launchPersistentContext } from "clearcote";

const context = await launchPersistentContext("./profile-7423", {
  fingerprint: "user-7423",
  platform: "windows",
});
```

### Widevine / DRM (`widevine: true`)

clearcote ships the **EME/Widevine plumbing** compiled in, but — being 100% open source — it does
**not** bundle Google's proprietary CDM. Pass `widevine: true` on a **persistent** context and the SDK
fetches that CDM once from Google's own component server (same as a real Chrome receives it), seeds it
into the profile, and enables it — so `navigator.requestMediaKeySystemAccess('com.widevine.alpha')`
resolves and DRM streams play, instead of EME being a "no-Widevine" tell.

```ts
import { launchPersistentContext } from "clearcote";

const ctx = await launchPersistentContext("./profile-drm", { widevine: true }); // fetch + seed + enable
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

- Requires a **persistent** context (the CDM lives in `userDataDir`) — not the incognito `launch()`.
- The CDM is cached under `~/.clearcote/WidevineCdm`; fetch it ahead of time with `fetchWidevine()`.
- It's **opt-in**: the clearcote package never distributes Google's CDM — *you* trigger the download.
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
>
> **Proxies:** a `socks5://user:pass@host:port` proxy is routed via `--proxy-server` (Playwright rejects credentials in its SOCKS descriptor). Chromium can't authenticate SOCKS5, so the credentials are dropped with a warning — put the auth on a local relay.

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

- `launch(options?)` → `Promise<Browser>` — launch and get a Playwright `Browser`. Pass `profile` (a name, path, or `Profile`) to launch a saved persona.
- `launchPersistentContext(userDataDir, options?)` → `Promise<BrowserContext>`.
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
await launch({ fingerprint: "seed-1", version: "149" });     // newest 149.x (free)
await launch({ version: "149.0.7827.114" });                 // an exact build
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

The identity surface is **free in full**. PRO does not unlock "more spoofing" — every fingerprint
control below is in the free build. What PRO adds is behavioural realism that needs recorded data
or engine work held out of the public tree, plus the licensing itself.

| Capability | Free | PRO |
|---|:---:|:---:|
| Seeded personas (`fingerprint`), per-site farbling | ✅ | ✅ |
| Canvas / WebGL / audio / font identity controls | ✅ | ✅ |
| All 18 metadata overrides (`screenWidth`, `deviceMemory`, `gpuVendor`, …) | ✅ | ✅ |
| `light_stealth` preset | ✅ | ✅ |
| TLS ClientHello profile (`tlsProfile`) | ✅ | ✅ |
| Proxy + `geoip` locale/timezone coherence | ✅ | ✅ |
| Humanized input (`humanize`) — synthetic bézier paths | ✅ | ✅ |
| **Humanized input — real recorded human trajectories** | — | ✅ |
| **Coalesced pointer samples** (`getCoalescedEvents` realism) | — | ✅ |
| **Coherent WebRTC srflx fabrication** (`webrtcIp`) | — | ✅ |
| **WebRTC host-candidate concealment** (`.local` names) | — | ✅ |
| **Request-header hygiene** on revalidation | — | ✅ |
| Floating-concurrency licensing + run-token gate | — | ✅ |

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
2. **checks out one concurrency slot** — a background heartbeat keeps it alive and rotates a
   short-lived run-token, and the slot is released when the browser closes.

The PRO engine refuses to launch without a valid run-token, so a copied binary alone won't run.
Resolution order for the binary is **`executablePath` → `CLEARCOTE_BINARY` → PRO (licensed) →
free** — an explicit binary always wins. A revoked/expired key raises (`ConcurrencyLimitError` /
`LicenseRevokedError` / `LicenseError`); it never silently downgrades to the free binary. Override
the backend with `licenseApiBase` or `CLEARCOTE_LICENSE_API`.

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
