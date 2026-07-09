<div align="center">

<img src="docs/assets/clyde.svg" alt="Clyde — the Clearcote chameleon" width="150" />

# Clearcote Browser

### Blend in. Stay clear.

[![Release](https://img.shields.io/github/v/release/clearcotelabs/clearcote-browser?include_prereleases&label=release&style=flat-square&labelColor=07080a&color=38e0d6)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![Chromium](https://img.shields.io/badge/Chromium-149-6ee7ff?style=flat-square&labelColor=07080a)](https://www.chromium.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64%20%7C%20Linux%20x64-a78bfa?style=flat-square&labelColor=07080a)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![npm](https://img.shields.io/npm/v/clearcote?style=flat-square&logo=npm&logoColor=white&label=npm&labelColor=07080a&color=CB3837)](https://www.npmjs.com/package/clearcote)
[![PyPI](https://img.shields.io/pypi/v/clearcote?style=flat-square&logo=pypi&logoColor=white&label=pip&labelColor=07080a&color=3776AB)](https://pypi.org/project/clearcote/)
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

</div>

<table>
<tr>
<td width="33%" valign="top">

#### Native-code parity
Every spoofed getter *is* a C++ getter: `toString` returns `[native code]`, **realm-invariant** across the main frame, iframes, and Web Workers. No JavaScript shim to self-reveal.

</td>
<td width="33%" valign="top">

#### Coherent to the network
One real Chromium keeps the JS identity, the **UA / UA-CH** headers, and the **TLS JA3/JA4 + HTTP/2** stack in agreement. No spoofed-JS-over-real-TLS seam for a cross-check to catch.

</td>
<td width="33%" valign="top">

#### Drop-in Playwright / Puppeteer
`launch()` returns a **standard Playwright `Browser`**. Swap the executable, keep your code. Node **and** Python SDKs auto-download + SHA-256-verify the right binary per OS.

</td>
</tr>
<tr>
<td width="33%" valign="top">

#### One seed → one machine
A single `--fingerprint` seed derives a whole **internally-consistent** identity — canvas, WebGL, audio, fonts, locale, hardware, GPU — stable across launches, unlinkable across seeds. Or import a **real** machine.

</td>
<td width="33%" valign="top">

#### Auditable patches
**32** small single-purpose diffs in [`patches/`](patches/). Read one in a minute; rebuild the engine with one script. No opaque binary, no phone-home.

</td>
<td width="33%" valign="top">

#### Don't trust us — verify us
Every release is **GPG-signed, checksummed, and reproducible from source**. Rebuild it yourself and diff the hash. Trust the math, not the vendor.

</td>
</tr>
</table>

> **🆕 What's new — [v0.1.0-pre.21](https://github.com/clearcotelabs/clearcote-browser/releases/tag/v0.1.0-pre.21) + SDK `clearcote` 0.15.0.** **Network request-header hygiene:** under a fingerprint persona the engine no longer emits redundant `Cache-Control` / `Pragma` request headers on navigations and reloads, matching a real Chrome cold navigation (the effective cache mode is unchanged; with no persona, stock behavior is preserved). **Humanized cursor — seeded motion model:** the SDK's humanizer now drives a shared, persona-seeded trajectory/timing core (minimum-jerk submovements, Fitts-law duration, colored noise, endpoint dwells) that is bit-identical across the Python **and** Node SDKs — one fingerprint ⇒ one stable motor identity — with an offline motion-score validator. Prior surfaces remain: locale coherence, `serve()` stealthy CDP endpoint + `clearcote-mcp` + Docker, render-vs-string **font coherence**, mobile/Android persona, **Edge** coherence, **TLS network persona** (`tlsProfile`), **Widevine / EME (DRM)**, the per-origin **[canvas bridge](docs/CANVAS-BRIDGE.md)**, real-fingerprint import, and the **[stealth-coherence gate](docs/STEALTH-COHERENCE.md)** that runs every release. Experimental pre-release.

---

## Contents

- [What it is](#what-it-is) · [The 12-second tour](#the-12-second-tour)
- [Quick start](#quick-start) — [SDK](#sdk--playwright-drop-in) · [Direct](#direct--any-cdp-client) · [Docker](#run-in-docker-)
- [Why patch the engine, not the page](#why-patch-the-engine-not-the-page)
- [Why Clearcote instead of the others](#why-clearcote-instead-of-the-others)
- [Configure the persona](#configure-the-persona--what-you-control)
- [Drive a page with an AI agent](#drive-a-page-with-an-ai-agent)
- [Proof & verify](#proof--verify)
- [Build from source](#build-from-source) · [Reference](#reference) · [Credits · License](#credits)

---

## What it is

An open-source [Chromium](https://www.chromium.org/) distribution built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (Google services + telemetry removed) plus a transparent stack of **32 source patches** that move fingerprint control **into the engine**. Two promises:

- **A coherent, private identity** — one plausible machine per session instead of an accidentally hyper-unique one, coherent *down to the network layer* and across the long-tail surfaces detectors love: WebGL `getParameter` limits, `navigator.getBattery()` / `connection` / `keyboard.getLayoutMap()`, AudioContext, `getScreenDetails()`, and CSS `@media`.
- **Radical verifiability** — no magic binary. Read every patch, rebuild it yourself, and confirm what you run matches what's published.

It's a **drop-in for [Playwright](https://playwright.dev/) / [Puppeteer](https://pptr.dev/)** — the same APIs you already use, pointed at the Clearcote binary.

### The 12-second tour

```bash
pip install clearcote      # or:  npm install clearcote
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

Published on **[npm](https://www.npmjs.com/package/clearcote)** and **[PyPI](https://pypi.org/project/clearcote/)**. Each `launch()` returns a standard Playwright `Browser`.

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

**Match a proxy automatically** — `geoip: true` resolves the proxy's exit region and sets a coherent timezone + `navigator.languages` + `Accept-Language` + WebRTC egress:

```javascript
await launch({ fingerprint: "u1", proxy: { server: "http://host:8080", username: "u", password: "p" }, geoip: true });
```

Full option list: [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python).

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

**Or build your own image** (SDK-driven, run your own script). Clearcote ships a **Linux x64** binary, so it runs headless in a container. The image needs the browser's runtime libraries, a **base font set** (so canvas/text hashes are coherent — the #1 Linux tell), and the SDK. On Linux the persona defaults to a coherent **native Linux** identity; WebRTC leak-proofing and Privacy-Sandbox-disable are on by default.

```dockerfile
FROM node:20-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      xz-utils libnss3 libnspr4 libgbm1 libasound2 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libxfixes3 libxext6 libxrender1 \
      libpango-1.0-0 libcairo2 libx11-6 libxcb1 libexpat1 libdbus-1-3 ca-certificates \
      fontconfig fonts-liberation fonts-noto-color-emoji fonts-unifont fonts-ipafont-gothic fonts-wqy-zenhei \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN npm i clearcote
RUN node --input-type=module -e "import { download } from 'clearcote'; await download();"   # bake the binary in
COPY run.mjs .
CMD ["node", "run.mjs"]
```

```javascript
// run.mjs — one coherent Linux persona, every stealth surface on
import { launchPersistentContext } from "clearcote";
const ctx = await launchPersistentContext("/tmp/prof", {
  headless: true,
  fingerprint: "user-1",
  proxy: { server: "http://gateway:8080", username: "u", password: "p" },
  geoip: true,          // timezone + languages + WebRTC IP matched to the proxy exit
  humanize: true,       // real, trusted bezier input; navigator.webdriver stays false
  args: ["--no-sandbox"],
});
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto("https://example.com");
await ctx.close();
```

> `--shm-size=1g` avoids `/dev/shm` crashes on heavy pages. Python is identical (`from clearcote import launch_persistent_context`, `snake_case` options).

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

### Per-build fingerprint audit

Every build is audited with [`scripts/creepjs_audit.py`](scripts/creepjs_audit.py) — it reads the signals the browser actually exposes, cross-checks them for internal consistency (e.g. **UA vs UA-CH**), confirms the WebRTC mock leaks no LAN address, and checks it isn't flagged as headless/automated.

<!-- CREEPJS_RESULTS:START -->
**Build `149.0.7827.114` · seed `demo` · platform `windows`**

| Signal | Value | Verdict |
|---|---|---|
| `navigator.webdriver` | False | ✅ hidden |
| User-Agent ↔ UA-CH | `Chrome/149` ↔ `149.0.7827.x` | ✅ consistent |
| UA-CH platform | Windows 19.0.0 | ✅ |
| WebGL vendor / renderer | Google Inc. (Intel) / ANGLE (Intel, Intel(R) UHD Graphics 770 … Direct3D11 …) | ✅ spoofed |
| Canvas 2D | deterministic per seed | ✅ noised |
| Timezone | America/New_York | ✅ |
| WebRTC host (LAN) candidate | none | ✅ no LAN leak |
| WebRTC srflx (public) | = mocked egress IP | ✅ |
| Headless (hard) / Stealth-detect | 0% / 0% | ✅ |
<!-- CREEPJS_RESULTS:END -->

Beyond the per-build audit, Clearcote is exercised against **independent, third-party detection services** and comes back clean across every category below. *Service names are omitted by policy; the categories are what matter.*

| Detection category | What it verifies | Result |
|---|---|:--|
| 🤖 **Webdriver / headless suites** | `navigator.webdriver`, headless heuristics, plugin / UA tells | ✅ Hidden · normal headful Chrome |
| 🧩 **Automation-framework leaks** | CDP `Runtime.enable` leak, injected init-scripts, main-world execution | ✅ No leaks · isolated world |
| 🔒 **TLS / JA4 client fingerprint** | Handshake matches a real Chrome — no spoofed-JS-over-tooling-TLS seam | ✅ Genuine Chrome 149 JA4 |
| 📡 **WebRTC leak tests** | STUN / host candidates exposing a real LAN or ISP IP | ✅ Only the egress IP |
| 🎨 **Canvas / WebGL / fonts / audio** | Per-surface fingerprints render coherently and deterministically per seed | ✅ Coherent GPU + font metrics |
| 🌐 **Locale / timezone coherence** | JS `Intl` / timezone ↔ `navigator.languages` ↔ network egress all agree | ✅ Aligned end-to-end via `geoip` |

> **Honest scope.** This is an **experimental pre-release**. The above is open-source, adversarial *coherence* auditing (a persona measured against a real Chrome on the same probe) — **not** published pass-rates against commercial services. And detection is only half the picture: a clean fingerprint over a **burned proxy IP** can still be blocked on IP reputation alone. Treat IP quality as a separate axis from browser identity.

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

- **[docs/BUILDING.md](docs/BUILDING.md)** — full build-from-source guide · **[patches/](patches/)** — the 32 diffs · **[docs/PATCHES.md](docs/PATCHES.md)** — what each one does.

## Reference

| | |
|---|---|
| **SDK options** | [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python) |
| **Docs** | [VERIFY](docs/VERIFY.md) · [BUILDING](docs/BUILDING.md) · [CANVAS-BRIDGE](docs/CANVAS-BRIDGE.md) · [STEALTH-COHERENCE](docs/STEALTH-COHERENCE.md) · [PATCHES](docs/PATCHES.md) |
| **For agents** | [AGENTS.md](AGENTS.md) · [llms.txt](llms.txt) |
| **Profiles** | [clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles) library · [collector](tools/fingerprint-collect) |
| **Roadmap** | [ROADMAP.md](ROADMAP.md) — macOS, ARM64, more coherence |

---

## Credits

Clearcote stands on excellent open-source work: **[Chromium](https://www.chromium.org/)** (BSD-3), **[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)** (de-Googled base), **[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)** (engine-level fingerprint controls), **[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/)** (the per-site "farbling" model), and **[Camoufox](https://github.com/daijro/camoufox)** (a sibling open anti-detect browser). It's an **independent project** — not affiliated with or derived from any commercial product, and ships **no** proprietary code. Full attributions: [CREDITS.md](CREDITS.md).

## Roadmap · License · Responsible use

- **[ROADMAP.md](ROADMAP.md)** — what's next (macOS, ARM64, more coherence, profile manager). ⭐ Star + watch to follow along.
- **License** — Clearcote's code and patches are **BSD-3-Clause** ([LICENSE](LICENSE)); upstream components keep their licenses ([CREDITS.md](CREDITS.md)).
- **Responsible use** — a privacy + automation tool for **lawful** purposes (privacy, QA/testing, research, authorized automation). Respect site terms and the law. Provided "as is." See [DISCLAIMER.md](DISCLAIMER.md).

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## Community

Questions, feedback, or want to get involved? **[Join the Clearcote Discord](https://discord.gg/WxvCjAnXZm).**

---

## Star History

<a href="https://www.star-history.com/?repos=clearcotelabs%2Fclearcote-browser&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&theme=dark&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=clearcotelabs/clearcote-browser&type=date&legend=top-left&sealed_token=uiWIVjXO781jFWSbU622576w1qicxtE9c7h7KwDue1SAX34vcnVbYMSeelttKoASKjl2v1ILrc1Bdd17aRXWAsZjFmEPMGr9j2OTJmyyEuB3i7YC-ke8sQ" />
 </picture>
</a>
