# clearcote — Complete Research Dossier

*A 100% open-source, reproducible anti-detect / fingerprint-hardened Chromium. Target: Windows x64, cross-compiled on a Linux box. This document consolidates all research conducted to date.*

- **Repo:** github.com/clearcotelabs/clearcote-browser
- **Base:** ungoogled-chromium 149.0.7827.114 (de-Googled) + adryfish/fingerprint-chromium patches (engine-level spoofing)
- **Build host:** a dedicated Linux box (8 vCPU / 32 GB RAM + 16 GB swap / ~640 GB disk, Ubuntu 22.04) that cross-compiles the Windows binary
- **Companion docs:** `RESEARCH.md` (landscape synthesis), `PATCHES.md` (file-level stealth-patch manifest)

---

## 0. Executive summary

clearcote is built on the exact same foundation the commercial anti-detect Chromium forks use, but ships **100% open and reproducible** — no proprietary binary. The foundation is:

```
Chromium (Google)
        │
ungoogled-chromium  ── de-Google: strips Google services/telemetry, build wiring
        │
adryfish/fingerprint-chromium  ── C++ source patches: canvas/WebGL/audio/fonts/WebRTC/UA spoofing
        │
clearcote-native upgrades  ── per-eTLD+1 farbling coherence, TLS group-table version pinning,
                              QUIC/STUN-over-SOCKS5, full de-Google, engine-level humanize
```

The single most important design insight from the research: **a good fingerprint is not random and not static — it is per-eTLD+1 deterministic and internally coherent.** Every stealth fork except Brave gets this wrong (they use one global seed, which is itself a cross-site linking supercookie). clearcote adopts Brave's farbling model as its core.

---

## 1. The foundation discovery

The commercial forks (CloakBrowser, ChromiumFish) market "42/49/57 source-level C++ patches" but ship **closed binaries** — you can read *what* they patch (published `.patch` descriptions) but not the full build. Tracing what they are actually built on:

- **CloakBrowser** — patched Chromium, closed binary. README claims 42→57 patches, humanize, geoip, persistent profiles, TLS match.
- **ChromiumFish** (arman-bd) — patched Chromium, closed binary, but publishes select example patches + pins `UPSTREAM_REVISION`. Best public TLS + persona-seed patches.
- **The open base both imitate** = **`adryfish/fingerprint-chromium`** (a Chinese project) "a fingerprint browser based on Ungoogled Chromium." This is clearcote's real starting point.

### adryfish delayed-source policy (critical operational fact)
adryfish's **main branch has no patches**. Source ships in **git tags**, released **~1 month after** the binary. Available source tags: `129, 130, 131, 132, 133, 134, 135, 136, 138, 139, 142, 144`. The **newest published source is `142.0.7444.175`** — the 144 binary is out but its source is not. Each tag is a **full ungoogled-chromium source tree**, not standalone `.patch` files, so the port is *diff(adryfish-142-tree vs vanilla 142) → replay onto 149*.

**Consequence:** the patch port is **142 → 149 (7 majors)**, not 144→149. Options: port from 142 now, or wait for 144 source for a shorter hop.

---

## 2. The reference landscape (16 repos studied)

All cloned to the box under `~/research/`.

| Repo | Engine | Approach | Open? | Key takeaway for clearcote |
|---|---|---|---|---|
| **brave-core** | Chromium | per-site **farbling** | ✅ full | The correct randomization model: per-eTLD+1 deterministic |
| **cromite** | Chromium | Brave farbling on ungoogled-style base | ✅ full | The exact port pattern + academic FP papers |
| **fingerprint-chromium** (adryfish) | Chromium | seed→fingerprint on ungoogled | ⚠️ tags | clearcote's base spoofing layer |
| **ungoogled-chromium-149** | Chromium | de-Google + build wiring | ✅ full | The base (149.0.7827.114) |
| **CloakBrowser** | Chromium | patches + humanize + geoip | ❌ binary | Humanize presets, geoip, persistent-profile UX |
| **chromiumfish** | Chromium | engine patches + persona + TLS | ⚠️ patches | TLS pinning, persona-seed plumbing, canvas bridge |
| **BotBrowser** | Chromium | encrypted device profiles, per-context | ❌ engine | Per-context FP, UDP-over-SOCKS5, port protection |
| **camoufox** | Firefox | C++ spoofing, widest surface list | ✅ full | The most complete surface checklist + MaskConfig |
| **invisible_playwright** | Firefox | C++ Gecko spoofing | ✅ | Cross-check of the Firefox approach |
| **kameleo** | both | engine masking (commercial) | ❌ | Dual-engine architecture framing |
| **undetectable-fingerprint-browser** | Chromium | open Multilogin alternative | ✅ | Direct open peer; profile-manager UX |
| **rebrowser-patches** | — | CDP-leak patches (PW/Pptr) | ✅ | The list of CDP leaks to also fix in-engine |
| **patchright-python** | — | patched Playwright | ✅ | `chrome.runtime` realism, CDP suppression |
| **nodriver** | — | no-WebDriver direct CDP | ✅ | Benchmarked zero-blocked; transport to mimic |
| **SeleniumBase** | — | automation framework + evasion | ✅ | Baseline detection vectors |
| **undetected-chromedriver** | — | flag/artifact scrub | ✅ | Classic detection vectors |

**Two camps:** *engine-level forks* (clearcote's category — top rows = the blueprint) and *client-side/CDP patchers* (bottom rows = the catalogue of detection vectors to neutralize in-engine).

---

## 3. The core thesis: engine-level + per-eTLD+1 coherence

### 3.1 Engine-level, never JavaScript
Every serious fork agrees: spoof in the C++ engine, never via injected JS. JS shims (`playwright-stealth`, `puppeteer-extra`, `undetected-chromedriver`) leave tamper marks exactly where FingerprintJS/CreepJS look — and break on every Chrome update. adryfish (clearcote's base) already patches at the C++ level.

### 3.2 Per-eTLD+1 deterministic coherence (the key upgrade)
Brave's **farbling** derives every semi-identifying value from a `(session_token × eTLD+1)` seed, so:
- same site → **same** values all session (no intra-session flicker tell),
- different sites → **different** values (no cross-site linking),
- next session → fresh persona.

Evidence in `brave-core`: a dedicated `components/brave_shields/content/browser/brave_farbling_service.cc` plus per-surface browsertests under `browser/farbling/` — canvas, audio, webgl, **webgpu**, screen, plugins, useragent, languages, hardwareConcurrency, deviceMemory, speech-synthesis, USB, enumerateDevices.

**Why it matters:** CloakBrowser / ChromiumFish / adryfish all use a **single global `--fingerprint` seed** → every site sees the same canvas → cross-site linkable. Adopting Brave's per-eTLD+1 model is clearcote's single biggest correctness advantage, and nobody in the *stealth* niche has done it.

### 3.3 Internal (vertical) coherence
Every surface must describe **one plausible real machine**, and that machine must match the **actual render backend the binary ships with**. A WebGL renderer string cannot coexist with a contradictory `MAX_TEXTURE_SIZE`, an off-distribution `hardwareConcurrency + deviceMemory`, a Berlin geolocation under a New York timezone, or — the strongest GPU tell — a `readPixels` hash that came from a different GPU than the renderer string claims. Surfaces are drawn **tier-first** from curated real-device distributions, never independently randomized.

---

## 4. Detection-surface matrix

✅ covered by adryfish base · ➕ clearcote adds/upgrades · ★ differentiator

| Surface | adryfish base | Best reference | clearcote action |
|---|:-:|---|---|
| UA + full Client Hints | ✅ | Camoufox navigator | keep, enforce congruence |
| navigator.webdriver / cdc_ scrub | ✅ | BotBrowser removeHeadless | keep + audit |
| Canvas 2D | ✅ noise | Brave per-site farble | ➕ per-eTLD+1 |
| WebGL params/vendor/renderer | ✅ | Brave + Camoufox | ➕ per-site + coherent GPU |
| **WebGPU** | ⚠️ | Brave webgpu | ➕ often-missed |
| AudioContext | ✅ | Brave webaudio | ➕ per-site |
| Fonts (list + metrics) | ⚠️ | Camoufox font-hijacker; Cromite | ➕ list + metric jitter |
| Screen/display/DPR | ✅ | ChromiumFish display | keep |
| Hardware (cores/mem) correlated | ✅ values | ChromiumFish fingerprint-profile | ➕ enforce coherence |
| Timezone + locale (native) | ✅ | CloakBrowser geoip | keep |
| **TLS / JA3 / JA4** | ❌ | ChromiumFish tls-fingerprint | ➕ **version-keyed group table** |
| WebRTC IP | ✅ | Camoufox webrtc-ip | keep |
| **QUIC/STUN over SOCKS5** | ❌ | BotBrowser | ★ rare, high value |
| Media devices | ⚠️ | BotBrowser; Camoufox | ➕ add, permission-gated |
| Speech/voices | ❌ | Camoufox + Brave | ➕ add, async race |
| Geolocation | ✅ | Camoufox | keep, no auto-grant |
| Device sensors | ❌ | ChromiumFish device-sensors | ➕ add |
| Timing (performance.now) | ❌ | ChromiumFish timing-misc | ➕ regression assert |
| Closed Shadow DOM | ✅ | adryfish FakeShadowRoot | keep |
| Port-scan protection | ❌ | BotBrowser | ★ differentiator |
| **De-Google telemetry (GCM :5228)** | partial | (ChromiumFish failed own audit) | ★ beat them on privacy |

---

## 5. Per-fork deep findings

### 5.1 Brave (the farbling gold standard)
Dedicated farbling service + per-surface browsertests; deterministic per-eTLD+1, per-session seed. The canonical open implementation of "randomize, don't block." This is the model clearcote's L0 seed engine copies.

### 5.2 ChromiumFish (best public TLS + persona)
- `persona-seed.patch` — shows the exact plumbing: `--persona-seed` forwarded to every renderer via `kSwitchNames[]` in `render_process_host_impl.cc`, read by `fingerprint_profile.cc DeriveFromSeed`.
- `tls-fingerprint.patch` — the most instructive network patch found (see §6).
- **Canvas Bridge** — an *optional* render service on a real Windows GPU; the headless Linux browser routes canvas/WebGL reads to it for genuine hardware results. The strongest answer to "SwiftShader gives you away." None of the others have it.
- Pins `UPSTREAM_REVISION` for reproducibility.
- **Its own published audit caught it phoning Google** (component updater + GCM push on port 5228) — a real privacy gap clearcote can beat.

### 5.3 BotBrowser (different model worth stealing from)
Mostly proprietary (only the GUI launcher + example patches open: `webglAttrs.diff`, `timezone.diff`, `removeHeadless.diff`, `video_capture_device_descriptor.cc.diff`). Its open architecture docs reveal:
- **Per-context fingerprint** — independent identity per `BrowserContext`, no new process, ms-level switching, ~29% memory saving. The scalability frontier.
- **UDP-over-SOCKS5** — tunnels QUIC/STUN through the proxy so they don't leak around it (DNS-through-proxy too). Most forks only mask WebRTC IP; this is a real gap they ignore.
- **Profiles as captured real-device bundles** (encrypted `.enc`) vs synthesized-from-seed — inherently coherent, at the cost of needing a profile library.
- Port protection (localhost service scanning defense).

### 5.4 Camoufox (the surface breadth reference)
Firefox fork, but the **most complete open spoofing surface list**: anti-font-fingerprinting, audio-context + audio-fingerprint-manager, fingerprint-injection, font-hijacker, font-list + system-ui-font, geolocation, locale, media-device, navigator, screen, timezone, voice/speech-voices, webgl, webrtc-ip, shadow-root-bypass. Config schema `additions/camoucfg/MaskConfig.hpp` (`webGl:parameters`, `voices`, `humanize:minTime/maxTime`, ranges with `precision`) is a good model for clearcote's config.

### 5.5 Cromite
ungoogled-style Chromium that pulls Brave-derived fingerprint mitigations as standalone patches (`build/patches/*fingerprint*`, `Multiple-fingerprinting-mitigations.patch`) — proves the Brave-onto-ungoogled port pattern clearcote needs. Also ships `docs/Papers/` — the academic fingerprinting literature underpinning all of this.

---

## 6. TLS fingerprinting — the most-studied subsystem (with a correction)

ChromiumFish's `tls-fingerprint.patch` touches `net/base/features.cc` (`kTlsMldsaSignatures`) and `net/socket/ssl_client_socket_impl.cc`. Original RESEARCH.md framing was: "149 adds ML-DSA sigalgs `0x904–0x906`; disable them so the ClientHello matches an older Chrome."

**Grounded correction (verified against the stock 149 tree):** `kTlsMldsaSignatures` **and** `kTLSTrustAnchorIDs` are **already `FEATURE_DISABLED_BY_DEFAULT` in stock 149**. So a stock 149 build's JA4 already matches a real Chrome 149 — the "1-bit not-real-148 tell" framing was backwards. The genuine TLS work for clearcote is:
- a **version-selectable `supported_groups` / `key_share` table coordinated with the UA generator** (so the ClientHello matches the *advertised* Chrome version), and
- **regression assertions** that the adryfish port didn't accidentally toggle the PQ sigalgs on (do NOT hardcode the classical branch — that desyncs from a server-triggered PQ handshake a real 149 completes).

This is the kind of correction that only comes from reading the actual source, not the marketing.

---

## 7. The stealth-patch architecture (summary of PATCHES.md)

66 candidate patches across 10 domains, organized in **3 layers**:

- **L0 — farbling-core + persona engine** (P0): `--clearcote-seed` root → per-eTLD+1 token → `FingerprintProfile` persona struct (UA/screen/HW/GPU drawn tier-first from real clusters). Two readers, one root. Everything resolves through `SessionCache::From(context)` / `fingerprinting::Current()`.
- **L1 — adryfish 142→149 base port**: baseline spoofing; its single global seed is **upgraded to per-eTLD+1**, not double-patched. An audit gate with a **build-time `static_assert`** fails compilation if any surface still reads the global seed.
- **L2 — per-surface patches**: canvas/webgl/webgpu/audio/fonts/navigator/screen/TLS/webrtc/geo-media/sensors/timing/de-google/humanize.

### Three non-negotiable rules
1. **Never blank, never zero** — a zeroed `readPixels` / empty `getVoices()` is a harder tell than the real value; perturb coherently.
2. **Never read the global seed directly** — enforced by build-time assertion.
3. **Never let a constant collapse the fleet** — font set, `prefers-color-scheme`, battery level drawn per-persona (a shared constant is a linkable cohort).

### Headline fixes from the adversarial detector-critique
- **One persona GPU** matching the actual render backend: Intel UHD 770 / ANGLE-D3D11 for real-Windows deploy, ANGLE-SwiftShader for headless — so renderer string and readPixels hash always agree (resolved a draft contradiction that would have *increased* detectability).
- **Permission-gated surfaces** mimic real Chrome: media labels empty until `getUserMedia`, geolocation not auto-granted, `speechSynthesis` preserves the async empty-then-`voiceschanged` race.
- Added missing surfaces: `getBoundingClientRect`/transform precision jitter, WebGL FLOAT readPixels epsilon, `getHighEntropyValues` ordering, transcendental-math ULP OS tell, OfflineAudioContext render-rate.

### Phasing
P0 = seed + persona + GPU resolution + coherence + version-freeze + TLS group table + audit gate. P1 = per-surface farbling. P2 = defense-in-depth + polish.

(Full file-level detail with real Chromium 149 target paths in `PATCHES.md`.)

---

## 8. Build infrastructure & the cross-compile journey

### 8.1 Target & method
- **Target: Windows x64.** **Method: cross-compile on the Linux box** (decided with the user; local PC ruled out — only VS 2019/2017 + old SDKs + 24 GB free, and the user requires all build work on SSH).
- Base Chromium 149.0.7827.114 via ungoogled's `downloads.py retrieve/unpack`.

### 8.2 Source preparation (done)
1. Provisioned box: 16 GB swap (Chromium link is RAM-hungry), build-essential, depot_tools.
2. Downloaded + hash-verified Chromium 149 source (1.5 GB lite tarball → 9.7 GB unpacked).
3. `prune_binaries.py` → `patches.py apply` (111 ungoogled patches) → `domain_substitution.py` → vanilla ungoogled-149 tree at `~/clearcoat/build/src` (7.9 GB).

### 8.3 Toolchain gotchas encountered & solved (Linux host)
- **Domain substitution breaks toolchain download URLs.** ungoogled obfuscates Google domains in source — including in `tools/clang/scripts/update.py` (`commondatastorage.googleapis.com` → `commondatastorage.9oo91eapis.qjz9zk`). Fix: revert domain substitution, fetch toolchain against real URLs (clang/rust/sysroot), then build. (Re-apply domain sub later in the de-Google hardening pass.)
- **depot_tools won't bootstrap its bundled Python as root** ("Running depot tools as root is sad"; `python3_bin_reldir.txt` never created) → its `gn`/`ninja` wrappers are dead. Fix: build `gn` from the in-tree source (`tools/gn/bootstrap/bootstrap.py`) + install real `ninja` from apt + put the tree clang on PATH (`clang++` not found otherwise).
- **`bootstrap.py -o out/Default` clobbers the directory** — `-o` is the gn *binary* output path, not a build dir; it overwrote `out/Default/` (and `args.gn`). Fix: use the gn binary it leaves at `out/Release/gn_build/gn`, recreate `out/Default/` + `args.gn`, then `gn gen`.
- **`gn gen` succeeded** for a Linux build; first `ninja chrome` then surfaced a missing **node** binary (a normal DEPS-fetched host tool) — fetched via `third_party/node/update_node_binaries` (node v24.12.0). (Linux build then made moot by the Windows pivot — but node is a *host* build tool needed for the Windows cross-build too.)

### 8.4 Windows cross-compile approach (in progress)
Cross-compiling Chromium for Windows on Linux uses **clang-cl + lld-link** (already in the tree) but needs the **MSVC + Windows SDK** headers/libs. Chromium's official path packages them from a Windows machine (`package_from_installed.py`) — ruled out (SSH-only, local PC inadequate).

**Solution: `xwin`** — downloads Microsoft's official Windows 11 SDK + CRT directly from MS's CDN (license accepted) onto the Linux box, no Windows machine needed. **Done: 630 MB SDK/CRT splatted to `~/clearcoat/winsdk`.**

**Remaining wiring:** Chromium (with `DEPOT_TOOLS_WIN_TOOLCHAIN=0`) reads `GYP_MSVS_OVERRIDE_PATH` + `WINDOWSSDKDIR`, and for cross-builds loads include/lib paths from a `win_sdk/bin/SetEnv.x64.json` (normally produced by `package_from_installed.py`). xwin's flat sysroot must be mapped into that `package_from_installed`-style layout:
- xwin `crt/{include,lib/x86_64}` → `VC/Tools/MSVC/<ver>/{include,lib/x64}`
- xwin `sdk/{include,lib}/...` → `win_sdk/{Include,Lib}/<sdkver>/...`
- synthesize `win_sdk/bin/SetEnv.x64.json` (INCLUDE = VC include + sdk um/shared/ucrt/winrt; LIB = VC lib/x64 + sdk um/x64 + ucrt/x64).
Then set env + `target_os="win"` `target_cpu="x64"` in `args.gn` → `gn gen` → `ninja chrome` → `chrome.exe`. (`rc.exe`/`midl` handled by Chromium's checked-in `third_party/win_build_output` + prebuilt linux `rc`.)

### 8.5 Current `args.gn` (Linux baseline; Windows adds target_os/cpu)
```
# ungoogled flags.gn (de-Google) + enable_widevine=false
is_debug=false
symbol_level=0
blink_symbol_level=0
is_official_build=false   # → switch to true for stealth-grade release (true-to-Chrome optimization)
is_component_build=false
use_remoteexec=false
target_cpu="x64"
proprietary_codecs=true
ffmpeg_branding="Chrome"
# Windows cross adds: target_os="win"
```

---

## 9. Open decisions & next steps

1. **Port source:** 142 now vs wait for 144 source (shorter hop).
2. **Release build:** flip `is_official_build=true` for production (matches real Chrome optimization → better TLS/behavior parity).
3. **Deployment target for GPU persona:** real-Windows-host (ANGLE-D3D11 / Intel UHD 770) vs headless (ANGLE-SwiftShader) — pick per deployment.
4. **Immediate next step:** finish the xwin→gn Windows wiring → first `chrome.exe` cross-build → verify Playwright drives it via `executablePath` (the hard drop-in requirement) → then layer adryfish 142→149 → then PATCHES.md P0.

### Required end-state: Playwright drop-in (hard requirement)
clearcote must work exactly like CloakBrowser: a `launch()` returning a standard Playwright `Browser`, plus raw `chromium.launch(executablePath=...)`. Python + JS SDKs that fetch → verify (SHA-256) → cache → launch. Reproducible build + Sigstore/cosign attestation = the "100% open + verifiable" trust differentiator.

---

## 10. Box file inventory

```
~/research/                 16 reference repos (brave-core, cromite, fingerprint-chromium,
                            ungoogled-chromium-149, CloakBrowser, chromiumfish, BotBrowser,
                            camoufox, invisible_playwright, kameleo,
                            undetectable-fingerprint-browser, rebrowser-patches,
                            patchright-python, nodriver, SeleniumBase, undetected-chromedriver)
~/clearcoat/build/src       vanilla ungoogled-149 source tree (pruned, patched)
~/clearcoat/build/src/out/Release/gn_build/gn   in-tree-built gn binary
~/clearcoat/winsdk          xwin: Windows 11 SDK + CRT (630 MB)
~/clearcoat/bin/            gn, xwin
~/clearcoat/logs/           build/toolchain logs + status files
~/clearcoat/*.sh            prepare/toolchain build scripts
~/depot_tools/              depot_tools (gn/ninja wrappers unusable as root; use in-tree gn)
```

---

*Companion documents: `RESEARCH.md` (landscape synthesis) · `PATCHES.md` (file-level stealth-patch manifest, 66 patches, real Chromium 149 targets).*
