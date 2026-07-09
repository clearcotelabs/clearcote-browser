"""Detector-emulator (Python) — extracts the feature vector a behavioral collector would compute from a
pointer stream and scores how human it looks. Faithful port of the Node SDK's ``src/motionscore.ts``.

This is a VALIDATION tool: it lets us prove, offline and deterministically, that the motion core
(``_motion.py``) improves the features that matter, instead of eyeballing trajectories or burning scarce
clean-IP end-to-end runs. It is intentionally an APPROXIMATION of what real collectors do (their exact
models are private); treat the score as a RELATIVE signal (old vs new), not an absolute pass/fail.

PURE (no Playwright, no I/O, standard library only). A ``Sample`` is a dict ``{"x","y","t","buttons"}`` or
a tuple ``(x, y, t[, buttons])``; ``buttons`` defaults to 0. ``steps_to_samples`` emits the dict form and
consumes the ``(x, y, sleep_ms)`` steps produced by ``_motion.plan_move``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple, Union

# A Sample is a dict {"x","y","t","buttons"?} or a tuple (x, y, t[, buttons]).
Sample = Union[dict, Sequence[float]]


def _sx(s: Sample) -> float:
    return s["x"] if isinstance(s, dict) else s[0]


def _sy(s: Sample) -> float:
    return s["y"] if isinstance(s, dict) else s[1]


def _st(s: Sample) -> float:
    """time in ms (0 when absent), mirroring the guarded ``_sb`` accessor so a 2-tuple (x, y) — or a
    dict without ``t`` — never raises IndexError/KeyError in ``extract_features``."""
    if isinstance(s, dict):
        return s.get("t") or 0
    return s[2] if len(s) > 2 else 0


def _sb(s: Sample) -> int:
    """buttons bitmask (0 when absent), matching TS ``s.buttons ?? 0``."""
    if isinstance(s, dict):
        return int(s.get("buttons") or 0)
    return int(s[3]) if len(s) > 3 else 0


@dataclass
class MotionFeatures:
    """The feature vector a behavioral collector would compute (see motionscore.ts for field semantics)."""

    n: int
    duration_ms: float
    #: Fraction of the movement at which peak speed occurs. Human ~0.3-0.45; pure ease-out ~0.15.
    peak_vel_frac: float
    #: Time-weighted skewness of the speed profile. Human > 0 (long decelerating homing tail); ~0 symmetric.
    vel_skew: float
    #: Corrective submovements = prominent interior speed minima (human 1-3; a single curve = 0).
    submovements: int
    #: Net displacement / path length (1 = perfectly straight).
    straightness: float
    #: Diagnostic only (NOT scored): residual of the whole speed profile vs a SINGLE min-jerk bell.
    min_jerk_residual: float
    dt_mean_ms: float
    dt_std_ms: float
    #: True when inter-sample dt looks i.i.d.-uniform (no cadence mode) — a tell.
    dt_uniform: bool
    #: Lag-1 autocorrelation of the sub-pixel jitter residual. White noise ~0; colored motor noise ~0.2-0.8.
    jitter_autocorr: float
    #: 2/3 power-law fit quality (diagnostic only).
    power_law_r: float
    #: Drag endpoint timing (only when a held-button segment is present), else None.
    grab_ms: Optional[float]
    release_ms: Optional[float]


def _speeds(s: Sequence[Sample]) -> Tuple[List[float], List[float]]:
    v: List[float] = []
    t_mid: List[float] = []
    for i in range(1, len(s)):
        dt = max(1e-3, _st(s[i]) - _st(s[i - 1]))
        v.append(math.hypot(_sx(s[i]) - _sx(s[i - 1]), _sy(s[i]) - _sy(s[i - 1])) / dt)
        t_mid.append((_st(s[i]) + _st(s[i - 1])) / 2)
    return v, t_mid


def _smooth(a: Sequence[float], w: int = 3) -> List[float]:
    """Smooth a series with a small moving average (window w)."""
    out: List[float] = []
    for i in range(len(a)):
        acc = 0.0
        c = 0
        for j in range(max(0, i - w), min(len(a) - 1, i + w) + 1):
            acc += a[j]
            c += 1
        out.append(acc / c)
    return out


def _mean(a: Sequence[float]) -> float:
    return sum(a) / len(a) if a else 0.0


def _std(a: Sequence[float]) -> float:
    if len(a) < 2:
        return 0.0
    m = _mean(a)
    return math.sqrt(_mean([(x - m) ** 2 for x in a]))


def extract_features(s: Sequence[Sample]) -> MotionFeatures:
    n = len(s)
    duration_ms = _st(s[n - 1]) - _st(s[0]) if n > 1 else 0.0
    v, t_mid = _speeds(s)
    sv = _smooth(v, 2)

    # peak velocity position
    peak_idx = 0
    for i in range(1, len(sv)):
        if sv[i] > sv[peak_idx]:
            peak_idx = i
    peak_vel_frac = (t_mid[peak_idx] - _st(s[0])) / duration_ms if duration_ms > 0 else 0.0

    # velocity skewness (positive for a human's fast-rise/slow-decay profile; ~0 for a symmetric curve)
    vmean = _mean(sv)
    vstd = _std(sv) or 1e-9
    vel_skew = _mean([((x - vmean) / vstd) ** 3 for x in sv])

    # corrective submovements: prominent interior local minima below 0.5*vmax, a true valley between two
    # higher shoulders, with a minimum index separation so one correction isn't counted several times.
    vmax = max([*sv, 1e-9])
    submovements = 0
    last_min = -10
    for i in range(2, len(sv) - 2):
        valley = sv[i] < sv[i - 1] and sv[i] <= sv[i + 1] and sv[i] < sv[i - 2] and sv[i] < sv[i + 2]
        if valley and sv[i] < 0.5 * vmax and i - last_min >= 3:
            submovements += 1
            last_min = i

    # straightness
    path_len = 0.0
    for i in range(1, n):
        path_len += math.hypot(_sx(s[i]) - _sx(s[i - 1]), _sy(s[i]) - _sy(s[i - 1]))
    net_disp = math.hypot(_sx(s[n - 1]) - _sx(s[0]), _sy(s[n - 1]) - _sy(s[0])) if n > 1 else 0.0
    straightness = net_disp / path_len if path_len > 1e-6 else 1.0

    # min-jerk residual: compare the normalized speed profile to 30*tau^2*(1-tau)^2
    min_jerk_residual = 1.0
    if len(sv) >= 4 and duration_ms > 0:
        area = 0.0
        for i in range(len(sv)):
            area += sv[i] * (0 if i == 0 else t_mid[i] - t_mid[i - 1])
        area = area or 1.0
        res = 0.0
        norm = 0.0
        for i in range(len(sv)):
            tau = (t_mid[i] - _st(s[0])) / duration_ms
            ideal = 30 * tau * tau * (1 - tau) * (1 - tau)  # integral over tau = 1
            obs = (sv[i] * duration_ms) / area  # normalize so integral obs d(tau) ~ 1
            res += (obs - ideal) ** 2
            norm += ideal ** 2
        min_jerk_residual = math.sqrt(res / norm) if norm > 0 else 1.0

    # dt stats + uniformity: a real device stream has a sharp cadence MODE (most samples near the median)
    # with a heavy tail of pauses; a rand(a,b) band has NO mode, so few samples cluster near the median.
    dts: List[float] = []
    for i in range(1, n):
        dts.append(_st(s[i]) - _st(s[i - 1]))
    dt_mean_ms = _mean(dts)
    dt_std_ms = _std(dts)
    sorted_dts = sorted(dts)
    median = sorted_dts[len(sorted_dts) // 2] if sorted_dts else 0.0
    core_frac = (
        len([d for d in dts if 0.8 * median <= d <= 1.2 * median]) / len(dts) if median > 0 else 0.0
    )
    dt_uniform = core_frac < 0.5

    # jitter color: lag-1 autocorrelation of the sub-pixel residual (position minus a smoothed trend).
    jitter_autocorr = 0.0
    if n >= 8:
        xs = [_sx(p) for p in s]
        ys = [_sy(p) for p in s]
        sx = _smooth(xs, 3)
        sy = _smooth(ys, 3)
        rx = [xs[i] - sx[i] for i in range(len(xs))]
        ry = [ys[i] - sy[i] for i in range(len(ys))]

        def _ac(r: List[float]) -> float:
            num = 0.0
            den = 0.0
            for i in range(1, len(r)):
                num += r[i] * r[i - 1]
            for i in range(len(r)):
                den += r[i] * r[i]
            return num / den if den > 1e-9 else 0.0

        jitter_autocorr = (_ac(rx) + _ac(ry)) / 2

    # 2/3 power law (diagnostic): correlation between log speed and -(1/3) log curvature
    power_law_r = 0.0
    if n >= 6:
        lv: List[float] = []
        lc: List[float] = []
        for i in range(1, n - 1):
            ax = _sx(s[i]) - _sx(s[i - 1])
            ay = _sy(s[i]) - _sy(s[i - 1])
            bx = _sx(s[i + 1]) - _sx(s[i])
            by = _sy(s[i + 1]) - _sy(s[i])
            cross = abs(ax * by - ay * bx)
            seg_a = math.hypot(ax, ay)
            seg_b = math.hypot(bx, by)
            curv = cross / (math.pow(seg_a * seg_b * math.hypot(bx + ax, by + ay), 1) + 1e-9)
            sp = (seg_a + seg_b) / 2 / max(1e-3, (_st(s[i + 1]) - _st(s[i - 1])) / 2)
            if curv > 1e-6 and sp > 1e-6:
                lv.append(math.log(sp))
                lc.append(math.log(curv))
        if len(lv) >= 4:
            mv = _mean(lv)
            mc = _mean(lc)
            num = 0.0
            dv = 0.0
            dc = 0.0
            for i in range(len(lv)):
                num += (lv[i] - mv) * (lc[i] - mc)
                dv += (lv[i] - mv) ** 2
                dc += (lc[i] - mc) ** 2
            power_law_r = num / math.sqrt(dv * dc) if (dv > 0 and dc > 0) else 0.0

    # drag endpoint timing: within the buttons==1 (held) segment. The `up` is the first sample AFTER the
    # held span (buttons back to 0); grab = down->first-movement, release = last-movement->up.
    grab_ms: Optional[float] = None
    release_ms: Optional[float] = None
    held_idx: List[int] = []
    for i in range(n):
        if _sb(s[i]) & 1:
            held_idx.append(i)
    if held_idx:
        d0 = held_idx[0]
        first_move_t = _st(s[d0])
        for i in held_idx:
            if math.hypot(_sx(s[i]) - _sx(s[d0]), _sy(s[i]) - _sy(s[d0])) > 1:
                first_move_t = _st(s[i])
                break
        grab_ms = first_move_t - _st(s[d0])
        last_move_idx = d0
        for k in range(1, len(held_idx)):
            i = held_idx[k]
            j = held_idx[k - 1]
            if math.hypot(_sx(s[i]) - _sx(s[j]), _sy(s[i]) - _sy(s[j])) > 1:
                last_move_idx = i
        last_held = held_idx[-1]
        up_t = _st(s[last_held + 1]) if last_held + 1 < n else _st(s[last_held])
        release_ms = up_t - _st(s[last_move_idx])

    return MotionFeatures(
        n=n,
        duration_ms=duration_ms,
        peak_vel_frac=peak_vel_frac,
        vel_skew=vel_skew,
        submovements=submovements,
        straightness=straightness,
        min_jerk_residual=min_jerk_residual,
        dt_mean_ms=dt_mean_ms,
        dt_std_ms=dt_std_ms,
        dt_uniform=dt_uniform,
        jitter_autocorr=jitter_autocorr,
        power_law_r=power_law_r,
        grab_ms=grab_ms,
        release_ms=release_ms,
    )


@dataclass
class MotionScore:
    score: float
    flags: List[str]
    features: MotionFeatures


def score_motion(s: Sequence[Sample], drag: bool = False) -> MotionScore:
    """Score 0..1 (higher = more human) with human-readable flags for the tells that fire."""
    f = extract_features(s)
    flags: List[str] = []
    score = 1.0

    def penalize(cond: bool, amt: float, flag: str) -> None:
        nonlocal score
        if cond:
            score -= amt
            flags.append(flag)

    # peak velocity should be asymmetric-human: penalize pure ease-out (very early) AND a too-symmetric
    # single-curve peak sitting right at the midpoint.
    penalize(f.peak_vel_frac < 0.15, 0.15, f"peak-velocity-ease-out({f.peak_vel_frac:.2f})")
    penalize(f.peak_vel_frac > 0.48, 0.12, f"peak-velocity-too-symmetric({f.peak_vel_frac:.2f})")
    # velocity profile should have the human right-skew (long homing tail), not a symmetric bell
    penalize(f.vel_skew < 0.1, 0.12, f"velocity-symmetric(skew={f.vel_skew:.2f})")
    # at least one corrective submovement (Fitts homing); a single analytic curve has none
    penalize(f.submovements < 1, 0.2, "no-corrective-submovement")
    # ...but not TOO many: an over-corrected, velocity-reversal-heavy reach (many interior speed minima)
    # is as non-human as a single smooth curve — real homing settles in 1-3 corrections.
    penalize(f.submovements > 4, 0.12, f"too-many-submovements({f.submovements})")
    # dt should NOT look i.i.d.-uniform (real device sampling has a cadence mode)
    penalize(f.dt_uniform, 0.15, "uniform-dt-cadence")
    # colored (not white) sub-pixel noise
    penalize(f.jitter_autocorr < 0.1, 0.1, f"white-noise-jitter({f.jitter_autocorr:.2f})")
    # path shouldn't be a ruler-straight line
    penalize(f.straightness > 0.9995, 0.08, "perfectly-straight-path")

    if drag:
        penalize(f.grab_ms is not None and f.grab_ms < 60, 0.2, f"grab-hesitation-too-short({f.grab_ms}ms)")
        penalize(
            f.release_ms is not None and f.release_ms < 40, 0.2, f"release-delay-too-short({f.release_ms}ms)"
        )

    return MotionScore(score=max(0.0, score), flags=flags, features=f)


def steps_to_samples(
    steps: Sequence[Tuple[float, float, float]],
    buttons_during: Optional[dict] = None,
) -> List[dict]:
    """Turn a Step list (absolute coords + post-step sleep) into timestamped Samples for scoring.

    A ``step`` is an ``(x, y, sleep_ms)`` tuple (matches ``_motion.plan_move`` output). ``buttons_during``
    marks the held-button (drag) span ``[start, end)`` with buttons=1 (dict ``{"start": i, "end": j}``).
    """
    out: List[dict] = []
    t = 0.0
    for i in range(len(steps)):
        held = (
            1
            if buttons_during is not None
            and buttons_during["start"] <= i < buttons_during["end"]
            else 0
        )
        x, y, sleep_ms = steps[i][0], steps[i][1], steps[i][2]
        out.append({"x": x, "y": y, "t": t, "buttons": held})
        t += sleep_ms
    return out
