"""docker/proxy_relay.py: how the Docker image's browser logs in to a proxy that needs a password. The engine
answers the login itself when it has the switch (--proxy-auth / --socks5-credentials); otherwise the browser
goes through a relay on the container's loopback that adds the login on the way out. Before, the image
dropped an http(s) proxy's password: the browser was challenged, nothing answered, every request failed.

The relay is driven here against stand-in upstream proxies that insist on the password. Loaded from the
repository's docker/ directory; skipped when the tree does not have it."""
import base64
import importlib.util
import os
import socket
import socketserver
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.normpath(os.path.join(HERE, "..", "..", "..", "docker", "proxy_relay.py"))
pytestmark = pytest.mark.skipif(not os.path.exists(PATH), reason="no docker/proxy_relay.py in this tree")

USER, PASSWORD = "user@corp", "p:a ss@w%rd"  # a ':' and '@' in the password, a '@' in the user
GOOD = b"Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode())


@pytest.fixture(scope="module")
def relay_mod():
    spec = importlib.util.spec_from_file_location("proxy_relay", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _url(scheme, port, user=USER, password=PASSWORD):
    from urllib.parse import quote
    return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@127.0.0.1:{port}"


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def _read_head(f):
    lines = []
    while True:
        line = f.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            return lines
        lines.append(line.rstrip(b"\r\n"))


@pytest.fixture
def http_upstream():
    """An HTTP proxy that wants GOOD in Proxy-Authorization: 407 otherwise. CONNECT: 200, then an echo.
    Absolute-form requests: read with their bodies (length or chunked) and answered on the same connection."""
    seen = []

    class H(socketserver.StreamRequestHandler):
        def handle(self):
            while True:
                head = _read_head(self.rfile)
                if head is None:
                    return
                fields = {}
                for ln in head[1:]:
                    k, _, v = ln.partition(b":")
                    fields.setdefault(k.strip().lower(), []).append(v.strip())
                auth = fields.get(b"proxy-authorization", [])
                seen.append((head[0], auth))
                if auth != [GOOD]:
                    self.wfile.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                     b"Proxy-Authenticate: Basic realm=\"t\"\r\nContent-Length: 0\r\n\r\n")
                    continue
                if head[0].startswith(b"CONNECT "):
                    self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    while True:
                        data = self.request.recv(65536)
                        if not data:
                            return
                        self.request.sendall(data)
                body = b""
                if b"chunked" in b"".join(fields.get(b"transfer-encoding", [])):
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(size)
                        self.rfile.readline()
                else:
                    body = self.rfile.read(int((fields.get(b"content-length") or [b"0"])[0]))
                reply = head[0] + b" body=" + body
                self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(reply), reply))

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def socks_upstream():
    """A SOCKS5 proxy that only accepts username/password (RFC 1929) with USER/PASSWORD, then echoes."""
    seen = []

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            s = self.request
            f = s.makefile("rb")
            ver, n = f.read(2)
            methods = f.read(n)
            if 2 not in methods:
                s.sendall(b"\x05\xff")
                return
            s.sendall(b"\x05\x02")
            f.read(1)
            user = f.read(f.read(1)[0]).decode()
            password = f.read(f.read(1)[0]).decode()
            seen.append(("login", user, password))
            if (user, password) != (USER, PASSWORD):
                s.sendall(b"\x01\x01")
                return
            s.sendall(b"\x01\x00")
            req = f.read(4)
            host = f.read(f.read(1)[0]).decode() if req[3] == 3 else socket.inet_ntoa(f.read(4))
            port = int.from_bytes(f.read(2), "big")
            seen.append(("connect", host, port))
            s.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            while True:
                data = s.recv(65536)
                if not data:
                    return
                s.sendall(data)

    srv = _Server(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], seen
    srv.shutdown()
    srv.server_close()


def _recv_until(s, marker, limit=1 << 16):
    data = b""
    while marker not in data and len(data) < limit:
        chunk = s.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def _relay_port(relay):
    return int(relay.url.rsplit(":", 1)[1])


def test_split_proxy(relay_mod):
    sp = relay_mod.split_proxy
    assert sp("http://u%40x:p%3Aw%20d@proxy.example:8080") == ("http", "proxy.example", 8080, "u@x", "p:w d")
    assert sp("socks5://u:p@ss@10.0.0.1:1080") == ("socks5", "10.0.0.1", 1080, "u", "p@ss")  # last '@' splits
    assert sp("proxy.example:3128") == ("http", "proxy.example", 3128, "", "")
    assert sp("https://[2001:db8::1]") == ("https", "2001:db8::1", 443, "", "")
    assert relay_mod.server_url("https", "2001:db8::1", 443) == "https://[2001:db8::1]:443"


def test_plan_uses_the_engines_own_login_when_it_has_one(relay_mod, tmp_path):
    with_switches = tmp_path / "chrome-licensed"
    with_switches.write_bytes(b"\x7fELF...\x00proxy-auth\x00...\x00socks5-credentials\x00...")
    without = tmp_path / "chrome-open"
    without.write_bytes(b"\x7fELF...\x00proxy-server\x00 proxy-authenticate ...")
    assert relay_mod.engine_supports_switch(str(with_switches), "proxy-auth")
    assert not relay_mod.engine_supports_switch(str(without), "proxy-auth")  # a header name is not the switch
    assert not relay_mod.engine_supports_switch(str(tmp_path / "missing"), "proxy-auth")

    assert relay_mod.plan("http://proxy.example:8080", str(without)) == (
        ["--proxy-server=http://proxy.example:8080"], None, None)
    assert relay_mod.plan(_url("http", 8080), str(with_switches)) == (
        ["--proxy-server=http://127.0.0.1:8080", f"--proxy-auth={USER}:{PASSWORD}"], "engine", None)
    assert relay_mod.plan(_url("socks5", 1080), str(with_switches)) == (
        ["--proxy-server=socks5://127.0.0.1:1080", f"--socks5-credentials={USER}:{PASSWORD}"], "engine", None)
    for scheme, front in (("http", "http"), ("https", "http"), ("socks5", "socks5")):
        args, how, relay = relay_mod.plan(_url(scheme, 9), str(without))
        try:
            assert how == "relay" and args == ["--proxy-server=" + relay.url]
            assert relay.url.startswith(f"{front}://127.0.0.1:") and USER not in relay.url
        finally:
            relay.close()
    with pytest.raises(ValueError, match="socks4 proxy takes no password"):
        relay_mod.plan(_url("socks4", 1080), str(without))
    with pytest.raises(ValueError, match="unsupported proxy"):
        relay_mod.plan("ftp://proxy.example:21", str(without))


def test_the_http_relay_logs_in_to_a_connect_tunnel(relay_mod, http_upstream):
    port, seen = http_upstream
    relay = relay_mod.Relay(_url("http", port))
    try:
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=10) as c:
            c.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n"
                      b"Proxy-Authorization: Basic d3Jvbmc=\r\n\r\n")  # whatever the browser sent is replaced
            assert _recv_until(c, b"\r\n\r\n").startswith(b"HTTP/1.1 200")
            c.sendall(b"tls bytes")
            assert _recv_until(c, b"bytes") == b"tls bytes"
        assert seen == [(b"CONNECT example.test:443 HTTP/1.1", [GOOD])]
    finally:
        relay.close()


def test_the_http_relay_logs_in_every_request_on_a_kept_alive_connection(relay_mod, http_upstream):
    port, seen = http_upstream
    relay = relay_mod.Relay(_url("http", port))
    try:
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=10) as c:
            c.sendall(b"GET http://example.test/a HTTP/1.1\r\nHost: example.test\r\nProxy-Connection: keep-alive\r\n\r\n")
            assert _recv_until(c, b"body=").endswith(b"GET http://example.test/a HTTP/1.1 body=")
            c.sendall(b"POST http://example.test/b HTTP/1.1\r\nHost: example.test\r\nContent-Length: 5\r\n\r\nhello")
            assert _recv_until(c, b"hello").endswith(b"body=hello")
            c.sendall(b"POST http://example.test/c HTTP/1.1\r\nHost: example.test\r\nTransfer-Encoding: chunked\r\n\r\n"
                      b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n")
            assert _recv_until(c, b"abcde").endswith(b"body=abcde")
        assert [auth for _line, auth in seen] == [[GOOD]] * 3
        assert [line.split(b" ")[1] for line, _a in seen] == [b"http://example.test/a", b"http://example.test/b",
                                                               b"http://example.test/c"]
    finally:
        relay.close()


def test_a_wrong_password_reaches_the_browser_as_the_proxys_own_refusal(relay_mod, http_upstream):
    port, _seen = http_upstream
    relay = relay_mod.Relay(_url("http", port, password="wrong"))
    try:
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=10) as c:
            c.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
            assert _recv_until(c, b"\r\n\r\n").startswith(b"HTTP/1.1 407")
    finally:
        relay.close()


def test_the_socks5_relay_logs_in_with_rfc1929(relay_mod, socks_upstream):
    port, seen = socks_upstream
    relay = relay_mod.Relay(_url("socks5", port))
    try:
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=10) as c:
            c.sendall(b"\x05\x01\x00")  # the browser offers no authentication
            assert c.recv(2) == b"\x05\x00"
            c.sendall(b"\x05\x01\x00\x03" + bytes([len(b"example.test")]) + b"example.test" + (443).to_bytes(2, "big"))
            assert _recv_until(c, b"\x00\x00", limit=10)[:2] == b"\x05\x00"
            c.sendall(b"tls bytes")
            assert _recv_until(c, b"bytes") == b"tls bytes"
        assert seen == [("login", USER, PASSWORD), ("connect", "example.test", 443)]  # the name, not an address
    finally:
        relay.close()
