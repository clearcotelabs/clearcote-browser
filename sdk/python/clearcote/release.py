"""Pinned Clearcote releases the SDK downloads and verifies — one pin per platform.

Bumping to a new browser build = updating the platform entry here (and the version in
pyproject.toml). The sha256 is the single trust anchor for the auto-download: if you trust this
package, the hash check guarantees you run exactly the published, signed binary. Published
checksums + GPG signatures live on each release page.
"""

import sys

# Per-platform pin. Each entry is a complete, self-contained pinned release for that OS: the exact
# signed asset + its SHA-256 (the trust anchor) + the inner-binary hash (defence in depth) + how to
# unpack it. Windows and Linux ship from their own release tags.
_WINDOWS = {
    "tag": "v0.1.0-pre.19",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.19/clearcote-149.0.7827.114-windows-x64.zip"
    ),
    # SHA-256 of the archive — verified after download; a mismatch is a hard failure.
    "sha256": "da47f325053a98130baf6f4907e13ba5135d37645fb5c150e59c8081e7df48b3",
    # SHA-256 of the inner browser binary — verified after extraction (defence in depth).
    "exe_sha256": "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a",
    "size": 242656951,
    "os": "win32",
    "archive": "zip",
    "binary": "chrome.exe",
    "asset_glob": "windows-x64",
}
_LINUX = {
    "tag": "v0.1.0-pre.19",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-linux-x64.tar.xz",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.19/clearcote-149.0.7827.114-linux-x64.tar.xz"
    ),
    "sha256": "1be5a9f83f8f8217d97caf52553b5fe8e24a3360dfc83c471ba91d2d95a97ac1",
    "exe_sha256": "7c5ea6ce563bd6c12642f12b1c85d308c09096814e9d7fcd59dd360fdfe6bb63",
    "size": 146861776,
    "os": "linux",
    "archive": "tar.xz",
    "binary": "chrome",
    "asset_glob": "linux-x64",
}

# sys.platform -> pinned release. Add an entry to support another OS.
PLATFORMS = {"win32": _WINDOWS, "linux": _LINUX}


def platform_release(plat=None):
    """The pinned release for the given platform (default: this OS), or None if unsupported."""
    return PLATFORMS.get(plat or sys.platform)


# The pin for the CURRENT platform. Falls back to the Windows entry on an unsupported OS so the
# existing error messaging still has a version to quote. Most code uses this; the download/guard
# paths branch on platform_release() to reject unsupported OSes.
RELEASE = platform_release() or _WINDOWS

# GitHub repo (owner/name) releases come from — used by the opt-in auto-update resolver.
REPO = "clearcotelabs/clearcote-browser"

# Clearcote's release-signing key fingerprint, pinned out-of-band. This NEVER changes between
# releases, so it is the durable trust anchor for auto_update: when a `gpg` binary is available,
# an auto-resolved (un-pinned) release's SHA256SUMS.txt.asc is verified against THIS fingerprint
# before the binary is trusted. (Pinned mode trusts the baked-in sha256 instead.)
SIGNING_KEY_FPR = "CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F"
