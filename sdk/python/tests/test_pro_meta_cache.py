"""Every PRO launch used to ask GET /api/v1/download/pro before looking at the cache. The answer is now kept
for 600 s, in the process and in ~/.clearcote/pro-meta-<hash>.json, and used only while the build it names is
installed and passes the cached-build check (which re-hashes the protected files with the kept answer's
hashes). A download always follows a fresh answer, and an error answer is never kept. Served by the local
fake of the download route; HOME is a temp dir."""
import hashlib
import importlib
import json
import os
import shutil
import sys
import time

import pytest

from _fake_build import BINARY, TAG, FakeBuildServer

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / "home"
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("CLEARCOTE_RELEASE_CHANNEL", raising=False)
    monkeypatch.setattr(download, "_pro_meta_memo", {})
    return h


def kept(home):
    d = home / ".clearcote"
    return sorted(d.glob("pro-meta-*.json")) if d.is_dir() else []


def ensure(srv, cache, key="cc_lic_meta", **kw):
    return download.pro_ensure_binary(key, api_base=srv.url, cache_dir=cache, quiet=True, **kw)


def test_a_second_launch_within_the_ttl_asks_nothing(home, tmp_path):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        first = ensure(srv, cache)
        second = ensure(srv, cache)
        url = download.pro_download_url(srv.url, download.pro_platform_key())
    assert first == second and os.path.isfile(first)
    assert srv.meta_hits == 1 and srv.archive_hits == 1
    want = hashlib.sha256(f"cc_lic_meta\n{url}".encode()).hexdigest()[:16]
    assert [p.name for p in kept(home)] == [f"pro-meta-{want}.json"]
    entry = json.loads(kept(home)[0].read_text())
    assert entry["meta"]["tag"] == TAG and abs(entry["at"] - time.time()) < 60


def test_another_process_uses_the_kept_file(home, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        monkeypatch.setattr(download, "_pro_meta_memo", {})  # a new process: only the file is left
        ensure(srv, cache)
    assert srv.meta_hits == 1


def test_a_missing_build_asks_again(home, tmp_path):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        shutil.rmtree(os.path.join(cache, TAG, "browser"))
        path = ensure(srv, cache)
    assert os.path.isfile(path)
    assert srv.meta_hits == 2 and srv.archive_hits == 2  # fresh answer, then the download it names


def test_a_build_that_fails_its_check_asks_again(home, tmp_path):
    """The kept answer still drives the full cached-build check, protected-code hash included."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        exe = ensure(srv, cache)
        with open(exe, "wb") as f:
            f.write(b"y" * 64)  # same size, different code
        path = ensure(srv, cache)
    assert srv.meta_hits == 2 and srv.archive_hits == 2
    with open(path, "rb") as f:
        assert f.read() == b"x" * 64  # repaired from a fresh download


def test_an_expired_entry_asks_again(home, tmp_path, monkeypatch):
    assert download._PRO_META_TTL == 600
    monkeypatch.setattr(download, "_PRO_META_TTL", 0.3)
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        time.sleep(0.4)
        ensure(srv, cache)
        ensure(srv, cache)  # the fresh answer is kept again
    assert srv.meta_hits == 2 and srv.archive_hits == 1


def test_an_entry_from_the_future_is_a_miss(home, tmp_path, monkeypatch):
    """``at`` is epoch seconds, as all three SDKs write it: a writer using milliseconds, or a clock set back,
    must never make an entry look fresh for good."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        entry = json.loads(kept(home)[0].read_text())
        assert isinstance(entry["at"], float) and abs(entry["at"] - time.time()) < 60
        monkeypatch.setattr(download, "_pro_meta_memo", {})
        kept(home)[0].write_text(json.dumps({"at": time.time() * 1000, "meta": entry["meta"]}))
        ensure(srv, cache)
    assert srv.meta_hits == 2


def test_an_error_answer_is_never_kept(home, tmp_path):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        srv.meta_status = 403
        with pytest.raises(RuntimeError, match="not authorized"):
            ensure(srv, cache)
        assert kept(home) == [] and download._pro_meta_memo == {}
        srv.meta_status = 200
        ensure(srv, cache)
        srv.meta_status = 403  # a revoke shows up once the kept answer runs out, not before
        ensure(srv, cache)
    assert srv.meta_hits == 2


def test_a_different_pin_or_key_is_a_different_entry(home, tmp_path):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        ensure(srv, cache, version="0.0.0")
        ensure(srv, cache, version="0.0.0")
        ensure(srv, cache)
        ensure(srv, cache, key="cc_lic_other")
        ensure(srv, cache, release_channel="preview")
    assert srv.meta_hits == 4 and srv.archive_hits == 1  # no pin, the pin, another key, another channel
    assert len(kept(home)) == 4


def test_a_corrupt_file_is_a_miss(home, tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        ensure(srv, cache)
        monkeypatch.setattr(download, "_pro_meta_memo", {})
        kept(home)[0].write_text("{half a file")
        ensure(srv, cache)
        monkeypatch.setattr(download, "_pro_meta_memo", {})
        kept(home)[0].write_text(json.dumps({"at": time.time(), "meta": {"tag": TAG}}))  # no usable build in it
        ensure(srv, cache)
    assert srv.meta_hits == 3 and srv.archive_hits == 1


def test_a_kept_answer_is_never_used_to_download(home, tmp_path):
    """Its signed URL may have expired: a build that is not installed always gets a fresh answer."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer() as srv:
        url = download.pro_download_url(srv.url, download.pro_platform_key())
        key = hashlib.sha256(f"cc_lic_meta\n{url}".encode()).hexdigest()[:16]
        stale = {"tag": TAG, "version": "0.0.0", "url": "http://127.0.0.1:9/expired.zip", "sha256": "0" * 64,
                 "exe_sha256": "0" * 64, "chrome_dll_sha256": "0" * 64, "asset": "fake.zip", "archive": "zip",
                 "binary": BINARY, "size": 1}
        os.makedirs(home / ".clearcote")
        (home / ".clearcote" / f"pro-meta-{key}.json").write_text(json.dumps({"at": time.time(), "meta": stale}))
        path = ensure(srv, cache)
    assert os.path.isfile(path) and srv.meta_hits == 1 and srv.archive_hits == 1
