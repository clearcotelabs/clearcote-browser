"""The local forwarder that sends Playwright's request API through a credentialed proxy (_reqproxy.py).

Fake proxies (HTTP CONNECT with Basic auth, SOCKS5 with RFC 1929 auth) and a fake origin run on
127.0.0.1; Playwright's own request context goes through the forwarder, as context.request does in
a launch. The forwarder sends loopback straight to the target, so the origin is addressed by
non-loopback names the fake proxies map to this machine. Mirrors sdk/node/test/reqproxy.test.ts."""

import base64
import http.server
import socket
import socketserver
import threading
import time

import pytest

from clearcote._launchopts import resolve_proxy
from clearcote._reqproxy import (
    Upstream, ipv6_bytes, is_loopback, parse_host_port, request_proxy_args, request_proxy_upstream,
    route_request_api, start_request_proxy,
)

#: Non-loopback names the fake proxies route to this machine (the forwarder sends loopback direct).
FAKE = {"origin.test": "127.0.0.1", "10.255.255.1": "127.0.0.1"}


def _resolve_fake(h):
    return FAKE.get(h, h)


class _Threaded(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _serve(server, cleanups):
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def stop():
        server.shutdown()
        server.server_close()
    cleanups.append(stop)
    return server.server_address[1]


@pytest.fixture
def cleanups():
    fns = []
    yield fns
    while fns:
        fns.pop()()


def _pipe(a, b):
    def one(src, dst):
        try:
            while True:
                d = src.recv(65536)
                if not d:
                    break
                dst.sendall(d)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass
    threading.Thread(target=one, args=(b, a), daemon=True).start()
    one(a, b)


def origin(cleanups):
    """An origin that echoes the request path."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = ("hello " + self.path).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    return _serve(srv, cleanups)


def _recv_head(sock):
    buf = b""
    while b"\r\n\r\n" not in buf:
        d = sock.recv(4096)
        if not d:
            break
        buf += d
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head.decode("latin-1"), rest


def auth_http_proxy(user, pw, cleanups):
    """An HTTP CONNECT proxy that demands Basic auth and logs what it saw."""
    log = []
    want = "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode("utf-8")).decode()

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            c = self.request
            head, rest = _recv_head(c)
            lines = head.split("\r\n")
            method, target = lines[0].split(" ")[:2]
            if method != "CONNECT":
                c.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
                return
            hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
            ok = hdrs.get("proxy-authorization") == want
            log.append("%s auth=%s" % (target, "ok" if ok else "bad"))
            if not ok:
                c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                return
            h, _, p = target.rpartition(":")
            up = socket.create_connection((_resolve_fake(h), int(p)))
            c.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                up.sendall(rest)
            _pipe(c, up)
    return _serve(_Threaded(("127.0.0.1", 0), H), cleanups), log


def _read_n(s, n):
    b = b""
    while len(b) < n:
        d = s.recv(n - len(b))
        if not d:
            raise OSError("eof")
        b += d
    return b


def auth_socks5(user, pw, cleanups):
    """A SOCKS5 proxy that only accepts username/password auth, logging the target it was asked for."""
    log = []

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            c = self.request
            ver, n = _read_n(c, 2)
            methods = _read_n(c, n)
            if 2 not in methods:
                c.sendall(bytes([5, 0xFF]))
                return
            c.sendall(bytes([5, 2]))
            _, ul = _read_n(c, 2)
            u = _read_n(c, ul).decode("utf-8")
            pl = _read_n(c, 1)[0]
            p = _read_n(c, pl).decode("utf-8")
            ok = u == user and p == pw
            log.append("auth=%s" % ("ok" if ok else "bad"))
            if not ok:
                c.sendall(bytes([1, 1]))
                return
            c.sendall(bytes([1, 0]))
            _, cmd, _, atyp = _read_n(c, 4)
            if atyp == 1:
                host, kind = socket.inet_ntoa(_read_n(c, 4)), "ip"
            elif atyp == 3:
                host, kind = _read_n(c, _read_n(c, 1)[0]).decode(), "name"
            else:
                c.sendall(bytes([5, 8, 0, 1, 0, 0, 0, 0, 0, 0]))
                return
            port = int.from_bytes(_read_n(c, 2), "big")
            log.append("connect %s %s:%d" % (kind, host, port))
            try:
                up = socket.create_connection((_resolve_fake(host), port))
            except OSError:
                c.sendall(bytes([5, 5, 0, 1, 0, 0, 0, 0, 0, 0]))
                return
            c.sendall(bytes([5, 0, 0, 1, 127, 0, 0, 1, 0, 0]))
            _pipe(c, up)
    return _serve(_Threaded(("127.0.0.1", 0), H), cleanups), log


def forwarder(up, cleanups, bypass=None):
    rp = start_request_proxy(up, bypass)
    cleanups.append(rp.close)
    return rp


@pytest.fixture
def pw():
    # per test: a sync driver left running keeps its loop alive and breaks the async tests below
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        p = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip("Playwright driver unavailable: %s" % exc)
    yield p
    p.stop()


def _get(pw, cleanups, proxy, url):
    ctx = pw.request.new_context(proxy=proxy)
    cleanups.append(ctx.dispose)
    return ctx.get(url)


# -- request_proxy_upstream ---------------------------------------------------------------------

def test_upstream_is_none_unless_resolve_proxy_moved_credentials_to_the_engine():
    assert request_proxy_upstream(None, []) is None
    plain = {"server": "http://h:3128"}
    assert request_proxy_upstream(plain, resolve_proxy(plain, True)[0]) is None
    # an old engine without --proxy-auth leaves http credentials with Playwright, which proxies the request API itself
    http1 = {"server": "http://h:3128", "username": "u", "password": "p"}
    assert request_proxy_upstream(http1, resolve_proxy(http1, False)[0]) is None


def test_upstream_reads_http_https_socks5_with_decoded_url_credentials():
    h = {"server": "http://us%40er:p%3Ass@h:3128"}
    assert request_proxy_upstream(h, resolve_proxy(h, True)[0]) == Upstream("http", "h", 3128, "us@er", "p:ss")
    s = {"server": "https://proxy.example", "username": "u", "password": "p"}
    got = request_proxy_upstream(s, resolve_proxy(s, True)[0])
    assert (got.protocol, got.host, got.port) == ("https", "proxy.example", 443)
    k = {"server": "socks5://u:p@[::1]:1080"}
    got = request_proxy_upstream(k, resolve_proxy(k)[0])
    assert (got.protocol, got.host, got.port) == ("socks5", "::1", 1080)


def test_upstream_leaves_socks4_alone():
    k = {"server": "socks4://u:p@h:1080"}  # no password auth to add
    assert request_proxy_upstream(k, resolve_proxy(k)[0]) is None


def test_request_proxy_args_override_playwrights_loopback_bypass_unless_the_caller_set_one():
    assert request_proxy_args(["--proxy-server=http://h:1", "--proxy-auth=u:p"]) == ["--proxy-bypass-list="]
    assert request_proxy_args(["--proxy-server=http://h:1", "--proxy-bypass-list=*.internal"]) == []


# -- parsing helpers ----------------------------------------------------------------------------

def test_parse_host_port():
    assert parse_host_port("example.com:443") == ("example.com", 443)
    assert parse_host_port("[2001:db8::1]:8443") == ("2001:db8::1", 8443)
    assert parse_host_port("example.com") is None
    assert parse_host_port("h:0") is None
    assert parse_host_port("h:70000") is None


def test_ipv6_bytes():
    assert ipv6_bytes("::1").hex() == "00000000000000000000000000000001"
    assert ipv6_bytes("2001:db8::8:1").hex() == "20010db8000000000000000000080001"
    assert ipv6_bytes("::ffff:1.2.3.4") is None
    assert ipv6_bytes("1::2::3") is None


def test_is_loopback():
    assert is_loopback("127.8.9.10")
    assert is_loopback("LOCALHOST.")
    assert is_loopback("app.localhost")
    assert is_loopback("::1")
    assert not is_loopback("128.0.0.1")
    assert not is_loopback("localhost.example.com")


# -- the forwarder ------------------------------------------------------------------------------

def test_tunnels_playwright_request_api_through_an_authenticated_http_proxy(pw, cleanups):
    o = origin(cleanups)
    port, log = auth_http_proxy("u1", "p@ss:w0rd#", cleanups)
    rp = forwarder(Upstream("http", "127.0.0.1", port, "u1", "p@ss:w0rd#"), cleanups)
    assert rp.playwright_proxy["server"].startswith("http://127.0.0.1:")
    res = _get(pw, cleanups, rp.playwright_proxy, "http://origin.test:%d/via-forwarder" % o)
    assert res.text() == "hello /via-forwarder"
    assert log == ["origin.test:%d auth=ok" % o]


def test_tunnels_through_an_authenticated_socks5_proxy_which_resolves_names_itself(pw, cleanups):
    o = origin(cleanups)
    port, log = auth_socks5("sockuser", "s3cr3t:é", cleanups)
    rp = forwarder(Upstream("socks5", "127.0.0.1", port, "sockuser", "s3cr3t:é"), cleanups)
    assert _get(pw, cleanups, rp.playwright_proxy, "http://origin.test:%d/socks" % o).text() == "hello /socks"
    assert _get(pw, cleanups, rp.playwright_proxy, "http://10.255.255.1:%d/ip" % o).text() == "hello /ip"
    assert log == ["auth=ok", "connect name origin.test:%d" % o, "auth=ok", "connect ip 10.255.255.1:%d" % o]


def test_answers_502_naming_the_cause_when_the_upstream_refuses_the_credentials(pw, cleanups):
    o = origin(cleanups)
    port, log = auth_http_proxy("u1", "right", cleanups)
    rp = forwarder(Upstream("http", "127.0.0.1", port, "u1", "wrong"), cleanups)
    res = _get(pw, cleanups, rp.playwright_proxy, "http://origin.test:%d/x" % o)
    assert res.status == 502
    assert "407" in res.headers["x-clearcote-proxy-error"]
    assert log == ["origin.test:%d auth=bad" % o]


def test_connects_loopback_targets_directly_never_through_the_proxy(pw, cleanups):
    o = origin(cleanups)
    port, log = auth_http_proxy("u1", "p", cleanups)
    rp = forwarder(Upstream("http", "127.0.0.1", port, "u1", "p"), cleanups)
    assert _get(pw, cleanups, rp.playwright_proxy, "http://localhost:%d/lo" % o).text() == "hello /lo"
    assert log == []


def _port(rp):
    return int(rp.playwright_proxy["server"].rsplit(":", 1)[1])


def test_refuses_anything_but_connect(cleanups):
    rp = forwarder(Upstream("http", "127.0.0.1", 9, "u", "p"), cleanups)
    s = socket.create_connection(("127.0.0.1", _port(rp)), timeout=10)
    s.sendall(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")
    out = b""
    while True:
        d = s.recv(4096)
        if not d:
            break
        out += d
    s.close()
    assert out.startswith(b"HTTP/1.1 405 ")


def test_passes_the_callers_bypass_list_to_playwright(cleanups):
    rp = forwarder(Upstream("http", "127.0.0.1", 9, "u", "p"), cleanups, " *.internal ")
    assert rp.playwright_proxy["bypass"] == "*.internal"
    assert "username" not in rp.playwright_proxy and "password" not in rp.playwright_proxy


def test_stops_accepting_connections_after_close():
    rp = start_request_proxy(Upstream("http", "127.0.0.1", 9, "u", "p"))
    port = _port(rp)
    rp.close()
    rp.close()  # idempotent
    time.sleep(0.05)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_route_request_api_starts_a_forwarder_only_when_credentials_went_to_the_engine(cleanups):
    plain = {"server": "http://h:3128"}
    assert route_request_api(plain, resolve_proxy(plain, True)[0], True) is None
    authed = {"server": "http://u:p@h:3128"}
    route = route_request_api(authed, resolve_proxy(authed, True)[0], True)
    assert route is not None
    cleanups.append(route.proxy.close)
    assert route.args == ["--proxy-bypass-list="]
    assert "username" not in route.proxy.playwright_proxy


# -- wiring: every local launch hands Playwright the forwarder and stops it with the browser ------

SOCKS = {"server": "socks5://user:pa%3Ass@proxy.example:1080"}  # moved to the engine on any build


class _Emitter:
    """A stand-in Browser / BrowserContext that records its event handlers."""

    def __init__(self):
        self.handlers = {}

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def emit(self, event):
        for fn in self.handlers.get(event, []):
            fn(self)

    def new_page(self, **kw):
        return kw

    def new_context(self, **kw):
        return kw

    def close(self, **kw):
        self.emit("close")


def _fake_exe(tmp_path):
    exe = tmp_path / "fake-chrome.exe"
    exe.write_bytes(b"\x00")
    return str(exe)


def _accepts(proxy):
    port = int(proxy["server"].rsplit(":", 1)[1])
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
        return True
    except OSError:
        return False


def _check_launch_kwargs(cap):
    kw = cap["kw"]
    proxy = kw["proxy"]
    assert proxy["server"].startswith("http://127.0.0.1:")
    assert "username" not in proxy and "password" not in proxy  # no credentials: no Fetch interception
    assert "_cc_request_route" not in kw
    assert "--proxy-server=socks5://proxy.example:1080" in kw["args"]
    assert "--proxy-bypass-list=" in kw["args"]  # overrides Playwright's <-loopback>
    assert _accepts(proxy)
    return proxy


def _patch_sync(monkeypatch, cap, fail=False):
    import clearcote

    class _Chromium:
        def _go(self, kw):
            cap["kw"] = kw
            if fail:
                raise RuntimeError("browser did not start")
            cap["obj"] = _Emitter()
            return cap["obj"]

        def launch(self, **kw):
            return self._go(kw)

        def launch_persistent_context(self, _udd, **kw):
            return self._go(kw)

    class _PW:
        chromium = _Chromium()

    monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
    monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
    monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)


@pytest.mark.parametrize("entry,extra,event", [
    ("launch", {}, "close"),  # the default: a persistent context on a throwaway profile
    ("launch", {"ephemeral_profile": False}, "disconnected"),
    ("launch_persistent_context", {}, "close"),
])
def test_sync_launches_route_the_request_api_and_stop_with_the_browser(monkeypatch, tmp_path, entry, extra, event):
    import clearcote
    cap = {}
    _patch_sync(monkeypatch, cap)
    args = (str(tmp_path / "prof"),) if entry == "launch_persistent_context" else ()
    getattr(clearcote, entry)(*args, executable_path=_fake_exe(tmp_path), proxy=dict(SOCKS), quiet=True, **extra)
    proxy = _check_launch_kwargs(cap)
    cap["obj"].emit(event)
    assert not _accepts(proxy)


def test_sync_launch_failure_stops_the_forwarder(monkeypatch, tmp_path):
    import clearcote
    cap = {}
    _patch_sync(monkeypatch, cap, fail=True)
    with pytest.raises(RuntimeError):
        clearcote.launch(executable_path=_fake_exe(tmp_path), proxy=dict(SOCKS), quiet=True, ephemeral_profile=False)
    assert not _accepts(cap["kw"]["proxy"])


def test_no_forwarder_without_credentials(monkeypatch, tmp_path):
    import clearcote
    cap = {}
    _patch_sync(monkeypatch, cap)
    clearcote.launch(executable_path=_fake_exe(tmp_path), proxy={"server": "socks5://proxy.example:1080"},
                     quiet=True, ephemeral_profile=False)
    assert cap["kw"]["proxy"] == {"server": "socks5://proxy.example:1080"}
    assert "--proxy-bypass-list=" not in cap["kw"]["args"]


def _patch_async(monkeypatch, cap, fail=False):
    from clearcote import async_api

    class _Chromium:
        async def _go(self, kw):
            cap["kw"] = kw
            if fail:
                raise RuntimeError("browser did not start")
            cap["obj"] = _Emitter()
            return cap["obj"]

        async def launch(self, **kw):
            return await self._go(kw)

        async def launch_persistent_context(self, _udd, **kw):
            return await self._go(kw)

    class _PW:
        chromium = _Chromium()

        async def stop(self):
            pass

    async def _start():
        return _PW()

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(async_api, "_start_driver", _start)
    monkeypatch.setattr(async_api, "_bind_driver", lambda *a, **k: None)
    monkeypatch.setattr(async_api, "install_humanize", _noop)
    monkeypatch.setattr(async_api, "install_humanize_on_context", _noop)


@pytest.mark.parametrize("entry,event", [("launch", "disconnected"), ("launch_persistent_context", "close")])
async def test_async_launches_route_the_request_api_and_stop_with_the_browser(monkeypatch, tmp_path, entry, event):
    from clearcote import async_api
    cap = {}
    _patch_async(monkeypatch, cap)
    args = (str(tmp_path / "prof"),) if entry == "launch_persistent_context" else ()
    await getattr(async_api, entry)(*args, executable_path=_fake_exe(tmp_path), proxy=dict(SOCKS), quiet=True)
    proxy = _check_launch_kwargs(cap)
    cap["obj"].emit(event)
    assert not _accepts(proxy)


async def test_async_launch_failure_stops_the_forwarder(monkeypatch, tmp_path):
    from clearcote import async_api
    cap = {}
    _patch_async(monkeypatch, cap, fail=True)
    with pytest.raises(RuntimeError):
        await async_api.launch(executable_path=_fake_exe(tmp_path), proxy=dict(SOCKS), quiet=True)
    assert not _accepts(cap["kw"]["proxy"])


@pytest.mark.filterwarnings("ignore:.*cannot authenticate to a SOCKS5 proxy")
def test_serve_path_warns_instead_of_routing(monkeypatch, tmp_path):
    import clearcote
    monkeypatch.delenv("CLEARCOTE_NO_WARN", raising=False)
    with pytest.warns(UserWarning, match="attached over CDP"):
        _exe, args, pw_kwargs, *_ = clearcote._prepare(
            {"executable_path": _fake_exe(tmp_path), "proxy": dict(SOCKS), "_cc_direct": True})
    assert "_cc_request_route" not in pw_kwargs
    assert "--proxy-bypass-list=" not in args  # serve() starts Chromium itself: no Playwright <-loopback>
