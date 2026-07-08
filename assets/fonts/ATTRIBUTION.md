# Bundled fonts — attribution & licenses

clearcote ships these **metric-compatible** open-source font clones with the Linux
release so that pages requesting common Windows font families render with the correct
metrics on servers/containers that don't have those families installed. Each clone is
redistributed under its own permissive license.

| Clone | Substitutes for | License | Upstream |
|---|---|---|---|
| Arimo | Arial / Helvetica | Apache-2.0 | Google Croscore |
| Tinos | Times New Roman / Times | Apache-2.0 | Google Croscore |
| Cousine | Courier New / Courier | Apache-2.0 | Google Croscore |
| Gelasio | Georgia | Apache-2.0 | Google |
| Carlito | Calibri | SIL OFL 1.1 | Google |
| Caladea | Cambria | SIL OFL 1.1 | Google |
| Selawik | Segoe UI | SIL OFL 1.1 | Microsoft |
| DejaVu Sans | Verdana | DejaVu Fonts License (permissive, Bitstream Vera derivative) | dejavu-fonts.github.io |
| DejaVu Sans Condensed | Tahoma | DejaVu Fonts License | dejavu-fonts.github.io |
| Comic Neue | Trebuchet MS | SIL OFL 1.1 | comicneue.com |
| Noto Sans | Comic Sans MS | SIL OFL 1.1 | Google Noto |
| Anton | Impact | SIL OFL 1.1 | Google Fonts |
| Inconsolata | Consolas | SIL OFL 1.1 | Google Fonts |

- **Apache-2.0**: <https://www.apache.org/licenses/LICENSE-2.0>
- **SIL Open Font License 1.1**: <https://openfontlicense.org/>
- **DejaVu Fonts License**: <https://dejavu-fonts.github.io/License.html> (Bitstream Vera + Arev, MIT-style permissive)

The name→clone mapping lives in `fonts.conf.template` and mirrors the engine's
`MetricCompatibleSubstitute` map; the SDK substitutes the runtime path and points
`FONTCONFIG_FILE` at it on Linux launch. Each Windows family maps to a **distinct** clone
whose advance width is close to the real font's, so a reference-free equality check (e.g.
`width(Verdana) == width(Arial)`, impossible on real Windows) no longer fires. The mapping
is metric-oriented, not glyph-identical; the one notable residual is **Consolas**, whose
exact width has no open monospace clone (Inconsolata is the nearest distinct mono, so
`Consolas` no longer collapses onto Courier New's width but is still ~90px off the real
value) — documented in the release notes.
