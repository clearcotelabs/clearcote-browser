"""real_computer: this computer's screen / touch / display language instead of the persona's generated values.
Same cases as the Node suite (test/realcomputer.test.ts)."""

import pytest

from clearcote import _realcomputer as R
from clearcote import _warnings
from clearcote._fingerprint import FINGERPRINT_KEYS, fingerprint_args

FACTS = {
    "display": {"width": 3440, "height": 1440, "avail_width": 3440, "avail_height": 1392, "device_pixel_ratio": 1},
    "touch": 2,
    "language": "en-GB",
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    R.reset_host_facts()
    if hasattr(_warnings, "reset_seen_warnings"):
        _warnings.reset_seen_warnings()
    elif hasattr(_warnings, "_SEEN"):
        _warnings._SEEN.clear()
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    yield
    R.reset_host_facts()


def reader(facts=FACTS):
    calls = []

    def read():
        calls.append(1)
        return facts
    read.calls = calls
    return read


def test_parse_windows():
    assert R.parse_windows("3440 1440 3440 1392 96 2 en-GB") == FACTS
    assert R.parse_windows("1920 1080 1920 1032 120 0 de-DE") == {
        "display": {"width": 1536, "height": 864, "avail_width": 1536, "avail_height": 826, "device_pixel_ratio": 1.25},
        "touch": 0, "language": "de-DE"}


def test_parse_windows_broken_gives_nothing():
    assert R.parse_windows("") == {"display": None, "touch": None, "language": None}
    assert R.parse_windows("abc def") == {"display": None, "touch": None, "language": None}
    assert R.parse_windows("640 300 640 300 96 0 en-US")["display"] is None


def test_parse_mac():
    assert R.parse_mac("1512 982 1512 944 2 en-GB") == {
        "display": {"width": 1512, "height": 982, "avail_width": 1512, "avail_height": 944, "device_pixel_ratio": 2},
        "touch": 0, "language": "en-GB"}


def test_parse_xrandr():
    out = "\n".join([
        "Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767",
        "HDMI-1 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 527mm x 296mm",
        "DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm",
    ])
    assert R.parse_xrandr(out) == {"width": 2560, "height": 1440, "avail_width": 2560, "avail_height": 1440, "device_pixel_ratio": 1}
    assert R.parse_xrandr("eDP-1 connected 1366x768+0+0 (normal)")["width"] == 1366
    assert R.parse_xrandr("Screen 0: current 1024 x 768\nVGA-1 disconnected") is None


def test_plausibility():
    assert R.plausible_display({"width": 1920, "height": 1080, "avail_width": 2000, "avail_height": 1040, "device_pixel_ratio": 1}) is None
    assert R.plausible_display({"width": 1920, "height": 1080, "avail_width": 1920, "avail_height": 1040, "device_pixel_ratio": 5}) is None


def test_linux_without_display_server():
    assert R.read_host_facts("linux", {"LANG": "fr_FR.UTF-8"}) == {"display": None, "touch": None, "language": "fr-FR"}


def test_parts():
    assert R.real_computer_parts(True) == {"screen": True, "touch": True, "languages": True}
    assert R.real_computer_parts({"screen": True}) == {"screen": True, "touch": False, "languages": False}
    assert R.real_computer_parts(False) == {"screen": False, "touch": False, "languages": False}
    assert R.real_computer_parts(None) == {"screen": False, "touch": False, "languages": False}


def test_off_reads_nothing():
    r = reader()
    fp = {"fingerprint": "a1"}
    assert R.with_real_computer(fp, read=r) is fp
    assert r.calls == []


def test_true_fills_every_part_without_mutating():
    fp = {"fingerprint": "a1", "real_computer": True}
    out = R.with_real_computer(fp, read=reader())
    assert out["screen_width"] == 3440 and out["screen_height"] == 1440 and out["avail_height"] == 1392
    assert out["device_pixel_ratio"] == 1 and out["max_touch_points"] == 2 and out["accept_language"] == "en-GB"
    assert fp == {"fingerprint": "a1", "real_computer": True}


def test_picks_parts():
    out = R.with_real_computer({"fingerprint": "a1", "real_computer": {"screen": True}}, read=reader())
    assert out["screen_width"] == 3440
    assert "max_touch_points" not in out and "accept_language" not in out


def test_caller_values_win():
    out = R.with_real_computer({"fingerprint": "a1", "real_computer": True, "screen_width": 1920, "screen_height": 1080,
                                "max_touch_points": 0, "accept_language": "de-DE"}, read=reader())
    assert out["screen_width"] == 1920 and out["max_touch_points"] == 0 and out["accept_language"] == "de-DE"
    assert "avail_width" not in out


def test_proxy_with_geoip_keeps_languages():
    out = R.with_real_computer({"fingerprint": "a1", "real_computer": True}, proxied=True, read=reader())
    assert out["screen_width"] == 3440 and out["max_touch_points"] == 2
    assert "accept_language" not in out


def test_passthrough_untouched():
    r = reader()
    fp = {"fingerprint": "off", "real_computer": True}
    assert R.with_real_computer(fp, read=r) is fp
    assert r.calls == []


def test_unreadable_parts_warn_once(capsys):
    none = reader({"display": None, "touch": None, "language": "en-US"})
    out = R.with_real_computer({"fingerprint": "a1", "real_computer": True}, read=none)
    assert "screen_width" not in out and "max_touch_points" not in out and out["accept_language"] == "en-US"
    R.with_real_computer({"fingerprint": "a1", "real_computer": True}, read=none)
    err = capsys.readouterr().err
    assert err.count("real_computer:") == 1
    assert "screen and touch" in err


def test_quiet_no_note(capsys):
    R.with_real_computer({"fingerprint": "a1", "real_computer": True}, quiet=True,
                         read=reader({"display": None, "touch": None, "language": None}))
    assert "real_computer:" not in capsys.readouterr().err


def test_values_reach_the_engine_switches():
    out = R.with_real_computer({"fingerprint": "a1", "real_computer": True}, read=reader())
    args = fingerprint_args(out)
    for a in ("--fingerprint-screen-width=3440", "--fingerprint-screen-height=1440", "--fingerprint-avail-width=3440",
              "--fingerprint-avail-height=1392", "--fingerprint-max-touch-points=2"):
        assert a in args
    assert any(a.startswith("--accept-lang=en-GB") for a in args)


def test_local_only_and_docker():
    from clearcote.cloud import LOCAL_ONLY_OPTIONS
    from clearcote._docker import container_env
    assert "real_computer" in FINGERPRINT_KEYS
    assert "real_computer" in LOCAL_ONLY_OPTIONS
    container_env({"real_computer": False})
    with pytest.raises(ValueError, match="real_computer is not available"):
        container_env({"real_computer": True})
