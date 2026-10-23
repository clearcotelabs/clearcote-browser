"""Proxy tunnelling (_net), geoip timeout + fail-closed (#3), licence seats + licence-through-proxy
(mirrors sdk/node/test/parity-network.test.ts). Local servers only, no internet."""
import base64
import json
import os
import stat
import sys
import time
import warnings

import pytest

import clearcote
from clearcote import _license
from clearcote._net import proxied_request, to_proxy_spec
from clearcote.geoip import GeoipError, geoip_timeout_seconds, resolve_geo_detailed

from _proxies import start_http_proxy, start_origin, start_silent, start_socks5


@pytest.fixture
def servers():
    started = []

    def keep(s):
        started.append(s)
        return s

    yield keep
    for s in reversed(started):
        s.close()


# -- to_proxy_spec -------------------------------------------------------------------------------

def test_to_proxy_spec_splits_credentials_and_default_ports():
    assert to_proxy_spec("socks5://us%40er:p%3Ass@h.test") == {
        "server": "socks5://h.test:1080", "username": "us@er", "password": "p:ss"}
    assert to_proxy_spec("proxy.test:3128") == {"server": "http://proxy.test:3128"}
    assert to_proxy_spec({"server": "http://h:8080", "username": "u", "password": "p"}) == {
        "server": "http://h:8080", "username": "u", "password": "p"}
    assert to_proxy_spec(None) is None


# -- proxied_request -------------------------------------------------------------------------------

def test_direct_without_proxy(servers):
    origin = servers(start_origin(lambda *a: (200, '{"ok":1}')))
    r = proxied_request(f"http://127.0.0.1:{origin.port}/x")
    assert r.status == 200 and r.json() == {"ok": 1}


def test_http_proxy_absolute_form_with_auth(servers):
    origin = servers(start_origin(lambda m, path, h, body: (200, json.dumps({"path": path, "body": body}))))
    auth = "Basic " + base64.b64encode(b"u:p").decode()
    proxy = servers(start_http_proxy(require_auth=auth))
    r = proxied_request(f"http://127.0.0.1:{origin.port}/api?q=1", method="POST", body='{"a":1}',
                        headers={"content-type": "application/json"},
                        proxy={"server": f"http://127.0.0.1:{proxy.port}", "username": "u", "password": "p"})
    assert r.ok
    assert r.json() == {"path": "/api?q=1", "body": '{"a":1}'}
    assert proxy.log == [{"kind": "absolute", "target": f"http://127.0.0.1:{origin.port}/api?q=1", "auth": "Basic dTpw"}]


def test_http_proxy_407_surfaces_as_status(servers):
    origin = servers(start_origin(lambda *a: (200, "{}")))
    proxy = servers(start_http_proxy(require_auth="Basic nope"))
    r = proxied_request(f"http://127.0.0.1:{origin.port}/", proxy={"server": f"http://127.0.0.1:{proxy.port}"})
    assert r.status == 407
    assert origin.log == []


def test_socks5_with_credentials_sends_hostname(servers):
    origin = servers(start_origin(lambda *a: (200, '{"via":"socks"}')))
    socks = servers(start_socks5(user="alice", password="s3cret"))
    r = proxied_request(f"http://localhost:{origin.port}/", proxy=f"socks5://alice:s3cret@127.0.0.1:{socks.port}")
    assert r.json() == {"via": "socks"}
    assert socks.log == [{"host": "localhost", "port": origin.port, "user": "alice"}]


def test_socks5_rejected_credentials(servers):
    origin = servers(start_origin(lambda *a: (200, "{}")))
    socks = servers(start_socks5(user="alice", password="right"))
    with pytest.raises(Exception, match="rejected the username/password"):
        proxied_request(f"http://127.0.0.1:{origin.port}/", timeout=5,
                        proxy=f"socks5://alice:wrong@127.0.0.1:{socks.port}")
    assert origin.log == []


def test_https_connect_tunnel_is_used(servers):
    # CONNECT is issued for an https target; the TLS handshake then fails against a plain origin,
    # but the proxy log proves the tunnel went through the proxy with credentials.
    origin = servers(start_origin(lambda *a: (200, "{}")))
    proxy = servers(start_http_proxy())
    with pytest.raises(Exception):
        proxied_request(f"https://127.0.0.1:{origin.port}/", timeout=5,
                        proxy={"server": f"http://127.0.0.1:{proxy.port}", "username": "u", "password": "p"})
    assert proxy.log[0]["kind"] == "connect"
    assert proxy.log[0]["target"] == f"127.0.0.1:{origin.port}"
    assert proxy.log[0]["auth"] == "Basic dTpw"


def test_ipv6_proxy_host_not_double_bracketed():
    assert to_proxy_spec("socks5://u:p@[2001:db8::1]:1080") == {
        "server": "socks5://[2001:db8::1]:1080", "username": "u", "password": "p"}
    assert to_proxy_spec({"server": "http://[::1]:3128"}) == {"server": "http://[::1]:3128"}


def test_ipv6_target_is_bracketed_in_connect_line(servers):
    proxy = servers(start_http_proxy(require_auth="Basic nope"))  # answers 407 after logging
    with pytest.raises(Exception, match="407"):
        proxied_request("https://[::1]:8443/x", timeout=5, proxy={"server": f"http://127.0.0.1:{proxy.port}"})
    assert proxy.log[0]["target"] == "[::1]:8443"


def test_truncated_bodies_are_errors():
    from clearcote._net import _parse_response
    with pytest.raises(ConnectionError, match="incomplete response body"):
        _parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc")
    with pytest.raises(ConnectionError, match="incomplete chunked"):
        _parse_response(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab")
    with pytest.raises(ConnectionError, match="incomplete chunked"):
        _parse_response(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n")
    ok = _parse_response(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n0\r\n\r\n")
    assert ok.text() == "abc"
    assert _parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\nabcdef").text() == "abc"


def test_times_out_against_silent_proxy(servers):
    silent = servers(start_silent())
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="timed out"):
        proxied_request("http://example.invalid/", proxy=f"socks5://127.0.0.1:{silent.port}", timeout=0.4)
    assert time.monotonic() - t0 < 3


# -- geoip timeout + fail-closed (#3) --------------------------------------------------------------

def test_geoip_timeout_env():
    assert geoip_timeout_seconds({}) == 20
    assert geoip_timeout_seconds({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "7"}) == 7
    assert geoip_timeout_seconds({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "1.5"}) == 1.5
    assert geoip_timeout_seconds({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "0"}) == 20
    assert geoip_timeout_seconds({"CLEARCOTE_GEOIP_TIMEOUT_SECONDS": "abc"}) == 20


def test_geoip_reports_why_within_budget(servers):
    silent = servers(start_silent())
    t0 = time.monotonic()
    geo, reason, ms = resolve_geo_detailed(f"socks5://127.0.0.1:{silent.port}", quiet=True, timeout=1.5)
    assert geo is None
    assert "timed out" in reason or "exit IP through the proxy" in reason
    assert time.monotonic() - t0 < 5
    assert ms >= 1000


def test_geoip_names_a_proxy_scheme_it_cannot_tunnel_through():
    # The coherence warning that used to cover this ("geoip cannot resolve a SOCKS proxy") was wrong
    # for socks5 and never printed before the GeoipError anyway; the error has to say it.
    geo, reason, _ms = resolve_geo_detailed({"server": "socks4://127.0.0.1:1"}, quiet=True, timeout=1.5)
    assert geo is None
    assert reason == "socks4:// proxies are not supported; use http, https or socks5"
    with pytest.raises(GeoipError, match="socks4:// proxies are not supported"):
        clearcote.apply_geoip({}, {"server": "socks4://127.0.0.1:1"}, quiet=True)


def test_geoip_refused_proxy_reports_exit_ip_reason():
    geo, reason, _ms = resolve_geo_detailed({"server": "socks5://127.0.0.1:1"}, quiet=True, timeout=3)
    assert geo is None
    assert "exit IP through the proxy" in reason or "timed out" in reason


def _geo_origin(servers):
    """A local origin answering as an IP echo (/echo, /myecho) and as ip-api (/ipapi)."""
    return servers(start_origin(lambda m, path, h, b: (
        200, "203.0.113.7" if "echo" in path else json.dumps(
            {"status": "success", "countryCode": "JP", "timezone": "Asia/Tokyo", "lat": 1, "lon": 2,
             "query": "203.0.113.7"}))))


def test_geoip_goes_through_socks5_with_auth(servers, monkeypatch):
    # Point the IP echo + ip-api at a local origin and prove both requests went through SOCKS5.
    from clearcote import geoip
    origin = _geo_origin(servers)
    socks = servers(start_socks5(user="u1", password="p1"))
    monkeypatch.setattr(geoip, "IPECHO_URLS", (f"http://localhost:{origin.port}/echo",))
    monkeypatch.setattr(geoip, "IPAPI_URL", f"http://localhost:{origin.port}/ipapi")
    monkeypatch.setattr(geoip, "_mmdb_lookup", lambda *a, **k: None)
    monkeypatch.setenv("CLEARCOTE_GEOIP_IPAPI", "1")
    geo, reason, _ms = resolve_geo_detailed(
        {"server": f"socks5://127.0.0.1:{socks.port}", "username": "u1", "password": "p1"}, quiet=True)
    assert reason is None
    assert geo["timezone"] == "Asia/Tokyo" and geo["accept_language"].startswith("ja-JP")
    assert [e["host"] for e in socks.log] == ["localhost", "localhost"]
    assert all(e["user"] == "u1" for e in socks.log)


def test_geoip_never_asks_ip_api_unless_opted_in(servers, monkeypatch):
    # ip-api.com's free endpoint is for non-commercial use only (45 requests/min per IP): it is not
    # in the default chain any more. Without the database there is no answer, and the reason says how.
    from clearcote import geoip
    origin = _geo_origin(servers)
    monkeypatch.setattr(geoip, "IPECHO_URLS", (f"http://localhost:{origin.port}/echo",))
    monkeypatch.setattr(geoip, "IPAPI_URL", f"http://localhost:{origin.port}/ipapi")

    def no_db(ip, deadline, quiet, info=None):
        info["db"] = "downloading"
        return None
    monkeypatch.setattr(geoip, "_mmdb_lookup", no_db)
    monkeypatch.delenv("CLEARCOTE_GEOIP_IPAPI", raising=False)
    geo, reason, _ms = resolve_geo_detailed(None, quiet=True, timeout=5)
    assert geo is None
    assert "still downloading" in reason and "CLEARCOTE_GEOIP_IPAPI=1" in reason
    assert [e["url"] for e in origin.log] == ["/echo"]
    monkeypatch.setenv("CLEARCOTE_GEOIP_IPAPI", "1")
    geo, reason, _ms = resolve_geo_detailed(None, quiet=True, timeout=5)
    assert reason is None and geo["timezone"] == "Asia/Tokyo"
    assert [e["url"] for e in origin.log] == ["/echo", "/echo", "/ipapi"]


def test_geoip_echo_url_setting_replaces_the_public_echoes(servers, monkeypatch):
    from clearcote import geoip
    origin = _geo_origin(servers)
    monkeypatch.setattr(geoip, "IPECHO_URLS", ("http://127.0.0.1:1/never",))
    monkeypatch.setenv("CLEARCOTE_GEOIP_ECHO_URL", f" http://localhost:{origin.port}/myecho ,")
    seen = []

    def db(ip, deadline, quiet, info=None):
        seen.append(ip)
        return {"ip": ip, "country": "NL", "timezone": "Europe/Amsterdam", "accept_language": "nl-NL",
                "location": None}
    monkeypatch.setattr(geoip, "_mmdb_lookup", db)
    geo, reason, _ms = resolve_geo_detailed(None, quiet=True, timeout=5)
    assert reason is None and geo["timezone"] == "Europe/Amsterdam"
    assert seen == ["203.0.113.7"]
    assert [e["url"] for e in origin.log] == ["/myecho"]


def test_geoip_echo_settings_and_answers():
    from clearcote import geoip
    assert geoip.echo_urls({}) == ("https://api.ipify.org", "https://icanhazip.com")
    assert not any("ip-api" in u for u in geoip.echo_urls({}))
    assert geoip.echo_urls({"CLEARCOTE_GEOIP_ECHO_URL": "http://a/ip, http://b/ip"}) == ("http://a/ip", "http://b/ip")
    assert geoip.echo_urls({"CLEARCOTE_GEOIP_ECHO_URL": " , "}) == geoip.IPECHO_URLS
    assert geoip.ipapi_enabled({}) is False
    assert all(geoip.ipapi_enabled({"CLEARCOTE_GEOIP_IPAPI": v}) for v in ("1", "true", "YES", "on"))
    assert not geoip.ipapi_enabled({"CLEARCOTE_GEOIP_IPAPI": "0"})
    assert geoip._ip_from_echo("203.0.113.7\n") == "203.0.113.7"
    assert geoip._ip_from_echo('{"ip": "2001:db8::1"}') == "2001:db8::1"
    assert geoip._ip_from_echo('{"query": "203.0.113.8", "status": "success"}') == "203.0.113.8"
    assert geoip._ip_from_echo("{not json") == ""


@pytest.fixture
def short_geoip(monkeypatch):
    # A refused loopback port takes ~2s per attempt on Windows; keep the budget small.
    monkeypatch.setenv("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "1.5")


def test_apply_geoip_fails_closed(short_geoip):
    fp = {}
    with pytest.raises(GeoipError) as ei:
        clearcote.apply_geoip(fp, {"server": "socks5://127.0.0.1:1"}, quiet=True)
    assert ei.value.code == "GEOIP_UNRESOLVED"
    assert "timezone" not in fp


def test_apply_geoip_explicit_timezone_and_language_still_launches(short_geoip):
    fp = {"timezone": "Europe/Paris", "accept_language": "fr-FR,fr"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert clearcote.apply_geoip(fp, {"server": "socks5://127.0.0.1:1"}, quiet=False) is None
    assert fp == {"timezone": "Europe/Paris", "accept_language": "fr-FR,fr"}
    assert any("using the explicit timezone and accept_language" in str(w.message) for w in caught)


def test_apply_geoip_only_one_explicit_still_fails_closed(short_geoip):
    with pytest.raises(GeoipError):
        clearcote.apply_geoip({"timezone": "Europe/Paris"}, {"server": "socks5://127.0.0.1:1"}, quiet=True)


def test_prepare_raises_geoip_error_before_resolving_binary(monkeypatch):
    called = []
    monkeypatch.setattr(clearcote, "_resolve_binary", lambda *a, **k: called.append(1))
    monkeypatch.setenv("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "1.5")
    with pytest.raises(GeoipError):
        clearcote._prepare({"geoip": True, "proxy": {"server": "socks5://127.0.0.1:1"}, "quiet": True})
    assert called == []


def test_launch_releases_lease_when_geoip_fails(monkeypatch):
    stopped = []

    class Lease:
        token = "t"

        def stop(self):
            stopped.append(1)

    monkeypatch.setattr(clearcote, "_acquire_lease_from_kwargs", lambda kw: Lease())
    monkeypatch.setattr(clearcote, "_prepare", lambda kw: (_ for _ in ()).throw(GeoipError("x")))
    with pytest.raises(GeoipError):
        clearcote.launch(ephemeral_profile=False, geoip=True)
    assert stopped == [1]


# -- licence seats + licence through proxy -------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.delenv("CLEARCOTE_LICENSE_KEY", raising=False)
    monkeypatch.delenv("CLEARCOTE_LICENSE_THROUGH_PROXY", raising=False)
    monkeypatch.delenv("CLEARCOTE_LICENSE_API", raising=False)
    monkeypatch.setenv("CLEARCOTE_INSTANCE_ID", "test-instance")
    monkeypatch.setattr(_license, "_MACHINE_LEASES", {})
    return h


def test_license_through_proxy_requested():
    assert _license.license_through_proxy_requested(None, {}) is False
    assert _license.license_through_proxy_requested(None, {"CLEARCOTE_LICENSE_THROUGH_PROXY": "1"}) is True
    assert _license.license_through_proxy_requested(False, {"CLEARCOTE_LICENSE_THROUGH_PROXY": "true"}) is False


def test_get_session_seats_states(servers, home):
    def api(m, path, h, b):
        auth = h.get("authorization")
        if path != "/api/v1/lease/seats":
            return 404, '{"error":"not found"}'
        if auth == "Bearer cc_lic_good":
            return 200, '{"used":2,"limit":5,"plan":"team"}'
        if auth == "Bearer cc_lic_unl":
            return 200, '{"used":1,"limit":null,"plan":"pro"}'
        return 401, '{"error":"Invalid license key."}'

    srv = servers(start_origin(api))
    base = f"http://127.0.0.1:{srv.port}"
    g = _license.get_session_seats
    assert g(license_key="cc_lic_good", api_base=base) == {"state": "ok", "used": 2, "limit": 5, "plan": "team"}
    assert g(license_key="cc_lic_unl", api_base=base) == {"state": "ok", "used": 1, "limit": None, "plan": "pro"}
    assert g(license_key="cc_lic_bad", api_base=base) == {"state": "invalid", "reason": "Invalid license key."}
    old = servers(start_origin(lambda *a: (404, "{}")))
    assert g(license_key="cc_lic_good", api_base=f"http://127.0.0.1:{old.port}") == {
        "state": "unavailable", "reason": "this licence server does not report seats yet"}
    dead = g(license_key="cc_lic_good", api_base="http://127.0.0.1:1")
    assert dead["state"] == "unavailable" and "unreachable" in dead["reason"]
    assert g() == {"state": "no-key"}


def test_get_session_seats_via_proxy_only_when_enabled(servers, home):
    api = servers(start_origin(lambda *a: (200, '{"used":0,"limit":3}')))
    proxy = servers(start_http_proxy())
    base = f"http://127.0.0.1:{api.port}"
    px = f"http://127.0.0.1:{proxy.port}"
    _license.get_session_seats(license_key="k", api_base=base, proxy=px)
    assert proxy.log == []
    _license.get_session_seats(license_key="k", api_base=base, proxy=px, license_through_proxy=True)
    assert [e["target"] for e in proxy.log] == [f"{base}/api/v1/lease/seats"]


def test_acquire_lease_checkout_through_socks5_only_when_enabled(servers, home):
    def api(m, path, h, b):
        return 200, json.dumps({"lease_id": f"L-{path}", "token": "tok", "exp": int(time.time()) + 3600,
                                "lease_ttl_sec": 600, "heartbeat_interval_sec": 3600,
                                "concurrency": {"used": 1, "limit": 5}})

    origin = servers(start_origin(api))
    socks = servers(start_socks5())
    base = f"http://127.0.0.1:{origin.port}"
    proxy = {"server": f"socks5://127.0.0.1:{socks.port}"}

    via = _license.acquire_lease(license_key="cc_lic_proxy_route", api_base=base, proxy=proxy,
                                 license_through_proxy=True, quiet=True)
    assert via.token == "tok"
    assert socks.log == [{"host": "127.0.0.1", "port": origin.port, "user": None}]
    assert origin.log[-1]["url"] == "/api/v1/lease/checkout"
    # keyed by key|server|username: one gateway can select different exits by username
    assert f"cc_lic_proxy_route|socks5://127.0.0.1:{socks.port}|" in _license._MACHINE_LEASES

    direct = _license.acquire_lease(license_key="cc_lic_direct_route", api_base=base, proxy=proxy, quiet=True)
    assert direct.token == "tok"
    assert len(socks.log) == 1  # unchanged: the second checkout did not use the proxy
    assert len(origin.log) == 2
    for ml in _license._MACHINE_LEASES.values():
        ml._stop.set()


def test_license_key_storage(home):
    assert _license.license_key_source() == {"source": "none"}
    p = _license.save_license_key("  cc_lic_abcdefghijklmnop  ")
    assert p == _license.license_key_path()
    with open(p, "rb") as fh:
        assert fh.read() == b"cc_lic_abcdefghijklmnop\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert _license.license_key_source() == {"source": "file", "masked": "cc_lic_…mnop"}
    os.environ["CLEARCOTE_LICENSE_KEY"] = "cc_lic_fromenv_zzzz"
    try:
        assert _license.license_key_source()["source"] == "env"
    finally:
        del os.environ["CLEARCOTE_LICENSE_KEY"]
    assert _license.remove_license_key() is True
    assert not os.path.exists(p)
    assert _license.remove_license_key() is False
    with pytest.raises(_license.LicenseError):
        _license.save_license_key("   ")


def _drift_setup(servers, monkeypatch, state):
    from clearcote import geoip
    origin = servers(start_origin(lambda m, path, h, b: (200, state["ip"])))
    proxy = servers(start_http_proxy())
    monkeypatch.setattr(geoip, "IPECHO_URLS", (f"http://localhost:{origin.port}/echo",))
    return geoip, {"server": f"http://127.0.0.1:{proxy.port}"}, proxy


def test_egress_drift_detects_a_rotating_exit(servers, monkeypatch):
    state = {"ip": "203.0.113.7"}
    geoip, spec, proxy = _drift_setup(servers, monkeypatch, state)
    assert geoip.check_egress_drift(spec, "203.0.113.7") is None       # sticky: same exit
    state["ip"] = "203.0.113.99"
    assert geoip.check_egress_drift(spec, "203.0.113.7") == "203.0.113.99"
    assert geoip.check_egress_drift(spec, "2001:db8::1") is None       # other family: not a rotation
    assert geoip.check_egress_drift(None, "203.0.113.7") is None       # no proxy: nothing to check
    assert len(proxy.log) == 3                                          # every lookup went via the proxy


def test_egress_drift_warning_prints_once_and_respects_quiet(servers, monkeypatch, capsys):
    state = {"ip": "203.0.113.99"}
    geoip, spec, _proxy = _drift_setup(servers, monkeypatch, state)
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    assert geoip.warn_on_egress_drift(spec, "203.0.113.7", quiet=True) is None
    t = geoip.warn_on_egress_drift(spec, "203.0.113.7")
    t.join(10)
    err = capsys.readouterr().err
    assert err.count("clearcote: warning:") == 1
    assert "203.0.113.7 -> 203.0.113.99" in err and "sticky session" in err
    state["ip"] = "203.0.113.7"
    geoip.warn_on_egress_drift(spec, "203.0.113.7").join(10)
    assert capsys.readouterr().err == ""
