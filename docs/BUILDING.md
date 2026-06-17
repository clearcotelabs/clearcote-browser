# Building Clearcote from source

Clearcote is meant to be rebuilt by anyone with a capable Linux box. This guide reproduces the published **Windows x64** binary by **cross-compiling on Linux** — no Windows machine required. **You should be able to produce a binary yourself and confirm it matches a published release** (see the reproducibility note at the end).

> Clearcote = Chromium → ungoogled-chromium (de-Google) → Windows overlay + Clearcote fingerprint patches → cross-build. Every layer is open and pinned.

## Pinned versions (what this release was built from)

| Component | Pin |
|---|---|
| Chromium | `149.0.7827.114` (see [`UPSTREAM_REVISION`](../UPSTREAM_REVISION)) |
| ungoogled-chromium | tag `149.0.7827.114-1` (commit `cf82700725f439e130f7b6290e7d5c6425585081`) |
| ungoogled-chromium-windows | tag `149.0.7827.114-1.1` |
| Toolchain | Chromium in-tree clang/lld (clang-cl + lld-link for the Windows target) |
| Windows SDK / CRT | Microsoft SDK `10.0.26100` + CRT, fetched on Linux via [xwin](https://github.com/Jake-Shadle/xwin) `0.9.0` |

## Prerequisites

- 64-bit Linux host. **~16 GB+ RAM** (32 GB recommended — the link step is RAM-hungry; a large swap file helps), **~120 GB free disk**, a many-core CPU, and **several hours**.
- `git`, `python3`, `curl`, `ninja`, `zip`, and **`ciopfs`** (case-insensitive FUSE, for the MSVC headers). The rest of Chromium's system build packages are installed by `scripts/02` via `build/install-build-deps.py` (Debian/Ubuntu; run as root or with sudo — other distros: install the equivalents).
- No system MSVC and no Windows machine — the toolchain is assembled from Microsoft's redistributable SDK packages via `xwin` (you accept Microsoft's license when xwin downloads them; Clearcote ships none of them).

## The fast path (scripted)

```bash
git clone https://github.com/clearcotelabs/clearcote-browser.git
cd clearcote-browser
WORK=~/clearcote-build ./build.sh
```

[`build.sh`](../build.sh) runs the six stages below in order and drops the packaged zip in `$WORK/dist`. You can also run the stages individually from [`scripts/`](../scripts):

| Stage | Script | What it does |
|---|---|---|
| 00 | `scripts/00-fetch-source.sh` | clone the pinned ungoogled tooling, retrieve + unpack Chromium 149, prune binaries |
| 01 | `scripts/01-apply-patches.sh` | apply the patch series (ungoogled base + windows overlay + Clearcote fingerprint) |
| 02 | `scripts/02-host-toolchain.sh` | fetch clang/rust/sysroot/node; build `gn` from in-tree source |
| 03 | `scripts/03-windows-sdk.sh` | xwin → assemble the `package_from_installed`-style Windows SDK/CRT sysroot |
| 04 | `scripts/04-configure-build.sh` | copy `config/args.gn`, `gn gen`, `ninja … chrome` |
| 05 | `scripts/05-package.sh` | zip the runtime + bundle the VC++ DLLs → `$WORK/dist` |

## 1. Source

`scripts/00-fetch-source.sh` checks out the pinned `ungoogled-chromium` and `ungoogled-chromium-windows` tags, then uses ungoogled's `downloads.py` to retrieve and unpack the exact Chromium 149 tarball (~1.5 GB download → ~10 GB unpacked) and prunes non-source binaries.

## 2. Patches

The identity/privacy behavior is defined **entirely by the patch set** — read it; that *is* the product. The series is, in order:

1. **ungoogled-chromium base patches** — removes Google integration/telemetry (`utils/patches.py apply`).
2. **ungoogled-chromium-windows overlay** — the Windows-specific patches.
3. **Clearcote fingerprint patches** — engine-level canvas/WebGL/audio/UA/fonts/etc. spoofing, in [`patches/`](../patches), listed in `patches/series`. See [PATCHES.md](PATCHES.md).

`scripts/01-apply-patches.sh` applies them with the ungoogled patch tooling. Every patch is a plain unified diff against the pinned revision.

> **Domain substitution** (ungoogled's URL-rewriting hardening) is intentionally **not** applied in the default flow: it rewrites Google download URLs and breaks the toolchain fetch in stage 02. Apply it (optional) *after* stage 02 if you want it, and re-run `gn gen`.

## 3. Host toolchain

`scripts/02-host-toolchain.sh` installs the system build deps (`install-build-deps.py`), reverts ungoogled domain substitution if it was applied (it breaks the toolchain download URLs), then fetches Chromium's clang/lld (the same compiler cross-targets Windows), Rust, the amd64 sysroot, and the host `node`. It then builds `gn` from the in-tree source — depot_tools' prebuilt `gn` won't bootstrap its Python as root, so we bootstrap with the tree clang, locate the produced binary, and copy it to `$WORK/bin/gn`.

## 4. Windows SDK toolchain (on Linux)

`scripts/03-windows-sdk.sh` downloads `xwin` and splats Microsoft's Windows SDK + CRT, then maps that flat payload into the layout Chromium expects from `package_from_installed.py`:

- xwin `crt/{include,lib/x86_64}` → `VC/Tools/MSVC/<ver>/{include,lib/x64}`
- xwin `sdk/{include,lib}/…` → `Windows Kits/10/{Include,Lib}/<sdkver>/…`
- a synthesized `Windows Kits/10/bin/SetEnv.x64.json` (the INCLUDE/LIB/PATH the cross-build reads)
- the SDK **`Include`** tree is served through a **`ciopfs`** case-insensitive mount (the Lib tree isn't — xwin already emits per-case symlinks for libs)

The build then runs with **`DEPOT_TOOLS_WIN_TOOLCHAIN=1`** (not 0) pointing `GYP_MSVS_OVERRIDE_PATH` at this synthetic tree.

## 5. Configure + build

`scripts/04-configure-build.sh` copies [`config/args.gn`](../config/args.gn) into `out/Default`, runs `gn gen`, and `ninja -C out/Default chrome`. The config is the exact `args.gn` the release used (Windows x64, de-Googled, `proprietary_codecs=true`, `is_official_build=false`).

## 6. Package + verify

`scripts/05-package.sh` zips the runtime (chrome.exe, chrome.dll, paks, ICU, locales, ANGLE/SwiftShader DLLs) and bundles the five VC++ runtime DLLs so it launches on a clean Windows box. Then **sign + publish** per [RELEASING.md](RELEASING.md) and verify a download per [VERIFY.md](VERIFY.md).

## Gotchas (the cross-build journey, distilled)

These bit us; the scripts handle them, but know they exist:

- **Domain substitution vs the toolchain fetch** — rewrites `commondatastorage.googleapis.com` in `tools/clang/scripts/update.py`; revert (or defer) it before fetching clang/rust. (handled: stage 02 runs before any domain-sub.)
- **depot_tools `gn` won't bootstrap Python as root** — stage 02 builds `gn` from `tools/gn/bootstrap/bootstrap.py` with the tree clang on `PATH` (no `-o`, to avoid the clobber where the `gn` binary overwrites the target dir), then locates and copies the produced binary to `$WORK/bin/gn`.
- **Windows-only `checkout_win` sub-DEPS** — `downloads.py` unpacks only the base tarball, so `third_party/microsoft_webauthn/src` and `third_party/microsoft_dxheaders/src` are missing and the link fails. Stage 00 clones them at their pinned DEPS revisions.
- **MSVC header casing** — the SDK headers `#include` each other with inconsistent casing; stage 03 serves the SDK `Include` tree through a **`ciopfs`** case-insensitive mount so they resolve (the Lib tree doesn't need it).
- **Domain substitution vs the toolchain fetch** — it rewrites Google download URLs; stage 02 reverts it (if applied) before fetching clang/rust.
- **Open-file limit** — the link step needs a high limit; `ulimit -n 1048576` (handled in stage 04).
- **Host `node`** — normally pulled by gclient runhooks; we fetch it directly (it's a shell script, not Python) since we don't run gclient.
- **Rust std** — `rust_sysroot_absolute=""` builds std from source rather than expecting a prebuilt absolute sysroot.
- Deeper one-offs encountered (an out-of-date UIA header, a missing `CLSID`, pruned MIDL `.tlb`, lib exact-casing) and the full narrative are in [RESEARCH-DOSSIER.md](RESEARCH-DOSSIER.md) §8.

## Reproducibility (honest scope)

Same pinned revision + same patch set + same `config/args.gn` → a functionally identical build. Chromium cross-builds are **not yet bit-for-bit deterministic** (embedded build paths, timestamps, linker/PGO nondeterminism), so a byte-identical hash match is the *goal*, not a guarantee today — what you can fully audit now is that every change is a readable patch and the config is public. A reproducible/attested build is tracked in [ROADMAP.md](../ROADMAP.md), Phase 4. See [VERIFY.md](VERIFY.md).
