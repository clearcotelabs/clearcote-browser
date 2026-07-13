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
        WriteCacheFile(home, key, new { token = "DISK-TOK", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800, lease_id = "Ld" });
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
        WriteCacheFile(home, key, new { token = "LEGACY", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800 }); // no lease_id
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.17.1", true, () => Engine("150.0.7871.114"));
        Assert.Equal(0, eps.Count(e => e == "checkout"));
        Assert.Equal("LEGACY", lease!.Token);
    }
}
