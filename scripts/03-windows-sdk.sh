#!/usr/bin/env bash
# 03 — assemble a Windows SDK + CRT sysroot on Linux (no Windows machine) with xwin,
# laid out the way Chromium's `package_from_installed` toolchain expects.
#
# The MSVC/SDK headers #include each other with inconsistent casing, so on a
# case-sensitive Linux filesystem the SDK *Include* tree must be served through a
# case-insensitive overlay (ciopfs). The *Lib* tree does not need this — xwin already
# emits per-case symlinks for libraries.
#
#   WORK   working dir (default: ~/clearcote-build)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
XWIN_VER="0.9.0"
SDK_VERSION="10.0.26100"            # pin the MS SDK payload xwin fetches
WINSDK="$WORK/winsdk"
TC="$WORK/wintoolchain"
# Synthetic version labels — just directory names in the layout below (symlinks/ciopfs make
# the xwin payload appear under them); they need only be internally consistent + match SetEnv.
VCVER="14.44.35207"
SDKVER="10.0.26100.0"

command -v ciopfs >/dev/null 2>&1 || { echo "FATAL: 'ciopfs' is required (apt-get install ciopfs)"; exit 1; }

# 1. fetch xwin + splat Microsoft's Windows SDK + CRT (license auto-accepted)
if [ ! -x "$WORK/bin/xwin" ] && ! command -v xwin >/dev/null 2>&1; then
  curl -L "https://github.com/Jake-Shadle/xwin/releases/download/${XWIN_VER}/xwin-${XWIN_VER}-x86_64-unknown-linux-musl.tar.gz" \
    | tar -xz -C "$WORK"
  install -D "$WORK/xwin-${XWIN_VER}-x86_64-unknown-linux-musl/xwin" "$WORK/bin/xwin"
fi
XWIN="$WORK/bin/xwin"; command -v xwin >/dev/null 2>&1 && XWIN="$(command -v xwin)"
[ -d "$WINSDK/crt" ] || "$XWIN" --accept-license --sdk-version "$SDK_VERSION" splat --output "$WINSDK"

CRT="$WINSDK/crt"; SDK="$WINSDK/sdk"
for d in "$CRT/include" "$CRT/lib/x86_64" "$SDK/include/um" "$SDK/include/shared" \
         "$SDK/include/ucrt" "$SDK/include/winrt" "$SDK/include/cppwinrt" \
         "$SDK/lib/um/x86_64" "$SDK/lib/ucrt/x86_64"; do
  [ -d "$d" ] || { echo "FATAL: xwin output missing $d" >&2; exit 1; }
done

# 2. (re)build the package_from_installed-style tree
VC_DIR="$TC/VC/Tools/MSVC/$VCVER"; WK="$TC/Windows Kits/10"
INC="$WK/Include/$SDKVER"
fusermount -u "$INC" 2>/dev/null || true        # release any prior ciopfs mount before rebuild
rm -rf "$TC"
mkdir -p "$VC_DIR/bin/HostX64/x64" "$VC_DIR/lib" "$INC" \
         "$WK/Lib/$SDKVER/um" "$WK/Lib/$SDKVER/ucrt" "$WK/bin/$SDKVER/x64" "$WK/bin" "$WK/UnionMetadata/$SDKVER"

# VC CRT headers + libs: plain symlinks are fine (CRT headers are already consistent-case)
ln -sfn "$CRT/include"    "$VC_DIR/include"
ln -sfn "$CRT/lib/x86_64" "$VC_DIR/lib/x64"
# SDK libs: xwin emits per-case lib symlinks, so plain symlinks resolve
ln -sfn "$SDK/lib/um/x86_64"   "$WK/Lib/$SDKVER/um/x64"
ln -sfn "$SDK/lib/ucrt/x86_64" "$WK/Lib/$SDKVER/ucrt/x64"
ln -sfn "Windows Kits/10"      "$TC/win_sdk"

# SDK Include tree: case-insensitive via ciopfs. Mount over the real Include/<ver> dir, then
# copy the SDK headers through the mount so they're stored case-folded + read case-insensitively.
BACK="$WORK/ci/inc_backing"
rm -rf "$BACK"; mkdir -p "$BACK"
ciopfs "$BACK" "$INC"
cp -a "$SDK/include/." "$INC/"
for sub in um shared winrt cppwinrt ucrt; do
  [ -d "$INC/$sub" ] || { echo "FATAL: ciopfs include tree missing $sub" >&2; exit 1; }
done

# Chromium probes for cl.exe; a stub satisfies the check (we actually build with clang-cl).
printf '#!/bin/sh\nexit 0\n' > "$VC_DIR/bin/HostX64/x64/cl.exe"; chmod +x "$VC_DIR/bin/HostX64/x64/cl.exe"
printf '2026\n' > "$TC/VS_VERSION"

# 3. synthesize SetEnv.x64.json (INCLUDE/LIB/PATH the cross-build reads)
python3 - "$TC" "$VCVER" "$SDKVER" <<'PY'
import json, collections, os, sys
tc, vc, sk = sys.argv[1], sys.argv[2], sys.argv[3]
e = collections.OrderedDict()
e["VSINSTALLDIR"]=[[".\\"]]; e["VCINSTALLDIR"]=[["VC\\"]]
e["INCLUDE"]=[["Windows Kits","10","Include",sk,"um"],["Windows Kits","10","Include",sk,"shared"],
             ["Windows Kits","10","Include",sk,"winrt"],["Windows Kits","10","Include",sk,"cppwinrt"],
             ["Windows Kits","10","Include",sk,"ucrt"],["VC","Tools","MSVC",vc,"include"]]
e["LIBPATH"]=[["VC","Tools","MSVC",vc,"lib","x64"],["Windows Kits","10","UnionMetadata",sk]]
e["VCToolsInstallDir"]=[["VC","Tools","MSVC",vc+"\\"]]
e["PATH"]=[["VC","Tools","MSVC",vc,"bin","HostX64","x64"],["Windows Kits","10","bin",sk,"x64"]]
e["LIB"]=[["VC","Tools","MSVC",vc,"lib","x64"],["Windows Kits","10","Lib",sk,"um","x64"],
          ["Windows Kits","10","Lib",sk,"ucrt","x64"]]
p=os.path.join(tc,"Windows Kits","10","bin","SetEnv.x64.json")
json.dump({"env":e}, open(p,"w")); print("wrote", p)
PY

echo "OK: Windows toolchain at $TC (SDK Include via ciopfs); GYP_MSVS_OVERRIDE_PATH=$TC"
echo "    (to release the mount later: fusermount -u \"$INC\")"
