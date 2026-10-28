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
    "tag": "v0.1.0-pre.25",
    "version": "151.0.7922.108",
    "asset": "clearcote-151.0.7922.108-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.25/clearcote-151.0.7922.108-windows-x64.zip"
    ),
    # SHA-256 of the archive — verified after download; a mismatch is a hard failure.
    "sha256": "a3e3455c4c2da6187c9224563d99651a0cfd1a2bac5f70c2f9a19366c4c541cd",
    # SHA-256 of the inner browser binary — verified after extraction (defence in depth).
    "exe_sha256": "e9c88e826534200e62b6544144ff3801c63065edb8940ff45e533918322e8dc0",
    "size": 204317302,
    "os": "win32",
    "archive": "zip",
    "binary": "chrome.exe",
    "asset_glob": "windows-x64",
}
_LINUX = {
    "tag": "v0.1.0-pre.25",
    "version": "151.0.7922.108",
    "asset": "clearcote-151.0.7922.108-linux-x64.tar.xz",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.25/clearcote-151.0.7922.108-linux-x64.tar.xz"
    ),
    "sha256": "6c06f95783410d09799828b93f7bb26ac8174354b87f580827c8bcf1f2226b7e",
    "exe_sha256": "6261bae468ced0b7b1fe0f71d73344cb6feca2d9abf4a1da285ba4f572c546cc",
    "size": 122845968,
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


# sys.platform -> the PRO catalog / /download/pro `platform` key. Wider than PLATFORMS: macOS has a
# native PRO build (Apple Silicon, a Chromium.app bundle) but no free pinned release, so it is here and
# not there. The PRO build is resolved by the site at run time, so there is nothing to pin for it.
PRO_PLATFORMS = {"win32": "windows", "linux": "linux", "darwin": "macos"}

# CPU architectures the native macOS PRO build runs on. It is arm64 only; an Intel Mac (or a Python
# running under Rosetta, which reports x86_64) uses the Docker image instead.
MACOS_ARCHES = ("arm64", "aarch64")


def host_machine():
    """This machine's CPU architecture, lower-cased (``platform.machine()``)."""
    import platform

    return (platform.machine() or "").strip().lower()


def pro_platform_key(plat=None, machine=None):
    """The PRO download platform key for an OS ("windows" / "linux" / "macos"), or None if there is
    no PRO build for it. A macOS host that is not Apple Silicon raises: the build exists, just not for
    this CPU, and "unsupported OS" would send the caller looking in the wrong place."""
    plat = plat or sys.platform
    key = next((v for k, v in PRO_PLATFORMS.items() if plat.startswith(k)), None)
    if key == "macos" and (machine if machine is not None else host_machine()) not in MACOS_ARCHES:
        raise RuntimeError(
            "The native macOS Clearcote PRO build is for Apple Silicon (arm64) only; this Mac reports "
            f"{(machine if machine is not None else host_machine()) or 'an unknown CPU'}. Use the Docker "
            "image instead (launch(docker=True)), or run on an Apple Silicon Mac.")
    return key


def native_macos_supported(plat=None, machine=None):
    """True on an Apple Silicon Mac, where the native PRO build runs."""
    plat = plat or sys.platform
    return plat == "darwin" and (machine if machine is not None else host_machine()) in MACOS_ARCHES


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
# platform keys are "windows"/"linux"/"macos" (matching the /download/pro `platform` param; macOS is
# PRO-only, Apple Silicon).
CATALOG_URL = "https://www.clearcotelabs.com/api/v1/versions"

# Offline fallback snapshot — keep in sync with published releases. Lets the SDK still VALIDATE a
# request (and download the free pins) when the live catalog is unreachable. Only list builds that are
# actually DOWNLOADABLE: when a new build (e.g. the 150 PRO) goes live, add it to the live catalog
# (/api/v1/versions) — no SDK republish needed — and to this snapshot on the next SDK release.
CATALOG_FALLBACK = {
    "schema": 1,
    "builds": [
        {
            "major": 149, "version": "149.0.7827.114", "tier": "free", "tag": "v0.1.0-pre.22",
            "platforms": {
                "windows": {
                    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
                    "url": "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-windows-x64.zip",
                    "sha256": "f40c83049aae13d8c0322e9822eeb2d7236fddd5e7915304eb6f0f5f1f79fcb6",
                    "exe_sha256": "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a", "size": 242661515,
                    "archive": "zip", "binary": "chrome.exe",
                },
                "linux": {
                    "asset": "clearcote-149.0.7827.114-linux-x64.tar.xz",
                    "url": "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-linux-x64.tar.xz",
                    "sha256": "6625971f51318acb6adc301fcc18dcf790c3de0ba2b313be7182eff180a8ec53",
                    "exe_sha256": "aea54f3c1b5bfc43b4c42aff00d41df49393575950df7c644cc580f72462db4a", "size": 146834232,
                    "archive": "tar.xz", "binary": "chrome",
                },
            },
        },
        {
            "major": 150, "version": "150.0.7871.114", "tier": "free", "tag": "v0.1.0-pre.23",
            "platforms": {
                "windows": {
                    "asset": "clearcote-150.0.7871.114-windows-x64.zip",
                    "url": "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-windows-x64.zip",
                    "sha256": "93fc03c45b931d8d82f714814318892929f44dd671b0993788332071d53f3135",
                    "exe_sha256": "f49b0d6bc5a08857e34f951ddc456abc643283ae45ff330ee7c2c39cd75b4869", "size": 245936073,
                    "archive": "zip", "binary": "chrome.exe",
                },
                "linux": {
                    "asset": "clearcote-150.0.7871.114-linux-x64.tar.xz",
                    "url": "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-linux-x64.tar.xz",
                    "sha256": "92ebd3de6fbf2c1349d3eb3c210952fcdb8dc9ed35655a35297cd68431fc8ef2",
                    "exe_sha256": "cc192460484b7dcb1de8df3b3b7bea6f78962d560b6347012d9dbedd3f66720d", "size": 149463344,
                    "archive": "tar.xz", "binary": "chrome",
                },
            },
        },
        {
            "major": 151, "version": "151.0.7922.108", "tier": "free", "tag": "v0.1.0-pre.25",
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
