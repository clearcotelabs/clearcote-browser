# clearcote fingerprint collector

Capture an **exhaustive, importable** fingerprint profile from a real Chrome on any machine, so
clearcote can present *that machine's* identity instead of a synthetic seed-derived one.

## Files
- **`collect.js`** — the collector library; `collectFingerprint()` returns a `Promise<profile>`.
- **`collect.html`** — host this and open it in the donor's Chrome; click to capture + download (and optionally POST to a collector endpoint that attaches the network layer).
- **`snippet.js`** — DevTools-console path: paste `collect.js`, then paste this; it downloads the JSON. No server needed.

## What it captures (the `clearcote-profile` schema)
A **superset** of the [chrome-fingerprints] dataset — nothing JS-observable is left behind:

| Section | Contents |
|---|---|
| `navigator` | UA, app*/product*/vendor*, platform, language(s), DNT, cookie/pdf-viewer, webdriver, **full UA-CH high-entropy** (architecture, bitness, model, platformVersion, uaFullVersion, **fullVersionList**, wow64, formFactors) |
| `screen` | all `screen.*` + orientation + DPR + window outer/inner/screenX/Y |
| `webgl` | `webgl1` + `webgl2`: **every `getParameter` value**, all 12 `getShaderPrecisionFormat` triples, `getSupportedExtensions`, `getContextAttributes`, drawing-buffer, max-anisotropy, unmasked vendor/renderer |
| `audio` | the **full Web Audio constant table** — every node's AudioParam default/min/max (Analyser, Biquad, BufferSource, ConstantSource, Delay, DynamicsCompressor, Gain, Oscillator, Panner, StereoPanner, **AudioListener**) + context sampleRate/latency |
| `speech` | full `getVoices()` (voiceURI, name, lang, localService, default) |
| `fonts` | detected fonts via measureText probe over a broad Windows/cross-platform list |
| `codecs` | `canPlayType` + `MediaSource.isTypeSupported` + `mediaCapabilities.decodingInfo` matrix |
| `css` | every fingerprintable `@media` query (pointer/hover/any-*, color-gamut, prefers-*, resolution, orientation, color depth…) |
| `webgpu` | adapter info (vendor/architecture/device/description), features, **all limits**, preferred canvas format |
| `webrtc` | RtpSender/Receiver audio+video codec + header-extension capabilities |
| `plugins`/`mime_types`, `keyboard` (getLayoutMap), `perf_memory` (jsHeapSizeLimit/total/used), `connection`, `media_devices`, `battery`, `permissions`, `intl`/timezone, `math` precision vector, `client_rects`, `canvas` (reference hashes), `hardware_concurrency`, `device_memory`, `max_touch_points` | |
| `network` | **filled server-side** — HTTP header set+order, TLS/HTTP-2 fingerprint (only via the hosted endpoint; not JS-reachable) |

Each profile carries `meta` (schema_version, captured_at, chrome_version, source).

## Two capture layers
1. **JS-observable** (everything above) — `collect.js`, runs in the page.
2. **Network** (`network`) — HTTP header **order** + TLS JA4 / HTTP-2 SETTINGS. These cannot be read from JS; the optional collector endpoint records them from the incoming request and stitches them into the profile. *(Lower priority: clearcote's native TLS/HTTP-2 already matches the advertised Chrome.)*

## Bootstrap from the 10k-profile dataset — `convert_dataset.py`
No donor machines? The open-source [chrome-fingerprints] dataset ships **10,000 real
Windows-Chrome fingerprints**. `convert_dataset.py` turns any record into a clearcote-profile,
resolving the dataset's string interning (it stores `voice_uri`/`fonts`/extension values as
integer refs) and remapping its `webgl.properties` (157 keys, camelCase) + `audio` (108 keys)
onto our schema:

```bash
pip install chrome-fingerprints                         # provides the dataset + tables
python convert_dataset.py --out ./profiles --count 100  # convert the first 100 records
python convert_dataset.py --index 0 --stdout            # inspect a single record
```

Then feed a profile to the browser like any other capture:

```python
from clearcote import launch
launch(fingerprint="seed-1", fingerprint_profile="./profiles/profile-00000.json")
```

It imports the **version-independent hardware identity** — GPU (WebGL unmasked vendor/renderer +
GL/GL2 `MAX_*` limits + bit depths/ranges), screen geometry, fonts, speech voices, Web Audio
metadata, CPU/memory, keyboard layout. It deliberately **does not import the dataset's Chrome
version** (the records are Chrome ~114/115; the clearcote binary is 149 — importing
`uaFullVersion` would disagree with the real UA string, a coherence tell). Pass `--include-version`
to override only if your binary's major version matches the dataset.

## How clearcote consumes a profile (engine)
- **`--fingerprint-profile=<gzip+base64 JSON>`** — the renderer base64-decodes + gunzips + parses the profile and the persona becomes **profile-driven**: any field present overrides the seed-derived value, absent fields fall back to `DerivePersona(seed)` so partial profiles stay coherent. (gzip keeps a full ~40 KB capture within the command-line length limit.)
- **SDK** (does the gzip+base64 for you, from a path / object / JSON string): `launch(fingerprint_profile="profile.json")` (Python) / `{ fingerprintProfile: "profile.json" }` (Node).
- **Implemented + verified (Phase 2):** the core identity — `hardwareConcurrency`, `deviceMemory`, screen geometry (w/h/avail/colorDepth/DPR), WebGL unmasked vendor+renderer and the GL/GL2 `MAX_*` limits, audio sample-rate/latency, Chrome full/major version.
- **Implemented + verified (Phase 3a):** **speech voices** — `speechSynthesis.getVoices()` presents the persona's voices (a default Windows SAPI set, or the imported profile's exact `speech` list), closing the `getVoices()=0` headless tell.
- **Implemented + verified (Phase 3b):** **font list** — `font_cache` reports exactly the profile's `fonts.detected` set as present (everything else hidden), so `measureText` font enumeration matches the donor machine.
- **Implemented + verified (Phase 3c):** the full WebGL `getParameter` table (bit depths, aliased ranges, anisotropy, `getSupportedExtensions`) and CSS `@media` display characteristics (`device-width/height`, `resolution`, color depth, `color-gamut`). CSS overrides are gated on `--fingerprint` so unfingerprinted runs keep the real screen.
- **Spec-fixed — no spoof needed:** the Web Audio AudioParam constant table and WebRTC `getCapabilities()` codec lists are hardcoded / compile-time-fixed — identical in every Chrome of the same version — so clearcote already matches real Chrome. The collector still captures them for completeness.
- Render-dependent surfaces (canvas pixels, audio DSP output) are **not** statically replayed — they're handled by clearcote's farbling (or `--disable-fingerprint-noise` → real GPU). The profile stores their *metadata*, not a replay.

[chrome-fingerprints]: https://github.com/Vinyzu/chrome-fingerprints
