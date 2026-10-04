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
    /// Send the licence calls (lease checkout / heartbeat / check-in, and seats) through the launch's
    /// own proxy instead of directly from this machine. Off by default: direct calls never spend proxy
    /// bandwidth. Null = CLEARCOTE_LICENSE_THROUGH_PROXY (1/true/yes/on). No effect without a proxy.
    public bool? LicenseThroughProxy { get; set; }
}

/// State of a <see cref="License.GetSessionSeatsAsync"/> answer.
public enum SeatsState
{
    /// Counts are present.
    Ok,
    /// No licence key is configured.
    NoKey,
    /// The backend rejected the key (401/403).
    Invalid,
    /// The backend is unreachable, or does not report seats; see <see cref="SessionSeats.Reason"/>.
    Unavailable,
}

/// Concurrency seats on a licence, as reported by the backend. <see cref="Limit"/> null with
/// <see cref="SeatsState.Ok"/> means unlimited.
public sealed record SessionSeats(SeatsState State, int? Used = null, int? Limit = null, string? Plan = null, string? Reason = null);

/// A per-launch run-token file (CLEARCOTE_RUN_TOKEN_FILE). It follows the owning lease's rotating
/// token so a supporting engine (152 r23+) can re-read it and stop a running FREE browser once the
/// token stops advancing. Call <see cref="Release"/> when the browser closes. Older engines ignore it.
public sealed class LaunchToken
{
    private readonly Action _release;
    private int _released;

    internal LaunchToken(string file, Action release)
    {
        File = file;
        _release = release;
    }

    /// The token file path to pass to the engine as CLEARCOTE_RUN_TOKEN_FILE.
    public string File { get; }

    /// Stop following the lease's token and remove the file (best-effort; safe to call twice).
    public void Release()
    {
        if (Interlocked.Exchange(ref _released, 1) != 0) return;
        _release();
    }
}

/// A live floating-concurrency lease. Keep it until the browser closes, then call <see cref="StopAsync"/>.
public sealed class LeaseSession
{
    private readonly Func<string> _token;
    private readonly Func<Task> _stop;
    private readonly Func<LaunchToken> _bindLaunch;
    private readonly Func<Task<bool>>? _refresh;
    private int _stopped;

    internal LeaseSession(Func<string> token, string leaseId, Func<Task> stop, Func<LaunchToken> bindLaunch,
        Func<Task<bool>>? refresh = null)
    {
        _token = token;
        LeaseId = leaseId;
        _stop = stop;
        _bindLaunch = bindLaunch;
        _refresh = refresh;
    }

    /// The engine refused this lease's token as older than one it has already accepted on this machine:
    /// mint a fresh one. True when <see cref="Token"/> now holds it.
    public Task<bool> RefreshTokenAsync() => _refresh?.Invoke() ?? Task.FromResult(false);

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

    /// Bind a per-launch run-token file (CLEARCOTE_RUN_TOKEN_FILE) that follows this lease's rotating
    /// token, so a supporting engine (r23+) can re-read it and stop a running free browser once the
    /// token stops advancing. Call <see cref="LaunchToken.Release"/> when the browser closes; older
    /// engines ignore the file, so this is purely additive.
    public LaunchToken BindLaunch() => _bindLaunch();
}

/// A set of per-launch run-token files that a lease keeps in step with its rotating token (engine
/// online-enforcement opt-in). A supporting engine (152 r23+) re-reads the run-token from
/// CLEARCOTE_RUN_TOKEN_FILE and stops a running FREE browser once the token stops advancing (the SDK
/// can only advance it by heartbeating, which the backend gates). This mirrors a lease's rotating
/// token into one file per launch and removes it on close. Older engines ignore the file (they read
/// CLEARCOTE_RUN_TOKEN once at launch), so it is purely additive — nothing breaks without support.
/// Write failures are non-fatal: the launch still carries CLEARCOTE_RUN_TOKEN. Mirrors license.ts.
internal sealed class TokenFileSet
{
    private sealed class Entry { public required string Path; }
    private readonly HashSet<Entry> _files = new();
    private readonly object _lock = new();

    /// Create a token file seeded with <paramref name="current"/>, kept updated until Release removes it.
    public LaunchToken Bind(string current)
    {
        var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"clearcote-rt-{Guid.NewGuid():N}.tok");
        WriteOne(path, current);
        var entry = new Entry { Path = path };
        lock (_lock) _files.Add(entry);
        return new LaunchToken(path, () =>
        {
            lock (_lock) _files.Remove(entry);
            try { System.IO.File.Delete(path); } catch { /* already gone */ }
        });
    }

    /// Rewrite every live file with the freshly-rotated token.
    public void Update(string token)
    {
        Entry[] snapshot;
        lock (_lock) snapshot = _files.ToArray();
        foreach (var e in snapshot) WriteOne(e.Path, token);
    }

    /// Remove every file (lease shutdown).
    public void CloseAll()
    {
        Entry[] snapshot;
        lock (_lock) { snapshot = _files.ToArray(); _files.Clear(); }
        foreach (var e in snapshot) { try { System.IO.File.Delete(e.Path); } catch { /* ignore */ } }
    }

    private static void WriteOne(string path, string token)
    {
        try
        {
            System.IO.File.WriteAllText(path, token);
            // Restrictive perms where the OS supports it (0600). Unsupported on Windows.
            if (!OperatingSystem.IsWindows())
                try { System.IO.File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite); } catch { }
        }
        catch { /* the launch still has CLEARCOTE_RUN_TOKEN */ }
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
                // A per-browser (free-tier) token belongs to the one browser it was checked out for.
                // Reusing it would start another browser without a slot, so it is never taken from the
                // cache, even one an older SDK wrote. Paid tokens are reused exactly as before.
                if (TokenPlan(t.GetString()!) == PerBrowserPlan) return null;
                return (t.GetString()!, exp, lid);
            }
        }
        catch { /* ignore */ }
        return null;
    }

    /// When a run-token was minted (its <c>iat</c>, epoch seconds), read from its payload WITHOUT verifying it.
    public static long? TokenIat(string? token)
    {
        try
        {
            var body = (token ?? "").Split('.')[0].Replace('-', '+').Replace('_', '/');
            body = body.PadRight(body.Length + (4 - body.Length % 4) % 4, '=');
            using var doc = JsonDocument.Parse(Convert.FromBase64String(body));
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("iat", out var i) && i.ValueKind == JsonValueKind.Number
                ? (long)i.GetDouble() : null;
        }
        catch { return null; }
    }

    /// What the PRO engine refuses a launch with when its run-token is older than the newest it has accepted.
    public const string StaleTokenRefusal = "older than the last one accepted";

    /// The newest run-token <c>iat</c> the PRO engine has accepted for this OS user (0 if none/unreadable).
    ///
    /// The engine (patch 990, clock-rollback guard) keeps it in <c>$LOCALAPPDATA/.clearcote/.cc_hwm</c>, else
    /// <c>$HOME/.clearcote/.cc_hwm</c>, as a decimal number, and refuses any token with a lower <c>iat</c>. It is
    /// per OS user, not per licence or process: another SDK process, the hosted-browser gateway or a run with
    /// a different key can all move it past a token this process still holds. Looked up exactly as the
    /// engine does it, env var by env var. <paramref name="env"/> defaults to this process's environment.
    public static long EngineHwm(IDictionary<string, string?>? env = null)
    {
        string? Get(string k) => env is null ? Environment.GetEnvironmentVariable(k) : (env.TryGetValue(k, out var v) ? v : null);
        var baseDir = Get("LOCALAPPDATA");
        if (string.IsNullOrEmpty(baseDir)) baseDir = Get("HOME");
        if (string.IsNullOrEmpty(baseDir)) return 0;
        try
        {
            return long.TryParse(File.ReadAllText(Path.Combine(baseDir, ".clearcote", ".cc_hwm")).Trim(), out var v) ? v : 0;
        }
        catch { return 0; }
    }

    // The plan whose tokens are per browser. Only used to keep such tokens out of the shared cache.
    private const string PerBrowserPlan = "free";

    /// The plan a run-token was minted for, read from its payload WITHOUT verifying it (routing only).
    public static string? TokenPlan(string? token)
    {
        try
        {
            var body = (token ?? "").Split('.')[0].Replace('-', '+').Replace('_', '/');
            body = body.PadRight(body.Length + (4 - body.Length % 4) % 4, '=');
            using var doc = JsonDocument.Parse(Convert.FromBase64String(body));
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("plan", out var p) && p.ValueKind == JsonValueKind.String
                ? p.GetString() : null;
        }
        catch { return null; }
    }

    /// One id per browser launch: the backend counts every launch_id as its own slot on per-browser plans.
    public static string NewLaunchId() => Guid.NewGuid().ToString("N");

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

    /// Whether licence calls should use the launch proxy: explicit option, else the env switch.
    public static bool LicenseThroughProxyRequested(bool? option, string? envValue = null)
    {
        if (option is { } o) return o;
        var raw = (envValue ?? Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_THROUGH_PROXY") ?? "").Trim();
        return System.Text.RegularExpressions.Regex.IsMatch(raw, "^(1|true|yes|on)$", System.Text.RegularExpressions.RegexOptions.IgnoreCase);
    }

    /// Names this SDK and its version on every licence call, so the licence server's logs can tell SDK builds
    /// apart, and tell clients that are not an SDK from ours. HttpClient sends no User-Agent at all by
    /// default, which is how every licence call from this SDK looked until now.
    internal static readonly string LicenseUserAgent = $"clearcote-sdk-dotnet/{Clearcote.Version}";

    private static async Task<HttpResponseMessage> PostJsonAsync(string url, string licenseKey, object body, ProxySpec? proxy = null)
    {
        // Through the launch proxy when LicenseThroughProxy is on; otherwise the unchanged direct path.
        using var client = proxy is null ? SdkHttp.Create() : ProxiedHttp.Create(proxy);
        if (proxy is not null && SdkHttp.HandlerOverride is null) client.Timeout = TimeSpan.FromSeconds(30);
        var req = new HttpRequestMessage(HttpMethod.Post, url)
        {
            Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json"),
        };
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", licenseKey);
        req.Headers.UserAgent.ParseAdd(LicenseUserAgent);
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
        private readonly ProxySpec? _proxy;
        private readonly SemaphoreSlim _gate = new(1, 1);
        private readonly SemaphoreSlim _refreshGate = new(1, 1);
        private readonly TokenFileSet _tokenFiles = new();
        private volatile string? _token;
        private long _exp;
        private string? _leaseId;
        private string? _engineResolved;   // memoized ("" once resolved-empty)
        private int _hbSec = 270;
        private bool _owner;               // only the cold-checkout owner heartbeats + checks in
        private CancellationTokenSource? _hbCts;
        private int _refs;
        // Learned from the first checkout: "browser" means every launch holds its own lease.
        private volatile string _scope = "machine";
        private readonly System.Collections.Concurrent.ConcurrentDictionary<BrowserLease, byte> _browsers = new();

        public MachineLease(string key, string baseUrl, string instanceId, string? sdkVersion,
            Func<Task<string?>>? engineVersion, bool quiet, ProxySpec? proxy = null)
        {
            _key = key; _baseUrl = baseUrl; _instanceId = instanceId;
            _sdkVersion = sdkVersion; _engineVersion = engineVersion; _quiet = quiet; _proxy = proxy;
        }

        private bool Valid() => _token != null && _exp > NowSec() + 60;

        // Set the shared rotating token AND push it into every bound run-token file, so a supporting
        // engine sees the rotation. Mirrors the license.ts token setter.
        private void SetToken(string? v)
        {
            _token = v;
            if (v != null) _tokenFiles.Update(v);
        }

        /// Bind a per-launch token file that follows this lease's rotating token.
        public LaunchToken BindLaunch() => _tokenFiles.Bind(_token ?? "");

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
            if (_scope == "browser") return await AcquireBrowserAsync().ConfigureAwait(false);
            CheckoutData? pending = null;
            string? pendingLaunch = null;
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (_scope != "browser" && !Valid())
                {
                    var cached = ReadCache(_key);
                    if (cached is { } c && c.exp > NowSec() + 60)
                    {
                        // cross-process reuse: another process's owner keeps the slot alive.
                        SetToken(c.token); _exp = c.exp; _leaseId = c.leaseId; _owner = false;
                    }
                    else
                    {
                        var launchId = NewLaunchId();
                        var d = await CheckoutAsync(launchId).ConfigureAwait(false);
                        if (d is not null)
                        {
                            // Per-browser plan: this checkout is THIS launch's own slot. Never shared or cached.
                            _scope = "browser";
                            pending = d; pendingLaunch = launchId;
                        }
                        else
                        {
                            _owner = true;
                            StartHeartbeat();
                        }
                    }
                }
            }
            finally { _gate.Release(); }

            if (pending is not null) return StartBrowser(pending, pendingLaunch!);
            if (_scope == "browser") return await AcquireBrowserAsync().ConfigureAwait(false);

            await RefreshIfBehindEngineAsync().ConfigureAwait(false);
            Interlocked.Increment(ref _refs);
            // Per-launch handle: reads the machine's (rotating) token live; StopAsync just decrefs
            // (NO checkin — the shared slot is checked in once at process exit).
            return new LeaseSession(() => _token!, _leaseId ?? "", () =>
            {
                Interlocked.Decrement(ref _refs);
                return Task.CompletedTask;
            }, BindLaunch, () => RefreshIfBehindEngineAsync(force: true));
        }

        /// Mint a fresh token when the one held is older than the newest the PRO engine has accepted here.
        ///
        /// The engine refuses such a token outright ("older than the last one accepted"), and a token reused
        /// from the cache or from memory for its 24 h life can be: the mark is per OS user, so anything else
        /// launching with a newer token moves it (another process, the hosted-browser gateway, another key).
        /// True when the token was replaced. <paramref name="force"/> skips the comparison: the engine has
        /// just refused this token (a race this check could not see coming).
        public async Task<bool> RefreshIfBehindEngineAsync(bool force = false)
        {
            if (_scope == "browser" || _token is null) return false;
            await _refreshGate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (!force)
                {
                    var iat = TokenIat(_token);
                    var hwm = EngineHwm();
                    if (iat is null or 0 || hwm == 0 || iat >= hwm) return false;
                }
                return await FreshTokenAsync().ConfigureAwait(false);
            }
            finally { _refreshGate.Release(); }
        }

        /// Replace the token with a freshly minted one. First by heartbeating the lease this process knows
        /// (its own, or the one another process's owner keeps alive): same lease, nothing revoked, so two
        /// processes never knock each other's lease out. With no lease id, or once the lease is gone, check
        /// out again like a cold acquire. A definitive refusal (limit, revoked) throws.
        private async Task<bool> FreshTokenAsync()
        {
            if (!string.IsNullOrEmpty(_leaseId))
            {
                try
                {
                    using var res = await PostJsonAsync($"{_baseUrl}/api/v1/lease/heartbeat", _key,
                        new { lease_id = _leaseId, nonce = Guid.NewGuid().ToString() }, _proxy).ConfigureAwait(false);
                    if (res.IsSuccessStatusCode)
                    {
                        using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                        if (d.RootElement.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String)
                        {
                            SetToken(t.GetString()!);
                            if (d.RootElement.TryGetProperty("exp", out var e) && e.TryGetInt64(out var exp)) _exp = exp;
                            WriteCache(_key, _token!, _exp, _leaseId);
                            return true;
                        }
                    }
                }
                catch { /* unreachable: the checkout below says so properly */ }
            }
            var browserScoped = await CheckoutAsync(NewLaunchId()).ConfigureAwait(false);
            if (browserScoped is not null)
            {
                // The plan turned per-browser mid-process: that checkout is nobody's, so give it straight back.
                try { using var _ = await PostAsync("/api/v1/lease/checkin", new { lease_id = browserScoped.LeaseId }).ConfigureAwait(false); }
                catch { /* the lease TTL reclaims it */ }
                return false;
            }
            if (!_owner)
            {
                // The slot is this process's now: keep it alive, check it in at exit.
                _owner = true;
                StartHeartbeat();
            }
            return true;
        }

        /// POST a checkout for one launch. Throws the backend's verdict; network errors propagate.
        internal async Task<CheckoutData> CheckoutForAsync(string launchId)
        {
            using var res = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkout", _key,
                new { instance_id = _instanceId,
                      // per-browser plans count each launch_id as its own slot; machine plans ignore it
                      launch_id = launchId,
                      os = Native.OsTag, sdk_version = _sdkVersion,
                      engine_version = await EngineVerAsync().ConfigureAwait(false) }, _proxy).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode) await ThrowForStatusAsync(res).ConfigureAwait(false);
            using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            var root = doc.RootElement;
            return new CheckoutData(
                root.GetProperty("token").GetString()!,
                root.GetProperty("exp").GetInt64(),
                root.GetProperty("lease_id").GetString()!,
                root.TryGetProperty("heartbeat_interval_sec", out var hb) && hb.TryGetInt32(out var v) ? v : 270,
                root.TryGetProperty("lease_scope", out var sc) && sc.ValueKind == JsonValueKind.String && sc.GetString() == "browser");
        }

        /// A licence call over this lease's route (direct or through the proxy), for a BrowserLease.
        internal Task<HttpResponseMessage> PostAsync(string path, object body)
            => PostJsonAsync($"{_baseUrl}{path}", _key, body, _proxy);

        /// Machine-lease cold checkout. Returns the checkout when the backend says it is per-browser
        /// (the caller hands it to that one launch); null when the machine lease now holds it.
        private async Task<CheckoutData?> CheckoutAsync(string launchId)
        {
            try
            {
                var d = await CheckoutForAsync(launchId).ConfigureAwait(false);
                if (d.BrowserScope) return d;
                SetToken(d.Token);
                _exp = d.Exp;
                _leaseId = d.LeaseId;
                _hbSec = d.HeartbeatSec;
                WriteCache(_key, _token!, _exp, _leaseId);
                return null;
            }
            catch (LicenseError) { throw; } // a definitive verdict must surface (never silently downgrade)
            catch (Exception e)
            {
                var cached = ReadCache(_key);
                if (cached is { } c && c.exp > NowSec() + 60)
                {
                    if (!_quiet) Console.Error.WriteLine($"[clearcote] [license] backend unreachable ({e.Message}); using cached run-token.");
                    SetToken(c.token); _exp = c.exp; _leaseId = c.leaseId;
                    return null;
                }
                throw new LicenseError($"Could not reach the license server and no valid cached token: {e.Message}");
            }
        }

        /// A new launch on a per-browser plan: its own checkout. A refusal (one browser running) throws.
        private async Task<LeaseSession> AcquireBrowserAsync()
        {
            var launchId = NewLaunchId();
            CheckoutData d;
            try { d = await CheckoutForAsync(launchId).ConfigureAwait(false); }
            catch (LicenseError) { throw; }
            catch (Exception e)
            {
                // No offline grace: without the backend there is no slot for this browser.
                throw new LicenseError($"Could not reach the license server to start this browser: {e.Message}");
            }
            return StartBrowser(d, launchId);
        }

        private LeaseSession StartBrowser(CheckoutData d, string launchId)
        {
            var lease = new BrowserLease(this, d, launchId);
            _browsers[lease] = 0;
            return lease.Session;
        }

        internal void Forget(BrowserLease lease) => _browsers.TryRemove(lease, out _);

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
                            new { lease_id = _leaseId, nonce = Guid.NewGuid().ToString() }, _proxy).ConfigureAwait(false);
                        if ((int)res.StatusCode == 409) // reclaimed/expired -> re-checkout to keep the slot
                        {
                            using var co = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkout", _key,
                                new { instance_id = _instanceId, os = Native.OsTag, sdk_version = _sdkVersion,
                                      engine_version = await EngineVerAsync().ConfigureAwait(false) }, _proxy).ConfigureAwait(false);
                            if (co.IsSuccessStatusCode)
                            {
                                using var d = JsonDocument.Parse(await co.Content.ReadAsStringAsync().ConfigureAwait(false));
                                _leaseId = d.RootElement.GetProperty("lease_id").GetString()!;
                                SetToken(d.RootElement.GetProperty("token").GetString()!);
                                _exp = d.RootElement.GetProperty("exp").GetInt64();
                                WriteCache(_key, _token!, _exp, _leaseId);
                            }
                            continue;
                        }
                        if (res.IsSuccessStatusCode)
                        {
                            using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                            SetToken(d.RootElement.GetProperty("token").GetString()!);
                            _exp = d.RootElement.GetProperty("exp").GetInt64();
                            WriteCache(_key, _token!, _exp, _leaseId);
                        }
                    }
                    catch { /* transient — offline grace until token exp */ }
                }
            });
        }

        // Single checkin at process exit (owner only). Frees the slot without waiting for the TTL.
        public async Task ShutdownAsync()
        {
            _tokenFiles.CloseAll();
            // Per-browser leases still open at exit (a browser nobody closed): release their slots too.
            foreach (var b in _browsers.Keys.ToArray())
                try { await b.StopAsync().ConfigureAwait(false); } catch { /* best-effort */ }
            _hbCts?.Cancel();
            if (_owner && _leaseId != null)
            {
                try
                {
                    using var _ = await PostJsonAsync($"{_baseUrl}/api/v1/lease/checkin", _key,
                        new { lease_id = _leaseId }, _proxy).ConfigureAwait(false);
                }
                catch { /* best-effort; the lease TTL reclaims it anyway */ }
            }
        }
    }

    internal sealed record CheckoutData(string Token, long Exp, string LeaseId, int HeartbeatSec, bool BrowserScope);

    // One browser's own lease on a per-browser plan (the free tier: "1 browser at a time"). Checked out
    // for exactly one launch, heartbeated while that browser runs, checked in when it closes. Its token
    // is never written to the shared cache and never handed to another launch.
    private sealed class BrowserLease
    {
        private readonly MachineLease _owner;
        private readonly string _launchId;
        private readonly CancellationTokenSource _cts = new();
        private readonly TokenFileSet _tokenFiles = new();
        private volatile string _token;
        private int _stopped;
        public LeaseSession Session { get; }

        // Set this browser's rotating token AND push it into every bound run-token file.
        private void SetToken(string v)
        {
            _token = v;
            _tokenFiles.Update(v);
        }

        /// Bind a per-launch token file that follows this browser's rotating token.
        private LaunchToken BindLaunch() => _tokenFiles.Bind(_token);

        public BrowserLease(MachineLease owner, CheckoutData d, string launchId)
        {
            _owner = owner;
            _launchId = launchId;
            _token = d.Token;
            Session = new LeaseSession(() => _token, d.LeaseId, StopAsync, BindLaunch, RefreshTokenAsync);
            var hbMs = Math.Max(5, d.HeartbeatSec) * 1000;
            var ct = _cts.Token;
            _ = Task.Run(async () =>
            {
                while (!ct.IsCancellationRequested)
                {
                    try { await Task.Delay(hbMs, ct).ConfigureAwait(false); }
                    catch (OperationCanceledException) { break; }
                    try
                    {
                        using var res = await _owner.PostAsync("/api/v1/lease/heartbeat",
                            new { lease_id = Session.LeaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
                        if (ct.IsCancellationRequested) break;
                        if ((int)res.StatusCode == 409)
                        {
                            // Reclaimed: re-take the slot as the SAME launch (the backend's own-lease takeover).
                            var again = await _owner.CheckoutForAsync(_launchId).ConfigureAwait(false);
                            Session.LeaseId = again.LeaseId;
                            SetToken(again.Token);
                            continue;
                        }
                        if (res.IsSuccessStatusCode)
                        {
                            using var d2 = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                            if (d2.RootElement.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String)
                                SetToken(t.GetString()!);
                        }
                    }
                    catch { /* transient; the next beat retries, and the lease TTL is the backstop */ }
                }
            });
        }

        /// The engine refused this token as older than one it accepted: mint a fresh one by heartbeating this
        /// browser's lease or, if the lease is gone, by re-taking the slot as the SAME launch.
        public async Task<bool> RefreshTokenAsync()
        {
            using var res = await _owner.PostAsync("/api/v1/lease/heartbeat",
                new { lease_id = Session.LeaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
            if (res.IsSuccessStatusCode)
            {
                using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                if (!d.RootElement.TryGetProperty("token", out var t) || t.ValueKind != JsonValueKind.String) return false;
                SetToken(t.GetString()!);
                return true;
            }
            if ((int)res.StatusCode != 409) return false;
            var again = await _owner.CheckoutForAsync(_launchId).ConfigureAwait(false); // a refusal throws
            Session.LeaseId = again.LeaseId;
            SetToken(again.Token);
            return true;
        }

        public async Task StopAsync()
        {
            if (Interlocked.Exchange(ref _stopped, 1) != 0) return;
            _cts.Cancel();
            _tokenFiles.CloseAll();
            _owner.Forget(this);
            try
            {
                using var _ = await _owner.PostAsync("/api/v1/lease/checkin", new { lease_id = Session.LeaseId }).ConfigureAwait(false);
            }
            catch { /* best-effort; the lease TTL reclaims it */ }
        }
    }

    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, MachineLease> _machineLeases = new();
    private static int _exitHooked;

    /// Acquire a concurrency lease for one launch. Returns null in free mode (no key).
    ///
    /// Machine plans (every paid plan): a per-MACHINE lease shared across every launch in this process
    /// (checks out ~once per token-TTL, not once per launch); StopAsync only drops a reference.
    /// Per-browser plans (the GitHub free tier, told by the backend's lease_scope "browser"): every
    /// launch checks out its own slot and StopAsync releases it, so a second browser is refused while
    /// one runs. Throws
    /// <see cref="ConcurrencyLimitError"/> / <see cref="LicenseRevokedError"/> / <see cref="LicenseError"/>
    /// only on a cold checkout the backend definitively refuses; falls back to a cached, still-valid
    /// token on a transient network failure (offline grace).
    ///
    /// <paramref name="sdkVersion"/> is the SDK PACKAGE version (checkout telemetry sdk_version).
    /// <paramref name="engineVersion"/> lazily resolves the browser build (engine_version) — invoked
    /// on a cold checkout only, so the catalog is never consulted per launch.
    public static async Task<LeaseSession?> AcquireLeaseAsync(
        LicenseOptions opts, string? sdkVersion = null, bool quiet = false,
        Func<Task<string?>>? engineVersion = null, ProxyOptions? proxy = null)
    {
        var licenseKey = ResolveLicenseKey(opts.LicenseKey);
        if (licenseKey is null) return null; // free mode — inert

        var baseUrl = ApiBase(opts);
        var wantProxy = LicenseThroughProxyRequested(opts.LicenseThroughProxy);
        var viaProxy = wantProxy ? ProxySpec.From(proxy) : null;
        if (wantProxy && viaProxy is null && !quiet)
            Console.Error.WriteLine("[clearcote] [license] licenseThroughProxy is on but this launch has no proxy; licence calls go direct.");
        // One lease per (key, route): a direct lease and a proxied lease are different network paths,
        // so they must not share the in-process heartbeat owner. The proxy username is part of the
        // route: rotating-session proxies encode the exit in it.
        var mapKey = viaProxy is null ? licenseKey : $"{licenseKey}|{viaProxy.ServerString}|{viaProxy.Username}";
        var ml = _machineLeases.GetOrAdd(mapKey,
            _ => new MachineLease(licenseKey, baseUrl, ResolveInstanceId(), sdkVersion, engineVersion, quiet, viaProxy));
        if (Interlocked.Exchange(ref _exitHooked, 1) == 0)
            AppDomain.CurrentDomain.ProcessExit += (_, _) => ShutdownAllLeasesAsync().GetAwaiter().GetResult();
        return await ml.AcquireAsync().ConfigureAwait(false);
    }

    /// Stop every machine lease's heartbeat and check it in (process exit), and forget it. Tests call this so
    /// a lease's background heartbeat never outlives its test and lands on the next test's handler.
    internal static async Task ShutdownAllLeasesAsync()
    {
        foreach (var (key, m) in _machineLeases.ToArray())
        {
            _machineLeases.TryRemove(key, out _);
            try { await m.ShutdownAsync().ConfigureAwait(false); } catch { /* best-effort */ }
        }
    }

    /// Start a browser; if it fails to start, release the lease before re-throwing. On a per-browser plan
    /// (the free tier) the slot would otherwise stay taken until the lease TTL. <paramref name="launchToken"/>
    /// (the launch's run-token file) goes too: a paid lease is shared, so stopping it only drops a reference,
    /// and the file stayed in the temp directory until the process exited.
    public static async Task<T> ReleaseLeaseOnFailureAsync<T>(LeaseSession? lease, Func<Task<T>> start, LaunchToken? launchToken = null)
    {
        try
        {
            return await start().ConfigureAwait(false);
        }
        catch
        {
            if (lease is not null) { try { await lease.StopAsync().ConfigureAwait(false); } catch { /* the start failure is what matters */ } }
            try { launchToken?.Release(); } catch { /* likewise */ }
            throw;
        }
    }

    /// Start a browser; if the PRO engine refuses the run-token as older than one it has already accepted on
    /// this machine ("older than the last one accepted"), mint a fresh token and start once more.
    ///
    /// AcquireLeaseAsync already replaces a token it can SEE is older than the engine's mark. This covers the
    /// race it cannot see: another process (a parallel launch, the hosted-browser gateway, another key) moving
    /// the mark between that check and this launch. <paramref name="start"/> must build its env from
    /// <c>lease.Token</c> each time it is called; the bound token file follows the lease by itself.
    /// Playwright's launch error carries the engine's stderr, which is where the refusal lands.
    public static async Task<T> RetryOnStaleRunTokenAsync<T>(LeaseSession? lease, Func<Task<T>> start)
    {
        try
        {
            return await start().ConfigureAwait(false);
        }
        catch (Exception e) when (lease is not null && e.ToString().Contains(StaleTokenRefusal))
        {
            if (!await lease.RefreshTokenAsync().ConfigureAwait(false)) throw;
            return await start().ConfigureAwait(false);
        }
    }

    /// Seats in use on a licence right now (live leases), without checking one out. Never cached and
    /// never throws: an unreachable backend, or an older one without the endpoint, reports
    /// <see cref="SeatsState.Unavailable"/> with the reason rather than a guessed number. Goes through
    /// <paramref name="proxy"/> only when LicenseThroughProxy is on.
    public static async Task<SessionSeats> GetSessionSeatsAsync(LicenseOptions? opts = null, ProxyOptions? proxy = null)
    {
        opts ??= new LicenseOptions();
        var key = ResolveLicenseKey(opts.LicenseKey);
        if (key is null) return new SessionSeats(SeatsState.NoKey);
        try
        {
            var via = LicenseThroughProxyRequested(opts.LicenseThroughProxy) ? ProxySpec.From(proxy) : null;
            using var client = ProxiedHttp.Create(via);
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{ApiBase(opts)}/api/v1/lease/seats");
            req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", key);
            req.Headers.UserAgent.ParseAdd(LicenseUserAgent);
            using var res = await client.SendAsync(req, cts.Token).ConfigureAwait(false);
            var status = (int)res.StatusCode;
            int? used = null, limit = null;
            string? plan = null, error = null;
            var hasUsed = false;
            try
            {
                using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false));
                var r = doc.RootElement;
                if (r.ValueKind == JsonValueKind.Object)
                {
                    if (r.TryGetProperty("used", out var u) && u.ValueKind == JsonValueKind.Number) { used = u.GetInt32(); hasUsed = true; }
                    if (r.TryGetProperty("limit", out var l) && l.ValueKind == JsonValueKind.Number) limit = l.GetInt32();
                    if (r.TryGetProperty("plan", out var p) && p.ValueKind == JsonValueKind.String) plan = p.GetString();
                    if (r.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String) error = e.GetString();
                }
            }
            catch { /* non-JSON body */ }
            if (res.IsSuccessStatusCode && hasUsed) return new SessionSeats(SeatsState.Ok, used, limit, plan);
            if (status is 401 or 403) return new SessionSeats(SeatsState.Invalid, Reason: string.IsNullOrEmpty(error) ? $"HTTP {status}" : error);
            if (status == 404) return new SessionSeats(SeatsState.Unavailable, Reason: "this licence server does not report seats yet");
            return new SessionSeats(SeatsState.Unavailable, Reason: string.IsNullOrEmpty(error) ? $"HTTP {status}" : error);
        }
        catch (Exception e)
        {
            return new SessionSeats(SeatsState.Unavailable, Reason: $"licence server unreachable ({e.Message})");
        }
    }

    internal const string RunTokenFileEnv = "CLEARCOTE_RUN_TOKEN_FILE";

    /// Merge the run-token into an env dictionary (base defaults to the current process env). When
    /// <paramref name="tokenFile"/> is given it is also passed as CLEARCOTE_RUN_TOKEN_FILE: a
    /// supporting engine (r23+) re-reads that file so revoke/check-in/over-limit stops a running free
    /// browser. Older engines ignore it, so it is purely additive — the launch-time token is unchanged.
    public static Dictionary<string, string> WithRunToken(string token, IDictionary<string, string>? baseEnv, string? tokenFile = null)
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
        if (tokenFile is not null) outEnv[RunTokenFileEnv] = tokenFile;
        return outEnv;
    }
}
