"""serve()'s close() waits for the browser to exit, then removes what it leaves in the temp
directory: the profile serve() made, and on Linux and macOS the browser's singleton-socket directory.

WHY THIS FILE EXISTS: a browser stopped with a signal never removes its org.chromium.Chromium.*
socket directory, and close() removed only the profile, so every serve() + close() on Linux left one
directory in the temp directory (measured with the open build, in all three SDKs). A browser still
running 10 s after SIGTERM was killed and its profile deleted without waiting for it to go.

The stand-in browser does what Chrome does there: links its pid and socket from the profile, keeps the
socket in a temp directory of its own, goes on writing the profile for half a second after SIGTERM,
and leaves the socket directory behind. The last test runs a real engine (CLEARCOTE_TEST_BINARY or
CLEARCOTE_LIVE_ENGINE).
"""
import http.server
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.request

import pytest

import clearcote
from clearcote import _profile, _serve

POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in is a shell script; Chrome keeps its socket links on Linux and macOS only")
ENGINE = os.environ.get("CLEARCOTE_TEST_BINARY") or os.environ.get("CLEARCOTE_LIVE_ENGINE")

STANDIN = textwrap.dedent("""\
    #!/bin/sh
    udd=
    for a in "$@"; do case "$a" in --user-data-dir=*) udd="${a#--user-data-dir=}" ;; esac; done
    sock=$(mktemp -d "${TMPDIR:-/tmp}/org.chromium.Chromium.XXXXXX") || exit 1
    : > "$sock/SingletonSocket"
    ln -s "$sock/SingletonSocket" "$udd/SingletonSocket"
    ln -s "standin-host-$$" "$udd/SingletonLock"
    shutdown() { i=0; while [ $i -lt 10 ]; do mkdir -p "$udd/Default" && : > "$udd/Default/Shutdown $i"; i=$((i + 1)); sleep 0.05; done; exit 0; }
    if [ -n "$STANDIN_IGNORE_TERM" ]; then trap "" TERM; else trap shutdown TERM; fi
    while :; do sleep 0.05; done
    """)


@pytest.fixture
def temp(tmp_path, monkeypatch):
    """The temp directory serve() and its browser see: anything left in it after a test leaked.

    Not under tmp_path: a real browser's socket path (<temp>/org.chromium.Chromium.XXXXXX/
    SingletonSocket) has to fit a Unix socket address, 107 bytes, and the browser exits at once
    when it does not. No licence (it would take a real lease), no saved profiles and no warnings
    from this machine."""
    temp = pathlib.Path(tempfile.mkdtemp(prefix="cc-sc-"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    for k in ("TMPDIR", "TMP", "TEMP"):  # the browser's, and a child interpreter's
        monkeypatch.setenv(k, str(temp))
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    monkeypatch.setattr(_profile, "PROFILE_DIR", str(tmp_path / "no-saved-profiles"))
    assert clearcote.resolve_license_key(None) is None  # harness: no serve() here takes a real lease
    yield temp
    shutil.rmtree(temp, ignore_errors=True)


@pytest.fixture
def standin(tmp_path):
    p = tmp_path / "engine" / "chrome"
    p.parent.mkdir()
    p.write_text(STANDIN)
    p.chmod(0o755)
    return str(p)


class _Endpoint:
    """Stands in for the browser's CDP endpoint, which serve() polls until it answers. Like Chrome's,
    it answers once the browser is up, its singleton included: the profile has a SingletonLock link."""

    def __init__(self, temp):
        self.temp = temp
        self.profile = None  # the caller's profile; else the one serve() makes in temp
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                for _ in range(500):
                    if endpoint.up():
                        break
                    time.sleep(0.01)
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def up(self):
        dirs = [self.profile] if self.profile else [str(p) for p in self.temp.glob("clearcote-serve-*")]
        return any(os.path.islink(os.path.join(d, "SingletonLock")) for d in dirs)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def cdp(temp):
    endpoint = _Endpoint(temp)
    yield endpoint
    endpoint.close()


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@POSIX_ONLY
def test_close_waits_for_the_browser_to_exit_then_removes_its_profile_and_socket_directory(temp, standin, cdp):
    srv = clearcote.serve(executable_path=standin, port=cdp.port, quiet=True)
    # the harness: a profile and the browser's socket directory, both in this test's temp directory
    assert sorted(re.sub(r"[^-.]+$", "*", n) for n in os.listdir(temp)) == [
        "clearcote-serve-*", "org.chromium.Chromium.*"]
    srv.close()
    assert srv.process.returncode == 0  # it shut down by itself, within the grace period
    time.sleep(0.7)  # a browser still shutting down would have written its profile back by now
    assert os.listdir(temp) == []


@POSIX_ONLY
def test_close_keeps_a_profile_that_is_the_callers_and_removes_the_socket_directory_it_links_to(
        tmp_path, temp, standin, cdp):
    profile = tmp_path / "profile"
    profile.mkdir()
    cdp.profile = str(profile)
    srv = clearcote.serve(executable_path=standin, port=cdp.port, quiet=True, user_data_dir=str(profile))
    srv.close()
    assert os.listdir(temp) == []
    assert (profile / "Default").is_dir()


@POSIX_ONLY
def test_close_kills_a_browser_still_running_after_the_grace_period_and_waits_for_it(
        temp, standin, cdp, monkeypatch):
    monkeypatch.setattr(_serve, "_CLOSE_GRACE", 0.5)
    monkeypatch.setenv("STANDIN_IGNORE_TERM", "1")
    srv = clearcote.serve(executable_path=standin, port=cdp.port, quiet=True)
    srv.close()
    assert srv.process.returncode == -signal.SIGKILL
    assert os.listdir(temp) == []


@POSIX_ONLY
def test_an_interpreter_that_exits_without_close_leaves_nothing_behind_either(temp, standin, cdp):
    code = ("import json, sys, clearcote; "
            "srv = clearcote.serve(executable_path=sys.argv[1], port=int(sys.argv[2]), quiet=True); "
            "print(json.dumps({'pid': srv.pid}), flush=True)")
    # the clearcote under test, wherever this run imports it from
    path = os.path.dirname(os.path.dirname(clearcote.__file__))
    r = subprocess.run([sys.executable, "-c", code, standin, str(cdp.port)], capture_output=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": path})
    assert r.returncode == 0, r.stderr.decode()
    pid = json.loads(r.stdout.decode().strip().splitlines()[-1])["pid"]
    assert not _alive(pid)  # atexit closed it, and waited for it
    time.sleep(0.7)
    assert os.listdir(temp) == []


class _FakeProcess:
    """A browser process that exits only on ``kill()``; records what close() asks of it."""

    pid = None

    def __init__(self):
        self.returncode = None
        self.calls = []

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.calls.append("wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("browser", timeout)
        return self.returncode

    def poll(self):
        return self.returncode


def test_close_returns_only_once_a_killed_browser_has_exited(tmp_path):
    proc = _FakeProcess()
    srv = _serve.Server(proc, "127.0.0.1", 9, str(tmp_path / "profile"), own_udd=False)
    srv.close()
    assert proc.calls == ["terminate", "wait", "kill", "wait"]
    srv.close()
    assert proc.calls == ["terminate", "wait", "kill", "wait"]  # once


@pytest.mark.skipif(not ENGINE, reason="set CLEARCOTE_TEST_BINARY (or CLEARCOTE_LIVE_ENGINE) to a Clearcote build")
def test_close_with_a_real_engine_leaves_nothing_in_the_temp_directory(temp):
    srv = clearcote.serve(executable_path=ENGINE, quiet=True)
    with urllib.request.urlopen(srv.cdp_url + "/json/version", timeout=10) as r:
        assert "Chrom" in json.load(r).get("Browser", "")
    srv.close()
    assert srv.process.poll() is not None
    time.sleep(1)
    # cc-fc-cache: the fontconfig cache every launch on a Linux machine shares, kept on purpose (_fonts.py)
    assert [n for n in os.listdir(temp) if n != "cc-fc-cache"] == []
