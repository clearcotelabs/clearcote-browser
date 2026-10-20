using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Per-machine token reuse + telemetry split (SDK 0.17.x). Mirrors the Python test_license_reuse.py
// and Node license-reuse.test.ts suites. Hermetic: the injected FakeHandler captures checkout bodies;
// a UNIQUE license key per test keeps the process-static machine-lease registry from leaking.
public class LicenseReuseTests
{
    // A mock license backend: records endpoints + checkout bodies, returns a token. heartbeat_interval
    // is set far out so the background heartbeat never fires mid-test.
    private static FakeHandler LeaseBackend(List<string> endpoints, List<JsonElement> checkoutBodies,
        HttpStatusCode checkoutStatus = HttpStatusCode.OK, string checkoutJson = "")
    {
        return new FakeHandler(req =>
        {
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            endpoints.Add(ep);
            var body = req.Content is null ? "{}" : req.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            if (ep == "checkout")
            {
                checkoutBodies.Add(JsonDocument.Parse(body).RootElement.Clone());
                if (checkoutStatus != HttpStatusCode.OK)
                    return new HttpResponseMessage(checkoutStatus) { Content = new StringContent(checkoutJson) };
                var exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800;
                return new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent($"{{\"lease_id\":\"L1\",\"token\":\"TOK\",\"exp\":{exp},\"heartbeat_interval_sec\":3600}}"),
                };
            }
            return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{}") };
        });
    }

    private static string UniqueKey(string p) => $"cc_lic_{p}_{Guid.NewGuid():N}";

    private static void WriteCacheFile(string home, string key, object obj)
    {
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))).ToLowerInvariant()[..16];
        var dir = Path.Combine(home, ".clearcote");
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, $"lease-{id}.json"), JsonSerializer.Serialize(obj));
    }

    private static Task<string?> Engine(string v) => Task.FromResult<string?>(v);

    [Fact]
    public async Task Shares_one_checkout_across_launches_and_stop_does_not_checkin()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("reuse"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(LeaseBackend(eps, bodies));
        s.TempHome();
        var o = new LicenseOptions();
        var h1 = await License.AcquireLeaseAsync(o, "0.17.1", true, () => Engine("150.0.7871.114"));
        var h2 = await License.AcquireLeaseAsync(o, "0.17.1", true, () => Engine("150.0.7871.114"));
        var h3 = await License.AcquireLeaseAsync(o, "0.17.1", true, () => Engine("150.0.7871.114"));
        Assert.Equal(1, eps.Count(e => e == "checkout"));       // the whole point
        Assert.Equal("TOK", h1!.Token);
        Assert.Equal(h1.Token, h2!.Token);
        await h1.StopAsync(); await h2.StopAsync(); await h3!.StopAsync();
        Assert.Equal(0, eps.Count(e => e == "checkin"));         // no per-launch checkin
    }

    [Fact]
    public async Task Free_mode_no_key_returns_null_and_makes_no_calls()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Http(LeaseBackend(eps, bodies));
        s.TempHome();
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
        Assert.Null(lease);
        Assert.Empty(eps);
    }

    [Fact]
    public async Task Throws_on_concurrency_limit_cold_checkout()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("limit"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local")
            .Http(LeaseBackend(eps, bodies, HttpStatusCode.TooManyRequests, "{\"error\":\"limit\",\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}"));
        s.TempHome();
        await Assert.ThrowsAsync<ConcurrencyLimitError>(() =>
            License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114")));
    }

    [Fact]
    public async Task Checkout_body_carries_sdk_and_engine_version_resolver_runs_once()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("tel"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(LeaseBackend(eps, bodies));
        s.TempHome();
        int resolved = 0;
        Func<Task<string?>> resolver = () => { resolved++; return Engine("150.0.7871.114"); };
        var o = new LicenseOptions();
        await License.AcquireLeaseAsync(o, "0.17.1", true, resolver);
        await License.AcquireLeaseAsync(o, "0.17.1", true, resolver);   // reuse -> no 2nd checkout
        Assert.Single(bodies);
        Assert.Equal("0.17.1", bodies[0].GetProperty("sdk_version").GetString());
        Assert.Equal("150.0.7871.114", bodies[0].GetProperty("engine_version").GetString());
        Assert.Equal(1, resolved);                              // memoized, resolved once (cold checkout)
    }

    [Fact]
    public async Task Throwing_engine_resolver_is_soft_checkout_still_succeeds()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("soft"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(LeaseBackend(eps, bodies));
        s.TempHome();
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true,
            () => throw new Exception("catalog down"));
        Assert.Equal("TOK", lease!.Token);                      // launch still works
        Assert.Equal(JsonValueKind.Null, bodies[0].GetProperty("engine_version").ValueKind); // omitted (null)
    }

    [Fact]
    public async Task Reuses_a_valid_on_disk_token_zero_checkout()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        var key = UniqueKey("disk");
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key)
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(LeaseBackend(eps, bodies));
        var home = s.TempHome();
        WriteCacheFile(home, key, new { token = "DISK-TOK", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800, lease_id = "Ld", scope = "machine" });
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
        Assert.Equal(0, eps.Count(e => e == "checkout"));
        Assert.Equal("DISK-TOK", lease!.Token);
    }

    [Fact]
    public async Task Reads_legacy_cache_without_lease_id_zero_checkout()
    {
        var eps = new List<string>(); var bodies = new List<JsonElement>();
        var key = UniqueKey("legacy");
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key)
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(LeaseBackend(eps, bodies));
        var home = s.TempHome();
        // no lease_id (an SDK before 0.17 wrote token+exp only); marked machine, so it may skip the checkout
        WriteCacheFile(home, key, new { token = "LEGACY", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800, scope = "machine" });
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
        Assert.Equal(0, eps.Count(e => e == "checkout"));
        Assert.Equal("LEGACY", lease!.Token);
    }

    // ── heartbeat 409: reclaimed/expired -> re-checkout ──────────────────────────────────────────────
    // The backend answers a heartbeat for a lease it no longer holds with 409 (LEASE_NOT_FOUND /
    // LEASE_EXPIRED) and the SDK must re-checkout to keep its slot. The per-browser (free) loop is covered
    // in LicensePerBrowserTests; these run the MACHINE (paid) loop for real at its 5 s floor. Every test
    // ends with ShutdownAllLeasesAsync: a live heartbeat would otherwise keep firing into later tests'
    // handlers (it did, once, in an ad-hoc probe of exactly this loop).

    private static HttpResponseMessage Resp(int status, string json) =>
        new((HttpStatusCode)status) { Content = new StringContent(json) };

    private static async Task<bool> Until(Func<bool> pred, int ms)
    {
        var end = DateTime.UtcNow.AddMilliseconds(ms);
        while (DateTime.UtcNow < end) { if (pred()) return true; await Task.Delay(100); }
        return pred();
    }

    /// The cold checkout gets L1 with a 5 s heartbeat; after that, heartbeat/checkout answers come from
    /// `script` in order (a null body throws, as a dropped connection would). Records (endpoint, lease_id).
    private static FakeHandler Scripted(List<(string Ep, string LeaseId)> hits, Queue<(string Ep, int Status, string? Json)> script)
    {
        var checkouts = 0;
        return new FakeHandler(req =>
        {
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            var body = req.Content is null ? "{}" : req.Content.ReadAsStringAsync().GetAwaiter().GetResult();
            var leaseId = ep == "heartbeat" ? JsonDocument.Parse(body).RootElement.GetProperty("lease_id").GetString()! : "";
            lock (hits) hits.Add((ep, leaseId));
            var exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800;
            if (ep == "checkout" && Interlocked.Increment(ref checkouts) == 1)
                return Resp(200, $"{{\"lease_id\":\"L1\",\"token\":\"TOK-1\",\"exp\":{exp},\"heartbeat_interval_sec\":5}}");
            lock (script)
            {
                if (script.Count > 0 && script.Peek().Ep == ep)
                {
                    var (_, status, json) = script.Dequeue();
                    if (json is null) throw new HttpRequestException("connection reset");
                    return Resp(status, json.Replace("{exp}", exp.ToString()));
                }
            }
            return ep == "heartbeat" ? Resp(200, $"{{\"token\":\"TOK-HB\",\"exp\":{exp}}}") : Resp(200, "{}");
        });
    }

    private static (string Ep, string LeaseId)[] Snapshot(List<(string Ep, string LeaseId)> hits)
    {
        lock (hits) return hits.ToArray();
    }

    [Fact]
    public async Task Machine_lease_409_rechecks_out_and_heartbeats_the_new_lease()
    {
        var hits = new List<(string Ep, string LeaseId)>();
        var script = new Queue<(string Ep, int Status, string? Json)>(new (string, int, string?)[]
        {
            ("heartbeat", 409, "{\"code\":\"LEASE_EXPIRED\"}"),
            ("checkout", 200, "{\"lease_id\":\"L2\",\"token\":\"TOK-2\",\"exp\":{exp}}"),
            ("heartbeat", 200, "{\"token\":\"TOK-3\",\"exp\":{exp}}"),
        });
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("hb409"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(Scripted(hits, script));
        s.TempHome();
        try
        {
            var h = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
            Assert.True(await Until(() => Snapshot(hits).Count(x => x.Ep == "heartbeat") >= 2, 20000));
            var seq = Snapshot(hits);
            Assert.Equal(new[] { "checkout", "heartbeat", "checkout", "heartbeat" }, seq.Take(4).Select(x => x.Ep).ToArray());
            Assert.Equal("L1", seq[1].LeaseId);   // the refused beat
            Assert.Equal("L2", seq[3].LeaseId);   // the next beat holds the re-checked-out lease
            Assert.True(await Until(() => h!.Token == "TOK-3", 2000));
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    [Fact]
    public async Task Machine_lease_409_keeps_retrying_through_a_refused_and_a_failed_recheckout()
    {
        var hits = new List<(string Ep, string LeaseId)>();
        var script = new Queue<(string Ep, int Status, string? Json)>(new (string, int, string?)[]
        {
            ("heartbeat", 409, "{\"code\":\"LEASE_EXPIRED\"}"),
            ("checkout", 429, "{\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}"),
            ("heartbeat", 409, "{\"code\":\"LEASE_EXPIRED\"}"),
            ("checkout", 0, null),                                                    // connection dropped
            ("heartbeat", 409, "{\"code\":\"LEASE_NOT_FOUND\"}"),
            ("checkout", 200, "{\"lease_id\":\"L9\",\"token\":\"TOK-9\",\"exp\":{exp}}"),
            ("heartbeat", 200, "{\"token\":\"TOK-10\",\"exp\":{exp}}"),
        });
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("hb409retry"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(Scripted(hits, script));
        s.TempHome();
        try
        {
            var h = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
            Assert.True(await Until(() => Snapshot(hits).Count(x => x.Ep == "heartbeat") >= 4, 35000));
            var seq = Snapshot(hits).Take(8).ToArray();
            Assert.Equal(new[] { "checkout", "heartbeat", "checkout", "heartbeat", "checkout", "heartbeat", "checkout", "heartbeat" },
                seq.Select(x => x.Ep).ToArray());
            Assert.Equal(new[] { "L1", "L1", "L1", "L9" }, seq.Where(x => x.Ep == "heartbeat").Select(x => x.LeaseId).ToArray());
            Assert.True(await Until(() => h!.Token == "TOK-10", 2000));
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }

    // ── User-Agent: every licence call names the SDK and its version ─────────────────────────────────
    // So the licence server's logs can tell SDK builds apart (2026-09-29: the only clients stuck on 409s
    // sent no User-Agent at all, and HttpClient sends none by default).
    [Fact]
    public async Task Licence_calls_send_one_user_agent_naming_the_sdk_version()
    {
        var seen = new List<(string Ep, string Uas)>();
        var handler = new FakeHandler(req =>
        {
            var ep = req.RequestUri!.AbsolutePath.Split('/').Last();
            var uas = req.Headers.TryGetValues("User-Agent", out var v) ? string.Join(" | ", v) : "<none>";
            lock (seen) seen.Add((ep, uas));
            var exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800;
            return ep == "checkout"
                ? Resp(200, $"{{\"lease_id\":\"L1\",\"token\":\"TOK\",\"exp\":{exp},\"heartbeat_interval_sec\":3600}}")
                : Resp(200, "{\"used\":1,\"limit\":5}");
        });
        using var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", UniqueKey("ua"))
            .Env("CLEARCOTE_LICENSE_API", "http://test.local").Http(handler);
        s.TempHome();
        try
        {
            await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
            Assert.Equal(SeatsState.Ok, (await License.GetSessionSeatsAsync(new LicenseOptions())).State);
            var want = $"clearcote-sdk-dotnet/{Clearcote.Version}";
            Assert.Equal(new[] { ("checkout", want), ("seats", want) }, seen.ToArray());
        }
        finally { await License.ShutdownAllLeasesAsync(); }
    }
}
