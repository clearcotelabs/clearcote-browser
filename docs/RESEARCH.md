# Clearcote — Research & Build Blueprint

*How to build a new, 100% open-source anti-detect Chromium that is genuinely better than the field.*

Synthesized from 16 reference projects cloned to the research box (`~/research/`):
CloakBrowser · ChromiumFish · BotBrowser · Camoufox · adryfish/fingerprint-chromium ·
ungoogled-chromium-149 · brave-core · cromite · undetectable-fingerprint-browser ·
kameleo · invisible_playwright · rebrowser-patches · patchright · nodriver ·
SeleniumBase · undetected-chromedriver.

---

## 1. The landscape, in one table

| Project | Engine | Approach | Open? | One thing to steal |
|---|---|---|:-:|---|
| **brave-core** | Chromium | per-site **farbling** (deterministic randomization) | ✅ full | The *correct* randomization model — per-eTLD+1, per-session seed |
| **cromite** | Chromium | Brave farbling ported onto an ungoogled-style base | ✅ full | The port pattern clearcote needs + academic papers |
| **adryfish/fingerprint-chromium** | Chromium | seed → fingerprint, on ungoogled | ⚠️ delayed (tags) | The exact base clearcote builds on |
| **CloakBrowser** | Chromium | source patches + humanize + geoip | ❌ binary closed | Humanize presets, geoip, persistent-profile UX |
| **ChromiumFish** | Chromium | engine patches + persona seed + **TLS pinning** | ⚠️ patches only | TLS sigalgs/JA3 pinning to advertised UA; canvas bridge |
| **BotBrowser** | Chromium | encrypted device **profiles**, per-context | ❌ engine closed | Per-context FP, UDP-over-SOCKS5, port protection |
| **Camoufox** | Firefox | C++ spoofing, widest surface list | ✅ full | The most complete *surface checklist* + MaskConfig design |
| **invisible_playwright** | Firefox | C++ Gecko spoofing | ✅ | Cross-check of the Firefox approach |
| **kameleo** | both | engine masking, commercial | ❌ | Dual-engine architecture framing |
| **undetectable-fingerprint-browser** | Chromium | open Multilogin-alt | ✅ | Direct peer; profile-manager UX |
| **rebrowser-patches** | — | CDP-leak patches for PW/Pptr | ✅ | The list of CDP leaks to also fix in-engine |
| **patchright** | — | patched Playwright | ✅ | `chrome.runtime` realism, CDP suppression |
| **nodriver** | — | no-WebDriver direct CDP | ✅ | Benchmarked zero-blocked; the automation transport to mimic |
| **SeleniumBase / undetected-chromedriver** | — | classic flag/artifact scrub | ✅ | Baseline detection vectors |

**Two camps:** *engine-level forks* (top of table — clearcote's category) and *client-side/CDP patchers* (bottom). Clearcote is an engine fork, so the top rows are the blueprint and the bottom rows are the **catalogue of detection vectors you must also neutralize** in-engine.

---

## 2. The thesis everyone shares — and the insight most miss

**Shared thesis:** spoof in the C++ engine, never in injected JS. JS shims leave tamper marks exactly where fingerprinting auditors look. adryfish (clearcote's base) already does this.

**The deeper insight, which only Brave got fully right:** a good fingerprint is not *random* and not *static* — it is **per-site deterministic and internally coherent**.

Brave's farbling derives every semi-identifying value from a `(session_token × eTLD+1)` seed, so:
- the same site sees the **same** value all session (no intra-session flicker = no detection),
- different sites see **different** values (no cross-site linking),
- next session → fresh values (no cross-session linking).

Evidence in `brave-core`: a dedicated `components/brave_shields/content/browser/brave_farbling_service.cc` plus per-surface coverage — canvas, audio, webgl, **webgpu**, screen, plugins, useragent, languages, hardwareConcurrency, deviceMemory, speech-synthesis, USB, enumerateDevices (see `browser/farbling/*_browsertest.cc`).

> **For clearcote:** adopt Brave's *model* (per-eTLD+1 deterministic seed), not just per-surface noise. This is the single biggest correctness upgrade over adryfish, CloakBrowser, and ChromiumFish, all of which use a single global seed (every site sees the same canvas → linkable across sites).

---

## 3. The detection-surface matrix (what clearcote must cover)

✅ = covered by base · ➕ = clearcote must add/upgrade · ★ = differentiator opportunity

| Surface | adryfish base | Best reference impl | Clearcote action |
|---|:-:|---|---|
| UA + full Client Hints set | ✅ | Camoufox navigator | ✅ keep |
| navigator.webdriver / cdc_ scrub | ✅ | BotBrowser `removeHeadless.diff` | ✅ keep + audit |
| Canvas 2D | ✅ noise | Brave per-site farble | ➕ make per-eTLD+1 |
| WebGL params/vendor/renderer | ✅ | Brave + Camoufox MaskConfig | ➕ per-site + coherent GPU |
| **WebGPU** | ⚠️ | Brave `webgpu_farbling` | ➕ often-missed surface |
| AudioContext | ✅ | Brave webaudio farble | ➕ per-site |
| Fonts (list + metrics) | ⚠️ partial | Camoufox font-hijacker; Cromite font mitigations | ➕ list + Skia/HarfBuzz metrics |
| Screen / display / DPR / taskbar | ✅ | ChromiumFish display | ✅ keep |
| Hardware (cores/mem) **correlated** | ✅ values | ChromiumFish fingerprint-profile | ➕ enforce coherence |
| Timezone + locale (from proxy IP) | ✅ | CloakBrowser geoip | ✅ keep, native flags |
| Geolocation (matches TZ) | ✅ | Camoufox | ✅ keep |
| **TLS / JA3 / JA4 / sigalgs** | ❌ | **ChromiumFish tls-fingerprint** | ➕ **#1 priority** |
| WebRTC IP leak | ✅ | Camoufox webrtc-ip | ✅ keep |
| **QUIC/STUN over proxy (UDP/SOCKS5)** | ❌ | **BotBrowser** | ★ rare, high value |
| Media devices enumeration | ⚠️ | BotBrowser `video_capture…diff`; Camoufox | ➕ add |
| Speech / voices | ❌ | Camoufox + Brave | ➕ add |
| Device sensors | ❌ | ChromiumFish device-sensors | ➕ add |
| Timing (`performance.now` quantize) | ❌ | ChromiumFish timing-misc | ➕ add |
| Closed Shadow DOM access (automation) | ✅ | adryfish FakeShadowRoot | ✅ keep |
| Port-scan protection (localhost) | ❌ | BotBrowser port-protection | ★ differentiator |
| **De-Google telemetry (port 5228 GCM)** | partial | (ChromiumFish *failed* its own audit here) | ★ beat them on privacy |

---

## 4. Where the field falls short → clearcote's opening

1. **Global seed, not per-site.** CloakBrowser/ChromiumFish/adryfish use one seed for all sites → cross-site linkable. **Brave-style per-eTLD+1 farbling fixes this and nobody in the *stealth* niche has copied it.**
2. **TLS mismatch on new bases.** Building on Chromium 149 while spoofing an older UA leaks at the TLS layer (149 adds ML-DSA sigalgs `0x904-0x906`; a major anti-bot service flags "UA 148 + TLS 149"). adryfish doesn't address this. ChromiumFish's patch is the fix.
3. **Network leaks around the proxy.** Most forks only mask WebRTC IP; QUIC/STUN still bypass SOCKS5. Only BotBrowser tunnels UDP-over-SOCKS5 — and it's closed/paid.
4. **Not actually private.** ChromiumFish's *own* audit (in your local files) caught it phoning Google (component updater + GCM on :5228). A truly de-Googled build is a real, checkable differentiator for a "100% open" project.
5. **Scale.** One process per identity is wasteful. BotBrowser's per-context fingerprint (shared GPU/net process, ms switching, ~29% memory saving) is the scalability frontier — and closed.
6. **Closed binaries.** CloakBrowser, ChromiumFish, BotBrowser, Kameleo all ship unreadable binaries. **Clearcote being byte-for-byte reproducible + fully open is itself the trust differentiator.**

---

## 5. Proposed clearcote architecture

```
                 ungoogled-chromium 149   (de-Google base, build wiring)
                          │
        ┌─────────────────┼──────────────────────────────────┐
        │                 │                                   │
  adryfish patches   Brave farbling model            clearcote-native patches
  (ported 142→149)   (per-eTLD+1 seed engine)         (TLS pin, QUIC/SOCKS5,
  canvas/webgl/audio  applied to every surface         timing, sensors, media,
  /ua/fonts/webrtc    instead of single global seed     port-protect, full de-Google)
        │                 │                                   │
        └─────────────────┴──────────────────────────────────┘
                          │
              thin SDK over Playwright/Puppeteer
              (fetch→verify→cache→launch; nodriver-style transport option)
                          │
              per-context fingerprint (stretch goal)
```

**Design rules (lifted from the best of each):**
- Repo layout = **ChromiumFish** (`patches/` + `assets/` + `apply.sh` + pinned `UPSTREAM_REVISION`).
- Surface checklist = **Camoufox** (widest); config schema = Camoufox `MaskConfig.hpp` (`webGl:parameters`, `voices`, `humanize:minTime/maxTime`, ranges w/ `precision`).
- Randomization semantics = **Brave** farbling (per-eTLD+1 deterministic).
- Network = **BotBrowser** (DNS-through-proxy, UDP-over-SOCKS5, WebRTC SDP/ICE filtering).
- TLS = **ChromiumFish** (pin sigalgs/cipher/GREASE/groups to advertised UA).
- Humanize = engine-level input (ChromiumFish) > client-side (CloakBrowser), but ship CloakBrowser's two presets (`default`/`careful`).
- Automation transport = offer a **nodriver-style** no-WebDriver path for the lowest CDP signature.

---

## 6. Concrete build plan

1. **Base up:** vanilla Chromium 149 → apply ungoogled-149 (already on box) → confirm `chrome --version` shows ungoogled.
2. **Port fingerprint patches `142 → 149`** (adryfish 144 source isn't published yet; 142 is the newest available tag). Diff adryfish-142 tree vs vanilla 142, replay onto 149, resolve rejects, fix API breaks, compile. *(Recon this first — dry-run the apply, count rejects.)*
3. **Layer the upgrades**, priority order:
   1. **TLS pinning** (ChromiumFish concept) — highest ROI, mandatory given 149 base.
   2. **Per-eTLD+1 farbling seed** (Brave model) over canvas/webgl/webgpu/audio.
   3. **QUIC/STUN over SOCKS5** + DNS-through-proxy.
   4. Timing, device-sensors, media-devices, speech (close adryfish gaps).
   5. **Full de-Google** — kill component updater + GCM :5228 (beat ChromiumFish's audited weakness).
   6. Port protection.
4. **SDK:** thin Python+JS wrapper over Playwright (`executablePath=…`), fetch/verify(SHA-256)/cache. Optional nodriver-style transport.
5. **Stretch:** per-context fingerprints (BotBrowser model) for scale.
6. **Validate** against CreepJS, BrowserScan, Pixelscan, live anti-bot services, + a JA3/JA4 byte-diff vs real Chrome of the advertised version.

---

## 7. What makes clearcote *better* (the pitch)

| Axis | Field today | Clearcote |
|---|---|---|
| Cross-site linkability | single global seed (linkable) | **per-eTLD+1 farbling (unlinkable)** |
| TLS coherence | ignored by adryfish | **pinned to advertised UA** |
| Proxy leak surface | WebRTC only | **+ QUIC/STUN/DNS over SOCKS5** |
| Privacy of the "private" browser | phones Google (ChromiumFish) | **fully de-Googled, verifiable** |
| Trust | closed binaries | **100% open + reproducible build** |
| Scale | 1 process/identity | **per-context fingerprints (stretch)** |

The thesis in one line: **take adryfish's open Chromium base, give it Brave's per-site randomization correctness, ChromiumFish's TLS rigor, and BotBrowser's network-leak coverage — as a single 100% open, reproducible build.**

---

## 8. Risks / open questions

- **Patch churn 142→149** is 7 majors; fingerprint-touched files move a lot. Mitigate by porting incrementally and pinning the upstream revision.
- **adryfish source lag** — may be worth waiting for the 144 source for a shorter hop, vs. starting from 142 now.
- **Farbling integration cost** — Brave's farbling is wired into Brave Shields/prefs; extracting just the seed+per-surface logic onto ungoogled is non-trivial (Cromite shows it's doable).
- **Build resources** — full Chromium build on the 8-vCPU/32 GB box is slow but feasible with the 640 GB disk; first build is the long pole.
- **Maintenance** — every Chrome major repeats the port. Budget for it; automate the diff/apply where possible.

---

## Appendix — further reading already on the box
`~/research/cromite/docs/Papers/` contains the academic fingerprinting literature
(large-scale fingerprinting analysis, anti-tracking countermeasure comparisons,
automatic discovery of emerging FP techniques) — the theory behind every patch above.
