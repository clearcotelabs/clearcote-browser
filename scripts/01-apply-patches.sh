#!/usr/bin/env bash
# 01 — apply the full patch series onto the pruned Chromium 149 tree, in order:
#        (1) ungoogled-chromium base       — de-Google / telemetry removal
#        (2) ungoogled-chromium-windows     — Windows-specific build fixes (overlay)
#        (3) Clearcote patches (patches/)   — engine-level fingerprint controls + cross-build source fixes
# Every patch is a plain unified diff against the pinned revision. Layer (3) is self-validated
# to apply with zero rejects on top of (1)+(2).
#
#   WORK   working dir (default: ~/clearcote-build)  — must match scripts/00-fetch-source.sh
#   REPO   path to this repository (default: parent of this script)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC="$WORK/build/src"
UG="$WORK/ungoogled-chromium"
UGW="$WORK/ungoogled-chromium-windows"

[ -d "$SRC" ]  || { echo "no source tree at $SRC — run scripts/00-fetch-source.sh first" >&2; exit 1; }
[ -d "$UG" ]   || { echo "no ungoogled-chromium at $UG — run scripts/00-fetch-source.sh first" >&2; exit 1; }

apply() { echo "  -> $2"; python3 "$UG/utils/patches.py" apply "$SRC" "$1"; }

echo "== 1/3  ungoogled-chromium base (de-Google) =="
apply "$UG/patches" "ungoogled base"

echo "== 2/3  ungoogled-chromium-windows overlay =="
# Windows-specific fixes. NOTE for cross-builds: this overlay's windows-disable-rcpy.patch is
# aimed at native Windows; the Linux cross-build relies on the prebuilt cross rc.py instead. If
# the link later complains about rc, see docs/BUILDING.md (the resource-compiler note).
apply "$UGW/patches" "windows overlay"

echo "== 3/3  Clearcote patches (engine fingerprint controls + cross-build source fixes) =="
apply "$REPO/patches" "clearcote ($(grep -cve '^[[:space:]]*$' "$REPO/patches/series") patches)"

echo "OK: applied ungoogled base + windows overlay + Clearcote series onto $SRC"
echo "next -> scripts/02-host-toolchain.sh"
