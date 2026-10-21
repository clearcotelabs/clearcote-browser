using System.Reflection;
using Microsoft.Playwright;

namespace Clearcote;

// The public pointer primitives: what Python's and Node's humanized page.mouse.move / down / up / dblclick and
// locator.tap do, as explicit .NET calls (see the Humanize class remarks: there is no object to patch here).
// Every move goes through GlideAsync, the same planner and wall-clock pacing as the targeted actions. The engine's
// own pointer path (when one is routed here) belongs at the marked seam in HumanMoveAsync.
public static partial class Humanize
{
    /// <summary>The fallback a tap on a touch context reports (Python/Node <c>TAP_NOT_HUMANIZED</c>): there is no
    /// humanized touch sequence yet, so the tap goes out as the humanized mouse click.</summary>
    public const string TapNotHumanized = "tap is not humanized for touch contexts yet";

    /// <summary>
    /// Move the pointer to viewport point (<paramref name="x"/>, <paramref name="y"/>) along the persona's path
    /// (minimum-jerk submovements, coloured noise, Fitts timing), from wherever it is; on a page the pointer has not
    /// been over yet the path comes in over the top edge. While a button is held
    /// (<see cref="HumanMouseDownAsync"/>) the move is a DRAG leg: planned on the line from grab to drop
    /// (<see cref="Motion.PlanDrag"/>), every sample carrying the button. Python/Node <c>page.mouse.move(x, y)</c>
    /// under humanize.
    /// </summary>
    public static async Task HumanMoveAsync(this IPage page, double x, double y)
    {
        ArgumentNullException.ThrowIfNull(page);
        var st = StateFor(page);
        // A held leg runs no after-action step (a move-off or an idle drift mid-drag would carry the button);
        // a bare move runs it first, as a coordinate action (no exploratory scroll), like Node's mouse.move.
        bool held = st.Held;
        await SerialAsync(page, async () =>
        {
            var p = PersonaFor(page);
            // seam: an engine pointer path takes the move here when it can (a held leg needs the buttons carried)
            await GlideAsync(page, p, st, x, y, 24, held: st.Held).ConfigureAwait(false);
        }, after: !held, coords: true).ConfigureAwait(false);
        if (held) AfterOf(page).LastEnd = NowMs();
    }

    /// <summary>
    /// Press a mouse button where the pointer is. Once a move has placed the pointer, the cursor is re-pinned to the
    /// tracked point and the press waits the persona's pre-press pause (40-250 ms) first: the hand confirms the
    /// target before the button goes down. A press with no move before it stays instant and lands where the driver's
    /// cursor sits (humanize never invents a target for it). The button stays tracked as held until
    /// <see cref="HumanMouseUpAsync"/>, so <see cref="HumanMoveAsync"/> walks drag legs meanwhile.
    /// Python/Node <c>page.mouse.down()</c> under humanize.
    /// </summary>
    public static async Task HumanMouseDownAsync(this IPage page, MouseDownOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, async () =>
        {
            var st = StateFor(page);
            if (st.Known)
            {
                // re-pin: the press must land where the caller's last move ended, whatever else moved the cursor
                try { await page.Mouse.MoveAsync((float)st.X, (float)st.Y).ConfigureAwait(false); }
                catch (PlaywrightException) { /* best effort: keep the press on target */ }
                await PrePressAsync(PersonaFor(page)).ConfigureAwait(false);
            }
            st.Held = true;
            try
            {
                await page.Mouse.DownAsync(options).ConfigureAwait(false);
            }
            catch
            {
                st.Held = false;   // a press that failed must not make every later move a drag leg
                throw;
            }
        }, after: false).ConfigureAwait(false);
        AfterOf(page).LastEnd = NowMs();
    }

    /// <summary>Release a mouse button; the held state ends whatever happens. Python/Node <c>page.mouse.up()</c>
    /// under humanize.</summary>
    public static async Task HumanMouseUpAsync(this IPage page, MouseUpOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        await SerialAsync(page, async () =>
        {
            var st = StateFor(page);
            try { await page.Mouse.UpAsync(options).ConfigureAwait(false); }
            finally { st.Held = false; }
        }, after: false).ConfigureAwait(false);
        AfterOf(page).LastEnd = NowMs();
    }

    /// <summary>
    /// A humanized double click at viewport point (<paramref name="x"/>, <paramref name="y"/>): the glide, then two
    /// presses with the persona's hold and its gap between them, as
    /// <see cref="HumanClickAsync(IPage,double,double,HumanClickOptions?)"/> with ClickCount 2 (Playwright's
    /// <c>Mouse.DblClickAsync</c> teleports and presses for ~0 ms). Python/Node <c>page.mouse.dblclick(x, y)</c>
    /// under humanize.
    /// </summary>
    public static Task HumanDblClickAsync(this IPage page, double x, double y, HumanClickOptions? options = null)
        => page.HumanClickAsync(x, y, (options ?? new HumanClickOptions()) with { ClickCount = 2 });

    /// <summary>
    /// Tap the element. A context without touch (Playwright's <c>HasTouch</c> off) gets Playwright's own
    /// <c>TapAsync</c>, which raises at once there, as stock. On a touch context there is no humanized touch sequence
    /// yet: the tap goes out as the humanized mouse click (<see cref="HumanClickAsync(ILocator,LocatorClickOptions)"/>,
    /// with Force, Modifiers, Position, Timeout and Trial), and it is reported (<see cref="TapNotHumanized"/>):
    /// strict mode throws <see cref="HumanizeStrictException"/> and nothing is sent, otherwise
    /// <see cref="HumanizeOptions.OnFallback"/> hears it. Python/Node <c>locator.tap()</c> under humanize.
    /// </summary>
    public static async Task HumanTapAsync(this ILocator locator, LocatorTapOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        // a frame locator keeps Playwright's tap (no frame route for a tap, as in Node and Python)
        if (HasTouch(page) == false || IsFrameLocator(locator))
        {
            await locator.TapAsync(options).ConfigureAwait(false);
            return;
        }
        Report(page, "HumanTapAsync", TapNotHumanized + ": sent as a humanized mouse click", SelectorOf(locator));
        await locator.HumanClickAsync(new LocatorClickOptions
        {
            Force = options?.Force, Modifiers = options?.Modifiers, Position = options?.Position,
            Timeout = options?.Timeout, Trial = options?.Trial,
        }).ConfigureAwait(false);
    }

    /// <summary>Was the page's context made with Playwright's <c>HasTouch</c>? Read from the options Playwright
    /// keeps on its own context object (the Node SDK reads <c>context._options.hasTouch</c>); null when they cannot be
    /// read (then the tap is treated as a touch tap, as Node does).</summary>
    internal static bool? HasTouch(IPage page)
    {
        try
        {
            var ctx = page.Context;
            if (ctx is null) return null;
            if (HasTouchOverride.TryGetValue(ctx, out var forced)) return forced.Value;
            const BindingFlags all = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            for (var t = ctx.GetType(); t is not null; t = t.BaseType)
            {
                object? opts = t.GetProperty("Options", all)?.GetValue(ctx) ?? t.GetField("_options", all)?.GetValue(ctx);
                if (opts?.GetType().GetProperty("HasTouch", all) is { } ht) return ht.GetValue(opts) as bool? ?? false;
            }
        }
        catch (Exception) { /* a context-like object */ }
        return null;
    }

    // Test seam: a context's HasTouch, in place of Playwright's own options (fake contexts have none).
    internal static readonly System.Runtime.CompilerServices.ConditionalWeakTable<IBrowserContext, System.Runtime.CompilerServices.StrongBox<bool>>
        HasTouchOverride = new();
}
