"""Two installers of the same build at once (two processes, or two containers sharing the macOS Docker
launch's engine-cache volume) must not download over each other: one installs, the other waits and uses
that tree. Before the install lock both downloaded, shared one ``.incoming`` directory, and the loser failed
with "Directory not empty" (measured with two parallel licensed containers on a fresh volume).

The lock is one protocol shared with the Node and .NET SDKs (see download.py): an O_EXCL lock file with an
owner record, a heartbeat, and stale-lock recovery."""
import hashlib
import importlib
import io
import json
import os
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
    rec = {"pid": os.getpid(), "host": socket.gethostname(), "boot": download._boot_id(),
           "pidns": download._pid_namespace(), "nonce": "f" * 32, "sdk": "node", "created": 0}
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


def test_a_lock_without_a_heartbeat_is_taken_over(tmp_path):
    """A holder on another machine or in another container cannot be asked whether it is alive: its
    heartbeat (the lock file's mtime) decides."""
    base = str(tmp_path / TAG)
    _write_lock(base, age=download._LOCK_STALE + 5, host="another-machine", pid=1)
    lock = download._acquire_install_lock(base, timeout=10)
    assert lock.held()
    lock.release()


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


def test_a_half_written_lock_is_taken_over_only_after_its_grace_period(tmp_path):
    base = str(tmp_path / TAG)
    os.makedirs(base)
    open(_lock_path(base), "w").close()  # created, owner record not written yet
    with pytest.raises(RuntimeError, match="a process that left no details"):
        download._acquire_install_lock(base, timeout=0.6)
    then = time.time() - download._LOCK_UNREADABLE - 5
    os.utime(_lock_path(base), (then, then))
    download._acquire_install_lock(base, timeout=10).release()


def test_a_breaker_left_by_a_crashed_process_is_cleared(tmp_path):
    base = str(tmp_path / TAG)
    _write_lock(base, age=download._LOCK_STALE + 5, host="another-machine")
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


def test_a_failed_install_leaves_no_lock_or_temp_files(tmp_path, monkeypatch):
    data = _archive()

    def download_to(url, path, size, quiet):
        with open(path, "wb") as fh:
            fh.write(data)
        return hashlib.sha256(data).hexdigest()

    monkeypatch.setattr(download, "_download", download_to)
    rel = {"tag": TAG, "version": "0.0.0", "url": "https://example.invalid/a.zip", "sha256": "0" * 64,
           "asset": "a.zip", "archive": "zip", "binary": BINARY, "size": len(data), "unpinned": False}
    base = str(tmp_path / TAG)
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        download._fetch_and_verify(rel, base, True)
    assert os.listdir(base) == []


def test_leftovers_of_an_install_that_stopped_part_way_are_cleared(tmp_path, monkeypatch):
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
    _write_lock(base, age=download._LOCK_STALE + 5, host="another-machine")  # its installer died
    rel = {"tag": TAG, "version": "0.0.0", "url": "https://example.invalid/a.zip", "sha256": sha,
           "asset": "a.zip", "archive": "zip", "binary": BINARY, "size": len(data), "unpinned": False}
    exe = download._fetch_and_verify(rel, base, True)
    assert os.path.isfile(exe)
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]
    assert not os.path.exists(os.path.join(base, "browser", "half"))
