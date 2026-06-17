#!/usr/bin/env bash
# 02 — install host build deps, fetch Chromium's toolchain (clang/lld, rust, sysroot, node)
# and build `gn`. The same clang/lld is also the cross-compiler for the Windows target.
# Run AFTER patches are applied. Idempotent.
#
#   WORK   working dir (default: ~/clearcote-build)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
SRC="$WORK/build/src"
UG="$WORK/ungoogled-chromium"
cd "$SRC"

# System packages Chromium needs (Debian/Ubuntu). On other distros install the equivalents.
# Run with sudo if you are not root.
python3 build/install-build-deps.py --no-prompt \
  || sudo python3 build/install-build-deps.py --no-prompt \
  || echo "warn: install-build-deps did not complete — ensure host build packages are present"

# If the patch stage applied ungoogled "domain substitution", REVERT it before any network
# fetch: it rewrites the toolchain download URLs (e.g. commondatastorage.googleapis.com) and
# breaks clang/rust fetching. Safe to run even if it was never applied.
CACHE="$WORK/build/domsubcache.tar.gz"
if [ -f "$CACHE" ]; then
  echo "reverting domain substitution before toolchain fetch"
  python3 "$UG/utils/domain_substitution.py" revert -c "$CACHE" "$SRC" || echo "warn: domsub revert"
fi

# Chromium clang/lld (host + Windows cross)
python3 tools/clang/scripts/update.py

# Rust — args.gn sets rust_sysroot_absolute="" so std is built from source; prebuilt rustc/std
# are still fetched here.
python3 tools/rust/update_rust.py || echo "warn: rust fetch (continuing)"

# amd64 sysroot for host-side tooling
python3 build/linux/sysroot_scripts/install-sysroot.py --arch=amd64 || echo "warn: sysroot (continuing)"

# Host node (normally pulled by gclient runhooks; we don't run gclient, so fetch it directly).
# NB: update_node_binaries is a SHELL script, not python — invoke it directly.
chmod +x third_party/node/update_node_binaries 2>/dev/null || true
third_party/node/update_node_binaries

# depot_tools' prebuilt `gn` refuses to bootstrap its Python as root, so build gn from the
# in-tree source using the toolchain clang we just fetched.
TC="$SRC/third_party/llvm-build/Release+Asserts/bin"
export PATH="$TC:$PATH" CC="$TC/clang" CXX="$TC/clang++" AR="$TC/llvm-ar"
python3 tools/gn/bootstrap/bootstrap.py --skip-generate-buildfiles
# bootstrap writes the gn binary under out/<dir>/gn_build/gn — locate it rather than assume.
GN_BIN="$(find out -type f -name gn -perm -u+x 2>/dev/null | head -1)"
[ -n "$GN_BIN" ] || { echo "FATAL: gn bootstrap produced no binary" >&2; exit 1; }
install -D "$GN_BIN" "$WORK/bin/gn"

echo "OK: host toolchain ready; gn -> $WORK/bin/gn ($("$WORK/bin/gn" --version))"
