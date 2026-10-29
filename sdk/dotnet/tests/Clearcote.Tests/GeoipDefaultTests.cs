using Xunit;

namespace Clearcote.Tests;

/// geoip on by default (0.66.0): mode selection, fail-open default, no-proxy cache, open-build WebRTC IP
/// (sdk/node test/geoip-default.test.ts).
public sealed class GeoipDefaultTests : IDisposable
{
    private static readonly GeoInfo Geo = new("203.0.113.7", "DE", "Europe/Berlin", "de-DE", "52.52,13.405");
    private static readonly ProxyOptions Proxy = new() { Server = "http://proxy.example:8080" };

    private readonly Sandbox _sb = new();
    private readonly Func<ProxyOptions?, bool, int?, Task<GeoResult>> _real = GeoIp.Resolver;
    private readonly TextWriter _savedErr = Console.Error;
    private readonly StringWriter _err = new();
    private GeoResult _answer = new(Geo, null, 5);
    private readonly List<ProxyOptions?> _asked = new();
    private readonly List<int?> _timeouts = new();

    public GeoipDefaultTests()
    {
        GeoIp.ResetDirectCache();
        _sb.Env("CLEARCOTE_NO_WARN", null).Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", null);
        GeoIp.Resolver = (proxy, _, timeout) => { _asked.Add(proxy); _timeouts.Add(timeout); return Task.FromResult(_answer); };
        Console.SetError(TextWriter.Synchronized(_err));
    }

    public void Dispose()
    {
        Console.SetError(_savedErr);
        GeoIp.Resolver = _real;
        GeoIp.ResetDirectCache();
        _sb.Dispose();
    }

    private static Dictionary<string, string?> E(string? v = null) => new() { [GeoIp.GeoipEnv] = v };

    [Fact]
    public void Mode_is_auto_when_unset_strict_for_true_off_for_false()
    {
        Assert.Equal(GeoipMode.Auto, GeoIp.Mode(null, E()));
        Assert.Equal(GeoipMode.Strict, GeoIp.Mode(true, E()));
        Assert.Equal(GeoipMode.Off, GeoIp.Mode(false, E()));
        Assert.Null(new LaunchOptions().Geoip);   // unset by default, so "unset" and "false" differ
    }

    [Fact]
    public void CLEARCOTE_GEOIP_0_turns_the_default_off_but_not_an_explicit_true()
    {
        foreach (var v in new[] { "0", "false", "OFF", "no", " off " }) Assert.Equal(GeoipMode.Off, GeoIp.Mode(null, E(v)));
        Assert.Equal(GeoipMode.Auto, GeoIp.Mode(null, E("1")));
        Assert.Equal(GeoipMode.Strict, GeoIp.Mode(true, E("0")));
    }

    [Fact]
    public async Task Auto_with_no_proxy_looks_this_machines_own_IP_up_and_fills_every_field()
    {
        var fp = new FingerprintOptions();
        var r = await GeoIp.ApplyModeAsync(fp, null, GeoipMode.Auto, quiet: true);
        Assert.Equal(new ProxyOptions?[] { null }, _asked);
        Assert.Equal(("Europe/Berlin", "de-DE", "52.52,13.405", "203.0.113.7"), (fp.Timezone, fp.AcceptLanguage, fp.Location, fp.WebrtcIp));
        Assert.True(r.AutoWebrtcIp);
        Assert.Equal(new int?[] { 10_000 }, _timeouts);   // the default's own 10 s budget
    }

    [Fact]
    public async Task Auto_takes_CLEARCOTE_GEOIP_TIMEOUT_SECONDS_when_set()
    {
        _sb.Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "3");
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Auto, quiet: true);
        Assert.Equal(new int?[] { 3000 }, _timeouts);
    }

    [Fact]
    public async Task Auto_reuses_a_no_proxy_lookup_but_looks_a_proxy_up_every_launch()
    {
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), null, GeoipMode.Auto, quiet: true);
        var fp = new FingerprintOptions();
        var r = await GeoIp.ApplyModeAsync(fp, null, GeoipMode.Auto, quiet: true);
        Assert.Single(_asked);
        Assert.Equal("Europe/Berlin", fp.Timezone);
        Assert.Equal("203.0.113.7", fp.WebrtcIp);
        Assert.True(r.AutoWebrtcIp);
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Auto, quiet: true);
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Auto, quiet: true);
        Assert.Equal(new[] { null, Proxy, Proxy }, _asked);
    }

    [Fact]
    public async Task Auto_keeps_what_the_caller_set_and_skips_the_lookup_when_nothing_is_left_to_fill()
    {
        var fp = new FingerprintOptions { Timezone = "Asia/Tokyo" };
        await GeoIp.ApplyModeAsync(fp, Proxy, GeoipMode.Auto, quiet: true);
        Assert.Equal("Asia/Tokyo", fp.Timezone);
        Assert.Equal("de-DE", fp.AcceptLanguage);
        _asked.Clear();
        var r = await GeoIp.ApplyModeAsync(
            new FingerprintOptions { Timezone = "a", AcceptLanguage = "b", Location = "1,2", WebrtcIp = "198.51.100.1" }, Proxy, GeoipMode.Auto, quiet: true);
        Assert.Empty(_asked);
        Assert.False(r.AutoWebrtcIp);
    }

    [Fact]
    public async Task Auto_does_not_count_a_callers_own_WebRTC_IP_as_the_defaults()
    {
        var fp = new FingerprintOptions { WebrtcIp = "198.51.100.1" };
        var r = await GeoIp.ApplyModeAsync(fp, Proxy, GeoipMode.Auto, quiet: true);
        Assert.Equal("198.51.100.1", fp.WebrtcIp);
        Assert.False(r.AutoWebrtcIp);
    }

    [Fact]
    public async Task Auto_warns_and_launches_when_the_region_cannot_be_resolved()
    {
        _answer = new GeoResult(null, "could not determine the exit IP", 5);
        var fp = new FingerprintOptions();
        var r = await GeoIp.ApplyModeAsync(fp, Proxy, GeoipMode.Auto, quiet: false);
        Assert.Null(fp.Timezone);
        Assert.Null(fp.AcceptLanguage);
        Assert.Null(fp.WebrtcIp);
        Assert.False(r.AutoWebrtcIp);
        var output = _err.ToString();
        Assert.Contains("clearcote: warning: geoip (on by default): could not resolve the proxy's region (could not determine the exit IP).", output);
        Assert.Contains("Geoip = false", output);
        Assert.DoesNotContain("Launching anyway", output);
        Assert.Single(output.Trim().Split('\n'));   // one warning
    }

    [Fact]
    public async Task Auto_failure_is_silent_when_quiet_or_CLEARCOTE_NO_WARN()
    {
        _answer = new GeoResult(null, "could not determine the exit IP", 5);
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Auto, quiet: true);
        _sb.Env("CLEARCOTE_NO_WARN", "1");
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Auto, quiet: false);
        Assert.Equal("", _err.ToString());
    }

    [Fact]
    public async Task Auto_does_not_cache_a_failed_no_proxy_lookup()
    {
        _answer = new GeoResult(null, "timed out after 10s", 5);
        await GeoIp.ApplyModeAsync(new FingerprintOptions(), null, GeoipMode.Auto, quiet: true);
        _answer = new GeoResult(Geo, null, 5);
        var fp = new FingerprintOptions();
        await GeoIp.ApplyModeAsync(fp, null, GeoipMode.Auto, quiet: true);
        Assert.Equal(2, _asked.Count);
        Assert.Equal("Europe/Berlin", fp.Timezone);
    }

    [Fact]
    public async Task Strict_still_fails_closed()
    {
        _answer = new GeoResult(null, "could not determine the exit IP", 5);
        await Assert.ThrowsAsync<GeoipException>(() => GeoIp.ApplyModeAsync(new FingerprintOptions(), Proxy, GeoipMode.Strict, quiet: true));
        Assert.Equal(new int?[] { null }, _timeouts);   // Strict keeps the 20 s CLEARCOTE_GEOIP_TIMEOUT_SECONDS budget
    }

    [Fact]
    public async Task Strict_never_marks_the_WebRTC_IP_as_the_defaults()
    {
        var fp = new FingerprintOptions();
        var r = await GeoIp.ApplyModeAsync(fp, Proxy, GeoipMode.Strict, quiet: true);
        Assert.Equal("203.0.113.7", fp.WebrtcIp);
        Assert.False(r.AutoWebrtcIp);
    }

    [Fact]
    public async Task Off_asks_nothing()
    {
        var fp = new FingerprintOptions();
        await GeoIp.ApplyModeAsync(fp, Proxy, GeoipMode.Off, quiet: true);
        Assert.Empty(_asked);
        Assert.Null(fp.Timezone);
    }

    [Fact]
    public void DropAutoWebrtcIp_drops_a_default_filled_WebRTC_IP_only_on_an_open_build_launch_through_a_proxy()
    {
        FingerprintOptions Fp() => new() { Timezone = "Europe/Berlin", WebrtcIp = "203.0.113.7" };
        GeoIp.GeoipApplied Auto(bool auto) => new(null, auto, Task.CompletedTask);
        var f = Fp(); GeoIp.DropAutoWebrtcIp(f, Auto(true), Proxy, licensed: false); Assert.Null(f.WebrtcIp);
        Assert.Equal("Europe/Berlin", f.Timezone);
        f = Fp(); GeoIp.DropAutoWebrtcIp(f, Auto(true), Proxy, licensed: true); Assert.Equal("203.0.113.7", f.WebrtcIp);
        f = Fp(); GeoIp.DropAutoWebrtcIp(f, Auto(true), null, licensed: false); Assert.Equal("203.0.113.7", f.WebrtcIp);
        f = Fp(); GeoIp.DropAutoWebrtcIp(f, Auto(false), Proxy, licensed: false); Assert.Equal("203.0.113.7", f.WebrtcIp);
    }

    [Fact]
    public async Task A_launch_path_applies_the_default_and_the_open_build_drop_after_the_binary()
    {
        // PrepareLaunchAsync (every native launch path) runs the default; DropAutoWebrtcIp then judges the licence.
        _sb.Env(GeoIp.GeoipEnv, null).Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_OFFLINE_KEY", null);
        _sb.TempHome();   // never this machine's own saved licence key
        var prepared = await Clearcote.PrepareLaunchAsync(new LaunchOptions { Proxy = Proxy, Quiet = true });
        Assert.Equal("Europe/Berlin", prepared.Options.Timezone);
        Assert.Equal("203.0.113.7", prepared.Options.WebrtcIp);
        Clearcote.DropAutoWebrtcIp(prepared.Options, prepared.Geo);
        Assert.Null(prepared.Options.WebrtcIp);   // open build + proxy
        Assert.Equal("Europe/Berlin", prepared.Options.Timezone);

        var keyed = await Clearcote.PrepareLaunchAsync(new LaunchOptions { Proxy = Proxy, Quiet = true, LicenseKey = "cc_lic_geoip_test_key_1234" });
        Clearcote.DropAutoWebrtcIp(keyed.Options, keyed.Geo);
        Assert.Equal("203.0.113.7", keyed.Options.WebrtcIp);   // the PRO build blocks non-proxied WebRTC UDP

        var off = await Clearcote.PrepareLaunchAsync(new LaunchOptions { Proxy = Proxy, Quiet = true, Geoip = false });
        Assert.Null(off.Options.Timezone);
        _sb.Env(GeoIp.GeoipEnv, "0");
        var envOff = await Clearcote.PrepareLaunchAsync(new LaunchOptions { Proxy = Proxy, Quiet = true });
        Assert.Null(envOff.Options.Timezone);
        Assert.Equal(2, _asked.Count);
    }
}
