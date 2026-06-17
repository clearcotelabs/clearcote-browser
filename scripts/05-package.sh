#!/usr/bin/env bash
# 05 — package the built browser into a distributable zip and bundle the VC++ runtime.
# The signing + checksum + GitHub-release steps live in docs/RELEASING.md.
#
#   WORK   working dir (default: ~/clearcote-build)
#   V      Chromium version for the asset name (default: 149.0.7827.114)
set -euo pipefail
WORK="${WORK:-$HOME/clearcote-build}"
SRC="$WORK/build/src"; OUT="$SRC/out/Default"
DIST="$WORK/dist"; mkdir -p "$DIST"
V="${V:-149.0.7827.114}"
ASSET="clearcote-$V-windows-x64.zip"

cd "$OUT"
rm -f "$DIST/$ASSET"
zip -r "$DIST/$ASSET" \
  chrome.exe chrome.dll chrome_elf.dll chrome_wer.dll \
  *.pak *.bin *.dat *.json locales \
  libEGL.dll libGLESv2.dll d3dcompiler_47.dll \
  vk_swiftshader.dll vulkan-1.dll VkICD_mock_icd.dll VkLayer_khronos_validation.dll

# Bundle the VC++ 2015-2022 runtime so chrome.exe launches on a clean Windows 10/11 box.
# Place these 5 DLLs (from the Microsoft VC++ redistributable) in $WORK/vcredist/ first.
if [ -d "$WORK/vcredist" ]; then
  ( cd "$WORK/vcredist" && zip -j "$DIST/$ASSET" \
      concrt140.dll msvcp140.dll ucrtbase.dll vcruntime140.dll vcruntime140_1.dll )
else
  echo "WARN: $WORK/vcredist not found — VC++ runtime NOT bundled; chrome.exe may not launch on a clean box."
fi

( cd "$DIST" && sha256sum "$ASSET" | tee "$ASSET.sha256" )
echo "OK: $DIST/$ASSET — now verify + GPG-sign + publish per docs/RELEASING.md"
