"""A burst of launches from one machine used to send one request per launch to the site (www), whose host
answers a burst from one address with a 403 challenge page and keeps challenging that address for minutes.
With a PRO licence the challenge on GET /api/v1/download/pro failed the launch ("PRO download not
authorized (HTTP 403)"), so a customer could not start more than ~20 browsers on one server (2026-11-24).

Now: one request per machine at a time (the others wait for the answer it keeps), and a challenge, a 5xx or
no connection launches the installed build the last answer named. A licence verdict (the route's own JSON
401/403) still fails the launch. HOME is a temp dir; the PRO route is the local fake.
"""
import importlib
import json
import os
import sys
import threading
import time
import urllib.error

import pytest

from _fake_build import FakeBuildServer

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

LIVE = {"schema": 1, "builds": [{"major": 999, "version": "999.0.1.2", "tier": "free", "platforms": {}}]}


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("CLEARCOTE_RELEASE_CHANNEL", raising=False)
    monkeypatch.setattr(download, "_catalog_memo", {})
    monkeypatch.setattr(download, "_catalog_spread", 0.0)
    monkeypatch.setattr(download, "_pro_meta_memo", {})
    monkeypatch.setattr(download, "_pro_meta_spread", 0.0)
    return h


def together(n, fn):
    """Run fn n times at once (threads take the same per-machine lock processes do): results or exceptions."""
    out = [None] * n
    gate = threading.Barrier(n)

    def run(i):
        gate.wait(10)
        try:
            out[i] = fn()
        except Exception as e:  # noqa: BLE001
            out[i] = e

    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(90)
    return out


# ── catalog ──────────────────────────────────────────────────────────────────────────────────────────────

def test_a_burst_with_nothing_kept_asks_the_catalog_once(home, monkeypatch):
    hits = []

    def site(url):
        hits.append(url)
        time.sleep(0.3)  # the holder is still asking while the others arrive
        return json.dumps(LIVE).encode()

    monkeypatch.setattr(download, "_http_get", site)
    assert together(20, lambda: download._fetch_catalog(quiet=True)) == [LIVE] * 20
    assert len(hits) == 1
    assert not (home / ".clearcote" / "catalog.lock").exists()


def test_a_failed_catalog_request_is_not_repeated_by_the_others(home, monkeypatch):
    from clearcote.release import CATALOG_FALLBACK

    hits = []

    def site(url):
        hits.append(url)
        time.sleep(0.3)
        raise urllib.error.HTTPError(url, 403, "Forbidden", None, None)

    monkeypatch.setattr(download, "_http_get", site)
    assert all(r is CATALOG_FALLBACK for r in together(20, lambda: download._fetch_catalog(quiet=True)))
    assert len(hits) == 1


def test_a_failure_older_than_the_quiet_time_is_asked_again(home, monkeypatch):
    hits = []
    monkeypatch.setattr(download, "_http_get", lambda url: hits.append(url) or json.dumps(LIVE).encode())
    (home / ".clearcote").mkdir(parents=True)
    failed = home / ".clearcote" / "catalog.failed"
    failed.write_text("")
    old = time.time() - download._FLIGHT_FAILED_QUIET - 1
    os.utime(failed, (old, old))
    assert download._fetch_catalog(quiet=True) == LIVE
    assert len(hits) == 1 and not failed.exists()


def test_a_lock_left_by_a_dead_process_is_taken_over(home, monkeypatch):
    hits = []
    monkeypatch.setattr(download, "_http_get", lambda url: hits.append(url) or json.dumps(LIVE).encode())
    (home / ".clearcote").mkdir(parents=True)
    lock = home / ".clearcote" / "catalog.lock"
    lock.write_text("12345")
    old = time.time() - download._FLIGHT_STALE - 1
    os.utime(lock, (old, old))
    t0 = time.monotonic()
    assert download._fetch_catalog(quiet=True) == LIVE
    assert len(hits) == 1 and time.monotonic() - t0 < 5


def test_a_stuck_holder_is_waited_for_at_most_the_flight_wait(home, monkeypatch):
    hits = []
    monkeypatch.setattr(download, "_http_get", lambda url: hits.append(url) or json.dumps(LIVE).encode())
    monkeypatch.setattr(download, "_FLIGHT_WAIT", 0.5)
    (home / ".clearcote").mkdir(parents=True)
    (home / ".clearcote" / "catalog.lock").write_text("12345")  # live: just made
    assert download._fetch_catalog(quiet=True) == LIVE
    assert len(hits) == 1


# ── PRO build lookup ─────────────────────────────────────────────────────────────────────────────────────

pro = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")
pytestmark = pytest.mark.usefixtures("pro_test_key")  # the fake route signs its answers with the test key


def ensure(srv, cache, **kw):
    return download.pro_ensure_binary("cc_lic_burst", api_base=srv.url, cache_dir=cache, quiet=True, **kw)


def expire_kept(home, seconds):
    for p in (home / ".clearcote").glob("pro-meta-*.json"):
        e = json.loads(p.read_text())
        e["at"] -= seconds
        p.write_text(json.dumps(e))


def new_process(monkeypatch):
    monkeypatch.setattr(download, "_pro_meta_memo", {})  # only the file is left


@pro
def test_a_burst_of_pro_launches_asks_once(home, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        first = ensure(srv, cache)  # installs the build
        expire_kept(home, 3600)
        new_process(monkeypatch)
        got = together(20, lambda: ensure(srv, cache))
    assert got == [first] * 20
    assert srv.meta_hits == 2  # the install, then one for the whole burst


@pro
def test_a_challenge_launches_the_installed_build_the_last_answer_named(home, tmp_path, monkeypatch, capsys):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        first = ensure(srv, cache)
        expire_kept(home, 3600)
        new_process(monkeypatch)
        srv.meta_challenge = True
        got = together(10, lambda: ensure(srv, cache))
    assert got == [first] * 10
    assert srv.meta_hits == 2  # the others did not ask after the challenge
    err = capsys.readouterr().err
    assert err.count("PRO build lookup") == 1 and "challenge page" in err


@pro
def test_a_challenge_with_no_installed_build_fails_with_a_clear_message(home, tmp_path):
    with FakeBuildServer() as srv:
        srv.meta_challenge = True
        with pytest.raises(RuntimeError, match=r"build lookup .* failed .*challenge page.*Try again"):
            ensure(srv, str(tmp_path / "cache"))


@pro
def test_an_answer_older_than_a_week_is_not_used(home, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        expire_kept(home, download._PRO_META_STALE_MAX + 1)
        new_process(monkeypatch)
        srv.meta_challenge = True
        with pytest.raises(RuntimeError, match="no earlier answer names an installed build"):
            ensure(srv, cache)


@pro
@pytest.mark.parametrize("status", [401, 403])
def test_a_licence_verdict_still_fails_the_launch(home, tmp_path, monkeypatch, status):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        expire_kept(home, 3600)
        new_process(monkeypatch)
        srv.meta_status = status
        got = together(5, lambda: ensure(srv, cache))
    assert all(isinstance(r, RuntimeError) and "not authorized" in str(r) for r in got)
    assert not list((home / ".clearcote").glob("*.failed"))  # a verdict is not a site failure


@pro
def test_a_server_error_launches_the_installed_build(home, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        first = ensure(srv, cache)
        expire_kept(home, 3600)
        new_process(monkeypatch)
        srv.meta_status = 503
        assert ensure(srv, cache) == first
