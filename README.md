<div align="center">

# Clearcote Browser

### A fully open, fully auditable Chromium build for a consistent, private browser identity.

**No opaque binaries. No phone-home. Every change is a readable patch, every build is reproducible from source.**

</div>

> [!NOTE]
> **Status: first public build is live — [v0.1.0-pre.1](https://github.com/clearcotelabs/clearcote-browser/releases/latest) (Chromium 149, Windows x64).**
> A signed, checksummed pre-release build is available now on the [Releases page](https://github.com/clearcotelabs/clearcote-browser/releases) — download it, [verify it](docs/VERIFY.md), and run it. It's an early, experimental build (engine identity controls compiled in, currently a single global seed, not yet stealth-validated); see the [Roadmap](ROADMAP.md) for what's next.

---

## What is Clearcote?

Clearcote is an open-source [Chromium](https://www.chromium.org/) distribution with two goals:

1. **A coherent, private browser identity.** A stock browser quietly exposes a unique, trackable fingerprint — canvas, WebGL, audio, fonts, locale, hardware, and more. Clearcote moves control of those signals **into the engine itself**, so a session presents one consistent, plausible identity instead of an accidental, hyper-unique one.
2. **Radical verifiability.** Clearcote is built on [ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium) (Google integration and telemetry removed) and a transparent stack of source patches. There is **no magic binary** — you can read every change, rebuild it yourself, and check that what you run matches what's published.

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

## SDK *(landing next)*

> The one-line drop-in below is the target developer experience for v1.0 — the same objects and methods as Playwright/Puppeteer, just a different import. It's published here so the design is in the open.

**Python**
```python
from clearcote import launch

browser = launch()                 # standard Chromium, Clearcote identity controls on
page = browser.new_page()
page.goto("https://example.com")
browser.close()
```

**Node (Playwright)**
```javascript
import { launch } from "clearcote";

const browser = await launch();
const page = await browser.newPage();
await page.goto("https://example.com");
await browser.close();
```

Already using Playwright/Puppeteer? Migration is meant to be a one-line import change — same objects, same methods.

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
