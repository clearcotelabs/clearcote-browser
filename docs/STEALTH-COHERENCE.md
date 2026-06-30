# Stealth-coherence gate

A regression gate that launches the shipped `chrome.exe` and asserts the
persona/farble layer is **internally coherent** — that it doesn't give itself
away in the ways a strict fingerprint-coherence check can see.

Every assertion is **self-referential**: it compares the browser against *itself*
across execution contexts and origins, never against an external "known-good"
value. That's deliberate — it means the gate needs **no network, no reference
corpus, and no second browser** to decide pass/fail, so it's a reliable CI gate
rather than a flaky one. (An optional `--baseline` run cross-checks that stock
Chrome passes every check, which validates the gate's own logic.)

## Why

A genuine Chromium is coherent:

- text metrics land on a fixed sub-pixel grid (multiples of **1/512 px** at `dpr=1`),
- the same string measures **identically on the main thread and in a worker**,
- the rect APIs (`getBoundingClientRect` vs `Range.getClientRects`) **agree**,
- a given machine renders the **same bytes on every site**.

A farble layer that breaks one of these invariants becomes detectable **without
the detector knowing the true value** — it just checks the invariant. This gate
locks the invariants in so the moment one breaks (say, a perturbation that only
hooks the window context and leaves workers un-noised) the **build fails instead
of the tell shipping**.

## The checks

| id | invariant | how a farble bug breaks it |
|----|-----------|----------------------------|
| `measuretext-grid` | every `measureText` width is a multiple of 1/512 px | a uniform sub-grid scale pushes widths off-grid |
| `worker-vs-main` | `measureText(s)` equal on main thread and in an `OffscreenCanvas` worker | farble hooks only the window execution context |
| `bcr-vs-range` | `el.getBoundingClientRect().left == Range.getClientRects()[0].left` for the same node, both on-grid | rect farble applied inconsistently across APIs |
| `origin-invariant` | canvas2d + WebGL readback hashes identical across two registrable domains in one session | farble seed keyed by registrable domain |
| `webgl-webgpu-vendor-match` | WebGPU adapter vendor agrees with the WebGL `UNMASKED_VENDOR` family | GPU identity spoofed on one surface but not the other |

## Run it

```bash
# gate the local build (auto-finds win-x64/chrome.exe, or set CLEARCOTE_BINARY)
py -3 scripts/stealth_coherence.py
py -3 scripts/stealth_coherence.py --binary C:\clearcote\chrome.exe --json out.json

# prove the checks themselves are correct: stock Chrome must pass all of them
py -3 scripts/stealth_coherence.py --baseline

# validate the check logic with no binary at all (runs anywhere, used on PRs)
py -3 scripts/stealth_coherence.py --selftest
```

Exit codes: `0` contract satisfied · `1` contract violation · `2` missing dep/binary · `3` gate crashed.

## The expected-state contract

Each check is either **REQUIRED** (must pass — the contract) or a **KNOWN_GAP** (a
tell not yet closed, tracked with its fix location), both declared at the top of
`scripts/stealth_coherence.py`. The gate **fails** when:

- a REQUIRED check fails → a **regression**, or
- a KNOWN_GAP check now **passes** → it got fixed; **promote it into `REQUIRED`** so
  it can never silently regress afterwards (the gate prints exactly this).

So today's documented gaps don't block releases, but every engine fix is enforced
forward the instant it lands.

### Fixed + enforced

| id | fix that landed |
|----|-----|
| `measuretext-grid` | measureText perturbation set to factor 0 (patch `060`): metrics are truthful and on the 1/512 grid. Entropy comes from the spoofed font set, not a sub-grid scale. |
| `worker-vs-main` | with the metric scale gone, the main thread returns the same truthful metrics as an `OffscreenCanvas` worker (patch `060`). |
| `bcr-vs-range` | clientRects offset set to factor 0 (patch `050`): `getBoundingClientRect` and `Range` rects agree and stay on-grid. |

These are now `REQUIRED` — a regression fails the build.

### Current known gaps

| id | status |
|----|-----|
| `origin-invariant` | **Left domain-keyed by design.** canvas/WebGL readback is keyed by registrable domain, so one identity renders differently per site. This retains per-site noise-unlinkability; flipping the seed to persona-keyed (patch `001`) is only warranted if the usage model becomes one-profile-per-identity. |

## CI

`.github/workflows/stealth-coherence.yml`:

- **`selftest`** (Ubuntu) runs on every PR that touches the gate — validates the
  check logic with no binary.
- **`gate`** (Windows) runs on `workflow_dispatch` and as a `workflow_call` release/
  promotion gate (the npm + PyPI publish workflows call it on every SDK deploy): it
  downloads + checksum-verifies the SDK-pinned `chrome.exe`
  (`scripts/fetch_release_binary.py`) and runs the gate against that exact build.

Run it locally as part of `docs/RELEASING.md` before publishing, and it gates the
pre-release → stable promotion.
