using System.Reflection;
using System.Text.Json;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// The humanized wheel scrolls whole notches of the persona platform's size, one axis per event
// (mirrors the Python and Node tests). Before, one scroll of 100 went out as 39/28/20/10/3, each
// reporting a full wheelDelta of 120 — events no device emits.
public class HumanizeWheelTests
{
    // Answers the isolated-world reads: the platform read with Platform (null JSON when unset), every
    // other read with a 1280x720 viewport.
    public class Cdp : DispatchProxy
    {
        public string? Platform { get; set; }
        public int PlatformReads { get; private set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name != "SendAsync") return Task.CompletedTask;
            var name = (string)args![0]!;
            var expr = (args.Length > 1 ? args[1] as Dictionary<string, object> : null)?.GetValueOrDefault("expression");
            string json;
            if (name == "Page.getFrameTree") json = "{\"frameTree\":{\"frame\":{\"id\":\"MAIN\"}}}";
            else if (name == "Page.createIsolatedWorld") json = "{\"executionContextId\":5}";
            else if (Equals(expr, $"({IsolatedWorld.Platform})(null)"))
            {
                PlatformReads++;
                json = $"{{\"result\":{{\"value\":{JsonSerializer.Serialize(Platform)}}}}}";
            }
            else json = "{\"result\":{\"value\":[1280,720]}}";
            return Task.FromResult<JsonElement?>(JsonDocument.Parse(json).RootElement.Clone());
        }
    }

    public class Mouse : DispatchProxy
    {
        public List<(float X, float Y)> Wheels { get; } = new();
        public int FailWheelCall { get; set; } = -1;   // this wheel call (0-based) throws, once
        private int _calls;

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "WheelAsync")
            {
                if (_calls++ == FailWheelCall)
                    return Task.FromException(new PlaywrightException("Target page, context or browser has been closed"));
                Wheels.Add(((float)args![0]!, (float)args[1]!));
            }
            return Task.CompletedTask;
        }
    }

    public class Page : HumanizeShiftTests.Recorder
    {
        public object? Context { get; set; }

        protected override object? Invoke(MethodInfo? method, object?[]? args)
        {
            if (method?.Name == "get_Context") return Context;
            if (method?.Name.StartsWith("Evaluate", StringComparison.Ordinal) == true)
                throw new InvalidOperationException("humanize must not evaluate in the page's world");
            return base.Invoke(method, args);
        }
    }

    private static (IPage Page, Mouse Mouse, Cdp Cdp) Make(string? platform, bool withCdp = true)
    {
        var cdp = DispatchProxy.Create<ICDPSession, Cdp>();
        var ctx = DispatchProxy.Create<IBrowserContext, IsolatedWorldTests.FakeContext>();
        var mouse = DispatchProxy.Create<IMouse, Mouse>();
        var page = DispatchProxy.Create<IPage, Page>();
        ((Cdp)(object)cdp).Platform = platform;
        ((IsolatedWorldTests.FakeContext)(object)ctx).Cdp = withCdp ? cdp : null;
        ((Page)(object)page).Context = ctx;
        ((Page)(object)page).Mouse = mouse;
        return (page, (Mouse)(object)mouse, (Cdp)(object)cdp);
    }

    [Theory]
    [InlineData("Windows", 0, 100, new float[] { 0, 100 })]
    [InlineData("Windows", 0, 39, new float[] { 0, 100 })]
    [InlineData("Linux", 0, 250, new float[] { 0, 120, 0, 120 })]
    [InlineData("macOS", 0, -100, new float[] { 0, -40, 0, -40, 0, -40 })]
    [InlineData("Windows", 150, 0, new float[] { 100, 0, 100, 0 })]
    [InlineData("Win32", 0, 0, new float[0])]
    public async Task Scrolls_whole_notches_of_the_platform_one_axis_per_event(string platform, float dx, float dy, float[] want)
    {
        var (page, mouse, _) = Make(platform);
        await page.HumanWheelAsync(dx, dy);
        Assert.Equal(want, mouse.Wheels.SelectMany(w => new[] { w.X, w.Y }).ToArray());
        int notch = Motion.WheelNotchPx[Motion.PlatformFromNavigator(platform)!];
        Assert.All(mouse.Wheels, w => Assert.Equal(notch, Math.Abs(w.X) + Math.Abs(w.Y)));
    }

    [Fact]
    public async Task Vertical_notches_go_first_then_horizontal()
    {
        var (page, mouse, _) = Make("Linux x86_64");
        await page.HumanWheelAsync(-130, 240);
        Assert.Equal(new (float, float)[] { (0, 120), (0, 120), (-120, 0) }, mouse.Wheels.ToArray());
    }

    [Fact]
    public async Task Reads_the_platform_once_per_page_in_the_isolated_world()
    {
        var (page, mouse, cdp) = Make("MacIntel");
        await page.HumanScrollAsync(40);
        await page.HumanScrollAsync(-40);
        Assert.Equal(1, cdp.PlatformReads);
        Assert.Equal(new (float, float)[] { (0, 40), (0, -40) }, mouse.Wheels.ToArray());
    }

    [Fact]
    public async Task Falls_back_to_the_host_os_when_the_read_fails()
    {
        var (page, mouse, _) = Make("Windows", withCdp: false);
        await page.HumanScrollAsync(1);
        float host = OperatingSystem.IsWindows() ? 100 : OperatingSystem.IsMacOS() ? 40 : 120;
        Assert.Equal(new (float, float)[] { (0, host) }, mouse.Wheels.ToArray());
    }

    [Fact]
    public async Task Falls_back_to_the_host_os_on_an_unknown_platform()
    {
        var (page, mouse, _) = Make("");
        await page.HumanScrollAsync(1);
        float host = OperatingSystem.IsWindows() ? 100 : OperatingSystem.IsMacOS() ? 40 : 120;
        Assert.Equal(new (float, float)[] { (0, host) }, mouse.Wheels.ToArray());
    }

    [Fact]
    public async Task A_failed_wheel_delivers_the_remaining_whole_notches()
    {
        // The second event fails: the fallback sends it and the third again, plainly — whole
        // notches only, never a sub-notch remainder, and no scroll lost.
        var (page, mouse, _) = Make("Linux");
        mouse.FailWheelCall = 1;
        await page.HumanWheelAsync(0, 360);
        Assert.Equal(new (float, float)[] { (0, 120), (0, 120), (0, 120) }, mouse.Wheels.ToArray());
    }
}
