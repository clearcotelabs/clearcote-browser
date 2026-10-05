"""Headless window-geometry tests.

WHY THIS FILE EXISTS. A default headless launch used to report a window LARGER than the screen it
claims to be on::

    screen 1280x720   avail 1280x720   inner 1280x720   outer 1288x851

``outer > screen`` on both axes is not a subtle statistical tell — it is a state no real browser can
be in, readable with two property lookups and no JS trickery. It happened because headless has only
an 800x600 surface for a display while Playwright's emulated viewport synthesizes a window on top.

The tests below lock down what keeps it fixed: the display (``--screen-info``) and the window fit
the SDK applies instead, the fact that the default is actually APPLIED at every launch entry point
(and never over a caller's own choice), and — in the live tests — that a real engine ends up with a
maximized window on that display whatever frame its platform draws (8x131 linux, 16x134 windows).

The parity vector is duplicated verbatim in the Node and .NET suites. If a seed ever selects a
different screen in one SDK, a persona stops being portable across them, so all three assert the
same table.
"""
import os

import pytest

import clearcote
from clearcote._geometry import (
    ENGINE_FRAME_HEIGHT,
    ENGINE_FRAME_WIDTH,
    HEADLESS_SCREEN_PROFILES,
    WINDOWS_TASKBAR_HEIGHT,
    apply_headless_geometry,
    caller_set_the_display,
    caller_sized_the_window,
    fit_window_to_work_area,
    fit_window_to_work_area_async,
    geometry_is_coherent,
    headless_display,
    headless_geometry,
    persona_active,
    profile_screen_from_args,
    screen_info_switch,
)


# --------------------------------------------------------------- the frame arithmetic
# headless_geometry's viewport is the cross-SDK contract (the PARITY vector below); launch takes only
# its screen row (the display) and fits the real window instead of sizing against the frame.
def test_every_profile_row_leaves_a_window_that_fits_its_screen():
    """The whole point: inner + frame must land ON the screen edge, never past it."""
    for sw, sh, _weight, _os in HEADLESS_SCREEN_PROFILES:
        inner = (sw - ENGINE_FRAME_WIDTH, sh - ENGINE_FRAME_HEIGHT)
        outer = (inner[0] + ENGINE_FRAME_WIDTH, inner[1] + ENGINE_FRAME_HEIGHT)
        # A CDP screen override forces avail == screen (measured), so that is what a page reads.
        assert geometry_is_coherent((sw, sh), (sw, sh), inner, outer), (
            f"{sw}x{sh}: inner={inner} outer={outer} escapes the screen")


def test_viewport_is_the_selected_screen_minus_the_engine_frame():
    """Whatever row a seed selects, the viewport must be derived from THAT row's screen."""
    table = {(sw, sh) for sw, sh, _w, _os in HEADLESS_SCREEN_PROFILES}
    for i in range(200):
        geom = headless_geometry(f"formula-{i}")
        sw, sh = geom["screen"]["width"], geom["screen"]["height"]
        assert (sw, sh) in table, "selected a screen that is not in the profile table"
        assert geom["viewport"] == {"width": sw - ENGINE_FRAME_WIDTH,
                                   "height": sh - ENGINE_FRAME_HEIGHT}


def test_geometry_is_coherent_rejects_the_old_default():
    """The regression itself: the pre-fix default (screen == inner, frame on top) must fail the
    invariant, or this helper would not have caught anything."""
    assert not geometry_is_coherent((1280, 720), (1280, 720), (1280, 720), (1288, 851))


def test_maximized_window_lands_flush_with_the_screen_edge():
    """outer == screen == avail is the shape of a maximized window on a display with no taskbar —
    the only shape headless can produce, and one 23 of 79 corpus networks also report."""
    geom = headless_geometry("flush")
    outer_w = geom["viewport"]["width"] + ENGINE_FRAME_WIDTH
    outer_h = geom["viewport"]["height"] + ENGINE_FRAME_HEIGHT
    assert (outer_w, outer_h) == (geom["screen"]["width"], geom["screen"]["height"])


def test_no_profile_screen_is_smaller_than_the_frame_it_must_hold():
    for sw, sh, _w, _os in HEADLESS_SCREEN_PROFILES:
        assert sw - ENGINE_FRAME_WIDTH >= 1024 and sh - ENGINE_FRAME_HEIGHT >= 600, (
            f"{sw}x{sh} leaves a sub-desktop viewport")


# --------------------------------------------------------------- selection
def test_selection_is_deterministic_and_seedless_is_stable():
    assert headless_geometry("abc") == headless_geometry("abc")
    # An unset seed must not be random — a seedless launch has to be reproducible.
    assert headless_geometry(None) == headless_geometry("") == headless_geometry(None)


def test_every_row_is_reachable_and_weighting_follows_the_corpus():
    from collections import Counter

    seen = Counter()
    for i in range(4000):
        g = headless_geometry(f"s{i}")
        seen[(g["screen"]["width"], g["screen"]["height"])] += 1
    assert len(seen) == len(HEADLESS_SCREEN_PROFILES), "a profile row is unreachable"
    # 1920x1080 carries the largest corpus weight, so it must dominate; the capped ultrawide must
    # stay rare (an ultrawide on ~1 launch in 5 would be its own oddity).
    assert seen[(1920, 1080)] == max(seen.values())
    assert seen[(3440, 1440)] / sum(seen.values()) < 0.08


# Cross-SDK parity: identical assertions live in sdk/node/test/geometry.test.ts and
# sdk/dotnet/tests/GeometryTests.cs. Same seed must mean the same persona in every SDK.
PARITY = [
    (None, 1920, 1080, 1912, 949),
    ("", 1920, 1080, 1912, 949),
    ("seed-1", 1920, 1200, 1912, 1069),
    ("acct-42", 1366, 768, 1358, 637),
    ("clearcote", 2560, 1440, 2552, 1309),
    ("x", 2560, 1440, 2552, 1309),
    ("y", 3440, 1440, 3432, 1309),
    ("z", 1920, 1080, 1912, 949),
    ("12345", 1920, 1080, 1912, 949),
]


@pytest.mark.parametrize("seed,sw,sh,vw,vh", PARITY)
def test_cross_sdk_parity_vector(seed, sw, sh, vw, vh):
    geom = headless_geometry(seed)
    assert geom == {"screen": {"width": sw, "height": sh},
                    "viewport": {"width": vw, "height": vh}}


# --------------------------------------------------------------- regime detection
def test_persona_active_tracks_the_fingerprint_switch_not_the_kwarg():
    """light_stealth passes a seed to the SDK but deliberately drops --fingerprint, so the persona
    is NOT running and the engine spoofs no screen — the regime has to be read off the command line,
    not off the caller's kwargs."""
    assert persona_active(["--fingerprint=abc", "--no-sandbox"])
    assert not persona_active(["--fingerprint-platform=windows", "--fingerprint-screen-width=1920"])
    assert not persona_active([])
    assert not persona_active(None)


def test_caller_sized_the_window_detects_every_window_flag():
    assert caller_sized_the_window(["--window-size=1920,1080"])
    assert caller_sized_the_window(["--window-position=0,0"])
    assert caller_sized_the_window(["--start-maximized"])
    assert not caller_sized_the_window(["--no-sandbox", "--fingerprint=x"])


def test_caller_set_the_display_detects_screen_info():
    assert caller_set_the_display(["--screen-info={1280x720}"])
    assert not caller_set_the_display(["--no-sandbox", "--window-size=1,1"])


# --------------------------------------------------------------- apply / skip rules
def test_regime_2_sets_the_seeds_display_and_no_viewport_when_headless_is_true_or_unset():
    for kwargs in ({"headless": True}, {}):
        applied = apply_headless_geometry(
            kwargs, "seed", args=["--no-sandbox", "--fingerprint-platform=windows"])
        screen = headless_geometry("seed")["screen"]
        display = {"width": screen["width"], "height": screen["height"], "avail_width": screen["width"],
                   "avail_height": screen["height"] - WINDOWS_TASKBAR_HEIGHT}
        assert applied == {"mode": "display", "display": display,
                           "args": [screen_info_switch(display), "--window-position=0,0"]}
        # the window is fitted to the display, never sized against a per-platform frame
        assert kwargs["no_viewport"] is True
        assert "screen" not in kwargs and "viewport" not in kwargs


def test_regime_2_display_has_a_taskbar_on_windows_only():
    d = headless_display("seed", ["--fingerprint-platform=linux"])
    assert (d["avail_width"], d["avail_height"]) == (d["width"], d["height"])
    d = headless_display("seed", ["--fingerprint-platform=windows"])
    assert d["avail_height"] == d["height"] - WINDOWS_TASKBAR_HEIGHT


def test_regime_2_display_agrees_with_an_explicit_screen_the_engine_spoofs():
    args = ["--fingerprint-platform=windows", "--fingerprint-screen-width=1600",
            "--fingerprint-screen-height=900"]
    assert headless_display("x", args) == {
        "width": 1600, "height": 900, "avail_width": 1600, "avail_height": 860}
    assert headless_display("x", args + ["--fingerprint-avail-height=870"])["avail_height"] == 870
    # never a work area larger than the screen
    assert headless_display("x", args + ["--fingerprint-avail-width=2000"])["avail_width"] == 1600


def test_screen_info_switch_writes_the_taskbar_as_a_work_area_inset():
    assert screen_info_switch({"width": 1920, "height": 1080, "avail_width": 1920,
                               "avail_height": 1040}) == "--screen-info={1920x1080 workAreaBottom=40}"
    assert screen_info_switch({"width": 1920, "height": 1080, "avail_width": 1920,
                               "avail_height": 1080}) == "--screen-info={1920x1080}"


def test_regime_2_keeps_a_callers_own_display_or_window_switch():
    own = apply_headless_geometry({}, "seed", args=["--screen-info={1280x720}"])
    assert own == {"mode": "display", "display": None, "args": ["--window-position=0,0"]}
    # a caller-sized window still gets a real screen under it; the fit then leaves it alone
    sized = apply_headless_geometry({}, "seed", args=["--window-size=1440,900"])
    assert len(sized["args"]) == 1 and sized["args"][0].startswith("--screen-info=")


def test_regime_1_takes_no_viewport_and_leaves_screen_to_the_persona():
    """Setting `screen` here would be a silent no-op: the persona's own value wins over the CDP
    override (measured), so the SDK must not pretend to control it."""
    kwargs = {"headless": True}
    applied = apply_headless_geometry(kwargs, "seed", args=["--fingerprint=seed", "--no-sandbox"])
    assert applied == {"mode": "persona", "args": []}
    assert kwargs["no_viewport"] is True
    assert "screen" not in kwargs and "viewport" not in kwargs


def test_skipped_when_headed():
    kwargs = {"headless": False}
    assert apply_headless_geometry(kwargs, "seed", args=["--fingerprint=seed"]) is None
    assert kwargs == {"headless": False}


@pytest.mark.parametrize("explicit", [
    {"viewport": {"width": 800, "height": 600}},
    {"viewport": None},
    {"no_viewport": True},
    {"screen": {"width": 1024, "height": 768}},
])
@pytest.mark.parametrize("args", [["--no-sandbox"], ["--fingerprint=seed"]])
def test_never_overrides_a_caller_who_expressed_geometry_intent(explicit, args):
    kwargs = {"headless": True, **explicit}
    assert apply_headless_geometry(kwargs, "seed", args) is None
    assert kwargs == {"headless": True, **explicit}


# --------------------------------------------------------------- the imported profile's screen
def _profile_arg(profile):
    """Encode a capture the way fingerprint_args does (gzip+base64 on --fingerprint-profile)."""
    import base64
    import gzip
    import json as _json

    packed = gzip.compress(_json.dumps(profile).encode("utf-8"), 9)
    return "--fingerprint-profile=" + base64.b64encode(packed).decode("ascii")


def test_profile_screen_is_read_off_the_switch():
    arg = _profile_arg({"screen": {"width": 3440, "height": 1440, "avail_height": 1392}})
    assert profile_screen_from_args([arg, "--no-sandbox"]) == (3440, 1440)


def test_profile_screen_is_used_instead_of_a_corpus_pick():
    """A seedless profile launch must show the display the caller imported, not a corpus screen —
    otherwise every profile launch shares one screen and the imported identity is thrown away."""
    arg = _profile_arg({"screen": {"width": 2560, "height": 1440}})
    kwargs = {"headless": True}
    applied = apply_headless_geometry(kwargs, "some-seed", args=[arg, "--fingerprint-platform=windows"])
    assert applied["display"] == {"width": 2560, "height": 1440, "avail_width": 2560,
                                  "avail_height": 1400}
    assert kwargs["no_viewport"] is True


def test_profile_screen_too_small_falls_back_to_the_corpus():
    """profile="auto" resolved on a headless host can carry the 800x600 headless surface as its
    screen (measured). Sizing a persona to that would be a worse tell than a corpus screen."""
    arg = _profile_arg({"screen": {"width": 800, "height": 600}})
    assert profile_screen_from_args([arg]) is None
    applied = apply_headless_geometry({"headless": True}, "seed", args=[arg])
    screen = headless_geometry("seed")["screen"]
    assert (applied["display"]["width"], applied["display"]["height"]) == (
        screen["width"], screen["height"])


@pytest.mark.parametrize("arg", [
    "--fingerprint-profile=not-base64!!",
    "--fingerprint-profile=" ,
    "--fingerprint-profile=aGVsbG8=",            # valid base64, not gzip
])
def test_an_unreadable_profile_never_breaks_the_launch(arg):
    assert profile_screen_from_args([arg]) is None
    applied = apply_headless_geometry({"headless": True}, "seed", args=[arg])
    assert applied["display"]["width"] == headless_geometry("seed")["screen"]["width"]


def test_a_profile_without_a_screen_block_falls_back():
    arg = _profile_arg({"navigator": {"platform": "Win32"}})
    assert profile_screen_from_args([arg]) is None


def test_a_seed_beside_a_profile_still_takes_the_persona_regime():
    """With --fingerprint present the ENGINE applies the profile's screen (measured), so the SDK must
    keep out of it and only fit the window."""
    arg = _profile_arg({"screen": {"width": 2560, "height": 1440}})
    kwargs = {"headless": True}
    applied = apply_headless_geometry(kwargs, "seed", args=[arg, "--fingerprint=seed"])
    assert applied == {"mode": "persona", "args": []}
    assert kwargs["no_viewport"] is True


# --------------------------------------------------------------- the work-area window fit
class _FakeCdp:
    def __init__(self, on_bounds=None):
        self.calls = []
        self.on_bounds = on_bounds

    def send(self, method, params=None):
        self.calls.append((method, params))
        if method == "Browser.getWindowForTarget":
            return {"windowId": 7}
        if method == "Browser.setWindowBounds" and self.on_bounds:
            self.on_bounds(params["bounds"])
        return {}


class _FakePageForFit:
    """Models the engine's bounds-vs-outer behaviour: it reports outerHeight height_bias px
    below the bounds height it was handed (33 on 149.0.7827.114)."""

    def __init__(self, avail=(1920, 1040), height_bias=33, bias_first_call_only=False,
                 initial=(0, 0), stale_reads=0):
        self.avail = list(avail)
        self.height_bias = height_bias
        # An engine that under-reports once and then honours bounds exactly would make the
        # correction overshoot — that is what the revert guard is for.
        self.bias_first_call_only = bias_first_call_only
        self.bounds_calls = 0
        self.outer = tuple(initial)
        # A page still reports the previous size for a while after a resize: that many reads.
        self.previous = self.outer
        self.stale_reads = stale_reads
        self.stale = 0
        self.cdp = _FakeCdp(on_bounds=self._apply_bounds)
        outer_self = self

        class _Ctx:
            def new_cdp_session(self, page):
                return outer_self.cdp

        self.context = _Ctx()

    def _apply_bounds(self, bounds):
        # CDP accepts a partial bounds; a position-only move must not touch the size.
        if "width" not in bounds or "height" not in bounds:
            return
        self.bounds_calls += 1
        biased = not self.bias_first_call_only or self.bounds_calls == 1
        h = bounds["height"] - (self.height_bias if biased else 0)
        self.previous = self.outer
        self.outer = (bounds["width"], h)
        self.stale = self.stale_reads

    def evaluate(self, js):
        if "availWidth" in js:
            return self.avail
        if self.stale > 0:
            self.stale -= 1
            return list(self.previous)
        return list(self.outer)          # outerWidth/Height after the last setWindowBounds


def test_window_fit_maximizes_into_the_personas_work_area():
    """One correction pass: the engine reports outerHeight below the bounds height it was given
    (33px on 149), so a single fit lands short of the work area and the shortfall is added back."""
    page = _FakePageForFit((1920, 1040), height_bias=33)
    assert fit_window_to_work_area(page, ["--fingerprint=x"]) == (1920, 1040)
    bounds = [p["bounds"] for m, p in page.cdp.calls if m == "Browser.setWindowBounds"]
    assert bounds == [
        {"left": 0, "top": 0, "width": 1920, "height": 1040},   # first attempt: lands 33 short
        {"left": 0, "top": 0, "width": 1920, "height": 1073},   # + the measured shortfall
    ]


def test_window_fit_needs_no_correction_when_the_engine_is_exact():
    page = _FakePageForFit((1920, 1040), height_bias=0)
    assert fit_window_to_work_area(page) == (1920, 1040)
    assert len([m for m, _ in page.cdp.calls if m == "Browser.setWindowBounds"]) == 1


def test_window_fit_reverts_rather_than_overshooting_the_work_area():
    """An engine that honours bounds exactly AND reports a short outer would make the correction
    overshoot; outer > avail is just as impossible as the bug being fixed, so revert instead."""
    page = _FakePageForFit((1920, 1040), height_bias=33, bias_first_call_only=True)
    assert fit_window_to_work_area(page) == (1920, 1040)
    bounds = [p["bounds"] for m, p in page.cdp.calls if m == "Browser.setWindowBounds"]
    assert len(bounds) == 3 and bounds[2] == bounds[0], "should have reverted to the safe bounds"


def test_window_fit_measures_the_shortfall_only_once_the_page_has_the_new_size():
    """A read straight after the resize can still have the engine's default window: correcting from
    it asked for 1920+975 px, and a second stale read hid the overshoot."""
    page = _FakePageForFit((1920, 1040), height_bias=33, initial=(945, 1020), stale_reads=2)
    assert fit_window_to_work_area(page, ["--fingerprint=x"]) == (1920, 1040)
    bounds = [p["bounds"] for m, p in page.cdp.calls if m == "Browser.setWindowBounds"]
    assert bounds == [
        {"left": 0, "top": 0, "width": 1920, "height": 1040},
        {"left": 0, "top": 0, "width": 1920, "height": 1073},
    ]


async def test_async_window_fit_measures_the_shortfall_only_once_the_page_has_the_new_size():
    sync = _FakePageForFit((1920, 1040), height_bias=33, initial=(945, 1020), stale_reads=2)

    class _AsyncCdp:
        async def send(self, method, params=None):
            return sync.cdp.send(method, params)

    class _AsyncCtx:
        async def new_cdp_session(self, page):
            return _AsyncCdp()

    class _AsyncPage:
        context = _AsyncCtx()

        async def evaluate(self, js):
            return sync.evaluate(js)

    assert await fit_window_to_work_area_async(_AsyncPage(), ["--fingerprint=x"]) == (1920, 1040)
    heights = [p["bounds"]["height"] for m, p in sync.cdp.calls if m == "Browser.setWindowBounds"]
    assert heights == [1040, 1073]


def test_window_fit_defers_to_a_caller_supplied_window_size():
    page = _FakePageForFit((1920, 1040))
    assert fit_window_to_work_area(page, ["--window-size=1024,768"]) is None
    assert page.cdp.calls == []


def test_window_fit_declines_an_implausible_work_area():
    """No display set -> the headless default work area. Maximizing to 800x600 would be worse
    than leaving the window alone."""
    page = _FakePageForFit((800, 600))
    assert fit_window_to_work_area(page) is None
    assert page.cdp.calls == []


def test_window_fit_never_raises():
    """A geometry improvement must not be able to fail a launch."""

    class _Boom:
        def evaluate(self, _js):
            raise RuntimeError("target closed")

    assert fit_window_to_work_area(_Boom()) is None


# --------------------------------------------------------------- wiring at the entry points
def _fake_exe(tmp_path):
    exe = tmp_path / "fake-chrome"
    exe.write_bytes(b"\x00")
    return str(exe)


class _Capture:
    """Records the options every launch path hands to Playwright, and the CDP traffic the persona
    window fit generates."""

    def __init__(self, avail=(1920, 1040)):
        self.launch_kwargs = None
        self.context_kwargs = None
        self.cdp = _FakeCdp()
        self.avail = list(avail)

    def install(self, monkeypatch):
        cap = self

        class _FakePage:
            def __init__(self, kw=None):
                self.kw = kw or {}

                class _Ctx:
                    def new_cdp_session(self, page):
                        return cap.cdp

                self.context = _Ctx()

            def evaluate(self, _js):
                return cap.avail

        class _FakeCtx:
            def __init__(self, with_page=True):
                # a persistent context already owns a page, exactly like the real one
                self.pages = [_FakePage()] if with_page else []

            def on(self, *a, **k):
                pass

            def new_page(self, **kw):
                return _FakePage(kw)

            def new_context(self, **kw):
                return _FakeCtx(with_page=False)

            def close(self, **kw):
                pass

        class _Chromium:
            def launch(self, **kw):
                cap.launch_kwargs = kw
                return _FakeCtx(with_page=False)

            def launch_persistent_context(self, user_data_dir, **kw):
                cap.context_kwargs = kw
                return _FakeCtx()

        class _PW:
            chromium = _Chromium()

        monkeypatch.setattr(clearcote, "_playwright", lambda: _PW())
        monkeypatch.setattr(clearcote, "install_humanize", lambda *a, **k: None)
        monkeypatch.setattr(clearcote, "install_humanize_on_context", lambda *a, **k: None)
        return self

    @property
    def bounds(self):
        """The setWindowBounds params, if the persona fit ran."""
        params = next((p for m, p in self.cdp.calls if m == "Browser.setWindowBounds"), None)
        return params["bounds"] if params else None


def _screen_info(kw):
    return [a for a in kw["args"] if a.startswith("--screen-info=")]


def test_persistent_context_seedless_sets_the_display_and_fits_the_window(monkeypatch, tmp_path):
    """Regime 2: no --fingerprint, so the SDK sets the headless display itself."""
    cap = _Capture(avail=(1920, 1040)).install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_fake_exe(tmp_path), quiet=True)
    screen = headless_geometry(None)["screen"]
    [switch] = _screen_info(cap.context_kwargs)
    assert switch.startswith(f"--screen-info={{{screen['width']}x{screen['height']}")
    assert "--window-position=0,0" in cap.context_kwargs["args"]
    assert cap.context_kwargs["no_viewport"] is True
    assert "screen" not in cap.context_kwargs and "viewport" not in cap.context_kwargs
    # maximized into the display's work area, like regime 1
    assert cap.bounds == {"left": 0, "top": 0, "width": 1920, "height": 1040}


def test_persistent_context_with_a_seed_uses_no_viewport_and_fits_the_window(monkeypatch, tmp_path):
    """Regime 1: --fingerprint is emitted, so the persona owns screen/avail and the SDK only
    maximizes the window into the persona's work area."""
    cap = _Capture(avail=(2560, 1400)).install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_fake_exe(tmp_path), fingerprint="geo-1", quiet=True)
    assert cap.context_kwargs["no_viewport"] is True
    assert "screen" not in cap.context_kwargs and "viewport" not in cap.context_kwargs
    assert _screen_info(cap.context_kwargs) == [], "the persona owns the display"
    assert cap.bounds == {"left": 0, "top": 0, "width": 2560, "height": 1400}


def test_persistent_context_stays_no_viewport_when_headed(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    clearcote.launch_persistent_context(
        str(tmp_path / "prof"), executable_path=_fake_exe(tmp_path), headless=False, quiet=True)
    assert cap.context_kwargs["no_viewport"] is True
    assert "screen" not in cap.context_kwargs and "viewport" not in cap.context_kwargs
    assert cap.bounds is None, "a headed window is sized by the OS, not by us"


def test_launch_default_path_carries_the_geometry_through_the_throwaway_profile(monkeypatch, tmp_path):
    """launch() delegates to a throwaway persistent context (0.23.0+), so the geometry has to arrive
    as context options there rather than via a new_page wrap."""
    cap = _Capture().install(monkeypatch)
    clearcote.launch(executable_path=_fake_exe(tmp_path), quiet=True)
    assert cap.context_kwargs["no_viewport"] is True
    assert len(_screen_info(cap.context_kwargs)) == 1


def test_incognito_launch_defaults_new_page_geometry(monkeypatch, tmp_path):
    """ephemeral_profile=False takes the pre-0.23 chromium.launch() path, which accepts no context
    options at all — the default has to ride on new_page/new_context instead."""
    cap = _Capture().install(monkeypatch)
    browser = clearcote.launch(
        executable_path=_fake_exe(tmp_path), ephemeral_profile=False, quiet=True)
    assert "no_viewport" not in cap.launch_kwargs, "no_viewport is not a chromium.launch option"
    assert len(_screen_info(cap.launch_kwargs)) == 1   # the display is browser-wide
    assert browser.new_page().kw == {"no_viewport": True}   # wrapper injected no_viewport
    assert cap.bounds == {"left": 0, "top": 0, "width": 1920, "height": 1040}
    # a caller's own choice still wins
    assert browser.new_page(no_viewport=True).kw == {"no_viewport": True}
    assert browser.new_page(viewport={"width": 640, "height": 480}).kw == {
        "viewport": {"width": 640, "height": 480}}


def test_incognito_launch_with_a_seed_fits_each_new_window(monkeypatch, tmp_path):
    cap = _Capture(avail=(1600, 860)).install(monkeypatch)
    browser = clearcote.launch(
        executable_path=_fake_exe(tmp_path), ephemeral_profile=False, fingerprint="geo-3", quiet=True)
    page = browser.new_page()
    assert page.kw == {"no_viewport": True}
    assert cap.bounds == {"left": 0, "top": 0, "width": 1600, "height": 860}


def test_incognito_headed_launch_still_uses_no_viewport(monkeypatch, tmp_path):
    cap = _Capture().install(monkeypatch)
    browser = clearcote.launch(
        executable_path=_fake_exe(tmp_path), ephemeral_profile=False, headless=False, quiet=True)
    assert browser.new_page().kw == {"no_viewport": True}
    assert cap.bounds is None


# --------------------------------------------------------------- live engine
# These are the only tests that can catch the engine silently un-fixing this. Point
# CLEARCOTE_LIVE_ENGINE at a chrome binary (plus CLEARCOTE_LICENSE_KEY for a PRO build) to run them;
# they belong in the release gate.
LIVE_EXE = os.environ.get("CLEARCOTE_LIVE_ENGINE")
live_only = pytest.mark.skipif(
    not LIVE_EXE, reason="set CLEARCOTE_LIVE_ENGINE=<path to chrome> to run")

_MEASURE_JS = """() => ({
    screen: [screen.width, screen.height],
    avail: [screen.availWidth, screen.availHeight],
    inner: [innerWidth, innerHeight],
    outer: [outerWidth, outerHeight],
    pos: [screenX, screenY],
    resizes: window.__resizes,
    media_agrees: matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches,
})"""

# Runs before any page script, so a window resized after a page starts running JS shows up as a
# resize event and a jump in innerWidth — which is what a detector would see.
_RECORDER_JS = ("window.__resizes = 0; "
                "addEventListener('resize', () => { window.__resizes++; }, true);")


def _measure_live(tmp_dir, tabs=1, **kwargs):
    """Launch for real and read the geometry each tab sees. Returns one dict per tab."""
    ctx = clearcote.launch_persistent_context(
        str(tmp_dir), executable_path=LIVE_EXE, args=["--no-sandbox"], quiet=True, **kwargs)
    try:
        ctx.add_init_script(_RECORDER_JS)
        out = []
        for _ in range(tabs):
            page = ctx.new_page()
            page.goto("data:text/html,<body style='margin:0'>geo</body>")
            page.wait_for_timeout(700)  # first paint: innerWidth/Height read 0 before it
            out.append(page.evaluate(_MEASURE_JS))
        return out if tabs > 1 else out[0]
    finally:
        ctx.close()


# Headed launches need a display (Xvfb on Linux, the desktop on Windows). Opt in explicitly so the
# default `pytest` run never pops windows on a developer machine.
headed_live_only = pytest.mark.skipif(
    not LIVE_EXE or not os.environ.get("CLEARCOTE_LIVE_HEADED"),
    reason="set CLEARCOTE_LIVE_ENGINE and CLEARCOTE_LIVE_HEADED=1 to run headed geometry tests")

# The seed that reproduced the window-box contradiction on r17/r18, headed with no_viewport, on
# Windows AND Linux (2026-09-02): persona screen 1536x864, avail 1536x824, while the OS window kept
# the host's default size -- inner 1689x1187 inside outer 1705x824. Pinned here so the exact persona
# that failed is the one the release gate re-measures.
WINDOW_BOX_REGRESSION_SEED = "verify-2026-09-02-A"


@headed_live_only
def test_live_headed_persona_window_fits_its_own_screen(tmp_path):
    """Headed + no_viewport is the path the SDK deliberately leaves to the engine (no CDP resize),
    so the engine's own window sizing must keep inner <= outer <= avail <= screen."""
    m = _measure_live(tmp_path / "live-headed-persona", headless=False, no_viewport=True,
                      fingerprint=WINDOW_BOX_REGRESSION_SEED)
    assert m["screen"] == [1536, 864] and m["avail"] == [1536, 824], (
        f"seed no longer derives the pinned persona screen; re-pin the regression seed: {m}")
    assert geometry_is_coherent(m["screen"], m["avail"], m["inner"], m["outer"]), (
        f"headed persona window escapes its own screen: {m}")
    # The engine sizes the window at creation, before any page runs -- a page must never observe
    # a resize.
    assert m["resizes"] == 0, f"window was resized after page start: {m}"


@live_only
def test_live_regime_1_persona_owns_the_screen_and_the_window_is_maximized(tmp_path):
    """Seeded launch: the engine's persona supplies screen + avail (with a taskbar) and the SDK
    maximizes the window into that work area."""
    m = _measure_live(tmp_path / "live-persona", fingerprint="live-geo")

    assert geometry_is_coherent(m["screen"], m["avail"], m["inner"], m["outer"]), (
        f"live geometry escapes its screen: {m}")
    # screen must NOT have collapsed onto the viewport — that collapse is the original bug.
    assert m["screen"] != m["inner"], f"screen tracked the viewport: {m}"
    # the persona reserves a taskbar; a headless launch that reports none is the regime-2 shape
    assert m["avail"][1] < m["screen"][1], f"persona reported no taskbar: {m}"
    # the window fit maximized into the work area
    assert m["outer"] == m["avail"], f"window was not fitted to the work area: {m}"
    # and the frame the engine synthesizes stays in the range real captures show
    dx, dy = m["outer"][0] - m["inner"][0], m["outer"][1] - m["inner"][1]
    assert 0 <= dx <= 16 and 60 <= dy <= 160, f"implausible window frame ({dx}, {dy}): {m}"
    # a maximized window starts at the work-area origin and does not hang off the screen
    assert m["pos"][0] + m["outer"][0] <= m["screen"][0], f"window overhangs the right edge: {m}"
    assert m["pos"][1] + m["outer"][1] <= m["screen"][1], f"window overhangs the bottom edge: {m}"
    # the fit ran on about:blank, before the page executed a line of script: a resize the page can
    # see would be a tell in itself
    assert m["resizes"] == 0, f"the page observed the window being resized: {m}"


def _assert_maximized_on_the_display(m):
    assert geometry_is_coherent(m["screen"], m["avail"], m["inner"], m["outer"]), (
        f"live geometry escapes its screen: {m}")
    assert m["outer"] == m["avail"], f"window was not fitted to the work area: {m}"
    assert m["pos"] == [0, 0], f"window is not at the origin: {m}"
    dx, dy = m["outer"][0] - m["inner"][0], m["outer"][1] - m["inner"][1]
    assert 0 <= dx <= 16 and 60 <= dy <= 160, f"implausible window frame ({dx}, {dy}): {m}"


@live_only
def test_live_regime_2_seedless_display_is_the_cross_sdk_row_and_the_window_is_maximized(tmp_path):
    """Seedless launch: no persona, so the SDK sets the headless display (the seed's cross-SDK row,
    with a taskbar on Windows) and maximizes the window into it — whatever frame this platform's
    engine draws (linux 8x131 vs windows 16x134 is why nothing is sized against a constant)."""
    m = _measure_live(tmp_path / "live-seedless")
    screen = headless_geometry(None)["screen"]
    display = headless_display(None, [])
    assert m["screen"] == [screen["width"], screen["height"]]
    assert m["avail"] == [display["avail_width"], display["avail_height"]]
    # a real display, not an emulated screen: device-width media queries agree with screen.*
    assert m["media_agrees"] is True, f"device-width media query disagrees with screen: {m}"
    _assert_maximized_on_the_display(m)
    assert m["resizes"] == 0, f"the page observed the window being moved/resized: {m}"


@live_only
def test_live_regime_2_incognito_launch_gives_every_context_its_own_maximized_window():
    """The chromium.launch() path: each new context is a new window on the same display."""
    browser = clearcote.launch(executable_path=LIVE_EXE, args=["--no-sandbox"], quiet=True,
                               ephemeral_profile=False)
    try:
        for page in (browser.new_page(), browser.new_context().new_page()):
            page.goto("data:text/html,<body style='margin:0'>geo</body>")
            page.wait_for_timeout(700)
            _assert_maximized_on_the_display(page.evaluate(_MEASURE_JS))
    finally:
        browser.close()


@live_only
def test_live_second_tab_reports_the_same_geometry(tmp_path):
    """The window fixup happens once, on the first page; later tabs share the window and must agree."""
    first, second = _measure_live(tmp_path / "live-tabs", tabs=2)
    assert second["screen"] == first["screen"]
    assert second["inner"] == first["inner"]
    assert second["outer"] == first["outer"]
    assert second["resizes"] == 0


@live_only
def test_live_explicit_viewport_is_still_honored_exactly(tmp_path):
    """The defaults must never take a caller's explicit geometry away from them."""
    m = _measure_live(tmp_path / "live-explicit",
                      viewport={"width": 1024, "height": 768},
                      screen={"width": 1280, "height": 1024})
    assert m["inner"] == [1024, 768]
    assert m["screen"] == [1280, 1024]
