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
    "tag": "v0.1.0-pre.16",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.16/clearcote-149.0.7827.114-windows-x64.zip"
    ),
    # SHA-256 of the archive — verified after download; a mismatch is a hard failure.
    "sha256": "4b24af67433f7de7e335b400f470cc99c920e9bc614f2b1e8ebb01f3c6e585fd",
    # SHA-256 of the inner browser binary — verified after extraction (defence in depth).
    "exe_sha256": "5743595256c89c6874804bf3315acce592fc7f1883760c8d380c010151a73b23",
    "size": 242642508,
    "os": "win32",
    "archive": "zip",
    "binary": "chrome.exe",
    "asset_glob": "windows-x64",
}
_LINUX = {
    "tag": "v0.1.0-pre.17",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-linux-x64.tar.xz",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.17/clearcote-149.0.7827.114-linux-x64.tar.xz"
    ),
    "sha256": "4beb6ef0df2ea9b35ed654a356094a27ed1ac0d34ea6cb284719a957da6f5981",
    "exe_sha256": "b4f60c1dc1858173a0b41624c5d2cf7340a915cc000bae0ea1465f118374b3e0",
    "size": 147074836,
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
