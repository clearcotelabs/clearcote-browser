"""After-action behaviour on the ASYNC surface (clearcote._humanize_async): hover dwell, move-off after a
click, the attention model, page.settle(), never on trial. Same bands as test_humanize_after_action.py
(SC 232 lane C); a virtual clock drives time.monotonic, the pacer's perf_counter and asyncio.sleep."""
import asyncio
import math

import pytest

import clearcote._humanize_async as H
import clearcote._pacing as PACING
from clearcote._isolated import CSS_BOX, FRAMES, PLATFORM, VIEWPORT
try:
    from clearcote._isolated import MOVE_OFF_SAFE
except ImportError:   # 0.67.1: no eager move-off
    MOVE_OFF_SAFE = None

try:
    from clearcote._attention import TEXT_LENGTH
except ImportError:   # 0.51.0 (the run that must fail): no attention model
    TEXT_LENGTH = None

SEEDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"]
BOX = {"x": 400.0, "y": 300.0, "width": 120.0, "height": 40.0}
_real_sleep = asyncio.sleep


class Clock:
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    perf_counter = time = monotonic

    def sleep(self, s):
        self.now += max(0.0, s)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(H, "time", c)
    monkeypatch.setattr(PACING, "time", c)

    async def vsleep(s=0, *a, **k):
        c.now += max(0.0, s or 0)
        await _real_sleep(0)
    monkeypatch.setattr(asyncio, "sleep", vsleep)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", "off")
    return c


class World:
    def __init__(self, page):
        self.page = page

    async def evaluate(self, fn, arg=None, await_promise=False):
        if fn == VIEWPORT:
            return [1280, 800]
        if TEXT_LENGTH is not None and fn == TEXT_LENGTH:
            return self.page.text_len
        if fn == PLATFORM:
            return "Win32"
        if fn == FRAMES:
            return True
        if fn == CSS_BOX:
            return None
        if MOVE_OFF_SAFE is not None and fn == MOVE_OFF_SAFE:
            return self.page.move_off_safe
        return None

    async def node_at(self, x, y, scroll=None):
        return 7

    async def call_on_node(self, nid, fn, arg=None):
        return True

    async def scroll(self):
        return (0.0, 0.0)

    async def send(self, method, params=None):
        return None


class Mouse:
    def __init__(self, p):
        self.p = p

    async def move(self, x, y, **kw):
        self.p.log.append(("move", self.p.clock.now, x, y))

    async def down(self, **kw):
        self.p.log.append(("down", self.p.clock.now))

    async def up(self, **kw):
        self.p.log.append(("up", self.p.clock.now))

    async def click(self, x, y, **kw):
        self.p.log.append(("click", self.p.clock.now, x, y))

    async def wheel(self, dx, dy):
        self.p.log.append(("wheel", self.p.clock.now, dx, dy))

    async def dblclick(self, *a, **kw):
        pass


class Keyboard:
    def __getattr__(self, name):
        async def noop(*a, **k):
            return None
        return noop


class Locator:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True

    async def bounding_box(self):
        return dict(BOX)

    async def wait_for(self, **kw):
        pass

    async def scroll_into_view_if_needed(self, **kw):
        pass

    async def count(self):
        return 1


class NoCdp:
    browser = None

    async def new_cdp_session(self, page):
        raise RuntimeError("no CDP in this fake")


class Page:
    def __init__(self, clock):
        self.clock = clock
        self.log = []
        self.handlers = {}
        self.text_len = 0
        self.move_off_safe = None
        self.mouse = Mouse(self)
        self.keyboard = Keyboard()
        self.main_frame = object()
        self.context = NoCdp()
        self.viewport_size = {"width": 1280, "height": 800}
        self._cc_isolated_world = World(self)

    def on(self, ev, fn):
        self.handlers.setdefault(ev, []).append(fn)

    def fire(self, ev):
        for fn in self.handlers.get(ev, []):
            fn(None)

    def locator(self, selector):
        return Locator(self)

    async def click(self, selector, **kw):
        self.log.append(("native-click", self.clock.now))

    async def hover(self, selector, **kw):
        self.log.append(("native-hover", self.clock.now, kw))

    def __getattr__(self, name):
        async def noop(*a, **k):
            return None
        return noop


async def attach(clock, seed):
    p = Page(clock)
    await H.attach_humanize(None, p, humanize=True, seed=seed)
    return p


def moves(p, since=0.0):
    return [e for e in p.log if e[0] == "move" and e[1] >= since]


def trace(p):
    return list(p.__dict__.get("_clearcote_after_trace") or [])


async def settle(p):
    s = p.__dict__.get("settle")
    return await s() if s else None


@pytest.mark.parametrize("seed", SEEDS)
def test_async_hover_dwells_on_arrival_inside_the_box(clock, seed):
    async def go():
        p = await attach(clock, seed)
        t0 = clock.now
        await p.hover("#t")
        end = clock.now
        mv = moves(p, t0)
        arrive = max(i for i, e in enumerate(mv) if float(e[2]).is_integer() and float(e[3]).is_integer())
        _, ta, ax, ay = mv[arrive]
        rest = mv[arrive + 1:]
        assert len(rest) >= 3, f"{len(rest)} samples after arrival"
        for _, _, x, y in rest:
            assert math.hypot(x - ax, y - ay) <= 3.0 + 1e-9
            assert BOX["x"] < x < BOX["x"] + BOX["width"] and BOX["y"] < y < BOX["y"] + BOX["height"]
        assert 150.0 <= (end - ta) * 1000.0 <= 900.0
    asyncio.run(go())


@pytest.mark.parametrize("seed", SEEDS)
def test_async_move_off_ratio_and_band(clock, seed):
    async def go():
        p = await attach(clock, seed)
        off = 0
        for i in range(40):
            x, y = 200.0 + (i * 137) % 800, 150.0 + (i * 89) % 450
            await p.mouse.click(x, y)
            t_up = [e for e in p.log if e[0] == "up"][-1][1]
            t = clock.now
            await settle(p)
            mv = moves(p, t)
            if mv:
                off += 1
                assert 200.0 <= (mv[0][1] - t_up) * 1000.0 <= 705.0
                d = math.hypot(mv[-1][2] - x, mv[-1][3] - y)
                assert 12.0 <= d <= 200.0 + 1e-6 and mv[-1][3] > y
            clock.sleep(0.05)
        assert 0.60 <= off / 40 <= 0.85, off / 40
    asyncio.run(go())


@pytest.mark.parametrize("seed", SEEDS[:3])
def test_async_attention_read_pause_scales_with_text(clock, seed):
    async def first_press(n):
        p = await attach(clock, seed)
        p._clearcote_attention = True
        p.text_len = n
        p.fire("load")
        t = clock.now
        await p.click("#t")
        return ([e for e in p.log if e[0] == "down"][0][1] - t) * 1000.0
    short = asyncio.run(first_press(120))
    long_ = asyncio.run(first_press(20000))
    assert short >= 800.0 and long_ - short >= 1500.0, (short, long_)


def test_async_nothing_on_trial_and_idle_drift_after_gap(clock):
    async def go():
        p = await attach(clock, "ha-4")
        p._clearcote_attention = True
        n = len(p.log)
        await p.click("#t", trial=True)
        assert p.log[n:] == []
        await p.mouse.click(300, 300)
        clock.sleep(2.0)
        await p.mouse.click(700, 300)
        drifts = [e for e in trace(p) if e[0] == "drift"]
        assert 1 <= len(drifts) <= 2
    asyncio.run(go())


def test_async_ambient_option_load(clock):
    async def go():
        # through the real options object; the flag the options also set is cleared
        p = Page(clock)
        await H.attach_humanize(None, p, humanize={"ambient": "load"}, seed="ha-1")
        p._clearcote_auto_ambient = False
        p.fire("load")
        for _ in range(50):
            await _real_sleep(0)
        assert len(moves(p)) > 20
    asyncio.run(go())


# --------------------------------------------------------------------------------- SC 299
@pytest.mark.parametrize("seed", SEEDS[:3])
def test_async_eager_move_off_before_the_click_returns(clock, seed):
    async def go():
        p = await attach(clock, seed)
        p.move_off_safe = True
        off = 0
        for i in range(40):
            x, y = 200.0 + (i * 137) % 800, 150.0 + (i * 89) % 450
            await p.mouse.click(x, y)
            t_up = [e for e in p.log if e[0] == "up"][-1][1]
            mv = moves(p, t_up)
            if mv:
                off += 1
                assert 200.0 <= (mv[0][1] - t_up) * 1000.0 <= 705.0
            clock.sleep(1.6)
        assert 0.60 <= off / 40 <= 0.85, off / 40
    asyncio.run(go())


def test_async_eager_move_off_stays_lazy_where_the_page_says_no(clock):
    async def go():
        p = await attach(clock, "ha-2")
        p.move_off_safe = False
        for i in range(12):
            await p.mouse.click(300.0 + i * 20, 300.0)
            t_up = [e for e in p.log if e[0] == "up"][-1][1]
            assert moves(p, t_up) == []
    asyncio.run(go())


def test_async_attention_is_on_by_default(clock):
    async def go():
        p = await attach(clock, "ha-1")
        p.text_len = 20000
        p.fire("load")
        t = clock.now
        await p.click("#t")
        assert [e for e in trace(p) if e[0] == "read"]
        assert clock.now - t >= 0.8
    asyncio.run(go())
