using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Per-browser leases (the GitHub free tier, "1 browser at a time") vs the per-machine shared lease
// (every paid plan). Mirrors test/license-per-browser.test.ts (Node) and tests/test_license_per_browser.py.
// Hermetic: a FakeHandler behaves like the backend — a free key gets lease_scope "browser" and one live
// lease per launch_id, a paid key gets the machine-shared lease. A UNIQUE key per test keeps the
// process-static lease registry from leaking between tests.
public class LicensePerBrowserTests
{
    private sealed class Backend
    {
        public readonly string Plan;
        public readonly int Limit;
        public bool FailNetwork;
        public int HeartbeatStatus = 200;
        public int HeartbeatSec = 3600;   // far out: the background heartbeat never fires unless a test asks
        public readonly List<(string ep, JsonElement body)> Calls = new();
        public readonly Dictionary<string, (string? launch, string instance)> Live = new();
        private int _n;
        private readonly object _lock = new();

        public Backend(string plan, int limit = 1) { Plan = plan; Limit = limit; }
        private bool PerBrowser => Plan == "free";

        public static string Tok(string plan, int n) =>
            Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { v = 1, plan, n })))
                .TrimEnd('=').Replace('+', '-').Replace('/', '_') + ".sig";

        public IEnumerable<JsonElement> Bodies(string ep) { lock (_lock) return Calls.Where(c => c.ep == ep).Select(c => c.body).ToList(); }

        public FakeHandler Handler() => new(req =>
        {
            if (FailNetwork) throw new HttpRequestException("network down");
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            var raw = req.Content is null ? "{}" : req.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var body = JsonDocument.Parse(raw).RootElement.Clone();
            lock (_lock)
            {
                Calls.Add((ep, body));
                var now = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
                if (ep == "checkout")
                {
                    var launch = body.TryGetProperty("launch_id", out var l) && l.ValueKind == JsonValueKind.String ? l.GetString() : null;
                    var inst = body.GetProperty("instance_id").GetString()!;
                    if (PerBrowser && launch is null)
                        return Resp(426, "{\"code\":\"SDK_UPGRADE_REQUIRED\",\"error\":\"upgrade\"}");
                    foreach (var k in Live.Where(kv => PerBrowser ? kv.Value.launch == launch : kv.Value.instance == inst).Select(kv => kv.Key).ToList())
                        Live.Remove(k);
                    if (Live.Count >= Limit)
                        return Resp(429, "{\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\",\"error\":\"The free tier runs one browser at a time.\"}");
                    var id = $"L{++_n}";
                    Live[id] = (launch, inst);
                    var scope = PerBrowser ? ",\"lease_scope\":\"browser\"" : "";
                    return Resp(200, $"{{\"lease_id\":\"{id}\",\"token\":\"{Tok(Plan, _n)}\",\"exp\":{now + 900},\"lease_ttl_sec\":360,\"heartbeat_interval_sec\":{HeartbeatSec},\"concurrency\":{{\"used\":{Live.Count},\"limit\":{Limit}}}{scope}}}");
                }
                if (ep == "heartbeat")
                {
                    if (HeartbeatStatus == 409)
                    {
                        Live.Remove(body.GetProperty("lease_id").GetString()!);
                        return Resp(409, "{\"code\":\"LEASE_EXPIRED\"}");
                    }
                    return Resp(200, $"{{\"token\":\"{Tok(Plan, 1000 + Calls.Count)}\",\"exp\":{now + 900}}}");
                }
                if (ep == "checkin")
                {
                    Live.Remove(body.GetProperty("lease_id").GetString()!);
                    return Resp(200, "{}");
                }
                return Resp(404, "{}");
            }
        });

        private static HttpResponseMessage Resp(int status, string json) =>
            new((HttpStatusCode)status) { Content = new StringContent(json) };
    }

    private static string UniqueKey(string p) => $"cc_lic_{p}_{Guid.NewGuid():N}";
    private static Task<string?> Engine() => Task.FromResult<string?>("152.0.7977.82");
    private static Task<LeaseSession?> Acquire() => License.AcquireLeaseAsync(new LicenseOptions(), "0.28.1", true, Engine);

    private static (Sandbox s, string home, string key) Setup(Backend be)
    {
        var key = UniqueKey(be.Plan);
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key).Env("CLEARCOTE_LICENSE_API", "http://test.local").Env("CLEARCOTE_INSTANCE_ID", null).Http(be.Handler());
        var home = s.TempHome();
        return (s, home, key);
    }

    private static string CacheFile(string home, string key)
    {
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))).ToLowerInvariant()[..16];
        return Path.Combine(home, ".clearcote", $"lease-{id}.json");
    }

    private static void WriteCache(string home, string key, string token)
    {
        Directory.CreateDirectory(Path.Combine(home, ".clearcote"));
        File.WriteAllText(CacheFile(home, key), JsonSerializer.Serialize(new { token, exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800, lease_id = "OLD" }));
    }

    private static async Task<bool> WaitUntil(Func<bool> pred, int ms = 5000)
    {
        var end = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < end) { if (pred()) return true; await Task.Delay(20); }
        return pred();
    }

    // ── helpers ──────────────────────────────────────────────────────────────

    [Fact]
    public void NewLaunchId_is_unique_and_backend_shaped()
    {
        var ids = Enumerable.Range(0, 500).Select(_ => License.NewLaunchId()).ToHashSet();
        Assert.Equal(500, ids.Count);
        Assert.All(ids, id => Assert.Matches("^[A-Za-z0-9_-]{8,64}$", id));
    }

    [Fact]
    public void TokenPlan_reads_the_claim_and_never_throws()
    {
        Assert.Equal("free", License.TokenPlan(Backend.Tok("free", 1)));
        Assert.Equal("pro", License.TokenPlan(Backend.Tok("pro", 1)));
        foreach (var junk in new[] { "", ".", "not-a-token", "%%%.sig", null, Convert.ToBase64String(Encoding.UTF8.GetBytes("[1,2]")) + ".x" })
            Assert.Null(License.TokenPlan(junk));
    }

    // ── free tier: one lease per browser ─────────────────────────────────────

    [Fact]
    public async Task Free_second_browser_in_same_process_is_refused()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var b1 = await Acquire();
            Assert.False(string.IsNullOrEmpty(b1!.Token));
            await Assert.ThrowsAsync<ConcurrencyLimitError>(Acquire);
            var cos = be.Bodies("checkout").ToList();
            Assert.Equal(2, cos.Count);
            Assert.All(cos, c => Assert.Matches("^[A-Za-z0-9_-]{8,64}$", c.GetProperty("launch_id").GetString()));
            Assert.NotEqual(cos[0].GetProperty("launch_id").GetString(), cos[1].GetProperty("launch_id").GetString());
            Assert.Equal(cos[0].GetProperty("instance_id").GetString(), cos[1].GetProperty("instance_id").GetString());
            await b1.StopAsync();
        }
    }

    [Fact]
    public async Task Free_closing_a_browser_checks_in_its_lease_and_the_next_can_start()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var b1 = await Acquire();
            await b1!.StopAsync();
            var checkins = be.Bodies("checkin").ToList();
            Assert.Single(checkins);
            Assert.Equal(b1.LeaseId, checkins[0].GetProperty("lease_id").GetString());
            var b2 = await Acquire();
            Assert.NotEqual(b1.LeaseId, b2!.LeaseId);
            await b2.StopAsync();
            Assert.Empty(be.Live);
        }
    }

    [Fact]
    public async Task Free_stop_is_idempotent()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var b1 = await Acquire();
            await Task.WhenAll(Enumerable.Range(0, 8).Select(_ => b1!.StopAsync()));
            await b1!.StopAsync();
            Assert.Single(be.Bodies("checkin"));
        }
    }

    [Fact]
    public async Task Free_simultaneous_launches_exactly_one_runs()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var tasks = Enumerable.Range(0, 6).Select(_ => Task.Run(Acquire)).ToList();
            var ok = new List<LeaseSession>();
            var refused = 0;
            foreach (var t in tasks)
            {
                try { ok.Add((await t)!); }
                catch (ConcurrencyLimitError) { refused++; }
            }
            Assert.Single(ok);
            Assert.Equal(5, refused);
            Assert.Equal(6, be.Bodies("checkout").Select(b => b.GetProperty("launch_id").GetString()).Distinct().Count());
            await ok[0].StopAsync();
        }
    }

    [Fact]
    public async Task Free_higher_cap_each_browser_its_own_lease()
    {
        var be = new Backend("free", limit: 3);
        var (s, _, _) = Setup(be);
        using (s)
        {
            var bs = new[] { (await Acquire())!, (await Acquire())!, (await Acquire())! };
            Assert.Equal(3, bs.Select(b => b.LeaseId).Distinct().Count());
            Assert.Equal(3, bs.Select(b => b.Token).Distinct().Count());
            await bs[1].StopAsync();
            Assert.Equal(2, be.Live.Count);
            Assert.False(be.Live.ContainsKey(bs[1].LeaseId));
            foreach (var b in bs) await b.StopAsync();
        }
    }

    [Fact]
    public async Task Free_token_is_never_written_to_the_cache()
    {
        var be = new Backend("free");
        var (s, home, key) = Setup(be);
        using (s)
        {
            var b1 = await Acquire();
            Assert.False(File.Exists(CacheFile(home, key)));
            await b1!.StopAsync();
            Assert.False(File.Exists(CacheFile(home, key)));
        }
    }

    [Fact]
    public async Task Free_ignores_a_cached_free_token_from_an_older_sdk()
    {
        var be = new Backend("free");
        var (s, home, key) = Setup(be);
        using (s)
        {
            WriteCache(home, key, Backend.Tok("free", 99));
            var b1 = await Acquire();
            Assert.Single(be.Bodies("checkout"));
            Assert.NotEqual(Backend.Tok("free", 99), b1!.Token);
            await Assert.ThrowsAsync<ConcurrencyLimitError>(Acquire);
            await b1.StopAsync();
        }
    }

    [Fact]
    public async Task Free_has_no_offline_grace()
    {
        var be = new Backend("free") { FailNetwork = true };
        var (s, home, key) = Setup(be);
        using (s)
        {
            WriteCache(home, key, Backend.Tok("free", 7));
            await Assert.ThrowsAnyAsync<LicenseError>(Acquire);
        }
    }

    [Fact]
    public async Task Free_heartbeats_its_own_lease_and_a_409_rechecks_out_as_the_same_launch()
    {
        var be = new Backend("free") { HeartbeatSec = 1 };
        var (s, _, _) = Setup(be);
        using (s)
        {
            var b1 = await Acquire();
            var firstLaunch = be.Bodies("checkout").First().GetProperty("launch_id").GetString();
            var firstLease = b1!.LeaseId;
            // Math.Max(5, hb) seconds: wait for the first beat.
            Assert.True(await WaitUntil(() => be.Bodies("heartbeat").Any(), 8000));
            Assert.Equal(firstLease, be.Bodies("heartbeat").First().GetProperty("lease_id").GetString());
            be.HeartbeatStatus = 409;
            Assert.True(await WaitUntil(() => be.Bodies("checkout").Count() >= 2, 8000));
            Assert.Equal(firstLaunch, be.Bodies("checkout").ElementAt(1).GetProperty("launch_id").GetString());
            Assert.True(await WaitUntil(() => b1.LeaseId != firstLease, 2000));
            be.HeartbeatStatus = 200;
            await b1.StopAsync();
            var beats = be.Bodies("heartbeat").Count();
            await Task.Delay(6000);
            Assert.Equal(beats, be.Bodies("heartbeat").Count());
        }
    }

    [Fact]
    public async Task Free_a_browser_that_fails_to_start_gives_its_slot_back()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            await Assert.ThrowsAsync<InvalidOperationException>(() =>
                License.ReleaseLeaseOnFailureAsync<object>(lease, () => throw new InvalidOperationException("browser failed to start")));
            Assert.Empty(be.Live);
            var next = await Acquire();
            Assert.False(string.IsNullOrEmpty(next!.Token));
            await next.StopAsync();
        }
    }

    /// A licensed launch writes a run-token file for the engine (&lt;tmp&gt;/clearcote-rt-*.tok) before the browser
    /// starts. When the browser then failed to start, the lease was released but the file stayed in the temp
    /// directory until the process exited: a paid lease is shared, so stopping it only drops a reference.
    [Fact]
    public async Task Paid_a_launch_that_fails_to_start_leaves_no_run_token_file()
    {
        var be = new Backend("pro", limit: 5);
        var (s, _, _) = Setup(be);
        var temp = TestTemp.Create("cc-rt-");
        using (s)
        {
            s.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp);
            // There, so the launch gets as far as starting it under a lease, and not a browser, so that fails.
            var exe = Path.Combine(temp, OperatingSystem.IsWindows() ? "chrome.exe" : "chrome");
            File.WriteAllText(exe, "");
            var udd = Directory.CreateDirectory(Path.Combine(temp, "profile")).FullName;
            try
            {
                await Assert.ThrowsAnyAsync<Exception>(() =>
                    Clearcote.LaunchPersistentContextAsync(udd, new LaunchOptions { ExecutablePath = exe, Quiet = true }));
                Assert.NotEmpty(be.Bodies("checkout"));
                Assert.Empty(Directory.GetFiles(temp, "clearcote-rt-*.tok"));
            }
            finally { TestTemp.Remove(temp); }
        }
    }

    [Fact]
    public async Task ReleaseLeaseOnFailure_success_does_not_release_and_failure_rethrows_original()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            Assert.Equal(42, await License.ReleaseLeaseOnFailureAsync(lease, () => Task.FromResult(42)));
            Assert.Empty(be.Bodies("checkin"));
            var boom = new TimeoutException("x");
            var thrown = await Assert.ThrowsAsync<TimeoutException>(() => License.ReleaseLeaseOnFailureAsync<int>(lease, () => throw boom));
            Assert.Same(boom, thrown);
            Assert.Equal(7, await License.ReleaseLeaseOnFailureAsync<int>(null, () => Task.FromResult(7)));
        }
    }

    // ── paid: the machine-shared lease is unchanged ──────────────────────────

    [Fact]
    public async Task Paid_launches_share_one_checkout_and_stop_does_not_checkin()
    {
        var be = new Backend("pro", limit: 5);
        var (s, _, _) = Setup(be);
        using (s)
        {
            var hs = new[] { (await Acquire())!, (await Acquire())!, (await Acquire())! };
            Assert.Single(be.Bodies("checkout"));
            Assert.Single(hs.Select(h => h.Token).Distinct());
            foreach (var h in hs) await h.StopAsync();
            Assert.Empty(be.Bodies("checkin"));
        }
    }

    [Fact]
    public async Task Paid_cap_of_one_still_runs_many_browsers()
    {
        var be = new Backend("pro", limit: 1);
        var (s, _, _) = Setup(be);
        using (s)
        {
            for (var i = 0; i < 4; i++) Assert.False(string.IsNullOrEmpty((await Acquire())!.Token));
            Assert.Single(be.Bodies("checkout"));
        }
    }

    [Fact]
    public async Task Paid_token_is_cached_and_a_cached_paid_token_is_reused_with_zero_calls()
    {
        var be = new Backend("pro", limit: 5);
        var (s, home, key) = Setup(be);
        using (s)
        {
            var h = await Acquire();
            using var doc = JsonDocument.Parse(File.ReadAllText(CacheFile(home, key)));
            Assert.Equal(h!.Token, doc.RootElement.GetProperty("token").GetString());
        }

        var be2 = new Backend("pro");
        var (s2, home2, key2) = Setup(be2);
        using (s2)
        {
            WriteCache(home2, key2, Backend.Tok("pro", 5));
            var h2 = await Acquire();
            Assert.Equal(Backend.Tok("pro", 5), h2!.Token);
            Assert.Empty(be2.Calls);
        }
    }

    [Fact]
    public async Task Paid_checkout_body_only_adds_launch_id()
    {
        var be = new Backend("pro", limit: 5);
        var (s, _, _) = Setup(be);
        using (s)
        {
            await Acquire();
            var body = be.Bodies("checkout").Single();
            var keys = body.EnumerateObject().Select(p => p.Name).OrderBy(n => n).ToArray();
            Assert.Equal(new[] { "engine_version", "instance_id", "launch_id", "os", "sdk_version" }, keys);
        }
    }

    // ── run-token file (engine online-enforcement opt-in) ────────────────────
    // Mirrors the Node "run-token file" suite (test/license-per-browser.test.ts): BindLaunch writes
    // the current token to a per-launch file, Release removes it, StopAsync removes still-bound files,
    // and two launches on one lease get independent files.

    [Fact]
    public async Task BindLaunch_writes_the_current_token_to_a_file_and_release_removes_it()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            try
            {
                var lt = lease!.BindLaunch();
                Assert.True(File.Exists(lt.File));
                Assert.Equal(lease.Token, File.ReadAllText(lt.File)); // seeded with the current token
                lt.Release();
                Assert.False(File.Exists(lt.File));
            }
            finally { await lease!.StopAsync(); }
        }
    }

    [Fact]
    public async Task Stop_removes_any_still_bound_token_files()
    {
        var be = new Backend("free");
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            var lt = lease!.BindLaunch();
            Assert.True(File.Exists(lt.File));
            await lease.StopAsync();
            Assert.False(File.Exists(lt.File)); // CloseAll on stop
        }
    }

    [Fact]
    public async Task Two_launches_on_one_lease_get_independent_files_that_each_follow_the_token()
    {
        var be = new Backend("pro", limit: 5);
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            try
            {
                var a = lease!.BindLaunch();
                var b = lease.BindLaunch();
                Assert.NotEqual(a.File, b.File);
                Assert.Equal(lease.Token, File.ReadAllText(a.File));
                Assert.Equal(lease.Token, File.ReadAllText(b.File));
                a.Release();
                Assert.False(File.Exists(a.File));
                Assert.True(File.Exists(b.File)); // b is independent
                b.Release();
            }
            finally { await lease!.StopAsync(); }
        }
    }

    [Fact]
    public async Task Release_is_idempotent_and_leaves_other_files_intact()
    {
        var be = new Backend("pro", limit: 5);
        var (s, _, _) = Setup(be);
        using (s)
        {
            var lease = await Acquire();
            try
            {
                var a = lease!.BindLaunch();
                var b = lease.BindLaunch();
                a.Release();
                a.Release(); // safe to call twice
                Assert.False(File.Exists(a.File));
                Assert.True(File.Exists(b.File));
                b.Release();
            }
            finally { await lease!.StopAsync(); }
        }
    }
}
