using System.Diagnostics;
using System.Text;
using Xunit;

namespace Clearcote.Tests;

// Network parity with the Node SDK (test/parity-network.test.ts): proxy normalisation, geoip through
// HTTP/SOCKS5 proxies with a whole-resolution budget and fail-closed launch, licence seats, and
// licence calls through the launch proxy. Every server is local; nothing touches the internet.
public class ParityNetworkTests : IDisposable
{
    private readonly string[] _savedEcho = GeoIp.IpEchoUrls;
    private readonly string _savedApi = GeoIp.IpApiUrl;
    private readonly Func<string, Stopwatch, int, bool, GeoIp.DbInfo, Task<GeoInfo?>> _savedDb = GeoIp.DbLookup;
    private readonly string? _savedIpApi = Environment.GetEnvironmentVariable(GeoIp.IpApiEnv);
    private readonly string? _savedEchoEnv = Environment.GetEnvironmentVariable(GeoIp.EchoUrlEnv);

    public ParityNetworkTests()
    {
        // Never reach the real services from a unit test: default to an unroutable local port.
        GeoIp.IpEchoUrls = new[] { "http://127.0.0.1:1/ip" };
        GeoIp.IpApiUrl = "http://127.0.0.1:1/json";
        // ...and never download the real database: the stand-in knows one exit IP.
        GeoIp.DbLookup = (ip, _, _, _, _) => Task.FromResult<GeoInfo?>(
            ip == "203.0.113.7" ? new GeoInfo(ip, "DE", "Europe/Berlin", "de-DE", "52.5,13.4") : null);
        Environment.SetEnvironmentVariable(GeoIp.IpApiEnv, null);
        Environment.SetEnvironmentVariable(GeoIp.EchoUrlEnv, null);
    }

    public void Dispose()
    {
        GeoIp.IpEchoUrls = _savedEcho;
        GeoIp.IpApiUrl = _savedApi;
        GeoIp.DbLookup = _savedDb;
        Environment.SetEnvironmentVariable(GeoIp.IpApiEnv, _savedIpApi);
        Environment.SetEnvironmentVariable(GeoIp.EchoUrlEnv, _savedEchoEnv);
    }

    private static string Basic(string user, string pass) => "Basic " + Convert.ToBase64String(Encoding.UTF8.GetBytes($"{user}:{pass}"));

    private const string GeoJson = "{\"status\":\"success\",\"countryCode\":\"DE\",\"timezone\":\"Europe/Berlin\",\"lat\":52.5,\"lon\":13.4,\"query\":\"203.0.113.7\"}";

    /// A local origin answering as an IP echo (any path with "echo") and as ip-api (anything else).
    private static TestOrigin GeoOrigin() => new(r => (200, r.Url.Contains("echo") ? "203.0.113.7" : GeoJson));

    // ── ProxySpec ────────────────────────────────────────────────────────────

    [Fact]
    public void ProxySpec_splits_inline_credentials_and_fills_default_ports()
    {
        var a = ProxySpec.From("socks5://us%40er:p%3Ass@h.test")!;
        Assert.Equal("socks5://h.test:1080", a.ServerString);
        Assert.Equal("us@er", a.Username);
        Assert.Equal("p:ss", a.Password);
        Assert.Equal(new ProxySpec(new Uri("http://proxy.test:3128")), ProxySpec.From("proxy.test:3128"));
        var b = ProxySpec.From(new ProxyOptions { Server = "http://h:8080", Username = "u", Password = "p" })!;
        Assert.Equal(("http://h:8080", "u", "p"), (b.ServerString, b.Username, b.Password));
        Assert.Equal("socks5://h:9", ProxySpec.From("socks5h://h:9")!.ServerString);
        // IPv6: bracketed exactly once, in both the URI and the display string.
        var v6 = ProxySpec.From("socks5://u:p@[::1]:1081")!;
        Assert.Equal("socks5://[::1]:1081", v6.ServerString);
        Assert.Equal("[::1]", v6.Server.Host);
        Assert.Equal("http://[2001:db8::2]:80", ProxySpec.From("[2001:db8::2]")!.ServerString);
        Assert.Null(ProxySpec.From((string?)null));
        Assert.Null(ProxySpec.From(new ProxyOptions()));
    }

    // ── geoip budget + fail-closed ───────────────────────────────────────────

    [Fact]
    public void Geoip_timeout_reads_env_defaulting_to_20s_and_ignoring_junk()
    {
        using var s = new Sandbox().Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", null);
        Assert.Equal(20_000, GeoIp.TimeoutMs());
        Assert.Equal(7000, GeoIp.TimeoutMs("7"));
        Assert.Equal(1500, GeoIp.TimeoutMs("1.5"));
        Assert.Equal(20_000, GeoIp.TimeoutMs("0"));
        Assert.Equal(20_000, GeoIp.TimeoutMs("abc"));
        s.Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "3");
        Assert.Equal(3000, GeoIp.TimeoutMs());
    }

    [Fact]
    public async Task Geoip_resolves_through_an_authenticated_socks5_proxy()
    {
        await using var api = GeoOrigin();
        await using var socks = new TestSocks5("alice", "s3cret");
        GeoIp.IpEchoUrls = new[] { $"http://localhost:{api.Port}/echo" };
        var r = await GeoIp.ResolveDetailedAsync(new ProxyOptions { Server = $"socks5://alice:s3cret@127.0.0.1:{socks.Port}" }, quiet: true, timeoutMs: 5000);
        Assert.Equal(new GeoInfo("203.0.113.7", "DE", "Europe/Berlin", "de-DE", "52.5,13.4"), r.Geo);
        Assert.Null(r.Reason);
        // Hostname sent to the proxy, not resolved locally; credentials checked by the proxy.
        Assert.Equal(new[] { new SocksHit("localhost", api.Port, "alice") }, socks.Log.ToArray());
        Assert.Single(api.Log);
    }

    [Fact]
    public async Task Geoip_resolves_through_an_authenticated_http_proxy_and_fills_only_unset_fields()
    {
        await using var api = GeoOrigin();
        await using var proxy = new TestHttpProxy(requireAuth: Basic("u", "p"));
        GeoIp.IpEchoUrls = new[] { $"http://127.0.0.1:{api.Port}/echo" };
        var fp = new FingerprintOptions { AcceptLanguage = "fr-FR,fr" };
        await GeoIp.ApplyAsync(fp, new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}", Username = "u", Password = "p" }, quiet: true, timeoutMs: 5000);
        Assert.Equal("Europe/Berlin", fp.Timezone);
        Assert.Equal("fr-FR,fr", fp.AcceptLanguage);  // explicit value kept
        Assert.Equal("52.5,13.4", fp.Location);
        Assert.Equal("203.0.113.7", fp.WebrtcIp);
        Assert.Contains(proxy.Log, h => h.Kind == "absolute" && h.Target == GeoIp.IpEchoUrls[0] && h.Auth == Basic("u", "p"));
        Assert.Single(api.Log);
    }

    [Fact]
    public async Task Geoip_never_asks_ip_api_unless_CLEARCOTE_GEOIP_IPAPI_opts_in()
    {
        // ip-api.com's free endpoint is for non-commercial use only (45 requests/min per IP): it is not in the
        // default chain any more. Without the database there is no answer, and the reason says how to get one.
        await using var api = GeoOrigin();
        GeoIp.IpEchoUrls = new[] { $"http://127.0.0.1:{api.Port}/echo" };
        GeoIp.IpApiUrl = $"http://127.0.0.1:{api.Port}/json";
        GeoIp.DbLookup = (_, _, _, _, info) => { info.Db = "downloading"; return Task.FromResult<GeoInfo?>(null); };
        var r = await GeoIp.ResolveDetailedAsync(null, quiet: true, timeoutMs: 5000);
        Assert.Null(r.Geo);
        Assert.Contains("still downloading", r.Reason);
        Assert.Contains("CLEARCOTE_GEOIP_IPAPI=1", r.Reason);
        Assert.Equal(new[] { "/echo" }, api.Log.Select(x => x.Url).ToArray());
        Environment.SetEnvironmentVariable(GeoIp.IpApiEnv, "1");
        r = await GeoIp.ResolveDetailedAsync(null, quiet: true, timeoutMs: 5000);
        Assert.Null(r.Reason);
        Assert.Equal("Europe/Berlin", r.Geo?.Timezone);
        Assert.Equal(new[] { "/echo", "/echo", "/json" }, api.Log.Select(x => x.Url).ToArray());
    }

    [Fact]
    public async Task Geoip_echo_url_setting_replaces_the_public_echoes()
    {
        await using var api = GeoOrigin();
        GeoIp.IpEchoUrls = new[] { "http://127.0.0.1:1/never" };
        Environment.SetEnvironmentVariable(GeoIp.EchoUrlEnv, $" http://127.0.0.1:{api.Port}/myecho ,");
        var seen = new List<string>();
        GeoIp.DbLookup = (ip, _, _, _, _) =>
        {
            seen.Add(ip);
            return Task.FromResult<GeoInfo?>(new GeoInfo(ip, "NL", "Europe/Amsterdam", "nl-NL", null));
        };
        var r = await GeoIp.ResolveDetailedAsync(null, quiet: true, timeoutMs: 5000);
        Assert.Null(r.Reason);
        Assert.Equal("Europe/Amsterdam", r.Geo?.Timezone);
        Assert.Equal(new[] { "203.0.113.7" }, seen.ToArray());
        Assert.Equal(new[] { "/myecho" }, api.Log.Select(x => x.Url).ToArray());
    }

    [Fact]
    public void Geoip_echo_settings_and_answers()
    {
        GeoIp.IpEchoUrls = _savedEcho;  // the shipped defaults, not the constructor's unroutable stand-in
        var none = new Dictionary<string, string?>();
        Assert.Equal(new[] { "https://api.ipify.org", "https://icanhazip.com" }, GeoIp.EchoUrls(none));
        Assert.DoesNotContain(GeoIp.EchoUrls(none), u => u.Contains("ip-api"));
        Assert.Equal(new[] { "http://a/ip", "http://b/ip" }, GeoIp.EchoUrls(new Dictionary<string, string?> { [GeoIp.EchoUrlEnv] = "http://a/ip, http://b/ip" }));
        Assert.Equal(GeoIp.IpEchoUrls, GeoIp.EchoUrls(new Dictionary<string, string?> { [GeoIp.EchoUrlEnv] = " , " }));
        Assert.False(GeoIp.IpApiEnabled(none));
        foreach (var v in new[] { "1", "true", "YES", "on" })
            Assert.True(GeoIp.IpApiEnabled(new Dictionary<string, string?> { [GeoIp.IpApiEnv] = v }));
        Assert.False(GeoIp.IpApiEnabled(new Dictionary<string, string?> { [GeoIp.IpApiEnv] = "0" }));
        Assert.Equal("203.0.113.7", GeoIp.IpFromEcho("203.0.113.7\n"));
        Assert.Equal("2001:db8::1", GeoIp.IpFromEcho("{\"ip\": \"2001:db8::1\"}"));
        Assert.Equal("203.0.113.8", GeoIp.IpFromEcho("{\"query\": \"203.0.113.8\", \"status\": \"success\"}"));
        Assert.Equal("", GeoIp.IpFromEcho("{not json"));
    }

    [Fact]
    public async Task Geoip_database_lookup_reads_the_real_database_when_asked()
    {
        // Live, opt-in (downloads ~52 MB once): CLEARCOTE_LIVE_GEOIP=1. Proves the MaxMind.Db reader and the
        // geoip-all-in-one record shape against the real file, for the address this machine is seen from.
        if (Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_GEOIP") != "1") return;
        GeoIp.IpEchoUrls = _savedEcho;
        GeoIp.DbLookup = _savedDb;
        var r = await GeoIp.ResolveDetailedAsync(null, quiet: false, timeoutMs: 180_000);
        Assert.Null(r.Reason);
        Assert.False(string.IsNullOrEmpty(r.Geo?.Timezone));
        Assert.Matches(@"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$", r.Geo?.Location ?? "");
        Console.Error.WriteLine($"[live geoip] {r.Geo}");
    }

    [Fact]
    public async Task Geoip_reports_why_it_failed_within_budget_through_a_dead_proxy()
    {
        var sw = Stopwatch.StartNew();
        var r = await GeoIp.ResolveDetailedAsync(new ProxyOptions { Server = "socks5://127.0.0.1:1" }, quiet: true, timeoutMs: 1500);
        Assert.Null(r.Geo);
        Assert.Matches("exit IP through the proxy|timed out", r.Reason);
        Assert.True(sw.ElapsedMilliseconds < 5000, $"{sw.ElapsedMilliseconds}ms");
    }

    [Fact]
    public async Task Geoip_against_a_silent_proxy_stops_at_the_budget()
    {
        await using var silent = new SilentServer();
        var sw = Stopwatch.StartNew();
        var r = await GeoIp.ResolveDetailedAsync(new ProxyOptions { Server = $"http://127.0.0.1:{silent.Port}" }, quiet: true, timeoutMs: 1500);
        var ms = sw.ElapsedMilliseconds;
        Assert.Null(r.Geo);
        Assert.Contains("timed out after 2s (CLEARCOTE_GEOIP_TIMEOUT_SECONDS)", r.Reason);
        Assert.InRange(ms, 1300, 4000);
    }

    [Fact]
    public async Task ApplyGeoip_throws_GeoipException_instead_of_launching_on_the_host_clock()
    {
        var fp = new FingerprintOptions();
        var e = await Assert.ThrowsAsync<GeoipException>(() => GeoIp.ApplyAsync(fp, new ProxyOptions { Server = "socks5://127.0.0.1:1" }, quiet: true, timeoutMs: 2000));
        Assert.Equal("GEOIP_UNRESOLVED", e.Code);
        Assert.Contains("proxy's region", e.Message);
        Assert.Null(fp.Timezone);
    }

    [Fact]
    public async Task ApplyGeoip_still_launches_when_BOTH_timezone_and_acceptLanguage_are_explicit()
    {
        using var err = new StderrCapture();
        var fp = new FingerprintOptions { Timezone = "Europe/Paris", AcceptLanguage = "fr-FR,fr" };
        await GeoIp.ApplyAsync(fp, new ProxyOptions { Server = "socks5://127.0.0.1:1" }, quiet: false, timeoutMs: 2000);
        Assert.Equal(("Europe/Paris", "fr-FR,fr", (string?)null, (string?)null), (fp.Timezone, fp.AcceptLanguage, fp.Location, fp.WebrtcIp));
        Assert.Contains("using the explicit timezone and acceptLanguage", err.Text);
    }

    [Fact]
    public async Task ApplyGeoip_with_only_one_explicit_still_fails_closed()
        => await Assert.ThrowsAsync<GeoipException>(() =>
            GeoIp.ApplyAsync(new FingerprintOptions { Timezone = "Europe/Paris" }, new ProxyOptions { Server = "socks5://127.0.0.1:1" }, quiet: true, timeoutMs: 2000));

    [Fact]
    public async Task Launch_with_geoip_fails_closed_before_the_binary_is_even_resolved()
    {
        // The executable does not exist: reaching binary resolution would throw FileNotFound instead.
        using var s = new Sandbox().Env("CLEARCOTE_GEOIP_TIMEOUT_SECONDS", "2");
        var opts = new LaunchOptions
        {
            Geoip = true, Quiet = true, ExecutablePath = "/definitely/not/here/chrome",
            Proxy = new ProxyOptions { Server = "http://127.0.0.1:1" },
        };
        await Assert.ThrowsAsync<GeoipException>(() => Clearcote.LaunchAsync(opts));
        await Assert.ThrowsAsync<GeoipException>(() => Clearcote.LaunchEphemeralProfileAsync(opts));
        await Assert.ThrowsAsync<GeoipException>(() => Clearcote.ServeAsync(new ServeOptions { Geoip = true, Quiet = true, Proxy = opts.Proxy }));
        Assert.Null(opts.Timezone);  // the caller's options are never mutated

        // Control: with both explicit values the geoip step passes and the launch proceeds to (and
        // fails at) binary resolution.
        opts.Timezone = "Asia/Tokyo";
        opts.AcceptLanguage = "ja-JP";
        await Assert.ThrowsAnyAsync<Exception>(async () =>
        {
            try { await Clearcote.LaunchAsync(opts); }
            catch (GeoipException) { Assert.Fail("geoip must not fail closed when both values are explicit"); }
        });
    }

    [Fact]
    public async Task Prepare_applies_geoip_to_a_copy()
    {
        await using var api = GeoOrigin();
        await using var socks = new TestSocks5();
        GeoIp.IpEchoUrls = new[] { $"http://127.0.0.1:{api.Port}/echo" };
        var opts = new ServeOptions { Geoip = true, Quiet = true, Port = 1234, Proxy = new ProxyOptions { Server = $"socks5://127.0.0.1:{socks.Port}" } };
        var copy = await Clearcote.PrepareAsync(opts);
        Assert.IsType<ServeOptions>(copy);
        Assert.Equal(1234, copy.Port);
        Assert.Equal("Europe/Berlin", copy.Timezone);
        Assert.Null(opts.Timezone);
        Assert.Single(socks.Log);
    }

    // ── licence seats + licence through proxy ────────────────────────────────

    private static Sandbox LicenseSandbox()
    {
        var s = new Sandbox()
            .Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null)
            .Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_INSTANCE_ID", "test-instance");
        s.TempHome();
        return s;
    }

    [Fact]
    public void LicenseThroughProxy_option_wins_else_env()
    {
        Assert.False(License.LicenseThroughProxyRequested(null, ""));
        Assert.True(License.LicenseThroughProxyRequested(null, "1"));
        Assert.True(License.LicenseThroughProxyRequested(null, "Yes"));
        Assert.False(License.LicenseThroughProxyRequested(false, "true"));
        Assert.True(License.LicenseThroughProxyRequested(true, ""));
    }

    [Fact]
    public async Task Seats_ok_invalid_older_server_unreachable_no_key_never_throws()
    {
        using var s = LicenseSandbox();
        await using var api = new TestOrigin(req =>
        {
            var auth = req.Header("authorization");
            if (req.Url != "/api/v1/lease/seats") return (404, "{\"error\":\"not found\"}");
            if (auth == "Bearer cc_lic_good") return (200, "{\"used\":2,\"limit\":5,\"plan\":\"team\"}");
            if (auth == "Bearer cc_lic_unl") return (200, "{\"used\":1,\"limit\":null,\"plan\":\"pro\"}");
            return (401, "{\"error\":\"Invalid license key.\"}");
        });
        var b = $"http://127.0.0.1:{api.Port}";
        Assert.Equal(new SessionSeats(SeatsState.Ok, 2, 5, "team"), await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_good", LicenseApiBase = b }));
        Assert.Equal(new SessionSeats(SeatsState.Ok, 1, null, "pro"), await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_unl", LicenseApiBase = b }));
        Assert.Equal(new SessionSeats(SeatsState.Invalid, Reason: "Invalid license key."), await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_bad", LicenseApiBase = b }));

        await using var old = new TestOrigin(_ => (404, "{}"));
        Assert.Equal(new SessionSeats(SeatsState.Unavailable, Reason: "this licence server does not report seats yet"),
            await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_good", LicenseApiBase = $"http://127.0.0.1:{old.Port}" }));
        var down = await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_good", LicenseApiBase = "http://127.0.0.1:1" });
        Assert.Equal(SeatsState.Unavailable, down.State);
        Assert.StartsWith("licence server unreachable", down.Reason);
        Assert.Equal(new SessionSeats(SeatsState.NoKey), await License.GetSessionSeatsAsync(new LicenseOptions()));
    }

    [Fact]
    public async Task Seats_go_through_the_proxy_only_when_licenseThroughProxy_is_on()
    {
        using var s = LicenseSandbox();
        await using var api = new TestOrigin(_ => (200, "{\"used\":0,\"limit\":3}"));
        await using var proxy = new TestHttpProxy();
        var b = $"http://127.0.0.1:{api.Port}";
        var px = new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" };
        Assert.Equal(SeatsState.Ok, (await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "k", LicenseApiBase = b }, px)).State);
        Assert.Empty(proxy.Log);
        Assert.Equal(SeatsState.Ok, (await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "k", LicenseApiBase = b, LicenseThroughProxy = true }, px)).State);
        Assert.Equal(new[] { $"{b}/api/v1/lease/seats" }, proxy.Log.Select(l => l.Target));
        s.Env("CLEARCOTE_LICENSE_THROUGH_PROXY", "1");
        await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "k", LicenseApiBase = b }, px);
        Assert.Equal(2, proxy.Log.Count);
    }

    [Fact]
    public async Task AcquireLease_checkout_goes_through_socks5_with_licenseThroughProxy_and_direct_without()
    {
        using var s = LicenseSandbox();
        await using var api = new TestOrigin(req => (200,
            $"{{\"lease_id\":\"L-{req.Url}\",\"token\":\"tok\",\"exp\":{DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600},\"heartbeat_interval_sec\":3600,\"concurrency\":{{\"used\":1,\"limit\":5}}}}"));
        await using var socks = new TestSocks5();
        var b = $"http://127.0.0.1:{api.Port}";
        var proxy = new ProxyOptions { Server = $"socks5://127.0.0.1:{socks.Port}" };

        var viaProxy = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = $"cc_lic_proxy_{Guid.NewGuid():N}", LicenseApiBase = b, LicenseThroughProxy = true }, quiet: true, proxy: proxy);
        Assert.Equal("tok", viaProxy!.Token);
        Assert.Equal(new[] { new SocksHit("127.0.0.1", api.Port, null) }, socks.Log.ToArray());
        Assert.Equal("/api/v1/lease/checkout", api.Log.Last().Url);

        var direct = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = $"cc_lic_direct_{Guid.NewGuid():N}", LicenseApiBase = b }, quiet: true, proxy: proxy);
        Assert.Equal("tok", direct!.Token);
        Assert.Single(socks.Log);  // unchanged: the second checkout did not use the proxy
        Assert.Equal(2, api.Log.Count);
    }

    [Fact]
    public async Task AcquireLease_keeps_direct_and_proxied_leases_for_one_key_apart()
    {
        using var s = LicenseSandbox();
        await using var api = new TestOrigin(req => (200,
            $"{{\"lease_id\":\"L\",\"token\":\"tok\",\"exp\":{DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600},\"heartbeat_interval_sec\":3600}}"));
        await using var proxy = new TestHttpProxy();
        var key = $"cc_lic_split_{Guid.NewGuid():N}";
        var b = $"http://127.0.0.1:{api.Port}";
        var px = new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" };
        var home = Environment.GetEnvironmentVariable("HOME")!;

        await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = key, LicenseApiBase = b }, quiet: true, proxy: px);
        Assert.Empty(proxy.Log);
        // The on-disk run-token cache would satisfy a second checkout without any network call;
        // clear it so the proxied route has to check out on its own.
        foreach (var f in Directory.GetFiles(Path.Combine(home, ".clearcote"), "lease-*.json")) File.Delete(f);
        await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = key, LicenseApiBase = b, LicenseThroughProxy = true }, quiet: true, proxy: px);
        Assert.Single(proxy.Log);
        Assert.Equal(2, api.Log.Count);

        // Same proxy server, different proxy username (a different exit on session proxies): its own lease.
        foreach (var f in Directory.GetFiles(Path.Combine(home, ".clearcote"), "lease-*.json")) File.Delete(f);
        var pxUser = new ProxyOptions { Server = px.Server, Username = "session-2", Password = "x" };
        await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = key, LicenseApiBase = b, LicenseThroughProxy = true }, quiet: true, proxy: pxUser);
        Assert.Equal(3, api.Log.Count);
        // ...and the same route again reuses it in-process (no further checkout).
        await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = key, LicenseApiBase = b, LicenseThroughProxy = true }, quiet: true, proxy: pxUser);
        Assert.Equal(3, api.Log.Count);
    }

    // A rotating proxy changes the exit per connection (mirrors the Python and Node egress-drift tests).
    [Fact]
    public async Task Egress_drift_detects_a_rotating_exit_through_the_proxy()
    {
        var ip = "203.0.113.7";
        await using var origin = new TestOrigin(_ => (200, ip));
        await using var proxy = new TestHttpProxy();
        GeoIp.IpEchoUrls = new[] { $"http://localhost:{origin.Port}/echo" };
        var spec = new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" };

        Assert.Null(await GeoIp.CheckEgressDriftAsync(spec, "203.0.113.7"));   // sticky: same exit
        ip = "203.0.113.99";
        Assert.Equal("203.0.113.99", await GeoIp.CheckEgressDriftAsync(spec, "203.0.113.7"));
        Assert.Null(await GeoIp.CheckEgressDriftAsync(spec, "2001:db8::1"));   // other family
        Assert.Null(await GeoIp.CheckEgressDriftAsync(null, "203.0.113.7"));   // no proxy
        Assert.Equal(3, proxy.Log.Count);
    }

    [Fact]
    public void Egress_drift_message_names_both_exits_and_the_fix()
    {
        var m = GeoIp.EgressDriftMessage("203.0.113.7", "203.0.113.99");
        Assert.Contains("203.0.113.7 -> 203.0.113.99", m);
        Assert.Contains("sticky session", m);
    }
}
