"""http_cache_dir: one locked slot per concurrent launch, stale locks taken over, refused off this machine, and
--disk-cache-dir=<slot> reaching every local entry point (the slot freed again on close).

Mirrors sdk/node/test/httpcache.test.ts and sdk/dotnet/tests/Clearcote.Tests/HttpCacheTests.cs. The entry-point
tests stub only the Playwright driver (and serve()'s process spawn), as test_autofillpref.py does.
"""
import os
import tempfile

import pytest

import clearcote
from clearcote import _profile, _serve, async_api
from clearcote._docker import container_env
from clearcote._httpcache import (HTTP_CACHE_SLOTS, acquire_http_cache_slot, check_http_cache_dir,
                                  take_http_cache)
from clearcote.cloud import session_body


def _lock(root, n):
    return os.path.join(str(root), "slot-%d.lock" % n)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# ------------------------------------------------------------------------------------------ slots
def test_takes_slot_0_and_marks_it_with_this_pid(tmp_path):
    s = acquire_http_cache_slot(str(tmp_path))
    assert s.dir == os.path.join(str(tmp_path), "slot-0")
    assert os.path.isdir(s.dir)
    assert _read(_lock(tmp_path, 0)) == str(os.getpid())
    s.release()
    assert not os.path.exists(_lock(tmp_path, 0))
    assert os.path.isdir(s.dir)  # the cache itself stays for the next run
    s.release()  # idempotent


def test_concurrent_launches_get_different_slots_and_a_released_one_is_reused(tmp_path):
    a, b, c = (acquire_http_cache_slot(str(tmp_path)) for _ in range(3))
    assert [a.dir, b.dir, c.dir] == [os.path.join(str(tmp_path), "slot-%d" % n) for n in range(3)]
    b.release()
    d = acquire_http_cache_slot(str(tmp_path))
    assert d.dir == os.path.join(str(tmp_path), "slot-1")
    for s in (a, c, d):
        s.release()


def test_takes_over_the_lock_of_a_process_that_is_gone(tmp_path):
    _write(_lock(tmp_path, 0), "999999999")
    _write(_lock(tmp_path, 1), "not a pid")
    s = acquire_http_cache_slot(str(tmp_path))
    assert s.dir == os.path.join(str(tmp_path), "slot-0")
    assert _read(_lock(tmp_path, 0)) == str(os.getpid())
    s.release()


def test_leaves_a_live_lock_alone_and_never_removes_one_it_no_longer_owns(tmp_path):
    _write(_lock(tmp_path, 0), str(os.getppid()))
    s = acquire_http_cache_slot(str(tmp_path))
    assert s.dir == os.path.join(str(tmp_path), "slot-1")
    _write(_lock(tmp_path, 1), str(os.getppid()))  # taken over by someone else meanwhile
    s.release()
    assert _read(_lock(tmp_path, 1)) == str(os.getppid())


def test_none_when_every_slot_is_held(tmp_path):
    for n in range(HTTP_CACHE_SLOTS):
        _write(_lock(tmp_path, n), str(os.getppid()))
    assert acquire_http_cache_slot(str(tmp_path), quiet=True) is None


def test_creates_a_folder_that_does_not_exist_yet(tmp_path):
    deep = tmp_path / "a" / "b"
    s = acquire_http_cache_slot(deep)  # a PathLike is fine
    assert s.dir == os.path.join(str(deep), "slot-0")
    s.release()


# ------------------------------------------------------------------------------------- the option
def test_take_appends_disk_cache_dir_after_the_callers_args(tmp_path):
    kw = {"http_cache_dir": str(tmp_path), "args": ["--foo"], "headless": True}
    release = take_http_cache(kw)
    assert kw == {"args": ["--foo", "--disk-cache-dir=" + os.path.join(str(tmp_path), "slot-0")], "headless": True}
    release()
    assert not os.path.exists(_lock(tmp_path, 0))


def test_take_is_a_no_op_without_the_option():
    kw = {"args": ["--foo"]}
    assert take_http_cache(kw) is None  # nothing to release, so nothing is registered on the browser
    assert kw == {"args": ["--foo"]}


def test_refuses_a_non_path_and_an_explicit_disk_cache_dir(tmp_path):
    with pytest.raises(TypeError):
        check_http_cache_dir(5, [])
    with pytest.raises(TypeError):
        check_http_cache_dir("  ", [])
    with pytest.raises(TypeError, match="cannot be combined"):
        take_http_cache({"http_cache_dir": str(tmp_path), "args": ["--disk-cache-dir=/x"]})
    assert not os.path.exists(_lock(tmp_path, 0))  # refused before taking a slot


def test_cloud_refuses_it():
    with pytest.raises(ValueError, match=r"^http_cache_dir is not available for cloud browsers$"):
        session_body({"http_cache_dir": "/tmp/c"})


def test_docker_refuses_it():
    with pytest.raises(ValueError, match="http_cache_dir is not available when launch"):
        container_env({"http_cache_dir": "/tmp/c"})


# ------------------------------------------------------------------------------------ entry points
@pytest.fixture
def env(tmp_path, monkeypatch):
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
    d = tmp_path / "engine"
    d.mkdir()
    exe = d / ("chrome.exe" if os.name == "nt" else "chrome")
    exe.write_bytes(b"\x00")
    return {"exe": str(exe), "cache": str(tmp_path / "http-cache")}


class _Target:
    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def fire(self, event):
        for fn in self.handlers.get(event, []):
            fn(self)

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return _Target()

    def close(self, **_kw):
        self.fire("close")
        self.fire("disconnected")


def _cache_args(args):
    return [a for a in args if a.startswith("--disk-cache-dir")]


@pytest.fixture
def pw(monkeypatch):
    seen = []

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, args=(), **kw):
            seen.append(list(args))
            return _Target()

        def launch(self, args=(), **kw):
            seen.append(list(args))
            return _Target()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    return seen


@pytest.mark.parametrize("how", ["throwaway", "user_data_dir", "persistent"])
def test_every_sync_entry_point_gets_the_slot_and_frees_it_on_close(tmp_path, env, pw, how):
    kw = {"executable_path": env["exe"], "headless": False, "http_cache_dir": env["cache"]}
    if how == "persistent":
        target = clearcote.launch_persistent_context(str(tmp_path / "profile"), **kw)
    elif how == "user_data_dir":
        target = clearcote.launch(user_data_dir=str(tmp_path / "profile"), **kw)
    else:
        target = clearcote.launch(**kw)
    assert _cache_args(pw[-1]) == ["--disk-cache-dir=" + os.path.join(env["cache"], "slot-0")]
    second = clearcote.launch(**kw)  # at the same time: the next slot
    assert _cache_args(pw[-1]) == ["--disk-cache-dir=" + os.path.join(env["cache"], "slot-1")]
    target.close()
    second.close()
    assert sorted(os.listdir(env["cache"])) == ["slot-0", "slot-1"]  # caches kept, locks gone


def test_a_launch_without_a_profile_refuses_it(env, pw):
    """Incognito keeps its HTTP cache in memory: the option would do nothing."""
    with pytest.raises(TypeError, match="http_cache_dir needs a profile"):
        clearcote.launch(executable_path=env["exe"], ephemeral_profile=False, http_cache_dir=env["cache"])
    assert pw == []  # refused before any browser started
    assert not os.path.exists(env["cache"])


async def test_the_async_launch_has_no_profile_and_refuses_it(env):
    with pytest.raises(TypeError, match="launch_persistent_context"):
        await async_api.launch(executable_path=env["exe"], http_cache_dir=env["cache"])
    assert not os.path.exists(env["cache"])


def test_a_failed_launch_frees_the_slot(env, monkeypatch):
    class _Chromium:
        def launch_persistent_context(self, *a, **kw):
            raise RuntimeError("boom")

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "_win_av_retry", lambda start, exe: start(exe))
    with pytest.raises(RuntimeError, match="boom"):
        clearcote.launch(executable_path=env["exe"], headless=False, http_cache_dir=env["cache"])
    assert not os.path.exists(_lock(env["cache"], 0))


async def test_async_entry_points_get_the_slot(tmp_path, env, monkeypatch):
    seen = []

    class _Chromium:
        async def launch_persistent_context(self, user_data_dir, args=(), **kw):
            seen.append(list(args))
            return _Target()

        async def launch(self, args=(), **kw):
            seen.append(list(args))
            return _Target()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def _start():
        return _PW()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(async_api, "_start_driver", _start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda *a, **k: None)
    for name in ("install_humanize", "install_humanize_on_context", "_install_window_fixup"):
        monkeypatch.setattr(async_api, name, _noop)
    kw = {"executable_path": env["exe"], "headless": False, "http_cache_dir": env["cache"]}
    ctx = await async_api.launch_persistent_context(str(tmp_path / "profile"), **kw)
    assert _cache_args(seen[-1]) == ["--disk-cache-dir=" + os.path.join(env["cache"], "slot-0")]
    ctx2 = await async_api.launch_persistent_context(str(tmp_path / "profile2"), **kw)
    assert _cache_args(seen[-1]) == ["--disk-cache-dir=" + os.path.join(env["cache"], "slot-1")]
    ctx.close()
    ctx2.close()
    assert not os.path.exists(_lock(env["cache"], 0))
    assert not os.path.exists(_lock(env["cache"], 1))


def test_serve_gets_the_slot_and_frees_it_on_close(env, monkeypatch):
    spawned = []

    class _Proc:
        pid = 4242
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        kill = terminate

        def wait(self, timeout=None):
            return 0

    def _popen(cmd, **kw):
        spawned.append(cmd)
        return _Proc()

    monkeypatch.setattr(clearcote, "_win_av_retry", lambda start, exe: start(exe))
    monkeypatch.setattr(_serve.subprocess, "Popen", _popen)
    monkeypatch.setattr(_serve.urllib.request, "urlopen", lambda *a, **k: None)
    srv = clearcote.serve(executable_path=env["exe"], headless=False, quiet=True, ready_timeout=5,
                          http_cache_dir=env["cache"])
    assert _cache_args(spawned[-1]) == ["--disk-cache-dir=" + os.path.join(env["cache"], "slot-0")]
    assert os.path.exists(_lock(env["cache"], 0))
    srv.close()
    assert not os.path.exists(_lock(env["cache"], 0))
