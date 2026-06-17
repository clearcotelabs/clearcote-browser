#!/usr/bin/env bash
# 01 — apply the full patch series to the pruned Chromium source tree, in order:
#       1. ungoogled-chromium base  (de-Google)
#       2. ungoogled-chromium-windows overlay  (Windows build patches)
#       3. Clearcote fingerprint set  (this repo's patches/, listed in patches/series)
#
# All three are applied with ungoogled's quilt-style patch tool (-p1). Every patch is a
# plain unified diff against the pinned revision in UPSTREAM_REVISION.
#
#   WORK   working dir (default: ~/clearcote-build)
#   REPO   path to this repository (default: parent of this script)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC="$WORK/build/src"
UG="$WORK/ungoogled-chromium"
UGW="$WORK/ungoogled-chromium-windows"
PATCHES_PY="$UG/utils/patches.py"

[ -d "$SRC" ] || { echo "FATAL: source tree $SRC not found — run scripts/00-fetch-source.sh first"; exit 1; }
[ -f "$PATCHES_PY" ] || { echo "FATAL: $PATCHES_PY not found — run scripts/00-fetch-source.sh first"; exit 1; }

apply() { echo "  applying: $2"; python3 "$PATCHES_PY" apply "$SRC" "$1"; }

apply "$UG/patches"             "ungoogled-chromium base (de-Google)"
apply "$UGW/patches"            "ungoogled-chromium-windows overlay"
apply "$REPO/patches"           "Clearcote fingerprint set (patches/series)"

echo "OK: full patch series applied to $SRC"
echo "next -> scripts/02-host-toolchain.sh"
