using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Clearcote;

/// A definitive licensing failure. Never silently downgraded to the free build.
public class LicenseError : Exception
{
    public string Code { get; }
    public LicenseError(string message, string code = "LICENSE_ERROR") : base(message) => Code = code;
}

/// The license has no free concurrency slot right now (429 / CONCURRENCY_LIMIT_EXCEEDED).
public sealed class ConcurrencyLimitError : LicenseError
{
    public ConcurrencyLimitError(string message) : base(message, "CONCURRENCY_LIMIT_EXCEEDED") { }
}

/// The license was revoked or has expired (403 / LICENSE_REVOKED / LICENSE_EXPIRED).
public sealed class LicenseRevokedError : LicenseError
{
    public LicenseRevokedError(string message) : base(message, "LICENSE_REVOKED") { }
}

/// Options for resolving a license + reaching the backend.
public class LicenseOptions
{
    /// License key ("cc_lic_..."). Resolved from this &gt; CLEARCOTE_LICENSE_KEY env &gt; ~/.clearcote/license.key.
    public string? LicenseKey { get; set; }
    /// Backend base URL. Default: CLEARCOTE_LICENSE_API env or clearcotelabs.com.
    public string? LicenseApiBase { get; set; }
}

/// A live floating-concurrency lease. Keep it until the browser closes, then call <see cref="StopAsync"/>.
public sealed class LeaseSession
{
    private readonly Func<Task> _stop;
    private volatile string _token;
    private int _stopped;

    internal LeaseSession(string token, string leaseId, Func<Task> stop)
    {
        _token = token;
        LeaseId = leaseId;
        _stop = stop;
    }

    /// The current (rotating) run-token injected as CLEARCOTE_RUN_TOKEN.
    public string Token => _token;
    internal void SetToken(string token) => _token = token;
    public string LeaseId { get; internal set; }

    /// Release the slot + stop the heartbeat (best-effort; safe to call twice).
    public Task StopAsync()
    {
        if (Interlocked.Exchange(ref _stopped, 1) != 0) return Task.CompletedTask;
        return _stop();
    }
}

/// Floating-concurrency licensing client (opt-in). Ports license.ts.
public static class License
{
    private const string DefaultApiBase = "https://www.clearcotelabs.com";
    internal const string RunTokenEnv = "CLEARCOTE_RUN_TOKEN";

    /// Resolve a license key: explicit &gt; CLEARCOTE_LICENSE_KEY env &gt; ~/.clearcote/license.key.
    public static string? ResolveLicenseKey(string? @explicit = null)
    {
        if (!string.IsNullOrWhiteSpace(@explicit)) return @explicit.Trim();
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_KEY");
        if (!string.IsNullOrWhiteSpace(env)) return env.Trim();
        try
        {
            var p = Path.Combine(Native.ClearcoteDir, "license.key");
            if (File.Exists(p))
            {
                var v = File.ReadAllText(p).Trim();
                if (v.Length > 0) return v;
            }
        }
        catch { /* ignore */ }
        return null;
    }

    /// A STABLE per-machine id so a restart REUSES its concurrency slot instead of spawning a second
    /// lease. Order: CLEARCOTE_INSTANCE_ID env &gt; ~/.clearcote/instance_id file &gt; a freshly generated id
    /// (persisted). Falls back to an ephemeral id if the file can't be written.
    public static string ResolveInstanceId()
    {
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_INSTANCE_ID");
        if (!string.IsNullOrWhiteSpace(env)) return env.Trim();
        var dir = Native.ClearcoteDir;
        var p = Path.Combine(dir, "instance_id");
        try
        {
            if (File.Exists(p))
            {
                var v = File.ReadAllText(p).Trim();
                if (v.Length > 0) return v;
            }
        }
        catch { /* ignore */ }
        var id = Guid.NewGuid().ToString();
        try
        {
            Directory.CreateDirectory(dir);
            File.WriteAllText(p, id + "\n");
        }
        catch { /* ephemeral fallback — set CLEARCOTE_INSTANCE_ID to persist */ }
        return id;
    }

    internal static string ApiBase(LicenseOptions opts)
        => (opts.LicenseApiBase
            ?? Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_API")
            ?? DefaultApiBase).TrimEnd('/');

    // ── offline token cache (best-effort grace) ──────────────────────────────
    private static string CachePath(string licenseKey)
    {
        var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(licenseKey)))
            .ToLowerInvariant()[..16];
        return Path.Combine(Native.ClearcoteDir, $"lease-{id}.json");
    }

    private static (string token, long exp)? ReadCache(string licenseKey)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(CachePath(licenseKey)));
            var root = doc.RootElement;
            if (root.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String
                && root.TryGetProperty("exp", out var e) && e.TryGetInt64(out var exp))
                return (t.GetString()!, exp);
        }
        catch { /* ignore */ }
        return null;
    }

    private static void WriteCache(string licenseKey, string token, long exp)
    {
        try
        {
            Directory.CreateDirectory(Native.ClearcoteDir);
            File.WriteAllText(CachePath(licenseKey),
                JsonSerializer.Serialize(new { token, exp }));
        }
        catch { /* ignore */ }
    }

    private static async Task<HttpResponseMessage> PostJsonAsync(string url, string licenseKey, object body)
    {
        using var client = SdkHttp.Create();
        var req = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json"),
        };
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", licenseKey);
        return await client.SendAsync(req).ConfigureAwait(false);
    }

    private static async Task ThrowForStatusAsync(HttpResponseMessage res)
    {
        string? error = null, code = null;
        try
        {
            using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            if (doc.RootElement.TryGetProperty("error", out var e)) error = e.GetString();
            if (doc.RootElement.TryGetProperty("code", out var c)) code = c.GetString();
        }
        catch { /* ignore */ }
        var status = (int)res.StatusCode;
        var msg = error ?? $"License request failed ({status}).";
        if (status == 429 || code == "CONCURRENCY_LIMIT_EXCEEDED") throw new ConcurrencyLimitError(msg);
        if (status == 403 || code == "LICENSE_REVOKED" || code == "LICENSE_EXPIRED")
            throw new LicenseRevokedError(msg);
        throw new LicenseError(msg, code ?? $"HTTP_{status}");
    }

    private static long NowSec() => DateTimeOffset.UtcNow.ToUnixTimeSeconds();

    /// Acquire a concurrency lease if a license key is configured. Returns null in free mode (no key).
    /// Throws <see cref="ConcurrencyLimitError"/> / <see cref="LicenseRevokedError"/> /
    /// <see cref="LicenseError"/> when a key IS present but the backend refuses. On a network failure
    /// with a still-valid cached token, resumes offline (degraded).
    public static async Task<LeaseSession?> AcquireLeaseAsync(
        LicenseOptions opts, string? sdkVersion = null, bool quiet = false)
    {
        var licenseKey = ResolveLicenseKey(opts.LicenseKey);
        if (licenseKey is null) return null; // free mode — inert

        var baseUrl = ApiBase(opts);
        var instanceId = ResolveInstanceId();
        void Warn(string m) { if (!quiet) Console.Error.WriteLine($"[clearcote] [license] {m}"); }

        JsonElement checkout;
        try
        {
            using var res = await PostJsonAsync($"{baseUrl}/api/v1/lease/checkout", licenseKey,
                new { instance_id = instanceId, os = Native.OsTag, sdk_version = sdkVersion }).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode) await ThrowForStatusAsync(res).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            checkout = doc.RootElement.Clone();
            WriteCache(licenseKey, checkout.GetProperty("token").GetString()!, checkout.GetProperty("exp").GetInt64());
        }
        catch (LicenseError) { throw; } // a definitive verdict must surface (never silently downgrade)
        catch (Exception e)
        {
            var cached = ReadCache(licenseKey);
            if (cached is { } c && c.exp > NowSec() + 60)
            {
                Warn($"backend unreachable ({e.Message}); using cached run-token (offline grace).");
                return new LeaseSession(c.token, "cached", () => Task.CompletedTask);
            }
            throw new LicenseError($"Could not reach the license server and no valid cached token: {e.Message}");
        }

        var leaseId = checkout.GetProperty("lease_id").GetString()!;
        var token = checkout.GetProperty("token").GetString()!;
        var hbSec = checkout.TryGetProperty("heartbeat_interval_sec", out var hb) && hb.TryGetInt32(out var v) ? v : 30;
        var hbMs = Math.Max(5, hbSec) * 1000;

        var cts = new CancellationTokenSource();
        LeaseSession session = null!;
        async Task Heartbeat()
        {
            while (!cts.IsCancellationRequested)
            {
                try { await Task.Delay(hbMs, cts.Token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
                try
                {
                    using var res = await PostJsonAsync($"{baseUrl}/api/v1/lease/heartbeat", licenseKey,
                        new { lease_id = session.LeaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
                    if ((int)res.StatusCode == 409)
                    {
                        using var co = await PostJsonAsync($"{baseUrl}/api/v1/lease/checkout", licenseKey,
                            new { instance_id = instanceId, os = Native.OsTag, sdk_version = sdkVersion }).ConfigureAwait(false);
                        if (co.IsSuccessStatusCode)
                        {
                            using var d = JsonDocument.Parse(await co.Content.ReadAsStringAsync().ConfigureAwait(false));
                            session.LeaseId = d.RootElement.GetProperty("lease_id").GetString()!;
                            session.SetToken(d.RootElement.GetProperty("token").GetString()!);
                            WriteCache(licenseKey, session.Token, d.RootElement.GetProperty("exp").GetInt64());
                        }
                        continue;
                    }
                    if (res.IsSuccessStatusCode)
                    {
                        using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                        session.SetToken(d.RootElement.GetProperty("token").GetString()!);
                        WriteCache(licenseKey, session.Token, d.RootElement.GetProperty("exp").GetInt64());
                    }
                }
                catch { /* transient — offline grace until token exp */ }
            }
        }

        async Task Stop()
        {
            cts.Cancel();
            try
            {
                using var _ = await PostJsonAsync($"{baseUrl}/api/v1/lease/checkin", licenseKey,
                    new { lease_id = session.LeaseId }).ConfigureAwait(false);
            }
            catch { /* best-effort; the lease TTL reclaims it anyway */ }
            cts.Dispose();
        }

        session = new LeaseSession(token, leaseId, Stop);
        _ = Task.Run(Heartbeat); // background; does not keep the process alive
        return session;
    }

    /// Merge the run-token into an env dictionary (base defaults to the current process env).
    public static Dictionary<string, string> WithRunToken(string token, IDictionary<string, string>? baseEnv)
    {
        var outEnv = new Dictionary<string, string>();
        if (baseEnv is not null)
        {
            foreach (var (k, v) in baseEnv) if (v is not null) outEnv[k] = v;
        }
        else
        {
            foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables())
                if (e.Value is string sv) outEnv[(string)e.Key] = sv;
        }
        outEnv[RunTokenEnv] = token;
        return outEnv;
    }
}
