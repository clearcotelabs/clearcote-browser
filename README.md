<div align="center">

<img src="docs/assets/clyde.svg" alt="Clyde — the Clearcote chameleon" width="150" />

# Clearcote Browser

### Blend in. Stay clear.

[![Release](https://img.shields.io/github/v/release/clearcotelabs/clearcote-browser?include_prereleases&label=release&style=flat-square&labelColor=07080a&color=38e0d6)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![Chromium](https://img.shields.io/badge/Chromium-150-6ee7ff?style=flat-square&labelColor=07080a)](https://www.chromium.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64%20%7C%20Linux%20x64-a78bfa?style=flat-square&labelColor=07080a)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![npm](https://img.shields.io/npm/v/clearcote?style=flat-square&logo=npm&logoColor=white&label=npm&labelColor=07080a&color=CB3837)](https://www.npmjs.com/package/clearcote)
[![PyPI](https://img.shields.io/pypi/v/clearcote?style=flat-square&logo=pypi&logoColor=white&label=pip&labelColor=07080a&color=3776AB)](https://pypi.org/project/clearcote/)
[![NuGet](https://img.shields.io/nuget/v/Clearcote?style=flat-square&logo=nuget&logoColor=white&label=nuget&labelColor=07080a&color=004880)](https://www.nuget.org/packages/Clearcote)
[![Docker](https://img.shields.io/docker/pulls/teamflatearth/clearcote?style=flat-square&logo=docker&logoColor=white&label=docker&labelColor=07080a&color=2496ED)](https://hub.docker.com/r/teamflatearth/clearcote)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-38e0d6?style=flat-square&labelColor=07080a)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?style=flat-square&logo=discord&logoColor=white&labelColor=07080a)](https://discord.gg/WxvCjAnXZm)
[![Copy for agent](https://img.shields.io/badge/Copy%20for%20agent-24292f?style=flat-square&logo=readme&logoColor=white)](https://raw.githubusercontent.com/clearcotelabs/clearcote-browser/main/AGENTS.md)
[![llms.txt](https://img.shields.io/badge/llms.txt-24292f?style=flat-square&logo=readme&logoColor=white)](https://raw.githubusercontent.com/clearcotelabs/clearcote-browser/main/llms.txt)

**Clearcote is an open-source stealth Chromium that stops your scrapers and browser agents from getting blocked.** Bot detectors flag automation by reading the browser fingerprint; Clearcote corrects that fingerprint **inside Chromium's C++** — so the browser presents as one ordinary, coherent Chrome install, all the way down to the TLS handshake. Point your existing **Playwright or Puppeteer** at it over the same API, and nothing else in your code changes.

<sub>**Blink · V8 · BoringSSL** patched in-tree · **ANGLE / D3D11**-backed WebGL · **JA3/JA4-coherent** TLS · **Windows + Linux** · signed, checksummed, **reproducible** releases</sub>

<table align="center"><tr>
<td align="center" width="150"><h3>32</h3><sub>single-surface<br/>C++ patches</sub></td>
<td align="center" width="150"><h3>0%</h3><sub>headless /<br/>stealth (audited)</sub></td>
<td align="center" width="160"><h3><code>[native&nbsp;code]</code></h3><sub>across every<br/>realm</sub></td>
<td align="center" width="150"><h3>BSD-3</h3><sub>open engine,<br/>free forever</sub></td>
</tr></table>

<sub><i>Meet <b>Clyde</b> — chameleons blend in to stay unseen. So does your browser.</i> · 💬 <a href="https://discord.gg/WxvCjAnXZm"><b>Join us on Discord</b></a></sub>

**🆕 The latest build is now free with GitHub for one browser at a time** — newest Chromium, recorded human motion and the private stealth patches. **[Get it free →](https://clearcotelabs.com/pricing#free)**

**☁️ Or skip the install: hosted Clearcote browsers** — one API call, a residential IP included, €1 per GB and nothing else. **[Hosted browsers →](#hosted-browsers--nothing-to-install)**

</div>

<table>
<tr>
<td width="33%" valign="top">

#### Native-code parity
Every spoofed getter *is* a C++ getter: `toString` returns `[native code]`, **realm-invariant** across the main frame, iframes, and Web Workers. No JavaScript shim to self-reveal.

</td>
<td width="33%" valign="top">

#### Coherent to the network
One real Chromium keeps the JS identity, the **UA / UA-CH** headers, and the **TLS JA3/JA4 + HTTP/2** stack in agreement. No spoofed-JS-over-real-TLS seam to catch.

</td>
<td width="33%" valign="top">

#### Drop-in Playwright / Puppeteer
`launch()` returns a **standard Playwright `Browser`**. Swap the executable, keep your code. Node, Python **and** .NET SDKs auto-download + SHA-256-verify the right binary per OS.

</td>
</tr>
</table>

> **What's new** — SDK `clearcote` **0.33.0**: a licensed launch no longer fails when another program on the same machine has started a browser with a newer licence token, typed capitals and symbols go through Shift like a real keyboard, and clearer launch warnings. The open build is **Chromium 150**: [v0.1.0-pre.23](https://github.com/clearcotelabs/clearcote-browser/releases/tag/v0.1.0-pre.23).
> In 150: a font list modelled on real Windows installs instead of one short list shared by every copy,
> separate WebGL1/WebGL2 extension lists with every advertised extension actually available,
> `deviceMemory` in the same steps real Chrome reports, touch-capable laptops that still report a mouse
> as the primary pointer, and `--disable-canvas-noise` / `--disable-gpu-string-spoof` as separate switches.
> Also recent: SOCKS5 proxy authentication (RFC 1929 — no local relay needed), portable profiles (copy a
> profile between machines with its cookies), unpacked **Chrome extension** loading incl. Manifest V2,
> client-hint headers that follow the persona, and locale/font coherence. Earlier surfaces remain:
> `serve()` CDP endpoint + `clearcote-mcp` + Docker, mobile/Android and Edge personas, TLS network
> persona, Widevine/EME, the per-origin [canvas bridge](docs/CANVAS-BRIDGE.md), real-fingerprint
> import, and the [stealth-coherence gate](docs/STEALTH-COHERENCE.md) that runs every release.
> Experimental pre-release.

---

## Contents

[What it is](#what-it-is) · [Quick start](#quick-start) · [Why patch the engine](#why-patch-the-engine-not-the-page) ·
[vs. the others](#why-clearcote-instead-of-the-others) · [Persona options](#configure-the-persona--what-you-control) ·
[AI agents](#drive-a-page-with-an-ai-agent) · [Verify](#proof--verify) · [Build](#build-from-source) ·
[Free with GitHub & Pro](#free-with-github-and-pro) · [Hosted browsers](#hosted-browsers--nothing-to-install) · [Reference](#reference)

---

## What it is

An open-source [Chromium](https://www.chromium.org/) distribution built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (Google services + telemetry removed) plus a transparent stack of **32 source patches** that move fingerprint control **into the engine**. Two promises:

- **A coherent, private identity** — one plausible machine per session instead of an accidentally hyper-unique one, coherent *down to the network layer* and across the long-tail surfaces detectors love: WebGL `getParameter` limits, `navigator.getBattery()` / `connection` / `keyboard.getLayoutMap()`, AudioContext, `getScreenDetails()`, and CSS `@media`.
- **Radical verifiability** — no magic binary. Read every patch, rebuild it yourself, and confirm what you run matches what's published.

It's a **drop-in for [Playwright](https://playwright.dev/) / [Puppeteer](https://pptr.dev/)** — the same APIs you already use, pointed at the Clearcote binary.

### The 12-second tour

```bash
pip install clearcote      # or:  npm install clearcote   ·   dotnet add package Clearcote
```
```python
from clearcote import launch

browser = launch(fingerprint="user-7423", platform="windows")   # returns a standard Playwright Browser
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

Same `fingerprint` seed ⇒ a stable identity across launches; a new seed ⇒ a fresh, unlinkable one. The SDK auto-downloads + SHA-256-verifies the right binary for your OS on first use, then caches it.

---

## Quick start

### SDK — Playwright drop-in

Published on **[npm](https://www.npmjs.com/package/clearcote)**, **[PyPI](https://pypi.org/project/clearcote/)** and **[NuGet](https://www.nuget.org/packages/Clearcote)**. Each `launch()` returns a standard Playwright `Browser` (a `Microsoft.Playwright IBrowser` in .NET).

```javascript
import { launch } from "clearcote";

const browser = await launch({
  fingerprint: "user-7423",         // same seed ⇒ same identity, different ⇒ unlinkable
  platform: "windows",              // "windows" | "linux" | "macos" | "android"
  brand: "Edge",                    // Chrome (default) | Edge — UA-CH + Sec-CH-UA kept coherent
  timezone: "America/New_York",
});
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

```python
from clearcote import launch
# inside an asyncio loop, use:  from clearcote.async_api import launch

browser = launch(fingerprint="user-7423", platform="windows", timezone="America/New_York")
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

```csharp
using Clearcote;

var browser = await Clearcote.Clearcote.LaunchAsync(new LaunchOptions {
    Fingerprint = "user-7423", Platform = "windows", Timezone = "America/New_York",
});
var page = await browser.NewPageAsync();      // a standard Microsoft.Playwright IBrowser
await page.GotoAsync("https://example.com");
await browser.CloseAsync();
```

**Match a proxy automatically** — `geoip: true` resolves the proxy's exit region and sets a coherent timezone + `navigator.languages` + `Accept-Language` + WebRTC egress:

```javascript
await launch({ fingerprint: "u1", proxy: { server: "http://host:8080", username: "u", password: "p" }, geoip: true });
```

Full option list: [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python) · [`sdk/dotnet`](sdk/dotnet).

### Direct — any CDP client

Download the signed build from the **[Releases page](https://github.com/clearcotelabs/clearcote-browser/releases)**, unzip, and drive `chrome` / `chrome.exe` from stock Playwright (or any CDP client) via `executable_path` + `--fingerprint` switches:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=r"C:\clearcote\chrome.exe",
        args=["--fingerprint=seed-123", "--fingerprint-platform=windows"],
    )
    page = browser.new_page(); page.goto("https://example.com"); browser.close()
```

**Or run a standing CDP endpoint** any existing framework attaches to unchanged — `connect_over_cdp`, `puppeteer.connect`, browser-use / Crawl4AI / Stagehand. It launches the binary directly (no `--enable-automation`), so `navigator.webdriver` stays `false` — stealthy by construction:

```bash
clearcote-serve --port 9222 --fingerprint seed-123 --platform windows   # prints http://127.0.0.1:9222
```
```python
from clearcote import serve
srv = serve(fingerprint="seed-123", platform="windows")   # -> srv.cdp_url; attach any CDP client
```

### Drive it from an AI agent (MCP) 🤖

Point Claude Desktop / Cursor / Cline at the **[Clearcote MCP server](mcp/)** — ~20 tools (`read_page`, `page_elements`, `click`, `fill_field`, `screenshot`, `save_profile`, `get_cdp_endpoint`, …) over one shared stealth browser. The persona is set via env, so the tool surface stays clean:

```json
{ "mcpServers": { "clearcote": { "command": "npx", "args": ["-y", "clearcote-mcp"],
    "env": { "CLEARCOTE_FINGERPRINT": "acct-1", "CLEARCOTE_PLATFORM": "windows" } } } }
```

### Run in Docker 🐧

**Official image — a stealth browser as a CDP endpoint.** Pull it and go; any **Playwright / Puppeteer / browser-use / Crawl4AI / Stagehand** client attaches over CDP, no code change:

```bash
docker run -d --rm -p 9222:9222 teamflatearth/clearcote      # CDP on http://localhost:9222
```
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")   # your code, unchanged
    page = browser.new_page(); page.goto("https://example.com"); print(page.title())
```

The image bakes in the signed Linux binary (SHA-256 verified), a base font set **and** the Windows metric-clone fonts, and defaults to a coherent **native Linux** persona. Configure it with env vars — `CC_PLATFORM` (`windows`/`linux`/`macos`/`android`), `CC_FINGERPRINT` (seed), `CC_BRAND` (`Edge`…), `CC_ACCEPT_LANGUAGE`, `CC_TIMEZONE`, `CC_TLS_PROFILE`:

```bash
docker run -d -p 9222:9222 -e CC_PLATFORM=windows -e CC_FINGERPRINT=user-7423 -e CC_BRAND=Edge teamflatearth/clearcote
```

> **Security:** the CDP endpoint is full browser control — publish it only to trusted networks (`-p 127.0.0.1:9222:9222` keeps it host-local). The [`docker/`](docker/) `Dockerfile` is auditable — rebuild + verify it yourself.

**Building your own image?** The full Dockerfile — runtime libraries, the base font set that keeps
canvas and text hashes coherent (the number-one Linux tell), and an SDK-driven entrypoint — is in
[`docker/`](docker/) and documented at [clearcotelabs.com/docs/deployment](https://www.clearcotelabs.com/docs/deployment).

> `--shm-size=1g` avoids `/dev/shm` crashes on heavy pages.

---

## Why patch the engine, not the page

The usual approach patches `navigator.webdriver`, spoofs the WebGL vendor, and overrides `navigator.plugins` **from script**. Detectors still flag it — and the reason is **structural**, not one more property left uncovered. A JavaScript spoof is a function standing where a native one belongs. A detector sets the returned value aside and interrogates whether the thing returning it is native:

| The tell | Why it catches a JS spoof |
|---|---|
| `toString` self-reveal | A native method stringifies to `function get vendor() { [native code] }`; an override stringifies to its own source — one `.toString()` catches it. |
| Descriptor / `hasOwnProperty` | `getOwnPropertyDescriptor` exposes redefined props, and `hasOwnProperty('toString')` returns `true` on a tampered function where a native one returns `false`. |
| Wrong-`this` `TypeError` | Native getters throw a specific `TypeError` on the wrong receiver; a naive shim stays quiet, and the silence is the signal. |
| **Realm re-acquisition** | A detector grabs a pristine `Function.prototype.toString` from a fresh iframe or Web Worker and turns it on your getter — a different realm from your main-world patch. It returns your source. Caught. |

Clearcote has **no such layer**. The getter for `navigator.vendor` **is** the C++ getter: it reports `[native code]` because it *is* native code, identical across every realm — main frame, iframe, and worker. There is no JavaScript hijacking to detect.

### The three layers of bot detection — and where Clearcote fits

Modern anti-bot systems read three structurally different surfaces, in three separate places. One tool rarely fixes all three:

| Layer | The tells | Where the fix lives | Clearcote |
|---|---|---|:--|
| **A · driver / binary artifacts** | `cdc_` ChromeDriver vars, the WebDriver protocol surface | Drive raw CDP, skip chromedriver | ✅ a plain Chromium binary — no driver artifacts |
| **B · CDP side-effects** | `Runtime.enable` leaks, injected init-scripts, main-world execution, automation-default viewport | The control / CDP-client layer | ✅ the SDK's launch defaults hold these back (isolated worlds, non-default viewport) |
| **C · fingerprint surface** | canvas, WebGL, audio, fonts, `navigator`, TLS — across main frame, iframes, workers | **The engine (C++)**, because JS overrides self-reveal (above) | ✅ **this is Clearcote** |

### The thing that matters most

Because the controls live **in the engine**, *the JavaScript a page sees and the network handshake underneath it come from one real Chromium.* There is **no spoofed-JS-over-real-TLS seam** for a cross-check to catch — the exact failure mode that gives injection-based tools away. One `--fingerprint` seed produces a single, internally consistent machine across canvas, WebGL, audio, fonts, locale, hardware — **and the TLS/HTTP-2 fingerprint underneath**. And when the *noise itself* is the tell, switch it off (`fingerprintNoise: false`): canvas/WebGL/audio return their natural values while the identity spoof stays on.

---

## Why Clearcote instead of the others?

Most "anti-detect" / stealth browsers are **closed, paid binaries** that rewrite your fingerprint with **injected JavaScript or CDP hooks** — brittle, self-revealing, and asking you to trust code you can't read. Clearcote inverts every one of those choices:

| | **Clearcote** | Typical anti-detect browser |
|---|---|---|
| **Source** | ✅ 100% open — every change is a readable patch | ❌ Closed binary |
| **Price** | ✅ **Free** | 💸 Paid subscription |
| **How signals change** | ✅ Compiled **into the C++ engine** — invisible to the page | ⚠️ Injected JS / CDP hooks (detectable artifacts) |
| **Coherence** | ✅ One seed → a whole consistent machine; the **JS identity and the real TLS/JA3/JA4 + HTTP/2 stack agree** | ⚠️ Per-surface values that disagree — with each other or with the network |
| **Trust model** | ✅ Signed, checksummed, **reproducible from source** | ❌ "Trust us" |
| **Automation** | ✅ **Drop-in Playwright / Puppeteer** — returns a standard `Browser` | ⚠️ Proprietary API / GUI profiles |
| **Real identities** | ✅ Import a real machine (or the curated [profile library](https://github.com/clearcotelabs/clearcote-profiles)) and **verify it loaded** | ⚠️ Rare / unverifiable |
| **Privacy** | ✅ De-Googled, **zero telemetry / phone-home** | ⚠️ Varies |

---

## Configure the persona — what you control

From **one `--fingerprint` seed** *or* an **imported real-machine profile**, all kept coherent together:

- **Identity** — UA + UA-CH brand / platform / version + high-entropy hints (`bitness` / `wow64` / `model`); a real "Google Chrome" **or "Microsoft Edge"** brand set — JS `navigator.userAgentData` and the HTTP `Sec-CH-UA` headers aligned.
- **GPU** — WebGL unmasked vendor/renderer + the full `getParameter` table & extension list, **and WebGPU (`navigator.gpu`) limits/features kept coherent with that same GPU**; session-constant.
- **Rendering** — deterministic per-site canvas / WebGL / audio noise, *or off* — plus an experimental **[real-GPU canvas bridge](docs/CANVAS-BRIDGE.md)** that renders on a real GPU host for hardware-accurate readbacks.
- **Fonts** — the claimed OS's font families render **present with correct advance widths** (metric-compatible clones bundled with the Linux release), so a Windows persona on a Linux server has no absent-font or wrong-width tell.
- **Hardware & screen** — `hardwareConcurrency`, `deviceMemory`, `storageQuota`, screen geometry / depth / DPR + `getScreenDetails()`, a realistic `jsHeapSizeLimit`, touch points.
- **Locale & network** — timezone + `navigator.languages` + `Accept-Language` + the **ICU / `Intl` locale all pinned to one language**, geolocation, a coherent WebRTC egress IP (no STUN/LAN leak), and the **TLS/HTTP-2** shape following the claimed Chrome version — all auto-matched to your proxy via `geoip`.
- **Long-tail** — speech-synthesis voices, installed fonts, `MediaCapabilities.decodingInfo()` codecs, `enumerateDevices()`, CSS `@media` (pointer / hover / color-gamut), battery, connection, keyboard layout.
- **Behavior** — humanized, *trusted* bezier mouse input that keeps `navigator.webdriver = false`.

**Import a real machine** — adopt the *exact* identity of a real Chrome (GPU + `getParameter` table, screen, fonts, voices, audio). Grab one from the curated **[clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles)** library or capture your own with the [collector](tools/fingerprint-collect) — then **prove it loaded** with [`verify_profile.py`](tools/fingerprint-collect/verify_profile.py).

---

## Drive a page with an AI agent

Clearcote ships an **in-browser AI agent**: it runs *inside* the browser process, perceives the live page, asks an LLM what to do, and executes steps as **real, trusted input** via Chrome's native Actor framework — not a synthetic-event shim. Point it at [OpenRouter](https://openrouter.ai) (default) and switch any model with one slug.

```javascript
import { launchAgent, runAgentTask } from "clearcote";

const ctx = await launchAgent({ agentLlmKey: process.env.OPENROUTER_API_KEY, agentModel: "openai/gpt-4o-mini" });
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto("https://news.ycombinator.com");
const result = await runAgentTask(page, "Open the top story and summarize it.", { maxSteps: 12 });
await ctx.close();
```

It combines naturally with the fingerprint spoofing and `humanize` input above — an agent that *looks human while it works*. (Python: `launch_agent()` + `run_agent_task()`.)

---

## Proof & verify

### Don't trust us — verify us

Every release is **GPG-signed, SHA-256-checksummed, and reproducible from source**. Pin the **Clearcote release signing key** (it does not change between releases) and check every download against it:

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

- **[docs/VERIFY.md](docs/VERIFY.md)** — verify a release: signature, checksums, reproducibility, and diffing the patch set against pinned upstream.
- **[docs/STEALTH-COHERENCE.md](docs/STEALTH-COHERENCE.md)** — the regression gate that launches the shipped binary on every release.

---

## Build from source

Build the Windows (cross-compiled) or Linux (native) binary yourself on a Linux host:

```bash
git clone https://github.com/clearcotelabs/clearcote-browser.git
cd clearcote-browser && WORK=~/clearcote-build ./build.sh
```

- **[docs/BUILDING.md](docs/BUILDING.md)** — full build-from-source guide · **[patches/](patches/)** — the 37 diffs · **[docs/PATCHES.md](docs/PATCHES.md)** — what each one does.

## Free with GitHub, and Pro

Clearcote is free and open source, and the open build always will be — **fully functional, reproducible from source, no account.** Beyond it there is one licensed build, and two ways to run it:

| | **Open source** | **Free with GitHub** | **Pro — $49/month** |
|---|---|---|---|
| Account | None | GitHub account, at least 30 days old | Clearcote account |
| Build | Open, reproducible | Latest licensed build | Latest licensed build |
| New Chromium majors | ~2 months after release | The day they ship | The day they ship |
| Recorded human motion, private stealth patches, profile library | — | ✅ | ✅ |
| Older builds and version pinning | Open builds | ✅ | ✅ |
| Browsers at once | Unlimited | **1** | Up to 250, more on request |
| Support | GitHub issues | GitHub issues | Email from the owner |

**Free with GitHub:** sign in at [clearcotelabs.com](https://clearcotelabs.com/pricing#free) with GitHub, open **Licenses** in the dashboard and click **Get it free**. The key lasts 30 days and renews for free. It counts every browser, so a second one waits until the first closes, and it always runs the latest build — picking an older build or the preview channel is a Pro feature. It needs **SDK 0.30.0 or newer** (`pip install -U clearcote`, `npm i clearcote@latest`, or the latest NuGet package); an older SDK is refused, because the browser expects the SDK to keep the licence current while it runs.

**Pro** is for running many browsers at once, and it funds the work. Let me be blunt about that work: keeping a Chromium fork current — tracking upstream, porting the anti-detection patches to every new Chromium version, testing, and building for Windows and Linux — is a lot of ongoing effort, mostly mine. You also get **direct email support from me, the owner.**

**What the licensed build is:** stealth work I keep private so it isn't trivially copied, which means the licensed binary is ***not* reproducible from public source** — unlike the open build, which is, and stays that way. Concretely: real recorded human mouse trajectories (the open build uses synthetic bézier paths), coalesced pointer samples, a coherent WebRTC server-reflexive candidate, host-candidate concealment, and request-header hygiene on revalidation.

**What it is not:** it does not unlock more spoofing. The whole identity surface — personas, canvas/WebGL/audio farbling, all 18 native metadata overrides, `light_stealth`, TLS profiles, humanized input — is in the open build, in full. The per-feature table lives in the SDK READMEs ([Node](sdk/node/README.md#whats-in-each-tier) / [Python](sdk/python/README.md#whats-in-each-tier)), mirroring `site/lib/tiers.ts`.

**→ [Get it free with GitHub](https://clearcotelabs.com/pricing#free) · [Get Pro](https://clearcotelabs.com/pricing)**

## Hosted browsers — nothing to install

The same engine also runs on our servers. One API call starts a Clearcote browser and returns a CDP WebSocket URL; your existing Playwright or Puppeteer code connects to it exactly as it connects to a local browser.

```javascript
const { connectUrl } = await fetch("https://www.clearcotelabs.com/api/v1/browsers", {
  method: "POST",
  headers: { authorization: "Bearer cc_live_...", "content-type": "application/json" },
  body: JSON.stringify({ identity: "account-1", country: "us" }),
}).then((r) => r.json());

const browser = await chromium.connectOverCDP(connectUrl);   // your Playwright code from here
```

Or let the SDK do it: the same `launch()` that starts a local Clearcote starts a hosted one with one
flag, and returns the same Playwright `Browser` (`CLEARCOTE_CLOUD=1` flips existing code without an edit):

```python
from clearcote import launch                    # Node: launch({ cloud: true, country: "us" })
browser = launch(cloud=True, country="us")      # CLEARCOTE_API_KEY=cc_live_...
```

The SDKs' `Cloud` client covers the rest of the hosted API: agent runs that take a task and return JSON,
cookie sync into cloud profiles, recordings, the event timeline, hand-off to a person and signed webhooks.
See "Local or cloud" in the [Python](sdk/python/README.md#local-or-cloud) and
[Node](sdk/node/README.md#local-or-cloud) SDK READMEs.

- **A residential IP, included.** Every session leaves through a real home connection. Pick a `country`, `state` or `city`, or leave it to us.
- **The Clearcote engine on every session.** Fingerprint control compiled in and on by default: one tier, nothing to upgrade to.
- **Identities that stay put.** The same `identity` label comes back as the same device on the same exit IP in every later session; timezone and language follow the exit IP on their own.
- **Dedicated physical servers,** not shared cloud virtual machines.
- **€1 per GB of traffic, and nothing else.** No plan, no per-hour clock, no separate proxy bill. Prepaid from €5, and `maxGb` caps any session.
- **A Playground in the dashboard** runs a script in a cloud browser with a live view, console and screenshots side by side, then hands you the same session as code.

| | **Clearcote hosted** | Most hosted browsers |
|---|---|---|
| **The browser** | The Clearcote engine, the same build you can run yourself | Stock Chromium with stealth injected on top |
| **Residential IPs** | Included in the one price | Metered separately, typically $5–12 per GB on top |
| **Stealth** | On by default, one tier | Basic on entry plans, the full version an enterprise upsell |
| **The bill** | Traffic only: no plan, no clock | Monthly plan + browser-hours + proxy GB |
| **The machine** | Dedicated physical servers | Shared cloud virtual machines |

Accounts that sign in with a GitHub account at least 30 days old get a one-time welcome credit to try it. **→ [Hosted browser docs](https://www.clearcotelabs.com/docs/hosted-browsers) · [Try the Playground](https://www.clearcotelabs.com/dashboard/playground)**

## Build availability

Which Chromium majors are shipped or in progress, and when each tier gets them:

| Chromium | Status | Free with GitHub & Pro | Open source |
|---|---|---|---|
| **153** (`153.0.8010.53`) | ✅ Available | ✅ **Available now** | ~Nov 2026 |
| **152** (`152.0.7977.82`) | ✅ Available | ✅ Available | ~Nov 2026 |
| **151** (`151.0.7922.108`) | ✅ Available | ✅ Available | ~Oct 2026 |
| **150** (`150.0.7871.114`) | ✅ Available | ✅ Available | ✅ **Available now** |
| **149** (`149.0.7827.114`) | ✅ Available | ✅ Available | ✅ Available |

*A new major reaches the licensed build (Free with GitHub and Pro) the day it's built; the open build gets
that same fully open, reproducible major roughly two months later — the open-build dates above are estimates
on that cadence, not promises. Pick one with the SDK — `launch(version="153", license_key=...)` with a key,
`version="150"` without — or omit it for the latest your key allows. Earlier Pro majors stay
selectable. Requires SDK ≥ 0.16.0.*

## Reference

| | |
|---|---|
| **SDK options** | [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python) · [`sdk/dotnet`](sdk/dotnet) |
| **Docs** | [VERIFY](docs/VERIFY.md) · [BUILDING](docs/BUILDING.md) · [CANVAS-BRIDGE](docs/CANVAS-BRIDGE.md) · [STEALTH-COHERENCE](docs/STEALTH-COHERENCE.md) · [PATCHES](docs/PATCHES.md) |
| **For agents** | [AGENTS.md](AGENTS.md) · [llms.txt](llms.txt) |
| **Profiles** | [clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles) library · [collector](tools/fingerprint-collect) |
| **Roadmap** | [ROADMAP.md](ROADMAP.md) — macOS, ARM64, more coherence |

---

## Credits · License · Responsible use

Clearcote stands on excellent open-source work: **[Chromium](https://www.chromium.org/)** (BSD-3),
**[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)** (de-Googled base),
**[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)** (engine-level fingerprint
controls), **[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/)** (the per-site
"farbling" model), and **[Camoufox](https://github.com/daijro/camoufox)** (a sibling open anti-detect
browser). It's an **independent project** — not affiliated with or derived from any commercial
product, and ships **no** proprietary code. Full attributions: [CREDITS.md](CREDITS.md).

Clearcote's code and patches are **BSD-3-Clause** ([LICENSE](LICENSE)); upstream components keep their
licenses. It's a privacy and automation tool for **lawful** purposes — privacy, QA and testing,
research, authorized automation. Respect site terms and the law. Provided "as is"
([DISCLAIMER.md](DISCLAIMER.md)).

What's next is in [ROADMAP.md](ROADMAP.md) — macOS, ARM64, more coherence. Contributions welcome
([CONTRIBUTING.md](CONTRIBUTING.md) · [AGENTS.md](AGENTS.md)), and questions are welcome in the
**[Discord](https://discord.gg/WxvCjAnXZm)**.

---

## Star History

<a href="https://www.star-history.com/?repos=clearcotelabs%2Fclearcote-browser&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&theme=dark&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
 </picture>
</a>
