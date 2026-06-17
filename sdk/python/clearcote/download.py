"""Resolve the Clearcote browser binary: download the pinned release, verify its SHA-256
against the value baked into the SDK, extract it to a per-version cache, and return the path
to chrome.exe. The hash check is mandatory — a mismatch raises and the partial file is removed.

Uses only the standard library (urllib + hashlib + zipfile) — no extra dependencies.
"""

import hashlib
import os
import shutil
import sys
import urllib.request
import zipfile

from .release import RELEASE


def _log(quiet, msg):
    if not quiet:
        sys.stderr.write(f"[clearcote] {msg}\n")
        sys.stderr.flush()


def _cache_root():
    env = os.environ.get("CLEARCOTE_CACHE")
    if env:
        return env
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local")
        return os.path.join(base, "clearcote", "Cache")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~/Library/Caches"), "clearcote")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "clearcote")


def _find(dirpath, name):
    name = name.lower()
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            if f.lower() == name:
                return os.path.join(root, f)
    return None


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url, dest, quiet):
    """Stream the download to dest, hashing as we go; return the hex digest."""
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-sdk"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or RELEASE["size"])
        seen = 0
        last = -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            seen += len(chunk)
            if not quiet:
                pct = int(seen * 100 / total) if total else 0
                if pct != last and pct % 5 == 0:
                    last = pct
                    sys.stderr.write(
                        f"\r[clearcote] downloading {pct}% "
                        f"({seen // 10**6}/{total // 10**6} MB)"
                    )
                    sys.stderr.flush()
    if not quiet:
        sys.stderr.write("\n")
        sys.stderr.flush()
    return h.hexdigest()


def ensure_binary(cache_dir=None, quiet=False):
    """Ensure the Clearcote binary is present and verified; return the chrome.exe path.
    Cached per release tag, so later calls are instant."""
    base = os.path.join(cache_dir or _cache_root(), RELEASE["tag"])
    browser_dir = os.path.join(base, "browser")
    marker = os.path.join(base, ".verified")

    if os.path.exists(marker):
        cached = _find(browser_dir, "chrome.exe")
        if cached:
            return cached

    os.makedirs(base, exist_ok=True)
    zip_path = os.path.join(base, RELEASE["asset"])

    _log(quiet, f"fetching Clearcote {RELEASE['version']} ({RELEASE['tag']}, "
                f"~{RELEASE['size'] // 10**6} MB)")
    got = _download(RELEASE["url"], zip_path, quiet)

    _log(quiet, "verifying SHA-256")
    if got.lower() != RELEASE["sha256"].lower():
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise RuntimeError(
            "Clearcote archive SHA-256 mismatch — refusing to use it.\n"
            f"  expected {RELEASE['sha256']}\n  got      {got}"
        )

    _log(quiet, "extracting")
    if os.path.isdir(browser_dir):
        shutil.rmtree(browser_dir, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(browser_dir)

    exe = _find(browser_dir, "chrome.exe")
    if not exe:
        raise RuntimeError("Clearcote archive verified but chrome.exe was not found inside it.")

    exe_hash = _sha256_file(exe)
    if exe_hash.lower() != RELEASE["exe_sha256"].lower():
        raise RuntimeError(
            "Clearcote chrome.exe SHA-256 mismatch — refusing to use it.\n"
            f"  expected {RELEASE['exe_sha256']}\n  got      {exe_hash}"
        )

    with open(marker, "w", encoding="utf-8") as f:
        f.write(RELEASE["sha256"] + "\n")
    try:
        os.remove(zip_path)  # reclaim ~250 MB; keep only the extracted tree
    except OSError:
        pass
    _log(quiet, f"ready: {exe}")
    return exe
