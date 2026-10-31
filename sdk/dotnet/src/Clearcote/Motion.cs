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
// PlanAmbient is ported (SC 232 lane C) with its caller, Humanize.HumanAmbientMotionAsync, and pinned to the
// other two SDKs by sdk/testdata/humanize_after_vectors.json.

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
    /// <summary>The HUMAN BANDS every timing draw is clamped to (ms); the per-identity medians inside them come
    /// from the salted timing stream (<see cref="Motion.TimingTraitsOf"/>). Grab hesitation after mouse down:</summary>
    public double GrabMinMs { get; init; } = 130;
    public double GrabMaxMs { get; init; } = 360;
    public double ReleaseMinMs { get; init; } = 90;
    public double ReleaseMaxMs { get; init; } = 230;
    /// <summary>Click press-hold band (mousedown→mouseup), 55–230 ms; a scripted click is ~2ms.</summary>
    public double ClickHoldMinMs { get; init; } = 55;
    public double ClickHoldMaxMs { get; init; } = 230;
    /// <summary>Per-key dwell band (keydown→keyup), 45–120 ms; scripted typing ~2–3ms.</summary>
    public double KeyDwellMinMs { get; init; } = 45;
    public double KeyDwellMaxMs { get; init; } = 120;
}

public readonly record struct Point(double X, double Y);
/// <summary>One dispatched sample: move to (X,Y), then sleep SleepMs before the next.</summary>
public readonly record struct Step(double X, double Y, double SleepMs);
public readonly record struct Box(double X, double Y, double Width, double Height);
/// <summary>Per-identity path-shape traits (see Motion.ShapeTraitsOf).</summary>
public readonly record struct ShapeTraits(double OvershootRate, double Curve, double DragTempo);
/// <summary>Per-identity timing medians (ms) and tempo (see Motion.TimingTraitsOf).</summary>
public readonly record struct TimingTraits(double ClickHoldMs, double KeyDwellMs, double GrabMs, double ReleaseMs,
    double DblclickGapMs, double PauseScale, double TypoRate);
/// <summary>Per-identity aiming traits (see Motion.AimTraitsOf).</summary>
public readonly record struct AimTraits(double Spread, double Undershoot, double SideBias, double LabelShare);

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
            // JS `>>> 0` is ToUint32: the product modulo 2^32 (fmod is exact). A cast to int of a
            // product this size is out of range, and x64 answers 0x80000000 for every nonzero seed.
            if (double.IsFinite(d)) return (uint)(Math.Abs(d) * 2654435761.0 % 4294967296.0);
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

    // Salt for the timing stream. Like the rollover and shape salts, a separate Mulberry32(seed ^ salt) stream keeps
    // MakePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint TimingSalt = 0xC2B2AE35;

    /// <summary>
    /// The persona's timing traits, drawn from a salted stream of its seed: the median click hold (70–140 ms), key
    /// dwell (60–95), drag grab (180–280) and release (120–180), double-click gap (90–140), a tempo on deliberation
    /// pauses (0.8–1.25) and the typo rate (0.5–4 %). Before this every identity shared one fixed window per timing.
    /// Same values as Python <c>timing_traits</c> and Node <c>timingTraits</c>.
    /// </summary>
    public static TimingTraits TimingTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ TimingSalt);
        double clickHold = Lerp(70, 140, r());
        double keyDwell = Lerp(60, 95, r());
        double grab = Lerp(180, 280, r());
        double release = Lerp(120, 180, r());
        double dbl = Lerp(90, 140, r());
        double pause = Lerp(0.8, 1.25, r());
        double typo = Lerp(0.005, 0.04, r());
        return new TimingTraits(clickHold, keyDwell, grab, release, dbl, pause, typo);
    }

    /// <summary><c>median * exp(sigma * N(0,1))</c> clamped to [lo, hi]: a right-skewed human timing.</summary>
    public static double LognormalMs(double median, double sigma, double lo, double hi, Rng rng)
        => Clamp(median * Math.Exp(sigma * GaussFrom(rng)), lo, hi);

    /// <summary>A human mousedown→mouseup hold (ms): log-normal (σ 0.25) around the persona's median, clamped to its band.</summary>
    public static double ClickHold(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        // the preset scales the median (Humanize.TempoOf); ApplyOptions already scaled the band
        return LognormalMs(TimingTraitsOf(p).ClickHoldMs * Humanize.TempoOf(p), 0.25, p.ClickHoldMinMs, p.ClickHoldMaxMs, rng);
    }

    /// <summary>A human per-key keydown→keyup dwell (ms): log-normal (σ 0.22) around the persona's median, clamped to its band.</summary>
    public static double KeyDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return LognormalMs(TimingTraitsOf(p).KeyDwellMs * Humanize.TempoOf(p), 0.22, p.KeyDwellMinMs, p.KeyDwellMaxMs, rng);
    }

    /// <summary>
    /// Where a reading hand parks the pointer before a wheel: x ~ 0.5w ± 0.10w clamped [0.12w, 0.88w], y ~ 0.34h
    /// ± 0.09h clamped [0.10h, 0.62h] (two gaussians, x first). One model in all three SDKs (Python
    /// <c>wheel_anchor_point</c>, motion.ts <c>wheelAnchorPoint</c>); before 0.52 .NET used 0.5/0.38, sd 0.12/0.10.
    /// </summary>
    public static (double X, double Y) WheelAnchorPoint(double vw, double vh, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double x = Clamp(vw * 0.5 + GaussFrom(rng) * vw * 0.10, vw * 0.12, vw * 0.88);
        double y = Clamp(vh * 0.34 + GaussFrom(rng) * vh * 0.09, vh * 0.10, vh * 0.62);
        return (x, y);
    }

    // Salt for the drag-start hesitation traits (Python _HESITATION_SALT, motion.ts HESITATION_SALT): a
    // salted stream keeps MakePersona's draw ORDER, and PlanDrag itself is untouched.
    private const uint HesitationSalt = 0xFD7046C5; // own stream (was the timing salt)
    /// <summary>The drag-start hesitation window (ms): a hand that has just closed on a handle holds 100-300 ms.</summary>
    public const double HesitationMinMs = 100, HesitationMaxMs = 300;
    /// <summary>... creeping a few px, never this far, before the leg proper.</summary>
    public const double HesitationMaxPx = 8;

    /// <summary>
    /// The still-but-not-frozen moment between the grab and the drag leg from->to: samples spanning
    /// 100-300 ms (centred per persona; <paramref name="durMs"/>, 40-300, overrides the length while the
    /// persona's draw is still made), creeping under 8 px toward <paramref name="to"/> with a little sideways
    /// wobble. Same numbers as Python <c>_motion.drag_hesitation</c> and motion.ts <c>dragHesitation</c>
    /// (sdk/testdata/humanize_scroll_vectors.json). Measured on r38 / SDK 0.51.0 (SC 232): every drag's first
    /// held move was already the full-speed leg.
    /// </summary>
    public static List<Step> DragHesitation(Point from, Point to, Persona p, Rng? rng = null, double? durMs = null)
    {
        rng ??= Random.Shared.NextDouble;
        Rng r = Mulberry32(p.Seed ^ HesitationSalt);
        double centre = Lerp(130, 260, r());
        double amp = Lerp(2, 6, r());
        double wobble = Lerp(0.4, 1.2, r());
        double d = Hypot(to.X - from.X, to.Y - from.Y);
        double ux = d >= 1e-6 ? (to.X - from.X) / d : 1, uy = d >= 1e-6 ? (to.Y - from.Y) / d : 0;
        double dur = Clamp(centre * Lerp(0.8, 1.2, rng()), HesitationMinMs, HesitationMaxMs);
        if (durMs is double dm) dur = Clamp(dm, 40, HesitationMaxMs);
        double reach = amp * Lerp(0.6, 1.0, rng());
        double stepMs = 1000 / p.DeviceHz;
        int n = (int)Math.Max(3, JsRound(dur / stepMs));
        double dt = dur / n;
        double phase = rng() * 2 * Math.PI;
        var outSteps = new List<Step>();
        double side = 0;
        for (int i = 1; i <= n; i++)
        {
            double tau = (double)i / n;
            double along = reach * MinJerk(tau);
            side = 0.8 * side + GaussFrom(rng) * wobble * 0.25;
            double s = Clamp(side + wobble * 0.5 * Math.Sin(phase + 2 * Math.PI * tau), -1.5, 1.5);
            double px = from.X + ux * along - uy * s, py = from.Y + uy * along + ux * s;
            if (Hypot(px - from.X, py - from.Y) >= HesitationMaxPx - 0.5)
            {
                px = from.X + ux * (HesitationMaxPx - 1);
                py = from.Y + uy * (HesitationMaxPx - 1);
            }
            outSteps.Add(new Step(px, py, dt * Lerp(0.9, 1.1, rng())));
        }
        return outSteps;
    }

    /// <summary>When (ms from the start of <paramref name="steps"/>) the walk first leaves the
    /// <paramref name="radius"/> circle around <paramref name="from"/>; the whole length when it never does.</summary>
    public static double LegExitMs(Point from, IReadOnlyList<Step> steps, double radius = HesitationMaxPx)
    {
        double t = 0;
        foreach (var s in steps)
        {
            if (Hypot(s.X - from.X, s.Y - from.Y) >= radius) return t;
            t += s.SleepMs;
        }
        return t;
    }

    /// <summary>
    /// The held part of a drag: the hesitation creep, then the <see cref="PlanDrag"/> leg, planned together so
    /// the pointer stays inside 8 px of the press for <paramref name="windowMs"/> (clamped 100-300: the
    /// persona's grab dwell) and then goes. Same as Python <c>hesitate_then_drag</c>.
    /// </summary>
    public static (List<Step> Steps, int Creep) HesitateThenDrag(Point from, Point to, Persona p, double windowMs,
        bool settle = true, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double w = Clamp(windowMs, HesitationMinMs, HesitationMaxMs);
        var creep = DragHesitation(from, to, p, rng, w);
        var leg = PlanDrag(new Point(creep[^1].X, creep[^1].Y), to, p, settle, rng);
        double t8 = LegExitMs(from, leg);
        double want = Math.Max(40, w - t8);
        double have = 0;
        foreach (var s in creep) have += s.SleepMs;
        if (Math.Abs(have - want) > 1)
        {
            double k = want / have, m = Math.Min(1, k);
            for (int i = 0; i < creep.Count; i++)
            {
                var s = creep[i];
                creep[i] = new Step(from.X + (s.X - from.X) * m, from.Y + (s.Y - from.Y) * m, s.SleepMs * k);
            }
            leg = PlanDrag(new Point(creep[^1].X, creep[^1].Y), to, p, settle, rng);
        }
        var all = new List<Step>(creep);
        all.AddRange(leg);
        return (all, creep.Count);
    }

    /// <summary>Grab/release dwell times (ms) for a held-button drag: log-normal (σ 0.2) around the persona's medians.</summary>
    public static (double GrabMs, double ReleaseMs) DragDwell(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        var tt = TimingTraitsOf(p);
        double t = Humanize.TempoOf(p);
        double grab = LognormalMs(tt.GrabMs * t, 0.2, p.GrabMinMs, p.GrabMaxMs, rng);
        double release = LognormalMs(tt.ReleaseMs * t, 0.2, p.ReleaseMinMs, p.ReleaseMaxMs, rng);
        return (grab, release);
    }

    /// <summary>The gap between the two presses of a double click (ms): log-normal (σ 0.15) around the persona's
    /// median, clamped to 70–170.</summary>
    public static double DblclickGap(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double t = Humanize.TempoOf(p);
        return LognormalMs(TimingTraitsOf(p).DblclickGapMs * t, 0.15, 70 * t, 170 * t, rng);
    }

    /// <summary>A deliberation pause (ms) from [lo, hi], scaled by the persona's tempo; without a persona the plain window.</summary>
    public static double PauseMs(Persona? p, double lo, double hi, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double v = Lerp(lo, hi, rng());
        return p is null ? v : v * TimingTraitsOf(p).PauseScale * Humanize.TempoOf(p);
    }

    // Salt for the rollover stream. Drawing from Mulberry32(seed ^ salt) instead of adding a field to
    // MakePersona keeps the persona draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint RolloverSalt = 0x9E3779B9;

    /// <summary>
    /// Share of eligible key pairs this persona types with ROLLOVER: the next key goes down before the
    /// previous one comes up, the way people type fast common pairs. Stable per seed, 0.12–0.38.
    /// Measured on r32/r33 before this existed: 0 overlapping keydowns in 30–32 typed keys, because
    /// every character was one press (down, dwell, up) before the next began. Same value as the other
    /// SDKs' rolloverRate / rollover_rate.
    /// </summary>
    public static double RolloverRate(Persona p) => Lerp(0.12, 0.38, Mulberry32(p.Seed ^ RolloverSalt)());

    /// <summary>
    /// Pixels one wheel notch scrolls, by the platform the persona claims. A wheel event sent over CDP
    /// always carries wheelDelta ±120 (input_handler.cc sets wheel_ticks to ±1 whatever the delta), and
    /// a real notch is the only event whose distance goes with a 120 wheelDelta:
    ///   windows 100 = 3 lines × 100/3 px (web_input_event_builders_win.cc, default 3 lines per notch)
    ///   linux   120 = ui::MouseWheelEvent::kWheelDelta (event.cc); measured 120/-120 per X11 notch
    ///   macos    40 = kScrollbarPixelsPerCocoaTick (cocoa_event_utils.h)
    /// android is not measured; it gets the windows value.
    /// </summary>
    public static readonly IReadOnlyDictionary<string, int> WheelNotchPx = new Dictionary<string, int>
    {
        ["windows"] = 100, ["linux"] = 120, ["macos"] = 40, ["android"] = 100,
    };

    /// <summary>
    /// Whole notches for a requested scroll distance: the nearest count, at least one when the caller
    /// asked for any scroll at all. A wheel cannot move 39px; it moves notches. Rounds half up like JS
    /// Math.round — Math.Round's default is banker's rounding, which would turn 250/100 into 2, not 3.
    /// </summary>
    public static int WheelNotches(double delta, double notchPx)
    {
        if (delta == 0 || double.IsNaN(delta)) return 0;   // NaN: JS plans no events for it either
        return Math.Max(1, (int)Math.Floor(Math.Abs(delta) / notchPx + 0.5));
    }

    /// <summary>Map navigator.userAgentData.platform / navigator.platform to a <see cref="WheelNotchPx"/> key.</summary>
    public static string? PlatformFromNavigator(string? value)
    {
        string v = (value ?? "").ToLowerInvariant();
        if (v.Length == 0) return null;
        if (v.Contains("android")) return "android";   // before linux: Android's navigator.platform is "Linux armv8l"
        if (v.Contains("win")) return "windows";
        if (v.Contains("mac")) return "macos";
        if (v.Contains("linux") || v.Contains("x11") || v.Contains("cros")) return "linux";
        return null;
    }

    // Salt for the pacer's hop cap (SC 299): own stream.
    private const uint PaceSalt = 0x5BD1E995;
    /// <summary>The longest hop this hand makes between two dispatched pointer points, 55..110 px (the first draw of
    /// its salted pace stream): what a quick reach covers in about one 60 Hz frame at a 3.5-7 px/ms peak. The
    /// wall-clock pacer (<see cref="Pacing.WalkAsync"/>) never hops further; a late walk stretches in time instead.
    /// Measured on r54 / SDK 0.67.1 (SC 298): a fixed 40 px cap put a wall at 40 px under every persona (0 of 4,462
    /// page-visible steps above 40.5 px), the same for every identity. Same value as Python <c>hop_cap_px</c> and
    /// Node <c>hopCapPx</c>.</summary>
    public static double HopCapPx(Persona p) => Lerp(55.0, 110.0, Mulberry32(p.Seed ^ PaceSalt)());

    // Salt for the aim traits (ClickPoint): the persona's landing dispersion, undershoot and handedness.
    private const uint AimSalt = 0x27D4EB2F;
    /// <summary>The persona's aiming traits from a salted stream of its seed: dispersion (sd 0.10–0.20 of the box),
    /// undershoot toward the approach side (0.06–0.16) and handedness (ApproachBias × 0.4). Same values as Python
    /// <c>aim_traits</c> and Node <c>aimTraits</c>.</summary>
    public static AimTraits AimTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ AimSalt);
        double spread = Lerp(0.10, 0.20, r());
        double under = Lerp(0.06, 0.16, r());
        double label = Lerp(0.25, 0.55, r());   // SC 232 integration: lane F's label aim, a trait (was one 0.4 for all)
        return new AimTraits(spread, under, p.ApproachBias * 0.4, label);
    }

    /// <summary>
    /// A human click point inside <paramref name="box"/>: a 2D gaussian toward the centre with the persona's
    /// dispersion, nudged toward the approach side (undershoot) and sideways by its handedness, clamped inside the
    /// edges. The SEED fixes the persona's spread, undershoot and handedness (it was one dispersion for everyone); each
    /// click's draw is unseeded (Random.Shared unless <paramref name="rng"/> is given), like every per-action draw, so
    /// no two visits of one identity replay the same landing sequence. Without a persona: sd 0.15, 12 % undershoot,
    /// no handedness.
    /// </summary>
    public static Point ClickPoint(Box box, Point from, Persona? p, Rng? rng = null)
    {
        double spread = 0.15, under = 0.12, side = 0;
        if (p is not null)
        {
            var at = AimTraitsOf(p);
            spread = at.Spread; under = at.Undershoot; side = at.SideBias;
        }
        rng ??= Random.Shared.NextDouble;
        double cx = box.X + box.Width / 2, cy = box.Y + box.Height / 2;
        double dx = cx - from.X, dy = cy - from.Y;
        double d = Hypot(dx, dy);
        if (d == 0) d = 1;
        double ux = -dx / d, uy = -dy / d;
        double x = cx + ux * box.Width * under + side * box.Width + GaussFrom(rng) * box.Width * spread;
        double y = cy + uy * box.Height * under + GaussFrom(rng) * box.Height * spread;
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

    // Salt for the path-shape stream. Like the rollover salt, a separate Mulberry32(seed ^ salt) stream
    // keeps MakePersona's draw ORDER (and so every existing seed's motor identity) unchanged.
    private const uint ShapeSalt = 0x85ebca6b;

    /// <summary>
    /// The persona's path-shape traits, drawn from a salted stream of its seed: the share of aimed moves
    /// that pass the target and come back (0.15–0.38), a scale on the primary submovement's bow
    /// (0.75–1.3) and a scale on held-drag duration (0.85–1.2). Measured on r38 / SDK 0.48.0 (SC 209):
    /// 1 of 36 planned approaches passed the target, against 15–40 % for real reaches. Same values as
    /// Python <c>shape_traits</c> and Node <c>shapeTraits</c>.
    /// </summary>
    public static ShapeTraits ShapeTraitsOf(Persona p)
    {
        Rng r = Mulberry32(p.Seed ^ ShapeSalt);
        double overshootRate = Lerp(0.15, 0.38, r());
        double curve = Lerp(0.75, 1.3, r());
        double dragTempo = Lerp(0.85, 1.2, r());
        return new ShapeTraits(overshootRate, curve, dragTempo);
    }

    // Sub-pixel noise scale. The coloured noise (OU drift + 8–12 Hz tremor + AR(1) jitter) stays, but
    // smaller and carried mostly while the hand is moving: a mouse resting on the pad reports no counts,
    // so a slow tail full of ±0.5 px wobble is what a quantizing page turns into 1 px zigzags.
    private const double Noise = 0.6;

    /// <summary>floor(x + 0.5): JavaScript Math.round / Python _js_round, not banker's rounding.</summary>
    private static double JsRound(double x) => Math.Floor(x + 0.5);

    /// <summary>Time warp 1−(1−τ)^skew: skew &gt; 1 moves the min-jerk speed peak before the midpoint.</summary>
    private static double Skewed(double tau, double skew)
    {
        double t = Clamp(tau, 0, 1);
        return skew != 1.0 ? 1 - Math.Pow(1 - t, skew) : t;
    }

    private static double SampleSubmove(
        List<Step> outSteps, Point a, Point b, double durMs, Persona p, Rng rng,
        double t0, Vec drift, Vec jit, double tremorPhase, bool land, double bowFrac, double skew)
    {
        double dx = b.X - a.X, dy = b.Y - a.Y;
        double dist = Hypot(dx, dy);
        if (dist == 0) dist = 1e-6;
        double stepMs = 1000 / p.DeviceHz;
        int n = (int)Math.Min(180, Math.Max(4, JsRound(durMs / stepMs)));
        double nx = -dy / dist, ny = dx / dist;
        // A cubic with both control points offset by c peaks at 0.75 c; the first control point carries
        // more of it, so the widest point of the arc comes before the middle, as in a reach.
        double c = bowFrac * dist / 0.75;
        double c1x = a.X + dx * 0.3 + nx * c * 1.15, c1y = a.Y + dy * 0.3 + ny * c * 1.15;
        double c2x = a.X + dx * 0.7 + nx * c * 0.85, c2y = a.Y + dy * 0.7 + ny * c * 0.85;
        double t = t0;
        // Each sample's POSITION is taken at its own report time (SC 299): the report intervals jitter
        // (stepMs x 0.88-1.12, drawn at the end of each iteration as before), and a position sampled at the
        // nominal i/n while its time jittered made the speed a page computes swing +-12 % sample to sample --
        // 5 smoothed velocity peaks per planned glide (SC 298). `el` is the time of sample i since the
        // submovement started; the hand has arrived once el reaches the nominal duration. Same as Python
        // _sample_submove and Node sampleSubmove.
        double total = n * stepMs;
        double el = stepMs;
        for (int i = 1; i <= n; i++)
        {
            double tau = i == n ? 1.0 : Math.Min(1.0, el / total);
            double e = MinJerk(Skewed(tau, skew));
            double px = Bez(a.X, c1x, c2x, b.X, e);
            double py = Bez(a.Y, c1y, c2y, b.Y, e);
            bool last = i == n;
            if (!(last && land))
            {
                // noise follows the speed envelope (min-jerk speed 30τ²(1−τ)² peaks at 1.875)
                double v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
                double g = Noise * (0.35 + 0.65 * v);
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
                px += (drift.X + trX + jit.X) * g;
                py += (drift.Y + trY + jit.Y) * g;
            }
            else { px = b.X; py = b.Y; }
            double sleepMs = stepMs * Lerp(0.88, 1.12, rng());
            outSteps.Add(new Step(px, py, sleepMs));
            t += sleepMs;
            el += sleepMs;
        }
        return t;
    }

    /// <summary>
    /// Plan a cursor move from→to as minimum-jerk submovements (primary + corrections) with a
    /// Fitts-scaled duration and coloured noise. Returns the samples to dispatch. A move with a button
    /// held (<paramref name="held"/>) or one that seats a handle (<paramref name="settle"/>) is a drag,
    /// planned by <see cref="PlanDrag"/>.
    /// <para>Shape (SC 209/210): the primary submovement arcs (peak sideways deviation ~6–18 % of the
    /// distance, per-persona curvature and handedness), reaches its speed peak before the middle of the
    /// move, and either stops short of the target (a short correction closes the gap) or, in
    /// <see cref="ShapeTraits.OvershootRate"/> of moves, passes it by 3–28 px and comes back.
    /// Straightness of the planned points lands around 0.85–0.95, the band of real reaches.</para>
    /// </summary>
    public static List<Step> PlanMove(Point from, Point to, Persona p,
        double targetW = 24, bool settle = false, Rng? rng = null, bool held = false)
    {
        if (settle || held) return PlanDrag(from, to, p, settle, rng);
        rng ??= Random.Shared.NextDouble;
        double W = Math.Max(6, targetW);
        double D = Hypot(to.X - from.X, to.Y - from.Y);
        var outSteps = new List<Step>();
        if (D < 1.5) { outSteps.Add(new Step(to.X, to.Y, 1000 / p.DeviceHz)); return outSteps; }

        double id = Log2(D / W + 1);
        // A distance floor (an average-speed bound) capped at the 1700 ms ceiling so a very long move can't invert
        // the clamp (lo > hi). 0.4 ms/px (was 0.6, SC 299): at 0.6 the floor overrode Fitts' law whenever a far
        // target was also wide, so planned time tracked DISTANCE instead of difficulty (planned MT/ID correlation
        // -0.14 median over 8 personas on SC 298's six targets; 0.58 at 0.4). Same as Python plan_move.
        double mtLo = Math.Min(1700, Math.Max(70, 40 + D * 0.4));
        double mt = Clamp((p.FittsA + p.FittsB * id) * (0.85 + 0.3 * rng()), mtLo, 1700);
        var tr = ShapeTraitsOf(p);
        double ux = (to.X - from.X) / D, uy = (to.Y - from.Y) / D;

        var targets = new List<Point>();
        bool over = false;
        if (D >= 40)
        {
            over = rng() < tr.OvershootRate;
            double along = over
                ? D + Clamp(D * Lerp(0.03, 0.08, rng()), 3, 28)
                : D * Clamp(p.PrimaryFrac + 0.05 + GaussFrom(rng) * 0.015, 0.88, 0.985);
            double lat = GaussFrom(rng) * Math.Min(6, D * 0.02);
            targets.Add(new Point(from.X + ux * along - uy * lat, from.Y + uy * along + ux * lat));
            if (rng() < 0.08 * p.MaxCorrections)
            {
                Point cur = targets[0];
                double close = Lerp(0.55, 0.8, rng());
                double mx = cur.X + (to.X - cur.X) * close + GaussFrom(rng) * 1.0;
                double my = cur.Y + (to.Y - cur.Y) * close + GaussFrom(rng) * 1.0;
                targets.Add(new Point(mx, my));
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

        // The primary's arc: handedness sign from the persona (1 move in 5 bends the other way), size
        // from the persona's curvature trait. Corrections are short and nearly straight.
        double sign = p.ApproachBias >= 0 ? 1 : -1;
        if (rng() < 0.2) sign = -sign;
        double bow = sign * Math.Min(0.25, (0.08 + Math.Abs(GaussFrom(rng)) * 0.11) * tr.Curve * (0.75 + Math.Abs(p.ApproachBias)));
        if (D * Math.Abs(bow) > 110) bow = bow < 0 ? -110 / D : 110 / D;

        var drift = new Vec();
        var jit = new Vec();
        double tremorPhase = rng() * 2 * Math.PI;
        Point aPt = from;
        double t = 0;
        for (int s = 0; s < targets.Count; s++)
        {
            Point b = targets[s];
            bool last = s == targets.Count - 1;
            bool first = s == 0;
            t = SampleSubmove(outSteps, aPt, b, Math.Max(20, segDur[s]), p, rng, t, drift, jit, tremorPhase, last,
                first ? bow : bow * 0.15, first ? 1.25 : 1.0);
            if (!last && outSteps.Count > 0)
            {
                // motor re-planning gap; a reversal after passing the target comes back quicker
                var lastStep = outSteps[^1];
                double gap = over && first ? Lerp(25, 90, rng()) : Lerp(40, 120, rng());
                outSteps[^1] = lastStep with { SleepMs = lastStep.SleepMs + gap };
            }
            aPt = b;
        }
        return outSteps;
    }

    /// <summary>
    /// Plan a held-button drag from→to (slider handle, drag-and-drop, range thumb). Measured on r38 /
    /// SDK 0.48.0 (SC 209): the held leg reused the aimed-move planner, whose arc put the hand up to
    /// 47 px off the line between grab and drop — off a 40 px slider track. A drag is a slower, flatter
    /// movement: one skewed min-jerk leg along the line with at most ~5 px of arc and ±6 px of total
    /// sideways error, 0.5–2 s for 300–400 px, and now and then (about 0.6× the persona's overshoot rate)
    /// a 2.5–10 px overshoot and pull-back. <paramref name="settle"/> adds a couple of sub-pixel seating
    /// jiggles before landing exactly on <paramref name="to"/>.
    /// </summary>
    public static List<Step> PlanDrag(Point from, Point to, Persona p, bool settle = false, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double D = Hypot(to.X - from.X, to.Y - from.Y);
        var outSteps = new List<Step>();
        double stepMs = 1000 / p.DeviceHz;
        if (D < 1.5) { outSteps.Add(new Step(to.X, to.Y, stepMs)); return outSteps; }
        var tr = ShapeTraitsOf(p);
        double ux = (to.X - from.X) / D, uy = (to.Y - from.Y) / D;
        double mt = Clamp((260 + 1.7 * D) * tr.DragTempo * Lerp(0.9, 1.1, rng()), Math.Min(500, 150 + 1.5 * D), 2000);
        bool over = D >= 60 && rng() < tr.OvershootRate * 0.6;
        double o = over ? Clamp(D * Lerp(0.01, 0.03, rng()), 2.5, 10) : 0;
        double sign = p.ApproachBias >= 0 ? 1 : -1;
        double h = sign * Math.Min(5, D * 0.015) * Lerp(0.3, 1.0, rng());
        double tremorPhase = rng() * 2 * Math.PI;

        var legs = new List<(double S0, double S1, double Dur, double H)> { (0, D + o, mt * (over ? 0.86 : 1.0), h) };
        if (over) legs.Add((D + o, D, mt * 0.14, 0));
        double drift = 0, jit = 0, t = 0;
        for (int li = 0; li < legs.Count; li++)
        {
            var (s0, s1, dur, hh) = legs[li];
            int n = (int)Math.Min(360, Math.Max(4, JsRound(dur / stepMs)));
            double dt = dur / n;
            bool lastLeg = li == legs.Count - 1;
            for (int i = 1; i <= n; i++)
            {
                double tau = (double)i / n;
                double e = MinJerk(Skewed(tau, 1.2));
                double along = s0 + (s1 - s0) * e;
                double v = 30 * tau * tau * (1 - tau) * (1 - tau) / 1.875;
                double k = dt / p.DriftTau;
                drift += -k * drift + Math.Sqrt(2 * k) * p.DriftAmp * 0.8 * GaussFrom(rng);
                jit = 0.7 * jit + GaussFrom(rng) * p.Jitter * 0.12;
                double tAmp = p.TremorAmp * (0.7 + 0.3 * Math.Sin(2 * Math.PI * t / 850 + tremorPhase));
                double tremor = tAmp * Math.Sin(tremorPhase + 2 * Math.PI * p.TremorHz * t / 1000);
                along += tAmp * 0.6 * Math.Sin(tremorPhase + 1.0 + 2 * Math.PI * (p.TremorHz * 0.93) * t / 1000) * v;
                double side = hh * Math.Sin(Math.PI * e) + (drift + tremor + jit) * (0.6 + 0.4 * v);
                side = Clamp(side, -6, 6);
                double px, py;
                if (lastLeg && i == n && !settle) { px = to.X; py = to.Y; }
                else { px = from.X + ux * along - uy * side; py = from.Y + uy * along + ux * side; }
                double sl = dt * Lerp(0.88, 1.12, rng());
                outSteps.Add(new Step(px, py, sl));
                t += sl;
            }
            if (!lastLeg && outSteps.Count > 0)
            {
                var ls = outSteps[^1];
                outSteps[^1] = ls with { SleepMs = ls.SleepMs + Lerp(30, 80, rng()) };   // notices, stops, pulls back
            }
        }

        if (settle)
        {
            // A couple of tiny sub-pixel seating jiggles, then land exactly (handle "clunk" into place).
            int jig = 1 + (int)Math.Floor(rng() * 2);
            for (int i = 0; i < jig; i++)
            {
                double jx = to.X + Clamp(GaussFrom(rng) * 0.8, -1.5, 1.5);
                double jy = to.Y + Clamp(GaussFrom(rng) * 0.8, -1.5, 1.5);
                outSteps.Add(new Step(jx, jy, Lerp(30, 90, rng())));
            }
            outSteps.Add(new Step(to.X, to.Y, stepMs));
        }
        return outSteps;
    }

    // ---- ambient (port of plan_ambient / planAmbient, SC 232 lane C) -----------------------------

    /// <summary>
    /// Plan ambient / pre-challenge cursor activity: idle drift + a few non-goal moves inside a central band
    /// of the viewport (12–88 % across, 14–80 % down), so a behavioral collector sees pointer entropy before
    /// the first goal action. About <paramref name="ms"/> long (it runs past it by whole excursions and rests;
    /// <see cref="Pacing.TrimTo"/> cuts it). Bit-identical to Python <c>plan_ambient</c> and Node
    /// <c>planAmbient</c> for the same persona and RNG (sdk/testdata/humanize_after_vectors.json).
    /// </summary>
    public static List<Step> PlanAmbient(Point from, double vw, double vh, Persona p, double ms, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        var outSteps = new List<Step>();
        var cur = from;
        double elapsed = 0;
        double budget = Math.Max(200, ms);
        double bx0 = vw * 0.12, bx1 = vw * 0.88;
        double by0 = vh * 0.14, by1 = vh * 0.8;
        while (elapsed < budget && outSteps.Count < 4000)
        {
            double tx = Lerp(bx0, bx1, rng());
            double ty = Lerp(by0, by1, rng());
            var target = new Point(tx, ty);
            foreach (var s in PlanMove(cur, target, p, 80, rng: rng))
            {
                outSteps.Add(s);
                elapsed += s.SleepMs;
            }
            double restMs = Lerp(120, 600, rng());
            double d0 = 0, d1 = 0;
            double stepMs = 1000 / p.DeviceHz;
            double held = 0;
            double tphase = rng() * 2 * Math.PI;
            while (held < restMs)
            {
                double k = stepMs / p.DriftTau;
                d0 += -k * d0 + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                d1 += -k * d1 + Math.Sqrt(2 * k) * p.DriftAmp * GaussFrom(rng);
                double ph = tphase + (2 * Math.PI * p.TremorHz * held) / 1000;
                outSteps.Add(new Step(target.X + d0 + p.TremorAmp * Math.Sin(ph),
                                      target.Y + d1 + p.TremorAmp * Math.Sin(ph + Math.PI / 2),
                                      stepMs * Lerp(0.9, 1.4, rng())));
                held += stepMs;
                elapsed += stepMs;
            }
            cur = target;
        }
        return outSteps;
    }

    // ---- after-action (SC 232 lane C) ------------------------------------------------------------
    // Salt for the after-action stream (hover dwell, move-off after a click, the attention model): a separate
    // Mulberry32(seed ^ salt) stream keeps MakePersona's draw order (every seed's motor identity) unchanged.
    // Same values in Python _motion.py and Node motion.ts.
    private const uint AfterSalt = 0x165667B1; // own stream (was the timing salt)

    /// <summary>The pointer keeps within this many px of where a hover arrived while it dwells there.</summary>
    public const double DwellRadiusPx = 3;
    /// <summary>A move-off after a click is kept this far inside the viewport.</summary>
    public const double MoveOffMarginPx = 8;

    /// <summary>
    /// The persona's after-action traits: the first four draws of its salted after-action stream. Measured on
    /// r42 / SDK 0.51.0 (SC 232 review): a hover stopped dead on arrival and the pointer never left the pressed
    /// pixel after a click. Same values as Python <c>after_traits</c> and Node <c>afterTraits</c>.
    /// </summary>
    public static AfterTraits AfterTraitsOf(Persona p)
    {
        var r = Mulberry32(p.Seed ^ AfterSalt);
        double dwell = Lerp(250, 700, r());
        double rate = Lerp(0.64, 0.82, r());
        double delay = Lerp(300, 600, r());
        double tempo = Lerp(0.85, 1.15, r());
        return new AfterTraits(dwell, rate, delay, tempo);
    }

    /// <summary>The persona's after-action stream positioned after its four trait draws. Used by the shared vectors
    /// only, to compare the SDKs draw for draw: the humanizer's per-action draws are unseeded, as in PlanMove -- the
    /// seed fixes traits only, or every session of an identity would replay one sequence.</summary>
    public static Rng AfterRng(Persona p)
    {
        var r = Mulberry32(p.Seed ^ AfterSalt);
        for (int i = 0; i < 4; i++) r();
        return r;
    }

    /// <summary>How long a hover dwells on arrival: the persona's centre × 0.6–1.4, kept in 150–800 ms (150–900 as the
    /// page measures it, with the glide's slow tail).</summary>
    public static double DwellMs(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return Clamp(AfterTraitsOf(p).DwellMs * Lerp(0.6, 1.4, rng()), 150, 800);
    }

    /// <summary>
    /// Plan the pointer's rest on a hover target: OU drift + 8–12 Hz tremor around <paramref name="at"/> for about
    /// <paramref name="ms"/> (at least 3 samples), every sample within <see cref="DwellRadiusPx"/> of it and inside
    /// <paramref name="box"/> (1 px in; null: no box). Drift ×2 and tremor ×1.5 of a glide's: a hovering hand is
    /// not braced the way it is before a press.
    /// </summary>
    public static List<Step> PlanDwell(Point at, Box? box, Persona p, double ms, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double stepMs = 1000 / p.DeviceHz;
        int n = (int)Math.Max(3, JsRound(Math.Max(0, ms) / (stepMs * 1.15)));   // rest gaps average 1.15 frames
        var outSteps = new List<Step>(n);
        double d0 = 0, d1 = 0;
        double tphase = rng() * 2 * Math.PI;
        double t = 0;
        for (int i = 0; i < n; i++)
        {
            double k = stepMs / p.DriftTau;
            d0 += -k * d0 + Math.Sqrt(2 * k) * p.DriftAmp * 2.0 * GaussFrom(rng);
            d1 += -k * d1 + Math.Sqrt(2 * k) * p.DriftAmp * 2.0 * GaussFrom(rng);
            double ph = tphase + (2 * Math.PI * p.TremorHz * t) / 1000;
            double dx = d0 + p.TremorAmp * 1.5 * Math.Sin(ph);
            double dy = d1 + p.TremorAmp * 1.5 * Math.Sin(ph + Math.PI / 2);
            double r = Hypot(dx, dy);
            if (r > DwellRadiusPx) { dx = dx * DwellRadiusPx / r; dy = dy * DwellRadiusPx / r; }
            double x = at.X + dx, y = at.Y + dy;
            if (box is Box b)
            {
                x = b.Width >= 2 ? Clamp(x, b.X + 1, b.X + b.Width - 1) : at.X;
                y = b.Height >= 2 ? Clamp(y, b.Y + 1, b.Y + b.Height - 1) : at.Y;
            }
            double sl = stepMs * Lerp(0.9, 1.4, rng());
            outSteps.Add(new Step(x, y, sl));
            t += sl;
        }
        return outSteps;
    }

    /// <summary>Does this click get a move-off? In <see cref="AfterTraits.MoveOffRate"/> of clicks: a draw against the
    /// rate, nudged by how far the page's running share (<paramref name="hits"/> of <paramref name="n"/> earlier clicks)
    /// has drifted from it.</summary>
    public static bool MoveOffRoll(Persona p, Rng? rng = null, int hits = 0, int n = 0)
    {
        rng ??= Random.Shared.NextDouble;
        double rate = AfterTraitsOf(p).MoveOffRate;
        return rng() < Clamp(rate + 0.5 * (rate * n - hits), 0.05, 0.98);
    }

    /// <summary>The release → move-off gap: the persona's centre × 0.7–1.3, kept in 200–660 ms (200–700 as the page measures it).</summary>
    public static double MoveOffDelayMs(Persona p, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double t = Humanize.TempoOf(p);
        return Clamp(AfterTraitsOf(p).MoveOffDelayMs * t * Lerp(0.7, 1.3, rng()), 200 * t, 660 * t);
    }

    /// <summary>
    /// Where the hand drifts after a click: 40–200 px from <paramref name="from"/> at an angle in the downward
    /// half-plane (20–160°, y down: the gateway's moveOnTarget model), kept <see cref="MoveOffMarginPx"/> inside the
    /// viewport; mirrored upward when the clamp would leave it on the spot (a click near the bottom edge).
    /// </summary>
    public static Point PlanMoveOff(Point from, double vw, double vh, Persona p, Rng? rng = null)
    {
        _ = p;
        rng ??= Random.Shared.NextDouble;
        double m = MoveOffMarginPx;
        double ang = Lerp(20, 160, rng()) * (Math.PI / 180);   // as Python's math.radians rounds it
        double dist = Lerp(40, 200, rng());
        double dx = Math.Cos(ang) * dist, dy = Math.Sin(ang) * dist;
        double x = Clamp(from.X + dx, m, vw - m);
        double y = Clamp(from.Y + dy, m, vh - m);
        if (Hypot(x - from.X, y - from.Y) < 12)
        {
            x = Clamp(from.X + dx, m, vw - m);
            y = Clamp(from.Y - dy, m, vh - m);
        }
        return new Point(x, y);
    }

    // ------------------------------------------------------------------ fresh-page entry (SC 239 / pro-r43-13)
    /// <summary>The first in-view sample of an entering path sits at y 0–4 (<see cref="EnterFromTop"/>).</summary>
    public const double EntryCrossPx = 4.0;

    /// <summary>An ambient drift on a fresh page is planned from the entry point too (its first excursion comes in over
    /// the top edge, Humanize.AmbientPlan); the dropped lead-in is ~100–200 ms, so it is planned this much longer and
    /// then trimmed to the ask.</summary>
    public const double EntryLeadMs = 250.0;

    /// <summary>Where a hand comes from on a page the pointer has not been over yet: 30–120 px ABOVE the view, within
    /// ±30 % of the view width of <paramref name="target"/> (8 px inside the view's sides) — in from the tab strip /
    /// address bar, as pro-r43-13 does for the engine's own first path. Python <c>entry_point</c> / Node <c>entryPoint</c>:
    /// x first, then y, two draws from <paramref name="rng"/>. A <paramref name="viewW"/> of 16 or less means a 1000 px view.</summary>
    public static Point EntryPoint(Point target, double viewW, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double w = viewW > 16 ? viewW : 1000.0;
        double x = Clamp(target.X + (rng() - 0.5) * 0.6 * w, 8.0, w - 8.0);
        double y = -(30.0 + rng() * 90.0);
        return new Point(x, y);
    }

    /// <summary>The steps of a path planned from the off-view <paramref name="start"/> with the samples still above the
    /// view dropped and the first one put where the path crosses the top edge (y 0–4; one draw from <paramref name="rng"/>
    /// when a crossing sample is made). The crossing sample takes the timing of the sample it replaces (or of the first
    /// kept one). A path whose start is inside the view, or that never comes in, is returned as it is.</summary>
    public static List<Step> EnterFromTop(List<Step> steps, Point start, Rng? rng = null)
    {
        if (steps.Count == 0 || start.Y >= 0) return steps;
        int i = 0;
        while (i < steps.Count && steps[i].Y < 0) i++;
        if (i >= steps.Count) return steps;
        var a = i == 0 ? start : new Point(steps[i - 1].X, steps[i - 1].Y);
        var b = steps[i];
        if (b.Y <= EntryCrossPx) return steps.GetRange(i, steps.Count - i);
        rng ??= Random.Shared.NextDouble;
        double yIn = rng() * EntryCrossPx;
        double f = b.Y > a.Y ? (yIn - a.Y) / (b.Y - a.Y) : 1.0;
        double cx = a.X + (b.X - a.X) * f;
        var outSteps = new List<Step>(steps.Count + 1);
        if (i == 0) { outSteps.Add(steps[0] with { X = cx, Y = yIn }); outSteps.AddRange(steps); }
        else { outSteps.Add(steps[i - 1] with { X = cx, Y = yIn }); outSteps.AddRange(steps.GetRange(i, steps.Count - i)); }
        return outSteps;
    }
}

/// <summary>Per-identity after-action traits (see <see cref="Motion.AfterTraitsOf"/>): the centre of the hover dwell
/// (250–700 ms), the share of clicks followed by a move-off (0.64–0.82), the centre of the release → move-off gap
/// (300–600 ms) and the scale on the attention model's read pause (0.85–1.15).</summary>
public readonly record struct AfterTraits(double DwellMs, double MoveOffRate, double MoveOffDelayMs, double ReadTempo);
