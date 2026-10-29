"""geoip on by default (0.66.0): mode selection, fail-open default, no-proxy cache, open-build WebRTC IP.
The same cases as sdk/node/test/geoip-default.test.ts, with a stubbed resolver."""
import pytest

import clearcote
from clearcote import _docker
from clearcote.geoip import geoip_mode

GEO = {"ip": "203.0.113.7", "country": "DE", "timezone": "Europe/Berlin", "accept_language": "de-DE",
       "location": "52.52,13.405"}
PROXY = {"server": "http://proxy.example:8080"}


def test_geoip_mode_is_auto_when_unset_strict_for_true_off_for_false():
    assert geoip_mode(None, {}) == "auto"
    assert geoip_mode(True, {}) == "strict"
    assert geoip_mode(False, {}) == "off"


def test_clearcote_geoip_0_turns_the_default_off_but_not_an_explicit_true():
    for v in ("0", "false", "OFF", "no", " off "):
        assert geoip_mode(None, {"CLEARCOTE_GEOIP": v}) == "off"
    assert geoip_mode(None, {"CLEARCOTE_GEOIP": "1"}) == "auto"
    assert geoip_mode(True, {"CLEARCOTE_GEOIP": "0"}) == "strict"


@pytest.fixture
def resolver(monkeypatch):
    """The resolver apply_geoip asks: records the proxy and answers ``state["answer"]``."""
    clearcote._reset_direct_geo_cache()
    state = {"answer": (dict(GEO), None, 5), "asked": [], "timeouts": []}

    def resolve(proxy, quiet=False, timeout=None):
        state["asked"].append(proxy)
        state["timeouts"].append(timeout)
        return state["answer"]

    monkeypatch.setattr(clearcote, "resolve_geo_detailed", resolve)
    monkeypatch.setattr(clearcote, "warn_on_egress_drift", lambda *a, **k: None)
    monkeypatch.delenv("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    yield state
    clearcote._reset_direct_geo_cache()


def test_auto_with_no_proxy_looks_this_machines_own_ip_up_and_fills_every_field(resolver):
    fp = {}
    geo, auto_webrtc_ip = clearcote.apply_geoip_mode(fp, None, "auto", quiet=True)
    assert resolver["asked"] == [None]
    assert fp == {"timezone": "Europe/Berlin", "accept_language": "de-DE", "location": "52.52,13.405",
                  "webrtc_ip": "203.0.113.7"}
    assert geo["ip"] == "203.0.113.7" and auto_webrtc_ip is True
    assert resolver["timeouts"] == [10.0]  # the default lookup's own budget


def test_auto_budget_follows_clearcote_geoip_timeout_seconds(resolver, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "3")
    clearcote.apply_geoip_mode({}, PROXY, "auto", quiet=True)
    assert resolver["timeouts"] == [3.0]


def test_auto_reuses_a_no_proxy_lookup_but_looks_a_proxy_up_every_launch(resolver):
    clearcote.apply_geoip_mode({}, None, "auto", quiet=True)
    fp = {}
    _geo, auto_webrtc_ip = clearcote.apply_geoip_mode(fp, None, "auto", quiet=True)
    assert len(resolver["asked"]) == 1
    assert fp["timezone"] == "Europe/Berlin" and auto_webrtc_ip is True
    clearcote.apply_geoip_mode({}, PROXY, "auto", quiet=True)
    clearcote.apply_geoip_mode({}, PROXY, "auto", quiet=True)
    assert resolver["asked"] == [None, PROXY, PROXY]


def test_auto_keeps_what_the_caller_set_and_skips_the_lookup_when_nothing_is_left(resolver):
    fp = {"timezone": "Asia/Tokyo"}
    clearcote.apply_geoip_mode(fp, PROXY, "auto", quiet=True)
    assert fp["timezone"] == "Asia/Tokyo" and fp["accept_language"] == "de-DE"
    resolver["asked"] = []
    full = {"timezone": "a", "accept_language": "b", "location": "1,2", "webrtc_ip": "198.51.100.1"}
    assert clearcote.apply_geoip_mode(full, PROXY, "auto", quiet=True) == (None, False)
    assert resolver["asked"] == []


def test_auto_warns_and_launches_when_the_region_cannot_be_resolved(resolver, capsys):
    resolver["answer"] = (None, "could not determine the exit IP", 5)
    fp = {}
    assert clearcote.apply_geoip_mode(fp, PROXY, "auto", quiet=False) == (None, False)
    assert fp == {}
    err = capsys.readouterr().err
    assert "geoip (on by default)" in err and "geoip=False" in err
    assert "Launching anyway" not in err  # the strict message's advice is replaced, not repeated


def test_auto_failure_is_silent_when_quiet(resolver, capsys):
    resolver["answer"] = (None, "could not determine the exit IP", 5)
    clearcote.apply_geoip_mode({}, PROXY, "auto", quiet=True)
    assert capsys.readouterr().err == ""


def test_strict_still_fails_closed(resolver):
    resolver["answer"] = (None, "could not determine the exit IP", 5)
    with pytest.raises(clearcote.GeoipError):
        clearcote.apply_geoip_mode({}, PROXY, "strict", quiet=True)


def test_off_asks_nothing(resolver):
    fp = {}
    assert clearcote.apply_geoip_mode(fp, PROXY, "off", quiet=True) == (None, False)
    assert resolver["asked"] == [] and fp == {}


def test_drop_auto_webrtc_ip_only_on_an_open_build_launch_through_a_proxy():
    def fp():
        return {"timezone": "Europe/Berlin", "webrtc_ip": "203.0.113.7"}

    f = fp()
    clearcote.drop_auto_webrtc_ip(f, (None, True), PROXY, False)
    assert "webrtc_ip" not in f and f["timezone"] == "Europe/Berlin"
    f = fp()
    clearcote.drop_auto_webrtc_ip(f, (None, True), PROXY, True)
    assert f["webrtc_ip"] == "203.0.113.7"
    f = fp()
    clearcote.drop_auto_webrtc_ip(f, (None, True), None, False)
    assert f["webrtc_ip"] == "203.0.113.7"
    f = fp()
    clearcote.drop_auto_webrtc_ip(f, (None, False), PROXY, False)
    assert f["webrtc_ip"] == "203.0.113.7"


# ── wired into the launch paths ─────────────────────────────────────────────────────────────────────

def _fake_exe(tmp_path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"\x00")
    return str(exe)


def _prepare_args(tmp_path, **kw):
    exe, args, _pw, *_ = clearcote._prepare({"executable_path": _fake_exe(tmp_path), "quiet": True,
                                              "widevine": False, **kw})
    return args


def test_launch_default_fills_the_persona_from_the_egress(resolver, monkeypatch, tmp_path):
    monkeypatch.delenv("CLEARCOTE_GEOIP", raising=False)
    args = _prepare_args(tmp_path)
    assert resolver["asked"] == [None]
    assert "--timezone=Europe/Berlin" in args and "--webrtc-ip=203.0.113.7" in args


def test_launch_open_build_through_a_proxy_keeps_no_default_webrtc_ip(resolver, monkeypatch, tmp_path):
    monkeypatch.delenv("CLEARCOTE_GEOIP", raising=False)
    args = _prepare_args(tmp_path, proxy=PROXY)
    assert "--timezone=Europe/Berlin" in args
    assert not any(a.startswith("--webrtc-ip=") for a in args)
    # licensed (the PRO build blocks non-proxied WebRTC UDP), or geoip=True: kept
    assert "--webrtc-ip=203.0.113.7" in _prepare_args(tmp_path, proxy=PROXY, _cc_pro=("cc_lic_x", None))
    assert "--webrtc-ip=203.0.113.7" in _prepare_args(tmp_path, proxy=PROXY, geoip=True)


def test_launch_with_clearcote_geoip_0_asks_nothing(resolver, monkeypatch, tmp_path):
    monkeypatch.setenv("CLEARCOTE_GEOIP", "0")
    args = _prepare_args(tmp_path, proxy=PROXY)
    assert resolver["asked"] == [] and not any(a.startswith("--webrtc-ip=") for a in args)


def test_docker_default_drops_the_webrtc_ip_without_a_key_and_keeps_it_with_one(resolver, monkeypatch,
                                                                                 tmp_path):
    monkeypatch.delenv("CLEARCOTE_GEOIP", raising=False)
    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # this machine's own saved key is never read
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    kw = _docker.apply_geoip_before_start({"proxy": PROXY}, quiet=True)
    assert kw["timezone"] == "Europe/Berlin" and "webrtc_ip" not in kw and "geoip" not in kw
    kw = _docker.apply_geoip_before_start({"proxy": PROXY, "license_key": "cc_lic_x"}, quiet=True)
    assert kw["webrtc_ip"] == "203.0.113.7"
    kw = _docker.apply_geoip_before_start({"proxy": PROXY, "geoip": False}, quiet=True)
    assert "timezone" not in kw and "geoip" not in kw
