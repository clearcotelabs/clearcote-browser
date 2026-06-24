<div align="center">

<img src="docs/assets/clyde.svg" alt="Clyde — the Clearcote chameleon" width="170" />

# Clearcote Browser

### Blend in. Stay clear.

**The open-source, verifiable anti-detect Chromium.** One coherent browser identity — controlled inside the engine, free forever, and a drop-in for Playwright/Puppeteer.

<br />

[![Release](https://img.shields.io/github/v/release/clearcotelabs/clearcote-browser?include_prereleases&label=release&style=flat-square&labelColor=07080a&color=38e0d6)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![Chromium](https://img.shields.io/badge/Chromium-149-6ee7ff?style=flat-square&labelColor=07080a)](https://www.chromium.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-a78bfa?style=flat-square&labelColor=07080a)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-38e0d6?style=flat-square&labelColor=07080a)](LICENSE)
[![Open source](https://img.shields.io/badge/100%25-open%20source-54d39b?style=flat-square&labelColor=07080a)](#license)
[![Discord](https://img.shields.io/badge/Discord-join%20the%20community-5865F2?style=flat-square&logo=discord&logoColor=white&labelColor=07080a)](https://discord.gg/WxvCjAnXZm)

<sub><i>Meet <b>Clyde</b> — chameleons blend in to stay unseen. So does your browser.</i></sub>

<sub>💬 Questions, ideas, or want to get involved? <b><a href="https://discord.gg/WxvCjAnXZm">Join us on Discord</a></b>.</sub>

</div>

> **Status:** [**v0.1.0-pre.13**](https://github.com/clearcotelabs/clearcote-browser/releases/tag/v0.1.0-pre.13) is live — Chromium 149, Windows x64, signed + checksummed ([verify it](docs/VERIFY.md)). Scopes the opt-in **[canvas bridge](docs/CANVAS-BRIDGE.md)** per origin (`--canvas-bridge-mode=off|all|allow|deny` + allow/deny lists, cold-miss `--canvas-bridge-fallback`, speculative prefetch) so you bridge only where it matters, and makes the **humanized cursor** (`Browser.humanizedClick`) move continuously — each path now starts from the last cursor position instead of snapping back to the corner between moves. **Windows x64 only for now**; macOS/Linux are on the [Roadmap](ROADMAP.md). An experimental pre-release.

---

## Why Clearcote — instead of the others?

Most "anti-detect" / stealth browsers are **closed, paid binaries** that rewrite your fingerprint with **injected JavaScript or CDP hooks** — brittle, self-revealing, and asking you to trust code you can't read. Clearcote inverts every one of those choices:

| | **Clearcote** | Typical anti-detect browser |
|---|---|---|
| **Source** | ✅ 100% open — every change is a readable patch | ❌ Closed binary |
| **Price** | ✅ **Free** | 💸 Paid subscription |
| **How signals change** | ✅ Compiled **into the C++ engine** — invisible to the page | ⚠️ Injected JS / CDP hooks (detectable artifacts) |
| **Coherence** | ✅ One seed → a whole consistent machine; the **JS identity and the real Chromium TLS/JA3/JA4 + HTTP/2 stack agree** | ⚠️ Per-surface values that disagree — with each other or with the network layer |
| **Trust model** | ✅ Signed, checksummed, **reproducible from source** | ❌ "Trust us" |
| **Automation** | ✅ **Drop-in Playwright / Puppeteer** — returns a standard `Browser` | ⚠️ Proprietary API / GUI profiles |
| **Real identities** | ✅ Import a real machine (or the curated [profile library](https://github.com/clearcotelabs/clearcote-profiles)) and **verify it loaded** | ⚠️ Rare / unverifiable |
| **Privacy** | ✅ De-Googled, **zero telemetry / phone-home** | ⚠️ Varies |

### The thing that matters most

Because the controls live **in the engine**, *the JavaScript a page sees and the network handshake underneath it come from one real Chromium.* There is **no spoofed-JS-over-real-TLS seam** for a cross-check to catch — the exact failure mode that gives injection-based tools away. One `--fingerprint` seed produces a single, internally consistent machine across **canvas, WebGL, audio, fonts, locale, hardware — and the TLS/HTTP-2 fingerprint underneath**.

And when the *noise itself* is the tell, switch it off (`fingerprintNoise: false`): canvas/WebGL/audio return their natural, unperturbed values while the identity spoof stays on. Your call, per session.

---

## What is Clearcote?

An open-source [Chromium](https://www.chromium.org/) distribution built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (Google services + telemetry removed) plus a transparent stack of source patches that move fingerprint control **into the engine**. Two promises:

- **A coherent, private identity** — one plausible machine per session instead of an accidentally hyper-unique one, coherent *down to the network layer* and across long-tail surfaces: WebGL `getParameter` limits, `navigator.getBattery()` / `connection` / `keyboard.getLayoutMap()`, AudioContext, `getScreenDetails()`, and CSS `@media`.
- **Radical verifiability** — no magic binary. Read every patch, rebuild it yourself, and confirm what you run matches what's published. **Don't trust us — [verify us](docs/VERIFY.md).**

It's a **drop-in for [Playwright](https://playwright.dev/) / [Puppeteer](https://pptr.dev/)** — the same APIs you already use, pointed at the Clearcote binary.

## What you control

From **one `--fingerprint` seed** *or* an **imported real-machine profile**, all kept coherent together:

- **Identity** — UA + UA-CH brand / platform / version + high-entropy hints (`bitness` / `wow64` / `model`); a real "Google Chrome" brand set, not bare "Chromium"
- **GPU** — WebGL unmasked vendor/renderer + the full `getParameter` table & extension list, **and WebGPU (`navigator.gpu`) limits/features kept coherent with that same GPU**; session-constant (never a per-origin tell)
- **Rendering** — deterministic per-site canvas / WebGL / audio noise, *or off* — plus an experimental **[real-GPU canvas bridge](docs/CANVAS-BRIDGE.md)** that renders on a real GPU host for hardware-accurate readbacks
- **Hardware & screen** — `hardwareConcurrency`, `deviceMemory`, **`storageQuota`** (a realistic on-disk size, not an incognito-looking one), screen geometry / depth / DPR + `getScreenDetails()`, a realistic `jsHeapSizeLimit`, touch points
- **Locale & network** — timezone + `navigator.languages` + `Accept-Language` + the **ICU / `Intl` locale all pinned to one language** (no `en-GB`-on-a-US-IP leak), geolocation, and a coherent WebRTC egress IP (no STUN/LAN leak) — all auto-matched to your proxy via `geoip`
- **Long-tail** — speech-synthesis voices, installed fonts, `MediaCapabilities.decodingInfo()` codecs, `enumerateDevices()`, CSS `@media` (pointer / hover / color-gamut), battery, connection, keyboard layout
- **Behavior** — humanized, *trusted* bezier mouse input that keeps `navigator.webdriver = false`

---

## Quickstart

Download the signed build from the **[Releases page](https://github.com/clearcotelabs/clearcote-browser/releases)**, unzip, and either run `chrome.exe` directly or drive it from stock Playwright:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=r"C:\clearcote\chrome.exe",
        args=["--fingerprint=seed-123", "--fingerprint-platform=windows"],
    )
    page = browser.new_page()
    page.goto("https://example.com")
    browser.close()
```

Same `--fingerprint=<seed>` ⇒ a stable identity across launches; a new seed ⇒ a fresh, unlinkable one.

### SDK — Playwright drop-in (Node + Python)

Published on **npm** and **PyPI**. Each `launch()` returns a standard Playwright `Browser` and auto-downloads + SHA-256-verifies the binary on first use, then caches it.

```bash
npm install clearcote      #  Node / TypeScript
pip install clearcote      #  Python
```

```javascript
import { launch } from "clearcote";

const browser = await launch({
  fingerprint: "user-7423",        // same seed ⇒ same identity, different ⇒ unlinkable
  platform: "windows",
  timezone: "America/New_York",
});
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

```python
from clearcote import launch

browser = launch(fingerprint="user-7423", platform="windows", timezone="America/New_York")
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

### Key SDK options

- **Match a proxy automatically** — `geoip: true` resolves the proxy's exit region and sets a coherent timezone + languages + `Accept-Language`:
  ```javascript
  await launch({ fingerprint: "u1", proxy: { server: "http://host:8080", username: "u", password: "p" }, geoip: true });
  ```
- **Human-like input + coherent WebRTC** — `humanize: true` moves the pointer along real, *trusted* bezier paths (engine-level, `webdriver` stays `false`); `webrtcIp` fabricates a coherent srflx candidate (no STUN/LAN leak).
- **Import a real machine** — adopt the *exact* identity of a real Chrome (GPU + `getParameter` table, screen, fonts, voices, audio). Grab one from the curated **[clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles)** library, capture your own with the [collector](tools/fingerprint-collect), or convert any record from the open [10k-profile dataset](https://github.com/Vinyzu/chrome-fingerprints):
  ```javascript
  await launch({ fingerprint: "u1", fingerprintProfile: "./profile.json" });
  ```
  …then **prove it loaded** with [`verify_profile.py`](tools/fingerprint-collect/verify_profile.py) (probes the live surfaces, prints a PASS/FAIL table).

Full option list: [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python).

### Drive a page with an AI agent

Clearcote ships an **in-browser AI agent**: it runs *inside* the browser process, perceives the live page, asks an LLM what to do, and executes steps as **real, trusted input** via Chrome's native Actor framework — not a synthetic-event shim. Point it at [OpenRouter](https://openrouter.ai) (default) and switch any model — GPT, Claude, Gemini, Llama — with one slug.

```javascript
import { launchAgent, runAgentTask } from "clearcote";

const ctx = await launchAgent({
  agentLlmKey: process.env.OPENROUTER_API_KEY,   // turns the agent on
  agentModel: "openai/gpt-4o-mini",
});
const page = ctx.pages()[0] ?? (await ctx.newPage());
await page.goto("https://news.ycombinator.com");
const result = await runAgentTask(page, "Open the top story and summarize it.", { maxSteps: 12 });
await ctx.close();
```

It combines naturally with the fingerprint spoofing and `humanize` input above — an agent that *looks human while it works*. (Python: `launch_agent()` + `run_agent_task()`.)

---

## Don't trust us — verify us

Every release is **GPG-signed, SHA-256-checksummed, and reproducible from source**. Build the Windows binary yourself (cross-compiled on a Linux host) in one command:

```bash
git clone https://github.com/clearcotelabs/clearcote-browser.git
cd clearcote-browser && WORK=~/clearcote-build ./build.sh
```

- **[docs/VERIFY.md](docs/VERIFY.md)** — verify a release: signature, checksums, reproducibility, and diffing the patch set against pinned upstream.
- **[docs/BUILDING.md](docs/BUILDING.md)** — full build-from-source guide.

Pin the **Clearcote release signing key** and check every download against it (it does not change between releases):

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

## Proof: fingerprint audit

Every build is audited with [`scripts/creepjs_audit.py`](scripts/creepjs_audit.py) — it reads the signals the browser actually exposes, cross-checks them for internal consistency (e.g. **UA vs UA-CH**), confirms the WebRTC mock leaks no LAN address, and checks it isn't flagged as headless/automated.

<!-- CREEPJS_RESULTS:START -->
**Build `149.0.7827.114` · audited 2026-06-18 · seed `demo` · platform `windows`**

| Signal | Value | Verdict |
|---|---|---|
| `navigator.webdriver` | False | ✅ hidden |
| User-Agent | `Chrome/149` | ✅ |
| UA-CH Chromium version | 149.0.7827.66 | ✅ matches UA |
| UA-CH platform | Windows 19.0.0 | ✅ |
| WebGL vendor / renderer | Google Inc. (Intel) / ANGLE (Intel, Intel(R) UHD Graphics 770 (0xA780) Direct3D11 … | ✅ spoofed |
| Canvas 2D | `1ca291c12d74236f` (deterministic per seed) | ✅ noised |
| hardwareConcurrency | 8 | ✅ |
| deviceMemory | 8 | ✅ |
| Timezone | America/New_York | ✅ |
| WebRTC host (LAN) candidate | none | ✅ no LAN leak |
| WebRTC srflx (public) | 203.0.113.45 | ✅ = mocked IP |
| Headless (hard) | 0% | ✅ |
| Stealth-detect | 0% | ✅ |

_UA ↔ UA-CH version consistency: ✅ (UA major `149`, UA-CH major `149`). WebRTC srflx mocked to the proxy/egress IP; real host candidates suppressed._
<!-- CREEPJS_RESULTS:END -->

> Spoofed per-seed identity (synthetic, not real machine data); a demo timezone and a documentation WebRTC IP are used so no real PII appears here. Regenerated each release.

---

## Credits

Clearcote stands on excellent open-source work: **[Chromium](https://www.chromium.org/)** (BSD-3), **[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)** (de-Googled base), **[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)** (engine-level fingerprint controls), **[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/)** (the per-site "farbling" model), and **[Camoufox](https://github.com/daijro/camoufox)** (a sibling open anti-detect browser). It's an **independent project** — not affiliated with or derived from any commercial product, and ships **no** proprietary code. Full attributions: [CREDITS.md](CREDITS.md).

## Roadmap · License · Responsible use

- **[ROADMAP.md](ROADMAP.md)** — what's next (macOS/Linux, more coherence, profile manager). ⭐ Star + watch to follow along.
- **License** — Clearcote's code and patches are **BSD-3-Clause** ([LICENSE](LICENSE)); upstream components keep their licenses ([CREDITS.md](CREDITS.md)).
- **Responsible use** — a privacy + automation tool for **lawful** purposes (privacy, QA/testing, research, authorized automation). You are responsible for how you use it; respect site terms and the law. Provided "as is." See [DISCLAIMER.md](DISCLAIMER.md).

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). The repo is laid out so **humans and automated contributors** alike can navigate and build it.

## Community

Questions, feedback, or want to get involved? **[Join the Clearcote Discord](https://discord.gg/WxvCjAnXZm).**
