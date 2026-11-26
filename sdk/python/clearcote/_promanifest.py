"""The signed Pro release manifest check (docs/PRO-RELEASE-MANIFEST.md).

A Pro download answer carries ``manifest = base64url(payload) "." base64url(signature)``: an Ed25519
signature, by a release key pinned in release.py, over the build's version, revision, platform, archive
hash and size, and the hashes of the binaries inside. The answer (fresh from the site, or kept under
``~/.clearcote``) is used only when the manifest verifies and says the same as the answer does.

There is no switch for this check: no environment variable, option or file adds a key or turns it off.
"""

import base64
import json
import re

from . import _ed25519, release

PRODUCT = "clearcote-pro"

_B64URL = re.compile(r"[A-Za-z0-9_-]*")
# The launcher's name when the answer gives none: the same default the download path uses.
_DEFAULT_BINARY = {"windows": "chrome.exe", "macos": "Chromium"}


class ProManifestError(RuntimeError):
    """The signed Pro release manifest is absent or does not match. ``reason`` is the code from the spec:
    missing, malformed, unknown-key, signature, product, platform, version, revision, archive, binary."""

    def __init__(self, reason, detail):
        super().__init__(
            f"Clearcote Pro release manifest check failed ({reason}): {detail}. Refusing to use this build.")
        self.reason = reason
        self.detail = detail

    def __reduce__(self):  # so it survives pickling (a process pool, a test runner)
        return (type(self), (self.reason, self.detail))


def _b64url(part):
    """The bytes of an unpadded base64url string, or None when it is not one."""
    if not _B64URL.fullmatch(part) or len(part) % 4 == 1:
        return None
    try:
        return base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None


def _shown(value):
    """An untrusted value, shortened for an error message."""
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _text(value):
    return isinstance(value, str) and value != ""


def _same_hash(a, b):
    return _text(a) and _text(b) and a.lower() == b.lower()


def _same_number(a, b):
    return (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and not isinstance(a, bool) and not isinstance(b, bool) and a == b)


def verify_pro_manifest(meta, platform, want_revision=None, want_version=None):
    """Check a Pro download answer against its signed release manifest and return the manifest's payload.

    ``meta`` is the answer of ``GET /api/v1/download/pro``, ``platform`` the platform asked for ("windows",
    "linux" or "macos"), ``want_revision`` / ``want_version`` a revision ("r68") or version the caller
    pinned. Raises ProManifestError, with the reason code of the first check that fails. The checks run in
    the order of the spec; nothing from the payload except ``key`` is used before the signature verified."""
    if not isinstance(meta, dict):
        raise ProManifestError("missing", "there is no download answer to check")
    token = meta.get("manifest")
    # 1. present and not empty
    if token is None or token == "":
        raise ProManifestError("missing", "the download answer carries no signed release manifest")

    # 2. two base64url parts, the first a JSON object
    if not isinstance(token, str):
        raise ProManifestError("malformed", "the manifest is not a string")
    parts = token.split(".")
    if len(parts) != 2:
        raise ProManifestError("malformed", f"the manifest has {len(parts)} parts, not the two it must have")
    body, signature = _b64url(parts[0]), _b64url(parts[1])
    if body is None or signature is None:
        raise ProManifestError("malformed", "the manifest is not base64url text")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError):  # not UTF-8, not JSON, or nested without end
        payload = None
    if not isinstance(payload, dict):
        raise ProManifestError("malformed", "the manifest's payload is not a JSON object")

    # 3. signed by a pinned key
    key_id = payload.get("key")
    pinned = release.PRO_RELEASE_KEYS
    if not isinstance(key_id, str) or key_id not in pinned:
        raise ProManifestError(
            "unknown-key", f"the manifest names signing key {_shown(key_id)}, which is not a release key pinned "
                           "in this SDK version (a newer clearcote package may have it)")

    # 4. the signature, over the base64url text of the payload
    try:
        public_key = bytes.fromhex(pinned[key_id])
    except (TypeError, ValueError):
        public_key = b""
    if len(signature) != 64 or not _ed25519.verify(public_key, parts[0].encode("ascii"), signature):
        raise ProManifestError("signature", f"the signature does not verify under release key {_shown(key_id)}")

    # From here on the payload is what the release key signed.
    # 5. format version
    version_field = payload.get("v")
    if isinstance(version_field, bool) or version_field != 1:
        raise ProManifestError("malformed", f"manifest format {_shown(version_field)} is not one this SDK reads")
    # 6. product
    if payload.get("product") != PRODUCT:
        raise ProManifestError("product", f"the manifest is for product {_shown(payload.get('product'))}, "
                                          f"not {PRODUCT}")
    # 7. platform
    if not _text(platform) or payload.get("platform") != platform:
        raise ProManifestError("platform", f"the manifest is for platform {_shown(payload.get('platform'))}, "
                                           f"and {_shown(platform)} was asked for")
    # 8. version
    signed_version = payload.get("version")
    if not _text(signed_version) or signed_version != meta.get("version"):
        raise ProManifestError("version", f"the manifest is for version {_shown(signed_version)}, and the "
                                          f"download answer says {_shown(meta.get('version'))}")
    if want_version is not None and signed_version != want_version:
        raise ProManifestError("version", f"the manifest is for version {_shown(signed_version)}, and version "
                                          f"{_shown(want_version)} was asked for")
    # 9. revision
    signed_revision = payload.get("revision")
    if not _text(signed_revision) or signed_revision != meta.get("revision"):
        raise ProManifestError("revision", f"the manifest is for revision {_shown(signed_revision)}, and the "
                                           f"download answer says {_shown(meta.get('revision'))}")
    if want_revision is not None and signed_revision != want_revision:
        raise ProManifestError("revision", f"the manifest is for revision {_shown(signed_revision)}, and "
                                           f"revision {_shown(want_revision)} was asked for")
    # 10. the archive
    archive = payload.get("archive")
    archive = archive if isinstance(archive, dict) else {}
    if not _same_hash(archive.get("sha256"), meta.get("sha256")):
        raise ProManifestError("archive", "the archive SHA-256 in the download answer is not the one in the "
                                          "manifest")
    if not _same_number(archive.get("size"), meta.get("size")):
        raise ProManifestError("archive", f"the archive size in the download answer ({_shown(meta.get('size'))}) "
                                          f"is not the one in the manifest ({_shown(archive.get('size'))})")
    # 11. the binaries inside it
    binaries = payload.get("binaries")
    binaries = binaries if isinstance(binaries, dict) else {}
    launcher = meta.get("binary") or _DEFAULT_BINARY.get(platform, "chrome")
    if not isinstance(launcher, str) or not _same_hash(binaries.get(launcher), meta.get("exe_sha256")):
        raise ProManifestError("binary", f"the SHA-256 of {_shown(launcher)} in the download answer is not the "
                                         "one in the manifest")
    if platform == "windows" and not _same_hash(binaries.get("chrome.dll"), meta.get("chrome_dll_sha256")):
        raise ProManifestError("binary", "the SHA-256 of chrome.dll in the download answer is not the one in "
                                         "the manifest")
    return payload
