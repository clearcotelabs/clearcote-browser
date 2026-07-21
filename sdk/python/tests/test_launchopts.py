import warnings

from clearcote._launchopts import (
    extension_args,
    merge_feature_flags,
    privacy_sandbox_args,
    quic_args,
    resolve_proxy,
    webrtc_default_deny_args,
)


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


def test_resolve_proxy_socks5_with_creds_routes_to_switch_and_drops_pw_proxy():
    proxy = {"server": "socks5://h:1080", "username": "u", "password": "p"}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        args, pw = resolve_proxy(proxy)
    assert args == ["--proxy-server=socks5://h:1080"]
    assert pw is None  # Playwright would reject creds in a SOCKS descriptor -> drop it


def test_resolve_proxy_socks5_without_creds_left_to_playwright():
    proxy = {"server": "socks5://h:1080"}
    assert resolve_proxy(proxy) == ([], proxy)


def test_resolve_proxy_http_with_creds_left_to_playwright():
    # authed HTTP proxies stay on Playwright's path (inline-cred handling needs an engine change)
    proxy = {"server": "http://h:8080", "username": "u", "password": "p"}
    assert resolve_proxy(proxy) == ([], proxy)


def test_resolve_proxy_socks5_with_creds_warns():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        resolve_proxy({"server": "socks5://h:1", "username": "u", "password": "p"})
    assert any("SOCKS5" in str(w.message) for w in caught)
