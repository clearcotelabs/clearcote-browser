using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// The PRO engine refuses a run-token older than the newest it has accepted for this OS user.
//
// Its clock-rollback guard (patch 990) keeps that newest iat in $LOCALAPPDATA/.clearcote/.cc_hwm (else $HOME)
// and refuses any lower one: "this run-token is older than the last one accepted here (system clock set back?);
// refusing." The SDK reuses a cached token for its 24 h life, and anything else under the same OS user that
// launches with a newer token (another process, the hosted-browser gateway, a run with another key) moves the
// mark past it. On a production worker that failed every job until the cache was deleted by hand.
//
// So (1) AcquireLeaseAsync replaces a token it can see is behind the mark, preferring a heartbeat of the lease
// it knows (same lease, nothing revoked) over a checkout; and (2) a launch the engine refuses anyway (a race)
// refreshes the token and launches once more. Mirrors the Python and Node suites; the same scenario ran end to
// end against the real r29 engine. Hermetic: FakeHandler, HOME/USERPROFILE/LOCALAPPDATA point at a temp dir.
public class LicenseStaleTokenTests
{
    private const string Refusal =
        "Target page, context or browser has been closed\nBrowser logs:\n[pid=1][err] [clearcote] licence: this run-token " +
        "is older than the last one accepted here (system clock set back?); refusing.";

    private static string Tok(long iat, string plan = "pro") =>
        Convert.ToBase64String(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { v = 1, plan, iat })))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_') + ".sig";

    private static long Exp() => DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 3600;
    private static string UniqueKey() => $"cc_lic_stale_{Guid.NewGuid():N}";

    private static HttpResponseMessage Resp(int status, object body) =>
        new((HttpStatusCode)status) { Content = new StringContent(JsonSerializer.Serialize(body)) };

    /// Records (endpoint, body); answers[endpoint] are served in order, the last one repeating.
    private static FakeHandler Backend(List<(string Ep, JsonElement Body)> calls, Dictionary<string, Queue<(int, object)>> answers) =>
        new(req =>
        {
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            var body = req.Content is null ? "{}" : req.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            lock (calls) calls.Add((ep, JsonDocument.Parse(body).RootElement.Clone()));
            if (!answers.TryGetValue(ep, out var q) || q.Count == 0) return Resp(200, new { });
            var (status, answer) = q.Count > 1 ? q.Dequeue() : q.Peek();
            return Resp(status, answer);
        });

    /// A temp HOME that is also LOCALAPPDATA (the engine mark lives there), for a fresh key.
    private static (Sandbox S, string Home, string Key) Isolate(FakeHandler http)
    {
        var key = UniqueKey();
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key).Env("CLEARCOTE_LICENSE_API", "http://test.local")
            .Env("CLEARCOTE_INSTANCE_ID", null).Http(http);
        var home = s.TempHome();
        s.Env("LOCALAPPDATA", home);
        return (s, home, key);
    }

    private static void SetMark(string home, long iat)
    {
        Directory.CreateDirectory(Path.Combine(home, ".clearcote"));
        File.WriteAllText(Path.Combine(home, ".clearcote", ".cc_hwm"), iat.ToString());
    }

    private static void CacheToken(string home, string key, long iat, string? leaseId = "L1")
    {
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))).ToLowerInvariant()[..16];
        Directory.CreateDirectory(Path.Combine(home, ".clearcote"));
        File.WriteAllText(Path.Combine(home, ".clearcote", $"lease-{id}.json"),
            JsonSerializer.Serialize(new { token = Tok(iat), exp = Exp(), lease_id = leaseId, scope = "machine" }));
    }

    private static Task<LeaseSession?> Acquire() =>
        License.AcquireLeaseAsync(new LicenseOptions(), "0.33.0", true, () => Task.FromResult<string?>("153.0.8010.53"));

    // ── reading the token and the engine's mark ──────────────────────────────────────────────────────

    [Fact]
    public void Token_iat_is_read_from_the_payload()
    {
        Assert.Equal(1790685547L, License.TokenIat(Tok(1790685547)));
        Assert.Null(License.TokenIat("not-a-token"));
        Assert.Null(License.TokenIat(null));
    }

    [Fact]
    public void Engine_mark_is_found_where_the_engine_keeps_it()
    {
        var root = TestTemp.Create("cc-stale-");
        try
        {
            string local = Path.Combine(root, "local"), home = Path.Combine(root, "home");
            foreach (var (d, v) in new[] { (local, "200"), (home, "100") })
            {
                Directory.CreateDirectory(Path.Combine(d, ".clearcote"));
                File.WriteAllText(Path.Combine(d, ".clearcote", ".cc_hwm"), v + "\n");
            }
            Assert.Equal(200, License.EngineHwm(new Dictionary<string, string?> { ["LOCALAPPDATA"] = local, ["HOME"] = home }));
            Assert.Equal(100, License.EngineHwm(new Dictionary<string, string?> { ["HOME"] = home }));
            Assert.Equal(100, License.EngineHwm(new Dictionary<string, string?> { ["LOCALAPPDATA"] = "", ["HOME"] = home }));
            Assert.Equal(0, License.EngineHwm(new Dictionary<string, string?> { ["HOME"] = Path.Combine(root, "none") }));
            Assert.Equal(0, License.EngineHwm(new Dictionary<string, string?>()));
            File.WriteAllText(Path.Combine(home, ".clearcote", ".cc_hwm"), "garbage");
            Assert.Equal(0, License.EngineHwm(new Dictionary<string, string?> { ["HOME"] = home }));
        }
        finally { TestTemp.Remove(root); }
    }

    // ── (1) acquire: a token the engine would refuse is replaced before the launch ─────────────────────

    [Fact]
    public async Task A_cached_token_behind_the_mark_is_refreshed_by_heartbeating_its_lease()
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, home, key) = Isolate(Backend(calls, new() { ["heartbeat"] = new(new[] { (200, (object)new { token = Tok(2000), exp = Exp() }) }) }));
        using (s)
        {
            CacheToken(home, key, 1000, "L-owner");
            SetMark(home, 1500);   // another process launched with a newer token
            try
            {
                var h = await Acquire();
                Assert.Equal(new[] { "heartbeat" }, calls.Select(c => c.Ep).ToArray());   // no checkout: nothing revoked
                Assert.Equal("L-owner", calls[0].Body.GetProperty("lease_id").GetString());
                Assert.Equal(2000L, License.TokenIat(h!.Token));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Theory]
    [InlineData(1000L)]   // equal: the engine only refuses iat < mark
    [InlineData(900L)]
    public async Task A_token_at_or_past_the_mark_is_reused_with_no_backend_call(long mark)
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, home, key) = Isolate(Backend(calls, new()));
        using (s)
        {
            CacheToken(home, key, 1000);
            SetMark(home, mark);
            try
            {
                Assert.Equal(1000L, License.TokenIat((await Acquire())!.Token));
                Assert.Empty(calls);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_gone_lease_falls_back_to_a_checkout()
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, home, key) = Isolate(Backend(calls, new()
        {
            ["heartbeat"] = new(new[] { (409, (object)new { code = "LEASE_NOT_FOUND" }) }),
            ["checkout"] = new(new[] { (200, (object)new { lease_id = "L-new", token = Tok(2000), exp = Exp(), heartbeat_interval_sec = 3600 }) }),
        }));
        using (s)
        {
            CacheToken(home, key, 1000, "L-dead");
            SetMark(home, 1500);
            try
            {
                var h = await Acquire();
                Assert.Equal(new[] { "heartbeat", "checkout" }, calls.Select(c => c.Ep).ToArray());
                Assert.Equal(2000L, License.TokenIat(h!.Token));
                Assert.Equal("L-new", h.LeaseId);
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task A_definitive_refusal_while_refreshing_surfaces_instead_of_a_doomed_launch()
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, home, key) = Isolate(Backend(calls, new()
        {
            ["heartbeat"] = new(new[] { (409, (object)new { code = "LEASE_EXPIRED" }) }),
            ["checkout"] = new(new[] { (429, (object)new { error = "limit", code = "CONCURRENCY_LIMIT_EXCEEDED" }) }),
        }));
        using (s)
        {
            CacheToken(home, key, 1000, "L-dead");
            SetMark(home, 1500);
            try { await Assert.ThrowsAsync<ConcurrencyLimitError>(Acquire); }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    [Fact]
    public async Task The_machine_handle_refreshes_on_demand_even_when_it_cannot_see_the_mark()
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, home, key) = Isolate(Backend(calls, new() { ["heartbeat"] = new(new[] { (200, (object)new { token = Tok(2000), exp = Exp() }) }) }));
        using (s)
        {
            CacheToken(home, key, 1000, "L1");
            try
            {
                var h = await Acquire();
                Assert.Empty(calls);
                Assert.True(await h!.RefreshTokenAsync());
                Assert.Equal(2000L, License.TokenIat(h.Token));
            }
            finally { await License.ShutdownAllLeasesAsync(); }
        }
    }

    // ── (2) launch: the engine refused anyway (a race) -> fresh token, one more launch ─────────────────

    private static (LeaseSession Lease, Func<int> Refreshes) FakeLease(bool ok = true)
    {
        var token = "TOK-OLD";
        var refreshes = 0;
        var lease = new LeaseSession(() => token, "L1", () => Task.CompletedTask, () => null!, () =>
        {
            refreshes++;
            if (ok) token = "TOK-FRESH";
            return Task.FromResult(ok);
        });
        return (lease, () => refreshes);
    }

    [Fact]
    public async Task A_refused_launch_is_retried_once_with_a_fresh_token()
    {
        var (lease, refreshes) = FakeLease();
        var seen = new List<string>();
        var result = await License.RetryOnStaleRunTokenAsync(lease, () =>
        {
            seen.Add(lease.Token);
            if (seen.Count == 1) throw new InvalidOperationException(Refusal);
            return Task.FromResult("context");
        });
        Assert.Equal("context", result);
        Assert.Equal(new[] { "TOK-OLD", "TOK-FRESH" }, seen);
        Assert.Equal(1, refreshes());
    }

    [Theory]
    [InlineData("other error")]
    [InlineData("refresh failed")]
    [InlineData("no lease")]
    [InlineData("still refused")]
    public async Task Anything_else_is_raised_as_it_was_with_at_most_one_retry(string c)
    {
        var (fake, refreshes) = FakeLease(ok: c != "refresh failed");
        var lease = c == "no lease" ? null : fake;
        var attempts = 0;
        await Assert.ThrowsAsync<InvalidOperationException>(() => License.RetryOnStaleRunTokenAsync<string>(lease, () =>
        {
            attempts++;
            throw new InvalidOperationException(c == "other error" ? "spawn UNKNOWN" : Refusal);
        }));
        Assert.Equal(c == "still refused" ? 2 : 1, attempts);
        if (c == "other error") Assert.Equal(0, refreshes());
    }

    // ── the per-browser (free) lease refreshes on demand too ──────────────────────────────────────────

    [Fact]
    public async Task A_browser_lease_refreshes_by_heartbeat_or_retakes_its_own_slot()
    {
        var calls = new List<(string Ep, JsonElement Body)>();
        var (s, _, _) = Isolate(Backend(calls, new()
        {
            ["checkout"] = new(new[]
            {
                (200, (object)new { lease_id = "L-b1", token = Tok(1000, "free"), exp = Exp(), heartbeat_interval_sec = 3600, lease_scope = "browser" }),
                (200, (object)new { lease_id = "L-b2", token = Tok(3000, "free"), exp = Exp(), heartbeat_interval_sec = 3600, lease_scope = "browser" }),
            }),
            ["heartbeat"] = new(new[] { (200, (object)new { token = Tok(2000, "free") }), (409, (object)new { code = "LEASE_EXPIRED" }) }),
        }));
        using (s)
        {
            var b = await Acquire();
            try
            {
                Assert.True(await b!.RefreshTokenAsync());
                Assert.Equal(2000L, License.TokenIat(b.Token));
                Assert.True(await b.RefreshTokenAsync());
                Assert.Equal(3000L, License.TokenIat(b.Token));
                Assert.Equal("L-b2", b.LeaseId);
                var checkouts = calls.Where(x => x.Ep == "checkout").ToArray();
                Assert.Equal(checkouts[0].Body.GetProperty("launch_id").GetString(),
                    checkouts[1].Body.GetProperty("launch_id").GetString());   // the SAME launch retakes it
            }
            finally
            {
                await b!.StopAsync();
                await License.ShutdownAllLeasesAsync();
            }
        }
    }
}
