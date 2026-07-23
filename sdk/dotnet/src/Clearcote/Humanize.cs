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
///                         at the driver's origin, a corner no reader scrolls from. Also eases the
///                         delta into chunks — one 1000px wheel event is not a gesture a wheel emits.
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
    // pre-keystroke placement has been spent.
    private sealed class PointerState { public double X; public double Y; public bool Known; public bool Ambient; }

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
    /// Clearcote.cs sets <c>ViewportSize = ViewportSize.NoViewport</c> for every headed launch, so
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
            var r = await page.EvaluateAsync<JsonElement?>("() => [innerWidth, innerHeight]")
                              .ConfigureAwait(false);
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
            var r = await page.EvaluateAsync<JsonElement?>(
                @"() => {
                    const el = document.activeElement;
                    if (!el || el === document.body || el === document.documentElement) return null;
                    const b = el.getBoundingClientRect();
                    return (b.width && b.height) ? { x: b.x, y: b.y, w: b.width, h: b.height } : null;
                }").ConfigureAwait(false);
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
    /// Scroll with the wheel from a place a reader would actually scroll from, easing the delta the
    /// way a wheel does.
    ///
    /// A wheel event carries the cursor's coordinates, and Playwright's cursor starts at the document
    /// origin: an unpositioned scroll delivers every wheel at (0,0), a corner where the element under
    /// the pointer is never the content being read. The pointer is only re-homed when it is nowhere
    /// sensible — a human does not move the mouse back to the middle between two scrolls of one page.
    /// </summary>
    public static async Task HumanWheelAsync(this IPage page, float deltaX, float deltaY)
    {
        ArgumentNullException.ThrowIfNull(page);
        var p = PersonaFor(page);
        var st = StateFor(page);

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

        int chunks = (int)Math.Clamp(Math.Round((Math.Abs(deltaX) + Math.Abs(deltaY)) / 60.0), 5, 24);
        float px = 0, py = 0;
        try
        {
            for (int i = 1; i <= chunks; i++)
            {
                double t = (double)i / chunks;
                double f = 1 - Math.Pow(1 - t, 2.2);   // ease-OUT: a fast flick decaying to an inertial settle
                float nx = (float)Math.Round(deltaX * f), ny = (float)Math.Round(deltaY * f);
                await page.Mouse.WheelAsync(nx - px, ny - py).ConfigureAwait(false);
                px = nx; py = ny;
                // Local sleep, never WaitForTimeoutAsync: that is a CDP round-trip per call, so it emits
                // protocol traffic a detector can score — a self-inflicted tell inside the humanize path.
                await Task.Delay((int)Rand(10, 38)).ConfigureAwait(false);
                if (Rnd.NextDouble() < 0.07)
                    await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);   // mid-scroll reading pause
                // A hand resting on the mouse does not hold it still through a long scroll, and two
                // dozen wheel events on byte-identical coordinates is not what a hand produces.
                if (chunks >= 10 && Rnd.NextDouble() < 0.06) await DriftAsync(page, st).ConfigureAwait(false);
            }
            if (px != deltaX || py != deltaY)
                await page.Mouse.WheelAsync(deltaX - px, deltaY - py).ConfigureAwait(false);   // exact total
        }
        catch (PlaywrightException)
        {
            // Deliver the remainder plainly: humanize must never lose scroll the caller asked for.
            await page.Mouse.WheelAsync(deltaX - px, deltaY - py).ConfigureAwait(false);
        }
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
    /// </summary>
    public static async Task HumanTypeAsync(this IPage page, string text)
    {
        ArgumentNullException.ThrowIfNull(page);
        ArgumentNullException.ThrowIfNull(text);
        var p = PersonaFor(page);
        await AmbientPlaceAsync(page, p, StateFor(page)).ConfigureAwait(false);
        for (int i = 0; i < text.Length; i++)
        {
            await page.Keyboard.PressAsync(text[i].ToString(),
                new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
            if (i < text.Length - 1)
            {
                // Gaussian inter-key cadence with a floor — a realistic distribution, not a uniform band.
                double d = Math.Max(25, Gauss(85, 45));
                if (char.IsWhiteSpace(text[i])) d += Rand(20, 100);
                if (Rnd.NextDouble() < 0.06) d += Rand(180, 450);   // occasional thinking pause
                await Task.Delay((int)d).ConfigureAwait(false);
            }
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
            var planJson = await locator.EvaluateAsync<JsonElement?>(
                @"(el, want) => {
                    if (!el || el.tagName !== 'SELECT' || el.multiple || el.disabled) return null;
                    const os = [...el.options];
                    const i = os.findIndex(o => o.value === want);
                    if (i < 0 || os[i].disabled) return null;
                    return { to: i, from: el.selectedIndex, ret: os[i].value };
                }", value).ConfigureAwait(false);

            if (planJson is { ValueKind: JsonValueKind.Object } plan)
            {
                int to = plan.GetProperty("to").GetInt32();
                int fromIdx = plan.GetProperty("from").GetInt32();
                string ret = plan.GetProperty("ret").GetString() ?? value;
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
                int got = await locator.EvaluateAsync<int>("el => el.selectedIndex").ConfigureAwait(false);
                if (got == to) return new[] { ret };
            }
        }
        catch (PlaywrightException)
        {
            // fall through to native
        }

        var res = await locator.SelectOptionAsync(new[] { value }).ConfigureAwait(false);
        return res.ToArray();
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
