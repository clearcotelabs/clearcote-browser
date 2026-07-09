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
    grab_min_ms: float = 130.0
    grab_max_ms: float = 360.0
    release_min_ms: float = 90.0
    release_max_ms: float = 230.0
    # click press-hold (mousedown->mouseup) and per-key dwell (keydown->keyup) windows, ms.
    click_hold_min_ms: float = 60.0
    click_hold_max_ms: float = 150.0
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


def _sample_submove(out, a, b, dur_ms, p: Persona, rng: Rng, t0, drift, jit, tremor_phase, land):
    """Sample ONE min-jerk submovement a→b along a slightly bowed path with colored noise. Appends
    (x, y, sleep_ms) tuples to ``out``. Returns the absolute clock (ms) at the end."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    dist = math.hypot(dx, dy) or 1e-6
    step_ms = 1000.0 / p.device_hz
    # Cap the sample count so a very long move can't emit thousands of steps (defends the Fitts cap).
    n = int(min(180, max(4, round(dur_ms / step_ms))))
    nx, ny = -dy / dist, dx / dist
    bow_sign = 1 if p.approach_bias >= 0 else -1
    bow = bow_sign * (0.04 + abs(gauss_from(rng)) * 0.05) * min(dist, 260) * (0.5 + abs(p.approach_bias))
    c1x, c1y = ax + dx * 0.33 + nx * bow, ay + dy * 0.33 + ny * bow
    c2x, c2y = ax + dx * 0.66 + nx * bow, ay + dy * 0.66 + ny * bow
    t = t0
    for i in range(1, n + 1):
        e = min_jerk(i / n)
        px = _bez(ax, c1x, c2x, bx, e)
        py = _bez(ay, c1y, c2y, by, e)
        last = i == n
        if not (last and land):
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
            px += drift[0] + tr_x + jit[0]
            py += drift[1] + tr_y + jit[1]
        else:
            px, py = bx, by
        sleep_ms = step_ms * _lerp(0.88, 1.12, rng())
        out.append((px, py, sleep_ms))
        t += sleep_ms
    return t


def plan_move(frm, to, p: Persona, target_w: float = 24.0, settle: bool = False, rng: Optional[Rng] = None):
    """Plan a free cursor move frm→to as a sum of minimum-jerk submovements (primary + corrections),
    Fitts-scaled duration and colored noise. Returns a list of (x, y, sleep_ms) tuples."""
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
    # floor capped at the 1700ms ceiling so a very long move can't invert the clamp (lo > hi).
    mt_lo = min(1700.0, max(70.0, 40.0 + d * 0.6))
    mt = _clamp((p.fitts_a + p.fitts_b * idv) * (0.85 + 0.3 * rng()), mt_lo, 1700.0)

    targets = []
    if d >= 40:
        f = p.primary_frac
        spread = min(16.0, d * p.overshoot)
        targets.append((fx + (tx - fx) * f + gauss_from(rng) * spread,
                        fy + (ty - fy) * f + gauss_from(rng) * spread))
        n_corr = 2 if rng() < 0.08 * p.max_corrections else 1
        cur = targets[0]
        for _ in range(n_corr):
            close = _lerp(0.55, 0.8, rng())
            nxt = (cur[0] + (tx - cur[0]) * close + gauss_from(rng) * 1.2,
                   cur[1] + (ty - cur[1]) * close + gauss_from(rng) * 1.2)
            targets.append(nxt)
            cur = nxt
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

    drift = [0.0, 0.0]
    jit = [0.0, 0.0]
    tremor_phase = rng() * 2 * math.pi
    a = frm
    t = 0.0
    for s in range(len(targets)):
        b = targets[s]
        last = s == len(targets) - 1
        t = _sample_submove(out, a, b, max(20.0, seg_dur[s]), p, rng, t, drift, jit, tremor_phase, last and not settle)
        if not last and out:
            x, y, sl = out[-1]
            out[-1] = (x, y, sl + _lerp(40, 120, rng()))  # motor re-planning gap
        a = b

    if settle:
        jig = 1 + int(rng() * 2)
        for _ in range(jig):
            out.append((tx + gauss_from(rng) * 1.4, ty + gauss_from(rng) * 1.4, _lerp(30, 90, rng())))
        out.append((tx, ty, 1000.0 / p.device_hz))
    return out


def drag_dwell(p: Persona, rng: Optional[Rng] = None):
    """(grab_ms, release_ms) for a held-button drag, from the persona window."""
    if rng is None:
        rng = random.random
    return (_lerp(p.grab_min_ms, p.grab_max_ms, rng()), _lerp(p.release_min_ms, p.release_max_ms, rng()))


def click_hold(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human mousedown->mouseup click hold (ms), right-skewed. Fixes the ~2 ms instant-press tell."""
    if rng is None:
        rng = random.random
    return p.click_hold_min_ms + min(1.0, abs(gauss_from(rng)) * 0.5) * (p.click_hold_max_ms - p.click_hold_min_ms)


def key_dwell(p: Persona, rng: Optional[Rng] = None) -> float:
    """A human per-key keydown->keyup dwell (ms), right-skewed. Fixes the ~2-3 ms instant-keystroke tell."""
    if rng is None:
        rng = random.random
    return p.key_dwell_min_ms + min(1.0, abs(gauss_from(rng)) * 0.45) * (p.key_dwell_max_ms - p.key_dwell_min_ms)


def click_point(box, frm, p: Persona, rng: Optional[Rng] = None):
    """A human click point inside ``box`` (dict with x/y/width/height): 2D gaussian toward center,
    nudged toward the approach side (undershoot), clamped a couple px inside the edges."""
    if rng is None:
        rng = random.random
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    dx, dy = cx - frm[0], cy - frm[1]
    d = math.hypot(dx, dy) or 1.0
    ux, uy = -dx / d, -dy / d
    x = cx + ux * box["width"] * 0.12 + gauss_from(rng) * box["width"] * 0.16
    y = cy + uy * box["height"] * 0.12 + gauss_from(rng) * box["height"] * 0.16
    # For a box narrower/shorter than the 4px inset the clamp bounds invert (lo > hi) and would return
    # a point OUTSIDE the element — use the center for such tiny targets.
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
