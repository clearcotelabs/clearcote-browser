"""Import + fallback behaviour for ``profile="auto"``. Mirrors the Node SDK's
profileimport/profileauto tests, against a REAL local HTTP server rather than mocks."""

import atexit
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = tempfile.mkdtemp(prefix="cc-pyauto-")
os.environ["CLEARCOTE_PROFILE_DIR"] = os.path.join(ROOT, "cache")
# Removed at interpreter exit, not module teardown: CLEARCOTE_PROFILE_DIR is read once, at import, so ROOT
# stays the profile directory for every test module that runs after this one.
atexit.register(shutil.rmtree, ROOT, True)

from clearcote._profileauto import (  # noqa: E402
    _reset_hint,
    local_setup_hint,
    resolve_auto,
    resolve_local,
)
from clearcote._profileimport import (  # noqa: E402
    import_directory,
    index_entry_from_profile,
    load_imported_profile,
)
from clearcote._profilelib import eligible, select_profile  # noqa: E402

HOST = {
    "os_family": "windows",
    "browser_major": 150,
    "gpu_vendor": "nvidia",
    "screen_width": 3440,
    "screen_height": 1440,
    "device_pixel_ratio": 1,
    "hardware_concurrency": 16,
    "device_memory": 16,
}

_next_major = [150]


def fresh_host():
    """Distinct major per case => distinct index-cache key.

    fetch_index deliberately serves a STALE cached index when the service is down, so without
    this a previous test's cache would make a 503 look like success.
    """
    _next_major[0] += 1
    return {**HOST, "browser_major": _next_major[0]}


def profile_blob(with_version: bool = False):
    """Version-agnostic by default — exactly like a converted chrome-fingerprints record, which
    carries hardware identity only and inherits the engine's version."""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    ua += "Chrome/150.0.0.0 Safari/537.36" if with_version else "Safari/537.36"
    return {
        "meta": {"captured_at": "2026-07-01T00:00:00+00:00"},
        "navigator": {"user_agent": ua, "platform": "Win32"},
        "screen": {"width": 1920, "height": 1080, "device_pixel_ratio": 1},
        "webgl": {"webgl1": {"debug": {
            "UNMASKED_RENDERER_WEBGL": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3080 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        }}},
        "hardware_concurrency": 16,
        "device_memory": 16,
    }


STATUS = [200]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = parse_qs(urlparse(self.path).query)
        if STATUS[0] != 200:
            self._send(STATUS[0], {"error": "service says no"})
            return
        if q.get("id"):
            self._send(200, {"id": "svc-1", "profile": profile_blob()})
            return
        major = int(q.get("major", ["150"])[0])
        self._send(200, {"candidates": [{
            "id": "svc-1", "os_family": "windows", "browser_major": major,
            "gpu_vendor": "nvidia", "screen_width": 1920, "screen_height": 1080,
            "device_pixel_ratio": 1, "hardware_concurrency": 16, "device_memory": 16,
            "encoded_size": 9000, "captured_at": "2026-07-20T00:00:00+00:00",
        }]})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # silence the test server
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def local_dir():
    d = os.path.join(ROOT, "imported")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "local-1.json"), "w", encoding="utf-8") as f:
        json.dump(profile_blob(), f)
    with open(os.path.join(d, "broken.json"), "w", encoding="utf-8") as f:
        f.write("{ not json")
    with open(os.path.join(d, "not-a-profile.json"), "w", encoding="utf-8") as f:
        json.dump({"hello": "world"}, f)
    return d


@pytest.fixture(autouse=True)
def reset():
    STATUS[0] = 200
    _reset_hint()


class TestImportDirectory:
    def test_indexes_valid_and_reports_skipped(self, local_dir):
        r = import_directory(local_dir)
        assert [e["id"] for e in r["index"]] == ["local-1"]
        # A half-converted directory must be visible, not silently thin.
        assert sorted(s["file"] for s in r["skipped"]) == ["broken.json", "not-a-profile.json"]

    def test_derives_fields_from_blob(self, local_dir):
        e = import_directory(local_dir)["index"][0]
        assert e["os_family"] == "windows"
        assert e["gpu_vendor"] == "nvidia"
        assert e["screen_width"] == 1920
        assert e["encoded_size"] > 0

    def test_rejects_non_directory(self, local_dir):
        with pytest.raises(ValueError, match="not a directory"):
            import_directory(os.path.join(local_dir, "local-1.json"))

    def test_path_traversal_refused(self, local_dir):
        with pytest.raises(ValueError, match="invalid profile id"):
            load_imported_profile(local_dir, "../../etc/passwd")


class TestVersionAgnostic:
    def test_no_version_means_eligible_on_any_engine(self, local_dir):
        # The chrome-fingerprints case: records are Chrome ~114/115, engine is 150, and the
        # converter drops the version so there is nothing to disagree with.
        e = import_directory(local_dir)["index"][0]
        assert e["browser_major"] is None
        assert eligible(e, HOST)
        assert eligible(e, {**HOST, "browser_major": 138})

    def test_pinned_wrong_version_still_rejected(self):
        e = index_entry_from_profile("old", {
            "navigator": {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/138.0.0.0",
                          "platform": "Win32"},
            "screen": {"width": 1920, "height": 1080},
        })
        assert e["browser_major"] == 138
        assert not eligible(e, HOST)


class TestServiceFirst:
    def test_uses_service_when_available(self, server, local_dir):
        r = resolve_auto(fresh_host(), license_key="cc_lic_test", api_base=server,
                         local_dir=local_dir, quiet=True)
        assert r["source"] == "service"
        assert r["selection"]["entry"]["id"] == "svc-1"


class TestFallback:
    @pytest.mark.parametrize("code", [429, 401, 503])
    def test_falls_back_on_service_failure(self, server, local_dir, code):
        # 429 = rate limited or daily budget exhausted; 401 = no/expired license. In all of
        # these a local persona beats none.
        STATUS[0] = code
        r = resolve_auto(fresh_host(), license_key="cc_lic_test", api_base=server,
                         local_dir=local_dir, quiet=True)
        assert r["source"] == "local"
        assert r["selection"]["entry"]["id"] == "local-1"
        assert r["fallback_reason"]  # the reason must survive, never a silent downgrade

    def test_prefer_local_skips_service(self, server, local_dir):
        r = resolve_auto(fresh_host(), license_key="cc_lic_test", api_base=server,
                         local_dir=local_dir, prefer_local=True, quiet=True)
        assert r["source"] == "local"


class TestNeitherSource:
    def test_reports_both_failures(self, server):
        # Surfacing only the local error would send the caller to fix a directory when the real
        # problem was the license.
        STATUS[0] = 401
        with pytest.raises(ValueError, match=r"service:[\s\S]*local:"):
            resolve_auto(fresh_host(), license_key="cc_lic_test", api_base=server,
                         local_dir=os.path.join(ROOT, "missing"), quiet=True)

    def test_includes_optional_setup_example(self, server):
        STATUS[0] = 503
        with pytest.raises(ValueError, match=r"convert_dataset\.py"):
            resolve_auto(fresh_host(), license_key="cc_lic_test", api_base=server,
                         local_dir=os.path.join(ROOT, "missing2"), quiet=True)


class TestLocalSetupHint:
    def test_frames_dataset_as_optional_and_user_fetched(self):
        h = local_setup_hint("/tmp/x")
        assert "optional" in h.lower()
        # We redistribute nothing: the user fetches it from its own repo under its own terms.
        assert "own repo" in h or "own licence" in h
        assert "pip install chrome-fingerprints" in h
        assert "/tmp/x" in h

    def test_offers_alternatives(self):
        h = local_setup_hint("/tmp/x")
        assert "capture your own" in h.lower()
        assert "CLEARCOTE_PROFILE_SOURCE" in h


class TestResolveLocal:
    def test_selects_from_directory(self, local_dir):
        r = resolve_local(fresh_host(), local_dir)
        assert r["source"] == "local"
        assert "navigator" in r["profile"]

    def test_missing_directory_explains_how_to_populate(self):
        with pytest.raises(ValueError, match=r"no local profile directory[\s\S]*convert_dataset"):
            resolve_local(HOST, os.path.join(ROOT, "nope"))


# -- host cache key + engine major -----------------------------------------------------------

class TestHostCacheKey:
    """The host probe is the one path that launches a second browser, so a cache MISS is
    expensive. It used to key on the caller's literal path string, so the same binary spelled
    another way (slashes, ``./``, a symlink, Windows case) re-measured every launch."""

    def test_same_binary_spelled_differently_hits(self, tmp_path, monkeypatch):
        import os

        from clearcote import _profilesource as PS

        monkeypatch.setattr(PS, "profile_cache_dir", lambda: str(tmp_path))
        exe = tmp_path / "chrome"
        exe.write_bytes(b"x")
        facts = {"os_family": "linux", "browser_major": 153, "gpu_vendor": "intel"}
        PS.write_cached_host(str(exe), facts)

        other = os.path.join(str(tmp_path), ".", "chrome")
        assert PS.read_cached_host(other) is not None
        assert PS.read_cached_host(str(exe))["browser_major"] == 153
        assert PS.read_cached_host(str(tmp_path / "other-chrome")) is None


class TestEngineMajor:
    """``browser_major`` is a HARD filter on profile selection, so it has to describe the binary
    that will actually run -- not whatever release this SDK happens to pin."""

    def test_reads_the_major_off_the_running_browser(self):
        from clearcote._profilesource import _engine_major

        class B:
            version = "153.0.8010.36"

        assert _engine_major(B(), 149) == 153

    def test_falls_back_when_the_version_is_unusable(self):
        from clearcote._profilesource import _engine_major

        class NoVersion:
            pass

        class Junk:
            version = "not-a-version"

        assert _engine_major(NoVersion(), 149) == 149
        assert _engine_major(Junk(), 149) == 149
