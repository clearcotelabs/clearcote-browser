"""How the container's browser reaches a proxy that needs a password (stdlib only; serve.py uses it).

launch() on a host answers a proxy's login in one of two ways: an engine with --proxy-auth (http/https)
or --socks5-credentials (SOCKS5) answers it itself; for an engine without them, the automation driver
answers the 407 challenge. serve.py exec's the browser itself, so there is no driver in the container to
do that. ``plan()`` therefore picks the engine's own switch when the engine has it, and otherwise points
the browser, without credentials, at a ``Relay`` on 127.0.0.1 that adds them on the way out:

- an http:// or https:// proxy: the browser speaks HTTP proxy to the relay. A CONNECT tunnel (every https
  and WebSocket connection) goes upstream with a Proxy-Authorization header added and is then relayed byte
  for byte; a plain-http request is forwarded request by request on its kept-alive connection, each with
  the header added. Responses come back unchanged.
- a socks5:// proxy: the browser speaks SOCKS5 without authentication to the relay, which opens the same
  connection upstream with RFC 1929 username/password and then relays byte for byte. The browser hands
  host names to a SOCKS5 proxy, so names are still resolved at the proxy.

Only this container's loopback can reach the relay."""
import base64
import socket
import ssl
import threading
from urllib.parse import unquote

DEFAULT_PORTS = {"http": 80, "https": 443, "socks4": 1080, "socks5": 1080, "socks5h": 1080}


def split_proxy(url):
    """``(scheme, host, port, username, password)`` of a proxy URL (``http://`` when it has no scheme). The
    userinfo is percent-decoded and split at the LAST '@' of the authority, so an unescaped '@' in a
    password survives."""
    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()
    authority = rest.split("/", 1)[0]
    user = password = ""
    if "@" in authority:
        info, authority = authority.rsplit("@", 1)
        u, sep, pw = info.partition(":")
        user, password = unquote(u), (unquote(pw) if sep else "")
    host, port = authority, None
    if authority.startswith("["):  # [v6]:port
        inner, _, tail = authority[1:].partition("]")
        host = inner
        if tail.startswith(":") and tail[1:].isdigit():
            port = int(tail[1:])
    elif authority.count(":") == 1:
        h, p = authority.split(":")
        if p.isdigit():
            host, port = h, int(p)
    return scheme, host, port or DEFAULT_PORTS.get(scheme, 80), user, password


def server_url(scheme, host, port):
    return "%s://%s:%d" % (scheme, "[%s]" % host if ":" in host else host, port)


def engine_supports_switch(exe, name):
    """Whether the engine binary implements the command-line switch ``name``: switch names are
    NUL-terminated literals in the binary (the SDK's own probe, engine_supports_switch)."""
    needle = b"\x00" + name.encode("ascii") + b"\x00"
    try:
        with open(exe, "rb") as fh:
            tail = b""
            while True:
                chunk = fh.read(8 << 20)
                if not chunk:
                    return False
                if needle in tail + chunk:
                    return True
                tail = chunk[-len(needle):]
    except OSError:
        return False


def plan(proxy_url, exe):
    """``(browser_args, proxy_auth, relay)`` for CC_PROXY: the --proxy-server (and login) switches, how the
    proxy's login is answered ("engine", "relay", or None when it needs none) and the Relay started for it.
    Raises ValueError for a proxy the browser cannot use."""
    scheme, host, port, user, password = split_proxy(proxy_url)
    if scheme not in DEFAULT_PORTS or not host:
        raise ValueError("unsupported proxy %r (use http://, https://, socks4:// or socks5://)" % scheme)
    server = server_url("socks5" if scheme == "socks5h" else scheme, host, port)
    if not (user or password):
        return ["--proxy-server=" + server], None, None
    if scheme == "socks4":
        raise ValueError("a socks4 proxy takes no password; use socks5://")
    switch = "socks5-credentials" if scheme.startswith("socks5") else "proxy-auth"
    if engine_supports_switch(exe, switch):
        return ["--proxy-server=" + server, "--%s=%s:%s" % (switch, user, password)], "engine", None
    relay = Relay(proxy_url)
    return ["--proxy-server=" + relay.url], "relay", relay


class _Buffered:
    """A socket read through a buffer: request heads and bodies are cut out of it, the rest relayed."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def _fill(self):
        data = self.sock.recv(65536)
        if not data:
            raise EOFError
        self.buf += data

    def until(self, sep, limit=1 << 16):
        while sep not in self.buf:
            if len(self.buf) > limit:
                raise ValueError("header too long")
            self._fill()
        i = self.buf.index(sep) + len(sep)
        out, self.buf = self.buf[:i], self.buf[i:]
        return out

    def exact(self, n):
        while len(self.buf) < n:
            self._fill()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def take(self):
        out, self.buf = self.buf, b""
        return out


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _close(*socks):
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


class Relay:
    """A local proxy without a password in front of ``upstream_url``'s proxy, which has one."""

    def __init__(self, upstream_url, bind="127.0.0.1"):
        self.scheme, self.host, self.port, self.user, self.password = split_proxy(upstream_url)
        self.socks = self.scheme.startswith("socks5")
        self.auth = base64.b64encode(("%s:%s" % (self.user, self.password)).encode("utf-8"))
        self.server = socket.create_server((bind, 0))
        self.url = "%s://%s:%d" % ("socks5" if self.socks else "http", bind, self.server.getsockname()[1])
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        _close(self.server)

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _upstream(self):
        s = socket.create_connection((self.host, self.port), timeout=30)
        s.settimeout(None)
        if self.scheme == "https":
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        return s

    def _handle(self, client):
        up = None
        try:
            up = self._socks(client) if self.socks else self._http(client)
        except (OSError, ValueError, EOFError, IndexError):
            pass
        finally:
            _close(client, *([up] if up else []))

    # -- http(s) upstream ---------------------------------------------------------------------------
    def _with_auth(self, head):
        lines = head[:-4].split(b"\r\n")
        kept = [ln for ln in lines[1:] if not ln.lower().startswith(b"proxy-authorization:")]
        return b"\r\n".join([lines[0], b"Proxy-Authorization: Basic " + self.auth] + kept) + b"\r\n\r\n"

    @staticmethod
    def _body(c, head, up):
        fields = {}
        for line in head[:-4].split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            fields[k.strip().lower()] = v.strip()
        if b"chunked" in fields.get(b"transfer-encoding", b"").lower():
            while True:
                size_line = c.until(b"\r\n")
                up.sendall(size_line)
                if int(size_line.split(b";")[0].strip() or b"0", 16) == 0:
                    while True:  # trailers, up to the empty line
                        line = c.until(b"\r\n")
                        up.sendall(line)
                        if line == b"\r\n":
                            return
                up.sendall(c.exact(int(size_line.split(b";")[0].strip(), 16) + 2))
        left = int(fields.get(b"content-length", b"0") or 0)
        while left > 0:
            part = c.exact(min(left, 65536))
            up.sendall(part)
            left -= len(part)

    def _http(self, client):
        c = _Buffered(client)
        head = c.until(b"\r\n\r\n")
        up = self._upstream()
        back = threading.Thread(target=_pipe, args=(up, client), daemon=True)
        if head.split(b" ", 1)[0].upper() == b"CONNECT":
            up.sendall(self._with_auth(head) + c.take())
            back.start()
            _pipe(client, up)
        else:
            back.start()
            try:
                while True:  # absolute-form requests, one after another on this connection
                    up.sendall(self._with_auth(head))
                    self._body(c, head, up)
                    head = c.until(b"\r\n\r\n")
            except (EOFError, ValueError, OSError):
                pass
            try:
                up.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        back.join()
        return up

    # -- socks5 upstream ----------------------------------------------------------------------------
    def _socks(self, client):
        c = _Buffered(client)
        version, n = c.exact(2)
        c.exact(n)
        if version != 5:
            return None
        client.sendall(b"\x05\x00")  # no authentication between the browser and the relay
        req = c.exact(4)
        atyp = req[3]
        addr = c.exact(4) if atyp == 1 else c.exact(16) if atyp == 4 else (lambda n: n + c.exact(n[0]))(c.exact(1))
        dest = req + addr + c.exact(2)
        if req[1] != 1:  # CONNECT only
            client.sendall(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
            return None
        up = self._upstream()
        u = _Buffered(up)
        up.sendall(b"\x05\x01\x02")
        user, password = self.user.encode("utf-8"), self.password.encode("utf-8")
        if u.exact(2) != b"\x05\x02":
            client.sendall(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
            return up
        up.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        if u.exact(2)[1] != 0:
            client.sendall(b"\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00")
            return up
        up.sendall(dest)
        rep = u.exact(4)
        bound = u.exact(4) if rep[3] == 1 else u.exact(16) if rep[3] == 4 else (lambda n: n + u.exact(n[0]))(u.exact(1))
        client.sendall(rep + bound + u.exact(2) + u.take())
        if rep[1] != 0:
            return up
        up.sendall(c.take())
        back = threading.Thread(target=_pipe, args=(up, client), daemon=True)
        back.start()
        _pipe(client, up)
        back.join()
        return up
