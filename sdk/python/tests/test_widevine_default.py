"""Widevine ON by default for licensed (PRO) launches, opt-in for free ones (0.48.0).

Hermetic: no network. Google's update check and the CDM download are stand-ins (``_post_json`` /
``urlopen``, or ``fetch_widevine`` itself), the CDM cache is a temp directory (CLEARCOTE_WIDEVINE_DIR),
Playwright is a stand-in that records the launch, and the "engine" is a small file carrying (or not)
the ``widevine-cdm-path`` switch name the probe looks for. Same cases in the Node and .NET suites.
"""
import asyncio
import io
import json
import os
import struct
import sys
import threading
import time
import zipfile

import pytest

import clearcote
import clearcote.async_api as async_api
from clearcote import _warnings
from clearcote import _widevine as W

# An engine with the --widevine-cdm-path switch (1022) AND CDM host verification (r63+): the licensed default turns
# Widevine on on every host. ENGINE_1022_UNVERIFIED (r32..r62) keeps the default off on Windows and macOS hosts.
ENGINE_1022 = b"\x00widevine-cdm-path\x00VerifyCdmHost_0\x00"
ENGINE_1022_UNVERIFIED = b"\x00widevine-cdm-path\x00"
WIN_OR_MAC = sys.platform.startswith("win") or sys.platform == "darwin"
VERSION = "4.10.9999.0"
KEY = "cc_lic_test_widevine_default"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("CLEARCOTE_WIDEVINE_DIR", str(tmp_path / "wv"))
    for var in ("CLEARCOTE_WIDEVINE", "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "CLEARCOTE_LICENSE_KEY",
                "CLEARCOTE_NO_WARN", "CLEARCOTE_BINARY", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER",
                "CLEARCOTE_PERSONA_ENV"):
        monkeypatch.delenv(var, raising=False)
    # Catch-all network guard: Google's update check and the download fail loudly unless a test stands in for
    # them (FakeGoogle, or a stand-in fetch_widevine), as the Node and .NET suites do with their fetch/HTTP seams.
    def no_network(*a, **k):
        raise AssertionError("unexpected network call from the Widevine code")

    monkeypatch.setattr(W, "_post_json", no_network)
    monkeypatch.setattr(W.urllib.request, "urlopen", no_network)
    W._reset_default_state()
    _warnings._seen_notes.discard(W.WARN_CODE)
    yield
    _join_background()
    W._reset_default_state()
    _warnings._seen_notes.discard(W.WARN_CODE)


def _join_background(timeout=10):
    """Wait for the default path's threads (a first download, a background refresh), so none outlives its test."""
    job, refresh = W._DEFAULT["job"], W._DEFAULT["refresh"]
    for t in ((job or {}).get("thread"), refresh):
        if t is not None:
            t.join(timeout)


def fake_engine(tmp_path, *markers, name="chrome"):
    p = tmp_path / name
    p.write_bytes(b"\x7fELF" + b"".join(markers) + b"\x00--remote-debugging-port\x00")
    return str(p)


def _sub_fn():
    _a, _b, _c, sub, fn = W._cdm_platform()
    return sub, fn


def cached_cdm_dir(root, version=VERSION, stamp=True, complete=True):
    """A CDM already in the cache; ``stamp``: Google was asked just now."""
    sub, fn = _sub_fn()
    d = os.path.join(root, version)
    os.makedirs(os.path.join(d, "_platform_specific", sub), exist_ok=True)
    with open(os.path.join(d, "manifest.json"), "w") as f:
        f.write("{}")
    if complete:
        with open(os.path.join(d, "_platform_specific", sub, fn), "wb") as f:
            f.write(b"cdm")
    if stamp:
        W._touch_stamp(root)
    return d


def crx(files):
    """A CRX3 blob ('Cr24' + version + header length + header + zip) holding ``files``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    header = b"\x01\x02\x03\x04"
    return b"Cr24" + struct.pack("<I", 3) + struct.pack("<I", len(header)) + header + buf.getvalue()


def cdm_crx():
    sub, fn = _sub_fn()
    return crx({"manifest.json": '{"version": "%s"}' % VERSION,
                "_platform_specific/%s/%s" % (sub, fn): b"\x7fELF fake cdm"})


class FakeGoogle:
    """Google's update server + download host: counts calls, can fail, block or serve a bad hash."""

    def __init__(self, monkeypatch, blob=None, fail=False, sha=None, block=None, version=VERSION):
        import hashlib
        self.blob = blob if blob is not None else cdm_crx()
        self.sha = hashlib.sha256(self.blob).hexdigest() if sha is None else sha
        self.fail, self.block, self.version = fail, block, version
        self.checks = 0
        self.downloads = 0
        self.lock = threading.Lock()
        monkeypatch.setattr(W, "_post_json", self._post_json)
        monkeypatch.setattr(W.urllib.request, "urlopen", self._urlopen)

    def _post_json(self, url, body):
        with self.lock:
            self.checks += 1
        if self.fail:
            raise OSError("no network")
        return {"response": {"app": [{"nextversion": self.version, "updatecheck": {"status": "ok", "pipelines": [
            {"operations": [{"urls": [{"url": "https://example.invalid/cdm.crx3"}], "out": {"sha256": self.sha}}]}]}}]}}

    def _urlopen(self, req, timeout=None):
        with self.lock:
            self.downloads += 1
        if self.block is not None:
            self.block.wait(30)
        blob = self.blob

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return blob

        return _Resp()


# -- the rules ----------------------------------------------------------------------------------

@pytest.mark.parametrize("option, licensed, env, expected", [
    (None, True, None, "default"),
    (None, False, None, "off"),
    (True, True, None, "explicit"),
    (True, False, None, "explicit"),
    (False, True, None, "off"),
    (False, False, None, "off"),
    (None, True, "0", "off"),
    (None, True, "false", "off"),
    (None, True, "OFF", "off"),
    (None, True, "no", "off"),
    (None, False, "1", "explicit"),
    (None, False, "true", "explicit"),
    (None, True, "on", "explicit"),
    (None, False, "yes", "explicit"),
    (True, True, "0", "explicit"),   # an explicit option wins over the environment
    (False, False, "1", "off"),
    (None, True, "maybe", "default"),  # an unknown value is ignored
    (None, False, "maybe", "off"),
    (None, True, "", "default"),
])
def test_mode_matrix(option, licensed, env, expected):
    assert W.widevine_mode(option, licensed, env={} if env is None else {"CLEARCOTE_WIDEVINE": env}) == expected


def test_mode_reads_the_process_environment(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_WIDEVINE", "0")
    assert W.widevine_mode(None, True) == "off"
    monkeypatch.setenv("CLEARCOTE_WIDEVINE", "1")
    assert W.widevine_mode(None, False) == "explicit"


@pytest.mark.parametrize("value, expected", [(None, 15.0), ("3", 3.0), ("0.5", 1.0), ("0", 15.0), ("-2", 15.0),
                                             ("soon", 15.0), ("", 15.0), ("600", 600.0), ("601", 600.0),
                                             ("1e10", 600.0), ("inf", 600.0), ("nan", 15.0), ("-inf", 15.0)])
def test_timeout_seconds(value, expected):
    # clamped to 1-600 s: a huge value used to overflow Thread.join and fail the launch
    assert W.timeout_seconds({} if value is None else {"CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS": value}) == expected


def test_the_names_match_the_other_sdks():
    assert W.WIDEVINE_ENV == "CLEARCOTE_WIDEVINE"
    assert W.TIMEOUT_ENV == "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS"
    assert W.DEFAULT_TIMEOUT_SECONDS == 15.0


# -- the cache ----------------------------------------------------------------------------------

def test_cached_cdm_picks_the_newest_complete_version(tmp_path):
    root = str(tmp_path / "wv")
    assert W.cached_cdm(root) is None  # no directory at all
    cached_cdm_dir(root, "4.10.100.0")
    newest = cached_cdm_dir(root, "4.10.2830.0")
    cached_cdm_dir(root, "4.10.9000.0", complete=False)  # half-extracted: never used
    cached_cdm_dir(root, "current")
    os.makedirs(os.path.join(root, ".tmp-abc"))
    assert W.cached_cdm(root) == newest


def test_stamp_freshness(tmp_path):
    root = str(tmp_path)
    assert not W._stamp_fresh(root)
    W._touch_stamp(root)
    assert W._stamp_fresh(root)
    old = time.time() - W.CHECK_INTERVAL_SECONDS - 60
    os.utime(os.path.join(root, W.STAMP_FILE), (old, old))
    assert not W._stamp_fresh(root)


def test_fetch_installs_verifies_and_stamps(monkeypatch, tmp_path):
    g = FakeGoogle(monkeypatch)
    root = str(tmp_path / "wv")
    d = W.fetch_widevine(quiet=True)
    assert d == os.path.join(root, VERSION) and W._valid_cdm(d)
    assert W._stamp_fresh(root)
    assert sorted(os.listdir(root)) == [W.STAMP_FILE, VERSION]  # no lock, no .tmp-*, no .trash-* left
    assert (g.checks, g.downloads) == (1, 1)


def test_fetch_does_not_download_a_cdm_it_already_has(monkeypatch, tmp_path):
    g = FakeGoogle(monkeypatch)
    W.fetch_widevine(quiet=True)
    W.fetch_widevine(quiet=True)
    assert (g.checks, g.downloads) == (2, 1)  # fetch_widevine always asks; it downloads once


def test_fetch_refuses_a_bad_hash_and_leaves_nothing(monkeypatch, tmp_path):
    FakeGoogle(monkeypatch, sha="00" * 32)
    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        W.fetch_widevine(quiet=True)
    root = str(tmp_path / "wv")
    assert W.cached_cdm(root) is None
    assert not [n for n in os.listdir(root) if n.startswith((".tmp-", ".trash-", ".install-lock"))]


def test_fetch_rejects_a_crx_without_the_cdm(monkeypatch, tmp_path):
    FakeGoogle(monkeypatch, blob=crx({"manifest.json": "{}"}))
    with pytest.raises(RuntimeError, match="CDM binary was not at"):
        W.fetch_widevine(quiet=True)
    assert W.cached_cdm(str(tmp_path / "wv")) is None


def test_fetch_replaces_a_half_extracted_tree(monkeypatch, tmp_path):
    root = str(tmp_path / "wv")
    cached_cdm_dir(root, VERSION, stamp=False, complete=False)  # an older SDK extracted in place and died
    with open(os.path.join(root, VERSION, "stray"), "w") as f:
        f.write("x")
    os.makedirs(os.path.join(root, ".tmp-dead"))  # and an interrupted install left this
    FakeGoogle(monkeypatch)
    d = W.fetch_widevine(quiet=True)
    assert W._valid_cdm(d) and not os.path.exists(os.path.join(d, "stray"))
    assert not [n for n in os.listdir(root) if n.startswith((".tmp-", ".trash-"))]


def test_concurrent_first_fetches_download_once_and_never_see_a_partial_cdm(monkeypatch, tmp_path):
    gate = threading.Event()
    g = FakeGoogle(monkeypatch, block=gate)
    root = str(tmp_path / "wv")
    results, errors = [], []

    def worker():
        try:
            results.append(W.fetch_widevine(quiet=True))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    deadline = time.time() + 10
    while g.downloads == 0 and time.time() < deadline:
        time.sleep(0.01)
    # mid-install: the version directory is not there yet, so nothing can read a half-extracted CDM
    assert W.cached_cdm(root) is None
    assert os.path.exists(os.path.join(root, ".install-lock"))
    gate.set()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(results) == 6 and len(set(results)) == 1 and W._valid_cdm(results[0])
    assert g.downloads == 1  # the others waited at the install lock, then found it installed
    assert not os.path.exists(os.path.join(root, ".install-lock"))


def test_resolve_uses_a_fresh_cache_without_any_network(monkeypatch, tmp_path):
    d = cached_cdm_dir(str(tmp_path / "wv"))
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    assert W.resolve_cdm(quiet=True) == d


def test_resolve_rechecks_a_stale_cache_and_keeps_it_when_offline(monkeypatch, tmp_path):
    d = cached_cdm_dir(str(tmp_path / "wv"), stamp=False)
    calls = []

    def offline(dest=None, quiet=False):
        calls.append(1)
        raise OSError("no network")

    monkeypatch.setattr(W, "fetch_widevine", offline)
    assert W.resolve_cdm(quiet=True) == d
    assert calls == [1]
    # the failed check is recorded: no launch (in any process) asks again for a day
    assert W._stamp_fresh(str(tmp_path / "wv"))
    assert W.resolve_cdm(quiet=True) == d
    assert calls == [1]


def test_resolve_without_a_cache_raises_when_offline(monkeypatch):
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(OSError):
        W.resolve_cdm(quiet=True)


def test_a_second_resolve_does_not_go_to_google_again(monkeypatch, tmp_path):
    g = FakeGoogle(monkeypatch)
    first = W.resolve_cdm(quiet=True)
    second = W.resolve_cdm(quiet=True)
    assert first == second and (g.checks, g.downloads) == (1, 1)


# -- the default: bounded, fail-soft, warns once -------------------------------------------------

def test_default_cdm_fail_soft_warns_once(monkeypatch, capsys):
    calls = []

    def offline(dest=None, quiet=False):
        calls.append(1)
        raise OSError("Google said 403")

    monkeypatch.setattr(W, "fetch_widevine", offline)
    assert W.default_cdm() is None
    assert W.default_cdm() is None  # within the retry window: no second attempt
    err = capsys.readouterr().err
    assert err.count("clearcote: warning: Widevine is on by default") == 1
    assert "OSError: Google said 403" in err
    # the advice leads with the variable, which works for every launch (cloud and Docker included)
    assert err.index("CLEARCOTE_WIDEVINE=0") < err.index("widevine=False")
    assert calls == [1]


def test_default_cdm_retries_after_the_window(monkeypatch):
    calls = []
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: calls.append(1) or (_ for _ in ()).throw(OSError("x")))
    W.default_cdm(quiet=True)
    W._DEFAULT["failed_at"] = time.monotonic() - W.RETRY_AFTER_SECONDS - 1
    W.default_cdm(quiet=True)
    assert calls == [1, 1]


def test_default_cdm_quiet_says_nothing(monkeypatch, capsys):
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("x")))
    assert W.default_cdm(quiet=True) is None
    assert capsys.readouterr().err == ""
    assert W.WARN_CODE not in _warnings._seen_notes  # quiet does not use the warning up


def test_default_cdm_no_warn_env_says_nothing(monkeypatch, capsys):
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("x")))
    assert W.default_cdm() is None
    assert capsys.readouterr().err == ""


def test_default_cdm_is_bounded_and_a_late_fetch_is_used_later(monkeypatch, tmp_path, capsys):
    root = str(tmp_path / "wv")
    gate = threading.Event()

    def slow(dest=None, quiet=False):
        gate.wait(30)
        return cached_cdm_dir(root)  # installs and stamps, as the real fetch does

    monkeypatch.setattr(W, "fetch_widevine", slow)
    t0 = time.monotonic()
    assert W.default_cdm(timeout=0.3) is None
    assert time.monotonic() - t0 < 3
    assert "not ready within 0.3 s" in capsys.readouterr().err
    # a launch meanwhile does not wait again
    t1 = time.monotonic()
    assert W.default_cdm(timeout=5) is None
    assert time.monotonic() - t1 < 1
    gate.set()
    W._DEFAULT["job"]["thread"].join(10)
    assert W.default_cdm(timeout=0.3) == os.path.join(root, VERSION)  # the late fetch installed it


def test_default_cdm_timeout_from_the_environment(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "0.2")
    gate = threading.Event()
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: gate.wait(30))
    t0 = time.monotonic()
    assert W.default_cdm(quiet=True) is None
    assert time.monotonic() - t0 < 3
    gate.set()


def test_a_stale_cache_is_used_at_once_and_refreshed_in_the_background(monkeypatch, tmp_path, capsys):
    # S12: a stamp over a day old used to make the launch wait (up to the timeout) for Google, and then warn
    # "launching without it" while it launched WITH the cached CDM.
    root = str(tmp_path / "wv")
    d = cached_cdm_dir(root, stamp=False)
    gate, calls = threading.Event(), []

    def hanging(dest=None, quiet=False):
        calls.append(quiet)
        gate.wait(30)
        raise OSError("blackholed")

    monkeypatch.setattr(W, "fetch_widevine", hanging)
    t0 = time.monotonic()
    assert W.default_cdm(timeout=5) == d
    assert W.default_cdm(timeout=5) == d  # a second launch meanwhile: no second refresh
    assert time.monotonic() - t0 < 1  # no network on the launch path
    gate.set()
    W._DEFAULT["refresh"].join(10)
    assert calls == [True]  # one background check, quiet
    out = capsys.readouterr()
    assert out.err == "" and out.out == ""  # Widevine works: nothing to warn about, nothing on stdout
    assert W._stamp_fresh(root)  # the failed check is recorded: no new check for a day
    assert W.default_cdm() == d and not W._DEFAULT["refresh"].is_alive() and calls == [True]


def test_a_background_refresh_installs_a_newer_cdm_for_the_next_launch(monkeypatch, tmp_path):
    root = str(tmp_path / "wv")
    old = cached_cdm_dir(root, "4.10.1.0", stamp=False)
    g = FakeGoogle(monkeypatch)  # offers VERSION, newer than 4.10.1.0
    assert W.default_cdm() == old  # this launch: the cached one, at once
    W._DEFAULT["refresh"].join(10)
    assert (g.checks, g.downloads) == (1, 1)
    assert W.default_cdm() == os.path.join(root, VERSION)  # the next launch: the new one, no network
    assert g.checks == 1


def test_default_cdm_with_a_fresh_cache_starts_no_thread(monkeypatch, tmp_path):
    d = cached_cdm_dir(str(tmp_path / "wv"))
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    assert W.default_cdm() == d and W._DEFAULT["job"] is None and W._DEFAULT["refresh"] is None


def test_the_default_path_prints_nothing_on_stdout(monkeypatch, capsys):
    # S17: the default fetch is quiet; only the warning (stderr) may ever be printed
    FakeGoogle(monkeypatch)
    assert W.default_cdm(quiet=False) is not None
    assert capsys.readouterr().out == ""


# -- an untrusted version, the Windows rename -----------------------------------------------------

@pytest.mark.parametrize("version", ["../victim", "4.10.1.0/../../victim", "/tmp/victim", "4", "4.10.1.0.5",
                                     "4.10.x.0", " 4.10.1.0", "4.10.1.0\n", "٤.10.1.0"])
def test_an_unexpected_version_never_becomes_a_path(monkeypatch, tmp_path, version):
    # S15: the version in Google's response named the directory the install moved aside and deleted
    root = tmp_path / "wv"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me")
    os.makedirs(root)
    FakeGoogle(monkeypatch, version=version)
    with pytest.raises(RuntimeError, match="unexpected version"):
        W.fetch_widevine(quiet=True)
    assert (victim / "precious.txt").read_text() == "keep me"
    assert W.default_cdm(quiet=True) is None  # the default path: fail-soft, nothing deleted either
    assert (victim / "precious.txt").read_text() == "keep me"


@pytest.mark.parametrize("version", ["4.10", "4.10.2830", "4.10.2830.0"])
def test_a_version_that_looks_like_one_is_used(monkeypatch, tmp_path, version):
    FakeGoogle(monkeypatch, version=version)
    assert W.fetch_widevine(quiet=True) == os.path.join(str(tmp_path / "wv"), version)


def test_no_version_still_installs_as_current(monkeypatch, tmp_path):
    FakeGoogle(monkeypatch, version="")
    assert W.fetch_widevine(quiet=True) == os.path.join(str(tmp_path / "wv"), "current")


def test_the_install_renames_go_through_downloads_windows_retry(monkeypatch, tmp_path):
    # S14: download._rename_dir retries while a Windows scanner holds the fresh tree; the CDM install uses it
    import importlib
    download = importlib.import_module("clearcote.download")  # the module (clearcote.download is also a function)
    real, seen = download._rename_dir, []

    def recording(src, dst):
        seen.append((os.path.basename(src)[:5], os.path.basename(dst)))
        return real(src, dst)

    monkeypatch.setattr(download, "_rename_dir", recording)
    root = str(tmp_path / "wv")
    cached_cdm_dir(root, VERSION, stamp=False, complete=False)  # an incomplete tree is moved aside first
    FakeGoogle(monkeypatch)
    W.fetch_widevine(quiet=True)
    assert [s for s, _ in seen] == [VERSION[:5], ".tmp-"]
    assert seen[0][1].startswith(".trash-") and seen[1][1] == VERSION


# -- the opt-out on cloud and Docker launches -----------------------------------------------------

@pytest.mark.parametrize("value", [False, None])
def test_cloud_accepts_and_drops_the_opt_out(value):
    # S13: the warning's advice (widevine=False) used to raise on cloud=True
    from clearcote.cloud import _prepare_launch
    _sdk, body = _prepare_launch({"widevine": value, "country": "us"})
    assert "widevine" not in body and body["country"] == "us"
    with pytest.raises(ValueError, match="^widevine is not available for cloud browsers$"):
        _prepare_launch({"widevine": True})


def test_a_cloud_launch_with_the_opt_out_gets_past_the_option_check(licensed, monkeypatch):
    from clearcote import cloud as cloud_mod
    bodies = []

    def stop(cloud, sdk):
        raise RuntimeError("stop: the options were accepted")

    real = cloud_mod._prepare_launch
    monkeypatch.setattr(cloud_mod, "_prepare_launch", lambda kw, *a, **k: bodies.append(real(kw, *a, **k)) or bodies[-1])
    monkeypatch.setattr(cloud_mod, "_client_for", stop)
    for kw in ({"widevine": False}, {"widevine": None}):
        with pytest.raises(RuntimeError, match="the options were accepted"):
            clearcote.launch(cloud=True, **kw)
        with pytest.raises(RuntimeError, match="the options were accepted"):
            clearcote.launch_persistent_context(cloud=True, profile="p", **kw)
    assert all("widevine" not in body for _sdk, body in bodies) and len(bodies) == 4


@pytest.mark.parametrize("value", [False, None])
def test_docker_accepts_and_drops_the_opt_out(value):
    from clearcote._docker import container_env
    assert container_env({"fingerprint": "17", "widevine": value}) == container_env({"fingerprint": "17"})
    with pytest.raises(ValueError, match="widevine is not available when launch"):
        container_env({"fingerprint": "17", "widevine": True})


# -- one launch's switch -------------------------------------------------------------------------

def test_launch_args_per_mode(monkeypatch, tmp_path):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    new, old = fake_engine(tmp_path, ENGINE_1022, name="new"), fake_engine(tmp_path, name="old")
    arg = "--widevine-cdm-path=" + cdm
    assert W.widevine_launch_args(new, None, True, quiet=True) == [arg]          # licensed default
    assert W.widevine_launch_args(new, None, False, quiet=True) == []            # free default: off
    assert W.widevine_launch_args(new, True, False, quiet=True) == [arg]         # asked for
    assert W.widevine_launch_args(new, False, True, quiet=True) == []            # turned off
    assert W.widevine_launch_args(old, None, True, quiet=True) == []             # no 1022: old default
    assert W.widevine_launch_args(old, True, True, quiet=True) == []             # (explicit seeds instead)
    mine = ["--widevine-cdm-path=/my/cdm"]
    assert W.widevine_launch_args(new, None, True, user_args=mine, quiet=True) == []   # the caller's own wins
    assert W.widevine_launch_args(new, True, True, user_args=mine, quiet=True) == []


def test_launch_args_off_touches_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    monkeypatch.setattr(W, "cached_cdm", lambda *a, **k: pytest.fail("cache read"))
    exe = fake_engine(tmp_path, ENGINE_1022)
    assert W.widevine_launch_args(exe, False, True) == []
    assert W.widevine_launch_args(exe, None, False) == []


def test_an_older_engine_costs_the_default_no_network(monkeypatch, tmp_path):
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    assert W.widevine_launch_args(fake_engine(tmp_path), None, True) == []


def test_cdm_host_verified(tmp_path):
    verified = fake_engine(tmp_path, ENGINE_1022, name="hv")
    unverified = fake_engine(tmp_path, ENGINE_1022_UNVERIFIED, name="nohv")
    assert W.cdm_host_verified(unverified, "linux") is True
    assert W.cdm_host_verified(verified) is True
    assert W.cdm_host_verified(unverified) is (not WIN_OR_MAC)
    assert W.cdm_host_verified(None, "win32") is False


def test_default_on_an_unverified_engine(monkeypatch, tmp_path, capsys):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    unverified = fake_engine(tmp_path, ENGINE_1022_UNVERIFIED, name="nohv2")
    arg = "--widevine-cdm-path=" + cdm
    want = [] if WIN_OR_MAC else [arg]
    assert W.widevine_launch_args(unverified, None, True) == want
    assert W.widevine_launch_args(unverified, None, True) == want
    assert capsys.readouterr().err.count("clear client id") == (1 if WIN_OR_MAC else 0)  # once per process
    assert W.widevine_launch_args(unverified, True, True, quiet=True) == [arg]           # asked for: on
    monkeypatch.setenv("CLEARCOTE_WIDEVINE", "1")
    assert W.widevine_launch_args(unverified, None, True, quiet=True) == [arg]           # env opt-in: on


def test_explicit_failure_is_unchanged_best_effort(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("x")))
    assert W.widevine_launch_args(fake_engine(tmp_path, ENGINE_1022), True, False) == []
    assert "Widevine is on by default" not in capsys.readouterr().err  # not the default's warning


# -- the launch entry points ---------------------------------------------------------------------

class _FakeBrowser:
    def on(self, *a, **k):
        pass

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self):
        pass


@pytest.fixture
def driver(monkeypatch):
    seen = {"launches": []}

    class _Chromium:
        def launch(self, **kw):
            seen["launches"].append(("incognito", None, kw))
            return _FakeBrowser()

        def launch_persistent_context(self, user_data_dir, **kw):
            seen["launches"].append(("persistent", user_data_dir, kw))
            return _FakeBrowser()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    return seen


@pytest.fixture
def licensed(monkeypatch):
    """A licence key resolves (the PRO selection) but no lease is taken: the backend is a stand-in."""
    monkeypatch.setenv("CLEARCOTE_LICENSE_KEY", KEY)
    monkeypatch.setattr(clearcote, "acquire_lease", lambda **kw: None)


def _last_args(driver):
    return driver["launches"][-1][2]["args"]


def _cdm_args(args):
    return [a for a in args if a.startswith("--widevine-cdm-path")]


def _persistent(tmp_path, exe, **kw):
    return clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, headless=True,
                                               quiet=kw.pop("quiet", True), **kw)


def test_licensed_persistent_launch_gets_widevine_by_default(driver, licensed, tmp_path):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022))
    kw = driver["launches"][-1][2]
    assert _cdm_args(kw["args"]) == ["--widevine-cdm-path=" + cdm]
    # the default only adds the switch: no profile seeding, the component updater left as Playwright has it
    assert not os.path.exists(tmp_path / "udd" / "WidevineCdm")
    assert "--disable-component-update" not in kw["ignore_default_args"]


def test_licensed_incognito_launch_gets_widevine_by_default(driver, licensed, tmp_path):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    clearcote.launch(executable_path=fake_engine(tmp_path, ENGINE_1022), headless=True, quiet=True,
                     ephemeral_profile=False)
    assert driver["launches"][-1][0] == "incognito"
    assert _cdm_args(_last_args(driver)) == ["--widevine-cdm-path=" + cdm]


def test_licensed_throwaway_profile_launch_gets_widevine_by_default(driver, licensed, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    browser = clearcote.launch(executable_path=fake_engine(tmp_path, ENGINE_1022), headless=True, quiet=True)
    assert driver["launches"][-1][0] == "persistent"
    assert _cdm_args(_last_args(driver)) == ["--widevine-cdm-path=" + cdm]
    browser.close()


@pytest.mark.parametrize("how", ["option", "env"])
def test_licensed_launch_opt_out(driver, licensed, tmp_path, monkeypatch, how):
    cached_cdm_dir(str(tmp_path / "wv"))
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    if how == "env":
        monkeypatch.setenv("CLEARCOTE_WIDEVINE", "0")
        _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022))
    else:
        _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), widevine=False)
    assert _cdm_args(_last_args(driver)) == []
    assert not os.path.exists(tmp_path / "udd" / "WidevineCdm")


def test_free_launch_stays_off(driver, tmp_path, monkeypatch):
    cached_cdm_dir(str(tmp_path / "wv"))
    monkeypatch.setattr(W, "fetch_widevine", lambda *a, **k: pytest.fail("network used"))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022))
    clearcote.launch(executable_path=fake_engine(tmp_path, ENGINE_1022, name="c2"), headless=True, quiet=True,
                     ephemeral_profile=False)
    assert all(_cdm_args(kw["args"]) == [] for _k, _u, kw in driver["launches"])


@pytest.mark.parametrize("how", ["option", "env"])
def test_free_launch_opt_in_still_seeds_and_enables(driver, tmp_path, monkeypatch, how):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    monkeypatch.setattr(W.sys, "platform", "linux")  # the hint-file route; Windows adds the fast-update scan
    if how == "env":
        monkeypatch.setenv("CLEARCOTE_WIDEVINE", "1")
        _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022))
    else:
        _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), widevine=True)
    kw = driver["launches"][-1][2]
    assert _cdm_args(kw["args"]) == ["--widevine-cdm-path=" + cdm]
    hint = tmp_path / "udd" / "WidevineCdm" / W.HINT_FILE
    assert json.loads(hint.read_text())["Path"].endswith(VERSION)
    assert "--disable-component-update" in kw["ignore_default_args"]
    assert "--enable-automation" in kw["ignore_default_args"]  # the automation strip survives


def test_licensed_launch_with_widevine_true_seeds_too(driver, licensed, tmp_path):
    cached_cdm_dir(str(tmp_path / "wv"))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), widevine=True)
    assert len(_cdm_args(_last_args(driver))) == 1
    assert (tmp_path / "udd" / "WidevineCdm" / W.HINT_FILE).exists()


def test_licensed_launch_fail_soft_offline(driver, licensed, tmp_path, monkeypatch, capsys):
    calls = []

    def offline(dest=None, quiet=False):
        calls.append(1)
        raise OSError("firewall")

    monkeypatch.setattr(W, "fetch_widevine", offline)
    exe = fake_engine(tmp_path, ENGINE_1022)
    _persistent(tmp_path, exe, quiet=False)
    _persistent(tmp_path, exe, quiet=False)
    assert len(driver["launches"]) == 2  # both launched
    assert all(_cdm_args(kw["args"]) == [] for _k, _u, kw in driver["launches"])
    assert capsys.readouterr().err.count("Widevine is on by default for licensed launches") == 1
    assert calls == [1]  # the second launch did not try again (10-minute back-off)


def test_licensed_launch_quiet_offline_says_nothing(driver, licensed, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("x")))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), quiet=True)
    assert "Widevine" not in capsys.readouterr().err


def test_licensed_launch_does_not_wait_past_the_timeout(driver, licensed, tmp_path, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "0.3")
    gate = threading.Event()
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: gate.wait(30))
    t0 = time.monotonic()
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022))
    assert time.monotonic() - t0 < 5
    assert _cdm_args(_last_args(driver)) == []
    gate.set()


def test_explicit_widevine_failure_keeps_its_old_behaviour(driver, tmp_path, monkeypatch, capsys):
    # widevine=True with no CDM to be had: the launch still goes on (it never raised), with the old note
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: (_ for _ in ()).throw(OSError("offline")))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), widevine=True, quiet=False)
    assert _cdm_args(_last_args(driver)) == []
    out = capsys.readouterr()
    assert "[widevine] setup failed (continuing without DRM)" in out.out
    assert "Widevine is on by default" not in out.err


def test_a_second_launch_does_not_download_again(driver, licensed, tmp_path, monkeypatch):
    g = FakeGoogle(monkeypatch)
    exe = fake_engine(tmp_path, ENGINE_1022)
    _persistent(tmp_path, exe)
    _persistent(tmp_path, exe)
    clearcote.launch(executable_path=exe, headless=True, quiet=True, ephemeral_profile=False)
    expected = "--widevine-cdm-path=" + os.path.join(str(tmp_path / "wv"), VERSION)
    assert all(_cdm_args(kw["args"]) == [expected] for _k, _u, kw in driver["launches"])
    assert (g.checks, g.downloads) == (1, 1)  # one update check, one download, for three launches


def test_the_callers_own_cdm_path_wins(driver, licensed, tmp_path):
    cached_cdm_dir(str(tmp_path / "wv"))
    _persistent(tmp_path, fake_engine(tmp_path, ENGINE_1022), args=["--widevine-cdm-path=/mine"])
    assert _cdm_args(_last_args(driver)) == ["--widevine-cdm-path=/mine"]


def test_prepare_for_serve_gets_the_default(licensed, tmp_path, monkeypatch):
    # serve() and serve_multiplex() build their command line with _prepare, as the launches do
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    kwargs = {"executable_path": fake_engine(tmp_path, ENGINE_1022), "quiet": True, "headless": True,
              "_cc_direct": True, "_cc_headed": False, "_cc_pro": (KEY, None)}
    _exe, args, *_rest = clearcote._prepare(kwargs)
    assert _cdm_args(args) == ["--widevine-cdm-path=" + cdm]
    kwargs = {"executable_path": fake_engine(tmp_path, ENGINE_1022, name="c2"), "quiet": True, "headless": True,
              "_cc_direct": True, "_cc_headed": False, "_cc_pro": None, "widevine": None}
    _exe, args, pw_kwargs, *_rest = clearcote._prepare(kwargs)
    assert _cdm_args(args) == []  # free: off
    assert "widevine" not in pw_kwargs  # never reaches Playwright


def test_async_launches_follow_the_same_rules(licensed, tmp_path, monkeypatch):
    cdm = cached_cdm_dir(str(tmp_path / "wv"))
    seen = []

    class _Browser:
        def on(self, *a, **k):
            pass

        async def new_page(self, **kw):
            return kw

        async def new_context(self, **kw):
            return kw

        async def close(self):
            pass

        pages = []

    class _Chromium:
        async def launch(self, **kw):
            seen.append(kw)
            return _Browser()

        async def launch_persistent_context(self, user_data_dir, **kw):
            seen.append(kw)
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def start():
        return _PW()

    async def nothing(*a, **k):
        return None

    monkeypatch.setattr(async_api, "_start_driver", start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda browser, pw: None)
    monkeypatch.setattr(async_api, "install_humanize", nothing)
    monkeypatch.setattr(async_api, "install_humanize_on_context", nothing)
    monkeypatch.setattr(async_api, "_install_window_fixup", nothing)
    exe = fake_engine(tmp_path, ENGINE_1022)
    asyncio.run(async_api.launch(executable_path=exe, headless=True, quiet=True))
    asyncio.run(async_api.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, headless=True,
                                                    quiet=True))
    asyncio.run(async_api.launch_persistent_context(str(tmp_path / "udd2"), executable_path=exe, headless=True,
                                                    quiet=True, widevine=False))
    arg = "--widevine-cdm-path=" + cdm
    assert [_cdm_args(kw["args"]) for kw in seen] == [[arg], [arg], []]


def test_a_cloud_launch_is_untouched(licensed, monkeypatch):
    called = {}
    monkeypatch.setattr(W, "default_cdm", lambda *a, **k: pytest.fail("widevine resolved for a cloud launch"))
    monkeypatch.setattr(clearcote, "launch_cloud", lambda cloud, kwargs, **kw: called.setdefault("kwargs", dict(kwargs)))
    clearcote.launch(cloud=True, headless=True)
    assert "widevine" not in called["kwargs"]  # nothing added for the hosted browser, which loads its own
    from clearcote.cloud import LOCAL_ONLY_OPTIONS
    assert "widevine" in LOCAL_ONLY_OPTIONS  # and an explicit widevine= is still refused there, as before


def test_the_auto_profile_host_probe_runs_without_widevine(monkeypatch):
    seen = {}

    def fake_launch(**kw):
        seen.update(kw)
        raise RuntimeError("stop here")

    def fake_measure(launcher, exe, major):
        launcher(headless=True)

    monkeypatch.setattr(clearcote, "launch", fake_launch)
    monkeypatch.setattr(clearcote, "measure_host", fake_measure)
    with pytest.raises(RuntimeError, match="stop here"):
        clearcote._apply_auto_profile({}, "/x/chrome", {}, quiet=True, pro=(KEY, None), lease=None)
    assert seen["widevine"] is False and seen["license_key"] == KEY
