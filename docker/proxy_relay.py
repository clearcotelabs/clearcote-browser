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

One thread relays each connection, both ways: an https:// proxy's connection is an ssl socket, and OpenSSL
does not allow one to be read in one thread while another writes to it (Python adds no lock).

Only this container's loopback can reach the relay."""
import base64
import selectors
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


def _fields(head):
    fields = {}
    for line in head[:-4].split(b"\r\n")[1:]:
        k, _, v = line.partition(b":")
        fields[k.strip().lower()] = v.strip()
    return fields


class _Requests:
    """The browser's plain-http requests to the proxy, cut out of its bytes as they arrive: each head is
    rewritten by ``with_auth``, each body (Content-Length or chunked) is passed on as it is."""

    def __init__(self, with_auth):
        self.with_auth = with_auth
        self.buf = b""
        self.state = "head"  # what the next line is: "head", a chunk's "size" line or a "trailer" line
        self.left = 0  # body bytes still to pass on (a chunk's closing CRLF included)

    def feed(self, data):
        """What to send to the proxy for ``data``, the browser's next bytes. ValueError: a head or chunk line
        that cannot be read."""
        self.buf += data
        out = []
        while True:
            if self.left:
                part, self.buf = self.buf[:self.left], self.buf[self.left:]
                out.append(part)
                self.left -= len(part)
                if self.left:
                    break
                continue
            sep = b"\r\n\r\n" if self.state == "head" else b"\r\n"
            i = self.buf.find(sep)
            if i < 0:
                if len(self.buf) > 1 << 16:
                    raise ValueError("header too long")
                break
            line, self.buf = self.buf[:i + len(sep)], self.buf[i + len(sep):]
            if self.state == "head":
                out.append(self.with_auth(line))
                fields = _fields(line)
                if b"chunked" in fields.get(b"transfer-encoding", b"").lower():
                    self.state = "size"
                else:
                    self.left = max(0, int(fields.get(b"content-length", b"0") or 0))
            elif self.state == "size":
                out.append(line)
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size:
                    self.left = size + 2
                else:
                    self.state = "trailer"
            else:  # trailers, up to the empty line
                out.append(line)
                if line == b"\r\n":
                    self.state = "head"
        return b"".join(out)


_CHUNK = 1 << 16
_BACKLOG = 1 << 20  # bytes waiting for one side before the relay stops reading the other
_READS = 16  # reads per wake-up: an ssl socket hands over one TLS record (16 KB at most) per read


class _End:
    """One socket of a relayed connection, non-blocking: the bytes waiting to be sent to it, and whether it
    is still read (its peer may send more) and written (the relay may send more)."""

    def __init__(self, sock, out=b""):
        sock.setblocking(False)
        self.sock = sock
        self.tls = isinstance(sock, ssl.SSLSocket)
        self.out = bytearray(out)
        self.reading = True
        self.writing = True
        self.wait = 0  # the event an unfinished ssl call waits for besides its own (a read may have to write)
        self.retry = 0  # the length of an ssl write to repeat: OpenSSL wants the same bytes again

    def ready_to_read(self):
        """Decrypted bytes already inside the ssl object, which the selector cannot see."""
        return self.tls and self.sock.pending() > 0

    def recv(self):
        """Bytes read now; b"" once the peer has ended (or the connection broke); None when none are ready."""
        try:
            return self.sock.recv(_CHUNK)
        except (BlockingIOError, InterruptedError, ssl.SSLWantReadError):
            return None
        except ssl.SSLWantWriteError:
            self.wait = selectors.EVENT_WRITE
            return None
        except OSError:
            return b""

    def send(self):
        """Sends what the socket takes now. False when it takes nothing more (its peer is gone)."""
        n = self.retry or min(len(self.out), _CHUNK)
        try:
            sent = self.sock.send(self.out[:n])
        except (BlockingIOError, InterruptedError):
            return True
        except (ssl.SSLWantWriteError, ssl.SSLWantReadError) as e:
            self.retry = n
            self.wait = selectors.EVENT_READ if isinstance(e, ssl.SSLWantReadError) else 0
            return True
        except OSError:
            return False
        self.retry = 0
        del self.out[:sent]
        return True

    def end_writing(self):
        """No more bytes for the peer (a TCP half-close). On an ssl socket it is done under the TLS layer:
        SSLSocket.shutdown() would drop that layer, and the bytes read afterwards would be passed on still
        encrypted."""
        self.writing = False
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_WR)
        except OSError:
            pass


def _watch(sel, sock, events):
    try:
        key = sel.get_key(sock)
    except KeyError:
        if events:
            sel.register(sock, events)
        return
    if not events:
        sel.unregister(sock)
    elif key.events != events:
        sel.modify(sock, events)


def _pump(client, up, forward=None, first=b""):
    """Relays between the browser's socket and the proxy's until both directions have ended, in this one
    thread: ``first`` and then the browser's bytes (through ``forward`` when given) go to the proxy, the
    proxy's bytes come back as they are. A side that has ended is half-closed toward the other once all
    that was read before it has been sent; a side that cannot be written any more stops the reading of the
    other."""
    browser, proxy = _End(client), _End(up, first)
    sel = selectors.DefaultSelector()
    try:
        while True:
            for src, dst in ((browser, proxy), (proxy, browser)):
                if not src.reading and not dst.out and dst.writing:
                    dst.end_writing()
            if not browser.writing and not proxy.writing:
                return
            for end, other in ((browser, proxy), (proxy, browser)):
                read = end.reading and len(other.out) < _BACKLOG
                _watch(sel, end.sock, end.wait | (selectors.EVENT_READ if read else 0)
                       | (selectors.EVENT_WRITE if end.out else 0))
            buffered = [e.sock for e, o in ((browser, proxy), (proxy, browser))
                        if e.reading and len(o.out) < _BACKLOG and e.ready_to_read()]
            ready = {key.fileobj for key, _mask in sel.select(0 if buffered else None)}
            ready.update(buffered)
            for end, other, transform in ((browser, proxy, forward), (proxy, browser, None)):
                if end.sock not in ready:
                    continue
                end.wait = 0
                if end.out and not end.send():
                    end.out.clear()
                    other.reading = False
                for _ in range(_READS):
                    if not end.reading or len(other.out) >= _BACKLOG:
                        break
                    data = end.recv()
                    if data is None:
                        break
                    if data == b"":
                        end.reading = False
                        break
                    try:
                        other.out += transform(data) if transform else data
                    except ValueError:
                        end.reading = False
    finally:
        sel.close()


def _close(*socks):
    for s in socks:
        try:
            s.close()
        except OSError:
            pass


class Relay:
    """A local proxy without a password in front of ``upstream_url``'s proxy, which has one. ``context``:
    the ssl context an https:// proxy's certificate is checked with (default: the system's CAs)."""

    def __init__(self, upstream_url, bind="127.0.0.1", context=None):
        self.scheme, self.host, self.port, self.user, self.password = split_proxy(upstream_url)
        self.context = context
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
            s = (self.context or ssl.create_default_context()).wrap_socket(s, server_hostname=self.host)
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

    def _http(self, client):
        c = _Buffered(client)
        head = c.until(b"\r\n\r\n")
        up = self._upstream()
        if head.split(b" ", 1)[0].upper() == b"CONNECT":
            _pump(client, up, first=self._with_auth(head) + c.take())
        else:  # absolute-form requests, one after another on this connection, each with the login
            requests = _Requests(self._with_auth)
            _pump(client, up, requests.feed, requests.feed(head + c.take()))
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
        _pump(client, up, first=c.take())
        return up
