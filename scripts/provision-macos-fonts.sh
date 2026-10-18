#!/usr/bin/env bash
# clearcote-956: put the metric-compatible font clones where chrome/BUILD.gn's
# bundle_data("clearcote_persona_fonts") expects them, for a macOS build.
#
# These are the same files the Linux release bundles (see assets/fonts/ATTRIBUTION.md).
# On Linux the SDK points FONTCONFIG_FILE at fonts.conf.template; macOS has no fontconfig,
# so the fonts travel inside the app bundle and are registered with CoreText at
# kCTFontManagerScopeProcess by blink::EnsurePersonaSubstituteFontsRegistered().
#
# Run this BEFORE `gn gen`: gn resolves bundle_data sources at generation time, so a
# missing file is a gn error, not a build error. macOS only — on a Windows or Linux
# target the bundle_data target is inside chrome/BUILD.gn's `is_mac` branch and is never
# declared, so nothing needs provisioning.
#
#   scripts/provision-macos-fonts.sh /path/to/chromium/src
set -euo pipefail
SRC="${1:-}"
[ -n "$SRC" ] || { echo "usage: $0 <chromium-src-dir>" >&2; exit 2; }
[ -d "$SRC/third_party" ] || { echo "not a chromium src dir: $SRC" >&2; exit 2; }

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ASSETS="$REPO/assets/fonts"
DEST="$SRC/third_party/clearcote_fonts"

mkdir -p "$DEST"
cp -f "$ASSETS"/*.ttf "$DEST/"
cp -f "$ASSETS"/*.otf "$DEST/"
cp -f "$ASSETS/ATTRIBUTION.md" "$DEST/"

# Verify against the pinned digests. A silently truncated or substituted font file would
# otherwise change rendering without changing the build, which is exactly the class of
# difference this project exists to avoid; it also keeps the macOS artifact reproducible
# from the public repo.
( cd "$ASSETS" && shasum -a 256 -c SHA256SUMS >/dev/null ) \
  || { echo "FATAL: assets/fonts does not match assets/fonts/SHA256SUMS" >&2; exit 1; }

n=$(find "$DEST" -type f \( -name '*.ttf' -o -name '*.otf' \) | wc -l | tr -d ' ')
echo "clearcote: provisioned $n font clones into $DEST"
