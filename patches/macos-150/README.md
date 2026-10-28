# macOS patches for the open Chromium 150 build

`956`–`958` apply to the **open Chromium 150 macOS build only** (`150.0.7871.114`, applied after the
top-level 150 series of `v0.1.0-pre.23`). They are not part of the Chromium 151 open build and are
not listed in [`../series`](../series): there is no macOS build of open 151, and they have not been
rebased to the 151 tree. `scripts/01-apply-patches.sh` and `scripts/verify_patches.py` read only the
top-level `patches/*.patch` files named in `series`, so nothing in this folder is applied or checked
by the Windows/Linux build. What they do and how the macOS build uses them is in
[`../../docs/MACOS-FONTS.md`](../../docs/MACOS-FONTS.md).
