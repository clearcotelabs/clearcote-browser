"""Whole-notch wheel, key rollover and the r34 command rename in the humanizer.

Measured on r32/r33 before these existed: a humanized scroll of 100 became five CDP wheel events of
39/28/20/10/3 px, each reporting wheelDelta -120 (CDP always sends one tick), and typing produced
0 overlapping keydowns in 32 keys. See sdk/python/clearcote/_motion.py for the notch sizes."""
import asyncio

import pytest

from clearcote import _humanize, _humanize_async
from clearcote._isolated import PLATFORM, VIEWPORT
from clearcote._motion import (
    WHEEL_NOTCH_PX, make_persona, platform_from_navigator, rollover_rate, wheel_notches,
)


# ---------------------------------------------------------------- shared helpers (parity values)
def test_rollover_rate_parity_values():
    # Same numbers in motion.ts rolloverRate and Motion.RolloverRate.
    want = {"1111": 0.18545862230472265, 1111: 0.33123080658726395, "abc": 0.3663359977304935,
            0: 0.21331139486283063, 31337: 0.3733816216979176}
    for seed, rate in want.items():
        assert rollover_rate(make_persona(seed)) == rate


def test_rollover_rate_is_in_range():
    for seed in ("x", "y", 7, 123456):
        assert 0.12 <= rollover_rate(make_persona(seed)) <= 0.38


def test_wheel_notches_rounds_to_whole_notches():
    assert [wheel_notches(d, 100) for d in (0, 3, 39, 49, 50, 100, 149, 151, 1000, -39, -250)] == \
        [0, 1, 1, 1, 1, 1, 1, 2, 10, 1, 3]
    assert [wheel_notches(d, 120) for d in (0, 39, 59, 60, 100, 180, 1000)] == [0, 1, 1, 1, 1, 2, 8]


def test_platform_from_navigator():
    cases = {"Win32": "windows", "Windows": "windows", "MacIntel": "macos", "macOS": "macos",
             "Linux x86_64": "linux", "Linux armv8l": "linux", "Android": "android", "": None,
             None: None, "CrOS": "linux"}
    for nav, plat in cases.items():
        assert platform_from_navigator(nav) == plat
    assert WHEEL_NOTCH_PX == {"windows": 100, "linux": 120, "macos": 40, "android": 100}


# ---------------------------------------------------------------- fakes
class _World:
    def __init__(self, platform):
        self.platform = platform

    def evaluate(self, expr, arg=None):
        if expr == PLATFORM:
            return self.platform
        if expr == VIEWPORT:
            return [1280, 800]
        return None


class _Mouse:
    def __init__(self):
        self.wheels = []

    def wheel(self, dx, dy):
        self.wheels.append((dx, dy))

    def move(self, *a, **k):
        pass

    def click(self, *a, **k):
        pass

    def down(self, *a, **k):
        pass

    def up(self, *a, **k):
        pass

    def dblclick(self, *a, **k):
        pass


class _Keyboard:
    def __init__(self):
        self.events = []

    def press(self, key, **kw):
        self.events.append(("press", key))

    def type(self, text, **kw):
        self.events.append(("type", text))

    def down(self, key):
        self.events.append(("down", key))

    def up(self, key):
        self.events.append(("up", key))


class _Page:
    def __init__(self):
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()
        self.main_frame = object()
        self.viewport_size = {"width": 1280, "height": 800}

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(_humanize.time, "sleep", lambda s: None)


def _wheels(monkeypatch, platform, *calls):
    monkeypatch.setattr(_humanize, "world_for", lambda page: _World(platform))
    page = _Page()
    _humanize.attach_humanize(None, page, humanize=True, seed="t")
    page.mouse.wheels.clear()
    for dx, dy in calls:
        page.mouse.wheel(dx, dy)
    return page.mouse.wheels


@pytest.mark.parametrize("platform,call,want", [
    ("Windows", (0, 100), [(0, 100)]),
    ("Windows", (0, 39), [(0, 100)]),
    ("Linux x86_64", (0, 250), [(0, 120), (0, 120)]),
    ("MacIntel", (0, -100), [(0, -40)] * 3),
    ("Windows", (150, 0), [(100, 0), (100, 0)]),
])
def test_wheel_sends_whole_notches(monkeypatch, fast, platform, call, want):
    assert _wheels(monkeypatch, platform, call) == want


def test_wheel_never_sends_a_sub_notch_event(monkeypatch, fast):
    got = _wheels(monkeypatch, "Windows", (0, 37), (0, 512), (0, -260), (75, 0))
    assert got
    assert all(abs(dx) in (0, 100) and abs(dy) in (0, 100) and (dx == 0) != (dy == 0) for dx, dy in got)


def test_wheel_falls_back_to_the_host_platform(monkeypatch, fast):
    got = _wheels(monkeypatch, None, (0, 300))
    notch = WHEEL_NOTCH_PX[_humanize._host_platform()]
    assert got and all(dy == notch for _, dy in got)


# ---------------------------------------------------------------- rollover
def _typed(events):
    """What the field ends up holding: printable downs/presses, Backspace deletes."""
    out = []
    for kind, key in events:
        if kind in ("down", "press") and key == "Backspace":
            if out:
                out.pop()
        elif kind in ("down", "press") and len(key) == 1:
            out.append(key)
    return "".join(out)


def _overlaps(events):
    held, n = set(), 0
    for kind, key in events:
        if kind == "down" and key != "Shift":
            if held:
                n += 1
            held.add(key)
        elif kind == "up":
            held.discard(key)
    return n


def _type(monkeypatch, rate, text):
    monkeypatch.setattr(_humanize, "rollover_rate", lambda p: rate)
    page = _Page()
    _humanize.attach_humanize(None, page, humanize=True, seed="t")
    page.keyboard.type(text)
    return page.keyboard.events


def test_rollover_overlaps_keys_and_keeps_the_text(monkeypatch, fast):
    ev = _type(monkeypatch, 1.0, "asdf jkl")
    assert _overlaps(ev) >= 1
    assert _typed(ev) == "asdf jkl"
    downs = sorted(k for kind, k in ev if kind == "down")
    ups = sorted(k for kind, k in ev if kind == "up")
    assert downs == ups   # every key that went down came up


def test_rollover_rate_zero_is_todays_behaviour(monkeypatch, fast):
    ev = _type(monkeypatch, 0.0, "asdf jkl")
    assert _overlaps(ev) == 0
    assert _typed(ev) == "asdf jkl"


def test_rollover_leaves_shifted_keys_alone(monkeypatch, fast):
    ev = [e for e in _type(monkeypatch, 1.0, "Hi!") if e[1] in ("Shift", "H", "i", "!")]
    assert ev == [
        ("down", "Shift"), ("press", "H"), ("up", "Shift"),
        ("press", "i"),
        ("down", "Shift"), ("press", "!"), ("up", "Shift"),
    ]


def test_rollover_skips_a_doubled_key(monkeypatch, fast):
    ev = _type(monkeypatch, 1.0, "aa")
    assert _overlaps(ev) == 0 and _typed(ev) == "aa"


# ---------------------------------------------------------------- async mirror
class _AMouse(_Mouse):
    async def wheel(self, dx, dy):
        self.wheels.append((dx, dy))

    async def move(self, *a, **k):
        pass


class _AKeyboard(_Keyboard):
    async def press(self, key, **kw):
        self.events.append(("press", key))

    async def type(self, text, **kw):
        self.events.append(("type", text))

    async def down(self, key):
        self.events.append(("down", key))

    async def up(self, key):
        self.events.append(("up", key))


class _AWorld(_World):
    async def evaluate(self, expr, arg=None):
        return _World.evaluate(self, expr, arg)


class _APage(_Page):
    def __init__(self):
        super().__init__()
        self.mouse = _AMouse()
        self.keyboard = _AKeyboard()


def test_async_wheel_and_rollover(monkeypatch):
    async def nosleep(s):
        return None
    monkeypatch.setattr(_humanize_async.asyncio, "sleep", nosleep)
    monkeypatch.setattr(_humanize_async, "async_world_for", lambda page: _AWorld("Linux x86_64"))
    monkeypatch.setattr(_humanize_async, "rollover_rate", lambda p: 1.0)

    async def run():
        page = _APage()
        await _humanize_async.attach_humanize(None, page, humanize=True, seed="t")
        page.mouse.wheels.clear()
        await page.mouse.wheel(0, 250)
        await page.keyboard.type("asdf jkl")
        return page

    page = asyncio.run(run())
    assert page.mouse.wheels == [(0, 120), (0, 120)]
    assert _overlaps(page.keyboard.events) >= 1 and _typed(page.keyboard.events) == "asdf jkl"


# ---------------------------------------------------------------- r34 command rename
class _Session:
    def __init__(self, missing=(), fail=None):
        self.calls, self.missing, self.fail = [], set(missing), fail

    def send(self, method, params=None):
        self.calls.append(method)
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": "T"}}
        if self.fail:
            raise Exception(self.fail)
        if method in self.missing:
            raise Exception(f"Protocol error ({method}): '{method}' wasn't found")
        return {}


def _engine_page(monkeypatch, session):
    monkeypatch.setattr(_humanize, "world_for", lambda page: _World("Windows"))
    page = _Page()
    ctx = type("Ctx", (), {})()
    ctx.new_cdp_session = lambda p: session
    ctx.browser = None
    page.context = ctx
    _humanize.attach_humanize(None, page, humanize=True, seed="t")
    return page


def _browser_calls(s):
    return [c for c in s.calls if c.startswith("Browser.")]


def test_engine_click_uses_the_new_name(monkeypatch, fast):
    s = _Session()
    page = _engine_page(monkeypatch, s)
    page.mouse.click(300, 200)
    page.mouse.click(320, 260)
    assert _browser_calls(s) == ["Browser.dispatchPointerPath"] * 2


def test_engine_click_falls_back_once_on_an_older_engine(monkeypatch, fast):
    s = _Session(missing={"Browser.dispatchPointerPath"})
    page = _engine_page(monkeypatch, s)
    page.mouse.click(300, 200)
    page.mouse.click(320, 260)
    assert _browser_calls(s) == [
        "Browser.dispatchPointerPath", "Browser.humanizedClick", "Browser.humanizedClick"]


def test_engine_click_does_not_fall_back_on_other_errors(monkeypatch, fast):
    s = _Session(fail="No target with given id found")
    page = _engine_page(monkeypatch, s)
    page.mouse.click(300, 200)
    page.mouse.click(320, 260)
    # one attempt and no old-name retry; the SDK path takes over for good
    assert _browser_calls(s) == ["Browser.dispatchPointerPath"]
