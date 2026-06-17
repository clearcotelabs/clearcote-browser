# Clearcote Roadmap

Clearcote is being built in the open. This roadmap is intentionally public so you can see where we're headed, hold us to it, and jump in. Dates are deliberately omitted — we ship milestones when they're verifiable, not before.

> **TL;DR:** a fully open, reproducible Chromium build with engine-level privacy/identity controls and a Playwright-compatible SDK — released with checksums and build attestation so anyone can confirm exactly what they're running.

---

## Phase 0 — Foundations *(in progress)*

The unglamorous groundwork that everything else depends on.

- [x] Project scope, principles, and architecture
- [x] De-Googled base via ungoogled-chromium, pinned to an exact upstream revision
- [x] Reproducible source-prep pipeline (fetch → prune → patch → verify)
- [ ] Clean, automated build from source on a commodity machine
- [ ] Published patch set: every change human-readable and individually documented

## Phase 1 — First public build *(next)*

The moment you can download something and check it yourself.

- [ ] **Windows x64 build** as the first target
- [ ] **Checksummed release artifacts** (SHA-256) on GitHub Releases
- [ ] **Reproducible build instructions** that a third party can follow to get a matching binary
- [ ] One-command source bootstrap for contributors

## Phase 2 — Coherent identity controls

Engine-level control over the signals a browser exposes — designed to be *consistent*, not random.

- [ ] Per-profile, deterministic identity seed (stable across sessions when you want it; fresh when you don't)
- [ ] Engine-level controls for canvas, WebGL, audio, fonts, locale/timezone, navigator & hardware reporting, and WebRTC
- [ ] **Per-site coherence** (farbling-style): the same site sees a stable identity; different sites don't correlate
- [ ] Sensible, documented defaults — privacy-respecting out of the box

## Phase 3 — Automation SDK

Make it a true drop-in for the tools developers already use.

- [ ] **Python + Node SDKs**, Playwright-first, Puppeteer supported
- [ ] `launch()` returns a standard Playwright object — one-line migration
- [ ] Auto-download + local cache of the verified binary
- [ ] Recipes: persistent profiles, proxy configuration, headless on servers

## Phase 4 — Trust & supply-chain

Raise the bar on verifiability beyond "here's a checksum."

- [ ] **Build provenance / attestation** (e.g. Sigstore-style) for every release
- [ ] Signed tags and a documented verification workflow
- [ ] CI that builds, checksums, and publishes in the open
- [ ] A public "verify this release" guide anyone can run in minutes

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
