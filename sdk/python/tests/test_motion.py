"""Validation tests for the humanize motion core (mirrors node/test/motion.test.ts).

These are STATISTICAL (averages/thresholds), not byte-exact, because the trajectory sampler uses
transcendental functions whose last-ULP results can differ between V8 and CPython (see _motion.py).
The seed->persona map is bit-identical across SDKs, so persona/determinism checks are exact.
"""

import math

from clearcote._motion import (
    click_point,
    drag_dwell,
    gauss_from,
    make_persona,
    min_jerk,
    mulberry32,
    plan_ambient,
    plan_move,
)
from clearcote._motionscore import extract_features, score_motion, steps_to_samples


def _old_glide(frm, to, rng):
    """Faithful reproduction of the OLD single-bezier glide (smoothstep easing, uniform rand(7,20) dt,
    flat white-gauss jitter) so we can score old-vs-new on the SAME detector-emulator. Ported from the
    ``oldGlide`` helper in motion.test.ts; returns (x, y, sleep_ms) tuples for steps_to_samples."""
    x0, y0 = frm
    dx, dy = to[0] - frm[0], to[1] - frm[1]
    dist = math.hypot(dx, dy)
    steps = int(math.floor(max(10, min(38, dist / 14))))
    nx, ny = -dy / dist, dx / dist
    bow = (rng() * 0.22 - 0.11) * dist
    c1x, c1y = x0 + dx * 0.33 + nx * bow, y0 + dy * 0.33 + ny * bow
    c2x, c2y = x0 + dx * 0.66 + nx * bow, y0 + dy * 0.66 + ny * bow
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)
        mt = 1 - e
        bx = mt * mt * mt * x0 + 3 * mt * mt * e * c1x + 3 * mt * e * e * c2x + e * e * e * to[0]
        by = mt * mt * mt * y0 + 3 * mt * mt * e * c1y + 3 * mt * e * e * c2y + e * e * e * to[1]
        out.append((bx + gauss_from(rng) * 0.6, by + gauss_from(rng) * 0.6, 7 + rng() * 13))
    out.append((to[0], to[1], 12))
    return out


# --- persona ---------------------------------------------------------------------------------------

def test_persona_deterministic_per_seed_and_varies_across_seeds():
    a, b, c = make_persona("id-alpha"), make_persona("id-alpha"), make_persona("id-beta")
    assert a == b                       # deterministic: same seed -> identical persona
    assert a.seed != c.seed             # a different fingerprint gets a different motor identity
    assert 8 <= a.tremor_hz <= 12       # physiological tremor band
    assert a.device_hz > 100            # sampled faster than a real pointer device


# --- min_jerk --------------------------------------------------------------------------------------

def test_min_jerk_monotone_and_peaks_near_middle():
    assert abs(min_jerk(0.0) - 0.0) < 1e-9
    assert abs(min_jerk(1.0) - 1.0) < 1e-9
    # discrete speed of min-jerk is non-negative (monotone 0->1) and peaks near tau=0.5
    peak = 0.0
    peak_tau = 0.0
    for i in range(1, 101):
        v = min_jerk(i / 100) - min_jerk((i - 1) / 100)
        assert v >= -1e-12              # monotone non-decreasing over [0, 1]
        if v > peak:
            peak = v
            peak_tau = i / 100
    assert 0.4 < peak_tau < 0.6


# --- plan_move vs old glide (detector-emulator) ----------------------------------------------------

def test_plan_move_scores_more_human_than_old_glide():
    p = make_persona("scorer-seed")
    new_tot = 0.0
    old_tot = 0.0
    N = 40
    frm, to = (200, 300), (720, 470)
    for k in range(N):
        rng_new = mulberry32(1000 + k)
        rng_old = mulberry32(1000 + k)
        new = score_motion(steps_to_samples(plan_move(frm, to, p, target_w=30, rng=rng_new)))
        old = score_motion(steps_to_samples(_old_glide(frm, to, rng_old)))
        new_tot += new.score
        old_tot += old.score
    new_avg = new_tot / N
    old_avg = old_tot / N
    assert new_avg > old_avg
    assert new_avg > 0.75


def test_plan_move_fixes_the_specific_tells():
    p = make_persona("tells")
    sub = 0.0
    skew = 0.0
    ac = 0.0
    uniform = 0
    straight = 0.0
    R = 30
    for k in range(R):
        f = extract_features(
            steps_to_samples(plan_move((150, 150), (780, 520), p, target_w=28, rng=mulberry32(k)))
        )
        sub += f.submovements
        skew += f.vel_skew
        ac += f.jitter_autocorr
        uniform += 1 if f.dt_uniform else 0
        straight += f.straightness
    assert sub / R >= 1          # has corrective submovements (Fitts homing)
    assert sub / R < 3.5         # but not an implausible number of them
    assert skew / R > 0.1        # right-skewed velocity (homing tail), not a symmetric bell
    assert ac / R > 0.1          # colored jitter, not white
    assert uniform == 0          # never looks i.i.d.-uniform
    assert straight / R < 0.999  # not a ruler-straight path


def test_duration_follows_fitts_law():
    p = make_persona("fitts")

    def dur(w):
        tot = 0.0
        R = 20
        for k in range(R):
            s = steps_to_samples(plan_move((100, 100), (600, 100), p, target_w=w, rng=mulberry32(k)))
            tot += s[-1]["t"]
        return tot / R

    assert dur(8) > dur(120)     # small target => longer than a big target at equal distance


# --- drag endpoints --------------------------------------------------------------------------------

def test_drag_dwell_produces_human_grab_release_windows():
    p = make_persona("drag")
    for k in range(50):
        grab_ms, release_ms = drag_dwell(p, mulberry32(k))
        assert grab_ms >= p.grab_min_ms
        assert grab_ms <= p.grab_max_ms
        assert release_ms >= p.release_min_ms


def test_full_drag_stream_passes_the_drag_scorer():
    p = make_persona("drag2")
    rng = mulberry32(3)
    handle, target = (300, 402), (560, 402)
    approach = plan_move((100, 400), handle, p, rng=rng)          # button UP
    grab_ms, release_ms = drag_dwell(p, rng)
    drag = plan_move(handle, target, p, settle=True, rng=rng)     # button HELD
    drag[-1] = (drag[-1][0], drag[-1][1], release_ms)             # pre-release settle dwell
    # sequence: approach(up) -> down@handle(dwell grab_ms) -> held drag -> up@target
    down = (handle[0], handle[1], grab_ms)
    up = (target[0], target[1], 0.0)
    steps = [*approach, down, *drag, up]
    start = len(approach)
    end = len(approach) + 1 + len(drag)                          # held = down + drag (not up)
    res = score_motion(steps_to_samples(steps, {"start": start, "end": end}), drag=True)
    assert not any(f.startswith("grab-hesitation-too-short") for f in res.flags)
    assert not any(f.startswith("release-delay-too-short") for f in res.flags)
    assert res.features.grab_ms > 60
    assert res.features.release_ms > 40


# --- click_point -----------------------------------------------------------------------------------

def test_click_point_stays_inside_the_box():
    p = make_persona("cp")
    box = {"x": 100, "y": 100, "width": 200, "height": 60}
    for k in range(100):
        px, py = click_point(box, (0, 130), p, mulberry32(k))
        assert px >= box["x"] + 2
        assert px <= box["x"] + box["width"] - 2
        assert py >= box["y"] + 2
        assert py <= box["y"] + box["height"] - 2


# --- plan_ambient ----------------------------------------------------------------------------------

def test_plan_ambient_produces_entropy_within_viewport():
    p = make_persona("amb")
    steps = plan_ambient((400, 300), {"width": 1280, "height": 800}, p, 1500, mulberry32(1))
    total = sum(s[2] for s in steps)
    assert total > 1200
    for s in steps:
        assert 0 < s[0] < 1280
    # distinct positions (real entropy, not a frozen cursor)
    distinct = {f"{round(s[0])},{round(s[1])}" for s in steps}
    assert len(distinct) > 20
