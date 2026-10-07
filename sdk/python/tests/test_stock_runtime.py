"""stock_runtime / CLEARCOTE_STOCK_RUNTIME: Chromium's own DevTools Runtime behaviour, back on request.

The engine holds back part of what V8 reports to a DevTools client (patch 110). Engine r32 adds
--disable-runtime-suppression, which restores stock Chromium for it: Playwright's console and pageerror events,
set_content() and expose_function() across navigations. The option is off by default and, like every new engine
switch, gated on the binary: an engine without the switch launches without it, with one warning per process.
It is a normal browser argument, never part of the persona payload; a Docker launch hands it to the image's
entrypoint, which applies the same gate; a cloud browser does not take it.

Hermetic: Playwright and the browser process are stand-ins that record what they were asked to launch, and the
"engine" is a small file carrying (or not) the switch names the probe looks for. Mirrors
sdk/node/test/stock-runtime.test.ts and sdk/dotnet/tests/Clearcote.Tests/StockRuntimeTests.cs.
"""
import asyncio
import json
import os
import subprocess
import sys

import pytest

import clearcote
import clearcote.async_api as async_api
from clearcote import _docker, _warnings
from clearcote import _personaenv as P
from _fake_cloud import API_KEY, start_fake_cloud, stop_fake_cloud

SWITCH = "--disable-runtime-suppression"
ENV = "CLEARCOTE_STOCK_RUNTIME"
ENGINE_R32 = b"\x00disable-runtime-suppression\x00"
ENGINE_1021 = b"\x00persona-from-env\x00"
UNSUPPORTED = "this engine does not support it"  # in the one-time warning
CLOUD_ONLY_LOCAL = "only applies to local and Docker launches"  # in the cloud's one-time warning
CONSOLE_NOTE = "page.on('console')"  # in the engine note about console events
ONCE_CODES = ("stock-runtime-unsupported", "stock-runtime-cloud", "cdp-console-events")


def fake_engine(tmp_path, *markers, name="chrome"):
    d = tmp_path / ("engine-" + name)
    d.mkdir(exist_ok=True)
    p = d / "chrome"
    p.write_bytes(b"\x7fELF" + b"".join(markers) + b"\x00--remote-debugging-port\x00")
    return str(p)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # No licence (a launch would take a real lease), nothing inherited from the shell, and every once-per-process
    # line this file looks at not said yet.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("CLEARCOTE_LICENSE_KEY", ENV, "CLEARCOTE_NO_WARN", P.OPT_OUT_ENV, P.ENV_VAR, "CLEARCOTE_CLOUD",
                "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"):
        monkeypatch.delenv(var, raising=False)
    for code in ONCE_CODES:
        _warnings._seen_notes.discard(code)
    yield
    for code in ONCE_CODES:
        _warnings._seen_notes.discard(code)


class _FakeContext:
    def __init__(self):
        self.pages = []

    def on(self, *a, **k):
        pass

    def add_init_script(self, *a, **k):
        pass


class _FakeBrowser:
    def __init__(self):
        self.contexts = [_FakeContext()]

    def on(self, *a, **k):
        pass

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self, *a, **k):
        pass


@pytest.fixture
def driver(monkeypatch):
    """Playwright, replaced: records the keyword arguments of the last launch (args, env, ...)."""
    seen = {}

    class _Chromium:
        def launch(self, **kw):
            seen.clear()
            seen.update(kw)
            return _FakeBrowser()

        def launch_persistent_context(self, user_data_dir, **kw):
            seen.clear()
            seen.update(kw)
            return _FakeBrowser()

        def connect_over_cdp(self, url, **kw):
            seen.clear()
            seen.update(kw, url=url)
            return _FakeBrowser()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    return seen


@pytest.fixture
def async_driver(monkeypatch):
    seen = {}

    class _Browser:
        def on(self, *a, **k):
            pass

        async def new_page(self, **kw):
            return kw

        async def new_context(self, **kw):
            return kw

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kw):
            seen.clear()
            seen.update(kw)
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def start():
        return _PW()

    async def no_humanize(*a, **k):
        return None

    monkeypatch.setattr(async_api, "_start_driver", start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda browser, pw: None)
    monkeypatch.setattr(async_api, "install_humanize", no_humanize)
    return seen


@pytest.fixture
def served(monkeypatch, tmp_path):
    """serve() up to a stand-in browser process: the command line it spawns."""
    import clearcote._geometry as _geometry
    import clearcote._serve as _serve
    monkeypatch.setattr(_geometry, "fit_served_window", lambda *a, **k: None)
    monkeypatch.setattr(_serve.urllib.request, "urlopen", lambda *a, **k: None)  # the endpoint is "up"
    lines = []

    class Proc:
        pid = 4242

        def __init__(self, cmd, **_kw):
            lines.append(cmd)

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(_serve.subprocess, "Popen", Proc)

    def run(exe, **kwargs):
        kwargs.setdefault("quiet", True)
        clearcote.serve(executable_path=exe, user_data_dir=str(tmp_path / "serve-udd"), **kwargs).close()
        return lines[-1]
    return run


def _local(entry, tmp_path, exe, driver, async_driver, **kw):
    """Launch through ``entry`` and return what Playwright was asked to launch with."""
    kw.setdefault("quiet", True)
    if entry == "launch":  # the throwaway profile (a persistent context)
        clearcote.launch(executable_path=exe, headless=True, **kw).close()
        return driver
    if entry == "launch-incognito":
        clearcote.launch(executable_path=exe, headless=True, ephemeral_profile=False, **kw)
        return driver
    if entry == "persistent":
        clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, headless=True, **kw)
        return driver
    if entry == "async":
        asyncio.run(async_api.launch(executable_path=exe, headless=True, ephemeral_profile=False, **kw))
        return async_driver
    raise AssertionError(entry)


LOCAL = ["launch", "launch-incognito", "persistent", "async"]


# -- on, off, and the environment variable --------------------------------------------------------

@pytest.mark.parametrize("entry", LOCAL)
def test_on_with_an_r32_engine_the_switch_is_there_exactly_once(entry, driver, async_driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_R32)
    seen = _local(entry, tmp_path, exe, driver, async_driver, stock_runtime=True)
    assert seen["args"].count(SWITCH) == 1
    assert "stock_runtime" not in seen  # an SDK option, never a Playwright one
    # the caller passed it too: still once
    seen = _local(entry, tmp_path, exe, driver, async_driver, stock_runtime=True, args=[SWITCH, "--lang=de-DE"])
    assert seen["args"].count(SWITCH) == 1 and "--lang=de-DE" in seen["args"]


def test_off_by_default(driver, tmp_path, monkeypatch):
    exe = fake_engine(tmp_path, ENGINE_R32)
    udd = str(tmp_path / "udd")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True)
    assert SWITCH not in driver["args"]
    for off in (None, False):
        clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True, stock_runtime=off)
        assert SWITCH not in driver["args"] and "stock_runtime" not in driver
    monkeypatch.setenv(ENV, "0")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True, stock_runtime=None)
    assert SWITCH not in driver["args"] and "stock_runtime" not in driver


def test_the_environment_variable(driver, tmp_path, monkeypatch):
    exe = fake_engine(tmp_path, ENGINE_R32)
    udd = str(tmp_path / "udd")
    for value, on in (("1", True), ("true", True), ("YES", True), (" on ", True),
                      ("0", False), ("", False), ("no", False), ("off", False), ("2", False)):
        monkeypatch.setenv(ENV, value)
        clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True)
        assert driver["args"].count(SWITCH) == (1 if on else 0), value
    # an explicit option wins over the variable, both ways
    monkeypatch.setenv(ENV, "1")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True, stock_runtime=False)
    assert SWITCH not in driver["args"] and "stock_runtime" not in driver
    monkeypatch.setenv(ENV, "0")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True, stock_runtime=True)
    assert driver["args"].count(SWITCH) == 1


# -- an engine without the switch -------------------------------------------------------------------

def test_an_engine_without_it_launches_without_it_and_says_so_once(driver, tmp_path, capsys):
    exe = fake_engine(tmp_path)  # r31 / an open build: no such switch
    udd = str(tmp_path / "udd")
    for _ in range(2):
        clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, stock_runtime=True)
        assert SWITCH not in driver["args"] and "stock_runtime" not in driver
    err = capsys.readouterr().err
    assert err.count(UNSUPPORTED) == 1, err
    line = next(x for x in err.splitlines() if UNSUPPORTED in x)
    assert line.startswith("clearcote: warning: ") and "stock_runtime" in line and "r32" in line


def test_quiet_and_no_warn_silence_it_without_using_it_up(driver, tmp_path, capsys, monkeypatch):
    exe = fake_engine(tmp_path)
    udd = str(tmp_path / "udd")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, quiet=True, stock_runtime=True)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, stock_runtime=True)
    assert UNSUPPORTED not in capsys.readouterr().err
    assert "stock_runtime" not in driver
    monkeypatch.delenv("CLEARCOTE_NO_WARN")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, stock_runtime=True)
    assert capsys.readouterr().err.count(UNSUPPORTED) == 1


# -- serve() ----------------------------------------------------------------------------------------

def test_serve(served, tmp_path, capsys):
    line = served(fake_engine(tmp_path, ENGINE_R32, name="new"), stock_runtime=True)
    assert line.count(SWITCH) == 1
    assert SWITCH not in served(fake_engine(tmp_path, ENGINE_R32, name="new"))  # off by default
    line = served(fake_engine(tmp_path, name="old"), stock_runtime=True, quiet=False)
    assert SWITCH not in line
    assert capsys.readouterr().err.count(UNSUPPORTED) == 1


# -- not a persona switch ---------------------------------------------------------------------------

def test_it_stays_on_the_command_line_when_the_persona_moves_to_the_environment(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_1021, ENGINE_R32)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, fingerprint="17",
                                        platform="windows", headless=True, quiet=True, stock_runtime=True)
    args = driver["args"]
    assert "--persona-from-env" in args  # the persona did move...
    assert args.count(SWITCH) == 1  # ...and this switch did not
    entries = P.decode(driver["env"][P.ENV_VAR])
    assert ("fingerprint", "17") in entries
    assert "disable-runtime-suppression" not in [name for name, _ in entries]
    assert not P.is_transported("disable-runtime-suppression")


# -- Docker -----------------------------------------------------------------------------------------

def test_docker_hands_the_switch_to_the_images_entrypoint(monkeypatch):
    from clearcote._docker import container_env
    assert "CC_EXTRA_ARGS" not in container_env({"fingerprint": "17"})
    assert "CC_EXTRA_ARGS" not in container_env({"fingerprint": "17", "stock_runtime": False})
    assert container_env({"stock_runtime": True})["CC_EXTRA_ARGS"] == SWITCH
    assert container_env({"stock_runtime": True, "args": ["--lang=de-DE"]})["CC_EXTRA_ARGS"] == "--lang=de-DE " + SWITCH
    assert container_env({"stock_runtime": True, "args": [SWITCH, "--lang=de-DE"]})["CC_EXTRA_ARGS"] == \
        SWITCH + " --lang=de-DE"  # once
    monkeypatch.setenv(ENV, "1")
    assert container_env({})["CC_EXTRA_ARGS"] == SWITCH
    assert "CC_EXTRA_ARGS" not in container_env({"stock_runtime": False})  # the option wins


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
def test_a_docker_launch_takes_the_option(monkeypatch, sync):
    # Stop the launch right after the container's environment is built: what launch() hands the image.
    seen = {}
    real = _docker.container_env

    def spy(kwargs):
        seen["env"] = real(kwargs)
        raise RuntimeError("stopped by the test")
    monkeypatch.setattr(_docker, "container_env", spy)
    with pytest.raises(RuntimeError, match="stopped by the test"):
        if sync:
            clearcote.launch(docker=True, stock_runtime=True, headless=True)
        else:
            asyncio.run(async_api.launch(docker=True, stock_runtime=True, headless=True))
    assert seen["env"]["CC_EXTRA_ARGS"] == SWITCH


# -- cloud ------------------------------------------------------------------------------------------

@pytest.fixture
def api(monkeypatch):
    a = start_fake_cloud()
    monkeypatch.setenv("CLEARCOTE_API_KEY", API_KEY)
    monkeypatch.setenv("CLEARCOTE_API_URL", a.url)
    yield a
    stop_fake_cloud(a)


def test_a_cloud_launch_does_not_take_it_and_says_so_once(api, driver, capsys, monkeypatch):
    clearcote.launch(cloud=True, stock_runtime=True, country="us").close()
    clearcote.launch_persistent_context(cloud=True, profile="acct-1", stock_runtime=True).close()
    bodies = [r["body"] for r in api.requests("POST", "/api/v1/browsers")]
    assert bodies[0] == {"country": "us"}  # nothing about it reaches the API
    assert not [k for b in bodies for k in b if "runtime" in k.lower()]
    err = capsys.readouterr().err
    assert err.count(CLOUD_ONLY_LOCAL) == 1, err
    assert next(x for x in err.splitlines() if CLOUD_ONLY_LOCAL in x).startswith("clearcote: warning: stock_runtime")
    # the environment variable too
    _warnings._seen_notes.discard("stock-runtime-cloud")
    monkeypatch.setenv(ENV, "1")
    clearcote.launch(cloud=True).close()
    assert capsys.readouterr().err.count(CLOUD_ONLY_LOCAL) == 1
    _warnings._seen_notes.discard("stock-runtime-cloud")
    clearcote.launch(cloud=True, quiet=True, stock_runtime=True).close()  # quiet: not said
    assert CLOUD_ONLY_LOCAL not in capsys.readouterr().err


# -- the console note -------------------------------------------------------------------------------

def test_the_console_note_names_the_option_and_is_not_said_when_the_switch_is_on(driver, tmp_path, capsys):
    exe = fake_engine(tmp_path, ENGINE_R32)
    udd = str(tmp_path / "udd")
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True, stock_runtime=True)
    assert CONSOLE_NOTE not in capsys.readouterr().err  # this browser does forward them
    clearcote.launch_persistent_context(udd, executable_path=exe, headless=True)
    note = [x for x in capsys.readouterr().err.splitlines() if CONSOLE_NOTE in x]
    assert len(note) == 1 and "stock_runtime=True" in note[0] and "r32" in note[0]


# -- the image's entrypoint (docker/serve.py) applies the same gate -------------------------------

SERVE_PY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "docker",
                                         "serve.py"))


@pytest.mark.skipif(sys.platform != "linux", reason="runs the image's Linux entrypoint with a shell stand-in engine")
@pytest.mark.skipif(not os.path.exists(SERVE_PY), reason="no docker/serve.py in this tree")
@pytest.mark.parametrize("r32", [True, False], ids=["r32-engine", "older-engine"])
def test_the_images_entrypoint_keeps_the_switch_only_on_an_engine_that_has_it(tmp_path, r32):
    # A stand-in engine writes down its arguments, then exits; the probe's literal sits after the exit, where the
    # shell never reads. socat is a stand-in too; CC_HEADLESS skips Xvfb.
    engine = tmp_path / "engine" / "chrome"
    engine.parent.mkdir()
    engine.write_bytes(b'#!/bin/sh\nd=$(dirname "$0")\nprintf \'%s\\n\' "$@" > "$d/argv"\nexit 3\n'
                       + (ENGINE_R32 if r32 else b"") + b"\n")
    engine.chmod(0o755)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "socat").write_text("#!/bin/sh\nexit 0\n")
    (stubs / "socat").chmod(0o755)
    sdk = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    env = {"PATH": str(stubs) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": sdk,
           "HOME": str(tmp_path / "home"), "TMPDIR": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "xdg"),
           "CLEARCOTE_BINARY": str(engine), "CC_HEADLESS": "1", "CC_PROFILE_DIR": str(tmp_path / "profile"),
           "CC_FINGERPRINT": "17", "CC_PLATFORM": "linux", "CC_EXTRA_ARGS": "--lang=de-DE " + SWITCH}
    run = subprocess.run([sys.executable, SERVE_PY], env=env, capture_output=True, text=True, timeout=180)
    assert run.returncode == 3, run.stdout + run.stderr  # the stand-in's own exit code: it ran
    argv = (engine.parent / "argv").read_text().splitlines()
    assert "--lang=de-DE" in argv  # the other extra args are untouched either way
    assert argv.count(SWITCH) == (1 if r32 else 0)
    warned = [x for x in run.stdout.splitlines() if "WARNING" in x and "disable-runtime-suppression" in x]
    assert len(warned) == (0 if r32 else 1), run.stdout
    state = json.loads(next(x for x in run.stdout.splitlines() if "serve-state " in x).split("serve-state ", 1)[1])
    assert state["stock_runtime"] is r32  # what the entrypoint says it did
