import warnings

from clearcote._launchopts import (
    extension_args,
    merge_feature_flags,
    privacy_sandbox_args,
    quic_args,
    socks5_udp_args,
    resolve_proxy,
    webrtc_default_deny_args,
)

import clearcote._launchopts as _lo  # module handle: web_bluetooth_args reads sys.platform


def test_merge_feature_flags_collapses_into_one_each():
    out = merge_feature_flags([
        "--enable-features=A", "--mute-audio", "--enable-features=B,C",
        "--disable-features=D", "--disable-features=D,E",
    ])
    assert [a for a in out if a.startswith("--enable-features=")] == ["--enable-features=A,B,C"]
    assert [a for a in out if a.startswith("--disable-features=")] == ["--disable-features=D,E"]
    assert "--mute-audio" in out


def test_privacy_sandbox_args():
    assert privacy_sandbox_args() == [
        "--disable-features=BrowsingTopics,BrowsingTopicsDocumentAPI,Fledge,InterestGroupStorage,"
        "PrivateAggregationApi,SharedStorageAPI,FencedFrames"
    ]


def test_webrtc_default_deny():
    assert webrtc_default_deny_args([], None) == ["--webrtc-ip-handling-policy=disable_non_proxied_udp"]
    # Regression: this used to return [] when webrtc_ip was set, on the theory that the engine's
    # srflx fabrication covered WebRTC. It does not. A page using iceTransportPolicy:"relay" forces
    # TURN; TURN prefers UDP; an HTTP/SOCKS proxy carries only TCP -- so the UDP left on the host's
    # own path and the TURN server read the real public IP off the packet, with no candidate
    # involved for the fabrication to rewrite. geoip=True sets webrtc_ip for you, so the coherent
    # configurations were the exposed ones.
    assert webrtc_default_deny_args([], "1.2.3.4") == ["--webrtc-ip-handling-policy=disable_non_proxied_udp"]
    assert webrtc_default_deny_args(["--webrtc-ip-handling-policy=default"], None) == []  # caller set it
    # An explicit caller policy still wins, even alongside a webrtc_ip.
    assert webrtc_default_deny_args(["--force-webrtc-ip-handling-policy=default"], "1.2.3.4") == []


def test_quic_args_disabled_only_when_proxied():
    # Behind any proxy (SOCKS or HTTP) QUIC can't tunnel -> disable so no UDP egresses around it.
    assert quic_args({"server": "socks5://host:1080"}) == ["--disable-quic"]
    assert quic_args({"server": "http://host:8080"}) == ["--disable-quic"]
    # No proxy -> leave QUIC on (matches real Chrome).
    assert quic_args(None) == []
    assert quic_args({}) == []  # malformed/empty proxy descriptor -> no flag


def test_extension_args_empty():
    assert extension_args(None) == []
    assert extension_args([]) == []


def test_extension_args_emits_load_and_disable_except():
    assert extension_args(["/a", "/b"]) == [
        "--load-extension=/a,/b",
        "--disable-extensions-except=/a,/b",
    ]


def test_resolve_proxy_passthrough_when_absent():
    assert resolve_proxy(None) == ([], None)


def test_resolve_proxy_socks5_with_creds_routes_to_switch_and_forwards_creds():
    proxy = {"server": "socks5://h:1080", "username": "u", "password": "p"}
    args, pw = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    # The engine implements RFC 1929, so the credentials are handed to it rather than dropped.
    assert args == ["--proxy-server=socks5://h:1080", "--socks5-credentials=u:p"]
    assert pw is None  # Playwright would reject creds in a SOCKS descriptor -> drop it


def test_resolve_proxy_socks5_strips_userinfo_already_in_url():
    proxy = {"server": "socks5://old:secret@h:1080", "username": "u", "password": "p"}
    args, pw = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    assert args == ["--proxy-server=socks5://h:1080", "--socks5-credentials=u:p"]
    assert pw is None


def test_resolve_proxy_socks5_without_creds_left_to_playwright():
    proxy = {"server": "socks5://h:1080"}
    assert resolve_proxy(proxy) == ([], proxy)


def test_resolve_proxy_http_with_creds_routes_to_engine_proxy_auth():
    # Credentials handed to Playwright switch on Fetch interception + Network.setCacheDisabled for
    # the whole context (a transport tell); the engine answers the 407 itself via --proxy-auth.
    proxy = {"server": "http://old:secret@h:3128", "username": "u", "password": "p"}
    args, pw = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    assert args == ["--proxy-server=http://h:3128", "--proxy-auth=u:p"]
    assert pw is None


def test_resolve_proxy_https_with_creds_keeps_bypass_list():
    proxy = {"server": "https://h:443", "username": "u", "password": "p", "bypass": "*.internal,localhost"}
    args, pw = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    assert args == ["--proxy-server=https://h:443", "--proxy-auth=u:p",
                    "--proxy-bypass-list=*.internal,localhost"]
    assert pw is None


def test_resolve_proxy_http_without_creds_left_to_playwright():
    proxy = {"server": "http://h:3128"}
    assert resolve_proxy(proxy) == ([], proxy)


def test_resolve_proxy_http_with_creds_no_longer_left_to_playwright():
    # Was: "authed HTTP proxies stay on Playwright's path (inline-cred handling needs an engine
    # change)". The engine change landed (--proxy-auth), and the Playwright path is exactly what
    # switches on Fetch interception + cache disabling, so the credentials never go there now.
    proxy = {"server": "http://h:8080", "username": "u", "password": "p"}
    args, pw = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    assert pw is None
    assert "--proxy-auth=u:p" in args


def test_resolve_proxy_socks5_with_creds_does_not_warn():
    # This used to warn that Chromium cannot authenticate SOCKS5 and to put the auth on a local
    # relay. The engine now speaks RFC 1929, so warning would send callers to a workaround they
    # no longer need.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolve_proxy({"server": "socks5://h:1", "username": "u", "password": "p"})
    assert not [w for w in caught if "SOCKS5" in str(w.message)]


# --------------------------------------------------------------------------- web bluetooth
# WHY THESE EXIST: Web Bluetooth is compiled into the engine but runtime-disabled on Linux only
# (Chromium marks WebBluetooth "stable" on Win/Mac and lets Linux fall to "experimental"), so a
# Linux host serving a Windows persona exposed navigator.usb/serial/hid but NOT
# navigator.bluetooth -- a combination no real Windows Chrome produces. The flag restores it.
def test_web_bluetooth_args_on_linux(monkeypatch):
    monkeypatch.setattr(_lo.sys, "platform", "linux")
    assert _lo.web_bluetooth_args() == ["--enable-features=WebBluetooth"]


def test_web_bluetooth_args_noop_off_linux(monkeypatch):
    for plat in ("win32", "darwin"):
        monkeypatch.setattr(_lo.sys, "platform", plat)
        assert _lo.web_bluetooth_args() == [], plat


def test_web_bluetooth_folds_into_one_enable_features(monkeypatch):
    """The flag must survive merge_feature_flags: Chromium honours only the LAST
    --enable-features, so a second occurrence would silently drop WebBluetooth."""
    monkeypatch.setattr(_lo.sys, "platform", "linux")
    merged = _lo.merge_feature_flags(
        _lo.web_bluetooth_args() + ["--enable-features=SomethingElse"])
    enables = [a for a in merged if a.startswith("--enable-features=")]
    assert len(enables) == 1
    assert "WebBluetooth" in enables[0] and "SomethingElse" in enables[0]


SOCKS5 = {"server": "socks5://gw.example.com:1080", "username": "u", "password": "p"}


def test_socks5_udp_emitted_only_when_opted_in():
    assert socks5_udp_args(True, SOCKS5) == ["--socks5-udp"]
    assert socks5_udp_args(False, SOCKS5) == []
    assert socks5_udp_args(None, SOCKS5) == []


def test_socks5_udp_silent_for_transports_that_cannot_carry_datagrams():
    # UDP ASSOCIATE is a SOCKS5 command. Emitting the switch for a transport that cannot relay a
    # datagram would be accepted and silently do nothing -- the failure mode this guards against.
    for server in ("http://p:8080", "https://p:8443", "socks4://p:1080"):
        assert socks5_udp_args(True, {"server": server}) == []


def test_socks5_udp_silent_without_a_proxy():
    assert socks5_udp_args(True, None) == []
    assert socks5_udp_args(True, {}) == []
    assert socks5_udp_args(True, {"server": ""}) == []


def test_socks5_udp_accepts_socks5h_and_is_case_insensitive():
    assert socks5_udp_args(True, {"server": "socks5h://p:1080"}) == ["--socks5-udp"]
    assert socks5_udp_args(True, {"server": "SOCKS5://p:1080"}) == ["--socks5-udp"]


def test_socks5_udp_composes_with_the_webrtc_deny_default():
    # Verified against the proxy's own log: the association is established with the deny policy in
    # force, so enabling UDP does not mean weakening the leak default.
    args = socks5_udp_args(True, SOCKS5)
    combined = args + webrtc_default_deny_args(args, None)
    assert "--socks5-udp" in combined
    assert "--webrtc-ip-handling-policy=disable_non_proxied_udp" in combined


def test_resolve_proxy_http_with_creds_stays_with_playwright_on_engines_without_proxy_auth():
    # r18 and earlier (and the free engine) do not implement --proxy-auth: routing the credentials
    # there would strip them from Playwright and every request would 407. Legacy path instead.
    proxy = {"server": "http://h:3128", "username": "u", "password": "p"}
    assert resolve_proxy(proxy) == ([], proxy)
    assert resolve_proxy(proxy, engine_supports_proxy_auth=False) == ([], proxy)


def test_resolve_proxy_socks5_routing_does_not_depend_on_the_proxy_auth_flag():
    proxy = {"server": "socks5://h:1080", "username": "u", "password": "p"}
    a1, _ = resolve_proxy(proxy)
    a2, _ = resolve_proxy(proxy, engine_supports_proxy_auth=True)
    assert a1 == a2 and any(a.startswith("--socks5-credentials=") for a in a1)


def test_engine_supports_switch_probes_the_nul_delimited_literal(tmp_path):
    from clearcote._launchopts import engine_supports_switch
    exe = tmp_path / "chrome"
    exe.write_bytes(b"xx\x00proxy-authenticate\x00yy")          # HPACK header name: must NOT match
    assert engine_supports_switch(str(exe), "proxy-auth") is False
    exe2 = tmp_path / "chrome2"
    exe2.write_bytes(b"xx\x00proxy-auth\x00yy")
    assert engine_supports_switch(str(exe2), "proxy-auth") is True
    assert engine_supports_switch(str(tmp_path / "missing"), "proxy-auth") is False
    assert engine_supports_switch("", "proxy-auth") is False


def test_warn_unsupported_engine_options_warns_only_for_missing_switches(tmp_path):
    from clearcote._launchopts import warn_unsupported_engine_options
    old = tmp_path / "old-chrome"; old.write_bytes(b"\x00socks5-credentials\x00")        # r17/r18-class engine
    new = tmp_path / "new-chrome"; new.write_bytes(b"\x00socks5-credentials\x00\x00fingerprint-schema\x00\x00fingerprint-gpu-backend-real\x00\x00proxy-auth\x00")
    fp = {"persona_schema": 2, "real_gpu_host": True}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_unsupported_engine_options(str(old), fp, {"server": "socks5://h:1080", "username": "u", "password": "p"})
    msgs = [str(w.message) for w in caught]
    assert any("persona_schema=2" in m for m in msgs) and any("real_gpu_host" in m for m in msgs)
    assert not any("SOCKS5" in m for m in msgs)                       # socks5 IS supported by the old engine
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_unsupported_engine_options(str(new), fp, {"server": "socks5://h:1080", "username": "u", "password": "p"})
    assert not caught                                                  # everything supported -> silence
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warn_unsupported_engine_options(str(old), {"persona_schema": 1}, None)
    assert not caught                                                  # schema 1 is the default: nothing to warn
