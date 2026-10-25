"""A new PRO release must not break launches that cannot download it (2026-10-24: one licence relaunched
4,048 times into the per-licence download limit after r50 shipped, with r48 on disk). A failed download of
a newer build launches the newest installed build of the same major, a 429 is remembered until its
Retry-After, and an exact revision pin never gets another build. Served by the local fake of the download
route; HOME and the browser cache are temp dirs."""
import calendar
import importlib
import json
import os
import re
import sys
import time

import pytest

from _fake_build import BINARY, FakeBuildServer, verified_tree

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

pro_platform = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")

OLD = "pro-155.0.8059.39-r48"
NEW = "pro-155.0.8059.39-r50"
OTHER_MAJOR = "pro-154.0.8037.57-r41"


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


@pytest.fixture
def srv():
    """The route answers NEW; the archive answers ``srv.archive_status`` (with ``srv.retry_after``) or the build."""
    with FakeBuildServer(tag=NEW, version="155.0.8059.39") as s:
        yield s


def ensure(srv, cache, key, **kw):
    return download.pro_ensure_binary(key, api_base=srv.url, cache_dir=cache, quiet=True, **kw)


def backoff(cache):
    with open(os.path.join(cache, f".download-backoff-{NEW}.json"), encoding="utf-8") as f:
        return json.load(f)


def left_ms(cache):
    return backoff(cache)["until"] - time.time() * 1000


def test_retry_after_reads_delta_seconds_and_http_dates_and_nothing_else():
    now = calendar.timegm(time.strptime("2026-10-24T10:00:00", "%Y-%m-%dT%H:%M:%S"))
    assert download._retry_after_seconds("86400", now) == 86400
    assert download._retry_after_seconds("Sat, 24 Oct 2026 10:01:30 GMT", now) == 90
    assert download._retry_after_seconds("Sat, 24 Oct 2026 09:00:00 GMT", now) == 0
    assert download._retry_after_seconds("", now) is None
    assert download._retry_after_seconds("soon", now) is None
    assert download._retry_after_seconds(None, now) is None


@pro_platform
def test_a_429_launches_the_installed_older_build_and_pauses_until_retry_after(home, tmp_path, srv, capsys):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "3600"
    old = verified_tree(os.path.join(cache, OLD))

    assert ensure(srv, cache, "cc_lic_fb1") == old
    assert srv.archive_hits == 1
    bo = backoff(cache)
    assert bo["status"] == 429 and isinstance(bo["until"], int)
    assert 3_500_000 < left_ms(cache) <= 3_600_000
    assert f"launching the installed {OLD} instead" in capsys.readouterr().err  # said even when quiet

    # The next launches skip the download altogether while the pause lasts.
    assert ensure(srv, cache, "cc_lic_fb1") == old
    assert ensure(srv, cache, "cc_lic_fb1") == old
    assert srv.archive_hits == 1


@pro_platform
def test_once_the_pause_is_over_the_new_build_is_installed_and_the_pause_file_removed(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "3600"
    verified_tree(os.path.join(cache, OLD))
    ensure(srv, cache, "cc_lic_fb2")

    f = os.path.join(cache, f".download-backoff-{NEW}.json")
    expired = {**backoff(cache), "until": int(time.time() * 1000) - 1}
    with open(f, "w", encoding="utf-8") as fh:
        json.dump(expired, fh)
    srv.archive_status = 200
    assert ensure(srv, cache, "cc_lic_fb2") == os.path.join(cache, NEW, "browser", BINARY)
    assert srv.archive_hits == 2
    assert not os.path.exists(f)


@pro_platform
def test_a_429_with_nothing_installed_fails_then_fails_fast_while_paused(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "600"
    with pytest.raises(download.DownloadHttpError, match=r"(?s)HTTP 429.*CLEARCOTE_CACHE") as e:
        ensure(srv, cache, "cc_lic_fb3")
    assert e.value.status == 429 and e.value.retry_after_sec == 600
    with pytest.raises(RuntimeError, match="download is paused until"):
        ensure(srv, cache, "cc_lic_fb3")
    assert srv.archive_hits == 1


@pro_platform
def test_a_429_without_retry_after_pauses_for_60_seconds(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status = 429
    verified_tree(os.path.join(cache, OLD))
    ensure(srv, cache, "cc_lic_fb4")
    assert 50_000 < left_ms(cache) <= 60_000


@pro_platform
def test_any_other_failure_falls_back_with_a_5_minute_pause_and_without_one_nothing_is_paused(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status = 503
    with pytest.raises(RuntimeError, match="HTTP 503"):
        ensure(srv, cache, "cc_lic_fb5")
    assert not os.path.exists(os.path.join(cache, f".download-backoff-{NEW}.json"))  # every launch keeps trying

    old = verified_tree(os.path.join(cache, OLD))
    assert ensure(srv, cache, "cc_lic_fb5") == old
    assert 290_000 < left_ms(cache) <= 300_000
    assert backoff(cache)["status"] == 503


@pro_platform
def test_an_exact_revision_pin_never_gets_another_build(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "3600"
    verified_tree(os.path.join(cache, OLD))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        ensure(srv, cache, "cc_lic_fb6", version="r50")


@pro_platform
def test_never_another_major_and_the_newest_revision_of_the_same_one(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "3600"
    verified_tree(os.path.join(cache, OTHER_MAJOR))
    with pytest.raises(RuntimeError, match="HTTP 429"):
        ensure(srv, cache, "cc_lic_fb7")

    verified_tree(os.path.join(cache, "pro-155.0.8059.39-r42"))
    r48 = verified_tree(os.path.join(cache, OLD))
    assert ensure(srv, cache, "cc_lic_fb7") == r48


@pro_platform
def test_a_damaged_older_build_is_not_a_fall_back(home, tmp_path, srv):
    cache = str(tmp_path / "cache")
    srv.archive_status, srv.retry_after = 429, "3600"
    old = verified_tree(os.path.join(cache, OLD))
    with open(old, "wb") as f:
        f.write(b"y" * 3)  # truncated: fails the manifest's size check
    with pytest.raises(RuntimeError, match="HTTP 429"):
        ensure(srv, cache, "cc_lic_fb8")
    assert "browser" in os.listdir(os.path.join(cache, OLD))  # left alone, never deleted


@pro_platform
def test_the_archive_download_says_which_sdk_fetched_it(home, tmp_path, srv):
    from clearcote import __version__

    cache = str(tmp_path / "cache")
    ensure(srv, cache, "cc_lic_fb9")
    assert os.path.isfile(os.path.join(cache, NEW, "browser", BINARY))
    assert re.fullmatch(r"clearcote-sdk/\d+\.\d+\.\d+ \(python\)", srv.user_agents[0])
    assert srv.user_agents[0] == download._download_user_agent() == f"clearcote-sdk/{__version__} (python)"


@pro_platform
def test_the_download_route_is_asked_with_the_sdk_version(home, tmp_path, srv, capsys):
    # Since 156-r51 a build can carry a minimum client version; the route holds back a client that
    # names no version (every SDK before 0.63 sent plain "clearcote-sdk" here), so the version must go.
    from clearcote import __version__

    srv.meta_extra = {"notice": "Clearcote 156-r51 needs a newer client; you were given 155-r50 instead."}
    download.pro_ensure_binary("cc_lic_fb10", api_base=srv.url, cache_dir=str(tmp_path / "cache"), quiet=False)
    assert srv.meta_user_agents[0] == f"clearcote-sdk-python/{__version__}"
    assert re.fullmatch(r"clearcote-sdk-python/\d+\.\d+\.\d+.*", srv.meta_user_agents[0])
    assert "needs a newer client" in capsys.readouterr().err
