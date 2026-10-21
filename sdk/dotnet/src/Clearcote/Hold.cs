using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// Options for <see cref="Humanize.PressAndHoldAsync(ILocator,PressAndHoldOptions?,CancellationToken)"/>.
/// </summary>
public sealed record PressAndHoldOptions
{
    /// <summary>How long the button stays down, in ms: taken exactly. With <see cref="Until"/> it is the longest the
    /// hold may last (15 s when not given). Without either, a fresh 2.8-3.8 s per call. Must be a positive, finite
    /// number (else <see cref="ArgumentException"/>).</summary>
    public double? Duration { get; init; }

    /// <summary>The point to press, relative to the element's PADDING box (as Playwright's <c>Position</c> and the
    /// humanized click's; on a humanized page the border widths are added -- before 0.52 it was the border box here).
    /// Default: a persona-shaped point near the centre on a humanized page (a covered one moves to the first probe point
    /// the hit test proves), the centre otherwise.</summary>
    public Position? Position { get; init; }

    /// <summary>The button held down (default <see cref="MouseButton.Left"/>).</summary>
    public MouseButton? Button { get; init; }

    /// <summary>How long to wait for the element to become visible, in ms (null = the page's default timeout).</summary>
    public float? Timeout { get; init; }

    /// <summary>Polled every 100 ms while the button is down; once it returns true the button is let go after a
    /// human reaction delay (160-350 ms), not on the same tick the condition flipped.</summary>
    public Func<Task<bool>>? Until { get; init; }

    /// <summary>A resting hand still moves the mouse now and then: sparse whole-pixel drift while the button is
    /// down, never more than 2 px from the press point. False keeps the pointer still.</summary>
    public bool Tremor { get; init; } = true;
}

/// <summary>
/// The pure plan of a press-and-hold (no I/O), a port of the Python SDK's <c>_hold.py</c>: the same numbers in
/// every SDK. How long the button stays down, the drift while it is down, the release reaction, the press point.
/// </summary>
internal static class HoldPlan
{
    public const double DefaultHoldMinMs = 2800, DefaultHoldMaxMs = 3800;
    /// <summary><c>Until</c> with no <c>Duration</c>: give up after this long.</summary>
    public const double UntilCapMs = 15000;
    /// <summary>How often <c>Until</c> is asked.</summary>
    public const double PollMs = 100;
    /// <summary>Drift never leaves this square around the press point.</summary>
    public const int TremorRadiusPx = 2;
    public const double ReactionMinMs = 160, ReactionMaxMs = 350;

    /// <summary>One drift step: at <c>T</c> ms into the hold, the pointer sits at (Dx, Dy) whole pixels from the press point.</summary>
    public readonly record struct Drift(double T, int Dx, int Dy);

    /// <summary>How long the button stays down: <paramref name="duration"/> exactly; else, with an <c>Until</c>, the cap
    /// after which the hold gives up; else a fresh 2.8-3.8 s.</summary>
    public static double HoldMs(double? duration, bool hasUntil, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        if (duration is double d)
        {
            if (!double.IsFinite(d) || d <= 0)
                throw new ArgumentException("PressAndHoldAsync: Duration must be a positive number of milliseconds", "options");
            return d;
        }
        if (hasUntil) return UntilCapMs;
        return DefaultHoldMinMs + rng() * (DefaultHoldMaxMs - DefaultHoldMinMs);
    }

    /// <summary>
    /// Whole-pixel offsets from the press point while the button is held. A first move 250-650 ms in, then one every
    /// 250-900 ms; each moves one pixel on x and/or y and stays within <see cref="TremorRadiusPx"/> of the press point
    /// (a step that would leave it goes back toward it). None in the last 120 ms: the hand is already letting go.
    /// </summary>
    public static List<Drift> TremorPlan(double totalMs, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        var outp = new List<Drift>();
        int ox = 0, oy = 0;
        double t = 250.0 + rng() * 400.0;
        while (t < totalMs - 120.0 && outp.Count < 1000)
        {
            int dx, dy;
            do
            {
                dx = (int)(rng() * 3) - 1;
                dy = (int)(rng() * 3) - 1;
            } while (dx == 0 && dy == 0);
            int nx = ox + dx, ny = oy + dy;
            if (Math.Abs(nx) > TremorRadiusPx) nx = ox - (nx > 0 ? 1 : -1);
            if (Math.Abs(ny) > TremorRadiusPx) ny = oy - (ny > 0 ? 1 : -1);
            ox = nx;
            oy = ny;
            outp.Add(new Drift(t, ox, oy));
            t += 250.0 + rng() * 650.0;
        }
        return outp;
    }

    /// <summary>The delay between <c>Until</c> turning true and the release.</summary>
    public static double ReactionMs(Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        return ReactionMinMs + rng() * (ReactionMaxMs - ReactionMinMs);
    }

    /// <summary>The viewport point to press: <paramref name="position"/> (relative to the PADDING box: the
    /// <paramref name="border"/> widths (left, top) are added, as the humanized click does), else the persona's click
    /// point when there is a persona, else the centre.</summary>
    public static Point HoldPoint(Box box, Point? from = null, Persona? persona = null, Position? position = null,
        (double L, double T) border = default)
    {
        if (position is not null) return new Point(box.X + border.L + position.X, box.Y + border.T + position.Y);
        if (persona is not null && from is Point f) return Motion.ClickPoint(box, f, persona);
        return new Point(box.X + box.Width / 2.0, box.Y + box.Height / 2.0);
    }

    public static MouseButton ButtonOf(MouseButton? button)
    {
        var b = button ?? MouseButton.Left;
        if (b != MouseButton.Left && b != MouseButton.Right && b != MouseButton.Middle)
            throw new ArgumentException("PressAndHoldAsync: Button must be Left, Right or Middle", "options");
        return b;
    }
}

public static partial class Humanize
{
    /// <summary>
    /// Press and hold this element: one long button press, for controls that act only while the button stays down
    /// (a hold-to-confirm button, a long-press menu, a "press and hold" verification button). Not a Playwright method.
    /// <list type="number">
    /// <item>waits for the element to be visible (<see cref="PressAndHoldOptions.Timeout"/>) and brings it into view -- on a
    /// humanized page with the WHEEL (<see cref="Scroll.BringIntoViewAsync"/>: whole notches or a trackpad swipe to 25-60 % of
    /// the viewport; before 0.52 this was a programmatic scroll), falling back to the programmatic scroll -- reported -- only
    /// for a container the wheel cannot move; Playwright's scroll on any other page;</item>
    /// <item>moves the pointer to a point inside it (<see cref="PressAndHoldOptions.Position"/>, else a persona-shaped
    /// point near the centre on a humanized page, the centre otherwise);</item>
    /// <item>presses and holds for <see cref="PressAndHoldOptions.Duration"/> ms, or until
    /// <see cref="PressAndHoldOptions.Until"/> returns true, then lets go after a 160-350 ms reaction;</item>
    /// <item>while the button is down, sparse whole-pixel drift within 2 px of the press point
    /// (<see cref="PressAndHoldOptions.Tremor"/>), every move a trusted move with the button reported held.</item>
    /// </list>
    /// <para>A HUMANIZED page is one with a persona (<see cref="Attach"/>, or any earlier <c>Human*</c> call on it):
    /// there the approach is a humanized glide, the press comes after the persona's pre-press pause, and the hold
    /// takes its turn with the page's other <c>Human*</c> calls. On any other page the pointer moves straight there
    /// with <c>page.Mouse</c>. A locator inside a frame (<c>FrameLocator</c>) works either way: the press is made at
    /// viewport coordinates.</para>
    /// <para>Returns true when the hold ran its full duration or <c>Until</c> became true, false when <c>Until</c> never
    /// did. The button is ALWAYS released, also when <c>Until</c> throws or <paramref name="cancellationToken"/> is
    /// cancelled mid-hold (then an <see cref="OperationCanceledException"/> follows the release). A bad Duration or
    /// Button raises <see cref="ArgumentException"/>; an element that never shows raises Playwright's timeout.</para>
    /// </summary>
    public static Task<bool> PressAndHoldAsync(this ILocator locator, PressAndHoldOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(locator);
        return PressAndHoldEntryAsync(locator, options ?? new PressAndHoldOptions(), cancellationToken);
    }

    /// <summary>The first element matching <paramref name="selector"/>, pressed and held as
    /// <see cref="PressAndHoldAsync(ILocator,PressAndHoldOptions?,CancellationToken)"/> does (page actions are not strict).</summary>
    public static Task<bool> PressAndHoldAsync(this IPage page, string selector, PressAndHoldOptions? options = null,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        return PressAndHoldEntryAsync(page.Locator(selector).First, options ?? new PressAndHoldOptions(), cancellationToken);
    }

    private static Task<bool> PressAndHoldEntryAsync(ILocator loc, PressAndHoldOptions o, CancellationToken ct)
    {
        // argument errors are raised before anything waits or moves
        var button = HoldPlan.ButtonOf(o.Button);
        double total = HoldPlan.HoldMs(o.Duration, o.Until is not null);
        return SerialAsync(loc.Page, () => PressAndHoldCoreAsync(loc, o, button, total, ct));
    }

    /// <summary>Does <paramref name="loc"/> target the page's main frame (so the isolated-world box helpers apply)? Not
    /// a FrameLocator, and a locator made on a child <see cref="IFrame"/> is told by the frame it was made on (read
    /// through Playwright's Locator._frame); false when that cannot be read, which takes Playwright's own box.</summary>
    internal static bool InMainFrame(ILocator loc)
        => !IsFrameLocator(loc) && FrameOf(loc) is { ParentFrame: null };   // FrameOf: Frames.cs

    private static async Task<bool> PressAndHoldCoreAsync(ILocator loc, PressAndHoldOptions o, MouseButton button,
        double total, CancellationToken ct)
    {
        ct.ThrowIfCancellationRequested();
        var page = loc.Page;
        Personas.TryGetValue(page, out var persona);   // humanized page: one with a persona
        var st = persona is null ? null : StateFor(page);

        LocatorBoundingBoxResult? bb = null;
        if (persona is not null && InMainFrame(loc))
        {
            try
            {
                bb = await BoxOfAsync(loc, o.Timeout, force: false).ConfigureAwait(false);
            }
            catch (Exception e) when (!IsTimeoutError(e))
            {
                bb = null;   // Playwright's own wait and scroll below (a strict-mode error is raised there)
            }
        }
        if (bb is null)
        {
            await loc.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible, Timeout = o.Timeout })
                .ConfigureAwait(false);
            await loc.ScrollIntoViewIfNeededAsync(new LocatorScrollIntoViewIfNeededOptions { Timeout = o.Timeout })
                .ConfigureAwait(false);
            bb = await loc.BoundingBoxAsync().ConfigureAwait(false);
        }
        if (bb is null) throw new PlaywrightException("PressAndHoldAsync: the element has no box (not rendered)");
        ct.ThrowIfCancellationRequested();

        Point? from = st is null ? null : st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
        // A humanized main-frame page reads the element through the isolated world: border widths for a padding-box
        // `Position` (like click's), and the covered-point check on the press point (the same hit test as a click).
        bool iso = persona is not null && InMainFrame(loc);
        (double, double) border = default;
        if (iso && o.Position is not null)
        {
            try
            {
                var info = await DescribeAsync(page, bb, null).ConfigureAwait(false);
                if (info is not null) border = (info.Bl, info.Bt);
            }
            catch (Exception) { border = default; }
        }
        var pt = HoldPlan.HoldPoint(new Box(bb.X, bb.Y, bb.Width, bb.Height), from, persona, o.Position, border);
        double x = Math.Round(pt.X), y = Math.Round(pt.Y);
        if (iso && o.Position is null
            && await ReachesPointAsync(page, bb, x, y, CssOf(loc)).ConfigureAwait(false) == false
            && await ProvenClickPointAsync(page, bb, CssOf(loc)).ConfigureAwait(false) is { } alt)
        {
            (x, y) = (alt.X, alt.Y);   // a covered persona point: the first probe point the hit test proves
        }

        if (persona is not null && st is not null)
        {
            await GlideAsync(page, persona, st, x, y, AimW(bb)).ConfigureAwait(false);
            await PrePressAsync(persona).ConfigureAwait(false);   // the hand confirms the target before it presses
        }
        else
        {
            await page.Mouse.MoveAsync((float)x, (float)y).ConfigureAwait(false);
        }
        // A drift move: a plain native move (it carries the held button), no glide; on a humanized page the tracked
        // pointer follows it so the next humanized action starts from where the hand really is.
        async Task Nudge(double nx, double ny)
        {
            await page.Mouse.MoveAsync((float)nx, (float)ny).ConfigureAwait(false);
            if (st is not null)
            {
                st.X = nx;
                st.Y = ny;
                st.Known = true;
            }
        }

        await page.Mouse.DownAsync(new MouseDownOptions { Button = button }).ConfigureAwait(false);
        var sw = Stopwatch.StartNew();
        bool done = o.Until is null;
        try
        {
            var drift = o.Tremor ? HoldPlan.TremorPlan(total) : new List<HoldPlan.Drift>();
            int i = 0;
            double nextPoll = 0;
            while (true)
            {
                ct.ThrowIfCancellationRequested();
                double now = sw.Elapsed.TotalMilliseconds;
                if (now >= total) break;
                if (o.Until is not null && now >= nextPoll)
                {
                    nextPoll = now + HoldPlan.PollMs;
                    if (await o.Until().ConfigureAwait(false))
                    {
                        done = true;
                        await Task.Delay(TimeSpan.FromMilliseconds(HoldPlan.ReactionMs()), ct).ConfigureAwait(false);
                        break;
                    }
                }
                while (i < drift.Count && drift[i].T <= now)
                {
                    await Nudge(x + drift[i].Dx, y + drift[i].Dy).ConfigureAwait(false);
                    i++;
                }
                double wake = total;
                if (i < drift.Count) wake = Math.Min(wake, drift[i].T);
                if (o.Until is not null) wake = Math.Min(wake, nextPoll);
                double sleep = Math.Max(0, Math.Min(wake - sw.Elapsed.TotalMilliseconds, 50));
                await Task.Delay(TimeSpan.FromMilliseconds(sleep), ct).ConfigureAwait(false);
            }
        }
        finally
        {
            // never leave the button down: every later move would be a drag (not cancellable on purpose)
            await page.Mouse.UpAsync(new MouseUpOptions { Button = button }).ConfigureAwait(false);
        }
        return done;
    }
}
