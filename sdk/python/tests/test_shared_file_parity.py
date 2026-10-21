"""Node/Python parity on the files the SDKs share under ~/.clearcote, and on the profile="auto" path.

* host.json: Node writes ``_at`` as an ISO time, this SDK as epoch seconds. Each must read the other.
* local-index.json: Node keys it on ``mtimeMs``; this SDK used to write ``mtime``, so each re-indexed after
  the other.
* Converted chrome-fingerprints records carry only UA-CH (``navigator.uadata``) and were skipped.
* profile="auto" on an engine below 150 falls back to the seed, as Node does.
* pro-meta-*.json and lease-*.json are written whole and renamed into place, with a Windows retry.
"""
import importlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

import clearcote
import clearcote._license as L
from clearcote import _profileauto, _profilesource
from clearcote._profileimport import index_entry_from_profile

download = importlib.import_module("clearcote.download")


@pytest.fixture
def profile_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARCOTE_PROFILE_DIR", str(tmp_path / "profiles"))
    return tmp_path


# ── host.json: both spellings of _at ────────────────────────────────────────────────────────────────

EXE = os.path.join("some", "engine", "chrome")
FACTS = {"os_family": "windows", "browser_major": 150, "gpu_vendor": "intel"}


def _write_host(at):
    with open(_profilesource._host_cache_path(), "w", encoding="utf-8") as f:
        json.dump({**FACTS, "_exe": _profilesource._exe_key(EXE), "_at": at}, f)


def test_host_cache_written_by_node_with_an_iso_time_is_reused(profile_dir):
    _write_host(datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    assert _profilesource.read_cached_host(EXE)["browser_major"] == 150


def test_host_cache_with_an_old_iso_time_is_a_miss(profile_dir):
    _write_host((datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace("+00:00", "Z"))
    assert _profilesource.read_cached_host(EXE) is None


@pytest.mark.parametrize("at", ["not a time", None, [], {}, True, ""])
def test_host_cache_with_an_unreadable_time_is_a_miss_not_an_error(profile_dir, at):
    _write_host(at)
    assert _profilesource.read_cached_host(EXE) is None


def test_host_cache_round_trips_epoch_seconds(profile_dir):
    _profilesource.write_cached_host(EXE, FACTS)
    with open(_profilesource._host_cache_path(), encoding="utf-8") as f:
        assert isinstance(json.load(f)["_at"], float)  # .NET reads this spelling too
    assert _profilesource.read_cached_host(EXE)["gpu_vendor"] == "intel"


# ── profile="auto" on an engine that cannot apply a profile ─────────────────────────────────────────

@pytest.fixture
def old_engine(monkeypatch):
    calls = {"resolve": 0}
    monkeypatch.setattr(clearcote, "measure_host", lambda launch_fn, exe, major: {**FACTS, "browser_major": 149})

    def fake_resolve(host, **kw):
        calls["resolve"] += 1
        return {"profile": {"navigator": {}}, "selection": {}, "source": "service"}

    monkeypatch.setattr(clearcote, "resolve_auto", fake_resolve)
    return calls


def test_auto_on_engine_149_falls_back_to_the_sticky_seed(old_engine, capsys):
    fp = {}
    clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=False)
    assert "fingerprint_profile" not in fp
    assert fp["fingerprint"] == clearcote.default_sticky_key()
    assert old_engine["resolve"] == 0  # no profile is spent on an engine that would drop it
    err = capsys.readouterr().err
    assert "engine 149 does not support imported profiles" in err and "150" in err


def test_auto_on_engine_149_keeps_a_caller_seed_and_quiet_says_nothing(old_engine, capsys):
    fp = {"fingerprint": "mine"}
    clearcote._apply_auto_profile(fp, "/path/chrome", {}, quiet=True)
    assert fp == {"fingerprint": "mine"}
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("major,ok", [(150, True), (151, True), (149, False), (None, False), ("x", False)])
def test_engine_supports_profiles(major, ok):
    assert clearcote.engine_supports_profiles(major) is ok


# ── UA-CH-only records (converted chrome-fingerprints) ──────────────────────────────────────────────

@pytest.mark.parametrize("uadata,os_family", [
    ({"platform": "Windows"}, "windows"),
    ({"high_entropy": {"platform": "macOS"}}, "macos"),
    ({"platform": "Linux"}, "linux"),
    ({"platform": "Chrome OS"}, "linux"),
    ({"platform": "Android"}, "android"),
    ({"platform": "Linux", "mobile": True}, "android"),
    ({"high_entropy": {"platform": "Linux", "mobile": True}}, "android"),
])
def test_ua_ch_only_records_are_classified(uadata, os_family):
    profile = {"navigator": {"user_agent": None, "platform": None, "uadata": uadata}, "screen": {"width": 1920}}
    entry = index_entry_from_profile("p1", profile)
    assert entry is not None and entry["os_family"] == os_family
    assert entry["browser_major"] is None  # no version: inherits the engine's


def test_a_record_with_nothing_to_classify_is_still_skipped():
    assert index_entry_from_profile("p1", {"navigator": {"user_agent": None, "uadata": {}}}) is None


# ── local-index.json: Node's mtimeMs key ────────────────────────────────────────────────────────────

def _local_dir(tmp_path):
    d = tmp_path / "imported"
    d.mkdir()
    (d / "a.json").write_text(json.dumps({"navigator": {"platform": "Win32"}}))
    return str(d)


def _counting_import(monkeypatch):
    seen = []
    real = _profileauto.import_directory
    monkeypatch.setattr(_profileauto, "import_directory", lambda d: seen.append(d) or real(d))
    return seen


def test_local_index_writes_node_mtime_ms_key(profile_dir, monkeypatch):
    d = _local_dir(profile_dir)
    _profileauto.load_local_index(d)
    with open(_profileauto._local_index_cache_path(), encoding="utf-8") as f:
        c = json.load(f)
    assert "mtime" not in c
    assert abs(c["mtimeMs"] - os.stat(d).st_mtime * 1000) < 1


def test_local_index_reuses_a_node_written_cache(profile_dir, monkeypatch):
    d = _local_dir(profile_dir)
    # As Node writes it: statSync(dir).mtimeMs, which can differ from ours in the last float bits.
    node_ms = os.stat(d).st_mtime_ns / 1e6 + 1e-7
    index = [{"id": "from-node", "os_family": "windows"}]
    with open(_profileauto._local_index_cache_path(), "w", encoding="utf-8") as f:
        json.dump({"dir": d, "mtimeMs": node_ms, "index": index}, f)
    seen = _counting_import(monkeypatch)
    assert _profileauto.load_local_index(d) == index
    assert seen == []


def test_local_index_reuses_an_older_python_cache(profile_dir, monkeypatch):
    d = _local_dir(profile_dir)
    index = [{"id": "from-old-python", "os_family": "windows"}]
    with open(_profileauto._local_index_cache_path(), "w", encoding="utf-8") as f:
        json.dump({"dir": d, "mtime": os.stat(d).st_mtime, "index": index}, f)
    seen = _counting_import(monkeypatch)
    assert _profileauto.load_local_index(d) == index
    assert seen == []


def test_local_index_reindexes_when_the_directory_changed(profile_dir, monkeypatch):
    d = _local_dir(profile_dir)
    with open(_profileauto._local_index_cache_path(), "w", encoding="utf-8") as f:
        json.dump({"dir": d, "mtimeMs": 1.0, "index": []}, f)
    seen = _counting_import(monkeypatch)
    assert [e["id"] for e in _profileauto.load_local_index(d)] == ["a"]
    assert seen == [d]


# ── atomic writes: pro-meta-*.json and lease-*.json ─────────────────────────────────────────────────

@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(download, "_pro_meta_memo", {})
    return tmp_path


def _flaky_replace(monkeypatch, module, fails=2):
    """os.replace as on Windows while a reader holds the target open: PermissionError, then success."""
    real = os.replace
    calls = []

    def replace(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) <= fails:
            raise PermissionError(13, "in use")
        return real(src, dst)

    monkeypatch.setattr(module.os, "replace", replace)
    monkeypatch.setattr(module.time, "sleep", lambda s: None)
    return calls


def _leftovers(d):
    return [p.name for p in d.iterdir() if p.name.endswith(".tmp")]


def test_pro_meta_is_written_by_rename_with_the_windows_retry(home, monkeypatch):
    calls = _flaky_replace(monkeypatch, download)
    download._keep_pro_meta("k1", {"tag": "pro-1"})
    path = download._pro_meta_path("k1")
    assert len(calls) == 3 and all(dst == path for _src, dst in calls)
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["meta"] == {"tag": "pro-1"}
    assert _leftovers(home / ".clearcote") == []


def test_pro_meta_write_never_leaves_a_tmp_file_when_the_rename_keeps_failing(home, monkeypatch):
    _flaky_replace(monkeypatch, download, fails=100)
    download._keep_pro_meta("k2", {"tag": "pro-2"})  # must not raise: this process still has it
    assert not os.path.exists(download._pro_meta_path("k2"))
    assert _leftovers(home / ".clearcote") == []
    assert download._kept_pro_meta("k2") == {"tag": "pro-2"}


def test_lease_cache_is_written_by_rename_with_the_windows_retry(home, monkeypatch):
    calls = _flaky_replace(monkeypatch, L)
    L._write_cache("cc_lic_ATOMIC", "TOK", time.time() + 600, "L1")
    path = L._cache_path("cc_lic_ATOMIC")
    assert len(calls) == 3 and all(dst == str(path) for _src, dst in calls)
    assert json.loads(path.read_text()) == {"token": "TOK", "exp": pytest.approx(time.time() + 600, abs=5),
                                            "lease_id": "L1", "scope": "machine"}
    assert _leftovers(path.parent) == []
