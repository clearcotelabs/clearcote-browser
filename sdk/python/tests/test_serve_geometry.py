"""serve() headless geometry: the display, the window, and the fit over a raw CDP connection.

serve() hands out a raw CDP endpoint, so no Playwright context options reach its pages; geometry is
set browser-wide instead (the headless display + the real window). Untouched, a served headless
browser reports the 800x600 surface as its screen. These mirror the Node SDK's serve() tests in
sdk/node/test/geometry.test.ts; the live tests at the bottom attach the way a user's client does.
"""
import concurrent.futures
import os

import pytest

import clearcote
from clearcote._fingerprint import _light_stealth_screen, _light_stealth_values
from clearcote._geometry import (
    WINDOWS_TASKBAR_HEIGHT,
    _fit_plan,
    fit_served_window,
    fit_window_over_cdp,
    geometry_is_coherent,
    headless_display,
    headless_geometry,
    screen_info_switch,
    served_geometry,
    validate_window_size,
)


# --------------------------------------------------------------- the headless display
def test_a_light_stealth_seed_gets_its_own_row_so_screen_and_dpr_stay_a_pair():
    for seed in ("a", "b", "c", "probe-1", "probe-7"):
        d = headless_display(seed, ["--fingerprint-platform=windows"], light_stealth=True)
        assert d == _light_stealth_screen(seed)
        # 1536x864 is the only non-1.0 laptop row
        if d["width"] == 1536:
            assert _light_stealth_values(seed)["device_pixel_ratio"] == 1.25


def test_light_stealth_row_matches_the_node_sdk():
    # the same sha256 construction as Node's lightStealthScreen (probe-1 -> the 4K row)
    assert _light_stealth_screen("probe-1") == {
        "width": 3840, "height": 2160, "avail_width": 3840, "avail_height": 2120}


def test_without_light_stealth_it_is_the_cross_sdk_corpus_row():
    d = headless_display("x", ["--fingerprint-platform=windows"])
    screen = headless_geometry("x")["screen"]
    assert (d["width"], d["height"]) == (screen["width"], screen["height"])
    assert d["avail_height"] == d["height"] - WINDOWS_TASKBAR_HEIGHT


def test_a_light_stealth_row_off_windows_has_no_taskbar():
    d = headless_display("x", ["--fingerprint-platform=linux"], light_stealth=True)
    assert d["avail_height"] == d["height"]


# --------------------------------------------------------------- which switches
def test_sets_the_display_and_window_origin_without_a_persona():
    args = ["--fingerprint-platform=windows"]
    g = served_geometry(args, "probe-1", light_stealth=True)
    assert g["persona"] is False
    assert g["args"] == [screen_info_switch(_light_stealth_screen("probe-1")), "--window-position=0,0"]


def test_leaves_the_display_to_a_persona():
    assert served_geometry(["--fingerprint=seed"], "seed") == {
        "persona": True, "display": None, "args": ["--window-position=0,0"]}


def test_never_passes_window_size_it_would_force_every_popup_to_that_size():
    for args in ([], ["--fingerprint=s"]):
        assert not any(a.startswith("--window-size") for a in served_geometry(args)["args"])


@pytest.mark.parametrize("flag", [
    "--window-size=1024,768", "--window-position=5,5", "--start-maximized", "--screen-info={1280x720}"])
def test_stays_out_of_the_way_of_a_callers_window_or_display(flag):
    assert served_geometry([flag]) is None


def test_stays_out_of_the_way_when_headed_or_for_the_android_window():
    assert served_geometry([], headless=False) is None
    # the SDK's own android --window-size counts: a phone persona sizes itself
    assert served_geometry(["--fingerprint=s", "--window-size=412,915"]) is None


@pytest.mark.parametrize("ok, expected", [
    (None, None), ({"width": 1440, "height": 900}, (1440, 900)), ((1440, 900), (1440, 900))])
def test_window_size_accepts_a_dict_or_a_pair(ok, expected):
    assert validate_window_size(ok) == expected


@pytest.mark.parametrize("bad", [
    {"width": 1440}, (1440,), (99, 900), (1440, 10001), (1440.5, 900), (True, 900), "1440x900"])
def test_window_size_rejects_anything_else(bad):
    with pytest.raises(TypeError):
        validate_window_size(bad)


# --------------------------------------------------------------- the window fit over CDP
class _FakeBrowser:
    """Models a served browser: the page reads its display, the window reports the bounds it got.

    ``stale_reads``: that many reads after each resize still report the size from before it, as a
    page does until the new window size reaches it (the read after that, the new size)."""

    def __init__(self, display, update_screen=True, height_bias=0, initial=(780, 580), stale_reads=0):
        self.display = list(display)
        self.update_screen = update_screen
        self.height_bias = height_bias
        self.outer = list(initial)
        self.previous = self.outer
        self.stale_reads = stale_reads
        self.stale = 0
        self.calls = []

    def send(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        if method == "Target.getTargets":
            return {"targetInfos": [{"targetId": "T1", "type": "page"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "S1"}
        if method == "Runtime.evaluate":
            if "availWidth" in params["expression"]:
                return {"result": {"value": self.display}}
            if self.stale > 0:
                self.stale -= 1
                return {"result": {"value": list(self.previous)}}
            return {"result": {"value": list(self.outer)}}
        if method == "Emulation.getScreenInfos":
            return {"screenInfos": [{"id": "2300000000", "isPrimary": True}]}
        if method == "Emulation.updateScreen" and not self.update_screen:
            raise RuntimeError("'Emulation.updateScreen' wasn't found")
        if method == "Browser.getWindowForTarget":
            return {"windowId": 3}
        if method == "Browser.setWindowBounds":
            b = params["bounds"]
            self.previous = self.outer
            self.outer = [b["width"], b["height"] - self.height_bias]
            self.stale = self.stale_reads
        return {}

    def methods(self):
        return [m for m, _p, _s in self.calls]

    def of(self, method):
        return [p for m, p, _s in self.calls if m == method]


def test_maximizes_onto_the_work_area_and_leaves_a_regime_2_display_alone():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040])
    assert fit_window_over_cdp(f, persona=False) == {
        "display": {"width": 1920, "height": 1080, "avail_width": 1920, "avail_height": 1040},
        "outer": (1920, 1040)}
    assert f.of("Browser.setWindowBounds") == [
        {"windowId": 3, "bounds": {"left": 0, "top": 0, "width": 1920, "height": 1040}}]
    assert "Emulation.updateScreen" not in f.methods()
    assert "Runtime.enable" not in f.methods()
    # the page is only ever read through its own session, and that session is detached again
    assert all(s == "S1" for m, _p, s in f.calls if m == "Runtime.evaluate")
    assert f.methods()[-1] == "Target.detachFromTarget"


def test_makes_the_headless_display_the_personas_own_before_sizing_the_window():
    f = _FakeBrowser([1536, 864, 0, 0, 1536, 824])
    fit_window_over_cdp(f, persona=True)
    assert f.of("Emulation.updateScreen") == [{
        "screenId": "2300000000", "left": 0, "top": 0, "width": 1536, "height": 864,
        "workAreaInsets": {"left": 0, "top": 0, "right": 0, "bottom": 40}}]
    assert f.methods().index("Emulation.updateScreen") < f.methods().index("Browser.setWindowBounds")


def test_leaves_the_window_alone_when_the_engine_cannot_resize_a_personas_display():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040], update_screen=False)
    assert fit_window_over_cdp(f, persona=True) is None
    assert "Browser.setWindowBounds" not in f.methods()
    assert f.methods()[-1] == "Target.detachFromTarget"


def test_honours_a_window_size_inside_the_work_area_and_clamps_one_past_it():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040])
    fit_window_over_cdp(f, persona=False, window_size=(1440, 900))
    assert f.of("Browser.setWindowBounds")[0] == {
        "windowId": 3, "bounds": {"left": 10, "top": 10, "width": 1440, "height": 900}}
    g = _FakeBrowser([1366, 768, 0, 0, 1366, 728])
    assert fit_window_over_cdp(g, persona=False, window_size=(1440, 900))["outer"] == (1366, 728)
    assert g.of("Browser.setWindowBounds")[0] == {
        "windowId": 3, "bounds": {"left": 0, "top": 0, "width": 1366, "height": 728}}


def test_adds_back_a_reported_shortfall_without_overshooting():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040], height_bias=33)
    fit_window_over_cdp(f, persona=False)
    assert [p["bounds"]["height"] for p in f.of("Browser.setWindowBounds")] == [1040, 1073]


def _sizes(f):
    return ["%(left)d,%(top)d %(width)dx%(height)d" % p["bounds"] for p in f.of("Browser.setWindowBounds")]


def test_waits_for_a_resize_to_reach_the_page_before_measuring_it():
    """Measured on 150/linux-x64 (no taskbar): the first read still had the default 945x1060 window,
    the fit "corrected" 1440 to 1440+495, and the next read had only caught up with the first resize,
    so the 1935x900 window on a 1920x1080 screen stayed."""
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1080], initial=(945, 1060), stale_reads=1)
    assert fit_window_over_cdp(f, persona=False, window_size=(1440, 900))["outer"] == (1440, 900)
    assert _sizes(f) == ["10,10 1440x900"]


def test_does_not_correct_a_maximized_window_from_a_stale_read():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040], initial=(945, 1020), stale_reads=2)
    assert fit_window_over_cdp(f, persona=False)["outer"] == (1920, 1040)
    assert _sizes(f) == ["0,0 1920x1040"]


def test_still_adds_back_a_real_shortfall_when_the_page_is_slow_to_see_it():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1040], height_bias=89, stale_reads=2)
    assert fit_window_over_cdp(f, persona=True)["outer"] == (1920, 1040)
    assert _sizes(f) == ["0,0 1920x1040", "0,0 1920x1129"]


def test_corrects_nothing_when_the_page_never_shows_the_resize():
    f = _FakeBrowser([1920, 1080, 0, 0, 1920, 1080], initial=(945, 1060), stale_reads=10 ** 9)
    fit_window_over_cdp(f, persona=False, window_size=(1440, 900), settle_timeout=0.1)
    assert _sizes(f) == ["10,10 1440x900"]


def test_never_plans_bounds_past_the_work_area_for_a_window_smaller_than_it():
    # 1440 wide at x=10 has 1910px of room; the misread shortfall would have asked for 1935.
    assert _fit_plan((1440, 900), (945, 1060), (1910, 1070)) == (1910, 900)
    # a window that fills the work area may need bounds beyond it (the persona engine clamps)
    assert _fit_plan((1920, 1040), (1920, 951), (1920, 1040)) == (1920, 1129)


def test_declines_the_800x600_surface():
    f = _FakeBrowser([800, 600, 0, 0, 800, 600])
    assert fit_window_over_cdp(f, persona=True) is None
    assert "Browser.setWindowBounds" not in f.methods()


def test_never_raises_no_page_a_dead_connection_no_endpoint():
    class _Empty:
        def send(self, *a, **k):
            return {"targetInfos": []}

    class _Dead:
        def send(self, *a, **k):
            raise RuntimeError("connection closed")

    assert fit_window_over_cdp(_Empty(), persona=False) is None
    assert fit_window_over_cdp(_Dead(), persona=True) is None
    assert fit_served_window(None, persona=False) is None
    assert fit_served_window("ws://127.0.0.1:9/devtools/browser/x", persona=False, timeout=0.5) is None


def test_serve_rejects_a_bad_window_size_before_launching_anything():
    with pytest.raises(TypeError):
        clearcote.serve(window_size=(50, 50))


# --------------------------------------------------------------- live engine
LIVE_EXE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
live_only = pytest.mark.skipif(not LIVE_EXE, reason="set CLEARCOTE_LIVE_ENGINE=<path to chrome> to run")

_READ_JS = """() => ({
    screen: [screen.width, screen.height],
    avail: [screen.availWidth, screen.availHeight],
    inner: [innerWidth, innerHeight],
    outer: [outerWidth, outerHeight],
    pos: [screenX, screenY],
    media_agrees: matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches,
})"""


def _served(**kwargs):
    """serve() for real, attach the way a user's client does, and read the first page, a new tab
    and two popups (one small, one far larger than the screen).

    Runs on a worker thread: Playwright's sync API refuses to start inside a running asyncio loop,
    and the async tests elsewhere in the suite (asyncio_mode = "auto") can leave one on this thread.
    A fresh thread has none, and every Playwright call below stays on it.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_served_on_this_thread, **kwargs).result()


def _served_on_this_thread(**kwargs):
    from playwright.sync_api import sync_playwright

    # --no-sandbox as in the launch live tests: containers usually cannot run the Chrome sandbox. It is
    # also the flag serve() adds itself as root on Linux, whose warning bar it keeps off the first tab.
    srv = clearcote.serve(executable_path=LIVE_EXE, quiet=True, args=["--no-sandbox"], **kwargs)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(srv.cdp_url)
            ctx = browser.contexts[0]
            out = {"first": ctx.pages[0].evaluate(_READ_JS)}
            tab = ctx.new_page()
            tab.goto("data:text/html,<body style='margin:0'>geo</body>")
            tab.wait_for_timeout(500)
            out["tab"] = tab.evaluate(_READ_JS)
            for w, h in ((500, 400), (4000, 3000)):
                with ctx.expect_page() as info:
                    tab.evaluate(f"window.open('about:blank', '_blank', 'width={w},height={h}')")
                info.value.wait_for_timeout(500)
                out[f"popup{w}"] = info.value.evaluate(_READ_JS)
            browser.close()
            return out
    finally:
        srv.close()


def _assert_on_screen(label, m):
    assert geometry_is_coherent(m["screen"], m["avail"], m["inner"], m["outer"]), f"{label}: {m}"
    assert m["pos"][0] + m["outer"][0] <= m["avail"][0], f"{label} overhangs the right edge: {m}"
    assert m["pos"][1] + m["outer"][1] <= m["avail"][1], f"{label} overhangs the bottom edge: {m}"
    assert m["media_agrees"], f"{label}: device-width media query disagrees with screen: {m}"


def _assert_no_warning_bar(out):
    """--no-sandbox is on Chromium's "unsupported command-line flag" list, and the warning bar it
    raises sits on the first tab: 56px off that tab's innerHeight, a frame (outer - inner) of 185px
    where a second tab in the same window has 129 (Windows; 177 vs 121 on Linux). The frame stays in
    the range real captures show, and the first tab reads what a second tab reads."""
    for label in ("first", "tab"):
        m = out[label]
        dx, dy = m["outer"][0] - m["inner"][0], m["outer"][1] - m["inner"][1]
        assert 0 <= dx <= 16 and 60 <= dy <= 160, f"{label}: implausible window frame ({dx}, {dy}): {m}"
    assert out["first"]["inner"] == out["tab"]["inner"], f"first tab differs from a second one: {out}"
    assert out["first"]["outer"] == out["tab"]["outer"], f"first tab differs from a second one: {out}"


@live_only
def test_live_served_seedless_browser_is_maximized_on_its_display():
    out = _served()
    screen = headless_geometry(None)["screen"]
    for label, m in out.items():
        _assert_on_screen(label, m)
        assert m["screen"] == [screen["width"], screen["height"]], f"{label}: {m}"
    for label in ("first", "tab"):
        assert out[label]["outer"] == out[label]["avail"], f"{label} not maximized: {out[label]}"
    # window.open() features are honoured, not forced to the window size
    assert out["popup500"]["inner"][0] == 500
    _assert_no_warning_bar(out)


@live_only
def test_live_served_persona_display_is_the_personas_own():
    out = _served(fingerprint="live-geo")
    for label, m in out.items():
        _assert_on_screen(label, m)
        assert m["avail"][1] < m["screen"][1], f"{label}: persona reported no taskbar: {m}"
    assert out["first"]["outer"] == out["first"]["avail"]
    _assert_no_warning_bar(out)


@live_only
def test_live_served_light_stealth_first_tab_keeps_the_frame_a_second_tab_has():
    out = _served(fingerprint="live-serve-py", light_stealth=True)
    _assert_on_screen("first", out["first"])
    assert out["first"]["outer"] == out["first"]["avail"]
    _assert_no_warning_bar(out)


@live_only
def test_live_served_window_size_is_honoured_inside_the_work_area():
    out = _served(window_size={"width": 1440, "height": 900})
    _assert_on_screen("first", out["first"])
    assert out["first"]["outer"] == [1440, 900]
    _assert_no_warning_bar(out)


# Headed opens real windows, so it is opt-in, as for the headed launch() test in test_geometry:
# CLEARCOTE_LIVE_HEADED=1 (and a display: on Linux run under xvfb-run).
@pytest.mark.skipif(not LIVE_EXE or not os.environ.get("CLEARCOTE_LIVE_HEADED"),
                    reason="set CLEARCOTE_LIVE_ENGINE and CLEARCOTE_LIVE_HEADED=1 to run headed serve tests")
def test_live_headed_served_first_tab_keeps_the_frame_a_second_tab_has():
    _assert_no_warning_bar(_served(headless=False))
