"""serve_multiplex: per-connection identities, routing, limits, request guards, idle close,
forwarded-host URLs and the socket-level WebSocket relay -- against fake browsers, so every behaviour
is observable without a binary (mirrors sdk/node/test/parity-multiplex.test.ts, plus the review
corrections: 409 on conflicting params, reserved "default", WS never launches, Host/Sec-Fetch guards,
close waits for launches)."""
import asyncio
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from clearcote._multiplex import (
    QUERY_PARAMS,
    SEED_PATTERN,
    MultiplexRequestError,
    _js_number,
    _rewrite_frontend_url,
    host_allowed,
    origin_allowed,
    parse_connection_identity,
    public_ws_base,
    request_forbidden,
    rewrite_ws_url,
    serve_multiplex,
)

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# -- parse_connection_identity ---------------------------------------------------------------------

def p(q):
    return parse_connection_identity(q)


def test_no_parameters_is_default():
    r = p("")
    assert (r["id"], r["options"], r["params"]) == ("default", {}, {})


def test_seed_is_identity_and_typed_options():
    r = p("fingerprint=acct-1&platform=windows&hardware-concurrency=8&device-pixel-ratio=1.25"
          "&fingerprint-noise=false&locale=de-DE&timezone=Europe/Berlin&geoip=true")
    assert r["id"] == "acct-1"
    assert r["options"] == {"fingerprint": "acct-1", "platform": "windows", "hardware_concurrency": 8,
                            "device_pixel_ratio": 1.25, "fingerprint_noise": False,
                            "accept_language": "de-DE", "timezone": "Europe/Berlin", "geoip": True}


def test_proxy_parsed_and_redacted():
    from urllib.parse import quote
    r = p("fingerprint=a&proxy=" + quote("socks5://u:pw@h.test:1080", safe=""))
    assert r["options"]["proxy"] == {"server": "socks5://h.test:1080", "username": "u", "password": "pw"}
    assert r["params"]["proxy"] == "socks5://h.test:1080 (with credentials)"
    assert "pw" not in json.dumps(r["params"])


@pytest.mark.parametrize("bad", ["../../etc", "a/b", "a\\b", "_p123", "", "x" * 129, "has space", "default"])
def test_rejects_unsafe_or_reserved_seeds(bad):
    from urllib.parse import quote
    with pytest.raises(MultiplexRequestError) as ei:
        p("fingerprint=" + quote(bad, safe=""))
    assert ei.value.status == 400


def test_seed_pattern():
    assert SEED_PATTERN.fullmatch("acct.1_x-Y")


def test_rejects_unknown_and_malformed():
    with pytest.raises(MultiplexRequestError, match="unknown query parameter 'gpu-vendr'"):
        p("fingerprint=a&gpu-vendr=x")
    with pytest.raises(MultiplexRequestError, match="must be a number"):
        p("hardware-concurrency=eight")
    with pytest.raises(MultiplexRequestError, match="true or false"):
        p("geoip=maybe")


def test_parameter_only_identity_is_stable():
    a, b = p("platform=macos&brand=Edge"), p("brand=Edge&platform=macos")
    assert a["id"].startswith("_p") and len(a["id"]) == 18
    assert a["id"] == b["id"]
    assert p("platform=linux")["id"] != a["id"]


def test_parameter_only_ids_match_the_node_sdk():
    # Computed with the Node SDK (JSON.stringify of the sorted [key, value] pairs); the same vectors are
    # in sdk/node/test/parity-multiplex.test.ts and the .NET MultiplexTests.
    from urllib.parse import quote
    assert p("platform=macos&brand=Edge")["id"] == "_p9bebdd7bbfdbb6c2"
    assert p("hardware-concurrency=8&device-pixel-ratio=1.25&fingerprint-noise=false&locale=de-DE"
             "&timezone=Europe/Berlin&proxy=" + quote("socks5://u:p+w@h.test:1080", safe=""))["id"] == "_p07a2e37476a5fbf5"
    # Number("1.0") === 1 in Node: the same identity as "1".
    assert p("device-pixel-ratio=1.0")["id"] == p("device-pixel-ratio=1")["id"]


def test_js_number_prints_like_javascript():
    for n, js in ((1.0, "1"), (8, "8"), (1.25, "1.25"), (-0.5, "-0.5"), (0.0, "0"), (1e-7, "1e-7"),
                  (0.000001, "0.000001"), (1e16, "10000000000000000"), (1e21, "1e+21"),
                  (123456789012345680000.0, "123456789012345680000"), (1.5e300, "1.5e+300"), (2.5e-10, "2.5e-10")):
        assert _js_number(n) == js, n


def test_engine_extras_are_kebab_params():
    assert QUERY_PARAMS["allow-third-party-cookies"] == "allow_third_party_cookies"
    assert QUERY_PARAMS["transparent-proxy"] == "transparent_proxy"
    assert QUERY_PARAMS["fingerprint-voices"] == "fingerprint_voices"


# -- URL + request guards ------------------------------------------------------------------------

def test_public_ws_base():
    assert public_ws_base({"host": "127.0.0.1:9222"}, "x") == "ws://127.0.0.1:9222"
    assert public_ws_base({"host": "internal:9222", "x-forwarded-host": "cdp.example.com, proxy2",
                           "x-forwarded-proto": "https"}, "x") == "wss://cdp.example.com"


def test_rewrite_ws_url():
    child = "ws://127.0.0.1:41234/devtools/browser/abc"
    assert rewrite_ws_url(child, "ws://127.0.0.1:9222", "default") == "ws://127.0.0.1:9222/devtools/browser/abc"
    assert rewrite_ws_url(child, "wss://cdp.example.com", "acct-1") == \
        "wss://cdp.example.com/fingerprint/acct-1/devtools/browser/abc"


def test_origin_allowed():
    assert origin_allowed(None)
    assert origin_allowed("http://localhost:3000")
    assert origin_allowed("http://127.0.0.1")
    assert not origin_allowed("null")
    assert not origin_allowed("https://evil.example")
    assert origin_allowed("https://tool.example", ["https://tool.example"])


def test_host_allowed():
    for ok in (None, "127.0.0.1:9222", "localhost", "LOCALHOST:1", "[::1]:9222", "10.0.0.5", "::1"):
        assert host_allowed(ok), ok
    for bad in ("evil.example", "evil.example:9222", "127.0.0.1.nip.io", ""):
        assert not host_allowed(bad), bad
    assert host_allowed("cdp.example.com:443", ["CDP.example.com"])


def test_rewrite_frontend_url_only_the_first_ws():
    url = "/devtools/inspector.html?ws=127.0.0.1:1/devtools/page/p1&x=1&ws=127.0.0.1:1/devtools/page/p2"
    assert _rewrite_frontend_url(url, "h:9", "/fingerprint/a") ==         "/devtools/inspector.html?ws=h:9/fingerprint/a/devtools/page/p1&x=1&ws=127.0.0.1:1/devtools/page/p2"


def test_request_forbidden():
    assert request_forbidden({"host": "127.0.0.1"}) is None
    # As in Node: an explicitly allowed Origin may come cross-site.
    allowed = {"host": "127.0.0.1", "sec-fetch-site": "cross-site", "origin": "https://tool.example"}
    assert request_forbidden(allowed, ["https://tool.example"]) is None
    assert request_forbidden(allowed) == "Forbidden: cross-site request"
    assert request_forbidden({"host": "127.0.0.1", "sec-fetch-site": "cross-site"})
    assert request_forbidden({"host": "127.0.0.1", "sec-fetch-site": "same-site"})
    assert request_forbidden({"host": "127.0.0.1", "sec-fetch-site": "none"}) is None
    assert request_forbidden({"host": "127.0.0.1", "origin": "https://evil.example"}) == "Forbidden origin"
    assert request_forbidden({"host": "rebind.example:9222"}) == "Forbidden host"


# -- integration against fake browsers -------------------------------------------------------------

class FakeBrowser:
    """A tiny CDP-shaped HTTP + WebSocket-upgrade echo server on the multiplexer's loop."""

    def __init__(self, options):
        self.options = options
        self.closed = False
        self.upgrades = []
        self.pid = 4242
        self.port = None
        self._server = None
        self._held = []

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader, writer):
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        lines = head.split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if "upgrade" in headers:
            self.upgrades.append({"path": path, "headers": headers})
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            await writer.drain()
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    writer.write(b"echo:" + data)
                    await writer.drain()
            finally:
                writer.close()
            return
        if path == "/json/version":
            body = {"Browser": "Chrome/152", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/uuid-1"}
        elif path == "/json/list":
            body = [{"id": "p1", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/p1",
                     "devtoolsFrontendUrl": f"/devtools/inspector.html?ws=127.0.0.1:{self.port}/devtools/page/p1"}]
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        data = json.dumps(body).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(data) + data)
        await writer.drain()
        # Like Chrome's DevTools server (measured live): the connection stays open after the
        # response, so the multiplexer must read by Content-Length, never to EOF.
        self._held.append(writer)

    async def close(self):
        self.closed = True
        self._server.close()
        for w in self._held:
            w.close()


@pytest.fixture
def mux():
    started = []

    def start(start_delay=0.0, **extra):
        browsers = []

        async def starter(opts):
            if start_delay:
                await asyncio.sleep(start_delay)
            b = await FakeBrowser(opts).start()
            browsers.append(b)
            return b

        m = serve_multiplex(port=0, quiet=True, start_browser=starter, **extra)
        started.append(m)
        return m, browsers

    yield start
    for m in started:
        m.close()


def get_json(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with _NO_PROXY.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def ws_round_trip(port, path, headers=None):
    """Raw WebSocket upgrade + one echo round trip. Returns (status_line, echoed)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        hdrs = {"Host": f"127.0.0.1:{port}", "Upgrade": "websocket", "Connection": "Upgrade",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"}
        hdrs.update(headers or {})
        s.sendall((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        status = buf.split(b"\r\n")[0].decode()
        if "101" not in status:
            return status, None
        s.sendall(b"ping")
        rest = buf.split(b"\r\n\r\n", 1)[1]
        while b"echo:ping" not in rest:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        return status, ("ping" if b"echo:ping" in rest else None)
    finally:
        s.close()


def test_routes_per_identity_reuses_seed_and_rewrites(mux):
    m, browsers = mux()
    _, a1 = get_json(f"{m.url}/json/version?fingerprint=acct-1&platform=windows")
    _, a2 = get_json(f"{m.url}/json/version/?fingerprint=acct-1&platform=windows")
    _, b = get_json(f"{m.url}/json/version?fingerprint=acct-2")
    _, d = get_json(f"{m.url}/json/version")
    assert len(browsers) == 3
    o = browsers[0].options
    assert (o["fingerprint"], o["platform"], o["host"], o["quiet"]) == ("acct-1", "windows", "127.0.0.1", True)
    assert a1["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{m.port}/fingerprint/acct-1/devtools/browser/uuid-1"
    assert a2["webSocketDebuggerUrl"] == a1["webSocketDebuggerUrl"]
    assert "/fingerprint/acct-2/devtools/browser/" in b["webSocketDebuggerUrl"]
    assert d["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{m.port}/devtools/browser/uuid-1"


def test_playwright_149_query_tail_path(mux):
    # Playwright <= 1.49 appends "/json/version/" to the whole endpoint string, query included.
    m, browsers = mux()
    code, a = get_json(f"{m.url}/?fingerprint=old-pw&platform=windows/json/version/")
    assert code == 200
    assert a["webSocketDebuggerUrl"] == f"ws://127.0.0.1:{m.port}/fingerprint/old-pw/devtools/browser/uuid-1"
    assert browsers[0].options["platform"] == "windows"
    code, lst = get_json(f"{m.url}/?fingerprint=old-pw&platform=windows/json/list")
    assert code == 200 and lst[0]["id"] == "p1"
    assert get_json(f"{m.url}/?fingerprint=old-pw&platform=windows/json")[0] == 200
    assert len(browsers) == 1
    assert get_json(f"{m.url}/?fingerprint=a/b")[0] == 200  # not a /json tail: the status page
    assert get_json(f"{m.url}/json/version?fingerprint=a/b")[0] == 400


def test_conflicting_parameters_for_running_identity_409(mux):
    m, browsers = mux()
    assert get_json(f"{m.url}/json/version?fingerprint=acct-1&timezone=Asia/Tokyo")[0] == 200
    code, body = get_json(f"{m.url}/json/version?fingerprint=acct-1&timezone=Europe/Paris")
    assert code == 409 and "different parameters" in body["error"]
    assert get_json(f"{m.url}/json/version?fingerprint=acct-1")[0] == 409
    assert get_json(f"{m.url}/json/version?fingerprint=acct-1&timezone=Asia/Tokyo")[0] == 200
    from urllib.parse import quote
    assert get_json(f"{m.url}/json/version?fingerprint=px&proxy=" + quote("http://u:a@h:1", safe=""))[0] == 200
    assert get_json(f"{m.url}/json/version?fingerprint=px&proxy=" + quote("http://u:b@h:1", safe=""))[0] == 409
    assert len(browsers) == 2


def test_forwarded_host_and_proto(mux):
    m, _ = mux()
    fwd = {"x-forwarded-host": "cdp.example.com", "x-forwarded-proto": "https"}
    _, r = get_json(f"{m.url}/json/version?fingerprint=acct-1", fwd)
    assert r["webSocketDebuggerUrl"] == "wss://cdp.example.com/fingerprint/acct-1/devtools/browser/uuid-1"
    _, lst = get_json(f"{m.url}/json/list?fingerprint=acct-1", fwd)
    assert lst[0]["webSocketDebuggerUrl"] == "wss://cdp.example.com/fingerprint/acct-1/devtools/page/p1"
    assert lst[0]["devtoolsFrontendUrl"] == "/devtools/inspector.html?ws=cdp.example.com/fingerprint/acct-1/devtools/page/p1"
    # a forwarded host is data, never a regex replacement template
    _, lst2 = get_json(f"{m.url}/json/list?fingerprint=acct-1", {"x-forwarded-host": r"a\1$1.example"})
    assert lst2[0]["devtoolsFrontendUrl"] == r"/devtools/inspector.html?ws=a\1$1.example/fingerprint/acct-1/devtools/page/p1"


def test_bad_seed_400_and_max_browsers_429(mux):
    m, browsers = mux(max_browsers=2)
    assert get_json(f"{m.url}/json/version?fingerprint=..%2F..%2Fetc")[0] == 400
    assert get_json(f"{m.url}/json/version?fingerprint=default")[0] == 400
    assert get_json(f"{m.url}/json/version?fingerprint=one")[0] == 200
    assert get_json(f"{m.url}/json/version?fingerprint=two")[0] == 200
    code, body = get_json(f"{m.url}/json/version?fingerprint=three")
    assert code == 429 and "maxBrowsers (2)" in body["error"]
    assert len(browsers) == 2


def test_http_guards_host_origin_sec_fetch(mux):
    m, browsers = mux()
    assert get_json(f"{m.url}/", {"Host": "rebind.example:9222"})[0] == 403
    assert get_json(f"{m.url}/json/version?fingerprint=a", {"Origin": "https://evil.example"})[0] == 403
    assert get_json(f"{m.url}/json/version?fingerprint=a", {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert browsers == []
    assert get_json(f"{m.url}/", {"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-origin"})[0] == 200


def test_http_allow_hosts(mux):
    m, _ = mux(allow_hosts=["cdp.example.com"])
    assert get_json(f"{m.url}/", {"Host": "cdp.example.com"})[0] == 200


def test_ws_relay_strips_origin_fixes_host(mux):
    m, browsers = mux()
    get_json(f"{m.url}/json/version?fingerprint=acct-1")
    status, echo = ws_round_trip(m.port, "/fingerprint/acct-1/devtools/browser/uuid-1", {"Origin": "http://localhost:5173"})
    assert "101" in status and echo == "ping"
    up = browsers[0].upgrades[0]
    assert up["path"] == "/devtools/browser/uuid-1"
    assert "origin" not in up["headers"]
    assert up["headers"]["host"] == f"127.0.0.1:{browsers[0].port}"


def test_ws_foreign_origin_and_bad_host_refused(mux):
    m, browsers = mux()
    get_json(f"{m.url}/json/version?fingerprint=acct-1")
    assert "403" in ws_round_trip(m.port, "/fingerprint/acct-1/devtools/browser/uuid-1", {"Origin": "https://evil.example"})[0]
    assert "403" in ws_round_trip(m.port, "/fingerprint/acct-1/devtools/browser/uuid-1", {"Host": "evil.example"})[0]
    assert "403" in ws_round_trip(m.port, "/fingerprint/acct-1/devtools/browser/uuid-1", {"Sec-Fetch-Site": "same-site"})[0]
    assert browsers[0].upgrades == []


def test_ws_route_never_launches(mux):
    m, browsers = mux()
    status, _ = ws_round_trip(m.port, "/fingerprint/direct-seed/devtools/browser/uuid-1")
    assert "404" in status and "identity not running" in status
    assert "404" in ws_round_trip(m.port, "/devtools/browser/uuid-1")[0]
    assert "404" in ws_round_trip(m.port, "/fingerprint/_p0123456789abcdef/devtools/browser/x")[0]
    assert browsers == []


def test_status_and_close(mux):
    from urllib.parse import quote
    m, browsers = mux()
    get_json(f"{m.url}/json/version?fingerprint=acct-1&proxy=" + quote("http://u:secret@p.test:8080", safe=""))
    _, st = get_json(f"{m.url}/")
    assert (st["status"], st["active"], st["maxBrowsers"], st["idleTimeoutSec"]) == ("ok", 1, 16, 0)
    proc = st["processes"][0]
    assert (proc["id"], proc["seed"], proc["pid"], proc["connections"]) == ("acct-1", "acct-1", 4242, 0)
    assert "secret" not in json.dumps(st)
    assert get_json(f"{m.url}/fingerprint/acct-1/close", method="POST") == (200, {"id": "acct-1", "terminated": True})
    assert browsers[0].closed
    assert get_json(f"{m.url}/fingerprint/acct-1/close", method="POST") == (200, {"id": "acct-1", "terminated": False})
    assert get_json(f"{m.url}/fingerprint/acct-1/close")[0] == 405


def test_idle_timeout_closes_after_last_connection(mux):
    m, browsers = mux(idle_timeout=0.3)
    get_json(f"{m.url}/json/version?fingerprint=idle-1")
    ws_round_trip(m.port, "/fingerprint/idle-1/devtools/browser/uuid-1")
    time.sleep(1.2)
    assert browsers[0].closed
    assert get_json(f"{m.url}/")[1]["active"] == 0


def test_profile_dirs_named_by_hash(mux, tmp_path):
    m, browsers = mux(data_dir=str(tmp_path / "profiles"))
    get_json(f"{m.url}/json/version?fingerprint=acct-1")
    udd = browsers[0].options["user_data_dir"]
    assert os.path.dirname(udd) == str(tmp_path / "profiles")
    name = os.path.basename(udd)
    assert len(name) == 24 and all(c in "0123456789abcdef" for c in name)
    assert "acct-1" not in udd


def test_close_waits_for_launch_in_flight_and_closes_it(mux):
    m, browsers = mux(start_delay=0.8)
    result = {}

    def req():
        try:
            result["r"] = get_json(f"{m.url}/json/version?fingerprint=slow")
        except Exception as e:  # noqa: BLE001  (shutdown ends the waiting request)
            result["e"] = e

    t = threading.Thread(target=req)
    t.start()
    time.sleep(0.3)  # the launch is now in flight
    m.close()
    t.join(10)
    assert not t.is_alive()
    assert len(browsers) == 1 and browsers[0].closed  # the late browser was closed, not leaked
    assert "e" in result or result["r"][0] == 503


def test_close_identity_on_launching_id(mux):
    m, browsers = mux(start_delay=0.6)
    result = {}
    t = threading.Thread(target=lambda: result.update(r=get_json(f"{m.url}/json/version?fingerprint=slow2")))
    t.start()
    time.sleep(0.2)
    assert m.close_identity("slow2") is True
    t.join(10)
    assert browsers[0].closed
    assert m.status()["active"] == 0
    assert result["r"][0] == 503  # the waiting request is told, and never handed the closed browser
