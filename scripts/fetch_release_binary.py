#!/usr/bin/env python3
r"""
Fetch + verify + extract the SDK-pinned clearcote chrome.exe (Windows).

Reads the ``RELEASE`` pin from ``sdk/python/clearcote/release.py`` (the single
source of truth, also checked by ``check_release_pin.py``), downloads the pinned
zip asset, verifies BOTH the zip sha256 and the extracted ``chrome.exe``
exe_sha256 against the pin, and unpacks the full archive (chrome.exe + its
bundled VC++ runtime DLLs + manifest) to ``--dest``.

Used by the stealth-coherence CI gate so it always tests the *exact published,
checksum-verified* binary — never an ad-hoc local build.

    py -3 scripts/fetch_release_binary.py --dest ./_release_bin
    # prints the verified chrome.exe path; sets GITHUB_OUTPUT `binary=` under Actions
"""
import argparse
import hashlib
import os
import runpy
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY_RELEASE = ROOT / "sdk" / "python" / "clearcote" / "release.py"


def _fail(msg):
    print(("::error::" if os.environ.get("GITHUB_ACTIONS") else "ERROR: ") + msg)
    sys.exit(1)


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="download + verify + extract the pinned chrome.exe")
    ap.add_argument("--dest", default=str(ROOT / "_release_bin"))
    a = ap.parse_args()

    rel = runpy.run_path(str(PY_RELEASE))["RELEASE"]
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    zip_path = dest / rel["asset"]

    print(f"downloading {rel['url']}", flush=True)
    req = urllib.request.Request(rel["url"], headers={"User-Agent": "clearcote-ci", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp, open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
    except Exception as exc:  # noqa: BLE001
        _fail(f"download failed: {exc}")

    got_zip = sha256_file(zip_path)
    if got_zip != rel["sha256"]:
        _fail(f"zip sha256 mismatch: got {got_zip} want {rel['sha256']}")

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)

    exe = dest / "chrome.exe"
    if not exe.exists():
        found = next(iter(dest.rglob("chrome.exe")), None)
        if found:
            exe = found
    if not exe.exists():
        _fail("chrome.exe not found in the extracted archive")

    got_exe = sha256_file(exe)
    if got_exe != rel["exe_sha256"]:
        _fail(f"chrome.exe sha256 mismatch: got {got_exe} want {rel['exe_sha256']}")

    print(f"verified: {rel['asset']} + chrome.exe checksums match the pin ({rel['tag']}, Chromium {rel['version']})")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"binary={exe}\n")
    print(exe)


if __name__ == "__main__":
    main()
