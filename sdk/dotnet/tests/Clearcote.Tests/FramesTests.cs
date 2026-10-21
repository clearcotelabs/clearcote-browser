using System.Reflection;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// The frame route (Frames.cs) on a DispatchProxy page: a locator inside a child frame is hit-tested in the
/// frame's own isolated world and pressed with the page's humanized pointer; a frame the page's CDP session cannot
/// see into (cross-origin) goes to Playwright, reported. Mirrors the frame cases of node/test/humanize-forms.test.ts
/// ("frame locators take the frame route") and the frame-tree arithmetic of node/src/frames.ts.
public class FramesTests
{
    private const float BX = 300, BY = 200, BW = 120, BH = 40;
    private const double OX = 18, OY = 260;   // where the child frame's viewport starts in the main viewport
    private const string FrameSel = "#fr >> internal:control=enter-frame >> #x";

    // A CDP session with a main frame MAIN and a child C1. The hit test answers a node in C1 (same-process frame)
    // or, with CrossOrigin, the <iframe> itself in MAIN (an out-of-process frame the session cannot see into).
    public class Cdp : DispatchProxy
    {
        public bool CrossOrigin { get; set; }
        public List<(string Method, Dictionary<string, object>? Args)> Calls { get; } = new();
        private int _ctx = 10;

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            var name = (string)args![0]!;
            var p = args.Length > 1 ? args[1] as Dictionary<string, object> : null;
            lock (Calls) Calls.Add((name, p));
            string V(string v) => "{\"result\":{\"value\":" + v + "}}";
            string json = name switch
            {
                "Page.getFrameTree" => "{\"frameTree\":{\"frame\":{\"id\":\"MAIN\"},\"childFrames\":[{\"frame\":{\"id\":\"C1\",\"parentId\":\"MAIN\"}}]}}",
                "Page.createIsolatedWorld" => $"{{\"executionContextId\":{++_ctx}}}",
                "DOM.getNodeForLocation" => CrossOrigin ? "{\"backendNodeId\":4,\"frameId\":\"MAIN\"}" : "{\"backendNodeId\":9,\"frameId\":\"C1\"}",
                "DOM.getFrameOwner" => "{\"backendNodeId\":4}",
                "DOM.resolveNode" => "{\"object\":{\"objectId\":\"o" + p!["backendNodeId"] + "\"}}",
                "Runtime.callFunctionOn" => Call((string)p!["functionDeclaration"]),
                "Runtime.releaseObject" => "{}",
                "Runtime.evaluate" => Eval((string)p!["expression"]),
                _ => "{}",
            };
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());

            string Call(string fn)
            {
                if (fn == Humanize.OwnerOrigin) return V($"[{OX},{OY}]");
                if (fn == Humanize.IsFrameOwner) return V(CrossOrigin ? "true" : "false");
                if (fn == IsolatedWorld.HitInBox) return V("true");
                if (fn == Humanize.FrameFocused) return V("true");
                return V("null");
            }

            string Eval(string e)
            {
                if (e.StartsWith($"({IsolatedWorld.Viewport})", StringComparison.Ordinal)) return V("[1280,800]");
                if (e.StartsWith($"({IsolatedWorld.Scroll})", StringComparison.Ordinal)) return V("[0,0]");
                if (e.StartsWith($"({IsolatedWorld.Platform})", StringComparison.Ordinal)) return V("\"Win32\"");
                return V("null");
            }
        }
    }

    // A locator whose ToString is Playwright's ("Locator@<selector>"): a FrameLocator's when the selector enters a frame.
    public class Loc : DispatchProxy
    {
        public IPage? Page { get; set; }
        public string Selector { get; set; } = FrameSel;
        public int Count { get; set; } = 1;
        public bool Checked { get; set; }
        public List<string> Native { get; } = new();

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            switch (method?.Name)
            {
                case "get_Page": return Page;
                case "ToString": return "Locator@" + Selector;
                case "CountAsync": return Task.FromResult(Count);
                case "IsCheckedAsync": return Task.FromResult(Checked);
                case "BoundingBoxAsync":
                    return Task.FromResult<LocatorBoundingBoxResult?>(new LocatorBoundingBoxResult { X = BX, Y = BY, Width = BW, Height = BH });
                case "ClickAsync": case "DblClickAsync": case "HoverAsync": case "FillAsync": case "CheckAsync": case "SetCheckedAsync":
                    Native.Add(method.Name);
                    return Task.CompletedTask;
            }
            if (method?.ReturnType == typeof(Task)) return Task.CompletedTask;
            return null;
        }

        public override string ToString() => "Locator@" + Selector;
    }

    private sealed record Fake(IPage Page, ILocator Locator, Loc Loc, AfterActionTests.Mouse Mouse, Cdp Cdp, List<HumanizeFallbackEvent> Events);

    private static Fake Make(bool crossOrigin = false, bool strict = false)
    {
        var cdp = DispatchProxy.Create<ICDPSession, Cdp>();
        ((Cdp)(object)cdp).CrossOrigin = crossOrigin;
        var ctx = DispatchProxy.Create<IBrowserContext, IsolatedWorldTests.FakeContext>();
        ((IsolatedWorldTests.FakeContext)(object)ctx).Cdp = cdp;
        var mouse = DispatchProxy.Create<IMouse, AfterActionTests.Mouse>();
        var kb = DispatchProxy.Create<IKeyboard, HumanizeShiftTests.Recorder>();
        var page = DispatchProxy.Create<IPage, AfterActionTests.Page>();
        var loc = DispatchProxy.Create<ILocator, Loc>();
        var pp = (AfterActionTests.Page)(object)page;
        pp.Context = ctx; pp.Mouse = mouse; pp.Keyboard = kb; pp.Loc = loc;
        ((Loc)(object)loc).Page = page;
        var events = new List<HumanizeFallbackEvent>();
        Humanize.Attach(page, new HumanizeOptions { OnFallback = events.Add, Strict = strict }, "frames-unit");
        return new Fake(page, loc, (Loc)(object)loc, (AfterActionTests.Mouse)(object)mouse, (Cdp)(object)cdp, events);
    }

    private static List<(string Op, double T, float X, float Y)> Ops(Fake f, string op)
    {
        lock (f.Mouse.Log) return f.Mouse.Log.Where(e => e.Op == op).ToList();
    }

    [Fact]
    public void Frame_tree_parents_and_child_frame_locators()
    {
        var tree = JsonDocument.Parse(@"{""frame"":{""id"":""MAIN""},""childFrames"":[
            {""frame"":{""id"":""C1""},""childFrames"":[{""frame"":{""id"":""C2""}}]},{""frame"":{""id"":""D1""}}]}").RootElement;
        var parents = FrameWorlds.Parents(tree);
        Assert.Null(parents["MAIN"]);
        Assert.Equal("MAIN", parents["C1"]);
        Assert.Equal("C1", parents["C2"]);
        Assert.Equal("MAIN", parents["D1"]);

        var f = Make();
        Assert.True(Humanize.InChildFrame(f.Locator));
        Assert.Equal("frame locators go to Playwright", Humanize.FrameWhy(f.Locator));
        f.Loc.Selector = "#plain";
        Assert.False(Humanize.InChildFrame(f.Locator));   // no readable _frame: a main-frame locator (Node frameLocatorOf)
        Assert.Null(Humanize.FrameWhy(f.Locator));
    }

    [Fact]
    public async Task A_frame_click_is_proven_in_the_frames_world_and_pressed_by_the_page_pointer()
    {
        var f = Make();
        await f.Locator.HumanClickAsync();
        Assert.Empty(f.Loc.Native);
        Assert.Empty(f.Events);
        Assert.Single(Ops(f, "down"));
        Assert.Single(Ops(f, "up"));
        var at = Ops(f, "move").Last(e => e.T <= Ops(f, "down")[0].T);
        Assert.InRange(at.X, BX, BX + BW);
        Assert.InRange(at.Y, BY, BY + BH);
        // the hit test ran in C1's own world, with the box moved into C1's viewport by its <iframe>'s content origin
        List<(string Method, Dictionary<string, object>? Args)> calls;
        lock (f.Cdp.Calls) calls = f.Cdp.Calls.ToList();
        Assert.Contains(calls, c => c.Method == "Page.createIsolatedWorld" && (string)c.Args!["frameId"] == "C1");
        Assert.Contains(calls, c => c.Method == "DOM.getFrameOwner" && (string)c.Args!["frameId"] == "C1");
        var hit = calls.First(c => c.Method == "Runtime.callFunctionOn" && (string)c.Args!["functionDeclaration"] == IsolatedWorld.HitInBox);
        var arg = (Dictionary<string, object?>)((object[])hit.Args!["arguments"])[0];
        var fbox = (Dictionary<string, object?>)arg["value"]!;
        Assert.Equal(BX - OX, (double)fbox["x"]!, 6);
        Assert.Equal(BY - OY, (double)fbox["y"]!, 6);
        Assert.Equal((double)BW, (double)fbox["width"]!, 6);
    }

    [Fact]
    public async Task A_cross_origin_frame_goes_to_Playwright_reported()
    {
        var f = Make(crossOrigin: true);
        await f.Locator.HumanClickAsync();
        Assert.Equal(new[] { "ClickAsync" }, f.Loc.Native);
        Assert.Empty(Ops(f, "down"));
        var e = Assert.Single(f.Events);
        Assert.Equal("HumanClickAsync", e.Api);
        Assert.Contains("cross-origin", e.Reason);
        Assert.Equal("Locator@" + FrameSel, e.Selector);
    }

    [Fact]
    public async Task Strict_mode_refuses_the_cross_origin_fallback_and_runs_nothing()
    {
        var f = Make(crossOrigin: true, strict: true);
        var e = await Assert.ThrowsAsync<HumanizeStrictException>(() => f.Locator.HumanFillAsync("x"));
        Assert.Equal("HumanFillAsync", e.Api);
        Assert.Contains("cross-origin", e.Reason);
        Assert.Empty(f.Loc.Native);
    }

    [Fact]
    public async Task Force_trial_and_modifiers_are_Playwrights_own_inside_frames()
    {
        var f = Make();
        await f.Locator.HumanClickAsync(new LocatorClickOptions { Force = true });
        await f.Locator.HumanClickAsync(new LocatorClickOptions { Modifiers = new[] { KeyboardModifier.Shift } });
        Assert.Equal(new[] { "ClickAsync", "ClickAsync" }, f.Loc.Native);
        Assert.All(f.Events, e => Assert.Contains("force/trial/modifiers", e.Reason));
        Assert.Empty(Ops(f, "down"));
    }

    [Fact]
    public async Task More_than_one_match_goes_to_Playwright_unreported()
    {
        // Playwright raises its own strict-mode error there; the frame route reports nothing
        var f = Make();
        f.Loc.Count = 2;
        await f.Locator.HumanClickAsync();
        Assert.Equal(new[] { "ClickAsync" }, f.Loc.Native);
        Assert.Empty(f.Events);
    }

    [Fact]
    public async Task A_checkbox_in_the_state_asked_for_is_left_alone()
    {
        var f = Make();
        f.Loc.Checked = true;
        await f.Locator.HumanCheckAsync();
        Assert.Empty(f.Loc.Native);
        Assert.Empty(f.Events);
        Assert.Empty(Ops(f, "down"));
    }

    [Fact]
    public async Task A_frame_field_the_hit_test_cannot_describe_goes_to_Playwright_reported()
    {
        // Describe answers null here: the element is not proven to be a text field (node: "a frame the hit test
        // cannot prove goes to Playwright, reported")
        var f = Make();
        await f.Locator.HumanFillAsync("new");
        Assert.Equal(new[] { "FillAsync" }, f.Loc.Native);
        var e = Assert.Single(f.Events);
        Assert.Equal("HumanFillAsync", e.Api);
        Assert.StartsWith("frame:", e.Reason);
    }
}
