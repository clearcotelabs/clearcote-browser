"""Resolve the Clearcote browser binary: download a release, verify it, extract it to a
per-version cache, and return the chrome.exe path.

Two modes:
  * pinned (default)    — download the exact release baked into this SDK (release.py) and verify
                          its zip + chrome.exe against the SHA-256 hashes there. The hash IS the
                          trust anchor: you audit it once, in the package you installed.
  * auto_update (opt-in)— resolve the NEWEST GitHub release at runtime, verify the zip against
                          that release's published SHA256SUMS.txt, and — when a ``gpg`` binary is
                          available — verify SHA256SUMS.txt.asc against the pinned signing-key
                          fingerprint. Stay current without bumping the SDK. Falls back to the
                          pinned release if GitHub is unreachable.
A hash mismatch (either mode) is always a hard failure and the partial file is removed.

Uses only the standard library (urllib + hashlib + zipfile); GPG verification is best-effort and
only runs if a ``gpg`` executable is on PATH.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: S404
import sys
import tempfile
import urllib.request
import zipfile

from .release import RELEASE, REPO, SIGNING_KEY_FPR


def _log(quiet, msg):
    if not quiet:
        sys.stderr.write(f"[clearcote] {msg}\n")
        sys.stderr.flush()


def _auto_update_requested(opt):
    if opt is not None:
        return bool(opt)
    return os.environ.get("CLEARCOTE_AUTO_UPDATE") in ("1", "true")


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


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-sdk"})
    # timeout is the socket idle timeout (max wait per read), so a stalled connection fails fast
    # instead of hanging first-run launch forever. 30s for small API/text fetches.
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        return resp.read()


def _download(url, dest, expected_size, quiet):
    """Stream the download to dest, hashing as we go; return the hex digest."""
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "clearcote-sdk"})
    # 60s socket idle timeout: aborts a stalled stream (no bytes for 60s) without capping the
    # total time of the large (~242 MB) binary download, since the timeout resets each read.
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or expected_size or 0)
        seen = 0
        last = -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            seen += len(chunk)
            if not quiet and total:
                pct = int(seen * 100 / total)
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


def _parse_sums(text, zip_name):
    """Pull the zip + chrome.exe hashes out of a SHA256SUMS.txt body."""
    out = {}
    for raw in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+[*]?(.+)$", raw.strip())
        if not m:
            continue
        base = re.split(r"[\\/]", m.group(2))[-1]
        if base == zip_name:
            out["zip"] = m.group(1).lower()
        elif base == "chrome.exe":
            out["exe"] = m.group(1).lower()
    return out


def _resolve_latest(quiet):
    """Resolve the newest non-draft GitHub release with a windows-x64 zip + SHA256SUMS.txt.

    Returns a release dict (with ``unpinned=True`` and ``asc_url``/``key_url``) or None.
    """
    try:
        data = json.loads(_http_get(f"https://api.github.com/repos/{REPO}/releases?per_page=30"))
    except Exception as exc:  # noqa: BLE001
        _log(quiet, f"auto-update: couldn't reach GitHub ({exc}); using pinned {RELEASE['tag']}")
        return None
    releases = sorted(
        (r for r in data if r and not r.get("draft")),
        key=lambda r: r.get("published_at") or "",
        reverse=True,
    )
    for r in releases:
        assets = r.get("assets") or []
        zip_asset = next((a for a in assets if re.match(r"^clearcote-.*-windows-x64\.zip$", a["name"])), None)
        sums_asset = next((a for a in assets if a["name"] == "SHA256SUMS.txt"), None)
        if not zip_asset or not sums_asset:
            continue
        try:
            parsed = _parse_sums(_http_get(sums_asset["browser_download_url"]).decode("utf-8", "replace"), zip_asset["name"])
        except Exception:  # noqa: BLE001
            continue
        if not parsed.get("zip"):
            continue
        m = re.match(r"^clearcote-(.+)-windows-x64\.zip$", zip_asset["name"])
        asc = next((a for a in assets if a["name"] == "SHA256SUMS.txt.asc"), None)
        key = next((a for a in assets if a["name"] == "clearcote-signing-key.asc"), None)
        return {
            "tag": r["tag_name"],
            "version": m.group(1) if m else r["tag_name"],
            "asset": zip_asset["name"],
            "url": zip_asset["browser_download_url"],
            "sha256": parsed["zip"],
            "exe_sha256": parsed.get("exe", ""),
            "size": zip_asset.get("size") or 0,
            "os": "win32",
            "unpinned": True,
            "asc_url": asc["browser_download_url"] if asc else None,
            "key_url": key["browser_download_url"] if key else None,
        }
    return None


def _has_gpg():
    try:
        return subprocess.run(["gpg", "--version"], capture_output=True).returncode == 0  # noqa: S603,S607
    except Exception:  # noqa: BLE001
        return False


def _gpg_verify(rel, sums_body, quiet):
    """Best-effort: import the published key into a throwaway keyring, confirm its fingerprint
    matches the pinned one, then verify SHA256SUMS.txt.asc. Returns "ok" | "skipped" | "failed".
    """
    if not rel.get("asc_url") or not rel.get("key_url"):
        return "skipped"
    if not _has_gpg():
        _log(quiet, "auto-update: gpg not found — skipping signature check (zip is still SHA-256-verified)")
        return "skipped"
    home = tempfile.mkdtemp(prefix="ccgpg-")
    key_path = os.path.join(home, "key.asc")
    sums_path = os.path.join(home, "SHA256SUMS.txt")
    asc_path = os.path.join(home, "SHA256SUMS.txt.asc")

    def gpg(*args):
        return subprocess.run(  # noqa: S603,S607
            ["gpg", "--homedir", home, "--batch", *args], capture_output=True, text=True
        )

    try:
        with open(sums_path, "w", encoding="utf-8") as f:
            f.write(sums_body)
        with open(key_path, "wb") as f:
            f.write(_http_get(rel["key_url"]))
        with open(asc_path, "wb") as f:
            f.write(_http_get(rel["asc_url"]))
        if gpg("--import", key_path).returncode != 0:
            return "failed"
        shown = gpg("--with-colons", "--fingerprint")
        fprs = [ln.split(":")[9] for ln in shown.stdout.splitlines() if ln.startswith("fpr:")]
        if SIGNING_KEY_FPR not in fprs:
            _log(quiet, f"auto-update: signing key fingerprint mismatch (expected {SIGNING_KEY_FPR})")
            return "failed"
        return "ok" if gpg("--verify", asc_path, sums_path).returncode == 0 else "failed"
    except Exception:  # noqa: BLE001
        return "failed"
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _fetch_and_verify(rel, base, quiet):
    """Download + verify a resolved release into ``base``; return the extracted chrome.exe path."""
    browser_dir = os.path.join(base, "browser")
    os.makedirs(base, exist_ok=True)
    zip_path = os.path.join(base, rel["asset"])

    tail = ", latest" if rel.get("unpinned") else ""
    _log(quiet, f"fetching Clearcote {rel['version']} ({rel['tag']}{tail}, "
                f"~{(rel.get('size') or 0) // 10**6} MB)")
    got = _download(rel["url"], zip_path, rel.get("size"), quiet)

    _log(quiet, "verifying SHA-256")
    if got.lower() != rel["sha256"].lower():
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise RuntimeError(
            "Clearcote archive SHA-256 mismatch — refusing to use it.\n"
            f"  expected {rel['sha256']}\n  got      {got}"
        )

    # For an auto-resolved (un-pinned) release, also confirm authenticity via the signed checksum file.
    if rel.get("unpinned") and rel.get("asc_url"):
        try:
            sums_body = _http_get(
                f"https://github.com/{REPO}/releases/download/{rel['tag']}/SHA256SUMS.txt"
            ).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            sums_body = ""
        if sums_body:
            verdict = _gpg_verify(rel, sums_body, quiet)
            if verdict == "failed":
                try:
                    os.remove(zip_path)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Clearcote {rel['tag']}: GPG signature verification FAILED against the pinned "
                    f"key {SIGNING_KEY_FPR} — refusing to use it."
                )
            if verdict == "ok":
                _log(quiet, f"auto-update: GPG signature OK (key {SIGNING_KEY_FPR})")

    _log(quiet, "extracting")
    if os.path.isdir(browser_dir):
        shutil.rmtree(browser_dir, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(browser_dir)

    exe = _find(browser_dir, "chrome.exe")
    if not exe:
        raise RuntimeError("Clearcote archive verified but chrome.exe was not found inside it.")

    if rel.get("exe_sha256"):
        exe_hash = _sha256_file(exe)
        if exe_hash.lower() != rel["exe_sha256"].lower():
            raise RuntimeError(
                "Clearcote chrome.exe SHA-256 mismatch — refusing to use it.\n"
                f"  expected {rel['exe_sha256']}\n  got      {exe_hash}"
            )

    with open(os.path.join(base, ".verified"), "w", encoding="utf-8") as f:
        f.write(rel["sha256"] + "\n")
    try:
        os.remove(zip_path)  # reclaim ~250 MB; keep only the extracted tree
    except OSError:
        pass
    _log(quiet, f"ready: {exe}")
    return exe


def ensure_binary(cache_dir=None, quiet=False, auto_update=None):
    """Ensure the Clearcote binary is present and verified; return the chrome.exe path.

    Cached per release tag, so later calls are instant. Set ``auto_update=True`` (or the env var
    ``CLEARCOTE_AUTO_UPDATE=1``) to resolve and download the latest GitHub release instead of the
    version pinned into this SDK.
    """
    cache_root = cache_dir or _cache_root()

    rel = None
    if _auto_update_requested(auto_update):
        latest = _resolve_latest(quiet)
        if latest and latest["tag"] == RELEASE["tag"]:
            rel = dict(RELEASE, unpinned=False)  # newest IS pinned — use the audited baked-in hashes
        else:
            rel = latest or dict(RELEASE, unpinned=False)
    else:
        rel = dict(RELEASE, unpinned=False)

    base = os.path.join(cache_root, rel["tag"])
    if os.path.exists(os.path.join(base, ".verified")):
        cached = _find(os.path.join(base, "browser"), "chrome.exe")
        if cached:
            return cached
    return _fetch_and_verify(rel, base, quiet)
