"""Live: the Frame API (incl. the main frame) and ElementHandle actions on a humanized page are humanized. Before,
ElementHandle actions and page.main_frame.* went straight to Playwright (a ~0 ms press, one teleporting move) and
strict mode said nothing (measured 2026-10-28, SC 295). A cross-origin frame's handle falls back to Playwright,
reported. Skipped unless CLEARCOTE_LIVE_ENGINE names a Clearcote binary. Mirrors humanize-frame-handle.live.test.ts.
"""
import http.server
import os
import socketserver
import threading

import pytest

LIVE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
pytestmark = pytest.mark.skipif(not LIVE, reason="needs CLEARCOTE_LIVE_ENGINE (a Clearcote build)")

REC = """<script>
window.__ev = [];
for (const t of ['mousemove','mousedown','mouseup','keydown','keyup'])
  document.addEventListener(t, (e) => window.__ev.push({t, tr: e.isTrusted, ts: e.timeStamp, k: e.code || ''}), true);
</script>"""
CHILD = ("<!doctype html><html><head>%s</head><body style='margin:8px'>"
         "<button id=fb style='width:120px;height:40px'>b</button><input id=fi></body></html>") % REC


def _main(cross):
    return ("<!doctype html><html><head>%s</head><body style='margin:10px'>"
            "<div id=t style='width:40px;height:40px;background:#c33'></div><textarea id=ta></textarea>"
            "<input id=cb type=checkbox>"
            "<iframe id=same src='/child.html' style='width:400px;height:160px'></iframe>"
            "<iframe id=cross src='%s/child.html' style='width:400px;height:160px'></iframe></body></html>") % (REC, cross)


@pytest.fixture(scope="module")
def page():
    import clearcote
    pages = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path.split("?")[0])
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *a):
            pass

    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    pages.update({"/main.html": _main("http://localhost:%d" % port), "/child.html": CHILD})
    b = clearcote.launch(executable_path=LIVE, headless=True, humanize=True, fingerprint="frame-handle-live-py",
                         quiet=True, args=["--no-sandbox"])
    try:
        p = b.new_page()
        p.goto("http://127.0.0.1:%d/main.html" % port)
        yield p
    finally:
        b.close()
        srv.shutdown()


def _same(page):
    return next(f for f in page.frames if f.url.startswith("http://127.0.0.1") and f.url.endswith("/child.html"))


def _measure(docs, act):
    for d in docs:
        d.evaluate("window.__ev.length = 0")
    act()
    ev = []
    for d in docs:
        ev += d.evaluate("window.__ev")
    down = [e["ts"] for e in ev if e["t"] == "mousedown"]
    up = [e["ts"] for e in ev if e["t"] == "mouseup"]
    held, holds = {}, []
    for e in ev:
        if e["t"] == "keydown":
            held[e["k"]] = e["ts"]
        elif e["t"] == "keyup" and e["k"] in held:
            holds.append(e["ts"] - held.pop(e["k"]))
    return {"moves": sum(1 for e in ev if e["t"] == "mousemove"), "press": (up[0] - down[0]) if down and up else None,
            "holds": holds, "trusted": all(e["tr"] for e in ev)}


def test_frame_click_child_and_main_frame_is_a_human_press(page):
    same = _same(page)
    for docs, act in (([page, same], lambda: same.click("#fb")), ([page], lambda: page.main_frame.click("#t"))):
        m = _measure(docs, act)
        assert m["trusted"] and m["moves"] > 3 and m["press"] > 30, m


def test_element_handle_click_type_check_are_human_input(page):
    m = _measure([page], lambda: page.query_selector("#t").click())
    assert m["moves"] > 3 and m["press"] > 30, m
    m = _measure([page], lambda: page.query_selector("#ta").type("hello"))
    assert len(m["holds"]) == 5 and min(m["holds"]) > 30, m
    assert page.input_value("#ta") == "hello"
    page.query_selector("#cb").check()
    assert page.is_checked("#cb")


def test_child_frame_handle_fill_types_keys(page):
    same = _same(page)
    m = _measure([page, same], lambda: same.query_selector("#fi").fill("handle"))
    assert len(m["holds"]) == 6, m
    assert same.input_value("#fi") == "handle"


def test_cross_origin_frame_handle_falls_back_and_reports(page):
    cross = next(f for f in page.frames if f.url.startswith("http://localhost"))
    page._clearcote_last_fallback = None
    cross.query_selector("#fb").click()
    fb = page._clearcote_last_fallback
    assert fb is not None and fb["api"] == "ElementHandle.click" and "cross-origin" in str(fb["reason"]), fb
