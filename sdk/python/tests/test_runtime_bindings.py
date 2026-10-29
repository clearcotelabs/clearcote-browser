"""runtime_bindings / CLEARCOTE_RUNTIME_BINDINGS: Runtime.addBinding bindings across navigations, on request.

Patch 110 keeps CDP bindings out of documents created after the binding was added, so Playwright's expose_function()
and expose_binding() fail after page.goto() (clearcote-browser#43). Engine r54 (pro-r54-04) adds
--enable-runtime-bindings, which restores only that. Off by default, gated on the binary like stock_runtime, never
part of the persona payload; redundant next to stock_runtime, which already restores it.

Hermetic, on the stand-ins of test_stock_runtime.py. Mirrors sdk/node/test/runtime-bindings.test.ts and
sdk/dotnet/tests/Clearcote.Tests/RuntimeBindingsTests.cs.
"""
import asyncio

import pytest

import clearcote
import clearcote.async_api as async_api
from clearcote import _docker, _warnings
from clearcote import _personaenv as P
from test_stock_runtime import (  # noqa: F401  (pytest fixtures, used by name)
    API_KEY, ENGINE_1021, ENGINE_R32, LOCAL, _local, api, async_driver, driver, fake_engine, served,
)

SWITCH = "--enable-runtime-bindings"
STOCK = "--disable-runtime-suppression"
ENV = "CLEARCOTE_RUNTIME_BINDINGS"
ENGINE_R54 = b"\x00enable-runtime-bindings\x00"
UNSUPPORTED = "runtime_bindings (CLEARCOTE_RUNTIME_BINDINGS) is on, but this engine does not support it"
CLOUD_ONLY_LOCAL = "runtime_bindings (CLEARCOTE_RUNTIME_BINDINGS) only applies to local and Docker launches"
BINDINGS_NOTE = "page.expose_function()"  # in the engine note about bindings
ONCE_CODES = ("runtime-bindings-unsupported", "runtime-bindings-cloud", "cdp-bindings", "cdp-console-events",
              "stock-runtime-unsupported", "stock-runtime-cloud")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("CLEARCOTE_LICENSE_KEY", ENV, "CLEARCOTE_STOCK_RUNTIME", "CLEARCOTE_NO_WARN", P.OPT_OUT_ENV,
                P.ENV_VAR, "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION"):
        monkeypatch.delenv(var, raising=False)
    for code in ONCE_CODES:
        _warnings._seen_notes.discard(code)
    yield
    for code in ONCE_CODES:
        _warnings._seen_notes.discard(code)


def _persistent(driver, tmp_path, exe, **kw):
    kw.setdefault("quiet", True)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, headless=True, **kw)
    return driver["args"]


@pytest.mark.parametrize("entry", LOCAL)
def test_on_with_an_r54_engine_the_switch_is_there_exactly_once(entry, driver, async_driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_R32, ENGINE_R54)
    seen = _local(entry, tmp_path, exe, driver, async_driver, runtime_bindings=True)
    assert seen["args"].count(SWITCH) == 1 and STOCK not in seen["args"]
    assert "runtime_bindings" not in seen  # an SDK option, never a Playwright one
    seen = _local(entry, tmp_path, exe, driver, async_driver, runtime_bindings=True, args=[SWITCH, "--lang=de-DE"])
    assert seen["args"].count(SWITCH) == 1 and "--lang=de-DE" in seen["args"]


def test_off_by_default_and_the_environment_variable(driver, tmp_path, monkeypatch):
    exe = fake_engine(tmp_path, ENGINE_R54)
    assert SWITCH not in _persistent(driver, tmp_path, exe)
    assert SWITCH not in _persistent(driver, tmp_path, exe, runtime_bindings=False)
    for value, on in (("1", True), ("true", True), (" on ", True), ("0", False), ("", False), ("off", False)):
        monkeypatch.setenv(ENV, value)
        assert _persistent(driver, tmp_path, exe).count(SWITCH) == (1 if on else 0), value
    monkeypatch.setenv(ENV, "1")
    assert SWITCH not in _persistent(driver, tmp_path, exe, runtime_bindings=False)  # the option wins
    monkeypatch.setenv(ENV, "0")
    assert _persistent(driver, tmp_path, exe, runtime_bindings=True).count(SWITCH) == 1


def test_not_added_next_to_stock_runtime_which_already_restores_it(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_R32, ENGINE_R54)
    args = _persistent(driver, tmp_path, exe, runtime_bindings=True, stock_runtime=True)
    assert args.count(STOCK) == 1 and SWITCH not in args


def test_an_engine_without_it_launches_without_it_and_says_so_once(driver, tmp_path, capsys):
    exe = fake_engine(tmp_path, ENGINE_R32)  # r32..r53, or an open build without the switch
    for _ in range(2):
        assert SWITCH not in _persistent(driver, tmp_path, exe, runtime_bindings=True, quiet=False)
    err = capsys.readouterr().err
    assert err.count(UNSUPPORTED) == 1, err
    line = next(x for x in err.splitlines() if UNSUPPORTED in x)
    assert line.startswith("clearcote: warning: ") and "r54" in line and "stock_runtime=True" in line


def test_quiet_and_no_warn_silence_it_without_using_it_up(driver, tmp_path, capsys, monkeypatch):
    exe = fake_engine(tmp_path)
    _persistent(driver, tmp_path, exe, runtime_bindings=True)
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    _persistent(driver, tmp_path, exe, runtime_bindings=True, quiet=False)
    assert UNSUPPORTED not in capsys.readouterr().err
    monkeypatch.delenv("CLEARCOTE_NO_WARN")
    _persistent(driver, tmp_path, exe, runtime_bindings=True, quiet=False)
    assert capsys.readouterr().err.count(UNSUPPORTED) == 1


def test_serve(served, tmp_path, capsys):
    assert served(fake_engine(tmp_path, ENGINE_R54, name="new"), runtime_bindings=True).count(SWITCH) == 1
    assert SWITCH not in served(fake_engine(tmp_path, ENGINE_R54, name="new"))
    assert SWITCH not in served(fake_engine(tmp_path, name="old"), runtime_bindings=True, quiet=False)
    assert capsys.readouterr().err.count(UNSUPPORTED) == 1


def test_it_stays_on_the_command_line_when_the_persona_moves_to_the_environment(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_1021, ENGINE_R54)
    args = _persistent(driver, tmp_path, exe, fingerprint="17", platform="windows", runtime_bindings=True)
    assert "--persona-from-env" in args and args.count(SWITCH) == 1
    assert "enable-runtime-bindings" not in [name for name, _ in P.decode(driver["env"][P.ENV_VAR])]
    assert not P.is_transported("enable-runtime-bindings")


def test_docker_hands_the_switch_to_the_images_entrypoint(monkeypatch):
    from clearcote._docker import container_env
    assert "CC_EXTRA_ARGS" not in container_env({"runtime_bindings": False})
    assert container_env({"runtime_bindings": True})["CC_EXTRA_ARGS"] == SWITCH
    assert container_env({"runtime_bindings": True, "args": [SWITCH]})["CC_EXTRA_ARGS"] == SWITCH  # once
    assert container_env({"runtime_bindings": True, "stock_runtime": True})["CC_EXTRA_ARGS"] == STOCK + " " + SWITCH
    monkeypatch.setenv(ENV, "1")
    assert container_env({})["CC_EXTRA_ARGS"] == SWITCH


@pytest.mark.parametrize("sync", [True, False], ids=["sync", "async"])
def test_a_docker_launch_takes_the_option(monkeypatch, sync):
    seen = {}
    real = _docker.container_env

    def spy(kwargs):
        seen["env"] = real(kwargs)
        raise RuntimeError("stopped by the test")
    monkeypatch.setattr(_docker, "container_env", spy)
    with pytest.raises(RuntimeError, match="stopped by the test"):
        if sync:
            clearcote.launch(docker=True, runtime_bindings=True, headless=True)
        else:
            asyncio.run(async_api.launch(docker=True, runtime_bindings=True, headless=True))
    assert seen["env"]["CC_EXTRA_ARGS"] == SWITCH


def test_a_cloud_launch_does_not_take_it_and_says_so_once(api, driver, capsys):  # noqa: F811
    clearcote.launch(cloud=True, runtime_bindings=True, country="us").close()
    clearcote.launch(cloud=True, runtime_bindings=True).close()
    bodies = [r["body"] for r in api.requests("POST", "/api/v1/browsers")]
    assert not [k for b in bodies for k in b if "runtime" in k.lower() or "binding" in k.lower()]
    assert capsys.readouterr().err.count(CLOUD_ONLY_LOCAL) == 1


def test_the_bindings_note_names_the_option_and_is_not_said_when_bindings_are_restored(driver, tmp_path, capsys):
    exe = fake_engine(tmp_path, ENGINE_R32, ENGINE_R54)
    _persistent(driver, tmp_path, exe, runtime_bindings=True, quiet=False)
    err = capsys.readouterr().err
    assert BINDINGS_NOTE not in err and "page.on('console')" in err  # console events stay suppressed
    _persistent(driver, tmp_path, exe, stock_runtime=True, quiet=False)
    assert BINDINGS_NOTE not in capsys.readouterr().err
    _persistent(driver, tmp_path, exe, quiet=False)
    note = [x for x in capsys.readouterr().err.splitlines() if BINDINGS_NOTE in x]
    assert len(note) == 1 and "runtime_bindings=True" in note[0] and "r54" in note[0]
