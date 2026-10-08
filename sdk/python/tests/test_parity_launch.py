"""Launch behaviour (mirrors sdk/node/test/parity-launch.test.ts): GPU defaults,
new engine-switch gating, pass-through, voices, third-party cookies, transparent proxy, release
channel, serve as root."""
import os
import sys
import warnings

import pytest

import clearcote
from clearcote._fingerprint import FINGERPRINT_KEYS, fingerprint_args, is_fingerprint_passthrough
from clearcote._launchopts import (
    DEFAULT_IGNORED_ARGS,
    GATED_ENGINE_SWITCHES,
    _SWITCH_CACHE,
    engine_extras_args,
    gate_engine_switches,
    gpu_backend_args,
    gpu_blocklist_args,
    mesa_egl_available,
    serve_infobar_args,
    x_display_available,
    serve_needs_no_sandbox,
)
from clearcote.download import pro_download_url, resolve_release_channel


@pytest.fixture(autouse=True)
def _clear_switch_cache():
    _SWITCH_CACHE.clear()
    yield
    _SWITCH_CACHE.clear()


def fake_engine(tmp_path, switches):
    """A fake engine binary containing exactly these NUL-delimited switch literals."""
    body = b"MZ\0padding\0" + b"".join(b"\0" + s.encode("latin-1") + b"\0" for s in switches) + b"\0end"
    exe = tmp_path / ("chrome.exe" if sys.platform == "win32" else "chrome")
    exe.write_bytes(body)
    if sys.platform == "win32":
        (tmp_path / "chrome.dll").write_bytes(body)
    return str(exe)


# -- GPU launch defaults (#1 + #2) -------------------------------------------------------------

def test_strips_playwright_automation_and_swiftshader_defaults():
    assert list(DEFAULT_IGNORED_ARGS) == ["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"]


def test_ignore_gpu_blocklist_when_headed_on_any_os():
    assert gpu_blocklist_args(True, "linux") == ["--ignore-gpu-blocklist"]
    assert gpu_blocklist_args(True, "win32") == ["--ignore-gpu-blocklist"]


def test_ignore_gpu_blocklist_on_windows_even_headless():
    assert gpu_blocklist_args(False, "win32") == ["--ignore-gpu-blocklist"]


def test_nothing_for_headless_linux():
    assert gpu_blocklist_args(False, "linux") == []


def test_never_duplicates_caller_flag():
    assert gpu_blocklist_args(True, "linux", ["--ignore-gpu-blocklist"]) == []


# -- GPU backend per claimed platform (Linux host) -----------------------------------------------

def _xsock(monkeypatch, present):
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: present if str(p).startswith("/tmp/.X11-unix/X") else real(p))


def test_windows_claim_on_linux_gets_swiftshader_headed_and_headless():
    # ANGLE's GL backend clamps vertex uniform vectors to 1024 under a Direct3D11 label; SwiftShader gives 4096
    assert gpu_backend_args("windows", True, "linux") == ["--use-angle=swiftshader-webgl"]
    assert gpu_backend_args("windows", False, "linux") == ["--use-angle=swiftshader-webgl"]


def test_linux_claim_prefers_mesa_over_egl_in_both_modes_else_over_the_display(monkeypatch):
    # Real Linux Chrome shows the "OpenGL ES 3.2" (EGL) form 4:1 over desktop GL, and gl-egl renders headed
    # under Xvfb too (SC 180), so the same backend serves both modes: one WebGL surface, headless or headed.
    _xsock(monkeypatch, True)
    for env in ({"DISPLAY": ":99"}, {}):
        assert gpu_backend_args("linux", False, "linux", environ=env, mesa_egl=True) == [
            "--use-angle=gl-egl", "--ignore-gpu-blocklist"]
        assert gpu_backend_args("linux", True, "linux", environ=env, mesa_egl=True) == [
            "--use-angle=gl-egl"]  # headed: gpu_blocklist_args already carries the override
    # no EGL: a reachable display still gives Mesa (desktop GL, the rarer real form)
    assert gpu_backend_args("linux", False, "linux", environ={"DISPLAY": ":99"}, mesa_egl=False) == [
        "--use-angle=gl", "--ignore-gpu-blocklist"]
    assert gpu_backend_args("linux", True, "linux", environ={"DISPLAY": ":99"}, mesa_egl=False) == ["--use-angle=gl"]
    _xsock(monkeypatch, False)
    for env in ({"DISPLAY": ":99"}, {}):  # a dead local display, or none at all
        assert gpu_backend_args("linux", False, "linux", environ=env, mesa_egl=True) == [
            "--use-angle=gl-egl", "--ignore-gpu-blocklist"]
        assert gpu_backend_args("linux", False, "linux", environ=env, mesa_egl=False) == []  # stays on SwiftShader
        assert gpu_backend_args("linux", True, "linux", environ=env, mesa_egl=False) == []


def test_backend_choice_never_overrides_the_caller_or_other_hosts(monkeypatch):
    _xsock(monkeypatch, True)
    env = {"DISPLAY": ":99"}
    assert gpu_backend_args("windows", True, "linux", ["--use-angle=vulkan"]) == []
    # a caller's own backend is kept; headless on a GPU-less host it still needs the blocklist override
    # (measured: without it a headless launch with the caller's gl-egl or gl had no WebGL context at all)
    assert gpu_backend_args("linux", False, "linux", ["--use-gl=egl"], environ=env) == ["--ignore-gpu-blocklist"]
    assert gpu_backend_args("linux", False, "linux", ["--use-angle=gl-egl"], environ={}, mesa_egl=True) == [
        "--ignore-gpu-blocklist"]
    assert gpu_backend_args("linux", False, "linux", ["--use-angle=gl", "--ignore-gpu-blocklist"], environ=env) == []
    assert gpu_backend_args("linux", True, "linux", ["--use-angle=gl"], environ=env) == []  # headed has it already
    assert gpu_backend_args("linux", False, "linux", ["--ignore-gpu-blocklist"], environ=env, mesa_egl=False) == [
        "--use-angle=gl"]
    assert gpu_backend_args("linux", False, "linux", ["--use-angle=swiftshader"], environ={}, mesa_egl=True) == [
        "--ignore-gpu-blocklist"]
    assert gpu_backend_args("linux", False, "linux", ["--ignore-gpu-blocklist"], environ={}, mesa_egl=True) == [
        "--use-angle=gl-egl"]
    assert gpu_backend_args("windows", False, "linux", environ={}, mesa_egl=True) == ["--use-angle=swiftshader-webgl"]
    assert gpu_backend_args("windows", True, "win32") == []
    assert gpu_backend_args("windows", True, "darwin") == []
    assert gpu_backend_args(None, False, "linux", environ=env) == []        # pass-through: no persona
    assert gpu_backend_args("android", False, "linux", environ=env) == []


def test_x_display_available(monkeypatch):
    _xsock(monkeypatch, True)
    assert x_display_available({"DISPLAY": ":1.0"}) is True
    assert x_display_available({"DISPLAY": "remotehost:0"}) is True
    assert x_display_available({"DISPLAY": ""}) is False
    assert x_display_available({"DISPLAY": ":abc"}) is False


def _libs(root, *names):
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
    return [str(root)]


def test_mesa_egl_needs_libegl_the_mesa_vendor_and_a_software_rasterizer(tmp_path):
    full = ("libEGL.so.1", "libEGL_mesa.so.0", "dri/swrast_dri.so")
    assert mesa_egl_available(_libs(tmp_path / "debian", *full)) is True
    assert mesa_egl_available(_libs(tmp_path / "kms", "libEGL.so.1", "libEGL_mesa.so.0", "dri/kms_swrast_dri.so")) is True
    # Mesa 24.2+ keeps its drivers in libgallium-<version>.so
    assert mesa_egl_available(_libs(tmp_path / "gallium", "libEGL.so.1", "libEGL_mesa.so.0", "libgallium-24.2.8.so")) is True
    # libEGL alone (the slim image before libegl1 shipped it: no WebGL at all under gl-egl), no vendor, no driver
    assert mesa_egl_available(_libs(tmp_path / "nolib", "libEGL_mesa.so.0", "dri/swrast_dri.so")) is False
    assert mesa_egl_available(_libs(tmp_path / "novendor", "libEGL.so.1", "dri/swrast_dri.so")) is False
    assert mesa_egl_available(_libs(tmp_path / "nodriver", "libEGL.so.1", "libEGL_mesa.so.0")) is False
    # the pieces may sit in different directories (multiarch dir + /usr/lib)
    split = _libs(tmp_path / "a", "libEGL.so.1") + _libs(tmp_path / "b", "libEGL_mesa.so.0", "dri/swrast_dri.so")
    assert mesa_egl_available(split) is True
    assert mesa_egl_available([str(tmp_path / "missing")]) is False


# -- gate_engine_switches -----------------------------------------------------------------------

ALL = list(GATED_ENGINE_SWITCHES)


def test_gate_keeps_every_switch_on_new_engine(tmp_path):
    exe = fake_engine(tmp_path, [s[2:] for s in ALL])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(exe, ["--foo"] + ALL, quiet=False)
    assert args == ["--foo"] + ALL
    assert notes == []
    assert not caught


def test_gate_drops_each_unsupported_switch_with_warning(tmp_path):
    exe = fake_engine(tmp_path, ["proxy-auth"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(exe, [
            "--foo=1", "--allow-third-party-cookies", "--transparent-proxy",
            "--disable-fingerprint-voices", "--fingerprint-passthrough"], quiet=False)
    assert args == ["--foo=1"]
    assert len(notes) == 4
    assert len(caught) == 4
    assert "allow_third_party_cookies=True needs engine 152 r22 or newer; this engine ignores it, so it was not applied." in "\n".join(notes)


def test_gate_silent_under_quiet_but_reports(tmp_path):
    exe = fake_engine(tmp_path, [])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(exe, ["--transparent-proxy"], quiet=True)
    assert args == []
    assert len(notes) == 1
    assert not caught


def test_gate_does_not_mistake_longer_literal(tmp_path):
    exe = fake_engine(tmp_path, ["allow-third-party-cookies-extra", "xtransparent-proxy"])
    args, _ = gate_engine_switches(exe, ["--allow-third-party-cookies", "--transparent-proxy"], quiet=True)
    assert args == []


# -- engine_extras_args -------------------------------------------------------------------------

def test_allow_third_party_cookies_switch():
    assert engine_extras_args(allow_third_party_cookies=True) == ["--allow-third-party-cookies"]
    assert engine_extras_args(allow_third_party_cookies=False) == []


def test_transparent_proxy_needs_a_proxy():
    assert engine_extras_args(transparent_proxy=True, proxy={"server": "http://p:8080"}) == ["--transparent-proxy"]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert engine_extras_args(transparent_proxy=True, proxy=None) == []
    assert any("no effect without a proxy" in str(w.message) for w in caught)


# -- pass-through ---------------------------------------------------------------------------------

@pytest.mark.parametrize("v", ["off", "OFF", " off ", "Off", False])
def test_recognises_passthrough(v):
    assert is_fingerprint_passthrough(v) is True


# Only "off" / False: 0, "0", "no", "false", "disable(d)" are ordinary seeds, so an identity keyed by
# such a value never silently loses its persona.
@pytest.mark.parametrize("v", ["seed-1", "offline", "0x1", "", 1, 0, "0", "no", "false", "disable",
                               "disabled", None, True])
def test_not_passthrough(v):
    assert is_fingerprint_passthrough(v) is False


def test_passthrough_emits_no_persona_switches():
    args = fingerprint_args({"fingerprint": "off", "platform": "windows", "brand": "Edge",
                             "gpu_vendor": "X", "light_stealth": True})
    assert args == ["--fingerprint-passthrough"]
    assert not any(a.startswith("--fingerprint=") for a in args)


def test_passthrough_keeps_only_explicit_locale_network():
    assert fingerprint_args({"fingerprint": "off", "timezone": "Europe/Berlin",
                             "accept_language": "de-DE,de;q=0.9", "webrtc_ip": "1.2.3.4"}) == [
        "--fingerprint-passthrough", "--timezone=Europe/Berlin", "--accept-lang=de-DE,de",
        "--lang=de", "--webrtc-ip=1.2.3.4"]


def test_passthrough_adds_no_coherence_defaults():
    joined = " ".join(fingerprint_args({"fingerprint": "off"}))
    for s in ("accept-lang", "timezone", "fingerprint-platform", "fingerprint-brand"):
        assert s not in joined


# -- fingerprint_voices -------------------------------------------------------------------------

def test_fingerprint_voices_is_a_fingerprint_key():
    assert "fingerprint_voices" in FINGERPRINT_KEYS


def test_fingerprint_voices_false_emits_switch():
    assert "--disable-fingerprint-voices" in fingerprint_args({"fingerprint": "s", "fingerprint_voices": False})
    assert "--disable-fingerprint-voices" not in fingerprint_args({"fingerprint": "s", "fingerprint_voices": True})
    assert "--disable-fingerprint-voices" not in fingerprint_args({"fingerprint": "s"})


# -- release channel (#5) -----------------------------------------------------------------------

def test_release_channel_resolution():
    assert resolve_release_channel(None, {}) == "stable"
    assert resolve_release_channel(None, {"CLEARCOTE_RELEASE_CHANNEL": "preview"}) == "preview"
    assert resolve_release_channel("stable", {"CLEARCOTE_RELEASE_CHANNEL": "preview"}) == "stable"
    assert resolve_release_channel(" Preview ", {}) == "preview"
    with pytest.raises(ValueError, match="Unknown release channel 'beta'"):
        resolve_release_channel("beta", {})


def test_pro_download_url_channel_only_for_preview():
    assert pro_download_url("https://x.test/", "linux") == "https://x.test/api/v1/download/pro?platform=linux"
    assert pro_download_url("https://x.test", "windows", "152", "stable") == \
        "https://x.test/api/v1/download/pro?platform=windows&version=152"
    assert pro_download_url("https://x.test", "windows", "152.0.7977.82-r21", "preview") == \
        "https://x.test/api/v1/download/pro?platform=windows&version=152.0.7977.82-r21&channel=preview"


def test_pro_ensure_binary_sends_channel(monkeypatch):
    import json
    import urllib.request

    from clearcote import download as _unused  # noqa: F401
    dl = sys.modules["clearcote.download"]
    seen = []

    class Resp:
        def __init__(self, body):
            self._b = body

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return Resp(json.dumps({"tag": "pro-x", "url": None}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="not currently available"):
        dl.pro_ensure_binary("k", api_base="https://x.test", release_channel="preview")
    with pytest.raises(RuntimeError):
        dl.pro_ensure_binary("k", api_base="https://x.test")
    assert seen == ["https://x.test/api/v1/download/pro?platform=linux&channel=preview",
                    "https://x.test/api/v1/download/pro?platform=linux"]


# -- serve as root (#9) -------------------------------------------------------------------------

def test_serve_needs_no_sandbox():
    assert serve_needs_no_sandbox("linux", 0, []) is True
    assert serve_needs_no_sandbox("linux", 1000, []) is False
    assert serve_needs_no_sandbox("linux", 0, ["--no-sandbox"]) is False
    assert serve_needs_no_sandbox("win32", None, []) is False


# -- serve: the "unsupported command-line flag" infobar ------------------------------------------

def test_serve_infobar_args_headless_always_disables_infobars_once():
    assert serve_infobar_args(True, []) == ["--disable-infobars"]
    assert serve_infobar_args(True, ["--no-sandbox"]) == ["--disable-infobars"]
    assert serve_infobar_args(True, ["--disable-infobars"]) == []


def test_serve_infobar_args_headed_test_type_only_with_no_sandbox_and_once():
    assert serve_infobar_args(False, []) == []
    assert serve_infobar_args(False, ["--ignore-certificate-errors"]) == []
    assert serve_infobar_args(False, ["--no-sandbox"]) == ["--test-type"]
    assert serve_infobar_args(False, ["--no-sandbox", "--test-type=browser"]) == []


@pytest.fixture
def served_line(monkeypatch, tmp_path):
    """serve() up to a stand-in browser process: the engine command line it spawns."""
    import clearcote._geometry as _geometry
    import clearcote._serve as _serve
    home = tmp_path / "home"
    home.mkdir()
    for k in ("HOME", "USERPROFILE"):  # no licence on this machine may select PRO or take a lease
        monkeypatch.setenv(k, str(home))
    for k in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY"):
        monkeypatch.delenv(k, raising=False)
    exe = fake_engine(tmp_path, NEW)
    monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: exe)
    monkeypatch.setattr(clearcote, "_guard", lambda exe: None)
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

        def kill(self):  # close() falls back to it when a browser outlives terminate()
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(_serve.subprocess, "Popen", Proc)

    def run(**kwargs):
        clearcote.serve(quiet=True, user_data_dir=str(tmp_path / "udd"), **kwargs).close()
        return lines[-1]
    return run


def test_serve_headless_puts_disable_infobars_on_the_engine_command_line(served_line):
    for args in ([], ["--no-sandbox"]):
        line = served_line(args=args)
        assert line.count("--disable-infobars") == 1, line
        assert not any(a.startswith("--test-type") for a in line), line


def test_serve_headed_puts_test_type_on_the_command_line_only_with_no_sandbox(served_line):
    # As root on Linux serve() adds --no-sandbox itself, and --test-type comes with it.
    as_root = serve_needs_no_sandbox(sys.platform, getattr(os, "getuid", lambda: None)(), [])
    line = served_line(headless=False)
    assert line.count("--test-type") == (1 if as_root else 0), line
    assert "--disable-infobars" not in line, line
    assert served_line(headless=False, args=["--no-sandbox"]).count("--test-type") == 1


# -- end to end through _prepare (the real arg assembly both launch paths and serve use) ----------

@pytest.fixture
def prepared(monkeypatch, tmp_path):
    # The WebGL backend choice reads the host (X display, Mesa's EGL) and has its own tests above;
    # keep these end-to-end ones the same on every host. A test can patch either back on.
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("clearcote._launchopts.mesa_egl_available", lambda *a, **k: False)

    def run(switches, **kwargs):
        exe = fake_engine(tmp_path, switches)
        monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: exe)
        monkeypatch.setattr(clearcote, "_guard", lambda exe: None)
        kwargs.setdefault("quiet", True)
        return clearcote._prepare(dict(kwargs))
    return run


NEW = [s[2:] for s in ALL] + ["proxy-auth"]


def test_prepare_defaults_ignore_args_and_gpu_blocklist(prepared):
    _exe, args, pw, *_ = prepared(NEW, headless=True)
    assert pw["ignore_default_args"] == ["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"]
    assert ("--ignore-gpu-blocklist" in args) == (sys.platform == "win32")
    _exe, args, pw, *_ = prepared(NEW, headless=False)
    assert "--ignore-gpu-blocklist" in args


def test_prepare_callers_ignore_default_args_win(prepared):
    _exe, _args, pw, *_ = prepared(NEW, ignore_default_args=["--enable-automation"])
    assert pw["ignore_default_args"] == ["--enable-automation"]


def test_prepare_serve_headed_flag(prepared, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    _exe, args, *_ = prepared(NEW, _cc_headed=True)
    assert "--ignore-gpu-blocklist" in args
    _exe, args, *_ = prepared(NEW, _cc_headed=False)
    assert "--ignore-gpu-blocklist" not in args


def test_prepare_headless_linux_claim_renders_through_mesa_egl_without_a_display(prepared, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("clearcote._launchopts.mesa_egl_available", lambda *a, **k: True)
    _exe, args, *_ = prepared(NEW, headless=True, platform="linux")
    assert "--use-angle=gl-egl" in args and "--ignore-gpu-blocklist" in args
    _exe, args, *_ = prepared(NEW, headless=True, platform="windows")
    assert "--use-angle=swiftshader-webgl" in args and "--use-angle=gl-egl" not in args
    _exe, args, *_ = prepared(NEW, headless=True, fingerprint="off")  # pass-through: no persona to match
    assert not any(a.startswith("--use-angle=") for a in args)


def test_prepare_passthrough_and_extras_on_new_engine(prepared):
    _exe, args, pw, *_ = prepared(NEW, fingerprint="off", platform="macos", fingerprint_voices=False,
                                  allow_third_party_cookies=True, transparent_proxy=True,
                                  proxy={"server": "http://127.0.0.1:3128"}, license_through_proxy=True,
                                  release_channel="preview")
    assert "--fingerprint-passthrough" in args
    assert not any(a.startswith("--fingerprint=") or a.startswith("--fingerprint-platform") for a in args)
    assert "--allow-third-party-cookies" in args and "--transparent-proxy" in args
    for leaked in ("allow_third_party_cookies", "transparent_proxy", "license_through_proxy", "release_channel"):
        assert leaked not in pw


def test_prepare_old_engine_drops_new_switches(prepared):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _exe, args, *_ = prepared(["proxy-auth"], fingerprint="off", fingerprint_voices=False,
                                  allow_third_party_cookies=True, quiet=False)
    assert not any(a in args for a in ALL)
    assert sum("needs engine 152 r22" in str(w.message) for w in caught) == 2  # passthrough + cookies


def test_prepare_passthrough_skips_auto_profile(prepared, monkeypatch):
    called = []
    monkeypatch.setattr(clearcote, "_apply_auto_profile", lambda *a, **k: called.append(1))
    prepared(NEW, fingerprint="off", profile="auto")
    assert called == []


def test_launch_persistent_context_default_ignore_args(monkeypatch):
    captured = {}

    class Ctx:
        pages = []

        def on(self, *a):
            pass

    class Chromium:
        def launch_persistent_context(self, udd, **kw):
            captured.update(kw)
            return Ctx()

    class PW:
        chromium = Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "_prepare",
                        lambda kw: ("chrome", [], dict(kw), False, False, None))
    monkeypatch.setattr(clearcote, "apply_headless_geometry", lambda *a, **k: None)
    clearcote.launch_persistent_context("udd", quiet=True)
    assert captured["ignore_default_args"] == ["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"]


async def test_async_launch_persistent_context_default_ignore_args(monkeypatch):
    from clearcote import async_api
    seen = {}

    def fake_prepare(kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop here")

    monkeypatch.setattr(async_api, "_prepare", fake_prepare)
    with pytest.raises(RuntimeError, match="stop here"):
        await async_api.launch_persistent_context("udd", quiet=True)
    assert seen["ignore_default_args"] == ["--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars"]


def test_release_channel_reaches_every_pro_download_path(monkeypatch):
    dl = sys.modules["clearcote.download"]
    seen = []
    monkeypatch.delenv("CLEARCOTE_BINARY", raising=False)
    monkeypatch.delenv("CLEARCOTE_BROWSER_VERSION", raising=False)
    monkeypatch.setattr(dl, "pro_ensure_binary",
                        lambda key, **kw: seen.append((kw.get("version"), kw.get("release_channel"))) or "exe")
    monkeypatch.setattr(dl, "resolve_version", lambda sel, has_license=False, quiet=False: ("pro", "152"))
    pro = ("cc_lic_x", None)
    clearcote._resolve_binary(None, pro=pro, release_channel="preview")                   # pinned PRO
    clearcote._resolve_binary(None, pro=pro, version="r22", release_channel="preview")    # revision pin
    clearcote._resolve_binary(None, pro=pro, version="152", release_channel="preview")    # catalog pin
    assert seen == [(None, "preview"), ("r22", "preview"), ("152", "preview")]
    with pytest.raises(ValueError, match="Unknown release channel"):
        clearcote._resolve_binary(None, pro=pro, version="152", release_channel="beta")


# -- Playwright's --disable-features (2026-10-06) ------------------------------------------------
# Playwright disables ThirdPartyStoragePartitioning. Measured on r30 vs genuine Chrome 154: a
# cross-site iframe then read the top-level site's storage, or got a SecurityError from localStorage
# with third-party cookies blocked -- genuine gives it an empty partition either way.

def _only_disable_features(args):
    found = [a for a in args if a.startswith("--disable-features=")]
    assert len(found) == 1, found
    return found[0].split("=", 1)[1].split(",")


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_launch_replaces_playwrights_list_without_partitioning(prepared, platform):
    _exe, args, _pw, *_ = prepared(NEW, fingerprint="s1", platform=platform, headless=True)
    feats = _only_disable_features(args)
    for f in ("ThirdPartyStoragePartitioning", "AcceptCHFrame", "HttpsUpgrades"):
        assert f not in feats, f  # what a page or a server can observe stays as in genuine Chrome
    # Playwright's stability/UI entries stay off on every claim (a Linux claim used to drop them all,
    # because its own --disable-features=WebBluetooth replaced Playwright's whole list). Which entries
    # Playwright disables varies by release (1.63 dropped RenderDocument): check the installed one's.
    from clearcote._launchopts import (PAGE_VISIBLE_PLAYWRIGHT_FEATURES, PLAYWRIGHT_DISABLED_FEATURES,
                                       installed_playwright_disabled_features)
    playwrights = installed_playwright_disabled_features() or PLAYWRIGHT_DISABLED_FEATURES
    kept = [f for f in playwrights if f not in PAGE_VISIBLE_PLAYWRIGHT_FEATURES]
    assert "MediaRouter" in kept
    for f in kept:
        assert f in feats, f
    assert ("WebBluetooth" in feats) == (platform == "linux")


def test_the_users_own_disable_features_are_kept(prepared):
    _exe, args, _pw, *_ = prepared(NEW, fingerprint="s1", headless=True,
                                   args=["--disable-features=Foo,ThirdPartyStoragePartitioning"])
    feats = _only_disable_features(args)
    assert "Foo" in feats and "ThirdPartyStoragePartitioning" in feats  # asked for explicitly


def test_no_copy_of_playwrights_list_when_the_caller_dropped_it(prepared):
    from clearcote._launchopts import PLAYWRIGHT_DISABLED_FEATURES, installed_playwright_disabled_features
    exact = "--disable-features=" + ",".join(installed_playwright_disabled_features()
                                              or PLAYWRIGHT_DISABLED_FEATURES)
    for ignore in (True, [exact]):
        _exe, args, _pw, *_ = prepared(NEW, headless=True, ignore_default_args=ignore)
        assert not any("MediaRouter" in a for a in args), args
    # a different --disable-features value does NOT remove Playwright's list (exact match only)
    _exe, args, _pw, *_ = prepared(NEW, headless=True, ignore_default_args=["--disable-features=X"])
    assert "MediaRouter" in _only_disable_features(args)


def test_serve_never_carries_playwrights_list(served_line):
    line = served_line()
    assert not any("HttpsUpgrades" in a or "ThirdPartyStoragePartitioning" in a for a in line), line
