using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Twins of Node and Python licence tests the .NET suite did not have (the behaviour was already in License.cs),
// plus the licence-key / device-login record API, LicenseError.Status, the fallback skip kept per network path,
// and the lease cache written atomically. Hermetic like LicensePaidBrowserTests: a FakeHandler plays the lease
// server (lease.test) and the fallback (www.test), HOME/USERPROFILE/LOCALAPPDATA are a temp dir, a unique key
// per test keeps the lease registry apart, and every test shuts its leases down.
public class LicenseParityTests : IDisposable
{
    private readonly (string, string, TimeSpan, TimeSpan[]) _seams =
        (License.DefaultLeaseBase, License.FallbackLeaseBase, License.PrimarySkipFor, License.RateLimitBackoff);

    public LicenseParityTests()
    {
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

    private static HttpResponseMessage Resp(int status, string body, string contentType = "application/json") =>
        new((HttpStatusCode)status) { Content = new StringContent(body, Encoding.UTF8, contentType) };

    /// The licence server as SDK 0.55 meets it (see LicensePaidBrowserTests.Backend), with scripted heartbeat
    /// answers and a lease server (lease.test) that can go down on its own while the fallback (www.test) answers.
    private sealed class Backend
    {
        public string Plan = "pro";
        public bool Uncapped = true;
        public bool Down;                   // neither base answers
        public bool PrimaryDown;            // lease.test answers 503, www.test still answers
        public int HeartbeatSec = 3600;
        public long Iat = 1_000_000;
        public readonly Queue<Func<HttpResponseMessage>> Checkouts = new();
        public readonly Queue<Func<HttpResponseMessage>> Heartbeats = new();
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
                if (PrimaryDown && req.RequestUri.Host == "lease.test") return Resp(503, "<html>503</html>", "text/html");
                if (ep == "checkout")
                {
                    if (Checkouts.Count > 0) return Checkouts.Dequeue()();
                    var offers = body.TryGetProperty("lease_scope_supported", out var ls) && ls.ValueKind == JsonValueKind.Array
                                 && ls.EnumerateArray().Any(x => x.GetString() == "browser")
                                 && body.TryGetProperty("launch_id", out var l) && l.ValueKind == JsonValueKind.String;
                    var browser = Plan == "free" || (Uncapped && offers);
                    var answer = new Dictionary<string, object?>
                    {
                        ["lease_id"] = $"L{++_n}", ["token"] = Tok(Plan, ++Iat), ["exp"] = Now() + 86400,
                        ["heartbeat_interval_sec"] = HeartbeatSec, ["concurrency"] = new { limit = 0 },
                    };
                    if (browser) answer["lease_scope"] = "browser";
                    return Resp(200, JsonSerializer.Serialize(answer));
                }
                if (ep == "heartbeat")
                {
                    if (Heartbeats.Count > 0) return Heartbeats.Dequeue()();
                    return Resp(200, JsonSerializer.Serialize(new { token = Tok(Plan, ++Iat), exp = Now() + 86400 }));
                }
                return Resp(200, "{}");
            }
        });
    }

    private static (Sandbox S, string Home, string Key) Setup(Backend be)
    {
        var key = $"cc_lic_par_{Guid.NewGuid():N}";
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key).Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", null)
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Env("CLEARCOTE_INSTANCE_ID", null).Http(be.Handler());
        var home = s.TempHome();
        s.Env("LOCALAPPDATA", home);   // the engine's high-water mark: none
        return (s, home, key);
    }

    private static Task<LeaseSession?> Acquire(bool quiet = true) =>
        License.AcquireLeaseAsync(new LicenseOptions(), "0.55.1", quiet, () => Task.FromResult<string?>("155.0.7700.0"));

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

    // ── the lease server's fallback (node license-lease-base.test.ts, py test_license_lease_base.py) ──────

    [Fact]
    public async Task Heartbeats_fall_back_to_the_website_too()
    {
        var be = new Backend();
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var lease = await Acquire();
                be.PrimaryDown = true;
                Assert.True(await lease!.RefreshTokenAsync());   // a heartbeat of this browser's lease
                Assert.Equal(new[] { ("lease.test", "checkout"), ("lease.test", "heartbeat"), ("www.test", "heartbeat") },
                    be.Calls().Select(c => (c.Host, c.Ep)));
                await lease.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task The_lease_base_does_not_move_the_pro_download()
    {
        if (Release.ProPlatformKey() is null) return;   // no PRO build for this platform
        string? seen = null;
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", "https://lease-env.test")
            .Http(new FakeHandler(req => { seen ??= req.RequestUri!.ToString(); return Resp(401, "{}"); }));
        s.TempHome();
        Download.ForgetProMeta();
        try
        {
            await Assert.ThrowsAnyAsync<Exception>(() => Download.ProEnsureBinaryAsync("cc_lic_x",
                new ProDownloadOptions { CacheDir = Path.Combine(Environment.GetEnvironmentVariable("HOME")!, "cache"), Quiet = true }));
            Assert.StartsWith("https://www.clearcotelabs.com/api/v1/download/pro?", seen);
        }
        finally { Download.ForgetProMeta(); }
    }

    [Fact]
    public async Task A_lease_server_skipped_on_the_direct_path_is_still_tried_through_the_proxy()
    {
        // license.ts keys the 60 s skip by primary|via-proxy-server: one network path failing says nothing about another.
        await using var fallback = new TestOrigin(_ => (200,
            $"{{\"lease_id\":\"L-fb\",\"token\":\"tok-fallback\",\"exp\":{Now() + 3600},\"heartbeat_interval_sec\":3600}}"));
        await using var proxy = new TestHttpProxy();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", null)
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Env("CLEARCOTE_INSTANCE_ID", "test-instance");
        s.Env("LOCALAPPDATA", s.TempHome());
        License.DefaultLeaseBase = "http://127.0.0.1:1";   // nothing listens: connection refused
        License.FallbackLeaseBase = $"http://127.0.0.1:{fallback.Port}";
        try
        {
            await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = $"cc_lic_d_{Guid.NewGuid():N}" }, "0.55.1", true);
            Assert.Single(fallback.Log);   // direct: the primary failed, now skipped on the direct path
            await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = $"cc_lic_p_{Guid.NewGuid():N}", LicenseThroughProxy = true },
                "0.55.1", true, proxy: new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" });
            Assert.Equal(new[] { "http://127.0.0.1:1/api/v1/lease/checkout", $"{License.FallbackLeaseBase}/api/v1/lease/checkout" },
                proxy.Log.Select(h => h.Target));   // the proxied path asked the primary first all the same
            await License.AcquireLeaseAsync(new LicenseOptions { LicenseKey = $"cc_lic_p2_{Guid.NewGuid():N}", LicenseThroughProxy = true },
                "0.55.1", true, proxy: new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" });
            Assert.Equal($"{License.FallbackLeaseBase}/api/v1/lease/checkout", proxy.Log.Last().Target);   // and now skips it
            Assert.Equal(3, proxy.Log.Count);
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    // ── paid per-browser leases (node license-paid-browser.test.ts, py test_license_paid_per_browser.py) ──

    [Fact]
    public async Task Five_concurrent_launches_each_hold_their_own_lease()
    {
        var be = new Backend();
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var leases = await Task.WhenAll(Enumerable.Range(0, 5).Select(_ => Acquire()));
                Assert.Equal(5, be.Bodies("checkout").Length);
                Assert.Equal(5, leases.Select(l => l!.LeaseId).Distinct().Count());
                foreach (var l in leases) await l!.StopAsync();
                Assert.Equal(5, be.Bodies("checkin").Length);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_rate_limited_heartbeat_just_waits_for_the_next_beat()
    {
        var be = new Backend { HeartbeatSec = 1 };   // the loop's floor: a beat every 5 s
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var lease = await Acquire();
                var (t0, id0) = (lease!.Token, lease.LeaseId);
                be.Heartbeats.Enqueue(() => Resp(429, RateLimited));
                var end = DateTime.UtcNow.AddSeconds(8);
                while (be.Bodies("heartbeat").Length < 1 && DateTime.UtcNow < end) await Task.Delay(50);
                Assert.Single(be.Bodies("heartbeat"));
                await Task.Delay(100);
                Assert.Equal(t0, lease.Token);                  // nothing rotated
                Assert.Single(be.Bodies("checkout"));           // and no re-checkout on RATE_LIMITED
                end = DateTime.UtcNow.AddSeconds(8);
                while (be.Bodies("heartbeat").Length < 2 && DateTime.UtcNow < end) await Task.Delay(50);
                end = DateTime.UtcNow.AddSeconds(2);
                while (lease.Token == t0 && DateTime.UtcNow < end) await Task.Delay(20);
                Assert.NotEqual(t0, lease.Token);               // the next beat rotated it
                Assert.Equal(id0, lease.LeaseId);
                await lease.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task Rate_limited_maps_to_its_own_code_and_every_answer_error_carries_its_status()
    {
        var be = new Backend();
        for (var i = 0; i < 4; i++) be.Checkouts.Enqueue(() => Resp(429, RateLimited));
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var e = await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.IsNotType<ConcurrencyLimitError>(e);
                Assert.Equal(("RATE_LIMITED", (int?)429), (e.Code, e.Status));
                be.Checkouts.Enqueue(() => Resp(429, "{\"error\":\"limit\",\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}"));
                Assert.Equal(429, (await Assert.ThrowsAsync<ConcurrencyLimitError>(() => Acquire())).Status);
                be.Checkouts.Enqueue(() => Resp(403, "{\"error\":\"License revoked.\",\"code\":\"LICENSE_REVOKED\"}"));
                Assert.Equal(403, (await Assert.ThrowsAsync<LicenseRevokedError>(() => Acquire())).Status);
                be.Checkouts.Enqueue(() => Resp(426, "{\"error\":\"Upgrade.\",\"code\":\"SDK_UPGRADE_REQUIRED\"}"));
                Assert.Equal(426, (await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire())).Status);
                be.Down = true;   // never answered: no status
                Assert.Null((await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire())).Status);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_challenge_with_no_usable_token_is_still_a_LicenseServerChallengeError()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                await (await Acquire())!.StopAsync();      // a paid browser: the scope is known, a grace token kept
                for (var i = 0; i < 2; i++)                 // the lease server, then the fallback
                    be.Checkouts.Enqueue(() => Resp(403, "<html>Access denied</html>", "text/html"));
                WriteFile(GraceFile(home, key), new { token = "x", exp = 0 });   // nothing usable left
                var e = await Assert.ThrowsAsync<LicenseServerChallengeError>(() => Acquire());
                Assert.Equal(403, e.Status);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task The_uncounted_report_is_kept_until_a_checkout_succeeds()
    {
        var be = new Backend();
        var (s, _, _) = Setup(be);
        using (s)
        {
            try
            {
                var first = await Acquire();
                be.Down = true;
                Assert.Equal("uncounted", (await Acquire())!.LeaseId);
                be.Down = false;
                License.ForgetLeaseServerDown();
                be.Checkouts.Enqueue(() => Resp(401, "{\"error\":\"Invalid licence key.\",\"code\":\"INVALID_LICENSE\"}"));
                await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.Equal(1, be.Bodies("checkout")[^1].GetProperty("uncounted_launches").GetInt32());
                var counted = await Acquire();               // not a 200 before: still owed
                Assert.Equal(1, be.Bodies("checkout")[^1].GetProperty("uncounted_launches").GetInt32());
                var next = await Acquire();
                Assert.False(be.Bodies("checkout")[^1].TryGetProperty("uncounted_launches", out _));
                foreach (var l in new[] { first, counted, next }) await l!.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task An_uncounted_browser_left_open_loses_its_token_file_at_exit()
    {
        var be = new Backend { Down = true };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                WriteFile(GraceFile(home, key), new { token = Backend.Tok("pro", 4), exp = Now() + 3600 });
                var lease = await Acquire();
                Assert.Equal("uncounted", lease!.LeaseId);
                var file = lease.BindLaunch();
                Assert.True(File.Exists(file.File));
                await License.ShutdownAllLeasesAsync();      // process exit
                Assert.False(File.Exists(file.File));
                Assert.Empty(be.Bodies("checkin"));          // no lease: nothing to check in
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_paid_browser_whose_grace_token_is_about_to_expire_raises()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var first = await Acquire();
                File.Delete(GraceFile(home, key));
                WriteFile(GraceFile(home, key), new { token = Backend.Tok("pro", 10_000_000_000), exp = Now() + 30 });  // inside the 60 s margin
                be.Down = true;
                var e = await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
                Assert.StartsWith("Could not reach the license server to start this browser", e.Message);
                await first!.StopAsync();
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_corrupt_grace_file_is_no_token()
    {
        var be = new Backend { Down = true };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(GraceFile(home, key))!);
                File.WriteAllText(GraceFile(home, key), "{not json");
                await Assert.ThrowsAnyAsync<LicenseError>(() => Acquire());
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── the shared cache's machine mark (py test_license_paid_per_browser.py) ──────────────────────────────

    [Fact]
    public async Task Keeping_an_unmarked_fallback_alive_never_marks_it()
    {
        var be = new Backend { Uncapped = false };
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                var old = Backend.Tok("pro", 2);
                WriteFile(LeaseFile(home, key), new { token = old, exp = Now() + 3600, lease_id = "L-old" });
                be.Down = true;
                var lease = await Acquire();                 // adopted as before: this process now keeps L-old alive
                Assert.Equal(old, lease!.Token);
                be.Down = false;
                License.ForgetLeaseServerDown();
                Assert.True(await lease.RefreshTokenAsync()); // one beat: a new token for the same lease
                var rotated = ReadJson(LeaseFile(home, key));
                Assert.Equal("L-old", rotated.GetProperty("lease_id").GetString());
                Assert.NotEqual(old, rotated.GetProperty("token").GetString());
                Assert.False(rotated.TryGetProperty("scope", out _));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_refresh_keeps_the_marked_cache_marked()
    {
        var be = new Backend();
        var (s, home, key) = Setup(be);
        using (s)
        {
            try
            {
                WriteFile(LeaseFile(home, key), new { token = Backend.Tok("pro", 1000), exp = Now() + 3600, lease_id = "L-m", scope = "machine" });
                File.WriteAllText(Path.Combine(home, ".clearcote", ".cc_hwm"), "1500");   // the engine accepted a newer token here
                var lease = await Acquire();
                Assert.Equal(new[] { "heartbeat" }, be.Calls().Select(c => c.Ep));
                var cached = ReadJson(LeaseFile(home, key));
                Assert.Equal(lease!.Token, cached.GetProperty("token").GetString());
                Assert.Equal("machine", cached.GetProperty("scope").GetString());
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── torn files: the grace file and the lease cache are replaced atomically ──────────────────────────────

    [Theory]
    [InlineData("grace")]
    [InlineData("lease")]
    public async Task A_reader_never_sees_a_half_written_file(string which)
    {
        using var s = new Sandbox();
        var home = s.TempHome();
        var key = $"cc_lic_torn_{Guid.NewGuid():N}";
        var path = which == "grace" ? GraceFile(home, key) : LeaseFile(home, key);
        var exp = Now() + 3600;
        void Write(long i)
        {
            if (which == "grace") License.WriteGrace(key, Backend.Tok("pro", 1000 + i), exp);
            else License.WriteCache(key, Backend.Tok("pro", 1000 + i), exp, $"L{i}-" + new string('y', 4096), machine: true);
        }
        Write(0);
        var torn = 0;
        var done = false;
        var writer = Task.Run(() => { for (var i = 1; i < 400; i++) Write(i); Volatile.Write(ref done, true); });
        while (!Volatile.Read(ref done))
        {
            try { using var _ = JsonDocument.Parse(File.ReadAllText(path)); }
            catch (JsonException) { torn++; }
            catch (IOException) { /* Windows: the rename and the read crossed; a real reader treats it as a miss */ }
            catch (UnauthorizedAccessException) { }
        }
        await writer;
        Assert.Equal(0, torn);
        Assert.Equal(1399L, License.TokenIat(ReadJson(path).GetProperty("token").GetString()));
        Assert.Empty(Directory.GetFiles(Path.GetDirectoryName(path)!, "*.tmp-*"));
    }

    // ── the proxied path (py test_license_challenge.py) ────────────────────────────────────────────────────

    [Fact]
    public async Task The_proxied_path_detects_the_challenge_too()
    {
        const string checkpoint = "<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head><body></body></html>";
        await using var origin = new TestOrigin(_ => (403, checkpoint));
        await using var proxy = new TestHttpProxy();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_LICENSE_API", null).Env("CLEARCOTE_LEASE_API", null)
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Env("CLEARCOTE_INSTANCE_ID", "test-instance");
        s.Env("LOCALAPPDATA", s.TempHome());
        try
        {
            var e = await Assert.ThrowsAsync<LicenseServerChallengeError>(() => License.AcquireLeaseAsync(
                new LicenseOptions { LicenseKey = $"cc_lic_pc_{Guid.NewGuid():N}", LicenseApiBase = $"http://127.0.0.1:{origin.Port}", LicenseThroughProxy = true },
                "0.55.1", true, proxy: new ProxyOptions { Server = $"http://127.0.0.1:{proxy.Port}" }));
            Assert.Equal(403, e.Status);
            Assert.NotEmpty(proxy.Log);      // it did go through the proxy
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    // ── the saved key and the device-login record (license.ts licenseKeyPath ... licenseExpiry) ────────────

    [Fact]
    public void The_saved_key_round_trips_through_the_same_file_the_other_SDKs_use()
    {
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null);
        var home = s.TempHome();
        Assert.Equal(Path.Combine(home, ".clearcote", "license.key"), License.LicenseKeyPath());
        Assert.Equal(new LicenseKeySourceInfo("none", null), License.LicenseKeySource());
        Assert.Equal("LICENSE_EMPTY", Assert.Throws<LicenseError>(() => License.SaveLicenseKey("   ")).Code);
        var p = License.SaveLicenseKey("  cc_lic_abcdefghijklmnop  ");
        Assert.Equal(License.LicenseKeyPath(), p);
        Assert.Equal("cc_lic_abcdefghijklmnop\n", File.ReadAllText(p));
        if (!OperatingSystem.IsWindows())
            Assert.Equal(UnixFileMode.UserRead | UnixFileMode.UserWrite, File.GetUnixFileMode(p));
        Assert.Equal("cc_lic_abcdefghijklmnop", License.ResolveLicenseKey());
        Assert.Equal(new LicenseKeySourceInfo("file", "cc_lic_\u2026mnop"), License.LicenseKeySource());
        s.Env("CLEARCOTE_LICENSE_KEY", "cc_lic_fromtheenvironment");
        Assert.Equal(new LicenseKeySourceInfo("env", "cc_lic_\u2026ment"), License.LicenseKeySource());
        Assert.Equal(new LicenseKeySourceInfo("option", "\u2026"), License.LicenseKeySource(" short "));
        s.Env("CLEARCOTE_LICENSE_KEY", null);
        License.SaveLicenseMeta("cc_lic_abcdefghijklmnop", "pro", "2027-01-01T00:00:00Z");
        Assert.True(License.RemoveLicenseKey());
        Assert.False(File.Exists(p));
        Assert.False(File.Exists(License.LicenseMetaPath()));   // the record of that key goes with it
        Assert.False(License.RemoveLicenseKey());
    }

    [Fact]
    public void The_device_login_record_describes_only_its_own_key()
    {
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null);
        var home = s.TempHome();
        Assert.Null(License.LicenseExpiry());                        // no key at all
        Assert.Equal(Path.Combine(home, ".clearcote", "license.meta.json"), License.LicenseMetaPath());
        var p = License.SaveLicenseMeta(" cc_lic_one ", "pro", "2027-01-01T00:00:00Z");
        using (var doc = JsonDocument.Parse(File.ReadAllText(p)))
        {
            var r = doc.RootElement;
            Assert.Equal(new[] { "key_sha256", "plan", "expires_at", "source", "recorded_at" }, r.EnumerateObject().Select(x => x.Name));
            Assert.Equal(Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes("cc_lic_one"))).ToLowerInvariant(),
                r.GetProperty("key_sha256").GetString());
            Assert.Equal("device-login", r.GetProperty("source").GetString());
            Assert.Matches(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", r.GetProperty("recorded_at").GetString());
            Assert.DoesNotContain("cc_lic_one", File.ReadAllText(p));
        }
        var mine = License.LicenseExpiry("cc_lic_one");
        Assert.Equal(("device-login", "2027-01-01T00:00:00Z"), (mine!.Source, mine.ExpiresAt));
        Assert.NotNull(mine.RecordedAt);
        Assert.Equal("unknown", License.LicenseExpiry("cc_lic_two")!.Source);
        License.SaveLicenseMeta("cc_lic_one", "pro", null);
        Assert.Null(License.LicenseExpiry("cc_lic_one")!.ExpiresAt);   // recorded: no expiry
        Assert.False(License.RemoveLicenseMeta(unlessKey: "cc_lic_one"));
        Assert.True(File.Exists(License.LicenseMetaPath()));
        Assert.True(License.RemoveLicenseMeta(unlessKey: "cc_lic_two"));
        Assert.False(License.RemoveLicenseMeta());
    }
}
