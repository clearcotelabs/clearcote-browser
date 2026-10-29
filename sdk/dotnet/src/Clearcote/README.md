# Clearcote (.NET)

A Playwright drop-in for the [Clearcote](https://github.com/clearcotelabs/clearcote-browser) anti-fingerprint
Chromium build. `Clearcote.LaunchAsync()` returns a standard **Microsoft.Playwright `IBrowser`** backed by
the verified Clearcote binary — auto-downloaded, cached, and **SHA-256-checked again before every launch** (the
executable, and `chrome.dll` on Windows). Every
persona knob maps to the engine's fingerprint switches; a PRO license key pulls the license-gated build and
checks out a floating-concurrency lease.

The .NET SDK mirrors the [npm `clearcote`](https://www.npmjs.com/package/clearcote) and
[PyPI `clearcote`](https://pypi.org/project/clearcote/) SDKs feature for feature (the command-line tools excepted:
`clearcote` and `clearcote-agent` ship with the npm and pip packages). Windows x64, Linux x64 and macOS on Apple
Silicon (the native PRO build; without it, `Docker = true` / `CLEARCOTE_DOCKER=1` runs the browser in the Clearcote
Docker image, which `LaunchAsync` and `LaunchEphemeralProfileAsync` pick on their own on such a Mac).

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

In Docker (`Docker = true`), geoip works exactly as for a native launch (on by default): the SDK resolves the proxy's
exit IP (or this machine's own IP without a proxy) before the container is started and hands the container the
timezone and Accept-Language it resolved (any `Timezone` / `AcceptLanguage` you set yourself still wins). With
`Geoip = false` or `CLEARCOTE_GEOIP=0` (and no `Timezone` / `AcceptLanguage` of your own) the container's entrypoint
builds the persona the way a native launch does: Accept-Language defaults to `en-US` and the timezone is derived from
its primary tag (`en-US` -> `America/New_York`), so every such container is New York, English. A container has no
WebRTC ICE candidates at all unless `WebrtcIp` is given; the value to give is the proxy's exit IP, which geoip fills in
as it does for a native launch. Without a licence key (the image's open build) a launch through a proxy keeps no WebRTC
IP from the default lookup, since the open build would send WebRTC UDP outside the proxy; `Geoip = true` or an
explicit `WebrtcIp` is kept.

Credentials stay off Playwright: a SOCKS5 proxy's always, and an http(s) proxy's on an engine with
`--proxy-auth` (r19+), go to the browser's own switches, because credentials handed to Playwright make it turn
on request interception and disable the cache for the whole context (a detectable transport tell). Playwright's
request API (`context.APIRequest`, `page.APIRequest`) does not go through the browser, so for those launches the
SDK runs a small forwarder on `127.0.0.1` that adds the credentials and tunnels to your proxy (HTTP/HTTPS
`CONNECT`, or SOCKS5 with username/password); Playwright is given that forwarder, without credentials, and it
stops when the browser or context closes. Loopback targets (`localhost`, `127.x`) are connected directly, as the
browser does. A client attached to `ServeAsync` over CDP runs the request API in its own process, so
`ServeAsync` warns that its `APIRequest` calls leave from that client's IP; make API calls inside the page
(`fetch` in `EvaluateAsync`) there instead.

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
silently downgrades to the free binary. Each browser checks out its own slot from the licence lease server
(`lease.clearcotelabs.com`, with `www.clearcotelabs.com` as its fallback); a background heartbeat keeps it
alive and rotates the token, and the slot is released when the browser closes. A paid licence still launches
on its last run-token while the lease server cannot be reached. Override the backend with `LicenseApiBase` or
`CLEARCOTE_LICENSE_API` (every licence call), or only the lease calls with `CLEARCOTE_LEASE_API`.

## Persistent profile

```csharp
var context = await Clearcote.Clearcote.LaunchPersistentContextAsync("./profile-7423", new LaunchOptions
{
    Fingerprint = "acct-1",
    Headless    = false,
});
```

## Saved personas (`Profile`) and `LocalProfile = "auto"`

A `Profile` is a named set of launch options saved as JSON in `~/.clearcote/profiles/<name>.json`
(`CLEARCOTE_PROFILE_DIR` overrides the directory), in the same format the Python and Node SDKs read and write, so
one file serves all three. `LocalProfile` launches one: its options fill everything left unset, and explicit
options win.

```csharp
new Profile("acct-1", new LaunchOptions { Fingerprint = "acct-1", Platform = "windows" }).Save();
var ctx = await Clearcote.Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions { LocalProfile = "acct-1" });
// or: await Profile.Load("acct-1").LaunchEphemeralProfileAsync();   Profile.ListProfiles() names them
```

`LocalProfile = "auto"` (or `Profile = "auto"`, the Node spelling) applies a real captured fingerprint for this
machine as `FingerprintProfile`, with no seed: the licensed profile service first, a local directory of
clearcote-profile JSON as backup (`CLEARCOTE_PROFILE_SOURCE`, default `~/.clearcote/profiles/imported`), announced
on stderr either way. It may launch the engine once, headless and without a persona, to measure this host's GPU and
display (cached per binary for 30 days). An engine before 150 cannot apply a profile and gets a stable seed persona
instead. `ProfileSelect` tunes it (`Mode`, `Key`, `PreferLocal`, `LocalDir`, `Offline`, ...);
`CLEARCOTE_PROFILE_API` points it at another service. `LocalProfile` is for local launches: a cloud or Docker
launch refuses it (`Profile` stays the cloud's cookie store).

## Widevine / DRM (`Widevine`)

clearcote ships the EME/Widevine plumbing but, being open source, not Google's proprietary CDM. The SDK fetches
that CDM from Google's own component server (as a real Chrome receives it), caches it and enables it, so
`navigator.requestMediaKeySystemAccess('com.widevine.alpha')` resolves and DRM streams play instead of EME being a
"no-Widevine" tell.

**On by default for PRO launches (since 0.48.0).** A licensed launch (a licence key set: the PRO build, which claims
the Google Chrome brand) on an engine from r32 loads the CDM by default, in `LaunchAsync`,
`LaunchPersistentContextAsync`, `LaunchEphemeralProfileAsync` and `ServeAsync` alike:

- The first such launch **downloads Google's CDM from Google** (`update.googleapis.com`), checks its SHA-256 and
  caches it under `~/.clearcote/WidevineCdm` (`CLEARCOTE_WIDEVINE_DIR` overrides). Later launches use the cache **at
  once, with no network**; once a day a background check asks Google for a newer CDM (used from the next launch
  on). A failed check counts as that day's check: the cached CDM keeps working.
- **Fail-soft**: only a launch with nothing cached waits for the download, at most
  `CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS` (default 15, clamped to 1-600). If it fails — offline, a firewall, Google
  refusing the request — the launch goes on without Widevine and prints one warning per process (`Quiet` or
  `CLEARCOTE_NO_WARN=1` silences it); the unfinished download is cancelled, and that process retries it after 10
  minutes.
- **Opt out** with `CLEARCOTE_WIDEVINE=0` (every launch), or `Widevine = false`. An explicit value wins over the
  environment; `Widevine = false` is also accepted (and ignored) by cloud and Docker launches.
- **Free (open) builds stay opt-in**: set `Widevine = true` (or `CLEARCOTE_WIDEVINE=1`). On a persistent context
  that also seeds the CDM into the profile, so an engine before r32 loads it too.

```csharp
var context = await Clearcote.Clearcote.LaunchPersistentContextAsync("./profile-drm", new LaunchOptions
{
    Widevine = true,   // free build: fetch + seed + enable (a PRO launch has it on already)
});
var cdmDir = await Widevine.FetchAsync();   // or fetch it ahead of time, e.g. in a Docker build step
```

Your own `--widevine-cdm-path=<dir>` in `Args` is left as it is. Cloud launches are unchanged (hosted browsers load
Widevine on the server). The package never distributes Google's CDM: it is downloaded from Google, on your
machine. Software-secure (L3) playback only.

## Certificate revocation (`Crlset`, opt-in)

Real Chrome receives Google's **CRLSet** (the list of revoked and distrusted certificate keys) in the background,
which is how a CA compromise reaches browsers within hours. A Clearcote engine never downloads it, so on its own it
keeps trusting certificates that Chrome already blocks. `Crlset = true` closes that gap for every engine the SDK
launches, old builds included:

- The SDK fetches the current CRLSet **from Google** (`update.googleapis.com`, the same service as the Widevine CDM),
  checks its SHA-256 and caches it under `~/.clearcote/crlset` (`CLEARCOTE_CRLSET_DIR` overrides; shared with the
  Python and Node SDKs). A cache older than 6 hours is refreshed in the background.
- It places the CRLSet in the profile and lets the engine's component updater load it (no network request of its
  own). The launch then **waits until the CRLSet is live**, usually a few seconds, so the first page you open is
  already protected.
- `CrlsetNoWait = true` skips the wait (live once the first tab has been idle for a moment), `CrlsetPath` seeds that
  file instead, `CrlsetTimeout` bounds the wait (default 15 s). `CLEARCOTE_CRLSET=1` turns it on for every launch.
- Fail-soft: a failed first download or an unconfirmed CRLSet prints one warning and the launch goes on. It needs a
  profile: `LaunchEphemeralProfileAsync` and `LaunchPersistentContextAsync`; `LaunchAsync` (incognito) warns and goes
  on without it.

```csharp
await using var context = await Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions { Crlset = true });
var page = context.Pages[0];
await page.GotoAsync("https://dv-g3b-emudhra.we-have-a-problem.com/"); // net::ERR_CERT_REVOKED, as in Chrome
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
  `ProxySession`, `TimeoutSec`, `IdleTimeoutSec`, `MaxGb`, `Url`, `Adblock`, `SolveSliders`, `SolveCheckboxes`, `SolvePressHold`,
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
Fitts-timed minimum-jerk pointer paths (an arcing primary that peaks before the halfway point and, in a
share of moves fixed per identity, passes the target and comes back; `HumanDragToAsync` legs stay
within a few pixels of the line from grab to drop), human press-hold and key dwell, dispersed landing points, and
dropdown selection driven with the keyboard so the **engine** fires `input`/`change` (Playwright's
`SelectOptionAsync` dispatches them from script, so they arrive `isTrusted: false`). Typing rolls over
on some fast key pairs (the next key goes down before the previous one comes up), and
`HumanWheelAsync` scrolls in **whole wheel notches** (100 px on a Windows identity, 120 on Linux, 40 on
macOS; the distance is rounded to the nearest notch), because a browser reports every wheel event as
one full notch.

```csharp
var page = await browser.NewPageAsync();
Humanize.Attach(page, "acct-1");        // same seed as your fingerprint => same motor signature

await page.HumanFillAsync("#name", "Ada Lovelace");                 // click, clear if needed, type, verify
await page.Locator("#email").HumanTypeAsync("someone@example.com");
await page.HumanSelectOptionAsync("#country", "Netherlands");       // value or label, one trusted change
await page.HumanFillAsync("#volume", "40");                         // a range: dragged, then nudged
await page.HumanFillAsync("#date", "2026-10-11");                   // typed segment by segment
await page.HumanCheckAsync("#terms");
await page.HumanClickAsync("#menu", new HumanClickOptions { Button = MouseButton.Right });
await page.HumanPressAsync("Enter");
```

Forms (`HumanFillAsync`, `HumanSelectOptionAsync`, `HumanCheckAsync` / `HumanUncheckAsync` /
`HumanSetCheckedAsync`) are handled the way a person handles them: a text field is clicked, emptied
only if it holds something (held Ctrl/Cmd+A, Backspace) and typed (a `maxlength` field gets only the
characters it can hold; a field whose input mask re-punctuates what is typed, such as an `MM/YY` expiry or a dashed phone number, is typed once and never wiped and retyped; a value over 200 characters is inserted in one go after the click); a drop-down is opened with a click,
moved by type-ahead and committed with Enter, so it fires **one** trusted change; list boxes get row
clicks with Ctrl/Cmd held for more rows; a range thumb is dragged; date and time editors are typed
segment by segment in the order the browser shows them, with the same key timing as text. Every press
comes after a short pause once the pointer has arrived (40-250 ms, typical length fixed per identity). Each result is verified, and anything the
humane route cannot prove goes to Playwright's own method. The click family (`HumanClickAsync`,
`HumanDblClickAsync`, `HumanHoverAsync`) honours every Playwright click option — button, click count,
hold, modifiers, a padding-box position, force and trial — through `HumanClickOptions` or
`LocatorClickOptions`. Typing makes the occasional fat-finger slip and corrects it, never in password,
e-mail, number, phone, URL or one-time-code fields, nor in a field inside an iframe.

**Behaviour changes in this release** (from the forms work):

- **Typos are on by default** (2 % of eligible characters) in `HumanTypeAsync` and `HumanFillAsync`; earlier
  .NET releases made no slips. Turn them off with `CLEARCOTE_HUMANIZE_TYPO_RATE=0` (the name is also
  `Humanize.TypoRateEnv`); any value from 0 to 1 sets the rate, read at every call.
- **A timeout is raised once, on the caller's deadline** (the option's `Timeout`, else the page's default
  timeout), instead of falling back to the native call and waiting a second time.
- **A range fill only takes values the browser keeps.** A value off the step, out of range or not a number
  raises Playwright's "Malformed value", exactly as `FillAsync` does; it is never snapped or clamped.
- **`page.HumanSelectOptionAsync(selector, …)` takes the first match**, like `page.SelectOptionAsync`
  (pass `Strict = true` for a strict one), and a string matches an option's label as well as its value,
  as in Playwright. The old closed-select arrow route is gone: whatever the humane route cannot do goes to
  `SelectOptionAsync`.
- **`HumanPressAsync` on a button or link focuses it without a click** (one activation); a text field is
  clicked into first, unless it already has focus.
- `Delay = 0` (Playwright's default) or a negative `Delay` holds the button for the persona's time, not
  0 ms; locators inside a frame (`FrameLocator`) go to Playwright's own method; and concurrent `Human*`
  calls on one page take turns instead of interleaving their keys.
- **Humanized clicks no longer run a Playwright `trial`** before pressing: the covered-point check is the
  browser's own hit test, read where the page cannot see it, so the pointer is not moved onto the target
  and back and no blocked press reaches the page. A click point inside a child frame is checked against the
  `<iframe>` element that holds it, and a layer with `pointer-events: none` over the target is skipped, as
  a real press skips it. `Trial = true` runs Playwright's checks in the SDK (visible, enabled, stable, the
  point receives the press) and throws a `TimeoutException` when they never pass, without moving the
  pointer or pressing. A plain-CSS target (`"#id"`, `page.Locator(".cls")`) is located without any Playwright element
  handle; a long drop-down list is crossed with a held arrow key and type-ahead that includes spaces.

`Attach` is optional — the first humanized call creates a random persona. Pass the same seed you pass
to `Fingerprint` and the identity moves the same way in the Python and Node SDKs too: the persona is
derived from the seed with a shared RNG, so it is a property of the identity rather than of the
language.

### Press and hold (`PressAndHoldAsync`)

For controls that act only while the button stays down — a hold-to-confirm button, a long-press menu, a
"press and hold" verification button — `PressAndHoldAsync` makes one long press on the element and returns
`Task<bool>`:

```csharp
// hold for 3 s exactly
await page.PressAndHoldAsync("#confirm", new PressAndHoldOptions { Duration = 3000 });

// hold until the page says it is done (polled every 100 ms, released 160-350 ms later; gives up after 15 s)
bool ok = await page.Locator("#hold").PressAndHoldAsync(new PressAndHoldOptions
{
    Until = async () => await page.Locator("#hold[data-state=done]").CountAsync() > 0,
});
```

It waits for the element to be visible (`Timeout`), brings it into view, moves to a point inside it
(`Position`, relative to the element's top-left, else a persona-shaped point near the centre), presses
(`Button`, default left) and holds for `Duration` ms (default 2.8-3.8 s) or until `Until` returns true. While
the button is down the pointer drifts now and then by whole pixels, never more than 2 px from the press point
(`Tremor = false` keeps it still); every one of those moves reports the button held. It returns `true` when the
hold ran its full duration or `Until` became true, `false` when `Until` never did. The button is always
released, also when `Until` throws or the `CancellationToken` is cancelled mid-hold. A non-positive or
non-finite `Duration`, or an unknown `Button`, throws `ArgumentException`.

On a page with a persona (`Humanize.Attach`, or any earlier `Human*` call on it) the approach is a humanized
glide, the press comes after the persona's pre-press pause, and the hold takes its turn with the page's other
`Human*` calls. On any other page the pointer moves straight there with `page.Mouse`. A locator inside an
iframe (`FrameLocator`, or a locator made on a child `IFrame`) works too: the press is made at viewport
coordinates.

**These are extension methods, not replacements.** C# cannot patch `IPage`/`ILocator` the way the
Python and Node SDKs patch their page objects, so ordinary `ClickAsync` stays exactly as Playwright
wrote it and you opt in per call. The locator forms are strict like Playwright's (more than one match
raises its strict-mode error), and a timeout is raised once, on the caller's deadline.

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
work area), or pass your own `--window-size` / `--screen-info` in `Args` to opt out. On an engine with
persona windows a seeded `ServeAsync` opens the persona's own window instead (see
[Window geometry](#the-personas-own-window-r39)); `WindowSize` still wins.

### Many identities on one endpoint (`ServeMultiplexAsync`)

One port, one browser per identity, chosen by the connection URL:

```csharp
await using var mux = await Clearcote.Clearcote.ServeMultiplexAsync(new MultiplexOptions { Port = 9222, IdleTimeoutSec = 300, DataDir = "./profiles" });
var a = await playwright.Chromium.ConnectOverCDPAsync("http://127.0.0.1:9222?fingerprint=acct-1&timezone=Europe/Berlin");
var b = await playwright.Chromium.ConnectOverCDPAsync(
    $"http://127.0.0.1:9222?fingerprint=acct-2&proxy={Uri.EscapeDataString("socks5://u:p@host:1080")}&geoip=true");
```

- Query options: `fingerprint`, `timezone`, `locale`, `proxy`, `geoip`, plus any fingerprint option in kebab-case (`platform`, `hardware-concurrency`, `allow-third-party-cookies`, ...). Unknown options are rejected (400).
- The same identity reuses its browser. Asking for a running identity with *different* options returns 409; close it first.
- `GET /` lists running browsers (`mux.Status()` in code); `POST /fingerprint/<id>/close` stops one (`mux.CloseIdentityAsync(id)`); `IdleTimeoutSec` (or `CLEARCOTE_SERVE_IDLE_TIMEOUT`) closes a browser after its last client disconnects; `MaxBrowsers` caps them (default 16, then 429).
- Every other `MultiplexOptions` setting (`ExecutablePath`, `Headless`, `Proxy`, persona defaults, `LicenseKey`, ...) is the base each identity's browser starts from; the query overrides it.
- Behind a reverse proxy, WebSocket URLs honour `X-Forwarded-Host` / `X-Forwarded-Proto`; add the public name to `AllowHosts`.
- Binds `127.0.0.1` by default. Requests a web page could make (cross-site fetches, a foreign `Origin`, a `Host` that isn't an IP / `localhost` / allowed) are refused.

## Just the binary

```csharp
var exe = await Clearcote.Clearcote.ExecutablePathAsync(new LaunchOptions { LicenseKey = "cc_lic_..." });
// download/verify only, no launch: picks the build a launch would (CLEARCOTE_BINARY, a catalog Version, PRO when a
// licence key resolves, else free)
var path = await Clearcote.Clearcote.DownloadAsync(new DownloadOptions { Version = "155" });
foreach (var b in Download.ListCachedBuilds()) Console.WriteLine($"{b.Tag} {b.Path}");
```

The licence key file the `clearcote login` command writes is shared by all three SDKs: `License.SaveLicenseKey`,
`License.RemoveLicenseKey`, `License.LicenseKeySource()` (where the key in effect comes from) and
`License.LicenseExpiry()` read and write the same `~/.clearcote` files.

Windows: when a cached build cannot start from the cache (`spawn UNKNOWN`, "the side-by-side configuration is incorrect"; this happens when the SDK runs inside an MSIX-packaged app, whose writes to `%LOCALAPPDATA%` Windows redirects), launches use one copy of that build in `~/.clearcote/recovered/` instead, made once and reused by every later launch from .NET, Python or Node. `WinLaunch.ClearRecovered()` removes those copies, as `clearcote clear-cache` does.

## How many browsers fit on this machine (`ClearcoteBench`)

"Why is my CPU at 100 %" and "how many browsers can I run" in one call. It starts `Concurrency` browsers (headless, temporary profiles, deleted afterwards), loads `Url` in each for `DurationSec` seconds, through `Proxy` if you give one, and reports what each browser costs in CPU threads and RAM on this machine, page timings (DOMContentLoaded, load, network idle), errors, about how many browsers the machine runs at once, and what to change. The lines are the ones `clearcote bench` prints in the Node and Python SDKs:

```csharp
var report = await ClearcoteBench.RunAsync(new BenchOptions { Concurrency = 5, Url = "https://example.com", Proxy = "http://user:pass@host:8080" });
foreach (var line in report.Lines()) Console.WriteLine(line);   // or report.ToJson()
foreach (var line in ClearcoteHost.Lines(ClearcoteHost.Info())) Console.WriteLine(line);   // CPU, RAM, OS, VM, GPU only
```

It runs fewer browsers than asked when free RAM looks too small (`Force = true` runs them all). Each browser holds a licence seat while it runs. Paste the lines into a support request; proxy credentials are never printed.

## Font report (what `clearcote info` says about fonts)

The Python and Node SDKs print the fonts a launch would see with `clearcote info`; this SDK has no CLI, so the
same report is an API. On Linux it says whether the build bundles its Windows font clones, which of 29 scripts
the fonts can draw (the bundle plus `FontDirs` / `CLEARCOTE_FONT_DIRS` and `CLEARCOTE_FALLBACK_FONT_DIRS`) and
which are missing (a page sees those as empty boxes), and which key Windows families your own fonts provide for
real. Off Linux the browser uses the host's fonts and the report says so.

```csharp
var report = await Clearcote.Clearcote.FontReportAsync(new LaunchOptions { FontDirs = new[] { "/srv/winfonts" } });
Console.WriteLine(report);              // the lines `clearcote info` prints
foreach (var script in report.ScriptsMissing ?? Array.Empty<string>()) Console.WriteLine($"missing: {script}");
// or for a build you already have, without resolving or downloading one:
var r = Clearcote.Clearcote.FontReportFor("/opt/clearcote/chrome");
```

`FontReportAsync` resolves the build as a launch would (downloading it when it is not cached yet);
`FontReportFor` only reads files. The result's properties: `Bundled`, `Note`, `ScriptsCovered`,
`ScriptsMissing`, `FontDirs`, `FallbackFontDirs`, `IgnoredFontDirs`, `GenuineWindowsFamilies`,
`LookalikeWindowsFamilies`, `GenuineFacesSupported`; `Lines()` / `ToString()` give the CLI text.

## Options (subset)

| Option | Switch / effect |
|---|---|
| `Fingerprint` | `--fingerprint` — the per-eTLD+1 farbling seed (stable identity) |
| `Platform` | `--fingerprint-platform` = `windows` \| `linux` \| `macos` \| `android` |
| `Brand` / `BrandVersion` | `--fingerprint-brand` / `-brand-version` (`Chrome`, `Edge`, …) |
| `TlsProfile` | `--fingerprint-tls-profile` — keep the TLS ClientHello coherent with the claimed Chrome major |
| `GpuVendor` / `GpuRenderer` | WebGL `UNMASKED_VENDOR` / `RENDERER`. A short vendor name (`"intel"`) is sent as Chrome reports it (`Google Inc. (Intel)`); a vendor and renderer from two GPU makers throw `ArgumentException`. Pin both. |
| `HardwareConcurrency` | `navigator.hardwareConcurrency` |
| `Timezone` / `AcceptLanguage` | IANA tz + `navigator.languages` (+ coherent `Intl` locale) |
| `WebrtcIp` | WebRTC egress IP (fabricated srflx; no real STUN leaks) |
| `DisableGpuFingerprint` | report the host's real GPU (most coherent vs strict classifiers) |
| `PersonaSchema` | `--fingerprint-schema` — which identity model a seed derives: 7 (the default since 2026-10-26, engine 156 r51+ via the persona service), 5 (r45 to r51), 4 (154 r37 to 155 r44), 3 (154 r36), 2 (153 r29 to 154 r35), 1 (before r29); there is no 6; a seed's identity under a schema never changes |
| `RealGpuHost` | `--fingerprint-gpu-backend-real` — this host renders on a real GPU, so a schema-2 persona may claim a dedicated card |
| `FingerprintNoise = false` | turn OFF farbling noise (canvas/WebGL/audio) |
| `FingerprintProfile` | import a real captured fingerprint (path / JSON string / object) |
| `StorageQuota` | `navigator.storage.estimate().quota` in MB |
| `CanvasBridge` | forward canvas/WebGL readback to a remote real-GPU host |
| `Proxy`, `Args`, `Extensions`, `Headless`, `Channel`, `Env` | Playwright pass-through + SDK arg handling |
| `ViewportSize`, `ScreenSize` | override the context geometry (opts out of the headless default below) |
| `ColorScheme` | the `prefers-color-scheme` Playwright emulates; unset = none on an engine from r32 (see below) |
| `FontDirs` | Linux: your own fonts, listed ahead of the bundled lookalikes (see below) |
| `PersonaEnv = false` | keep the persona switches on the browser's command line (see below) |
| `StockRuntime = true` | stock DevTools behaviour for debugging (see below) |
| `RuntimeBindings = true` | `ExposeFunctionAsync` / `ExposeBindingAsync` across navigations, engine r54+ (see below) |
| `PortableProfile` / `EncryptionKey` | a profile directory that opens on another machine (engine switch) |
| `PlaywrightLaunchOptions` / `PlaywrightContextOptions` | any other Playwright launch / persistent-context field (`DownloadsPath`, `Locale`, `RecordVideoDir`, `StorageStatePath`, ...); a field `LaunchOptions` has its own property for throws |
| `Humanize` / `HumanizeOptions` / `ShowCursor` | humanize every page of the launch, seeded from `Fingerprint` (see [Human input](#human-input-humanize)) |
| `AgentLlmUrl` / `AgentLlmKey` / `AgentModel` / `AgentToolMode` / `AgentTyping` | the in-browser AI agent (see below) |

An HTTP(S) proxy with a username and password goes to the engine's own `--proxy-auth` (r19+) instead of
Playwright's request interception, which pages can notice; SOCKS5 credentials go to `--socks5-credentials`. An
option the engine is too old for (`PersonaSchema` 2-5 and 7, `RealGpuHost`, SOCKS5 credentials) warns and is ignored.

## Window geometry

`LaunchPersistentContextAsync` / `LaunchEphemeralProfileAsync` give a headless context a coherent
window geometry by default (0.24.0+), so `screen`, `availWidth/Height`, `innerWidth/Height` and
`outerWidth/Height` agree with each other the way a real window's do. With a `Fingerprint` seed the
engine's own screen and work area are used and the window is sized to them; without a seed the SDK
sets the headless display to a screen size drawn from real captured desktops (with a taskbar on
Windows) and sizes the window to its work area the same way. It is applied at launch, before your
first navigation. Set `ViewportSize` or `ScreenSize` to opt out. On Linux a seeded headless launch also gets a
display the persona's size (`--screen-info`; your own in `Args` wins), so full-screen, pop-ups and moved windows
stay inside the screen the page is told about.

`LaunchAsync` can only do half of this: it sets the display, but it returns an `IBrowser` whose
`NewPageAsync`/`NewContextAsync` you call yourself, and a page with Playwright's default emulated
viewport still reports a window larger than its screen. Prefer `LaunchEphemeralProfileAsync` (also
recommended for DRM/CDM reasons), or finish it yourself:

```csharp
var page = await browser.NewPageAsync(new() { ViewportSize = ViewportSize.NoViewport });
await Geometry.FitWindowToWorkAreaAsync(page);
```

### The persona's own window (r39+)

On an engine with persona window support (licensed builds from r39), a `Fingerprint` seed decides the
browser window too, not only the screen. The engine derives the window that machine has open on its
screen — maximized for most seeds, a restored window of a seed-derived size and position for the rest,
always inside the work area — and creates the **real** window with those bounds and that state before the
first page exists; in headless mode the headless display becomes the persona's display. `outerWidth`,
`outerHeight`, `screenX` and `screenY` are then the real window's, so they agree with the viewport,
screenshots, mouse-event coordinates, pop-ups and `device-width` media queries. The same seed opens the
same window in every process and on every launch path (including `LaunchAsync`, `ServeAsync`, raw CLI
and CDP); different seeds open different windows. The SDK then adds no `--screen-info` and runs no window
fit for that launch (older engines keep the fit). A seedless launch has no persona and keeps the
behaviour above.

Precedence, first match wins:

1. **Your explicit configuration**: `ViewportSize` (including `NoViewport`) / `ScreenSize`, `WindowSize` for
   `ServeAsync`, and `--window-size`, `--window-position`, `--start-maximized` in `Args`. An explicit
   `ViewportSize` is honoured as given; the SDK sizes the real window around it (a restored window with
   the browser's normal frame) and keeps that window on the work area.
2. **The placement this identity saved in its profile**: a `UserDataDir` reused with the same seed restores
   the window it closed with; a profile last used with another seed (or an older engine) gets the new
   seed's window.
3. **The persona's window**, derived from the seed and its final screen and work area.
4. **No persona** (no seed, or `LightStealth`): the SDK's display and a window maximized onto its work area.

`--fingerprint-window=off` in `Args` keeps a seeded launch on the previous behaviour; a pinned
`--fingerprint-schema=1`..`3` keeps it too unless you add `--fingerprint-window=on`.

## In-browser AI agent (`LaunchAgentAsync`)

The engine has its own agent that reads and drives the page with an LLM (OpenRouter by default, or any
OpenAI-compatible endpoint):

```csharp
await using var ctx = await Clearcote.Clearcote.LaunchAgentAsync(new LaunchOptions { AgentLlmKey = "sk-or-..." });
var page = ctx.Pages[0];
await page.GotoAsync("https://example.com");
var result = await Clearcote.Clearcote.RunAgentTaskAsync(page, "Find the price of the cheapest plan");
Console.WriteLine(result.FinalText);
```

## Render coherence (`CheckRenderCoherenceAsync`)

`await Clearcote.Clearcote.CheckRenderCoherenceAsync(page)` reads what WebGL reports on the running page and returns
a `RenderVerdict`: whether the GPU it claims is a software renderer, below a desktop GPU's capability floor, or a
different family from the one you expected (`claimedGpu`), with a warning per finding.

## Environment variables

`CLEARCOTE_LICENSE_KEY`, `CLEARCOTE_LICENSE_API`, `CLEARCOTE_LEASE_API`, `CLEARCOTE_LICENSE_THROUGH_PROXY`,
`CLEARCOTE_OFFLINE_KEY`, `CLEARCOTE_INSTANCE_ID`, `CLEARCOTE_RUN_TOKEN_FILE`, `CLEARCOTE_BINARY`,
`CLEARCOTE_BROWSER_VERSION`, `CLEARCOTE_RELEASE_CHANNEL`, `CLEARCOTE_DOCKER`, `CLEARCOTE_DOCKER_IMAGE`,
`CLEARCOTE_DOCKER_CACHE_VOLUME`, `CLEARCOTE_DOCKER_IDLE_EXIT`, `CLEARCOTE_PROFILE_DIR`, `CLEARCOTE_PROFILE_API`,
`CLEARCOTE_PROFILE_SOURCE`, `CLEARCOTE_PROFILE_KEY`, `CLEARCOTE_SERVE_IDLE_TIMEOUT`, `CLEARCOTE_SHADER_DIALECT`,
`CLEARCOTE_HUMANIZE_ENGINE_CLICKS`, `CLEARCOTE_HUMANIZE_ENGINE_EXT`, `CLEARCOTE_HUMANIZE_SCORE`,
`CLEARCOTE_HUMANIZE_SCORE_FILE`,
`CLEARCOTE_CACHE`, `CLEARCOTE_AUTO_UPDATE`, `CLEARCOTE_STOCK_RUNTIME`, `CLEARCOTE_RUNTIME_BINDINGS`, `CLEARCOTE_PERSONA_ENV`,
`CLEARCOTE_FONT_DIRS`, `CLEARCOTE_FALLBACK_FONT_DIRS`, `CLEARCOTE_GEOIP_TIMEOUT_SECONDS`, `CLEARCOTE_GEOIP_ECHO_URL`,
`CLEARCOTE_GEOIP_IPAPI`, `CLEARCOTE_NO_WARN`, `CLEARCOTE_WIDEVINE`, `CLEARCOTE_WIDEVINE_DIR`,
`CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS`, `CLEARCOTE_HUMANIZE_TYPO_RATE` (the humanized typing's slip rate, 0-1,
default 0.02; `0` turns typos off); for the cloud, `CLEARCOTE_CLOUD`,
`CLEARCOTE_API_KEY` and `CLEARCOTE_API_URL`.

Downloaded browsers are cached per build, in the same place the Python and Node SDKs use (`CLEARCOTE_CACHE`
overrides it). Processes that share this cache take turns installing a build: the first one downloads it, the
others wait and then use it, whichever SDK (Python, Node or .NET) each one runs. That needs every process
sharing the cache to run an SDK release with this install lock (any release after 0.40.1); earlier releases do
not wait for it.

## Scope

This SDK covers the core: persona → engine switches, free + PRO binary resolution (download / verify /
extract / cache, with the Windows first-launch AV-race work-around), the full floating-concurrency licensing
client, `LaunchAsync` / `LaunchPersistentContextAsync` / `ServeAsync` / `ExecutablePathAsync`, the default
stealth args, and cloud mode with the full hosted API (see [Local or cloud](#local-or-cloud)). The Widevine/EME helper is here too since 0.48.0 (see [Widevine / DRM](#widevine--drm-widevine)). The
higher-level add-ons of the Node/Python SDKs are here as well: the humanized cursor and keyboard (with the engine
pointer route and humanized child frames), saved personas and `LocalProfile = "auto"`, the multi-identity endpoint,
the in-browser AI agent and the render-coherence check. Only the command-line tools are Node/Python-only.

Also available (0.29.0; on by default since 0.66.0): geoip (through the proxy, incl. SOCKS5, or this machine's own IP
without a proxy; one `CLEARCOTE_GEOIP_TIMEOUT_SECONDS` deadline. Left unset, it fills only what you did not set,
runs within 10 s, warns and launches when the lookup fails, and keeps no WebRTC IP on an open-build launch through a
proxy; `Geoip = false` or `CLEARCOTE_GEOIP=0` turns it off. `Geoip = true` throws `GeoipException` before launch if
unresolved unless `Timezone` and `AcceptLanguage` are both set; the exit IP comes from an IP echo through the proxy, its geo from the
offline geoip-all-in-one database shared with the Python and Node SDKs, downloaded on first use;
`CLEARCOTE_GEOIP_ECHO_URL` points the echo at your own endpoint(s), and `CLEARCOTE_GEOIP_IPAPI=1` also asks
ip-api.com, whose free endpoint is for non-commercial use only), `Fingerprint = "off"` (no persona), `FingerprintVoices = false`,
`TransparentProxy = true` (engine 152 r22+; skipped with a warning on older engines), `LicenseThroughProxy`,
`ReleaseChannel`, and `License.GetSessionSeatsAsync()`. Third-party cookies are allowed by default since 0.45.0,
as in Chrome; `AllowThirdPartyCookies = false` blocks them (an engine before 152 r22 always blocks them).

`StockRuntime = true` (or `CLEARCOTE_STOCK_RUNTIME=1`) gives Playwright the browser's stock DevTools behaviour
back: the page's `Console` and `PageError` events fire, `SetContentAsync` works, and `ExposeFunctionAsync` /
`ExposeBindingAsync` keep working after a navigation. It is off by default because pages can observe some of what
it restores. It needs an engine from r32 on; an older engine launches without it and the SDK warns once. It applies
to local and Docker launches, not to cloud browsers.

`RuntimeBindings = true` (or `CLEARCOTE_RUNTIME_BINDINGS=1`) restores only the last of those: `ExposeFunctionAsync` /
`ExposeBindingAsync` (and any CDP `Runtime.addBinding`) keep working after `GotoAsync` or a reload, while console
events stay suppressed. A launch that adds no binding looks the same to a page; one that does shows the page that
binding's global, as Chrome does. Off by default. It needs an engine from r54 on; an older engine launches without it
and the SDK warns once. Local and Docker launches only.

On an engine from r32 two more things change; the SDK checks the binary first, so an older engine launches
exactly as before:

- **The persona stays off the command line** (`PersonaEnv`, on by default). The seed, every persona override,
  proxy credentials and the canvas-bridge token reach the browser in the `CLEARCOTE_PERSONA_ARGS` environment
  variable instead of on its command line, which any local user can read. `PersonaEnv = false` or
  `CLEARCOTE_PERSONA_ENV=0` keeps them on the command line; a Docker launch hands the choice to the container.
- **Light or dark comes from the persona.** The engine picks the persona's colour scheme (about one in three is
  dark; `--fingerprint-color-scheme=light|dark` in `Args` forces one), and CSS, `matchMedia`, the
  `Sec-CH-Prefers-Color-Scheme` header and system colours follow it. `LaunchPersistentContextAsync` and
  `LaunchEphemeralProfileAsync` then leave Playwright's colour-scheme emulation off (`ColorScheme.Null`) unless you
  set `ColorScheme`. With `LaunchAsync`, pass `ColorScheme = ColorScheme.Null` to the contexts you create.

A launch that claims Linux on a Windows host warns once per process: Direct3D caps the WebGL and WebGPU limits
there, and a persona can lower a limit but never raise one past the driver's, so a Linux persona reports limits no
Linux machine has. Claim `Platform = "windows"` on Windows, or run Linux personas on Linux or in Docker.

**Your own fonts on Linux (`FontDirs`).** On Linux the browser sees only the fonts the SDK points it at: the
release's bundle of Windows metric-compatible lookalikes (Arial→Arimo, Segoe UI→Selawik, …), never the host's
(before 0.41.0 the .NET SDK did not point it at the bundle at all, so a .NET launch on Linux used the host's fonts
and none of the lookalikes). `FontDirs` (or `CLEARCOTE_FONT_DIRS`, directories separated by `:`) adds your own
fonts, typically a copy of a Windows machine's `C:\Windows\Fonts`, ahead of the bundle. On an engine that lets a
genuine face win over its lookalike (the licensed build from r32) every family they provide renders as itself, and
Helvetica, Times, Courier and the CSS generics follow; an older engine keeps its lookalikes for the families a
persona lists and uses the directories only for characters nothing else covers. `CLEARCOTE_FALLBACK_FONT_DIRS`
adds fonts used only for characters nothing else covers (CJK, Indic scripts, emoji). A path that is not a directory
throws before the licence lease. Never list all of `/usr/share/fonts`: its Latin families change the widths a
Windows persona reports. A persona hides a family's name from CSS, but the browser can still draw characters that
only that font covers, so put only fonts in these directories that you are happy for a page to see. The font
configuration is written next to the browser binary; when that directory is not writable it goes to
`CLEARCOTE_CACHE/fontconfig` or `~/.cache/clearcote/fontconfig`, then to a private temp directory, and if none can
be written the SDK warns once. Ignored on Windows and macOS; refused for cloud and Docker launches. Only use fonts you
are licensed to use.

```csharp
var ctx = await Clearcote.Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions
{
    Fingerprint = "acct-1",
    Platform = "windows",
    FontDirs = new[] { "/srv/windows-fonts" },
});
```

## License

BSD-3-Clause. See [LICENSE](https://github.com/clearcotelabs/clearcote-browser/blob/main/LICENSE).
