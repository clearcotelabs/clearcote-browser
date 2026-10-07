using System.IO.Compression;
using System.Reflection;
using System.Text;
using System.Text.Json;
using Clearcote;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Headless window-geometry tests.
/// </summary>
/// <remarks>
/// A default headless launch used to report a window LARGER than the screen it claims to be on
/// (screen 1280x720, outer 1288x851) — not a subtle statistical tell but a state no real browser can
/// be in, readable with two property lookups. These lock down the frame arithmetic (the cross-SDK
/// contract), the regime split (with --fingerprint the engine's persona owns screen/avail and the SDK
/// only fits the window; without it the SDK also sets the headless display with --screen-info), and
/// the rule that a caller's own geometry always wins.
///
/// PARITY: the vector below is duplicated verbatim in sdk/python/tests/test_geometry.py and
/// sdk/node/test/geometry.test.ts. A seed must select the same persona in every SDK, or a persona
/// stops being portable between them.
/// </remarks>
public class GeometryTests
{
    // ─────────────────────────────────────────────────────── frame arithmetic
    [Fact]
    public void EveryProfileRowLeavesAWindowThatFitsItsScreen()
    {
        foreach (var (w, h, _, _) in Geometry.HeadlessScreenProfiles)
        {
            var inner = new[] { w - Geometry.EngineFrameWidth, h - Geometry.EngineFrameHeight };
            var outer = new[] { inner[0] + Geometry.EngineFrameWidth, inner[1] + Geometry.EngineFrameHeight };
            // A CDP screen override forces avail == screen (measured), so that is what a page reads.
            Assert.True(Geometry.GeometryIsCoherent(new[] { w, h }, new[] { w, h }, inner, outer),
                $"{w}x{h}: window escapes its screen");
        }
    }

    [Fact]
    public void ViewportIsDerivedFromWhicheverScreenTheSeedSelected()
    {
        var table = Geometry.HeadlessScreenProfiles.Select(p => (p.Width, p.Height)).ToHashSet();
        for (var i = 0; i < 200; i++)
        {
            var (screen, viewport) = Geometry.HeadlessGeometry($"formula-{i}");
            Assert.Contains((screen.Width, screen.Height), table);
            Assert.Equal(screen.Width - Geometry.EngineFrameWidth, viewport.Width);
            Assert.Equal(screen.Height - Geometry.EngineFrameHeight, viewport.Height);
        }
    }

    [Fact]
    public void CoherenceCheckRejectsThePreFixDefault()
    {
        // If the invariant accepted this, it would not have caught anything.
        Assert.False(Geometry.GeometryIsCoherent(
            new[] { 1280, 720 }, new[] { 1280, 720 }, new[] { 1280, 720 }, new[] { 1288, 851 }));
    }

    [Fact]
    public void WindowLandsFlushWithTheScreenEdge()
    {
        var (screen, viewport) = Geometry.HeadlessGeometry("flush");
        Assert.Equal(screen.Width, viewport.Width + Geometry.EngineFrameWidth);
        Assert.Equal(screen.Height, viewport.Height + Geometry.EngineFrameHeight);
    }

    // ─────────────────────────────────────────────────────── selection
    [Fact]
    public void SelectionIsDeterministicAndSeedlessIsStableRatherThanRandom()
    {
        var a = Geometry.HeadlessGeometry("abc");
        var b = Geometry.HeadlessGeometry("abc");
        Assert.Equal((a.Screen.Width, a.Screen.Height), (b.Screen.Width, b.Screen.Height));

        var n1 = Geometry.HeadlessGeometry(null);
        var n2 = Geometry.HeadlessGeometry("");
        Assert.Equal((n1.Screen.Width, n1.Screen.Height), (n2.Screen.Width, n2.Screen.Height));
    }

    [Fact]
    public void EveryRowIsReachableAndWeightingFollowsTheCorpus()
    {
        var seen = new Dictionary<(int, int), int>();
        for (var i = 0; i < 4000; i++)
        {
            var (screen, _) = Geometry.HeadlessGeometry($"s{i}");
            var key = (screen.Width, screen.Height);
            seen[key] = seen.GetValueOrDefault(key) + 1;
        }
        Assert.Equal(Geometry.HeadlessScreenProfiles.Length, seen.Count);
        Assert.Equal(seen.Values.Max(), seen[(1920, 1080)]);
        // the capped ultrawide must stay rare
        Assert.True(seen.GetValueOrDefault((3440, 1440)) / 4000.0 < 0.08);
    }

    [Theory]
    [InlineData(null, 1920, 1080, 1912, 949)]
    [InlineData("", 1920, 1080, 1912, 949)]
    [InlineData("seed-1", 1920, 1200, 1912, 1069)]
    [InlineData("acct-42", 1366, 768, 1358, 637)]
    [InlineData("clearcote", 2560, 1440, 2552, 1309)]
    [InlineData("x", 2560, 1440, 2552, 1309)]
    [InlineData("y", 3440, 1440, 3432, 1309)]
    [InlineData("z", 1920, 1080, 1912, 949)]
    [InlineData("12345", 1920, 1080, 1912, 949)]
    public void CrossSdkParityVector(string? seed, int sw, int sh, int vw, int vh)
    {
        var (screen, viewport) = Geometry.HeadlessGeometry(seed);
        Assert.Equal((sw, sh), (screen.Width, screen.Height));
        Assert.Equal((vw, vh), (viewport.Width, viewport.Height));
    }

    // ─────────────────────────────────────────────────────── regime detection
    [Fact]
    public void PersonaActiveTracksTheFingerprintSwitchNotTheOption()
    {
        // LightStealth passes a seed to the SDK but deliberately drops --fingerprint, so no persona runs.
        Assert.True(Geometry.PersonaActive(new[] { "--fingerprint=abc", "--no-sandbox" }));
        Assert.False(Geometry.PersonaActive(new[] { "--fingerprint-platform=windows", "--fingerprint-screen-width=1920" }));
        Assert.False(Geometry.PersonaActive(Array.Empty<string>()));
        Assert.False(Geometry.PersonaActive(null));
    }

    [Fact]
    public void CallerSizedTheWindowDetectsEveryWindowFlag()
    {
        Assert.True(Geometry.CallerSizedTheWindow(new[] { "--window-size=1920,1080" }));
        Assert.True(Geometry.CallerSizedTheWindow(new[] { "--window-position=0,0" }));
        Assert.True(Geometry.CallerSizedTheWindow(new[] { "--start-maximized" }));
        Assert.False(Geometry.CallerSizedTheWindow(new[] { "--no-sandbox", "--fingerprint=x" }));
    }

    // ─────────────────────────────────────────────────────── resolve / skip rules
    [Theory]
    [InlineData(null)]
    [InlineData(true)]
    public void RegimeTwoSetsTheSeedsDisplayWhenHeadless(bool? headless)
    {
        var r = Geometry.ResolveHeadless(headless, "seed",
            new[] { "--no-sandbox", "--fingerprint-platform=windows" }, callerSetGeometry: false);
        var (screen, _) = Geometry.HeadlessGeometry("seed");
        var display = new Geometry.Display(screen.Width, screen.Height, screen.Width,
            screen.Height - Geometry.WindowsTaskbarHeight);
        Assert.Equal(Geometry.Mode.Display, r.Mode);
        Assert.Equal(display, r.Display);
        Assert.Equal(new[] { Geometry.ScreenInfoSwitch(display), "--window-position=0,0" }, r.Args);
    }

    [Fact]
    public void RegimeTwoDisplayHasATaskbarOnWindowsOnly()
    {
        var linux = Geometry.HeadlessDisplay("seed", new[] { "--fingerprint-platform=linux" });
        Assert.Equal((linux.Width, linux.Height), (linux.AvailWidth, linux.AvailHeight));
        var windows = Geometry.HeadlessDisplay("seed", new[] { "--fingerprint-platform=windows" });
        Assert.Equal(windows.Height - Geometry.WindowsTaskbarHeight, windows.AvailHeight);
    }

    [Fact]
    public void RegimeTwoDisplayAgreesWithAnExplicitScreenTheEngineSpoofs()
    {
        var args = new[] { "--fingerprint-platform=windows", "--fingerprint-screen-width=1600", "--fingerprint-screen-height=900" };
        Assert.Equal(new Geometry.Display(1600, 900, 1600, 860), Geometry.HeadlessDisplay("x", args));
        Assert.Equal(870, Geometry.HeadlessDisplay("x", args.Append("--fingerprint-avail-height=870")).AvailHeight);
        // never a work area larger than the screen
        Assert.Equal(1600, Geometry.HeadlessDisplay("x", args.Append("--fingerprint-avail-width=2000")).AvailWidth);
    }

    [Fact]
    public void ScreenInfoSwitchWritesTheTaskbarAsAWorkAreaInset()
    {
        Assert.Equal("--screen-info={1920x1080 workAreaBottom=40}",
            Geometry.ScreenInfoSwitch(new Geometry.Display(1920, 1080, 1920, 1040)));
        Assert.Equal("--screen-info={1920x1080}",
            Geometry.ScreenInfoSwitch(new Geometry.Display(1920, 1080, 1920, 1080)));
    }

    [Fact]
    public void RegimeTwoKeepsACallersOwnDisplayOrWindowSwitch()
    {
        var own = Geometry.ResolveHeadless(true, "seed", new[] { "--screen-info={1280x720}" }, callerSetGeometry: false);
        Assert.Equal(Geometry.Mode.Display, own.Mode);
        Assert.Null(own.Display);
        Assert.Equal(new[] { "--window-position=0,0" }, own.Args);
        Assert.True(Geometry.CallerSetTheDisplay(new[] { "--screen-info={1x1}" }));
        // a caller-sized window still gets a real screen under it; the fit then leaves it alone
        var sized = Geometry.ResolveHeadless(true, "seed", new[] { "--window-size=1440,900" }, callerSetGeometry: false);
        Assert.StartsWith("--screen-info=", Assert.Single(sized.Args));
    }

    [Fact]
    public void RegimeOneSetsTheLinuxHeadlessDisplayToThePersona()
    {
        // Setting screen here would be a silent no-op: the persona's value beats the CDP override.
        var r = Geometry.ResolveHeadless(true, "seed", new[] { "--fingerprint=seed" }, callerSetGeometry: false);
        Assert.Equal(Geometry.Mode.Persona, r.Mode);
        Assert.Null(r.Display);
        if (OperatingSystem.IsLinux())
        {
            Assert.Single(r.Args);
            Assert.Equal("--screen-info={1920x1080}", r.Args[0]);
        }
        else
            Assert.Empty(r.Args);
    }

    [Fact]
    public void SkippedWhenHeaded()
    {
        var r = Geometry.ResolveHeadless(false, "seed", new[] { "--fingerprint=seed" }, callerSetGeometry: false);
        Assert.Equal(Geometry.Mode.None, r.Mode);
        Assert.Empty(r.Args);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void NeverOverridesACallerWhoSetTheirOwnGeometry(bool persona)
    {
        var args = persona ? new[] { "--fingerprint=seed" } : new[] { "--no-sandbox" };
        var r = Geometry.ResolveHeadless(true, "seed", args, callerSetGeometry: true);
        Assert.Equal(Geometry.Mode.None, r.Mode);
        Assert.Null(r.Display);
        Assert.Empty(r.Args);
    }

    [Fact]
    public void TheObsoleteResolveStillReturnsItsOldShape()
    {
        // Kept so existing callers compile and behave as before; the SDK itself no longer uses it.
#pragma warning disable CS0618
        var r = Geometry.Resolve(true, "seed", new[] { "--no-sandbox" }, callerSetGeometry: false);
#pragma warning restore CS0618
        var expected = Geometry.HeadlessGeometry("seed");
        Assert.Equal(Geometry.Mode.Profile, r.Mode);
        Assert.Equal((expected.Viewport.Width, expected.Viewport.Height), (r.Viewport!.Width, r.Viewport.Height));
    }

    // ─────────────────────────────────────────────────────── the imported profile's screen
    /// Encode a capture the way Fingerprint.Args does (gzip+base64 on --fingerprint-profile).
    private static string ProfileArg(object profile)
    {
        var json = JsonSerializer.Serialize(profile);
        using var buffer = new MemoryStream();
        using (var gz = new GZipStream(buffer, CompressionLevel.Optimal, leaveOpen: true))
        {
            var bytes = Encoding.UTF8.GetBytes(json);
            gz.Write(bytes, 0, bytes.Length);
        }
        return "--fingerprint-profile=" + Convert.ToBase64String(buffer.ToArray());
    }

    [Fact]
    public void ProfileScreenIsReadOffTheSwitch()
    {
        var arg = ProfileArg(new { screen = new { width = 3440, height = 1440 } });
        Assert.Equal((3440, 1440), Geometry.ProfileScreenFromArgs(new[] { arg, "--no-sandbox" }));
    }

    [Fact]
    public void ImportedDisplayIsUsedInsteadOfACorpusPick()
    {
        // Otherwise every seedless profile launch shares one screen and the imported identity is lost.
        var arg = ProfileArg(new { screen = new { width = 2560, height = 1440 } });
        var r = Geometry.ResolveHeadless(true, "some-seed", new[] { arg, "--fingerprint-platform=windows" }, callerSetGeometry: false);
        Assert.Equal(Geometry.Mode.Display, r.Mode);
        Assert.Equal(new Geometry.Display(2560, 1440, 2560, 1400), r.Display);
    }

    [Fact]
    public void ProfileScreenTooSmallFallsBackToTheCorpus()
    {
        // profile="auto" resolved on a headless host can carry the 800x600 headless surface (measured).
        var arg = ProfileArg(new { screen = new { width = 800, height = 600 } });
        Assert.Null(Geometry.ProfileScreenFromArgs(new[] { arg }));
        var r = Geometry.ResolveHeadless(true, "seed", new[] { arg }, callerSetGeometry: false);
        var (corpus, _) = Geometry.HeadlessGeometry("seed");
        Assert.Equal((corpus.Width, corpus.Height), (r.Display!.Width, r.Display.Height));
    }

    [Theory]
    [InlineData("--fingerprint-profile=not-base64!!")]
    [InlineData("--fingerprint-profile=")]
    [InlineData("--fingerprint-profile=aGVsbG8=")]
    public void AnUnreadableProfileNeverBreaksTheLaunch(string arg)
    {
        Assert.Null(Geometry.ProfileScreenFromArgs(new[] { arg }));
        Assert.Equal(Geometry.Mode.Display,
            Geometry.ResolveHeadless(true, "seed", new[] { arg }, callerSetGeometry: false).Mode);
    }

    [Fact]
    public void ProfileWithoutAScreenBlockFallsBack()
    {
        var arg = ProfileArg(new { navigator = new { platform = "Win32" } });
        Assert.Null(Geometry.ProfileScreenFromArgs(new[] { arg }));
    }

    [Fact]
    public void ASeedBesideAProfileStillTakesThePersonaRegime()
    {
        // With --fingerprint present the ENGINE applies the profile's screen, so the SDK keeps out.
        var arg = ProfileArg(new { screen = new { width = 2560, height = 1440 } });
        var r = Geometry.ResolveHeadless(true, "seed", new[] { arg, "--fingerprint=seed" }, callerSetGeometry: false);
        Assert.Equal(Geometry.Mode.Persona, r.Mode);
        Assert.Null(r.Display);
        if (OperatingSystem.IsLinux())
        {
            Assert.Single(r.Args);
            Assert.Equal("--screen-info={2560x1440}", r.Args[0]);
        }
        else
            Assert.Empty(r.Args);
    }

    // ─────────────────────────────────────────────────────── the fit correction
    [Fact]
    public void FitPlanAddsBackExactlyTheMeasuredShortfall()
    {
        // On 149 the window reports 33px below the bounds height it was handed, so one fit lands short.
        Assert.Equal((1920, 1073), Geometry.FitPlan(new[] { 1920, 1040 }, new[] { 1920, 1007 }));
        // never asks for more than the shortfall
        Assert.Equal((1940, 1040), Geometry.FitPlan(new[] { 1920, 1040 }, new[] { 1900, 1040 }));
    }

    [Fact]
    public void CallerWindowPositionSuppressesTheFit()
    {
        // The window fit defers to a caller who positioned or sized the window themselves.
        Assert.True(Geometry.CallerSizedTheWindow(new[] { "--window-position=100,100" }));
    }

    [Fact]
    public void FitPlanIsNullWhenTheWindowAlreadyFillsTheWorkArea()
    {
        Assert.Null(Geometry.FitPlan(new[] { 1920, 1040 }, new[] { 1920, 1040 }));
        // and never shrinks a window that somehow overshot
        Assert.Null(Geometry.FitPlan(new[] { 1920, 1040 }, new[] { 1930, 1050 }));
    }

    // ─────────────────────────────────────────────────────── the launch fit, on a fake page
    /// The engine side of FitWindowToWorkAreaAsync: the window reports the bounds it got less
    /// <see cref="HeightBias"/>, and <see cref="StaleReads"/> reads after each resize still report the
    /// size from before it, as a page does until the new window size reaches it.
    public class FitPage : DispatchProxy
    {
        public int[] Avail = { 1920, 1040 };
        public int HeightBias = 33;
        public int StaleReads;
        public int[] Outer = { 0, 0 };
        public int[] Previous = { 0, 0 };
        public object? Context;
        public readonly List<string> Bounds = new();
        private int _stale;

        public void SetBounds(Dictionary<string, object> bounds)
        {
            var (w, h) = ((int)bounds["width"], (int)bounds["height"]);
            Bounds.Add($"{w}x{h}");
            Previous = Outer;
            Outer = new[] { w, h - HeightBias };
            _stale = StaleReads;
        }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "get_Context") return Context;
            if (method is { Name: "EvaluateAsync", IsGenericMethod: true })
            {
                var js = (string)args![0]!;
                if (js.Contains("availWidth")) return Task.FromResult(Avail);
                if (_stale > 0) { _stale--; return Task.FromResult(Previous); }
                return Task.FromResult(Outer);
            }
            throw new InvalidOperationException(method?.Name);
        }
    }

    public class FitCdp : DispatchProxy
    {
        public FitPage? Page;
        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            var name = (string)args![0]!;
            if (name == "Browser.setWindowBounds")
                Page!.SetBounds((Dictionary<string, object>)((Dictionary<string, object>)args[1]!)["bounds"]);
            var json = name == "Browser.getWindowForTarget" ? "{\"windowId\":7}" : "{}";
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());
        }
    }

    public class FitContext : DispatchProxy
    {
        public ICDPSession? Cdp;
        protected override object? Invoke(MethodInfo? method, object?[]? args) =>
            method?.Name == "NewCDPSessionAsync" ? Task.FromResult(Cdp!) : throw new InvalidOperationException(method?.Name);
    }

    private static (IPage Page, FitPage Fake) FitFake(int[] initial, int staleReads)
    {
        var page = DispatchProxy.Create<IPage, FitPage>();
        var ctx = DispatchProxy.Create<IBrowserContext, FitContext>();
        var cdp = DispatchProxy.Create<ICDPSession, FitCdp>();
        var fake = (FitPage)(object)page;
        (fake.Outer, fake.Previous, fake.StaleReads, fake.Context) = (initial, initial, staleReads, ctx);
        ((FitContext)(object)ctx).Cdp = cdp;
        ((FitCdp)(object)cdp).Page = fake;
        return (page, fake);
    }

    [Fact]
    public async Task TheLaunchFitAddsBackTheShortfall()
    {
        var (page, fake) = FitFake(new[] { 945, 1020 }, staleReads: 0);
        Assert.Equal((1920, 1040), (await Geometry.FitWindowToWorkAreaAsync(page, new[] { "--fingerprint=x" }))!.Value);
        Assert.Equal(new[] { "1920x1040", "1920x1073" }, fake.Bounds);
    }

    [Fact]
    public async Task TheLaunchFitMeasuresTheShortfallOnlyOnceThePageHasTheNewSize()
    {
        // A read straight after the resize can still have the engine's default window: correcting from
        // it asked for 1920+975 px, and a second stale read hid the overshoot.
        var (page, fake) = FitFake(new[] { 945, 1020 }, staleReads: 2);
        Assert.Equal((1920, 1040), (await Geometry.FitWindowToWorkAreaAsync(page, new[] { "--fingerprint=x" }))!.Value);
        Assert.Equal(new[] { "1920x1040", "1920x1073" }, fake.Bounds);
    }
}
