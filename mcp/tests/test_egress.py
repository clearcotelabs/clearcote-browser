"""The private-address guard: a url is judged the way the browser will read it (WHATWG URL rules), only http and https
pass, every address a host stands for is checked against the full list of non-public ranges, and the same check runs
on every request the browser makes (tests/test_browser_e2e.py proves that part on a real browser). No network: name
resolution is faked."""
import socket

import pytest

from clearcote_mcp import server as S

try:
    from clearcote_mcp import _egress
except ImportError:  # before the guard had its own module
    _egress = None

P = 8765
RESOLVES = {"example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
            "inside.example": ["10.0.0.7"], "mixed.example": ["93.184.215.14", "192.168.0.9"]}


@pytest.fixture(autouse=True)
def offline_dns(monkeypatch):
    monkeypatch.delenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", raising=False)

    def fake(host, port, *args, **kwargs):
        if host not in RESOLVES:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
                for a in RESOLVES[host]]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


REFUSED = [
    f"http://2130706433:{P}/",               # 127.0.0.1 as one number
    f"http://0x7f.1:{P}/", f"http://0177.0.0.1:{P}/", f"http://127.1:{P}/", "http://0x7f000001/", "http://0/",
    f"http:127.0.0.1:{P}/", f"http:/127.0.0.1:{P}/", f"http:\\\\127.0.0.1:{P}/", f"HTTP://127.0.0.1:{P}/",
    f"  http://127.0.0.1:{P}/", f"ht\ttp://127.0.0.1:{P}/", f"http://127.0.0.1\n:{P}/",
    f"http://127.0.0.1:{P}\\@example.com/",  # the browser ends the host at the backslash
    f"http://user:pw@127.0.0.1:{P}/", f"http://example.com@127.0.0.1:{P}/",
    f"http://%31%32%37.0.0.1:{P}/",          # a percent-encoded host
    "http://\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11/",  # fullwidth digits and dots
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/", "http://[0:0:0:0:0:ffff:a00:1]/",
    "http://[64:ff9b::a9fe:a9fe]/", "http://[2002:7f00:1::]/", "http://[fd00::1]/", "http://[fe80::1]/",
    "http://[::]/", "http://[ff02::1]/",
    "http://100.100.100.200/", "http://100.64.0.1/", "http://169.254.169.254/", "http://0.0.0.0/",
    "http://10.0.0.1/", "http://172.16.5.4/", "http://192.168.1.1/", "http://224.0.0.1/", "http://255.255.255.255/",
    "http://198.18.0.1/",
    "http://localhost/", "http://LOCALHOST./", "http://app.localhost/", "http://metadata.google.internal./",
    "http://metadata.google.internal/", "http://metadata/",
    "http://inside.example/", "http://mixed.example/",  # a name for a private address, or for one among others
    "view-source:http://127.0.0.1/", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,hi",
    "ftp://127.0.0.1/", "ws://127.0.0.1/", "chrome://settings", "about:blank", "http://[::1", "http:",
]
ALLOWED = ["https://example.com/", "HTTPS://EXAMPLE.COM:8443/a?b=1#c", "http://8.8.8.8/",
           "http://[2606:4700:4700::1111]/", "http://[::ffff:8.8.8.8]/", "http://1.1.1.1:80/",
           "https://not-resolvable.example/"]   # does not resolve here: the browser will say so


@pytest.mark.asyncio
@pytest.mark.parametrize("url", REFUSED)
async def test_refused(url):
    with pytest.raises(ValueError, match="refused"):
        await S._check_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ALLOWED)
async def test_allowed(url):
    await S._check_url(url)


@pytest.mark.asyncio
async def test_opting_in_allows_private_addresses(monkeypatch):
    monkeypatch.setenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", "1")
    for url in (f"http://2130706433:{P}/", "http://localhost/", "http://inside.example/", "http://[::1]:8080/"):
        await S._check_url(url)


NOT_WEB = ["file:///tmp/page.html", "file:///C:/Windows/win.ini", "FILE:///etc/passwd", " file:/etc/passwd",
           "chrome://settings", "chrome://version", "chrome-extension://abcdefghijklmnopabcdefghijklmnop/page.html",
           f"view-source:http://127.0.0.1:{P}/", "devtools://devtools/bundled/inspector.html",
           f"filesystem:http://127.0.0.1:{P}/temporary/a.html", "javascript:alert(1)", "data:text/html,<p>hi",
           "about:blank", "about:version", f"blob:http://127.0.0.1:{P}/0a1b", "ftp://127.0.0.1/"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", NOT_WEB)
@pytest.mark.parametrize("opt_in", [False, True])
async def test_only_web_urls_are_opened_even_with_private_addresses_allowed(monkeypatch, url, opt_in):
    if opt_in:
        monkeypatch.setenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", "1")
    with pytest.raises(ValueError, match=r"^refused url scheme '[a-z-]+': only http and https urls are opened$"):
        await S._check_url(url)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["file:///etc/passwd", "chrome://settings", "view-source:http://127.0.0.1/"])
async def test_a_tool_refuses_a_local_scheme_before_using_the_browser(monkeypatch, url):
    monkeypatch.setenv("CLEARCOTE_ALLOW_PRIVATE_EGRESS", "1")

    async def no_browser():
        raise AssertionError("the browser was used")
    monkeypatch.setattr(S, "_b", no_browser)
    for answer in (await S.navigate(url), await S.read_page(url), await S.new_tab(url), await S.evaluate_js("1", url)):
        assert answer["status"] == "error" and answer["error"].startswith("ValueError: refused url scheme '"), answer


@pytest.mark.parametrize("ip,blocked", [
    ("100.100.100.200", True), ("100.64.0.0", True), ("100.127.255.255", True), ("100.128.0.1", False),
    ("192.0.0.8", True), ("198.19.255.255", True), ("240.0.0.1", True), ("::", True), ("fec0::1", True),
    ("2001:db8::1", True), ("::ffff:10.0.0.1", True), ("::ffff:8.8.8.8", False), ("8.8.8.8", False),
    ("2606:4700:4700::1111", False)])
def test_ip_ranges(ip, blocked):
    assert S._ip_blocked(ip) is blocked


def test_the_guard_has_its_own_module():
    assert _egress is not None, "the url guard moved to clearcote_mcp._egress"


class Route:
    def __init__(self, url):
        self.request = type("R", (), {"url": url})()
        self.done = None

    async def abort(self, code=None):
        self.done = ("abort", code)

    async def fallback(self):
        self.done = ("fallback", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("url,verdict", [
    (f"http://127.0.0.1:{P}/redirected", "abort"), ("http://[::1]/x", "abort"), ("http://inside.example/a.png", "abort"),
    ("http://100.100.100.200/latest", "abort"), ("https://example.com/app.js", "fallback"),
    ("data:image/png;base64,AAAA", "fallback"), ("blob:https://example.com/1", "fallback")])
async def test_every_request_goes_through_the_guard(url, verdict):
    assert _egress is not None
    route = Route(url)
    await _egress.guard_route(route)
    assert route.done[0] == verdict


@pytest.mark.asyncio
@pytest.mark.parametrize("url,verdict", [
    ("file:///C:/Windows/win.ini", "abort"), ("file:///etc/passwd", "abort"),
    ("devtools://devtools/bundled/inspector.html", "abort"), ("filesystem:https://example.com/temporary/a", "abort"),
    ("view-source:https://example.com/", "abort"), ("about:version", "abort"), ("ftp://example.com/", "abort"),
    ("chrome-error://chromewebdata/", "abort"),
    # what pages and new tabs are made of, and never leaves the browser
    ("about:blank", "fallback"), ("about:blank#top", "fallback"), ("about:srcdoc", "fallback"),
    ("data:text/html,<p>hi", "fallback"), ("blob:https://example.com/0a1b-2c3d", "fallback"),
    ("https://example.com/app.js", "fallback"),
    # the browser's own resources: its PDF viewer is an extension that loads these (a web page cannot)
    ("chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/main.js", "fallback"),
    ("chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.css", "fallback"),
    ("chrome://resources/css/text_defaults_md.css", "fallback"), ("chrome://version/", "fallback")])
async def test_only_web_requests_leave_the_browser(url, verdict):
    route = Route(url)
    await _egress.guard_route(route)
    assert route.done[0] == verdict


class CDP:
    def __init__(self):
        self.sent, self.handlers = [], {}

    def on(self, event, handler):
        self.handlers[event] = handler

    async def send(self, method, params=None):
        self.sent.append((method, params))


class Context:
    def __init__(self, pages=()):
        self.pages, self.routes, self.events, self.sessions = list(pages), [], {}, []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def on(self, event, handler):
        self.events[event] = handler

    async def new_cdp_session(self, page):
        self.sessions.append(CDP())
        return self.sessions[-1]


@pytest.mark.asyncio
async def test_the_guard_is_installed_on_the_whole_context():
    assert _egress is not None
    page = type("Page", (), {})()
    ctx = Context([page])
    await _egress.guard_context(ctx)
    assert ctx.routes and ctx.routes[0][0] == "**/*" and "page" in ctx.events  # later pages are guarded too
    assert ("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]}) in ctx.sessions[0].sent
    await _egress.guard_redirects(ctx, page)
    assert len(ctx.sessions) == 1  # once per page


@pytest.mark.asyncio
@pytest.mark.parametrize("status,location,verdict", [
    (302, f"http://127.0.0.1:{P}/landing", "Fetch.failRequest"), (301, f"http:127.0.0.1:{P}/", "Fetch.failRequest"),
    (307, "//169.254.169.254/latest", "Fetch.failRequest"), (302, "http://inside.example/", "Fetch.failRequest"),
    (302, "https://example.com/next", "Fetch.continueRequest"), (302, "/same/host", "Fetch.continueRequest"),
    (302, "chrome://settings", "Fetch.failRequest"), (301, "devtools://devtools/x.html", "Fetch.failRequest"),
    (307, "filesystem:https://example.com/temporary/a", "Fetch.failRequest"),
    (302, "chrome-extension://mhjfbmdgcfjbbpaeojofohoefgiehjai/index.html", "Fetch.failRequest"),
    (302, "data:text/html,<p>hi", "Fetch.failRequest"),  # a web server's Location: web urls only
    (200, None, "Fetch.continueRequest"), (304, None, "Fetch.continueRequest")])
async def test_a_redirect_to_a_private_address_is_failed(status, location, verdict):
    import asyncio
    assert _egress is not None
    ctx = Context()
    await _egress.guard_redirects(ctx, type("Page", (), {})())
    cdp = ctx.sessions[0]
    headers = [{"name": "Location", "value": location}] if location else []
    cdp.handlers["Fetch.requestPaused"]({"requestId": "r1", "request": {"url": "https://example.com/start"},
                                         "responseStatusCode": status, "responseHeaders": headers})
    for _ in range(20):
        await asyncio.sleep(0.01)
    assert cdp.sent[-1][0] == verdict and cdp.sent[-1][1]["requestId"] == "r1"
