using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

// The cloud client (Cloud.cs) and cloud launch (CloudLaunch.cs) against an in-memory hosted API: option
// mapping, local-only option rejection, env switching, CloudException mapping, every resource, run
// polling, webhook signatures and cookie selection. Offline: the only server is FakeCloud, and the
// browser is a stand-in. Mirrors sdk/node/test/cloud.test.ts and sdk/python/tests/test_cloud.py.
public sealed class CloudTests : IAsyncLifetime
{
    private readonly Sandbox _sb = new();
    private readonly FakeCloud _api = new();
    private readonly string _tmp = TestTemp.Create("cc-cloud-");

    public Task InitializeAsync()
    {
        _sb.Env("CLEARCOTE_API_KEY", FakeCloud.ApiKey).Env("CLEARCOTE_API_URL", _api.Url).Env("CLEARCOTE_CLOUD", null);
        return Task.CompletedTask;
    }

    public async Task DisposeAsync()
    {
        CloudLaunch.ConnectOverride = null;
        await _api.DisposeAsync();
        _sb.Dispose();
        TestTemp.Remove(_tmp);
    }

    /// JSON with object keys sorted at every level, so two bodies compare regardless of key order.
    private static string Canon(JsonNode? n) => n switch
    {
        JsonObject o => "{" + string.Join(",", o.OrderBy(p => p.Key, StringComparer.Ordinal).Select(p => JsonSerializer.Serialize(p.Key) + ":" + Canon(p.Value))) + "}",
        JsonArray a => "[" + string.Join(",", a.Select(Canon)) + "]",
        null => "null",
        _ => n.ToJsonString(),
    };

    private static void AssertJson(string expected, JsonNode? actual) => Assert.Equal(Canon(JsonNode.Parse(expected)), Canon(actual));

    // ── a stand-in Playwright browser ──────────────────────────────────────────────────────────────

    public class FakeBrowser : DispatchProxy
    {
        public bool Closed;
        public bool ThrowOnContexts;
        public readonly List<object?> NewPageOptions = new();
        public IBrowserContext Context = null!;
        private EventHandler<IBrowser>? _disconnected;

        public static (IBrowser Browser, FakeBrowser Fake) Make()
        {
            var b = Create<IBrowser, FakeBrowser>();
            var f = (FakeBrowser)(object)b;
            f.Context = FakeContext.Make();
            return (b, f);
        }

        protected override object? Invoke(MethodInfo? m, object?[]? args)
        {
            switch (m!.Name)
            {
                case "get_Contexts":
                    if (ThrowOnContexts) throw new InvalidOperationException("target crashed");
                    return new List<IBrowserContext> { Context };
                case "get_IsConnected": return !Closed;
                case "get_Version": return "fake";
                case "CloseAsync":
                    Closed = true;
                    _disconnected?.Invoke(this, (IBrowser)(object)this);
                    return Task.CompletedTask;
                case "NewPageAsync":
                    NewPageOptions.Add(args![0]);
                    return Task.FromResult<IPage>(null!);
                case "NewContextAsync":
                    NewPageOptions.Add(args![0]);
                    return Task.FromResult(Context);
                case "add_Disconnected": _disconnected += (EventHandler<IBrowser>)args![0]!; return null;
                case "remove_Disconnected": _disconnected -= (EventHandler<IBrowser>)args![0]!; return null;
                default: throw new NotSupportedException(m.Name);
            }
        }
    }

    public class FakeContext : DispatchProxy
    {
        public bool Closed;
        public static IBrowserContext Make() => Create<IBrowserContext, FakeContext>();

        protected override object? Invoke(MethodInfo? m, object?[]? args) => m!.Name switch
        {
            "get_Pages" => new List<IPage>(),
            "CloseAsync" => Close(),
            _ => throw new NotSupportedException(m.Name),
        };

        private Task Close() { Closed = true; return Task.CompletedTask; }
    }

    private (FakeBrowser Fake, List<(string Url, BrowserTypeConnectOverCDPOptions Options)> Calls) StandIn()
    {
        var (browser, fake) = FakeBrowser.Make();
        var calls = new List<(string, BrowserTypeConnectOverCDPOptions)>();
        CloudLaunch.ConnectOverride = (url, o) => { calls.Add((url, o)); return Task.FromResult(browser); };
        return (fake, calls);
    }

    // ── local or cloud ─────────────────────────────────────────────────────────────────────────────

    [Theory]
    [InlineData("1", true)] [InlineData("true", true)] [InlineData("YES", true)] [InlineData(" yes ", true)]
    [InlineData("0", false)] [InlineData("", false)] [InlineData("no", false)] [InlineData("cloud", false)]
    public void Cloud_follows_CLEARCOTE_CLOUD(string value, bool expected)
    {
        _sb.Env("CLEARCOTE_CLOUD", value);
        Assert.Equal(expected, CloudLaunch.Requested(new LaunchOptions()));
        Assert.Equal(expected, CloudLaunch.Requested(null));
    }

    [Fact]
    public void An_explicit_flag_beats_the_environment()
    {
        _sb.Env("CLEARCOTE_CLOUD", "1");
        Assert.False(CloudLaunch.Requested(new LaunchOptions { Cloud = false }));
        Assert.False(CloudLaunch.Requested(new LaunchOptions { Cloud = false, CloudClient = new Cloud() }));
        _sb.Env("CLEARCOTE_CLOUD", null);
        Assert.True(CloudLaunch.Requested(new LaunchOptions { Cloud = true }));
        Assert.True(CloudLaunch.Requested(new LaunchOptions { CloudClient = new Cloud() })); // a client means cloud
    }

    [Fact]
    public async Task LaunchAsync_follows_CLEARCOTE_CLOUD_and_Cloud_false_keeps_it_local()
    {
        var (_, calls) = StandIn();
        _sb.Env("CLEARCOTE_CLOUD", "1");
        await Clearcote.LaunchAsync(new LaunchOptions { Country = "us" });
        Assert.Single(calls);
        AssertJson("""{"country":"us"}""", _api.Requests("POST", "/api/v1/browsers")[0].Body);
        // local: fails on the missing binary, without ever talking to the API
        await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(new LaunchOptions
        {
            Cloud = false, ExecutablePath = Path.Combine(_tmp, "missing", "chrome"), ApiKey = "k", Quiet = true,
        }));
        Assert.Single(_api.Requests("POST"));
        Assert.Single(calls);
    }

    [Fact]
    public async Task Needs_an_API_key()
    {
        _sb.Env("CLEARCOTE_API_KEY", null);
        Assert.Contains("CLEARCOTE_API_KEY", Assert.Throws<InvalidOperationException>(() => new Cloud()).Message);
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Country = "us" }));
        Assert.Contains("CLEARCOTE_API_KEY", e.Message);
    }

    [Fact]
    public void Base_URL_default_env_argument_and_the_key_is_never_printed()
    {
        _sb.Env("CLEARCOTE_API_URL", null);
        Assert.Equal("https://www.clearcotelabs.com", new Cloud("k").BaseUrl);
        _sb.Env("CLEARCOTE_API_URL", "http://127.0.0.1:8480/");
        Assert.Equal("http://127.0.0.1:8480", new Cloud("k").BaseUrl);
        Assert.Equal("https://staging.example", new Cloud("k", "https://staging.example").BaseUrl);
        Assert.DoesNotContain("cc_live_secret", new Cloud("cc_live_secret").ToString());
    }

    [Theory]
    [InlineData("http://127.0.0.1:8480")] [InlineData("http://localhost:3000/")] [InlineData("http://[::1]:8480")]
    [InlineData("HTTP://LOCALHOST")] [InlineData("https://www.clearcotelabs.com")] [InlineData("https://10.0.0.5")]
    public void Accepts_the_base_URL(string url) => Assert.Equal(url.TrimEnd('/'), new Cloud("k", url).BaseUrl);

    [Theory]
    [InlineData("http://www.clearcotelabs.com")] [InlineData("http://10.0.0.5:8480")] [InlineData("http://127.0.0.2")]
    [InlineData("http://localhost.evil.com")] [InlineData("http://127.0.0.1.nip.io")] [InlineData("http://[::2]")]
    public async Task Refuses_plain_http_off_this_machine(string url)
    {
        Assert.Contains("unencrypted", Assert.Throws<ArgumentException>(() => new Cloud("k", url)).Message);
        _sb.Env("CLEARCOTE_API_URL", url);
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, ApiKey = "k" }));
        Assert.Contains("unencrypted", e.Message);
    }

    [Theory]
    [InlineData("ftp://example.com")] [InlineData("www.clearcotelabs.com")] [InlineData("https://")] [InlineData("https:example.com")]
    public void Refuses_a_non_web_URL(string url)
        => Assert.Contains("must start with https://", Assert.Throws<ArgumentException>(() => new Cloud("k", url)).Message);

    // ── option mapping ─────────────────────────────────────────────────────────────────────────────

    [Fact]
    public void Every_session_option_keeps_its_API_name()
    {
        var body = Cloud.SessionBody(new CloudSessionOptions
        {
            Fingerprint = "seed-1", Platform = "windows", Brand = "Chrome", Timezone = "Europe/Amsterdam", Locale = "nl-NL",
            Geoip = false, Headless = false, LightStealth = true, Country = "us", State = "ca", City = "los angeles",
            ProxySession = "sticky-1", TimeoutSec = 600, IdleTimeoutSec = 120, MaxGb = 0.5, Version = "153", Profile = "acct-1",
            Url = "https://example.com", Adblock = true, SolveSliders = false, SolveCheckboxes = false, KeepAlive = true, Record = true, Note = "n", Worker = "w1", Identity = "acct-1",
            Proxy = new ProxyOptions { Server = "managed" },
        }, run: false);
        AssertJson("""
            {"fingerprint":"seed-1","platform":"windows","brand":"Chrome","timezone":"Europe/Amsterdam","locale":"nl-NL",
             "geoip":false,"headless":false,"lightStealth":true,"country":"us","state":"ca","city":"los angeles",
             "proxySession":"sticky-1","timeoutSec":600,"idleTimeoutSec":120,"maxGb":0.5,"version":"153","profile":"acct-1",
             "url":"https://example.com","adblock":true,"solveSliders":false,"solveCheckboxes":false,"keepAlive":true,"record":true,"note":"n","worker":"w1","identity":"acct-1",
             "proxy":"managed"}
            """, body);
        AssertJson("{}", Cloud.SessionBody(new CloudSessionOptions(), run: false));
    }

    [Fact]
    public void LaunchOptions_map_onto_the_session_with_the_local_names()
    {
        var s = Cloud.SessionBody(CloudLaunch.SessionOptionsOf(new LaunchOptions
        {
            AcceptLanguage = "de-DE", Fingerprint = "f", Headless = true, Country = "de", Quiet = true, SlowMo = 5, ApiKey = "k",
            SolveSliders = false, SolveCheckboxes = false,
        }), run: false);
        AssertJson("""{"locale":"de-DE","fingerprint":"f","headless":true,"country":"de","solveSliders":false,"solveCheckboxes":false}""", s);
        // Geoip is a plain bool locally: off is "unset" (the server's own default applies), on is sent
        AssertJson("{}", Cloud.SessionBody(CloudLaunch.SessionOptionsOf(new LaunchOptions { Geoip = false }), run: false));
        AssertJson("""{"geoip":true}""", Cloud.SessionBody(CloudLaunch.SessionOptionsOf(new LaunchOptions { Geoip = true }), run: false));
    }

    public static IEnumerable<object[]> ProxyCases() => new[]
    {
        new object[] { new ProxyOptions { Server = "managed" }, "\"managed\"" },
        new object[] { new ProxyOptions { Server = "http://us%40er:p%3Ass@proxy.example:3128" }, """{"server":"http://proxy.example:3128","username":"us@er","password":"p:ss"}""" },
        new object[] { new ProxyOptions { Server = "socks5://proxy.example:1080" }, """{"server":"socks5://proxy.example:1080"}""" },
        new object[] { new ProxyOptions { Server = "http://proxy.example:8080", Username = "u", Password = "p" }, """{"server":"http://proxy.example:8080","username":"u","password":"p"}""" },
        new object[] { new ProxyOptions { Server = "http://a:b@proxy.example:8080" }, """{"server":"http://proxy.example:8080","username":"a","password":"b"}""" },
    };

    [Theory]
    [MemberData(nameof(ProxyCases))]
    public void Proxy_forms(ProxyOptions given, string sent)
        => AssertJson($$"""{"proxy":{{sent}}}""", Cloud.SessionBody(new CloudSessionOptions { Proxy = given }, run: false));

    [Fact]
    public void Refuses_a_proxy_bypass_list_and_junk()
    {
        var e = Assert.Throws<ArgumentException>(() => Cloud.SessionBody(new CloudSessionOptions { Proxy = new ProxyOptions { Server = "http://p:1", Bypass = "*.local" } }, run: false));
        Assert.Equal("Proxy.Bypass is not available for cloud browsers", e.Message);
        Assert.Contains("Proxy must", Assert.Throws<ArgumentException>(() => Cloud.SessionBody(new CloudSessionOptions { Proxy = new ProxyOptions() }, run: false)).Message);
        Assert.Contains("Proxy must", Assert.Throws<ArgumentException>(() => Cloud.SessionBody(new CloudSessionOptions { Proxy = new ProxyOptions { Server = "managed", Username = "u" } }, run: false)).Message);
    }

    [Fact]
    public void Profile_forms()
    {
        AssertJson("""{"profile":"acct-1"}""", Cloud.SessionBody(new CloudSessionOptions { Profile = "acct-1" }, run: false));
        AssertJson("""{"profile":{"name":"acct-1","persist":true}}""",
            Cloud.SessionBody(new CloudSessionOptions { Profile = new CloudProfile { Name = "acct-1", Persist = true } }, run: false));
        Assert.Contains("profile \"auto\"", Assert.Throws<ArgumentException>(() => Cloud.SessionBody(new CloudSessionOptions { Profile = "auto" }, run: false)).Message);
        Assert.Throws<ArgumentException>(() => Cloud.SessionBody(new CloudSessionOptions { Profile = " " }, run: false));
    }

    public static IEnumerable<object[]> LocalOnly() => new[]
    {
        new object[] { "ExecutablePath", new LaunchOptions { ExecutablePath = "/opt/chrome" } },
        new object[] { "Args", new LaunchOptions { Args = new[] { "--x" } } },
        new object[] { "Extensions", new LaunchOptions { Extensions = new[] { "/ext" } } },
        new object[] { "IgnoreDefaultArgs", new LaunchOptions { IgnoreDefaultArgs = Array.Empty<string>() } },
        new object[] { "GpuVendor", new LaunchOptions { GpuVendor = "NVIDIA" } },
        new object[] { "HardwareConcurrency", new LaunchOptions { HardwareConcurrency = 8 } },
        new object[] { "WebrtcIp", new LaunchOptions { WebrtcIp = "1.2.3.4" } },
        new object[] { "LicenseKey", new LaunchOptions { LicenseKey = "cc_lic_x" } },
        new object[] { "Env", new LaunchOptions { Env = new Dictionary<string, string>() } },
        new object[] { "Socks5Udp", new LaunchOptions { Socks5Udp = true } },
        new object[] { "AllowThirdPartyCookies", new LaunchOptions { AllowThirdPartyCookies = true } },
        new object[] { "ViewportSize", new LaunchOptions { ViewportSize = new ViewportSize { Width = 800, Height = 600 } } },
        new object[] { "FingerprintProfile", new LaunchOptions { FingerprintProfile = "x.json" } },
    };

    [Theory]
    [MemberData(nameof(LocalOnly))]
    public void Refuses_a_local_only_option_by_name(string name, LaunchOptions o)
        => Assert.Equal($"{name} is not available for cloud browsers", Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(o)).Message);

    [Fact]
    public void Every_LaunchOptions_property_is_classified()
    {
        var all = typeof(LaunchOptions).GetProperties(BindingFlags.Public | BindingFlags.Instance).Select(p => p.Name).ToHashSet();
        // the two hand-written lists name real properties (a rename would silently drop an option)...
        foreach (var n in CloudLaunch.SessionOptions.Concat(CloudLaunch.SdkSideOptions)) Assert.Contains(n, all);
        // ...and are disjoint from what is refused, which is everything else
        var local = CloudLaunch.LocalOnlyOptions.Select(p => p.Name).ToHashSet();
        Assert.Empty(local.Intersect(CloudLaunch.SessionOptions));
        Assert.Equal(all.Count, local.Count + CloudLaunch.SessionOptions.Length + CloudLaunch.SdkSideOptions.Length);
        foreach (var n in new[] { "ExecutablePath", "Args", "Extensions", "IgnoreDefaultArgs", "LicenseKey", "Env" }) Assert.Contains(n, local);
        // a default LaunchOptions has nothing local set
        CloudLaunch.SessionOptionsOf(new LaunchOptions());
    }

    [Fact]
    public async Task Launch_refuses_local_options_before_any_request()
    {
        var e1 = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, ExecutablePath = "/opt/chrome" }));
        Assert.Equal("ExecutablePath is not available for cloud browsers", e1.Message);
        var e2 = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchPersistentContextAsync("/tmp/x", new LaunchOptions { Cloud = true, Profile = "p" }));
        Assert.Contains("user data directory", e2.Message);
        var e3 = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true }));
        Assert.Contains("needs Profile = \"name\"", e3.Message);
        Assert.Empty(_api.Log);
    }

    [Fact]
    public async Task A_local_LaunchPersistentContext_still_needs_a_directory()
    {
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchPersistentContextAsync(new LaunchOptions()));
        Assert.Contains("needs a userDataDir", e.Message);
    }

    // ── LaunchAsync(Cloud = true) with a stand-in Playwright ───────────────────────────────────────

    [Fact]
    public async Task Creates_connects_and_closes()
    {
        _api.ConnectUrl = "wss://w1.example/v1/connect/bs_1?token=t";
        var (fake, calls) = StandIn();
        var b = await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Country = "us", Identity = "acct-1", SlowMo = 10 });
        Assert.Equal(_api.ConnectUrl, calls.Single().Url);
        Assert.Equal(10f, calls.Single().Options.SlowMo);
        AssertJson("""{"country":"us","identity":"acct-1"}""", _api.Requests("POST", "/api/v1/browsers")[0].Body);
        var info = Cloud.SessionOf(b)!;
        Assert.Equal("bs_1", Cloud.Str(info["id"]));
        Assert.Null(info["connectUrl"]);
        Assert.Null(Cloud.SessionOf(new object()));
        // no emulated viewport, as a local launch: on a default call, and when only other options are set
        await b.NewPageAsync();
        await b.NewPageAsync(new BrowserNewPageOptions { Locale = "nl-NL" });
        await b.NewPageAsync(new BrowserNewPageOptions { ViewportSize = new ViewportSize { Width = 640, Height = 480 } });
        await b.NewContextAsync();
        // NoViewport is a fresh { -1, -1 } on every access, so compare the values
        static bool None(ViewportSize? v) => v is { Width: -1, Height: -1 };
        var opts = fake.NewPageOptions;
        Assert.True(None(((BrowserNewPageOptions)opts[0]!).ViewportSize));
        Assert.True(None(((BrowserNewPageOptions)opts[1]!).ViewportSize));
        Assert.Equal("nl-NL", ((BrowserNewPageOptions)opts[1]!).Locale);
        Assert.Equal(640, ((BrowserNewPageOptions)opts[2]!).ViewportSize!.Width);   // an explicit viewport stays
        Assert.True(None(((BrowserNewContextOptions)opts[3]!).ViewportSize));
        Assert.Equal("fake", b.Version);   // everything else is Playwright's own object
        Assert.True(b.IsConnected);
        await b.CloseAsync();
        Assert.True(fake.Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));   // once, though Disconnected fired too
    }

    [Fact]
    public async Task Sends_the_key_and_the_SDK_user_agent()
    {
        StandIn();
        await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, ApiKey = FakeCloud.ApiKey, ApiUrl = _api.Url });
        var r = _api.Requests("POST", "/api/v1/browsers")[0];
        Assert.Equal($"Bearer {FakeCloud.ApiKey}", r.Header("authorization"));
        Assert.Equal($"clearcote-sdk-dotnet/{Clearcote.Version}", r.Header("user-agent"));
        Assert.StartsWith("application/json", r.Header("content-type"));
    }

    [Fact]
    public async Task A_failed_connect_ends_the_session()
    {
        CloudLaunch.ConnectOverride = (_, _) => throw new PlaywrightException("connect refused");
        var e = await Assert.ThrowsAsync<PlaywrightException>(() => Clearcote.LaunchAsync(new LaunchOptions { Cloud = true }));
        Assert.Equal("connect refused", e.Message);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
    }

    [Fact]
    public async Task A_failure_after_connecting_disconnects_and_ends_the_session_keepAlive_or_not()
    {
        var (fake, _) = StandIn();
        fake.ThrowOnContexts = true;
        var e = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "p", KeepAlive = true }));
        Assert.Equal("target crashed", e.Message);
        Assert.True(fake.Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
    }

    [Fact]
    public async Task A_keep_alive_session_is_left_running_on_close()
    {
        StandIn();
        var b = await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, KeepAlive = true });
        await b.CloseAsync();
        Assert.Empty(_api.Requests("DELETE"));
    }

    [Fact]
    public async Task DisposeAsync_ends_the_session_too()
    {
        var (fake, _) = StandIn();
        await using (await Clearcote.LaunchAsync(new LaunchOptions { Cloud = true })) { }
        Assert.True(fake.Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
    }

    [Fact]
    public async Task A_persistent_cloud_context_loads_and_saves_the_profile()
    {
        var (fake, _) = StandIn();
        var ctx = await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1" });
        AssertJson("""{"name":"acct-1","persist":true}""", _api.Requests("POST", "/api/v1/browsers")[0].Body!["profile"]);
        Assert.Equal("bs_1", Cloud.Str(Cloud.SessionOf(ctx)!["id"]));
        Assert.Empty(ctx.Pages);                         // the session's own context, through the proxy
        Assert.Equal("bs_1", Cloud.Str(Cloud.SessionOf(ctx.Browser!)!["id"]));
        await ctx.CloseAsync();                          // closes the browser, which ends (and saves) the session
        Assert.True(fake.Closed);
        Assert.False(((FakeContext)(object)fake.Context).Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));

        StandIn();
        await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = new CloudProfile { Name = "acct-2", Persist = false } });
        AssertJson("""{"name":"acct-2","persist":false}""", _api.Requests("POST", "/api/v1/browsers")[1].Body!["profile"]);
    }

    [Fact]
    public async Task Context_Browser_is_the_cloud_browser_so_closing_it_ends_the_session()
    {
        var (fake, _) = StandIn();
        var ctx = await Clearcote.LaunchPersistentContextAsync(new LaunchOptions { Cloud = true, Profile = "acct-1" });
        await ctx.Browser!.CloseAsync();
        Assert.True(fake.Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
    }

    [Fact]
    public async Task LaunchEphemeralProfile_moves_to_the_cloud_with_CLEARCOTE_CLOUD()
    {
        // .NET's recommended local entry point; flipping the env var must move it too, unedited
        var (fake, _) = StandIn();
        _sb.Env("CLEARCOTE_CLOUD", "yes");
        var ctx = await Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions { Note = "from env" });
        AssertJson("""{"note":"from env"}""", _api.Requests("POST", "/api/v1/browsers")[0].Body);   // no profile is forced
        await ctx.CloseAsync();
        Assert.True(fake.Closed);
        Assert.Single(_api.Requests("DELETE", "/api/v1/browsers/bs_1"));
    }

    // ── errors ─────────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task CloudException_carries_status_code_and_the_servers_message()
    {
        var e1 = await Assert.ThrowsAsync<CloudException>(() => new Cloud("wrong").Browsers.GetAsync("bs_1"));
        Assert.Equal((401, (string?)null, "Missing or invalid API key."), (e1.Status, e1.Code, e1.Message));
        var e2 = await Assert.ThrowsAsync<CloudException>(() =>
            new Cloud().Profiles.ImportCookiesAsync("busy", new[] { JsonNode.Parse("""{"name":"a","value":"b","domain":"x.com"}""")! }));
        Assert.Equal((409, (string?)"PROFILE_IN_USE", "A live session is saving to this profile."), (e2.Status, e2.Code, e2.Message));
    }

    [Fact]
    public async Task CloudException_for_a_non_JSON_answer()
    {
        _api.Flaky = 99;
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud().Runs.GetAsync("bs_run1"));
        Assert.Equal((503, "upstream restarting"), (e.Status, e.Message));
    }

    [Fact]
    public async Task A_connection_cut_while_the_answer_is_read_is_a_NETWORK_error()
    {
        await using var srv = new OneShotServer(OneShotServer.HttpAnswer("HTTP/1.1 200 OK", "{\"id\":", length: 500));
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud("k", srv.Url).Runs.GetAsync("bs_1"));
        Assert.Equal((0, (string?)"NETWORK"), (e.Status, e.Code));
    }

    [Fact]
    public async Task Reads_a_nested_error_object_too()
    {
        var body = """{"error":{"message":"Balance too low.","code":"INSUFFICIENT_BALANCE"}}""";
        await using var srv = new OneShotServer(OneShotServer.HttpAnswer("HTTP/1.1 402 Payment Required", body));
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud("k", srv.Url).Browsers.CreateAsync());
        Assert.Equal((402, (string?)"INSUFFICIENT_BALANCE", "Balance too low."), (e.Status, e.Code, e.Message));
    }

    [Fact]
    public async Task When_unreachable()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var port = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud("k", $"http://127.0.0.1:{port}").Browsers.ListAsync());
        Assert.Equal((0, (string?)"NETWORK"), (e.Status, e.Code));
    }

    // ── browsers ───────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task Browsers_create_get_list_stop_live_share()
    {
        var c = new Cloud();
        Assert.Equal("bs_1", Cloud.Str((await c.Browsers.CreateAsync(new CloudSessionOptions { Country = "us", Record = true }))!["id"]));
        AssertJson("""{"country":"us","record":true}""", _api.Log.Last().Body);
        Assert.Equal("active", Cloud.Str((await c.Browsers.GetAsync("bs_1"))!["status"]));
        Assert.Equal(12.5, (await c.Browsers.ListAsync(new[] { "active", "ended" }, limit: 5))!["balanceEur"]!.GetValue<double>());
        Assert.Equal("status=active%2Cended&limit=5", _api.Log.Last().Query);
        Assert.Equal("ended", Cloud.Str((await c.Browsers.StopAsync("bs_1"))!["status"]));
        Assert.True((await c.Browsers.LiveAsync("bs_1", control: true))!["interactive"]!.GetValue<bool>());
        Assert.Equal("control=1", _api.Log.Last().Query);
        Assert.False((await c.Browsers.LiveAsync("bs_1"))!["interactive"]!.GetValue<bool>());
        var share = await c.Browsers.ShareAsync("bs_1", recording: true, minutes: 30);
        Assert.Contains("/replay/", Cloud.Str(share!["url"]));
        AssertJson("""{"minutes":30,"recording":true}""", _api.Log.Last().Body);
    }

    [Fact]
    public async Task Hand_off_request_wait_done()
    {
        var c = new Cloud();
        var h = await c.Browsers.HandoffAsync("bs_1", reason: "solve the captcha", timeoutSec: 300);
        AssertJson($$"""{"state":"waiting","reason":"solve the captcha","since":"{{FakeCloud.T0}}","expiresAt":"2026-10-02T10:10:00.000Z","liveUrl":"{{FakeCloud.Live}}"}""", h);
        AssertJson("""{"reason":"solve the captcha","timeoutSec":300}""", _api.Log.Last().Body);
        Assert.Equal("done", Cloud.Str((await c.Browsers.WaitHandoffAsync("bs_1", poll: 0.01))!["handoff"]!["state"]));
        Assert.Equal(3, _api.Requests("GET", "/api/v1/browsers/bs_1").Count);
        Assert.Equal("done", Cloud.Str((await c.Browsers.HandoffDoneAsync("bs_1"))!["state"]));
        Assert.Equal("/api/v1/browsers/bs_1/handoff/done", _api.Log.Last().Path);
    }

    [Fact]
    public async Task WaitHandoff_times_out_with_the_last_view()
    {
        _api.HandoffPolls = 10_000;
        var c = new Cloud();
        await c.Browsers.HandoffAsync("bs_1");
        var e = await Assert.ThrowsAsync<CloudTimeoutException>(() => c.Browsers.WaitHandoffAsync("bs_1", timeout: 0.05, poll: 0.01));
        Assert.Equal("waiting", Cloud.Str(e.Last!["handoff"]!["state"]));
    }

    [Fact]
    public async Task WaitHandoff_on_a_session_with_no_hand_off_throws_NO_HANDOFF()
    {
        // handoff: null reads as "not waiting"; returning at once would look like a finished hand-off
        var c = new Cloud();
        var e = await Assert.ThrowsAsync<CloudException>(() => c.Browsers.WaitHandoffAsync("bs_7", poll: 0.01));
        Assert.Equal((200, (string?)"NO_HANDOFF"), (e.Status, e.Code));
        Assert.Contains("no hand-off was requested for session bs_7", e.Message);
        Assert.Single(_api.Requests("GET", "/api/v1/browsers/bs_7"));
        // a hand-off that is already over still returns at once
        _api.HandoffPolls = 0;
        await c.Browsers.HandoffAsync("bs_8");
        Assert.Equal("done", Cloud.Str((await c.Browsers.WaitHandoffAsync("bs_8", poll: 0.01))!["handoff"]!["state"]));
    }

    [Fact]
    public async Task Events_are_paged_with_next()
    {
        var c = new Cloud();
        var page = await c.Browsers.EventsAsync("bs_1");
        Assert.Equal("[1,2]", new JsonArray(page!["events"]!.AsArray().Select(e => (JsonNode?)e!["seq"]!.DeepClone()).ToArray()).ToJsonString());
        Assert.Equal(2, page["next"]!.GetValue<long>());
        page = await c.Browsers.EventsAsync("bs_1", after: page["next"]!.GetValue<long>(), limit: 10);
        Assert.Equal("[3,4]", new JsonArray(page!["events"]!.AsArray().Select(e => (JsonNode?)e!["seq"]!.DeepClone()).ToArray()).ToJsonString());
        Assert.Null(page["next"]);
        Assert.Equal("after=2&limit=10", _api.Log.Last().Query);
    }

    [Fact]
    public async Task Recording_url_download_without_the_key_errors()
    {
        var c = new Cloud();
        Assert.Equal($"{_api.Url}/dev/blob/rec.mp4?sig=abc", await c.Browsers.RecordingUrlAsync("bs_1"));
        var outPath = await c.Browsers.DownloadRecordingAsync("bs_1", Path.Combine(_tmp, "r.mp4"));
        Assert.Equal(FakeCloud.Recording, File.ReadAllBytes(outPath));
        Assert.Null(_api.Requests("GET", "/dev/blob/rec.mp4").Last().Header("authorization"));
        _api.RecordingState = "processing";
        var e = await Assert.ThrowsAsync<CloudException>(() => c.Browsers.RecordingUrlAsync("bs_1"));
        Assert.Equal((409, (string?)"NOT_READY"), (e.Status, e.Code));
        var e404 = await Assert.ThrowsAsync<CloudException>(() => c.Browsers.DownloadRecordingAsync("bs_unrecorded", Path.Combine(_tmp, "x.mp4")));
        Assert.Equal(404, e404.Status);
        Assert.False(File.Exists(Path.Combine(_tmp, "x.mp4")));
        Assert.False(File.Exists(Path.Combine(_tmp, "x.mp4.part")));
    }

    [Fact]
    public async Task The_recording_URL_must_be_a_web_URL()
    {
        _api.RecordingLocation = "file:///etc/passwd";
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud().Browsers.RecordingUrlAsync("bs_1"));
        Assert.Equal("the API did not answer with a recording URL", e.Message);
    }

    [Fact]
    public async Task A_recording_on_another_host_never_gets_the_API_key_and_the_redirect_is_not_followed()
    {
        await using var storage = new FakeCloud();
        _api.RecordingLocation = $"{storage.Url}/dev/blob/rec.mp4?sig=abc";
        var c = new Cloud();
        Assert.Equal(_api.RecordingLocation, await c.Browsers.RecordingUrlAsync("bs_1"));
        Assert.Empty(storage.Log);
        var outPath = await c.Browsers.DownloadRecordingAsync("bs_1", Path.Combine(_tmp, "r.mp4"));
        Assert.Equal(FakeCloud.Recording, File.ReadAllBytes(outPath));
        var hit = Assert.Single(storage.Log);
        Assert.Equal(("/dev/blob/rec.mp4", "sig=abc"), (hit.Path, hit.Query));
        Assert.Null(hit.Header("authorization"));
        Assert.All(_api.Log, r => Assert.Equal($"Bearer {FakeCloud.ApiKey}", r.Header("authorization")));
    }

    // ── runs ───────────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task Runs_create_maps_the_options_and_polls_to_the_end()
    {
        var updates = new List<string>();
        var schema = JsonNode.Parse("""{"type":"object","properties":{"price":{"type":"string"}}}""");
        var run = await new Cloud().Runs.CreateAsync("Find the price", new RunOptions
        {
            Url = "https://example.com/", Schema = schema,
            Secrets = new Dictionary<string, object> { ["pw"] = new { value = "hunter2", domains = new[] { "example.com" } } },
            MaxSteps = 20, Handoff = true, HandoffTimeoutSec = 120, Record = true, Country = "us", Poll = 0.01,
            OnUpdate = v => { updates.Add(Cloud.Str(v["status"])!); return Task.CompletedTask; },
        });
        AssertJson("""
            {"task":"Find the price","url":"https://example.com/","schema":{"type":"object","properties":{"price":{"type":"string"}}},
             "secrets":{"pw":{"value":"hunter2","domains":["example.com"]}},"maxSteps":20,"handoff":true,"handoffTimeoutSec":120,
             "record":true,"country":"us"}
            """, _api.Requests("POST", "/api/v1/runs")[0].Body);
        Assert.Equal("succeeded", Cloud.Str(run!["status"]));
        AssertJson("""{"plan":"Starter","price":"9.99"}""", run["result"]!["output"]);
        Assert.Equal(new[] { "queued", "running", "waiting_for_human", "running", "succeeded" }, updates);
    }

    [Fact]
    public async Task Reports_waiting_for_human_on_stderr_without_a_callback()
    {
        using var err = new StderrCapture();
        await new Cloud().Runs.CreateAsync("t", new RunOptions { Poll = 0.01 });
        Assert.Contains($"run bs_run1 is waiting for a human (needs a login): {FakeCloud.Live}", err.Text);
    }

    [Fact]
    public async Task Wait_false_returns_the_create_answer()
    {
        Assert.Equal("queued", Cloud.Str((await new Cloud().Runs.CreateAsync("t", new RunOptions { Wait = false }))!["status"]));
        Assert.Empty(_api.Requests("GET"));
    }

    [Fact]
    public async Task Runs_time_out_with_the_last_view()
    {
        _api.RunStatuses = new[] { "running" };
        var e = await Assert.ThrowsAsync<CloudTimeoutException>(() => new Cloud().Runs.WaitAsync("bs_run1", timeout: 0.05, poll: 0.01));
        Assert.Equal("running", Cloud.Str(e.Last!["status"]));
        Assert.Contains("bs_run1", e.Message);
    }

    [Fact]
    public async Task Runs_retry_transient_errors()
    {
        _api.RunStatuses = new[] { "succeeded" };
        _api.Flaky = 2;
        Assert.Equal("succeeded", Cloud.Str((await new Cloud().Runs.WaitAsync("bs_run1", poll: 0.01))!["status"]));
    }

    [Fact]
    public async Task The_servers_own_rules_come_back_as_CloudException()
    {
        var e = await Assert.ThrowsAsync<CloudException>(() => new Cloud().Runs.CreateAsync("t", new RunOptions { KeepAlive = true }));
        Assert.Equal((400, "keepAlive does not apply to runs"), (e.Status, e.Message));
    }

    [Fact]
    public async Task Runs_get_list_cancel()
    {
        var c = new Cloud();
        Assert.Equal("queued", Cloud.Str((await c.Runs.GetAsync("bs_run1"))!["status"]));
        Assert.Equal("bs_run1", Cloud.Str((await c.Runs.ListAsync(limit: 5))!["runs"]![0]!["id"]));
        Assert.Equal("limit=5", _api.Log.Last().Query);
        Assert.Equal("cancelled", Cloud.Str((await c.Runs.CancelAsync("bs_run1"))!["status"]));
        Assert.Equal("DELETE", _api.Log.Last().Method);
    }

    // ── profiles ───────────────────────────────────────────────────────────────────────────────────

    private static readonly JsonNode State = JsonNode.Parse("""
        {"cookies":[
          {"name":"sid","value":"1","domain":".example.com","path":"/","expires":-1,"httpOnly":true,"secure":true,"sameSite":"Lax"},
          {"name":"pref","value":"2","domain":"www.example.com","path":"/","expires":1893456000,"httpOnly":false,"secure":false,"sameSite":"None"},
          {"name":"x","value":"3","domain":"badexample.com","path":"/","expires":-1,"httpOnly":false,"secure":false,"sameSite":"Lax"},
          {"name":"t","value":"4","domain":".tracker.net","path":"/","expires":-1,"httpOnly":false,"secure":false,"sameSite":"Lax","size":5,"priority":"Medium","sourceScheme":"Secure"}
        ],"origins":[]}
        """)!;

    private static List<JsonNode> StateCookies() => State["cookies"]!.AsArray().Select(c => c!.DeepClone()).ToList();

    [Fact]
    public void Filters_cookies_by_domain()
    {
        string[] Names(params string[] d) => CloudCookies.Filter(StateCookies(), d).Select(c => Cloud.Str(c["name"])!).ToArray();
        Assert.Equal(new[] { "sid", "pref" }, Names("example.com"));
        Assert.Equal(new[] { "sid", "pref" }, Names(".EXAMPLE.com "));
        Assert.Equal(new[] { "sid", "pref" }, Names("www.example.com"));   // the browser sends .example.com there too
        Assert.Equal(new[] { "t" }, Names("tracker.net", "nope.org"));
        Assert.Empty(Names());
    }

    [Fact]
    public void Keeps_the_parent_domain_and_subdomain_cookies_of_a_host_never_a_bare_suffix()
    {
        var jar = new[] { "www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com",
            ".com", "com", "badexample.com", "www.badexample.com", "example.com.evil.net", ".co.uk", "localhost" };
        var cookies = jar.Select((d, i) => (JsonNode)new JsonObject { ["name"] = $"c{i}", ["value"] = "v", ["domain"] = d }).ToList();
        string[] Domains(params string[] allowed) => CloudCookies.Filter(cookies, allowed).Select(c => Cloud.Str(c["domain"])!).ToArray();
        Assert.Equal(new[] { "www.example.com", ".example.com", "example.com", "a.www.example.com" }, Domains("www.example.com"));
        Assert.Equal(new[] { "www.example.com", ".example.com", "example.com", "a.www.example.com", "other.example.com" }, Domains("example.com"));
        Assert.Equal(Domains("www.example.com"), Domains(".WWW.Example.com"));
        Assert.Equal(new[] { ".co.uk" }, Domains("shop.example.co.uk"));   // no PSL: a two-label parent is kept
        Assert.Equal(new[] { "localhost" }, Domains("localhost"));
        Assert.Equal(new[] { "example.com.evil.net" }, Domains("evil.net"));
        Assert.Equal(new[] { "example.com.evil.net" }, Domains("net"));   // an explicit choice of a whole TLD
    }

    [Fact]
    public void Parses_a_storage_state_a_cookie_array_and_refuses_anything_else()
    {
        Assert.Equal(4, CloudCookies.FromState(State.DeepClone()).Count);
        Assert.Equal(4, CloudCookies.FromState(State["cookies"]!.DeepClone()).Count);
        Assert.Empty(CloudCookies.FromState(JsonNode.Parse("""{"cookies":[]}""")));
        Assert.Contains("storage state", Assert.Throws<ArgumentException>(() => CloudCookies.FromState(JsonNode.Parse("""{"origins":[]}"""))).Message);
        Assert.Contains("name and a domain", Assert.Throws<ArgumentException>(() => CloudCookies.FromState(JsonNode.Parse("""[{"value":"x"}]"""))).Message);
    }

    [Fact]
    public async Task Profiles_list_import_get_delete()
    {
        var c = new Cloud();
        Assert.Equal("acct-1", Cloud.Str((await c.Profiles.ListAsync())!["profiles"]![0]!["name"]));
        var r = await c.Profiles.ImportCookiesAsync("acct 1/x", new[] { JsonNode.Parse("""{"name":"a","value":"1","domain":"a.com"}""")! });
        Assert.Equal("/api/v1/browsers/profiles/acct%201%2Fx/cookies", _api.Log.Last().Path);
        AssertJson("""{"cookies":[{"name":"a","value":"1","domain":"a.com"}],"mode":"merge"}""", _api.Log.Last().Body);
        Assert.Equal(1, r!["imported"]!.GetValue<int>());
        Assert.Equal(1, (await c.Profiles.GetAsync("acct 1/x"))!["cookies"]!.GetValue<int>());
        AssertJson("""{"ok":true}""", await c.Profiles.DeleteAsync("acct-1"));
    }

    [Fact]
    public async Task Sync_from_a_file_uploads_only_the_chosen_domains()
    {
        var f = Path.Combine(_tmp, "state.json");
        File.WriteAllText(f, State.ToJsonString());
        var c = new Cloud();
        var res = await c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f, Domains = new[] { "example.com" }, Replace = true });
        var sent = _api.Requests("PUT").Last().Body!;
        Assert.Equal("replace", Cloud.Str(sent["mode"]));
        Assert.Equal(Canon(new JsonArray(StateCookies()[0], StateCookies()[1])), Canon(sent["cookies"]));
        AssertJson($$"""{"name":"acct-1","cookies":2,"imported":2,"domains":["example.com","www.example.com"],"bytes":1234,"updatedAt":"{{FakeCloud.T0}}"}""", res);
        await c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f, AllDomains = true });
        var all = _api.Requests("PUT").Last().Body!;
        Assert.Equal("merge", Cloud.Str(all["mode"]));
        Assert.Equal(4, all["cookies"]!.AsArray().Count);
        Assert.Null(all["cookies"]![3]!["size"]);   // only the fields the API keeps are uploaded
    }

    [Fact]
    public async Task Sync_refuses_without_a_domain_choice_and_reads_nothing()
    {
        var f = Path.Combine(_tmp, "state.json");
        File.WriteAllText(f, State.ToJsonString());
        var c = new Cloud();
        Assert.Contains("AllDomains = true", (await Assert.ThrowsAsync<ArgumentException>(() => c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f }))).Message);
        Assert.Contains("not both", (await Assert.ThrowsAsync<ArgumentException>(() =>
            c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f, Domains = new[] { "a.com" }, AllDomains = true }))).Message);
        Assert.Contains("exactly one", (await Assert.ThrowsAsync<ArgumentException>(() =>
            c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f, FromCdp = "http://127.0.0.1:1", Domains = new[] { "a.com" } }))).Message);
        Assert.Equal("no cookies found for nothing.example; nothing was uploaded", (await Assert.ThrowsAsync<InvalidOperationException>(() =>
            c.Profiles.SyncAsync("acct-1", new ProfileSyncOptions { FromFile = f, Domains = new[] { "nothing.example" } }))).Message);
        Assert.Empty(_api.Log);
    }

    // ── webhooks ───────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task Webhooks_create_list_delete_test()
    {
        var c = new Cloud();
        var hook = await c.Webhooks.CreateAsync("https://hooks.example.com/x", new[] { "run.finished" }, "d");
        Assert.Equal("whsec_test_secret", Cloud.Str(hook!["secret"]));
        AssertJson("""{"url":"https://hooks.example.com/x","events":["run.finished"],"description":"d"}""", _api.Log.Last().Body);
        await c.Webhooks.CreateAsync("https://hooks.example.com/y");
        AssertJson("""{"url":"https://hooks.example.com/y"}""", _api.Log.Last().Body);
        Assert.Equal(new[] { "wh_1", "wh_2" }, (await c.Webhooks.ListAsync())!["webhooks"]!.AsArray().Select(h => Cloud.Str(h!["id"])).ToArray());
        AssertJson("""{"ok":true}""", await c.Webhooks.DeleteAsync("wh_1"));
        Assert.True((await c.Webhooks.TestAsync("wh_2"))!["ok"]!.GetValue<bool>());
        Assert.Equal("/api/v1/webhooks/wh_2/test", _api.Log.Last().Path);
    }

    // ── VerifyWebhook ──────────────────────────────────────────────────────────────────────────────

    private const string Secret = "whsec_test_secret";
    private const string Body = """{"id":"evt_1","type":"run.finished","createdAt":"2026-10-02T10:00:00.000Z","data":{"id":"bs_run1","status":"succeeded"}}""";
    private const long Now = 1_790_000_000;

    private static string Sign(string body, long t = Now, string secret = Secret)
        => $"t={t},v1={Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret), Encoding.UTF8.GetBytes($"{t}.{body}"))).ToLowerInvariant()}";

    [Fact]
    public void Accepts_the_fixed_vector_the_Node_and_Python_tests_use_too()
    {
        const string vector = "t=1790000000,v1=e7f51b8aaf2b3c2682ae9fa9dcf7d2c08b4c0f624cf5e55250685f75bdcaf419";
        Assert.Equal(vector, Sign(Body));
        Assert.Equal("succeeded", Cloud.Str(Cloud.VerifyWebhook(Body, vector, Secret, 300, Now)!["data"]!["status"]));
        Assert.Equal("evt_1", Cloud.Str(Cloud.VerifyWebhook(Encoding.UTF8.GetBytes(Body), Sign(Body), Secret, 300, Now + 299)!["id"]));
    }

    [Fact]
    public void Refuses_a_tampered_body_a_wrong_secret_and_a_changed_timestamp()
    {
        Assert.Contains("does not match", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body.Replace("succeeded", "failed"), Sign(Body), Secret, 300, Now)).Message);
        Assert.Contains("does not match", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, Sign(Body, Now, "whsec_other"), Secret, 300, Now)).Message);
        Assert.Contains("does not match", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, Sign(Body).Replace($"t={Now}", $"t={Now + 1}"), Secret, 300, Now)).Message);
    }

    [Fact]
    public void Refuses_an_old_or_future_timestamp_unless_the_window_is_off()
    {
        Assert.Contains("tolerance", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, Sign(Body), Secret, 300, Now + 301)).Message);
        Assert.Contains("tolerance", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, Sign(Body), Secret, 300, Now - 301)).Message);
        Assert.Equal("evt_1", Cloud.Str(Cloud.VerifyWebhook(Body, Sign(Body), Secret, null, Now + 1_000_000)!["id"]));
        Assert.Equal("evt_1", Cloud.Str(Cloud.VerifyWebhook(Body, Sign(Body), Secret, 600, Now + 500)!["id"]));
    }

    [Fact]
    public void Any_one_of_several_v1_signatures_is_enough()
    {
        var good = Sign(Body).Split("v1=")[1];
        Assert.Equal("run.finished", Cloud.Str(Cloud.VerifyWebhook(Body, $"t={Now}, v1={new string('0', 64)}, v1={good.ToUpperInvariant()}", Secret, 300, Now)!["type"]));
        Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, $"t={Now},v1={new string('0', 64)},v1=abc", Secret, 300, Now));
    }

    [Theory]
    [InlineData("")] [InlineData("v1=abc")] [InlineData("t=1790000000")] [InlineData("t=abc,v1=0000")] [InlineData("garbage")] [InlineData(null)]
    public void Refuses_a_malformed_header(string? header)
        => Assert.Contains("invalid Clearcote-Signature", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, header, Secret, 300, Now)).Message);

    [Theory]
    [InlineData(null)] [InlineData("")] [InlineData("  ")]
    public void Needs_a_secret(string? secret)
    {
        foreach (var key in new[] { "undefined", "null", "", "  " })
            Assert.Contains("signing secret", Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, Sign(Body, Now, key), secret!, 300, Now)).Message);
    }

    [Fact]
    public void Refuses_an_oversized_header_before_any_work()
    {
        var many = $"{Sign(Body)},v1={string.Join(",v1=", Enumerable.Repeat(new string('0', 64), 200))}";
        Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, many, Secret, 300, Now));
        Assert.Throws<WebhookSignatureException>(() => Cloud.VerifyWebhook(Body, $"t={new string('9', 100000)},v1={new string('0', 64)}", Secret, 300, Now));
    }
}
