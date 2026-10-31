using System;
using System.Collections.Generic;
using System.Linq;
using System.Diagnostics;
using System.Threading.Tasks;

namespace Clearcote;

/// <summary>
/// Wall-clock pacing for planned pointer paths (mirrors sdk/python/clearcote/_pacing.py and
/// sdk/node/src/pacing.ts).
///
/// The planner (<see cref="Motion.PlanMove"/>) returns samples at the persona's device rate (~6-8 ms
/// apart). Walking them as "dispatch, then sleep the planned gap" adds the dispatch round trip to
/// EVERY sample: measured on PRO r38, one mouse move costs 27 ms median, so a 1.05 s planned glide took
/// 3.5-5.4 s and the page saw ~25 pointermove/s (SC 209). On Windows Task.Delay also rounds every
/// 6-8 ms gap up to the ~15.6 ms timer tick, which stretched the same glide on its own.
///
/// <see cref="WalkAsync"/> schedules each sample at its planned offset from the start and keeps that
/// clock: on time it sleeps until the sample's planned time; behind, it sends the point ON the planned path at
/// the time that has passed (linear between the two planned samples around it), so the move keeps the plan's
/// shape, its duration and its SPEED profile. Never skipped: the final sample and a sample marked as changing
/// button state -- both go out as planned. Never a teleport: no hop longer than the persona's cap
/// (<see cref="Motion.HopCapPx"/>, 55-110 px; <see cref="MaxJumpPx"/> without a persona), nor further than
/// <see cref="IdleHopPx"/> after <see cref="IdleMs"/> with nothing sent; a longer hop goes to the farthest point
/// along the path within the cap, so a late walk stretches in time rather than jumping (SC 210 lab v2: the
/// old cap, 3 px/ms x the time since the last dispatch, turned slow dispatches under load into hops of
/// 60-103 px a few ms apart; an r40 engine walks any hop &gt;= 24 px after &gt;= 40 ms idle). SC 298 (r54, SDK
/// 0.67.1, Windows): the old walk skipped to whole planned samples under a FIXED 40 px cap; with an ~18 ms round
/// trip it hopped 2 or 3 samples at a time (a sawtooth of 7-8 velocity peaks per glide) and no page-visible step
/// exceeded 40 px under any persona. A stall is a pause: a single dispatch slower than <see cref="StallMs"/>
/// shifts the schedule by the excess instead of being caught up by skipping. Sleeps are local timers, never a
/// protocol round trip. Nothing here changes what the planner returns.
/// </summary>
internal static class Pacing
{
    /// <summary>The hop cap when the caller names no persona (the humanizer passes the persona's
    /// <see cref="Motion.HopCapPx"/>). Same as Python MAX_JUMP_PX.</summary>
    public const double MaxJumpPx = 72;

    /// <summary>A single dispatch slower than this is a stall (the page or the box was busy), not latency:
    /// nothing reached the page while it lasted, so the plan pauses with it instead of skipping ahead.</summary>
    public const double StallMs = 60;

    /// <summary>A fast hand: ~3 px/ms (a quick flick across a 1080p screen in ~0.6 s). No longer a dispatch cap
    /// (the hop cap is <see cref="MaxJumpPx"/> / <see cref="IdleHopPx"/> since SC 210 round 2): kept as the
    /// speed bound the tests check.</summary>
    public const double FastPxPerMs = 3;

    /// <summary>After this long with nothing dispatched, a hop may be at most <see cref="IdleHopPx"/>.</summary>
    public const double IdleMs = 40;

    /// <summary>What a hand at a moderate 2.5 px/ms covers in one 125 Hz report (8 ms), below the 24 px an r40
    /// engine walks.</summary>
    public const double IdleHopPx = 20;

    /// <summary>Planned dispatch offsets (ms from the start) of every step, and the planned total.</summary>
    public static (double[] Due, double Total) Schedule(IReadOnlyList<Step> steps)
    {
        var due = new double[steps.Count];
        double t = 0;
        for (int i = 0; i < steps.Count; i++) { due[i] = t; t += Math.Max(0, steps[i].SleepMs); }
        return (due, t);
    }

    /// <summary>Legacy helper (the pre-SC 299 walk): the planned sample to dispatch next when only whole samples
    /// may go out; step <paramref name="i"/> is the first not yet sent. <see cref="WalkAsync"/> itself sends points
    /// between samples (<see cref="Target"/>).</summary>
    public static int NextIndex(IReadOnlyList<Step> steps, double[] due, int i, (double X, double Y)? last,
        double elapsedMs, double maxJump = MaxJumpPx, Func<int, bool>? pinned = null)
    {
        int n = steps.Count, j = i;
        while (j + 1 < n && due[j + 1] <= elapsedMs && !(pinned?.Invoke(j) ?? false)) j++;
        if (last is { } l)
            while (j > i && Math.Sqrt(Sq(steps[j].X - l.X) + Sq(steps[j].Y - l.Y)) > maxJump) j--;
        return j;
    }

    private static double Sq(double v) => v * v;

    /// <summary>The first index k with due[k] &gt; t (Python bisect.bisect_right).</summary>
    private static int BisectRight(double[] due, double t)
    {
        int lo = 0, hi = due.Length;
        while (lo < hi)
        {
            int mid = (lo + hi) >> 1;
            if (t < due[mid]) hi = mid; else lo = mid + 1;
        }
        return lo;
    }

    /// <summary>The point of the planned path at plan time <paramref name="t"/> (ms): linear between the two
    /// samples around it. Port of _pacing._pos_at.</summary>
    internal static (double X, double Y) PosAt(IReadOnlyList<Step> steps, double[] due, double t)
    {
        if (t <= due[0]) return (steps[0].X, steps[0].Y);
        if (t >= due[^1]) return (steps[^1].X, steps[^1].Y);
        int k = BisectRight(due, t);   // due[k-1] <= t < due[k]
        var a = steps[k - 1];
        var b = steps[k];
        double span = due[k] - due[k - 1];
        double f = span > 0 ? (t - due[k - 1]) / span : 1.0;
        return (a.X + (b.X - a.X) * f, a.Y + (b.Y - a.Y) * f);
    }

    /// <summary>What goes out next when step <paramref name="i"/> is the first planned sample not yet passed and
    /// <paramref name="elapsedMs"/> of the plan have gone by: (x, y, plan time, next i). On time: step i itself.
    /// Behind: the point of the path at <paramref name="elapsedMs"/> -- or, when one is due, the first pinned step or
    /// the final step, exactly -- pulled back along the path until it lies within <paramref name="cap"/> of the last
    /// dispatched point (the walk then stretches in time). Port of _pacing._target.</summary>
    internal static (double X, double Y, double PlanT, int Next) Target(IReadOnlyList<Step> steps, double[] due, int i,
        double? tPlanLast, (double X, double Y)? last, double elapsedMs, double cap, Func<int, bool>? pinned)
    {
        int n = steps.Count;
        if (elapsedMs <= due[i]) return (steps[i].X, steps[i].Y, due[i], i + 1);
        int j = i;
        while (j + 1 < n && due[j + 1] <= elapsedMs && !(pinned?.Invoke(j) ?? false)) j++;
        bool exact = j == n - 1 || (pinned?.Invoke(j) ?? false);
        double x, y, tp;
        if (exact) { x = steps[j].X; y = steps[j].Y; tp = due[j]; }
        else { (x, y) = PosAt(steps, due, elapsedMs); tp = elapsedMs; }
        if (last is { } l && Math.Sqrt(Sq(x - l.X) + Sq(y - l.Y)) > cap)
        {
            double lo = tPlanLast ?? 0.0, hi = tp;
            for (int k = 0; k < 24; k++)   // the farthest plan time whose point is within the cap
            {
                double mid = (lo + hi) / 2.0;
                var (px, py) = PosAt(steps, due, mid);
                if (Math.Sqrt(Sq(px - l.X) + Sq(py - l.Y)) <= cap) lo = mid; else hi = mid;
            }
            (x, y) = PosAt(steps, due, lo);
            tp = lo;
            exact = false;
            // no progress within the cap (a zero-length planned gap jumps): send step i as planned
            if (tPlanLast is { } tl && tp <= tl + 1e-9) return (steps[i].X, steps[i].Y, due[i], i + 1);
        }
        if (exact) return (x, y, tp, j + 1);
        // the next planned sample still ahead of the point just sent (never one already passed)
        return (x, y, tp, Math.Max(i, BisectRight(due, tp)));
    }

    private static readonly Stopwatch Clock = Stopwatch.StartNew();
    private static double RealNow() => Clock.Elapsed.TotalMilliseconds;
    private static Task RealSleep(double ms) => ms > 0 ? Task.Delay(TimeSpan.FromMilliseconds(ms)) : Task.CompletedTask;

    /// <summary>
    /// Dispatch <paramref name="steps"/> through <paramref name="move"/> against a wall clock. A move
    /// that throws propagates (the caller decides what a closed page means). Returns the number of
    /// points dispatched. <paramref name="maxJump"/>: the persona's hop cap (<see cref="Motion.HopCapPx"/>).
    /// </summary>
    public static async Task<int> WalkAsync(IReadOnlyList<Step> steps, Func<double, double, Task> move,
        Func<double>? now = null, Func<double, Task>? sleep = null, double maxJump = MaxJumpPx,
        Func<int, bool>? pinned = null, string kind = "glide", int buttons = 0)
    {
        now ??= RealNow;
        sleep ??= RealSleep;
        if (steps.Count == 0) return 0;
        // Score hook (HumanizeTrace, SC 232): with CLEARCOTE_HUMANIZE_SCORE set the dispatched samples and their
        // ACTUAL dispatch times are scored after the walk, as `kind` (glide | drag | ambient | hover); `buttons`
        // (a drag leg: 1) is carried on every sample. Read-only: it changes nothing here.
        var trace = HumanizeTrace.Enabled ? new List<MotionSample>() : null;
        var (due, total) = Schedule(steps);
        double t0 = now(), tLast = t0, tStart = t0;
        int i = 0, sent = 0;
        (double X, double Y)? last = null;
        double? tPlanLast = null;
        while (i < steps.Count)
        {
            double el = now() - t0;
            if (el < due[i])
            {
                await sleep(due[i] - el).ConfigureAwait(false);
                el = due[i];
            }
            var (sx, sy, tp, nxt) = Target(steps, due, i, tPlanLast, last, el, JumpCap(maxJump, now() - tLast), pinned);
            double tb = now();
            try { await move(sx, sy).ConfigureAwait(false); }
            catch
            {
                if (trace is { Count: > 0 }) HumanizeTrace.Record(kind, trace, new Dictionary<string, object?> { ["planned_n"] = steps.Count, ["sent"] = sent, ["aborted"] = true });
                throw;
            }
            sent++;
            trace?.Add(new MotionSample(sx, sy, tb - tStart, buttons));
            // Measured on PRO r38 under load: one move answered after 523 ms. Catching that up by skipping
            // coarsened the rest of the glide to maximum hops, and across the fixture's iframe the page saw
            // a 110 px jump. A stall pauses the plan; only StallMs of it is caught up.
            double took = now() - tb;
            if (took > StallMs) t0 += took - StallMs;
            last = (sx, sy);
            tLast = tb;
            tPlanLast = tp;
            i = nxt;
        }
        double end = now() - t0;
        if (end < total) await sleep(total - end).ConfigureAwait(false);   // the last sample's own dwell
        if (trace is { Count: > 0 })
            HumanizeTrace.Record(kind, trace, new Dictionary<string, object?> { ["planned_n"] = steps.Count, ["planned_ms"] = total, ["sent"] = sent });
        return sent;
    }

    /// <summary>How far inside the viewport a planned path is kept (SC 210 fix 2, verifier B: the planner's
    /// overshoot arcs left the window near its edges and the page saw no events while the pointer was outside).</summary>
    public const double ViewMarginPx = 2;

    /// <summary><paramref name="steps"/> with every sample but the last moved inside the viewport
    /// (<see cref="ViewMarginPx"/> in). The last sample -- where the move was aimed -- is never changed, and a
    /// move aimed outside the viewport is returned as it is. Only coordinates change. Port of _pacing.keep_in_view.</summary>
    public static IReadOnlyList<Step> KeepInView(IReadOnlyList<Step> steps, double vw, double vh, double margin = ViewMarginPx)
    {
        if (steps.Count == 0 || vw <= 0 || vh <= 0) return steps;
        var t = steps[^1];
        if (!(t.X >= 0 && t.X <= vw && t.Y >= 0 && t.Y <= vh)) return steps;
        double lx = Math.Min(margin, t.X), hx = Math.Max(vw - margin, t.X);
        double ly = Math.Min(margin, t.Y), hy = Math.Max(vh - margin, t.Y);
        var out_ = new List<Step>(steps.Count);
        for (int k = 0; k < steps.Count; k++)
        {
            var s = steps[k];
            out_.Add(k == steps.Count - 1 ? s : s with { X = Math.Clamp(s.X, lx, hx), Y = Math.Clamp(s.Y, ly, hy) });
        }
        return out_;
    }

    /// <summary>The largest hop between two dispatched points <paramref name="dtMs"/> apart:
    /// <paramref name="maxJump"/>, and <see cref="IdleHopPx"/> after an idle gap. Never more for a longer gap: a
    /// walk that fell behind stretches in time instead of catching up with a jump. Port of _pacing._jump_cap.</summary>
    public static double JumpCap(double maxJump, double dtMs) => dtMs < IdleMs ? maxJump : Math.Min(maxJump, IdleHopPx);

    /// <summary>px/ms arriving at sample <paramref name="k"/> (0 for the first). Port of _pacing._speed.</summary>
    private static double SpeedAt(IReadOnlyList<Step> steps, int k)
    {
        if (k == 0) return 0;
        var a = steps[k - 1];
        var b = steps[k];
        return Math.Sqrt((b.X - a.X) * (b.X - a.X) + (b.Y - a.Y) * (b.Y - a.Y)) / Math.Max(1e-3, a.SleepMs);
    }

    /// <summary>
    /// Cut an ambient plan so it lasts about <paramref name="ms"/> (port of _pacing.trim_to / pacing.ts trimTo, SC 232
    /// lane C: .NET had no ambient until now). <see cref="Motion.PlanAmbient"/> keeps adding whole excursions and rests
    /// until its budget is spent, so it runs well past the ask. Of the samples whose end falls in
    /// [<paramref name="lo"/>·ms, <paramref name="hi"/>·ms], keep up to the slowest one; only a tail is dropped. When
    /// every candidate is mid-flight the cut moves forward to where that move has slowed down (&lt;= 10 % of the plan's
    /// peak speed), within <paramref name="stretch"/>·ms.
    /// </summary>
    public static List<Step> TrimTo(IReadOnlyList<Step> steps, double ms, double lo = 0.85, double hi = 1.10, double stretch = 2.0)
    {
        if (steps.Count == 0 || ms <= 0) return new List<Step>(steps);
        var (due, total) = Schedule(steps);
        if (total <= hi * ms) return new List<Step>(steps);
        int n = steps.Count;
        var end = new double[n];
        for (int k = 0; k < n; k++) end[k] = due[k] + Math.Max(0, steps[k].SleepMs);
        int best = -1;
        double bestV = 0;
        for (int k = 0; k < n; k++)
        {
            if (end[k] < lo * ms) continue;
            if (end[k] > hi * ms) break;
            double v = SpeedAt(steps, k);
            if (best < 0 || v < bestV) { best = k; bestV = v; }
        }
        if (best < 0)
        {
            best = 0;
            for (int k = 0; k < n; k++) if (end[k] <= hi * ms) best = k;
        }
        double peak = 0;
        for (int k = 0; k < n; k++) peak = Math.Max(peak, SpeedAt(steps, k));
        if (peak > 0 && SpeedAt(steps, best) > 0.3 * peak)
        {
            int wide = -1;
            double wideV = 0;
            for (int k = 0; k < n; k++)
            {
                if (end[k] < 0.8 * ms || end[k] > 1.15 * ms) continue;
                double v = SpeedAt(steps, k);
                if (wide < 0 || v < wideV) { wide = k; wideV = v; }
            }
            if (wide >= 0 && wideV <= 0.3 * peak) return steps.Take(wide + 1).ToList();
            for (int k = best + 1; k < n; k++)
            {
                if (end[k] > stretch * ms) break;
                if (SpeedAt(steps, k) <= 0.1 * peak) { best = k; break; }
            }
        }
        return steps.Take(best + 1).ToList();
    }
}
