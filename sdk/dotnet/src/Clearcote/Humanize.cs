using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// Human input for Playwright .NET: pointer paths, click/key dwell, landing dispersion and
/// engine-fired dropdown selection.
///
/// WHY THIS IS EXTENSION METHODS AND NOT A TRANSPARENT PATCH, unlike Python and Node. Those two
/// SDKs replace <c>page.click</c> / <c>Locator.press</c> on the live object, so an existing script
/// becomes humanised with one launch flag and no edits. C# cannot do that: <see cref="IPage"/> and
/// <see cref="ILocator"/> are interfaces implemented by internal Playwright classes, there is no
/// prototype to reassign, and a decorating wrapper would have to reimplement the entire interface
/// and would still be bypassed the moment a caller obtained a page from an event or a popup.
///
/// So the .NET surface is explicit: <c>HumanClickAsync</c> beside <c>ClickAsync</c>. The cost is
/// that it is opt-in per call rather than per launch. The benefit is that it is honest — there is
/// no launch flag that silently does nothing, which is what a half-working transparent patch would
/// have been.
///
/// WHAT EACH METHOD DEFEATS, all measured against clearcotelabs.com/audit:
///   HumanClickAsync       pointer-press-dwell-duration (a scripted click holds ~0.3ms; no switch
///                         can), pointer-landing-dispersion (a driver lands on the identical
///                         sub-pixel point every time), pointer-movement-precedes-click
///   HumanTypeAsync /      key-press-dwell-duration (a scripted keystroke holds ~1-3ms). Both also
///   HumanPressAsync       place the cursor once per page before the first keystroke: keys that
///                         arrive in a session carrying no pointer events at all are separable on
///                         that alone, however good the dwell is.
///   HumanSelectOptionAsync  interaction-select-change-trust — Playwright's SelectOptionAsync
///                         dispatches input+change from script, so both arrive isTrusted=false and
///                         the engine cannot produce that. Driving the closed select with arrow
///                         keys makes the BROWSER fire them, trusted. The IPage overload exists
///                         because page.SelectOptionAsync(selector, value) is what most scripts
///                         actually write, and it goes straight to the untrusted path.
///   HumanWheelAsync /     pointer-movement-precedes-click, wheel edition: a wheel event carries the
///   HumanScrollAsync      cursor's coordinates, so an unpositioned scroll delivers every one of them
///                         at the driver's origin, a corner no reader scrolls from. Also scrolls in
///                         whole notches of the persona platform's size, in bursts — a CDP wheel event
///                         always reports a full notch, so a sub-notch slice is an event no device emits.
///   HumanDragToAsync      pointer-press-dwell-duration at both ends of the gesture (grab hesitation
///                         after the press, settle before the release) plus a held-button path
///                         instead of down/teleport/up, which a range thumb reads as no drag at all.
/// </summary>
public static class Humanize
{
    // Persona per page. ConditionalWeakTable so a closed page is collectable — a Dictionary here
    // would pin every page a long-lived process ever opened.
    private static readonly ConditionalWeakTable<IPage, Persona> Personas = new();
    private static readonly ConditionalWeakTable<IPage, PointerState> Pointers = new();

    // Known is the invariant every press depends on: false means the driver's cursor has never been
    // moved on this page and still sits at the document origin. Ambient records that the one-per-page
    // pre-keystroke placement has been spent. NotchPx is the wheel notch of the platform the page
    // claims, read on the first wheel and kept (0 until then).
    private sealed class PointerState { public double X; public double Y; public bool Known; public bool Ambient; public int NotchPx; }

    // Test seam: a forced rollover share for one page, in place of the persona's (the tests pin 0 and 1).
    internal static readonly ConditionalWeakTable<IPage, StrongBox<double>> RolloverOverride = new();

    /// <summary>
    /// Attach a motor persona to a page. Pass the fingerprint seed so the same identity moves the
    /// same way here as it does under the Python and Node SDKs; omit it for a random persona.
    /// </summary>
    public static void Attach(IPage page, object? seed = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        Personas.Remove(page);
        Personas.Add(page, Motion.MakePersona(seed));
    }

    /// <summary>The page's persona, creating a random one on first use if none was attached.</summary>
    public static Persona PersonaFor(IPage page)
    {
        ArgumentNullException.ThrowIfNull(page);
        if (Personas.TryGetValue(page, out var p)) return p;
        var made = Motion.MakePersona();
        Personas.Add(page, made);
        return made;
    }

    private static PointerState StateFor(IPage page)
    {
        if (Pointers.TryGetValue(page, out var s)) return s;
        var made = new PointerState();
        Pointers.Add(page, made);
        return made;
    }

    private static readonly Random Rnd = Random.Shared;
    private static double Rand(double lo, double hi) => lo + Rnd.NextDouble() * (hi - lo);

    /// <summary>
    /// Give the pointer an origin when it has never had one: start somewhere plausible rather than
    /// teleporting from (0,0), which is itself a shape a real session does not produce.
    /// </summary>
    /// <summary>
    /// The REAL viewport, in CSS px — NOT <c>page.ViewportSize</c>.
    ///
    /// Clearcote.cs sets <c>ViewportSize = ViewportSize.NoViewport</c> on every headed context launch
    /// (and on headless ones where the engine's persona owns the screen), so
    /// that innerWidth tracks the real OS window instead of Playwright's emulated 1280x720 (an
    /// emulated viewport on a headed window is itself a tell). With NoViewport, page.ViewportSize is
    /// null for the life of the page, so a <c>?? 1280 / ?? 800</c> fallback was taken on EVERY headed
    /// run: the scroll anchor's "is the pointer inside the viewport" test then compared against a box
    /// unrelated to the window, and on a maximized display a cursor legitimately at x=1400 read as
    /// out-of-bounds and got re-homed on every scroll — the exact motion that gate exists to avoid.
    /// innerWidth/innerHeight are correct in both modes; the constant is a last resort only.
    /// </summary>
    /// <summary>
    /// Is this Playwright failure a TIMEOUT?
    ///
    /// Playwright for .NET exports exactly ONE exception type — <c>PlaywrightException</c>. There is
    /// no <c>Microsoft.Playwright.TimeoutException</c> (verified by reflecting over
    /// Microsoft.Playwright 1.49.0: PlaywrightException is the only exported Exception subtype), so
    /// a timeout can only be told apart by its message, which Playwright formats as
    /// "Timeout 30000ms exceeded". Catching the wrong thing here matters: a timeout must be
    /// rethrown so a missing element fails on ITS deadline, while any other Playwright error falls
    /// through to the native call.
    /// </summary>
    private static bool IsTimeout(PlaywrightException e) =>
        e.Message.Contains("Timeout", StringComparison.OrdinalIgnoreCase)
        && e.Message.Contains("exceeded", StringComparison.OrdinalIgnoreCase);

    private static async Task<(double W, double H)> ViewportAsync(IPage page)
    {
        try
        {
            // Read in an isolated world (IsolatedWorld): the page never sees it.
            var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.Viewport).ConfigureAwait(false);
            if (r is { } el && el.ValueKind == JsonValueKind.Array && el.GetArrayLength() == 2)
            {
                double w = el[0].GetDouble(), h = el[1].GetDouble();
                if (w > 0 && h > 0) return (w, h);
            }
        }
        catch { /* detached / navigating — fall through to the driver's own answer */ }
        var vp = page.ViewportSize;
        return (vp?.Width ?? 1280, vp?.Height ?? 800);
    }

    private static async Task SeedPointerAsync(IPage page, PointerState st)
    {
        if (st.Known) return;
        var (vw, vh) = await ViewportAsync(page).ConfigureAwait(false);
        st.X = Rand(vw * 0.2, vw * 0.8);
        st.Y = Rand(vh * 0.2, vh * 0.8);
        st.Known = true;
        await page.Mouse.MoveAsync((float)st.X, (float)st.Y).ConfigureAwait(false);
    }

    /// <summary>
    /// Glide the cursor to a point along a planned path, dispatching every sample. <c>settle</c> adds
    /// the seating jiggle a hand makes when it stops on a target — what a range thumb needs at the end
    /// of a drag leg to register the final value.
    /// </summary>
    private static async Task GlideAsync(IPage page, Persona p, PointerState st, double toX, double toY,
        double targetW, bool settle = false)
    {
        await SeedPointerAsync(page, st).ConfigureAwait(false);
        var steps = Motion.PlanMove(new Point(st.X, st.Y), new Point(toX, toY), p, targetW, settle);
        foreach (var s in steps)
        {
            // Native moves carry button state, so a button held via Mouse.DownAsync stays pressed
            // across the whole path — that is what makes a drag a drag rather than a teleport.
            await page.Mouse.MoveAsync((float)s.X, (float)s.Y).ConfigureAwait(false);
            if (s.SleepMs > 0) await Task.Delay((int)Math.Round(s.SleepMs)).ConfigureAwait(false);
        }
        st.X = toX;
        st.Y = toY;
    }

    /// <summary>
    /// Move the pointer onto the element along a human path, without pressing. False means there was
    /// no box to aim at (or the element was not workable), which is the caller's signal to hand the
    /// whole action to Playwright rather than fail it.
    /// </summary>
    private static async Task<bool> GlideOntoAsync(ILocator locator)
    {
        var page = locator.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);
        try
        {
            await locator.ScrollIntoViewIfNeededAsync().ConfigureAwait(false);
            var bb = await locator.BoundingBoxAsync().ConfigureAwait(false);
            if (bb is null) return false;
            var box = new Box(bb.X, bb.Y, bb.Width, bb.Height);
            var from = st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
            // Aim at a point drawn from the persona, not the geometric centre — the centre every time is
            // what a landing-dispersion check measures.
            var target = Motion.ClickPoint(box, from, p);
            await GlideAsync(page, p, st, target.X, target.Y, Math.Max(6, bb.Width)).ConfigureAwait(false);
            return true;
        }
        catch (PlaywrightException e) when (IsTimeout(e))
        {
            // An element that never appears has to fail on ITS timeout. Swallowing this would send the
            // caller to a native call that waits the whole timeout a second time before saying so.
            throw;
        }
        catch (PlaywrightException)
        {
            return false;
        }
    }

    /// <summary>
    /// Guarantee a pointer position before a button goes down. Mouse.DownAsync presses wherever the
    /// driver's cursor sits, and on a page nothing has moved on that is the document origin — the
    /// press then lands on &lt;body&gt; and a drag written as down/move/up grabs nothing at all.
    /// </summary>
    private static async Task EnsurePointerAsync(IPage page, Persona p, PointerState st)
    {
        if (st.Known) return;
        await AmbientPlaceAsync(page, p, st).ConfigureAwait(false);
        await SeedPointerAsync(page, st).ConfigureAwait(false);   // no-op once ambient has placed it
    }

    /// <summary>
    /// A brief non-goal cursor movement, spent at most once per page, before the first keystroke on a
    /// page the pointer has never been on. Placement only: it never presses, so focus and the active
    /// element are untouched, and its endpoints keep clear of the focused control so a widget that
    /// focuses on hover cannot pull focus out from under the keys mid-word.
    /// </summary>
    private static async Task AmbientPlaceAsync(IPage page, Persona p, PointerState st)
    {
        if (st.Ambient || st.Known) return;
        st.Ambient = true;   // spent up front: a failure here must not re-run this on every keystroke
        try
        {
            var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
            var avoid = await FocusedRectAsync(page).ConfigureAwait(false);
            int legs = Rnd.NextDouble() < 0.5 ? 2 : 1;
            for (int i = 0; i < legs; i++)
            {
                var to = IdlePoint(w, h, avoid);
                await GlideAsync(page, p, st, to.X, to.Y, 60).ConfigureAwait(false);
                if (i + 1 < legs) await Task.Delay((int)Rand(80, 240)).ConfigureAwait(false);
            }
        }
        catch (PlaywrightException)
        {
            // Best effort: the keystrokes still go out, just without the pointer history.
        }
    }

    /// <summary>The focused element's viewport rect, so ambient placement can steer around it.</summary>
    private static async Task<Box?> FocusedRectAsync(IPage page)
    {
        try
        {
            // Read in an isolated world (IsolatedWorld): the page never sees it.
            var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.FocusedRect).ConfigureAwait(false);
            if (r is { ValueKind: JsonValueKind.Object } rect)
                return new Box(rect.GetProperty("x").GetDouble(), rect.GetProperty("y").GetDouble(),
                               rect.GetProperty("w").GetDouble(), rect.GetProperty("h").GetDouble());
        }
        catch (PlaywrightException)
        {
            // No frame to evaluate in: place without steering rather than skip the placement.
        }
        return null;
    }

    /// <summary>A plausible idle spot, kept 40px clear of <paramref name="avoid"/> where possible.</summary>
    private static Point IdlePoint(double w, double h, Box? avoid)
    {
        Point pt = new(w * 0.5, h * 0.5);
        for (int i = 0; i < 6; i++)
        {
            pt = new Point(Rand(w * 0.12, w * 0.88), Rand(h * 0.12, h * 0.80));
            if (avoid is not Box a) break;
            if (pt.X < a.X - 40 || pt.X > a.X + a.Width + 40 ||
                pt.Y < a.Y - 40 || pt.Y > a.Y + a.Height + 40) break;
        }
        return pt;
    }

    /// <summary>Move to the element along a human path and click it with a human press-hold.</summary>
    public static async Task HumanClickAsync(this ILocator locator, LocatorClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);

        if (!await GlideOntoAsync(locator).ConfigureAwait(false))
        {
            await locator.ClickAsync(options).ConfigureAwait(false);
            return;
        }
        await Task.Delay((int)Rand(40, 130)).ConfigureAwait(false);   // settle before pressing
        await EnsurePointerAsync(page, p, st).ConfigureAwait(false);
        await page.Mouse.DownAsync().ConfigureAwait(false);
        await Task.Delay((int)Math.Round(Motion.ClickHold(p))).ConfigureAwait(false);
        await page.Mouse.UpAsync().ConfigureAwait(false);
    }

    /// <summary>
    /// Press on the element and drag to a page point, holding the button for the whole path.
    ///
    /// The press is the part that has to be guaranteed: Mouse.DownAsync presses wherever the driver's
    /// cursor sits, so a drag written as down/move/up on a fresh page grabs the document origin and
    /// the slider never moves. The glide leaves a known position and the press is guarded by it.
    ///
    /// Every leg is a native mouse move, which carries button state, so the button stays down across
    /// the trajectory — a range thumb ignores a move that reports no button held. The grab hesitation
    /// after pressing and the settle before releasing are the two endpoint dwells a scripted drag has
    /// neither of, and the settle jiggle on the final leg is what seats the thumb on a value.
    ///
    /// <c>targetW</c> is the width of whatever is being dropped on, and only scales the Fitts duration.
    /// </summary>
    public static async Task HumanDragToAsync(this ILocator source, double toX, double toY, double targetW = 24)
    {
        ArgumentNullException.ThrowIfNull(source);
        var page = source.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);

        if (!await GlideOntoAsync(source).ConfigureAwait(false))
        {
            // Nothing to aim at: run Playwright's own drag recipe, whose HoverAsync raises the proper
            // actionability error if the element cannot be used at all.
            await source.HoverAsync().ConfigureAwait(false);
            await page.Mouse.DownAsync().ConfigureAwait(false);
            await page.Mouse.MoveAsync((float)toX, (float)toY).ConfigureAwait(false);
            await page.Mouse.UpAsync().ConfigureAwait(false);
            st.X = toX; st.Y = toY; st.Known = true;
            return;
        }

        var (grabMs, releaseMs) = Motion.DragDwell(p);
        await Task.Delay((int)Rand(100, 200)).ConfigureAwait(false);   // hand arrives before the button goes down
        await EnsurePointerAsync(page, p, st).ConfigureAwait(false);
        await page.Mouse.DownAsync().ConfigureAwait(false);
        try
        {
            await Task.Delay((int)Math.Round(grabMs)).ConfigureAwait(false);        // grab hesitation
            await GlideAsync(page, p, st, toX, toY, targetW, settle: true).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(releaseMs)).ConfigureAwait(false);     // settle before letting go
        }
        finally
        {
            // A button left down survives into every later action, so the release is unconditional.
            try { await page.Mouse.UpAsync().ConfigureAwait(false); } catch (PlaywrightException) { }
        }
    }

    /// <inheritdoc cref="HumanDragToAsync(ILocator,double,double,double)"/>
    public static async Task HumanDragToAsync(this ILocator source, ILocator target)
    {
        ArgumentNullException.ThrowIfNull(source);
        ArgumentNullException.ThrowIfNull(target);
        var page = source.Page;
        var st = StateFor(page);

        Point? drop = null;
        double dropW = 24;
        try
        {
            await target.ScrollIntoViewIfNeededAsync().ConfigureAwait(false);
            var tb = await target.BoundingBoxAsync().ConfigureAwait(false);
            if (tb is not null)
            {
                var from = st.Known ? new Point(st.X, st.Y) : new Point(tb.X - 120, tb.Y - 90);
                // Disperse the drop point too: releasing on the target's exact centre every time is
                // the same signature as clicking it.
                drop = Motion.ClickPoint(new Box(tb.X, tb.Y, tb.Width, tb.Height), from, PersonaFor(page));
                dropW = Math.Max(6, tb.Width);
            }
        }
        catch (PlaywrightException e) when (IsTimeout(e))
        {
            throw;   // as in GlideOntoAsync: do not make a missing target wait out two timeouts
        }
        catch (PlaywrightException)
        {
            // fall through to native
        }

        if (drop is not Point d) { await source.DragToAsync(target).ConfigureAwait(false); return; }
        await source.HumanDragToAsync(d.X, d.Y, dropW).ConfigureAwait(false);
    }

    /// <summary>
    /// Scroll with the wheel from a place a reader would actually scroll from, in whole notches.
    ///
    /// A wheel event carries the cursor's coordinates, and Playwright's cursor starts at the document
    /// origin: an unpositioned scroll delivers every wheel at (0,0), a corner where the element under
    /// the pointer is never the content being read. The pointer is only re-homed when it is nowhere
    /// sensible — a human does not move the mouse back to the middle between two scrolls of one page.
    ///
    /// Humanize scrolls WHOLE NOTCHES: the distance delivered is the nearest number of notches (at least
    /// one) of the size the page's platform scrolls per notch — 100px Windows, 120px Linux, 40px macOS —
    /// so a 39px request scrolls 100px under a Windows persona. A wheel event sent over CDP reports a
    /// full notch (wheelDelta ±120) whatever its distance, so the eased sub-notch slices this used to
    /// send (one scroll of 100 as 39/28/20/10/3, measured on r32 and r33) were events no device emits.
    /// Use <c>page.Mouse.WheelAsync</c> where an exact pixel distance matters more than that.
    /// </summary>
    public static async Task HumanWheelAsync(this IPage page, float deltaX, float deltaY)
    {
        ArgumentNullException.ThrowIfNull(page);
        var p = PersonaFor(page);
        var st = StateFor(page);
        int notch = await NotchPxAsync(page, st).ConfigureAwait(false);

        try
        {
            var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
            bool homeless = !st.Known || st.X < 2 || st.Y < 2 || st.X > w - 2 || st.Y > h - 2;
            if (homeless)
            {
                // Upper-middle, gaussian, never the exact centre: landing on (w/2, h/2) to the pixel
                // is the same dispersion tell as a driver that clicks computed centres.
                double rx = Math.Clamp(w * 0.5 + Gauss(0, w * 0.12), w * 0.12, w * 0.88);
                double ry = Math.Clamp(h * 0.38 + Gauss(0, h * 0.10), h * 0.12, h * 0.75);
                await GlideAsync(page, p, st, rx, ry, 80).ConfigureAwait(false);
                await Task.Delay((int)Rand(50, 190)).ConfigureAwait(false);   // read a moment before the flick
            }
        }
        catch (PlaywrightException)
        {
            // Placement is best effort; the scroll below still runs.
        }

        // One notch per event and one axis per event, like a real wheel or tilt: vertical, then horizontal.
        var plan = new List<(float X, float Y)>();
        for (int i = Motion.WheelNotches(deltaY, notch); i > 0; i--) plan.Add((0, Math.Sign(deltaY) * notch));
        for (int i = Motion.WheelNotches(deltaX, notch); i > 0; i--) plan.Add((Math.Sign(deltaX) * notch, 0));
        int next = 0;
        try
        {
            // A finger spins a wheel in bursts: a few notches close together, then a gap while it resets.
            int burstLeft = Rnd.Next(2, 6);
            while (next < plan.Count)
            {
                await page.Mouse.WheelAsync(plan[next].X, plan[next].Y).ConfigureAwait(false);
                if (++next == plan.Count) break;
                // Local sleep, never WaitForTimeoutAsync: that is a CDP round-trip per call, so it emits
                // protocol traffic a detector can score — a self-inflicted tell inside the humanize path.
                if (--burstLeft == 0)
                {
                    await Task.Delay((int)Rand(110, 320)).ConfigureAwait(false);
                    burstLeft = Rnd.Next(2, 6);
                }
                else await Task.Delay((int)Rand(18, 55)).ConfigureAwait(false);
                if (Rnd.NextDouble() < 0.07)
                    await Task.Delay((int)Rand(150, 450)).ConfigureAwait(false);   // mid-scroll reading pause
                // A hand resting on the mouse does not hold it still through a long scroll, and a run of
                // wheel events on byte-identical coordinates is not what a hand produces.
                if (plan.Count >= 6 && Rnd.NextDouble() < 0.06) await DriftAsync(page, st).ConfigureAwait(false);
            }
        }
        catch (PlaywrightException)
        {
            // Deliver the rest plainly: humanize must never lose scroll the caller asked for. Whole
            // notches and no sleeps — a sub-notch remainder would be the very event this method avoids.
            for (; next < plan.Count; next++)
                await page.Mouse.WheelAsync(plan[next].X, plan[next].Y).ConfigureAwait(false);
        }
    }

    /// <summary>
    /// Pixels per wheel notch on this page: read once, from the platform the page claims (the
    /// persona's, which need not be the host's), then kept. The host OS stands in when the read fails.
    /// </summary>
    private static async Task<int> NotchPxAsync(IPage page, PointerState st)
    {
        if (st.NotchPx > 0) return st.NotchPx;
        // Read in an isolated world (IsolatedWorld): the page never sees it.
        var r = await IsolatedWorld.For(page).EvaluateAsync(IsolatedWorld.Platform).ConfigureAwait(false);
        string? platform = r is { ValueKind: JsonValueKind.String } s ? Motion.PlatformFromNavigator(s.GetString()) : null;
        platform ??= OperatingSystem.IsWindows() ? "windows" : OperatingSystem.IsMacOS() ? "macos" : "linux";
        return st.NotchPx = Motion.WheelNotchPx[platform];
    }

    /// <summary>Vertical <see cref="HumanWheelAsync(IPage,float,float)"/>: the common reading scroll.</summary>
    public static Task HumanScrollAsync(this IPage page, float deltaY)
    {
        ArgumentNullException.ThrowIfNull(page);
        return page.HumanWheelAsync(0, deltaY);
    }

    /// <summary>A few pixels of hand drift, kept inside the viewport so the wheel stays over the content.</summary>
    private static async Task DriftAsync(IPage page, PointerState st)
    {
        if (!st.Known) return;
        var (w, h) = await ViewportAsync(page).ConfigureAwait(false);
        double nx = Math.Clamp(st.X + Gauss(0, 2.2), 2, w - 2);
        double ny = Math.Clamp(st.Y + Gauss(0, 2.2), 2, h - 2);
        await page.Mouse.MoveAsync((float)nx, (float)ny).ConfigureAwait(false);
        st.X = nx; st.Y = ny;
    }

    /// <summary>Type into the element key-by-key with human dwell and cadence.</summary>
    public static async Task HumanTypeAsync(this ILocator locator, string text)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(text);
        // Clicking is what focuses the field, and it brings the move that precedes the keystrokes
        // with it — so the page-level type below never has to place the cursor itself.
        await locator.HumanClickAsync().ConfigureAwait(false);
        await Task.Delay((int)Rand(60, 180)).ConfigureAwait(false);
        await locator.Page.HumanTypeAsync(text).ConfigureAwait(false);
    }

    /// <summary>
    /// Type into whatever holds focus, key-by-key with human dwell and cadence. A bare type with no
    /// pointer history on the page gets one ambient placement first: keystrokes from a session where
    /// the mouse never existed are separable from a human's however good the per-key timing is.
    /// Some pairs of plain keys roll over — the next key goes down before the previous one is up — at
    /// the persona's stable <see cref="Motion.RolloverRate"/>.
    /// </summary>
    public static async Task HumanTypeAsync(this IPage page, string text)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(text);
        var p = PersonaFor(page);
        await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);
        // One key per code point (a surrogate pair is one character, not two half keys).
        var chars = text.EnumerateRunes().Select(r => r.ToString()).ToArray();
        double rollover = RolloverOverride.TryGetValue(page, out var forced) ? forced.Value : Motion.RolloverRate(p);
        // Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a
        // moment before the key (35-120 ms) and comes up just after it (10-70 ms), held across a run of
        // them. PressAsync("A") alone sent the key with shiftKey=false and no Shift key at all
        // (measured on r28 with "Ab!") -- input no keyboard produces.
        var shift = false;
        try
        {
            for (int i = 0; i < chars.Length; i++)
            {
                var ch = chars[i];
                if (NeedsShift(ch) && !shift)
                {
                    await page.Keyboard.DownAsync("Shift").ConfigureAwait(false);
                    shift = true;
                    await Task.Delay((int)Rand(35, 120)).ConfigureAwait(false);
                }
                // Rollover: one press per character never overlaps two keys, and a real typist's fast
                // pairs do (0 overlapping keydowns in 32 keys, measured on r32/r33). Only two different
                // plain keys, never under Shift.
                if (!shift && i + 1 < chars.Length && CanRoll(ch) && CanRoll(chars[i + 1]) && ch != chars[i + 1]
                    && Rnd.NextDouble() < rollover && await RollPairAsync(page, ch, chars[i + 1], p).ConfigureAwait(false))
                    ch = chars[++i];   // both typed: carry on as if after the second
                else
                    await PressOrInsertAsync(page, ch, p).ConfigureAwait(false);
                if (shift && (i == chars.Length - 1 || !NeedsShift(chars[i + 1])))
                {
                    await Task.Delay((int)Rand(10, 70)).ConfigureAwait(false);
                    await page.Keyboard.UpAsync("Shift").ConfigureAwait(false);
                    shift = false;
                }
                if (i < chars.Length - 1)
                {
                    // Gaussian inter-key cadence with a floor — a realistic distribution, not a uniform band.
                    double d = Math.Max(25, Gauss(85, 45));
                    if (ch.Length == 1 && char.IsWhiteSpace(ch[0])) d += Rand(20, 100);
                    if (Rnd.NextDouble() < 0.06) d += Rand(180, 450);   // occasional thinking pause
                    await Task.Delay((int)d).ConfigureAwait(false);
                }
            }
        }
        finally
        {
            // never leave Shift held for the caller's next keystroke
            if (shift)
            {
                try { await page.Keyboard.UpAsync("Shift").ConfigureAwait(false); }
                catch (PlaywrightException) { /* best-effort */ }
            }
        }
    }

    // The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
    // types with Shift held: capitals and the shifted symbols.
    internal static bool NeedsShift(string ch)
        => ch.Length == 1 && ((ch[0] >= 'A' && ch[0] <= 'Z') || "~!@#$%^&*()_+{}|:\"<>?".IndexOf(ch[0]) >= 0);

    // Keys that may roll: lower-case, unshifted, one physical key each on the US layout.
    private const string RollKeys = "abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=";
    private static bool CanRoll(string ch) => ch.Length == 1 && RollKeys.IndexOf(ch[0]) >= 0;

    // Two keys with rollover: b goes down while a is still held, then a comes up, then b. DownAsync
    // inserts the character just as PressAsync does, so the typed value is unchanged. False (nothing
    // sent) when the dwell is too short to overlap inside. A failure releases both keys, best effort,
    // and stops the typing the way a failed press does.
    private static async Task<bool> RollPairAsync(IPage page, string a, string b, Persona p)
    {
        double d1 = Motion.KeyDwell(p), d2 = Motion.KeyDwell(p);
        if (d1 - 8 < 12) return false;
        double overlap = Rand(12, Math.Min(55, d1 - 8));
        try
        {
            await page.Keyboard.DownAsync(a).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(d1 - overlap)).ConfigureAwait(false);
            await page.Keyboard.DownAsync(b).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(overlap)).ConfigureAwait(false);
            await page.Keyboard.UpAsync(a).ConfigureAwait(false);
            await Task.Delay((int)Math.Round(Math.Max(5, d2 - overlap))).ConfigureAwait(false);
            await page.Keyboard.UpAsync(b).ConfigureAwait(false);
            return true;
        }
        catch
        {
            // As with Shift: never leave a key held for the caller's next keystroke.
            foreach (var k in new[] { a, b })
            {
                try { await page.Keyboard.UpAsync(k).ConfigureAwait(false); }
                catch (Exception) { /* best-effort */ }
            }
            throw;
        }
    }

    // One key with the persona's hold. Playwright maps only its US layout; for anything else ("ö",
    // emoji) PressAsync throws "Unknown key", and the text is inserted instead -- the same fallback
    // the Python and Node SDKs use.
    private static async Task PressOrInsertAsync(IPage page, string ch, Persona p)
    {
        try
        {
            await page.Keyboard.PressAsync(ch,
                new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
        }
        catch (PlaywrightException)
        {
            await page.Keyboard.TypeAsync(ch).ConfigureAwait(false);
        }
    }

    private static double Gauss(double mean, double sd)
    {
        double u = 1.0 - Rnd.NextDouble(), v = Rnd.NextDouble();
        return mean + sd * Math.Sqrt(-2 * Math.Log(u)) * Math.Cos(2 * Math.PI * v);
    }

    /// <summary>
    /// Press a key with the persona's hold. This is the .NET equivalent of the fix that landed in
    /// the Python and Node SDKs in 0.19.3: <c>PressAsync</c> without a Delay emits keydown and keyUp
    /// in the same instant, which no finger does.
    /// </summary>
    public static async Task HumanPressAsync(this IPage page, string key)
    {
        ArgumentNullException.ThrowIfNull(page);
        var p = PersonaFor(page);
        // Same reason as the page-level type: one ambient placement when nothing has moved the
        // pointer here yet, so the key does not arrive on a mouse-free session.
        await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);
        await page.Keyboard.PressAsync(key,
            new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
    }

    /// <inheritdoc cref="HumanPressAsync(IPage,string)"/>
    public static async Task HumanPressAsync(this ILocator locator, string key)
    {
        ArgumentNullException.ThrowIfNull(locator);
        await locator.FocusAsync().ConfigureAwait(false);
        await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);
        await locator.Page.HumanPressAsync(key).ConfigureAwait(false);
    }

    /// <summary>
    /// Choose a &lt;select&gt; option with the keyboard, so the ENGINE fires input and change.
    ///
    /// Playwright's SelectOptionAsync assigns the value and dispatches both events from script, so
    /// they arrive with isTrusted=false — and the engine cannot produce an untrusted change, which
    /// makes it one of the most reliable dropdown tells there is. A select that has focus and is
    /// CLOSED steps on ArrowUp/ArrowDown and the browser emits the events itself.
    ///
    /// Falls back to native SelectOptionAsync when the keyboard route cannot be SHOWN to have
    /// worked: multi-selects, disabled or unresolvable options, and platforms where arrows open the
    /// popup instead of stepping (macOS). SelectedIndex is verified afterwards rather than assumed,
    /// because a silently wrong selection is worse than an untrusted one.
    /// </summary>
    public static async Task<IReadOnlyList<string>> HumanSelectOptionAsync(this ILocator locator, string value)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(value);
        var page = locator.Page;

        try
        {
            if (await PlanSelectAsync(locator, value).ConfigureAwait(false) is { } plan)
            {
                var (to, fromIdx, ret) = plan;
                if (to == fromIdx) return new[] { ret };   // already selected; forge nothing

                // Move to the control before operating it. Placement only, no press: the arrow-key
                // route needs the select CLOSED, and a change event in a session with no pointer
                // movement toward the select is still separable even once it is trusted.
                await GlideOntoAsync(locator).ConfigureAwait(false);
                await locator.FocusAsync().ConfigureAwait(false);
                await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
                string step = to > fromIdx ? "ArrowDown" : "ArrowUp";
                for (int i = 0; i < Math.Abs(to - fromIdx); i++)
                {
                    await page.HumanPressAsync(step).ConfigureAwait(false);
                    await Task.Delay((int)Rand(45, 120)).ConfigureAwait(false);
                }
                if (await locator.InputValueAsync().ConfigureAwait(false) == ret) return new[] { ret };
            }
        }
        catch (PlaywrightException)
        {
            // fall through to native
        }

        var res = await locator.SelectOptionAsync(new[] { value }).ConfigureAwait(false);
        return res.ToArray();
    }

    /// <summary>
    /// Where the keyboard route has to go: the target option's index, the current one, and the value.
    /// Built only from Playwright's element queries (attributes, disabled state, text, input value),
    /// which run in Playwright's utility world -- the page never sees them. A locator.EvaluateAsync
    /// ran the lookup in the page's own world. Null when the route cannot apply (multi-select,
    /// disabled, no such option).
    /// </summary>
    private static async Task<(int To, int From, string Ret)?> PlanSelectAsync(ILocator select, string value)
    {
        if (await select.GetAttributeAsync("multiple").ConfigureAwait(false) is not null) return null;
        if (await select.IsDisabledAsync().ConfigureAwait(false)) return null;
        var options = select.Locator("option");
        int n = await options.CountAsync().ConfigureAwait(false);
        var current = await select.InputValueAsync().ConfigureAwait(false);
        int to = -1, from = -1;
        for (int i = 0; i < n && (to < 0 || from < 0); i++)
        {
            var option = options.Nth(i);
            // An option's value is its value attribute, else its text with whitespace collapsed.
            var v = await option.GetAttributeAsync("value").ConfigureAwait(false)
                    ?? string.Join(' ', ((await option.TextContentAsync().ConfigureAwait(false)) ?? "")
                        .Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries));
            if (from < 0 && v == current) from = i;
            if (to < 0 && v == value)
            {
                if (await option.IsDisabledAsync().ConfigureAwait(false)) return null;
                to = i;
            }
        }
        return to < 0 || from < 0 ? null : (to, from, value);
    }

    /// <inheritdoc cref="HumanSelectOptionAsync(ILocator,string)"/>
    /// <remarks>
    /// The selector form exists because page.SelectOptionAsync(selector, value) is the call scripts
    /// actually reach for, and every one of them lands on the untrusted script-assigned change that
    /// interaction-select-change-trust reads. Same resolution Playwright does, then the same
    /// keyboard route as the locator overload — no second implementation to drift out of step.
    /// </remarks>
    public static Task<IReadOnlyList<string>> HumanSelectOptionAsync(this IPage page, string selector, string value)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(selector);
        ArgumentNullException.ThrowIfNull(value);
        return page.Locator(selector).HumanSelectOptionAsync(value);
    }
}
