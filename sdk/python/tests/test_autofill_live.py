"""LIVE: a form submitted in a launch_persistent_context() profile is remembered in Default/Web Data, as Chrome
does. The engine's own defaults (r54) have autofill off and save 0 rows; the SDK seeds it on (_autofillpref).
Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary (and a licence is available for a PRO build:
CLEARCOTE_LICENSE_KEY or ~/.clearcote/license.key).

    CLEARCOTE_LIVE_ENGINE=/path/to/chrome python -m pytest tests/test_autofill_live.py

Mirrors sdk/node/test/autofill.live.test.ts.
"""
import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import clearcote
from clearcote import async_api

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

FORM = b"""<!doctype html><form method="post" action="/done">
<input name="name" autocomplete="name"><input name="email" type="email" autocomplete="email">
<input name="city" autocomplete="address-level2"><button>Send</button></form>"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self):
        if self.command == "POST":
            self.rfile.read(int(self.headers.get("content-length") or 0))
        body = b"<p>ok</p>" if self.path == "/done" else FORM
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _send

    def log_message(self, *_a):
        pass


@pytest.fixture
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % srv.server_address[1]
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def udd():
    d = tempfile.mkdtemp(prefix="cc-autofill-live-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _rows(user_data_dir):
    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(user_data_dir, "Default", "Web Data"), uri=True)
    try:
        return db.execute("select count(*) from autofill").fetchone()[0]
    finally:
        db.close()


def _autofill(user_data_dir):
    with open(os.path.join(user_data_dir, "Default", "Preferences"), encoding="utf-8") as f:
        return json.load(f).get("autofill")


def _fill(page, site):
    page.goto(site)
    page.fill('[name="name"]', "Ada Lovelace")
    page.fill('[name="email"]', "ada@example.com")
    page.fill('[name="city"]', "London")
    with page.expect_navigation(url="**/done"):
        page.click("button")
    page.wait_for_timeout(1500)


def _launch(user_data_dir):
    return clearcote.launch_persistent_context(user_data_dir, executable_path=LIVE, headless=True, quiet=True,
                                               fingerprint="autofill-live-1", geoip=False)


def test_remembers_what_was_typed_into_a_submitted_form(site, udd):
    ctx = _launch(udd)
    try:
        _fill(ctx.pages[0] if ctx.pages else ctx.new_page(), site)
    finally:
        ctx.close()  # graceful: Chrome writes Web Data on the way out
    autofill = _autofill(udd)
    assert autofill["profile_enabled"] is True and autofill["credit_card_enabled"] is True
    rows = _rows(udd)
    print("autofill rows (seeded on): %d" % rows)
    assert rows > 0


def test_a_profile_that_turned_it_off_keeps_it_off(site, udd):
    os.makedirs(os.path.join(udd, "Default"))
    with open(os.path.join(udd, "Default", "Preferences"), "w", encoding="utf-8") as f:
        json.dump({"autofill": {"profile_enabled": False}}, f)
    ctx = _launch(udd)
    try:
        _fill(ctx.pages[0] if ctx.pages else ctx.new_page(), site)
    finally:
        ctx.close()
    assert _autofill(udd)["profile_enabled"] is False
    rows = _rows(udd)
    print("autofill rows (explicit off): %d" % rows)
    assert rows == 0


def test_async_remembers_what_was_typed_into_a_submitted_form(site, udd):
    """The async launch_persistent_context, on its own thread: its event loop must not meet the sync API's."""
    async def run():
        ctx = await async_api.launch_persistent_context(udd, executable_path=LIVE, headless=True, quiet=True,
                                                        fingerprint="autofill-live-1", geoip=False)
        try:
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            await page.goto(site)
            await page.fill('[name="name"]', "Ada Lovelace")
            await page.fill('[name="email"]', "ada@example.com")
            await page.fill('[name="city"]', "London")
            async with page.expect_navigation(url="**/done"):
                await page.click("button")
            await page.wait_for_timeout(1500)
        finally:
            await ctx.close()

    errors = []

    def target():
        try:
            asyncio.run(run())
        except BaseException as e:  # noqa: BLE001 -- reported below, in the test's thread
            errors.append(e)

    th = threading.Thread(target=target)
    th.start()
    th.join(180)
    assert not th.is_alive(), "the async run did not finish in 180 s"
    assert not errors, errors
    rows = _rows(udd)
    print("autofill rows (async, seeded on): %d" % rows)
    assert rows > 0
