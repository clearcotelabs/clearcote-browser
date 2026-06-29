import struct

import pytest

from clearcote import _widevine
from clearcote._widevine import (
    HINT_FILE,
    OMAHA_URL,
    WIDEVINE_APP_ID,
    _crx3_to_zip,
    _omaha_request_body,
    _parse_update,
    apply_widevine_launch,
)


def test_constants_pin_the_widevine_component():
    # the Widevine CDM component app id + Google's Omaha endpoint must not drift
    assert WIDEVINE_APP_ID == "oimompecagnajdejgnnjijobebaeigek"
    assert OMAHA_URL.startswith("https://update.googleapis.com/")
    assert HINT_FILE == "latest-component-updated-widevine-cdm"


def test_omaha_request_body_targets_windows_x64():
    body = _omaha_request_body()["request"]
    assert body["@os"] == "win" and body["arch"] == "x64"
    assert body["acceptformat"] == "crx3"
    app = body["app"][0]
    assert app["appid"] == WIDEVINE_APP_ID
    assert app["version"] == "0.0.0.0"  # forces the server to return the latest


def test_parse_update_pipelines_shape():
    resp = {"response": {"app": [{
        "appid": WIDEVINE_APP_ID,
        "nextversion": "4.10.3050.0",
        "updatecheck": {"status": "ok", "pipelines": [{"operations": [
            {"type": "download",
             "urls": [{"url": "https://x/cdm.crx3"}],
             "out": {"sha256": "abcd"}},
        ]}]},
    }]}}
    assert _parse_update(resp) == ("https://x/cdm.crx3", "abcd", "4.10.3050.0")


def test_parse_update_classic_shape():
    resp = {"response": {"app": [{
        "appid": WIDEVINE_APP_ID,
        "updatecheck": {"status": "ok",
                        "urls": {"url": [{"codebase": "https://x/dl/"}]},
                        "manifest": {"version": "4.10.3050.0",
                                     "packages": {"package": [
                                         {"name": "cdm.crx3", "hash_sha256": "beef"}]}}},
    }]}}
    assert _parse_update(resp) == ("https://x/dl/cdm.crx3", "beef", "4.10.3050.0")


def test_parse_update_rejects_non_ok_status():
    resp = {"response": {"app": [{"updatecheck": {"status": "noupdate"}}]}}
    with pytest.raises(RuntimeError):
        _parse_update(resp)


def test_crx3_to_zip_strips_the_header():
    header = b"\x10\x20\x30\x40"
    zip_bytes = b"PK\x03\x04 the zip payload"
    crx = b"Cr24" + struct.pack("<I", 3) + struct.pack("<I", len(header)) + header + zip_bytes
    assert _crx3_to_zip(crx) == zip_bytes


def test_crx3_to_zip_passes_through_plain_zip():
    plain = b"PK\x03\x04 already a zip"
    assert _crx3_to_zip(plain) == plain


def _no_network_seed(monkeypatch):
    # apply_widevine_launch seeds the CDM (network/disk) first — stub it out for arg-only tests
    monkeypatch.setattr(_widevine, "seed_widevine", lambda *a, **k: "X:/cdm")


def test_apply_widevine_launch_adds_the_two_flags(monkeypatch):
    _no_network_seed(monkeypatch)
    kw = {}
    apply_widevine_launch("prof", kw, quiet=True)
    # Playwright disables the component updater by default; we un-suppress it + force the scan.
    # It must ONLY touch the component-updater flags — not inject unrelated defaults.
    assert kw["ignore_default_args"] == ["--disable-component-update"]
    assert "--component-updater=fast-update" in kw["args"]


def test_apply_widevine_launch_handles_boolean_ignore_default_args(monkeypatch):
    _no_network_seed(monkeypatch)
    # ignore_default_args=True (a valid Playwright value) must NOT crash launch (list(True) -> TypeError)
    kw = {"ignore_default_args": True}
    apply_widevine_launch("prof", kw, quiet=True)
    assert kw["ignore_default_args"] is True  # already drops all defaults -> left untouched
    assert "--component-updater=fast-update" in kw["args"]


def test_apply_widevine_launch_never_aborts_on_failure(monkeypatch):
    # any failure (seed OR arg-munging) degrades to a no-DRM launch, never raises
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(_widevine, "seed_widevine", boom)
    apply_widevine_launch("prof", {"ignore_default_args": True}, quiet=True)  # must not raise


def test_apply_widevine_launch_is_idempotent(monkeypatch):
    _no_network_seed(monkeypatch)
    kw = {}
    apply_widevine_launch("prof", kw, quiet=True)
    apply_widevine_launch("prof", kw, quiet=True)
    assert kw["ignore_default_args"].count("--disable-component-update") == 1
    assert kw["args"].count("--component-updater=fast-update") == 1


def test_apply_widevine_launch_preserves_user_values(monkeypatch):
    _no_network_seed(monkeypatch)
    kw = {"args": ["--foo"], "ignore_default_args": ["--bar"]}
    apply_widevine_launch("prof", kw, quiet=True)
    assert kw["args"][0] == "--foo" and "--component-updater=fast-update" in kw["args"]
    assert "--bar" in kw["ignore_default_args"] and "--disable-component-update" in kw["ignore_default_args"]


def test_apply_widevine_launch_respects_user_component_updater(monkeypatch):
    _no_network_seed(monkeypatch)
    kw = {"args": ["--component-updater=test-request"]}
    apply_widevine_launch("prof", kw, quiet=True)
    # don't clobber a user-chosen component-updater mode
    assert "--component-updater=fast-update" not in kw["args"]
    assert "--component-updater=test-request" in kw["args"]


def test_apply_widevine_launch_skips_args_when_seeding_fails(monkeypatch):
    # if the CDM can't be seeded, DRM is gracefully off — don't add flags that imply it works
    def boom(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(_widevine, "seed_widevine", boom)
    kw = {}
    apply_widevine_launch("prof", kw, quiet=True)
    assert "args" not in kw and "ignore_default_args" not in kw


def test_fetch_widevine_refuses_unverified_when_no_hash(monkeypatch, tmp_path):
    # a CDM is a native DLL loaded into the browser — an update response with an EMPTY hash
    # (the classic shape can yield one) must NOT install it unverified.
    resp = {"response": {"app": [{"updatecheck": {"status": "ok",
            "urls": {"url": [{"codebase": "https://x/dl/"}]},
            "manifest": {"version": "1.2.3.4",
                         "packages": {"package": [{"name": "cdm.crx3", "hash_sha256": ""}]}}}}]}}
    monkeypatch.setattr(_widevine, "_post_json", lambda *a, **k: resp)

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"not a real cdm"

    monkeypatch.setattr(_widevine.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    with pytest.raises(RuntimeError, match="unverified"):
        _widevine.fetch_widevine(dest=str(tmp_path), quiet=True)
