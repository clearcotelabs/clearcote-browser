# Clearcote Roadmap

Clearcote is being built in the open. This roadmap is intentionally public so you can see where we're headed, hold us to it, and jump in. Dates are deliberately omitted — we ship milestones when they're verifiable, not before.

> **TL;DR:** a fully open, reproducible Chromium build with engine-level privacy/identity controls and a Playwright-compatible SDK — released with checksums and build attestation so anyone can confirm exactly what they're running.

---

## Phase 0 — Foundations *(complete)*

The unglamorous groundwork that everything else depends on.

- [x] Project scope, principles, and architecture
- [x] De-Googled base via ungoogled-chromium, pinned to an exact upstream revision
- [x] Reproducible source-prep pipeline (fetch → prune → patch → verify)
- [x] Clean, automated build from source on a commodity machine
- [x] Published patch set: every change human-readable and individually documented

## Phase 1 — First public build *(shipped — v0.1.0-pre.2)*

The moment you can download something and check it yourself.

- [x] **Windows x64 build** as the first target
- [x] **Checksummed release artifacts** (SHA-256) on GitHub Releases
- [x] **Reproducible build instructions** that a third party can follow to get a matching binary
- [x] One-command source bootstrap for contributors

## Phase 2 — Coherent identity controls *(in progress)*

Engine-level control over the signals a browser exposes — designed to be *consistent*, not random.

- [x] Per-profile, deterministic identity seed (stable across sessions when you want it; fresh when you don't)
- [x] Engine-level controls for canvas, WebGL, audio, fonts, locale/timezone, navigator & hardware reporting, and WebRTC *(fabricated srflx at `--webrtc-ip`, no real STUN/LAN leak)*
- [x] **Per-site coherence** (farbling-style): the same site sees a stable identity; different sites don't correlate
- [x] **Cross-surface coherence pass** *(v0.1.0-pre.6)*: coherent WebGL `getParameter` limits + session-constant GPU; `navigator` battery/connection/keyboard; audio, screen & `getScreenDetails()` metadata; CSS pointer/hover; OS-coherent `URL`; `navigator.share`/`canShare` — validated against open-source fingerprint auditors (0% headless / 0% stealth)
- [x] **Fingerprint profile import** *(v0.1.0-pre.8)*: adopt a *real* machine's identity — capture it with the [collector](tools/fingerprint-collect), or convert a record from a 10k-profile open dataset, and apply it via `--fingerprint-profile` / the SDK's `fingerprint_profile` option. Drives the donor's GPU (WebGL vendor/renderer + the full `getParameter` table + extensions), screen, fonts, speech voices, audio, Chrome version, and CSS `@media`; fields absent from the profile fall back to the `--fingerprint` seed, so partial profiles stay coherent.
- [x] **Cross-API coherence + audit pass** *(v0.1.0-pre.10)*: `navigator.gpu` (WebGPU adapter vendor/architecture + limits) made coherent with the WebGL persona GPU for **seed-only** personas (no host-GPU leak); UI/ICU locale pinned to the language (`--lang`) so `Intl` matches `navigator.language` on the main thread **and** workers; persona speech default-voice (no host-locale voice leak); UA-CH high-entropy `bitness`/`wow64`/`model`; `MediaCapabilities.decodingInfo()` + `enumerateDevices()` persona coherence; `storageQuota`. Adversarially audited against open fingerprint auditors — 0% headless / 0% stealth, no API-tampering lies.
- [ ] Sensible, documented defaults — privacy-respecting out of the box

## Phase 3 — Automation SDK *(SDKs shipped)*

Make it a true drop-in for the tools developers already use.

- [x] **Python + Node SDKs**, Playwright-first, Puppeteer supported *(Node on [npm](https://www.npmjs.com/package/clearcote) + Python on [PyPI](https://pypi.org/project/clearcote/), both `clearcote` — Playwright drop-in; Puppeteer can point at the same binary)*
- [x] `launch()` returns a standard Playwright object — one-line migration
- [x] Auto-download + local cache of the verified binary
- [x] **In-browser AI agent** *(v0.1.0-pre.10)*: `launch_agent` / `run_agent_task` + the `clearcote-agent` CLI (one-shot `--goal` or `-i` REPL) — drive a page with natural-language goals via your own OpenAI-compatible / OpenRouter key. Opt-in (off by default); perceives the page, asks the LLM, and acts through Chrome's Actor framework with real trusted input.
- [ ] Recipes: persistent profiles, proxy configuration, headless on servers

## Phase 4 — Trust & supply-chain

Raise the bar on verifiability beyond "here's a checksum."

- [ ] **Build provenance / attestation** (e.g. Sigstore-style) for every release
- [ ] Signed tags and a documented verification workflow
- [ ] CI that builds, checksums, and publishes in the open
- [x] A public "verify this release" guide anyone can run in minutes

## Phase 5 — Beyond

Where it grows once the core is solid.

- [ ] Additional platforms (Linux, macOS)
- [ ] A self-hostable **profile manager** for organizing identities and proxies
- [ ] Integrations with common automation/agent frameworks
- [ ] Hardening informed by open fingerprinting research

---

## How to follow along

- **Watch** this repo for release notifications.
- **Issues** track concrete tasks; **Discussions** (when enabled) are for ideas.
- Want to help build it? Start with [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

This roadmap will evolve as the project does. Nothing here is a promise of a date — but everything here is a promise of *how* we'll ship it: in the open, and verifiable.
