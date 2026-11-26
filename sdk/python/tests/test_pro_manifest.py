"""The signed Pro release manifest (docs/PRO-RELEASE-MANIFEST.md).

Before it, the expected hashes of a Pro download came in the same answer as the download link, and were kept
in a file the user can write (~/.clearcote/pro-meta-*.json): whoever could change either could change the
build and its hashes together. Now an answer is used only when its manifest verifies under a release key
pinned in release.py and says the same as the answer.

Three parts: the Ed25519 verifier, the shared vectors (sdk/testdata/pro_manifest_vectors.json, run by all
three SDKs), and the download path through the local fake of the route (HOME and the cache are temp dirs).
The fake signs with the TEST key, which a test pins for itself with the ``pro_test_key`` fixture.
"""
import hashlib
import importlib
import json
import os
import sys

import pytest

import clearcote
from clearcote import _ed25519, release
from clearcote._promanifest import ProManifestError, verify_pro_manifest

import _manifest_signer as signer
from _fake_build import BINARY, PLATFORM, TAG, FakeBuildServer

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

with open(signer.VECTORS, encoding="utf-8") as _f:
    VECTORS = json.load(_f)

pro_route = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")
REASONS = {"missing", "malformed", "unknown-key", "signature", "product", "platform", "version", "revision",
           "archive", "binary"}


# ── the Ed25519 verifier ─────────────────────────────────────────────────────────────────────────────

def _rfc(v):
    return tuple(bytes.fromhex(v[k]) for k in ("public_key_hex", "message_hex", "signature_hex"))


def _flip(raw, i):
    return raw[:i] + bytes([raw[i] ^ 1]) + raw[i + 1:]


@pytest.mark.parametrize("vector", VECTORS["rfc8032"], ids=["test1", "test2", "test3"])
def test_rfc8032_signatures_verify_and_fail_when_one_byte_changes(vector):
    key, message, signature = _rfc(vector)
    assert _ed25519.verify(key, message, signature) is True
    for i in range(64):
        assert _ed25519.verify(key, message, _flip(signature, i)) is False, f"signature byte {i}"
    for i in range(32):
        assert _ed25519.verify(_flip(key, i), message, signature) is False, f"key byte {i}"
    for i in range(len(message)):
        assert _ed25519.verify(key, _flip(message, i), signature) is False, f"message byte {i}"
    assert _ed25519.verify(key, message + b"\x00", signature) is False


def test_wrong_lengths_and_types_are_refused():
    key, message, signature = _rfc(VECTORS["rfc8032"][1])
    assert _ed25519.verify(key[:31], message, signature) is False
    assert _ed25519.verify(key + b"\x00", message, signature) is False
    assert _ed25519.verify(key, message, signature[:63]) is False
    assert _ed25519.verify(key, message, signature + b"\x00") is False
    assert _ed25519.verify(b"", message, b"") is False
    assert _ed25519.verify(key.hex(), message, signature) is False  # text, not bytes
    assert _ed25519.verify(key, message, None) is False
    assert _ed25519.verify(bytearray(key), bytearray(message), bytearray(signature)) is True


def test_a_non_canonical_s_is_refused():
    """S + L gives the same point as S, so it would verify: RFC 8032 refuses any S that is not below L."""
    key, message, signature = _rfc(VECTORS["rfc8032"][2])
    s = int.from_bytes(signature[32:], "little")
    assert s < _ed25519._L
    for bigger in (s + _ed25519._L, s + 2 * _ed25519._L):
        forged = signature[:32] + bigger.to_bytes(32, "little")
        # The curve equation holds for it: only the range check stands in the way.
        r, a = _ed25519._decode(signature[:32]), _ed25519._decode(key)
        k = int.from_bytes(hashlib.sha512(signature[:32] + key + message).digest(), "little") % _ed25519._L
        assert _ed25519._same(_ed25519._mul(bigger, _ed25519._BASE), _ed25519._add(r, _ed25519._mul(k, a)))
        assert _ed25519.verify(key, message, forged) is False


NOT_POINTS = {
    "y = 2 has no x on the curve": (2).to_bytes(32, "little"),
    "y = p, a second spelling of y = 0": _ed25519._P.to_bytes(32, "little"),
    "y = p + 1, a second spelling of y = 1": (_ed25519._P + 1).to_bytes(32, "little"),
    "x = 0 with the sign bit set": (1 | 1 << 255).to_bytes(32, "little"),
}


@pytest.mark.parametrize("raw", list(NOT_POINTS.values()), ids=list(NOT_POINTS))
def test_a_public_key_or_r_that_is_not_a_curve_point_is_refused(raw):
    key, message, signature = _rfc(VECTORS["rfc8032"][0])
    assert _ed25519._decode(raw) is None
    assert _ed25519.verify(raw, message, signature) is False  # as the public key
    assert _ed25519.verify(key, message, raw + signature[32:]) is False  # as R
    assert _ed25519.verify(raw, message, raw + bytes(32)) is False  # as both, with S = 0


def test_the_test_signer_agrees_with_the_vectors():
    """The signer is test-only code; Ed25519 is deterministic, so it must reproduce what Node signed."""
    assert signer.public_key().hex() == VECTORS["public_key_hex"]
    token = next(c for c in VECTORS["cases"] if c["name"] == "valid windows")["meta"]["manifest"]
    body, signature = token.split(".")
    assert signer.b64url(signer.sign(body.encode("ascii"))) == signature


# ── the shared vectors ───────────────────────────────────────────────────────────────────────────────

def test_the_vectors_cover_every_reason_code():
    assert {c["expect"] for c in VECTORS["cases"]} == REASONS | {"ok"}


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[c["name"] for c in VECTORS["cases"]])
def test_vector(case, pro_test_key):
    args = (case["meta"], case["platform"], case["want_revision"], case["want_version"])
    if case["expect"] == "ok":
        payload = verify_pro_manifest(*args)
        assert payload["product"] == "clearcote-pro" and payload["revision"] == case["meta"]["revision"]
        return
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(*args)
    assert e.value.reason == case["expect"]
    assert isinstance(e.value, RuntimeError)
    text = str(e.value)
    assert text.startswith(f"Clearcote Pro release manifest check failed ({case['expect']}): ")
    assert text.endswith(". Refusing to use this build.")


def _case(name):
    return next(c for c in VECTORS["cases"] if c["name"] == name)


def test_the_error_carries_its_reason_and_survives_pickling():
    import pickle

    err = ProManifestError("archive", "the archive is not the signed one")
    assert str(err) == ("Clearcote Pro release manifest check failed (archive): the archive is not the signed "
                        "one. Refusing to use this build.")
    back = pickle.loads(pickle.dumps(err))
    assert isinstance(back, ProManifestError) and back.reason == "archive" and str(back) == str(err)


def test_no_key_is_pinned_until_one_is_listed_in_release_py():
    """The shipped map: the test key is never in it, and what is in it is a usable key."""
    assert signer.KEY_ID not in release.PRO_RELEASE_KEYS
    assert signer.PUBLIC_KEY_HEX not in release.PRO_RELEASE_KEYS.values()
    for key_id, key_hex in release.PRO_RELEASE_KEYS.items():
        assert isinstance(key_id, str) and key_id
        assert len(key_hex) == 64 and _ed25519._decode(bytes.fromhex(key_hex)) is not None


def test_without_a_pinned_key_a_valid_manifest_is_refused(monkeypatch):
    case = _case("valid windows")
    monkeypatch.setattr(release, "PRO_RELEASE_KEYS", {})
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(case["meta"], "windows")
    assert e.value.reason == "unknown-key"


def test_nothing_in_the_environment_adds_a_key_or_skips_the_check(monkeypatch):
    case = _case("valid windows")
    monkeypatch.setattr(release, "PRO_RELEASE_KEYS", {})
    for name in ("CLEARCOTE_PRO_RELEASE_KEYS", "CLEARCOTE_PRO_RELEASE_KEY", "CLEARCOTE_SKIP_MANIFEST",
                 "CLEARCOTE_INSECURE", "CLEARCOTE_NO_VERIFY"):
        monkeypatch.setenv(name, json.dumps(signer.TEST_KEYS))
    with pytest.raises(ProManifestError):
        verify_pro_manifest(case["meta"], "windows")


def test_a_key_that_is_pinned_wrongly_never_verifies(monkeypatch):
    case = _case("valid windows")
    for bad in ("", "zz" * 32, signer.PUBLIC_KEY_HEX[:-2], None, 5):
        monkeypatch.setattr(release, "PRO_RELEASE_KEYS", {signer.KEY_ID: bad})
        with pytest.raises(ProManifestError) as e:
            verify_pro_manifest(case["meta"], "windows")
        assert e.value.reason == "signature"


def test_the_payload_is_not_believed_before_the_signature(pro_test_key):
    """A payload that is wrong in every later check and carries a bad signature fails on the signature;
    with a key id that is not pinned, on the key."""
    meta = dict(_case("valid windows")["meta"])
    payload = signer.manifest_payload(meta, "windows", {"v": 7, "product": "other", "platform": "linux",
                                                        "version": "1", "revision": "r0", "archive": {},
                                                        "binaries": {}})
    body = signer.b64url(json.dumps(payload).encode())
    meta["manifest"] = body + "." + signer.b64url(bytes(64))
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(meta, "windows")
    assert e.value.reason == "signature"
    payload["key"] = "not-pinned"
    meta["manifest"] = signer.b64url(json.dumps(payload).encode()) + "." + signer.b64url(bytes(64))
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(meta, "windows")
    assert e.value.reason == "unknown-key"


@pytest.mark.parametrize("manifest, reason", [
    (None, "missing"),
    (5, "malformed"),
    (["a", "b"], "malformed"),
    (".", "malformed"),  # two empty parts: no payload
    ("e30.", "unknown-key"),  # payload {} names no key
    ("e30=.AAAA", "malformed"),  # padding is not base64url
    ("e30 .AAAA", "malformed"),
    ("e+0.AAAA", "malformed"),  # the standard alphabet, not the URL one
    ("W10.AAAA", "malformed"),  # payload [] is not an object
    ("bnVsbA.AAAA", "malformed"),  # payload null
    ("/w.AAAA", "malformed"),
    ("_w.AAAA", "malformed"),  # payload is the byte ff: not UTF-8
])
def test_shapes_that_are_not_a_manifest(pro_test_key, manifest, reason):
    meta = {**_case("valid windows")["meta"], "manifest": manifest}
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(meta, "windows")
    assert e.value.reason == reason


def test_a_manifest_with_an_empty_signature_fails_on_the_signature(pro_test_key):
    meta = dict(_case("valid windows")["meta"])
    meta["manifest"] = meta["manifest"].split(".")[0] + "."
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(meta, "windows")
    assert e.value.reason == "signature"


def test_an_answer_that_is_not_an_object_is_refused(pro_test_key):
    for meta in (None, [], "x"):
        with pytest.raises(ProManifestError) as e:
            verify_pro_manifest(meta, "windows")
        assert e.value.reason == "missing"


def test_fields_absent_on_both_sides_do_not_count_as_equal(pro_test_key):
    """An answer and a manifest that both leave a field out must not pass on None == None."""
    base = dict(_case("valid linux")["meta"])
    for field, reason in (("version", "version"), ("revision", "revision"), ("sha256", "archive"),
                          ("size", "archive"), ("exe_sha256", "binary")):
        meta = {k: v for k, v in base.items() if k != field}
        payload = signer.manifest_payload(base, "linux")
        if field in payload:
            del payload[field]
        elif field in ("sha256", "size"):
            del payload["archive"][field]
        else:
            payload["binaries"] = {}
        meta["manifest"] = signer.sign_manifest(payload)
        with pytest.raises(ProManifestError) as e:
            verify_pro_manifest(meta, "linux")
        assert e.value.reason == reason, field


def test_a_true_size_is_not_the_number_one(pro_test_key):
    meta = {**_case("valid linux")["meta"], "size": True}
    meta["manifest"] = signer.sign_manifest(signer.manifest_payload({**meta, "size": 1}, "linux"))
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(meta, "linux")
    assert e.value.reason == "archive"


def test_without_a_launcher_name_the_platform_default_is_the_one_checked(pro_test_key):
    meta = {k: v for k, v in _case("valid linux")["meta"].items() if k != "binary"}
    meta = signer.signed_answer(meta, "linux")  # signs binaries = {"chrome": ...}
    assert "chrome" in verify_pro_manifest(meta, "linux")["binaries"]
    wrong = signer.signed_answer(meta, "linux", {"binaries": {"Chromium": meta["exe_sha256"]}})
    with pytest.raises(ProManifestError) as e:
        verify_pro_manifest(wrong, "linux")
    assert e.value.reason == "binary"


def test_what_a_version_selector_pins():
    pins = download._pro_pins
    assert pins(None) == pins("") == pins("latest") == pins("158") == (None, None)
    assert pins("r68") == pins("R68") == pins(" r68 ") == ("r68", None)
    assert pins("158.0.8127.36") == (None, "158.0.8127.36")
    assert pins("158.0.8127.36-r68") == pins("pro-158.0.8127.36-r68") == ("r68", "158.0.8127.36")
    assert pins("158.0.8127") == pins("0.0.0") == (None, None)  # not an exact version: the server picks


# ── the download path ────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("LOCALAPPDATA", str(h))
    monkeypatch.delenv("CLEARCOTE_LICENSE_API", raising=False)
    monkeypatch.delenv("CLEARCOTE_RELEASE_CHANNEL", raising=False)
    monkeypatch.setattr(download, "_pro_meta_memo", {})
    return h


def ensure(srv, cache, **kw):
    return download.pro_ensure_binary("cc_lic_manifest", api_base=srv.url, cache_dir=cache, quiet=True, **kw)


def kept(home):
    d = home / ".clearcote"
    return sorted(d.glob("pro-meta-*.json")) if d.is_dir() else []


def new_process(monkeypatch):
    monkeypatch.setattr(download, "_pro_meta_memo", {})  # only the kept file is left


def edit_kept(home, monkeypatch, change, age=0):
    """Rewrite the kept answer as someone with write access to ~/.clearcote would."""
    new_process(monkeypatch)
    (path,) = kept(home)
    entry = json.loads(path.read_text())
    change(entry["meta"])
    entry["at"] -= age
    path.write_text(json.dumps(entry))


def refused(srv, cache, reason, **kw):
    with pytest.raises(ProManifestError) as e:
        ensure(srv, cache, **kw)
    assert e.value.reason == reason
    return e.value


def nothing_installed(cache):
    return not os.path.isdir(cache) or all(
        not os.path.exists(os.path.join(cache, d, "browser")) for d in os.listdir(cache))


# (a) an unsigned answer

@pro_route
def test_an_unsigned_answer_is_refused_before_the_archive_is_asked_for(home, tmp_path, pro_test_key):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        srv.signed = False
        refused(srv, cache, "missing")
        refused(srv, cache, "missing")  # and it was not kept: the route is asked again
    assert srv.meta_hits == 2 and srv.archive_hits == 0
    assert kept(home) == [] and download._pro_meta_memo == {}
    assert nothing_installed(cache)


@pro_route
def test_an_unsigned_answer_is_not_made_up_for_by_an_installed_build(home, tmp_path, monkeypatch, pro_test_key):
    """The build is installed and the kept answer for it is good, but the route now answers without a
    manifest: that is an error, not a reason to launch from the older answer."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        edit_kept(home, monkeypatch, lambda m: None, age=3600)  # past the TTL: the route is asked
        srv.signed = False
        refused(srv, cache, "missing")
    assert srv.archive_hits == 1


@pro_route
@pytest.mark.parametrize("manifest", ["", "not-a-manifest", "e30.AAAA", {"v": 1}])
def test_a_manifest_that_is_not_one_is_refused(home, tmp_path, pro_test_key, manifest):
    with FakeBuildServer() as srv:
        srv.meta_extra = {"manifest": manifest}
        with pytest.raises(ProManifestError):
            ensure(srv, str(tmp_path / "cache"))
    assert srv.archive_hits == 0 and kept(home) == []


@pro_route
def test_a_manifest_signed_by_another_key_is_refused(home, tmp_path, pro_test_key):
    other = hashlib.sha256(b"some other key").digest()
    with FakeBuildServer() as srv:
        good = srv.answer()
        payload = signer.manifest_payload(good, PLATFORM)
        srv.meta_extra = {"manifest": signer.sign_manifest(payload, seed=other)}
        refused(srv, str(tmp_path / "cache"), "signature")
    assert srv.archive_hits == 0


# (b) a correctly signed manifest of another build

@pro_route
def test_a_signed_manifest_of_another_revision_is_refused(home, tmp_path, pro_test_key):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        srv.manifest_over = {"revision": "r2"}  # the answer says r1
        refused(srv, cache, "revision")
    assert srv.archive_hits == 0 and kept(home) == [] and nothing_installed(cache)


@pro_route
def test_a_signed_manifest_of_another_version_or_platform_is_refused(home, tmp_path, pro_test_key):
    cache = str(tmp_path / "cache")
    other = "linux" if PLATFORM == "windows" else "windows"
    with FakeBuildServer() as srv:
        srv.manifest_over = {"version": "0.0.1"}
        refused(srv, cache, "version")
        srv.manifest_over = {"platform": other}
        refused(srv, cache, "platform")
        srv.manifest_over = {"product": "clearcote-free"}
        refused(srv, cache, "product")
    assert srv.archive_hits == 0 and kept(home) == []


@pro_route
def test_a_pinned_revision_or_version_must_be_the_signed_one(home, tmp_path, pro_test_key):
    """The route and the manifest agree on r1 of 0.0.0.0, but the caller pinned something else."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer(tag="pro-0.0.0.0-r1", version="0.0.0.0") as srv:
        refused(srv, cache, "revision", version="r2")
        refused(srv, cache, "revision", version="0.0.0.0-r2")
        refused(srv, cache, "version", version="0.0.0.1")
        refused(srv, cache, "version", version="pro-0.0.0.1-r1")
        assert srv.archive_hits == 0 and kept(home) == []
        # What the catalog made of "0" is not a pin; what the caller wrote is.
        refused(srv, cache, "version", version="0.0.0.0", pinned="0.0.0.1")
        assert srv.archive_hits == 0
        assert os.path.isfile(ensure(srv, cache, version="0.0.0.1", pinned="0"))
        for pin in ("r1", "R1", "0.0.0.0", "0.0.0.0-r1", "pro-0.0.0.0-r1", "0"):
            assert os.path.isfile(ensure(srv, cache, version=pin))
    assert srv.archive_hits == 1


def test_the_catalog_route_passes_what_the_caller_wrote_as_the_pin(monkeypatch):
    seen = []
    monkeypatch.delenv("CLEARCOTE_BINARY", raising=False)
    monkeypatch.delenv("CLEARCOTE_BROWSER_VERSION", raising=False)
    monkeypatch.setattr(download, "pro_ensure_binary", lambda key, **kw: seen.append(kw) or "exe")
    monkeypatch.setattr(download, "resolve_version", lambda sel, has_license=False, quiet=False: ("pro", "158.0.8127.36"))
    clearcote._resolve_binary(None, pro=("cc_lic_x", None), version="158")
    clearcote._resolve_binary(None, pro=("cc_lic_x", None), version="158.0.8127.36")
    assert [(kw["version"], kw["pinned"]) for kw in seen] == [("158.0.8127.36", "158"),
                                                              ("158.0.8127.36", "158.0.8127.36")]


# (c) an archive that is not the signed one

@pro_route
def test_an_altered_archive_is_refused(home, tmp_path, pro_test_key):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        altered = bytearray(srv.archive)
        altered[len(altered) // 2] ^= 1  # same size, one bit
        srv.served_archive = bytes(altered)
        with pytest.raises(RuntimeError, match="archive SHA-256 mismatch"):
            ensure(srv, cache)
    assert srv.archive_hits == 1
    assert os.listdir(os.path.join(cache, TAG)) == []  # nothing of it was extracted or left behind


@pro_route
def test_an_archive_whose_launcher_is_not_the_signed_one_is_refused(home, tmp_path, pro_test_key):
    """The answer and the manifest agree on a launcher hash the archive does not hold."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer(exe_sha256="0" * 64) as srv:
        with pytest.raises(RuntimeError, match=f"{BINARY} SHA-256 mismatch"):
            ensure(srv, cache)
    assert os.listdir(os.path.join(cache, TAG)) == []


# (d) an installed build that was altered: the launch-time check, as before

def _alter(path):
    with open(path, "wb") as f:
        f.write(b"y" * 64)  # same size, other code
    return hashlib.sha256(b"y" * 64).hexdigest()


def _protected(exe):
    names = [BINARY] + (["chrome.dll"] if sys.platform == "win32" else [])
    return [os.path.join(os.path.dirname(exe), n) for n in names]


@pro_route
@pytest.mark.parametrize("which", [0, pytest.param(1, marks=pytest.mark.skipif(
    sys.platform != "win32", reason="chrome.dll is checked on Windows"))], ids=["launcher", "chrome.dll"])
def test_an_altered_installed_build_is_not_launched(home, tmp_path, monkeypatch, pro_test_key, which):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        exe = ensure(srv, cache)
        target = _protected(exe)[which]
        _alter(target)
        srv.archive_status = 503  # no repair possible: the launch must fail, not use the altered tree
        with pytest.raises(RuntimeError, match="HTTP 503"):
            ensure(srv, cache)  # the kept answer is good, the tree is not
        new_process(monkeypatch)
        with pytest.raises(RuntimeError, match="HTTP 503"):
            ensure(srv, cache)
        srv.meta_challenge = True  # nor from the stale answer when the site cannot be reached
        with pytest.raises(RuntimeError, match="no earlier answer names an installed build"):
            ensure(srv, cache)
        srv.meta_challenge, srv.archive_status = False, 200
        for failed in (home / ".clearcote").glob("*.failed"):
            failed.unlink()  # the 30 s in which no launch asks again after a failed lookup are over
        again = ensure(srv, cache)  # with the archive back, it is replaced by the signed build
    assert again == exe and srv.archive_hits == 4  # the install, two refused with 503, the repair
    with open(target, "rb") as f:
        assert f.read() == b"x" * 64


# (e) a kept answer that was edited

def _swap_launcher(exe):
    """What the manifest is for: the installed launcher is replaced, and the kept answer is edited to
    carry the new launcher's hash, so the launch-time hash check alone would pass."""
    altered = _alter(exe)
    return lambda m: m.update(exe_sha256=altered)


@pro_route
@pytest.mark.parametrize("age", [0, 3600], ids=["within the TTL", "past the TTL"])
@pytest.mark.parametrize("drop_manifest", [False, True], ids=["manifest left as it is", "manifest removed"])
def test_a_kept_answer_with_edited_hashes_is_not_used(home, tmp_path, monkeypatch, pro_test_key, age,
                                                      drop_manifest):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        exe = ensure(srv, cache)
        swap = _swap_launcher(exe)

        def change(m):
            swap(m)
            if drop_manifest:
                del m["manifest"]

        edit_kept(home, monkeypatch, change, age)
        # Before the manifest, this launched the altered build: the kept hash matched the altered file.
        integrity = download._launch_integrity(download._pro_release(json.loads(kept(home)[0].read_text())["meta"],
                                                                     PLATFORM))
        assert download.verify_install(os.path.dirname(exe), full=False, integrity=integrity) == []

        # The site cannot be reached: the launch fails with the manifest error.
        srv.meta_challenge = True
        refused(srv, cache, "missing" if drop_manifest else "binary")
        srv.meta_challenge, srv.meta_status = False, 503
        new_process(monkeypatch)
        with open(os.path.join(str(home), ".clearcote", f"{kept(home)[0].stem}.failed"), "w"):
            pass  # another launch on this machine just failed to reach the site
        refused(srv, cache, "missing" if drop_manifest else "binary")
        os.remove(os.path.join(str(home), ".clearcote", f"{kept(home)[0].stem}.failed"))
        srv.meta_status = 200
        hits = srv.meta_hits

        # The site can: it is asked as if nothing was kept, and its signed hashes find the altered launcher.
        new_process(monkeypatch)
        again = ensure(srv, cache)
        assert srv.meta_hits == hits + 1 and srv.archive_hits == 2
    assert again == exe
    with open(exe, "rb") as f:
        assert f.read() == b"x" * 64  # the signed build again
    assert "manifest" in json.loads(kept(home)[0].read_text())["meta"]  # and a signed answer is kept again


@pro_route
@pytest.mark.parametrize("field, value, reason", [
    ("sha256", "0" * 64, "archive"),
    ("size", 1, "archive"),
    ("chrome_dll_sha256", "0" * 64, "binary" if sys.platform == "win32" else None),
    ("version", "9.9.9.9", "version"),
    ("revision", "r9", "revision"),
    ("binary", "other.exe", "binary"),
])
def test_any_signed_field_edited_in_the_kept_answer_is_noticed(home, tmp_path, monkeypatch, pro_test_key, field,
                                                               value, reason):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        exe = ensure(srv, cache)
        edit_kept(home, monkeypatch, lambda m: m.update({field: value}))
        srv.meta_challenge = True
        if reason is None:  # not a signed field on this platform: the answer is still good
            assert ensure(srv, cache) == exe
        else:
            refused(srv, cache, reason)


@pro_route
def test_a_kept_answer_from_before_the_manifest_is_not_used(home, tmp_path, monkeypatch, pro_test_key):
    """What an older SDK left in ~/.clearcote: the same answer, no manifest. The site is asked."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        exe = ensure(srv, cache)
        edit_kept(home, monkeypatch, lambda m: m.pop("manifest"))
        assert ensure(srv, cache) == exe
    assert srv.meta_hits == 2 and srv.archive_hits == 1  # asked again; the installed build is the signed one


@pro_route
def test_a_kept_answer_is_checked_on_every_launch(home, tmp_path, monkeypatch, pro_test_key):
    cache = str(tmp_path / "cache")
    calls = []
    real = download.verify_pro_manifest
    monkeypatch.setattr(download, "verify_pro_manifest", lambda *a, **k: calls.append(a) or real(*a, **k))
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        calls.clear()
        for _ in range(3):
            ensure(srv, cache)  # from this process's own copy of the answer
        assert len(calls) == 3
        new_process(monkeypatch)
        ensure(srv, cache)  # from the file
        assert len(calls) == 4
    assert srv.meta_hits == 1
    assert all(a[1] == PLATFORM and a[2:] == (None, None) for a in calls)


@pro_route
def test_a_kept_answer_must_match_what_this_call_pinned(home, tmp_path, monkeypatch, pro_test_key):
    """One kept answer per request URL; the pin of the call that reads it decides."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer(tag="pro-0.0.0.0-r1", version="0.0.0.0") as srv:
        exe = ensure(srv, cache, version="0.0.0.0", pinned="0")
        assert ensure(srv, cache, version="0.0.0.0", pinned="0.0.0.0-r1") == exe
        srv.meta_challenge = True
        refused(srv, cache, "revision", version="0.0.0.0", pinned="0.0.0.0-r2")


@pro_route
def test_an_answer_a_peer_kept_while_this_call_waited_is_checked_too(home, tmp_path, monkeypatch, pro_test_key):
    cache = str(tmp_path / "cache")
    seen = {}

    def flight(name, read_kept, ask, transient=None):
        key = name[len("pro-meta-"):]
        good = ask()
        download._keep_pro_meta(key, {k: v for k, v in good.items() if k != "manifest"})
        seen["unsigned"] = read_kept()
        download._keep_pro_meta(key, {**good, "sha256": "0" * 64})
        seen["edited"] = read_kept()
        download._keep_pro_meta(key, good)
        seen["good"] = read_kept()
        return good

    monkeypatch.setattr(download, "_single_flight", flight)
    with FakeBuildServer() as srv:
        assert os.path.isfile(ensure(srv, cache))
        assert seen["unsigned"] is None and seen["edited"] is None and seen["good"] == srv.answer()


# (f) the SDK as it ships today: no key pinned

@pro_route
def test_with_no_pinned_key_a_pro_download_is_refused(home, tmp_path, monkeypatch):
    monkeypatch.setattr(release, "PRO_RELEASE_KEYS", {})
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        err = refused(srv, cache, "unknown-key")
    assert "Refusing to use this build" in str(err)
    assert srv.archive_hits == 0 and kept(home) == [] and nothing_installed(cache)


@pro_route
def test_with_no_pinned_key_an_installed_build_is_not_launched_either(home, tmp_path, monkeypatch, pro_test_key):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        monkeypatch.setattr(release, "PRO_RELEASE_KEYS", {})
        refused(srv, cache, "unknown-key")  # the kept answer fails, then the fresh one
        srv.meta_challenge = True
        refused(srv, cache, "unknown-key")
    assert srv.archive_hits == 1


def test_the_free_build_path_does_not_ask_for_a_manifest(monkeypatch, tmp_path):
    """Free builds are pinned in the SDK source: no manifest, and the manifest check is never called."""
    monkeypatch.setattr(download, "verify_pro_manifest", lambda *a, **k: pytest.fail("checked a free build"))
    monkeypatch.setattr(download, "_download", lambda *a, **k: pytest.fail("no download in this test"))
    assert download._cached(str(tmp_path / "nothing"), BINARY, quiet=True, repair=False) is None
    kind, rel = download.resolve_version("151", catalog=release.CATALOG_FALLBACK) if sys.platform != "darwin" \
        else ("free", {})
    assert kind == "free" and "manifest" not in rel
