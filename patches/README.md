# Clearcote patch set

These are the **Clearcote fingerprint patches** — plain unified diffs against the pinned
Chromium revision in [`../UPSTREAM_REVISION`](../UPSTREAM_REVISION), applied **after** the
ungoogled-chromium base and the ungoogled-chromium-windows overlay (see
[`../scripts/01-apply-patches.sh`](../scripts/01-apply-patches.sh) and
[`../docs/BUILDING.md`](../docs/BUILDING.md)).

They are listed in [`series`](series) and applied in order with ungoogled's quilt-style
patch tool (`patches.py apply`, `-p1`). Reading these diffs *is* how you audit what Clearcote
changes in the engine — there is no compiled-in behavior that isn't here.

> [`../docs/PATCHES.md`](../docs/PATCHES.md) is the broader **design manifest** (the full
> coherence model and the roadmap of surfaces still to come). This README documents what the
> committed patch set actually contains today.

## Seed model

Clearcote derives fingerprint noise **per eTLD+1**, not from a single global seed: the
`--fingerprint` value is the session root, and `components/ungoogled/farble_seed.{cc,h}`
(`ungoogled::GetFarbleSeed64`) mixes it with the registrable domain so the same site is stable
across a session while different sites are mutually unlinkable. The seed parse is non-throwing
(`base::StringToUint64` with a `base::PersistentHash` fallback) — a non-numeric seed can never
crash the renderer.

## The series

| Patch | What it changes |
|---|---|
| `000-fingerprint-switches` | Defines the `--fingerprint*` command-line switches and forwards them from the browser to renderer/child processes. |
| `001-farble-seed-core` | The per-eTLD+1 seed engine (`farble_seed.{cc,h}`), the `fingerprint_data.h` tables, and its `BUILD.gn`. Every noise surface below reads from this. Also exposes `FingerprintNoiseEnabled()` — the master toggle behind `--disable-fingerprint-noise`, which turns the per-site canvas/WebGL/audio/client-rect noise OFF (surfaces return natural values; identity spoofs stay on). |
| `002-persona-profile` | Coherent per-seed persona engine (`components/ungoogled/persona_profile.{cc,h}`) — derives an internally consistent identity so surfaces agree rather than contradict. Also the **profile-import loader**: `--fingerprint-profile=<gzip+base64 JSON>` (`ApplyProfileOverride`) makes the persona present a captured real-Chrome profile, overriding seed-derived fields; absent fields fall back to the seed. |
| `010-user-agent-and-webdriver` | UA / UA-CH (`navigator.userAgent`, `Sec-CH-UA*` incl. the high-entropy `bitness`=`64` / `wow64`=`false` / `model`=`""` fields), `navigator.platform` spoofing, and hiding `navigator.webdriver` / the automation + Headless tells. |
| `020-audio` | AudioContext farbling (`AudioBuffer`, analyser, offline render) keyed to the per-site seed, plus coherent `sampleRate`/`baseLatency`/`outputLatency` metadata from the persona. |
| `030-hardware-concurrency` | `navigator.hardwareConcurrency` and `deviceMemory` (seed-derived, with the `--fingerprint-hardware-concurrency` override). |
| `040-fonts` | Font enumeration via the engine allowlist (`font_cache.cc`) — exposes the persona's font set (a default installed-Windows list, or an imported profile's `fonts.detected`), so `measureText` enumeration matches the donor. |
| `050-shadow-dom` | Closed shadow-root semantics + related DOM (`document`, `element`, `range`). |
| `060-canvas` | Canvas 2D farbling — `toDataURL`/`getImageData`/`toBlob`/`measureText` and the encoder/bitmap paths. |
| `070-webgl-gpu` | WebGL `UNMASKED_VENDOR/RENDERER` (**session-constant** — the persona GPU, identical on every origin), the full `getParameter` table (WebGL1 + WebGL2 limits, bit depths, aliased line/point ranges, max anisotropy) **and** `getSupportedExtensions` — all persona/profile-driven (coherent ANGLE/D3D11 values, or an imported profile's exact GPU), plus GPU info and `readPixels` farbling (`gpu_fingerprint.{cc,h}`, `webgl_rendering_context_base.cc`). |
| `075-webgpu-coherence` | WebGPU `GPUAdapterInfo` (vendor/architecture/device/description) **and** `adapter.limits` (`GPUSupportedLimits`, a canonical desktop table) forced coherent with the WebGL persona GPU (`webgpu/gpu_adapter.cc`) so `navigator.gpu` can't contradict WebGL. Vendor/architecture are derived from the persona's WebGL GPU for **seed-only** personas too (Intel→`intel`/`gen-12-lp`, NVIDIA→`nvidia`/`ampere`, AMD→`amd`/`rdna2`), with `--fingerprint-webgpu-vendor/-architecture` + imported-profile (`webgpu.vendor`/`.architecture`) overrides. |
| `080-client-rects` | Sub-pixel jitter for client-rect geometry (`quad_f`). |
| `090-timezone` | Native timezone pin (`timezone_controller.cc`, `--timezone`). |
| `100-webrtc-leak` | WebRTC: **fabricates** the server-reflexive candidate at `--webrtc-ip` and sends **no real STUN** (`stun_port.cc`), and suppresses raw host candidates (`port.cc`) — reports the proxy IP, never leaks the real IP at the packet level. |
| `110-runtime-enable` | Suppresses the `Runtime.enable` CDP automation tell (`v8-runtime-agent-impl`). |
| `120-headless` | Removes a headless-mode tell (`headless_browser_impl`). |
| `130-humanized-input` | Human-like CDP input + cursor overlay (`devtools/protocol/input_handler`, `browser_handler`, `humanized_cursor_overlay`) so dispatched input isn't trivially bot-flagged. |
| `140-screen` | Coherent display geometry from the persona: `screen.*` (`screen.cc`), multi-screen `getScreenDetails()` (`screen_detailed.cc`), and `window.outer*`/`screenX/Y`/`devicePixelRatio` (`local_dom_window.cc`) — never the real display. |
| `141-media-queries` | CSS `@media` coherence — `(pointer: fine)` / `(hover: hover)` (and the `any-` forms) report a real desktop-with-mouse instead of a touchless/headless device, plus persona/profile-driven display characteristics (`device-width/height`, `resolution`, color depth, `color-gamut`) gated on `--fingerprint` so unfingerprinted runs keep the real screen (`media_values*`). |
| `145-storage-quota` | `navigator.storage.estimate()` quota coherence (`quota/storage_manager.cc`). |
| `146-perf-memory` | `performance.memory.jsHeapSizeLimit` set to a realistic desktop value from the persona (`memory_info.cc`, profile-overridable) so it doesn't vary by host RAM or contradict `deviceMemory`. |
| `147-media-capabilities` | `MediaCapabilities.decodingInfo()` (`media_capabilities.cc`) reports a persona-coherent codec-support matrix (H.264/VP8/VP9/AV1/HEVC `{supported, smooth, powerEfficient}`) from a canonical desktop table, so HW-decode answers stay coherent with the spoofed GPU instead of varying with the host's actual hardware. Imported profiles override per codec. |
| `148-media-devices` | `enumerateDevices()` (`media_devices_manager.cc`) exposes a coherent persona media-device set (a real-Windows onboard audio codec: default/communications/device mic + speaker, one shared `groupId`) with seed-derived stable `deviceId`/`groupId` and empty labels pre-permission, so the device list doesn't read as a headless server. Imported profiles override the roster. |
| `150-device-sensors` | Coherent device APIs: `navigator.getBattery()` (desktop on AC — charging, 100%, no discharge), `navigator.connection` (residential 4g profile), `navigator.keyboard.getLayoutMap()` (US-QWERTY, Writing-System keys only), and `navigator.maxTouchPoints`=0 (mouse-only desktop). |
| `160-coherence-misc` | OS-coherence details: `new URL("C:/").protocol` → `"file:"` (`dom_url.cc`, the Windows behaviour) and exposing `navigator.share`/`canShare` on the Windows-persona build (`chrome_content_renderer_client.cc`). |
| `170-speech-voices` | `speechSynthesis.getVoices()` returns the persona's voice list — a default Windows SAPI set (en-US David/Zira/Mark), or an imported profile's `speech[]`. When fingerprinting, the **browser** serves the persona voice set in place of the build host's real SAPI voices (`speech_synthesis_impl.cc`), so the default voice + ordering track the persona locale (an en-US default — no en-GB host voice leaking under a US persona); the renderer also fills an empty list (`speech_synthesis.cc`), closing the `getVoices()=0` headless tell. |
| `900-windows-build-fixes` | Cross-build mechanics for the Windows target: the `rc.py` resource-compiler wrapper, the `.rc` branding conditionals, a missing UIA `CLSID`, and the `compiler_builtins` fix. Not fingerprint behavior — needed to link the Windows binary on Linux. |

## Re-generating / verifying

The set is produced by diffing the build tree against a freshly reconstructed
`pristine 149 → prune → ungoogled base → windows overlay` baseline, so it captures exactly the
Clearcote delta and nothing else. Every patch re-applies to that baseline with zero rejects.
After editing a patch, rebuild **incrementally** (`ninja -C out/Default chrome`) — never a clean
rebuild — so only the touched translation units recompile.
