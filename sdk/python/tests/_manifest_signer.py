"""TEST-ONLY signer for the Pro release manifest (docs/PRO-RELEASE-MANIFEST.md).

It signs with the test key of sdk/testdata/pro_manifest_vectors.json. That key is pinned nowhere and signs
nothing real; a test pins it for itself with the ``pro_test_key`` fixture (conftest.py). The SDK itself only
verifies and holds no signing code: this file stays in the tests folder.
"""
import base64
import hashlib
import json
import os

from clearcote import _ed25519 as ed

VECTORS = os.path.join(os.path.dirname(__file__), "..", "..", "testdata", "pro_manifest_vectors.json")

with open(VECTORS, encoding="utf-8") as _f:
    _V = json.load(_f)

KEY_ID = _V["key_id"]
PUBLIC_KEY_HEX = _V["public_key_hex"]
SEED = bytes.fromhex(_V["private_seed_hex"])
TEST_KEYS = {KEY_ID: PUBLIC_KEY_HEX}  # what a test pins in clearcote.release.PRO_RELEASE_KEYS

DEFAULT_LAUNCHER = {"windows": "chrome.exe", "macos": "Chromium"}


def _encode(point):
    """A point as its 32 bytes: y, with the lowest bit of x in the top bit."""
    z = pow(point[2], ed._P - 2, ed._P)
    x, y = point[0] * z % ed._P, point[1] * z % ed._P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _secret(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(seed=SEED):
    """The raw 32-byte public key of a seed."""
    return _encode(ed._mul(_secret(seed)[0], ed._BASE))


def sign(message, seed=SEED):
    """The 64-byte Ed25519 signature of ``message`` (RFC 8032, section 5.1.6)."""
    a, prefix = _secret(seed)
    public = _encode(ed._mul(a, ed._BASE))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % ed._L
    big_r = _encode(ed._mul(r, ed._BASE))
    k = int.from_bytes(hashlib.sha512(big_r + public + message).digest(), "little") % ed._L
    return big_r + ((r + k * a) % ed._L).to_bytes(32, "little")


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign_manifest(payload, seed=SEED):
    """``base64url(payload) "." base64url(signature)``, the signature over the base64url text."""
    body = b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return body + "." + b64url(sign(body.encode("ascii"), seed))


def manifest_payload(meta, platform, over=None):
    """The correct manifest payload for a download answer; ``over`` (a dict) replaces fields before signing."""
    launcher = meta.get("binary") or DEFAULT_LAUNCHER.get(platform, "chrome")
    binaries = {launcher: meta["exe_sha256"]}
    if platform == "windows":
        binaries["chrome.dll"] = meta["chrome_dll_sha256"]
    payload = {"v": 1, "product": "clearcote-pro", "version": meta["version"], "revision": meta["revision"],
               "platform": platform,
               "archive": {"name": meta["asset"], "sha256": meta["sha256"], "size": meta["size"]},
               "binaries": binaries, "key": KEY_ID, "issued": "2026-11-26"}
    payload.update(over or {})
    return payload


def signed_answer(meta, platform, over=None):
    """``meta`` with the release manifest for exactly what it says, signed with the test key."""
    return {**meta, "manifest": sign_manifest(manifest_payload(meta, platform, over))}
