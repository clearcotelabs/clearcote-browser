using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// A paid licence without a cap counts every browser: the backend answers a checkout that offers
// lease_scope_supported ["browser"] with a per-browser lease, exactly like the free tier's. On top of that
// path, a paid browser keeps its newest token as a grace token (grace-<hash>.json, never the shared
// lease-<hash>.json), and a checkout that gets no verdict (unreachable, a 5xx, a challenge, RATE_LIMITED)
// starts the browser on it, uncounted, reported by the next checkout. The free tier stays fail-closed.
// Mirrors the Python and Node suites. Hermetic: a FakeHandler plays the lease server and the fallback, HOME,
// USERPROFILE and LOCALAPPDATA point at a temp dir, a unique key per test keeps the lease registry apart, and
// every test shuts its leases down so no heartbeat outlives it.
public class LicensePaidBrowserTests : IDisposable
{
    private readonly (string, string, TimeSpan, TimeSpan[]) _seams =
        (License.DefaultLeaseBase, License.FallbackLeaseBase, License.PrimarySkipFor, License.RateLimitBackoff);

    public LicensePaidBrowserTests()
    {
        // Names that never resolve: only the FakeHandler (or nothing) answers them.
        License.DefaultLeaseBase = "http://lease.test";
        License.FallbackLeaseBase = "http://www.test";
        License.RateLimitBackoff = new[] { TimeSpan.FromMilliseconds(10), TimeSpan.FromMilliseconds(20), TimeSpan.FromMilliseconds(40) };
        License.ForgetLeaseServerDown();
    }

    public void Dispose()
    {
        (License.DefaultLeaseBase, License.FallbackLeaseBase, License.PrimarySkipFor, License.RateLimitBackoff) = _seams;
        License.ForgetLeaseServerDown();
    }

    private static long Now() => DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    private static HttpResponseMessage Resp(int status, string json) =>
        new((HttpStatusCode)status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    /// The licence server as SDK 0.55 meets it. A paid licence without a cap (Uncapped) gets a per-browser lease
    /// for a checkout that offers the browser scope with a launch_id; a capped one gets the machine lease.
    private sealed class Backend
    {
        public string Plan = "pro";
        public bool Uncapped = true;
        public bool Down;                   // neither the lease server nor the fallback answers
        public string? Notice;
        public int HeartbeatSec = 3600;     // the background heartbeat stays out of the way unless a test asks
        public long Iat = 1_000_000;        // the next token's iat is one more
        public readonly Queue<(int Status, string Json)> Checkouts = new();   // scripted checkout answers first
        private readonly List<(string Host, string Ep, JsonElement Body)> _calls = new();
        private int _n;
        private readonly object _lock = new();

        public static string Tok(string plan, long iat) =>
            Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { v = 1, plan, iat })))
                .TrimEnd('=').Replace('+', '-').Replace('/', '_') + ".sig";

        public (string Host, string Ep, JsonElement Body)[] Calls() { lock (_lock) return _calls.ToArray(); }
        public JsonElement[] Bodies(string ep) => Calls().Where(c => c.Ep == ep).Select(c => c.Body).ToArray();

        public FakeHandler Handler() => new(req =>
        {
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            var raw = req.Content is null ? "{}" : req.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var body = JsonDocument.Parse(raw).RootElement.Clone();
            lock (_lock)
            {
                _calls.Add((req.RequestUri.Host, ep, body));
                if (Down) throw new HttpRequestException("Connection refused");
                if (ep == "checkout")
                {
                    if (Checkouts.Count > 0)
                    {
                        var (status, json) = Checkouts.Dequeue();
                        return Resp(status, json);
                    }
                    var offers = body.TryGetProperty("lease_scope_supported", out var ls) && ls.ValueKind == JsonValueKind.Array
                                 && ls.EnumerateArray().Any(x => x.GetString() == "browser")
                                 && body.TryGetProperty("launch_id", out var l) && l.ValueKind == JsonValueKind.String;
                    var browser = Plan == "free" || (Uncapped && offers);
                    var answer = new Dictionary<string, object?>
                    {
                        ["lease_id"] = $"L{++_n}",
                        ["token"] = Tok(Plan, ++Iat),
                        ["exp"] = Now() + (Plan == "free" ? 180 : 86400),
                        ["lease_ttl_sec"] = browser ? 810 : 360,
                        ["heartbeat_interval_sec"] = HeartbeatSec,
                        ["concurrency"] = new { limit = 0 },   // no "used": the SDK never needs it
                    };
                    if (browser) answer["lease_scope"] = "browser";
                    if (Notice is not null) answer["notice"] = Notice;
                    return Resp(200, JsonSerializer.Serialize(answer));
                }
                if (ep == "heartbeat") return Resp(200, JsonSerializer.Serialize(new { token = Tok(Plan, ++Iat), exp = Now() + 86400 }));
                return Resp(200, "{}");
            }
        });
    }

    private static (Sandbox S, string Home, string Key) Setup(Backend be)
    {
        var key = $"cc_lic_paid_{Guid.NewGuid():N}";
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key).Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", null)
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Env("CLEARCOTE_INSTANCE_ID", null).Http(be.Handler());
        var home = s.TempHome();
        s.Env("LOCALAPPDATA", home);   // the engine's high-water mark: none
        return (s, home, key);
    }

    private static Task<LeaseSession?> Acquire(bool quiet = true) =>
        License.AcquireLeaseAsync(new LicenseOptions(), "0.55.0", quiet, () => Task.FromResult<string?>("155.0.7700.0"));

    private static string Id(string key) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))).ToLowerInvariant()[..16];
    private static string LeaseFile(string home, string key) => Path.Combine(home, ".clearcote", $"lease-{Id(key)}.json");
    private static string GraceFile(string home, string key) => Path.Combine(home, ".clearcote", $"grace-{Id(key)}.json");
    private static JsonElement ReadJson(string path) => JsonDocument.Parse(File.ReadAllText(path)).RootElement.Clone();

    private static void WriteFile(string path, object body)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, JsonSerializer.Serialize(body));
    }

    private const string RateLimited = "{\"error\":\"Rate limit exceeded.\",\"code\":\"RATE_LIMITED\"}";
    private const string Unreachable = "[clearcote] [license] licence server unreachable (";
    private const string UncountedTail = "); this browser starts on a cached run-token and is not counted.";

    private static int Count(string text, string part)
    {
        var n = 0;
        for (var i = text.IndexOf(part, StringComparison.Ordinal); i >= 0; i = text.IndexOf(part, i + part.Length, StringComparison.Ordinal)) n++;
        return n;
    }

    // ── 2 + 3: every launch offers the browser scope; a paid browser-scope licence counts each one ─────

    [Fact]
    public async Task Paid_browser_scope_gives_every_launch_its_own_lease_and_never_touches_the_shared_cache()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var leases = new List<LeaseSession>();
                for (var i = 0; i < 5; i++) leases.Add((await Acquire())!);
                var cos = be.Bodies("checkout");
                Assert.Equal(5, cos.Length);
                Assert.All(cos, c => Assert.Equal(new[] { "browser" },
                    c.GetProperty("lease_scope_supported").EnumerateArray().Select(e => e.GetString())));
                Assert.Equal(5, cos.Select(c => c.GetProperty("launch_id").GetString()).Distinct().Count());
                Assert.Equal(5, leases.Select(l => l.LeaseId).Distinct().Count());
                Assert.Equal(5, leases.Select(l => l.Token).Distinct().Count());
                foreach (var l in leases) await l.StopAsync();
                Assert.Equal(leases.Select(l => l.LeaseId).OrderBy(x => x),
                    be.Bodies("checkin").Select(b => b.GetProperty("lease_id").GetString()).OrderBy(x => x));
                Assert.False(File.Exists(LeaseFile(home, key)));   // older SDKs read that one
                var grace = ReadJson(GraceFile(home, key));
                Assert.Equal(leases[^1].Token, grace.GetProperty("token").GetString());   // the newest token
                Assert.True(grace.GetProperty("exp").GetInt64() > Now() + 3600);
                Assert.All(be.Calls(), c => Assert.Equal("lease.test", c.Host));   // the lease server answered it all
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task Machine_scope_answer_keeps_the_shared_lease_and_the_legacy_cache_exactly_as_before()
    {
        var be = new Backend { Uncapped = false };   // a paid licence with a cap
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var a = await Acquire();
                var b = await Acquire();
                var co = Assert.Single(be.Bodies("checkout"));
                Assert.Equal("browser", co.GetProperty("lease_scope_supported")[0].GetString());
                Assert.Equal(a!.Token, b!.Token);
                Assert.Equal(a.Token, ReadJson(LeaseFile(home, key)).GetProperty("token").GetString());
                Assert.False(File.Exists(GraceFile(home, key)));
                await a.StopAsync();
                await b.StopAsync();
                Assert.Empty(be.Bodies("checkin"));   // the shared slot is checked in once, at exit
                await License.ShutdownAllLeasesAsync();
                Assert.Single(be.Bodies("checkin"));
                // The next process reuses the cached token with zero checkouts.
                var c = await Acquire();
                Assert.Equal(a.Token, c!.Token);
                Assert.Single(be.Bodies("checkout"));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task An_answer_without_concurrency_used_is_accepted()
    {
        var be = new Backend();
        be.Checkouts.Enqueue((200, JsonSerializer.Serialize(new
        {
            lease_id = "L-x", token = Backend.Tok("pro", 7), exp = Now() + 86400, lease_ttl_sec = 810,
            heartbeat_interval_sec = 270, concurrency = new { limit = 0 }, lease_scope = "browser",
        })));
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var lease = await Acquire();
                Assert.Equal(Backend.Tok("pro", 7), lease!.Token);
                Assert.Equal("L-x", lease.LeaseId);
                await lease.StopAsync();
                Assert.Equal("L-x", Assert.Single(be.Bodies("checkin")).GetProperty("lease_id").GetString());
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── the shared cache: only a file this SDK marked as a machine lease skips a checkout ──────────────
    // An older SDK never offered the browser scope, so its valid lease-<hash>.json says nothing about whether
    // the licence counts every browser: reusing it kept an upgraded machine uncounted for up to 24 h.

    [Fact]
    public async Task An_older_sdks_cache_does_not_skip_the_checkout_and_a_browser_scope_answer_wins()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var old = Backend.Tok("pro", 4000);
                WriteFile(LeaseFile(home, key), new { token = old, exp = Now() + 3600, lease_id = "L-old" });
                var a = await Acquire();
                var b = await Acquire();
                var cos = be.Bodies("checkout");
                Assert.Equal(2, cos.Length);
                Assert.All(cos, c => Assert.Equal("browser", c.GetProperty("lease_scope_supported")[0].GetString()));
                Assert.NotEqual(old, a!.Token);
                Assert.NotEqual(a.LeaseId, b!.LeaseId);
                var file = ReadJson(LeaseFile(home, key));   // left as the older SDK wrote it
                Assert.Equal(old, file.GetProperty("token").GetString());
                Assert.False(file.TryGetProperty("scope", out _));
                await a.StopAsync();
                await b.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_machine_scope_answer_marks_the_cache_and_from_then_on_it_is_reused_with_zero_calls()
    {
        var be = new Backend { Uncapped = false };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                WriteFile(LeaseFile(home, key), new { token = Backend.Tok("pro", 4000), exp = Now() + 3600, lease_id = "L-old" });
                var a = await Acquire();
                Assert.Single(be.Bodies("checkout"));
                var file = ReadJson(LeaseFile(home, key));
                Assert.Equal(new[] { "exp", "lease_id", "scope", "token" }, file.EnumerateObject().Select(p => p.Name).OrderBy(n => n));
                Assert.Equal(a!.Token, file.GetProperty("token").GetString());
                Assert.Equal(a.LeaseId, file.GetProperty("lease_id").GetString());
                Assert.True(file.GetProperty("exp").GetInt64() > Now() + 3600);
                Assert.Equal("machine", file.GetProperty("scope").GetString());
                await License.ShutdownAllLeasesAsync();      // this process exits
                var next = await Acquire();                  // the next one reuses the marked file
                Assert.Equal(a.Token, next!.Token);
                Assert.Single(be.Bodies("checkout"));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_cache_marked_machine_is_reused_with_zero_calls()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var marked = Backend.Tok("pro", 4000);
                WriteFile(LeaseFile(home, key), new { token = marked, exp = Now() + 3600, lease_id = "L-m", scope = "machine" });
                Assert.Equal(marked, (await Acquire())!.Token);
                Assert.Empty(be.Calls());
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task An_older_sdks_cache_still_rides_out_an_outage()
    {
        var be = new Backend { Down = true };
        var (s, home, key) = Setup(be);
        using (s)
        using (var err = new StderrCapture())
        {
            try
            {
                var old = Backend.Tok("pro", 4000);
                WriteFile(LeaseFile(home, key), new { token = old, exp = Now() + 3600, lease_id = "L-old" });
                var lease = await Acquire(quiet: false);
                Assert.Equal(old, lease!.Token);
                Assert.Equal(new[] { "lease.test", "www.test" }, be.Calls().Select(c => c.Host));   // asked first
                Assert.Contains("[clearcote] [license] backend unreachable (", err.Text);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task In_an_outage_a_paid_grace_token_goes_before_an_older_sdks_cache()
    {
        var be = new Backend { Down = true };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                WriteFile(LeaseFile(home, key), new { token = Backend.Tok("pro", 4000), exp = Now() + 3600, lease_id = "L-old" });
                var grace = Backend.Tok("pro", 5000);
                WriteFile(GraceFile(home, key), new { token = grace, exp = Now() + 3600 });
                var lease = await Acquire();
                Assert.Equal(grace, lease!.Token);
                Assert.Equal("uncounted", lease.LeaseId);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── 1: the lease server, its fallback and the explicit bases ─────────────────────────────────────

    private static string MachineAnswer(string token) =>
        $"{{\"lease_id\":\"L-fb\",\"token\":\"{token}\",\"exp\":{Now() + 3600},\"heartbeat_interval_sec\":3600}}";

    private static Sandbox RouteSandbox()
    {
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", null)
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Env("CLEARCOTE_INSTANCE_ID", "test-instance");
        var home = s.TempHome();
        s.Env("LOCALAPPDATA", home);
        return s;
    }

    private static Task<LeaseSession?> AcquireKey(string key, string? apiBase = null) =>
        License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = key, LicenseApiBase = apiBase }, "0.55.0", true);

    [Fact]
    public async Task An_unreachable_lease_server_sends_the_call_to_the_fallback()
    {
        await using var fallback = new TestOrigin(_ => (200, MachineAnswer("tok-fallback")));
        using var s = RouteSandbox();
        License.DefaultLeaseBase = "http://127.0.0.1:1";   // nothing listens: connection refused
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        try
        {
            var lease = await AcquireKey($"cc_lic_route_{Guid.NewGuid():N}");
            Assert.Equal("tok-fallback", lease!.Token);
            Assert.Equal(new[] { "/api/v1/lease/checkout" }, fallback.Log.Select(r => r.Url));
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    [Fact]
    public async Task A_failed_lease_server_is_skipped_for_a_while_then_tried_again()
    {
        await using var primary = new TestOrigin(_ => (503, "<html>503 Service Unavailable</html>"));
        await using var fallback = new TestOrigin(_ => (200, MachineAnswer("tok-fallback")));
        using var s = RouteSandbox();
        License.DefaultLeaseBase = $"http://127.0.0.1:{primary.Port}";
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        License.PrimarySkipFor = TimeSpan.FromSeconds(1);
        try
        {
            await AcquireKey($"cc_lic_skip1_{Guid.NewGuid():N}");
            Assert.Equal((1, 1), (primary.Log.Count, fallback.Log.Count));
            await AcquireKey($"cc_lic_skip2_{Guid.NewGuid():N}");   // within the window: straight to the fallback
            Assert.Equal((1, 2), (primary.Log.Count, fallback.Log.Count));
            await Task.Delay(1300);
            await AcquireKey($"cc_lic_skip3_{Guid.NewGuid():N}");   // after it: the lease server first again
            Assert.Equal((2, 3), (primary.Log.Count, fallback.Log.Count));
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    public static IEnumerable<object[]> PrimaryAnswers() => new[]
    {
        new object[] { 502, "<html>Bad Gateway</html>", true },
        new object[] { 503, "{\"error\":\"maintenance\"}", true },     // a gateway status, whatever the body
        new object[] { 522, "", true },
        new object[] { 403, "<html>Access denied</html>", true },     // a challenge page, not a verdict
        new object[] { 401, "{\"error\":\"Invalid license key.\",\"code\":\"INVALID_LICENSE\"}", false },
        new object[] { 403, "{\"error\":\"License revoked.\",\"code\":\"LICENSE_REVOKED\"}", false },
        new object[] { 426, "{\"error\":\"Upgrade the SDK.\",\"code\":\"SDK_UPGRADE_REQUIRED\"}", false },
        new object[] { 429, "{\"error\":\"limit\",\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}", false },
        new object[] { 500, "{\"error\":\"boom\"}", false },
    };

    [Theory]
    [MemberData(nameof(PrimaryAnswers))]
    public async Task Only_a_lease_server_that_does_not_answer_is_retried_on_the_fallback(int status, string body, bool fallsBack)
    {
        await using var primary = new TestOrigin(_ => (status, body));
        await using var fallback = new TestOrigin(_ => (200, MachineAnswer("tok-fallback")));
        using var s = RouteSandbox();
        License.DefaultLeaseBase = $"http://127.0.0.1:{primary.Port}";
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        try
        {
            if (fallsBack) Assert.Equal("tok-fallback", (await AcquireKey($"cc_lic_ans_{Guid.NewGuid():N}"))!.Token);
            else await Assert.ThrowsAnyAsync<LicenseError>(() => AcquireKey($"cc_lic_ans_{Guid.NewGuid():N}"));
            Assert.Single(primary.Log);
            Assert.Equal(fallsBack ? 1 : 0, fallback.Log.Count);   // a JSON answer from the lease server is final
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    public static IEnumerable<object[]> ExplicitBases() => new[]
    {
        new object[] { "option" }, new object[] { "license_api" }, new object[] { "lease_api" },
        new object[] { "option beats both envs" }, new object[] { "license_api beats lease_api" },
    };

    [Theory]
    [MemberData(nameof(ExplicitBases))]
    public async Task An_explicit_base_is_used_alone_with_no_fallback(string how)
    {
        var be = new Backend { Down = true };
        var (s, _, key) = Setup(be);
        using (s)
        {
            string? option = null;
            if (how.StartsWith("option")) option = "http://option.test";
            if (how == "license_api" || how.Contains("beats")) s.Env("CLEARCOTE_LICENSE_API", "http://license-api.test/");
            if (how == "lease_api" || how.Contains("beats")) s.Env("CLEARCOTE_LEASE_API", "http://lease-api.test");
            var want = how switch
            {
                "option" or "option beats both envs" => "option.test",
                "license_api" or "license_api beats lease_api" => "license-api.test",
                _ => "lease-api.test",
            };
            try
            {
                await Assert.ThrowsAnyAsync<LicenseError>(() =>
                    License.AcquireLeaseAsync(new LicenseOptions { LicenseApiBase = option }, "0.55.0", true));
                Assert.Equal(new[] { want }, be.Calls().Select(c => c.Host));   // one call, there, and nowhere else
                Assert.Equal("checkout", be.Calls()[0].Ep);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task By_default_the_lease_calls_go_to_the_lease_server_then_the_website()
    {
        // The real defaults; the FakeHandler answers before anything leaves this machine.
        (License.DefaultLeaseBase, License.FallbackLeaseBase, _, _) = _seams;
        var be = new Backend { Down = true };
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.Equal(new[] { "lease.clearcotelabs.com", "www.clearcotelabs.com" }, be.Calls().Select(c => c.Host));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task The_fallback_works_the_same_through_the_launch_proxy()
    {
        await using var fallback = new TestOrigin(_ => (200, MachineAnswer("tok-fallback")));
        await using var proxy = new TestHttpProxy();
        using var s = RouteSandbox();
        License.DefaultLeaseBase = "http://127.0.0.1:1";
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        try
        {
            var lease = await License.AcquireLeaseAsync(
                new LicenseOptions { LicenseKey = $"cc_lic_px_{Guid.NewGuid():N}", LicenseThroughProxy = true }, "0.55.0", true,
                proxy: new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" });
            Assert.Equal("tok-fallback", lease!.Token);
            Assert.Equal(new[] { "http://127.0.0.1:1/api/v1/lease/checkout", $"{License.FallbackLeaseBase}/api/v1/lease/checkout" },
                proxy.Log.Select(h => h.Target));
            Assert.Single(fallback.Log);
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    [Fact]
    public async Task Seats_use_the_lease_server_and_its_fallback()
    {
        await using var fallback = new TestOrigin(r => r.Url == "/api/v1/lease/seats" ? (200, "{\"used\":3,\"limit\":0,\"plan\":\"pro\"}") : (404, "{}"));
        using var s = RouteSandbox();
        License.DefaultLeaseBase = "http://127.0.0.1:1";
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        var got = await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = "cc_lic_seats" });
        Assert.Equal(new SessionSeats(SeatsState.Ok, 3, 0, "pro"), got);
        Assert.Single(fallback.Log);
    }

    // ── 3b: no verdict + a paid grace token -> the browser starts uncounted ──────────────────────────

    [Fact]
    public async Task A_paid_browser_that_cannot_reach_either_server_starts_on_its_grace_token_uncounted()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        using (var err = new StderrCapture())
        {
            try
            {
                var first = await Acquire(quiet: false);      // a paid per-browser lease: the grace token is written
                await first!.StopAsync();
                Assert.Equal(first.Token, ReadJson(GraceFile(home, key)).GetProperty("token").GetString());
                be.Down = true;
                var calls = be.Calls().Length;
                var offline = await Acquire(quiet: false);
                Assert.Equal(first.Token, offline!.Token);
                Assert.Equal("uncounted", offline.LeaseId);
                Assert.Equal(new[] { "lease.test", "www.test" }, be.Calls().Skip(calls).Select(c => c.Host)); // fallback tried
                Assert.Equal(1, Count(err.Text, Unreachable));
                Assert.Contains(UncountedTail, err.Text);
                var file = offline.BindLaunch();
                Assert.Equal(first.Token, File.ReadAllText(file.File));
                file.Release();
                await offline.StopAsync();                   // no lease: nothing to check in
                Assert.Single(be.Bodies("checkin"));

                be.Down = false;
                var back = await Acquire(quiet: false);
                var bodies = be.Bodies("checkout");
                Assert.Equal(1, bodies[^1].GetProperty("uncounted_launches").GetInt32());
                var again = await Acquire(quiet: false);     // the 200 settled it
                Assert.False(be.Bodies("checkout")[^1].TryGetProperty("uncounted_launches", out _));
                await back!.StopAsync();
                await again!.StopAsync();
                Assert.Empty(be.Bodies("heartbeat"));        // the uncounted browser never heartbeat
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_new_process_with_a_valid_paid_grace_file_starts_uncounted_and_reports_it()
    {
        var be = new Backend { Down = true };
        var (s, home, key) = Setup(be);
        using (s)
        using (var err = new StderrCapture())
        {
            try
            {
                var grace = Backend.Tok("pro", 5000);
                WriteFile(GraceFile(home, key), new { token = grace, exp = Now() + 3600 });
                var lease = await Acquire(quiet: false);
                Assert.Equal(grace, lease!.Token);
                Assert.Equal(new[] { "lease.test", "www.test" }, be.Calls().Select(c => c.Host));
                Assert.Contains(Unreachable, err.Text);
                Assert.Contains(UncountedTail, err.Text);
                be.Down = false;
                License.ForgetLeaseServerDown();
                var counted = await Acquire();
                Assert.NotEqual("uncounted", counted!.LeaseId);
                Assert.Equal(1, be.Bodies("checkout")[^1].GetProperty("uncounted_launches").GetInt32());
                var next = await Acquire();
                Assert.False(be.Bodies("checkout")[^1].TryGetProperty("uncounted_launches", out _));
                await counted.StopAsync();
                await next!.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_quiet_uncounted_launch_prints_nothing_and_a_legacy_paid_token_is_the_second_choice()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        using (var err = new StderrCapture())
        {
            try
            {
                var first = await Acquire();
                await first!.StopAsync();
                File.Delete(GraceFile(home, key));
                var legacy = Backend.Tok("pro", 4000);
                WriteFile(LeaseFile(home, key), new { token = legacy, exp = Now() + 3600, lease_id = "L-old" });
                be.Down = true;
                var lease = await Acquire();
                Assert.Equal(legacy, lease!.Token);
                Assert.DoesNotContain("[clearcote]", err.Text);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    public static IEnumerable<object[]> NoUsableToken() => new[]
    {
        new object[] { "new process, no files", "Could not reach the license server and no valid cached token" },
        new object[] { "new process, grace token about to expire", "Could not reach the license server and no valid cached token" },
        new object[] { "paid browser in this process, grace file gone", "Could not reach the license server to start this browser" },
    };

    [Theory]
    [MemberData(nameof(NoUsableToken))]
    public async Task Without_a_usable_token_it_is_the_same_LicenseError_as_before(string how, string message)
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                if (how.StartsWith("paid browser"))
                {
                    await (await Acquire())!.StopAsync();
                    File.Delete(GraceFile(home, key));
                }
                if (how.Contains("about to expire"))
                    WriteFile(GraceFile(home, key), new { token = Backend.Tok("pro", 5000), exp = Now() + 30 });
                be.Down = true;
                var e = await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.StartsWith(message, e.Message);
                Assert.Equal("LICENSE_ERROR", e.Code);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Theory]
    [InlineData("free lease in this process, paid grace file")]
    [InlineData("new process, free token in the grace file")]
    public async Task A_free_licence_stays_fail_closed_even_with_a_grace_file(string how)
    {
        var be = new Backend { Plan = "free" };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                if (how.StartsWith("free lease"))
                {
                    await (await Acquire())!.StopAsync();
                    Assert.False(File.Exists(GraceFile(home, key)));   // a free token is never kept
                }
                var plan = how.StartsWith("free lease") ? "pro" : "free";
                WriteFile(GraceFile(home, key), new { token = Backend.Tok(plan, 9000), exp = Now() + 3600 });
                be.Down = true;
                await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── 4: RATE_LIMITED is this IP's request budget, not the licence's limit ─────────────────────────

    [Fact]
    public async Task A_rate_limited_checkout_is_retried_and_then_succeeds()
    {
        var be = new Backend();
        be.Checkouts.Enqueue((429, RateLimited));
        be.Checkouts.Enqueue((429, RateLimited));
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var lease = await Acquire();
                Assert.False(string.IsNullOrEmpty(lease!.Token));
                Assert.NotEqual("uncounted", lease.LeaseId);
                var cos = be.Bodies("checkout");
                Assert.Equal(3, cos.Length);
                Assert.Single(cos.Select(c => c.GetProperty("launch_id").GetString()).Distinct());   // the same launch
                await lease.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_persistent_rate_limit_with_a_paid_grace_token_starts_uncounted()
    {
        var be = new Backend();
        for (var i = 0; i < 4; i++) be.Checkouts.Enqueue((429, RateLimited));
        var (s, home, key) = Setup(be);
        using (s)
        using (var err = new StderrCapture())
        {
            try
            {
                var grace = Backend.Tok("pro", 5000);
                WriteFile(GraceFile(home, key), new { token = grace, exp = Now() + 3600 });
                var lease = await Acquire(quiet: false);
                Assert.Equal(grace, lease!.Token);
                Assert.Equal(4, be.Bodies("checkout").Length);   // the first try and 3 retries
                Assert.Contains($"{Unreachable}rate limited{UncountedTail}", err.Text);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_persistent_rate_limit_without_a_token_is_a_LicenseError_not_a_concurrency_limit()
    {
        var be = new Backend();
        for (var i = 0; i < 4; i++) be.Checkouts.Enqueue((429, RateLimited));
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var e = await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.IsNotType<ConcurrencyLimitError>(e);
                Assert.Equal("RATE_LIMITED", e.Code);
                Assert.StartsWith("Rate limit exceeded.", e.Message);
                Assert.Equal(4, be.Bodies("checkout").Length);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Theory]
    [InlineData("{\"error\":\"limit\",\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}")]
    [InlineData("{\"error\":\"limit\"}")]
    public async Task A_concurrency_limit_429_is_raised_at_once_even_with_a_grace_token(string json)
    {
        var be = new Backend();
        be.Checkouts.Enqueue((429, json));
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                WriteFile(GraceFile(home, key), new { token = Backend.Tok("pro", 5000), exp = Now() + 3600 });
                await Assert.ThrowsAsync<ConcurrencyLimitError>(() => Acquire());
                Assert.Single(be.Bodies("checkout"));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── 3d: definitive verdicts never fall back ──────────────────────────────────────────────────────

    [Theory]
    [InlineData(426, "{\"error\":\"This SDK is too old for this licence: upgrade to 0.55.0 or later.\",\"code\":\"SDK_UPGRADE_REQUIRED\"}", "SDK_UPGRADE_REQUIRED")]
    [InlineData(401, "{\"error\":\"Invalid license key.\",\"code\":\"INVALID_LICENSE\"}", "INVALID_LICENSE")]
    [InlineData(403, "{\"error\":\"License expired.\",\"code\":\"LICENSE_EXPIRED\"}", "LICENSE_REVOKED")]
    public async Task A_definitive_verdict_is_raised_with_the_servers_message_and_no_grace_token(int status, string json, string code)
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var first = await Acquire();               // a paid browser: the grace token is there
                await first!.StopAsync();
                Assert.True(File.Exists(GraceFile(home, key)));
                be.Checkouts.Enqueue((status, json));
                var e = await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.Equal(JsonDocument.Parse(json).RootElement.GetProperty("error").GetString(), e.Message);
                Assert.Equal(code, e.Code);
                Assert.Equal(2, be.Bodies("checkout").Length);   // and not sent again to the fallback
                Assert.All(be.Calls(), c => Assert.Equal("lease.test", c.Host));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── 5: notice ────────────────────────────────────────────────────────────────────────────────────

    [Fact]
    public async Task A_notice_is_printed_once_per_process_and_no_notice_prints_nothing()
    {
        var notice = $"Your plan renews in 3 days ({Guid.NewGuid():N}).";
        var be = new Backend { Notice = notice };
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                using (var err = new StderrCapture())
                {
                    await (await Acquire(quiet: false))!.StopAsync();
                    await (await Acquire(quiet: false))!.StopAsync();
                    Assert.Equal(2, be.Bodies("checkout").Length);
                    Assert.Equal(1, Count(err.Text, $"[clearcote] [license] {notice}"));
                }
                var plain = new Backend();
                var (s2, _, _) = Setup(plain);
                using (s2)
                using (var err = new StderrCapture())
                {
                    await (await Acquire(quiet: false))!.StopAsync();
                    Assert.DoesNotContain("[clearcote] [license]", err.Text);
                }
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── 3a: the grace file only moves forward ───────────────────────────────────────────────────────

    [Fact]
    public async Task The_grace_file_is_never_rolled_back_to_an_older_token_and_heartbeats_move_it_forward()
    {
        var be = new Backend { HeartbeatSec = 1 };   // the loop's floor: a beat every 5 s
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var newer = Backend.Tok("pro", 5_000_000);
                WriteFile(GraceFile(home, key), new { token = newer, exp = Now() + 3600 });
                var lease = await Acquire();                  // its token: iat 1_000_001
                Assert.Equal(1_000_001L, License.TokenIat(lease!.Token));
                Assert.Equal(newer, ReadJson(GraceFile(home, key)).GetProperty("token").GetString());
                be.Iat = 9_000_000;
                Assert.True(await lease.RefreshTokenAsync());  // a heartbeat on demand
                Assert.Equal(9_000_001L, License.TokenIat(ReadJson(GraceFile(home, key)).GetProperty("token").GetString()));
                be.Iat = 12_000_000;
                var beats = be.Bodies("heartbeat").Length;
                var end = DateTime.UtcNow.AddSeconds(12);
                while (be.Bodies("heartbeat").Length == beats && DateTime.UtcNow < end) await Task.Delay(50);
                Assert.True(be.Bodies("heartbeat").Length > beats, "the background heartbeat never ran");
                end = DateTime.UtcNow.AddSeconds(2);
                while (License.TokenIat(ReadJson(GraceFile(home, key)).GetProperty("token").GetString()) != 12_000_001L && DateTime.UtcNow < end)
                    await Task.Delay(20);
                Assert.Equal(12_000_001L, License.TokenIat(ReadJson(GraceFile(home, key)).GetProperty("token").GetString()));
                Assert.Equal(lease.Token, ReadJson(GraceFile(home, key)).GetProperty("token").GetString());
                await lease.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }
}
