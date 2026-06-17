#!/usr/bin/env bash
# Clearcote — one-shot Linux cross-build of the Windows x64 browser.
#
# Prerequisites (a capable Linux host):
#   - 64-bit Linux, ~16 GB+ RAM (32 GB recommended; the link step is RAM-hungry), a big swap helps
#   - ~120 GB free disk, a many-core CPU, and several hours
#   - git, python3, curl, ninja, zip, and (for case-folded SDK headers) ciopfs
#
# Usage:
#   WORK=~/clearcote-build ./build.sh        # runs every stage in order
# or run the numbered scripts in scripts/ individually.
set -euo pipefail
export WORK="${WORK:-$HOME/clearcote-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export REPO="$HERE"

run() { echo; echo "==== $1 ===="; bash "$HERE/scripts/$2"; }

run "00  fetch + prune Chromium 149 source"        00-fetch-source.sh
run "01  apply patch series"                       01-apply-patches.sh    # ungoogled base + windows overlay + clearcote fingerprint (see patches/)
run "02  host toolchain (clang/rust/gn)"           02-host-toolchain.sh
run "03  windows SDK toolchain (xwin)"             03-windows-sdk.sh
run "04  configure + cross-compile chrome.exe"     04-configure-build.sh
run "05  package into a distributable zip"         05-package.sh

echo
echo "DONE — artifact in $WORK/dist."
echo "Verify, GPG-sign, and publish it per docs/RELEASING.md; verify a download per docs/VERIFY.md."
