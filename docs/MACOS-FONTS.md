# macOS: persona fonts

How a cross-OS persona gets its fonts on a macOS build, what is verified, and what is not.
Patches `956`–`958`. If you are building for Windows or Linux, none of this applies — those
targets never declare the bundle and never compile the registration.

> **Scope: the open Chromium 150 macOS build only.** `956`–`958` live in
> [`patches/macos-150/`](../patches/macos-150/) and are written against the 150 tree. They are not in
> the Chromium 151 `patches/series`, and there is no macOS build of open 151.

## The problem

A Windows persona has to be served Windows fonts, or the font list says one thing and every
advance width says another. On Linux the release bundles metric-compatible open clones in
`assets/fonts/` and the SDK points `FONTCONFIG_FILE` at `assets/fonts/fonts.conf.template`,
so `Segoe UI` resolves to Selawik, `Calibri` to Carlito and so on.

macOS has no fontconfig, and a stock macOS 27.0 machine has exactly one file in
`/Library/Fonts` — none of Arimo, Carlito, Caladea, Tinos, Cousine, Selawik or Gelasio. So on
macOS the same persona got no substitutions at all.

Measured on the native 150.0.7871.114 arm64 build, `platform="windows"`, before `956`–`958`:

| probe | before | after |
|---|---|---|
| Segoe UI, Calibri, Consolas, Cambria, Candara, Corbel | absent | **present** |
| Helvetica Neue | present | **absent** |
| Lucida Grande, Menlo, Geneva, Optima, Apple Chancery, Hiragino Sans | absent | absent |
| `16px system-ui` | 56.84 (same as the macos persona) | **57.96** |
| `32px` serif / monospace / cursive | 111.98 / 111.98 / 111.98 | **111.98 / 96.00 / 106.82** |

`width(monospace) == width(serif)` is impossible on real Windows and takes no reference data
to spot, so the generics mattered as much as the named families.

## Building

```bash
scripts/provision-macos-fonts.sh /path/to/chromium/src   # BEFORE gn gen
```

It copies `assets/fonts/` into `third_party/clearcote_fonts/` and checks it against
`assets/fonts/SHA256SUMS`. It has to run before `gn gen`, because gn resolves `bundle_data`
sources at generation time — a missing file is a gn error, not a build error.

No new fonts and no new licences: these are the same files the Linux release already bundles.
See `assets/fonts/ATTRIBUTION.md`.

At runtime the fonts live at
`Chromium.app/Contents/Frameworks/Chromium Framework.framework/Resources/clearcote_fonts/` and
are registered with CoreText at `kCTFontManagerScopeProcess` the first time a cross-OS persona
needs one — visible to that process only, never written to the user's font database, gone when
the process exits. A launch with no persona, and a same-OS persona, register nothing. The
renderer sandbox already allows reading the app bundle, so no sandbox change was needed.

## Verifying

```bash
python3 scripts/verify_macos_persona.py          # CC_APP=... to point elsewhere
```

Two things about measuring this, both learned the hard way:

* **Pass a seed.** `ungoogled::PersonaActive()` is true only with `--fingerprint` or
  `--fingerprint-profile`, and the whole font block in `FontCache::GetFontPlatformData` is
  gated on it. A `platform=`-only launch spoofs the strings and does no font work at all, so
  it measures nothing.
* **Width alone is not presence.** Compare the whole `TextMetrics` vector, against more than
  one generic fallback. macOS's monospace default is Menlo, so a monospace-metric family
  checked against a monospace baseline looks absent; and Tinos, the Times New Roman clone, is
  metrically identical to macOS's Times, which is what the serif fallback resolves to under a
  Windows persona. `document.fonts.check()` returns true for almost any family name and is
  useless here.

## Known gaps

* **`cursive` and `fantasy` both land on Comic Neue.** Genuine Windows has Comic Sans MS and
  Impact, which differ. `fantasy` is deliberately not remapped: macOS's default for it is
  Papyrus, which is a family real Windows machines *have*, so remapping would make
  `font-family: Papyrus, fantasy` resolve to Impact where Windows resolves it to Papyrus —
  trading a present font for an absent one, the worse of the two errors.
* **Emoji is not substituted.** `MetricCompatibleSubstitute` maps Segoe UI Emoji to Noto Color
  Emoji, which `assets/fonts/` does not ship and which CoreText cannot render in any case (it
  is a CBDT/CBLC bitmap font; CoreText does COLR/CPAL and sbix).
* **`957` resolves `Helvetica` to Arial from the HKLM `FontSubstitutes` registry key**, read
  from documentation rather than measured on a Windows host. Worth confirming on a Windows run.
* **The clone widths are the clones' widths.** Nothing here compares them against a genuine
  Windows Chrome; `assets/fonts/ATTRIBUTION.md` already records Consolas/Inconsolata as the
  known-worst case.
* **`scripts/verify_patches.py` cannot complete Layer 1 on a macOS tree.**
  `905-rc-invoked-guard.patch` flips `#if (0)` to `#if (1)` in `chrome/app/chrome_command_ids.h`,
  and that `#if (0)` is written by the **ungoogled-chromium-windows** overlay; a macOS tree
  built on the macos overlay has `#if BUILDFLAG(IS_LINUX)` there instead, so `905` never
  applies and the reverse-apply peel stops at it. Layer 0 passes and `956`–`958` peel cleanly;
  everything below `905` in the series goes unchecked on macOS. Pre-existing, not introduced
  by these patches, and the same class of mismatch as the overlay-dependent hunks already
  known on that tree.
* More generally, the gate proves a patch is *in the tree*; it cannot prove the patched file
  is *in the build graph for the target*. That distinction is what let three font patches ship
  into a macOS build doing nothing at all. A `gn desc out/Default <target> sources` assertion
  per patch would close it.
