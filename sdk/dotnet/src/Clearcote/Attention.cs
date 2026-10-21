using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// The attention model (SC 232 lane C): what a reader does BETWEEN the actions a script asks for. A port of
/// sdk/python/clearcote/_attention.py and sdk/node/src/attention.ts (same values; pinned by
/// sdk/testdata/humanize_after_vectors.json). Pure planning: the humanizer calls these at the start of the NEXT
/// action, never from a timer.
/// <list type="bullet">
/// <item>After a page load the first action waits a read pause of 0.8–4 s, scaled by how much text the page shows
/// (read in the isolated world, capped at <see cref="ReadCapChars"/>), and in <see cref="ExploreRate"/> of loads rolls
/// the wheel 1–3 notches first.</item>
/// <item>When more than <see cref="IdleGapS"/> pass between two actions, the hand idles: 1–2 ambient drifts of
/// 300–900 ms before the next action.</item>
/// </list>
/// OFF by default.
/// </summary>
public static class Attention
{
    public const double ReadMinMs = 800;
    public const double ReadMaxMs = 4000;
    public const int ReadCapChars = 20000;
    public const double ExploreRate = 0.30;
    public const double IdleGapS = 1.5;
    public const double IdleDriftMinMs = 300;
    public const double IdleDriftMaxMs = 900;

    /// <summary>Visible text length of the document, capped (run in the isolated world). Same source as Python's.</summary>
    internal const string TextLength = "() => { const b = document.body; if (!b) return 0; let t = ''; try { t = b.innerText || ''; }"
        + " catch (e) { t = b.textContent || ''; } return Math.min(20000, t.length); }";

    private static double Lerp(double a, double b, double t) => a + (b - a) * t;
    private static double Clamp(double v, double lo, double hi) => Math.Max(lo, Math.Min(hi, v));

    /// <summary>The read pause before the first action on a freshly loaded page (ms): <see cref="ReadMinMs"/> for an
    /// empty page, <see cref="ReadMaxMs"/> from <see cref="ReadCapChars"/> of text (square-root growth between), × the
    /// persona's read tempo × 0.85–1.15, kept in the band.</summary>
    public static double ReadPauseMs(double textLen, Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double n = double.IsFinite(textLen) ? textLen : 0;
        double frac = Math.Sqrt(Clamp(n / ReadCapChars, 0, 1));
        double t = Humanize.TempoOf(p);   // the preset (careful / quick) scales the read pause and its band
        double ms = (ReadMinMs + (ReadMaxMs - ReadMinMs) * frac) * Motion.AfterTraitsOf(p).ReadTempo * t * Lerp(0.85, 1.15, rng());
        return Clamp(ms, ReadMinMs * t, ReadMaxMs * t);
    }

    /// <summary>0 (most loads) or 1–3 exploratory wheel notches down, in <see cref="ExploreRate"/> of loads.</summary>
    public static int ExploreNotches(Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        if (rng() >= ExploreRate) return 0;
        return 1 + (int)(rng() * 3);
    }

    /// <summary>The ambient drifts (ms each) after <paramref name="gapS"/> seconds without an action: none up to
    /// <see cref="IdleGapS"/>, else 1–2 of <see cref="IdleDriftMinMs"/>–<see cref="IdleDriftMaxMs"/>.</summary>
    public static List<double> IdleDrifts(double? gapS, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        var outMs = new List<double>();
        if (gapS is not double g || g <= IdleGapS) return outMs;
        int n = 1 + (rng() < 0.5 ? 1 : 0);
        for (int i = 0; i < n; i++) outMs.Add(Lerp(IdleDriftMinMs, IdleDriftMaxMs, rng()));
        return outMs;
    }
}

public static partial class Humanize
{
    // ------------------------------------------------------------ after-action (SC 232 lane C)
    // What the hand does once an action has landed: it rests on a hovered element (DwellAsync), drifts off a
    // clicked one (ScheduleMoveOff / RunMoveOffAsync), and between actions it reads and idles (Attention). All
    // LAZY: run at the start of the next humanized action inside the page gate (SerialAsync -> PreActionAsync),
    // or by HumanSettleAsync, never from a timer. The persona's traits come from a salted stream of its seed
    // (Motion.AfterTraitsOf: every existing draw keeps its order); every per-action draw is unseeded.

    private sealed class AfterState
    {
        public long? LastEnd, LoadAt;
        public (long Due, double X, double Y)? MoveOff;
        public int MoHits, MoN;
        public readonly List<object[]> Trace = new();
        public bool LoadHooked;
    }

    private static readonly ConditionalWeakTable<IPage, AfterState> Afters = new();
    private static AfterState AfterOf(IPage page) => Afters.GetValue(page, _ => new AfterState());

    /// <summary>Hidden switch for the attention model (test seam; the public switch is <c>HumanizeOptions.Ambient = "idle"</c>).</summary>
    internal static readonly ConditionalWeakTable<IPage, StrongBox<bool>> AttentionFlag = new();

    /// <summary>Test seam for the page's ambient mode, "off" | "load" | "idle", consulted when the page has no
    /// <see cref="HumanizeOptions"/> (absent means "off"). "load" = an ambient burst after every load; "idle" = the
    /// attention model.</summary>
    internal static readonly ConditionalWeakTable<IPage, string> AmbientModes = new();

    /// <summary>Test seam: what the after-action model did on this page, newest last (bounded).</summary>
    internal static IReadOnlyList<object[]> AfterTrace(IPage page) => AfterOf(page).Trace;

    private static long NowMs() => Environment.TickCount64;

    private static string AmbientMode(IPage page)
    {
        // The page's HumanizeOptions.Ambient (Attach(page, options)) wins; the internal table is the test seam.
        var m = OptionsFor(page)?.Ambient;
        if (m is null && (!AmbientModes.TryGetValue(page, out m) || m is null)) return "off";
        m = m.Trim().ToLowerInvariant();
        return m is "load" or "idle" ? m : "off";
    }

    private static bool AttentionOn(IPage page)
        => (AttentionFlag.TryGetValue(page, out var f) && f.Value) || AmbientMode(page) == "idle";

    // Per-action draws are UNSEEDED, as in PlanMove: the seed fixes the persona's traits only (Motion.AfterTraitsOf);
    // a seeded per-action sequence would replay identically in every session of an identity.
    private static Rng AfterRngOf(IPage page, Persona p)
    {
        _ = page; _ = p;
        return Random.Shared.NextDouble;
    }

    private static void Trace(AfterState af, params object[] e)
    {
        af.Trace.Add(e);
        if (af.Trace.Count > 256) af.Trace.RemoveRange(0, af.Trace.Count - 256);
    }

    /// <summary>Hook the page's Load event once: the attention model's read pause counts from the last load, and
    /// ambient "load" runs an ambient burst after it.</summary>
    private static void HookLoad(IPage page)
    {
        var af = AfterOf(page);
        if (af.LoadHooked) return;
        af.LoadHooked = true;
        page.Load += (_, _) =>
        {
            AfterOf(page).LoadAt = NowMs();
            if (AmbientMode(page) == "load") _ = LoadAmbientAsync(page);
        };
    }

    private static async Task LoadAmbientAsync(IPage page)
    {
        try { await page.HumanAmbientMotionAsync(Rand(450, 950)).ConfigureAwait(false); }
        catch (Exception) { /* best effort: the page may be gone */ }
    }

    /// <summary><see cref="Motion.PlanAmbient"/> from the tracked position — or, on a page the pointer has not been over
    /// yet, from a point above the view with the samples still above it dropped (<see cref="Motion.EntryPoint"/> /
    /// <see cref="Motion.EnterFromTop"/>): the drift's first excursion comes in over the top edge, as GlideAsync's first
    /// move does (pro-r43-13's rule, lab row H 13). Planned <see cref="Motion.EntryLeadMs"/> longer then, so the dropped
    /// lead-in (~100–200 ms) leaves the walk no shorter than <paramref name="ms"/>; the caller trims to it. <paramref
    /// name="rng"/> null: unseeded (the attention model passes its own). Python <c>_ambient_plan</c>, Node <c>ambientPlan</c>.</summary>
    private static List<Step> AmbientPlan(PointerState st, Persona p, double w, double h, double ms, Rng? rng = null)
    {
        if (st.Known) return Motion.PlanAmbient(new Point(st.X, st.Y), w, h, p, ms, rng);
        var start = Motion.EntryPoint(new Point(st.X, st.Y), w, rng);
        return Motion.EnterFromTop(Motion.PlanAmbient(start, w, h, p, ms + Motion.EntryLeadMs, rng), start, rng);
    }

    /// <summary>
    /// Ambient cursor activity for about <paramref name="ms"/>: idle drift and a few non-goal moves inside the central
    /// band of the viewport (<see cref="Motion.PlanAmbient"/>, cut by <see cref="Pacing.TrimTo"/>), so a behavioral
    /// collector sees pointer entropy before the first goal action. It never presses. Python's
    /// <c>page.ambient_motion(ms)</c> and Node's <c>page.ambientMotion(ms)</c>.
    /// </summary>
    public static async Task HumanAmbientMotionAsync(this IPage page, double ms = 1200)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, async () =>
        {
            var p = PersonaFor(page);
            var st = StateFor(page);
            var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
            // PlanAmbient overruns its budget (whole excursions, rests drawn at 0.9–1.4x a frame): cut the plan at a
            // slow sample near ms so the walk lasts about ms. On a page the pointer has not been over yet the drift
            // comes in over the top edge (AmbientPlan).
            var steps = Pacing.TrimTo(AmbientPlan(st, p, w, h, ms), ms);
            await WalkStepsAsync(page, st, steps).ConfigureAwait(false);
        }).ConfigureAwait(false);
    }

    /// <summary>
    /// Let the hand settle after the last action: wait for a pending move-off (scheduled by a humanized click in the
    /// persona's 60–85 % of clicks, due 200–700 ms after the release) to come due and make it -- one short glide
    /// 40–200 px away, downward. False when none was pending or the pointer has moved since. Python's
    /// <c>page.settle()</c>, Node's <c>page.settle()</c>.
    /// </summary>
    public static Task<bool> HumanSettleAsync(this IPage page)
    {
        ArgumentNullException.ThrowIfNull(page);
        return SerialAsync(page, async () =>
        {
            if (!Personas.TryGetValue(page, out var p)) return false;   // no persona: nothing was ever scheduled
            bool done = await RunMoveOffAsync(page, p, StateFor(page), wait: true).ConfigureAwait(false);
            if (done) AfterOf(page).LastEnd = NowMs();
            return done;
        }, after: false);
    }

    /// <summary>Walk planned after-action steps (ambient drift, hover dwell): the preset's tempo scales the sample gaps
    /// as GlideAsync does, and the score hook (HumanizeTrace) sees them as <paramref name="kind"/>.</summary>
    private static async Task WalkStepsAsync(IPage page, PointerState st, IReadOnlyList<Step> steps, string kind = "ambient")
    {
        if (steps.Count == 0) return;
        var vp = await ViewBoundsAsync(page, st).ConfigureAwait(false);
        double t = Tempo(page);   // HumanizeOptions.Preset: every planned duration x1.4 / x1.0 / x0.7 (as GlideAsync)
        if (t != 1.0) steps = steps.Select(s => s with { SleepMs = s.SleepMs * t }).ToList();
        await Pacing.WalkAsync(vp is { } v ? Pacing.KeepInView(steps, v.W, v.H) : steps,
            (x, y) => page.Mouse.MoveAsync((float)x, (float)y), kind: kind).ConfigureAwait(false);
        st.X = steps[^1].X;
        st.Y = steps[^1].Y;
        st.Known = true;
    }

    /// <summary>Rest on a hover target: OU drift + tremor within 3 px of the arrival point and inside the box, for the
    /// persona's 150–900 ms (<paramref name="dwell"/> ms overrides it; 0 or less: no dwell).</summary>
    private static async Task DwellAsync(IPage page, Persona p, PointerState st, double x, double y,
        LocatorBoundingBoxResult bb, double? dwell)
    {
        var rng = AfterRngOf(page, p);
        double ms;
        if (dwell is double d && double.IsFinite(d))
        {
            if (d <= 0) return;
            ms = d;
        }
        else ms = Motion.DwellMs(p, rng);
        var steps = Motion.PlanDwell(new Point(x, y), new Box(bb.X, bb.Y, bb.Width, bb.Height), p, ms, rng);
        Trace(AfterOf(page), "dwell", ms, steps.Count);
        await WalkStepsAsync(page, st, steps, kind: "hover").ConfigureAwait(false);
    }

    /// <summary>After a press: in the persona's 60–85 % of clicks a move-off comes due 200–700 ms after the release.
    /// It runs at the start of the next action (or in HumanSettleAsync) if it is due by then and nothing has moved the
    /// pointer since; otherwise it is dropped -- the next glide is the move-off.</summary>
    private static void ScheduleMoveOff(IPage page, Persona p, PointerState st)
    {
        var af = AfterOf(page);
        var rng = AfterRngOf(page, p);
        bool hit = Motion.MoveOffRoll(p, rng, af.MoHits, af.MoN);
        af.MoN++;
        af.MoveOff = null;
        if (hit)
        {
            af.MoHits++;
            af.MoveOff = (NowMs() + (long)Math.Round(Motion.MoveOffDelayMs(p, rng)), st.X, st.Y);
        }
    }

    private static async Task<bool> RunMoveOffAsync(IPage page, Persona p, PointerState st, bool wait)
    {
        var af = AfterOf(page);
        var mo = af.MoveOff;
        af.MoveOff = null;
        if (mo is not { } m || !st.Known || st.X != m.X || st.Y != m.Y) return false;
        long left = m.Due - NowMs();
        if (left > 0)
        {
            if (!wait) return false;
            await Task.Delay((int)left).ConfigureAwait(false);
        }
        var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
        var to = Motion.PlanMoveOff(new Point(st.X, st.Y), w, h, p, AfterRngOf(page, p));
        Trace(af, "move_off", st.X, st.Y, to.X, to.Y);
        await GlideAsync(page, p, st, to.X, to.Y, 140).ConfigureAwait(false);   // one coarse glide: nothing to acquire
        return true;
    }

    private static async Task IdleDriftAsync(IPage page, Persona p, PointerState st, double ms)
    {
        Trace(AfterOf(page), "drift", ms);
        // No placing glide first: AmbientPlan itself comes in over the top edge on a page the pointer has not been
        // over yet, inside the drift's budget (Node idleDrift, Python _idle_drift; SC 239).
        var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
        var steps = Pacing.TrimTo(AmbientPlan(st, p, w, h, ms, AfterRngOf(page, p)), ms);
        await WalkStepsAsync(page, st, steps).ConfigureAwait(false);
    }

    private static async Task AttendAsync(IPage page, Persona p, PointerState st, bool coords, long now)
    {
        var af = AfterOf(page);
        var rng = AfterRngOf(page, p);
        // `now`: when the action started -- a move-off glide just made does not count as idle time
        if (af.LoadAt is long la)
        {
            af.LoadAt = null;
            double n = 0;
            try
            {
                var r = await IsolatedWorld.For(page).EvaluateAsync(Attention.TextLength).ConfigureAwait(false);
                if (r is { ValueKind: JsonValueKind.Number } e) n = e.GetDouble();
            }
            catch (Exception) { /* no frame: an empty page's pause */ }
            double pause = Attention.ReadPauseMs(n, p, rng);
            int notches = Attention.ExploreNotches(rng);
            Trace(af, "read", pause, n, coords ? 0 : notches);
            double left = pause - (now - la);
            if (left > 350) await IdleDriftAsync(page, p, st, Math.Min(left * 0.5, Attention.IdleDriftMaxMs)).ConfigureAwait(false);
            long rest = la + (long)Math.Round(pause) - NowMs();
            if (rest > 0) await Task.Delay((int)rest).ConfigureAwait(false);
            // A coordinate action names a point in the current layout: never scroll that away.
            if (notches > 0 && !coords)
                await WheelCoreAsync(page, 0, notches * await NotchPxAsync(page, st).ConfigureAwait(false)).ConfigureAwait(false);
            return;
        }
        if (af.LastEnd is long le)
            foreach (var ms in Attention.IdleDrifts((now - le) / 1000.0, rng))
                await IdleDriftAsync(page, p, st, ms).ConfigureAwait(false);
    }

    /// <summary>The lazy after-action step, run by SerialAsync for the outermost action once the gate is held.</summary>
    private static async Task PreActionAsync(IPage page, bool coords)
    {
        // Only on a page that has a persona: PersonaFor would CREATE one, and that changes what other humanized
        // paths do on a page that never had one (press-and-hold walks raw moves there).
        if (!Personas.TryGetValue(page, out var p)) return;
        try
        {
            var st = StateFor(page);
            long now = NowMs();
            await RunMoveOffAsync(page, p, st, wait: false).ConfigureAwait(false);
            if (AttentionOn(page)) await AttendAsync(page, p, st, coords, now).ConfigureAwait(false);
        }
        catch (Exception e) when (e is PlaywrightException or InvalidOperationException or TimeoutException)
        {
            // the after-action model never stops the action
        }
    }
}
