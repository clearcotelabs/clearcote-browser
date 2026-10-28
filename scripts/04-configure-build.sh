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

# ungoogled domain substitution. The released 151 binaries are built from a tree WITH it (it rewrites
# Google hostnames baked into the source, so it changes the binary). It must come after step 02 —
# it also rewrites the toolchain download URLs — and 02 reverts it from this cache if re-run. Applying
# it after the Clearcote series gives the same tree as applying it before, so it sits here.
UG="$WORK/ungoogled-chromium"
DOMSUB_CACHE="$WORK/build/domsubcache.tar.gz"
if [ ! -f "$DOMSUB_CACHE" ]; then
  echo "  applying: ungoogled domain substitution"
  python3 "$UG/utils/domain_substitution.py" apply -r "$UG/domain_regex.list" \
    -f "$UG/domain_substitution.list" -c "$DOMSUB_CACHE" "$SRC"
fi

# Official builds (since Chromium 151) use PGO: the profile named by pgo_data_path in the args file
# must exist before gn gen. The Linux profile ships in the source tarball; the Windows one does not,
# so fetch the file chrome/build/win64.pgo.txt names (tools/update_pgo_profiles.py would do the same
# but needs vpython3). Then check the args file points at that exact profile.
ensure_pgo_profile() { # args-file  pgo.txt
  local args="$1" txt="chrome/build/$2" name want
  name="$(tr -d '[:space:]' < "$txt")"
  if [ ! -f "chrome/build/pgo_profiles/$name" ]; then
    echo "  fetching PGO profile $name"
    mkdir -p chrome/build/pgo_profiles
    curl -fL -o "chrome/build/pgo_profiles/$name" \
      "https://storage.googleapis.com/chromium-optimization-profiles/pgo_profiles/$name"
  fi
  want="$(sed -n 's|^pgo_data_path *= *"//chrome/build/pgo_profiles/\([^"]*\)".*|\1|p' "$args")"
  if [ -n "$want" ] && [ "$want" != "$name" ]; then
    echo "FATAL: $args has pgo_data_path=$want but $txt names $name"; exit 1
  fi
}

if [ "$TARGET" = "windows" ]; then
  # the synthetic Windows SDK toolchain from step 03.
  # KEY TRICK: DEPOT_TOOLS_WIN_TOOLCHAIN=1 (not 0) + a package_from_installed-style dir.
  export DEPOT_TOOLS_WIN_TOOLCHAIN=1 \
         GYP_MSVS_OVERRIDE_PATH="$WORK/wintoolchain" \
         WINDOWSSDKDIR="$WORK/wintoolchain/win_sdk" \
         GYP_MSVS_VERSION=2026 \
         WDK_DIR=
  ensure_pgo_profile "$REPO/config/args.gn" win64.pgo.txt
  cp "$REPO/config/args.gn" out/Default/args.gn         # Windows x64, de-Googled, codecs on
  "$GN" gen out/Default
  exec ninja -C out/Default chrome
else
  # native Linux: no Windows toolchain env; build against the bundled sysroot (use_sysroot=true)
  ensure_pgo_profile "$REPO/config/args.linux.gn" linux.pgo.txt   # ships in the tarball; checked only
  cp "$REPO/config/args.linux.gn" out/Default/args.gn   # Linux x64, de-Googled, codecs on
  "$GN" gen out/Default
  # chrome (the browser) + chrome_sandbox (the setuid helper) + the crash handler
  exec ninja -C out/Default chrome chrome_sandbox chrome_crashpad_handler
fi
