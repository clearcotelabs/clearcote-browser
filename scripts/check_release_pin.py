#!/usr/bin/env python3
"""Version-drift guard for the SDK's pinned browser release.

Asserts that the RELEASE constant pinned in ALL THREE SDKs (sdk/node/src/release.ts,
sdk/python/clearcote/release.py and sdk/dotnet/src/Clearcote/Release.cs):
  1. is identical across the three SDKs (same platforms, same fields per platform),
  2. is internally consistent (url <-> repo <-> tag <-> asset <-> version, hex digests),
  3. actually exists as a published GitHub release with that asset,
  4. the pinned sha256 (zip) + exeSha256 (chrome.exe) match the release's published
     SHA256SUMS.txt, and
  5. where GitHub exposes a `digest` for the release asset ("sha256:<hex>", computed by
     GitHub at upload), it equals the pinned archive sha256. Assets without one (uploaded
     before GitHub computed digests) rely on check 4 alone.

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
CS_RELEASE = ROOT / "sdk" / "dotnet" / "src" / "Clearcote" / "Release.cs"

# The .NET SDK names platforms "windows"/"linux" (its Platforms keys and ReleaseInfo.Os); the other two
# use Node/Python platform ids. Normalised here so all three compare field by field.
DOTNET_OS = {"windows": "win32", "linux": "linux"}

FIELDS = ("tag", "version", "asset", "url", "sha256", "exeSha256", "size", "os", "binary", "repo", "fpr")


def fail(msg: str) -> "None":
    prefix = "::error::" if os.environ.get("GITHUB_ACTIONS") else "ERROR: "
    print(f"{prefix}{msg}")
    sys.exit(1)


def load_python() -> dict:
    """release.py is a pure data module (no imports) — exec it, read its per-OS PLATFORMS map.
    Returns {os_key: normalized-pin}."""
    ns = runpy.run_path(str(PY_RELEASE))
    repo, fpr = ns["REPO"], ns["SIGNING_KEY_FPR"]
    out = {}
    for oskey, r in ns["PLATFORMS"].items():
        out[oskey] = {
            "tag": r["tag"], "version": r["version"], "asset": r["asset"], "url": r["url"],
            "sha256": r["sha256"], "exeSha256": r["exe_sha256"], "size": int(r["size"]),
            "os": r["os"], "binary": r["binary"], "repo": repo, "fpr": fpr,
        }
    return out


def load_node() -> dict:
    """Parse the per-OS pin objects (WINDOWS + LINUX) out of release.ts. Returns {os_key: pin}."""
    t = TS_RELEASE.read_text(encoding="utf-8")

    def mod(name: str) -> "str | None":
        m = re.search(rf'\b{name}\s*=\s*"([^"]+)"', t)
        return m.group(1) if m else None

    def block(name: str) -> str:
        m = re.search(rf"const {name}\b[^{{]*\{{(.*?)\}};", t, re.DOTALL)
        return m.group(1) if m else ""

    repo, fpr = mod("REPO"), mod("SIGNING_KEY_FPR")
    out = {}
    for tsname, oskey in (("WINDOWS", "win32"), ("LINUX", "linux")):
        b = block(tsname)

        def s(key: str, blk: str = b) -> "str | None":
            m = re.search(rf'\b{key}:\s*"([^"]+)"', blk)
            return m.group(1) if m else None

        size_m = re.search(r"\bsize:\s*(\d+)", b)
        out[oskey] = {
            "tag": s("tag"), "version": s("version"), "asset": s("asset"), "url": s("url"),
            "sha256": s("sha256"), "exeSha256": s("exeSha256"),
            "size": int(size_m.group(1)) if size_m else None,
            "os": s("os"), "binary": s("binary"), "repo": repo, "fpr": fpr,
        }
    return out


def load_dotnet() -> dict:
    """Parse the per-OS pins out of Release.cs: the `Platforms` map ({["windows"] = Windows, ...}) names
    which `ReleaseInfo` initialisers are pinned. Returns {os_key: pin} keyed like the other SDKs.
    Every field must be a single string/number literal; anything else fails loudly rather than
    comparing a None."""
    t = CS_RELEASE.read_text(encoding="utf-8")
    name = CS_RELEASE.name

    def const(key: str) -> str:
        m = re.search(rf'\bconst\s+string\s+{key}\s*=\s*"([^"]+)"', t)
        if not m:
            fail(f"{name}: could not find `const string {key}`")
        return m.group(1)

    repo, fpr = const("Repo"), const("SigningKeyFpr")
    pm = re.search(r"\bPlatforms\s*=\s*new\s+Dictionary<[^>]*>\s*\{(.*?)\};", t, re.DOTALL)
    if not pm:
        fail(f"{name}: could not find the `Platforms` dictionary")
    platforms = re.findall(r'\[\s*"(\w+)"\s*\]\s*=\s*(\w+)', pm.group(1))
    if not platforms:
        fail(f"{name}: `Platforms` lists no platforms")

    out = {}
    for cs_os, ident in platforms:
        bm = re.search(rf"\bReleaseInfo\s+{ident}\s*=\s*new\(\)\s*\{{(.*?)\}};", t, re.DOTALL)
        if not bm:
            fail(f"{name}: Platforms[\"{cs_os}\"] = {ident}, but no `ReleaseInfo {ident} = new() {{ ... }};`")
        b = bm.group(1)

        def s(key: str, blk: str = b, ident: str = ident) -> str:
            m = re.search(rf'\b{key}\s*=\s*"([^"]*)"\s*(?:,|$)', blk, re.MULTILINE)
            if not m:
                fail(f"{name}: {ident}.{key} is missing or not a single string literal")
            return m.group(1)

        size_m = re.search(r"\bSize\s*=\s*(\d+)\s*(?:,|$)", b, re.MULTILINE)
        if not size_m:
            fail(f"{name}: {ident}.Size is missing or not a number literal")
        os_field = s("Os")
        if os_field != cs_os:
            fail(f"{name}: Platforms[\"{cs_os}\"] = {ident}, but {ident}.Os = \"{os_field}\"")
        if cs_os not in DOTNET_OS:
            fail(f"{name}: unknown platform \"{cs_os}\" (known: {sorted(DOTNET_OS)})")
        out[DOTNET_OS[cs_os]] = {
            "tag": s("Tag"), "version": s("Version"), "asset": s("Asset"), "url": s("Url"),
            "sha256": s("Sha256"), "exeSha256": s("ExeSha256"), "size": int(size_m.group(1)),
            "os": DOTNET_OS[os_field], "binary": s("Binary"), "repo": repo, "fpr": fpr,
        }
    return out


def check_asset_digest(oskey: str, pin: dict, asset: dict) -> None:
    """GitHub's own sha256 of the uploaded asset must equal the pinned archive hash (when it has one)."""
    digest = asset.get("digest")
    if not digest:
        print(f"[{oskey}] note: release asset {pin['asset']} has no digest field; relying on SHA256SUMS.txt")
        return
    algo, _, value = str(digest).partition(":")
    if algo.lower() != "sha256":
        print(f"[{oskey}] note: release asset digest is {algo}, not sha256; not compared")
        return
    if value.strip().lower() != pin["sha256"]:
        fail(f"[{oskey}] GitHub asset digest mismatch for {pin['asset']}: pinned sha256={pin['sha256']} "
             f"GitHub digest={digest}")
    print(f"[{oskey}] OK: GitHub asset digest matches the pinned sha256")


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
    sdks = {"node": load_node(), "python": load_python(), "dotnet": load_dotnet()}
    py = sdks["python"]

    if len({frozenset(pins) for pins in sdks.values()}) != 1:
        fail("platform sets differ between SDKs: " + " ".join(f"{k}={sorted(v)}" for k, v in sdks.items()))

    token = os.environ.get("GITHUB_TOKEN")
    budget = int(os.environ.get("RELEASE_PIN_WAIT_SECS", "180"))

    for oskey in sorted(py):
        # 1) cross-SDK consistency (per platform)
        for k in FIELDS:
            vals = {sdk: pins[oskey].get(k) for sdk, pins in sdks.items()}
            if len({str(v) for v in vals.values()}) != 1:
                fail(f"[{oskey}] RELEASE.{k} differs between SDKs: "
                     + " ".join(f"{sdk}={v!r}" for sdk, v in vals.items()))
        r = sdks["node"][oskey]
        print(f"[{oskey}] pinned: {r['tag']} (Chromium {r['version']}), asset {r['asset']}, binary {r['binary']}")

        # 2) internal consistency
        if not re.fullmatch(r"[0-9a-f]{64}", r["sha256"] or ""):
            fail(f"[{oskey}] sha256 is not 64 lowercase hex")
        if not re.fullmatch(r"[0-9a-f]{64}", r["exeSha256"] or ""):
            fail(f"[{oskey}] exeSha256 is not 64 lowercase hex")
        if not re.fullmatch(r"[0-9A-F]{40}", r["fpr"] or ""):
            fail(f"[{oskey}] SIGNING_KEY_FPR is not a 40-hex GPG fingerprint")
        if r["version"] not in r["asset"]:
            fail(f"[{oskey}] asset name does not contain the Chromium version")
        expected_url = f"https://github.com/{r['repo']}/releases/download/{r['tag']}/{r['asset']}"
        if r["url"] != expected_url:
            fail(f"[{oskey}] url != {expected_url}")

        # 3) release exists + asset present (polls briefly to absorb the push-before-release window)
        rel = wait_for_release(r["repo"], r["tag"], r["asset"], token, budget)
        assets = {a["name"]: a for a in rel.get("assets", [])}

        # 4) checksums match the published SHA256SUMS.txt (inner-binary name is per-OS)
        sums = http(assets["SHA256SUMS.txt"]["browser_download_url"], token)
        by_name = {}
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2:
                by_name[parts[1].strip()] = parts[0].strip().lower()
        if by_name.get(r["asset"]) != r["sha256"]:
            fail(f"[{oskey}] archive sha256 mismatch: pinned={r['sha256']} published={by_name.get(r['asset'])}")
        if by_name.get(r["binary"]) != r["exeSha256"]:
            fail(f"[{oskey}] {r['binary']} sha256 mismatch: pinned={r['exeSha256']} published={by_name.get(r['binary'])}")
        print(f"[{oskey}] OK: release exists; archive + {r['binary']} checksums match SHA256SUMS.txt")

        # 5) GitHub's own digest of the archive, where the API exposes one
        check_asset_digest(oskey, r, assets[r["asset"]])

    print("OK: all platform pins verified across all three SDKs")


if __name__ == "__main__":
    main()
