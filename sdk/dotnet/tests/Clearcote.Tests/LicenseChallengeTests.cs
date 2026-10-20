using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// A firewall/bot challenge page in front of the licence server (e.g. the "Vercel Security Checkpoint", a
// 403 HTML page) is not a licence verdict: it throws LicenseServerChallengeError (retryable), never
// LicenseRevokedError, and a cached token still rides it out. The licence server's own JSON 403 still means
// revoked. Mirrors the Python test_license_challenge.py and Node license-challenge.test.ts suites.
public class LicenseChallengeTests
{
    private const string Checkpoint =
        "<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head>" +
        "<body><p>We're verifying your browser</p></body></html>";

    /// Every call gets the same canned answer.
    private static FakeHandler Answer(int status, string body, string contentType, string? mitigated = null) =>
        new(_ =>
        {
            var res = new HttpResponseMessage((HttpStatusCode)status)
            {
                Content = new StringContent(body, Encoding.UTF8, contentType),
            };
            if (mitigated is not null) res.Headers.TryAddWithoutValidation("x-vercel-mitigated", mitigated);
            return res;
        });

    private static string UniqueKey(string p) => $"cc_lic_{p}_{Guid.NewGuid():N}";

    private static Sandbox Backend(string key, FakeHandler h)
    {
        var s = new Sandbox().Env("CLEARCOTE_LICENSE_KEY", key).Env("CLEARCOTE_LICENSE_API", "http://test.local")
            .Env("CLEARCOTE_LICENSE_THROUGH_PROXY", null).Http(h);
        s.TempHome();
        return s;
    }

    private static Task<string?> Engine() => Task.FromResult<string?>("151.0.0.0");

    public static IEnumerable<object?[]> ChallengePages() => new[]
    {
        new object?[] { 403, Checkpoint, "text/html", "challenge" },
        new object?[] { 403, Checkpoint, "text/html", null },                       // the page alone is enough
        new object?[] { 403, "<html>Access denied</html>", "text/html", null },     // any non-JSON 403
        new object?[] { 429, "<html>Too many requests</html>", "text/html", null }, // not a concurrency verdict
    };

    [Theory]
    [MemberData(nameof(ChallengePages))]
    public async Task Challenge_page_is_a_challenge_error_not_a_revoke(int status, string body, string ct, string? mitigated)
    {
        using var s = Backend(UniqueKey("chal"), Answer(status, body, ct, mitigated));
        var ex = await Assert.ThrowsAsync<LicenseServerChallengeError>(
            () => License.AcquireLeaseAsync(new LicenseOptions(), "0.48.0", true, Engine));
        Assert.IsAssignableFrom<LicenseError>(ex); // existing `catch (LicenseError)` handlers still catch it
        Assert.Equal("LICENSE_SERVER_CHALLENGE", ex.Code);
        Assert.True(ex.Retryable);
        Assert.Contains("not revoked", ex.Message);
        Assert.Contains($"HTTP {status}", ex.Message);
    }

    [Fact]
    public async Task Json_403_from_the_licence_server_is_still_revoked()
    {
        using var s = Backend(UniqueKey("rev"),
            Answer(403, "{\"error\":\"License revoked.\",\"code\":\"LICENSE_REVOKED\"}", "application/json"));
        await Assert.ThrowsAsync<LicenseRevokedError>(
            () => License.AcquireLeaseAsync(new LicenseOptions(), "0.48.0", true, Engine));
    }

    [Fact]
    public async Task Json_429_is_still_the_concurrency_limit()
    {
        using var s = Backend(UniqueKey("lim"),
            Answer(429, "{\"error\":\"limit\",\"code\":\"CONCURRENCY_LIMIT_EXCEEDED\"}", "application/json"));
        await Assert.ThrowsAsync<ConcurrencyLimitError>(
            () => License.AcquireLeaseAsync(new LicenseOptions(), "0.48.0", true, Engine));
    }

    [Fact]
    public async Task Challenge_on_a_forced_refresh_rides_on_the_cached_token()
    {
        var key = UniqueKey("grace");
        using var s = Backend(key, Answer(403, Checkpoint, "text/html", "challenge"));
        var home = Environment.GetEnvironmentVariable("HOME") ?? Environment.GetEnvironmentVariable("USERPROFILE")!;
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key))).ToLowerInvariant()[..16];
        Directory.CreateDirectory(Path.Combine(home, ".clearcote"));
        File.WriteAllText(Path.Combine(home, ".clearcote", $"lease-{id}.json"), JsonSerializer.Serialize(new
        {
            token = "CACHED-TOK", exp = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 800, lease_id = "Lcache", scope = "machine",
        }));
        var lease = await License.AcquireLeaseAsync(new LicenseOptions(), "0.48.0", true, Engine); // cache: no call
        Assert.Equal("CACHED-TOK", lease!.Token);
        // The engine refused the held token: heartbeat, then check out. Both get the challenge; the checkout
        // falls back to the cache like a network error instead of throwing a revoke.
        Assert.True(await lease.RefreshTokenAsync());
        Assert.Equal("CACHED-TOK", lease.Token);
        await lease.StopAsync();
    }

    [Fact]
    public async Task Seats_reports_a_challenge_as_unavailable_not_invalid()
    {
        var key = UniqueKey("seats");
        using var s = Backend(key, Answer(403, Checkpoint, "text/html"));
        var got = await License.GetSessionSeatsAsync(new LicenseOptions { LicenseKey = key });
        Assert.Equal(SeatsState.Unavailable, got.State);
        Assert.Contains("challenge", got.Reason);
    }

    [Fact]
    public void ChallengeReason_never_flags_json_errors_or_successes()
    {
        Assert.Null(License.ChallengeReason(403, "application/json", "{\"error\":\"x\"}"));
        Assert.Null(License.ChallengeReason(200, "text/html", Checkpoint));
        Assert.Null(License.ChallengeReason(500, "text/html", "<html>oops</html>"));
        Assert.Equal("x-vercel-mitigated: challenge", License.ChallengeReason(403, "text/html", "", "challenge"));
    }
}
