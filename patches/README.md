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
| `001-farble-seed-core` | The per-eTLD+1 seed engine (`farble_seed.{cc,h}`), the `fingerprint_data.h` tables, and its `BUILD.gn`. Every surface below reads from this. |
| `010-user-agent-and-webdriver` | UA / UA-CH (`navigator.userAgent`, `Sec-CH-UA*`), `navigator.platform` spoofing, and hiding `navigator.webdriver` / the automation + Headless tells. |
| `020-audio` | AudioContext farbling (`AudioBuffer`, analyser, offline render) keyed to the per-site seed. |
| `030-hardware-concurrency` | `navigator.hardwareConcurrency` and `deviceMemory` (seed-derived, with the `--fingerprint-hardware-concurrency` override). |
| `040-fonts` | Font enumeration via the engine allowlist (`font_cache.cc`). |
| `050-shadow-dom` | Closed shadow-root semantics + related DOM (`document`, `element`, `range`). |
| `060-canvas` | Canvas 2D farbling — `toDataURL`/`getImageData`/`toBlob`/`measureText` and the encoder/bitmap paths. |
| `070-webgl-gpu` | WebGL `UNMASKED_VENDOR/RENDERER`, GPU info, and `readPixels` farbling (`gpu_fingerprint.{cc,h}`). |
| `080-client-rects` | Sub-pixel jitter for client-rect geometry (`quad_f`). |
| `090-timezone` | Native timezone pin (`timezone_controller.cc`, `--timezone`). |
| `100-webrtc-leak` | WebRTC IP-leak coverage — ICE handling + the `webrtc-ip` switch (`peer_connection_dependency_factory`, `p2p/base` port/stun). |
| `110-runtime-enable` | Suppresses the `Runtime.enable` CDP automation tell (`v8-runtime-agent-impl`). |
| `120-headless` | Removes a headless-mode tell (`headless_browser_impl`). |
| `900-windows-build-fixes` | Cross-build mechanics for the Windows target: the `rc.py` resource-compiler wrapper, the `.rc` branding conditionals, a missing UIA `CLSID`, and the `compiler_builtins` fix. Not fingerprint behavior — needed to link the Windows binary on Linux. |

## Re-generating / verifying

The set is produced by diffing the build tree against a freshly reconstructed
`pristine 149 → prune → ungoogled base → windows overlay` baseline, so it captures exactly the
Clearcote delta and nothing else. Every patch re-applies to that baseline with zero rejects.
After editing a patch, rebuild **incrementally** (`ninja -C out/Default chrome`) — never a clean
rebuild — so only the touched translation units recompile.
