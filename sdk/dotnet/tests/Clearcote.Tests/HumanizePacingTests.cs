using System.Diagnostics;
using System.Reflection;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// Wall-clock pacing of planned pointer paths (SC 210 lane P; mirrors the Python and Node tests).
// The planner returns ~130 samples ~6.6 ms apart for a ~1 s glide. The old loop awaited every move and
// THEN slept the planned gap, so a 27 ms move round trip (PRO r38 median) turned a ~1 s glide into
// 3.5-5.4 s (SC 209). The glide is driven through the real (private) GlideAsync with a fake mouse whose
// move costs 0 or 25 ms. .NET's planner draws from Random.Shared, so the planned duration is bounded by
// sampling the same planner for the same move; on 0.48.0 the 25 ms case runs ~3x past that bound.
public class HumanizePacingTests
{
    public class Mouse : DispatchProxy
    {
        public int LatencyMs { get; set; }
        public Stopwatch Clock { get; } = Stopwatch.StartNew();
        public List<(string Op, double T, float X, float Y)> Log { get; } = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            return method?.Name switch
            {
                "MoveAsync" => Ev("move", (float)args![0]!, (float)args[1]!),
                "DownAsync" => Ev("down", 0, 0),
                "UpAsync" => Ev("up", 0, 0),
                _ => Task.CompletedTask,
            };
        }

        private async Task Ev(string op, float x, float y)
        {
            // The SEND time: the pacer's hop cap counts from when it sent the previous sample. Stamped after
            // the latency, Task.Delay's own jitter (a 25 ms delay answering in 25-40 ms) shrank the measured
            // gap below the pacer's and failed the hop check on a hop the pacer was allowed (97.9 px in 32 ms).
            double t = Clock.Elapsed.TotalMilliseconds;
            if (LatencyMs > 0) await Task.Delay(LatencyMs);
            lock (Log) Log.Add((op, t, x, y));
        }
    }

    private static readonly MethodInfo Glide = typeof(Humanize).GetMethod("GlideAsync", BindingFlags.NonPublic | BindingFlags.Static)!;
    private static readonly Type StateType = typeof(Humanize).GetNestedType("PointerState", BindingFlags.NonPublic)!;

    private static object State(double x, double y)
    {
        var st = Activator.CreateInstance(StateType, nonPublic: true)!;
        StateType.GetField("X")!.SetValue(st, x);
        StateType.GetField("Y")!.SetValue(st, y);
        StateType.GetField("Known")!.SetValue(st, true);
        return st;
    }

    private static (double Lo, double Hi, double MaxGap) PlannedRange(Persona p, Point from, Point to, double w, bool settle)
    {
        double lo = double.MaxValue, hi = 0, gap = 0;
        for (int k = 0; k < 400; k++)
        {
            var plan = Motion.PlanMove(from, to, p, w, settle);
            double sum = plan.Sum(s => s.SleepMs);
            lo = Math.Min(lo, sum); hi = Math.Max(hi, sum);
            var prev = from;
            foreach (var s in plan)
            {
                gap = Math.Max(gap, Math.Sqrt((s.X - prev.X) * (s.X - prev.X) + (s.Y - prev.Y) * (s.Y - prev.Y)));
                prev = new Point(s.X, s.Y);
            }
        }
        return (lo, hi, gap);
    }

    [Theory]
    [InlineData(0, false)]
    [InlineData(25, false)]
    [InlineData(25, true)]    // the held-drag leg (settle) DragToAsync and range drags walk
    public async Task Glide_lasts_its_planned_duration(int latencyMs, bool settle)
    {
        var mouse = DispatchProxy.Create<IMouse, Mouse>();
        var m = (Mouse)(object)mouse;
        m.LatencyMs = latencyMs;
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        var persona = Motion.MakePersona("pace-net");
        var moves = new[] { (150.0, 150.0, 900.0, 600.0), (900.0, 600.0, 150.0, 150.0), (200.0, 300.0, 560.0, 310.0) };
        foreach (var (fx, fy, tx, ty) in moves)
        {
            var (lo, hi, ownGap) = PlannedRange(persona, new Point(fx, fy), new Point(tx, ty), 24, settle);
            int n0 = m.Log.Count;
            var sw = Stopwatch.StartNew();
            await (Task)Glide.Invoke(null, new object?[] { page, persona, State(fx, fy), tx, ty, 24.0, settle, false })!;
            double took = sw.Elapsed.TotalMilliseconds;
            Assert.True(took >= 0.8 * lo && took <= 1.2 * hi,
                $"glide took {took:F0} ms; the planner plans {lo:F0}-{hi:F0} ms for this move");
            var leg = m.Log.Skip(n0).ToList();
            Assert.All(leg, e => Assert.Equal("move", e.Op));
            Assert.Equal(((float)tx, (float)ty), (leg[^1].X, leg[^1].Y));
            // No hop longer than a fast hand covers between the two events (3 px/ms, >= 40 px or the plan's own).
            double first = Math.Sqrt((leg[0].X - fx) * (leg[0].X - fx) + (leg[0].Y - fy) * (leg[0].Y - fy));
            Assert.True(first <= Math.Max(40, ownGap) + 0.01, $"the first sample hopped {first:F1} px");
            for (int k = 1; k < leg.Count; k++)
            {
                double d = Math.Sqrt(Math.Pow(leg[k].X - leg[k - 1].X, 2) + Math.Pow(leg[k].Y - leg[k - 1].Y, 2));
                double cap = Math.Max(Math.Max(40, ownGap), Pacing.FastPxPerMs * (leg[k].T - leg[k - 1].T));
                Assert.True(d <= cap + 0.01, $"a skipped stretch hopped {d:F1} px in {leg[k].T - leg[k - 1].T:F0} ms");
            }
        }
    }

    private sealed class FakeClock
    {
        public double T;
        public List<(double X, double Y, double T)> Sent { get; } = new();
        public Func<double, double, Task> Move(double lat) => (x, y) => { T += lat; Sent.Add((x, y, T)); return Task.CompletedTask; };
        public Task Sleep(double ms) { T += Math.Max(0, ms); return Task.CompletedTask; }
    }

    [Fact]
    public async Task Pacer_never_drops_a_pinned_step_or_the_last()
    {
        var steps = Enumerable.Range(0, 200).Select(i => new Step(i, 0, 2)).ToList();
        var pins = new HashSet<int> { 37, 38, 120 };
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, c.Move(25), () => c.T, c.Sleep, pinned: pins.Contains);
        var xs = c.Sent.Select(s => s.X).ToList();
        Assert.True(xs.Count < 100);
        foreach (var k in new[] { 37, 38, 120, 199 }) Assert.Contains((double)k, xs);
        Assert.Equal(xs.OrderBy(v => v), xs);
    }

    [Fact]
    public async Task Pacer_on_time_dispatches_every_sample_at_its_planned_time()
    {
        var steps = Enumerable.Range(0, 50).Select(i => new Step(i * 3, 0, 7)).ToList();
        var c = new FakeClock();
        Assert.Equal(50, await Pacing.WalkAsync(steps, c.Move(0), () => c.T, c.Sleep));
        for (int k = 0; k < 50; k++) Assert.Equal(7.0 * k, c.Sent[k].T, 6);
        Assert.Equal(350.0, c.T, 6);
    }

    [Theory]
    [InlineData(200)]
    [InlineData(350)]
    [InlineData(500)]
    public async Task Pacer_after_a_long_stall_stretches_instead_of_jumping(double stall)
    {
        // SC 210 lab v2: 35 hops of 60-103 px a few ms apart on r38 under load. The old cap (3 px/ms x the time
        // since the last dispatch) let a slow dispatch turn into one big catch-up hop. Now <= 40 px, and
        // <= 20 px right after an idle gap: the walk takes longer instead.
        var steps = Enumerable.Range(0, 300).Select(i => new Step(i * 3, 0, 2)).ToList();
        var c = new FakeClock();
        var stalls = new HashSet<int> { 3, 9, 15 };
        await Pacing.WalkAsync(steps, (x, y) => { c.T += stalls.Contains(c.Sent.Count) ? stall : 25; c.Sent.Add((x, y, c.T)); return Task.CompletedTask; },
            () => c.T, c.Sleep);
        var hops = c.Sent.Zip(c.Sent.Skip(1), (a, b) => b.X - a.X).ToList();
        Assert.True(hops.Max() <= 40 + 1e-6, $"a hop of {hops.Max()} px");
        foreach (var k in stalls) Assert.True(hops[k] <= 20 + 1e-6, $"the hop after stall {k}: {hops[k]} px");
        Assert.Equal(299 * 3.0, c.Sent[^1].X);
    }

    [Fact]
    public async Task Pacer_pauses_through_a_stall_instead_of_skipping_after_it()
    {
        // Measured on PRO r38 under load: one move answered after 523 ms, the pacer then skipped to catch
        // up and the fixture's iframe crossing showed the page a 110 px jump. Now the plan pauses.
        var steps = Enumerable.Range(0, 200).Select(i => new Step(i * 2, 0, 5)).ToList();
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, (x, y) => { c.T += c.Sent.Count == 10 ? 500 : 2; c.Sent.Add((x, y, c.T)); return Task.CompletedTask; },
            () => c.T, c.Sleep);
        Assert.True(c.Sent.Count >= 185, $"{c.Sent.Count} sent");
        Assert.True(Math.Abs(c.T - (1000 + 500 - Pacing.StallMs)) < 30, $"took {c.T}");
        Assert.Equal(398.0, c.Sent[^1].X);
    }

    [Fact]
    public async Task Pacer_dispatches_the_in_between_point_instead_of_a_long_jump()
    {
        var steps = Enumerable.Range(0, 40).Select(i => new Step(i * 30, 0, 1)).ToList();
        var c = new FakeClock();
        await Pacing.WalkAsync(steps, c.Move(25), () => c.T, c.Sleep);
        double prev = 0, gap = 0;
        foreach (var s in c.Sent) { gap = Math.Max(gap, s.X - prev); prev = s.X; }
        // 30 px/ms is ten times a fast hand: every hop stays within what one covers in a 25 ms dispatch.
        Assert.True(gap <= Pacing.FastPxPerMs * 25 + 1e-6, $"hop {gap}");
        Assert.True(c.Sent.Count > 10);
        Assert.Equal(39 * 30.0, c.Sent[^1].X);
    }

    private static readonly MethodInfo Drift = typeof(Humanize).GetMethod("IdleDriftAsync", BindingFlags.NonPublic | BindingFlags.Static)!;

    // The attention model's idle drift on a page the pointer has not been over yet (Node idleDrift, Python _idle_drift;
    // SC 239): the drift itself comes in over the top edge, inside its budget -- its first event is at y 0-4 and the
    // walk lasts about ms. Before, a placing glide to a random interior point went first, so the drift ran a whole
    // glide (~0.4-1 s) past its budget.
    [Theory]
    [InlineData(600)]
    [InlineData(1200)]
    public async Task Idle_drift_on_a_fresh_page_enters_over_the_top_edge_inside_its_budget(double ms)
    {
        var mouse = DispatchProxy.Create<IMouse, Mouse>();
        var m = (Mouse)(object)mouse;
        var page = DispatchProxy.Create<IPage, HumanizeShiftTests.Recorder>();
        ((HumanizeShiftTests.Recorder)(object)page).Mouse = mouse;
        var st = Activator.CreateInstance(StateType, nonPublic: true)!;   // Known false: nothing placed it yet
        var sw = Stopwatch.StartNew();
        await (Task)Drift.Invoke(null, new object?[] { page, Motion.MakePersona("drift-net"), st, ms })!;
        double took = sw.Elapsed.TotalMilliseconds;
        var mv = m.Log.Where(e => e.Op == "move").ToList();
        Assert.True(mv.Count > 10, $"{mv.Count} moves");
        Assert.InRange(mv[0].Y, 0, 4);
        Assert.All(mv, e => Assert.True(e.X >= 0 && e.X <= 1280 && e.Y >= 0 && e.Y <= 800, $"({e.X}, {e.Y})"));
        Assert.True(took >= 0.75 * ms && took <= 1.25 * ms + 150, $"a {ms} ms drift took {took:F0} ms");
    }

    [Fact]
    public void Keep_in_view_moves_samples_inside_but_never_the_target()
    {
        var steps = new[] { new Step(10, 5, 8), new Step(-12, -3, 8), new Step(1290, 400, 8), new Step(1279, 799, 8) };
        Assert.Equal(new[] { (10.0, 5.0), (2.0, 2.0), (1279.0, 400.0), (1279.0, 799.0) },
            Pacing.KeepInView(steps, 1280, 800).Select(s => (s.X, s.Y)).ToArray());
        var aimedOut = new[] { new Step(10, 5, 8), new Step(1300, 900, 8) };
        Assert.Same(aimedOut, Pacing.KeepInView(aimedOut, 1280, 800));
    }
}
