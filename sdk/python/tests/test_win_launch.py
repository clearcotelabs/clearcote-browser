"""Windows: launching a cached build that Windows refuses to start in place.

chrome.exe can fail to start with "spawn UNKNOWN" / "side-by-side configuration is incorrect": inside
an MSIX-packaged app a build downloaded into %LOCALAPPDATA% is invisible to the activation-context
check, and real-time antivirus scanning a freshly extracted chrome_elf.dll causes the same error for a
while. _win_av_retry() probes the cached exe (suspended CreateProcess), re-scans + backs off, and then
launches from one recovered copy per build in ~/.clearcote/recovered, which later launches reuse.

The launch must leave the temp directory as it found it: the old fallback copied the whole browser to
a fresh %TEMP%\\clearcote-recover-* on every launch and never deleted it, and every failed Playwright
attempt leaked a playwright_chromiumdev_profile-* and a playwright-artifacts-* directory."""

import asyncio
import hashlib
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

import clearcote
from clearcote import _commands, _is_win_launch_race, _launch_attempt, _win_av_retry, _winlaunch
from clearcote import async_api
from clearcote.download import warm_files

SXS = OSError(22, "[WinError 14001] The application has failed to start because its side-by-side "
                  "configuration is incorrect")


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


@pytest.fixture
def win(monkeypatch, tmp_path):
    """A fake Windows host: HOME and the temp directory under tmp_path, no sleeping or warming, a
    cached build, and a spawn probe whose answer the test sets (state["probe"])."""
    monkeypatch.setattr(clearcote.sys, "platform", "win32")
    monkeypatch.setattr(clearcote.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(clearcote, "warm_files", lambda *_a: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    monkeypatch.setenv("TEMP", str(tmp))
    bdir = tmp_path / "cache" / "pro-1.2.3-r9" / "browser"
    bdir.mkdir(parents=True)
    (bdir / "chrome.exe").write_bytes(b"stub")
    (bdir / "1.2.3.manifest").write_text("<assembly/>")
    state = {"probe": None, "probes": 0, "copies": 0}

    def probe(_exe):
        state["probes"] += 1
        return state["probe"]

    monkeypatch.setattr(_winlaunch, "spawn_error", probe)
    real_copytree = _winlaunch.shutil.copytree

    def counting_copytree(*a, **kw):
        state["copies"] += 1
        return real_copytree(*a, **kw)

    monkeypatch.setattr(_winlaunch.shutil, "copytree", counting_copytree)
    return SimpleNamespace(exe=str(bdir / "chrome.exe"), home=home, tmp=tmp, state=state,
                           root=home / ".clearcote" / "recovered")


def test_launches_in_place_when_the_probe_passes(win):
    calls = []
    assert _win_av_retry(lambda e: calls.append(e) or "browser", win.exe) == "browser"
    assert calls == [win.exe]
    assert win.state["copies"] == 0 and not win.root.exists()


def test_retry_succeeds_on_a_later_attempt(win):
    state = {"n": 0}

    def do(_exe):
        state["n"] += 1
        if state["n"] < 2:
            raise Exception("side-by-side configuration is incorrect")
        return "browser"

    assert _win_av_retry(do, win.exe) == "browser"
    assert state["n"] == 2  # failed once, retried, succeeded: no recovered copy needed
    assert win.state["copies"] == 0


def test_unlaunchable_build_is_recovered_once_then_reused(win):
    """The probe fails, so Playwright is never pointed at the cached exe (each failed Playwright
    attempt used to leak two temp directories); the copy goes to ~/.clearcote/recovered, never to
    the temp directory, and the next launch uses it straight away."""
    win.state["probe"] = SXS
    calls = []
    first = _win_av_retry(lambda e: calls.append(e) or e, win.exe)
    key = _winlaunch.recovery_key(win.exe)
    assert first == str(win.root / key / "browser" / "chrome.exe")
    assert calls == [first]  # the cached exe itself was never launched
    assert win.state["probes"] == 3 and win.state["copies"] == 1
    assert (win.root / key / _winlaunch.MARKER).is_file()
    assert (win.root / key / "browser" / "1.2.3.manifest").is_file()  # the whole build came along
    assert os.listdir(win.tmp) == []  # nothing in the temp directory

    second = _win_av_retry(lambda e: calls.append(e) or e, win.exe)
    assert second == first
    assert win.state["probes"] == 3 and win.state["copies"] == 1  # no probe, no new copy
    assert sorted(os.listdir(win.root)) == [key]


def test_a_rebuilt_build_gets_a_fresh_copy_and_the_stale_one_goes(win):
    win.state["probe"] = SXS
    first = _win_av_retry(lambda e: e, win.exe)
    old_key = _winlaunch.recovery_key(win.exe)
    with open(win.exe, "wb") as f:
        f.write(b"rebuilt stub")  # same path, new build
    second = _win_av_retry(lambda e: e, win.exe)
    new_key = _winlaunch.recovery_key(win.exe)
    assert new_key != old_key and second != first
    assert sorted(os.listdir(win.root)) == [new_key]


def test_a_copy_whose_build_is_gone_is_swept(win):
    win.state["probe"] = SXS
    _win_av_retry(lambda e: e, win.exe)
    old_key = _winlaunch.recovery_key(win.exe)
    other = win.home / "cache2" / "pro-1.2.4-r10" / "browser"
    other.mkdir(parents=True)
    (other / "chrome.exe").write_bytes(b"next release")
    os.remove(win.exe)  # clear-cache / a new release replaced the old build
    _win_av_retry(lambda e: e, str(other / "chrome.exe"))
    assert old_key not in os.listdir(win.root)


def test_a_copy_in_use_is_never_deleted(win, monkeypatch):
    """Windows refuses to delete an exe a process runs from; that refusal is what keeps a running
    browser's copy (renaming its directory would succeed and prove nothing)."""
    win.state["probe"] = SXS
    copy_exe = _win_av_retry(lambda e: e, win.exe)
    copy_dir = os.path.dirname(os.path.dirname(copy_exe))
    real_remove = os.remove

    def remove(p, *a, **kw):
        if os.path.normcase(p) == os.path.normcase(copy_exe):
            raise PermissionError(13, "Access is denied", p)
        return real_remove(p, *a, **kw)

    monkeypatch.setattr(_winlaunch.os, "remove", remove)
    assert _winlaunch.discard_copy(copy_dir) is False
    assert os.path.isfile(copy_exe) and os.path.isfile(os.path.join(copy_dir, _winlaunch.MARKER))


@pytest.mark.skipif(os.name != "nt", reason="needs Windows' rule that a running exe cannot be deleted")
def test_a_copy_a_process_runs_from_is_kept_on_real_windows(tmp_path):
    import shutil
    import subprocess

    browser = tmp_path / "copy" / "browser"
    browser.mkdir(parents=True)
    running = browser / "chrome.exe"
    shutil.copyfile(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "PING.EXE"), running)
    proc = subprocess.Popen([str(running), "-n", "30", "127.0.0.1"], stdout=subprocess.DEVNULL,
                            creationflags=0x08000000)  # CREATE_NO_WINDOW
    try:
        assert _winlaunch.discard_copy(str(tmp_path / "copy")) is False
        assert running.is_file()
    finally:
        proc.kill()
        proc.wait()
    time.sleep(0.2)
    assert _winlaunch.discard_copy(str(tmp_path / "copy")) is True


def test_old_sdks_temp_copies_are_swept(win):
    old = win.tmp / "clearcote-recover-old" / "browser"
    old.mkdir(parents=True)
    (old / "chrome.exe").write_bytes(b"x")
    two_min_ago = time.time() - 120
    os.utime(old.parent, (two_min_ago, two_min_ago))
    fresh = win.tmp / "clearcote-recover-new"  # possibly a concurrent old-SDK launch mid-copy
    fresh.mkdir()
    win.state["probe"] = SXS
    _win_av_retry(lambda e: e, win.exe)
    assert sorted(os.listdir(win.tmp)) == ["clearcote-recover-new"]


def test_recovery_key_contract(tmp_path):
    """Node computes the same key (winlaunch.ts recoveryKey), so both SDKs share one copy."""
    b = tmp_path / "pro-9.9.9-r1" / "browser"
    b.mkdir(parents=True)
    exe = b / "chrome.exe"
    exe.write_bytes(b"12345")
    os.utime(exe, ns=(1_700_000_000_123_456_789, 1_700_000_000_123_456_789))
    ident = "%s|5|1700000000123" % os.path.normcase(str(b))
    assert _winlaunch.recovery_key(str(exe)) == \
        "pro-9.9.9-r1-" + hashlib.sha256(ident.encode("utf-8")).hexdigest()[:12]


def _launch_error(profile=None):
    udd = profile or r"C:\Users\me\AppData\Local\Temp\clearcote-run-abc"
    return Exception("BrowserType.launch: spawn UNKNOWN\nCall log:\n  - <launching> C:\\c\\chrome.exe "
                     "--disable-field-trial-config --user-data-dir=%s --remote-debugging-pipe about:blank" % udd)


def _mk(d, name):
    p = d / name
    p.mkdir()
    time.sleep(0.03)  # distinct creation times
    return p


def test_failed_launch_leaves_no_temp_dirs(tmp_path):
    """launch(): the profile named in the call log, and the empty artifacts dir made just before it."""
    older = _mk(tmp_path, "playwright-artifacts-old111")
    started = time.time()
    time.sleep(0.03)
    ours = _mk(tmp_path, "playwright-artifacts-ours22")
    prof = _mk(tmp_path, "playwright_chromiumdev_profile-abcDEF")
    later = _mk(tmp_path, "playwright-artifacts-later3")  # another launch, after our profile
    busy = _mk(tmp_path, "playwright-artifacts-busy44")
    (busy / "download.bin").write_bytes(b"x")
    _winlaunch.remove_failed_launch_dirs(_launch_error(str(prof)), started)
    assert sorted(os.listdir(tmp_path)) == sorted([older.name, later.name, busy.name])


def test_failed_persistent_launch_removes_only_its_artifacts_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    caller_profile = _mk(tmp_path, "my-profile")
    started = time.time()
    time.sleep(0.03)
    _mk(tmp_path, "playwright-artifacts-ours22")
    _winlaunch.remove_failed_launch_dirs(_launch_error(str(caller_profile)), started)
    assert os.listdir(tmp_path) == ["my-profile"]  # the caller's profile is never touched


def test_ambiguous_failure_removes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))
    started = time.time()
    time.sleep(0.03)
    _mk(tmp_path, "playwright-artifacts-aaaaaa")
    _mk(tmp_path, "playwright-artifacts-bbbbbb")  # a concurrent launch: cannot tell which is ours
    _winlaunch.remove_failed_launch_dirs(_launch_error(), started)
    assert len(os.listdir(tmp_path)) == 2


def test_cleanup_needs_a_playwright_call_log(tmp_path, monkeypatch):
    """A raw spawn (serve) never made Playwright directories: nothing of anyone else's goes."""
    monkeypatch.setenv("TEMP", str(tmp_path))
    started = time.time()
    time.sleep(0.03)
    _mk(tmp_path, "playwright-artifacts-aaaaaa")
    _winlaunch.remove_failed_launch_dirs(SXS, started)
    assert len(os.listdir(tmp_path)) == 1


def test_each_failed_attempt_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TEMP", str(tmp_path))

    def do(_exe):  # what Playwright does when Windows cannot start the process
        (tmp_path / ("playwright-artifacts-%06d" % len(os.listdir(tmp_path)))).mkdir()
        prof = tmp_path / ("playwright_chromiumdev_profile-%06d" % len(os.listdir(tmp_path)))
        prof.mkdir()
        raise _launch_error(str(prof))

    with pytest.raises(Exception, match="spawn UNKNOWN"):
        _launch_attempt(do, "C:/c/chrome.exe")
    assert os.listdir(tmp_path) == []


def test_retry_reraises_non_race_errors(win):
    def do(_exe):
        raise Exception("Timeout 30000ms exceeded")

    with pytest.raises(Exception, match="Timeout"):
        _win_av_retry(do, win.exe)


def test_async_retry_recovers_once_then_reuses(win, monkeypatch):
    async def nosleep(*_a):
        return None

    monkeypatch.setattr(async_api, "warm_files", lambda *_a: None)
    monkeypatch.setattr(async_api.asyncio, "sleep", nosleep)
    win.state["probe"] = SXS
    calls = []

    async def do(e):
        calls.append(e)
        return e

    first = asyncio.run(async_api._win_av_retry_async(do, win.exe))
    second = asyncio.run(async_api._win_av_retry_async(do, win.exe))
    assert first == second == calls[0] == calls[1] != win.exe
    assert win.state["copies"] == 1 and os.listdir(win.tmp) == []


def test_clear_cache_removes_recovered_copies(win, monkeypatch, capsys):
    monkeypatch.setenv("CLEARCOTE_CACHE", str(win.home / "no-cache"))
    win.state["probe"] = SXS
    _win_av_retry(lambda e: e, win.exe)
    assert _commands.main(["clear-cache"]) == 0
    out = capsys.readouterr().out
    assert "removed 1 recovered build copy from" in out
    assert not win.root.exists()
