#!/usr/bin/env bash
# 00 — fetch the pinned ungoogled-chromium tooling + Chromium source, then prune binaries.
# Run on a capable Linux host. Produces a pruned, vanilla Chromium 149 source tree.
#
#   WORK   working dir (default: ~/clearcote-build)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
UG_TAG="149.0.7827.114-1"        # ungoogled-chromium tag -> pins Chromium 149.0.7827.114
UGW_TAG="149.0.7827.114-1.1"     # ungoogled-chromium-windows overlay tag
mkdir -p "$WORK"; cd "$WORK"

# pinned tooling (the exact tags this release was built from)
[ -d ungoogled-chromium ] || git clone https://github.com/ungoogled-software/ungoogled-chromium.git
git -C ungoogled-chromium fetch --tags --quiet
git -C ungoogled-chromium -c advice.detachedHead=false checkout "$UG_TAG"

[ -d ungoogled-chromium-windows ] || git clone https://github.com/ungoogled-software/ungoogled-chromium-windows.git
git -C ungoogled-chromium-windows fetch --tags --quiet
git -C ungoogled-chromium-windows -c advice.detachedHead=false checkout "$UGW_TAG"

# retrieve + unpack the exact Chromium source the tag pins (~1.5 GB download, ~10 GB unpacked)
mkdir -p download_cache build
python3 ungoogled-chromium/utils/downloads.py retrieve -c download_cache -i ungoogled-chromium/downloads.ini
python3 ungoogled-chromium/utils/downloads.py unpack  -c download_cache -i ungoogled-chromium/downloads.ini -- build/src

# prune non-source binaries (ungoogled list)
python3 ungoogled-chromium/utils/prune_binaries.py build/src ungoogled-chromium/pruning.list

# Windows-only sub-DEPS (DEPS `condition: 'checkout_win'`) that downloads.py does NOT fetch —
# the build needs these or gn gen / link fails. Clone the exact pinned revisions.
CGIT="https://chromium.googlesource.com"
fetch_dep() { # url  rev  dest(relative to build/src)
  local url="$1" rev="$2" dest="build/src/$3"
  if [ ! -e "$dest/.clearcote-pinned" ]; then
    rm -rf "$dest"; git clone --quiet "$url" "$dest"
    git -C "$dest" -c advice.detachedHead=false checkout --quiet "$rev"
    touch "$dest/.clearcote-pinned"
  fi
}
fetch_dep "$CGIT/external/github.com/microsoft/webauthn.git"        273689d1d54232f0c316b31f596e7928acb1cd5a third_party/microsoft_webauthn/src
fetch_dep "$CGIT/external/github.com/microsoft/DirectX-Headers.git" 62c23d5ec700659453c6fe89d296554b2a5e7edc third_party/microsoft_dxheaders/src

echo "OK: pruned Chromium 149 source + win sub-DEPS at $WORK/build/src"
echo "next -> scripts/01-apply-patches.sh   (patch series: ungoogled base + windows overlay + clearcote fingerprint; see patches/ and docs/BUILDING.md)"
