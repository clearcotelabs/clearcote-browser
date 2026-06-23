"""Pinned Clearcote release the SDK downloads and verifies.

Bumping to a new browser build = updating these values (and the version in pyproject.toml).
The sha256 is the single trust anchor for the auto-download: if you trust this package, the
hash check guarantees you run exactly the published, signed binary. Published checksums + GPG
signatures live on the release page.
"""

RELEASE = {
    "tag": "v0.1.0-pre.12",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.12/clearcote-149.0.7827.114-windows-x64.zip"
    ),
    # SHA-256 of the zip — verified after download; a mismatch is a hard failure.
    "sha256": "1c1d7e58f50463c09b3c1ad96ec79cc7777ef6482f00e2f3bc8dc6d03df47ced",
    # SHA-256 of chrome.exe inside the zip — verified after extraction (defense in depth).
    "exe_sha256": "5743595256c89c6874804bf3315acce592fc7f1883760c8d380c010151a73b23",
    "size": 242636042,
    "os": "win32",
}

# GitHub repo (owner/name) releases come from — used by the opt-in auto-update resolver.
REPO = "clearcotelabs/clearcote-browser"

# Clearcote's release-signing key fingerprint, pinned out-of-band. This NEVER changes between
# releases, so it is the durable trust anchor for auto_update: when a `gpg` binary is available,
# an auto-resolved (un-pinned) release's SHA256SUMS.txt.asc is verified against THIS fingerprint
# before the binary is trusted. (Pinned mode trusts the baked-in sha256 instead.)
SIGNING_KEY_FPR = "CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F"
