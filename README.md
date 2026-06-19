<div align="center">

<img src="docs/assets/clyde.svg" alt="Clyde — the Clearcote chameleon" width="170" />

# Clearcote Browser

### Blend in. Stay clear.

**A fully open, fully auditable Chromium build for a consistent, private browser identity.**

No opaque binaries. No phone-home. Every change is a readable patch, every build is reproducible from source.

<br />

[![Release](https://img.shields.io/github/v/release/clearcotelabs/clearcote-browser?include_prereleases&label=release&style=flat-square&labelColor=07080a&color=38e0d6)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![Chromium](https://img.shields.io/badge/Chromium-149-6ee7ff?style=flat-square&labelColor=07080a)](https://www.chromium.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-a78bfa?style=flat-square&labelColor=07080a)](https://github.com/clearcotelabs/clearcote-browser/releases)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-38e0d6?style=flat-square&labelColor=07080a)](LICENSE)
[![Open source](https://img.shields.io/badge/100%25-open%20source-54d39b?style=flat-square&labelColor=07080a)](#license)

<sub><i>Meet <b>Clyde</b> — chameleons blend in to stay unseen. So does your browser.</i></sub>

</div>

> [!NOTE]
> **Status: latest build is live — [v0.1.0-pre.8](https://github.com/clearcotelabs/clearcote-browser/releases/tag/v0.1.0-pre.8) (Chromium 149, Windows x64).**
> A signed, checksummed pre-release build is available now on the [Releases page](https://github.com/clearcotelabs/clearcote-browser/releases) — download it, [verify it](docs/VERIFY.md), and run it. This build adds **fingerprint profile import**: capture a real Chrome's identity with the [collector](tools/fingerprint-collect) (or convert a record from a 10k-profile open dataset) and pass it via `--fingerprint-profile` / the SDK's `fingerprint_profile` option, so the browser presents *that machine's* GPU, screen, fonts, speech voices, audio, and WebGL — fields you don't import fall back to the `--fingerprint` seed, so partial profiles stay coherent. Builds on the earlier fingerprint-coherence pass and still **passes open-source fingerprint auditors** (`navigator.webdriver=false`, 0% headless / 0% stealth) on real Windows — still an experimental pre-release; see the [Roadmap](ROADMAP.md) for what's next.

> [!IMPORTANT]
> **Windows x64 only for now.** That's the single platform we're focused on today — the build, the binary, and the [npm/PyPI SDKs](#quickstart) are all Windows-first. macOS and Linux are on the [Roadmap](ROADMAP.md) but **not yet available**.

---

## What is Clearcote?

Clearcote is an open-source [Chromium](https://www.chromium.org/) distribution with two goals:

1. **A coherent, private browser identity.** A stock browser quietly exposes a unique, trackable fingerprint — canvas, WebGL, audio, fonts, locale, hardware, and more. Clearcote moves control of those signals **into the engine itself**, so a session presents one consistent, plausible identity instead of an accidental, hyper-unique one.
2. **Radical verifiability.** Clearcote is built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (Google integration and telemetry removed) and a transparent stack of source patches. There is **no magic binary** — you can read every change, rebuild it yourself, and check that what you run matches what's published.

The identity is also **coherent across secondary surfaces**, not just the obvious ones: WebGL `getParameter` limits report the canonical ANGLE/D3D11 values a real Windows GPU returns and the unmasked renderer/vendor stay **session-constant** (one GPU on every site, never a per-origin tell); `navigator.getBattery()`, `navigator.connection`, and `navigator.keyboard.getLayoutMap()` report a coherent desktop; AudioContext, `getScreenDetails()`, and CSS `(pointer: fine)` / `(hover: hover)` match a real Chrome-on-Windows machine. Validated on real Windows against open-source fingerprint auditors: `navigator.webdriver=false`, 0% headless / 0% stealth, stable per-seed surfaces.

It's designed as a **drop-in for standard browser automation** ([Playwright](https://playwright.dev/) / [Puppeteer](https://pptr.dev/)): same APIs you already use, just pointed at the Clearcote binary.

## Why it exists

- **Privacy is the default, not an add-on.** De-Googled base, engine-level signal control, no built-in tracking or update beacons.
- **Trust through transparency.** The "stealth browser" space is full of closed binaries that ask for blind faith. Clearcote takes the opposite stance: **don't trust us — verify us.** See [VERIFY.md](docs/VERIFY.md).
- **Built for builders.** Clean repo layout, scriptable builds, and an [AGENTS.md](AGENTS.md) so humans *and* automated tooling can navigate and contribute.
- **It's just Chromium.** Native Playwright/Puppeteer support, a real Chromium network/render stack, and the ecosystem you already know.

## Principles

| Principle | What it means in practice |
|---|---|
| **Open by default** | 100% open source. Every patch is human-readable. No proprietary blobs in the tree. |
| **Engine-level, not script injection** | Identity/signal controls are compiled into the binary, not bolted on via injected JavaScript that's brittle and self-revealing. |
| **Coherent identity** | Signals are controlled *together* so they stay internally consistent and stable per site — inspired by Brave's per-site "farbling" model — rather than random values that don't add up. |
| **Reproducible & verifiable** | Pinned upstream revision, deterministic patch set, checksummed artifacts, build-from-source instructions anyone can follow. |
| **Responsible by design** | Built for privacy, testing, and lawful automation. See [Responsible Use](#responsible-use). |

## How it works

Clearcote is a thin, transparent stack over Chromium:

```
            Chromium  (Google, BSD-3)
                │
   ungoogled-chromium  →  removes Google services, telemetry, and integration
                │
       Clearcote patches  →  engine-level identity & privacy controls
                │
   reproducible build  →  checksummed, attestable, rebuildable by anyone
                │
        Clearcote Browser  +  automation SDK (Playwright / Puppeteer drop-in)
```

Nothing here is hidden: the de-Googling comes from ungoogled-chromium, the identity controls live in readable patches, and the build is something you can run end-to-end yourself.

## 🧪 Fingerprint test results

Every build is audited with [`scripts/creepjs_audit.py`](scripts/creepjs_audit.py): it reads the signals the browser actually exposes, cross-checks them for internal consistency (e.g. **User-Agent vs. UA Client Hints**), confirms the WebRTC mock leaks no LAN address, and checks it isn't flagged as headless/automated. Latest build:

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
_Regenerate with `py -3 creepjs_audit.py --readme clearcote-browser/README.md` on each release._
<!-- CREEPJS_RESULTS:END -->

> Regenerated on each release with `py -3 scripts/creepjs_audit.py --readme README.md`. Values are the **spoofed** per-seed identity (synthetic, not real machine data); a demo timezone and documentation WebRTC IP are used so no real PII appears here.

## Inspiration & credits

Clearcote stands on the shoulders of excellent open-source work and is grateful to:

- **[Chromium](https://www.chromium.org/)** (BSD-3-Clause) — the engine.
- **[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)** (BSD-3-Clause) — the de-Googled base and patch tooling.
- **[fingerprint-chromium](https://github.com/adryfish/fingerprint-chromium)** by adryfish (BSD-3-Clause) — pioneering engine-level fingerprint controls on an ungoogled base.
- **[Brave](https://brave.com/privacy-updates/3-fingerprint-randomization/)** — the per-site, per-session deterministic "farbling" model that informs Clearcote's coherent-identity approach.
- **[Camoufox](https://github.com/daijro/camoufox)** — a sibling open anti-detect browser (Firefox-based) whose breadth of signal coverage is a design reference.

Clearcote is an **independent project**. It is not affiliated with, endorsed by, or derived from any commercial product, and it ships **no** third-party proprietary code. See [CREDITS.md](CREDITS.md) for full attributions and licenses.

## Get the build

The first public build is on the **[Releases page](https://github.com/clearcotelabs/clearcote-browser/releases)** — `clearcote-149.0.7827.114-windows-x64.zip` (Chromium 149, Windows x64), shipped with SHA-256 checksums and a GPG signature so you can [verify it](docs/VERIFY.md) before running. Unzip it and run `chrome.exe` directly, or drive it from stock Playwright today:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=r"C:\clearcote\chrome.exe",   # the unzipped build
        args=["--fingerprint=seed-123", "--fingerprint-platform=windows"],
    )
    page = browser.new_page()
    page.goto("https://example.com")
    browser.close()
```

Same `--fingerprint=<seed>` ⇒ a stable identity across launches; a new seed ⇒ a fresh one.

## SDK

Clearcote ships **Playwright drop-in SDKs for Node and Python**, published on **npm** and **PyPI**. Each `launch()` returns a standard Playwright `Browser` and auto-downloads + SHA-256-verifies the Clearcote binary on first use, then caches it — no manual download.

**Node / TypeScript** — [`sdk/node`](sdk/node) · [npm: clearcote](https://www.npmjs.com/package/clearcote)
```bash
npm install clearcote
```
```javascript
import { launch } from "clearcote";

const browser = await launch({
  fingerprint: "user-7423",        // per-eTLD+1 seed: same seed ⇒ same identity, different ⇒ unlinkable
  platform: "windows",
  timezone: "America/New_York",
});
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

**Python** — [`sdk/python`](sdk/python) · [PyPI: clearcote](https://pypi.org/project/clearcote/)
```bash
pip install clearcote
```
```python
from clearcote import launch

browser = launch(fingerprint="user-7423", platform="windows", timezone="America/New_York")
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

**Match a proxy automatically.** `geoip` resolves the proxy's exit IP — looked up in the offline [geoip-all-in-one](https://github.com/daijro/geoip-all-in-one) database — and sets a coherent **timezone + `navigator.languages` + `Accept-Language`** for that region:

```javascript
const browser = await launch({
  fingerprint: "user-7423",
  proxy: { server: "http://host:8080", username: "u", password: "p" },
  geoip: true,                       // timezone + language auto-matched to the proxy's region
});
```

(Python: `launch(fingerprint="user-7423", proxy={...}, geoip=True)`. Or set `acceptLanguage` / `accept_language` explicitly.)

**Human-like input + a coherent WebRTC IP.** Set `humanize: true` to move the pointer along real, trusted bezier paths (engine-level `humanizedClick` — `navigator.webdriver` stays `false`), add `showCursor: true` to render the cursor for debugging, and `webrtcIp` to fabricate a coherent srflx candidate (no STUN leak; with `geoip` it tracks the proxy's exit IP automatically):

```javascript
const browser = await launch({
  fingerprint: "user-7423",
  humanize: true,                    // trusted bezier mouse movement; webdriver stays false
  showCursor: true,                  // render the cursor (debugging)
  webrtcIp: "203.0.113.7",           // fabricated srflx; no STUN/LAN leak
});
```

(Python: `launch(fingerprint="user-7423", humanize=True, show_cursor=True, webrtc_ip="203.0.113.7")`.) Full option list: [`sdk/node`](sdk/node) · [`sdk/python`](sdk/python).

**Import a real machine's fingerprint.** Beyond the synthetic `--fingerprint` seed, Clearcote can adopt the *exact* identity of a real Chrome. Get a profile three ways — grab a ready-made one from the curated **[clearcote-profiles](https://github.com/clearcotelabs/clearcote-profiles)** library, capture your own with the [collector](tools/fingerprint-collect) (open `collect.html` on the donor machine and click **Capture**), or convert any record from the open [10k-profile dataset](https://github.com/Vinyzu/chrome-fingerprints) with [`convert_dataset.py`](tools/fingerprint-collect/convert_dataset.py) — then pass it:

```javascript
const browser = await launch({ fingerprint: "user-7423", fingerprintProfile: "./profile.json" });
```

(Python: `launch(fingerprint="user-7423", fingerprint_profile="profile.json")`.) The SDK gzip+base64-encodes the profile for you (from a path, object, or JSON string). It drives the donor's GPU (WebGL vendor/renderer + the `getParameter` table + extensions), screen geometry, fonts, speech voices, audio, and CSS display metadata; any field not present falls back to the `--fingerprint` seed, so partial profiles stay coherent. See [`tools/fingerprint-collect`](tools/fingerprint-collect) for capture + dataset conversion.

**Verify it loaded.** Confirm the browser is actually presenting the profile — [`verify_profile.py`](tools/fingerprint-collect/verify_profile.py) launches the binary with the profile, probes the live surfaces, and prints a PASS/FAIL table:

```bash
python tools/fingerprint-collect/verify_profile.py --executable /path/to/clearcote/chrome.exe profile.json
#   surface                expected                       actual
#   hardwareConcurrency    12                             12                  PASS
#   glRenderer             ANGLE (Intel, Arc A770 …)      ANGLE (Intel, Arc … PASS
#   …          VERIFIED: clearcote is loading the profile.
```

Already using Playwright? It's a one-line import change — the returned object is a standard Playwright `Browser`, and the verified Windows binary is fetched + cached for you.

## Build it yourself

You never have to take our word for what's inside. Clearcote cross-compiles the Windows binary on a Linux box — one command on a capable host:

```bash
git clone https://github.com/clearcotelabs/clearcote-browser.git
cd clearcote-browser && WORK=~/clearcote-build ./build.sh
```

- **[docs/BUILDING.md](docs/BUILDING.md)** — full build-from-source guide: pinned versions, the [`scripts/`](scripts) pipeline, and every cross-build gotcha.
- **[docs/VERIFY.md](docs/VERIFY.md)** — how to verify a release: signature, checksums, reproducibility, and diffing the patch set against pinned upstream.

Releases are GPG-signed. Pin the **Clearcote release signing key** fingerprint and check every download against it — it does not change between releases:

```
CA96 F185 F96A 693A EDB3  AC1F CB00 D851 B7A8 6B0F
```

## Roadmap

We're building toward a first public release and a lot more after it — a Playwright/Puppeteer SDK, signed reproducible artifacts, a profile manager, and broader platform coverage. The full plan, with milestones, is in **[ROADMAP.md](ROADMAP.md)**.

⭐ **Star and watch the repo** to get notified when the first build drops.

## Responsible use

Clearcote is a privacy and automation tool intended for lawful purposes — protecting your own privacy, web/QA testing, research, accessibility, and automation you are authorized to run. **You are responsible for how you use it.** Respect website terms of service, applicable laws, and others' rights. Clearcote is provided "as is," without warranty of any kind. See [DISCLAIMER.md](DISCLAIMER.md).

## License

Clearcote's own code and patches are released under the **BSD-3-Clause** license (see [LICENSE](LICENSE)). Upstream components retain their original licenses; see [CREDITS.md](CREDITS.md).

## Contributing

Issues and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md). Whether you're a human or an automated contributor, the repo is laid out to be easy to navigate and build.
