using System;
using System.Collections.Generic;
using System.Linq;
using Clearcote;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Motion-model tests. The cross-SDK vectors are the important ones: the persona is derived from the
/// FINGERPRINT SEED, so a profile driven from Python one day and .NET the next must move the same way.
/// That only holds if mulberry32 and the FNV-1a seed hash agree bit for bit with the other SDKs —
/// which is easy to break silently, because a subtly different RNG still looks perfectly random.
///
/// The expected values below were produced by the NODE implementation and pasted in. If a change to
/// Motion.cs turns these red, the port has drifted and the personas have forked; do not "fix" the
/// expectations without re-deriving them from Node and Python.
/// </summary>
public class MotionTests
{
    // Emitted by the Node implementation (sdk/node/dist/motion.js) — see the class comment.
    [Theory]
    [InlineData("clearcote", 840974591u)]
    [InlineData("", 2166136261u)]
    [InlineData("a", 3826002220u)]
    public void HashSeed_matches_node(string seed, uint expected)
        => Assert.Equal(expected, Motion.HashSeed(seed));

    [Fact]
    public void Mulberry32_stream_matches_node()
    {
        var rng = Motion.Mulberry32(12345);
        var got = Enumerable.Range(0, 5).Select(_ => rng()).ToArray();
        var want = new[]
        {
            0.979728267761, 0.3067522645, 0.484205421526, 0.817934412509, 0.509428369347,
        };
        for (int i = 0; i < want.Length; i++)
            Assert.True(Math.Abs(got[i] - want[i]) < 1e-9, $"draw {i}: got {got[i]}, want {want[i]}");
    }

    [Fact]
    public void Persona_matches_node_for_the_same_seed()
    {
        // The whole cross-SDK contract in one assertion: same seed, same motor signature. Every
        // field is drawn from the shared RNG stream, so a single misordered draw shifts them all.
        var p = Motion.MakePersona("identity-42");
        Assert.Equal(1361500432u, p.Seed);
        Assert.Equal(151, p.DeviceHz);
        Assert.Equal(8.919656302, p.TremorHz, 8);
        Assert.Equal(133.927525934, p.FittsA, 8);
        Assert.Equal(0.878270189, p.PrimaryFrac, 8);
        Assert.Equal(2, p.MaxCorrections);
        Assert.Equal(-0.172724717, p.ApproachBias, 8);
    }

    [Fact]
    public void Persona_is_stable_for_a_seed()
    {
        var a = Motion.MakePersona("identity-42");
        var b = Motion.MakePersona("identity-42");
        Assert.Equal(a.Seed, b.Seed);
        Assert.Equal(a.DeviceHz, b.DeviceHz);
        Assert.Equal(a.TremorHz, b.TremorHz);
        Assert.Equal(a.FittsA, b.FittsA);
        Assert.Equal(a.ApproachBias, b.ApproachBias);
    }

    [Fact]
    public void Persona_differs_between_seeds()
    {
        var a = Motion.MakePersona("identity-a");
        var b = Motion.MakePersona("identity-b");
        Assert.NotEqual(a.Seed, b.Seed);
    }

    [Fact]
    public void Persona_ranges_are_inside_the_documented_envelopes()
    {
        for (int i = 0; i < 200; i++)
        {
            var p = Motion.MakePersona($"seed-{i}");
            Assert.InRange(p.DeviceHz, 110, 155);
            Assert.InRange(p.TremorHz, 8, 12);
            Assert.InRange(p.PrimaryFrac, 0.84, 0.94);
            Assert.InRange(p.MaxCorrections, 1, 3);
            Assert.InRange(p.ApproachBias, -0.25, 0.25);
        }
    }

    [Fact]
    public void KeyDwell_stays_in_the_human_window_and_is_never_instant()
    {
        // The whole point of the 0.19.3 fix in the other SDKs: a scripted keystroke holds ~1–3ms,
        // which no finger does. Every draw must clear the floor.
        var p = Motion.MakePersona("dwell");
        var rng = Motion.Mulberry32(7);
        for (int i = 0; i < 500; i++)
        {
            double d = Motion.KeyDwell(p, rng);
            Assert.InRange(d, p.KeyDwellMinMs, p.KeyDwellMaxMs);
            Assert.True(d >= 45, $"key dwell {d}ms is below the human floor");
        }
    }

    [Fact]
    public void ClickHold_stays_in_the_human_window()
    {
        var p = Motion.MakePersona("hold");
        var rng = Motion.Mulberry32(9);
        for (int i = 0; i < 500; i++)
        {
            double d = Motion.ClickHold(p, rng);
            Assert.InRange(d, p.ClickHoldMinMs, p.ClickHoldMaxMs);
            Assert.True(d >= 60, $"click hold {d}ms is below the human floor");
        }
    }

    [Fact]
    public void ClickPoint_disperses_and_stays_inside_the_box()
    {
        // pointer-landing-dispersion fails when a driver lands on the identical point every time.
        var p = Motion.MakePersona("aim");
        var rng = Motion.Mulberry32(11);
        var box = new Box(100, 100, 120, 40);
        var pts = new List<Point>();
        for (int i = 0; i < 40; i++)
        {
            var pt = Motion.ClickPoint(box, new Point(0, 0), p, rng);
            Assert.InRange(pt.X, box.X, box.X + box.Width);
            Assert.InRange(pt.Y, box.Y, box.Y + box.Height);
            pts.Add(pt);
        }
        int distinct = pts.Select(q => (Math.Round(q.X, 1), Math.Round(q.Y, 1))).Distinct().Count();
        Assert.True(distinct > 30, $"only {distinct}/40 distinct landings — not enough dispersion");
    }

    [Fact]
    public void ClickPoint_on_a_tiny_box_returns_the_centre_rather_than_escaping_it()
    {
        var p = Motion.MakePersona("tiny");
        var box = new Box(10, 10, 3, 3);   // below the 4px inset: the clamp would invert
        var pt = Motion.ClickPoint(box, new Point(0, 0), p, Motion.Mulberry32(3));
        Assert.Equal(11.5, pt.X, 6);
        Assert.Equal(11.5, pt.Y, 6);
    }

    [Fact]
    public void PlanMove_produces_a_travelled_path_that_lands_on_target()
    {
        var p = Motion.MakePersona("path");
        var steps = Motion.PlanMove(new Point(0, 0), new Point(600, 400), p, 40, false, Motion.Mulberry32(5));
        Assert.True(steps.Count >= 10, $"only {steps.Count} samples for a 720px move");
        var last = steps[^1];
        Assert.Equal(600, last.X, 6);
        Assert.Equal(400, last.Y, 6);
        Assert.All(steps, s => Assert.InRange(s.SleepMs, 0, 200));
        // pointer-movement-precedes-click: the path must actually visit distinct places.
        Assert.True(steps.Select(s => (Math.Round(s.X), Math.Round(s.Y))).Distinct().Count() > 5);
    }

    [Fact]
    public void PlanMove_for_a_negligible_distance_emits_a_single_sample()
    {
        var p = Motion.MakePersona("hop");
        var steps = Motion.PlanMove(new Point(10, 10), new Point(10.5, 10.5), p, 24, false, Motion.Mulberry32(2));
        Assert.Single(steps);
    }

    [Fact]
    public void MinJerk_is_a_monotone_zero_to_one_easing()
    {
        Assert.Equal(0, Motion.MinJerk(0), 9);
        Assert.Equal(1, Motion.MinJerk(1), 9);
        Assert.Equal(0.5, Motion.MinJerk(0.5), 9);
        Assert.Equal(0, Motion.MinJerk(-3), 9);   // clamped
        Assert.Equal(1, Motion.MinJerk(4), 9);
        double prev = -1;
        for (double t = 0; t <= 1.0001; t += 0.05)
        {
            double v = Motion.MinJerk(t);
            Assert.True(v >= prev, $"not monotone at {t}");
            prev = v;
        }
    }
}
