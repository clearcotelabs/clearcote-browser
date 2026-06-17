"""Pinned Clearcote release the SDK downloads and verifies.

Bumping to a new browser build = updating these values (and the version in pyproject.toml).
The sha256 is the single trust anchor for the auto-download: if you trust this package, the
hash check guarantees you run exactly the published, signed binary. Published checksums + GPG
signatures live on the release page.
"""

RELEASE = {
    "tag": "v0.1.0-pre.5",
    "version": "149.0.7827.114",
    "asset": "clearcote-149.0.7827.114-windows-x64.zip",
    "url": (
        "https://github.com/clearcotelabs/clearcote-browser/releases/download/"
        "v0.1.0-pre.5/clearcote-149.0.7827.114-windows-x64.zip"
    ),
    # SHA-256 of the zip — verified after download; a mismatch is a hard failure.
    "sha256": "21b6c520f2866ead759c19aa02ac30570aee3ebb41f5196518c27604a693c7e6",
    # SHA-256 of chrome.exe inside the zip — verified after extraction (defense in depth).
    "exe_sha256": "5743595256c89c6874804bf3315acce592fc7f1883760c8d380c010151a73b23",
    "size": 242548712,
    "os": "win32",
}
