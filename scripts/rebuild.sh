#!/usr/bin/env bash
# rebuild — fast incremental rebuild after editing patches/.
#
# A clean build (build.sh) takes hours; a patch tweak should take MINUTES. This re-applies
# ONLY the Clearcote series (the ungoogled base + windows overlay stay applied) and runs an
# incremental ninja, which recompiles just the translation units whose source changed and
# relinks chrome.dll. It deliberately NEVER wipes out/Default, NEVER re-runs `gn gen`, and
# NEVER re-fetches the source — so ninja's dependency graph does the minimum work.
#
# Note: editing a widely-included header (a Blink .h) fans out to many TUs; editing a .cc is
# narrow. That fan-out is inherent to C++, not to this script.
#
#   WORK   working dir (default: ~/clearcote-build)  — must match scripts/00-fetch-source.sh
#   REPO   path to this repository (default: parent of this script)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
REPO="${REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
SRC="$WORK/build/src"
UG="$WORK/ungoogled-chromium"
[ -d "$SRC/out/Default" ] || { echo "no out/Default — run a full build first (build.sh); don't start clean" >&2; exit 1; }

# 1) re-apply the Clearcote series so edits land in the tree (only changed files are rewritten).
echo "re-applying Clearcote patch series ($REPO/patches)"
python3 "$UG/utils/patches.py" unapply "$SRC" "$REPO/patches" 2>/dev/null || \
  tac "$REPO/patches/series" | while IFS= read -r p; do
    p="${p%%#*}"; [ -z "${p// }" ] && continue
    patch -R -p1 -s -d "$SRC" -i "$REPO/patches/$p" 2>/dev/null || true
  done
python3 "$UG/utils/patches.py" apply "$SRC" "$REPO/patches"

# 2) incremental compile/link — out/Default is preserved, so ninja does the minimum.
cd "$SRC"
ulimit -n 1048576 2>/dev/null || ulimit -n "$(ulimit -Hn)"
export PATH="$SRC/third_party/llvm-build/Release+Asserts/bin:$PATH"
export DEPOT_TOOLS_WIN_TOOLCHAIN=1 \
       GYP_MSVS_OVERRIDE_PATH="$WORK/wintoolchain" \
       WINDOWSSDKDIR="$WORK/wintoolchain/win_sdk" \
       GYP_MSVS_VERSION=2026 WDK_DIR=
echo "incremental ninja (recompiles only changed TUs + relinks)"
time ninja -C out/Default chrome
echo "done: $SRC/out/Default/chrome.exe"
