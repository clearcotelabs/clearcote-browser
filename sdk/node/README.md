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
  webrtcIp: "203.0.113.10",        // make WebRTC report the proxy egress IP
});
```

### Auto geo-match (`geoip`)

Set `geoip: true` and Clearcote resolves the **proxy's exit IP** (looked up *through* the proxy) and auto-fills any unset `timezone`, `acceptLanguage`, and `location` so they match the proxy's region — no manual timezone/locale bookkeeping:

```ts
const browser = await launch({
  fingerprint: "user-7423",
  proxy: { server: "http://host:8080", username: "u", password: "p" },
  geoip: true,              // timezone + navigator.languages/Accept-Language auto-set from the proxy's geo
});
```

Anything you set explicitly wins over `geoip`. With no proxy it uses your direct connection's IP. The lookup needs an **http(s) proxy** — SOCKS proxies are skipped (set `timezone`/`acceptLanguage` yourself).

### Persistent profile

```ts
import { launchPersistentContext } from "clearcote";

const context = await launchPersistentContext("./profile-7423", {
  fingerprint: "user-7423",
  platform: "windows",
});
```

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
| `webrtcIp` | `--webrtc-ip` | WebRTC egress IP to report (your proxy IP). |
| `disableGpuFingerprint` | `--disable-gpu-fingerprint` | Turn off GPU/WebGL spoofing. |
| `geoip` | _(directive)_ | `true` → resolve the proxy's exit-IP geo and auto-fill timezone/acceptLanguage/location. |

## API

- `launch(options?)` → `Promise<Browser>` — launch and get a Playwright `Browser`.
- `launchPersistentContext(userDataDir, options?)` → `Promise<BrowserContext>`.
- `executablePath(options?)` → `Promise<string>` — resolve (download/verify if needed) the chrome.exe path, e.g. for raw `chromium.launch({ executablePath })`.
- `download(options?)` → `Promise<string>` — pre-fetch + verify the binary without launching.
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

## License

BSD-3-Clause. See [LICENSE](../../LICENSE).
