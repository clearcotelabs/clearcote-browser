"""Autofill on, as in Chrome: _autofillpref seeds ``autofill.profile_enabled`` / ``credit_card_enabled`` into a
profile's Default/Preferences only where they are missing, before every launch on a profile directory.

Mirrors sdk/node/test/autofillpref.test.ts + the autofill block of launch-entrypoints.test.ts, and the .NET
AutofillPrefsTests.cs. The entry-point tests stub only the Playwright driver (and serve()'s process spawn).
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

import clearcote
from clearcote import _profile, _serve, async_api
from clearcote._autofillpref import apply_autofill_prefs, seed_autofill_prefs

ON = {"profile_enabled": True, "credit_card_enabled": True}


def _prefs_path(udd):
    return os.path.join(str(udd), "Default", "Preferences")


def _read(udd):
    with open(_prefs_path(udd), encoding="utf-8") as f:
        return json.load(f)


def _write(udd, value):
    os.makedirs(os.path.join(str(udd), "Default"), exist_ok=True)
    with open(_prefs_path(udd), "w", encoding="utf-8") as f:
        f.write(value if isinstance(value, str) else json.dumps(value))


# ------------------------------------------------------------------------------- seed_autofill_prefs
def test_a_new_profile_gets_both_on(tmp_path):
    udd = tmp_path / "not-yet"
    assert seed_autofill_prefs(str(udd)) == ["profile_enabled", "credit_card_enabled"]
    assert _read(udd) == {"autofill": ON}


def test_keeps_an_explicit_off_and_every_other_preference(tmp_path):
    _write(tmp_path, {"autofill": {"profile_enabled": False, "other": 1}, "browser": {"x": 2}})
    assert seed_autofill_prefs(str(tmp_path)) == ["credit_card_enabled"]
    assert _read(tmp_path) == {
        "autofill": {"profile_enabled": False, "other": 1, "credit_card_enabled": True}, "browser": {"x": 2}}


def test_does_nothing_when_both_are_set(tmp_path):
    text = json.dumps({"autofill": {"profile_enabled": False, "credit_card_enabled": False}}, indent=1)
    _write(tmp_path, text)
    assert seed_autofill_prefs(str(tmp_path)) == []
    with open(_prefs_path(tmp_path), encoding="utf-8") as f:
        assert f.read() == text


@pytest.mark.parametrize("bad", ["{not json", "[1,2]", "null", ""])
def test_never_replaces_a_file_it_cannot_read(tmp_path, bad):
    _write(tmp_path, bad)
    assert seed_autofill_prefs(str(tmp_path)) == []
    with open(_prefs_path(tmp_path), encoding="utf-8") as f:
        assert f.read() == bad


def test_apply_never_raises(tmp_path):
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    apply_autofill_prefs(str(not_a_dir), quiet=True)
    assert not (tmp_path / "file" / "Default").exists()


# ------------------------------------------------------------------------------------- entry points
@pytest.fixture
def env(tmp_path, monkeypatch):
    """No licence, no saved profiles, no warnings; temp profiles land in tmp_path/temp."""
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
    assert clearcote.resolve_license_key(None) is None
    d = tmp_path / "engine"
    d.mkdir()
    exe = d / ("chrome.exe" if os.name == "nt" else "chrome")
    exe.write_bytes(b"\x00")
    return {"temp": temp, "exe": str(exe)}


class _Ctx:
    def __init__(self):
        self.handlers = {}
        self.pages = []

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return _Ctx()

    def close(self, **_kw):
        for fn in self.handlers.get("close", []):
            fn(self)


def _autofill_or_none(udd):
    try:
        return _read(udd).get("autofill")
    except FileNotFoundError:
        return None


@pytest.fixture
def pw(monkeypatch):
    """Sync Playwright stand-in: records, per launch, the profile and its autofill prefs at that moment."""
    seen = []

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            seen.append((user_data_dir, _autofill_or_none(user_data_dir)))
            return _Ctx()

        def launch(self, **kw):
            seen.append((None, None))
            return _Ctx()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    return seen


def test_launch_persistent_context_seeds_before_the_browser_starts(tmp_path, env, pw):
    udd = str(tmp_path / "profile")
    clearcote.launch_persistent_context(udd, executable_path=env["exe"], headless=False)
    assert pw[-1] == (udd, ON)


def test_launch_persistent_context_keeps_an_explicit_off(tmp_path, env, pw):
    udd = tmp_path / "profile"
    _write(udd, {"autofill": {"profile_enabled": False}, "browser": {"x": 1}})
    clearcote.launch_persistent_context(str(udd), executable_path=env["exe"], headless=False)
    assert _read(udd) == {"autofill": {"profile_enabled": False, "credit_card_enabled": True}, "browser": {"x": 1}}


def test_launch_seeds_the_throwaway_profile(env, pw):
    browser = clearcote.launch(executable_path=env["exe"], headless=False)
    udd, autofill = pw[-1]
    assert os.path.basename(udd).startswith("clearcote-run-")
    assert autofill == ON
    browser.close()
    assert os.listdir(env["temp"]) == []


def test_incognito_launch_writes_nothing(tmp_path, env, pw):
    clearcote.launch(executable_path=env["exe"], headless=False, ephemeral_profile=False)
    assert pw[-1] == (None, None)
    assert os.listdir(env["temp"]) == []
    assert sorted(os.listdir(tmp_path)) == ["engine", "home", "temp"]


async def test_async_launch_persistent_context_seeds(tmp_path, env, monkeypatch):
    seen = []

    class _AsyncCtx(_Ctx):
        pass

    class _Chromium:
        async def launch_persistent_context(self, user_data_dir, **kw):
            seen.append(_autofill_or_none(user_data_dir))
            return _AsyncCtx()

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
    udd = tmp_path / "profile"
    await async_api.launch_persistent_context(str(udd), executable_path=env["exe"], headless=False)
    assert seen == [ON]


async def test_async_incognito_launch_writes_nothing(tmp_path, env, monkeypatch):
    class _Browser:
        def on(self, *a):
            pass

    class _Chromium:
        async def launch(self, **kw):
            return _Browser()

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
    for name in ("install_humanize", "install_humanize_on_context"):
        if hasattr(async_api, name):
            monkeypatch.setattr(async_api, name, _noop)
    try:
        await async_api.launch(executable_path=env["exe"], headless=False)
    except Exception:  # noqa: BLE001 -- the stand-in browser is minimal; only what it wrote matters here
        pass
    assert os.listdir(env["temp"]) == []


@pytest.mark.parametrize("own", [False, True], ids=["caller's directory", "its own"])
def test_serve_seeds_before_the_browser_starts(tmp_path, env, monkeypatch, own):
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
        udd = next(a for a in cmd if a.startswith("--user-data-dir=")).split("=", 1)[1]
        spawned.append((udd, _autofill_or_none(udd)))
        return _Proc()

    monkeypatch.setattr(clearcote, "_win_av_retry", lambda start, exe: start(exe))
    monkeypatch.setattr(_serve.subprocess, "Popen", _popen)
    monkeypatch.setattr(_serve.urllib.request, "urlopen", lambda *a, **k: None)
    kw = {} if own else {"user_data_dir": str(tmp_path / "served")}
    srv = clearcote.serve(executable_path=env["exe"], headless=False, quiet=True, ready_timeout=5, **kw)
    try:
        udd, autofill = spawned[-1]
        assert autofill == ON
        if not own:
            assert udd == str(tmp_path / "served")
    finally:
        srv.close()
