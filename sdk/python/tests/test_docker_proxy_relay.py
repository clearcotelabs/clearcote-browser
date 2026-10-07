"""docker/proxy_relay.py: how the Docker image's browser logs in to a proxy that needs a password. The engine
answers the login itself when it has the switch (--proxy-auth / --socks5-credentials); otherwise the browser
goes through a relay on the container's loopback that adds the login on the way out. Before, the image
dropped an http(s) proxy's password: the browser was challenged, nothing answered, every request failed.

The relay is driven here against stand-in upstream proxies that insist on the password. Loaded from the
repository's docker/ directory; skipped when the tree does not have it."""
import asyncio
import base64
import hashlib
import importlib.util
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import time

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


# -- an https:// proxy: the relay's connection to it is an ssl socket ------------------------------------
# OpenSSL does not allow an ssl socket to be read in one thread while another writes to it, and Python adds no
# lock: the relay read the proxy's connection in one thread and wrote it in another (expect "bad record mac"
# under load). Its half-close was SSLSocket.shutdown(), which drops the TLS layer: whatever the proxy sent after
# it reached the browser still encrypted.

MB = 1 << 20


@pytest.fixture(scope="module")
def tls_files(tmp_path_factory):
    """(CA file, certificate, key): a test CA and a certificate for 127.0.0.1 it signed, made with the openssl
    command (skipped without one)."""
    if not shutil.which("openssl"):
        pytest.skip("no openssl command to make a test certificate with")
    d = tmp_path_factory.mktemp("tls")
    (d / "ca.cnf").write_text("[req]\ndistinguished_name = dn\nx509_extensions = ca\nprompt = no\n"
                              "[dn]\nCN = relay test CA\n"
                              "[ca]\nbasicConstraints = critical, CA:TRUE\nkeyUsage = critical, keyCertSign\n"
                              "subjectKeyIdentifier = hash\n")
    (d / "leaf.cnf").write_text("[req]\ndistinguished_name = dn\nprompt = no\n[dn]\nCN = 127.0.0.1\n"
                                "[leaf]\nbasicConstraints = critical, CA:FALSE\nkeyUsage = critical, digitalSignature\n"
                                "extendedKeyUsage = serverAuth\nsubjectAltName = IP:127.0.0.1\n"
                                "subjectKeyIdentifier = hash\nauthorityKeyIdentifier = keyid\n")
    ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]
    for args in (["req", "-x509", "-config", "ca.cnf", *ec, "-keyout", "ca.key", "-out", "ca.pem", "-days", "2"],
                 ["req", "-new", "-config", "leaf.cnf", *ec, "-keyout", "leaf.key", "-out", "leaf.csr"],
                 ["x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-set_serial", "1",
                  "-days", "2", "-extfile", "leaf.cnf", "-extensions", "leaf", "-out", "leaf.pem"]):
        subprocess.run(["openssl", *args], cwd=str(d), check=True, capture_output=True, timeout=60)
    return str(d / "ca.pem"), str(d / "leaf.pem"), str(d / "leaf.key")


def _server_context(tls_files):
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(tls_files[1], tls_files[2])
    return ctx


def _watched_context(tls_files):
    """(context, log): the relay's client context, trusting the test CA, whose ssl sockets note in ``log`` every
    recv / send / shutdown call with the thread that made it."""
    log = []

    class Watched(ssl.SSLSocket):
        def recv(self, *a, **k):
            log.append((id(self), threading.get_ident(), "recv"))
            return super().recv(*a, **k)

        def send(self, *a, **k):  # sendall() sends through send()
            log.append((id(self), threading.get_ident(), "send"))
            return super().send(*a, **k)

        def shutdown(self, *a, **k):
            log.append((id(self), threading.get_ident(), "shutdown"))
            return super().shutdown(*a, **k)

    ctx = ssl.create_default_context(cafile=tls_files[0])
    ctx.sslsocket_class = Watched
    return ctx, log


def _one_thread_per_ssl_socket(log):
    threads = {}
    for sock, thread, _op in log:
        threads.setdefault(sock, set()).add(thread)
    assert threads, "the relay never used an ssl socket"
    assert {sock: len(t) for sock, t in threads.items()} == {sock: 1 for sock in threads}, \
        "an ssl socket was used by more than one thread"
    assert "shutdown" not in {op for _s, _t, op in log}, "SSLSocket.shutdown() drops the TLS layer"


@pytest.fixture
def https_upstream(tls_files):
    """An https:// proxy (TLS to the proxy itself) that wants GOOD in Proxy-Authorization: 407 otherwise. On an
    authorized request it sends X-Down random bytes WHILE it reads the request's own bytes (X-Up after a CONNECT;
    the body of any other request, by length or chunked), then the SHA-256 of what it sent and of what it read
    (128 hex digits). Tunnels then wait for the browser's side to end; other requests may follow on the
    connection. asyncio, so this server never shares one of its ssl sockets between threads either."""
    seen = []
    loop = asyncio.new_event_loop()

    async def read_body(reader, fields):
        if b"chunked" in fields.get(b"transfer-encoding", b"").lower():
            data = bytearray()
            while True:
                size = int((await reader.readuntil(b"\r\n")).split(b";")[0].strip(), 16)
                if size == 0:
                    while await reader.readuntil(b"\r\n") != b"\r\n":
                        pass
                    return bytes(data)
                data += (await reader.readexactly(size + 2))[:-2]
        return await reader.readexactly(int(fields.get(b"content-length", b"0")))

    async def handle(reader, writer):
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                lines = head[:-4].split(b"\r\n")
                fields = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(b":") for ln in lines[1:])}
                seen.append((lines[0], fields.get(b"proxy-authorization")))
                if fields.get(b"proxy-authorization") != GOOD:
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                                 b"Proxy-Authenticate: Basic realm=\"t\"\r\nContent-Length: 0\r\n\r\n")
                    continue
                down = os.urandom(int(fields.get(b"x-down", b"0")))
                tunnel = lines[0].startswith(b"CONNECT ")
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n" if tunnel else
                             b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (len(down) + 128))

                async def send():
                    for i in range(0, len(down), 1 << 16):
                        writer.write(down[i:i + (1 << 16)])
                        await writer.drain()

                read = reader.readexactly(int(fields[b"x-up"])) if tunnel else read_body(reader, fields)
                _sent, got = await asyncio.gather(send(), read)
                writer.write(hashlib.sha256(down).hexdigest().encode() + hashlib.sha256(got).hexdigest().encode())
                await writer.drain()
                if tunnel:
                    await reader.read()
                    return
        finally:
            writer.close()

    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    server = asyncio.run_coroutine_threadsafe(
        asyncio.start_server(handle, "127.0.0.1", 0, ssl=_server_context(tls_files)), loop).result(10)
    yield server.sockets[0].getsockname()[1], seen

    async def stop():
        server.close()
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run_coroutine_threadsafe(stop(), loop).result(10)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(10)
    loop.close()


def _read_exactly(f, n):
    data = f.read(n)
    assert len(data) == n, f"the connection ended after {len(data)} of {n} bytes"
    return data


def _both_ways(sock, f, upload, down_bytes):
    """Sends ``upload`` from another thread while reading the reply here: ``down_bytes`` bytes and then the
    server's two digests. Returns (what came down, the digest of what the server sent, of what it got)."""
    sender = threading.Thread(target=sock.sendall, args=(upload,), daemon=True)
    sender.start()
    got = _read_exactly(f, down_bytes)
    digests = _read_exactly(f, 128)
    sender.join(30)
    return got, digests[:64].decode(), digests[64:].decode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_an_https_proxy_tunnel_moves_large_bodies_both_ways_at_once(relay_mod, tls_files, https_upstream):
    port, seen = https_upstream
    ctx, log = _watched_context(tls_files)
    relay = relay_mod.Relay(_url("https", port), context=ctx)
    try:
        upload = os.urandom(16 * MB)
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=60) as c:
            f = c.makefile("rb")
            c.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n"
                      b"X-Up: %d\r\nX-Down: %d\r\n\r\n" % (len(upload), 16 * MB))
            assert _read_head(f)[0].startswith(b"HTTP/1.1 200")
            got, sent_digest, got_digest = _both_ways(c, f, upload, 16 * MB)
            assert _sha(got) == sent_digest  # what the proxy sent arrived intact (decrypted, in order)
            assert got_digest == _sha(upload)  # and so did what the browser sent
        assert seen == [(b"CONNECT example.test:443 HTTP/1.1", GOOD)]
        _one_thread_per_ssl_socket(log)
    finally:
        relay.close()


@pytest.mark.parametrize("chunked", [False, True])
def test_an_https_proxy_takes_plain_http_requests_with_large_bodies_both_ways_at_once(
        relay_mod, tls_files, https_upstream, chunked):
    port, seen = https_upstream
    ctx, log = _watched_context(tls_files)
    relay = relay_mod.Relay(_url("https", port), context=ctx)
    try:
        upload = os.urandom(8 * MB)
        if chunked:
            sizes = [1, 65535, 3 * MB, 7] + [MB] * 4
            sizes.append(len(upload) - sum(sizes))
            body, at = b"", 0
            for n in sizes:
                body += b"%x;ext=1\r\n" % n + upload[at:at + n] + b"\r\n"
                at += n
            body += b"0\r\nX-Trailer: t\r\n\r\n"
            framing = b"Transfer-Encoding: chunked\r\n"
        else:
            body, framing = upload, b"Content-Length: %d\r\n" % len(upload)
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=60) as c:
            f = c.makefile("rb")
            for path, up, down in ((b"/a", body, 8 * MB), (b"/b", b"", 5)):  # the second: same connection
                c.sendall(b"POST http://example.test%s HTTP/1.1\r\nHost: example.test\r\n%sX-Down: %d\r\n\r\n"
                          % (path, framing if up else b"Content-Length: 0\r\n", down))
                head = _read_head(f)
                assert head[0] == b"HTTP/1.1 200 OK"
                got, sent_digest, got_digest = _both_ways(c, f, up, down)
                assert _sha(got) == sent_digest
                assert got_digest == _sha(upload if up else b"")
        assert seen == [(b"POST http://example.test/a HTTP/1.1", GOOD), (b"POST http://example.test/b HTTP/1.1", GOOD)]
        _one_thread_per_ssl_socket(log)
    finally:
        relay.close()


def test_the_https_relay_still_decrypts_after_the_browser_half_closes(relay_mod, tls_files):
    # The browser's side ends first; the proxy then sends two TLS records and closes. Both must reach the browser
    # decrypted, and then the end of the connection. The relay passes the half-close on as a TCP FIN under the
    # TLS layer (Python cannot send a TLS close_notify and keep reading), which OpenSSL 3 servers take as an error
    # unless told to take it as the end of the stream: this proxy is.
    listener = socket.create_server(("127.0.0.1", 0))
    server_ctx = _server_context(tls_files)
    server_ctx.options |= getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    heard = []

    def serve():
        conn, _ = listener.accept()
        with server_ctx.wrap_socket(conn, server_side=True) as s:
            f = s.makefile("rb")
            head = _read_head(f)
            heard.append((head[0], [ln for ln in head if ln.lower().startswith(b"proxy-authorization:")]))
            s.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            got = f.read()  # up to the browser's half-close
            s.sendall(b"got " + got)
            time.sleep(0.2)
            s.sendall(b", and more")

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    ctx, log = _watched_context(tls_files)
    relay = relay_mod.Relay(_url("https", listener.getsockname()[1]), context=ctx)
    try:
        with socket.create_connection(("127.0.0.1", _relay_port(relay)), timeout=20) as c:
            f = c.makefile("rb")
            c.sendall(b"CONNECT example.test:443 HTTP/1.1\r\nHost: example.test:443\r\n\r\n")
            assert _read_head(f)[0].startswith(b"HTTP/1.1 200")
            c.sendall(b"ping")
            c.shutdown(socket.SHUT_WR)
            assert f.read() == b"got ping, and more"
        server.join(10)
        assert heard == [(b"CONNECT example.test:443 HTTP/1.1", [b"Proxy-Authorization: " + GOOD])]
        _one_thread_per_ssl_socket(log)
    finally:
        relay.close()
        listener.close()
