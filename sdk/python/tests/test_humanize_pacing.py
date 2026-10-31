"""Wall-clock pacing of planned pointer paths (SC 210 lane P).

The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old dispatch loop awaited
mouse.move for each sample and THEN slept the planned gap, so the dispatch round trip (27 ms median on
PRO r38) was added to every sample: glides ran 3.5-5.4 s, ambient_motion(1500) 5.7x as long (SC 209).

These tests drive the real attach_humanize wrappers with a fake mouse whose move costs 0 ms or 25 ms
of a FAKE clock (time.sleep / time.perf_counter / asyncio.sleep advance it), so they are exact and
fast. On 0.48.0 the 25 ms duration checks and the ambient checks fail.
"""
import asyncio
import math
import time

import pytest

import clearcote._humanize as hz
import clearcote._humanize_async as hza
from clearcote import _pacing

LATENCIES = (0.0, 25.0)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        if s > 0:
            self.t += s

    async def asleep(self, s, *a, **k):
        if s and s > 0:
            self.t += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(time, "sleep", c.sleep)
    monkeypatch.setattr(time, "perf_counter", c.now)
    monkeypatch.setattr(time, "monotonic", c.now)
    return c


class Mouse:
    def __init__(self, clock, latency_ms):
        self.clock, self.lat = clock, latency_ms / 1000.0
        self.log = []   # (op, t_ms, x, y)

    def _ev(self, op, x=None, y=None):
        self.clock.t += self.lat
        self.log.append((op, self.clock.t * 1000.0, x, y))

    def move(self, x, y, **k):
        self._ev("move", x, y)

    def down(self, **k):
        self._ev("down")

    def up(self, **k):
        self._ev("up")

    def click(self, *a, **k):
        pass

    def wheel(self, *a, **k):
        pass

    def dblclick(self, *a, **k):
        pass


class AsyncMouse(Mouse):
    async def move(self, x, y, **k):
        self._ev("move", x, y)

    async def down(self, **k):
        self._ev("down")

    async def up(self, **k):
        self._ev("up")

    async def click(self, *a, **k):
        pass

    async def wheel(self, *a, **k):
        pass

    async def dblclick(self, *a, **k):
        pass


class Keyboard:
    def __getattr__(self, name):
        return lambda *a, **k: None


class AsyncKeyboard:
    def __getattr__(self, name):
        async def f(*a, **k):
            return None
        return f


class Page:
    viewport_size = {"width": 1280, "height": 800}

    def __init__(self, mouse, keyboard):
        self.mouse, self.keyboard = mouse, keyboard
        self.main_frame = object()

    def on(self, *a, **k):
        pass

    def __getattr__(self, name):
        return lambda *a, **k: None


def _spy_plans(monkeypatch, mod):
    plans = []
    real = mod.plan_move
    real_enter = mod.enter_from_top

    def spy(frm, *a, **k):
        p = real(frm, *a, **k)
        plans.append((tuple(frm), p))
        return p

    def spy_enter(steps, start, *a, **k):
        # A fresh page's first glide (SC 239): the samples above the view are not dispatched, so the walk the
        # test checks is the kept part, from its first (crossing) sample.
        out = real_enter(steps, start, *a, **k)
        if plans and plans[-1][1] is steps and out is not steps:
            plans[-1] = ((out[0][0], out[0][1]), out)
        return out
    monkeypatch.setattr(mod, "plan_move", spy)
    monkeypatch.setattr(mod, "enter_from_top", spy_enter)
    return plans


def _moves(mouse, t0=None, t1=None):
    return [e for e in mouse.log if e[0] == "move" and (t0 is None or e[1] > t0) and (t1 is None or e[1] <= t1)]


def _max_gap(pts):
    return max((math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])), default=0.0)


# The largest persona hop cap (_motion.hop_cap_px draws 55-110 px).
HOP_CAP_MAX = 110.0


def _hops_ok(moves, floor=HOP_CAP_MAX):
    """No dispatched hop longer than a fast hand covers between the two events (3 px/ms, >= floor)."""
    for a, b in zip(moves, moves[1:]):
        d = math.hypot(b[2] - a[2], b[3] - a[3])
        if d > max(floor, _pacing.FAST_PX_PER_MS * (b[1] - a[1])) + 1e-6:
            return False
    return True


def _check_walk(plan, moves, dur_ms, start):
    planned = sum(s[2] for s in plan)
    assert 0.8 * planned <= dur_ms <= 1.2 * planned, f"glide took {dur_ms:.0f} ms, planned {planned:.0f} ms"
    assert (moves[-1][2], moves[-1][3]) == (plan[-1][0], plan[-1][1]), "the final sample must land exactly"
    pts = [start] + [(m[2], m[3]) for m in moves]
    own = _max_gap([start] + [(s[0], s[1]) for s in plan])
    assert math.hypot(pts[1][0] - start[0], pts[1][1] - start[1]) <= max(HOP_CAP_MAX, own) + 1e-6
    assert _hops_ok(moves, max(HOP_CAP_MAX, own)), "a skipped stretch teleported the cursor"


@pytest.mark.parametrize("lat", LATENCIES)
def test_glide_lasts_its_planned_duration(clock, monkeypatch, lat):
    plans = _spy_plans(monkeypatch, hz)
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-1")
    targets = [(900, 600), (150, 150), (640, 420), (1100, 120)]
    for (x, y) in targets:
        t0 = clock.now() * 1000.0
        n0 = len(plans)
        page.mouse.move(x, y)
        t1 = clock.now() * 1000.0
        assert len(plans) == n0 + 1
        frm, plan = plans[-1]
        mv = _moves(m, t0, t1)
        _check_walk(plan, mv, t1 - t0, frm)
        assert (mv[-1][2], mv[-1][3]) == (x, y)


@pytest.mark.parametrize("lat", LATENCIES)
def test_held_drag_keeps_button_order_and_duration(clock, monkeypatch, lat):
    plans = _spy_plans(monkeypatch, hz)
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-2")
    page.mouse.move(200, 300)
    page.mouse.down()
    t0 = clock.now() * 1000.0
    page.mouse.move(560, 310)          # held leg: the slider-captcha shape
    t1 = clock.now() * 1000.0
    page.mouse.up()
    ops = [e[0] for e in m.log]
    d, u = ops.index("down"), ops.index("up")
    assert ops.count("down") == 1 and ops.count("up") == 1 and d < u
    leg = [e for e in m.log[d + 1:u]]
    assert leg and all(e[0] == "move" for e in leg)
    assert (leg[-1][2], leg[-1][3]) == (560, 310), "the button must come up at the target"
    frm, plan = plans[-1]
    _check_walk(plan, leg, t1 - t0, frm)


@pytest.mark.parametrize("lat", LATENCIES)
@pytest.mark.parametrize("ms", (600, 1500, 3000))
def test_ambient_motion_lasts_what_was_asked(clock, lat, ms):
    m = Mouse(clock, lat)
    page = Page(m, Keyboard())
    hz.attach_humanize(None, page, humanize=True, seed="pace-3")
    t0 = clock.now() * 1000.0
    page.ambient_motion(ms)
    took = clock.now() * 1000.0 - t0
    assert _moves(m), "ambient dispatched nothing"
    # a fresh page's drift comes in over the top edge, inside its budget (SC 239): the first event is at y 0-4
    assert 0 <= _moves(m)[0][3] <= 4, f"first ambient sample at y {_moves(m)[0][3]}"
    # With 25 ms dispatch latency a late walk now stretches instead of catching up with jumps (SC 210
    # fix 2, lab v2: no hop over 40 px): ambient's fast flicks run long. Measured over 30 plans: median
    # 1.04-1.06x, worst 1.67x (600 ms), 1.38x (1500), 1.13x (3000).
    assert 0.8 * ms <= took <= (1.2 if lat == 0 else 1.8) * ms, f"ambient_motion({ms}) took {took:.0f} ms"
    assert _hops_ok(_moves(m))


@pytest.mark.parametrize("lat", LATENCIES)
def test_async_glide_and_ambient_are_paced(clock, monkeypatch, lat):
    monkeypatch.setattr(asyncio, "sleep", clock.asleep)
    plans = _spy_plans(monkeypatch, hza)
    m = AsyncMouse(clock, lat)
    page = Page(m, AsyncKeyboard())

    async def run():
        await hza.attach_humanize(None, page, humanize=True, seed="pace-4")
        for (x, y) in [(900, 600), (150, 150)]:
            t0 = clock.now() * 1000.0
            await page.mouse.move(x, y)
            t1 = clock.now() * 1000.0
            mv = _moves(m, t0, t1)
            frm, plan = plans[-1]
            _check_walk(plan, mv, t1 - t0, frm)
        t0 = clock.now() * 1000.0
        await page.ambient_motion(1500)
        took = clock.now() * 1000.0 - t0
        assert 1200 <= took <= (1800 if lat == 0 else 2700), f"async ambient_motion(1500) took {took:.0f} ms"

    asyncio.run(run())


# ---------------------------------------------------------------- the pacer itself

def _fake(lat_ms):
    c = Clock()
    sent = []

    def move(x, y):
        c.t += lat_ms / 1000.0
        sent.append((x, y, c.t))
    return c, sent, move


def test_pacer_never_drops_a_pinned_step_or_the_last():
    # 200 samples 2 ms apart with 25 ms dispatch: most must be skipped, but never one marked as
    # changing button state, and never the last.
    steps = [(i * 1.0, 0.0, 2.0, i in (37, 38, 120)) for i in range(200)]
    c, sent, move = _fake(25.0)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    xs = [s[0] for s in sent]
    assert len(xs) < 100
    for k in (37, 38, 120, 199):
        assert float(k) in xs
    assert xs == sorted(xs)


def test_pacer_on_time_dispatches_every_sample_at_its_planned_time():
    steps = [(i * 3.0, 0.0, 7.0) for i in range(50)]
    c, sent, move = _fake(0.0)
    t0 = c.t
    assert _pacing.walk(steps, move, clock=c.now, sleep=c.sleep) == 50
    for k, s in enumerate(sent):
        assert abs((s[2] - t0) * 1000.0 - 7.0 * k) < 1e-6
    assert abs((c.t - t0) * 1000.0 - 350.0) < 1e-6


def test_pacer_inserts_the_in_between_point_instead_of_a_long_jump():
    # 30 px per sample, 1 ms apart (30 px/ms, ten times a fast hand), 25 ms dispatch: skipping to the
    # most recent due sample would hop ~750 px; the pacer must keep every hop to what a fast hand covers
    # in 25 ms (75 px).
    steps = [(i * 30.0, 0.0, 1.0) for i in range(40)]
    c, sent, move = _fake(25.0)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    pts = [(s[0], s[1]) for s in sent]
    assert _max_gap([(0.0, 0.0)] + pts) <= _pacing.FAST_PX_PER_MS * 25.0 + 1e-6
    assert len(pts) > 10
    assert pts[-1] == (39 * 30.0, 0.0)


def test_pacer_pauses_through_a_stall_instead_of_skipping_after_it():
    # Measured on PRO r38 under load: one move answered after 523 ms. The samples after it must still
    # go out (only STALL_MS of lateness is caught up by skipping); the walk runs that much longer.
    steps = [(i * 2.0, 0.0, 5.0) for i in range(200)]
    c = Clock()
    sent = []

    def move(x, y):
        c.t += (0.5 if len(sent) == 10 else 0.002)
        sent.append(x)
    t0 = c.t
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    took = (c.t - t0) * 1000.0
    assert len(sent) >= 185, len(sent)
    assert abs(took - (1000.0 + 500.0 - _pacing.STALL_MS)) < 30, took
    assert sent[-1] == 398.0


def test_pacer_a_slow_first_sample_is_a_stall_too():
    # A page busy after a scroll answered the first move late: only STALL_MS of it is caught up.
    steps = [(i * 2.0, 0.0, 5.0) for i in range(40)]
    c = Clock()
    sent = []

    def move(x, y):
        c.t += (0.3 if not sent else 0.0)
        sent.append(x)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    assert len(sent) >= 40 - _pacing.STALL_MS / 5.0 - 1


def test_pacer_stops_at_a_failing_move():
    calls = []

    def move(x, y):
        calls.append(x)
        if len(calls) == 3:
            raise RuntimeError("Target closed")
    c = Clock()
    assert _pacing.walk([(i, 0, 5) for i in range(10)], move, clock=c.now, sleep=c.sleep) == 2
    assert len(calls) == 3


def test_trim_to_cuts_near_the_budget_at_a_slow_sample():
    from clearcote._motion import make_persona, plan_ambient
    p = make_persona("trim")
    for ms in (600, 1200, 1500, 4000):
        plan = plan_ambient((300, 300), {"width": 1280, "height": 800}, p, ms)
        cut = _pacing.trim_to(plan, ms)
        total = sum(s[2] for s in cut)
        assert 0.85 * ms <= total <= 1.1 * ms, (ms, total)
        assert cut == plan[:len(cut)], "trim only drops a tail; it never changes a sample"


def test_trim_to_a_short_ask_does_not_stop_the_pointer_mid_flight():
    # ~300 ms against a first excursion of 0.5-1 s: every sample in the window is mid-glide. The cut
    # moves to where the glide has slowed down (<= 30 % of peak), within 2x the ask.
    from clearcote._motion import make_persona, plan_ambient
    for seed in range(60):
        plan = plan_ambient((300, 300), {"width": 1280, "height": 800}, make_persona(seed), 300)
        cut = _pacing.trim_to(plan, 300)
        peak = max(_pacing._speed(plan, k) for k in range(len(plan)))
        total = sum(s[2] for s in cut)
        assert cut == plan[:len(cut)]
        assert total <= 600 + 1e-6, (seed, total)
        if total > 1.1 * 300:   # moved forward: only ever to a slow point
            assert _pacing._speed(plan, len(cut) - 1) <= 0.3 * peak, seed


@pytest.mark.parametrize("stall_ms", (200, 350, 500))
def test_pacer_after_a_long_stall_stretches_instead_of_jumping(stall_ms):
    """SC 210 lab v2 on r38 under load: 35 hops of 60-103 px between samples a few ms apart. A stall of
    200-500 ms used to let the next skip hop 3 px/ms x the stall; now no hop is longer than the hop cap
    (MAX_JUMP_PX without a persona), nor more than 20 px right after an idle gap: the walk takes longer."""
    from clearcote._motion import plan_move, make_persona
    steps = plan_move((100.0, 500.0), (1150.0, 140.0), make_persona("stall-1"), target_w=24)
    own = _max_gap([(s[0], s[1]) for s in steps])
    c = Clock()
    sent = []

    def move(x, y):
        c.t += stall_ms / 1000.0 if len(sent) in (3, 9, 15) else 0.025
        sent.append((x, y, c.t))
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    hops = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(sent, sent[1:])]
    assert max(hops) <= max(_pacing.MAX_JUMP_PX, own) + 1e-6, "a hop of %.0f px (planned spacing %.0f px)" % (max(hops), own)
    after = [hops[k] for k in (3, 9, 15) if k < len(hops)]
    assert max(after) <= max(20.0, own) + 1e-6, "the hop after a stall: %s" % [round(h) for h in after]
    assert (sent[-1][0], sent[-1][1]) == (steps[-1][0], steps[-1][1])


def test_keep_in_view_moves_samples_inside_but_never_the_target():
    steps = [(10.0, 5.0, 8.0), (-12.0, -3.0, 8.0), (1290.0, 400.0, 8.0, True), (1279.0, 799.0, 8.0)]
    out = _pacing.keep_in_view(steps, 1280, 800)
    assert out == [(10.0, 5.0, 8.0), (2.0, 2.0, 8.0), (1279.0, 400.0, 8.0, True), (1279.0, 799.0, 8.0)]  # the target widens the box
    aimed_out = [(10.0, 5.0, 8.0), (1300.0, 900.0, 8.0)]   # the caller aims outside: left as it is
    assert _pacing.keep_in_view(aimed_out, 1280, 800) == aimed_out


# ---------------------------------------------------------------- SC 299: interpolated points, persona hop cap

def _velocity_peaks(sent, smooth=5):
    """Local maxima (above 40 % of the peak) of the 5-sample moving average of hop / dt."""
    v = []
    for a, b in zip(sent, sent[1:]):
        dt = (b[2] - a[2]) * 1000.0
        if dt > 0:
            v.append(math.hypot(b[0] - a[0], b[1] - a[1]) / dt)
    if smooth > 1:
        v = [sum(v[max(0, i - smooth // 2):i + smooth // 2 + 1]) / len(v[max(0, i - smooth // 2):i + smooth // 2 + 1])
             for i in range(len(v))]
    top = max(v) if v else 0.0
    return sum(1 for i in range(1, len(v) - 1) if v[i] >= v[i - 1] and v[i] > v[i + 1] and v[i] > 0.4 * top)


def _jittery(lat_ms, spread_ms, seed=7):
    import random as _r
    rr = _r.Random(seed)
    c = Clock()
    sent = []

    def move(x, y):
        c.t += max(1.0, rr.uniform(lat_ms - spread_ms, lat_ms + spread_ms)) / 1000.0
        sent.append((x, y, c.t))
    return c, sent, move


def test_pacer_sends_the_point_on_the_path_when_late_and_keeps_its_speed():
    # SC 298 on Windows: an ~18 ms round trip against ~7 ms planned samples. Skipping to whole samples hopped
    # 2 or 3 at a time (a sawtooth of 7-8 velocity peaks per glide). Sending the path's point at the elapsed
    # time keeps one smooth speed profile: the smoothed velocity has at most 3 peaks, as the plan does.
    # The fake stamps each point when its round trip ends, so the jitter itself adds some wobble; over 20
    # personas the median stays at <= 3.5 smoothed peaks (the old walk: 6-7) and none exceeds 8.
    import statistics
    from clearcote._motion import plan_move, make_persona
    pk = []
    for seed in range(20):
        p = make_persona(str(seed))
        plan = plan_move((120.0, 140.0), (1020.0, 480.0), p, target_w=40)
        c, sent, move = _jittery(18.0, 6.0, seed=seed)
        _pacing.walk(plan, move, clock=c.now, sleep=c.sleep, max_jump=200.0)
        assert (sent[-1][0], sent[-1][1]) == (plan[-1][0], plan[-1][1])
        pk.append(_velocity_peaks(sent))
    assert statistics.median(pk) <= 3.5, pk
    assert max(pk) <= 8, pk


def test_planned_glide_speed_follows_its_report_times():
    # SC 299: a planned sample's position was taken at the nominal i/n while its report interval jittered
    # +-12 %, so the speed a page computes swung sample to sample (5 smoothed velocity peaks per planned glide,
    # SC 298). Walked on time, a single-submovement plan now shows one smooth speed bell.
    import statistics
    from clearcote._motion import plan_move, make_persona
    pk = []
    for seed in range(30):
        plan = plan_move((120.0, 140.0), (1020.0, 480.0), make_persona(str(seed)), target_w=40)
        c, sent, move = _fake(0.0)
        _pacing.walk(plan, move, clock=c.now, sleep=c.sleep, max_jump=200.0)
        pk.append(_velocity_peaks(sent))
    assert statistics.median(pk) <= 1.5, pk
    assert max(pk) <= 3, pk


def test_pacer_hops_beyond_40_px_when_the_plan_is_that_fast():
    # The old fixed 40 px cap was a wall every persona shared (SC 298: 0 of 4,462 page steps above 40.5 px).
    # A fast reach at ~3 px/ms with an 18-25 ms round trip covers 55-75 px between two dispatches.
    from clearcote._motion import plan_move, make_persona, hop_cap_px
    p = make_persona("3")
    cap = hop_cap_px(p)
    plan = plan_move((60.0, 100.0), (1900.0, 900.0), p, target_w=60)
    c, sent, move = _jittery(22.0, 3.0)
    _pacing.walk(plan, move, clock=c.now, sleep=c.sleep, max_jump=cap)
    hops = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(sent, sent[1:])]
    assert max(hops) > 45.0, max(hops)
    assert max(hops) <= cap + 1e-6


def test_hop_cap_is_a_persona_trait():
    from clearcote._motion import make_persona, hop_cap_px
    caps = [hop_cap_px(make_persona(str(s))) for s in range(200)]
    assert all(55.0 <= c <= 110.0 for c in caps)
    assert max(caps) - min(caps) > 40.0
    assert hop_cap_px(make_persona("same")) == hop_cap_px(make_persona("same"))


def test_pacer_never_sends_a_point_behind_one_already_sent():
    # Interpolated points move forward along the plan: the plan time of every dispatched point grows.
    steps = [(i * 4.0, (i % 7) * 1.5, 6.5) for i in range(160)]
    c, sent, move = _jittery(20.0, 8.0, seed=3)
    _pacing.walk(steps, move, clock=c.now, sleep=c.sleep)
    xs = [s[0] for s in sent]
    assert xs == sorted(xs)
    assert xs[-1] == steps[-1][0]
