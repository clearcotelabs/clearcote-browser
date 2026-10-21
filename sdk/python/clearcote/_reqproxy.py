"""A local forwarder that sends Playwright's request API through the caller's proxy.

Playwright's request API (``context.request``, ``page.request``) does not go through the browser.
The driver sends it with Node's own HTTP client, through the proxy in Playwright's launch/context
options (playwright-core: ``proxy: this._context._options.proxy || this._context._browser.options.proxy``)
and never through the browser's ``--proxy-server``. A proxy WITH credentials goes to the engine's own
switches (``resolve_proxy``) and is removed from Playwright's options, so before this module those
requests left from this machine's own IP: measured 2026-10-21 on SDK 0.55.1 + r46, an authenticated
proxy logged the page's CONNECT and never saw ``context.request`` or ``page.request``, both of which
returned 200.

The fix keeps the credentials away from Playwright (credentials there make its driver turn on Fetch
interception and ``Network.setCacheDisabled`` for the whole context, the transport tell
``resolve_proxy`` exists to avoid). Playwright gets a credential-less proxy on 127.0.0.1 instead; this
forwarder adds the credentials and tunnels to the real proxy (HTTP/HTTPS CONNECT, or SOCKS5 with
RFC 1929 auth and remote name resolution). The browser itself never uses it: Playwright puts its
``--proxy-server`` before the caller's args, ours comes later in the same argv and Chromium keeps the
last value of a switch; the same holds for the ``--proxy-bypass-list`` it adds (``<-loopback>``),
which ``request_proxy_args`` overrides. Playwright's fetch always tunnels with CONNECT
(HttpsProxyAgent), even for http:// URLs, so that is the one method served.

Plain sockets and daemon threads, not asyncio: the sync API runs Playwright's own loop in a greenlet
and the async API runs the caller's, and the forwarder has to serve both without either one.
Mirrors sdk/node/src/reqproxy.ts and sdk/dotnet/src/Clearcote/RequestProxy.cs."""

import base64
import ipaddress
import os
import re
import select
import socket
import ssl
import threading
import warnings
from collections import namedtuple
from contextlib import contextmanager
from urllib.parse import urlsplit

from ._launchopts import proxy_credentials

#: The real proxy the forwarder tunnels to.
Upstream = namedtuple("Upstream", "protocol host port username password")

#: What ``route_request_api`` started: the forwarder, and the engine args that go with it.
RequestRoute = namedtuple("RequestRoute", "proxy args")

HEAD_LIMIT = 16 * 1024
HANDSHAKE_S = 30.0
_POLL_S = 1.0  # how often a relay notices close(): select() is not woken by a socket closed elsewhere


def request_proxy_upstream(proxy, proxy_args):
    """The upstream the forwarder needs, or None when nothing has to be forwarded: no proxy, a proxy
    with no credentials, or one ``resolve_proxy`` left with Playwright (it reaches context.request
    already). ``proxy_args`` is what ``resolve_proxy`` returned; credentials moved to the engine
    exactly when it holds a --proxy-server."""
    if not isinstance(proxy, dict) or not any(a.startswith("--proxy-server=") for a in proxy_args):
        return None
    server, username, password = proxy_credentials(proxy)
    if not username and not password:
        return None
    try:
        url = urlsplit(server)
        url_port = url.port
    except ValueError:
        return None
    scheme = (url.scheme or "").lower()
    protocol = scheme if scheme in ("http", "https") else "socks5" if scheme in ("socks5", "socks5h") else None
    if not protocol or not url.hostname:
        return None  # socks4 has no password auth: nothing a forwarder can add
    port = url_port or (443 if protocol == "https" else 1080 if protocol == "socks5" else 80)
    # urlsplit lower-cases the host and drops the brackets of an IPv6 literal
    return Upstream(protocol, url.hostname, port, username or "", password or "")


def request_proxy_args(proxy_args):
    """Engine args that keep the browser's bypass list the caller's own: Playwright adds
    ``<-loopback>`` (proxy even loopback) whenever it is given a proxy, and an empty
    --proxy-bypass-list is the same as none to Chromium. Nothing to add when ``resolve_proxy``
    already wrote one."""
    return [] if any(a.startswith("--proxy-bypass-list=") for a in proxy_args) else ["--proxy-bypass-list="]


class RequestProxy:
    """A running forwarder: the proxy to hand Playwright (``playwright_proxy``), and ``close()``."""

    def __init__(self, upstream, bypass=None):
        self._upstream = upstream
        self._sockets = set()
        self._lock = threading.Lock()
        self._closed = False
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._listener.bind(("127.0.0.1", 0))
            self._listener.listen(64)
        except OSError:
            self._listener.close()
            raise
        port = self._listener.getsockname()[1]
        b = (bypass or "").strip()
        #: ``http://127.0.0.1:<port>``, plus the caller's bypass list so bypassed hosts go direct as
        #: in the browser.
        self.playwright_proxy = {"server": "http://127.0.0.1:%d" % port, **({"bypass": b} if b else {})}
        # daemon: never keeps a script alive on its own
        threading.Thread(target=self._accept_loop, name="clearcote-reqproxy", daemon=True).start()

    def close(self):
        """Stop accepting and drop every open tunnel. Idempotent, never raises, never blocks."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            socks = list(self._sockets)
            self._sockets.clear()
        # Linux keeps a socket listening while another thread sits in accept() on it, close() or not;
        # shutdown() wakes that accept. Windows refuses shutdown() on a listener, and needs only close().
        try:
            self._listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        _close(self._listener)
        for s in socks:
            _close(s)

    def _track(self, sock):
        with self._lock:
            if self._closed:
                _close(sock)
                return False
            self._sockets.add(sock)
            return True

    def _untrack(self, *socks):
        with self._lock:
            for s in socks:
                self._sockets.discard(s)
        for s in socks:
            _close(s)

    def _accept_loop(self):
        while not self._closed:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return  # the listener was closed
            if not self._track(client):
                return
            threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()

    def _handle_client(self, client):
        remote = None
        try:
            client.settimeout(HANDSHAKE_S)
            head, rest = _read_head(client)
            parts = head.split("\r\n", 1)[0].split(" ")
            dest = parse_host_port(parts[1]) if len(parts) >= 2 and parts[0] == "CONNECT" else None
            if dest is None:
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                return
            try:
                if is_loopback(dest[0]):
                    remote, early = socket.create_connection(dest, timeout=HANDSHAKE_S), b""
                else:
                    remote, early = open_tunnel(self._upstream, dest[0], dest[1])
            except Exception as exc:  # noqa: BLE001
                msg = re.sub(r"[\r\n]", " ", str(exc) or type(exc).__name__)[:200]
                client.sendall(("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nX-Clearcote-Proxy-Error: %s\r\n"
                                "Content-Length: 0\r\n\r\n" % msg).encode("latin-1", "replace"))
                return
            if not self._track(remote):
                return
            client.settimeout(None)
            remote.settimeout(None)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if rest:
                remote.sendall(rest)
            if early:
                client.sendall(early)
            self._relay(client, remote)
        except Exception:  # noqa: BLE001  (a dropped client, a timeout: the tunnel just ends)
            pass
        finally:
            self._untrack(client, *([remote] if remote is not None else []))

    def _relay(self, client, remote):
        """Copy both ways until both sides are done. One thread, select(): an SSL socket must not be
        read and written from two threads at once. Client EOF half-closes a plain upstream; a TLS one
        cannot be half-closed without dropping its session, so the tunnel ends there."""
        peer = {client: remote, remote: client}
        reading = [client, remote]
        while reading and not self._closed:
            ready = [s for s in reading if isinstance(s, ssl.SSLSocket) and s.pending()]
            if not ready:
                ready, _, _ = select.select(reading, [], [], _POLL_S)
            for src in ready:
                data = src.recv(65536)
                dst = peer[src]
                if data:
                    dst.sendall(data)
                    continue
                reading.remove(src)
                if isinstance(dst, ssl.SSLSocket):
                    return
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    return


def start_request_proxy(upstream, bypass=None):
    """Start a forwarder on 127.0.0.1 (a free port) for ``upstream``."""
    return RequestProxy(upstream, bypass)


def _close(sock):
    try:
        sock.close()
    except Exception:  # noqa: BLE001
        pass


def _read_head(sock):
    """Read up to the end of an HTTP head: ``(head, rest)``, ``rest`` being whatever arrived after it."""
    buf = b""
    while True:
        end = buf.find(b"\r\n\r\n")
        if end >= 0:
            return buf[:end].decode("latin-1"), buf[end + 4:]
        if len(buf) > HEAD_LIMIT:
            raise OSError("proxy response head too large")
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("connection closed before the end of the head")
        buf += chunk


def is_loopback(host):
    """Loopback never goes through the proxy, as in Chromium (its implicit bypass rules): the request
    API reached localhost directly before the forwarder existed, and a remote proxy cannot reach it
    anyway."""
    h = host.lower()
    if h.endswith("."):
        h = h[:-1]
    return (h == "localhost" or h.endswith(".localhost") or bool(re.match(r"^127\.\d+\.\d+\.\d+$", h))
            or h in ("::1", "0:0:0:0:0:0:0:1"))


_HOSTPORT_V6 = re.compile(r"^\[([^\]]+)\]:(\d{1,5})$")
_HOSTPORT = re.compile(r"^([^:\s]+):(\d{1,5})$")


def parse_host_port(target):
    """``(host, port)`` from ``host:port`` or ``[v6]:port``; None otherwise."""
    if not target:
        return None
    m = _HOSTPORT_V6.match(target) or _HOSTPORT.match(target)
    if not m:
        return None
    port = int(m.group(2))
    return (m.group(1), port) if 0 < port < 65536 else None


def _ip_family(host):
    try:
        return ipaddress.ip_address(host).version
    except ValueError:
        return 0


def _connect(upstream):
    sock = socket.create_connection((upstream.host, upstream.port), timeout=HANDSHAKE_S)
    if upstream.protocol != "https":
        return sock
    try:
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["http/1.1"])
        # server_hostname is also what the certificate is checked against; Python sends no SNI for an IP
        return ctx.wrap_socket(sock, server_hostname=upstream.host)
    except BaseException:
        sock.close()
        raise


def open_tunnel(upstream, host, port):
    """A socket already tunnelled through ``upstream`` to host:port: ``(sock, early)``, ``early``
    being bytes the destination sent that arrived with the proxy's reply."""
    try:
        sock = _connect(upstream)
    except socket.timeout:
        raise OSError("timed out connecting to the proxy %s:%d" % (upstream.host, upstream.port)) from None
    try:
        if upstream.protocol == "socks5":
            _socks5_connect(sock, upstream, host, port)
            early = b""
        else:
            early = _http_connect(sock, upstream, host, port)
        sock.settimeout(None)
        return sock, early
    except BaseException:
        _close(sock)
        raise


def _http_connect(sock, upstream, host, port):
    authority = "[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)
    token = base64.b64encode(("%s:%s" % (upstream.username, upstream.password)).encode("utf-8")).decode("ascii")
    sock.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\nProxy-Authorization: Basic %s\r\n\r\n"
                  % (authority, authority, token)).encode("utf-8"))
    head, rest = _read_head(sock)
    status = head.split("\r\n", 1)[0]
    if not re.match(r"^HTTP/1\.[01] 2\d\d", status):
        raise OSError("the proxy refused the tunnel: %s" % status)
    return rest


def _read_n(sock, n):
    """Read exactly n bytes."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("the SOCKS5 proxy closed the connection")
        buf += chunk
    return buf


_SOCKS_REPLY = {
    1: "general failure", 2: "connection not allowed by ruleset", 3: "network unreachable", 4: "host unreachable",
    5: "connection refused", 6: "TTL expired", 7: "command not supported", 8: "address type not supported",
}


def _socks5_connect(sock, upstream, host, port):
    user = upstream.username.encode("utf-8")
    pw = upstream.password.encode("utf-8")
    if len(user) > 255 or len(pw) > 255:
        raise OSError("SOCKS5 username and password are limited to 255 bytes each")
    sock.sendall(bytes([5, 1, 2]))  # version 5, one method: username/password
    choice = _read_n(sock, 2)
    if choice[0] != 5 or choice[1] != 2:
        raise OSError("the SOCKS5 proxy does not accept username/password authentication")
    sock.sendall(bytes([1, len(user)]) + user + bytes([len(pw)]) + pw)
    auth = _read_n(sock, 2)
    if auth[1] != 0:
        raise OSError("the SOCKS5 proxy rejected the username or password")
    family = _ip_family(host)
    v6 = ipv6_bytes(host) if family == 6 else None
    if family == 4:
        addr = bytes([1]) + socket.inet_aton(host)
    elif v6 is not None:
        addr = bytes([4]) + v6
    else:
        name = host.encode("utf-8")  # the proxy resolves the name, as the browser's SOCKS5 does
        if len(name) > 255:
            raise OSError("host name too long for SOCKS5")
        addr = bytes([3, len(name)]) + name
    sock.sendall(bytes([5, 1, 0]) + addr + bytes([port >> 8, port & 0xFF]))
    head = _read_n(sock, 4)
    if head[1] != 0:
        raise OSError("the SOCKS5 proxy could not connect: %s" % _SOCKS_REPLY.get(head[1], "reply %d" % head[1]))
    length = 4 if head[3] == 1 else 16 if head[3] == 4 else _read_n(sock, 1)[0]
    _read_n(sock, length + 2)  # bound address + port, unused


def ipv6_bytes(ip):
    """The 16 bytes of an IPv6 literal; None for a form with an embedded IPv4 tail (sent as a name
    instead) or a zone index."""
    if "." in ip or "%" in ip:
        return None
    try:
        return socket.inet_pton(socket.AF_INET6, ip)
    except (OSError, ValueError):
        return None


def route_request_api(proxy, proxy_args, quiet=False):
    """Start the forwarder when ``resolve_proxy`` moved the credentials to the engine, else None. A
    failure to start never fails the launch: it warns that the request API will not use the proxy."""
    upstream = request_proxy_upstream(proxy, proxy_args)
    if upstream is None:
        return None
    try:
        return RequestRoute(start_request_proxy(upstream, proxy.get("bypass")), request_proxy_args(proxy_args))
    except Exception as exc:  # noqa: BLE001
        warn_request_api("clearcote: could not start the local proxy for context.request (%s); context.request "
                         "and page.request will connect from this machine's IP, not through your proxy." % exc, quiet)
        return None


def warn_request_api(message, quiet=False):
    """A request-API warning, silenced by ``quiet`` or CLEARCOTE_NO_WARN like the other launch warnings."""
    if not quiet and not os.environ.get("CLEARCOTE_NO_WARN"):
        warnings.warn(message, stacklevel=3)


@contextmanager
def close_on_failure(route):
    """Around a launch: stop the forwarder when the browser never came up, then re-raise."""
    try:
        yield
    except BaseException:
        if route is not None:
            route.proxy.close()
        raise
