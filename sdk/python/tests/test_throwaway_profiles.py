"""Throwaway profiles: ``launch()`` and ``launch_agent()`` create a temp profile the caller never
names, so the SDK owns its removal -- when the context closes, and when the launch fails.

WHY THIS FILE EXISTS: both created the directory BEFORE launching and never removed it when the
launch failed, so every failed launch left a profile in %TEMP%; and a successful
``launch_agent()`` without ``user_data_dir`` kept its profile forever, although the caller has no
way to learn its path.

Only the Playwright driver and the humanizer are stubbed: option resolution (``_prepare``), the
binary guard and the temp-directory handling all run for real.

AND NOT UNDER A LIVE BROWSER: Playwright fires ``close`` when the browser's pipe drops, ~100 ms
before the process exits on Linux, and Chrome keeps writing the profile until then -- it even
writes the directory back after it was deleted. Deleting it on the event left one clearcote-run-*
directory in /tmp per release smoke run, so the stand-in context below behaves the same way.
"""
import inspect
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading

import pytest

import clearcote
from clearcote import _profile, async_api

BROWSER_FAILED = "browser failed to start"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="Chrome keeps its pid and socket links in the profile on Linux and macOS only")


@pytest.fixture
def temp(tmp_path, monkeypatch):
    """Every temp profile lands in this directory; anything left in it after a test leaked.

    No licence (it would select the PRO binary and take a real lease), no saved profiles and no
    warnings from this machine are visible."""
    temp = tmp_path / "temp"
    home = tmp_path / "home"
    temp.mkdir()
    home.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(home))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    monkeypatch.setattr(_profile, "PROFILE_DIR", str(tmp_path / "no-saved-profiles"))
    assert clearcote.resolve_license_key(None) is None  # harness: no launch here takes a real lease
    return temp


@pytest.fixture
def exe(tmp_path):
    """A real file used as the explicit executable_path, so nothing is downloaded. Kept outside the
    watched temp directory."""
    d = tmp_path / "engine"
    d.mkdir()
    p = d / ("chrome.exe" if os.name == "nt" else "chrome")
    p.write_bytes(b"\x00")
    return str(p)


class _FakeContext:
    """A persistent context whose ``close()`` behaves like Playwright's: the ``close`` handlers run
    first (the pipe dropped), then the browser finishes shutting down (``shutdown``), and only then
    does ``close()`` return."""

    def __init__(self, user_data_dir=None, shutdown=None):
        self.handlers = {}
        self.pages = []
        self._udd, self._shutdown = user_data_dir, shutdown

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def new_page(self, **kw):
        return kw

    def close(self, **_kw):
        for fn in self.handlers.get("close", []):
            fn(self)
        if self._shutdown:
            self._shutdown(self._udd)


class _AsyncFakeContext(_FakeContext):
    async def close(self, **_kw):
        for fn in self.handlers.get("close", []):
            r = fn(self)
            if inspect.isawaitable(r):
                await r
        if self._shutdown:
            self._shutdown(self._udd)


def _shutdown_writes(user_data_dir):
    """What Chrome does between dropping its pipe and exiting: write its profile -- recreating the
    directory if it was already deleted, which is how the deleted profile came back."""
    os.makedirs(os.path.join(user_data_dir, "Default"), exist_ok=True)
    with open(os.path.join(user_data_dir, "Local State"), "w") as f:
        f.write("{}")


@pytest.fixture
def pw(monkeypatch):
    """Sync Playwright stand-in: records each persistent launch; ``pw["fail"]`` makes it raise,
    ``pw["on_launch"]`` runs on the new profile, ``pw["shutdown"]`` is the browser's shutdown."""
    state = {"fail": None, "dirs": [], "on_launch": None, "shutdown": None}

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            state["dirs"].append(user_data_dir)
            if state["fail"]:
                raise state["fail"]
            if state["on_launch"]:
                state["on_launch"](user_data_dir)
            return _FakeContext(user_data_dir, state["shutdown"])

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    return state


def _close(context):
    for fn in context.handlers.get("close", []):
        fn(context)


# --------------------------------------------------------------------------------------- launch()
def test_launch_removes_its_profile_when_the_browser_fails(temp, exe, pw):
    pw["fail"] = RuntimeError(BROWSER_FAILED)
    with pytest.raises(RuntimeError, match=BROWSER_FAILED):
        clearcote.launch(executable_path=exe, headless=False)
    assert os.path.basename(pw["dirs"][-1]).startswith("clearcote-run-")
    assert os.listdir(temp) == []


def test_launch_removes_its_profile_when_options_fail_before_any_browser(temp, exe, pw):
    with pytest.raises(FileNotFoundError):
        clearcote.launch(executable_path=exe, headless=False, profile="no-such-profile")
    assert pw["dirs"] == []
    assert os.listdir(temp) == []


def test_launch_still_removes_its_profile_on_close(temp, exe, pw):
    browser = clearcote.launch(executable_path=exe, headless=False)
    assert len(os.listdir(temp)) == 1
    _close(browser)
    assert os.listdir(temp) == []


# --------------------------------------------------------------------------------- launch_agent()
def test_launch_agent_removes_its_profile_when_the_launch_fails(temp, exe, pw):
    pw["fail"] = RuntimeError(BROWSER_FAILED)
    with pytest.raises(RuntimeError, match=BROWSER_FAILED):
        clearcote.launch_agent(executable_path=exe, headless=False)
    assert os.path.basename(pw["dirs"][-1]).startswith("clearcote-agent-")
    assert os.listdir(temp) == []


def test_launch_agent_removes_the_profile_it_created_on_close(temp, exe, pw):
    context = clearcote.launch_agent(executable_path=exe, headless=False)
    udd = pw["dirs"][-1]
    assert os.path.isdir(udd)
    _close(context)
    assert not os.path.exists(udd)
    assert os.listdir(temp) == []


def test_launch_agent_keeps_a_profile_the_caller_named(tmp_path, temp, exe, pw):
    keep = tmp_path / "keep-me"
    keep.mkdir()
    context = clearcote.launch_agent(str(keep), executable_path=exe, headless=False)
    assert pw["dirs"][-1] == str(keep)
    assert "close" not in context.handlers  # nothing registered to delete it
    assert keep.is_dir()


# --------------------------------------------------------------------------- async launch_agent()
@pytest.fixture
def async_pw(monkeypatch):
    state = {"fail": None, "dirs": [], "shutdown": None}

    class _Chromium:
        async def launch_persistent_context(self, user_data_dir, **kw):
            state["dirs"].append(user_data_dir)
            if state["fail"]:
                raise state["fail"]
            return _AsyncFakeContext(user_data_dir, state["shutdown"])

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def _start():
        return _PW()

    async def _no_humanize(*a, **k):
        return None

    monkeypatch.setattr(async_api, "_start_driver", _start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda *a, **k: None)
    monkeypatch.setattr(async_api, "install_humanize_on_context", _no_humanize)
    return state


async def test_async_launch_agent_removes_its_profile_when_the_launch_fails(temp, exe, async_pw):
    async_pw["fail"] = RuntimeError(BROWSER_FAILED)
    with pytest.raises(RuntimeError, match=BROWSER_FAILED):
        await async_api.launch_agent(executable_path=exe, headless=False)
    assert os.path.basename(async_pw["dirs"][-1]).startswith("clearcote-agent-")
    assert os.listdir(temp) == []


async def test_async_launch_agent_removes_the_profile_it_created_on_close(temp, exe, async_pw):
    context = await async_api.launch_agent(executable_path=exe, headless=False)
    udd = async_pw["dirs"][-1]
    assert os.path.isdir(udd)
    for fn in context.handlers.get("close", []):
        await fn(context)
    assert not os.path.exists(udd)
    assert os.listdir(temp) == []


async def test_async_launch_agent_close_removes_the_profile_only_after_the_browser_has_exited(
        temp, exe, async_pw):
    async_pw["shutdown"] = _shutdown_writes
    context = await async_api.launch_agent(executable_path=exe, headless=False)
    await context.close()
    assert os.listdir(temp) == []


# ------------------------------------------------------------- never under a live browser (sync)
# Regression (0.35.0/0.36.0 release smoke on Linux: a clearcote-run-* directory per run): the
# profile was deleted by the ``close`` handler while the browser was still shutting down, and the
# browser wrote it back after the cleanup had already marked itself done.
@pytest.mark.parametrize("entry", ["launch", "launch_agent"])
def test_close_removes_the_profile_only_after_the_browser_has_exited(temp, exe, pw, entry):
    pw["shutdown"] = _shutdown_writes
    browser = getattr(clearcote, entry)(executable_path=exe, headless=False)
    browser.close()
    assert os.listdir(temp) == []


def test_an_entry_vanishing_mid_delete_is_not_taken_for_the_profile_being_gone(temp, exe, pw,
                                                                              monkeypatch):
    """rmtree raises FileNotFoundError for ANY entry that disappears during the walk -- measured:
    Chrome renaming its ``.org.chromium.Chromium.*`` temp file into ``Local State`` -- not only for
    the directory itself. Taking that for "already gone" marked the cleanup done and leaked the
    directory (2 of 10 launches on Linux)."""
    browser = clearcote.launch(executable_path=exe, headless=False)
    udd = pw["dirs"][-1]
    real_rmtree, calls = shutil.rmtree, []

    def rmtree(path, *a, **kw):
        calls.append(path)
        if len(calls) == 1:
            raise FileNotFoundError(2, "No such file or directory",
                                    os.path.join(path, ".org.chromium.Chromium.4effKM"))
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    browser.close()
    assert not os.path.exists(udd)
    assert os.listdir(temp) == []


@POSIX_ONLY
def test_a_browser_that_exits_on_its_own_loses_its_profile_only_once_its_process_has_gone(
        temp, exe, pw, monkeypatch):
    """No close() to wait for (a crash, its window closed): Chrome names its pid in the profile's
    SingletonLock symlink ("<host>-<pid>"), so the ``close`` handler waits for that process."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        pw["on_launch"] = lambda udd: os.symlink(f"test-host-{proc.pid}",
                                                 os.path.join(udd, "SingletonLock"))
        browser = clearcote.launch(executable_path=exe, headless=False)
        udd = pw["dirs"][-1]
        real_rmtree, alive_at_delete = shutil.rmtree, []

        def alive(pid):  # os.kill, not proc.poll(): poll() reads None while another thread reaps
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False

        def rmtree(path, *a, **kw):
            if path == udd:
                alive_at_delete.append(alive(proc.pid))
            return real_rmtree(path, *a, **kw)

        monkeypatch.setattr(shutil, "rmtree", rmtree)
        # the browser finishes shutting down 300 ms after its pipe dropped (and is reaped)
        threading.Timer(0.3, lambda: (proc.kill(), proc.wait())).start()
        _close(browser)  # the pipe dropped
        assert alive_at_delete and not any(alive_at_delete)
        assert not os.path.exists(udd)
    finally:
        proc.kill()
        proc.wait()


@POSIX_ONLY
def test_the_browsers_singleton_socket_directory_goes_with_the_profile(temp, exe, pw):
    """Chrome keeps its singleton socket in a temp directory of its own, found through the
    profile's SingletonSocket link; a browser that is killed rather than closed leaves it behind."""
    sock = temp / "org.chromium.Chromium.Ab12Cd"
    sock.mkdir()
    (sock / "SingletonSocket").write_bytes(b"")
    pw["on_launch"] = lambda udd: os.symlink(str(sock / "SingletonSocket"),
                                             os.path.join(udd, "SingletonSocket"))
    clearcote.launch(executable_path=exe, headless=False).close()
    assert os.listdir(temp) == []


@POSIX_ONLY
def test_a_singleton_socket_link_to_anything_else_is_never_followed(tmp_path, temp, exe, pw):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "SingletonSocket").write_bytes(b"")
    pw["on_launch"] = lambda udd: os.symlink(str(precious / "SingletonSocket"),
                                             os.path.join(udd, "SingletonSocket"))
    clearcote.launch(executable_path=exe, headless=False).close()
    assert os.listdir(temp) == []
    assert (precious / "SingletonSocket").exists()


# --------------------------------------------------------------- interpreter exit, browser open
_EXIT_SCRIPT = textwrap.dedent('''
    import atexit, os, sys
    import clearcote

    class Context:
        def __init__(self):
            self.handlers = {}
        def on(self, event, fn):
            self.handlers.setdefault(event, []).append(fn)
        def close(self, **_kw):
            pass

    class Driver:
        """The shared Playwright driver. Stopping it closes a browser the script left open -- and
        that browser writes its profile while it shuts down."""
        dirs = []
        class chromium:
            @staticmethod
            def launch_persistent_context(user_data_dir, **kw):
                Driver.dirs.append(user_data_dir)
                return Context()
        def stop(self):
            for udd in self.dirs:
                os.makedirs(os.path.join(udd, "Default"), exist_ok=True)
                with open(os.path.join(udd, "Local State"), "w") as f:
                    f.write("{}")

    driver, started = Driver(), []

    def _playwright():  # as clearcote._playwright: started on the first launch, stopped at exit
        if not started:
            started.append(1)
            atexit.register(clearcote._stop_quietly, driver)
        return driver

    clearcote._playwright = _playwright
    clearcote.install_humanize_on_context = lambda *a, **k: None
    clearcote.launch(executable_path=sys.argv[1], headless=False)
    print("launched", Driver.dirs[-1])
    # ... and exit with the browser still open
''')


def test_a_browser_left_open_at_exit_loses_its_profile_after_the_driver_has_closed_it(tmp_path,
                                                                                    exe):
    """atexit runs last-in-first-out. The per-launch hook was registered after the driver's stop,
    so it ran FIRST: it deleted the profile under the running browser, which then wrote it back while
    the driver closed it (5 of 5 launches on Linux left a directory behind)."""
    temp, home = tmp_path / "exit-temp", tmp_path / "exit-home"
    temp.mkdir()
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION")}
    env.update(TMPDIR=str(temp), TEMP=str(temp), TMP=str(temp), HOME=str(home),
               USERPROFILE=str(home), CLEARCOTE_NO_WARN="1",
               CLEARCOTE_PROFILE_DIR=str(tmp_path / "no-saved-profiles"),
               PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(clearcote.__file__))))
    r = subprocess.run([sys.executable, "-c", _EXIT_SCRIPT, exe], env=env, capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "launched" in r.stdout and "clearcote-run-" in r.stdout  # the launch really happened
    assert os.listdir(temp) == []
