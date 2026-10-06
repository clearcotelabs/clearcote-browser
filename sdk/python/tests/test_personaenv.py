"""Engine patches 1021 (persona in the environment, "env mode") and 1022 (--widevine-cdm-path), SDK side.

Both are gated on the engine: they change a launch only when the binary implements the switch, so an
r30/r31 engine launches exactly as before. Hermetic: Playwright is replaced by a stand-in that records
what it was asked to launch, and the "engine" is a small file carrying (or not) the switch names the probe
looks for.
"""
import asyncio
import base64

import pytest

import clearcote
import clearcote.async_api as async_api
from clearcote import _personaenv as P
from clearcote import _widevine as W

ENGINE_1021 = b"\x00persona-from-env\x00"
ENGINE_1022 = b"\x00widevine-cdm-path\x00"
ON = lambda exe, name: True  # noqa: E731 - probe stubs
OFF = lambda exe, name: False  # noqa: E731


def fake_engine(tmp_path, *markers, name="chrome"):
    p = tmp_path / name
    p.write_bytes(b"\x7fELF" + b"".join(markers) + b"\x00--remote-debugging-port\x00")
    return str(p)


@pytest.fixture(autouse=True)
def _no_licence_no_opt_out(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("CLEARCOTE_LICENSE_KEY", P.OPT_OUT_ENV, P.ENV_VAR):
        monkeypatch.delenv(var, raising=False)


# -- the payload and the switch list ------------------------------------------------------------

def test_payload_is_the_engines_format():
    entries = [("fingerprint", "17"), ("disable-canvas-noise", ""), ("fingerprint-timezone", "Europe/Zürich")]
    raw = b"fingerprint\x0017\x00disable-canvas-noise\x00\x00fingerprint-timezone\x00Europe/Z\xc3\xbcrich\x00"
    assert P.encode(entries) == base64.b64encode(raw).decode("ascii")
    assert P.decode(P.encode(entries)) == entries


@pytest.mark.parametrize("name", ["fingerprint", "fingerprint-platform", "fingerprint-profile", "canvas-bridge-url",
                                  "canvas-bridge-auth", "disable-canvas-noise", "disable-fingerprint-noise",
                                  "disable-fingerprint-voices", "disable-gpu-fingerprint", "disable-gpu-string-spoof",
                                  "proxy-auth", "socks5-credentials", "webrtc-ip"])
def test_the_engines_transported_switches(name):
    assert P.is_transported(name)


@pytest.mark.parametrize("name", ["proxy-server", "lang", "user-data-dir", "fingerprinting", "disable-features",
                                  "persona-from-env", "disable-persona-env-transport", "headless", "canvas-bridge"])
def test_everything_else_stays_on_the_command_line(name):
    # The engine stops the launch on a name outside its list: the SDK's list must never be wider.
    assert not P.is_transported(name)


# -- apply() ------------------------------------------------------------------------------------

def test_persona_switches_move_into_the_environment():
    args = ["--fingerprint=17", "--lang=en-US", "--fingerprint-platform=windows", "--socks5-credentials=u:p",
            "--no-first-run", "https://example.com/"]
    base = {"KEEP": "1"}
    new_args, env = P.apply("chrome", args, base, supports=ON)
    assert new_args == ["--lang=en-US", "--no-first-run", "https://example.com/", "--persona-from-env"]
    assert P.decode(env[P.ENV_VAR]) == [("fingerprint", "17"), ("fingerprint-platform", "windows"),
                                        ("socks5-credentials", "u:p")]
    assert env["KEEP"] == "1"
    assert P.ENV_VAR not in base  # the caller's dict is not touched


def test_the_process_environment_is_the_base(monkeypatch):
    monkeypatch.setenv("CC_TEST_MARKER", "1")
    _args, env = P.apply("chrome", ["--fingerprint=1"], None, supports=ON)
    assert env["CC_TEST_MARKER"] == "1" and P.ENV_VAR in env


def test_the_last_copy_of_a_repeated_switch_wins():
    # On a command line Chromium keeps the LAST copy; the engine adopts the FIRST payload entry.
    args = ["--fingerprint-hardware-concurrency=8", "--fingerprint=1", "--fingerprint-hardware-concurrency=4"]
    _args, env = P.apply("chrome", args, {}, supports=ON)
    assert P.decode(env[P.ENV_VAR]) == [("fingerprint", "1"), ("fingerprint-hardware-concurrency", "4")]


def test_inert_on_an_engine_without_the_switch():
    args, base = ["--fingerprint=17", "--lang=en-US"], {"KEEP": "1"}
    new_args, env = P.apply("chrome", args, base, supports=OFF)
    assert new_args == args and env is base


@pytest.mark.parametrize("enabled,env_value", [(False, None), (None, "0"), (None, "false"), (None, "off")])
def test_turned_off(monkeypatch, enabled, env_value):
    if env_value is not None:
        monkeypatch.setenv(P.OPT_OUT_ENV, env_value)
    args = ["--fingerprint=17"]
    assert P.apply("chrome", args, None, enabled=enabled, supports=ON) == (args, None)


def test_the_option_beats_the_environment_variable(monkeypatch):
    monkeypatch.setenv(P.OPT_OUT_ENV, "0")
    new_args, env = P.apply("chrome", ["--fingerprint=17"], {}, enabled=True, supports=ON)
    assert new_args == ["--persona-from-env"] and P.ENV_VAR in env


@pytest.mark.parametrize("chosen", ["--disable-persona-env-transport", "--persona-from-env"])
def test_a_transport_the_caller_chose_is_left_alone(chosen):
    args = ["--fingerprint=17", chosen]
    assert P.apply("chrome", args, None, supports=ON) == (args, None)


def test_nothing_to_move_does_not_even_probe():
    def probe(exe, name):
        raise AssertionError("probed without a persona switch to move")
    args = ["--lang=en-US", "--no-first-run"]
    assert P.apply("chrome", args, None, supports=probe) == (args, None)


def test_an_oversized_persona_stays_on_the_command_line():
    args = ["--fingerprint=17", "--fingerprint-profile=" + "A" * 40000]
    assert P.apply("chrome", args, None, supports=ON) == (args, None)


def test_the_real_probe_reads_the_engine(tmp_path):
    with_1021, without = fake_engine(tmp_path, ENGINE_1021, name="new"), fake_engine(tmp_path, name="old")
    assert P.apply(with_1021, ["--fingerprint=17"], {})[0] == ["--persona-from-env"]
    assert P.apply(without, ["--fingerprint=17"], {})[0] == ["--fingerprint=17"]


# -- the launch entry points ------------------------------------------------------------------

class _FakeBrowser:
    def on(self, *a, **k):
        pass

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self):
        pass


@pytest.fixture
def driver(monkeypatch):
    seen = {}

    class _Chromium:
        def launch(self, **kw):
            seen.update(kw)
            return _FakeBrowser()

        def launch_persistent_context(self, user_data_dir, **kw):
            seen.update(kw)
            return _FakeBrowser()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    return seen


def _persona(seen):
    env = seen.get("env") or {}
    return P.decode(env[P.ENV_VAR]) if P.ENV_VAR in env else None


def test_persistent_context_on_a_1021_engine(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_1021)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, fingerprint="17",
                                        platform="windows", headless=True, quiet=True)
    assert "--persona-from-env" in driver["args"]
    assert not [a for a in driver["args"] if a.startswith("--fingerprint")]
    assert ("fingerprint", "17") in _persona(driver)


def test_persistent_context_on_an_older_engine_is_unchanged(driver, tmp_path):
    exe = fake_engine(tmp_path)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, fingerprint="17",
                                        platform="windows", headless=True, quiet=True)
    assert "--fingerprint=17" in driver["args"] and "--persona-from-env" not in driver["args"]
    assert _persona(driver) is None


def test_plain_launch_on_a_1021_engine(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_1021)
    clearcote.launch(executable_path=exe, fingerprint="17", headless=True, quiet=True, ephemeral_profile=False)
    assert "--persona-from-env" in driver["args"] and ("fingerprint", "17") in _persona(driver)


def test_persona_env_false_keeps_the_command_line(driver, tmp_path):
    exe = fake_engine(tmp_path, ENGINE_1021)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, fingerprint="17",
                                        headless=True, quiet=True, persona_env=False)
    assert "--fingerprint=17" in driver["args"] and _persona(driver) is None


def test_async_launch_on_a_1021_engine(monkeypatch, tmp_path):
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
            seen.update(kw)
            return _Browser()

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def start():
        return _PW()

    monkeypatch.setattr(async_api, "_start_driver", start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda browser, pw: None)

    async def no_humanize(*a, **k):
        return None
    monkeypatch.setattr(async_api, "install_humanize", no_humanize)
    exe = fake_engine(tmp_path, ENGINE_1021)
    asyncio.run(async_api.launch(executable_path=exe, fingerprint="17", headless=True, quiet=True,
                                 ephemeral_profile=False))
    assert "--persona-from-env" in seen["args"] and ("fingerprint", "17") in _persona(seen)


# -- 1022: --widevine-cdm-path ----------------------------------------------------------------

def test_widevine_cdm_path_only_on_an_engine_that_has_it(monkeypatch, tmp_path):
    cdm = str(tmp_path / "WidevineCdm" / "4.10.2830.0")
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: cdm)
    assert W.widevine_cdm_args(fake_engine(tmp_path, ENGINE_1022, name="new")) == ["--widevine-cdm-path=" + cdm]
    assert W.widevine_cdm_args(fake_engine(tmp_path, name="old")) == []


def test_widevine_cdm_path_is_best_effort(monkeypatch, tmp_path):
    def broken(dest=None, quiet=False):
        raise OSError("no network")
    monkeypatch.setattr(W, "fetch_widevine", broken)
    assert W.widevine_cdm_args(fake_engine(tmp_path, ENGINE_1022)) == []


def test_persistent_widevine_launch_passes_the_cdm_path(driver, monkeypatch, tmp_path):
    cdm = str(tmp_path / "cdm")
    monkeypatch.setattr(clearcote, "apply_widevine_launch", lambda udd, kwargs, quiet=False: None)
    monkeypatch.setattr(W, "fetch_widevine", lambda dest=None, quiet=False: cdm)
    exe = fake_engine(tmp_path, ENGINE_1022)
    clearcote.launch_persistent_context(str(tmp_path / "udd"), executable_path=exe, headless=True, quiet=True,
                                        widevine=True)
    assert "--widevine-cdm-path=" + cdm in driver["args"]
