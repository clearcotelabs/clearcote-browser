"""LIVE: a humanized select_option whose change handler stalls the page keeps to its timeout, as stock
select_option does. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary.

Strict humanize: a fallback to stock Playwright would pass the timing checks too, so it fails the test.

- A sync XHR that is never answered blocks the renderer, so the Enter that commits the pick is never
  acknowledged: it used to wait forever (631 s measured), stock raises at the timeout.
- A change handler that navigates to a server that never answers leaves a document that cannot
  answer the read-back: it used to raise at the page default (15 s), stock returns the value at once.
"""
import asyncio
import http.server
import os
import socketserver
import threading
import time

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

PAGES = {
    "/xhr": ('<select id="s" onchange="var x=new XMLHttpRequest();x.open(\'GET\',\'/hang\',false);'
             'try{x.send()}catch(e){};window.__changed=this.value">'
             '<option value="a">Alpha</option><option value="b">Bravo</option></select>'),
    "/nav": ('<select id="s" onchange="window.__changed=this.value;location.href=\'/hang\'">'
             '<option value="a">Alpha</option><option value="b">Bravo</option></select>'),
    "/fxhr": '<iframe src="/xhr" width="300" height="80"></iframe>',
    "/fnav": '<iframe src="/nav" width="300" height="80"></iframe>',
    "/ok": ('<select id="s" onchange="window.__changed=this.value">'
            '<option value="a">Alpha</option><option value="b">Bravo</option></select>'),
}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    stop = threading.Event()

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/hang":
            self.stop.wait(60)   # never answers while the test runs
            return
        data = ("<!doctype html><html><body>%s</body></html>" % PAGES.get(path, "")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(autouse=True, scope="module")
def _no_font_cache_left_behind():
    """A Linux launch points fontconfig at <tmp>/cc-fc-cache: remove it afterwards only when this
    module created it (CI fails on anything left in TMPDIR)."""
    import shutil
    import tempfile
    cache = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
    existed = os.path.exists(cache)
    yield
    if not existed:
        shutil.rmtree(cache, ignore_errors=True)


@pytest.fixture(scope="module")
def base():
    srv = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_address[1]
    _Handler.stop.set()
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def browser():
    # one browser per test: a page left blocked by a sync XHR must not hold up the next test
    import clearcote
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize={"strict": True}, fingerprint="select-commit-1",
                         quiet=True)
    yield b
    b.close()


def test_sync_xhr_change_handler_raises_at_the_timeout(browser, base):
    from playwright.sync_api import TimeoutError as PWTimeout
    p = browser.new_page()
    p.goto(base + "/xhr")
    t0 = time.monotonic()
    with pytest.raises(PWTimeout) as e:
        p.select_option("#s", "b", timeout=5000)
    took = time.monotonic() - t0
    assert took < 9, "raised after %.1f s: the 5 s timeout was not kept" % took
    assert "5000ms" in str(e.value)


def test_navigating_change_handler_returns_the_value(browser, base):
    p = browser.new_page()
    p.set_default_timeout(15000)
    p.goto(base + "/nav")
    t0 = time.monotonic()
    assert p.select_option("#s", "b", timeout=5000) == ["b"]
    took = time.monotonic() - t0
    assert took < 5, "took %.1f s: waited on a document that is navigating away" % took


@pytest.mark.parametrize("path", ["/fxhr", "/fnav"])
def test_inside_a_frame(browser, base, path):
    """The frame route (_frames.do_select): the same commit, inside an iframe."""
    from playwright.sync_api import TimeoutError as PWTimeout
    p = browser.new_page()
    p.set_default_timeout(15000)
    p.goto(base + path)
    sel = p.frame_locator("iframe").locator("#s")
    sel.wait_for()
    t0 = time.monotonic()
    if path == "/fxhr":
        with pytest.raises(PWTimeout):
            sel.select_option("b", timeout=5000)
        assert time.monotonic() - t0 < 9
    else:
        assert sel.select_option("b", timeout=5000) == ["b"]
        assert time.monotonic() - t0 < 5


def test_healthy_page_with_a_short_timeout_still_selects(browser, base):
    """The humane motion takes longer than a short timeout: the commit still gets its floor."""
    p = browser.new_page()
    p.goto(base + "/ok")
    assert p.select_option("#s", "b", timeout=300) == ["b"]
    assert p.evaluate("window.__changed") == "b"


def test_async_sync_xhr_and_navigating_change_handlers(base):
    """The async twin (_humanize_async), on its own thread: its event loop must not meet the sync
    API's in this one."""
    from playwright.async_api import TimeoutError as PWTimeout
    from clearcote import async_api

    async def run():
        b = await async_api.launch(executable_path=LIVE, headless=True, humanize={"strict": True},
                                   fingerprint="select-commit-1", quiet=True)
        try:
            p = await b.new_page()
            await p.goto(base + "/xhr")
            t0 = time.monotonic()
            try:
                await p.select_option("#s", "b", timeout=5000)
                out["xhr"] = "returned"
            except PWTimeout:
                out["xhr"] = time.monotonic() - t0
            p2 = await b.new_page()
            await p2.goto(base + "/nav")
            t0 = time.monotonic()
            out["nav"] = (await p2.select_option("#s", "b", timeout=5000), time.monotonic() - t0)
        finally:
            await b.close()

    out = {}
    errors = []
    th = threading.Thread(target=lambda: _run_catching(lambda: asyncio.run(run()), errors))
    th.start()
    th.join(120)
    assert not th.is_alive(), "the async run did not finish in 120 s"
    assert not errors, errors
    assert isinstance(out["xhr"], float) and out["xhr"] < 9, out
    assert out["nav"][0] == ["b"] and out["nav"][1] < 5, out


def _run_catching(fn, errors):
    try:
        fn()
    except BaseException as e:  # noqa: BLE001
        errors.append(repr(e))
