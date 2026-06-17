# AGENTS.md — guide for contributors (human and automated)

This repository is structured so that both people and automated coding tools can navigate, build, and contribute to it without guesswork. If you're an automated contributor, read this first.

## What this project is

Clearcote Browser: an open-source, reproducible Chromium build with engine-level privacy/identity controls. Base is ungoogled-chromium; changes are delivered as readable source patches. Target platform is Windows x64 first; the build can be produced on Linux (native or cross-compile).

## Repository layout

```
clearcote-browser/
├── README.md            # what/why, quickstart, credits
├── ROADMAP.md           # phased plan
├── AGENTS.md            # this file
├── CONTRIBUTING.md      # contribution workflow
├── CREDITS.md           # upstream attributions + licenses
├── DISCLAIMER.md        # responsible use + legal
├── UPSTREAM_REVISION    # pinned Chromium revision the patches target
├── build.sh             # one-shot Linux cross-build orchestrator
├── scripts/             # numbered build stages (fetch, toolchain, windows-sdk, build, package)
├── patches/             # Clearcote's source patches (unified diffs) + series
├── config/              # args.gn and build configuration
└── docs/
    ├── BUILDING.md          # build from source
    ├── VERIFY.md            # verify a release
    ├── RELEASING.md         # maintainer release runbook (canonical)
    ├── PATCHES.md           # patch-set overview
    ├── RESEARCH.md          # design research
    └── RESEARCH-DOSSIER.md  # deep build/stealth dossier
```

## Conventions

- **Patches over forks.** Changes to Chromium are unified diffs under `patches/`, listed in `patches/series`, applied with the ungoogled `patches.py` tooling. Keep one logical change per patch with a descriptive name and a header comment explaining *why*.
- **Pin everything.** Patches target the revision in `UPSTREAM_REVISION`. If you bump it, re-test the whole series and update both together.
- **Engine-level, not JS injection.** Identity/privacy behavior belongs in compiled code, not in injected scripts.
- **No secrets, no blobs.** Never commit credentials, tokens, or opaque binaries. The tree is source + text patches only.
- **Privacy-relevant patches are mandatory.** Do not drop a privacy/identity patch to make a build pass; fix the patch.

## Common commands

```bash
# Apply the full patch set onto a prepared Chromium source tree
python3 <ungoogled>/utils/patches.py apply build/src ./patches

# Configure + build
cp config/args.gn build/src/out/Default/args.gn
( cd build/src && gn gen out/Default && ninja -C out/Default chrome )

# Hash an artifact
sha256sum out/Default/<artifact>
```

See [docs/BUILDING.md](docs/BUILDING.md) for the complete pipeline and [docs/VERIFY.md](docs/VERIFY.md) for verification.

## Adding or fixing a patch

1. Make the source change in `build/src`.
2. Generate a clean unified diff and place it under `patches/` with a clear name.
3. Add it to `patches/series` in the right order.
4. Re-run a full apply on a clean tree to confirm it applies without rejects.
5. Document the intent in the patch header and your PR.

## Ground rules for automated contributors

- Prefer small, reviewable PRs with a clear description of intent and verification steps.
- Never introduce network calls that phone home or fetch unverified code at runtime.
- If a change affects what the browser exposes to websites, say so explicitly and explain the privacy reasoning.
- When in doubt, open an issue to discuss before large changes.
