"""Wall-clock pacing for planned pointer paths.

The motion planner (``_motion.plan_move`` / ``plan_ambient``) returns samples ``(x, y, sleep_ms)``
at the persona's device rate (~6-8 ms apart). Walking them as "dispatch, then sleep the planned
gap" adds the dispatch round trip to EVERY sample: measured on PRO r38, one ``mouse.move`` costs
27 ms median, so a 1.05 s planned glide took 3.5-5.4 s and the page saw ~25 pointermove/s
(SC 209). Real hands do not slow down because the driver is slow.

``walk`` schedules each sample at its planned offset from the start (the cumulative sum of the
sleeps before it) and keeps that clock:

* on time   -> sleep until the sample's planned time, then dispatch it;
* behind    -> send the point on the planned path at the time that has passed (between the two planned
               samples around it), so the move keeps the plan's shape, its duration and its SPEED profile:
               each hop is the plan's speed x the real time since the last dispatch, whatever the round trip;
* never skipped: the final sample (the move must land exactly where it was aimed), and a sample
  marked as changing button state -- both go out as planned;
* never a teleport: no hop longer than the persona's ``max_jump`` (``_motion.hop_cap_px``, 55-110 px: what a
  quick reach covers in about one 60 Hz frame), and no more than IDLE_HOP_PX (20 px) after IDLE_MS (40 ms)
  with nothing sent; a longer hop goes to the farthest point along the path within the cap, so a late walk
  stretches in time rather than jumping (SC 210 lab v2: the old cap, 3 px/ms x the time since the last
  dispatch, let a slow dispatch under load turn into 35 hops of 60-103 px a few ms apart -- and an r40 engine
  walks any hop >= 24 px after >= 40 ms idle).
  SC 298 (r54, SDK 0.67.1, Windows): the old walk skipped to whole planned samples under a FIXED 40 px cap.
  With an ~18 ms round trip it hopped 2 or 3 samples at a time -- a sawtooth of 7-8 velocity peaks per glide
  -- and no page-visible step exceeded 40 px under any persona (0 of 4,462), a wall shared by every identity;
* a stall is a pause: a single dispatch slower than ``STALL_MS`` shifts the schedule by the excess
  instead of being caught up by skipping.

Sleeps are local (``time.sleep`` / ``asyncio.sleep``), never ``page.wait_for_timeout``: that is a
CDP round trip per call, protocol traffic a detector can score inside the humanize path.

Nothing here changes what the planner returns: the samples, their coordinates and their timing are
the planner's; this module only decides WHEN each one goes out, and when it runs late, which point between
two of them goes out instead.
"""
import asyncio
import bisect
import math
import time

from . import _trace

# The hop cap when the caller names no persona (the humanizers pass the persona's _motion.hop_cap_px).
MAX_JUMP_PX = 72.0
# A single dispatch slower than this is a STALL (the page or the box was busy), not latency: nothing
# reached the page while it lasted, so the plan pauses with it instead of skipping ahead afterwards.
STALL_MS = 60.0
# A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s). No longer a dispatch cap (the
# hop cap is MAX_JUMP_PX / IDLE_HOP_PX since SC 210 round 2): kept as the speed bound the tests check.
FAST_PX_PER_MS = 3.0
# After this long with nothing dispatched, a skip may hop at most IDLE_HOP_PX: what a hand moving at a
# moderate 2.5 px/ms covers in one 125 Hz report (8 ms), and below the 24 px an r40 engine walks.
IDLE_MS = 40.0
IDLE_HOP_PX = 20.0


def schedule(steps):
    """Planned dispatch offsets (ms from the start) of every step, and the planned total."""
    due = []
    t = 0.0
    for s in steps:
        due.append(t)
        t += max(0.0, float(s[2]))
    return due, t


def _default_pinned(step):
    # A planner sample is (x, y, sleep_ms); a 4th truthy field marks one that changes button state.
    return len(step) > 3 and bool(step[3])


def next_index(steps, due, i, last, elapsed_ms, max_jump=MAX_JUMP_PX, pinned=_default_pinned):
    """Legacy helper (the pre-SC 299 walk): the planned sample to dispatch next when only whole samples may
    go out. ``walk`` itself sends points between samples (_target); kept for callers outside it."""
    n = len(steps)
    j = i
    while j + 1 < n and due[j + 1] <= elapsed_ms and not pinned(steps[j]):
        j += 1
    if last is not None:
        while j > i and math.hypot(steps[j][0] - last[0], steps[j][1] - last[1]) > max_jump:
            j -= 1
    return j


def _jump_cap(max_jump, dt_ms):
    """The largest hop between two dispatched points ``dt_ms`` apart: ``max_jump``, and IDLE_HOP_PX after
    an idle gap. Never more for a longer gap: a walk that fell behind stretches in time instead of catching
    up with a jump (the planner's own consecutive samples always go out)."""
    return max_jump if dt_ms < IDLE_MS else min(max_jump, IDLE_HOP_PX)


def _pos_at(steps, due, t):
    """The point of the planned path at plan time ``t`` (ms): linear between the two samples around it."""
    if t <= due[0]:
        return steps[0][0], steps[0][1]
    if t >= due[-1]:
        return steps[-1][0], steps[-1][1]
    k = bisect.bisect_right(due, t)        # due[k-1] <= t < due[k]
    a, b = steps[k - 1], steps[k]
    span = due[k] - due[k - 1]
    f = (t - due[k - 1]) / span if span > 0 else 1.0
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f


def _target(steps, due, i, t_plan_last, last, elapsed_ms, cap, pinned):
    """What goes out next when step ``i`` is the first planned sample not yet passed and ``elapsed_ms`` of
    the plan have gone by. Returns (x, y, plan_time, next_i).

    On time: step i itself. Behind: the point of the path at ``elapsed_ms`` -- or, when one is due, the
    first pinned step or the final step, exactly -- pulled back along the path until it lies within ``cap``
    of the last dispatched point (the walk then stretches in time)."""
    n = len(steps)
    if elapsed_ms <= due[i]:
        return steps[i][0], steps[i][1], due[i], i + 1
    j = i
    while j + 1 < n and due[j + 1] <= elapsed_ms and not pinned(steps[j]):
        j += 1
    exact = j == n - 1 or pinned(steps[j])
    if exact:
        x, y, tp = steps[j][0], steps[j][1], due[j]
    else:
        x, y = _pos_at(steps, due, elapsed_ms)
        tp = elapsed_ms
    if last is not None and math.hypot(x - last[0], y - last[1]) > cap:
        lo = t_plan_last if t_plan_last is not None else 0.0
        hi = tp
        for _ in range(24):   # the farthest plan time whose point is within the cap
            mid = (lo + hi) / 2.0
            px, py = _pos_at(steps, due, mid)
            if math.hypot(px - last[0], py - last[1]) <= cap:
                lo = mid
            else:
                hi = mid
        x, y = _pos_at(steps, due, lo)
        tp = lo
        exact = False
        if t_plan_last is not None and tp <= t_plan_last + 1e-9:
            # no progress within the cap (a zero-length planned gap jumps): send step i as planned
            return steps[i][0], steps[i][1], due[i], i + 1
    if exact:
        return x, y, tp, j + 1
    # the next planned sample still ahead of the point just sent (never one already passed)
    return x, y, tp, max(i, bisect.bisect_right(due, tp))


def _walk_state(steps):
    due, total = schedule(steps)
    return due, total


def walk(steps, move, clock=None, sleep=None, max_jump=MAX_JUMP_PX, pinned=_default_pinned, kind="glide",
         buttons=0):
    """Dispatch ``steps`` through ``move(x, y)`` against a wall clock (see module doc). Stops at the
    first ``move`` that raises. Returns the number of points dispatched.

    ``max_jump``: the persona's hop cap (``_motion.hop_cap_px``). ``kind`` names the action for the score
    hook (``_trace``: ``glide`` | ``drag`` | ``ambient`` | ``hover``); with ``CLEARCOTE_HUMANIZE_SCORE`` set, the
    dispatched points and their actual dispatch times are scored and appended to the score file after the
    walk. ``buttons`` (a drag leg: 1) is carried on every recorded sample. Read-only: it changes nothing
    about the walk."""
    clock = clock or time.perf_counter
    sleep = sleep or time.sleep
    if not steps:
        return 0
    trace = [] if _trace.enabled() else None
    due, total = _walk_state(steps)
    t0 = clock()
    t_start = t0
    n = len(steps)
    i = 0
    last = None
    t_plan_last = None
    t_last = t0
    sent = 0
    while i < n:
        el = (clock() - t0) * 1000.0
        if el < due[i]:
            sleep((due[i] - el) / 1000.0)
            el = due[i]
        x, y, tp, nxt = _target(steps, due, i, t_plan_last, last, el,
                                _jump_cap(max_jump, (clock() - t_last) * 1000.0), pinned)
        tb = clock()
        try:
            move(x, y)
        except Exception:  # noqa: BLE001 - page gone / navigating: stop walking
            _trace.record(kind, trace, {"planned_n": n, "sent": sent, "aborted": True}) if trace else None
            return sent
        sent += 1
        if trace is not None:
            trace.append({"x": x, "y": y, "t": (tb - t_start) * 1000.0, "buttons": buttons} if buttons
                         else {"x": x, "y": y, "t": (tb - t_start) * 1000.0})
        t0 += _stall_s(tb, clock())
        last, t_last, t_plan_last = (x, y), tb, tp
        i = nxt
    el = (clock() - t0) * 1000.0
    if el < total:
        sleep((total - el) / 1000.0)   # the last sample's own dwell (a settle hold, a frame)
    if trace:
        _trace.record(kind, trace, {"planned_n": n, "planned_ms": total, "sent": sent})
    return sent


async def walk_async(steps, move, clock=None, sleep=None, max_jump=MAX_JUMP_PX, pinned=_default_pinned, kind="glide",
                     buttons=0):
    """``walk`` for the async API: ``move`` and ``sleep`` are awaited."""
    clock = clock or time.perf_counter
    sleep = sleep or asyncio.sleep
    if not steps:
        return 0
    trace = [] if _trace.enabled() else None
    due, total = _walk_state(steps)
    t0 = clock()
    t_start = t0
    n = len(steps)
    i = 0
    last = None
    t_plan_last = None
    t_last = t0
    sent = 0
    while i < n:
        el = (clock() - t0) * 1000.0
        if el < due[i]:
            await sleep((due[i] - el) / 1000.0)
            el = due[i]
        x, y, tp, nxt = _target(steps, due, i, t_plan_last, last, el,
                                _jump_cap(max_jump, (clock() - t_last) * 1000.0), pinned)
        tb = clock()
        try:
            await move(x, y)
        except Exception:  # noqa: BLE001
            _trace.record(kind, trace, {"planned_n": n, "sent": sent, "aborted": True}) if trace else None
            return sent
        sent += 1
        if trace is not None:
            trace.append({"x": x, "y": y, "t": (tb - t_start) * 1000.0, "buttons": buttons} if buttons
                         else {"x": x, "y": y, "t": (tb - t_start) * 1000.0})
        t0 += _stall_s(tb, clock())
        last, t_last, t_plan_last = (x, y), tb, tp
        i = nxt
    el = (clock() - t0) * 1000.0
    if el < total:
        await sleep((total - el) / 1000.0)
    if trace:
        _trace.record(kind, trace, {"planned_n": n, "planned_ms": total, "sent": sent})
    return sent


# How far inside the viewport a planned path is kept (SC 210 fix 2, verifier B: the planner's overshoot
# arcs left the window near its edges -- x past the right edge, y < 0 -- and the page saw no events for
# 138-383 ms while the pointer was outside).
VIEW_MARGIN_PX = 2.0


def keep_in_view(steps, vw, vh, margin=VIEW_MARGIN_PX):
    """``steps`` with every sample but the last moved inside the viewport ``vw`` x ``vh`` (``margin`` px
    in). The last sample -- where the move was aimed -- is never changed, and a move aimed outside the
    viewport (the caller's own choice) is returned as it is. Only coordinates change: the timing, and
    any further fields of a step, are kept."""
    if not steps or not vw or not vh:
        return steps
    tx, ty = steps[-1][0], steps[-1][1]
    if not (0 <= tx <= vw and 0 <= ty <= vh):
        return steps
    lo_x, hi_x = min(margin, tx), max(vw - margin, tx)
    lo_y, hi_y = min(margin, ty), max(vh - margin, ty)
    out = []
    for k, st in enumerate(steps):
        if k == len(steps) - 1:
            out.append(st)
            continue
        x = min(max(st[0], lo_x), hi_x)
        y = min(max(st[1], lo_y), hi_y)
        out.append(st if (x, y) == (st[0], st[1]) else (x, y) + tuple(st[2:]))
    return out


def _stall_s(tb, ta):
    """Seconds to shift the schedule by after a dispatch that ran from ``tb`` to ``ta``.

    Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping
    coarsened the rest of the glide to maximum hops, and across the fixture's iframe the page saw a
    110 px jump. A single dispatch slower than STALL_MS is a stall: nothing reached the page while it
    lasted, so the plan pauses with it and only STALL_MS is caught up."""
    took = (ta - tb) * 1000.0
    return (took - STALL_MS) / 1000.0 if took > STALL_MS else 0.0


def _speed(steps, k):
    """px/ms arriving at sample k (0 for the first)."""
    if k == 0:
        return 0.0
    a, b = steps[k - 1], steps[k]
    return math.hypot(b[0] - a[0], b[1] - a[1]) / max(1e-3, float(a[2]))


def trim_to(steps, ms, lo=0.85, hi=1.10, stretch=2.0):
    """Cut an ambient plan so it lasts about ``ms``.

    ``plan_ambient`` keeps adding whole excursions + rests until its budget is spent and draws each
    rest sample's gap from 0.9-1.4x the frame time it counts, so its plan runs well past what the
    caller asked for (ambient_motion(1500) planned up to ~2.5 s). Of the samples whose end falls in
    [lo*ms, hi*ms], keep up to the slowest one -- a point inside a rest or the tail of a move. Only a
    tail is dropped; no sample is changed. A plan already no longer than hi*ms is returned whole.

    The one exception to "about ms": when the whole window lies inside the first excursion (a short
    ask, ~300 ms, against a ~0.5-1 s glide) every candidate is mid-flight, and a pointer that stops dead
    at speed is a kinematic no hand produces. Then the cut moves forward to where that move has slowed
    down (<= 10 % of the plan's peak speed), as long as that is within ``stretch`` x ms."""
    if not steps or ms <= 0:
        return steps
    due, total = schedule(steps)
    if total <= hi * ms:
        return steps
    end = [due[k] + max(0.0, float(steps[k][2])) for k in range(len(steps))]
    best, best_v = None, None
    for k in range(len(steps)):
        if end[k] < lo * ms:
            continue
        if end[k] > hi * ms:
            break
        v = _speed(steps, k)
        if best_v is None or v < best_v:
            best, best_v = k, v
    if best is None:
        # One sample spans the whole window (a long settle dwell): cut at the last one before it.
        ends = [k for k in range(len(steps)) if end[k] <= hi * ms]
        best = ends[-1] if ends else 0
    peak = max(_speed(steps, k) for k in range(len(steps)))
    if peak > 0 and _speed(steps, best) > 0.3 * peak:
        # First the rest of the +-20 % the caller was promised, then forward.
        wide = [k for k in range(len(steps)) if 0.8 * ms <= end[k] <= 1.15 * ms]
        if wide:
            k = min(wide, key=lambda k: _speed(steps, k))
            if _speed(steps, k) <= 0.3 * peak:
                return steps[:k + 1]
        for k in range(best + 1, len(steps)):
            if end[k] > stretch * ms:
                break
            if _speed(steps, k) <= 0.1 * peak:
                best = k
                break
    return steps[:best + 1]
