# Clearcote (.NET)

A Playwright drop-in for the [Clearcote](https://github.com/clearcotelabs/clearcote-browser) anti-fingerprint
Chromium build. `Clearcote.LaunchAsync()` returns a standard **Microsoft.Playwright `IBrowser`** backed by
the verified Clearcote binary — auto-downloaded and **SHA-256-checked** on first use, then cached. Every
persona knob maps to the engine's fingerprint switches; a PRO license key pulls the license-gated build and
checks out a floating-concurrency lease.

The .NET SDK mirrors the [npm `clearcote`](https://www.npmjs.com/package/clearcote) and
[PyPI `clearcote`](https://pypi.org/project/clearcote/) SDKs. Windows x64 + Linux x64.

## Install

```bash
dotnet add package Clearcote
```

It builds on `Microsoft.Playwright` — no `playwright install` needed (Clearcote ships its own browser binary,
auto-downloaded on first launch).

## Quick start

```csharp
using Clearcote;

var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions
{
    Fingerprint = "seed-123",   // stable per-seed identity across launches
    Platform    = "windows",    // persona OS (defaults to the host)
    Headless    = false,
});

var page = await browser.NewPageAsync();
await page.GotoAsync("https://abrahamjuliot.github.io/creepjs/");
// ... it's a normal Playwright IBrowser from here ...
await browser.CloseAsync();
```

`--enable-automation` is dropped (so `navigator.webdriver` stays `false`), QUIC/HTTP-3 is disabled when a
proxy is set, the Privacy-Sandbox APIs are turned off, and WebRTC defaults to deny-non-proxied-UDP — all
automatically.

## Through a proxy (report the proxy's IP, not your host's)

```csharp
var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions
{
    Fingerprint = "seed-123",
    Proxy       = new ProxyOptions { Server = "http://host:8080", Username = "u", Password = "p" },
    WebrtcIp    = "203.0.113.10",   // WebRTC reports the proxy egress IP, not your host's
    Timezone    = "America/New_York",
    AcceptLanguage = "en-US,en",
});
```

## PRO tier (license key)

By default you get the **free** build. With a PRO license key the SDK pulls the license-gated browser and
checks out one floating-concurrency slot; the engine gate refuses to launch without the injected run-token.
**With no key it is byte-for-byte the free client** and never contacts the license backend.

```csharp
var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions
{
    Fingerprint = "seed-123",
    LicenseKey  = "cc_lic_...",   // or set CLEARCOTE_LICENSE_KEY, or ~/.clearcote/license.key
});
```

Binary resolution order: **`ExecutablePath` → `CLEARCOTE_BINARY` env → PRO (when licensed) → free**. A
revoked/expired key throws (`ConcurrencyLimitError` / `LicenseRevokedError` / `LicenseError`) — it never
silently downgrades to the free binary. A background heartbeat keeps the slot alive and rotates the token;
the slot is released when the browser closes. Override the backend with `LicenseApiBase` or
`CLEARCOTE_LICENSE_API`.

## Persistent profile

```csharp
var context = await Clearcote.Clearcote.LaunchPersistentContextAsync("./profile-7423", new LaunchOptions
{
    Fingerprint = "acct-1",
    Headless    = false,
});
```

## Local or cloud

The same call can run the browser on Clearcote's hosted servers instead of this machine. `Cloud = true`
(or `CLEARCOTE_CLOUD=1`, which moves existing code without editing it) returns the same Playwright types,
connected over CDP:

```csharp
// API key: ApiKey, or CLEARCOTE_API_KEY (create one in the Clearcote dashboard)
var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Country = "us" });
var page = await browser.NewPageAsync();   // no emulated viewport, as for a local browser
await page.GotoAsync("https://example.com");
await browser.CloseAsync();                // disconnects and ends the hosted session
```

- `LaunchEphemeralProfileAsync` moves to the cloud the same way and returns the session's own context.
- `LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1" })` opens a named
  cloud profile and saves its cookies back when the context closes.
- The persona options a hosted browser also has carry over: `Fingerprint`, `Platform`, `Brand`, `Timezone`,
  `AcceptLanguage`, `LightStealth`, `Geoip`, `Headless`, `Proxy` (`new() { Server = "managed" }` for the
  included residential connection) and `Version`. Cloud-only: `Identity`, `Country`, `State`, `City`,
  `ProxySession`, `TimeoutSec`, `IdleTimeoutSec`, `MaxGb`, `Url`, `Adblock`, `SolveSliders`, `SolveCheckboxes`,
  `ChallengeService` (default off: `true`, or a `CloudChallengeService` with categories, sites, your key or ours,
  report mode and limits), `KeepAlive`, `Record`, `Note` and `Worker`. `SlowMo` and `Timeout` go to the CDP connect, where `Timeout` defaults to
  120 s (the browser starts as you connect).
- An option only a browser on this machine can take (`ExecutablePath`, `Args`, `Extensions`, a user data
  directory, ...) stops the launch with an error naming it, before anything is created.
- `Cloud.SessionOf(browser)` returns the hosted session (id, expiry, ...). `Humanize` works on a cloud
  page as on a local one.

The rest of the hosted API is on `Cloud`:

```csharp
var cloud = new Cloud();   // CLEARCOTE_API_KEY; CLEARCOTE_API_URL points it at another server
var run = await cloud.Runs.CreateAsync("Find the price of the cheapest plan",
    new RunOptions { Url = "https://example.com" });
Console.WriteLine(run?["result"]?["output"]);
```

`Browsers` (sessions, live view, sharing, hand-off to a person, the event timeline, recordings), `Runs` (a
task in, JSON out), `Profiles` (import cookies, or sync them from a local profile directory, a CDP endpoint, a
storage-state file or a login in a local window, only for the domains you name unless you set `AllDomains`)
and `Webhooks`. Every method returns the endpoint's parsed JSON as a `JsonNode`; a refused request throws
`CloudException` with the server's own message and `Code`. `Cloud.VerifyWebhook(body, header, secret)`
checks a delivery's signature. The API is reached over https only (plain http is accepted for this machine
alone), and a recording download never sends your API key to the storage host.

The `clearcote cloud` command line ships with the Node and Python packages only.

## Human input (`Humanize`)

Playwright's input is instant: a click presses and releases in the same millisecond, a keystroke
holds for ~1 ms, and every click lands on the element's exact centre. No hand does any of that, and
each is separately measurable from the page. `Humanize` replaces those with a seeded motor persona —
Fitts-timed minimum-jerk pointer paths, human press-hold and key dwell, dispersed landing points, and
dropdown selection driven with the keyboard so the **engine** fires `input`/`change` (Playwright's
`SelectOptionAsync` dispatches them from script, so they arrive `isTrusted: false`).

```csharp
var page = await browser.NewPageAsync();
Humanize.Attach(page, "acct-1");        // same seed as your fingerprint => same motor signature

await page.Locator("#submit").HumanClickAsync();
await page.Locator("#email").HumanTypeAsync("someone@example.com");
await page.Locator("#country").HumanSelectOptionAsync("NL");
await page.HumanPressAsync("Enter");
```

`Attach` is optional — the first humanized call creates a random persona. Pass the same seed you pass
to `Fingerprint` and the identity moves the same way in the Python and Node SDKs too: the persona is
derived from the seed with a shared RNG, so it is a property of the identity rather than of the
language.

**These are extension methods, not replacements.** C# cannot patch `IPage`/`ILocator` the way the
Python and Node SDKs patch their page objects, so ordinary `ClickAsync` stays exactly as Playwright
wrote it and you opt in per call. `HumanSelectOptionAsync` falls back to `SelectOptionAsync` for
multi-selects and anywhere the keyboard route cannot be verified to have worked (it re-reads
`selectedIndex` afterwards rather than assuming).

## A standing, stealthy CDP endpoint (`ServeAsync`)

Launches the engine directly (not through Playwright), so `--enable-automation` is never added, and returns a
loopback CDP endpoint any Playwright/Puppeteer/CDP client can attach to via `ConnectOverCDP`:

```csharp
var srv = await Clearcote.Clearcote.ServeAsync(new ServeOptions { Fingerprint = "seed-1", Platform = "windows" });
Console.WriteLine(srv.CdpUrl);        // e.g. http://127.0.0.1:53522
// var browser = await playwright.Chromium.ConnectOverCDPAsync(srv.CdpUrl);
await srv.CloseAsync();
```

Headless, `ServeAsync` gives the browser a real-size display (the persona's, or one drawn from real
desktops) and maximizes its window onto the work area before any client attaches, so every page, tab
and popup reports a window that fits its screen. Set `WindowSize` for a smaller window (clamped to the
work area), or pass your own `--window-size` / `--screen-info` in `Args` to opt out.

## Just the binary

```csharp
var exe = await Clearcote.Clearcote.ExecutablePathAsync(new LaunchOptions { LicenseKey = "cc_lic_..." });
// download/verify only, no launch:
var path = await Clearcote.Clearcote.DownloadAsync();
```

Windows: when a cached build cannot start from the cache (`spawn UNKNOWN`, "the side-by-side configuration is incorrect"; this happens when the SDK runs inside an MSIX-packaged app, whose writes to `%LOCALAPPDATA%` Windows redirects), launches use one copy of that build in `~/.clearcote/recovered/` instead, made once and reused by every later launch from .NET, Python or Node. `WinLaunch.ClearRecovered()` removes those copies, as `clearcote clear-cache` does.

## Options (subset)

| Option | Switch / effect |
|---|---|
| `Fingerprint` | `--fingerprint` — the per-eTLD+1 farbling seed (stable identity) |
| `Platform` | `--fingerprint-platform` = `windows` \| `linux` \| `macos` \| `android` |
| `Brand` / `BrandVersion` | `--fingerprint-brand` / `-brand-version` (`Chrome`, `Edge`, …) |
| `TlsProfile` | `--fingerprint-tls-profile` — keep the TLS ClientHello coherent with the claimed Chrome major |
| `GpuVendor` / `GpuRenderer` | WebGL `UNMASKED_VENDOR` / `RENDERER` |
| `HardwareConcurrency` | `navigator.hardwareConcurrency` |
| `Timezone` / `AcceptLanguage` | IANA tz + `navigator.languages` (+ coherent `Intl` locale) |
| `WebrtcIp` | WebRTC egress IP (fabricated srflx; no real STUN leaks) |
| `DisableGpuFingerprint` | report the host's real GPU (most coherent vs strict classifiers) |
| `FingerprintNoise = false` | turn OFF farbling noise (canvas/WebGL/audio) |
| `FingerprintProfile` | import a real captured fingerprint (path / JSON string / object) |
| `StorageQuota` | `navigator.storage.estimate().quota` in MB |
| `CanvasBridge` | forward canvas/WebGL readback to a remote real-GPU host |
| `Proxy`, `Args`, `Extensions`, `Headless`, `Channel`, `Env` | Playwright pass-through + SDK arg handling |
| `ViewportSize`, `ScreenSize` | override the context geometry (opts out of the headless default below) |

## Window geometry

`LaunchPersistentContextAsync` / `LaunchEphemeralProfileAsync` give a headless context a coherent
window geometry by default (0.24.0+), so `screen`, `availWidth/Height`, `innerWidth/Height` and
`outerWidth/Height` agree with each other the way a real window's do. With a `Fingerprint` seed the
engine's own screen and work area are used and the window is sized to them; without a seed the SDK
sets the headless display to a screen size drawn from real captured desktops (with a taskbar on
Windows) and sizes the window to its work area the same way. It is applied at launch, before your
first navigation. Set `ViewportSize` or `ScreenSize` to opt out.

`LaunchAsync` can only do half of this: it sets the display, but it returns an `IBrowser` whose
`NewPageAsync`/`NewContextAsync` you call yourself, and a page with Playwright's default emulated
viewport still reports a window larger than its screen. Prefer `LaunchEphemeralProfileAsync` (also
recommended for DRM/CDM reasons), or finish it yourself:

```csharp
var page = await browser.NewPageAsync(new() { ViewportSize = ViewportSize.NoViewport });
await Geometry.FitWindowToWorkAreaAsync(page);
```

## Environment variables

`CLEARCOTE_LICENSE_KEY`, `CLEARCOTE_LICENSE_API`, `CLEARCOTE_INSTANCE_ID`, `CLEARCOTE_BINARY`,
`CLEARCOTE_CACHE`, `CLEARCOTE_AUTO_UPDATE`; for the cloud, `CLEARCOTE_CLOUD`, `CLEARCOTE_API_KEY` and
`CLEARCOTE_API_URL`.

Downloaded browsers are cached per build, in the same place the Python and Node SDKs use (`CLEARCOTE_CACHE`
overrides it). Processes that share this cache take turns installing a build: the first one downloads it, the
others wait and then use it, whichever SDK (Python, Node or .NET) each one runs. That needs every process
sharing the cache to run an SDK release with this install lock (any release after 0.40.1); earlier releases do
not wait for it.

## Scope

This SDK covers the core: persona → engine switches, free + PRO binary resolution (download / verify /
extract / cache, with the Windows first-launch AV-race work-around), the full floating-concurrency licensing
client, `LaunchAsync` / `LaunchPersistentContextAsync` / `ServeAsync` / `ExecutablePathAsync`, the default
stealth args, and cloud mode with the full hosted API (see [Local or cloud](#local-or-cloud)). The higher-level add-ons in the Node/Python SDKs — the humanized cursor, in-browser AI agent,
Widevine/EME helper, saved-profile manager, and render-coherence linter — are planned follow-ups; the
underlying engine switches are all reachable today via `Args`.

Also available (0.29.0): `Geoip = true` (through the proxy, incl. SOCKS5; one
`CLEARCOTE_GEOIP_TIMEOUT_SECONDS` deadline; throws `GeoipException` before launch if unresolved unless
`Timezone` and `AcceptLanguage` are both set), `Fingerprint = "off"` (no persona), `FingerprintVoices = false`,
`AllowThirdPartyCookies = true`, `TransparentProxy = true` (engine 152 r22+; skipped with a warning on older
engines), `LicenseThroughProxy`, `ReleaseChannel`, and `License.GetSessionSeatsAsync()`.

## License

BSD-3-Clause. See [LICENSE](https://github.com/clearcotelabs/clearcote-browser/blob/main/LICENSE).
