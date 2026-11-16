"""fingerprint="off" (pass-through) shows the host.

From engine r51 a pass-through launch still carries a persona token, and the engine serves that token's GPU
and voices unless --disable-gpu-fingerprint and --disable-fingerprint-voices say otherwise; the default geoip
lookup filled a language, timezone and WebRTC IP from the IP address, which went out as if the caller had set
them. Measured on r60 (2026-11-16): a Windows host with an RTX 3070 set to en-GB answered "GTX 1660 ... OpenGL
4.5.0", no voices and nl-NL from a Dutch address. So pass-through sends the two switches itself and skips the
default lookup.

Hermetic: the geoip resolver is a stand-in and the "engine" is a small file. Mirrors
sdk/node/test/passthrough.test.ts.
"""
import warnings

import pytest

import clearcote
from clearcote import _docker, _persona
from clearcote._fingerprint import PASSTHROUGH_HOST_SWITCHES, fingerprint_args, without_caller_repeats
from clearcote._launchopts import _SWITCH_CACHE, gate_engine_switches
from clearcote.geoip import geoip_mode

PASSTHROUGH = "--fingerprint-passthrough"
GPU = "--disable-gpu-fingerprint"
VOICES = "--disable-fingerprint-voices"
GEO = {"ip": "203.0.113.7", "country": "NL", "timezone": "Europe/Amsterdam", "accept_language": "nl-NL",
       "location": "52.37,4.89"}
# 152 r22 brought pass-through and the voices switch; the GPU switch is in every engine.
ENGINE_R22 = ["fingerprint-passthrough", "disable-fingerprint-voices", "disable-gpu-fingerprint",
              "allow-third-party-cookies"]
ENGINE_OLD = ["disable-gpu-fingerprint"]


@pytest.fixture(autouse=True)
def _clear_switch_cache():
    _SWITCH_CACHE.clear()
    yield
    _SWITCH_CACHE.clear()


def fake_engine(tmp_path, switches, name="engine"):
    """A stand-in engine: the probe reads the NUL-delimited switch names in it."""
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    exe = d / "chrome"
    exe.write_bytes(b"\x7fELF\0" + b"\0".join(s.encode("latin-1") for s in switches) + b"\0--remote-debugging-port\0")
    return str(exe)


@pytest.fixture
def resolver(monkeypatch):
    """The geoip default ON (the suite's conftest turns it off) with a resolver that records what it is asked."""
    clearcote._reset_direct_geo_cache()
    asked = []

    def resolve(proxy, quiet=False, timeout=None):
        asked.append(proxy)
        return dict(GEO), None, 5

    monkeypatch.setattr(clearcote, "resolve_geo_detailed", resolve)
    monkeypatch.setattr(clearcote, "warn_on_egress_drift", lambda *a, **k: None)
    monkeypatch.setenv("CLEARCOTE_GEOIP", "1")
    yield asked
    clearcote._reset_direct_geo_cache()


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    """Run the real arg assembly both launch paths and serve() use, on a stand-in engine."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("clearcote._launchopts.mesa_egl_available", lambda *a, **k: False)

    def run(switches, **kwargs):
        exe = fake_engine(tmp_path, switches)
        monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: exe)
        monkeypatch.setattr(clearcote, "_guard", lambda exe: None)
        kwargs.setdefault("quiet", True)
        return clearcote._prepare(dict(kwargs))[1]
    return run


def named(args, prefix):
    return [a for a in args if a.startswith(prefix)]


# -- the switches a pass-through launch sends ------------------------------------------------------

def test_sends_the_host_gpu_and_host_voices_switches():
    assert fingerprint_args({"fingerprint": "off"}) == [PASSTHROUGH, GPU, VOICES]
    assert fingerprint_args({"fingerprint": False}) == [PASSTHROUGH, GPU, VOICES]
    assert PASSTHROUGH_HOST_SWITCHES == (GPU, VOICES)


def test_both_are_selector_switches_so_the_persona_token_is_issued_for_them():
    for s in PASSTHROUGH_HOST_SWITCHES:
        assert s[2:] in _persona.SELECTOR_SWITCHES
    sel, _profile, _args = _persona.selector_from_args(fingerprint_args({"fingerprint": "off"}))
    assert sel == {"disable-gpu-fingerprint": "", "disable-fingerprint-voices": ""}


def test_the_options_that_send_the_same_switches_add_no_second_copy():
    assert fingerprint_args({"fingerprint": "off", "disable_gpu_fingerprint": True,
                             "fingerprint_voices": False}) == [PASSTHROUGH, GPU, VOICES]


def test_a_persona_launch_is_unchanged():
    args = fingerprint_args({"fingerprint": "seed-1"})
    assert GPU not in args and VOICES not in args


def test_without_caller_repeats_drops_only_the_host_switches_the_caller_passed():
    fp = [PASSTHROUGH, GPU, VOICES, "--timezone=Europe/Berlin"]
    assert without_caller_repeats(fp, []) == fp
    assert without_caller_repeats(fp, None) == fp
    assert without_caller_repeats(fp, [GPU]) == [PASSTHROUGH, VOICES, "--timezone=Europe/Berlin"]
    assert without_caller_repeats(fp, [VOICES, GPU, "--timezone=UTC", PASSTHROUGH]) == [
        PASSTHROUGH, "--timezone=Europe/Berlin"]


def test_each_switch_is_on_the_command_line_exactly_once(prepared):
    args = prepared(ENGINE_R22, fingerprint="off")
    for s in (PASSTHROUGH, GPU, VOICES):
        assert args.count(s) == 1
    assert named(args, "--fingerprint=") == [] and named(args, "--fingerprint-platform") == []
    # the caller passed them too: still once
    args = prepared(ENGINE_R22, fingerprint="off", args=[GPU, VOICES])
    for s in (PASSTHROUGH, GPU, VOICES):
        assert args.count(s) == 1


# -- older engines ---------------------------------------------------------------------------------

def test_an_engine_without_passthrough_gets_one_warning_and_keeps_the_gpu_switch(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(fake_engine(tmp_path, ENGINE_OLD), fingerprint_args({"fingerprint": "off"}),
                                           quiet=False)
    assert args == [GPU]
    assert len(notes) == 1 and len(caught) == 1
    assert 'fingerprint="off" (pass-through debug mode) needs engine 152 r22' in notes[0]


def test_fingerprint_voices_false_under_a_persona_still_warns_there(tmp_path):
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(fake_engine(tmp_path, ENGINE_OLD), ["--fingerprint=seed-1", VOICES],
                                           quiet=False)
    assert args == ["--fingerprint=seed-1"]
    assert len(notes) == 1 and "fingerprint_voices=False needs engine 152 r22" in notes[0]


def test_the_voices_switch_passthrough_brings_along_never_gets_a_warning_of_its_own(tmp_path):
    exe = fake_engine(tmp_path, ["fingerprint-passthrough", "disable-gpu-fingerprint"])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        args, notes = gate_engine_switches(exe, fingerprint_args({"fingerprint": "off"}), quiet=False)
    assert args == [PASSTHROUGH, GPU]
    assert notes == [] and caught == []


def test_an_engine_with_passthrough_keeps_all_three_without_a_warning(tmp_path):
    args, notes = gate_engine_switches(fake_engine(tmp_path, ENGINE_R22), fingerprint_args({"fingerprint": "off"}),
                                       quiet=False)
    assert args == [PASSTHROUGH, GPU, VOICES]
    assert notes == []


# -- nothing taken from the IP address -------------------------------------------------------------

def test_geoip_mode_passthrough_turns_the_default_off_and_true_still_asks():
    assert geoip_mode(None, {}, passthrough=True) == "off"
    assert geoip_mode(None, {"CLEARCOTE_GEOIP": "1"}, passthrough=True) == "off"
    assert geoip_mode(False, {}, passthrough=True) == "off"
    assert geoip_mode(True, {}, passthrough=True) == "strict"
    assert geoip_mode(None, {}, passthrough=False) == "auto"
    assert geoip_mode(None, {}) == "auto"


def test_a_persona_launch_looks_the_address_up_and_passthrough_does_not(prepared, resolver):
    args = prepared(ENGINE_R22, fingerprint="seed-1")
    assert len(resolver) == 1
    assert "--accept-lang=nl-NL,nl,en-US,en" in args
    assert "--timezone=Europe/Amsterdam" in args
    assert "--webrtc-ip=203.0.113.7" in args

    del resolver[:]
    clearcote._reset_direct_geo_cache()
    args = prepared(ENGINE_R22, fingerprint="off")
    assert resolver == []
    for p in ("--accept-lang=", "--lang=", "--timezone=", "--webrtc-ip=", "--fingerprint-location=",
              "--webrtc-direct-stun"):
        assert named(args, p) == []


def test_the_lookup_a_persona_launch_cached_in_this_process_is_not_applied_either(prepared, resolver):
    prepared(ENGINE_R22, fingerprint="seed-1")
    args = prepared(ENGINE_R22, fingerprint="off")
    assert len(resolver) == 1
    for p in ("--accept-lang=", "--timezone=", "--webrtc-ip="):
        assert named(args, p) == []


def test_what_the_caller_set_is_kept(prepared, resolver):
    args = prepared(ENGINE_R22, fingerprint="off", timezone="Europe/Berlin", accept_language="de-DE",
                    webrtc_ip="198.51.100.4")
    assert resolver == []
    assert "--timezone=Europe/Berlin" in args
    assert "--accept-lang=de-DE,de,en-US,en" in args
    assert "--webrtc-ip=198.51.100.4" in args


def test_geoip_true_is_the_caller_asking_for_the_lookup(prepared, resolver, monkeypatch):
    monkeypatch.delenv("CLEARCOTE_GEOIP", raising=False)
    args = prepared(ENGINE_R22, fingerprint="off", geoip=True, timezone="Europe/Berlin")
    assert len(resolver) == 1
    assert "--timezone=Europe/Berlin" in args
    assert "--accept-lang=nl-NL,nl,en-US,en" in args
    assert "--webrtc-ip=203.0.113.7" in args


def test_the_docker_launch_does_not_look_the_address_up_for_passthrough_either(resolver):
    off = _docker.apply_geoip_before_start({"fingerprint": "off"}, quiet=True)
    assert resolver == []
    assert off == {"fingerprint": "off"}
    seeded = _docker.apply_geoip_before_start({"fingerprint": "seed-1"}, quiet=True)
    assert len(resolver) == 1
    assert seeded["timezone"] == "Europe/Amsterdam"
