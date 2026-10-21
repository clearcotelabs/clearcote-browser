using System.Reflection;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// Context-level attach and the launch's Humanize / ShowCursor (Node installHumanize / installHumanizeOnContext,
/// Python install_humanize_on_context), the public pointer primitives (Node/Python humanized mouse.move / down / up /
/// dblclick) and the tap, against fakes. The live twins are in HumanizeAttachLiveTests.
public class HumanizeAttachTests
{
    // ------------------------------------------------------------------------------ fakes
    public class Ctx : DispatchProxy
    {
        public readonly List<IPage> PageList = new();
        public EventHandler<IPage>? PageEv;
        public int Handlers;
        public readonly List<string?> InitScripts = new();
        public void Open(IPage p) { PageList.Add(p); PageEv?.Invoke(this, p); }

        protected override object? Invoke(MethodInfo? m, object?[]? a)
        {
            switch (m!.Name)
            {
                case "get_Pages": return PageList.ToList();
                case "add_Page": PageEv += (EventHandler<IPage>)a![0]!; Handlers++; return null;
                case "remove_Page": return null;
                case "AddInitScriptAsync": InitScripts.Add((string?)a![0]); return Task.CompletedTask;
                case "NewCDPSessionAsync": return Task.FromException<ICDPSession>(new PlaywrightException("no cdp"));
            }
            return m.ReturnType == typeof(Task) ? Task.CompletedTask : null;
        }
    }

    public class Pg : DispatchProxy
    {
        public object? Context { get; set; }
        public readonly List<string> Evals = new();
        public EventHandler<IPage>? Load;

        protected override object? Invoke(MethodInfo? m, object?[]? a)
        {
            switch (m!.Name)
            {
                case "get_Context": return Context;
                case "add_Load": Load += (EventHandler<IPage>)a![0]!; return null;
                case "EvaluateAsync" when !m.IsGenericMethod:
                    lock (Evals) Evals.Add((string)a![0]!);
                    return Task.FromResult<JsonElement?>(null);
            }
            return m.ReturnType == typeof(Task) ? Task.CompletedTask : null;
        }
    }

    public class Br : DispatchProxy
    {
        public List<IBrowserContext> Ctxs = new();
        public Func<IPage>? MakePage;
        public Func<IBrowserContext>? MakeCtx;

        protected override object? Invoke(MethodInfo? m, object?[]? a) => m!.Name switch
        {
            "get_Contexts" => Ctxs.ToList(),
            "NewPageAsync" => Task.FromResult(MakePage!()),
            "NewContextAsync" => Task.FromResult(MakeCtx!()),
            "get_Version" => "fake",
            _ => throw new NotSupportedException(m.Name),
        };
    }

    private static (IBrowserContext Ctx, Ctx Fake) NewCtx()
    {
        var c = DispatchProxy.Create<IBrowserContext, Ctx>();
        return (c, (Ctx)(object)c);
    }

    private static (IPage Page, Pg Fake) NewPage(IBrowserContext? ctx = null)
    {
        var p = DispatchProxy.Create<IPage, Pg>();
        var f = (Pg)(object)p;
        f.Context = ctx;
        return (p, f);
    }

    private static uint SeedOf(object seed) => Motion.MakePersona(seed).Seed;

    // ------------------------------------------------------------------------------ context attach
    [Fact]
    public void Context_attach_covers_present_pages_and_every_popup_with_one_persona()
    {
        var (ctx, fc) = NewCtx();
        var (p0, _) = NewPage(ctx);
        fc.PageList.Add(p0);
        var opts = new HumanizeOptions { Preset = "quick" };
        Humanize.Attach(ctx, opts, "ctx-seed");
        Assert.Same(opts, Humanize.OptionsFor(p0));
        Assert.Equal(SeedOf("ctx-seed"), Humanize.PersonaFor(p0).Seed);
        var (popup, _) = NewPage(ctx);
        fc.Open(popup);                                   // window.open / target=_blank: the context's Page event
        Assert.Same(opts, Humanize.OptionsFor(popup));
        Assert.Equal(SeedOf("ctx-seed"), Humanize.PersonaFor(popup).Seed);
        // a second install adds no second handler, and a page keeps the persona it has
        Humanize.Attach(ctx, new HumanizeOptions(), "other");
        Assert.Equal(1, fc.Handlers);
        Assert.Same(opts, Humanize.OptionsFor(popup));
        var (mine, _) = NewPage(ctx);
        Humanize.Attach(mine, "mine");
        fc.Open(mine);
        Assert.Null(Humanize.OptionsFor(mine));
        Assert.Equal(SeedOf("mine"), Humanize.PersonaFor(mine).Seed);
        // no overlay was asked for
        Assert.Empty(fc.InitScripts);
    }

    [Fact]
    public void Context_attach_validates_its_options()
    {
        var (ctx, fc) = NewCtx();
        Assert.Throws<ArgumentException>(() => Humanize.Attach(ctx, new HumanizeOptions { Preset = "fast" }));
        Assert.Equal(0, fc.Handlers);
        Humanize.Attach(ctx, "seed-only");                // the seed overload: default options
        var (p, _) = NewPage(ctx);
        fc.Open(p);
        Assert.NotNull(Humanize.OptionsFor(p));
        Assert.Equal(SeedOf("seed-only"), Humanize.PersonaFor(p).Seed);
    }

    [Fact]
    public async Task Show_cursor_alone_injects_the_overlay_and_attaches_no_persona()
    {
        var (ctx, fc) = NewCtx();
        Humanize.Install(ctx, new HumanizeLaunch(null, true, null));
        Assert.Equal(new[] { Humanize.CursorOverlay }, fc.InitScripts);
        var (p, fp) = NewPage(ctx);
        fc.Open(p);
        await Task.Delay(50);
        Assert.Equal(new[] { Humanize.CursorOverlay }, fp.Evals);   // the page that is already loaded
        fp.Load?.Invoke(p, p);                                      // and again after every load
        await Task.Delay(50);
        Assert.Equal(2, fp.Evals.Count);
        Assert.Null(Humanize.OptionsFor(p));
        // humanize added later on the same context: the hook it has delivers both
        Humanize.Install(ctx, new HumanizeLaunch(new HumanizeOptions(), false, "later"));
        Assert.Equal(1, fc.Handlers);
        var (p2, fp2) = NewPage(ctx);
        fc.Open(p2);
        await Task.Delay(50);
        Assert.NotNull(Humanize.OptionsFor(p2));
        Assert.Single(fp2.Evals);
    }

    [Fact]
    public void Cursor_overlay_is_the_Node_source()
    {
        Assert.StartsWith("(() => {\n  if (window.__clearcoteCursor) return;", Humanize.CursorOverlay.Replace("\r\n", "\n"));
        var nodePath = Path.Combine(Path.GetDirectoryName(SharedTestData.PathOf("humanize_forms_vectors.json"))!,
            "..", "node", "src", "humanize.ts");
        if (!File.Exists(nodePath)) return;
        var ts = File.ReadAllText(nodePath).Replace("\r\n", "\n");
        const string head = "export const CURSOR_OVERLAY = `";
        int i = ts.IndexOf(head, StringComparison.Ordinal);
        Assert.True(i >= 0);
        int s = i + head.Length, e = ts.IndexOf('`', s);
        Assert.Equal(ts[s..e], Humanize.CursorOverlay.Replace("\r\n", "\n"));
    }

    // ------------------------------------------------------------------------------ launch options
    [Fact]
    public void Launch_setting_is_off_locally_and_on_in_the_cloud_unless_said()
    {
        Assert.False(HumanizeLaunch.Of(null, cloud: false).Active);
        Assert.False(HumanizeLaunch.Of(new LaunchOptions(), cloud: false).Active);
        Assert.NotNull(HumanizeLaunch.Of(new LaunchOptions(), cloud: true).Options);
        Assert.NotNull(HumanizeLaunch.Of(new LaunchOptions { Humanize = true }, cloud: false).Options);
        var o = new HumanizeOptions { Preset = "careful" };
        Assert.Same(o, HumanizeLaunch.Of(new LaunchOptions { HumanizeOptions = o }, cloud: false).Options);   // options turn it on
        Assert.Null(HumanizeLaunch.Of(new LaunchOptions { Humanize = false, HumanizeOptions = o }, cloud: true).Options);
        var cur = HumanizeLaunch.Of(new LaunchOptions { ShowCursor = true }, cloud: false);
        Assert.True(cur.Active && cur.ShowCursor && cur.Options is null);
        Assert.Equal("fp", HumanizeLaunch.Of(new LaunchOptions(), cloud: true, seed: "fp").Seed);
        // a bad option throws before anything starts, even when humanize is off
        Assert.Throws<ArgumentException>(() => HumanizeLaunch.Of(new LaunchOptions { Humanize = false, HumanizeOptions = new() { Engine = "x" } }, false));
    }

    [Fact]
    public async Task A_launched_browser_humanizes_its_contexts_new_pages_and_new_contexts()
    {
        var (c0, f0) = NewCtx();
        var br = DispatchProxy.Create<IBrowser, Br>();
        var fb = (Br)(object)br;
        fb.Ctxs.Add(c0);
        var (c1, f1) = NewCtx();
        var (pageOwnCtx, ownCtxFake) = NewCtx();
        fb.MakeCtx = () => c1;
        fb.MakePage = () => NewPage(pageOwnCtx).Page;
        var hz = new HumanizeLaunch(new HumanizeOptions(), false, "launch-seed");
        Assert.Same(br, new HumanizeLaunch(null, false, null).Wrap(br));   // nothing to install: unchanged
        var b = hz.Wrap(br);
        Assert.Equal(1, f0.Handlers);                     // a context it already had (connectOverCDP's own)
        var page = await b.NewPageAsync();
        Assert.NotNull(Humanize.OptionsFor(page));
        Assert.Equal(SeedOf("launch-seed"), Humanize.PersonaFor(page).Seed);
        Assert.Equal(1, ownCtxFake.Handlers);             // NewPageAsync's implicit context: its popups too
        var ctx = await b.NewContextAsync();
        Assert.Same(c1, ctx);
        Assert.Equal(1, f1.Handlers);
        var (pop, _) = NewPage(c1);
        f1.Open(pop);
        Assert.Equal(SeedOf("launch-seed"), Humanize.PersonaFor(pop).Seed);
        Assert.Equal("fake", b.Version);                  // everything else is the browser's own
    }

    [Fact]
    public void Payload_is_the_JSON_able_options_with_unset_values_left_out()
    {
        Assert.Equal("""{"preset":"default","strict":false,"engine":"auto"}""", new HumanizeOptions().ToPayload().ToJsonString());
        Assert.Equal("""{"preset":"quick","strict":true,"engine":"off","ambient":"idle","typoRate":0,"handedness":"left","deviceHz":500,"inputDevice":"trackpad"}""",
            new HumanizeOptions { Preset = "quick", Strict = true, Engine = "off", Ambient = "idle", TypoRate = 0, Handedness = "left",
                DeviceHz = 500, InputDevice = "trackpad", OnFallback = _ => { } }.ToPayload().ToJsonString());
    }

    [Fact]
    public void Docker_takes_the_humanize_options_on_this_side()
    {
        var env = DockerLaunch.ContainerEnv(new LaunchOptions { Humanize = true, ShowCursor = true, HumanizeOptions = new() });
        Assert.DoesNotContain(env.Keys, k => k.Contains("HUMAN", StringComparison.Ordinal) || k.Contains("CURSOR", StringComparison.Ordinal));
        Assert.DoesNotContain(DockerLaunch.RefusedOptions, p => p.Name is "Humanize" or "HumanizeOptions" or "ShowCursor");
    }

    // ------------------------------------------------------------------------------ pointer primitives
    public class FailingMouse : AfterActionTests.Mouse
    {
        public bool FailDown;
        protected override object? Invoke(MethodInfo? method, object?[]? args)
            => FailDown && method?.Name == "DownAsync" ? Task.FromException(new PlaywrightException("down refused")) : base.Invoke(method, args);
    }

    public class TapLoc : AfterActionTests.Loc
    {
        public int Taps;
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "TapAsync") { Taps++; return Task.CompletedTask; }
            return base.Invoke(method, args);
        }
    }

    private sealed record Fake(IPage Page, ILocator Loc, FailingMouse Mouse, IBrowserContext Ctx)
    {
        public List<(string Op, double T, float X, float Y)> Log { get { lock (Mouse.Log) return Mouse.Log.ToList(); } }
    }

    private static Fake Make(string seed, HumanizeOptions? options = null)
    {
        var cdp = DispatchProxy.Create<ICDPSession, AfterActionTests.Cdp>();
        var ctx = DispatchProxy.Create<IBrowserContext, IsolatedWorldTests.FakeContext>();
        var mouse = DispatchProxy.Create<IMouse, FailingMouse>();
        var kb = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, AfterActionTests.Page>();
        var loc = DispatchProxy.Create<ILocator, TapLoc>();
        ((IsolatedWorldTests.FakeContext)(object)ctx).Cdp = cdp;
        var pp = (AfterActionTests.Page)(object)page;
        pp.Context = ctx; pp.Mouse = mouse; pp.Keyboard = kb; pp.Loc = loc;
        ((TapLoc)(object)loc).Page = page;
        if (options is null) Humanize.Attach(page, seed); else Humanize.Attach(page, options, seed);
        return new Fake(page, loc, (FailingMouse)(object)mouse, ctx);
    }

    private static readonly MethodInfo StateFor = typeof(Humanize).GetMethod("StateFor", BindingFlags.NonPublic | BindingFlags.Static)!;
    private static bool Held(IPage page)
    {
        var st = StateFor.Invoke(null, new object[] { page })!;
        return (bool)st.GetType().GetField("Held")!.GetValue(st)!;
    }

    [Fact]
    public async Task Move_down_held_move_up_is_a_drag_on_its_line()
    {
        var f = Make("ptr-1");
        await f.Page.HumanMoveAsync(300, 200);
        var first = f.Log;
        Assert.All(first, e => Assert.Equal("move", e.Op));
        Assert.InRange(first[0].Y, 0, 4);                 // a fresh page: the path comes in over the top edge
        Assert.Equal((300f, 200f), (first[^1].X, first[^1].Y));
        int n0 = first.Count;
        await f.Page.HumanMouseDownAsync();
        var log = f.Log;
        // re-pinned to the tracked point, then the pre-press pause (40-250 ms), then the press
        Assert.Equal(("move", 300f, 200f), (log[n0].Op, log[n0].X, log[n0].Y));
        Assert.Equal("down", log[n0 + 1].Op);
        Assert.True(log[n0 + 1].T - log[n0].T >= 35, $"pre-press pause {log[n0 + 1].T - log[n0].T:F0} ms");
        Assert.True(Held(f.Page));
        int n1 = log.Count;
        await f.Page.HumanMoveAsync(600, 210);
        var leg = f.Log.Skip(n1).ToList();
        Assert.True(leg.Count >= 10, $"{leg.Count} held samples");
        Assert.All(leg, e => Assert.Equal("move", e.Op));
        Assert.Equal((600f, 210f), (leg[^1].X, leg[^1].Y));
        // planned by PlanDrag: on the line from grab to drop (an aimed reach's arc would bow 6-18 % of 300 px off it)
        double dx = 300, dy = 10, d = Math.Sqrt(dx * dx + dy * dy);
        double side = leg.Max(e => Math.Abs(((e.X - 300) * dy - (e.Y - 200) * dx) / d));
        Assert.True(side <= 9, $"the held leg strayed {side:F1} px off its line");
        await f.Page.HumanMouseUpAsync();
        Assert.Equal("up", f.Log[^1].Op);
        Assert.False(Held(f.Page));
    }

    [Fact]
    public async Task A_press_with_no_move_before_it_stays_instant_and_a_failed_press_is_not_held()
    {
        var f = Make("ptr-2");
        var sw = System.Diagnostics.Stopwatch.StartNew();
        await f.Page.HumanMouseDownAsync();
        Assert.True(sw.ElapsedMilliseconds < 35, $"{sw.ElapsedMilliseconds} ms");
        Assert.Equal(new[] { "down" }, f.Log.Select(e => e.Op));   // no invented target: nothing moved
        await f.Page.HumanMouseUpAsync();
        Assert.False(Held(f.Page));
        f.Mouse.FailDown = true;
        await f.Page.HumanMoveAsync(200, 200);
        await Assert.ThrowsAsync<PlaywrightException>(() => f.Page.HumanMouseDownAsync());
        Assert.False(Held(f.Page));                       // a later move is a glide, not a drag leg
    }

    [Fact]
    public async Task Coordinate_dblclick_is_a_glide_and_two_held_presses()
    {
        var f = Make("ptr-3");
        await f.Page.HumanDblClickAsync(400, 300);
        var log = f.Log;
        Assert.Equal(new[] { "down", "up", "down", "up" }, log.Where(e => e.Op != "move").Select(e => e.Op));
        var lastMove = log.Last(e => e.Op == "move");
        Assert.Equal((400f, 300f), (lastMove.X, lastMove.Y));
        var downs = log.Where(e => e.Op == "down").ToList();
        var ups = log.Where(e => e.Op == "up").ToList();
        Assert.True(ups[0].T - downs[0].T >= 20 && ups[1].T - downs[1].T >= 20, "each press is held");
        Assert.InRange(downs[1].T - ups[0].T, 40, 400);   // the persona's gap inside a double click
    }

    // ------------------------------------------------------------------------------ tap
    [Fact]
    public async Task Tap_without_touch_is_Playwrights_own_and_with_touch_a_reported_humanized_click()
    {
        var events = new List<HumanizeFallbackEvent>();
        var f = Make("tap-1", new HumanizeOptions { OnFallback = events.Add });
        var tl = (TapLoc)(object)f.Loc;
        Humanize.HasTouchOverride.AddOrUpdate(f.Ctx, new System.Runtime.CompilerServices.StrongBox<bool>(false));
        await f.Loc.HumanTapAsync();
        Assert.Equal(1, tl.Taps);                         // stock: Playwright raises there itself
        Assert.Empty(f.Log);
        Assert.Empty(events);
        Humanize.HasTouchOverride.AddOrUpdate(f.Ctx, new System.Runtime.CompilerServices.StrongBox<bool>(true));
        await f.Loc.HumanTapAsync();
        Assert.Equal(1, tl.Taps);
        Assert.Single(f.Log, e => e.Op == "down");
        var ev = Assert.Single(events, e => e.Api == "HumanTapAsync");
        Assert.Equal(Humanize.TapNotHumanized + ": sent as a humanized mouse click", ev.Reason);
        Assert.Equal("tap is not humanized for touch contexts yet", Humanize.TapNotHumanized);
    }

    [Fact]
    public async Task Tap_on_a_touch_context_in_strict_mode_throws_and_sends_nothing()
    {
        var f = Make("tap-2", HumanizeOptions.StrictMode);
        Humanize.HasTouchOverride.AddOrUpdate(f.Ctx, new System.Runtime.CompilerServices.StrongBox<bool>(true));
        var e = await Assert.ThrowsAsync<HumanizeStrictException>(() => f.Loc.HumanTapAsync());
        Assert.Equal("HumanTapAsync", e.Api);
        Assert.Empty(f.Log);
        Assert.Equal(0, ((TapLoc)(object)f.Loc).Taps);
    }

    [Fact]
    public void HasTouch_is_unknown_on_a_context_without_Playwrights_options()
    {
        var f = Make("tap-3");
        Assert.Null(Humanize.HasTouch(f.Page));
    }
}
