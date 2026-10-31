"""After-action behaviour (SC 232 lane C), on a fake page with a virtual clock.

Measured on r42 / SDK 0.51.0 (the review): a hover stopped dead on arrival -- 0 pointer samples after the
glide's last one -- and after a click the pointer never left the pressed pixel. Real hands keep drifting on
a hovered element for a few hundred ms and drift off a clicked one. Bands (EVAL.md rows C):
  hover dwell -- >= 3 samples after arrival, within 3 px of it and inside the box, 150-900 ms per seed;
  move-off    -- 60-85 % of clicks per seed (40 clicks), 200-700 ms after the release, 40-200 px, downward;
  attention   -- after a load the first action waits 0.8-4 s, longer on a page with more text; idle drifts
                 when > 1.5 s pass between two actions; ON unless attention=False (SC 299);
  eager move-off -- (SC 299) the click itself waits the move-off delay and drifts off before it returns, unless the
                 page says the point is not safe to leave (a field, a menu, a popup trigger: MOVE_OFF_SAFE);
  ambient     -- ambient="load" is the per-load burst, "idle" is the attention model;
  never on trial, with a button held, or around press-and-hold.
"""
import math

import pytest

import clearcote._hold as HOLD
import clearcote._humanize as H
import clearcote._pacing as PACING
try:
    from clearcote._attention import TEXT_LENGTH
except ImportError:   # 0.51.0 (the run that must fail): no attention model
    TEXT_LENGTH = None
from clearcote._isolated import CSS_BOX, FRAMES, PLATFORM, VIEWPORT
try:
    from clearcote._isolated import MOVE_OFF_SAFE
except ImportError:   # 0.67.1 (the run that must fail): no eager move-off
    MOVE_OFF_SAFE = None

SEEDS = ["ha-1", "ha-2", "ha-3", "ha-4", "ha-5", "ha-6"]
BOX = {"x": 400.0, "y": 300.0, "width": 120.0, "height": 40.0}


class Clock:
    """Virtual time for the humanizer, the pacer and the hold: sleep advances it."""

    def __init__(self):
        self.now = 1000.0

    def sleep(self, s):
        self.now += max(0.0, s)

    def monotonic(self):
        return self.now

    perf_counter = time = monotonic


class World:
    def __init__(self, page):
        self.page = page

    def evaluate(self, fn, arg=None, await_promise=False):
        if fn == VIEWPORT:
            return [1280, 800]
        if fn == TEXT_LENGTH:
            return self.page.text_len
        if fn == PLATFORM:
            return "Win32"
        if fn == FRAMES:
            return True
        if fn == CSS_BOX:
            return None
        if MOVE_OFF_SAFE is not None and fn == MOVE_OFF_SAFE:
            return self.page.move_off_safe   # None (the page cannot answer): the lazy rule
        return None

    def node_at(self, x, y, scroll=None):
        return 7

    def call_on_node(self, nid, fn, arg=None):
        return True   # every point reaches the element

    def scroll(self):
        return (0.0, 0.0)

    def send(self, method, params=None):
        return None


class Mouse:
    def __init__(self, page):
        self.p = page

    def move(self, x, y, **kw):
        self.p.log.append(("move", self.p.clock.now, x, y))

    def down(self, **kw):
        self.p.log.append(("down", self.p.clock.now))

    def up(self, **kw):
        self.p.log.append(("up", self.p.clock.now))

    def click(self, x, y, **kw):
        self.p.log.append(("click", self.p.clock.now, x, y))

    def wheel(self, dx, dy):
        self.p.log.append(("wheel", self.p.clock.now, dx, dy))

    def dblclick(self, *a, **kw):
        pass


class Keyboard:
    def __getattr__(self, name):
        return lambda *a, **k: None


class Locator:
    def __init__(self, page):
        self.page = page
        self.first = self

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def bounding_box(self):
        return dict(BOX)

    def wait_for(self, **kw):
        pass

    def scroll_into_view_if_needed(self, **kw):
        pass

    def count(self):
        return 1


class NoCdp:
    def new_cdp_session(self, page):
        raise RuntimeError("no CDP in this fake")

    browser = None


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

    def click(self, selector, **kw):
        self.log.append(("native-click", self.clock.now))

    def hover(self, selector, **kw):
        self.log.append(("native-hover", self.clock.now, kw))

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    for mod in (H, PACING, HOLD):
        monkeypatch.setattr(mod, "time", c)
    monkeypatch.setenv("CLEARCOTE_HUMANIZE_ENGINE_CLICKS", "off")
    return c


def attach(clock, seed):
    p = Page(clock)
    H.attach_humanize(None, p, humanize=True, seed=seed)
    return p


def trace(p):
    return list(p.__dict__.get("_clearcote_after_trace") or [])


def settle(p):
    return getattr(p, "settle", lambda: None)()


def moves(p, since=0.0):
    return [e for e in p.log if e[0] == "move" and e[1] >= since]


# ------------------------------------------------------------------------------------- hover dwell
def _after_arrival(p, t0):
    """Moves of the last hover after the glide's arrival (its last whole-pixel sample)."""
    mv = moves(p, t0)
    arrive = max(i for i, e in enumerate(mv) if float(e[2]).is_integer() and float(e[3]).is_integer())
    return mv[arrive], mv[arrive + 1:]


@pytest.mark.parametrize("seed", SEEDS)
def test_hover_dwells_on_arrival_inside_the_box(clock, seed):
    p = attach(clock, seed)
    t0 = clock.now
    p.hover("#t")
    end = clock.now
    (_, ta, ax, ay), rest = _after_arrival(p, t0)
    assert len(rest) >= 3, f"{len(rest)} samples after arrival"
    for _, _, x, y in rest:
        assert math.hypot(x - ax, y - ay) <= 3.0 + 1e-9
        assert BOX["x"] < x < BOX["x"] + BOX["width"] and BOX["y"] < y < BOX["y"] + BOX["height"]
    dwell = (end - ta) * 1000.0
    assert 150.0 <= dwell <= 900.0, dwell   # the EVAL band (planned 150-800 ms, walked at the planned frame rate)
    planned = [e for e in trace(p) if e[0] == "dwell"][-1][1]
    assert 150.0 <= planned <= 800.0


def test_hover_dwell_is_per_persona(clock):
    medians = []
    for seed in SEEDS:
        p = attach(clock, seed)
        ds = []
        for _ in range(5):
            p.hover("#t")
            ds.append([e for e in trace(p) if e[0] == "dwell"][-1][1])
        medians.append(sorted(ds)[2])
    assert len({round(m) for m in medians}) >= 4, medians


def test_hover_dwell_override_and_zero(clock):
    p = attach(clock, "ha-1")
    t0 = clock.now
    p.hover("#t", dwell=0)
    _, rest = _after_arrival(p, t0)
    assert rest == []
    t1 = clock.now
    p.hover("#t", dwell=400)
    (_, ta, _, _), rest = _after_arrival(p, t1)
    assert len(rest) >= 3
    assert 380 <= (clock.now - ta) * 1000.0 <= 400 * 1.4 + 20


def test_hover_dwell_is_not_forwarded_to_a_native_hover(clock):
    p = attach(clock, "ha-1")
    p.locator = lambda s: type("L", (), {"first": property(lambda self: self), "is_visible": lambda self: True,
                                         "bounding_box": lambda self: None, "count": lambda self: 1})()
    p.hover("#t", dwell=300)
    assert p.log[-1][0] == "native-hover" and "dwell" not in p.log[-1][2]


# ---------------------------------------------------------------------------------------- move-off
def _clicks_with_settle(p, clock, n=40):
    out = []
    for i in range(n):
        x, y = 200.0 + (i * 137) % 800, 150.0 + (i * 89) % 450
        p.mouse.click(x, y)
        t_up = [e for e in p.log if e[0] == "up"][-1][1]
        ups = clock.now
        settle(p)
        mv = moves(p, ups)
        out.append((t_up, (x, y), mv))
        clock.sleep(0.05)
    return out


@pytest.mark.parametrize("seed", SEEDS)
def test_move_off_ratio_timing_distance_and_direction(clock, seed):
    p = attach(clock, seed)
    res = _clicks_with_settle(p, clock)
    off = [(t_up, at, mv) for t_up, at, mv in res if mv]
    ratio = len(off) / len(res)
    assert 0.60 <= ratio <= 0.85, ratio
    for t_up, (x, y), mv in off:
        lag = (mv[0][1] - t_up) * 1000.0
        assert 200.0 <= lag <= 700.0 + 5, lag
        ex, ey = mv[-1][2], mv[-1][3]
        d = math.hypot(ex - x, ey - y)
        assert 12.0 <= d <= 200.0 + 1e-6, d
        assert 8.0 <= ex <= 1280 - 8 and 8.0 <= ey <= 800 - 8
        assert ey > y, "downward"   # every click point here is far from the bottom edge


def test_move_off_is_dropped_when_the_next_action_comes_first(clock):
    p = attach(clock, "ha-1")
    for _ in range(10):
        p.mouse.click(500, 300)
        n = len(moves(p))
        p.mouse.click(700, 400)   # at once: the glide to the next target is the move-off
        assert not [e for e in trace(p) if e[0] == "move_off"]
        assert len(moves(p)) > n


def test_move_off_runs_at_the_next_action_when_due(clock):
    p = attach(clock, "ha-2")
    ran = 0
    for _ in range(10):
        p.mouse.click(500, 300)
        clock.sleep(0.75)          # past any due time
        before = len([e for e in trace(p) if e[0] == "move_off"])
        p.mouse.click(600, 200)
        ran += len([e for e in trace(p) if e[0] == "move_off"]) - before
        clock.sleep(0.75)
    assert 3 <= ran <= 10   # click 1's draws share the page's running share with click 2's


def test_selector_click_schedules_a_move_off(clock):
    p = attach(clock, "ha-3")
    n = 0
    for _ in range(10):
        p.click("#t")
        n += bool(settle(p))
    assert 4 <= n <= 10


# --------------------------------------------------------------------------------- never on trial / held / hold
def test_nothing_after_action_on_trial(clock):
    p = attach(clock, "ha-1")
    p._clearcote_attention = True
    p.fire("load")
    n = len(p.log)
    p.click("#t", trial=True)
    p.hover("#t", trial=True)
    assert p.log[n:] == [], "a trial moved the pointer"
    assert list(trace(p)) == []


def test_no_move_off_while_a_button_is_held(clock):
    p = attach(clock, "ha-1")
    for _ in range(6):
        p.mouse.click(400, 400)
        clock.sleep(0.8)
        p.mouse.down()
        clock.sleep(0.8)
        p.mouse.move(600, 420)     # a drag leg: no move-off, no idle drift in the middle of it
        p.mouse.up()
    assert not [e for e in trace(p) if e[0] == "move_off"]
    assert not settle(p)


def test_press_and_hold_schedules_no_move_off_and_drifts_nothing_mid_hold(clock):
    p = attach(clock, "ha-1")
    p._clearcote_attention = True
    loc = Locator(p)
    HOLD.press_and_hold(loc, 1500, tremor=False)
    downs = [e for e in p.log if e[0] == "down"]
    ups = [e for e in p.log if e[0] == "up"]
    assert len(downs) == 1 and len(ups) == 1
    held = [e for e in moves(p) if downs[0][1] <= e[1] <= ups[0][1]]
    assert held == []
    assert not settle(p)


# --------------------------------------------------------------------------------------- attention
def _first_press_after_load(seed, clock, text_len):
    p = attach(clock, seed)
    p._clearcote_attention = True
    p.text_len = text_len
    p.fire("load")
    t_load = clock.now
    p.click("#t")
    t_down = [e for e in p.log if e[0] == "down"][0][1]
    return p, (t_down - t_load) * 1000.0


@pytest.mark.parametrize("seed", SEEDS)
def test_attention_read_pause_scales_with_text(clock, seed):
    p_short, short = _first_press_after_load(seed, clock, 120)
    p_long, long_ = _first_press_after_load(seed, clock, 20000)
    reads = [e for e in trace(p_short) if e[0] == "read"]
    assert reads and 800.0 <= reads[0][1] <= 4000.0
    assert short >= 800.0, short
    assert long_ - short >= 1500.0, (short, long_)
    assert [e for e in trace(p_long) if e[0] == "drift"], "the hand drifts while reading"


def test_attention_is_on_by_default_and_attention_false_turns_it_off(clock):
    # SC 299: measured on r54 / 0.67.1 with the model off, the first input came 0.21-0.31 s after the load.
    p = attach(clock, "ha-1")
    p.text_len = 20000
    p.fire("load")
    t = clock.now
    p.click("#t")
    assert [e for e in trace(p) if e[0] == "read"], "on by default"
    assert (clock.now - t) >= 0.8
    p2 = Page(clock)
    H.attach_humanize(None, p2, humanize=H.HumanizeOptions(attention=False), seed="ha-1")
    p2.text_len = 20000
    p2.fire("load")
    t = clock.now
    p2.click("#t")
    assert (clock.now - t) < 3.0
    assert not [e for e in trace(p2) if e[0] in ("read", "drift")]
    p3 = attach(clock, "ha-1")
    p3._clearcote_attention = False      # the page flag overrides both ways
    p3.text_len = 20000
    p3.fire("load")
    p3.click("#t")
    assert not [e for e in trace(p3) if e[0] in ("read", "drift")]


def test_attention_idle_drifts_after_a_long_gap_only(clock):
    p = attach(clock, "ha-4")
    p._clearcote_attention = True
    p.mouse.click(300, 300)
    clock.sleep(1.0)
    p.mouse.click(500, 300)
    assert not [e for e in trace(p) if e[0] == "drift"]
    clock.sleep(2.0)
    p.mouse.click(700, 300)
    drifts = [e for e in trace(p) if e[0] == "drift"]
    assert 1 <= len(drifts) <= 2
    assert all(300.0 <= e[1] <= 900.0 for e in drifts)


def test_explore_scroll_happens_in_some_loads_never_before_a_coordinate_action(clock):
    wheels = 0
    for i in range(60):
        p = attach(clock, f"ex-{i}")
        p._clearcote_attention = True
        p.fire("load")
        p.mouse.click(300, 300)
        assert not [e for e in p.log if e[0] == "wheel"]
        p.fire("load")
        p.click("#t")
        wheels += bool([e for e in p.log if e[0] == "wheel"])
    assert 6 <= wheels <= 32, wheels   # EXPLORE_RATE 0.30 of 60 loads (unseeded draws)


# ----------------------------------------------------------------------------------- ambient option
def test_ambient_option_mapping(clock):
    p = attach(clock, "ha-1")
    p.fire("load")
    assert moves(p) == [], "off by default"
    # Through the real options object (HumanizeOptions on the page, _hzoptions.options_of); the flag the
    # options also set is cleared, so the burst can only come from the option itself.
    p = Page(clock)
    H.attach_humanize(None, p, humanize={"ambient": "load"}, seed="ha-1")
    p._clearcote_auto_ambient = False
    p.fire("load")
    assert len(moves(p)) > 20, "ambient='load' runs the per-load burst"
    p2 = Page(clock)
    H.attach_humanize(None, p2, humanize=H.HumanizeOptions(ambient="idle"), seed="ha-1")
    p2.fire("load")
    assert moves(p2) == []
    p2.click("#t")
    assert [e for e in trace(p2) if e[0] == "read"], "ambient='idle' is the attention model"
    p3 = attach(clock, "ha-1")
    p3._clearcote_auto_ambient = True        # the hidden flag still works
    p3.fire("load")
    assert len(moves(p3)) > 20


# ----------------------------------------------------------------------------------- shared vectors
def _close(a, b, path="$"):
    if isinstance(a, float) or isinstance(b, float):
        assert abs(float(a) - float(b)) < 1e-6, (path, a, b)
    elif isinstance(a, dict):
        assert set(a) == set(b), path
        for k in a:
            _close(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, (list, tuple)):
        assert len(a) == len(b), (path, len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            _close(x, y, f"{path}[{i}]")
    else:
        assert a == b, (path, a, b)


def test_shared_after_action_vectors():
    import json
    import os
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from gen_after_vectors import OUT, build
    with open(OUT, encoding="utf-8") as f:
        want = json.load(f)
    _close(json.loads(json.dumps(build())), want)


def test_same_seed_same_traits_but_unseeded_per_action_draws(clock):
    """The seed fixes the persona's TRAITS only; every per-action draw is unseeded (coordinator rule, SC 232):
    a seeded per-action sequence would replay identically in every session of one identity."""
    from clearcote._motion import after_traits, make_persona
    assert after_traits(make_persona("same-1")) == after_traits(make_persona("same-1"))
    seqs = []
    for _ in range(2):
        p = attach(clock, "same-1")
        for _ in range(5):
            p.hover("#t")
            p.click("#t")
            settle(p)
        seqs.append([round(e[1], 6) for e in trace(p) if e[0] == "dwell"]
                    + [tuple(round(v, 6) for v in e[2]) for e in trace(p) if e[0] == "move_off"])
    assert len(seqs[0]) >= 5 and seqs[0] != seqs[1], seqs


def test_per_call_humanize_false_skips_the_after_action_step(clock):
    # The per-call switch is wired outermost (integration of lanes A + C): the stock call runs and no read
    # pause / idle drift / move-off is made for it; the next humanized action still gets its step.
    p = attach(clock, "ha-1")
    p._clearcote_attention = True
    p.fire("load")
    p.click("#t", humanize=False)
    assert not [e for e in trace(p) if e[0] in ("read", "drift", "move_off")]
    p.click("#t")
    assert [e for e in trace(p) if e[0] == "read"]


# --------------------------------------------------------------------------------- SC 299: eager move-off
@pytest.mark.parametrize("seed", SEEDS)
def test_eager_move_off_leaves_the_pressed_pixel_before_the_click_returns(clock, seed):
    """SC 299 (r54 / 0.67.1): with the lazy rule alone the pointer stayed on the pressed pixel through every 1.6 s
    pause after a click (0 of 12). Now the click waits the persona's 200-700 ms and drifts off before it returns,
    in its 60-85 % of clicks, when the page says the point may be left."""
    p = attach(clock, seed)
    p.move_off_safe = True
    off = 0
    n = 40
    for i in range(n):
        x, y = 200.0 + (i * 137) % 800, 150.0 + (i * 89) % 450
        p.mouse.click(x, y)                         # returns after any eager move-off
        t_up = [e for e in p.log if e[0] == "up"][-1][1]
        mv = moves(p, t_up)
        if mv:
            off += 1
            lag = (mv[0][1] - t_up) * 1000.0
            assert 200.0 <= lag <= 700.0 + 5, lag
            ex, ey = mv[-1][2], mv[-1][3]
            assert 12.0 <= math.hypot(ex - x, ey - y) <= 200.0 + 1e-6
        clock.sleep(1.6)                            # the script rests: nothing more may move
        assert moves(p, clock.now - 1.5) == []
    assert 0.60 <= off / n <= 0.85, off / n
    assert [e for e in trace(p) if e[0] == "move_off_eager"]


def test_eager_move_off_stays_lazy_where_the_page_says_no(clock):
    p = attach(clock, "ha-2")
    p.move_off_safe = False                         # a field, a menu, a popup trigger
    for i in range(12):
        p.mouse.click(300.0 + i * 20, 300.0)
        t_up = [e for e in p.log if e[0] == "up"][-1][1]
        assert moves(p, t_up) == [], "no drift inside the click"
    assert not [e for e in trace(p) if e[0] == "move_off_eager"]
