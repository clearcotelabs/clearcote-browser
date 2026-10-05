using System.Text.Json;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// ServeAsync headless geometry: the display, the window, and the fit over a raw CDP connection.
/// </summary>
/// <remarks>
/// ServeAsync hands out a raw CDP endpoint, so no Playwright context options reach its pages; geometry
/// is set browser-wide instead (the headless display + the real window). Untouched, a served headless
/// browser reports the 800x600 surface as its screen. These mirror the Node and Python SDKs' serve()
/// tests; the live tests at the bottom attach the way a user's client does.
/// </remarks>
public class ServeGeometryTests
{
    // ─────────────────────────────────────────────────────── the headless display
    [Fact]
    public void ALightStealthSeedGetsItsOwnRow_SoScreenAndDprStayAPair()
    {
        foreach (var seed in new[] { "a", "b", "c", "probe-1", "probe-7" })
        {
            var d = Geometry.HeadlessDisplay(seed, new[] { "--fingerprint-platform=windows" }, lightStealth: true);
            Assert.Equal(Fingerprint.LightStealthScreen(seed), d);
            if (d.Width == 1536) Assert.Equal(1.25, Fingerprint.LightStealthValues(seed).DevicePixelRatio);
        }
    }

    [Fact]
    public void LightStealthRowMatchesTheNodeAndPythonSdks()
    {
        // the same sha256 construction in every SDK (probe-1 -> the 4K row)
        Assert.Equal(new Geometry.Display(3840, 2160, 3840, 2120), Fingerprint.LightStealthScreen("probe-1"));
    }

    [Fact]
    public void ALightStealthRowOffWindowsHasNoTaskbar()
    {
        var d = Geometry.HeadlessDisplay("x", new[] { "--fingerprint-platform=linux" }, lightStealth: true);
        Assert.Equal(d.Height, d.AvailHeight);
    }

    // ─────────────────────────────────────────────────────── which switches
    [Fact]
    public void SetsTheDisplayAndWindowOriginWithoutAPersona()
    {
        var g = Geometry.ServedGeometry(new[] { "--fingerprint-platform=windows" }, "probe-1", lightStealth: true, headless: true)!;
        Assert.False(g.Persona);
        Assert.Equal(new[] { Geometry.ScreenInfoSwitch(Fingerprint.LightStealthScreen("probe-1")), "--window-position=0,0" }, g.Args);
    }

    [Fact]
    public void LeavesTheDisplayToAPersona()
    {
        var g = Geometry.ServedGeometry(new[] { "--fingerprint=seed" }, "seed", lightStealth: false, headless: true)!;
        Assert.True(g.Persona);
        Assert.Null(g.Display);
        Assert.Equal(new[] { "--window-position=0,0" }, g.Args);
    }

    [Fact]
    public void NeverPassesWindowSize_ItWouldForceEveryPopupToThatSize()
    {
        foreach (var args in new[] { Array.Empty<string>(), new[] { "--fingerprint=s" } })
            Assert.DoesNotContain(Geometry.ServedGeometry(args, null, false, true)!.Args, a => a.StartsWith("--window-size"));
    }

    [Theory]
    [InlineData("--window-size=1024,768")]
    [InlineData("--window-position=5,5")]
    [InlineData("--start-maximized")]
    [InlineData("--screen-info={1280x720}")]
    public void StaysOutOfTheWayOfACallersWindowOrDisplay(string flag) =>
        Assert.Null(Geometry.ServedGeometry(new[] { flag }, null, false, true));

    [Fact]
    public void StaysOutOfTheWayWhenHeadedOrForTheAndroidWindow()
    {
        Assert.Null(Geometry.ServedGeometry(Array.Empty<string>(), null, false, headless: false));
        // the SDK's own android --window-size counts: a phone persona sizes itself
        Assert.Null(Geometry.ServedGeometry(new[] { "--fingerprint=s", "--window-size=412,915" }, null, false, true));
    }

    [Theory]
    [InlineData(99, 900)]
    [InlineData(1440, 10001)]
    public void RejectsAWindowSizeOutOfRange(int w, int h) =>
        Assert.Throws<ArgumentException>(() => Geometry.ValidateWindowSize(new ViewportSize { Width = w, Height = h }));

    [Fact]
    public async Task ServeRejectsABadWindowSizeBeforeLaunchingAnything() =>
        await Assert.ThrowsAsync<ArgumentException>(() =>
            Clearcote.ServeAsync(new ServeOptions { WindowSize = new ViewportSize { Width = 50, Height = 50 }, Quiet = true }));

    // ─────────────────────────────────────────────────────── the window fit over CDP
    /// Models a served browser: the page reads its display, the window reports the bounds it got.
    /// <c>staleReads</c>: that many reads after each resize still report the size from before it, as a
    /// page does until the new window size reaches it (the read after that, the new size).
    private sealed class FakeBrowser : Geometry.ICdpSend
    {
        private readonly int[] _display;
        private readonly bool _updateScreen;
        private readonly int _heightBias;
        private readonly int _staleReads;
        private int[] _outer;
        private int[] _previous;
        private int _stale;
        public readonly List<(string Method, JsonElement Params, string? Session)> Calls = new();

        public FakeBrowser(int[] display, bool updateScreen = true, int heightBias = 0, int[]? initial = null, int staleReads = 0)
        {
            (_display, _updateScreen, _heightBias, _staleReads) = (display, updateScreen, heightBias, staleReads);
            _outer = _previous = initial ?? new[] { 780, 580 };
        }

        private int[] Outer()
        {
            if (_stale <= 0) return _outer;
            _stale--;
            return _previous;
        }

        private static JsonElement J(object o) => JsonSerializer.SerializeToElement(o);

        public Task<JsonElement> SendAsync(string method, object? parameters = null, string? sessionId = null)
        {
            var p = J(parameters ?? new Dictionary<string, object>());
            Calls.Add((method, p, sessionId));
            return Task.FromResult(method switch
            {
                "Target.getTargets" => J(new { targetInfos = new[] { new { targetId = "T1", type = "page" } } }),
                "Target.attachToTarget" => J(new { sessionId = "S1" }),
                "Runtime.evaluate" => J(new { result = new { value = p.GetProperty("expression").GetString()!.Contains("availWidth") ? _display : Outer() } }),
                "Emulation.getScreenInfos" => J(new { screenInfos = new[] { new { id = "2300000000", isPrimary = true } } }),
                "Emulation.updateScreen" when !_updateScreen => throw new Exception("'Emulation.updateScreen' wasn't found"),
                "Browser.getWindowForTarget" => J(new { windowId = 3 }),
                "Browser.setWindowBounds" => SetBounds(p.GetProperty("bounds")),
                _ => J(new { }),
            });
        }

        private JsonElement SetBounds(JsonElement b)
        {
            _previous = _outer;
            _outer = new[] { b.GetProperty("width").GetInt32(), b.GetProperty("height").GetInt32() - _heightBias };
            _stale = _staleReads;
            return J(new { });
        }

        public string[] Methods => Calls.Select(c => c.Method).ToArray();
        public JsonElement[] Of(string method) => Calls.Where(c => c.Method == method).Select(c => c.Params).ToArray();
        public static string Bounds(JsonElement p)
        {
            var b = p.GetProperty("bounds");
            return $"{b.GetProperty("left")},{b.GetProperty("top")} {b.GetProperty("width")}x{b.GetProperty("height")}";
        }
    }

    [Fact]
    public async Task MaximizesOntoTheWorkArea_AndLeavesARegime2DisplayAlone()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 });
        var r = await Geometry.FitWindowOverCdpAsync(f, persona: false);
        Assert.Equal(new Geometry.Display(1920, 1080, 1920, 1040), r!.Value.Display);
        Assert.Equal(new[] { 1920, 1040 }, r.Value.Outer);
        Assert.Equal(new[] { "0,0 1920x1040" }, f.Of("Browser.setWindowBounds").Select(FakeBrowser.Bounds));
        Assert.DoesNotContain("Emulation.updateScreen", f.Methods);
        Assert.DoesNotContain("Runtime.enable", f.Methods);
        Assert.All(f.Calls.Where(c => c.Method == "Runtime.evaluate"), c => Assert.Equal("S1", c.Session));
        Assert.Equal("Target.detachFromTarget", f.Methods[^1]);
    }

    [Fact]
    public async Task MakesTheHeadlessDisplayThePersonasOwn_BeforeSizingTheWindow()
    {
        var f = new FakeBrowser(new[] { 1536, 864, 0, 0, 1536, 824 });
        await Geometry.FitWindowOverCdpAsync(f, persona: true);
        var u = Assert.Single(f.Of("Emulation.updateScreen"));
        Assert.Equal("2300000000", u.GetProperty("screenId").GetString());
        Assert.Equal((1536, 864), (u.GetProperty("width").GetInt32(), u.GetProperty("height").GetInt32()));
        Assert.Equal(40, u.GetProperty("workAreaInsets").GetProperty("bottom").GetInt32());
        Assert.True(Array.IndexOf(f.Methods, "Emulation.updateScreen") < Array.IndexOf(f.Methods, "Browser.setWindowBounds"));
    }

    [Fact]
    public async Task LeavesTheWindowAloneWhenTheEngineCannotResizeAPersonasDisplay()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 }, updateScreen: false);
        Assert.Null(await Geometry.FitWindowOverCdpAsync(f, persona: true));
        Assert.DoesNotContain("Browser.setWindowBounds", f.Methods);
        Assert.Equal("Target.detachFromTarget", f.Methods[^1]);
    }

    [Fact]
    public async Task HonoursAWindowSizeInsideTheWorkArea_AndClampsOnePastIt()
    {
        var size = new ViewportSize { Width = 1440, Height = 900 };
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 });
        await Geometry.FitWindowOverCdpAsync(f, persona: false, size);
        Assert.Equal("10,10 1440x900", FakeBrowser.Bounds(f.Of("Browser.setWindowBounds")[0]));
        var g = new FakeBrowser(new[] { 1366, 768, 0, 0, 1366, 728 });
        Assert.Equal(new[] { 1366, 728 }, (await Geometry.FitWindowOverCdpAsync(g, persona: false, size))!.Value.Outer);
        Assert.Equal("0,0 1366x728", FakeBrowser.Bounds(g.Of("Browser.setWindowBounds")[0]));
    }

    [Fact]
    public async Task AddsBackAReportedShortfallWithoutOvershooting()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 }, heightBias: 33);
        await Geometry.FitWindowOverCdpAsync(f, persona: false);
        Assert.Equal(new[] { 1040, 1073 },
            f.Of("Browser.setWindowBounds").Select(p => p.GetProperty("bounds").GetProperty("height").GetInt32()));
    }

    [Fact]
    public async Task WaitsForAResizeToReachThePageBeforeMeasuringIt()
    {
        // Measured on 150/linux-x64 (no taskbar): the first read still had the default 945x1060 window,
        // the fit "corrected" 1440 to 1440+495, and the next read had only caught up with the first
        // resize, so the 1935x900 window on a 1920x1080 screen stayed.
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1080 }, initial: new[] { 945, 1060 }, staleReads: 1);
        var r = await Geometry.FitWindowOverCdpAsync(f, persona: false, new ViewportSize { Width = 1440, Height = 900 });
        Assert.Equal(new[] { 1440, 900 }, r!.Value.Outer);
        Assert.Equal(new[] { "10,10 1440x900" }, f.Of("Browser.setWindowBounds").Select(FakeBrowser.Bounds));
    }

    [Fact]
    public async Task DoesNotCorrectAMaximizedWindowFromAStaleRead()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 }, initial: new[] { 945, 1020 }, staleReads: 2);
        Assert.Equal(new[] { 1920, 1040 }, (await Geometry.FitWindowOverCdpAsync(f, persona: false))!.Value.Outer);
        Assert.Equal(new[] { "0,0 1920x1040" }, f.Of("Browser.setWindowBounds").Select(FakeBrowser.Bounds));
    }

    [Fact]
    public async Task StillAddsBackARealShortfallWhenThePageIsSlowToSeeIt()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1040 }, heightBias: 89, staleReads: 2);
        Assert.Equal(new[] { 1920, 1040 }, (await Geometry.FitWindowOverCdpAsync(f, persona: true))!.Value.Outer);
        Assert.Equal(new[] { "0,0 1920x1040", "0,0 1920x1129" }, f.Of("Browser.setWindowBounds").Select(FakeBrowser.Bounds));
    }

    [Fact]
    public async Task CorrectsNothingWhenThePageNeverShowsTheResize()
    {
        var f = new FakeBrowser(new[] { 1920, 1080, 0, 0, 1920, 1080 }, initial: new[] { 945, 1060 }, staleReads: int.MaxValue);
        await Geometry.FitWindowOverCdpAsync(f, persona: false, new ViewportSize { Width = 1440, Height = 900 }, settleMs: 100);
        Assert.Equal(new[] { "10,10 1440x900" }, f.Of("Browser.setWindowBounds").Select(FakeBrowser.Bounds));
    }

    [Fact]
    public void NeverPlansBoundsPastTheWorkAreaForAWindowSmallerThanIt()
    {
        // 1440 wide at x=10 has 1910px of room; the misread shortfall would have asked for 1935.
        Assert.Equal((1910, 900), Geometry.FitPlan(new[] { 1440, 900 }, new[] { 945, 1060 }, new[] { 1910, 1070 }));
        // a window that fills the work area may need bounds beyond it (the persona engine clamps)
        Assert.Equal((1920, 1129), Geometry.FitPlan(new[] { 1920, 1040 }, new[] { 1920, 951 }, new[] { 1920, 1040 }));
    }

    [Fact]
    public async Task DeclinesThe800x600Surface()
    {
        var f = new FakeBrowser(new[] { 800, 600, 0, 0, 800, 600 });
        Assert.Null(await Geometry.FitWindowOverCdpAsync(f, persona: true));
        Assert.DoesNotContain("Browser.setWindowBounds", f.Methods);
    }

    [Fact]
    public async Task NeverThrows_NoEndpointOrADeadOne()
    {
        Assert.Null(await Geometry.FitServedWindowAsync(null, persona: false));
        Assert.Null(await Geometry.FitServedWindowAsync("ws://127.0.0.1:9/devtools/browser/x", persona: false, timeoutMs: 500));
    }

    // ─────────────────────────────────────────────────────── live engine
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    private record Measured(int[] Screen, int[] Avail, int[] Inner, int[] Outer, int[] Pos, bool MediaAgrees);

    private const string ReadJs =
        "[[screen.width, screen.height], [screen.availWidth, screen.availHeight], [innerWidth, innerHeight], " +
        "[outerWidth, outerHeight], [screenX, screenY], " +
        "[+matchMedia(`(device-width: ${screen.width}px) and (device-height: ${screen.height}px)`).matches]]";

    private static async Task<Measured> ReadAsync(IPage page)
    {
        var m = await page.EvaluateAsync<int[][]>(ReadJs).ConfigureAwait(false);
        return new Measured(m[0], m[1], m[2], m[3], m[4], m[5][0] == 1);
    }

    /// ServeAsync for real, attach the way a user's client does, and read the first page, a new tab and
    /// two popups (one small, one far larger than the screen).
    private static async Task<Dictionary<string, Measured>> ServedAsync(ServeOptions options)
    {
        options.ExecutablePath = LiveExe;
        options.Quiet = true;
        options.Args = new[] { "--no-sandbox" };   // as in the launch live tests: containers cannot sandbox
        var srv = await Clearcote.ServeAsync(options).ConfigureAwait(false);
        try
        {
            using var pw = await Playwright.CreateAsync().ConfigureAwait(false);
            var browser = await pw.Chromium.ConnectOverCDPAsync(srv.CdpUrl).ConfigureAwait(false);
            var ctx = browser.Contexts[0];
            var out_ = new Dictionary<string, Measured> { ["first"] = await ReadAsync(ctx.Pages[0]).ConfigureAwait(false) };
            var tab = await ctx.NewPageAsync().ConfigureAwait(false);
            await tab.GotoAsync("data:text/html,<body style='margin:0'>geo</body>").ConfigureAwait(false);
            await tab.WaitForTimeoutAsync(500).ConfigureAwait(false);
            out_["tab"] = await ReadAsync(tab).ConfigureAwait(false);
            foreach (var (w, h) in new[] { (500, 400), (4000, 3000) })
            {
                var popup = await ctx.RunAndWaitForPageAsync(() =>
                    tab.EvaluateAsync($"window.open('about:blank', '_blank', 'width={w},height={h}')")).ConfigureAwait(false);
                await popup.WaitForTimeoutAsync(500).ConfigureAwait(false);
                out_[$"popup{w}"] = await ReadAsync(popup).ConfigureAwait(false);
            }
            await browser.CloseAsync().ConfigureAwait(false);
            return out_;
        }
        finally
        {
            await srv.CloseAsync().ConfigureAwait(false);
        }
    }

    private static string Fmt(int[] pair) => $"{pair[0]}x{pair[1]}";

    private static void AssertOnScreen(string label, Measured m)
    {
        var at = $"{label}: screen={Fmt(m.Screen)} avail={Fmt(m.Avail)} inner={Fmt(m.Inner)} outer={Fmt(m.Outer)} pos={m.Pos[0]},{m.Pos[1]}";
        Assert.True(Geometry.GeometryIsCoherent(m.Screen, m.Avail, m.Inner, m.Outer), at);
        Assert.True(m.Pos[0] + m.Outer[0] <= m.Avail[0] && m.Pos[1] + m.Outer[1] <= m.Avail[1], $"overhangs: {at}");
        Assert.True(m.MediaAgrees, $"device-width media query disagrees with screen: {at}");
    }

    [Fact]
    public async Task Live_ServedSeedlessBrowserIsMaximizedOnItsDisplay()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var out_ = await ServedAsync(new ServeOptions());
        var (screen, _) = Geometry.HeadlessGeometry(null);
        foreach (var (label, m) in out_)
        {
            AssertOnScreen(label, m);
            Assert.Equal($"{screen.Width}x{screen.Height}", Fmt(m.Screen));
        }
        foreach (var label in new[] { "first", "tab" })
            Assert.Equal(Fmt(out_[label].Avail), Fmt(out_[label].Outer));
        Assert.Equal(500, out_["popup500"].Inner[0]);   // window.open() features honoured, not forced
    }

    [Fact]
    public async Task Live_ServedPersonaDisplayIsThePersonasOwn()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var out_ = await ServedAsync(new ServeOptions { Fingerprint = "live-geo" });
        foreach (var (label, m) in out_)
        {
            AssertOnScreen(label, m);
            Assert.True(m.Avail[1] < m.Screen[1], $"{label}: persona reported no taskbar");
        }
        Assert.Equal(Fmt(out_["first"].Avail), Fmt(out_["first"].Outer));
    }

    [Fact]
    public async Task Live_ServedWindowSizeIsHonouredInsideTheWorkArea()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var out_ = await ServedAsync(new ServeOptions { WindowSize = new ViewportSize { Width = 1440, Height = 900 } });
        AssertOnScreen("first", out_["first"]);
        Assert.Equal("1440x900", Fmt(out_["first"].Outer));
    }
}
