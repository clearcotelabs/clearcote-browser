#!/usr/bin/env bash
# Clearcote — one-shot reproducible build of the browser, on a Linux host, for either:
#   - Windows x64  (cross-compiled with clang + a Windows SDK sysroot fetched by xwin)  [default]
#   - Linux x64    (native)
#
# Prerequisites (a capable Linux host):
#   - 64-bit Linux, ~16 GB+ RAM (32 GB recommended; the link step is RAM-hungry), a big swap helps
#   - ~120 GB free disk, a many-core CPU, and several hours
#   - git, python3, curl, ninja, zip, xz; and for the Windows target, ciopfs (case-folded SDK headers)
#
# Usage:
#   WORK=~/clearcote-build ./build.sh windows     # -> chrome.exe zip  (default if no arg)
#   WORK=~/clearcote-build ./build.sh linux       # -> chrome tar.xz
# or run the numbered scripts in scripts/ individually (they read $TARGET).
#
# For a fully-pinned, reproducible environment, build inside the container instead — see the
# Dockerfile at the repo root and docs/BUILDING.md.
set -euo pipefail
export TARGET="${1:-${TARGET:-windows}}"
case "$TARGET" in windows|linux) ;; *) echo "usage: $0 [windows|linux]"; exit 2 ;; esac
export WORK="${WORK:-$HOME/clearcote-build}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export REPO="$HERE"

run() { echo; echo "==== $1 ===="; bash "$HERE/scripts/$2"; }

echo "### Clearcote build — TARGET=$TARGET  WORK=$WORK  (Chromium $(cat "$HERE/UPSTREAM_REVISION"))"
run "00  fetch + prune Chromium 149 source"        00-fetch-source.sh
run "01  apply patch series ($TARGET)"             01-apply-patches.sh
run "02  host toolchain (clang/rust/gn/sysroot)"   02-host-toolchain.sh
if [ "$TARGET" = "windows" ]; then
  run "03  windows SDK toolchain (xwin)"           03-windows-sdk.sh
fi
run "04  configure + compile ($TARGET)"            04-configure-build.sh
run "05  package the distributable"                05-package.sh

echo
echo "DONE — artifact in $WORK/dist."
echo "Verify, GPG-sign, and publish it per docs/RELEASING.md; verify a download per docs/VERIFY.md."
