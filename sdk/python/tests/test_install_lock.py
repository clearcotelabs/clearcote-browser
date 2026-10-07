"""Two installers of the same build at once (two processes, or two containers sharing the macOS Docker
launch's engine-cache volume) must not download over each other: one installs, the other waits and uses
that tree. Before the install lock both downloaded, shared one ``.incoming`` directory, and the loser failed
with "Directory not empty" (measured with two parallel licensed containers on a fresh volume).

The lock is one protocol shared with the Node and .NET SDKs (see download.py): an O_EXCL lock file with an
owner record, a heartbeat, and stale-lock recovery."""
import contextlib
import hashlib
import importlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile

import pytest

from _fake_build import FakeBuildServer, TAG, verified_tree

download = importlib.import_module("clearcote.download")  # the module: `clearcote.download` is also a function

BINARY = "chrome.exe" if sys.platform == "win32" else "chrome"
needs_pro_platform = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")


def _archive():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:  # every file a healthy install must have (download.CRITICAL_FILES)
        for name in {BINARY, *download.CRITICAL_FILES["win32" if sys.platform == "win32" else "other"]}:
            z.writestr(name, b"x" * 64)  # at the root, where verify_install looks for them
    return buf.getvalue()


def test_two_installers_at_once_download_once(tmp_path, monkeypatch):
    data = _archive()
    sha = hashlib.sha256(data).hexdigest()
    rel = {"tag": "pro-test-r1", "version": "1.0", "url": "https://example.invalid/a.zip", "sha256": sha,
           "asset": "a.zip", "archive": "zip", "binary": BINARY, "size": len(data), "unpinned": False}
    both_started = threading.Barrier(2)
    downloads = []

    def slow_download(url, path, size, quiet):
        downloads.append(url)
        threading.Event().wait(0.5)  # long enough for the other installer to arrive meanwhile
        with open(path, "wb") as fh:
            fh.write(data)
        return sha

    monkeypatch.setattr(download, "_download", slow_download)
    base = str(tmp_path / "cache" / "pro-test-r1")
    results, errors = [], []

    def install():
        both_started.wait()
        try:
            results.append(download._fetch_and_verify(dict(rel), base, True))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert errors == []
    assert len(downloads) == 1  # the second installer waited and used the first one's tree
    assert len(results) == 2 and results[0] == results[1] and os.path.isfile(results[0])
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]  # no lock or temp left


# ── the lock itself ──────────────────────────────────────────────────────────────────────────────────

def _lock_path(base):
    return os.path.join(base, download.INSTALL_LOCK)


def _write_lock(base, age=0.0, **fields):
    """A lock file as any of the three SDKs writes it; ``age`` back-dates its heartbeat."""
    rec = {"pid": os.getpid(), "start": download._process_start(os.getpid()), "host": socket.gethostname(),
           "boot": download._boot_id(), "pidns": download._pid_namespace(), "nonce": "f" * 32, "sdk": "node",
           "created": int(time.time() * 1000)}
    rec.update(fields)
    os.makedirs(base, exist_ok=True)
    with open(_lock_path(base), "w", encoding="utf-8") as f:
        f.write(json.dumps(rec))
    if age:
        then = time.time() - age
        os.utime(_lock_path(base), (then, then))


def test_acquire_writes_an_owner_record_and_release_removes_it(tmp_path):
    base = str(tmp_path / TAG)
    lock = download._acquire_install_lock(base, timeout=1)
    with open(_lock_path(base), encoding="utf-8") as f:
        rec = json.load(f)
    assert (rec["pid"], rec["nonce"], rec["sdk"]) == (os.getpid(), lock.nonce, "python")
    assert rec["host"] == socket.gethostname() and lock.held()
    lock.release()
    assert os.listdir(base) == []  # no lock file, no breaker
    download._acquire_install_lock(base, timeout=1).release()  # free again


def test_a_held_lock_makes_the_next_installer_wait_then_give_up_plainly(tmp_path):
    base = str(tmp_path / TAG)
    lock = download._acquire_install_lock(base, timeout=1)
    try:
        started = time.monotonic()
        with pytest.raises(RuntimeError) as err:
            download._acquire_install_lock(base, timeout=0.6)
        assert time.monotonic() - started >= 0.5  # it waited
        msg = str(err.value)
        assert msg.startswith("Gave up after 1 second waiting for another program to finish installing")
        assert f"process {os.getpid()} on {socket.gethostname()} (Clearcote Python SDK)" in msg
        assert _lock_path(base) in msg  # what to delete if nothing else is installing
    finally:
        lock.release()


def test_a_lock_left_by_a_dead_process_on_this_machine_is_taken_over_at_once(tmp_path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    base = str(tmp_path / TAG)
    _write_lock(base, pid=gone.pid)  # fresh heartbeat: only the pid tells it is dead
    started = time.monotonic()
    lock = download._acquire_install_lock(base, timeout=10)
    assert time.monotonic() - started < 3
    assert lock.held()
    lock.release()
    assert os.listdir(base) == []


def test_a_lock_from_elsewhere_that_stops_changing_is_taken_over(tmp_path, monkeypatch):
    """A holder on another machine or in another container cannot be asked whether it is alive: its
    heartbeat decides, timed by the waiter's own clock."""
    monkeypatch.setattr(download, "_LOCK_STALE", 1.0)
    base = str(tmp_path / TAG)
    _write_lock(base, host="another-machine", pid=1)
    started = time.monotonic()
    lock = download._acquire_install_lock(base, timeout=10)
    assert time.monotonic() - started >= 0.9  # watched it stay unchanged first
    assert lock.held()
    lock.release()


def _keep_touching(path, offset, stop):
    """A live holder's heartbeat, written by a clock ``offset`` seconds off ours."""
    i = 0
    while not stop.wait(0.1):
        i += 1
        then = time.time() + offset + i * 0.01
        with contextlib.suppress(OSError):
            os.utime(path, (then, then))


def test_a_lock_from_elsewhere_that_keeps_changing_is_not_broken_whatever_its_clock(tmp_path, monkeypatch):
    """Machines sharing a cache can disagree about the time: only "unchanged while I watched" counts."""
    monkeypatch.setattr(download, "_LOCK_STALE", 1.0)
    monkeypatch.setattr(download, "_LOCK_UNREADABLE", 1.0)
    base = str(tmp_path / TAG)
    for offset in (-3600, 3600):  # its clock an hour behind ours, then an hour ahead
        _write_lock(base, host="another-machine", pid=1)
        stop = threading.Event()
        toucher = threading.Thread(target=_keep_touching, args=(_lock_path(base), offset, stop))
        toucher.start()
        try:
            with pytest.raises(RuntimeError, match="Gave up"):
                download._acquire_install_lock(base, timeout=2.5)
        finally:
            stop.set()
            toucher.join()
        with open(_lock_path(base), encoding="utf-8") as f:
            assert json.load(f)["nonce"] == "f" * 32  # not broken
    started = time.monotonic()
    download._acquire_install_lock(base, timeout=10).release()  # once it stops changing, it is taken over
    assert time.monotonic() - started >= 0.9


def test_a_live_holder_on_this_machine_is_never_taken_over_by_age(tmp_path, monkeypatch):
    """Its heartbeat can stop while it is alive (a debugger, a paused container, a laptop asleep, a
    blocked event loop): a live process here keeps its lock, however old the heartbeat."""
    monkeypatch.setattr(download, "_LOCK_STALE", 1.0)
    base = str(tmp_path / TAG)
    _write_lock(base, age=600)  # this very process: alive
    with pytest.raises(RuntimeError) as err:
        download._acquire_install_lock(base, timeout=2)
    holder = f"process {os.getpid()} on {socket.gethostname()} (Clearcote Node SDK), which is still running"
    assert holder in str(err.value)
    with open(_lock_path(base), encoding="utf-8") as f:
        assert json.load(f)["nonce"] == "f" * 32  # not broken


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process start times are read from /proc")
def test_a_reused_pid_on_this_machine_is_taken_over_at_once(tmp_path):
    base = str(tmp_path / TAG)
    _write_lock(base, start="linux:1")  # our pid, but a process that started at another time
    started = time.monotonic()
    download._acquire_install_lock(base, timeout=10).release()
    assert time.monotonic() - started < 3


def test_a_live_lock_from_another_machine_is_respected(tmp_path):
    base = str(tmp_path / TAG)
    _write_lock(base, age=5, host="another-machine", pid=1)
    with pytest.raises(RuntimeError, match="process 1 on another-machine \\(Clearcote Node SDK\\)"):
        download._acquire_install_lock(base, timeout=0.6)
    with open(_lock_path(base), encoding="utf-8") as f:
        assert json.load(f)["host"] == "another-machine"  # not broken


def test_a_lock_of_a_live_process_here_is_respected(tmp_path):
    base = str(tmp_path / TAG)
    _write_lock(base, sdk="dotnet")  # this very process: alive
    with pytest.raises(RuntimeError, match="Clearcote .NET SDK"):
        download._acquire_install_lock(base, timeout=0.6)


def test_a_half_written_lock_is_taken_over_only_after_its_grace_period(tmp_path, monkeypatch):
    base = str(tmp_path / TAG)
    os.makedirs(base)
    open(_lock_path(base), "w").close()  # created, owner record not written yet
    with pytest.raises(RuntimeError, match="a process that left no details"):
        download._acquire_install_lock(base, timeout=0.6)
    monkeypatch.setattr(download, "_LOCK_UNREADABLE", 1.0)
    download._acquire_install_lock(base, timeout=10).release()


def test_a_breaker_left_by_a_crashed_process_is_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_LOCK_STALE", 1.0)
    monkeypatch.setattr(download, "_LOCK_BREAKER_STALE", 1.0)
    base = str(tmp_path / TAG)
    _write_lock(base, host="another-machine")
    breaker = _lock_path(base) + ".break"
    open(breaker, "w").close()
    then = time.time() - download._LOCK_BREAKER_STALE - 5
    os.utime(breaker, (then, then))
    download._acquire_install_lock(base, timeout=10).release()
    assert os.listdir(base) == []


def test_a_holder_that_lost_its_lock_does_not_delete_the_new_one(tmp_path):
    base = str(tmp_path / TAG)
    lock = download._acquire_install_lock(base, timeout=1)
    _write_lock(base, nonce="e" * 32)  # another process judged ours stale and took over
    assert not lock.held()
    lock.release()
    with open(_lock_path(base), encoding="utf-8") as f:
        assert json.load(f)["nonce"] == "e" * 32


# ── installs under the lock ──────────────────────────────────────────────────────────────────────────

@needs_pro_platform
def test_waits_for_the_holder_then_uses_its_build_without_downloading(tmp_path):
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    lock = download._acquire_install_lock(base, timeout=1)  # another installer is busy with this build
    out, errors = [], []
    with FakeBuildServer() as srv:
        def install():
            try:
                out.append(download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=install)
        t.start()
        assert srv.meta_seen.wait(20)
        time.sleep(0.7)  # the installer is waiting on the lock by now
        exe = verified_tree(base)  # the holder finishes...
        lock.release()  # ...and lets go
        t.join(30)
    assert errors == [] and out == [exe]
    assert srv.archive_hits == 0  # used the holder's build: no second download
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@needs_pro_platform
def test_a_failed_install_leaves_nothing_behind(tmp_path):
    """The browser binary fails its own hash check, after the archive was extracted: no lock, no temp
    files, and no unverified tree at browser/ for anything to pick up."""
    cache = str(tmp_path / "cache")
    with FakeBuildServer(exe_sha256="0" * 64) as srv:
        with pytest.raises(RuntimeError, match=f"{BINARY} SHA-256 mismatch"):
            download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert os.listdir(os.path.join(cache, TAG)) == []


def test_leftovers_of_an_install_that_stopped_part_way_are_cleared(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_LOCK_STALE", 1.0)
    data = _archive()
    sha = hashlib.sha256(data).hexdigest()

    def download_to(url, path, size, quiet):
        with open(path, "wb") as fh:
            fh.write(data)
        return sha

    monkeypatch.setattr(download, "_download", download_to)
    base = str(tmp_path / TAG)
    for leftover in (".tmp-0123", ".incoming", os.path.join("browser", "half")):
        os.makedirs(os.path.join(base, leftover))
    _write_lock(base, host="another-machine")  # its installer died
    rel = {"tag": TAG, "version": "0.0.0", "url": "https://example.invalid/a.zip", "sha256": sha,
           "asset": "a.zip", "archive": "zip", "binary": BINARY, "size": len(data), "unpinned": False}
    exe = download._fetch_and_verify(rel, base, True)
    assert os.path.isfile(exe)
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]
    assert not os.path.exists(os.path.join(base, "browser", "half"))


@needs_pro_platform
def test_a_verified_build_that_appears_during_the_install_is_used_not_replaced(tmp_path):
    """Another installer finished this build after this one's cache check (it held the lock before us, or
    took it over): the tree it marked verified may already be running, so it is used, never moved."""
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    finished = []
    with FakeBuildServer(on_archive=lambda: finished.append(verified_tree(base))) as srv:
        path = download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert path == finished[0]
    with open(os.path.join(base, ".verified"), encoding="utf-8") as f:
        assert f.read() == "0" * 64 + "\n"  # the other installer's marker, not rewritten
    with open(path, "rb") as f:
        assert f.read() == b"y" * 64  # its files, not replaced by ours
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@needs_pro_platform
def test_a_holder_that_lost_the_lock_never_marks_its_tree_verified(tmp_path, monkeypatch):
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    taken = threading.Event()
    real = download._write_manifest

    def manifest_then_lose_the_lock(b, browser):
        real(b, browser)
        if not taken.is_set():  # another process takes the lock over right after the tree is in place
            _write_lock(base, nonce="e" * 32)
            taken.set()

    monkeypatch.setattr(download, "_write_manifest", manifest_then_lose_the_lock)
    out, errors = [], []
    with FakeBuildServer() as srv:
        def install():
            try:
                out.append(download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=install)
        t.start()
        assert taken.wait(30)
        time.sleep(0.7)
        verified_while_lost = os.path.exists(os.path.join(base, ".verified"))
        with open(_lock_path(base), encoding="utf-8") as f:
            nonce = json.load(f)["nonce"]
        with contextlib.suppress(OSError):
            os.remove(_lock_path(base))  # the other process lets go without installing
        t.join(30)
    assert not verified_while_lost  # never marked verified without the lock
    assert nonce == "e" * 32  # and the new holder's lock was left alone
    assert errors == [] and os.path.isfile(out[0])
    assert srv.archive_hits == 2  # it waited, then installed under the lock again
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@needs_pro_platform
def test_a_breaker_and_trash_left_by_a_hard_kill_are_cleared_by_the_next_install(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_LOCK_BREAKER_STALE", 0.3)  # the install below takes longer than this
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    os.makedirs(os.path.join(base, ".trash-0123", "locales"))
    breaker = _lock_path(base) + ".break"
    open(breaker, "w").close()  # killed between deleting a stale lock and deleting its breaker
    then = time.time() - 60
    os.utime(breaker, (then, then))
    with FakeBuildServer(archive_delay=0.6) as srv:
        download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@needs_pro_platform
def test_a_breaker_that_changes_during_the_install_is_left_alone(tmp_path):
    """Its timestamp may come from a machine whose clock is far behind: only one that stayed exactly the same
    for the breaker window, by this process's own clock, is removed."""
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    breaker = _lock_path(base) + ".break"
    os.makedirs(base)
    open(breaker, "w").close()
    then = time.time() - 3600
    os.utime(breaker, (then, then))

    def another_waiter_takes_it():
        later = time.time() - 7200
        with contextlib.suppress(OSError):
            os.utime(breaker, (later, later))

    with FakeBuildServer(on_archive=another_waiter_takes_it) as srv:
        download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert os.path.exists(breaker)


def test_a_breaker_that_looks_old_is_removed_only_after_staying_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_LOCK_STALE", 0.5)
    monkeypatch.setattr(download, "_LOCK_BREAKER_STALE", 1.5)
    base = str(tmp_path / TAG)
    _write_lock(base, host="another-machine", pid=1)
    breaker = _lock_path(base) + ".break"
    open(breaker, "w").close()
    then = time.time() - 3600  # an hour old by its timestamp: that alone proves nothing
    os.utime(breaker, (then, then))
    started = time.monotonic()
    download._acquire_install_lock(base, timeout=10).release()
    assert time.monotonic() - started >= 1.4  # watched it stay unchanged for the breaker window first
    assert os.listdir(base) == []


@needs_pro_platform
@pytest.mark.parametrize("damage", ["binary removed", "browser folder removed"])
def test_a_verified_build_whose_browser_is_gone_is_installed_again(tmp_path, damage):
    """Antivirus quarantined chrome.exe, or someone deleted browser/ by hand, and .verified stayed behind:
    the next install repairs it with one download instead of giving up."""
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    exe = verified_tree(base)
    if damage == "binary removed":
        os.remove(exe)
    else:
        shutil.rmtree(os.path.dirname(exe))
    with FakeBuildServer() as srv:
        path = download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert os.path.isfile(path)
    assert srv.archive_hits == 1
    with open(os.path.join(base, ".verified"), encoding="utf-8") as f:
        assert f.read() == srv.sha + "\n"
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@pytest.mark.skipif(download._boot_id() is not None, reason="with a boot id (Linux) the boot id tells the boot")
def test_a_lock_written_before_this_machine_started_is_taken_over_at_once(tmp_path):
    """Its process number may belong to an unrelated program since the restart."""
    base = str(tmp_path / TAG)
    _write_lock(base, created=0)  # our pid, alive -- but the record is older than this boot
    started = time.monotonic()
    download._acquire_install_lock(base, timeout=3).release()
    assert time.monotonic() - started < 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation times")
def test_a_pid_now_used_by_a_newer_process_is_taken_over_at_once(tmp_path):
    """Windows reuses process numbers quickly: a live process created after the lock is not its holder."""
    newer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)
        base = str(tmp_path / TAG)
        _write_lock(base, pid=newer.pid, start=None, created=int(time.time() * 1000) - 10_000)
        started = time.monotonic()
        download._acquire_install_lock(base, timeout=5).release()
        assert time.monotonic() - started < 3
    finally:
        newer.kill()
        newer.wait()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a record without a start time, read on Linux")
def test_a_live_pid_that_cannot_be_checked_is_named_with_care(tmp_path):
    base = str(tmp_path / TAG)
    _write_lock(base, start=None)  # alive, but nothing tells whether it is still the same process
    with pytest.raises(RuntimeError) as err:
        download._acquire_install_lock(base, timeout=1)
    msg = str(err.value)
    assert f"process {os.getpid()} on {socket.gethostname()} (Clearcote Node SDK)" in msg
    assert "that process number may now belong to another program" in msg
    assert "If no Clearcote program is installing this build, delete this file" in msg



@pytest.mark.skipif(download._boot_id() is None, reason="needs a boot id (Linux)")
def test_a_live_holder_from_this_boot_is_kept_even_when_its_lock_looks_older_than_the_boot(tmp_path):
    """A forward clock step (a VM resumed, a Docker Desktop VM after sleep) can make a live holder's lock look
    written before the boot. The boot id already tells the boot, so only the process check counts."""
    base = str(tmp_path / TAG)
    _write_lock(base, created=0)  # our pid, this boot, our start time
    with pytest.raises(RuntimeError, match="which is still running"):
        download._acquire_install_lock(base, timeout=1.5)


def _rename_in_use(monkeypatch, times=None):
    """Moving browser/ aside fails as if a program had its files open (``times`` times; None: until undone)."""
    real = download._rename_dir
    state = {"left": times, "on": True}

    def rename(src, dst):
        if (state["on"] and os.path.basename(src) == "browser" and os.path.basename(dst).startswith(".trash-")
                and (state["left"] is None or state["left"] > 0)):
            if state["left"] is not None:
                state["left"] -= 1
            raise PermissionError(13, "The process cannot access the file because it is being used by another process", src)
        real(src, dst)

    monkeypatch.setattr(download, "_rename_dir", rename)
    return state


@needs_pro_platform
def test_a_damaged_build_in_use_is_left_marked_and_reported_at_once(tmp_path, monkeypatch):
    """A tree marked verified but damaged, whose files a running browser still holds (Windows will not move
    them): stop at once with "close it", without downloading and without unmarking it, so that the launch after
    the browser is closed repairs it."""
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    exe = verified_tree(base)
    os.remove(os.path.join(os.path.dirname(exe), "icudtl.dat"))  # damaged
    state = _rename_in_use(monkeypatch)
    with FakeBuildServer() as srv:
        for _ in range(2):  # this launch and the next, while it is still in use
            with pytest.raises(RuntimeError, match="still using them"):
                download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
            assert srv.archive_hits == 0  # nothing downloaded
            assert os.path.exists(os.path.join(base, ".verified"))  # nor unmarked
            assert os.path.exists(os.path.join(base, download.MANIFEST))
        state["on"] = False  # the browser was closed
        path = download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
        assert os.path.isfile(path) and srv.archive_hits == 1
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@pytest.mark.skipif(sys.platform != "win32", reason="a file held open blocks moving its folder on Windows only")
def test_a_damaged_build_really_in_use_is_left_marked_and_reported_at_once(tmp_path):
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    exe = verified_tree(base)
    os.remove(os.path.join(os.path.dirname(exe), "icudtl.dat"))  # damaged
    with FakeBuildServer() as srv:
        with open(os.path.join(os.path.dirname(exe), "chrome.dll"), "rb"):  # a running browser holds its files
            with pytest.raises(RuntimeError, match="still using them"):
                download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
            assert srv.archive_hits == 0
            assert os.path.exists(os.path.join(base, ".verified"))
        path = download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
        assert os.path.isfile(path) and srv.archive_hits == 1


@needs_pro_platform
def test_a_holder_that_lost_the_lock_writes_no_manifest(tmp_path, monkeypatch):
    """Once its tree is in place it checks the lock before writing .manifest.json too: a late manifest could
    overwrite the new holder's, or describe a tree that the new holder is replacing."""
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    taken = threading.Event()
    real = download._rename_dir

    def rename(src, dst):
        real(src, dst)
        if os.path.basename(dst) == "browser" and not taken.is_set():  # its tree is in place; another takes over
            _write_lock(base, nonce="e" * 32)
            taken.set()

    monkeypatch.setattr(download, "_rename_dir", rename)
    out, errors = [], []
    with FakeBuildServer() as srv:
        def install():
            try:
                out.append(download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=install)
        t.start()
        assert taken.wait(30)
        time.sleep(0.7)
        manifest_while_lost = os.path.exists(os.path.join(base, download.MANIFEST))
        verified_while_lost = os.path.exists(os.path.join(base, ".verified"))
        with contextlib.suppress(OSError):
            os.remove(_lock_path(base))  # the other process lets go without installing
        t.join(30)
    assert not manifest_while_lost and not verified_while_lost
    assert errors == [] and os.path.isfile(out[0])
    assert srv.archive_hits == 2
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


@needs_pro_platform
def test_after_taking_over_an_unconfirmed_holder_it_waits_for_its_tree_to_be_free(tmp_path, monkeypatch):
    """A holder taken over without proof that it died (it was suspended) can still hold its half-installed tree;
    it lets go when it resumes and finds the lock gone. Wait for that, instead of failing."""
    monkeypatch.setattr(download, "_LOCK_STALE", 0.5)
    monkeypatch.setattr(download, "_IN_USE_POLL", 0.2, raising=False)
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    os.makedirs(os.path.join(base, "browser", "half"))  # its tree, moved in but not verified
    _write_lock(base, host="another-machine", pid=1)  # cannot be confirmed dead: taken over once it stops changing
    state = _rename_in_use(monkeypatch, times=3)
    with FakeBuildServer() as srv:
        path = download.pro_ensure_binary("test-key", api_base=srv.url, cache_dir=cache, quiet=True)
    assert os.path.isfile(path) and srv.archive_hits == 1
    assert state["left"] == 0  # it did wait for the files
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]
