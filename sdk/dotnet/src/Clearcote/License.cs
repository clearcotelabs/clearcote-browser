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
    private readonly Func<string> _token;
    private readonly Func<Task> _stop;
    private int _stopped;

    internal LeaseSession(Func<string> token, string leaseId, Func<Task> stop)
    {
        _token = token;
        LeaseId = leaseId;
        _stop = stop;
    }

    /// The current (rotating) run-token injected as CLEARCOTE_RUN_TOKEN. Reads the shared
    /// per-machine lease's live token, so a heartbeat rotation is reflected here.
    public string Token => _token();
    public string LeaseId { get; internal set; }

    /// Release this launch's handle (best-effort; safe to call twice). Per-machine reuse means this
    /// does NOT check the slot in — the shared lease is checked in once, at process exit.
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

    // The on-disk cache now also carries lease_id (for the exit checkin). A LEGACY cache written by an
    // older SDK (token+exp only) is still honored — leaseId is simply null then.
    private static (string token, long exp, string? leaseId)? ReadCache(string licenseKey)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(CachePath(licenseKey)));
            var root = doc.RootElement;
            if (root.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String
                && root.TryGetProperty("exp", out var e) && e.TryGetInt64(out var exp))
            {
                string? lid = root.TryGetProperty("lease_id", out var l) && l.ValueKind == JsonValueKind.String
                    ? l.GetString() : null;
                return (t.GetString()!, exp, lid);
            }
        }
        catch { /* ignore */ }
        return null;
    }

    private static void WriteCache(string licenseKey, string token, long exp, string? leaseId)
    {
        try
        {
            Directory.CreateDirectory(Native.ClearcoteDir);
            File.WriteAllText(CachePath(licenseKey),
                JsonSerializer.Serialize(new { token, exp, lease_id = leaseId }));
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

    // ── per-machine lease reuse ──────────────────────────────────────────────
    // One shared lease per (process, license key). Concurrency is per-MACHINE (the backend dedups by
    // the stable instance_id), so re-checking-out on every launch is redundant — the machine already
    // holds its one slot. Check out at most once per token-TTL and share the run-token across every
    // launch in the process; only the cold-checkout owner heartbeats + checks in (once, at exit).
    // This is the .NET port of the Python/Node MachineLease (SDK 0.17.x).
    private sealed class MachineLease
    {
        private readonly string _key, _baseUrl, _instanceId;
        private readonly string? _sdkVersion;
        private readonly Func<Task<string?>>? _engineVersion;
        private readonly bool _quiet;
        private readonly SemaphoreSlim _gate = new(1, 1);
        private volatile string? _token;
        private long _exp;
        private string? _leaseId;
        private string? _engineResolved;   // memoized ("" once resolved-empty)
        private int _hbSec = 270;
        private bool _owner;               // only the cold-checkout owner heartbeats + checks in
        private CancellationTokenSource? _hbCts;
        private int _refs;

        public MachineLease(string key, string baseUrl, string instanceId, string? sdkVersion,
            Func<Task<string?>>? engineVersion, bool quiet)
        {
            _key = key; _baseUrl = baseUrl; _instanceId = instanceId;
            _sdkVersion = sdkVersion; _engineVersion = engineVersion; _quiet = quiet;
        }

        private bool Valid() => _token != null && _exp > NowSec() + 60;

        // Resolved engine version for telemetry — memoized, resolved at most once (cold checkout only).
        private async Task<string?> EngineVerAsync()
        {
            if (_engineResolved == null)
            {
                try { _engineResolved = (_engineVersion != null ? await _engineVersion().ConfigureAwait(false) : null) ?? ""; }
                catch { _engineResolved = ""; }
            }
            return _engineResolved.Length == 0 ? null : _engineResolved;
        }

        public async Task<LeaseSession> AcquireAsync()
        {
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (!Valid())
                {
                    var cached = ReadCache(_key);
                    if (cached is { } c && c.exp > NowSec() + 60)
                    {
                        // cross-process reuse: another process's owner keeps the slot alive.
                        _token = c.token; _exp = c.exp; _leaseId = c.leaseId; _owner = false;
                    }
                    else
                    {
                        await CheckoutAsync().ConfigureAwait(false);
                        _owner = true;
                        StartHeartbeat();
                    }
                }
            }
            finally { _gate.Release(); }

            Interlocked.Increment(ref _refs);
            // Per-launch handle: reads the machine's (rotating) token live; StopAsync just decrefs
            // (NO checkin — the shared slot is checked in once at process exit).
            return new LeaseSession(() => _token!, _leaseId ?? "", () =>
            {
                Interlocked.Decrement(ref _refs);
                return Task.CompletedTask;
            });
        }

        private async Task CheckoutAsync()
        {
            try
            {
                using var res = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkout", _key,
                    new { instance_id = _instanceId, os = Native.OsTag, sdk_version = _sdkVersion,
                          engine_version = await EngineVerAsync().ConfigureAwait(false) }).ConfigureAwait(false);
                if (!res.IsSuccessStatusCode) await ThrowForStatusAsync(res).ConfigureAwait(false);
                using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                var root = doc.RootElement;
                _token = root.GetProperty("token").GetString()!;
                _exp = root.GetProperty("exp").GetInt64();
                _leaseId = root.GetProperty("lease_id").GetString()!;
                _hbSec = root.TryGetProperty("heartbeat_interval_sec", out var hb) && hb.TryGetInt32(out var v) ? v : 270;
                WriteCache(_key, _token, _exp, _leaseId);
            }
            catch (LicenseError) { throw; } // a definitive verdict must surface (never silently downgrade)
            catch (Exception e)
            {
                var cached = ReadCache(_key);
                if (cached is { } c && c.exp > NowSec() + 60)
                {
                    if (!_quiet) Console.Error.WriteLine($"[clearcote] [license] backend unreachable ({e.Message}); using cached run-token.");
                    _token = c.token; _exp = c.exp; _leaseId = c.leaseId;
                    return;
                }
                throw new LicenseError($"Could not reach the license server and no valid cached token: {e.Message}");
            }
        }

        private void StartHeartbeat()
        {
            if (_hbCts != null) return;
            _hbCts = new CancellationTokenSource();
            var ct = _hbCts.Token;
            var hbMs = Math.Max(5, _hbSec) * 1000;
            _ = Task.Run(async () =>
            {
                while (!ct.IsCancellationRequested)
                {
                    try { await Task.Delay(hbMs, ct).ConfigureAwait(false); }
                    catch (OperationCanceledException) { break; }
                    try
                    {
                        using var res = await PostJsonAsync($"{_baseUrl}/api/v1/lease/heartbeat", _key,
                            new { lease_id = _leaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
                        if ((int)res.StatusCode == 409) // reclaimed/expired -> re-checkout to keep the slot
                        {
                            using var co = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkout", _key,
                                new { instance_id = _instanceId, os = Native.OsTag, sdk_version = _sdkVersion,
                                      engine_version = await EngineVerAsync().ConfigureAwait(false) }).ConfigureAwait(false);
                            if (co.IsSuccessStatusCode)
                            {
                                using var d = JsonDocument.Parse(await co.Content.ReadAsStringAsync().ConfigureAwait(false));
                                _leaseId = d.RootElement.GetProperty("lease_id").GetString()!;
                                _token = d.RootElement.GetProperty("token").GetString()!;
                                _exp = d.RootElement.GetProperty("exp").GetInt64();
                                WriteCache(_key, _token, _exp, _leaseId);
                            }
                            continue;
                        }
                        if (res.IsSuccessStatusCode)
                        {
                            using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                            _token = d.RootElement.GetProperty("token").GetString()!;
                            _exp = d.RootElement.GetProperty("exp").GetInt64();
                            WriteCache(_key, _token, _exp, _leaseId);
                        }
                    }
                    catch { /* transient — offline grace until token exp */ }
                }
            });
        }

        // Single checkin at process exit (owner only). Frees the slot without waiting for the TTL.
        public async Task ShutdownAsync()
        {
            _hbCts?.Cancel();
            if (_owner && _leaseId != null)
            {
                try
                {
                    using var _ = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkin", _key,
                        new { lease_id = _leaseId }).ConfigureAwait(false);
                }
                catch { /* best-effort; the lease TTL reclaims it anyway */ }
            }
        }
    }

    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, MachineLease> _machineLeases = new();
    private static int _exitHooked;

    /// Acquire a per-MACHINE concurrency lease, shared across every launch in this process (checks out
    /// ~once per token-TTL, not once per launch). Returns null in free mode (no key). Throws
    /// <see cref="ConcurrencyLimitError"/> / <see cref="LicenseRevokedError"/> / <see cref="LicenseError"/>
    /// only on a cold checkout the backend definitively refuses; falls back to a cached, still-valid
    /// token on a transient network failure (offline grace).
    ///
    /// <paramref name="sdkVersion"/> is the SDK PACKAGE version (checkout telemetry sdk_version).
    /// <paramref name="engineVersion"/> lazily resolves the browser build (engine_version) — invoked
    /// on a cold checkout only, so the catalog is never consulted per launch.
    public static async Task<LeaseSession?> AcquireLeaseAsync(
        LicenseOptions opts, string? sdkVersion = null, bool quiet = false,
        Func<Task<string?>>? engineVersion = null)
    {
        var licenseKey = ResolveLicenseKey(opts.LicenseKey);
        if (licenseKey is null) return null; // free mode — inert

        var baseUrl = ApiBase(opts);
        var ml = _machineLeases.GetOrAdd(licenseKey,
            k => new MachineLease(k, baseUrl, ResolveInstanceId(), sdkVersion, engineVersion, quiet));
        if (Interlocked.Exchange(ref _exitHooked, 1) == 0)
        {
            AppDomain.CurrentDomain.ProcessExit += (_, _) =>
            {
                foreach (var m in _machineLeases.Values)
                    try { m.ShutdownAsync().GetAwaiter().GetResult(); } catch { /* best-effort */ }
            };
        }
        return await ml.AcquireAsync().ConfigureAwait(false);
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
