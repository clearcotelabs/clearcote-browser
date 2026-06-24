# clearcote (Node / TypeScript SDK)

A **Playwright drop-in** for [Clearcote](https://github.com/clearcotelabs/clearcote-browser) — the
open, reproducible, anti-fingerprint Chromium build. `launch()` returns a standard Playwright
`Browser`, so migrating is a one-line import change.

The verified Clearcote binary is **auto-downloaded and SHA-256 checked** on first use, then cached —
you don't manage zips or paths.

> **Platform:** Clearcote currently ships a **Windows x64** binary, so `launch()` runs on Windows.
> (The SDK will download + verify the binary on any OS — useful for packaging — but only launches it
> on Windows. Linux/macOS builds are on the [roadmap](../../ROADMAP.md).)

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

### Persistent profile

```ts
import { launchPersistentContext } from "clearcote";

const context = await launchPersistentContext("./profile-7423", {
  fingerprint: "user-7423",
  platform: "windows",
});
```

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
| `platform` | `--fingerprint-platform` | `windows` \| `linux` \| `macos`. |
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

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
