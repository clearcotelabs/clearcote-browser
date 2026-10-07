import pytest

from clearcote._warnings import coherence_warnings


def codes(opts, host="win32", build="149"):
    return {w["code"] for w in coherence_warnings(opts, host_platform=host, build_major=build)}


def test_coherent_default_is_silent():
    assert coherence_warnings(
        {"platform": "windows", "fingerprint": "s", "headless": False},
        host_platform="win32", build_major="149") == []


def test_proxy_without_geo():
    assert "proxy-no-geo" in codes({"proxy": {"server": "http://h:8080"}, "headless": False})
    # silent when geoip is on or manual geo is supplied
    assert "proxy-no-geo" not in codes({"proxy": {"server": "http://h:8080"}, "geoip": True, "headless": False})
    assert "proxy-no-geo" not in codes(
        {"proxy": "http://h:8080", "timezone": "America/New_York", "accept_language": "en-US,en", "headless": False})


def test_socks_geoip_is_not_flagged():
    # This warned "geoip cannot resolve a SOCKS proxy's exit IP" on every SOCKS launch, long after
    # the lookup learned to tunnel through SOCKS5 with credentials (0.29.0).
    assert "socks-geoip" not in codes({"proxy": "socks5://u:p@h:1", "geoip": True, "headless": False})
    assert codes({"proxy": {"server": "socks5://h:1"}, "geoip": True, "headless": False}) == set()


def test_platform_vs_host_fonts():
    assert "platform-host-fonts" in codes({"platform": "macos", "headless": False})
    assert "platform-host-fonts" not in codes({"platform": "windows", "headless": False})
    assert "platform-host-fonts" not in codes(
        {"platform": "macos", "fingerprint_profile": "p.json", "headless": False})


def test_gpu_incoherent_with_platform():
    assert "gpu-platform" in codes(
        {"platform": "macos", "gpu_renderer": "ANGLE (Apple, Direct3D11)", "headless": False}, host="darwin")
    assert "gpu-platform" not in codes(
        {"platform": "windows", "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Direct3D11)", "headless": False})


def test_software_gpu_string():
    assert "gpu-software" in codes({"gpu_renderer": "ANGLE (Google, Vulkan SwiftShader Device)", "headless": False})


def test_brand_and_version_vs_build():
    assert "brand-mismatch" in codes({"brand": "edge", "headless": False})
    assert "brand-mismatch" not in codes({"brand": "chrome", "headless": False})
    assert "version-mismatch" in codes({"brand_version": "146", "headless": False})
    assert "version-mismatch" not in codes({"brand_version": "149.0.1", "headless": False})


def test_disable_gpu_needs_noise_off():
    assert "gpu-noise" in codes({"disable_gpu_fingerprint": True, "headless": False})
    assert "gpu-noise" not in codes({"disable_gpu_fingerprint": True, "fingerprint_noise": False, "headless": False})


def test_headless_render_note():
    assert "headless-render" in codes({"headless": True})
    assert "headless-render" not in codes({"headless": False})
    assert "headless-render" not in codes({"headless": True, "canvas_bridge": {"url": "ws://h:1"}})
    assert "headless-render" not in codes({"headless": True, "disable_gpu_fingerprint": True})


def test_bridge_without_gpu_pin_note():
    assert "bridge-no-gpu" in codes({"canvas_bridge": {"url": "ws://h:1"}, "headless": False})
    assert "bridge-no-gpu" not in codes(
        {"canvas_bridge": {"url": "ws://h:1"}, "gpu_renderer": "ANGLE (Intel)", "headless": False})


def test_automation_arg_readded():
    assert "automation-arg" in codes({"_user_args": ["--enable-automation"], "headless": False})
    assert "automation-arg" in codes({"_user_args": ["--remote-debugging-port=9222"], "headless": False})
    assert "automation-arg" not in codes({"_user_args": ["--no-sandbox"], "headless": False})


def _codes(opts):
    return {w["code"] for w in coherence_warnings(opts)}


def test_narrow_gpu_and_canvas_switch_warnings():
    """The r12 halves each carry their own advice, and neither contradicts the other.

    History matters here. gpu-noise once said "pair with fingerprint_noise=False"; it was then
    softened to accept canvas_noise=False as sufficient under disable_gpu_fingerprint; and on
    2026-09-15 measurement showed the softening was wrong. Under dgf the WebGL readPixels farble
    stands down but a WebGL canvas's toDataURL/toBlob farble does not (it keys on fingerprint_noise,
    not canvas_noise), so canvas_noise=False leaves two reads of one buffer disagreeing. gpu-noise
    must therefore keep firing under dgf until fingerprint_noise itself is off.
    """
    # The narrow GPU switch leaves readPixels noised, so its advice differs from the wide flag's.
    c = _codes({"gpu_string_spoof": False})
    assert "gpu-noise-string" in c
    assert "gpu-noise" not in c          # the wide flag's warning must not double-fire
    assert "gpu-string-only" in c        # names the WebGL-vs-WebGPU split the narrow switch opens

    # canvas_noise=False must surface the toBlob note (a switch gap on engines before 152 r21).
    assert "canvas-noise-toblob" in _codes({"canvas_noise": False})

    # Already-correct configurations must not be nagged.
    assert "gpu-noise-string" not in _codes({"gpu_string_spoof": False, "fingerprint_noise": False})
    # canvas_noise=False is NOT enough under dgf: the WebGL export stays farbled, so keep warning.
    assert "gpu-noise" in _codes({"disable_gpu_fingerprint": True, "canvas_noise": False})
    # ...but the wide flag on its own still warns, exactly as before.
    assert "gpu-noise" in _codes({"disable_gpu_fingerprint": True})


def test_engine_notes_fire_once_via_emitter(capsys, monkeypatch):
    """Engine-behaviour notes are NOT coherence findings: coherence_warnings() must stay silent for
    a coherent default (asserted above), so they are emitted by emit_coherence_warnings() instead -
    once per process, and honouring quiet like every other note."""
    from clearcote import _warnings
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    opts = {"platform": "windows", "fingerprint": "s", "headless": False}
    _warnings.emit_coherence_warnings(opts, host_platform="win32", build_major="149")
    _warnings.emit_coherence_warnings(opts, host_platform="win32", build_major="149")
    assert capsys.readouterr().err.count("page.on('console')") == 1   # once per process, not per launch
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    _warnings.emit_coherence_warnings(opts, quiet=True, host_platform="win32", build_major="149")
    assert "page.on('console')" not in capsys.readouterr().err


def test_devtools_open_is_flagged():
    assert "devtools-open" in codes({"devtools": True})
    assert "devtools-open" in codes({"_user_args": ["--auto-open-devtools-for-tabs"]})
    assert "devtools-open" not in codes({"devtools": False})


def test_custom_user_agent_is_flagged():
    assert "custom-user-agent" in codes({"user_agent": "Mozilla/5.0 (Macintosh) Chrome/120"})
    assert "custom-user-agent" in codes({"_user_args": ["--user-agent=Mozilla/5.0 Foo"]})
    assert "custom-user-agent" not in codes({"user_agent": None})


def test_cdp_exposure_in_args():
    assert "cdp-public-bind" in codes({"_user_args": ["--remote-debugging-address=0.0.0.0"]})
    assert "cdp-public-bind" not in codes({"_user_args": ["--remote-debugging-address=127.0.0.1"]})
    # Chromium keeps the LAST value
    assert "cdp-public-bind" not in codes(
        {"_user_args": ["--remote-debugging-address=0.0.0.0", "--remote-debugging-address=::1"]})
    assert "cdp-any-origin" in codes({"_user_args": ["--remote-allow-origins=*"]})
    assert "cdp-any-origin" in codes({"_user_args": ["--remote-allow-origins=http://a.test, *"]})
    assert "cdp-any-origin" not in codes({"_user_args": ["--remote-allow-origins=http://127.0.0.1:9222"]})


def test_serve_exposure_warnings():
    from clearcote._warnings import serve_exposure_warnings
    c = lambda host, origins: {w["code"] for w in serve_exposure_warnings(host, origins)}
    assert c("127.0.0.1", "http://127.0.0.1:9222,http://localhost:9222") == set()
    assert c("localhost", "http://localhost:9222") == set()
    assert c("[::1]", "http://localhost:9222") == set()
    assert c("0.0.0.0", "http://0.0.0.0:9222") == {"cdp-public-bind"}
    assert c("127.0.0.1", "*") == {"cdp-any-origin"}
    assert c("10.0.0.5", "*") == {"cdp-public-bind", "cdp-any-origin"}


def test_emit_warnings_respects_quiet(capsys, monkeypatch):
    from clearcote._warnings import emit_warnings, serve_exposure_warnings
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    emit_warnings(serve_exposure_warnings("0.0.0.0", "*"), quiet=True)
    assert capsys.readouterr().err == ""
    emit_warnings(serve_exposure_warnings("0.0.0.0", "*"))
    err = capsys.readouterr().err
    assert err.count("clearcote: warning:") == 2
    monkeypatch.setenv("CLEARCOTE_NO_WARN", "1")
    emit_warnings(serve_exposure_warnings("0.0.0.0", "*"))
    assert capsys.readouterr().err == ""


# -- a Linux persona on a Windows host ----------------------------------------------------------------
# Direct3D 11 clamps the WebGL/WebGPU limits on a Windows host, so a Linux claim there cannot be coherent.
# Said once per process, for a local launch only: a Docker launch runs on Linux in its container and a cloud
# launch runs remotely.

LINUX_ON_WINDOWS = "linux-persona-windows-host"
LINE = "a Linux persona on a Windows host"


def test_a_linux_persona_on_a_windows_host_is_flagged():
    assert LINUX_ON_WINDOWS in codes({"platform": "linux", "fingerprint": "17"})
    assert LINUX_ON_WINDOWS in codes({"platform": " Linux ", "fingerprint_profile": "p.json"})
    assert LINUX_ON_WINDOWS in codes({"platform": "linux"})  # no seed: the engine still claims Linux


def test_not_for_a_windows_claim_or_off_windows():
    assert LINUX_ON_WINDOWS not in codes({"platform": "windows", "fingerprint": "17"})
    assert LINUX_ON_WINDOWS not in codes({"fingerprint": "17"})  # no platform: the host's own
    assert LINUX_ON_WINDOWS not in codes({"platform": "linux", "fingerprint": "17"}, host="linux")
    assert LINUX_ON_WINDOWS not in codes({"platform": "linux", "fingerprint": "17"}, host="darwin")


def test_not_without_a_persona():
    # Pass-through runs with no persona at all (the engine presents the real host); nothing set claims the host.
    assert LINUX_ON_WINDOWS not in codes({"platform": "linux", "fingerprint": "off"})
    assert LINUX_ON_WINDOWS not in codes({"platform": "linux", "fingerprint": False})
    assert LINUX_ON_WINDOWS not in codes({})


def test_it_is_said_once_per_process(capsys, monkeypatch):
    from clearcote import _warnings
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    opts = {"platform": "linux", "fingerprint": "17", "headless": False}
    _warnings.emit_coherence_warnings(opts, quiet=True, host_platform="win32", build_major="149")
    assert LINE not in capsys.readouterr().err  # quiet says nothing, and does not use up the one time
    for _ in range(3):
        _warnings.emit_coherence_warnings(opts, host_platform="win32", build_major="149")
    err = capsys.readouterr().err
    assert err.count(LINE) == 1
    assert ("clearcote: warning: a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps "
            "WebGL)") in err
    assert "platform='windows'" in err and "docker=True" in err


@pytest.fixture
def windows_host(monkeypatch):
    """launch()'s warnings judged as on a Windows host, wherever the suite runs; the launch itself is real."""
    import clearcote
    from clearcote import _warnings
    real = _warnings.emit_coherence_warnings
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    monkeypatch.setattr(clearcote, "emit_coherence_warnings",
                        lambda opts, **kw: real(opts, **{**kw, "host_platform": "win32"}))


@pytest.fixture
def no_licence(monkeypatch, tmp_path):
    """No saved or exported licence key is visible (a launch would take a real lease), nor anything else from
    this machine's environment that picks a binary or a launch path."""
    from clearcote._license import resolve_license_key
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    for var in ("CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD",
                "CLEARCOTE_DOCKER", "CLEARCOTE_PERSONA_ENV"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_license_key() is None


@pytest.fixture
def no_browser(monkeypatch, no_licence):
    """Playwright replaced by a stand-in: launches record their arguments and start nothing."""
    import clearcote
    seen = {}

    class _Context:
        def on(self, *a, **k):
            pass

        def new_page(self, **kw):
            return kw

        def close(self):
            pass

    class _Chromium:
        def launch_persistent_context(self, user_data_dir, **kw):
            seen.update(kw)
            return _Context()

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
    # A seeded persona draws a real font list from the machine's local profile corpus, if it has one: not
    # what these tests are about, and seconds of reading on a machine with a large corpus.
    monkeypatch.setattr(clearcote, "ensure_persona_fonts", lambda fp, quiet=False: None)
    return seen


def _engine(tmp_path):
    exe = tmp_path / "engine" / "chrome"
    exe.parent.mkdir()
    exe.write_bytes(b"\x7fELF\x00fingerprint-passthrough\x00--remote-debugging-port\x00")  # has pass-through
    return str(exe)


def test_a_local_launch_says_it(windows_host, no_browser, tmp_path, capsys):
    import clearcote
    exe = _engine(tmp_path)
    for udd in ("a", "b"):
        clearcote.launch_persistent_context(str(tmp_path / udd), executable_path=exe, platform="linux",
                                            fingerprint="17", headless=True)
    assert "--fingerprint-platform=linux" in no_browser["args"]
    assert capsys.readouterr().err.count(LINE) == 1


def test_a_local_launch_with_a_windows_persona_or_none_does_not(windows_host, no_browser, tmp_path, capsys):
    import clearcote
    exe = _engine(tmp_path)
    clearcote.launch_persistent_context(str(tmp_path / "a"), executable_path=exe, platform="windows",
                                        fingerprint="17", headless=True)
    clearcote.launch_persistent_context(str(tmp_path / "b"), executable_path=exe, platform="linux",
                                        fingerprint="off", headless=True)
    clearcote.launch_persistent_context(str(tmp_path / "c"), executable_path=exe, headless=True)
    assert LINE not in capsys.readouterr().err


def test_a_docker_or_cloud_launch_does_not(no_licence, monkeypatch, capsys):
    import sys
    import clearcote
    from clearcote import _warnings
    monkeypatch.setattr(_warnings, "_seen_notes", set())
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")  # only the routing runs before the stand-ins below
    monkeypatch.setattr(clearcote, "launch_docker", lambda kwargs: ("docker", kwargs))
    monkeypatch.setattr(clearcote, "launch_cloud", lambda cloud, kwargs, **kw: ("cloud", kwargs))
    assert clearcote.launch(docker=True, platform="linux", fingerprint="17")[0] == "docker"
    assert clearcote.launch(cloud=True, platform="linux", fingerprint="17")[0] == "cloud"
    monkeypatch.undo()
    assert LINE not in capsys.readouterr().err
