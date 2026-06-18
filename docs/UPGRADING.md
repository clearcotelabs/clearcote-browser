# Upgrading clearcote: Chromium 149 → 150 — Canonical Plan

`$BOX` = build host (SSH). All build work happens on `$BOX` only; `gh`/`git push`/releases run from the local Windows PC. Repo clone referenced as `/tmp/ccrepo`; build tree as `~/clearcoat/build/src`. This plan generalizes to any major bump (N → N+1).

---

## 1. TL;DR / Critical Path

The few things that gate everything:

1. **Wait for the ungoogled-150 base tag to exist.** As of 2026-06-17 it does NOT (latest is `149.0.7827.114-1`). Chrome 150 stable targets 2026-06-30; expect the ungoogled `150.*` tag early-to-mid July 2026. **Do not start the real port against a moving Dev target.** (See §2.)
2. **Re-validate patch `900-windows-build-fixes` FIRST** once you have a 150 tree. A break here blocks the *entire* cross-build — fix the toolchain/`.rc`/UIA-CLSID hunks before touching any source-fingerprint patch.
3. **Re-port the 6 high-fragility patches** (`010` UA, `040` fonts, `050` clientRects, `060` canvas, `070` webgl-gpu, plus `100` webrtc) — this is where the human/agent hours go.
4. **One detached `ninja -k 0` build** in the existing `out/Default` (incremental, never clean).
5. **Validate the fingerprint defenses actually still work** (not just "it compiled") — Skia Graphite is the #1 silent-breakage risk this bump.

**Realistic time split:**
- **Unavoidable machine time:** full 150 source fetch+unpack (~1.5 GB → ~10 GB) + first full build = ~multi-hour to ~2 days of wall-clock ninja. This is fixed cost; overlap it with nothing-blocking work.
- **Human/agent effort:** patch-porting + reject resolution. With the parallel one-agent-per-reject pass (how 142→149 was done), budget **~0.5–1.5 days** of active work, dominated by the 6 high-fragility patches + the `fingerprint_data.h` version hand-bump + Graphite re-validation.

Net: the build is the long pole in wall-clock; the porting is the long pole in *attention*. Kick the full build off detached early, port in parallel while it runs.

---

## 2. Prerequisites & Go/No-Go

| Dependency | Status (2026-06-17) | Decision |
|---|---|---|
| **ungoogled-chromium 150 tag** | none (latest `149.0.7827.114-1`) | **GATE.** See decision tree below. |
| **ungoogled-chromium-windows 150 overlay** | none (latest `149.0.7827.114-1.1`) | Lands hours-to-days after the core tag. Confirm it exists before pinning `UGW_TAG`. |
| **adryfish 150** | dead end (stuck at 144) | **Ignore.** Do not wait on it. The clearcote-on-149 stack IS the authoritative base; re-port `142→150` forward from clearcote, not from adryfish. |
| **Chromium 150 tarball** | M150 branch = **7846**; Dev is `150.0.7846.4`; stable will be `150.0.7846.NN` | Get exact version/hash from ungoogled's `chromium_version.txt` + `downloads.ini` once their tag lands — NOT from chromiumdash guesses. |
| **Windows SDK 10.0.26100 / xwin toolchain** | present on `$BOX` | **REUSE.** Decoupled from Chromium. Only overlay a newer header if 150 forces it (149 already needed a `UIAutomationCore.h` overlay — re-check). |

**Go/no-go decision tree:**

- **If ungoogled-150 tag exists** → GO. Pull the exact stable version from their `chromium_version.txt`, proceed to §4.
- **If it does NOT exist yet and the bump is not urgent** → **WAIT.** Diffing against Dev wastes effort (anchors move under you). Re-check the [tags page](https://github.com/ungoogled-software/ungoogled-chromium/tags) and the 150 tracking issue weekly.
- **If it does NOT exist and you must move now** → **port the ungoogled deltas yourself**: take the upstream Chromium `150.0.7846.NN -lite` tarball directly, apply ungoogled's `master`-branch patch set (de-Google) against it manually, then the windows overlay, then clearcote. Higher effort and you eat the ungoogled-rebase risk yourself — only do this if the schedule truly demands it.

> Cadence note: this is the **last comfortable (~4-week) bump**. Chrome moves to a **two-week** milestone cadence from M153 (~2026-09-08). Consider scripting the re-port now.

---

## 3. Version-Pin Checklist (every "149" to bump)

Load-bearing pins are **bold** — these actually select source / are trust anchors. The rest are documentation/cosmetic but must be done for correctness.

**Source selectors (load-bearing):**
- [ ] **`scripts/00-fetch-source.sh` `UG_TAG`** `149.0.7827.114-1` → `150.0.7846.NN-1` ← *actually selects Chromium*
- [ ] **`scripts/00-fetch-source.sh` `UGW_TAG`** `149.0.7827.114-1.1` → `150.0.7846.NN-1.1` (confirm overlay tag exists)
- [ ] **`scripts/00-fetch-source.sh` sub-DEPS** re-read 150's `DEPS`: `microsoft_webauthn` + `microsoft_dxheaders` → update both SHAs
- [ ] `UPSTREAM_REVISION` `149.0.7827.114` → `150.0.7846.NN` (doc source-of-truth)

**Content-correctness (easy to forget, detectable if missed):**
- [ ] **`components/ungoogled/fingerprint_data.h`** hand-bump hardcoded `149.0.7827.*` Chromium + Edge/Opera/Vivaldi versions → 150 release numbers. *Stale = internally-inconsistent spoofed UA/UA-CH = detectable.* (This is patch `001` content, not a reject.)
- [ ] `config/args.gn` — no version string, but **diff against 150's ungoogled `flags.gn`** and fix any gn-rejected/renamed args (`v8_drumbrake_bounds_checks`, `dawn_use_built_dxc`, `blink_symbol_level`, `chrome_pgo_phase`, …)

**Artifact / release (recompute from the 150 zip — do NOT guess hashes):**
- [ ] `scripts/05-package.sh` `V` default `149.0.7827.114` → new (or pass `V=` inline); update the manifest comment
- [ ] **`sdk/node/src/release.ts`** `tag`, `version`/`asset`/`url`, **recompute `sha256` (zip) + `exeSha256` (chrome.exe) + `size`** from the 150 artifact
- [ ] `sdk/node/package.json` `version` → bump (independent of Chromium)
- [ ] **`sdk/python/clearcote/release.py`** `tag`/`version`/`asset`/`url`, **recompute `sha256`/`exe_sha256`/`size`** — byte-identical to node
- [ ] `sdk/python/pyproject.toml` `version` → bump

**Docs / cosmetic:**
- [ ] `docs/BUILDING.md` three pins + ungoogled commit SHA, prose
- [ ] `docs/RELEASING.md` worked-example values; **fix the `149.*.manifest` grep guard**
- [ ] `README.md` — badge `Chromium-149`→`150`, status/tag, audit-table Build/UA/UA-CH (re-run CreepJS audit), asset name. *Note UA-CH minor ≠ build patch — refresh from the real 150 binary.*
- [ ] `scripts/creepjs_audit.py` hardcoded `Build 149.0.7827.114` → read from `chrome/VERSION`/`UPSTREAM_REVISION` instead of hardcoding
- [ ] `build.sh` stage banner `149`→`150` (cosmetic)
- [ ] `docs/PATCHES.md`, `RESEARCH-DOSSIER.md`, `RESEARCH.md`, `patches/README.md` — re-verify regression assertions (TLS feature defaults, `time_clamper.h`, `ssl_client_socket_impl.cc` line numbers) against the 150 tree; `149`→`150`, port target becomes `142→150`

---

## 4. Step-by-Step Upgrade

All on `$BOX`, in the existing `~/clearcoat/build` tree (incremental — do **not** nuke `out/Default`).

1. **Pin sources** — bump `UG_TAG`/`UGW_TAG`/sub-DEPS SHAs (§3) once the 150 tags are confirmed live.
2. **Fetch + unpack (`00-fetch-source.sh`)** — clone/checkout ungoogled-150 + windows-150 overlay; `downloads.py retrieve+unpack` the exact 150 tarball into `build/src`; `prune_binaries.py` with `pruning.list`; `fetch_dep` the two Windows sub-DEPS at the 150-pinned SHAs.
3. **Restore MIDL `.tlb` files** — pruning deletes ~48 checked-in `.tlb` under `third_party/win_build_output`; **re-derive the 150 set** (it can shift) and restore from the 150 tarball. Cross MIDL can't regenerate them.
4. **Apply patch layers (`01-apply-patches.sh`)** — (1) ungoogled base, (2) windows overlay, (3) clearcote `patches/` series. Expect rejects in layer 3 → §5.
5. **Reuse the toolchain — do NOT refetch.** Keep `~/clearcoat/wintoolchain` (MSVC + SDK 10.0.26100, `SetEnv.x64.json`, ciopfs include mount), the winsdk, host sysroot, host node. **Re-check only:** does 150 need a newer SDK header overlay? (149 needed `UIAutomationCore.h` + a hand-defined CLSID.)
6. **Let the fresh in-tree toolchain come with 150 (`02-host-toolchain.sh`)** — clang/lld, Rust, gn are pinned BY the 150 checkout and refetch automatically. **Update the `clang_rt.builtins` drop path**: the clang major dir will likely bump with 150's clang roll → drop `clang_rt.builtins-x86_64.lib` into `third_party/llvm-build/Release+Asserts/lib/clang/<NEW_N>/lib/windows/`.
7. **Configure (`04-configure-build.sh`)** — `ulimit -n 1048576`; tree clang/lld on PATH; `DEPOT_TOOLS_WIN_TOOLCHAIN=1` (NOT 0), `GYP_MSVS_OVERRIDE_PATH`, `WINDOWSSDKDIR`, `GYP_MSVS_VERSION=2026`, `WDK_DIR=`; copy validated `args.gn`; `gn gen out/Default` (fix any flag-drift errors here, §3).
8. **Build** — §6 (single detached `ninja -k 0`).
9. **Validate** — §7. Do not ship on a clean compile alone.
10. **Package (`05-package.sh`)** — confirm the auto-derived `150.0.7846.NN.manifest` is actually in the zip (the `*.manifest` glob is version-agnostic — good).
11. **Re-cut release + bump SDKs** — §8.

---

## 5. Efficient Patch-Port Strategy

**Order — touch the riskiest first so rejects surface early and parallelize:**

1. **`900-windows-build-fixes` (build-blocker, HIGH)** — validate before anything else. Re-port the `tool_wrapper.py` `ExecRcWrapper` rewrite, the `clang_lib("compiler_builtins")` `libname="builtins"` for `is_win`, the `.rc` `BUILDFLAG()` guards. **Watch for the UIA-CLSID flipping from missing-symbol → *duplicate*-symbol** if 150 raises the SDK floor and defines `CLSID_CUIAutomationClientInfoSource` itself.
2. **Known recurring break points (check first, cheap):** `090-timezone` (`timezone_controller.cc` — WTF `String::FromUtf8` rename history; re-check for another rename + `SetIcuTimeZoneAndNotifyV8` signature + lazy-timezone-init gate) and `030-hardware-concurrency` (the old `std::stoull` crash, already fixed to `base::StringToUint` — confirm it stuck).
3. **The 6 high-fragility patches:** `010-user-agent`, `040-fonts`, `050-shadow-dom`, `060-canvas`, `070-webgl-gpu`, `100-webrtc-leak`.
4. **Dependency-ordered within the set:** apply **`080-client-rects` before `050`** (080 defines a `QuadF` helper that 050 calls — if 080 fails, `element.cc`/`range.cc` won't compile). **`060` before `070`** (060's `ShuffleSubchannelColorData` gains a `uint64_t` seed that 070's ReadPixels consumes — coupled signature).

**Mechanics:**

- **Use `git apply --3way` / `patch --merge`** so conflicts land as in-file `<<<<<<<` markers instead of bare `.rej` — far faster to resolve in context. This is how 142→149 was done.
- **PARALLEL one-agent-per-reject pass:** fan out one agent per rejected hunk/file. They work independently; you serialize only the coupled pairs (080→050, 060→070) and the shared `fingerprint_data.h`.
- **Compile-check each changed TU before the full relink:** `ninja -C out/Default path/to/file.o` so type/API-rename errors surface in **seconds**, not after a multi-hour link. Hit the high-risk `.o`s first: `webgl_rendering_context_base.o`, `static_bitmap_image.o`, `html_canvas_element.o`, `user_agent_utils.o`, `user_agent_metadata.o`, `font_cache.o`, `stun_port.o`.

**Pre-empt the known API churn (fix proactively, don't wait for the error):**

- **Skia Graphite/Dawn** is the highest-risk surface. `060`/`070` canvas+WebGL readback were written against legacy **Ganesh**; re-validate the paint/readback path under Graphite — *a clean build does not mean the noise injection still fires.*
- **`base` span helpers** (`base::byte_span_from_ref`, span APIs) churn — likely break in `farble_seed.cc` (`001`).
- **`010` struct fields:** `blink::UserAgentBrandList` / `UserAgentMetadata` (`brand_version_list`, `brand_full_version_list`, `full_version`, `platform_version`, `architecture`); UA template + `Sec-CH-UA` brand version → **150**.
- **`040` `FontFaceSet` IDL** changed in 150 (`[LegacyNoInterfaceObject]` removed → global constructor); re-check font-enumeration hooks. Also `FontFaceCreationParams` / `GetOrCreateFontPlatformData` churn.
- **`060` paths:** Skia (`SkImages::RasterFromData`, `PaintImage::GetNextContentId`) + `partition_alloc::MaxDirectMapped()` include path (already moved once — likely again).
- **`100` webrtc** is a *separately-rolled* dep — a 150 bump pulls a different libwebrtc revision; the `AddAddress(...)` arg list + `IceCandidateType::kSrflx`/`ICE_TYPE_PREFERENCE_SRFLX` and `P2PPortAllocator::Config` fields (`enable_multiple_routes`, `enable_nonproxied_udp`) can move independently. `110` (`v8/src/inspector`) same caveat — a silent signature change here re-exposes the `Runtime.enable` automation vector (correctness, not compile, regression).
- **Storage/quota note:** 150 rewrites IndexedDB onto SQLite — not in the clearcote stack today, but if any patch grows into storage/quota surfaces (e.g. the storage-quota spoof), expect collisions.

---

## 6. Build Efficiently

- **One detached build, `-k 0`:** `ninja -C out/Default -k 0 chrome` run detached on `$BOX` (e.g. `setsid`/`nohup`/`tmux`). `-k 0` keeps going past the first error so **all** compile failures surface in one pass instead of one-per-rebuild — invaluable right after a reject-heavy port.
- **Incremental only.** Re-port onto the **existing** `out/Default`. Never a clean ~2-day rebuild mid-iteration (project rule).
- **One ninja, one out dir, no concurrency.** Coordination rule: never run concurrent clearcote builds across sessions/boxes. One out dir, one ninja, build detached.
- **Raise FD limit before launching:** `ulimit -n 1048576` for both ciopfs and ninja, or deep WinRT include chains × parallel jobs blow past 1024 → "Too many open files".
- **Keep ccache/reclient if available** — unchanged TUs (most of the tree) hit cache; only re-ported files recompile.
- **Overlap the long build with nothing-blocking work:** while the first full build runs, do the §3 doc/SDK pin edits, draft the release notes, and pre-stage the validation scripts. Don't sit idle on ninja.
- **Triage with `ninja <file.o>` first** (per §5) so you never spend link time on a TU that won't compile.

---

## 7. Validation Gate (all must pass before shipping)

A clean build is **necessary but not sufficient** — Skia Graphite can silently neuter canvas/WebGL noise. Run on the freshly built 150 binary:

- [ ] **`validate_fp.py` → 6/6** — core fingerprint switches all firing.
- [ ] **`validate_full.py`** — proxy egress + **WebRTC IP coherence** (srflx rewritten to proxy egress, host candidates suppressed — verify it reports the proxy IP coherently, never just "disabled").
- [ ] **`release_verify.py`** — CreepJS + browserleaks + browserscan audit against the 150 binary.
- [ ] **Interactive-challenge spot-check** — real-world challenge passes.
- [ ] **Graphite-specific manual re-check:** canvas `toDataURL`/`getImageData`, WebGL `readPixels`, `UNMASKED_VENDOR/RENDERER`, and font enumeration each show per-eTLD+1 perturbation under the Graphite paint path. **If noise didn't fire, the patch compiled but the defense is dead — re-port against the Graphite path, do not ship.**
- [ ] **UA/UA-CH coherence:** spoofed Chrome/Edge/Opera/Vivaldi versions are internally consistent and read **150** everywhere (cross-check the `fingerprint_data.h` bump landed).

> Per project rule: never drop a stealth/anti-fingerprint patch to make a reject or a failing check go away. Fix it.

---

## 8. Release + Rollback

1. **Build the artifact**, package via `05-package.sh` (confirm `150.*.manifest` in the zip).
2. **Recompute trust anchors** from the *actual* 150 zip: `sha256` (zip), `exeSha256`/`exe_sha256` (chrome.exe inside), `size`. Never guess.
3. **Update both SDKs** (`release.ts` + `release.py`) with new `tag`/`version`/`asset`/`url`/hashes; bump SDK package versions (`package.json` / `pyproject.toml`). Keep node and python byte-identical.
4. **Follow `docs/RELEASING.md`** end-to-end; cut **`v0.1.x-pre.N`** for 150. The release tag is orthogonal to the Chromium version.
5. **Run from the LOCAL Windows PC** for `gh`/`git push`/release upload. `$BOX` has no `gh`/token. **No self `Co-Authored-By` trailer** on commits/PRs.
6. **Update the site** (`site/`): `llms.txt` + `llms-full.txt` + `app/` docs, and the repo README/docs, with the 150 features — for LLMs/agents AND humans.
7. **Rollback:** **KEEP the previous (149) release published.** If 150 regresses (esp. a Graphite fingerprint leak that slips past validation), pin the SDKs back to the `149.0.7827.114` asset + its tag — a one-line revert in `release.ts`/`release.py`. Don't delete the 149 artifact.

---

## 9. Time-Savers & Footguns (don't re-hit these)

**Cross-build footguns (all version-sensitive — re-confirm on 150):**
- **SxS manifest:** `chrome.exe` needs `<full-version>.manifest` at zip root or it dies with "side-by-side configuration is incorrect" (`spawn UNKNOWN` via Playwright). The `05-package.sh` `*.manifest` glob is fine; **fix the hardcoded `149.*.manifest` grep guard in `RELEASING.md`** and re-confirm the file is present.
- **ciopfs case-insensitive Include mount:** must stay up the whole build or `windows.h` not found. Mount path `Include/10.0.26100.0` — changes if 150 forces a newer SDK.
- **`DEPOT_TOOLS_WIN_TOOLCHAIN=1` (NOT 0):** counter-intuitive; `=0` routes to `vcvarsall.bat` (impossible on Linux). Linchpin of the whole cross-build.
- **`ulimit -n 1048576`** on both ciopfs and ninja.
- **MIDL `.tlb` restore** after pruning — re-derive the 150 file set.
- **`rc.py`:** the overlay's `windows-disable-rcpy.patch` is for *native* builds; the cross build needs `rc.py` + prebuilt Linux `rc` (re-pointed by `900`). Re-port if 150's overlay changes it.
- **`clang_rt.builtins` drop path** — clang major dir will change with 150's clang roll; update the drop dir or fp16 builtins (`__truncsfhf2`) go unresolved.
- **`UIAutomationCore.h` / CLSID** — re-verify 150 still needs the overlay + hand-defined CLSID; watch for the duplicate-symbol flip.
- **Keep in `args.gn`:** `rust_sysroot_absolute=""` (build std from source), `fatal_linker_warnings=false` (tolerate LNK4099).
- **Do NOT run `domain_substitution` before fetching the toolchain** — it rewrites Google download URLs and breaks clang/rust/sysroot fetch (`02` reverts it first).
- **gn must bootstrap in-tree** (depot_tools' prebuilt gn refuses to bootstrap as root) — `02` handles it; 150's gn source is used automatically.
- **Exact-casing `.lib` symlinks** — re-scan the 150 `.rsp` set; add symlinks for any lib xwin didn't case-fold ("could not open Foo.lib").
- **`args.gn` flag drift** — gn hard-errors on unknown args; diff against 150's `flags.gn` before `gn gen`.

**What NOT to do:**
- No clean rebuild mid-iteration (it's the ~2-day path; use incremental ninja).
- No concurrent ninjas / second out dir across sessions or boxes.
- Don't start the port against Chromium Dev — wait for the ungoogled 150 tag (§2).
- Don't wait on adryfish — it's abandoned for current versions; re-port clearcote-on-149 forward.
- Don't refetch the Windows SDK/MSVC toolchain — reuse it; only the in-tree clang/rust/gn come fresh with the checkout.
- Don't ship on a clean compile — Skia Graphite can silently kill canvas/WebGL/font noise; validate the defense, not just the build.
- Don't drop any stealth/anti-fingerprint patch to clear a reject.
- Don't guess the SDK hashes — recompute from the built 150 zip.
- Don't leak infra (build-host address / personal paths / personal emails) into any public repo, commit, or release.
