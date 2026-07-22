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

## Just the binary

```csharp
var exe = await Clearcote.Clearcote.ExecutablePathAsync(new LaunchOptions { LicenseKey = "cc_lic_..." });
// download/verify only, no launch:
var path = await Clearcote.Clearcote.DownloadAsync();
```

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

## Environment variables

`CLEARCOTE_LICENSE_KEY`, `CLEARCOTE_LICENSE_API`, `CLEARCOTE_INSTANCE_ID`, `CLEARCOTE_BINARY`,
`CLEARCOTE_CACHE`, `CLEARCOTE_AUTO_UPDATE`.

## Scope

This SDK covers the core: persona → engine switches, free + PRO binary resolution (download / verify /
extract / cache, with the Windows first-launch AV-race work-around), the full floating-concurrency licensing
client, `LaunchAsync` / `LaunchPersistentContextAsync` / `ServeAsync` / `ExecutablePathAsync`, and the default
stealth args. The higher-level add-ons in the Node/Python SDKs — the humanized cursor, in-browser AI agent,
Widevine/EME helper, geoip auto-fill, saved-profile manager, and render-coherence linter — are planned
follow-ups; the underlying engine switches are all reachable today via `Args`.

## License

BSD-3-Clause. See [LICENSE](https://github.com/clearcotelabs/clearcote-browser/blob/main/LICENSE).
