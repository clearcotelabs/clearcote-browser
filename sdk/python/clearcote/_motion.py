"""Human motion core (Python) — the trajectory/timing model shared by the sync and async humanizers.

Faithful port of the Node SDK's ``src/motion.ts``. PURE (no Playwright, no I/O): given a start point,
a target and a seeded :class:`Persona`, it returns a list of ``Step``s (absolute cursor coordinates +
a post-step sleep in ms) that the wrapper walks with the engine's NATIVE mouse.move + an off-protocol
sleep. Keeping the math here unifies sync/async (and mirrors Node) so they can't drift, and makes it
unit-testable offline. See ``motion.ts`` for the full modelling rationale (minimum-jerk submovements,
Fitts duration, colored noise, seeded persona, endpoint dwells).

The RNG (mulberry32 + FNV-1a seed hash) is bit-identical to the Node version, so the seed→persona map
is the SAME across both SDKs (a fingerprint gets one motor identity regardless of language). Trajectory
sampling uses transcendental functions (sqrt/log/sin/cos) whose last-ULP results can differ between V8
and CPython, so per-sample paths are statistically — not byte — identical across languages.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Optional, Union

Rng = Callable[[], float]

_MASK = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    """Emulate JS Math.imul: low 32 bits of the product (unsigned representation)."""
    return ((a & _MASK) * (b & _MASK)) & _MASK


def mulberry32(seed_int: int) -> Rng:
    """Deterministic 0..1 RNG, bit-identical to the Node mulberry32 (unsigned-32 arithmetic)."""
    state = seed_int & _MASK

    def rng() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _MASK
        a = state
        t = _imul(a ^ (a >> 15), 1 | a)
        t = ((t + _imul(t ^ (t >> 7), 61 | t)) & _MASK) ^ t
        t &= _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296.0

    return rng


def _js_round(x: float) -> int:
    """JS Math.round: round half toward +infinity (floor(x+0.5)), not Python's banker's rounding."""
    return math.floor(x + 0.5)


def hash_seed(seed: Optional[Union[str, int, float]]) -> int:
    """FNV-1a (string) / knuth-multiplicative (number) hash to a 32-bit int, matching Node hashSeed."""
    if seed is None:
        return int(random.random() * 0xFFFFFFFF) & _MASK
    if isinstance(seed, (int, float)) and not isinstance(seed, bool) and math.isfinite(seed):
        # Multiply in IEEE double (like JS) then ToUint32 — an exact big-int multiply would diverge.
        return int(float(abs(seed)) * 2654435761.0) & _MASK
    s = str(seed)
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch) & 0xFFFF   # match JS charCodeAt (full UTF-16 code unit), not just the low byte
        h = _imul(h, 0x01000193)
    return h & _MASK


def gauss_from(rng: Rng) -> float:
    """Standard-normal sample (Box–Muller) from a given RNG."""
    u = 0.0
    v = 0.0
    while u == 0.0:
        u = rng()
    while v == 0.0:
        v = rng()
    return math.sqrt(-2.0 * math.log(u)) * math.cos(2.0 * math.pi * v)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Persona:
    """The per-identity motor signature (see motion.ts for field semantics/ranges)."""

    seed: int
    device_hz: float
    tremor_hz: float
    tremor_amp: float
    drift_amp: float
    drift_tau: float
    jitter: float
    fitts_a: float
    fitts_b: float
    primary_frac: float
    overshoot: float
    max_corrections: int
    approach_bias: float
    # The HUMAN BANDS every timing draw is clamped to (ms). The per-identity medians inside them come
    # from the salted timing stream (timing_traits): grab 130-360, release 90-230, click hold 55-230,
    # key dwell 45-120.
    grab_min_ms: float = 130.0
    grab_max_ms: float = 360.0
    release_min_ms: float = 90.0
    release_max_ms: float = 230.0
    click_hold_min_ms: float = 55.0
    click_hold_max_ms: float = 230.0
    key_dwell_min_ms: float = 45.0
    key_dwell_max_ms: float = 120.0


def make_persona(seed: Optional[Union[str, int, float]] = None) -> Persona:
    """Derive a stable motor persona from a fingerprint seed (random when unseeded).

    The RNG draw ORDER mirrors motion.ts exactly (including the short-circuit in the max-corrections
    ternary) so the seed→persona mapping is identical across the two SDKs."""
    seed_int = hash_seed(seed)
    r = mulberry32(seed_int)
    device_hz = _js_round(_lerp(110, 155, r()))
    tremor_hz = _lerp(8, 12, r())
    tremor_amp = _lerp(0.12, 0.5, r())
    drift_amp = _lerp(0.1, 0.4, r())
    drift_tau = _lerp(60, 160, r())
    jitter = _lerp(0.25, 0.7, r())
    fitts_a = _lerp(90, 150, r())
    fitts_b = _lerp(120, 190, r())
    primary_frac = _lerp(0.84, 0.94, r())
    overshoot = _lerp(0.03, 0.08, r())
    # short-circuit ternary: r() < 0.15 ? 3 : (r() < 0.6 ? 2 : 1) — second draw only when first fails
    if r() < 0.15:
        max_corrections = 3
    else:
        max_corrections = 2 if r() < 0.6 else 1
    approach_bias = (r() - 0.5) * 0.5
    return Persona(
        seed=seed_int, device_hz=device_hz, tremor_hz=tremor_hz, tremor_amp=tremor_amp,
        drift_amp=drift_amp, drift_tau=drift_tau, jitter=jitter, fitts_a=fitts_a, fitts_b=fitts_b,
        primary_frac=primary_frac, overshoot=overshoot, max_corrections=max_corrections,
        approach_bias=approach_bias,
    )


# A Step is (x, y, sleep_ms): move the cursor to (x, y), then sleep sleep_ms before the next.
Step = "tuple[float, float, float]"


def min_jerk(tau: float) -> float:
    """Minimum-jerk easing s(τ)=10τ³−15τ⁴+6τ⁵ (speed 30τ²(1−τ)²)."""
    t = _clamp(tau, 0.0, 1.0)
    return t * t * t * (10 + t * (-15 + 6 * t))


def _bez(p0: float, c1: float, c2: float, p1: float, e: float) -> float:
    m = 1 - e
    return m * m * m * p0 + 3 * m * m * e * c1 + 3 * m * e * e * c2 + e * e * e * p1


def _log2(x: float) -> float:
    return math.log(x) / math.log(2.0)


# Salt for the path-shape stream. Like the rollover salt, a separate mulberry32(seed ^ salt) stream
# keeps make_persona's draw ORDER (and so every existing seed's motor identity) unchanged.
_SHAPE_SALT = 0x85EBCA6B


@dataclass(frozen=True)
class ShapeTraits:
    """Per-identity path-shape traits, stable per seed (see shape_traits)."""

    overshoot_rate: float   # share of aimed moves that pass the target and come back, 0.15..0.38
    curve: float            # scale on the primary submovement's bow, 0.75..1.3
    drag_tempo: float       # scale on held-drag duration, 0.85..1.2


def shape_traits(p: Persona) -> ShapeTraits:
    """The persona's path-shape traits, drawn from a salted stream of its seed.

    Measured on r38 / SDK 0.48.0 (SC 209): 1 of 36 planned approaches passed the target, against
    15-40 % for real reaches and 14/36 for the engine's recorded trajectories, because the primary
    submovement always aimed at 84-94 % of the distance. Same values in motion.ts shapeTraits and
    Motion.ShapeTraitsOf."""
    r = mulberry32((p.seed ^ _SHAPE_SALT) & _MASK)
    overshoot_rate = _lerp(0.15, 0.38, r())
    curve = _lerp(0.75, 1.3, r())
    drag_tempo = _lerp(0.85, 1.2, r())
    return ShapeTraits(overshoot_rate=overshoot_rate, curve=curve, drag_tempo=drag_tempo)


# Sub-pixel noise scale. The colored noise (OU drift + 8-12 Hz tremor + AR(1) jitter) stays, but
# smaller and carried mostly while the hand is moving: a mouse resting on the pad reports no counts,
# so a slow tail full of +-0.5 px wobble is what a quantizing page turns into 1 px zigzags.
_NOISE = 0.6


def _skewed(tau: float, skew: float) -> float:
    """Time warp 1-(1-tau)^skew: skew > 1 moves the min-jerk speed peak before the midpoint."""
    t = _clamp(tau, 0.0, 1.0)
    return 1.0 - math.pow(1.0 - t, skew) if skew != 1.0 else t


def _sample_submove(out, a, b, dur_ms, p: Persona, rng: Rng, t0, drift, jit, tremor_phase, land,
                    bow_frac, skew):
    """Sample ONE min-jerk submovement a->b along a bowed path with colored noise. Appends
    (x, y, sleep_ms) tuples to ``out``. Returns the absolute clock (ms) at the end.

    ``bow_frac``: signed peak sideways deviation as a share of the submovement's length.
    ``skew``: time warp of the min-jerk profile (1 = symmetric)."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    dist = math.hypot(dx, dy) or 1e-6
    step_ms = 1000.0 / p.device_hz
    # Cap the sample count so a very long move can't emit thousands of steps (defends the Fitts cap).
    n = int(min(180, max(4, _js_round(dur_ms / step_ms))))
    nx, ny = -dy / dist, dx / dist
    # A cubic with both control points offset by c peaks at 0.75 c; the first control point carries
    # more of it, so the widest point of the arc comes before the middle, as in a reach.
    c = bow_frac * dist / 0.75
    c1x, c1y = ax + dx * 0.3 + nx * c * 1.15, ay + dy * 0.3 + ny * c * 1.15
    c2x, c2y = ax + dx * 0.7 + nx * c * 0.85, ay + dy * 0.7 + ny * c * 0.85
    t = t0
    # Each sample's POSITION is taken at its own report time (SC 299): the report intervals jitter
    # (step_ms x 0.88-1.12, drawn at the end of each iteration as before), and a position sampled at the
    # nominal i/n while its time jittered made the speed a page computes swing +-12 % sample to sample --
    # 5 smoothed velocity peaks per planned glide (SC 298). `el` is the time of sample i since the
    # submovement started; the hand has arrived once el reaches the nominal duration.
    total = n * step_ms
    el = step_ms
    for i in range(1, n + 1):
        tau = 1.0 if i == n else min(1.0, el / total)
        e = min_jerk(_skewed(tau, skew))
        px = _bez(ax, c1x, c2x, bx, e)
        py = _bez(ay, c1y, c2y, by, e)
        last = i == n
        if not (last and land):
            # noise follows the speed envelope (min-jerk speed 30 tau^2 (1-tau)^2 peaks at 1.875)
            v = 30.0 * tau * tau * (1 - tau) * (1 - tau) / 1.875
            g = _NOISE * (0.35 + 0.65 * v)
            k = step_ms / p.drift_tau
            drift[0] += -k * drift[0] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            drift[1] += -k * drift[1] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            # 8-12 Hz tremor: absolute-clock phase (continuous across submovements) + slow amplitude
            # breathing + a slightly DETUNED/phase-offset y axis (not a fixed-amplitude 90-deg-locked tone).
            t_amp = p.tremor_amp * (0.7 + 0.3 * math.sin((2 * math.pi * t) / 850.0 + tremor_phase))
            tr_x = t_amp * math.sin(tremor_phase + (2 * math.pi * p.tremor_hz * t) / 1000.0)
            tr_y = t_amp * math.sin(tremor_phase + 1.0 + (2 * math.pi * (p.tremor_hz * 0.93) * t) / 1000.0)
            # Sub-pixel jitter as a COLORED AR(1) process, not i.i.d. white noise (white noise adds a
            # fresh speed reversal every sample -> spurious "submovements" + flat noise spectrum).
            jit[0] = 0.7 * jit[0] + gauss_from(rng) * p.jitter * 0.22
            jit[1] = 0.7 * jit[1] + gauss_from(rng) * p.jitter * 0.22
            px += (drift[0] + tr_x + jit[0]) * g
            py += (drift[1] + tr_y + jit[1]) * g
        else:
            px, py = bx, by
        sleep_ms = step_ms * _lerp(0.88, 1.12, rng())
        out.append((px, py, sleep_ms))
        t += sleep_ms
        el += sleep_ms
    return t


def plan_move(frm, to, p: Persona, target_w: float = 24.0, settle: bool = False, rng: Optional[Rng] = None,
              held: bool = False):
    """Plan a free cursor move frm->to as a sum of minimum-jerk submovements (primary + corrections),
    Fitts-scaled duration and colored noise. Returns a list of (x, y, sleep_ms) tuples.

    A move with a button held (``held``) or one that seats a handle (``settle``) is a drag, planned
    by :func:`plan_drag`.

    Shape (SC 209/210): the primary submovement arcs (peak sideways deviation ~6-18 % of the
    distance, per-persona curvature and handedness), reaches its speed peak before the middle of the
    move, and either stops short of the target (a short corrective submovement closes the gap) or,
    in ``shape_traits(p).overshoot_rate`` of moves, passes it by 3-28 px and comes back. Straightness
    (distance / path length) of the planned points lands around 0.85-0.95, the band of real reaches."""
    if settle or held:
        return plan_drag(frm, to, p, settle=settle, rng=rng)
    if rng is None:
        rng = random.random
    fx, fy = frm
    tx, ty = to
    w = max(6.0, target_w)
    d = math.hypot(tx - fx, ty - fy)
    out: list = []
    if d < 1.5:
        out.append((tx, ty, 1000.0 / p.device_hz))
        return out

    idv = _log2(d / w + 1)
    # A distance floor (an average-speed bound) capped at the 1700 ms ceiling so a very long move can't invert
    # the clamp (lo > hi). 0.4 ms/px (was 0.6, SC 299): at 0.6 the floor overrode Fitts' law whenever a far
    # target was also wide, so planned time tracked DISTANCE instead of difficulty -- on SC 298's six targets
    # (ID 2.5-4.6) the planned MT/ID correlation was -0.14 median over 8 personas; 0.58 at 0.4, with the
    # fastest far reach (3300 px) still under 8.3 px/ms at its peak.
    mt_lo = min(1700.0, max(70.0, 40.0 + d * 0.4))
    mt = _clamp((p.fitts_a + p.fitts_b * idv) * (0.85 + 0.3 * rng()), mt_lo, 1700.0)
    tr = shape_traits(p)
    ux, uy = (tx - fx) / d, (ty - fy) / d

    targets = []
    over = False
    if d >= 40:
        over = rng() < tr.overshoot_rate
        if over:
            along = d + _clamp(d * _lerp(0.03, 0.08, rng()), 3.0, 28.0)
        else:
            along = d * _clamp(p.primary_frac + 0.05 + gauss_from(rng) * 0.015, 0.88, 0.985)
        lat = gauss_from(rng) * min(6.0, d * 0.02)
        targets.append((fx + ux * along - uy * lat, fy + uy * along + ux * lat))
        if rng() < 0.08 * p.max_corrections:
            cur = targets[0]
            close = _lerp(0.55, 0.8, rng())
            targets.append((cur[0] + (tx - cur[0]) * close + gauss_from(rng) * 1.0,
                            cur[1] + (ty - cur[1]) * close + gauss_from(rng) * 1.0))
    targets.append((tx, ty))

    seg_dur = []
    if len(targets) == 1:
        seg_dur.append(mt)
    else:
        seg_dur.append(mt * 0.78)
        rest = mt * 0.22
        nc = len(targets) - 1
        for _ in range(nc):
            seg_dur.append((rest / nc) * _lerp(0.8, 1.2, rng()))

    # The primary's arc: handedness sign from the persona (1 move in 5 bends the other way), size
    # from the persona's curvature trait. Corrections are short and nearly straight.
    sign = 1.0 if p.approach_bias >= 0 else -1.0
    if rng() < 0.2:
        sign = -sign
    bow = sign * min(0.25, (0.08 + abs(gauss_from(rng)) * 0.11) * tr.curve * (0.75 + abs(p.approach_bias)))
    if d * abs(bow) > 110.0:
        bow = math.copysign(110.0 / d, bow)

    drift = [0.0, 0.0]
    jit = [0.0, 0.0]
    tremor_phase = rng() * 2 * math.pi
    a = frm
    t = 0.0
    for s in range(len(targets)):
        b = targets[s]
        last = s == len(targets) - 1
        first = s == 0
        t = _sample_submove(out, a, b, max(20.0, seg_dur[s]), p, rng, t, drift, jit, tremor_phase, last,
                            bow if first else bow * 0.15, 1.25 if first else 1.0)
        if not last and out:
            x, y, sl = out[-1]
            # motor re-planning gap; a reversal after passing the target comes back quicker
            out[-1] = (x, y, sl + (_lerp(25, 90, rng()) if (over and first) else _lerp(40, 120, rng())))
        a = b
    return out


def plan_drag(frm, to, p: Persona, settle: bool = False, rng: Optional[Rng] = None):
    """Plan a held-button drag frm->to (slider handle, drag-and-drop, range thumb).

    Measured on r38 / SDK 0.48.0 (SC 209): the held leg reused the aimed-move planner, whose arc
    put the hand up to 47 px off the line between grab and drop -- off a 40 px slider track. A drag
    is a slower, flatter movement: one skewed min-jerk leg along the line with at most ~4 px of arc
    and +-6 px of total sideways error, 0.5-2 s for 300-400 px (``shape_traits(p).drag_tempo``),
    and now and then (about 0.6 x the persona's overshoot rate) a 2.5-10 px overshoot and pull-back.
    ``settle`` adds a couple of sub-pixel seating jiggles before landing exactly on ``to``."""
    if rng is None:
        rng = random.random
    fx, fy = frm
    tx, ty = to
    d = math.hypot(tx - fx, ty - fy)
    out: list = []
    step_ms = 1000.0 / p.device_hz
    if d < 1.5:
        out.append((tx, ty, step_ms))
        return out
    tr = shape_traits(p)
    ux, uy = (tx - fx) / d, (ty - fy) / d
    mt = _clamp((260.0 + 1.7 * d) * tr.drag_tempo * _lerp(0.9, 1.1, rng()), min(500.0, 150.0 + 1.5 * d), 2000.0)
    over = d >= 60 and rng() < tr.overshoot_rate * 0.6
    o = _clamp(d * _lerp(0.01, 0.03, rng()), 2.5, 10.0) if over else 0.0
    sign = 1.0 if p.approach_bias >= 0 else -1.0
    h = sign * min(5.0, d * 0.015) * _lerp(0.3, 1.0, rng())
    tremor_phase = rng() * 2 * math.pi

    legs = [(0.0, d + o, mt * (0.86 if over else 1.0), h)]
    if over:
        legs.append((d + o, d, mt * 0.14, 0.0))
    drift = 0.0
    jit = 0.0
    t = 0.0
    for li, (s0, s1, dur, hh) in enumerate(legs):
        n = int(min(360, max(4, _js_round(dur / step_ms))))
        dt = dur / n
        last_leg = li == len(legs) - 1
        for i in range(1, n + 1):
            tau = i / n
            e = min_jerk(_skewed(tau, 1.2))
            along = s0 + (s1 - s0) * e
            v = 30.0 * tau * tau * (1 - tau) * (1 - tau) / 1.875
            k = dt / p.drift_tau
            drift += -k * drift + math.sqrt(2 * k) * p.drift_amp * 0.8 * gauss_from(rng)
            jit = 0.7 * jit + gauss_from(rng) * p.jitter * 0.12
            t_amp = p.tremor_amp * (0.7 + 0.3 * math.sin((2 * math.pi * t) / 850.0 + tremor_phase))
            tremor = t_amp * math.sin(tremor_phase + (2 * math.pi * p.tremor_hz * t) / 1000.0)
            along += t_amp * 0.6 * math.sin(tremor_phase + 1.0 + (2 * math.pi * (p.tremor_hz * 0.93) * t) / 1000.0) * v
            side = hh * math.sin(math.pi * e) + (drift + tremor + jit) * (0.6 + 0.4 * v)
            side = _clamp(side, -6.0, 6.0)
            if last_leg and i == n and not settle:
                px, py = tx, ty
            else:
                px, py = fx + ux * along - uy * side, fy + uy * along + ux * side
            sl = dt * _lerp(0.88, 1.12, rng())
            out.append((px, py, sl))
            t += sl
        if not last_leg and out:
            x, y, sl = out[-1]
            out[-1] = (x, y, sl + _lerp(30, 80, rng()))   # the hand notices, stops, pulls back

    if settle:
        # A couple of tiny sub-pixel seating jiggles, then land exactly (handle "clunk" into place).
        jig = 1 + int(rng() * 2)
        for _ in range(jig):
            out.append((tx + _clamp(gauss_from(rng) * 0.8, -1.5, 1.5), ty + _clamp(gauss_from(rng) * 0.8, -1.5, 1.5),
                        _lerp(30, 90, rng())))
        out.append((tx, ty, step_ms))
    return out


# Salt for the timing stream. Like the rollover and shape salts, a separate mulberry32(seed ^ salt)
# stream keeps make_persona's draw ORDER (and so every existing seed's motor identity) unchanged.
_TIMING_SALT = 0xC2B2AE35


@dataclass(frozen=True)
class TimingTraits:
    """Per-identity timing medians (ms) and tempo, stable per seed (see timing_traits)."""

    click_hold_ms: float     # median mousedown->mouseup hold, 70..140
    key_dwell_ms: float      # median keydown->keyup dwell, 60..95
    grab_ms: float           # median grab hesitation after a drag's mousedown, 180..280
    release_ms: float        # median settle before a drag's mouseup, 120..180
    dblclick_gap_ms: float   # median gap between the two presses of a double click, 90..140
    pause_scale: float       # scale on deliberation pauses (select popup, datetime segments), 0.8..1.25
    typo_rate: float         # per-character fat-finger rate, 0.005..0.04


def timing_traits(p: Persona) -> TimingTraits:
    """The persona's timing traits, drawn from a salted stream of its seed.

    Before this every identity shared ONE fixed window for each timing (click hold 60-150, key dwell
    45-120, grab 130-360, release 90-230, double-click gap 70-170, the select popup pauses, the
    datetime gaps, the 2 % typo rate), so two personas were told apart by nothing in their timing.
    Each draw below is the identity's MEDIAN; the samples around it are log-normal (click_hold,
    key_dwell, drag_dwell, dblclick_gap) and clamped to the human band on the Persona. Same values in
    motion.ts timingTraits and Motion.TimingTraitsOf."""
    r = mulberry32((p.seed ^ _TIMING_SALT) & _MASK)
    click_hold_ms = _lerp(70, 140, r())
    key_dwell_ms = _lerp(60, 95, r())
    grab_ms = _lerp(180, 280, r())
    release_ms = _lerp(120, 180, r())
    dblclick_gap_ms = _lerp(90, 140, r())
    pause_scale = _lerp(0.8, 1.25, r())
    typo_rate = _lerp(0.005, 0.04, r())
    return TimingTraits(click_hold_ms=click_hold_ms, key_dwell_ms=key_dwell_ms, grab_ms=grab_ms,
                        release_ms=release_ms, dblclick_gap_ms=dblclick_gap_ms, pause_scale=pause_scale,
                        typo_rate=typo_rate)


def _tempo(p) -> float:
    """The preset's tempo on ``p`` (HumanizeOptions: careful 1.4 / default 1.0 / quick 0.7, set by
    _hzoptions.apply_to_persona after make_persona; 1.0 on a bare persona, so every vector is unchanged)."""
    t = getattr(p, "tempo", 1.0)
    return float(t) if isinstance(t, (int, float)) and not isinstance(t, bool) and t > 0 else 1.0


def lognormal_ms(median: float, sigma: float, lo: float, hi: float, rng: Rng) -> float:
    """``median * exp(sigma * N(0,1))`` clamped to [lo, hi]: a right-skewed human timing around a
    per-identity median (the shape of measured hold and dwell distributions)."""
    return _clamp(median * math.exp(sigma * gauss_from(rng)), lo, hi)
# Salt for the drag-start hesitation traits (per persona, like _SHAPE_SALT / _ROLLOVER_SALT): drawing
# them from mulberry32(seed ^ salt) keeps make_persona's draw ORDER, so every seed's motor identity is
# what it was, and plan_drag itself is untouched (its vectors still pin the three SDKs).
_HESITATION_SALT = 0xFD7046C5   # own stream (was the timing salt: hesitation moved with the click hold)
# A hand that has just closed on a handle does not set off at once: it holds 100-300 ms, creeping a
# few px (never 8) in the direction it is about to go, before the leg proper. Measured on r38 / SDK
# 0.51.0 (SC 232 review): every drag's first held move was already the full-speed leg start.
HESITATION_MS = (100.0, 300.0)
HESITATION_MAX_PX = 8.0


def hesitation_traits(p: Persona):
    """This persona's hesitation TRAITS, from its salted stream: (typical length ms 130-260, creep reach
    px 2-6, sideways wobble px 0.4-1.2). Only traits come from the seed; every per-drag draw in
    :func:`drag_hesitation` is unseeded (``random.random``) unless a test passes ``rng``, so two sessions of
    one identity do not replay identical drags."""
    r = mulberry32((p.seed ^ _HESITATION_SALT) & _MASK)
    return _lerp(130.0, 260.0, r()), _lerp(2.0, 6.0, r()), _lerp(0.4, 1.2, r())


def drag_hesitation(frm, to, p: Persona, rng: Optional[Rng] = None, dur_ms: Optional[float] = None):
    """Plan the still-but-not-frozen moment between the grab and the drag leg frm->to: (x, y, sleep_ms)
    samples spanning 100-300 ms (centred per persona), the pointer creeping < 8 px toward ``to`` with a
    little sideways wobble. The last sample is where :func:`plan_drag` then starts. The samples are
    drawn from ``rng`` AFTER nothing: a caller plans the leg from the returned end point, so the leg's
    own draws keep their order. ``dur_ms``: the caller's own length for the creep (40-300 ms; see
    :func:`hesitate_then_drag`), in place of this persona's draw (which is still drawn, so the order holds).
    Same numbers in motion.ts dragHesitation and Motion.DragHesitation."""
    if rng is None:
        rng = random.random
    centre, amp, wobble = hesitation_traits(p)
    fx, fy = frm
    tx, ty = to
    d = math.hypot(tx - fx, ty - fy)
    ux, uy = ((tx - fx) / d, (ty - fy) / d) if d >= 1e-6 else (1.0, 0.0)
    dur = _clamp(centre * _lerp(0.8, 1.2, rng()), HESITATION_MS[0], HESITATION_MS[1])
    if dur_ms is not None:
        dur = _clamp(float(dur_ms), 40.0, HESITATION_MS[1])
    reach = amp * _lerp(0.6, 1.0, rng())
    step_ms = 1000.0 / p.device_hz
    n = int(max(3, _js_round(dur / step_ms)))
    dt = dur / n
    phase = rng() * 2 * math.pi
    out: list = []
    side = 0.0
    for i in range(1, n + 1):
        tau = i / n
        along = reach * min_jerk(tau)
        side = 0.8 * side + gauss_from(rng) * wobble * 0.25
        s = _clamp(side + wobble * 0.5 * math.sin(phase + 2 * math.pi * tau), -1.5, 1.5)
        px, py = fx + ux * along - uy * s, fy + uy * along + ux * s
        # the budget is a hard bound: the creep never leaves an 8 px circle around the grab
        if math.hypot(px - fx, py - fy) >= HESITATION_MAX_PX - 0.5:
            px, py = fx + ux * (HESITATION_MAX_PX - 1.0), fy + uy * (HESITATION_MAX_PX - 1.0)
        out.append((px, py, dt * _lerp(0.9, 1.1, rng())))
    return out


def leg_exit_ms(frm, steps, radius=HESITATION_MAX_PX):
    """When (ms from the start of ``steps``) the walk first leaves the ``radius`` circle around ``frm``;
    the whole length when it never does."""
    t = 0.0
    for x, y, sl in steps:
        if math.hypot(x - frm[0], y - frm[1]) >= radius:
            return t
        t += sl
    return t


def hesitate_then_drag(frm, to, p: Persona, window_ms: float, settle: bool = True, rng: Optional[Rng] = None):
    """The held part of a drag: the hesitation creep, then the :func:`plan_drag` leg, planned together
    so the pointer stays inside 8 px of the press for ``window_ms`` (clamped 100-300; the persona's grab
    dwell) and then goes. The leg's own slow start counts toward the window: the creep is planned for
    what the window leaves (at least 40 ms). Returns (steps, creep_count)."""
    if rng is None:
        rng = random.random
    w = _clamp(float(window_ms), HESITATION_MS[0], HESITATION_MS[1])
    creep = drag_hesitation(frm, to, p, rng, dur_ms=w)
    leg = plan_drag(creep[-1][:2], to, p, settle=settle, rng=rng)
    t8 = leg_exit_ms(frm, leg)
    want = max(40.0, w - t8)
    have = sum(s[2] for s in creep)
    if abs(have - want) > 1.0:
        # re-time the creep to what the window leaves (same shape, scaled in time and reach)
        k = want / have
        creep = [(frm[0] + (x - frm[0]) * min(1.0, k), frm[1] + (y - frm[1]) * min(1.0, k), sl * k)
                 for x, y, sl in creep]
        leg = plan_drag(creep[-1][:2], to, p, settle=settle, rng=rng)
    return creep + leg, len(creep)


def wheel_anchor_point(vw: float, vh: float, rng: Optional[Rng] = None):
    """Where a reading hand parks the pointer before a wheel: x ~ 0.5w +- 0.10w clamped to [0.12w, 0.88w],
    y ~ 0.34h +- 0.09h clamped to [0.10h, 0.62h] (two gaussians from ``rng``, x first). One model in all
    three SDKs (motion.ts wheelAnchorPoint, Motion.WheelAnchorPoint / Scroll.WheelAnchorPoint); before 0.52
    Node and .NET used other centres, spreads and clamps. _scroll.anchor_point is this function."""
    if rng is None:
        rng = random.random
    x = _clamp(vw * 0.5 + gauss_from(rng) * vw * 0.10, vw * 0.12, vw * 0.88)
    y = _clamp(vh * 0.34 + gauss_from(rng) * vh * 0.09, vh * 0.10, vh * 0.62)
    return x, y


def drag_dwell(p: Persona, rng: Optional[Rng] = None):
    """(grab_ms, release_ms) for a held-button drag: log-normal (sigma 0.2) around the persona's
    medians, inside its grab/release bands."""
    if rng is None:
        rng = random.random
    tt = timing_traits(p)
    t = _tempo(p)   # the preset scales the median; apply_to_persona already scaled the bands
    return (lognormal_ms(tt.grab_ms * t, 0.2, p.grab_min_ms, p.grab_max_ms, rng),
            lognormal_ms(tt.release_ms * t, 0.2, p.release_min_ms, p.release_max_ms, rng))


def click_hold(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human mousedown->mouseup click hold (ms): log-normal (sigma 0.25) around the persona's own
    median (70-140 ms), clamped to its band (55-230 ms). Fixes the ~2 ms instant-press tell, and the
    one-window-for-everyone tell that replaced it."""
    if rng is None:
        rng = random.random
    return lognormal_ms(timing_traits(p).click_hold_ms * _tempo(p), 0.25, p.click_hold_min_ms, p.click_hold_max_ms, rng)


def key_dwell(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human per-key keydown->keyup dwell (ms): log-normal (sigma 0.22) around the persona's own
    median (60-95 ms), clamped to its band (45-120 ms)."""
    if rng is None:
        rng = random.random
    return lognormal_ms(timing_traits(p).key_dwell_ms * _tempo(p), 0.22, p.key_dwell_min_ms, p.key_dwell_max_ms, rng)


def dblclick_gap(p: Persona, rng: Optional[Rng] = None) -> float:
    """The gap between the two presses of a double click (ms): log-normal (sigma 0.15) around the
    persona's median (90-140 ms), clamped to 70-170 ms (the band the engine's clickCount:2 walk uses)."""
    if rng is None:
        rng = random.random
    t = _tempo(p)
    return lognormal_ms(timing_traits(p).dblclick_gap_ms * t, 0.15, 70.0 * t, 170.0 * t, rng)


def pause_ms(p: Optional[Persona], lo: float, hi: float, rng: Optional[Rng] = None) -> float:
    """A deliberation pause (ms) from the window [lo, hi], scaled by the persona's tempo
    (timing_traits.pause_scale, 0.8-1.25). Without a persona: the plain window."""
    if rng is None:
        rng = random.random
    v = _lerp(lo, hi, rng())
    return v * timing_traits(p).pause_scale * _tempo(p) if p is not None else v


# Salt for the rollover stream. Drawing from mulberry32(seed ^ salt) instead of adding a field to
# make_persona keeps the persona draw ORDER (and so every existing seed's motor identity) unchanged.
_ROLLOVER_SALT = 0x9E3779B9


def rollover_rate(p: Persona) -> float:
    """Share of eligible key pairs this persona types with ROLLOVER: the next key goes down before
    the previous one comes up, the way people type fast common pairs. Stable per seed, 0.12..0.38.

    Measured on r32/r33 before this existed: 0 overlapping keydowns in 30-32 typed keys, because
    every character was one press() (down, dwell, up) before the next began.
    Same value in motion.ts rolloverRate and Motion.RolloverRate."""
    return _lerp(0.12, 0.38, mulberry32((p.seed ^ _ROLLOVER_SALT) & _MASK)())


# Pixels one wheel notch scrolls, by the platform the persona claims. A wheel event sent over CDP
# always carries wheelDelta +-120 (input_handler.cc sets wheel_ticks to +-1 whatever the delta), and
# a real notch is the only event whose distance goes with a 120 wheelDelta:
#   windows 100 = 3 lines x 100/3 px (web_input_event_builders_win.cc, default 3 lines per notch)
#   linux   120 = ui::MouseWheelEvent::kWheelDelta (event.cc); measured 120/-120 per X11 notch
#   macos    40 = kScrollbarPixelsPerCocoaTick (cocoa_event_utils.h)
# android is not measured; it gets the windows value.
WHEEL_NOTCH_PX = {"windows": 100, "linux": 120, "macos": 40, "android": 100}


def wheel_notches(delta: float, notch_px: float) -> int:
    """Whole notches for a requested scroll distance: the nearest count, at least one when the
    caller asked for any scroll at all. A wheel cannot move 39 px; it moves notches."""
    if not delta:
        return 0
    return max(1, _js_round(abs(delta) / notch_px))


def platform_from_navigator(value: Optional[str]) -> Optional[str]:
    """Map navigator.userAgentData.platform / navigator.platform to a WHEEL_NOTCH_PX key."""
    v = (value or "").lower()
    if not v:
        return None
    if "android" in v:   # before linux: Android's navigator.platform is "Linux armv8l"
        return "android"
    if "win" in v:
        return "windows"
    if "mac" in v:
        return "macos"
    if "linux" in v or "x11" in v or "cros" in v:
        return "linux"
    return None


# Salt for the aim traits (click_point): the persona's landing dispersion, undershoot and handedness.
_PACE_SALT = 0x5BD1E995   # own stream: the pacer's hop cap (SC 299)


def hop_cap_px(p: Persona) -> float:
    """The longest hop this hand makes between two dispatched pointer points, 55..110 px (the first draw of its
    salted pace stream): what a quick reach covers in about one 60 Hz frame at a 3.5-7 px/ms peak. The wall-clock
    pacer (_pacing.walk) never hops further; a late walk stretches in time instead.

    Measured on r54 / SDK 0.67.1 (SC 298): a fixed 40 px cap put a wall at 40 px under every persona (0 of 4,462
    page-visible steps above 40.5 px), the same for every identity. Same value in motion.ts hopCapPx and
    Motion.HopCapPx."""
    r = mulberry32((p.seed ^ _PACE_SALT) & _MASK)
    return _lerp(55.0, 110.0, r())


_AIM_SALT = 0x27D4EB2F


@dataclass(frozen=True)
class AimTraits:
    """Per-identity aiming traits, stable per seed (see aim_traits)."""

    spread: float       # landing dispersion, sd as a share of the box, 0.10..0.20
    undershoot: float   # pull toward the side the cursor came from, share of the box, 0.06..0.16
    side_bias: float    # handedness: lateral offset as a share of the box width, -0.1..0.1 (approach_bias * 0.4)
    label_share: float  # share of visible checkbox / radio clicks that land on the label text, 0.25..0.55


def aim_traits(p: Persona) -> AimTraits:
    """The persona's aiming traits, drawn from a salted stream of its seed. Same values in motion.ts
    aimTraits and Motion.AimTraitsOf."""
    r = mulberry32((p.seed ^ _AIM_SALT) & _MASK)
    spread = _lerp(0.10, 0.20, r())
    undershoot = _lerp(0.06, 0.16, r())
    label_share = _lerp(0.25, 0.55, r())   # SC 232 integration: lane F's label aim, a trait (was one 0.4 for all)
    return AimTraits(spread=spread, undershoot=undershoot, side_bias=p.approach_bias * 0.4, label_share=label_share)


def click_point(box, frm, p: Optional[Persona], rng: Optional[Rng] = None):
    """A human click point inside ``box`` (dict with x/y/width/height): a 2D gaussian toward the
    centre with the persona's dispersion (sd 0.10-0.20 of the box), nudged toward the approach side
    (undershoot, 6-16 %) and sideways by its handedness (approach_bias), clamped a couple px inside
    the edges. The SEED fixes the persona's spread, undershoot and handedness (it was one dispersion
    for everyone); each click's draw is unseeded (random.random unless ``rng`` is given), like every
    per-action draw, so no two visits of one identity replay the same landing sequence.
    Without a persona: sd 0.15, 12 % undershoot, no handedness."""
    if rng is None:
        rng = random.random
    if p is None:
        spread, under, side = 0.15, 0.12, 0.0
    else:
        at = aim_traits(p)
        spread, under, side = at.spread, at.undershoot, at.side_bias
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    dx, dy = cx - frm[0], cy - frm[1]
    d = math.hypot(dx, dy) or 1.0
    ux, uy = -dx / d, -dy / d
    x = cx + ux * box["width"] * under + side * box["width"] + gauss_from(rng) * box["width"] * spread
    y = cy + uy * box["height"] * under + gauss_from(rng) * box["height"] * spread
    # For a box narrower/shorter than the 4px inset the clamp bounds invert (lo > hi) and would return
    # a point OUTSIDE the element -- use the center for such tiny targets.
    return (_clamp(x, box["x"] + 2, box["x"] + box["width"] - 2) if box["width"] >= 4 else cx,
            _clamp(y, box["y"] + 2, box["y"] + box["height"] - 2) if box["height"] >= 4 else cy)


def plan_ambient(frm, viewport, p: Persona, ms: float, rng: Optional[Rng] = None):
    """Plan ambient / pre-challenge cursor activity (idle drift + non-goal moves within the viewport)
    so a behavioral collector sees non-zero pointer entropy before the first goal action. ~``ms`` long."""
    if rng is None:
        rng = random.random
    out: list = []
    cur = frm
    elapsed = 0.0
    budget = max(200.0, ms)
    vw, vh = viewport["width"], viewport["height"]
    bx0, bx1 = vw * 0.12, vw * 0.88
    by0, by1 = vh * 0.14, vh * 0.8
    while elapsed < budget and len(out) < 4000:
        target = (_lerp(bx0, bx1, rng()), _lerp(by0, by1, rng()))
        for s in plan_move(cur, target, p, target_w=80, rng=rng):
            out.append(s)
            elapsed += s[2]
        rest_ms = _lerp(120, 600, rng())
        drift = [0.0, 0.0]
        step_ms = 1000.0 / p.device_hz
        held = 0.0
        tphase = rng() * 2 * math.pi
        while held < rest_ms:
            k = step_ms / p.drift_tau
            drift[0] += -k * drift[0] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            drift[1] += -k * drift[1] + math.sqrt(2 * k) * p.drift_amp * gauss_from(rng)
            ph = tphase + (2 * math.pi * p.tremor_hz * held) / 1000.0
            out.append((target[0] + drift[0] + p.tremor_amp * math.sin(ph),
                        target[1] + drift[1] + p.tremor_amp * math.sin(ph + math.pi / 2),
                        step_ms * _lerp(0.9, 1.4, rng())))
            held += step_ms
            elapsed += step_ms
        cur = target
    return out


# ------------------------------------------------------------------ after-action (SC 232 lane C)
# Salt for the after-action stream (hover dwell, move-off after a click, the attention model). Like the
# rollover and shape salts, a separate mulberry32(seed ^ salt) stream keeps make_persona's draw ORDER
# (and so every existing seed's motor identity) unchanged. Same values in motion.ts / Motion.cs.
_AFTER_SALT = 0x165667B1   # own stream (was the timing salt: dwell moved with the click hold)

# The pointer keeps within this many px of where a hover arrived while it dwells there.
DWELL_RADIUS_PX = 3.0
# A move-off after a click is kept this far inside the viewport.
MOVE_OFF_MARGIN_PX = 8.0


@dataclass(frozen=True)
class AfterTraits:
    """Per-identity after-action traits, stable per seed (see after_traits)."""

    dwell_ms: float          # centre of this hand's hover dwell, 250..700 (draws stay in 150..900)
    move_off_rate: float     # share of clicks followed by a move-off, 0.64..0.82 (a 40-click run stays in 60-85 %)
    move_off_delay_ms: float  # centre of the release -> move-off gap, 300..600 (draws stay in 200..700)
    read_tempo: float        # scale on the attention model's read pause, 0.85..1.15


def after_traits(p: Persona) -> AfterTraits:
    """The persona's after-action traits, the first four draws of its salted after-action stream.

    Measured on r42 / SDK 0.51.0 (SC 232 review): a hover stopped dead on arrival (0 samples after the
    last glide sample) and the pointer never left the pressed pixel after a click, both tells against
    a hand that keeps breathing on the mouse. Same values in motion.ts afterTraits and
    Motion.AfterTraitsOf."""
    r = mulberry32((p.seed ^ _AFTER_SALT) & _MASK)
    return AfterTraits(dwell_ms=_lerp(250, 700, r()), move_off_rate=_lerp(0.64, 0.82, r()),
                       move_off_delay_ms=_lerp(300, 600, r()), read_tempo=_lerp(0.85, 1.15, r()))


def after_rng(p: Persona) -> Rng:
    """The persona's after-action stream positioned AFTER its four trait draws: the per-action draws
    (a dwell's length, a move-off's roll / angle / distance / delay, the attention model's pauses) come
    from here in the shared vectors (sdk/testdata/humanize_after_vectors.json) so the three SDKs can be compared
    draw for draw. The humanizers do NOT use it: the seed fixes traits only, and every per-action draw is
    unseeded (random.random), as in plan_move -- a seeded per-action sequence would replay identically in
    every session of an identity, a cross-session fingerprint."""
    r = mulberry32((p.seed ^ _AFTER_SALT) & _MASK)
    for _ in range(4):
        r()
    return r


def dwell_ms(p: Persona, rng: Optional[Rng] = None) -> float:
    """How long a hover dwells on arrival: the persona's centre x 0.6..1.4, kept in 150..800 ms (the band is
    150-900 ms as the page measures it: the glide's slow tail inside 3 px adds up to ~70 ms)."""
    if rng is None:
        rng = random.random
    return _clamp(after_traits(p).dwell_ms * _lerp(0.6, 1.4, rng()), 150.0, 800.0)


def plan_dwell(at, box, p: Persona, ms: float, rng: Optional[Rng] = None):
    """Plan the pointer's rest on a hover target: OU drift + 8-12 Hz tremor samples around ``at`` for
    about ``ms`` (at least 3 samples), every sample within DWELL_RADIUS_PX of ``at`` and inside ``box``
    (a dict with x/y/width/height, 1 px in; None: no box). Returns (x, y, sleep_ms) tuples.

    The hand is not braced on a hover the way it is before a press, so the drift runs at twice the
    glide's amplitude and the tremor at 1.5x; the radius clamp keeps the pointer on the element."""
    if rng is None:
        rng = random.random
    step_ms = 1000.0 / p.device_hz
    # rest gaps run 0.9-1.4 frames (mean 1.15): the sample count makes the walked dwell last about ms
    n = int(max(3, _js_round(max(0.0, ms) / (step_ms * 1.15))))
    out: list = []
    drift = [0.0, 0.0]
    tphase = rng() * 2 * math.pi
    t = 0.0
    ax, ay = at
    for _ in range(n):
        k = step_ms / p.drift_tau
        drift[0] += -k * drift[0] + math.sqrt(2 * k) * p.drift_amp * 2.0 * gauss_from(rng)
        drift[1] += -k * drift[1] + math.sqrt(2 * k) * p.drift_amp * 2.0 * gauss_from(rng)
        ph = tphase + (2 * math.pi * p.tremor_hz * t) / 1000.0
        dx = drift[0] + p.tremor_amp * 1.5 * math.sin(ph)
        dy = drift[1] + p.tremor_amp * 1.5 * math.sin(ph + math.pi / 2)
        r = math.hypot(dx, dy)
        if r > DWELL_RADIUS_PX:
            dx, dy = dx * DWELL_RADIUS_PX / r, dy * DWELL_RADIUS_PX / r
        x, y = ax + dx, ay + dy
        if box is not None:
            bx, by, bw, bh = box["x"], box["y"], box["width"], box["height"]
            x = _clamp(x, bx + 1, bx + bw - 1) if bw >= 2 else ax
            y = _clamp(y, by + 1, by + bh - 1) if bh >= 2 else ay
        sl = step_ms * _lerp(0.9, 1.4, rng())
        out.append((x, y, sl))
        t += sl
    return out


def move_off_roll(p: Persona, rng: Optional[Rng] = None, hits: int = 0, n: int = 0) -> bool:
    """Does this click get a move-off? In ``after_traits(p).move_off_rate`` of clicks: a draw against
    the rate, nudged by how far the page's running share (``hits`` of ``n`` earlier clicks) has drifted
    from it, so 40 clicks land near the persona's rate instead of anywhere a coin allows (+-15 %)."""
    if rng is None:
        rng = random.random
    rate = after_traits(p).move_off_rate
    return rng() < _clamp(rate + 0.5 * (rate * n - hits), 0.05, 0.98)


def move_off_delay_ms(p: Persona, rng: Optional[Rng] = None) -> float:
    """The release -> move-off gap: the persona's centre x 0.7..1.3, kept in 200..660 ms (200-700 as the page
    measures it from its mouseup: the release round trip and the first move's dispatch add 20-60 ms)."""
    if rng is None:
        rng = random.random
    t = _tempo(p)
    return _clamp(after_traits(p).move_off_delay_ms * t * _lerp(0.7, 1.3, rng()), 200.0 * t, 660.0 * t)


def plan_move_off(frm, viewport, p: Persona, rng: Optional[Rng] = None):
    """Where the hand drifts after a click: 40-200 px from ``frm`` at an angle in the downward
    half-plane (20..160 deg, y down -- the gateway's moveOnTarget model), kept MOVE_OFF_MARGIN_PX inside
    ``viewport`` (dict with width/height). Mirrored upward when the clamp would leave it on the spot
    (a click near the bottom edge). Returns the (x, y) target; the glide there is plan_move's."""
    if rng is None:
        rng = random.random
    vw, vh = viewport["width"], viewport["height"]
    m = MOVE_OFF_MARGIN_PX
    ang = math.radians(_lerp(20, 160, rng()))
    dist = _lerp(40, 200, rng())
    dx, dy = math.cos(ang) * dist, math.sin(ang) * dist
    x = _clamp(frm[0] + dx, m, vw - m)
    y = _clamp(frm[1] + dy, m, vh - m)
    if math.hypot(x - frm[0], y - frm[1]) < 12.0:
        x = _clamp(frm[0] + dx, m, vw - m)
        y = _clamp(frm[1] - dy, m, vh - m)
    return (x, y)


# ------------------------------------------------------------------ fresh-page entry (SC 239 / pro-r43-13)
# Where a hand comes from on a page the pointer has not been over yet: 30-120 px ABOVE the view, within +-30 % of
# the view width of the target -- in from the tab strip / address bar, as pro-r43-13 does for the engine's own
# first path. The samples still above the view are not sent; the page's first event is where the path crosses
# the top edge (y 0-4). Before 0.54 the SDK's first glide started at an interior point 140-380 x 90-240 px in, so
# a page's first pointer event appeared mid-page (lab row H 13). Node entryPoint/enterFromTop and .NET
# EntryPoint/EnterFromTop are ports; sdk/testdata/humanize_motion_vectors.json "entry" pins them.
ENTRY_ABOVE_PX = (30.0, 120.0)
ENTRY_SPREAD = 0.6
ENTRY_CROSS_PX = 4.0
ENTRY_EDGE_PX = 8.0
ENTRY_DEFAULT_VIEW_W = 1000.0
# An ambient drift on a fresh page is planned from the entry point too (its first excursion comes in over the top
# edge, _humanize._ambient_plan); the dropped lead-in is ~100-200 ms, so it is planned this much longer and then
# trimmed to the ask.
ENTRY_LEAD_MS = 250.0


def entry_point(target, view_w=None, rng: Optional[Rng] = None):
    """The off-view point a fresh page's first glide starts from: x within +-30 % of the view width of ``target``
    (8 px inside the view's sides), y 30-120 px above the view. Two draws from ``rng``: x first, then y. Without a
    usable ``view_w`` the view is taken as 1000 px wide."""
    if rng is None:
        rng = random.random
    w = float(view_w) if view_w and view_w > 16 else ENTRY_DEFAULT_VIEW_W
    x = _clamp(target[0] + (rng() - 0.5) * ENTRY_SPREAD * w, ENTRY_EDGE_PX, w - ENTRY_EDGE_PX)
    y = -(ENTRY_ABOVE_PX[0] + rng() * (ENTRY_ABOVE_PX[1] - ENTRY_ABOVE_PX[0]))
    return (x, y)


def enter_from_top(steps, start, rng: Optional[Rng] = None):
    """``steps`` of a path planned from the off-view ``start`` with the samples still above the view dropped and
    the first one put where the path crosses the top edge (y 0-4; one draw from ``rng`` when a crossing sample
    is made). The crossing sample takes the timing of the sample it replaces (or of the first kept one). A path
    whose start is inside the view, or that never comes in, is returned as it is."""
    if not steps or start[1] >= 0:
        return steps
    i = 0
    while i < len(steps) and steps[i][1] < 0:
        i += 1
    if i >= len(steps):
        return steps
    a = (start[0], start[1]) if i == 0 else (steps[i - 1][0], steps[i - 1][1])
    b = steps[i]
    if b[1] <= ENTRY_CROSS_PX:
        return list(steps[i:])
    if rng is None:
        rng = random.random
    y_in = rng() * ENTRY_CROSS_PX
    f = (y_in - a[1]) / (b[1] - a[1]) if b[1] > a[1] else 1.0
    cross = (a[0] + (b[0] - a[0]) * f, y_in)
    if i == 0:
        return [cross + tuple(steps[0][2:])] + list(steps)
    return [cross + tuple(steps[i - 1][2:])] + list(steps[i:])
