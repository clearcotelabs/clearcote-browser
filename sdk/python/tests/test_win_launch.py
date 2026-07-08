"""The Windows first-launch antivirus-scan race work-around (SDK 0.12.1).

A freshly-extracted, unsigned chrome.exe can fail its first launch with "spawn UNKNOWN" /
"side-by-side configuration is incorrect" while real-time AV scans chrome_elf.dll, and Windows
caches that negative activation context against the path. warm_files() pre-scans to prevent it;
_win_av_retry() re-scans + retries, then relaunches from a fresh copy (a poisoned path never
recovers in place)."""

import os

import clearcote
from clearcote import _is_win_launch_race, _win_av_retry
from clearcote.download import warm_files


def test_warm_files_reads_tree_without_error(tmp_path):
    (tmp_path / "chrome.exe").write_bytes(b"x" * 1000)
    sub = tmp_path / "locales"
    sub.mkdir()
    (sub / "en-US.pak").write_bytes(b"y" * 500)
    warm_files(str(tmp_path))  # forces an on-access AV scan; must simply not raise
    warm_files(str(tmp_path / "does-not-exist"))  # missing dir is a no-op, never raises


def test_is_win_launch_race_classifies():
    assert _is_win_launch_race(Exception("BrowserType.launch: spawn UNKNOWN"))
    assert _is_win_launch_race(Exception("The application has failed to start because its "
                                         "side-by-side configuration is incorrect."))
    assert not _is_win_launch_race(Exception("Timeout 30000ms exceeded"))
    assert not _is_win_launch_race(Exception("net::ERR_CONNECTION_REFUSED"))


def test_retry_is_passthrough_off_windows(monkeypatch):
    monkeypatch.setattr(clearcote.sys, "platform", "linux")
    calls = []

    def do(exe):
        calls.append(exe)
        return "browser"

    assert _win_av_retry(do, "/x/chrome") == "browser"
    assert calls == ["/x/chrome"]  # called exactly once, no retry machinery


def test_retry_succeeds_on_a_later_attempt(monkeypatch):
    monkeypatch.setattr(clearcote.sys, "platform", "win32")
    monkeypatch.setattr(clearcote.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(clearcote, "warm_files", lambda *_a: None)
    state = {"n": 0}

    def do(_exe):
        state["n"] += 1
        if state["n"] < 2:
            raise Exception("side-by-side configuration is incorrect")
        return "browser"

    assert _win_av_retry(do, "/x/chrome.exe") == "browser"
    assert state["n"] == 2  # failed once, retried, succeeded — no fresh-copy recovery needed


def test_retry_recovers_from_a_fresh_copy_when_path_stays_poisoned(monkeypatch, tmp_path):
    monkeypatch.setattr(clearcote.sys, "platform", "win32")
    monkeypatch.setattr(clearcote.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(clearcote, "warm_files", lambda *_a: None)
    bdir = tmp_path / "browser"
    bdir.mkdir()
    (bdir / "chrome.exe").write_bytes(b"stub")
    exe = str(bdir / "chrome.exe")

    def do(e):
        if e == exe:  # the original (poisoned) path always fails with the race
            raise Exception("BrowserType.launch: spawn UNKNOWN")
        return ("ok", e)  # any fresh path launches cleanly

    result = _win_av_retry(do, exe)
    assert result[0] == "ok"
    assert result[1] != exe  # launched from a recovered copy on a different path
    assert os.path.basename(result[1]) == "chrome.exe"
    assert os.path.exists(result[1])  # the fresh copy really exists on disk


def test_retry_reraises_non_race_errors(monkeypatch):
    monkeypatch.setattr(clearcote.sys, "platform", "win32")

    def do(_exe):
        raise Exception("Timeout 30000ms exceeded")

    try:
        _win_av_retry(do, "/x/chrome.exe")
        raise AssertionError("expected the non-race error to propagate")
    except Exception as exc:  # noqa: BLE001
        assert "Timeout" in str(exc)
