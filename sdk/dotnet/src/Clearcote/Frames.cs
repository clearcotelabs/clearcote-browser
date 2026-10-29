using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// Isolated worlds for every frame of one page, on the page's CDP session, one per frameId (Node frames.ts
/// FrameWorlds, Python _frames.FrameWorlds). The main frame's own world stays <see cref="IsolatedWorld"/>; these
/// read the nodes the browser's hit test finds INSIDE a same-process child frame, and the &lt;iframe&gt; owners up
/// the frame chain. Every call answers null when it cannot be made (no CDP session, a frame gone mid-call).
internal sealed class FrameWorlds
{
    private static readonly ConditionalWeakTable<IPage, FrameWorlds> All = new();
    private readonly IPage _page;
    private readonly Dictionary<string, int> _ctx = new();

    private FrameWorlds(IPage page) => _page = page;

    /// The page's frame worlds (created on first use).
    public static FrameWorlds For(IPage page) => All.GetValue(page, p => new FrameWorlds(p));

    private IsolatedWorld W => IsolatedWorld.For(_page);

    public Task<JsonElement?> SendAsync(string method, Dictionary<string, object>? args = null) => W.SendAsync(method, args);

    /// The main frame's id (the root of Page.getFrameTree), or null.
    public async Task<string?> MainAsync()
    {
        var t = await SendAsync("Page.getFrameTree").ConfigureAwait(false);
        return t is { ValueKind: JsonValueKind.Object } o && o.TryGetProperty("frameTree", out var ft)
               && ft.ValueKind == JsonValueKind.Object && ft.TryGetProperty("frame", out var f)
               && f.ValueKind == JsonValueKind.Object && f.TryGetProperty("id", out var id) && id.ValueKind == JsonValueKind.String
            ? id.GetString()
            : null;
    }

    private async Task<int?> ContextAsync(string fid)
    {
        if (_ctx.TryGetValue(fid, out var c)) return c;
        var r = await SendAsync("Page.createIsolatedWorld", new Dictionary<string, object> { ["frameId"] = fid }).ConfigureAwait(false);
        if (r is not { ValueKind: JsonValueKind.Object } o || !o.TryGetProperty("executionContextId", out var e)
            || e.ValueKind != JsonValueKind.Number)
            return null;
        _ctx[fid] = e.GetInt32();
        return _ctx[fid];
    }

    internal static JsonElement? Value(JsonElement? r)
    {
        if (r is not { } res || res.ValueKind != JsonValueKind.Object || res.TryGetProperty("exceptionDetails", out _))
            return null;
        return res.TryGetProperty("result", out var result) && result.ValueKind == JsonValueKind.Object
               && result.TryGetProperty("value", out var value)
            ? value.Clone()
            : null;
    }

    /// Run <paramref name="fn"/> (a <c>function (arg) { ... }</c> whose <c>this</c> is the node) on node
    /// <paramref name="nid"/> in frame <paramref name="fid"/>'s isolated world; its JSON value, or null.
    public async Task<JsonElement?> CallAsync(string fid, int nid, string fn, object? arg = null)
    {
        for (int i = 0; i < 2; i++)
        {
            if (await ContextAsync(fid).ConfigureAwait(false) is not int c) return null;
            var obj = await SendAsync("DOM.resolveNode", new Dictionary<string, object>
            {
                ["backendNodeId"] = nid, ["executionContextId"] = c,
            }).ConfigureAwait(false);
            if (obj is not { ValueKind: JsonValueKind.Object } o || !o.TryGetProperty("object", out var ob)
                || ob.ValueKind != JsonValueKind.Object || !ob.TryGetProperty("objectId", out var oidEl)
                || oidEl.GetString() is not { } oid)
            {
                _ctx.Remove(fid);   // a stale world (the frame navigated)
                continue;
            }
            try
            {
                return Value(await SendAsync("Runtime.callFunctionOn", new Dictionary<string, object>
                {
                    ["objectId"] = oid, ["functionDeclaration"] = fn,
                    ["arguments"] = new object[] { new Dictionary<string, object?> { ["value"] = arg } },
                    ["returnByValue"] = true,
                }).ConfigureAwait(false));
            }
            finally
            {
                await SendAsync("Runtime.releaseObject", new Dictionary<string, object> { ["objectId"] = oid }).ConfigureAwait(false);
            }
        }
        return null;
    }

    /// The browser's hit test at viewport point (x, y): the deepest node there and the frame that owns it (for a
    /// same-process child frame, the element INSIDE the frame), or null.
    public async Task<(int Node, string Frame)?> HitAsync(double x, double y)
    {
        if (await MainAsync().ConfigureAwait(false) is not { } main) return null;
        var h = await SendAsync("DOM.getNodeForLocation",
            IsolatedWorld.NodeAtParams(x, y, await W.ScrollAsync().ConfigureAwait(false))).ConfigureAwait(false);
        if (h is not { ValueKind: JsonValueKind.Object } o || !o.TryGetProperty("backendNodeId", out var id)
            || id.ValueKind != JsonValueKind.Number)
            return null;
        var fid = o.TryGetProperty("frameId", out var f) && f.ValueKind == JsonValueKind.String
                  && !string.IsNullOrEmpty(f.GetString()) ? f.GetString()! : main;
        return (id.GetInt32(), fid);
    }

    /// frameId -> parent frameId (null for the root), from a Page.getFrameTree answer's <c>frameTree</c>.
    internal static Dictionary<string, string?> Parents(JsonElement tree)
    {
        var out_ = new Dictionary<string, string?>();
        void Walk(JsonElement t, string? parent)
        {
            if (t.ValueKind != JsonValueKind.Object) return;
            string? id = null;
            if (t.TryGetProperty("frame", out var f) && f.ValueKind == JsonValueKind.Object
                && f.TryGetProperty("id", out var idEl) && idEl.GetString() is { Length: > 0 } s)
            {
                id = s;
                out_[id] = parent;
            }
            if (t.TryGetProperty("childFrames", out var kids) && kids.ValueKind == JsonValueKind.Array)
                foreach (var k in kids.EnumerateArray()) Walk(k, id);
        }
        Walk(tree, null);
        return out_;
    }

    /// Where frame <paramref name="fid"/>'s viewport starts in the main viewport: each owning &lt;iframe&gt;'s
    /// content-box origin (read in its parent frame's isolated world), summed up the frame chain; null when a link
    /// of the chain cannot be read.
    public async Task<(double X, double Y)?> OriginAsync(string fid)
    {
        var main = await MainAsync().ConfigureAwait(false);
        var tree = await SendAsync("Page.getFrameTree").ConfigureAwait(false);
        if (main is null || tree is not { ValueKind: JsonValueKind.Object } t || !t.TryGetProperty("frameTree", out var ft))
            return null;
        var parents = Parents(ft);
        double ox = 0, oy = 0;
        var f = fid;
        for (int i = 0; i < 16; i++)
        {
            if (f == main) return (ox, oy);
            if (!parents.TryGetValue(f, out var up) || up is null) return null;
            var own = await SendAsync("DOM.getFrameOwner", new Dictionary<string, object> { ["frameId"] = f }).ConfigureAwait(false);
            if (own is not { ValueKind: JsonValueKind.Object } ow || !ow.TryGetProperty("backendNodeId", out var b)
                || b.ValueKind != JsonValueKind.Number)
                return null;
            var o = await CallAsync(up, b.GetInt32(), Humanize.OwnerOrigin).ConfigureAwait(false);
            if (o is not { ValueKind: JsonValueKind.Array } a || a.GetArrayLength() != 2
                || a[0].ValueKind != JsonValueKind.Number || a[1].ValueKind != JsonValueKind.Number)
                return null;
            ox += a[0].GetDouble();
            oy += a[1].GetDouble();
            f = up;
        }
        return null;
    }
}

/// Humanized input for elements inside child frames (a FrameLocator's locator, or a locator made on a child
/// <see cref="IFrame"/>). Mirrors sdk/node/src/frames.ts and sdk/python/clearcote/_frames.py.
///
/// <para>Such a locator used to go straight to Playwright's own action: an untrusted <c>change</c> for a select, a
/// ~0 ms click, keys with no dwell. The pointer and the keyboard are the PAGE's, so the route only has to find -- and
/// prove with the browser's own hit test, read in the child frame's isolated world (<see cref="FrameWorlds"/>) -- the
/// viewport point a press inside the frame lands on, then glide, press and type through the page's humanized input.
/// A frame whose nodes the page's CDP session cannot see (a cross-origin frame running out of process) is answered by
/// its &lt;iframe&gt;: the action goes to Playwright and the fallback is reported (OnFallback, or strict mode's
/// <see cref="HumanizeStrictException"/>). So does anything the route does not cover: list boxes, modifiers,
/// Force / Trial, a field that is not text.</para>
public static partial class Humanize
{
    // The origin of an <iframe>'s CONTENT box (where its document's viewport starts), in the viewport of the
    // document that holds the <iframe>: border and padding inside the border box.
    internal const string OwnerOrigin = @"function () {
  const r = this.getBoundingClientRect(), cs = getComputedStyle(this);
  return [r.x + this.clientLeft + (parseFloat(cs.paddingLeft) || 0),
          r.y + this.clientTop + (parseFloat(cs.paddingTop) || 0)]; }";
    // Does this element (or something inside it) hold the focus of its document?
    internal const string FrameFocused = @"function () { const a = document.activeElement;
  return !!a && (a === this || this.contains(a) || (this.shadowRoot && this.shadowRoot.activeElement != null)); }";
    // Is the hit node an <iframe>/<frame> (the owner of a frame the session cannot see into)?
    internal const string IsFrameOwner = @"function () { const e = this.nodeType === 1 ? this : this.parentElement;
  return !!e && (e.tagName === 'IFRAME' || e.tagName === 'FRAME'); }";

    // An arrow run this long is not walked inside a frame: Playwright's own select.
    private const int FrameArrowHold = 40;

    /// <summary>The frame route cannot (or could not) do this one: report why, then Playwright's own call.</summary>
    private sealed class FrameNativeException : Exception
    {
        public FrameNativeException(string why) : base(why) { }
    }

    /// <summary>The frame a locator was made on (Playwright's Locator._frame), or null when that cannot be read.</summary>
    internal static IFrame? FrameOf(ILocator loc)
    {
        try
        {
            const BindingFlags F = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
            for (var t = loc.GetType(); t is not null; t = t.BaseType)
                if (t.GetField("_frame", F)?.GetValue(loc) is IFrame f) return f;
        }
        catch (Exception)
        {
            // not this Playwright's shape
        }
        return null;
    }

    /// <summary>Does <paramref name="loc"/> reach into a child frame: a FrameLocator's locator (its selector enters
    /// the frame) or a locator made on a child <see cref="IFrame"/>? False when neither can be told (Node
    /// frameLocatorOf).</summary>
    internal static bool InChildFrame(ILocator loc)
        => IsFrameLocator(loc) || FrameOf(loc) is { ParentFrame: not null };

    /// <summary>Why a locator is not routed to the main-frame humanized route; null for a main-frame locator.</summary>
    internal static string? FrameWhy(ILocator loc)
        => IsFrameLocator(loc) ? "frame locators go to Playwright"
            : FrameOf(loc) is { ParentFrame: not null } ? "a locator in a child frame goes to Playwright" : null;

    /// <summary>
    /// The frame route's entry (Node froute + frameRoute): false when <paramref name="loc"/> is a main-frame locator
    /// (the caller's own route runs). Otherwise the action was done here: by the frame route, or -- more than one
    /// match (Playwright raises its strict-mode error), or a fallback reported -- by <paramref name="native"/>.
    /// Playwright's own errors (a timeout, strict mode) and the strict humanize refusal propagate.
    /// </summary>
    private static async Task<(bool Done, T? Result)> FrameRouteAsync<T>(ILocator loc, string api, Func<Task<T>> route,
        Func<Task<T>> native)
    {
        if (!InChildFrame(loc)) return (false, default);
        if (!await StrictOkAsync(loc).ConfigureAwait(false)) return (true, await native().ConfigureAwait(false));
        try
        {
            return (true, await route().ConfigureAwait(false));
        }
        catch (FrameNativeException e)
        {
            Report(loc.Page, api, e.Message, SelectorOf(loc));
        }
        catch (Exception e) when (e is not PlaywrightException && !Propagates(e))
        {
            // the route failing on something it could not read (Node: a TypeError)
            Report(loc.Page, api, $"frame: {e.GetType().Name}: {e.Message}", SelectorOf(loc));
        }
        return (true, await native().ConfigureAwait(false));   // outside any catch: raises once
    }

    private static async Task<bool> FrameRouteAsync(ILocator loc, string api, Func<Task> route, Func<Task> native)
        => (await FrameRouteAsync(loc, api, async () => { await route().ConfigureAwait(false); return true; },
            async () => { await native().ConfigureAwait(false); return true; }).ConfigureAwait(false)).Done;

    // ------------------------------------------------------------------ the entries Humanize.cs calls
    /// <summary>Click / double click / hover inside a child frame. True when done (here or natively).</summary>
    private static Task<bool> FrameClickAsync(ILocator loc, HumanClickOptions options, NormalizedClick o, bool noClick, Func<Task> native)
        => FrameRouteAsync(loc, noClick ? "HumanHoverAsync" : o.ClickCount == 2 ? "HumanDblClickAsync" : "HumanClickAsync",
            () => FrameDoClickAsync(loc, options, o, noClick), native);

    /// <summary>Fill / clear inside a child frame.</summary>
    private static Task<bool> FrameFillAsync(ILocator loc, string api, string value, float? timeout, bool force, Func<Task> native)
        => FrameRouteAsync(loc, api,
            () => FrameDoFillAsync(loc, value, timeout, force), native);

    /// <summary>Type / pressSequentially inside a child frame.</summary>
    private static Task<bool> FrameTypeAsync(ILocator loc, string text, float? delay, Func<Task> native)
        => FrameRouteAsync(loc, "HumanTypeAsync", async () =>
        {
            var t = await FrameTextTargetAsync(loc, null).ConfigureAwait(false);
            await FrameClickIntoAsync(loc.Page, t).ConfigureAwait(false);
            await TypeTextAsync(loc.Page, PersonaFor(loc.Page), text, null, typos: true, floorMs: delay ?? 0, exact: false)
                .ConfigureAwait(false);
        }, native);

    /// <summary>Press a key on an element inside a child frame.</summary>
    private static Task<bool> FramePressAsync(ILocator loc, string key, int repeat, float? delay, Func<Task> native)
        => FrameRouteAsync(loc, "HumanPressAsync", () => FrameDoPressAsync(loc, key, repeat, delay), native);

    /// <summary>Select an option of a drop-down inside a child frame.</summary>
    private static Task<(bool Done, IReadOnlyList<string>? Result)> FrameSelectAsync(ILocator loc, List<SelectWant> wants,
        float? timeout, bool force, Func<Task<IReadOnlyList<string>>> native)
        => FrameRouteAsync<IReadOnlyList<string>>(loc, "HumanSelectOptionAsync",
            () => FrameDoSelectAsync(loc, wants, timeout, force), native);

    /// <summary>Check / uncheck / setChecked inside a child frame.</summary>
    private static Task<bool> FrameSetCheckedAsync(ILocator loc, bool want, float? timeout, bool force, Position? position,
        bool trial, Func<Task> native)
        => FrameRouteAsync(loc, "HumanSetCheckedAsync",
            () => FrameDoSetCheckedAsync(loc, want, timeout, force, position, trial), native);

    // ------------------------------------------------------------------ locating and proving the point
    private sealed class FrameTarget
    {
        public string Fid = "";
        public int Nid;
        public LocatorBoundingBoxResult Box = new();
        public Dictionary<string, object?> FBox = new();
        public FieldInfo? Info;
        public (double Fx, double Fy) Probe;
    }

    private static bool InView(LocatorBoundingBoxResult? b, double vw, double vh)
        => b is not null && b.X >= 0 && b.Y >= 0 && b.X + b.Width <= vw && b.Y + b.Height <= vh;

    /// <summary><paramref name="box"/> brought into the viewport: the WINDOW scrolled first; Playwright's scroll
    /// (which waits for "stable", measured with animation frames inside the frame -- an out-of-process frame below the
    /// fold draws none in headless) only when that was not enough.</summary>
    private static async Task<LocatorBoundingBoxResult?> FrameIntoViewAsync(ILocator loc, LocatorBoundingBoxResult? box, float? timeout)
    {
        if (box is null) return box;
        var w = IsolatedWorld.For(loc.Page);
        var vp = await w.EvaluateAsync(IsolatedWorld.Viewport).ConfigureAwait(false);
        double vw = 0, vh = 0;
        if (vp is { ValueKind: JsonValueKind.Array } a && a.GetArrayLength() == 2
            && a[0].ValueKind == JsonValueKind.Number && a[1].ValueKind == JsonValueKind.Number)
            (vw, vh) = (a[0].GetDouble(), a[1].GetDouble());
        if (vw <= 0 || vh <= 0)
        {
            await loc.ScrollIntoViewIfNeededAsync(new LocatorScrollIntoViewIfNeededOptions { Timeout = timeout }).ConfigureAwait(false);
            return await loc.BoundingBoxAsync().ConfigureAwait(false);
        }
        if (InView(box, vw, vh)) return box;
        double dx = box.X < 0 || box.X + box.Width > vw ? box.X + box.Width / 2.0 - vw / 2 : 0;
        double dy = box.Y < 0 || box.Y + box.Height > vh ? box.Y + box.Height / 2.0 - vh / 2 : 0;
        await w.EvaluateAsync(IsolatedWorld.ScrollBy, new[] { dx, dy }).ConfigureAwait(false);
        var again = await loc.BoundingBoxAsync().ConfigureAwait(false);
        if (InView(again, vw, vh)) return again;
        await loc.ScrollIntoViewIfNeededAsync(new LocatorScrollIntoViewIfNeededOptions { Timeout = timeout }).ConfigureAwait(false);
        return await loc.BoundingBoxAsync().ConfigureAwait(false);
    }

    private static bool SameRect(double[] r, Dictionary<string, object?> b)
        => r.Length == 4 && Math.Abs(r[0] - (double)b["x"]!) <= 1.5 && Math.Abs(r[1] - (double)b["y"]!) <= 1.5
           && Math.Abs(r[2] - (double)b["width"]!) <= 1.5 && Math.Abs(r[3] - (double)b["height"]!) <= 1.5;

    /// <summary>
    /// Wait for <paramref name="loc"/> (Playwright's own wait), bring it into view, and prove where a press reaches it:
    /// the probe points of its box, hit-tested; a hit inside a child frame is read in that frame's isolated world with
    /// the box moved into the frame's viewport. <paramref name="want"/>: null (any element: a click target -- the hit
    /// may be a child of it), or the CSS of the control to describe. Raises FrameNativeException when nothing proves it.
    /// </summary>
    private static async Task<FrameTarget> FrameLocateAsync(ILocator loc, float? timeout, string? want)
    {
        await loc.WaitForAsync(new LocatorWaitForOptions { State = WaitForSelectorState.Visible, Timeout = timeout }).ConfigureAwait(false);
        var box = await FrameIntoViewAsync(loc, await loc.BoundingBoxAsync().ConfigureAwait(false), timeout).ConfigureAwait(false);
        if (box is null || box.Width <= 0 || box.Height <= 0) throw new FrameNativeException("frame: the element has no box");
        var fw = FrameWorlds.For(loc.Page);
        var main = await fw.MainAsync().ConfigureAwait(false);
        int owners = 0;
        foreach (var (fx, fy) in ProbePoints)
        {
            if (await fw.HitAsync(box.X + box.Width * fx, box.Y + box.Height * fy).ConfigureAwait(false) is not { } h)
                throw new FrameNativeException("frame: no CDP hit test here");
            var (nid, fid) = h;
            if (fid == main)
            {
                // the point is answered by an <iframe>: a frame this session cannot see into
                if (await fw.CallAsync(fid, nid, IsFrameOwner).ConfigureAwait(false) is { ValueKind: JsonValueKind.True }) owners++;
                continue;
            }
            if (await fw.OriginAsync(fid).ConfigureAwait(false) is not { } o) continue;
            var fbox = new Dictionary<string, object?>
            {
                ["x"] = box.X - o.X, ["y"] = box.Y - o.Y, ["width"] = (double)box.Width, ["height"] = (double)box.Height,
            };
            if (want is null)
            {
                if (await fw.CallAsync(fid, nid, IsolatedWorld.HitInBox, fbox).ConfigureAwait(false) is { ValueKind: JsonValueKind.True })
                    return new FrameTarget { Fid = fid, Nid = nid, Box = box, FBox = fbox, Probe = (fx, fy) };
                continue;
            }
            var r = await fw.CallAsync(fid, nid, IsolatedWorld.Describe, new Dictionary<string, object?> { ["want"] = want })
                .ConfigureAwait(false);
            if (r is { } el && FieldInfo.From(el, nid) is { } info && SameRect(info.Rect, fbox))
                return new FrameTarget { Fid = fid, Nid = nid, Box = box, FBox = fbox, Info = info, Probe = (fx, fy) };
        }
        if (owners > 0)
            throw new FrameNativeException("frame: cross-origin frame (its document runs in another process; the SDK holds no CDP session for it)");
        throw new FrameNativeException("frame: no point of the element is proven to receive the press (covered)");
    }

    private static async Task<bool> FrameReachesAsync(IPage page, FrameTarget t, double x, double y)
    {
        var fw = FrameWorlds.For(page);
        if (await fw.HitAsync(x, y).ConfigureAwait(false) is not { } h || h.Frame != t.Fid) return false;
        return await fw.CallAsync(t.Fid, h.Node, IsolatedWorld.HitInBox, t.FBox).ConfigureAwait(false) is { ValueKind: JsonValueKind.True };
    }

    /// <summary>The press point in viewport coordinates (whole pixels): <paramref name="position"/> (from the padding
    /// box), else the persona's point when the hit test proves it, else the probe point that proved the element.</summary>
    private static async Task<(double X, double Y)> FramePointAsync(IPage page, FrameTarget t, (double X, double Y)? position = null)
    {
        var b = t.Box;
        if (position is { } pos)
            return (Math.Round(b.X + (t.Info?.Bl ?? 0) + pos.X), Math.Round(b.Y + (t.Info?.Bt ?? 0) + pos.Y));
        var st = StateFor(page);
        var from = st.Known ? new Point(st.X, st.Y) : new Point(b.X - 120, b.Y - 90);
        var pt = Motion.ClickPoint(new Box(b.X, b.Y, b.Width, b.Height), from, PersonaFor(page));
        double x = Math.Round(pt.X), y = Math.Round(pt.Y);
        if (await FrameReachesAsync(page, t, x, y).ConfigureAwait(false)) return (x, y);
        return (Math.Round(b.X + b.Width * t.Probe.Fx), Math.Round(b.Y + b.Height * t.Probe.Fy));
    }

    private static async Task<bool> FrameFocusedAsync(IPage page, FrameTarget t)
        => await FrameWorlds.For(page).CallAsync(t.Fid, t.Nid, FrameFocused).ConfigureAwait(false) is { ValueKind: JsonValueKind.True };

    private static async Task<FieldInfo?> FrameDescribeAsync(IPage page, FrameTarget t, string want)
        => await FrameWorlds.For(page).CallAsync(t.Fid, t.Nid, IsolatedWorld.Describe, new Dictionary<string, object?> { ["want"] = want })
            .ConfigureAwait(false) is { } el ? FieldInfo.From(el, t.Nid) : null;

    private static async Task<string?> FrameValueAsync(IPage page, FrameTarget t)
        => (await FrameDescribeAsync(page, t, IsolatedWorld.FormControl).ConfigureAwait(false))?.Value;

    /// <summary>Glide to (x, y) and press there as <paramref name="o"/> asks (the page's humanized pointer).</summary>
    private static async Task FrameGlidePressAsync(IPage page, FrameTarget t, double x, double y, NormalizedClick o, bool moveOff)
    {
        var p = PersonaFor(page);
        var st = StateFor(page);
        await GlideAsync(page, p, st, x, y, AimW(t.Box)).ConfigureAwait(false);
        await PressAtAsync(page, p, o).ConfigureAwait(false);
        if (moveOff) ScheduleMoveOff(page, p, st);
    }

    private static async Task FrameClickIntoAsync(IPage page, FrameTarget t)
    {
        if (await FrameFocusedAsync(page, t).ConfigureAwait(false)) return;
        var (x, y) = await FramePointAsync(page, t).ConfigureAwait(false);
        await FrameGlidePressAsync(page, t, x, y, new HumanClickOptions().Normalize(), moveOff: false).ConfigureAwait(false);
        await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);
        if (!await FrameFocusedAsync(page, t).ConfigureAwait(false))
            throw new FrameNativeException("frame: the click did not put the caret in the field");
    }

    private static async Task<FrameTarget> FrameTextTargetAsync(ILocator loc, float? timeout)
    {
        var t = await FrameLocateAsync(loc, timeout, IsolatedWorld.FormControl).ConfigureAwait(false);
        var i = t.Info!;
        if (Forms.ClassifyField(i.Tag, i.Type, i.Ce) != "text" || i.Disabled || i.ReadOnly)
            throw new FrameNativeException("frame: not a text field (Playwright's own call)");
        return t;
    }

    // ------------------------------------------------------------------ the actions
    private static async Task FrameDoClickAsync(ILocator loc, HumanClickOptions options, NormalizedClick o, bool noClick)
    {
        if (o.Force || o.Trial || o.Modifiers.Count > 0)
            throw new FrameNativeException("frame: force/trial/modifiers are Playwright's own inside frames");
        var page = loc.Page;
        var t = await FrameLocateAsync(loc, options.Timeout, null).ConfigureAwait(false);
        var (x, y) = await FramePointAsync(page, t, o.Position).ConfigureAwait(false);
        if (noClick)
        {
            var p = PersonaFor(page);
            var st = StateFor(page);
            await GlideAsync(page, p, st, x, y, AimW(t.Box)).ConfigureAwait(false);
            await DwellAsync(page, p, st, x, y, t.Box, options.Dwell).ConfigureAwait(false);   // the hand rests on what it hovered
        }
        else await FrameGlidePressAsync(page, t, x, y, o, moveOff: true).ConfigureAwait(false);
    }

    private static async Task FrameDoFillAsync(ILocator loc, string value, float? timeout, bool force)
    {
        if (force) throw new FrameNativeException("frame: fill(force / non-string) is Playwright's own");
        var page = loc.Page;
        var p = PersonaFor(page);
        var t = await FrameTextTargetAsync(loc, timeout).ConfigureAwait(false);
        if (t.Info!.Value == value) return;
        await FrameClickIntoAsync(page, t).ConfigureAwait(false);
        if (!string.IsNullOrEmpty(await FrameValueAsync(page, t).ConfigureAwait(false)))
        {
            await SelectAllAsync(page, p).ConfigureAwait(false);
            await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
            await KeyAsync(page, p, "Backspace").ConfigureAwait(false);
            await Task.Delay((int)Rand(60, 160)).ConfigureAwait(false);
            if (!string.IsNullOrEmpty(await FrameValueAsync(page, t).ConfigureAwait(false)))
                throw new FrameNativeException("frame: the field would not clear");
        }
        if (value.Length > 0)
        {
            if (value.EnumerateRunes().Count() > 200)
            {
                // bulk values are pasted, as on the main-frame route (typing thousands of keys would crawl)
                await Task.Delay((int)Rand(120, 320)).ConfigureAwait(false);
                await page.Keyboard.InsertTextAsync(value).ConfigureAwait(false);
            }
            else await TypeTextAsync(page, p, value, null, typos: true).ConfigureAwait(false);
        }
        var got = await FrameValueAsync(page, t).ConfigureAwait(false);
        if (got != value && !Forms.FormattedEqual(got, value))
            throw new FrameNativeException("frame: the typed value did not land; Playwright's fill corrected it");
    }

    private static async Task FrameDoPressAsync(ILocator loc, string key, int repeat, float? delay)
    {
        var page = loc.Page;
        var t = await FrameLocateAsync(loc, null, null).ConfigureAwait(false);
        var info = await FrameDescribeAsync(page, t, IsolatedWorld.FormControl).ConfigureAwait(false);
        if (info is not null && Forms.ClassifyField(info.Tag, info.Type, info.Ce) == "text" && SameRect(info.Rect, t.FBox))
        {
            t.Info = info;
            await FrameClickIntoAsync(page, t).ConfigureAwait(false);
        }
        else if (!await FrameFocusedAsync(page, t).ConfigureAwait(false))
        {
            await loc.FocusAsync().ConfigureAwait(false);   // a button / link: focused without a click, as Playwright does
            await Task.Delay((int)Rand(40, 120)).ConfigureAwait(false);
        }
        await PressFocusedAsync(page, key, repeat, delay).ConfigureAwait(false);
    }

    private static async Task<IReadOnlyList<string>> FrameDoSelectAsync(ILocator loc, List<SelectWant> wants, float? timeout, bool force)
    {
        if (force) throw new FrameNativeException("frame: force is Playwright's own");
        if (wants.Count == 0) throw new FrameNativeException("frame: nothing to select by value/label/index");
        var page = loc.Page;
        var p = PersonaFor(page);
        var dl = SelectDeadline.For(page, timeout);
        var t = await FrameLocateAsync(loc, timeout, "select").ConfigureAwait(false);
        var info = t.Info!;
        if (info.Tag != "select" || info.Disabled) throw new FrameNativeException("frame: not an enabled select");
        if (info.Multiple || info.Size > 1) throw new FrameNativeException("frame: list boxes inside frames are Playwright's own");
        var opts = info.Options;
        var idx = new List<int>();
        foreach (var w in wants)
        {
            int i = MatchWant(opts, w);
            if (i < 0 || opts[i].Disabled) throw new FrameNativeException("frame: option missing or disabled (Playwright waits / reports it)");
            idx.Add(i);
        }
        int tgt = idx.Min();   // Playwright: the FIRST option (document order) matching any wanted one
        if (info.SelectedIndex == tgt) return new[] { opts[tgt].Value ?? "" };
        var labels = opts.Select(o => o.Disabled ? null : (o.Label ?? "")).ToList();
        var plan = Forms.PlanSelect(labels, info.SelectedIndex, tgt);
        if (plan is null || Math.Abs(plan.Arrows) > FrameArrowHold) throw new FrameNativeException("frame: no short key route to the option");
        var (x, y) = await FramePointAsync(page, t).ConfigureAwait(false);
        // opens the drop-down (the browser's own popup); this sequence owns the pointer until the pick lands
        await FrameGlidePressAsync(page, t, x, y, new HumanClickOptions().Normalize(), moveOff: false).ConfigureAwait(false);
        await Task.Delay((int)Motion.PauseMs(p, 220, 480)).ConfigureAwait(false);   // the list opens; the eye finds the option
        double tempo = Motion.TimingTraitsOf(p).PauseScale;
        foreach (var ch in plan.Keys)
        {
            await page.Keyboard.PressAsync(ch.ToString(), new KeyboardPressOptions { Delay = (float)Motion.KeyDwell(p) }).ConfigureAwait(false);
            await Task.Delay((int)Math.Max(40, Gauss(100, 35) * tempo)).ConfigureAwait(false);   // inside type-ahead's 1 s window
        }
        if (plan.Arrows != 0)
        {
            await Task.Delay((int)Motion.PauseMs(p, 120, 300)).ConfigureAwait(false);
            await ArrowRunAsync(page, p, plan.Arrows > 0 ? "ArrowDown" : "ArrowUp", Math.Abs(plan.Arrows)).ConfigureAwait(false);
        }
        await Task.Delay((int)Motion.PauseMs(p, 150, 400)).ConfigureAwait(false);
        // commits: ONE trusted input + change; a document that is navigating away cannot answer the read-back
        if (await CommitSelectAsync(page, p, loc.ToString() ?? "the select", dl).ConfigureAwait(false))
            return new[] { opts[tgt].Value ?? "" };
        var again = await FrameDescribeAsync(page, t, "select").ConfigureAwait(false);
        if (again?.SelectedIndex == tgt) return new[] { opts[tgt].Value ?? "" };
        throw new FrameNativeException("frame: the drop-down did not commit the option");
    }

    private static async Task FrameDoSetCheckedAsync(ILocator loc, bool want, float? timeout, bool force, Position? position, bool trial)
    {
        if (force || trial) throw new FrameNativeException("frame: force/trial are Playwright's own inside frames");
        if (await loc.IsCheckedAsync(new LocatorIsCheckedOptions { Timeout = timeout }).ConfigureAwait(false) == want) return;
        var page = loc.Page;
        var t = await FrameLocateAsync(loc, timeout, "input").ConfigureAwait(false);
        if (t.Info!.Type is not ("checkbox" or "radio") || t.Info.Disabled)
            throw new FrameNativeException("frame: not an enabled checkbox / radio");
        var (x, y) = await FramePointAsync(page, t, position is { } pos ? (pos.X, pos.Y) : null).ConfigureAwait(false);
        await FrameGlidePressAsync(page, t, x, y, new HumanClickOptions().Normalize(), moveOff: true).ConfigureAwait(false);
        if (await loc.IsCheckedAsync().ConfigureAwait(false) == want) return;
        throw new PlaywrightException("Clicking the checkbox did not change its state");
    }
}
