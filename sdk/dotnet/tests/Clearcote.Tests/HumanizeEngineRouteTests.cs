using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// The engine route (EngineRoute.cs) on a DispatchProxy page whose context and browser hand out fake CDP sessions -- a
/// port of sdk/node/test/humanize-engine-command.test.ts, sdk/node/test/humanize-engine-route.test.ts and
/// sdk/python/tests/test_humanize_engine_route.py (with the _R43Session / _R42Session fakes of
/// test_humanize_forms_async.py).
///
/// Measured on r38: the engine (Browser.dispatchPointerPath / humanizedClick) walks from ITS OWN last position on the
/// page, which an SDK glide does not update -- an engine click, an SDK glide to (800,550), then a click at (400,200):
/// the engine's first sample was at the previous engine click, a 559 px teleport. And it answers only after the walk
/// and the click, so sleeping a full `duration` afterwards added an idle second walk's worth of time to every click.
/// </summary>
public class HumanizeEngineRouteTests
{
    private const string PointerPath = "Browser.dispatchPointerPath";
    private const string Legacy = "Browser.humanizedClick";
    private const string KeySequence = "Browser.dispatchKeySequence";

    private static Task<JsonElement?> Json(string json) => Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());

    // ----------------------------------------------------------------------------------------------- fakes
    /// <summary>A CDP session that rejects the methods <see cref="Reject"/> names and answers the rest with {}.</summary>
    public class FnSession : DispatchProxy
    {
        public Func<string, Exception?> Reject { get; set; } = _ => null;
        public List<(string Method, Dictionary<string, object>? Args)> Calls { get; } = new();
        public List<string> Names => Calls.Select(c => c.Method).ToList();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            var name = (string)args![0]!;
            var a = args.Length > 1 ? args[1] as Dictionary<string, object> : null;
            lock (Calls) Calls.Add((name, a));
            var e = Reject(name);
            return e is not null ? Task.FromException<JsonElement?>(e) : Json("{}");
        }
    }

    /// <summary>
    /// The browser-level session of an engine. Kind: "r42" (the pointer path answers, dispatchKeySequence is not
    /// found), "r43" (an EMPTY dispatchKeySequence is invalid params, -32602), "r33" (only the pre-r34 name), "none"
    /// (neither name), "invalid-target" (a proxy whose probe fails with other wording). The pointer path answers after
    /// the walk (+120 ms press/release), like r38.
    /// </summary>
    public class Engine : DispatchProxy
    {
        public string Kind { get; set; } = "r42";
        public List<string>? Order { get; set; }
        public List<Dictionary<string, object>> Paths { get; } = new();
        public List<(string Method, Dictionary<string, object>? Args)> Calls { get; } = new();
        public List<Dictionary<string, object>> Keys { get; } = new();
        public int Probes { get; private set; }
        public List<string> Names => Calls.Select(c => c.Method).ToList();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            return SendAsync((string)args![0]!, args.Length > 1 ? args[1] as Dictionary<string, object> : null);
        }

        private async Task<JsonElement?> SendAsync(string m, Dictionary<string, object>? a)
        {
            lock (Calls) Calls.Add((m, a));
            Order?.Add(m);
            if (m == KeySequence)
            {
                bool empty = a?["keys"] is System.Collections.ICollection c && c.Count == 0;
                if (Kind == "r43" && !empty) { Keys.Add(a!); return null; }
                if (empty) Probes++;
                throw Kind switch
                {
                    "r43" => new PlaywrightException($"Protocol error ({KeySequence}): Invalid parameters"),
                    "invalid-target" => new PlaywrightException($"Protocol error ({KeySequence}): Invalid target"),
                    _ => new PlaywrightException($"Protocol error ({KeySequence}): '{KeySequence}' wasn't found"),
                };
            }
            if (m == PointerPath && Kind is "r33" or "none" || m == Legacy && Kind == "none")
                throw new PlaywrightException($"Protocol error ({m}): '{m}' wasn't found");
            if (m is PointerPath or Legacy)
            {
                Paths.Add(a!);
                await Task.Delay((int)(Convert.ToDouble(a!["duration"]) * 1000 + 120));
            }
            return null;
        }
    }

    /// <summary>The page's own CDP session: the target id, the isolated world and the hit test (true everywhere);
    /// <see cref="FrameDelayMs"/> > 0 holds the frame wait that long and logs "frames" in <see cref="Order"/>.</summary>
    public class Target : AfterActionTests.Cdp
    {
        public List<string>? Order { get; set; }
        public int FrameDelayMs { get; set; }
        public bool? AwaitPromise { get; private set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "SendAsync")
            {
                var name = (string)args![0]!;
                var a = args.Length > 1 ? args[1] as Dictionary<string, object> : null;
                if (name == "Target.getTargetInfo") return Json("{\"targetInfo\":{\"targetId\":\"T1\"}}");
                if (name == "Runtime.evaluate" && a?["expression"] is string e && e.Contains("requestAnimationFrame", StringComparison.Ordinal))
                    return FramesAsync(a);
            }
            return base.Invoke(method, args);
        }

        private async Task<JsonElement?> FramesAsync(Dictionary<string, object> a)
        {
            AwaitPromise = a.TryGetValue("awaitPromise", out var ap) && ap is true;
            if (FrameDelayMs > 0) await Task.Delay(FrameDelayMs);
            Order?.Add("frames");
            return JsonDocument.Parse("{\"result\":{\"value\":true}}").RootElement.Clone();
        }
    }

    public class Browser : DispatchProxy
    {
        public object? Engine { get; set; }
        protected override object? Invoke(MethodInfo? method, object?[]? args)
            => method?.Name == "NewBrowserCDPSessionAsync" ? Task.FromResult((ICDPSession)Engine!) : null;
    }

    public class Context : DispatchProxy
    {
        public object? Cdp { get; set; }
        public object? Browser { get; set; }
        protected override object? Invoke(MethodInfo? method, object?[]? args) => method?.Name switch
        {
            "NewCDPSessionAsync" => Task.FromResult((ICDPSession)Cdp!),
            "get_Browser" => Browser,
            _ => null,
        };
    }

    /// <summary>A locator Playwright's own click is the only way for: it has no box.</summary>
    public class NoBoxLoc : AfterActionTests.Loc
    {
        protected override object? Invoke(MethodInfo? method, object?[]? args)
            => method?.Name == "BoundingBoxAsync" ? Task.FromResult<LocatorBoundingBoxResult?>(null) : base.Invoke(method, args);
    }

    private sealed record Fake(IPage Page, AfterActionTests.Mouse Mouse, HumanizeShiftTests.Recorder Kb, Target Target,
        AfterActionTests.Loc Loc, List<string> Order)
    {
        public int Downs => Mouse.Log.Count(e => e.Op == "down");
        public List<(string Op, double T, float X, float Y)> Moves => Mouse.Log.Where(e => e.Op == "move").ToList();
        public List<string> KbOf(string op) => Kb.Log.Where(e => e.Op == op).Select(e => e.Arg).ToList();
    }

    /// <summary>A fake page of a browser whose browser-level CDP session is <paramref name="engine"/> (an
    /// <see cref="Engine"/> or a <see cref="FnSession"/>); pass the same <paramref name="browser"/> for a second page of
    /// that browser.</summary>
    private static Fake Make(object engine, HumanizeOptions? options = null, IBrowser? browser = null, bool noBox = false,
        double typoRate = 0, double? roll = 0)
    {
        var order = new List<string>();
        if (engine is Engine en) en.Order = order;
        var target = DispatchProxy.Create<ICDPSession, Target>();
        ((Target)(object)target).Order = order;
        browser ??= MakeBrowser(engine);
        var ctx = DispatchProxy.Create<IBrowserContext, Context>();
        ((Context)(object)ctx).Cdp = target;
        ((Context)(object)ctx).Browser = browser;
        var mouse = DispatchProxy.Create<IMouse, AfterActionTests.Mouse>();
        var kb = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, AfterActionTests.Page>();
        var loc = noBox ? DispatchProxy.Create<ILocator, NoBoxLoc>() : DispatchProxy.Create<ILocator, AfterActionTests.Loc>();
        var pp = (AfterActionTests.Page)(object)page;
        pp.Context = ctx; pp.Mouse = mouse; pp.Keyboard = kb; pp.Loc = loc;
        ((AfterActionTests.Loc)(object)loc).Page = page;
        Humanize.Attach(page, options ?? new HumanizeOptions(), "route");
        Humanize.TypoRateOverride.AddOrUpdate(page, new StrongBox<double>(typoRate));
        if (roll is { } r) Humanize.RolloverOverride.AddOrUpdate(page, new StrongBox<double>(r));
        return new Fake(page, (AfterActionTests.Mouse)(object)mouse, (HumanizeShiftTests.Recorder)(object)kb,
            (Target)(object)target, (AfterActionTests.Loc)(object)loc, order);
    }

    private static IBrowser MakeBrowser(object engine)
    {
        var b = DispatchProxy.Create<IBrowser, Browser>();
        ((Browser)(object)b).Engine = engine;
        return b;
    }

    private static Engine NewEngine(string kind)
    {
        var e = DispatchProxy.Create<ICDPSession, Engine>();
        ((Engine)(object)e).Kind = kind;
        return (Engine)(object)e;
    }

    private static FnSession NewFn(Func<string, Exception?> reject)
    {
        var s = DispatchProxy.Create<ICDPSession, FnSession>();
        ((FnSession)(object)s).Reject = reject;
        return (FnSession)(object)s;
    }

    // ------------------------------------------------------------------- SendEngineCommandAsync (the -32601 fallback)
    // r34 renamed Browser.humanizedClick to Browser.dispatchPointerPath. The new name goes first; the old one is tried
    // only on a method-not-found, and the answer is remembered per browser.
    private static readonly Dictionary<string, object> Params = new()
    {
        ["targetId"] = "T1", ["x"] = 10.0, ["y"] = 20.0, ["duration"] = 0.3, ["noClick"] = true,
    };
    private static Exception NotFoundRaw() => new PlaywrightException($"'{PointerPath}' wasn't found (-32601)");
    private static Exception NotFoundPw() => new PlaywrightException($"Protocol error ({PointerPath}): '{PointerPath}' wasn't found");

    [Fact]
    public async Task An_r34_engine_answers_the_new_name_and_the_old_one_is_never_sent()
    {
        var s = NewFn(_ => null);
        var key = new object();
        await Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params);
        await Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params);
        Assert.Equal(new[] { PointerPath, PointerPath }, s.Names);
        Assert.Same(Params, s.Calls[0].Args);
    }

    [Fact]
    public async Task An_older_engine_is_retried_once_under_the_old_name_which_is_then_used_directly()
    {
        foreach (var notFound in new Func<Exception>[] { NotFoundRaw, NotFoundPw })
        {
            var s = NewFn(m => m == PointerPath ? notFound() : null);
            var key = new object();
            await Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params);
            Assert.Equal(new[] { PointerPath, Legacy }, s.Names);
            Assert.Same(Params, s.Calls[1].Args);
            await Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params);
            Assert.Equal(new[] { PointerPath, Legacy, Legacy }, s.Names);
        }
    }

    [Fact]
    public async Task Any_other_error_propagates_with_no_fallback_and_nothing_cached()
    {
        var boom = new PlaywrightException($"Protocol error ({PointerPath}): Invalid parameters");
        var s = NewFn(_ => boom);
        var key = new object();
        Assert.Same(boom, await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params)));
        Assert.Equal(new[] { PointerPath }, s.Names);
        Assert.Same(boom, await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params)));
        Assert.Equal(new[] { PointerPath, PointerPath }, s.Names);
    }

    [Fact]
    public async Task An_engine_with_neither_name_is_asked_once_per_browser_then_refused_without_a_round_trip()
    {
        var notFound = NotFoundPw();
        var s = NewFn(_ => notFound);
        var key = new object();
        Assert.Same(notFound, await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params)));
        Assert.Equal(new[] { PointerPath, Legacy }, s.Names);
        Assert.True(Humanize.EngineHasNoPointerPath(key));
        var again = await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params));
        Assert.True(Humanize.IsMethodNotFound(again));
        Assert.Equal(new[] { PointerPath, Legacy }, s.Names);
        Assert.False(Humanize.EngineHasNoPointerPath(new object()));   // another browser is asked afresh
    }

    [Fact]
    public async Task An_old_name_that_also_fails_propagates_its_error_and_caches_nothing()
    {
        var legacyErr = new PlaywrightException("Protocol error (Browser.humanizedClick): Target closed");
        var s = NewFn(m => m == PointerPath ? NotFoundRaw() : legacyErr);
        var key = new object();
        Assert.Same(legacyErr, await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params)));
        Assert.Same(legacyErr, await Assert.ThrowsAsync<PlaywrightException>(() => Humanize.SendEngineCommandAsync((ICDPSession)(object)s, key, PointerPath, Legacy, Params)));
        Assert.Equal(new[] { PointerPath, Legacy, PointerPath, Legacy }, s.Names);
    }

    [Fact]
    public void Invalid_params_is_minus_32602_and_nothing_else()
    {
        Assert.True(Humanize.IsInvalidParams(new PlaywrightException($"Protocol error ({KeySequence}): Invalid parameters")));
        Assert.True(Humanize.IsInvalidParams(new PlaywrightException("Protocol error: -32602")));
        Assert.False(Humanize.IsInvalidParams(new PlaywrightException($"Protocol error ({KeySequence}): Invalid target")));
        Assert.False(Humanize.IsInvalidParams(new PlaywrightException("Invalid session id")));
        Assert.False(Humanize.IsInvalidParams(new PlaywrightException($"Protocol error ({KeySequence}): '{KeySequence}' wasn't found")));
    }

    // ------------------------------------------------------------------------- coordinate clicks through the engine
    [Fact]
    public async Task An_r33_engine_misses_once_for_the_whole_browser_then_gets_the_old_name_straight_away()
    {
        var eng = NewEngine("r33");
        var browser = MakeBrowser(eng);
        var p1 = Make(eng, browser: browser);
        var p2 = Make(eng, browser: browser);
        await p1.Page.HumanClickAsync(300, 200);
        await p1.Page.HumanClickAsync(320, 240);
        await p2.Page.HumanClickAsync(100, 100);
        Assert.Equal(new[] { PointerPath, Legacy, Legacy, Legacy }, eng.Names);
        var first = eng.Calls[1].Args!;
        Assert.Equal("T1", first["targetId"]);
        Assert.Equal(300.0, first["x"]);
        Assert.Equal(200.0, first["y"]);
        Assert.False((bool)first["noClick"]);
        Assert.Equal(0, p1.Downs + p2.Downs);
    }

    [Fact]
    public async Task An_engine_with_neither_name_is_not_probed_again_by_the_second_page_of_the_browser()
    {
        var eng = NewEngine("none");
        var browser = MakeBrowser(eng);
        var p1 = Make(eng, browser: browser);
        var p2 = Make(eng, browser: browser);
        await p1.Page.HumanClickAsync(300, 200);
        await p2.Page.HumanClickAsync(100, 100);
        Assert.Equal(new[] { PointerPath, Legacy }, eng.Names);
        Assert.Equal(1, p1.Downs);
        Assert.Equal(1, p2.Downs);
    }

    // A fresh headless page drops what the engine walks before it has drawn frames (SC 216, r38 and r40). Waiting for
    // two animation frames first fixed 18 of 18. The wait runs in the isolated world, before the command is sent.
    [Fact]
    public async Task The_pointer_path_is_sent_only_after_the_frame_wait_answered()
    {
        var eng = NewEngine("r42");
        var f = Make(eng);
        f.Target.FrameDelayMs = 300;
        await f.Page.HumanClickAsync(300, 200);
        Assert.Equal(new[] { "frames", PointerPath }, f.Order);
        Assert.True(f.Target.AwaitPromise);
    }

    [Fact]
    public async Task Consecutive_coordinate_clicks_walk_in_the_engine()
    {
        var eng = NewEngine("r42");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 300);
        await f.Page.HumanClickAsync(400, 200);
        Assert.Equal(new[] { 300.0, 400.0 }, eng.Paths.Select(c => (double)c["x"]));
        Assert.Equal(0, f.Downs);
    }

    [Fact]
    public async Task After_an_SDK_glide_the_engine_does_not_walk_from_its_stale_position()
    {
        var eng = NewEngine("r42");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 300);         // engine: its cursor is now (300, 300)
        await f.Page.HumanAmbientMotionAsync(400);      // SDK planner moves; the engine does not see them
        int before = f.Moves.Count;
        await f.Page.HumanClickAsync(400, 200);
        Assert.Single(eng.Paths);
        var glide = f.Moves.Skip(before).ToList();
        Assert.True(glide.Count > 10, $"{glide.Count} moves");
        Assert.Equal((400f, 200f), (glide[^1].X, glide[^1].Y));
        Assert.Equal(1, f.Downs);
    }

    [Fact]
    public async Task After_a_native_fallback_click_the_engine_does_not_walk_from_its_stale_position()
    {
        // A selector click the SDK hands to Playwright moves the real pointer to the element; the engine does not see
        // it either (verifier A #6).
        var eng = NewEngine("r42");
        var f = Make(eng, noBox: true);
        await f.Page.HumanClickAsync(300, 300);         // engine: its cursor is now (300, 300)
        await f.Page.HumanClickAsync("#elsewhere");     // no box: Playwright's own click
        Assert.Equal(new[] { "click" }, f.Loc.Native);
        await f.Page.HumanClickAsync(400, 200);
        Assert.Single(eng.Paths);
        Assert.Equal((400f, 200f), (f.Moves[^1].X, f.Moves[^1].Y));
        Assert.Equal(1, f.Downs);
    }

    [Fact]
    public async Task An_engine_answer_after_its_walk_is_not_followed_by_a_second_wait()
    {
        var eng = NewEngine("r42");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 300);
        var sw = Stopwatch.StartNew();
        await f.Page.HumanClickAsync(310, 320);
        double spent = sw.Elapsed.TotalMilliseconds;
        double walk = (double)eng.Paths[1]["duration"] * 1000 + 120;
        Assert.True(spent >= walk - 5, $"{spent:F0} ms for a {walk:F0} ms walk");
        Assert.True(spent < walk + 150, $"{spent:F0} ms for a {walk:F0} ms walk");
    }

    [Fact]
    public async Task Engine_clicks_off_keeps_every_click_on_the_planner()
    {
        using (new Sandbox().Env(Humanize.EngineClicksEnv, "off"))
        {
            var eng = NewEngine("r42");
            var f = Make(eng);
            await f.Page.HumanClickAsync(300, 300);
            await f.Page.HumanClickAsync(400, 200);
            Assert.Empty(eng.Calls);
            Assert.Equal(2, f.Downs);
        }
        // the page option says the same without the environment
        var eng2 = NewEngine("r42");
        var g = Make(eng2, new HumanizeOptions { Engine = "off" });
        await g.Page.HumanClickAsync(300, 300);
        Assert.Empty(eng2.Calls);
        Assert.Equal(1, g.Downs);
    }

    [Fact]
    public void Engine_clicks_mode_values_match_Python_and_Node()
    {
        foreach (var (v, want) in new[] { ("", "coords"), ("0", "off"), ("off", "off"), ("False", "off"), ("all", "all"),
                     ("selector", "all"), ("coords", "coords"), ("whatever", "coords") })
        {
            using var _ = new Sandbox().Env(Humanize.EngineClicksEnv, v);
            Assert.Equal(want, Humanize.EngineClicksMode());
        }
        using (new Sandbox().Env(Humanize.EngineExtEnv, "no")) Assert.False(Humanize.EngineExtMode());
        using (new Sandbox().Env(Humanize.EngineExtEnv, null)) Assert.True(Humanize.EngineExtMode());
    }

    // ------------------------------------------------------------------------ r43 engine extensions (SC 232 lane H)
    // The SDK side of pro-r43-07 (dispatchPointerPath fromX/fromY, button, clickCount, modifiers, buttons, settle;
    // answers after its last event) and pro-r43-09 (Browser.dispatchKeySequence), feature-detected once per browser by
    // an EMPTY key sequence.
    [Fact]
    public async Task An_r43_engine_walks_right_double_and_modified_clicks_held_legs_and_slip_free_typed_runs()
    {
        var eng = NewEngine("r43");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 200);                                                  // plain, in sync: r42 route, no probe
        await f.Page.HumanClickAsync(300, 200, new HumanClickOptions { Button = MouseButton.Right });   // the probe, then the engine
        await f.Page.HumanClickAsync(310, 200, new HumanClickOptions { ClickCount = 2 });
        await f.Page.HumanClickAsync(320, 200, new HumanClickOptions { Modifiers = new[] { KeyboardModifier.Shift } });
        await f.Page.HumanClickAsync(300, 200, new HumanClickOptions { Delay = 250 });          // a caller-chosen hold stays SDK-side
        Assert.Equal(4, eng.Paths.Count);
        Assert.Equal(1, eng.Probes);
        Assert.False(eng.Paths[0].ContainsKey("fromX"));
        Assert.False(eng.Paths[0].ContainsKey("button"));
        Assert.False((bool)eng.Paths[0]["noClick"]);
        Assert.Equal("right", eng.Paths[1]["button"]);
        Assert.Equal(2, eng.Paths[2]["clickCount"]);
        Assert.Equal(8, eng.Paths[3]["modifiers"]);
        Assert.Equal(new[] { "Shift" }, f.KbOf("down"));   // the SDK presses the modifier key around the engine click
        Assert.Equal(new[] { "Shift" }, f.KbOf("up"));
        Assert.Equal(1, f.Downs);                          // only the held-delay click pressed natively
        // a held leg: dispatchPointerPath {buttons: 1, noClick}, starting where the SDK's own press left the pointer
        Assert.True(await Humanize.EngineHeldLegAsync(f.Page, 360, 240));
        var held = eng.Paths.Where(c => c.ContainsKey("buttons")).ToList();
        Assert.Single(held);
        Assert.Equal(1, held[0]["buttons"]);
        Assert.True((bool)held[0]["noClick"]);
        Assert.Equal(300.0, held[0]["fromX"]);
        Assert.Equal(200.0, held[0]["fromY"]);
        Assert.Equal(360.0, held[0]["x"]);
        Assert.Equal(240.0, held[0]["y"]);
        // a typed run with no slip to plan: ONE dispatchKeySequence, US codes, Shift as modifiers 8
        await f.Page.HumanTypeAsync("hello, World!");
        Assert.Single(eng.Keys);
        var keys = (List<Dictionary<string, object>>)eng.Keys[0]["keys"];
        Assert.Equal("hello, World!", string.Concat(keys.Select(k => (string)k["key"])));
        Assert.Equal("Comma", keys[5]["code"]);
        Assert.Equal(8, keys[7]["modifiers"]);
        Assert.False(keys[0].ContainsKey("modifiers"));
        Assert.Equal("Digit1", keys[12]["code"]);
        Assert.Equal(8, keys[12]["modifiers"]);
        Assert.Equal(0.0, keys[12]["gapMs"]);
        foreach (var k in keys) Assert.InRange((double)k["dwellMs"], 45, 120);
        foreach (var k in keys.Take(keys.Count - 1)) Assert.True((double)k["gapMs"] >= 25, $"gap {k["gapMs"]}");
        Assert.Empty(f.KbOf("press"));   // nothing typed per key
    }

    [Fact]
    public async Task A_pro_engine_without_the_extensions_is_reported_once_through_OnFallback_and_presses_stay_SDK_side()
    {
        var events = new List<HumanizeFallbackEvent>();
        var eng = NewEngine("r42");
        var f = Make(eng, new HumanizeOptions { Strict = true, OnFallback = events.Add });
        await f.Page.HumanClickAsync(300, 200);                                                  // the pointer path answers: a PRO engine
        await f.Page.HumanClickAsync(300, 200, new HumanClickOptions { Button = MouseButton.Right });   // the probe says no: reported once
        await f.Page.HumanClickAsync(310, 200, new HumanClickOptions { ClickCount = 2 });       // no second probe, no second report
        Assert.Single(eng.Paths);
        Assert.Equal(1, eng.Probes);
        Assert.Equal(new[] { new HumanizeFallbackEvent("engine_ext", Humanize.EngineExtMissing, null) }, events);
        Assert.Equal(3, f.Downs);
    }

    [Fact]
    public async Task Engine_ext_off_keeps_the_r42_route_on_an_r43_engine()
    {
        using var _ = new Sandbox().Env(Humanize.EngineExtEnv, "off");
        var eng = NewEngine("r43");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 200);
        await f.Page.HumanClickAsync(300, 200, new HumanClickOptions { Button = MouseButton.Right });
        await f.Page.HumanTypeAsync("hello");
        Assert.Single(eng.Paths);
        Assert.Equal(0, eng.Probes);
        Assert.Equal(1, f.Downs);
        Assert.Empty(eng.Keys);
    }

    // --------------------------------------------------------------------- fixes after verifier A (SC 232, R3-R5)
    private static List<string> TypedKeys(Fake f)
        => f.Kb.Log.Where(e => e.Op is "press" or "down" && e.Arg.Length == 1).Select(e => e.Arg).ToList();

    [Fact]
    public async Task R3a_engine_off_keeps_a_typed_run_per_key_on_an_r43_engine()
    {
        var eng = NewEngine("r43");
        var f = Make(eng, new HumanizeOptions { Engine = "off" });
        await f.Page.HumanTypeAsync("hello");
        Assert.Empty(eng.Keys);
        Assert.Equal(0, eng.Probes);
        Assert.Equal("hello".Select(c => c.ToString()), TypedKeys(f));
    }

    [Fact]
    public async Task R3b_a_run_the_persona_rolls_over_stays_per_key_and_a_run_with_none_goes_engine_side()
    {
        var eng = NewEngine("r43");
        var f = Make(eng, roll: 1);
        await f.Page.HumanTypeAsync("hello world");
        Assert.Empty(eng.Keys);   // a rolled run stays per key
        Assert.Equal("hello world".Select(c => c.ToString()), TypedKeys(f));
        Assert.True(f.KbOf("down").Count(k => k.Length == 1) >= 6);
        var eng2 = NewEngine("r43");
        var g = Make(eng2, roll: 0);
        await g.Page.HumanTypeAsync("hello world");
        Assert.Single(eng2.Keys);   // a run with no rollover: the engine
        Assert.Equal(11, ((List<Dictionary<string, object>>)eng2.Keys[0]["keys"]).Count);
    }

    [Fact]
    public async Task R3c_an_engine_typed_run_records_one_typed_trace_line_with_the_planned_times()
    {
        var dir = Directory.CreateTempSubdirectory("cc-r3c-");
        var file = Path.Combine(dir.FullName, "score.jsonl");
        try
        {
            using var _ = new Sandbox().Env(HumanizeTrace.ScoreEnv, "1").Env(HumanizeTrace.ScoreFileEnv, file).Env("CLEARCOTE_NO_WARN", "1");
            var eng = NewEngine("r43");
            var f = Make(eng);
            await f.Page.HumanClickAsync(300, 200);   // the pointer is placed: no ambient placement before the keys
            await f.Page.HumanTypeAsync("hello world");
            Assert.Single(eng.Keys);
            var lines = File.Exists(file) ? File.ReadAllLines(file).Where(l => l.Length > 0).Select(l => JsonDocument.Parse(l).RootElement.Clone()).ToList() : new();
            var typed = lines.Where(l => l.GetProperty("kind").GetString() == "typed").ToList();
            Assert.Single(typed);
            Assert.Equal(11, typed[0].GetProperty("n").GetInt32());
            var meta = typed[0].GetProperty("meta");
            Assert.Equal(11, meta.GetProperty("n").GetInt32());
            Assert.False(meta.GetProperty("typos").GetBoolean());
            Assert.Equal(0, meta.GetProperty("floor_ms").GetDouble());
            Assert.True(meta.GetProperty("engine").GetBoolean());
        }
        finally
        {
            dir.Delete(true);
        }
    }

    [Fact]
    public async Task R4_a_probe_that_fails_with_other_wording_than_32602_is_not_an_r43_engine()
    {
        var eng = NewEngine("invalid-target");
        var f = Make(eng);
        await f.Page.HumanClickAsync(300, 200);                                                  // plain and in sync: the r42 route
        await f.Page.HumanClickAsync(300, 200, new HumanClickOptions { Button = MouseButton.Right });   // probe: "Invalid target" -> SDK-side
        await f.Page.HumanAmbientMotionAsync(300);                                               // an SDK glide: out of sync now
        await f.Page.HumanClickAsync(520, 410);                                                  // declined (no fromX on a non-r43 engine)
        Assert.Equal(1, eng.Probes);
        Assert.Single(eng.Paths);
        Assert.False(eng.Paths[0].ContainsKey("fromX"));
        Assert.False(eng.Paths[0].ContainsKey("button"));
        Assert.Equal(2, f.Downs);
    }

    [Fact]
    public async Task R5_with_engine_all_selector_clicks_with_a_button_a_count_or_modifiers_take_the_r43_extensions()
    {
        var eng = NewEngine("r43");
        var f = Make(eng, new HumanizeOptions { Engine = "all" });
        await f.Page.HumanClickAsync("#b");
        await f.Page.HumanClickAsync("#b", new HumanClickOptions { Button = MouseButton.Right });
        await f.Page.HumanDblClickAsync("#b");
        await f.Page.HumanClickAsync("#b", new HumanClickOptions { Modifiers = new[] { KeyboardModifier.Shift } });
        Assert.Equal(4, eng.Paths.Count);
        Assert.False((bool)eng.Paths[0]["noClick"]);
        Assert.False(eng.Paths[0].ContainsKey("button"));
        Assert.Equal("right", eng.Paths[1]["button"]);
        Assert.Equal(2, eng.Paths[2]["clickCount"]);
        Assert.Equal(8, eng.Paths[3]["modifiers"]);
        Assert.Equal(0, f.Downs);
        Assert.Equal(new[] { "Shift" }, f.KbOf("down"));
        Assert.Equal(new[] { "Shift" }, f.KbOf("up"));
        Assert.Empty(f.Loc.Native);
    }

    [Fact]
    public async Task Selector_clicks_stay_on_the_planner_by_default()
    {
        var eng = NewEngine("r43");
        var f = Make(eng);
        await f.Page.HumanClickAsync("#b");
        Assert.Empty(eng.Paths);
        Assert.Equal(1, f.Downs);
    }

    // ------------------------------------------------------------------------------------- the pure planners
    [Fact]
    public void Engine_click_params_held_bits_and_key_plan_match_Node()
    {
        NormalizedClick N(string button = "left", int count = 1, double? delay = null, params string[] mods)
            => Forms.NormalizeClickOptions(button, count, delay, mods);
        Assert.Empty(Forms.EngineClickParams(N())!);
        Assert.Equal("right", Forms.EngineClickParams(N("right"))!["button"]);
        Assert.Equal(3, Forms.EngineClickParams(N(count: 3))!["clickCount"]);
        Assert.Null(Forms.EngineClickParams(N(count: 4)));
        Assert.Null(Forms.EngineClickParams(N(delay: 200)));
        Assert.Equal(1 | 2 | 8, Forms.EngineClickParams(N(mods: new[] { "Alt", "Control", "Shift" }))!["modifiers"]);
        Assert.False(Forms.EngineClickParams(N(mods: new[] { "ControlOrMeta" }))!.ContainsKey("modifiers"));
        Assert.Equal(1, Forms.EngineHeldBits(null));
        Assert.Equal(2, Forms.EngineHeldBits("right"));
        Assert.Equal(4, Forms.EngineHeldBits("middle"));
        Assert.Equal(1, Forms.EngineHeldBits("bogus"));

        var plan = Forms.EngineKeyPlan("Ab\n\t?", () => 70, (_, _) => 90, Humanize.NeedsShift)!;
        Assert.Equal(new[] { "A", "b", "Enter", "Tab", "?" }, plan.Select(k => (string)k["key"]));
        Assert.Equal(8, plan[0]["modifiers"]);
        Assert.False(plan[0].ContainsKey("code"));
        Assert.Equal("Enter", plan[2]["code"]);
        Assert.Equal("Slash", plan[4]["code"]);
        Assert.Equal(8, plan[4]["modifiers"]);
        Assert.Equal(90.0, plan[0]["gapMs"]);
        Assert.Equal(0.0, plan[4]["gapMs"]);
        Assert.Null(Forms.EngineKeyPlan("café", () => 70, (_, _) => 90, Humanize.NeedsShift));
        Assert.Null(Forms.EngineKeyPlan("a\U0001F600", () => 70, (_, _) => 90, Humanize.NeedsShift));
    }
}
