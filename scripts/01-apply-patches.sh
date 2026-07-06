#!/usr/bin/env bash
# 01 — apply the patch series to the pruned Chromium source tree, in order:
#       1. ungoogled-chromium base            (de-Google)                     — both targets
#       2. ungoogled-chromium-windows overlay (Windows build patches)         — WINDOWS only
#       3. Clearcote patch set (this repo's patches/, listed in patches/series) — both targets,
#          except 900-windows-build-fixes.patch which is Windows-only.
#
# For the Linux target the Windows overlay is skipped: its command-id/build patches assume a
# Windows target, and on Linux the vanilla upstream guards are already correct. Every patch is a
# plain unified diff (-p1) against the pinned revision in UPSTREAM_REVISION.
#
#   TARGET  windows | linux   (default: windows)
#   WORK    working dir (default: ~/clearcote-build)
#   REPO    path to this repository (default: parent of this script)
set -euo pipefail
TARGET="${TARGET:-windows}"
WORK="${WORK:-$HOME/clearcote-build}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC="$WORK/build/src"
UG="$WORK/ungoogled-chromium"
UGW="$WORK/ungoogled-chromium-windows"
PATCHES_PY="$UG/utils/patches.py"

[ -d "$SRC" ] || { echo "FATAL: source tree $SRC not found — run scripts/00-fetch-source.sh first"; exit 1; }
[ -f "$PATCHES_PY" ] || { echo "FATAL: $PATCHES_PY not found — run scripts/00-fetch-source.sh first"; exit 1; }

# 1. ungoogled base (both targets)
echo "  applying: ungoogled-chromium base (de-Google)"
python3 "$PATCHES_PY" apply "$SRC" "$UG/patches"

# 2. ungoogled-chromium-windows overlay (Windows only)
if [ "$TARGET" = "windows" ]; then
  echo "  applying: ungoogled-chromium-windows overlay"
  python3 "$PATCHES_PY" apply "$SRC" "$UGW/patches"
fi

# 3. Clearcote patch set, in patches/series order (-p1). 900-windows-build-fixes.patch is
#    Windows-only (touches .rc + build/config/clang/BUILD.gn against the overlay state).
echo "  applying: Clearcote patch set (patches/series, target=$TARGET)"
while IFS= read -r line; do
  p="${line%%#*}"; p="$(printf '%s' "$p" | tr -d '[:space:]')"; [ -z "$p" ] && continue
  if [ "$TARGET" = "linux" ] && [ "$p" = "900-windows-build-fixes.patch" ]; then
    echo "    skip (linux): $p"; continue
  fi
  echo "    $p"
  patch -p1 -s -d "$SRC" < "$REPO/patches/$p"
done < "$REPO/patches/series"

echo "OK: patch series applied to $SRC (target=$TARGET)"
echo "next -> scripts/02-host-toolchain.sh"
