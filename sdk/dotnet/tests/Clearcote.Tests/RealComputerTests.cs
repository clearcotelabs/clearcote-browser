using System.Text.Json.Nodes;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// RealComputer: this computer's screen / touch / display language instead of the persona's generated values.
/// Same cases as the Node suite (test/realcomputer.test.ts) and the Python suite (tests/test_realcomputer.py).
/// </summary>
public class RealComputerTests : IDisposable
{
    private readonly StringWriter _err = new();
    private readonly TextWriter _stderr = Console.Error;

    private static readonly RealComputer.HostFacts Facts =
        new(new RealComputer.HostDisplay(3440, 1440, 3440, 1392, 1), 2, "en-GB");

    public RealComputerTests()
    {
        LaunchWarnings.ForgetSaid();
        RealComputer.ResetForTests();
        Console.SetError(_err);
    }

    public void Dispose()
    {
        Console.SetError(_stderr);
        LaunchWarnings.ForgetSaid();
        RealComputer.ResetForTests();
    }

    private static Func<Task<RealComputer.HostFacts>> Reader(RealComputer.HostFacts f, List<int>? calls = null)
        => () => { calls?.Add(1); return Task.FromResult(f); };

    [Fact]
    public void Windows_physical_px_divided_by_the_system_scaling()
    {
        Assert.Equal(Facts, RealComputer.ParseWindows("3440 1440 3440 1392 96 2 en-GB"));
        Assert.Equal(new RealComputer.HostFacts(new RealComputer.HostDisplay(1536, 864, 1536, 826, 1.25), 0, "de-DE"),
            RealComputer.ParseWindows("1920 1080 1920 1032 120 0 de-DE"));
    }

    [Fact]
    public void Windows_broken_answer_gives_nothing()
    {
        Assert.Equal(new RealComputer.HostFacts(null, null, null), RealComputer.ParseWindows(""));
        Assert.Equal(new RealComputer.HostFacts(null, null, null), RealComputer.ParseWindows("abc def"));
        Assert.Null(RealComputer.ParseWindows("640 300 640 300 96 0 en-US").Display);
    }

    [Fact]
    public void Mac_points_are_css_px_and_touch_is_zero()
        => Assert.Equal(new RealComputer.HostFacts(new RealComputer.HostDisplay(1512, 982, 1512, 944, 2), 0, "en-GB"),
            RealComputer.ParseMac("1512 982 1512 944 2 en-GB"));

    [Fact]
    public void Linux_primary_xrandr_output_else_first_connected()
    {
        var outp = string.Join("\n",
            "Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767",
            "HDMI-1 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 527mm x 296mm",
            "DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm");
        Assert.Equal(new RealComputer.HostDisplay(2560, 1440, 2560, 1440, 1), RealComputer.ParseXrandr(outp));
        Assert.Equal(1366, RealComputer.ParseXrandr("eDP-1 connected 1366x768+0+0 (normal)")!.Width);
        Assert.Null(RealComputer.ParseXrandr("Screen 0: current 1024 x 768\nVGA-1 disconnected"));
    }

    [Fact]
    public void Plausibility()
    {
        Assert.Null(RealComputer.Plausible(new RealComputer.HostDisplay(1920, 1080, 2000, 1040, 1)));
        Assert.Null(RealComputer.Plausible(new RealComputer.HostDisplay(1920, 1080, 1920, 1040, 5)));
    }

    [Fact]
    public async Task Linux_without_a_display_server()
    {
        var f = await RealComputer.ReadCoreAsync("linux", n => n == "LANG" ? "fr_FR.UTF-8" : null);
        Assert.Equal(new RealComputer.HostFacts(null, null, "fr-FR"), f);
    }

    [Fact]
    public void Bool_converts_to_parts()
    {
        RealComputer on = true, off = false;
        Assert.True(on.Screen && on.Touch && on.Languages);
        Assert.False(off.Any);
    }

    [Fact]
    public async Task Off_reads_nothing()
    {
        var calls = new List<int>();
        var o = new LaunchOptions { Fingerprint = "a1" };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, Reader(Facts, calls));
        Assert.Empty(calls);
        Assert.Null(o.ScreenWidth);
    }

    [Fact]
    public async Task True_fills_every_part()
    {
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = true };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, Reader(Facts));
        Assert.Equal((3440, 1440, 3440, 1392, 1.0, 2, "en-GB"),
            (o.ScreenWidth, o.ScreenHeight, o.AvailWidth, o.AvailHeight, o.DevicePixelRatio, o.MaxTouchPoints, o.AcceptLanguage));
    }

    [Fact]
    public async Task Picks_parts()
    {
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = new RealComputer { Screen = true } };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, Reader(Facts));
        Assert.Equal(3440, o.ScreenWidth);
        Assert.Null(o.MaxTouchPoints);
        Assert.Null(o.AcceptLanguage);
    }

    [Fact]
    public async Task Caller_values_win()
    {
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = true, ScreenWidth = 1920, ScreenHeight = 1080, MaxTouchPoints = 0, AcceptLanguage = "de-DE" };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, Reader(Facts));
        Assert.Equal((1920, 1080, 0, "de-DE"), (o.ScreenWidth, o.ScreenHeight, o.MaxTouchPoints, o.AcceptLanguage));
        Assert.Null(o.AvailWidth);
    }

    [Fact]
    public async Task Proxy_with_geoip_keeps_languages()
    {
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = true };
        await RealComputer.ApplyAsync(o, proxied: true, quiet: false, Reader(Facts));
        Assert.Equal((3440, 2), (o.ScreenWidth, o.MaxTouchPoints));
        Assert.Null(o.AcceptLanguage);
    }

    [Fact]
    public async Task Passthrough_untouched()
    {
        var calls = new List<int>();
        var o = new LaunchOptions { Fingerprint = "off", RealComputer = true };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, Reader(Facts, calls));
        Assert.Empty(calls);
        Assert.Null(o.ScreenWidth);
    }

    [Fact]
    public async Task Unreadable_parts_warn_once()
    {
        var none = Reader(new RealComputer.HostFacts(null, null, "en-US"));
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = true };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: false, none);
        Assert.Null(o.ScreenWidth);
        Assert.Null(o.MaxTouchPoints);
        Assert.Equal("en-US", o.AcceptLanguage);
        await RealComputer.ApplyAsync(new LaunchOptions { Fingerprint = "a1", RealComputer = true }, proxied: false, quiet: false, none);
        var notes = _err.ToString().Split('\n').Where(l => l.Contains("RealComputer:", StringComparison.Ordinal)).ToList();
        Assert.Single(notes);
        Assert.Contains("screen and touch", notes[0]);
    }

    [Fact]
    public async Task Values_reach_the_engine_switches()
    {
        var o = new LaunchOptions { Fingerprint = "a1", RealComputer = true };
        await RealComputer.ApplyAsync(o, proxied: false, quiet: true, Reader(Facts));
        var args = Fingerprint.Args(o);
        foreach (var a in new[] { "--fingerprint-screen-width=3440", "--fingerprint-screen-height=1440", "--fingerprint-avail-width=3440",
                     "--fingerprint-avail-height=1392", "--fingerprint-max-touch-points=2" })
            Assert.Contains(a, args);
        Assert.Contains(args, a => a.StartsWith("--accept-lang=en-GB", StringComparison.Ordinal));
    }

    [Fact]
    public void Serve_query_and_saved_profiles_convert_to_the_option()
    {
        var ident = Multiplex.ParseConnectionIdentity("real-computer=true&fingerprint=a1");
        Assert.True(ident.Options["realComputer"] is RealComputer { Screen: true, Touch: true, Languages: true });

        var o = new LaunchOptions();
        var unused = ProfileOptionsMap.Apply(o, new JsonObject { ["realComputer"] = new JsonObject { ["screen"] = true } }, onlyUnset: false);
        Assert.Empty(unused);
        Assert.True(o.RealComputer is { Screen: true, Touch: false, Languages: false });
        var o2 = new LaunchOptions();
        ProfileOptionsMap.Apply(o2, new JsonObject { ["real_computer"] = true }, onlyUnset: false);
        Assert.True(o2.RealComputer?.Any ?? false);
    }

    [Fact]
    public void Cloud_and_Docker_drop_false_and_refuse_true()
    {
        CloudLaunch.SessionOptionsOf(new LaunchOptions { Cloud = true, RealComputer = false });
        Assert.ThrowsAny<Exception>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { Cloud = true, RealComputer = true }));
        DockerLaunch.ContainerEnv(new LaunchOptions { RealComputer = false });
        Assert.ThrowsAny<Exception>(() => DockerLaunch.ContainerEnv(new LaunchOptions { RealComputer = true }));
    }
}
