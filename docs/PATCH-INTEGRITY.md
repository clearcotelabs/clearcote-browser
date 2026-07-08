# Patch integrity — the release gate standard

**Clearcote *is* its patch set.** Every stealth behavior lives in [`../patches/`](../patches),
applied on top of a pinned Chromium (there is no compiled-in behavior that isn't a readable
patch — see [`../patches/README.md`](../patches/README.md)). So the single most important
release invariant is: **every patch is correctly applied, compiled in, and effective — and no
build ships if it isn't.**

## Why this needs a hard gate (the failure mode)

A lost stealth patch **fails open.** If `040-fonts` or `010-user-agent-and-webdriver` silently
fails to apply — a reject swallowed during an incremental rebuild, a stale committed `patches/`
set, an upstream merge that moved the anchor, a translation unit that didn't recompile — the
browser **still launches and still works.** It is simply *less stealthy*: `navigator.webdriver`
leaks, or the font list no longer matches the persona, or the TLS ClientHello reverts to the
build's native shape. Nothing crashes. No smoke test notices. The first thing that "notices" is
the detector we were trying to beat.

That is the opposite of normal software, where a broken change usually breaks loudly. Here the
broken change is *invisible at runtime unless you look for the specific missing behavior.* The
only defense is an **explicit, per-patch, release-blocking check.** That is
[`../scripts/verify_patches.py`](../scripts/verify_patches.py).

## The standard: three layers, because "applied" fails three ways

"Is the patch applied?" has three independent answers, and a build can pass one while failing
the next. The gate checks all three; **any failure exits non-zero and blocks the release.**

| Layer | Question | Mechanism | Failure it catches |
|---|---|---|---|
| **1 — Source** | Are the patch's exact hunks in the tree that built the binary? | `patch --dry-run -R` of every `series` patch against the build tree. A correctly-applied patch reverses cleanly; a missing/mangled one fails. | A patch that silently rejected, fuzzed into the wrong place, or partially applied; a committed `patches/` set that no longer reproduces the built tree. |
| **2 — Binary** | Did that source actually make it into the shipped binary? | Required **marker strings** (command-line switch values + distinctive literals the patch adds) must be present in `chrome` / `chrome.dll`. | A stale binary: the incremental build didn't recompile the touched TU, the wrong `out/` dir was packaged, or dead-code elimination dropped it. Source is right; the artifact is wrong. |
| **3 — Runtime** | Does the patched code actually *do* its job? | A behavioral **witness** per surface (`navigator.webdriver===false`, `getVoices()` non-empty with the persona set, `measureText` grid quantization, WebGL persona renderer, …), run via [`stealth_coherence.py`](../scripts/stealth_coherence.py) against the launched binary. | "Compiled in but broken" — the code is present but a logic error or a bad interaction makes the effect wrong. |

**Why all three, not just one.** They are not redundant; each has a blind spot the next covers:

- Layer 1 can be perfectly clean while the **binary is stale** (you edited source, but `ninja`
  didn't rebuild that object, or you packaged the wrong `out/` dir). Only Layer 2 sees that.
- Layer 2 can pass — the marker string is in the binary — while the **behavior is broken**
  (the switch is parsed but the code path that uses it regressed). Only Layer 3 sees that.
- Layer 3 is the strongest but the most expensive and least exhaustive (you only assert the
  surfaces you wrote witnesses for). Layers 1–2 are cheap and **total over the patch set**, so
  they are the backstop that guarantees *nothing* is silently missing.

Layer 1 is the anchor: it is **exact** (byte-level hunk matching) and **zero-maintenance** —
the patch files are the specification, so a newly added patch is covered the moment it lands in
`series`, with no extra code to write.

## What the gate enforces (Layer 0: integrity)

Before any tree/binary check, `verify_patches.py` asserts the bookkeeping is sound — this is
what forces the standard to stay honest as patches are added:

- Every entry in `series` exists as a file, and every `*.patch` file is listed in `series`
  (an **orphan patch never applies** and would otherwise be a silent hole).
- No duplicate `series` entries.
- **Manifest completeness:** every `series` patch has a witness entry in
  [`../scripts/patch_markers.json`](../scripts/patch_markers.json). You **cannot add a patch
  without recording how to verify it** — a patch with no entry fails Layer 0, so it can't slip
  past Layers 2/3 unnoticed.

## Where it runs (three enforcement points — a release genuinely cannot skip it)

1. **Build time** — [`scripts/01-apply-patches.sh`](../scripts/01-apply-patches.sh) runs Layer 1
   immediately after applying the series. A silent apply failure aborts the build *before any
   compilation*, not after a multi-hour build. (`CLEARCOTE_SKIP_PATCH_VERIFY=1` for local
   debugging only — never in a release build.)
2. **Release time** — [`docs/RELEASING.md`](RELEASING.md) §11.0a runs the **full** gate
   (`--tree` after `gen_patches.sh` regenerates `patches/`, **and** `--binary` against the
   staged `chrome.dll`) as a mandatory step. Layer 1 here proves the *committed* patch set
   reproduces the built tree; Layer 2 proves that tree's code is in the artifact being signed.
3. **CI** — [`.github/workflows/patch-integrity.yml`](../.github/workflows/patch-integrity.yml)
   runs Layer 0 on every push/PR (integrity + manifest completeness, no tree needed). Layer 2
   against the pinned release binary is an **on-demand** job (the "Run workflow" button, on a
   Windows runner): dispatch it right after a release — when the pin matches the patch set — as a
   post-publish check on the artifact users actually download. It is intentionally *not*
   per-push, because between releases `patches/` runs ahead of the pin, so a marker for a
   not-yet-released patch would be legitimately absent from the older pinned binary. The
   authoritative *pre-publish* Layer 2 is §4a below, against the freshly-built binary.

## Adding a patch — the maintenance contract

When you add `NNN-thing.patch` to `series`, you must also add its witness to
`scripts/patch_markers.json` or Layer 0 fails:

```jsonc
"NNN-thing.patch": {
  "surface": "one line: what stealth behavior this implements",
  "windows_only": false,                 // true only for the Windows-target overlay patches
  "markers": [                            // >=1 string that SURVIVES into a stripped release binary
    "my-new-switch",                      //   best: a command-line switch value string
    "Some Distinctive Literal"            //   good: a hardcoded literal exposed to JS / logged
  ],                                      //   NEVER a C++ symbol name (stripped); [] = runtime-only
  "runtime": {                            // Layer 3 witness
    "surface": "navigator.somdthing",
    "probe":   "navigator.something",
    "expect":  "the correctly-patched value (vs the vanilla value)"
  }
}
```

Choosing a **binary marker**: prefer the switch value strings the patch registers (they are
compared at runtime, so they are always in the binary and are distinctive to Clearcote). Failing
that, a hardcoded string literal the patch adds that is exposed to JavaScript or logged. Do
**not** use function/class/variable names — the release binary is stripped. If a patch is a pure
algorithmic/geometry change with no surviving distinctive string (e.g. sub-pixel jitter), leave
`markers: []` and rely on the `runtime` witness; Layer 2 skips it and Layer 3 covers it.

## When the gate fails — how to fix it (never suppress it)

- **Layer 1 fails ("does NOT reverse-apply cleanly")** — the committed `patches/` no longer
  match the build tree. This is expected *during development* (the tree drifts ahead of the
  committed set). The fix at release is exactly the runbook: run `gen_patches.sh` to regenerate
  `patches/` **from the tree** (it self-validates zero-reject re-apply to a pristine baseline),
  then re-run the gate — it must now be clean. If a single patch fails right after
  `01-apply-patches.sh`, that patch genuinely didn't apply: fix the reject, don't skip it (see
  the [no-skip-stealth-patches](../CONTRIBUTING.md) rule).
- **Layer 2 fails ("did not compile into the binary")** — the source has the patch but the
  artifact doesn't. Rebuild the affected target (`ninja -C out/Default chrome`), confirm you are
  packaging that `out/` dir, and re-run. If the marker itself was wrong (optimized away),
  correct it in the manifest against a known-good binary.
- **Layer 3 fails** — the behavior regressed; debug the surface. See
  [STEALTH-COHERENCE.md](STEALTH-COHERENCE.md).

**Do not** add `CLEARCOTE_SKIP_PATCH_VERIFY=1` to a release build, and do not delete a patch's
witness to make Layer 0 pass. The whole value of the project is that the stealth is actually
there in what ships.
