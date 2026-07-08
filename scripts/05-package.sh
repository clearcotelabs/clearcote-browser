#!/usr/bin/env bash
# 05 — package the built browser into a distributable archive:
#       windows -> clearcote-<V>-windows-x64.zip  (chrome.exe + DLLs + VC++ runtime)
#       linux   -> clearcote-<V>-linux-x64.tar.xz (chrome + chrome-sandbox + .so/.pak/…)
# The signing + checksum + GitHub-release steps live in docs/RELEASING.md.
#
#   TARGET  windows | linux   (default: windows)
#   WORK    working dir (default: ~/clearcote-build)
#   V       Chromium version for the asset name (default: 149.0.7827.114)
set -euo pipefail
TARGET="${TARGET:-windows}"
WORK="${WORK:-$HOME/clearcote-build}"
SRC="$WORK/build/src"; OUT="$SRC/out/Default"
DIST="$WORK/dist"; mkdir -p "$DIST"
V="${V:-149.0.7827.114}"

if [ "$TARGET" = "windows" ]; then
  ASSET="clearcote-$V-windows-x64.zip"
  cd "$OUT"; rm -f "$DIST/$ASSET"
  # NOTE: *.manifest (the SxS version-assembly manifest, e.g. 149.0.7827.114.manifest) is
  # REQUIRED at the archive root — without it chrome.exe fails to start with "the side-by-side
  # configuration is incorrect" (surfaced as `spawn UNKNOWN` via Playwright). Do not drop it.
  zip -r "$DIST/$ASSET" \
    chrome.exe chrome.dll chrome_elf.dll chrome_wer.dll \
    *.manifest \
    *.pak *.bin *.dat *.json locales \
    libEGL.dll libGLESv2.dll d3dcompiler_47.dll \
    vk_swiftshader.dll vulkan-1.dll VkICD_mock_icd.dll VkLayer_khronos_validation.dll
  # Bundle the VC++ 2015-2022 runtime so chrome.exe launches on a clean Windows 10/11 box.
  if [ -d "$WORK/vcredist" ]; then
    ( cd "$WORK/vcredist" && zip -j "$DIST/$ASSET" \
        concrt140.dll msvcp140.dll ucrtbase.dll vcruntime140.dll vcruntime140_1.dll )
  else
    echo "WARN: $WORK/vcredist not found — VC++ runtime NOT bundled; chrome.exe may not launch on a clean box."
  fi
  INNER="chrome.exe"
else
  # --- Linux: deterministic tar.xz of the deployed-file set (from chrome/installer/linux) ---
  ASSET="clearcote-$V-linux-x64.tar.xz"
  STAGE="$DIST/stage-linux-x64"; rm -rf "$STAGE"; mkdir -p "$STAGE/locales" "$STAGE/lib"
  copy_in() { [ -e "$OUT/$1" ] && { mkdir -p "$STAGE/$(dirname "$1")"; cp -a "$OUT/$1" "$STAGE/$1"; } || \
              { [ "${2:-}" = req ] && { echo "FATAL: missing $1"; exit 1; } || echo "  (skip: $1)"; }; }
  for f in chrome chrome_crashpad_handler icudtl.dat resources.pak; do copy_in "$f" req; done
  cp -a "$OUT/chrome_sandbox" "$STAGE/chrome-sandbox"    # installer renames + setuid-4755 at install
  for f in libEGL.so libGLESv2.so libvulkan.so.1 libvk_swiftshader.so vk_swiftshader_icd.json \
           chrome_100_percent.pak chrome_200_percent.pak v8_context_snapshot.bin snapshot_blob.bin; do
    copy_in "$f"; done
  copy_in "lib/libc++.so"
  [ -d "$OUT/locales" ] && find "$OUT/locales" -maxdepth 1 -name '*.pak' -exec cp -a {} "$STAGE/locales/" \;
  [ -e "$STAGE/v8_context_snapshot.bin" ] || [ -e "$STAGE/snapshot_blob.bin" ] || { echo "FATAL: no snapshot blob"; exit 1; }
  # strip to release size + set perms (chrome-sandbox ships 0755; setuid can't survive a user-extracted tar)
  strip "$STAGE/chrome" "$STAGE/chrome_crashpad_handler" 2>/dev/null || true
  find "$STAGE" -type f \( -name '*.so' -o -name '*.so.*' \) -exec strip {} \; 2>/dev/null || true
  chmod 0755 "$STAGE/chrome" "$STAGE/chrome-sandbox" "$STAGE/chrome_crashpad_handler"
  find "$STAGE" -type f \( -name '*.pak' -o -name '*.dat' -o -name '*.bin' -o -name '*.json' \) -exec chmod 0644 {} +
  # Bundle the metric-compatible font clones + the self-contained fontconfig template. The SDK
  # points FONTCONFIG_FILE at it on Linux launch so Segoe UI/Arial/Times/... resolve to their
  # clones on bare servers/containers (no host fonts). See assets/fonts/ATTRIBUTION.md.
  FONTS_SRC="${FONTS_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/assets/fonts}"
  if [ -d "$FONTS_SRC" ] && ls "$FONTS_SRC"/*.ttf >/dev/null 2>&1; then
    mkdir -p "$STAGE/fonts"
    # .ttf + .otf (Comic Neue / Inconsolata ship as OpenType/CFF; fontconfig reads both)
    cp -a "$FONTS_SRC"/*.ttf "$FONTS_SRC/fonts.conf.template" "$STAGE/fonts/"
    ls "$FONTS_SRC"/*.otf >/dev/null 2>&1 && cp -a "$FONTS_SRC"/*.otf "$STAGE/fonts/"
    [ -e "$FONTS_SRC/ATTRIBUTION.md" ] && cp -a "$FONTS_SRC/ATTRIBUTION.md" "$STAGE/fonts/"
    find "$STAGE/fonts" -type f -exec chmod 0644 {} +
    echo "  bundled $(ls "$STAGE/fonts"/*.ttf "$STAGE/fonts"/*.otf 2>/dev/null | wc -l) font clones + fonts.conf.template"
  else
    echo "WARN: $FONTS_SRC has no fonts — NOT bundled; Segoe UI/Arial may be absent on bare Linux."
  fi
  rm -f "$DIST/$ASSET"
  ( cd "$STAGE" && tar --sort=name --owner=root:0 --group=root:0 --mtime=@0 -cf - . ) | xz -9 -T0 > "$DIST/$ASSET"
  INNER="chrome"
  CHROME_HASH="$(sha256sum "$STAGE/chrome" | awk '{print $1}')"
  rm -rf "$STAGE"
fi

cd "$DIST"
ARCH_HASH="$(sha256sum "$ASSET" | awk '{print $1}')"
printf '%s  %s\n' "$ARCH_HASH" "$ASSET" | tee "$ASSET.sha256"
# aggregate SHA256SUMS.txt: the archive line + the inner-binary line (chrome.exe / chrome)
if [ "$TARGET" = "linux" ]; then
  printf '%s  %s\n%s  %s\n' "$ARCH_HASH" "$ASSET" "$CHROME_HASH" "$INNER" > SHA256SUMS.txt
else
  printf '%s  %s\n%s  %s\n' "$ARCH_HASH" "$ASSET" "$(sha256sum "$OUT/$INNER" | awk '{print $1}')" "$INNER" > SHA256SUMS.txt
fi
echo "OK: $DIST/$ASSET — now verify + GPG-sign SHA256SUMS.txt + publish per docs/RELEASING.md"
