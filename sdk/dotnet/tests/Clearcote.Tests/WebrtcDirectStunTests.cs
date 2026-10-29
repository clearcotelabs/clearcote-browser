using System.Text;
using Xunit;

namespace Clearcote.Tests;

/// pro-r54-06: real STUN without a proxy (--webrtc-direct-stun), only for this machine's own WebRTC IP, only on
/// r54+ (sdk/node test/webrtc-direct-stun.test.ts).
public sealed class WebrtcDirectStunTests : IDisposable
{
    private const string Own = "203.0.113.7";
    private static readonly ProxyOptions Proxy = new() { Server = "http://p:8080" };
    private readonly List<string> _dirs = new();
    private readonly Sandbox _sb = new();
    private readonly Func<ProxyOptions?, bool, int?, Task<GeoResult>> _real = GeoIp.Resolver;
    private int _asked;

    public WebrtcDirectStunTests()
    {
        GeoIp.ResetDirectCache();
        _sb.Env("CLEARCOTE_NO_WARN", null).Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", null).Env(GeoIp.GeoipEnv, null);
        GeoIp.Resolver = (_, _, _) =>
        {
            _asked++;
            return Task.FromResult(new GeoResult(new GeoInfo(Own, "DE", "Europe/Berlin", "de-DE", "52.52,13.405"), null, 5));
        };
    }

    public void Dispose()
    {
        GeoIp.Resolver = _real;
        GeoIp.ResetDirectCache();
        _sb.Dispose();
        foreach (var d in _dirs) try { Directory.Delete(d, true); } catch { /* best effort */ }
    }

    private string Engine(string sw)
    {
        var d = TestTemp.Create("cc-direct-stun-");
        _dirs.Add(d);
        var exe = Path.Combine(d, Native.IsWindows ? "chrome.exe" : "chrome");
        var body = Encoding.Latin1.GetBytes($"xx\0{sw}\0yy");
        File.WriteAllBytes(exe, body);
        if (Native.IsWindows) File.WriteAllBytes(Path.Combine(d, "chrome.dll"), body);
        return exe;
    }

    private static GeoIp.GeoipApplied Applied(bool auto, string? ip) => new(null, auto, Task.CompletedTask, ip);

    [Fact]
    public void DirectStunEligible_only_with_no_proxy_and_the_WebRTC_IP_the_lookup_found()
    {
        var fp = new FingerprintOptions { WebrtcIp = Own };
        Assert.True(GeoIp.DirectStunEligible(fp, Applied(true, Own), null));
        Assert.True(GeoIp.DirectStunEligible(fp, Applied(false, Own), null));   // Geoip = true
        Assert.False(GeoIp.DirectStunEligible(fp, Applied(true, Own), Proxy));
        Assert.False(GeoIp.DirectStunEligible(new FingerprintOptions { WebrtcIp = "198.51.100.9" }, Applied(false, Own), null));
        Assert.False(GeoIp.DirectStunEligible(new FingerprintOptions(), Applied(false, Own), null));
        Assert.False(GeoIp.DirectStunEligible(fp, Applied(false, null), null));   // geoip off, hand-set IP
    }

    [Fact]
    public void WebrtcDirectStunArgs_emits_the_switch_only_on_an_engine_that_has_it()
    {
        var r54 = Engine("webrtc-direct-stun");
        var r53 = Engine("socks5-udp");
        var none = Array.Empty<string>();
        Assert.Equal(new[] { "--webrtc-direct-stun" }, LaunchOpts.WebrtcDirectStunArgs(r54, true, none));
        Assert.Empty(LaunchOpts.WebrtcDirectStunArgs(r53, true, none));
        Assert.Empty(LaunchOpts.WebrtcDirectStunArgs(null, true, none));
        Assert.Empty(LaunchOpts.WebrtcDirectStunArgs(r54, false, none));
    }

    [Fact]
    public void WebrtcDirectStunArgs_never_alongside_a_proxy_in_the_callers_own_args()
    {
        var r54 = Engine("webrtc-direct-stun");
        Assert.Empty(LaunchOpts.WebrtcDirectStunArgs(r54, true, new[] { "--proxy-server=http://p:8080" }));
        Assert.Empty(LaunchOpts.WebrtcDirectStunArgs(r54, true, new[] { "--proxy-pac-url=http://x/p.pac" }));
    }

    [Fact]
    public async Task ApplyModeAsync_reports_the_egress_IP_from_a_lookup_the_no_proxy_cache_and_strict()
    {
        Assert.Equal(Own, (await GeoIp.ApplyModeAsync(new FingerprintOptions(), null, GeoipMode.Auto, quiet: true)).Ip);
        var cached = await GeoIp.ApplyModeAsync(new FingerprintOptions(), null, GeoipMode.Auto, quiet: true);
        Assert.Equal(1, _asked);
        Assert.Equal(Own, cached.Ip);
        Assert.Equal(Own, (await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Strict, quiet: true)).Ip);
        Assert.Null((await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Off, quiet: true)).Ip);
    }

    [Fact]
    public async Task AssembleArgs_carries_the_switch_for_this_machines_own_WebRTC_IP_only()
    {
        var r54 = Engine("webrtc-direct-stun");
        async Task<List<string>> Args(LaunchOptions o)
        {
            var prepared = await Clearcote.PrepareLaunchAsync(o);
            var opts = prepared.Options;
            var direct = GeoIp.DirectStunEligible(opts, prepared.Geo, opts.Proxy);
            return Clearcote.AssembleArgs(new(), new(), new(), null, opts.WebrtcIp, opts.Args ?? Array.Empty<string>(), opts.Proxy, false,
                new Clearcote.EngineExtras(r54, false, true, null, null, DirectStun: direct));
        }
        Assert.Contains("--webrtc-direct-stun", await Args(new LaunchOptions { Quiet = true }));
        Assert.Contains("--webrtc-direct-stun", await Args(new LaunchOptions { Quiet = true, Geoip = true }));
        Assert.DoesNotContain("--webrtc-direct-stun", await Args(new LaunchOptions { Quiet = true, Proxy = Proxy }));
        Assert.DoesNotContain("--webrtc-direct-stun", await Args(new LaunchOptions { Quiet = true, WebrtcIp = "198.51.100.9" }));
        Assert.DoesNotContain("--webrtc-direct-stun", await Args(new LaunchOptions { Quiet = true, Geoip = false, WebrtcIp = Own }));
        Assert.DoesNotContain("--webrtc-direct-stun",
            await Args(new LaunchOptions { Quiet = true, Args = new[] { "--proxy-server=http://p:8080" } }));
    }
}
