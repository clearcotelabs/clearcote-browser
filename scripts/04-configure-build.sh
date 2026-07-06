#!/usr/bin/env bash
# 04 — configure (args.gn) and compile the browser:
#       windows -> cross-compile chrome.exe for Windows x64 (clang + the xwin SDK from step 03)
#       linux   -> natively compile chrome + chrome_sandbox + chrome_crashpad_handler for Linux x64
#
#   TARGET  windows | linux   (default: windows)
#   WORK    working dir (default: ~/clearcote-build)
#   REPO    path to this repository (default: parent of this script)
set -euo pipefail
TARGET="${TARGET:-windows}"
WORK="${WORK:-$HOME/clearcote-build}"
SRC="$WORK/build/src"
GN="$WORK/bin/gn"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$SRC"

# Chromium's link step opens a huge number of files
ulimit -n 1048576 2>/dev/null || ulimit -n "$(ulimit -Hn)"

# the in-tree clang/lld is the (cross-)compiler for both targets
export PATH="$SRC/third_party/llvm-build/Release+Asserts/bin:$PATH"

mkdir -p out/Default

if [ "$TARGET" = "windows" ]; then
  # the synthetic Windows SDK toolchain from step 03.
  # KEY TRICK: DEPOT_TOOLS_WIN_TOOLCHAIN=1 (not 0) + a package_from_installed-style dir.
  export DEPOT_TOOLS_WIN_TOOLCHAIN=1 \
         GYP_MSVS_OVERRIDE_PATH="$WORK/wintoolchain" \
         WINDOWSSDKDIR="$WORK/wintoolchain/win_sdk" \
         GYP_MSVS_VERSION=2026 \
         WDK_DIR=
  cp "$REPO/config/args.gn" out/Default/args.gn         # Windows x64, de-Googled, codecs on
  "$GN" gen out/Default
  exec ninja -C out/Default chrome
else
  # native Linux: no Windows toolchain env; build against the bundled sysroot (use_sysroot=true)
  cp "$REPO/config/args.linux.gn" out/Default/args.gn   # Linux x64, de-Googled, codecs on
  "$GN" gen out/Default
  # chrome (the browser) + chrome_sandbox (the setuid helper) + the crash handler
  exec ninja -C out/Default chrome chrome_sandbox chrome_crashpad_handler
fi
