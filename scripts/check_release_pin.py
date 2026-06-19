#!/usr/bin/env python3
"""Version-drift guard for the SDK's pinned browser release.

Asserts that the RELEASE constant pinned in BOTH SDKs (sdk/node/src/release.ts and
sdk/python/clearcote/release.py):
  1. is identical across the two SDKs,
  2. is internally consistent (url <-> repo <-> tag <-> asset <-> version, hex digests),
  3. actually exists as a published GitHub release with that asset, and
  4. the pinned sha256 (zip) + exeSha256 (chrome.exe) match the release's published
     SHA256SUMS.txt.

This prevents shipping an SDK whose auto-download points at a missing, renamed, or
checksum-mismatched binary. Run in CI (no token needed for public repos, but set
GITHUB_TOKEN to avoid rate limits).

    python scripts/check_release_pin.py
"""

import json
import os
import re
import runpy
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_RELEASE = ROOT / "sdk" / "python" / "clearcote" / "release.py"
TS_RELEASE = ROOT / "sdk" / "node" / "src" / "release.ts"

FIELDS = ("tag", "version", "asset", "url", "sha256", "exeSha256", "size", "os", "repo", "fpr")


def fail(msg: str) -> "None":
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") else "ERROR: "
    print(f"{prefix}{msg}")
    sys.exit(1)


def load_python() -> dict:
    """release.py is a pure data module (no imports) — exec it in isolation."""
    ns = runpy.run_path(str(PY_RELEASE))
    r = dict(ns["RELEASE"])
    return {
        "tag": r["tag"], "version": r["version"], "asset": r["asset"], "url": r["url"],
        "sha256": r["sha256"], "exeSha256": r["exe_sha256"], "size": int(r["size"]),
        "os": r["os"], "repo": ns["REPO"], "fpr": ns["SIGNING_KEY_FPR"],
    }


def load_node() -> dict:
    t = TS_RELEASE.read_text(encoding="utf-8")

    def s(key: str) -> "str | None":
        m = re.search(rf'\b{key}:\s*"([^"]+)"', t)
        return m.group(1) if m else None

    size_m = re.search(r"\bsize:\s*(\d+)", t)
    repo_m = re.search(r'REPO\s*=\s*"([^"]+)"', t)
    fpr_m = re.search(r'SIGNING_KEY_FPR\s*=\s*"([^"]+)"', t)
    return {
        "tag": s("tag"), "version": s("version"), "asset": s("asset"), "url": s("url"),
        "sha256": s("sha256"), "exeSha256": s("exeSha256"),
        "size": int(size_m.group(1)) if size_m else None,
        "os": s("os"),
        "repo": repo_m.group(1) if repo_m else None,
        "fpr": fpr_m.group(1) if fpr_m else None,
    }


def http(url: str, token: "str | None") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-ci", "Accept": "*/*"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8")


def wait_for_release(repo: str, tag: str, asset: str, token: "str | None", budget_s: int) -> dict:
    """Fetch the release JSON, retrying while it's missing or its assets aren't attached yet.

    A release inherently races the pin bump: the runbook pushes the pin-bump commit (which
    triggers this CI) and only *then* runs ``gh release create`` — so for a short window the
    pinned tag legitimately 404s. Poll with backoff instead of failing on the first miss; a
    genuinely-missing release still fails after the budget. On any normal push (the release
    already exists) the first attempt succeeds, so there is no added latency.
    Set RELEASE_PIN_WAIT_SECS=0 to require the release immediately (no polling)."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    deadline = time.monotonic() + max(budget_s, 0)
    delay, note = 5, ""
    while True:
        try:
            rel = json.loads(http(url, token))
            names = {a.get("name") for a in rel.get("assets", [])}
            if asset in names and "SHA256SUMS.txt" in names:
                return rel
            note = (f"release {tag} exists but expected assets aren't attached yet "
                    f"(have: {sorted(n for n in names if n)})")
        except Exception as exc:  # noqa: BLE001
            note = f"GitHub release {tag} not found / unreachable: {exc}"
        if time.monotonic() >= deadline:
            fail(note)
        print(f"::notice::{note} — retrying in {delay}s (waiting up to {budget_s}s for a fresh release to publish)")
        time.sleep(delay)
        delay = min(delay * 2, 30)


def main() -> "None":
    py, nd = load_python(), load_node()

    # 1) cross-SDK consistency
    for k in FIELDS:
        if str(py.get(k)) != str(nd.get(k)):
            fail(f"node vs python RELEASE.{k} differ: node={nd.get(k)!r} python={py.get(k)!r}")
    r = nd
    print(f"pinned release: {r['tag']} (Chromium {r['version']}), asset {r['asset']}")

    # 2) internal consistency
    if not re.fullmatch(r"[0-9a-f]{64}", r["sha256"] or ""):
        fail("sha256 is not 64 lowercase hex")
    if not re.fullmatch(r"[0-9a-f]{64}", r["exeSha256"] or ""):
        fail("exeSha256 is not 64 lowercase hex")
    if not re.fullmatch(r"[0-9A-F]{40}", r["fpr"] or ""):
        fail("SIGNING_KEY_FPR is not a 40-hex GPG fingerprint")
    if r["version"] not in r["asset"]:
        fail("asset name does not contain the Chromium version")
    expected_url = f"https://github.com/{r['repo']}/releases/download/{r['tag']}/{r['asset']}"
    if r["url"] != expected_url:
        fail(f"url != {expected_url}")

    # 3) release exists + asset present (polls briefly to absorb the push-before-release window)
    token = os.environ.get("GITHUB_TOKEN")
    budget = int(os.environ.get("RELEASE_PIN_WAIT_SECS", "180"))
    rel = wait_for_release(r["repo"], r["tag"], r["asset"], token, budget)
    assets = {a["name"]: a for a in rel.get("assets", [])}

    # 4) checksums match the published SHA256SUMS.txt
    sums = http(assets["SHA256SUMS.txt"]["browser_download_url"], token)
    by_name = {}
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2:
            by_name[parts[1].strip()] = parts[0].strip().lower()
    if by_name.get(r["asset"]) != r["sha256"]:
        fail(f"zip sha256 mismatch: pinned={r['sha256']} published={by_name.get(r['asset'])}")
    if by_name.get("chrome.exe") != r["exeSha256"]:
        fail(f"chrome.exe sha256 mismatch: pinned={r['exeSha256']} published={by_name.get('chrome.exe')}")

    print("OK: pinned release exists; zip + chrome.exe checksums match the published SHA256SUMS.txt")


if __name__ == "__main__":
    main()
