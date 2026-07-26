using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// Live-engine geometry tests. Skipped unless CLEARCOTE_LIVE_ENGINE points at a chrome binary
/// (add CLEARCOTE_LICENSE_KEY for a PRO build). These belong in the release gate.
/// </summary>
/// <remarks>
/// Unit tests with a faked page cannot catch a binding mismatch: the Node port's window fit silently
/// did nothing for a while because Playwright's JS binding evaluates an arrow-function STRING to a
/// function object instead of calling it, and the fake — which sniffed the string — happily played
/// along. Only a real browser catches that class of bug, and .NET's EvaluateAsync has the same
/// expression-vs-function ambiguity, so it needs its own live check.
/// </remarks>
public class GeometryLiveTests
{
    private static string? LiveExe => Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");

    private record Measured(
        int[] Screen, int[] Avail, int[] Inner, int[] Outer, int[] Pos, int Resizes);

    private static async Task<(Measured First, Measured SecondTab)> MeasureBothAsync(string? fingerprint)
    {
        var dir = Path.Combine(Path.GetTempPath(), "cc-live2-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        try
        {
            var context = await Clearcote.LaunchPersistentContextAsync(dir, new LaunchOptions
            {
                ExecutablePath = LiveExe, Args = new[] { "--no-sandbox" }, Quiet = true,
                Fingerprint = fingerprint,
            }).ConfigureAwait(false);
            try
            {
                await context.AddInitScriptAsync(
                    "window.__resizes = 0; addEventListener('resize', () => { window.__resizes++; }, true);")
                    .ConfigureAwait(false);
                var first = await ReadAsync(await context.NewPageAsync().ConfigureAwait(false)).ConfigureAwait(false);
                var second = await ReadAsync(await context.NewPageAsync().ConfigureAwait(false)).ConfigureAwait(false);
                return (first, second);
            }
            finally
            {
                await context.CloseAsync().ConfigureAwait(false);
            }
        }
        finally
        {
            TestTemp.Remove(dir);
        }
    }

    private static async Task<Measured> ReadAsync(IPage page)
    {
        await page.GotoAsync("data:text/html,<body style='margin:0'>geo</body>").ConfigureAwait(false);
        await page.WaitForTimeoutAsync(700).ConfigureAwait(false);
        var m = await page.EvaluateAsync<int[][]>(
            "[[screen.width, screen.height], [screen.availWidth, screen.availHeight], " +
            "[innerWidth, innerHeight], [outerWidth, outerHeight], [screenX, screenY], " +
            "[window.__resizes]]").ConfigureAwait(false);
        return new Measured(m[0], m[1], m[2], m[3], m[4], m[5][0]);
    }

    private static async Task<Measured> MeasureAsync(string? fingerprint)
    {
        var dir = Path.Combine(Path.GetTempPath(), "cc-live-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        try
        {
            var context = await Clearcote.LaunchPersistentContextAsync(dir, new LaunchOptions
            {
                ExecutablePath = LiveExe,
                Args = new[] { "--no-sandbox" },
                Quiet = true,
                Fingerprint = fingerprint,
            }).ConfigureAwait(false);
            try
            {
                // Runs before any page script: if the window were resized after a page starts
                // running JS, a detector would see a resize event and a jump in innerWidth.
                await context.AddInitScriptAsync(
                    "window.__resizes = 0; addEventListener('resize', () => { window.__resizes++; }, true);")
                    .ConfigureAwait(false);
                var page = await context.NewPageAsync().ConfigureAwait(false);
                await page.GotoAsync("data:text/html,<body style='margin:0'>geo</body>").ConfigureAwait(false);
                await page.WaitForTimeoutAsync(700).ConfigureAwait(false);   // first paint
                var m = await page.EvaluateAsync<int[][]>(
                    "[[screen.width, screen.height], [screen.availWidth, screen.availHeight], " +
                    "[innerWidth, innerHeight], [outerWidth, outerHeight], [screenX, screenY], " +
                    "[window.__resizes]]").ConfigureAwait(false);
                return new Measured(m[0], m[1], m[2], m[3], m[4], m[5][0]);
            }
            finally
            {
                await context.CloseAsync().ConfigureAwait(false);
            }
        }
        finally
        {
            TestTemp.Remove(dir);
        }
    }

    // No Skippable* package here (and not worth a new test dependency), so an unset
    // CLEARCOTE_LIVE_ENGINE makes these no-op instead of failing the normal suite.
    [Fact]
    public async Task Regime1_PersonaOwnsTheScreen_AndTheWindowIsMaximized()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var m = await MeasureAsync("live-geo-dotnet");

        Assert.True(Geometry.GeometryIsCoherent(m.Screen, m.Avail, m.Inner, m.Outer),
            $"live geometry escapes its screen: screen={Fmt(m.Screen)} avail={Fmt(m.Avail)} " +
            $"inner={Fmt(m.Inner)} outer={Fmt(m.Outer)}");
        // screen must not have collapsed onto the viewport — that collapse is the original bug
        Assert.NotEqual(Fmt(m.Screen), Fmt(m.Inner));
        // the persona reserves a taskbar
        Assert.True(m.Avail[1] < m.Screen[1], "persona reported no taskbar");
        // the fit maximized into the work area — the assertion that catches a silently no-op fit
        Assert.Equal(Fmt(m.Avail), Fmt(m.Outer));
        // and it happened on about:blank, before the page ran any script
        Assert.Equal(0, m.Resizes);
    }

    [Fact]
    public async Task Regime2_SeedlessScreenOverride_AndFrameConstantStillHolds()
    {
        if (string.IsNullOrEmpty(LiveExe)) return;
        var m = await MeasureAsync(null);
        var (screen, viewport) = Geometry.HeadlessGeometry(null);

        Assert.Equal($"{screen.Width}x{screen.Height}", $"{m.Screen[0]}x{m.Screen[1]}");
        Assert.Equal($"{viewport.Width}x{viewport.Height}", $"{m.Inner[0]}x{m.Inner[1]}");
        Assert.True(Geometry.GeometryIsCoherent(m.Screen, m.Avail, m.Inner, m.Outer),
            "live geometry escapes its screen");
        // the engine's frame must still match the constants the regime-2 viewport is sized against
        Assert.Equal(Geometry.EngineFrameWidth, m.Outer[0] - m.Inner[0]);
        Assert.Equal(Geometry.EngineFrameHeight, m.Outer[1] - m.Inner[1]);
        // moved to the origin, so the window does not hang off the spoofed screen edge
        Assert.Equal("0,0", $"{m.Pos[0]},{m.Pos[1]}");
        Assert.Equal(0, m.Resizes);
    }

    [Fact]
    public async Task ScreenOverrideAlsoReachesPagesOpenedLater()
    {
        // The .NET screen override is per-TARGET (verified: a second tab does not inherit it), so a
        // tab opened after launch is where a half-done implementation shows up.
        if (string.IsNullOrEmpty(LiveExe)) return;
        var (first, second) = await MeasureBothAsync(null);
        var (screen, _) = Geometry.HeadlessGeometry(null);

        foreach (var (label, m) in new[] { ("first page", first), ("second tab", second) })
        {
            Assert.Equal($"{screen.Width}x{screen.Height}", Fmt(m.Screen));
            Assert.True(Geometry.GeometryIsCoherent(m.Screen, m.Avail, m.Inner, m.Outer),
                $"{label}: geometry escapes its screen (screen={Fmt(m.Screen)} outer={Fmt(m.Outer)})");
            Assert.Equal(0, m.Resizes);
        }
    }

    private static string Fmt(int[] pair) => $"{pair[0]}x{pair[1]}";
}
