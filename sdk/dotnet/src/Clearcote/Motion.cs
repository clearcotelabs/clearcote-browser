using System;
using System.Collections.Generic;

namespace Clearcote;

// Motor-persona model — a faithful port of sdk/node/src/motion.ts and sdk/python/clearcote/_motion.py.
//
// WHY IT IS A PORT AND NOT A REWRITE. The persona is derived from the FINGERPRINT SEED, so the same
// identity must produce the same motor signature in every SDK: a profile driven from Python one day
// and from .NET the next has to move the same way, or the "stable per-identity persona" the other two
// SDKs promise is a lie the moment a caller switches language. That means the RNG and the hash have to
// agree bit for bit, not merely "be random" — which is why Mulberry32 and the FNV-1a seed hash below
// are transcribed operation for operation rather than replaced with System.Random and GetHashCode.
//
// Two places where C# and JavaScript disagree and the JS behaviour is the one that must win:
//   * Math.imul is a 32-bit signed multiply that WRAPS. C# `*` on int wraps only in an unchecked
//     context, and the project may compile checked, so Imul() below is explicit.
//   * `>>> 0` coerces to uint32. C# `>>` on int is arithmetic (sign-propagating), so every place the
//     TS reads `>>> n` uses a uint here.
//
// PlanAmbient is deliberately not ported yet — it exists in the other two SDKs for pre-challenge idle
// entropy and nothing in the .NET surface calls it. Porting it without a caller would be untested code.

/// <summary>Deterministic 0..1 generator, matching the other SDKs' mulberry32.</summary>
public delegate double Rng();

/// <summary>The per-identity motor signature. Ranges sit inside measured human envelopes.</summary>
public sealed class Persona
{
    public uint Seed { get; init; }
    /// <summary>Cursor sampling cadence (Hz) → step spacing ≈ 1000/DeviceHz ms (real mice: 125–1000 Hz).</summary>
    public double DeviceHz { get; init; }
    /// <summary>Physiological tremor frequency (Hz), band 8–12.</summary>
    public double TremorHz { get; init; }
    public double TremorAmp { get; init; }
    /// <summary>Ornstein–Uhlenbeck drift amplitude (px) — slow low-frequency wander.</summary>
    public double DriftAmp { get; init; }
    public double DriftTau { get; init; }
    public double Jitter { get; init; }
    /// <summary>Fitts intercept a (ms) and slope b (ms/bit).</summary>
    public double FittsA { get; init; }
    public double FittsB { get; init; }
    public double PrimaryFrac { get; init; }
    public double Overshoot { get; init; }
    public int MaxCorrections { get; init; }
    public double ApproachBias { get; init; }
    public double GrabMinMs { get; init; } = 130;
    public double GrabMaxMs { get; init; } = 360;
    public double ReleaseMinMs { get; init; } = 90;
    public double ReleaseMaxMs { get; init; } = 230;
    /// <summary>Click press-hold (mousedown→mouseup). Human ~60–150ms; a scripted click is ~2ms.</summary>
    public double ClickHoldMinMs { get; init; } = 60;
    public double ClickHoldMaxMs { get; init; } = 150;
    /// <summary>Per-key dwell (keydown→keyup). Human ~45–120ms; scripted typing ~2–3ms.</summary>
    public double KeyDwellMinMs { get; init; } = 45;
    public double KeyDwellMaxMs { get; init; } = 120;
}

public readonly record struct Point(double X, double Y);
/// <summary>One dispatched sample: move to (X,Y), then sleep SleepMs before the next.</summary>
public readonly record struct Step(double X, double Y, double SleepMs);
public readonly record struct Box(double X, double Y, double Width, double Height);

public static class Motion
{
    // ---- primitives that must match the other SDKs exactly -------------------------------------

    /// <summary>JavaScript Math.imul: 32-bit signed multiply that wraps.</summary>
    private static int Imul(int a, int b) => unchecked((int)((long)a * b));

    /// <summary>mulberry32, transcribed from the TS. Same seed ⇒ same stream in every SDK.</summary>
    public static Rng Mulberry32(uint seedInt)
    {
        uint a = seedInt;
        return () =>
        {
            unchecked
            {
                a = (uint)((int)a + 0x6d2b79f5);
                int t = Imul((int)(a ^ (a >> 15)), (int)(1u | a));
                t = (t + Imul(t ^ (int)((uint)t >> 7), (int)(61u | (uint)t))) ^ t;
                return ((uint)t ^ ((uint)t >> 14)) / 4294967296.0;
            }
        };
    }

    /// <summary>FNV-1a hash of an arbitrary seed to a 32-bit int for the persona RNG.</summary>
    public static uint HashSeed(object? seed)
    {
        if (seed is null) return (uint)(Random.Shared.NextDouble() * 0xffffffff);
        if (seed is int or long or double or float)
        {
            double d = Convert.ToDouble(seed);
            if (double.IsFinite(d)) return unchecked((uint)(int)(Math.Abs(d) * 2654435761.0));
        }
        string s = seed.ToString() ?? "";
        unchecked
        {
            int h = (int)0x811c9dc5;
            // charCodeAt is a UTF-16 code unit, which is exactly what indexing a C# string yields.
            foreach (char c in s)
            {
                h ^= c;
                h = Imul(h, 0x01000193);
            }
            return (uint)h;
        }
    }

    /// <summary>Standard-normal sample (Box–Muller) from a given RNG.</summary>
    public static double GaussFrom(Rng rng)
    {
        double u = 0, v = 0;
        while (u == 0) u = rng();
        while (v == 0) v = rng();
        return Math.Sqrt(-2 * Math.Log(u)) * Math.Cos(2 * Math.PI * v);
    }

    private static double Lerp(double a, double b, double t) => a + (b - a) * t;
    private static double Clamp(double v, double lo, double hi) => Math.Max(lo, Math.Min(hi, v));
    private static double Log2(double x) => Math.Log(x) / Math.Log(2);
    private static double Hypot(double x, double y) => Math.Sqrt(x * x + y * y);

    // ---- persona -------------------------------------------------------------------------------

    /// <summary>Derive a stable motor persona from a fingerprint seed (or random when unseeded).</summary>
    public static Persona MakePersona(object? seed = null)
    {
        uint seedInt = HashSeed(seed);
        Rng r = Mulberry32(seedInt);
        // Evaluation order matters: each r() consumes the shared stream, so these must be drawn in
        // the same sequence as the TS object literal or the persona diverges across SDKs.
        double deviceHz = Math.Round(Lerp(110, 155, r()));
        double tremorHz = Lerp(8, 12, r());
        double tremorAmp = Lerp(0.12, 0.5, r());
        double driftAmp = Lerp(0.1, 0.4, r());
        double driftTau = Lerp(60, 160, r());
        double jitter = Lerp(0.25, 0.7, r());
        double fittsA = Lerp(90, 150, r());
        double fittsB = Lerp(120, 190, r());
        double primaryFrac = Lerp(0.84, 0.94, r());
        double overshoot = Lerp(0.03, 0.08, r());
        int maxCorrections = r() < 0.15 ? 3 : (r() < 0.6 ? 2 : 1);
        double approachBias = (r() - 0.5) * 0.5;
        return new Persona
        {
            Seed = seedInt,
            DeviceHz = deviceHz,
            TremorHz = tremorHz,
            TremorAmp = tremorAmp,
            DriftAmp = driftAmp,
            DriftTau = driftTau,
            Jitter = jitter,
            FittsA = fittsA,
            FittsB = fittsB,
            PrimaryFrac = primaryFrac,
            Overshoot = overshoot,
            MaxCorrections = maxCorrections,
            ApproachBias = approachBias,
        };
    }

    /// <summary>A human mousedown→mouseup hold (ms), right-skewed. Fixes the ~2ms instant-press tell.</summary>
    public static double ClickHold(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return p.ClickHoldMinMs + Math.Min(1, Math.Abs(GaussFrom(rng)) * 0.5) * (p.ClickHoldMaxMs - p.ClickHoldMinMs);
    }

    /// <summary>A human per-key keydown→keyup dwell (ms), right-skewed. Fixes the ~2–3ms tell.</summary>
    public static double KeyDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return p.KeyDwellMinMs + Math.Min(1, Math.Abs(GaussFrom(rng)) * 0.45) * (p.KeyDwellMaxMs - p.KeyDwellMinMs);
    }

    /// <summary>Grab/release dwell times (ms) for a held-button drag.</summary>
    public static (double GrabMs, double ReleaseMs) DragDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return (Lerp(p.GrabMinMs, p.GrabMaxMs, rng()), Lerp(p.ReleaseMinMs, p.ReleaseMaxMs, rng()));
    }

    /// <summary>
    /// A human click point inside <paramref name="box"/>: a 2D gaussian around centre, nudged toward
    /// the approach side (people undershoot toward where the cursor came from), clamped inside the edges.
    /// This is what defeats a landing-dispersion check — a driver that clicks the computed centre lands
    /// on the identical sub-pixel point every time, which aiming never does.
    /// </summary>
    public static Point ClickPoint(Box box, Point from, Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double cx = box.X + box.Width / 2, cy = box.Y + box.Height / 2;
        double dx = cx - from.X, dy = cy - from.Y;
        double d = Hypot(dx, dy);
        if (d == 0) d = 1;
        double ux = -dx / d, uy = -dy / d;
        double bx = ux * box.Width * 0.12, by = uy * box.Height * 0.12;
        double x = cx + bx + GaussFrom(rng) * box.Width * 0.16;
        double y = cy + by + GaussFrom(rng) * box.Height * 0.16;
        // Below the 4px inset the clamp bounds invert and would return a point OUTSIDE the element.
        return new Point(
            box.Width >= 4 ? Clamp(x, box.X + 2, box.X + box.Width - 2) : cx,
            box.Height >= 4 ? Clamp(y, box.Y + 2, box.Y + box.Height - 2) : cy);
    }

    // ---- path planning -------------------------------------------------------------------------

    /// <summary>Minimum-jerk easing s(τ)=10τ³−15τ⁴+6τ⁵.</summary>
    public static double MinJerk(double tau)
    {
        double t = Clamp(tau, 0, 1);
        return t * t * t * (10 + t * (-15 + 6 * t));
    }

    private static double Bez(double p0, double c1, double c2, double p1, double e)
    {
        double m = 1 - e;
        return m * m * m * p0 + 3 * m * m * e * c1 + 3 * m * e * e * c2 + e * e * e * p1;
    }

    private sealed class Vec { public double X, Y; }

    private static double SampleSubmove(
        List<Step> outSteps, Point a, Point b, double durMs, Persona p, Rng rng,
        double t0, Vec drift, Vec jit, double tremorPhase, bool land)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y;
        double dist = Hypot(dx, dy);
        if (dist == 0) dist = 1e-6;
        double stepMs = 1000 / p.DeviceHz;
        int n = (int)Math.Min(180, Math.Max(4, Math.Round(durMs / stepMs)));
        double nx = -dy / dist, ny = dx / dist;
        double bowSign = p.ApproachBias >= 0 ? 1 : -1;
        double bow = bowSign * (0.04 + Math.Abs(GaussFrom(rng)) * 0.05) * Math.Min(dist, 260) * (0.5 + Math.Abs(p.ApproachBias));
        double c1x = a.X + dx * 0.33 + nx * bow, c1y = a.Y + dy * 0.33 + ny * bow;
        double c2x = a.X + dx * 0.66 + nx * bow, c2y = a.Y + dy * 0.66 + ny * bow;
        double t = t0;
        for (int i = 1; i <= n; i++)
        {
            double e = MinJerk((double)i / n);
            double px = Bez(a.X, c1x, c2x, b.X, e);
            double py = Bez(a.Y, c1y, c2y, b.Y, e);
            bool last = i == n;
            if (!(last && land))
            {
                double k = stepMs / p.DriftTau;
                drift.X += -k * drift.X + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                drift.Y += -k * drift.Y + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                double tAmp = p.TremorAmp * (0.7 + 0.3 * Math.Sin(2 * Math.PI * t / 850 + tremorPhase));
                double trX = tAmp * Math.Sin(tremorPhase + 2 * Math.PI * p.TremorHz * t / 1000);
                double trY = tAmp * Math.Sin(tremorPhase + 1.0 + 2 * Math.PI * (p.TremorHz * 0.93) * t / 1000);
                // AR(1) coloured jitter, not white noise: white noise adds a fresh speed reversal every
                // sample and manufactures spurious submovements.
                jit.X = 0.7 * jit.X + GaussFrom(rng) * p.Jitter * 0.22;
                jit.Y = 0.7 * jit.Y + GaussFrom(rng) * p.Jitter * 0.22;
                px += drift.X + trX + jit.X;
                py += drift.Y + trY + jit.Y;
            }
            else { px = b.X; py = b.Y; }
            double sleepMs = stepMs * Lerp(0.88, 1.12, rng());
            outSteps.Add(new Step(px, py, sleepMs));
            t += sleepMs;
        }
        return t;
    }

    /// <summary>
    /// Plan a cursor move from→to as minimum-jerk submovements (primary + corrections) with a
    /// Fitts-scaled duration and coloured noise. Returns the samples to dispatch.
    /// </summary>
    public static List<Step> PlanMove(Point from, Point to, Persona p,
        double targetW = 24, bool settle = false, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double W = Math.Max(6, targetW);
        double D = Hypot(to.X - from.X, to.Y - from.Y);
        var outSteps = new List<Step>();
        if (D < 1.5) { outSteps.Add(new Step(to.X, to.Y, 1000 / p.DeviceHz)); return outSteps; }

        double id = Log2(D / W + 1);
        double mtLo = Math.Min(1700, Math.Max(70, 40 + D * 0.6));
        double mt = Clamp((p.FittsA + p.FittsB * id) * (0.85 + 0.3 * rng()), mtLo, 1700);

        var targets = new List<Point>();
        if (D >= 40)
        {
            double f = p.PrimaryFrac;
            double spread = Math.Min(16, D * p.Overshoot);
            var primary = new Point(
                from.X + (to.X - from.X) * f + GaussFrom(rng) * spread,
                from.Y + (to.Y - from.Y) * f + GaussFrom(rng) * spread);
            targets.Add(primary);
            int nCorr = rng() < 0.08 * p.MaxCorrections ? 2 : 1;
            Point cur = primary;
            for (int i = 0; i < nCorr; i++)
            {
                double close = Lerp(0.55, 0.8, rng());
                var nx2 = new Point(
                    cur.X + (to.X - cur.X) * close + GaussFrom(rng) * 1.2,
                    cur.Y + (to.Y - cur.Y) * close + GaussFrom(rng) * 1.2);
                targets.Add(nx2);
                cur = nx2;
            }
        }
        targets.Add(to);

        var segDur = new List<double>();
        if (targets.Count == 1) segDur.Add(mt);
        else
        {
            segDur.Add(mt * 0.78);
            double rest = mt * 0.22;
            int nc = targets.Count - 1;
            for (int i = 0; i < nc; i++) segDur.Add(rest / nc * Lerp(0.8, 1.2, rng()));
        }

        var drift = new Vec();
        var jit = new Vec();
        double tremorPhase = rng() * 2 * Math.PI;
        Point aPt = from;
        double t = 0;
        for (int s = 0; s < targets.Count; s++)
        {
            Point b = targets[s];
            bool last = s == targets.Count - 1;
            t = SampleSubmove(outSteps, aPt, b, Math.Max(20, segDur[s]), p, rng, t, drift, jit, tremorPhase, last && !settle);
            if (!last && outSteps.Count > 0)
            {
                var lastStep = outSteps[^1];
                outSteps[^1] = lastStep with { SleepMs = lastStep.SleepMs + Lerp(40, 120, rng()) };
            }
            aPt = b;
        }

        if (settle)
        {
            int jig = 1 + (int)(rng() * 2);
            for (int i = 0; i < jig; i++)
                outSteps.Add(new Step(to.X + GaussFrom(rng) * 1.4, to.Y + GaussFrom(rng) * 1.4, Lerp(30, 90, rng())));
            outSteps.Add(new Step(to.X, to.Y, 1000 / p.DeviceHz));
        }
        return outSteps;
    }
}
