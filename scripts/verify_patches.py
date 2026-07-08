#!/usr/bin/env python3
"""Patch-integrity release gate for Clearcote.

Clearcote *is* its patch set — every stealth behavior lives in `patches/`, applied on top
of a pinned Chromium. A patch that silently fails to apply, gets dropped on an incremental
rebuild, or is left out of a stale committed set does NOT crash the browser: it still
launches and runs, just **less stealthy**. That failure is invisible to any "does it start?"
smoke test, so it must be caught by an explicit, release-blocking gate. This is that gate.

WHY THREE LAYERS — "applied" means three different things, and each fails independently:

  Layer 1  APPLIED TO SOURCE   Every patch in `patches/series` reverse-applies cleanly to the
                               build tree that produced the binary (`patch --dry-run -R`). If a
                               patch's exact hunks are present, it reverses; if not, it is
                               missing or was mangled. Zero-maintenance: the patch files are the
                               spec, so a newly added patch is covered automatically. Catches a
                               silently-rejected/dropped patch at build time, and a stale
                               committed `patches/` that no longer reproduces the built tree.
  Layer 2  COMPILED INTO BINARY Each patch's required marker string(s) are present in the shipped
                               chrome / chrome.dll. Catches "source is right but the binary is
                               stale" — an incremental build that didn't recompile the touched
                               translation unit, the wrong out-dir, or dead-code elimination.
  Layer 3  EFFECTIVE AT RUNTIME A behavioral witness per surface actually observes the effect.
                               Catches "compiled in but broken." Delegated to
                               scripts/stealth_coherence.py + the per-patch witnesses recorded in
                               scripts/patch_markers.json (see docs/PATCH-INTEGRITY.md).

Any failure exits non-zero, so scripts/01-apply-patches.sh, the release runbook, and CI all
abort. See docs/PATCH-INTEGRITY.md for the standard and how to add a witness for a new patch.

Usage:
  # Layer 0 (series/manifest integrity) always runs.
  # Layer 1 — against the build tree (run at build time AND right before packaging):
  python scripts/verify_patches.py --tree ~/clearcote-build/build/src --target windows
  # Layer 2 — against a shipped binary, a directory of binaries, or the release zip:
  python scripts/verify_patches.py --binary out/Default/chrome.dll
  python scripts/verify_patches.py --binary clearcote-149.0.7827.114-windows-x64.zip
  # Both (the full pre-release gate):
  python scripts/verify_patches.py --tree <src> --target windows --binary <chrome.dll>

Stdlib only. Exit 0 = every checked layer clean; exit 1 = a patch is not correctly applied.
"""

from __future__ import annotations

import argparse
import json
import mmap
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PATCHES_DIR = ROOT / "patches"
SERIES = PATCHES_DIR / "series"
MANIFEST = ROOT / "scripts" / "patch_markers.json"

# Windows-only patches — mirror scripts/01-apply-patches.sh (the ungoogled-chromium-windows
# overlay + this list are skipped for the Linux target). Kept here as the source of truth for
# the gate; the manifest's per-patch `windows_only` is cross-checked against it.
WINDOWS_ONLY = {"900-windows-build-fixes.patch"}

_GHA = bool(os.environ.get("GITHUB_ACTIONS"))


def err(msg: str) -> None:
    print(f"{'::error::' if _GHA else 'ERROR: '}{msg}")


def note(msg: str) -> None:
    print(f"{'::notice::' if _GHA else '  '}{msg}")


def die(msg: str) -> None:
    """Setup/usage error — stop immediately (distinct from a patch-integrity failure)."""
    err(msg)
    sys.exit(2)


def read_series() -> list[str]:
    if not SERIES.is_file():
        die(f"series file not found: {SERIES}")
    out: list[str] = []
    for line in SERIES.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip()
        if name:
            out.append(name)
    return out


# --------------------------------------------------------------------------- Layer 0
def layer0_integrity(series: list[str], manifest: dict | None) -> list[str]:
    """Series <-> patch files <-> manifest consistency. Needs no tree or binary, so it always
    runs — it catches an orphan patch (a .patch file not in series never applies) or a patch
    with no recorded witness (which would slip past Layers 2/3)."""
    fails: list[str] = []

    # duplicates in series
    seen: set[str] = set()
    for p in series:
        if p in seen:
            fails.append(f"[series] '{p}' listed more than once")
        seen.add(p)

    # every series entry exists as a file
    for p in series:
        if not (PATCHES_DIR / p).is_file():
            fails.append(f"[series] '{p}' is listed but patches/{p} does not exist")

    # every *.patch file is in series (an orphan silently never applies)
    on_disk = {p.name for p in PATCHES_DIR.glob("*.patch")}
    for p in sorted(on_disk - set(series)):
        fails.append(f"[series] patches/{p} exists but is NOT listed in series (it will never be applied)")

    # manifest completeness: every series patch must have a witness entry, and the manifest
    # must not reference a patch that no longer exists. This is what forces a new patch to
    # ship with a marker/witness instead of silently escaping Layers 2/3.
    if manifest is not None:
        entries = manifest.get("patches", {})
        for p in series:
            if p not in entries:
                fails.append(f"[manifest] '{p}' has no witness entry in {MANIFEST.name} "
                             f"(add its markers/runtime witness — see docs/PATCH-INTEGRITY.md)")
        for p in sorted(set(entries) - set(series)):
            fails.append(f"[manifest] {MANIFEST.name} references '{p}' which is not in series")
        # cross-check windows_only agreement
        for p, e in entries.items():
            declared = bool(e.get("windows_only"))
            expected = p in WINDOWS_ONLY
            if declared != expected:
                fails.append(f"[manifest] '{p}' windows_only={declared} disagrees with the gate's "
                             f"WINDOWS_ONLY set ({expected})")

    if not fails:
        n = len(series)
        note(f"Layer 0 OK: {n} patches, series<->files consistent"
             + ("; manifest complete" if manifest is not None else "; (no manifest loaded)"))
    return fails


# --------------------------------------------------------------------------- Layer 1
def _patch_bin() -> str:
    from shutil import which
    exe = which("patch")
    if not exe:
        die("`patch` not found on PATH — Layer 1 (reverse-apply) needs GNU patch")
    return exe


def layer1_source(tree: Path, target: str, series: list[str]) -> list[str]:
    """Reverse-apply dry-run of every series patch against the build tree. A patch that was
    correctly applied reverses cleanly (returncode 0); one that is missing or mangled fails."""
    if not tree.is_dir():
        die(f"--tree {tree} is not a directory")
    patch = _patch_bin()
    fails: list[str] = []
    checked = skipped = 0
    for p in series:
        if target == "linux" and p in WINDOWS_ONLY:
            skipped += 1
            continue
        checked += 1
        proc = subprocess.run(
            [patch, "-p1", "-R", "--dry-run", "-f", "-i", str(PATCHES_DIR / p), "-d", str(tree)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if proc.returncode != 0:
            reason = "missing or mangled in the tree"
            out = proc.stdout or ""
            if "can't find file" in out or "No file to patch" in out:
                reason = "target file(s) absent"
            elif "Unreversed patch detected" in out:
                reason = "hunks do not match (tree diverged from the committed patch)"
            elif "FAILED" in out:
                bad = [ln for ln in out.splitlines() if "FAILED" in ln]
                reason = "hunk mismatch: " + "; ".join(bad[:3])
            fails.append(f"[source] {p} does NOT reverse-apply cleanly -> {reason}")
    if not fails:
        note(f"Layer 1 OK: all {checked} patches reverse-apply cleanly to {tree} "
             f"(target={target}, {skipped} windows-only skipped)")
    return fails


# --------------------------------------------------------------------------- Layer 2
def _iter_binaries(binary: Path) -> list[tuple[str, Path, "tempfile.TemporaryDirectory | None"]]:
    """Yield (label, path, tmpdir_keepalive) for each scannable binary. A .zip is expanded to a
    temp dir and its chrome/chrome.exe/chrome.dll members are scanned; a dir is scanned for the
    same; a plain file is scanned directly."""
    out: list[tuple[str, Path, object]] = []
    names = ("chrome", "chrome.exe", "chrome.dll")
    if binary.is_file() and binary.suffix.lower() == ".zip":
        td = tempfile.TemporaryDirectory(prefix="cc-verify-")
        with zipfile.ZipFile(binary) as z:
            for m in z.namelist():
                base = os.path.basename(m)
                if base in names:
                    z.extract(m, td.name)
                    out.append((base, Path(td.name) / m, td))
        if not out:
            die(f"--binary {binary} (zip) has no chrome/chrome.exe/chrome.dll member")
        return out
    if binary.is_dir():
        for n in names:
            f = binary / n
            if f.is_file():
                out.append((n, f, None))
        if not out:  # nested layout (e.g. some extractors keep an archive subdir) — search
            for n in names:
                found = next(iter(binary.rglob(n)), None)
                if found:
                    out.append((n, found, None))
        if not out:
            die(f"--binary {binary} (dir) has no chrome/chrome.exe/chrome.dll")
        return out
    if binary.is_file():
        return [(binary.name, binary, None)]
    die(f"--binary {binary} not found")
    return out  # unreachable


def _present(paths: list[Path], needle: str) -> bool:
    b = needle.encode("utf-8", "ignore")
    if not b:
        return False
    for path in paths:
        try:
            with open(path, "rb") as fh:
                with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    if mm.find(b) != -1:
                        return True
        except (ValueError, OSError):
            # empty file / mmap failure -> fall back to a chunked scan
            try:
                data = path.read_bytes()
                if b in data:
                    return True
            except OSError:
                pass
    return False


def layer2_binary(binary: Path, manifest: dict, series: list[str], target: str | None) -> list[str]:
    """Every patch that declares binary markers must have >=1 marker present in the shipped
    binary set. Patches with no markers are runtime-only (Layer 3) and skipped here."""
    scans = _iter_binaries(binary)
    paths = [p for (_lbl, p, _td) in scans]
    labels = ", ".join(sorted({lbl for (lbl, _p, _td) in scans}))

    # infer target from what we're scanning, unless overridden
    if target is None:
        target = "windows" if any(lbl.endswith((".dll", ".exe")) for (lbl, _p, _td) in scans) else "linux"

    entries = manifest.get("patches", {})
    fails: list[str] = []
    verified = runtime_only = skipped = 0
    for p in series:
        e = entries.get(p, {})
        if target == "linux" and (p in WINDOWS_ONLY or e.get("windows_only")):
            skipped += 1
            continue
        markers = [m.get("string", "") if isinstance(m, dict) else str(m) for m in e.get("markers", [])]
        markers = [m for m in markers if m]
        if not markers:
            runtime_only += 1
            continue
        if any(_present(paths, m) for m in markers):
            verified += 1
        else:
            fails.append(f"[binary] {p}: NONE of its markers are present in {labels} "
                         f"({markers[:4]}) — the patch did not compile into the binary")
    if not fails:
        note(f"Layer 2 OK: {verified} patches have a marker in the binary "
             f"({labels}); {runtime_only} runtime-only; {skipped} windows-only skipped")
    return fails


# --------------------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser(description="Clearcote patch-integrity release gate")
    ap.add_argument("--tree", type=Path, help="build source tree to reverse-apply patches against (Layer 1)")
    ap.add_argument("--target", choices=("windows", "linux"),
                    help="build target for --tree (which patches to expect); required with --tree")
    ap.add_argument("--binary", type=Path,
                    help="shipped chrome/chrome.dll, a dir of them, or the release .zip (Layer 2)")
    ap.add_argument("--binary-target", choices=("windows", "linux"),
                    help="override target inference for --binary")
    args = ap.parse_args()

    if not args.tree and not args.binary:
        # Layer 0 alone is still useful (CI on every push), but warn it's not a full gate.
        note("no --tree/--binary given: running Layer 0 (integrity) only")

    manifest: dict | None = None
    if MANIFEST.is_file():
        try:
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            die(f"{MANIFEST} is not valid JSON: {exc}")
    elif args.binary:
        die(f"--binary needs the marker manifest {MANIFEST} (run the release runbook's witness step)")

    series = read_series()
    fails: list[str] = []

    fails += layer0_integrity(series, manifest)

    if args.tree:
        if not args.target:
            die("--tree requires --target windows|linux (which patch set to expect)")
        fails += layer1_source(args.tree, args.target, series)

    if args.binary:
        assert manifest is not None
        fails += layer2_binary(args.binary, manifest, series, args.binary_target)

    print()
    if fails:
        err(f"PATCH INTEGRITY FAILED — {len(fails)} problem(s); release BLOCKED:")
        for f in fails:
            print(f"    - {f}")
        print("\nSee docs/PATCH-INTEGRITY.md. Do NOT release until every layer is clean.")
        sys.exit(1)
    print("OK: patch integrity verified — every checked layer is clean.")


if __name__ == "__main__":
    main()
