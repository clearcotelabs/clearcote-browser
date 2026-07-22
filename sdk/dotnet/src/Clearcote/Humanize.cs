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
///   HumanTypeAsync /      key-press-dwell-duration (a scripted keystroke holds ~1-3ms)
///   HumanPressAsync
///   HumanSelectOptionAsync  interaction-select-change-trust — Playwright's SelectOptionAsync
///                         dispatches input+change from script, so both arrive isTrusted=false and
///                         the engine cannot produce that. Driving the closed select with arrow
///                         keys makes the BROWSER fire them, trusted.
/// </summary>
public static class Humanize
{
    // Persona per page. ConditionalWeakTable so a closed page is collectable — a Dictionary here
    // would pin every page a long-lived process ever opened.
    private static readonly ConditionalWeakTable<IPage, Persona> Personas = new();
    private static readonly ConditionalWeakTable<IPage, PointerState> Pointers = new();

    private sealed class PointerState { public double X; public double Y; public bool Known; }

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

    /// <summary>Glide the cursor to a point along a planned path, dispatching every sample.</summary>
    private static async Task GlideAsync(IPage page, Persona p, PointerState st, double toX, double toY, double targetW)
    {
        // With no known origin the pointer has never been anywhere: start somewhere plausible rather
        // than teleporting from (0,0), which is itself a shape a real session does not produce.
        if (!st.Known)
        {
            var vp = page.ViewportSize;
            st.X = vp is null ? 400 : Rand(vp.Width * 0.2, vp.Width * 0.8);
            st.Y = vp is null ? 300 : Rand(vp.Height * 0.2, vp.Height * 0.8);
            st.Known = true;
            await page.Mouse.MoveAsync((float)st.X, (float)st.Y).ConfigureAwait(false);
        }
        var steps = Motion.PlanMove(new Point(st.X, st.Y), new Point(toX, toY), p, targetW);
        foreach (var s in steps)
        {
            await page.Mouse.MoveAsync((float)s.X, (float)s.Y).ConfigureAwait(false);
            if (s.SleepMs > 0) await Task.Delay((int)Math.Round(s.SleepMs)).ConfigureAwait(false);
        }
        st.X = toX;
        st.Y = toY;
    }

    /// <summary>Move to the element along a human path and click it with a human press-hold.</summary>
    public static async Task HumanClickAsync(this ILocator locator, LocatorClickOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(locator);
        var page = locator.Page;
        var p = PersonaFor(page);
        var st = StateFor(page);

        await locator.ScrollIntoViewIfNeededAsync().ConfigureAwait(false);
        var bb = await locator.BoundingBoxAsync().ConfigureAwait(false);
        if (bb is null) { await locator.ClickAsync(options).ConfigureAwait(false); return; }

        var box = new Box(bb.X, bb.Y, bb.Width, bb.Height);
        var from = st.Known ? new Point(st.X, st.Y) : new Point(bb.X - 120, bb.Y - 90);
        // Aim at a point drawn from the persona, not the geometric centre — the centre every time is
        // what a landing-dispersion check measures.
        var target = Motion.ClickPoint(box, from, p);

        await GlideAsync(page, p, st, target.X, target.Y, Math.Max(6, bb.Width)).ConfigureAwait(false);
        await Task.Delay((int)Rand(40, 130)).ConfigureAwait(false);   // settle before pressing
        await page.Mouse.DownAsync().ConfigureAwait(false);
        await Task.Delay((int)Math.Round(Motion.ClickHold(p))).ConfigureAwait(false);
        await page.Mouse.UpAsync().ConfigureAwait(false);
    }

    /// <summary>Type into the element key-by-key with human dwell and cadence.</summary>
    public static async Task HumanTypeAsync(this ILocator locator, string text)
    {
        ArgumentNullException.ThrowIfNull(locator);
        ArgumentNullException.ThrowIfNull(text);
        var page = locator.Page;
        var p = PersonaFor(page);
        await locator.HumanClickAsync().ConfigureAwait(false);
        await Task.Delay((int)Rand(60, 180)).ConfigureAwait(false);
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
}
