"""Separate processes installing one build at once, through the public PRO download path against a local
fake of the download route (a small fake archive, no real browser). Field failure this guards: right after a
new PRO build shipped, two processes on one host found it missing and installed into the same folder at the
same time; one removed the browser files the other was already running and died at startup.

The lock is shared with the Node and .NET SDKs, so the second test plays the other SDK's part by writing the
lock file exactly as they do."""
import json
import os
import subprocess
import sys
import time

import pytest

from _fake_build import FakeBuildServer, TAG, verified_tree

pytestmark = pytest.mark.skipif(sys.platform == "darwin", reason="the PRO route serves Windows and Linux")

CHILD = """
import json, sys
from clearcote.download import pro_ensure_binary
print(json.dumps({"path": pro_ensure_binary("test-key", api_base=sys.argv[1], cache_dir=sys.argv[2], quiet=True)}))
"""


def _start(api, cache):
    return subprocess.Popen([sys.executable, "-c", CHILD, api, cache],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(child):
    try:
        out, err = child.communicate(timeout=120)
    except subprocess.TimeoutExpired:
        child.kill()
        out, err = child.communicate()
    assert child.returncode == 0, f"installer exited {child.returncode}:\n{err}"
    return json.loads(out.strip().splitlines()[-1])["path"]


def test_two_processes_install_one_build_once(tmp_path):
    cache = str(tmp_path / "cache")
    with FakeBuildServer(meta_barrier=2, archive_delay=1.0) as srv:  # both reach the cache check together
        children = [_start(srv.url, cache) for _ in range(2)]
        paths = [_finish(c) for c in children]
    assert paths[0] == paths[1] and os.path.isfile(paths[0])
    assert srv.archive_hits == 1  # one download; the other process waited and used it
    base = os.path.join(cache, TAG)
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]  # no lock or temp files left


def test_waits_for_another_sdks_install_and_uses_it(tmp_path):
    cache = str(tmp_path / "cache")
    base = os.path.join(cache, TAG)
    os.makedirs(base)
    lock = os.path.join(base, ".install-lock")
    with open(lock, "w", encoding="utf-8") as f:  # what the Node / .NET SDK writes; this process plays it
        json.dump({"pid": os.getpid(), "host": __import__("socket").gethostname(), "boot": _boot_id(),
                   "pidns": _pid_namespace(), "nonce": "a" * 32, "sdk": "node", "created": 0}, f)
    with FakeBuildServer(archive_delay=0.5) as srv:
        child = _start(srv.url, cache)
        assert srv.meta_seen.wait(30)
        time.sleep(1.0)  # the child has found the build missing by now
        exe = verified_tree(base)  # the other SDK finishes its install...
        os.remove(lock)  # ...and releases the lock
        path = _finish(child)
    assert path == exe
    assert srv.archive_hits == 0  # it waited instead of downloading over the other install
    assert sorted(os.listdir(base)) == [".manifest.json", ".verified", "browser"]


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _pid_namespace():
    try:
        return os.readlink("/proc/self/ns/pid")
    except (OSError, AttributeError, NotImplementedError, ValueError):
        return None
