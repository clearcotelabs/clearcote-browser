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
    "tag": "v0.1.0-pre.21",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.21/clearcote-149.0.7827.114-windows-x64.zip"
    ),
    # SHA-256 of the archive — verified after download; a mismatch is a hard failure.
    "sha256": "79b03d2d875b374970b2d54eae54f77070eba06b6a446dc163420854ec068c4d",
    # SHA-256 of the inner browser binary — verified after extraction (defence in depth).
    "exe_sha256": "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a",
    "size": 242655762,
    "os": "win32",
    "archive": "zip",
    "binary": "chrome.exe",
    "asset_glob": "windows-x64",
}
_LINUX = {
    "tag": "v0.1.0-pre.21",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-linux-x64.tar.xz",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.21/clearcote-149.0.7827.114-linux-x64.tar.xz"
    ),
    "sha256": "5e7241a3e90033bc84f6079821829e99a6e6f0f6479eaa291d8b6590363aa292",
    "exe_sha256": "dd5aef845b47f63ebf84d769cc349dae69178639fe5c703fc52779c5a0606cce",
    "size": 146851212,
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


# ── Version catalog ──────────────────────────────────────────────────────────
# The catalog is the source of truth for "which browser majors exist and what tier each is". The SDK
# fetches it at runtime so a NEW release becomes switchable (launch(version="150")) without an SDK
# bump. Each build declares a `tier`: FREE builds are public on GitHub and carry their url+sha256; PRO
# builds (license-gated, not yet public) advertise existence ONLY — the SDK validates the request and
# routes the actual download through the authenticated /api/v1/download/pro route. When a PRO major is
# later promoted to public, flip its `tier` to "free" and add the GitHub url — no SDK change needed.
#
# platform keys are "windows"/"linux" (matching the /download/pro `platform` param).
CATALOG_URL = "https://www.clearcotelabs.com/api/v1/versions"

# Offline fallback snapshot — keep in sync with published releases. Lets the SDK still VALIDATE a
# request (and download the free pins) when the live catalog is unreachable. Only list builds that are
# actually DOWNLOADABLE: when a new build (e.g. the 150 PRO) goes live, add it to the live catalog
# (/api/v1/versions) — no SDK republish needed — and to this snapshot on the next SDK release.
CATALOG_FALLBACK = {
    "schema": 1,
    "builds": [
        {
            "major": 149, "version": "149.0.7827.114", "tier": "free", "tag": "v0.1.0-pre.21",
            "platforms": {
                "windows": {
                    "asset": _WINDOWS["asset"], "url": _WINDOWS["url"], "sha256": _WINDOWS["sha256"],
                    "exe_sha256": _WINDOWS["exe_sha256"], "size": _WINDOWS["size"],
                    "archive": "zip", "binary": "chrome.exe",
                },
                "linux": {
                    "asset": _LINUX["asset"], "url": _LINUX["url"], "sha256": _LINUX["sha256"],
                    "exe_sha256": _LINUX["exe_sha256"], "size": _LINUX["size"],
                    "archive": "tar.xz", "binary": "chrome",
                },
            },
        },
    ],
}
