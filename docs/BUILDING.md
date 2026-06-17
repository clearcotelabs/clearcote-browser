# Building Clearcote from source

Clearcote is meant to be rebuilt by anyone. This guide describes the full pipeline at a level you can follow end to end. The goal is simple: **you should be able to produce a binary yourself and confirm it matches a published release.**

> Clearcote = upstream Chromium → ungoogled-chromium (de-Google) → Clearcote patches → reproducible build. Every layer is open.

## Overview

1. Fetch a pinned Chromium source tarball.
2. Prune non-source binaries (ungoogled tooling).
3. Apply the ungoogled-chromium patch set (removes Google integration/telemetry).
4. Apply the Clearcote patch set (engine-level identity & privacy controls).
5. Configure the build (`args.gn`).
6. Compile with the Chromium clang/lld toolchain.
7. Package + checksum the output.

The exact upstream revision is pinned in [`UPSTREAM_REVISION`](../UPSTREAM_REVISION) (and mirrored by the ungoogled version used). Pinning is what makes the result reproducible.

## Prerequisites

- A 64-bit Linux build host (recommended) or Windows. ~100 GB free disk, 16 GB+ RAM (32 GB recommended), and a multi-core CPU.
- `git`, `python3`, `curl`, and a recent `ninja`.
- The Chromium toolchain (clang/lld, Rust, sysroot) — fetched by the helper scripts below; no system compiler version games.

Windows binaries can be produced **natively on Windows** (Visual Studio Build Tools + Windows SDK) or **cross-compiled from Linux** using clang-cl/lld-link against a Windows SDK sysroot. Both paths are supported; the cross-build keeps the whole pipeline on one machine.

## 1–4: Source + patches

```bash
# 1. Get the de-Googled base + patch tooling (pinned to the Clearcote target version)
git clone https://github.com/ungoogled-software/ungoogled-chromium.git
git -C ungoogled-chromium checkout <PINNED_TAG>

# 2. Retrieve + unpack the matching Chromium source tarball
mkdir -p build/download_cache
python3 ungoogled-chromium/utils/downloads.py retrieve -c build/download_cache -i ungoogled-chromium/downloads.ini
python3 ungoogled-chromium/utils/downloads.py unpack   -c build/download_cache -i ungoogled-chromium/downloads.ini -- build/src

# 3. Prune + apply ungoogled patches
python3 ungoogled-chromium/utils/prune_binaries.py build/src ungoogled-chromium/pruning.list
python3 ungoogled-chromium/utils/patches.py apply  build/src ungoogled-chromium/patches

# 4. Apply Clearcote's patch set (this repo)
python3 ungoogled-chromium/utils/patches.py apply  build/src ./patches
```

Every file under [`patches/`](../patches) is a plain unified diff. Read them. That *is* the product.

## 5: Configure

A documented `args.gn` lives in [`config/`](../config). It enables a clang build, disables Google API keys, and turns on Clearcote's identity controls. Copy it into your output dir:

```bash
mkdir -p build/src/out/Default
cp config/args.gn build/src/out/Default/args.gn
```

## 6: Build

```bash
cd build/src
gn gen out/Default
ninja -C out/Default chrome
```

For a Windows cross-build from Linux, the toolchain is clang-cl + lld-link with a Windows SDK sysroot; the build configuration and helper scripts for assembling that sysroot are documented in [`config/`](../config). (Producing the SDK sysroot uses Microsoft-provided redistributable packages under Microsoft's own license — Clearcote ships none of them.)

## 7: Output + checksums

The browser binary lands in `out/Default`. Package it and record hashes:

```bash
sha256sum out/Default/<artifact> > <artifact>.sha256
```

Then compare against the published checksums for that release — see [VERIFY.md](VERIFY.md).

## Notes

- **No hidden steps.** If a build needs an external helper binary (e.g. a resource compiler or JS bundler), it's a standard Chromium build dependency fetched from its public source — documented, not smuggled in.
- **Reproducibility.** Same pinned revision + same patch set + same toolchain → matching output. Divergence is a bug; please file it.
- This guide is intentionally engine-focused. For the developer-facing SDK, see the [Roadmap](../ROADMAP.md).
