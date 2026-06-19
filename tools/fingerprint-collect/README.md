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

## Using an external dataset to bootstrap
The [chrome-fingerprints] dataset (10k real Windows Chrome records) maps cleanly onto this schema and is a great library seed. Two notes for the converter (Phase 4):
- It **interns strings as integer refs** (`voice_uri: 24`, `fonts: [0,1,…]`, header/codec indices) — resolve them via its string table when importing.
- Its `webgl.properties` (157 keys) and `audio` (108 keys) map onto our `webgl.*.parameters`/`shader_precision` and `audio` dicts; our dicts accept arbitrary keys, so no field is dropped.

## How clearcote consumes a profile (engine)
- **`--fingerprint-profile=<gzip+base64 JSON>`** — the renderer base64-decodes + gunzips + parses the profile and the persona becomes **profile-driven**: any field present overrides the seed-derived value, absent fields fall back to `DerivePersona(seed)` so partial profiles stay coherent. (gzip keeps a full ~40 KB capture within the command-line length limit.)
- **SDK** (does the gzip+base64 for you, from a path / object / JSON string): `launch(fingerprint_profile="profile.json")` (Python) / `{ fingerprintProfile: "profile.json" }` (Node).
- **Implemented + verified (Phase 2):** the core identity — `hardwareConcurrency`, `deviceMemory`, screen geometry (w/h/avail/colorDepth/DPR), WebGL unmasked vendor+renderer and the GL/GL2 `MAX_*` limits, audio sample-rate/latency, Chrome full/major version.
- **Implemented + verified (Phase 3a):** **speech voices** — `speechSynthesis.getVoices()` presents the persona's voices (a default Windows SAPI set, or the imported profile's exact `speech` list), closing the `getVoices()=0` headless tell.
- **Coming (Phase 3b+):** font list, the full WebGL `getParameter` table, the Web Audio constant table, WebRTC codec capabilities, CSS media queries.
- Render-dependent surfaces (canvas pixels, audio DSP output) are **not** statically replayed — they're handled by clearcote's farbling (or `--disable-fingerprint-noise` → real GPU). The profile stores their *metadata*, not a replay.

[chrome-fingerprints]: https://github.com/Vinyzu/chrome-fingerprints
