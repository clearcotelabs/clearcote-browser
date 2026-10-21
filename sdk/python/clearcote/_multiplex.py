"""serve_multiplex -- one CDP endpoint, many identities (mirrors the Node SDK's ``multiplex.ts``).

``serve()`` starts ONE browser with one persona. This puts an HTTP/WebSocket front on a port and
starts a separate browser per identity on demand, chosen by the connection URL::

    p.chromium.connect_over_cdp("http://127.0.0.1:9222?fingerprint=acct-1&platform=windows")
    p.chromium.connect_over_cdp("http://127.0.0.1:9222?fingerprint=acct-2&proxy=socks5://u:p@host:1080&geoip=true")

Playwright/Puppeteer fetch ``/json/version`` (keeping the query), receive a WebSocket URL routed
through this server (``/fingerprint/<id>/devtools/browser/<uuid>``), and connect. The same seed
with the same parameters reuses the same browser; the same seed with DIFFERENT parameters is refused
(409) while it runs. No seed and no parameters = one shared default browser.

Endpoints: GET / (status) - GET /json/version - GET /json/list - GET /json -
POST /fingerprint/<id>/close - WS /fingerprint/<id>/devtools/* - WS /devtools/* (default).
Browsers are only ever started by the HTTP /json/* routes; a WebSocket to an identity that is not
running is refused (404).

SAFETY:
  * binds 127.0.0.1 by default; a non-loopback bind prints a warning, because anyone who can reach
    the port can start browsers and drive them;
  * seeds are validated against a strict pattern and profile directories are named by a HASH of
    the identity, so no request value ever becomes part of a filesystem path;
  * every request (HTTP and WebSocket) is refused when it carries ``Sec-Fetch-Site: cross-site`` or
    ``same-site`` (unless its Origin is explicitly allowed), a browser Origin that is not loopback or explicitly allowed, or a Host header that
    is not an IP literal or "localhost" (or explicitly allowed) -- the same DNS-rebinding rule as
    Chrome's own DevTools server;
  * the number of concurrent browsers is capped (max_browsers, default 16).

WebSockets are relayed at the socket level (the upgrade request is rewritten and the two streams
piped), so no WebSocket library is needed and CDP traffic is not re-framed. Pure stdlib: an asyncio
loop on a background thread, so the returned handle is usable from synchronous code.
"""
from __future__ import annotations

import asyncio
import atexit
import hashlib
import inspect
import ipaddress
import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from ._net import _parse_response, to_proxy_spec

__all__ = [
    "SEED_PATTERN", "QUERY_PARAMS", "MultiplexRequestError", "MultiplexServer",
    "parse_connection_identity", "public_ws_base", "rewrite_ws_url", "origin_allowed",
    "host_allowed", "request_forbidden", "serve_multiplex",
]

# A seed usable in a URL path segment. Leading "_" is reserved for parameter-only identities and
# "default" for the shared no-parameter identity.
SEED_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESERVED_IDS = ("default",)

_NUMERIC = (
    "hardware_concurrency", "device_memory", "screen_width", "screen_height", "avail_width",
    "avail_height", "color_depth", "device_pixel_ratio", "max_touch_points", "storage_quota",
    "persona_schema",
)
_BOOLEAN = (
    "light_stealth", "real_gpu_host", "disable_gpu_fingerprint", "fingerprint_noise",
    "gpu_string_spoof", "canvas_noise", "fingerprint_voices", "allow_third_party_cookies",
    "transparent_proxy", "socks5_udp",
)
_STRING = (
    "platform", "platform_version", "brand", "brand_version", "gpu_vendor", "gpu_renderer",
    "location", "accept_language", "webrtc_ip", "webrtc_mdns", "tls_profile",
)
# Query parameter (kebab-case) -> launch option name, for every parameter a connection may set.
QUERY_PARAMS = {k.replace("_", "-"): k for k in (_NUMERIC + _BOOLEAN + _STRING)}
_SPECIAL = ("fingerprint", "timezone", "locale", "proxy", "geoip")


def seed_ok(value):
    return bool(SEED_PATTERN.fullmatch(value or ""))


class MultiplexRequestError(Exception):
    """A request the multiplexer refuses, with the HTTP status to answer."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _parse_bool(name, v):
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "on"):
        return True
    if t in ("0", "false", "no", "off"):
        return False
    raise MultiplexRequestError(400, f"query parameter '{name}' must be true or false")


def _parse_number(name, v):
    t = str(v).strip()
    try:
        return int(t)
    except ValueError:
        pass
    try:
        n = float(t)
    except ValueError:
        n = float("nan")
    if not math.isfinite(n):
        raise MultiplexRequestError(400, f"query parameter '{name}' must be a number")
    return n


def _camel(name):
    return re.sub(r"_([a-z0-9])", lambda m: m.group(1).upper(), name)


def _js_number(n):
    """A number as JavaScript's ``String(n)`` prints it (1.0 -> "1", 1e-7 -> "1e-7", 1e16 -> "10000000000000000")."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        raise TypeError(n)
    if isinstance(n, int):
        n = float(n)
    if n == 0:
        return "0"
    sign = "-" if n < 0 else ""
    # Shortest round-trip digits (the same digits JS picks), then JS's exponent rule.
    mant, _e, exp = repr(abs(n)).partition("e")
    digits = mant.replace(".", "")
    point = (mant.index(".") if "." in mant else len(mant)) + (int(exp) if exp else 0)
    stripped = digits.lstrip("0")
    point -= len(digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    k, e = len(digits), point  # value = 0.<digits> * 10**e
    if k <= e <= 21:
        out = digits + "0" * (e - k)
    elif 0 < e <= 21:
        out = digits[:e] + "." + digits[e:]
    elif -6 < e <= 0:
        out = "0." + "0" * -e + digits
    else:
        x = e - 1
        out = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if x > 0 else "-") + str(abs(x))
    return sign + out


def _js_json(v):
    """``JSON.stringify(v)`` for the values an identity holds (str, bool, number, dict, list, None)."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return _js_number(v) if math.isfinite(v) else "null"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, dict):  # insertion order, undefined (None) members dropped, as JSON.stringify does
        return "{" + ",".join(f"{_js_json(str(k))}:{_js_json(x)}" for k, x in v.items() if x is not None) + "}"
    return "[" + ",".join(_js_json(x) for x in v) + "]"


def _canonical(options):
    """The Node SDK's canonical form, byte for byte: ``JSON.stringify`` of the [key, value] pairs sorted by
    the (camelCase) Node option name, so a parameter-only ``_p<hash>`` id is the same in every SDK."""
    pairs = sorted(((_camel(k), v) for k, v in options.items()),
                   key=lambda kv: kv[0].encode("utf-16-be"))
    return _js_json([[k, v] for k, v in pairs])


def parse_connection_identity(query):
    """Turn a connection's query string (or ``(name, value)`` pairs) into an identity.

    Returns ``{"id", "seed"?, "options", "params", "canonical"}``: ``id`` is "default", the seed, or
    ``_p<sha256-16>`` for a parameter-only identity; ``options`` are launch kwargs; ``params`` the raw
    accepted parameters with proxy credentials redacted; ``canonical`` an order-independent form of
    the options (proxy credentials included) used to detect a conflicting reconnect. Unknown
    parameters raise :class:`MultiplexRequestError` (400) rather than being guessed at, so a typo
    never silently launches a different identity."""
    pairs = parse_qsl(query, keep_blank_values=True) if isinstance(query, str) else list(query or ())
    options, params = {}, {}
    seed = None
    for name, value in pairs:
        if name in _SPECIAL:
            if name == "fingerprint":
                if not seed_ok(value):
                    raise MultiplexRequestError(400, "fingerprint must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
                if value in _RESERVED_IDS:
                    raise MultiplexRequestError(
                        400, f"fingerprint '{value}' is reserved for the no-parameter browser; pick another seed")
                seed = value
                options["fingerprint"] = value
            elif name == "timezone":
                options["timezone"] = value
            elif name == "locale":
                options["accept_language"] = value
            elif name == "geoip":
                options["geoip"] = _parse_bool(name, value)
            elif name == "proxy":
                try:
                    spec = to_proxy_spec(value)
                except Exception:  # noqa: BLE001
                    raise MultiplexRequestError(
                        400, "proxy must be a URL such as socks5://user:pass@host:1080") from None
                if not spec:
                    raise MultiplexRequestError(400, "proxy is empty")
                options["proxy"] = dict(spec)
                params["proxy"] = spec["server"] + (" (with credentials)" if spec.get("username") else "")
                continue
            params[name] = value
            continue
        key = QUERY_PARAMS.get(name)
        if key is None:
            supported = ", ".join(list(_SPECIAL) + list(QUERY_PARAMS))
            raise MultiplexRequestError(400, f"unknown query parameter '{name}'. Supported: {supported}")
        if key in _NUMERIC:
            options[key] = _parse_number(name, value)
        elif key in _BOOLEAN:
            options[key] = _parse_bool(name, value)
        else:
            options[key] = value
        params[name] = value
    canonical = _canonical(options)
    if seed:
        return {"id": seed, "seed": seed, "options": options, "params": params, "canonical": canonical}
    if not options:
        return {"id": "default", "options": options, "params": params, "canonical": canonical}
    # Parameter-only identity: canonical form so ?a=1&b=2 and ?b=2&a=1 share one browser.
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return {"id": f"_p{digest}", "options": options, "params": params, "canonical": canonical}


def public_ws_base(headers, fallback_host):
    """Public ws(s):// base for URLs handed back, honouring a reverse proxy's forwarded headers.
    ``headers`` maps lower-cased names to values."""
    def first(name):
        v = headers.get(name)
        if isinstance(v, (list, tuple)):
            v = v[0] if v else None
        return v.split(",")[0].strip() if v else ""

    host = first("x-forwarded-host") or first("host") or fallback_host
    proto = first("x-forwarded-proto").lower()
    return f"{'wss' if proto in ('https', 'wss') else 'ws'}://{host}"


def _prefix(ident_id):
    return "" if ident_id == "default" else f"/fingerprint/{quote(ident_id, safe='')}"


def rewrite_ws_url(url, ws_base, ident_id):
    """Rewrite a child's ws://127.0.0.1:<port>/devtools/... URL to route through the multiplexer."""
    m = re.match(r"^wss?://[^/]+(/devtools/.*)$", url or "")
    if not m:
        return url
    return f"{ws_base}{_prefix(ident_id)}{m.group(1)}"


def _rewrite_frontend_url(url, host_part, prefix):
    """"/devtools/inspector.html?ws=127.0.0.1:<childport>/devtools/page/<id>" -> via the multiplexer.
    Only the first ``ws=``/``wss=``, as the Node SDK does. A function replacement: the forwarded host is
    data, never a pattern."""
    return re.sub(r"([?&]wss?=)[^/&]+(/devtools/)",
                  lambda m: f"{m.group(1)}{host_part}{prefix}{m.group(2)}", url, count=1)


def origin_allowed(origin, extra=()):
    """Origin allowed to talk to the endpoint: none (non-browser client), loopback, or listed."""
    if origin is None:
        return True
    if not origin or origin == "null":
        return False
    if origin in (extra or ()):
        return True
    try:
        h = (urlsplit(origin).hostname or "").strip("[]")
    except ValueError:
        return False
    return h in ("localhost", "127.0.0.1", "::1")


def host_allowed(host_header, extra=()):
    """Host header allowed: absent, an IP literal, "localhost", or listed (host part, any case).

    A browser page on an attacker's domain that re-resolves to 127.0.0.1 (DNS rebinding) still sends
    its own domain as Host, so only names that cannot be rebound are accepted -- the rule Chrome's
    DevTools HTTP server applies."""
    if host_header is None:
        return True
    h = host_header.strip()
    if not h:
        return False
    if h.startswith("["):
        name = h[1:h.find("]")] if "]" in h else h[1:]
    elif h.count(":") == 1:
        name = h.rsplit(":", 1)[0]
    else:
        name = h
    name = name.lower()
    if name == "localhost" or name in {e.lower() for e in (extra or ())}:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def request_forbidden(headers, allow_origins=(), allow_hosts=()):
    """Why a request must be refused (403), or None. ``headers`` maps lower-cased names to values."""
    site = (headers.get("sec-fetch-site") or "").strip().lower()
    origin = headers.get("origin")
    # An explicitly allowed Origin is a site the operator chose to let in, cross-site or not.
    if site in ("cross-site", "same-site") and not (origin and origin in (allow_origins or ())):
        return "Forbidden: cross-site request"
    if "origin" in headers and not origin_allowed(headers.get("origin"), allow_origins):
        return "Forbidden origin"
    if not host_allowed(headers.get("host"), allow_hosts):
        return "Forbidden host"
    return None


# A /json, /json/version or /json/list path appended to the query (see MultiplexServer._route).
_QUERY_TAIL_PATH = re.compile(r"^(.*?)(/json(?:/version|/list)?)/?$", re.S)


def _idle_timeout_from_env():
    try:
        n = float(str(os.environ.get("CLEARCOTE_SERVE_IDLE_TIMEOUT") or "").strip())
    except ValueError:
        return 0
    return n if math.isfinite(n) and n > 0 else 0


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Child:
    __slots__ = ("id", "seed", "params", "canonical", "srv", "connections", "started_at", "idle_since",
                 "idle_handle")

    def __init__(self, ident, srv):
        self.id = ident["id"]
        self.seed = ident.get("seed")
        self.params = ident["params"]
        self.canonical = ident.get("canonical", _canonical(ident.get("options") or {}))
        self.srv = srv
        self.connections = 0
        self.started_at = time.time()
        self.idle_since = None
        self.idle_handle = None


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class MultiplexServer:
    """Handle for a running multiplexer. Thread-safe; usable from sync code."""

    def __init__(self, port=9222, host="127.0.0.1", idle_timeout=None, data_dir=None, max_browsers=16,
                 allow_origins=None, allow_hosts=None, ready_timeout=30.0, start_browser=None,
                 quiet=False, **base):
        self.host = host
        self._requested_port = int(port or 0)
        self.port = None
        self.idle_timeout = float(idle_timeout) if idle_timeout is not None else _idle_timeout_from_env()
        self.data_dir = data_dir
        self.max_browsers = int(max_browsers)
        self.allow_origins = list(allow_origins or [])
        self.allow_hosts = list(allow_hosts or [])
        self.ready_timeout = ready_timeout
        self.quiet = quiet
        self._base = dict(base)
        self._base["quiet"] = quiet
        self._start_browser = start_browser
        self._children = {}
        self._pending = {}    # id -> (task, canonical): launches in flight
        self._closing_ids = {}  # id -> task: closes in flight (a relaunch waits for them)
        self._doomed = set()    # ids whose launch in flight is to be closed as soon as it finishes
        self._clients = set()
        self._relays = set()
        self._closing = False
        self._closed = threading.Event()
        self._loop = None
        self._server = None
        self._thread = None

    # -- lifecycle ------------------------------------------------------------------------------

    @property
    def url(self):
        h = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{h}:{self.port}"

    def start(self):
        ready = threading.Event()
        err = []

        def run():
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            try:
                self._server = loop.run_until_complete(asyncio.start_server(
                    self._handle, self.host, self._requested_port, limit=1 << 20))
                self.port = self._server.sockets[0].getsockname()[1]
            except BaseException as e:  # noqa: BLE001
                err.append(e)
                ready.set()
                loop.close()
                return
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        self._thread = threading.Thread(target=run, name="clearcote-multiplex", daemon=True)
        self._thread.start()
        ready.wait()
        if err:
            raise err[0]
        loopback = self.host in ("127.0.0.1", "localhost", "::1")
        if not loopback and not self.quiet:
            sys.stderr.write(
                f"[clearcote] WARNING: serve_multiplex is bound to {self.host}. Anyone who can reach "
                "this port can start browsers and control them. Keep it on 127.0.0.1 or put "
                "authentication in front of it.\n")
        atexit.register(self.close)
        if not self.quiet:
            sys.stderr.write(
                f"[clearcote] multiplexed CDP endpoint ready: {self.url}\n"
                f"            connect_over_cdp(\"{self.url}?fingerprint=<seed>\")  -  status: GET {self.url}/\n")
        return self

    def _call(self, coro, timeout=None):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def status(self):
        """Snapshot of the running browsers (the GET / body)."""
        return self._status()

    def close_identity(self, ident_id):
        """Close one identity's browser (waiting for a launch in flight). True when one was running."""
        if self._closed.is_set():
            return False
        return self._call(self._close_identity(ident_id))

    def close(self):
        """Stop the server and every browser, including launches still in flight."""
        if self._closing:
            self._closed.wait(120)
            return
        self._closing = True
        try:
            if self._loop is not None and self._loop.is_running():
                self._call(self._aclose(), timeout=180)
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(10)
        finally:
            self._closed.set()

    def serve_forever(self):
        """Block until :meth:`close` is called (from another thread or a signal handler)."""
        while not self._closed.wait(0.5):
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()

    # -- state (loop thread) --------------------------------------------------------------------

    def _status(self):
        procs = []
        for c in list(self._children.values()):
            p = {"id": c.id}
            if c.seed:
                p["seed"] = c.seed
            p.update({"pid": getattr(c.srv, "pid", None), "port": c.srv.port, "connections": c.connections,
                      "startedAt": _iso(c.started_at)})
            if c.idle_since:
                p["idleSince"] = _iso(c.idle_since)
            p["params"] = c.params
            procs.append(p)
        idle = self.idle_timeout
        return {"status": "ok", "active": len(procs), "maxBrowsers": self.max_browsers,
                "idleTimeoutSec": int(idle) if float(idle).is_integer() else idle, "processes": procs}

    def _schedule_idle(self, c):
        if c.idle_handle is not None:
            c.idle_handle.cancel()
            c.idle_handle = None
        if c.connections > 0:
            c.idle_since = None
            return
        c.idle_since = time.time()
        if self.idle_timeout > 0:
            loop = self._loop
            c.idle_handle = loop.call_later(
                self.idle_timeout, lambda: loop.create_task(self._close_identity(c.id)))

    async def _call_close(self, srv):
        close = srv.close
        if inspect.iscoroutinefunction(close):
            await close()
        else:
            await _maybe_await(await self._loop.run_in_executor(None, close))

    async def _start_child(self, ident):
        closing = self._closing_ids.get(ident["id"])
        if closing is not None:  # same profile dir: never start before the previous browser is gone
            await asyncio.gather(closing, return_exceptions=True)
        opts = dict(self._base)
        opts.update(ident["options"])
        opts.update({"host": "127.0.0.1", "port": None, "quiet": True, "ready_timeout": self.ready_timeout})
        # Profile directory named by a hash of the identity -- never by a request value.
        opts["user_data_dir"] = (
            os.path.join(self.data_dir, hashlib.sha256(f"clearcote:{ident['id']}".encode()).hexdigest()[:24])
            if self.data_dir else None)
        starter = self._start_browser
        if starter is None:
            from ._serve import serve

            def starter(o):
                return serve(**o)
        if inspect.iscoroutinefunction(starter):
            srv = await starter(opts)
        else:
            srv = await _maybe_await(await self._loop.run_in_executor(None, starter, opts))
        if self._closing:
            await self._call_close(srv)
            raise MultiplexRequestError(503, "shutting down")
        child = _Child(ident, srv)
        self._children[ident["id"]] = child
        self._schedule_idle(child)
        return child

    def _conflict(self, ident_id):
        return MultiplexRequestError(
            409, f"identity '{ident_id}' is already running with different parameters; close it first "
                 f"(POST /fingerprint/{quote(ident_id, safe='')}/close)")

    async def _ensure_child(self, ident):
        if self._closing:
            raise MultiplexRequestError(503, "shutting down")
        canonical = ident.get("canonical", _canonical(ident.get("options") or {}))
        existing = self._children.get(ident["id"])
        if existing:
            if existing.canonical != canonical:
                raise self._conflict(ident["id"])
            self._schedule_idle(existing)  # a fresh /json/version means a client is about to connect
            return existing
        inflight = self._pending.get(ident["id"])
        if inflight:
            task, inflight_canonical = inflight
            if inflight_canonical != canonical:
                raise self._conflict(ident["id"])
            return await asyncio.shield(task)
        if len(self._children) + len(self._pending) >= self.max_browsers:
            raise MultiplexRequestError(
                429, f"maxBrowsers ({self.max_browsers}) reached; close an identity first")
        task = self._loop.create_task(self._start_child(dict(ident, canonical=canonical)))
        self._pending[ident["id"]] = (task, canonical)

        def _done(t, key=ident["id"]):
            if self._pending.get(key, (None,))[0] is t:
                del self._pending[key]
            if not t.cancelled():
                t.exception()  # retrieve, so a failed launch nobody awaits is not logged as unhandled

        task.add_done_callback(_done)
        return await asyncio.shield(task)

    async def _close_identity(self, ident_id):
        inflight = self._pending.get(ident_id)
        if inflight:  # closing a launching id: let the launch finish, then close what it started
            self._doomed.add(ident_id)  # requests waiting on that launch must not use the browser
            try:
                await asyncio.gather(inflight[0], return_exceptions=True)
                return await self._close_identity(ident_id)
            finally:
                self._doomed.discard(ident_id)
        closing = self._closing_ids.get(ident_id)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
            return False
        c = self._children.pop(ident_id, None)
        if c is None:
            return False
        if c.idle_handle is not None:
            c.idle_handle.cancel()
        task = self._loop.create_task(self._call_close(c.srv))
        self._closing_ids[ident_id] = task
        try:
            await task
        finally:
            if self._closing_ids.get(ident_id) is task:
                del self._closing_ids[ident_id]
        return True

    async def _aclose(self):
        self._server.close()
        # server.close() leaves accepted connections open: keep-alive clients, requests waiting on a
        # launch and relayed CDP sessions would hold them indefinitely. Shutting down means ending
        # them, immediately (abort, not a graceful close the stopping loop might never flush).
        for w in list(self._clients) + list(self._relays):
            try:
                w.transport.abort()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(0)
        try:
            await asyncio.wait_for(self._server.wait_closed(), 5)
        except Exception:  # noqa: BLE001
            pass
        # Launches in flight close their own browser once they see _closing (see _start_child).
        await asyncio.gather(*(t for t, _c in list(self._pending.values())), return_exceptions=True)
        await asyncio.gather(*(self._close_identity(i) for i in list(self._children)),
                             return_exceptions=True)
        await asyncio.gather(*list(self._closing_ids.values()), return_exceptions=True)

    # -- HTTP ------------------------------------------------------------------------------------

    async def _handle(self, reader, writer):
        self._clients.add(writer)
        relayed = False
        try:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                return
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ")
            if len(parts) < 3:
                return
            method, target = parts[0], parts[1]
            raw_headers = []
            headers = {}
            for line in lines[1:]:
                if not line:
                    continue
                k, sep, v = line.partition(":")
                if not sep:
                    continue
                raw_headers.append((k.strip(), v.strip()))
                headers.setdefault(k.strip().lower(), v.strip())
            if "websocket" in headers.get("upgrade", "").lower():
                self._clients.discard(writer)
                relayed = True
                await self._upgrade(reader, writer, target, raw_headers, headers)
                return
            length = int(headers.get("content-length") or 0)
            if length > 0:
                await reader.readexactly(min(length, 1 << 20))
            forbidden = request_forbidden(headers, self.allow_origins, self.allow_hosts)
            if forbidden:
                code, body = 403, {"error": forbidden}
            else:
                code, body = await self._route(method, target, headers)
            data = json.dumps(body).encode("utf-8")
            try:
                phrase = HTTPStatus(code).phrase
            except ValueError:
                phrase = ""
            writer.write(
                f"HTTP/1.1 {code} {phrase}\r\nContent-Type: application/json; charset=utf-8\r\n"
                f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode("latin-1") + data)
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._clients.discard(writer)
            if not relayed:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _route(self, method, target, headers):
        try:
            u = urlsplit(target)
            path = u.path.rstrip("/") or "/"
            query = u.query
            # Playwright clients up to 1.49 build the discovery URL by appending "/json/version/" to the
            # endpoint string, so connect_over_cdp("http://host:port?fingerprint=a") asks for
            # GET /?fingerprint=a/json/version/: the path lands at the end of the query. (Newer releases
            # append it to the URL's path.) A seed cannot contain '/', so nothing else matches.
            tail = _QUERY_TAIL_PATH.match(query) if path == "/" else None
            if tail:
                query, path = tail.group(1), tail.group(2)
            ws_base = public_ws_base(headers, f"{self.host}:{self.port}")
            if method == "GET" and path == "/":
                return 200, self._status()
            m = re.match(r"^/fingerprint/([^/]+)/close$", path)
            if m:
                if method != "POST":
                    return 405, {"error": "use POST"}
                ident_id = unquote(m.group(1))
                return 200, {"id": ident_id, "terminated": await self._close_identity(ident_id)}
            if method == "GET" and path in ("/json/version", "/json/list", "/json"):
                ident = parse_connection_identity(query)
                child = await self._ensure_child(ident)
                if self._children.get(child.id) is not child or child.id in self._doomed:
                    raise MultiplexRequestError(503, f"identity '{child.id}' was closed while starting")
                data = await self._child_json(child.srv.port, "/json/list" if path == "/json" else path)
                host_part = re.sub(r"^wss?://", "", ws_base)
                prefix = _prefix(child.id)

                def fix(o):
                    if isinstance(o, dict):
                        if isinstance(o.get("webSocketDebuggerUrl"), str):
                            o["webSocketDebuggerUrl"] = rewrite_ws_url(o["webSocketDebuggerUrl"], ws_base, child.id)
                        if isinstance(o.get("devtoolsFrontendUrl"), str):
                            o["devtoolsFrontendUrl"] = _rewrite_frontend_url(o["devtoolsFrontendUrl"], host_part, prefix)
                    return o

                return 200, ([fix(o) for o in data] if isinstance(data, list) else fix(data))
            return 404, {"error": "not found"}
        except MultiplexRequestError as e:
            return e.status, {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e) or type(e).__name__}

    async def _child_json(self, port, path):
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 10)
        try:
            w.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n".encode())
            await w.drain()
            # Chrome's DevTools HTTP server keeps the connection open after answering, even with
            # "Connection: close": read the head, then exactly Content-Length bytes (never to EOF).
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 15)
            length = None
            for line in head.decode("latin-1").split("\r\n")[1:]:
                k, _sep, v = line.partition(":")
                if k.strip().lower() == "content-length":
                    length = int(v.strip())
            body = (await asyncio.wait_for(r.readexactly(length), 15) if length is not None
                    else await asyncio.wait_for(r.read(), 15))
        finally:
            w.close()
        return _parse_response(head + body).json()

    # -- WebSocket relay ------------------------------------------------------------------------

    async def _upgrade(self, reader, writer, target, raw_headers, headers):
        async def reject(code, msg):
            msg = msg.replace("\r", " ").replace("\n", " ")
            try:
                writer.write(f"HTTP/1.1 {code} {msg}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n{msg}\n"
                             .encode("latin-1", "replace"))
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            writer.close()

        forbidden = request_forbidden(headers, self.allow_origins, self.allow_hosts)
        if forbidden:
            return await reject(403, forbidden)
        u = urlsplit(target)
        ident_id, rest = "default", u.path
        m = re.match(r"^/fingerprint/([^/]+)(/devtools/.*)$", u.path)
        if m:
            ident_id, rest = unquote(m.group(1)), m.group(2)
        elif not u.path.startswith("/devtools/"):
            return await reject(404, "Not Found")
        # The WebSocket route never starts a browser: identities (and their parameters) are chosen
        # on the HTTP /json/* routes, where they can be validated and conflicts refused.
        child = self._children.get(ident_id)
        if child is None:
            host = headers.get("host") or f"{self.host}:{self.port}"
            return await reject(404, f"identity not running; connect via http://{host}/?fingerprint=...")
        try:
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", child.srv.port), 10)
        except Exception:  # noqa: BLE001
            return await reject(502, "Browser unreachable")
        if reader.at_eof() or writer.is_closing():  # the client left while we connected upstream
            up_w.close()
            writer.close()
            return

        head = f"GET {rest}{('?' + u.query) if u.query else ''} HTTP/1.1\r\n"
        for k, v in raw_headers:
            # The origin was checked above; the browser's own --remote-allow-origins check would
            # reject a forwarded browser Origin, and Host must name the child.
            if k.lower() in ("host", "origin"):
                continue
            head += f"{k}: {v}\r\n"
        head += f"Host: 127.0.0.1:{child.srv.port}\r\n\r\n"
        # From here on the two streams are relayed: nothing but upstream bytes is ever written to
        # the client (no HTTP error can be injected into a WebSocket stream).
        up_w.write(head.encode("latin-1"))
        child.connections += 1
        self._schedule_idle(child)
        self._relays.update((writer, up_w))

        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:  # noqa: BLE001
                pass

        tasks = [self._loop.create_task(pump(reader, up_w)), self._loop.create_task(pump(up_r, writer))]
        try:
            # Tear down BOTH sides as soon as EITHER ends, or a finished CDP session holds the relay
            # open forever: its connection never counts as gone and idle close never runs.
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            for w in (writer, up_w):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
                self._relays.discard(w)
            child.connections = max(0, child.connections - 1)
            if self._children.get(child.id) is child:
                self._schedule_idle(child)


def serve_multiplex(port=9222, host="127.0.0.1", idle_timeout=None, data_dir=None, max_browsers=16,
                    allow_origins=None, allow_hosts=None, ready_timeout=30.0, start_browser=None,
                    quiet=False, **base):
    """Start a multiplexing CDP endpoint and return a running :class:`MultiplexServer`.

    ``port``           port for the multiplexer (default 9222; 0 = ephemeral).
    ``host``           bind address (default 127.0.0.1).
    ``idle_timeout``   close an identity's browser this many seconds after its last WebSocket
                       disconnects (0 = never, the default; also CLEARCOTE_SERVE_IDLE_TIMEOUT).
    ``data_dir``       keep each identity's profile under this directory (sub-directory named by a
                       hash of the identity). Default: a temporary profile per browser.
    ``max_browsers``   maximum concurrent browsers (default 16); further identities get HTTP 429.
    ``allow_origins``  extra browser Origins (exact) allowed to use the endpoint.
    ``allow_hosts``    extra Host names (besides IP literals and localhost) the endpoint answers,
                       e.g. the public name a reverse proxy forwards unchanged.
    ``ready_timeout``  per-browser startup timeout in seconds.
    ``start_browser``  how a browser is started for an identity (default :func:`clearcote.serve`);
                       returns an object with ``port``, ``pid`` and ``close()``. Sync or async.
    All other kwargs (headless, executable_path, fingerprint defaults, proxy, license_key, ...) are
    the base ``serve()`` options every identity starts from; per-connection parameters override them.
    """
    return MultiplexServer(port=port, host=host, idle_timeout=idle_timeout, data_dir=data_dir,
                           max_browsers=max_browsers, allow_origins=allow_origins,
                           allow_hosts=allow_hosts, ready_timeout=ready_timeout,
                           start_browser=start_browser, quiet=quiet, **base).start()
