"""launch(cloud=True) end to end against a REAL browser: the fake hosted API answers the create
with the CDP WebSocket of a local Chromium, so the SDK's connect, humanize install, close and
DELETE run for real. Profile sync reads cookies off the same browser over CDP.

Needs a Chromium: CLEARCOTE_TEST_BINARY, or one Playwright has downloaded. Skips without one.
Mirrors sdk/node/test/cloud-launch.test.ts."""
import json
import time
import urllib.request

import clearcote
import pytest
from _chromium import LocalChromium, find_chromium
from _fake_cloud import API_KEY, start_fake_cloud, stop_fake_cloud
from clearcote import async_api
from clearcote._cdpws import CdpConnection
from clearcote.cloud import Cloud

pytestmark = pytest.mark.skipif(not find_chromium(), reason="no Chromium (set CLEARCOTE_TEST_BINARY)")


@pytest.fixture
def chromium():
    with LocalChromium() as c:
        yield c


@pytest.fixture(autouse=True)
def _stop_sync_driver():
    """The sync launch shares one Playwright driver per process, and while it runs it holds the
    thread's event loop, which an async test after it then cannot use. Stop it after each test."""
    yield
    if clearcote._pw is not None:
        clearcote._pw.stop()
        clearcote._pw = None


@pytest.fixture
def api(monkeypatch, chromium):
    a = start_fake_cloud()
    a.connect_url = chromium.ws_url
    monkeypatch.setenv("CLEARCOTE_API_KEY", API_KEY)
    monkeypatch.setenv("CLEARCOTE_API_URL", a.url)
    monkeypatch.delenv("CLEARCOTE_CLOUD", raising=False)
    yield a
    stop_fake_cloud(a)


BUTTON = "<title>t</title><button id=b onclick=\"document.title='clicked'\">go</button>"


def test_cloud_launch_is_a_working_humanized_browser(api, chromium):
    browser = clearcote.launch(cloud=True, humanize=True, country="us", identity="acct-1")
    try:
        assert browser.is_connected()
        assert type(browser).__name__ == "Browser"  # the same Playwright type a local launch returns
        assert api.requests("POST", "/api/v1/browsers")[0]["body"] == {"country": "us", "identity": "acct-1"}
        assert browser.cloud_session["id"] == "bs_1"
        # the session's own context: its open tab and any new one are humanized
        ctx = browser.contexts[0]
        first = ctx.pages[0] if ctx.pages else ctx.new_page()
        assert getattr(first, "_clearcote_persona", None) is not None
        tab = ctx.new_page()
        assert getattr(tab, "_clearcote_persona", None) is not None
        # a page from browser.new_page() (a new context) is humanized too, and input works over CDP
        page = browser.new_page()
        assert getattr(page, "_clearcote_persona", None) is not None
        page.set_content(BUTTON)
        page.click("#b")
        assert page.title() == "clicked"
    finally:
        browser.close()
    assert not browser.is_connected()
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")
    assert chromium.alive()  # close() only disconnects: ending the browser is the gateway's job


def test_cloud_launch_with_cloud_env(api, chromium, monkeypatch):
    monkeypatch.setenv("CLEARCOTE_CLOUD", "true")
    browser = clearcote.launch(note="from env")
    try:
        assert browser.is_connected() and browser.version
    finally:
        browser.close()
    assert api.requests("POST", "/api/v1/browsers")[0]["body"] == {"note": "from env"}


def test_cloud_persistent_context(api, chromium):
    ctx = clearcote.launch_persistent_context(cloud=True, profile="acct-1", humanize=True)
    assert api.requests("POST", "/api/v1/browsers")[0]["body"]["profile"] == {"name": "acct-1", "persist": True}
    page = ctx.new_page()
    assert getattr(page, "_clearcote_persona", None) is not None
    page.set_content(BUTTON)
    page.click("#b")
    assert page.title() == "clicked"
    ctx.close()
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")


async def test_async_cloud_launch_is_a_working_humanized_browser(api, chromium):
    browser = await async_api.launch(cloud=True, humanize=True)
    try:
        page = await browser.new_page()
        assert getattr(page, "_clearcote_persona", None) is not None
        await page.set_content(BUTTON)
        await page.click("#b")
        assert await page.title() == "clicked"
    finally:
        await browser.close()
    assert api.requests("DELETE", "/api/v1/browsers/bs_1")


# ── profile sync off a real browser ──────────────────────────────────────────────────────────────

def _set_cookies(ws_url, cookies):
    conn = CdpConnection(ws_url, timeout=10)
    try:
        conn.send("Storage.setCookies", {"cookies": cookies})
    finally:
        conn.close()


COOKIES = [
    {"name": "sid", "value": "a1", "domain": ".example.com", "path": "/", "secure": True, "httpOnly": True,
     "sameSite": "Lax", "expires": 1893456000},
    {"name": "lang", "value": "nl", "domain": "shop.example.com", "path": "/", "secure": False, "httpOnly": False},
    {"name": "trk", "value": "zz", "domain": ".tracker.net", "path": "/", "secure": False, "httpOnly": False},
]


@pytest.mark.parametrize("which", ["http", "ws"])
def test_sync_from_cdp(api, chromium, which):
    _set_cookies(chromium.ws_url, COOKIES)
    endpoint = chromium.http_url if which == "http" else chromium.ws_url
    res = Cloud().profiles.sync("acct-1", from_cdp=endpoint, domains=["example.com"])
    sent = api.requests("PUT")[-1]["body"]["cookies"]
    assert sorted(c["name"] for c in sent) == ["lang", "sid"]
    sid = next(c for c in sent if c["name"] == "sid")
    # persistent (Chromium caps the lifetime at 400 days, so not the exact value set)
    assert sid["domain"] == ".example.com" and sid["httpOnly"] is True and sid["expires"] > time.time()
    lang = next(c for c in sent if c["name"] == "lang")
    assert lang["expires"] == -1  # a session cookie, as CDP reports it
    assert set(sid) <= {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
    assert res["imported"] == 2
    assert chromium.alive()  # reading cookies never closes the browser it reads from


class _Served:
    """What serve() returns, standing in for a local Clearcote: the test Chromium."""

    def __init__(self, chromium, calls):
        self._c = chromium
        self.calls = calls

    @property
    def ws_url(self):
        return self._c.ws_url

    @property
    def cdp_url(self):
        return self._c.http_url

    def is_alive(self):
        return self._c.alive()

    def close(self):
        self.calls.append("close")


def test_sync_from_profile_reads_a_headless_local_browser(api, chromium, monkeypatch, tmp_path):
    _set_cookies(chromium.ws_url, COOKIES)
    calls = []

    def fake_serve(**kw):
        calls.append(kw)
        return _Served(chromium, calls)

    monkeypatch.setattr(clearcote, "serve", fake_serve)
    Cloud().profiles.sync("acct-1", from_profile=str(tmp_path), domains=["tracker.net"])
    assert calls[0] == {"user_data_dir": str(tmp_path), "headless": True, "quiet": True}
    assert calls[-1] == "close"
    assert [c["name"] for c in api.requests("PUT")[-1]["body"]["cookies"]] == ["trk"]


def test_sync_by_login_opens_the_page_and_waits(api, chromium, monkeypatch):
    calls = []

    def fake_serve(**kw):
        calls.append(kw)
        return _Served(chromium, calls)

    def confirm():
        # the user "signs in": the page the SDK opened sets the cookie; wait until it has loaded
        deadline = time.time() + 15
        while time.time() < deadline and not api.requests("GET", "/login-page"):
            time.sleep(0.05)
        time.sleep(0.3)
        calls.append("confirmed")

    monkeypatch.setattr(clearcote, "serve", fake_serve)
    res = Cloud().profiles.sync("acct-1", login_url=f"{api.url}/login-page", domains=["127.0.0.1"], confirm=confirm)
    assert calls[0] == {"headless": False, "quiet": True}
    assert calls[1:] == ["confirmed", "close"]
    tabs = json.load(urllib.request.urlopen(chromium.http_url + "/json/list"))
    assert any(t["url"].endswith("/login-page") for t in tabs)
    sent = api.requests("PUT")[-1]["body"]["cookies"]
    assert [(c["name"], c["value"], c["domain"]) for c in sent] == [("sid", "s3cret", "127.0.0.1")]
    assert res["imported"] == 1
