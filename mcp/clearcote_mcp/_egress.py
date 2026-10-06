"""Where the browser may go: not to this machine, the local network or a cloud metadata endpoint.

Two layers, one rule:

* check_url() judges a url a tool is given, read the way the browser will read it (WHATWG URL rules: tabs and
  newlines dropped, backslashes and missing slashes after http:, userinfo, percent-encoded and full-width hosts, IPv4
  written as one number or in hex or octal or with parts left out, IPv6 with an IPv4 address inside). Only http and
  https pass.
* guard_context() puts the same check on every request the browser makes in the context (navigations, images,
  frames, scripts' requests, popups), on the url as the browser itself has canonicalised it, and on every redirect
  a page follows (Playwright continues a redirect's next request without asking its routes, so each page also gets a
  CDP session that holds 3xx answers until their Location is judged).

An address is refused when it is in a non-public range (BLOCKED_V4 / BLOCKED_V6, IPv4 inside IPv6 checked as IPv4),
when its name is localhost or a metadata name, or when the name resolves to any such address.

What it cannot close:
* DNS rebinding: a name is resolved here and again by the browser a moment later; a name that answers with a public
  address first and a private one second still gets through. Verdicts are not cached, which keeps the window small,
  not closed.
* WebSockets: Playwright can only route them by replacing the page's WebSocket object in JavaScript, which this
  browser does not do; a page script can still open a ws:// connection to a private address.
* With a proxy, the proxy resolves names: a name that resolves differently there is judged by the local answer.
* A popup (or any page the site opens) gets its redirect check when it appears, so a redirect in its very first load
  can come before the check is in place; the page's first request itself is always checked. Redirects of requests
  made inside workers or cross-site frames are not held either.
Routing every request also makes Playwright turn the browser's HTTP cache off for the context, and each response is
held for a moment until it is judged.

CLEARCOTE_ALLOW_PRIVATE_EGRESS=1 turns all of it off (local development, file: pages).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
from urllib.parse import unquote_to_bytes

log = logging.getLogger("clearcote-mcp")
OPT_IN = "CLEARCOTE_ALLOW_PRIVATE_EGRESS"
ALLOWED_SCHEMES = ("http", "https")
# Schemes of requests that never leave the browser.
LOCAL_SCHEMES = ("data", "blob", "about", "chrome", "chrome-extension", "devtools", "filesystem")

BLOCKED_V4 = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24",
    "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4")]
BLOCKED_V6 = [ipaddress.ip_network(n) for n in (
    "::/96",            # unspecified, loopback, IPv4-compatible
    "64:ff9b:1::/48", "100::/64", "2001::/23", "2001:db8::/32", "fc00::/7", "fe80::/10", "fec0::/10", "ff00::/8")]
BLOCKED_NAMES = ("localhost", "metadata", "metadata.google.internal")


def private_allowed() -> bool:
    return os.environ.get(OPT_IN, "0") == "1"


def ip_blocked(text: str) -> bool:
    """True for an address outside the public internet (IPv4 inside IPv6 is judged as the IPv4 address)."""
    try:
        ip = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6:
        packed = int(ip)
        if ip.ipv4_mapped is not None:                                       # ::ffff:a.b.c.d
            return ip_blocked(str(ip.ipv4_mapped))
        if ip in ipaddress.ip_network("64:ff9b::/96"):                      # NAT64
            return ip_blocked(str(ipaddress.IPv4Address(packed & 0xFFFFFFFF)))
        if ip.sixtofour is not None:                                         # 2002:a.b.c.d::/48
            return ip_blocked(str(ip.sixtofour))
        return any(ip in net for net in BLOCKED_V6)
    return any(ip in net for net in BLOCKED_V4)


# ── reading a url the way the browser does ───────────────────────────────────
_TAB_NL = re.compile(r"[\t\n\r]")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):")


def _ipv4_number(part: str) -> int | None:
    if part[:2] in ("0x", "0X"):
        digits, base = part[2:], 16
    elif len(part) > 1 and part[0] == "0":
        digits, base = part[1:], 8
    else:
        digits, base = part, 10
    if digits == "":
        return 0
    try:
        return int(digits, base)
    except ValueError:
        return None


def _ipv4(host: str) -> str | None:
    """The WHATWG IPv4 parser: '2130706433', '0x7f.1', '0177.0.0.1', '127.1' -> '127.0.0.1'; None if not IPv4."""
    parts = host.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    last = parts[-1]
    if not (last.isdigit() or (last[:2].lower() == "0x" and all(c in "0123456789abcdefABCDEF" for c in last[2:]))):
        return None  # does not end in a number: a domain
    if len(parts) > 4 or "" in parts:
        raise ValueError(f"invalid IPv4 host {host!r}")
    numbers = [_ipv4_number(p) for p in parts]
    if None in numbers or any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise ValueError(f"invalid IPv4 host {host!r}")
    value = numbers[-1]
    for i, n in enumerate(numbers[:-1]):
        value += n * 256 ** (3 - i)
    return str(ipaddress.IPv4Address(value))


def url_scheme_and_host(url: str) -> tuple[str, str]:
    """(scheme, host) of `url` as the browser parses it; host is an IP address or a lower-case domain without a
    trailing dot. Raises ValueError for a url the browser would not open."""
    url = _TAB_NL.sub("", url).strip("\x00\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x10\x11\x12\x13\x14\x15"
                                       "\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f ")
    m = _SCHEME.match(url)
    if not m:
        raise ValueError(f"not an absolute url: {url[:80]!r}")
    scheme = m.group(1).lower()
    if scheme not in ALLOWED_SCHEMES:
        return scheme, ""
    rest = url[m.end():].lstrip("/\\")                      # http:, http:/, http:\\ all read as http://
    authority = re.split(r"[/\\?#]", rest, maxsplit=1)[0]
    hostport = authority.rpartition("@")[2]
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0:
            raise ValueError(f"invalid IPv6 host in {url[:80]!r}")
        return scheme, str(ipaddress.IPv6Address(hostport[1:end]))
    host = hostport.rsplit(":", 1)[0] if ":" in hostport else hostport
    host = unquote_to_bytes(host).decode("utf-8", "replace")
    if not host:
        raise ValueError(f"no host in {url[:80]!r}")
    try:
        host = host.encode("idna").decode("ascii")          # full-width digits and dots fold to ASCII, as UTS 46
    except UnicodeError:
        host = host.lower()
    host = host.lower()
    return scheme, _ipv4(host) or host.rstrip(".")


async def _resolve(host: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().run_in_executor(
            None, lambda: socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP))
    except (socket.gaierror, UnicodeError, OSError):
        return []  # does not resolve here: the browser will fail too (see the module notes on proxies)
    return [info[4][0] for info in infos]


async def host_refusal(host: str) -> str | None:
    if host in BLOCKED_NAMES or host.endswith(".localhost"):
        return f"refused private/metadata host {host!r} (set {OPT_IN}=1 to allow)"
    try:
        ipaddress.ip_address(host)
    except ValueError:
        for address in await _resolve(host):
            if ip_blocked(address):
                return f"refused private/internal address for {host!r} (set {OPT_IN}=1 to allow)"
        return None
    if ip_blocked(host):
        return f"refused private/internal address {host!r} (set {OPT_IN}=1 to allow)"
    return None


async def check_url(url: str | None) -> None:
    """Refuse a url a tool was given that is not http(s) or points at a private address (see the module notes)."""
    if url is None or private_allowed():
        return
    try:
        scheme, host = url_scheme_and_host(url)
    except ValueError as exc:
        raise ValueError(f"refused url: {exc}") from None
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError(f"refused url scheme {scheme!r}: only http and https (set {OPT_IN}=1 to allow others)")
    reason = await host_refusal(host)
    if reason:
        raise ValueError(reason)


async def request_refusal(url: str) -> str | None:
    """Why the browser must not make this request (a url it has canonicalised itself), or None."""
    scheme = url.split(":", 1)[0].lower()
    if scheme in LOCAL_SCHEMES:
        return None
    if scheme not in ALLOWED_SCHEMES + ("ws", "wss", "ftp"):
        return f"refused url scheme {scheme!r}"
    try:
        _, host = url_scheme_and_host("http" + url[len(scheme):] if scheme not in ALLOWED_SCHEMES else url)
    except ValueError as exc:
        return f"refused url: {exc}"
    return await host_refusal(host)


async def guard_route(route) -> None:
    """Playwright route handler: abort a request to a private address, let every other one through."""
    url = route.request.url
    try:
        reason = await request_refusal(url)
    except Exception as exc:  # noqa: BLE001 -- fail closed
        reason = f"could not check the address: {exc}"
    if reason:
        log.warning("blocked a browser request to %s: %s", url[:200], reason)
        refused.append((url, reason))
        del refused[:-20]
        await route.abort("blockedbyclient")
    else:
        await route.fallback()


refused: list[tuple[str, str]] = []  # the latest refusals, so a failed navigation can say why


def _redirect_target(base: str, location: str) -> str:
    """Where a Location header sends the browser, enough to judge its host (an absolute url as is, including the
    'http:host' forms the browser accepts; a path stays on the same host)."""
    location = _TAB_NL.sub("", location).strip()
    if _SCHEME.match(location):
        return location
    if location[:2] in ("//", "\\\\", "/\\", "\\/"):
        return base.split(":", 1)[0] + ":" + location
    return base


async def guard_redirects(context, page) -> None:
    """Fail a redirect that leads to a private address, for requests `page` makes. Playwright continues a redirect's
    next request without asking its routes, so the 3xx answer is checked here instead, from its own CDP session that
    holds each response until it is judged. Returns once that is in place; every caller for the same page waits for
    the same setup (the context's "page" event and the code that opened the tab both call this)."""
    setup = getattr(page, "_cc_redirect_guard", None)
    if setup is None:
        setup = page._cc_redirect_guard = asyncio.ensure_future(_hold_redirects(context, page))
    await setup


async def _hold_redirects(context, page) -> None:
    cdp = await context.new_cdp_session(page)

    async def decide(event):
        request_id, reason, target = event["requestId"], None, None
        try:
            status = event.get("responseStatusCode") or 0
            location = next((h["value"] for h in event.get("responseHeaders") or []
                             if h["name"].lower() == "location"), None)
            if 300 <= status < 400 and location:
                target = _redirect_target(event["request"]["url"], location)
                reason = await request_refusal(target)
        except Exception as exc:  # noqa: BLE001 -- fail closed
            reason = f"could not check a redirect: {exc}"
        try:
            if reason:
                log.warning("blocked a redirect to %s: %s", (target or "?")[:200], reason)
                refused.append((target or event["request"]["url"], reason))
                del refused[:-20]
                await cdp.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"})
            else:
                await cdp.send("Fetch.continueRequest", {"requestId": request_id})
        except Exception:  # noqa: BLE001 -- the page or the request is gone
            pass

    cdp.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(decide(event)))
    await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})


async def guard_context(context) -> None:
    """Check every request the browser makes in `context`, and every redirect its pages follow (see the module notes
    for what this costs and misses). A page opened later is guarded when it appears: call guard_redirects() before a
    tab you open yourself navigates, so its first redirect is covered too."""
    await context.route("**/*", guard_route)
    for page in context.pages:
        await guard_redirects(context, page)
    context.on("page", lambda page: asyncio.ensure_future(guard_redirects(context, page)))
