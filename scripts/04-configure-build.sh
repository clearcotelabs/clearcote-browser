#!/usr/bin/env bash
# 04 — configure (args.gn) and cross-compile chrome.exe for Windows x64 on Linux.
#
#   WORK   working dir (default: ~/clearcote-build)
#   REPO   path to this repository (default: parent of this script)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
SRC="$WORK/build/src"
GN="$WORK/bin/gn"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$SRC"

# Chromium's link step opens a huge number of files
ulimit -n 1048576 2>/dev/null || ulimit -n "$(ulimit -Hn)"

# toolchain clang/lld + the synthetic Windows SDK toolchain from step 03.
# KEY TRICK: DEPOT_TOOLS_WIN_TOOLCHAIN=1 (not 0) + a package_from_installed-style dir.
export PATH="$SRC/third_party/llvm-build/Release+Asserts/bin:$PATH"
export DEPOT_TOOLS_WIN_TOOLCHAIN=1 \
       GYP_MSVS_OVERRIDE_PATH="$WORK/wintoolchain" \
       WINDOWSSDKDIR="$WORK/wintoolchain/win_sdk" \
       GYP_MSVS_VERSION=2026 \
       WDK_DIR=

# exact build configuration (Windows x64, de-Googled, codecs on)
mkdir -p out/Default
cp "$REPO/config/args.gn" out/Default/args.gn

"$GN" gen out/Default
exec ninja -C out/Default chrome
