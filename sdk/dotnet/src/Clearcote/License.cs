using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Clearcote;

/// A definitive licensing failure. Never silently downgraded to the free build.
public class LicenseError : Exception
{
    public string Code { get; }
    /// HTTP status of the answer this error reports; null when the licence server never answered.
    public int? Status { get; internal set; }
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

/// The licence server never answered: something in front of it (a CDN firewall or bot check, e.g. the
/// "Vercel Security Checkpoint") returned a challenge page instead of a licence verdict. Says nothing about
/// the licence itself, which is NOT revoked or over its limit. Retryable: such challenges are usually
/// temporary and tied to this machine's network; <see cref="LicenseOptions.LicenseThroughProxy"/> sends
/// the calls another way.
public sealed class LicenseServerChallengeError : LicenseError
{
    public bool Retryable => true;
    public LicenseServerChallengeError(string message) : base(message, "LICENSE_SERVER_CHALLENGE") { }
}

/// A licence call that ended without the licence server's verdict: it could not be reached, answered a 5xx,
/// or kept rate-limiting this IP address. Says nothing about the licence. The message and code are the ones
/// these failures always had; <see cref="Reason"/> is the short form for the uncounted-launch warning.
internal sealed class LicenseNoVerdictError : LicenseError
{
    public string Reason { get; }
    public LicenseNoVerdictError(string message, string code, string reason) : base(message, code) => Reason = reason;
}

/// Options for resolving a license + reaching the backend.
public class LicenseOptions
{
    /// License key ("cc_lic_..."). Resolved from this &gt; CLEARCOTE_LICENSE_KEY env &gt; ~/.clearcote/license.key.
    public string? LicenseKey { get; set; }
    /// Backend base URL, used alone for every licence call. Default: CLEARCOTE_LICENSE_API env; else, for the
    /// lease calls (checkout, heartbeat, check-in, seats), CLEARCOTE_LEASE_API env, else the licence lease server
    /// (lease.clearcotelabs.com) with www.clearcotelabs.com as its fallback.
    public string? LicenseApiBase { get; set; }
    /// Send the licence calls (lease checkout / heartbeat / check-in, and seats) through the launch's
    /// own proxy instead of directly from this machine. Off by default: direct calls never spend proxy
    /// bandwidth. Null = CLEARCOTE_LICENSE_THROUGH_PROXY (1/true/yes/on). No effect without a proxy.
    public bool? LicenseThroughProxy { get; set; }
    /// Offline mode: path to an Offline Key file. Null = CLEARCOTE_OFFLINE_KEY. See <see cref="Offline"/>.
    public string? OfflineKey { get; set; }
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
///
/// LEFT BEHIND, ONE PER RUN (SC 283): a browser still open when the process ended kept its file until a
/// hook ran, and a process killed outright runs none. Every live file is in <see cref="Live"/>, removed
/// synchronously on ProcessExit; the name carries the pid and a host tag, and the first bind of a later
/// process sweeps files whose process is gone (<see cref="SweepDead"/>).
internal sealed class TokenFileSet
{
    private sealed class Entry { public required string Path; }
    private readonly HashSet<Entry> _files = new();
    private readonly object _lock = new();

    private static readonly HashSet<string> Live = new();
    private static int _hooked;
    // clearcote-rt-<pid>-<hosttag>-<guid N>.tok; the old clearcote-rt-<guid>.tok has no owner and is never swept.
    private static readonly System.Text.RegularExpressions.Regex FileRe =
        new(@"^clearcote-rt-(\d+)-([0-9a-f]{8})-[0-9a-f]{32}\.tok$");

    /// Short tag of this machine: a temp dir shared across hosts (a mounted volume) never sweeps another's files.
    internal static string HostTag() =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Environment.MachineName))).ToLowerInvariant()[..8];

    /// Remove every live run-token file now (process exit).
    internal static void RemoveLive()
    {
        string[] snapshot;
        lock (Live) { snapshot = Live.ToArray(); Live.Clear(); }
        foreach (var p in snapshot) { try { System.IO.File.Delete(p); } catch { /* ignore */ } }
    }

    /// Remove run-token files whose process is gone (killed, crashed: it ran no exit hook). Only files this
    /// host wrote with a pid in the name; a live pid (or a reused one) keeps its file.
    internal static int SweepDead(string? dir = null, Func<int, bool>? alive = null)
    {
        alive ??= DockerLaunch.PidAlive;
        dir ??= System.IO.Path.GetTempPath();
        string[] names;
        try { names = System.IO.Directory.GetFiles(dir, "clearcote-rt-*.tok"); } catch { return 0; }
        var tag = HostTag();
        var removed = 0;
        foreach (var full in names)
        {
            var m = FileRe.Match(System.IO.Path.GetFileName(full));
            if (!m.Success || m.Groups[2].Value != tag) continue;
            if (!int.TryParse(m.Groups[1].Value, out var pid) || pid == Environment.ProcessId || alive(pid)) continue;
            try { System.IO.File.Delete(full); removed++; } catch { /* another process got it */ }
        }
        return removed;
    }

    /// Create a token file seeded with <paramref name="current"/>, kept updated until Release removes it.
    public LaunchToken Bind(string current)
    {
        if (Interlocked.Exchange(ref _hooked, 1) == 0)
        {
            SweepDead();
            AppDomain.CurrentDomain.ProcessExit += (_, _) => RemoveLive();
        }
        var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(),
            $"clearcote-rt-{Environment.ProcessId}-{HostTag()}-{Guid.NewGuid():N}.tok");
        WriteOne(path, current);
        var entry = new Entry { Path = path };
        lock (_lock) _files.Add(entry);
        lock (Live) Live.Add(path);
        return new LaunchToken(path, () =>
        {
            lock (_lock) _files.Remove(entry);
            lock (Live) Live.Remove(path);
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
        lock (Live) foreach (var e in snapshot) Live.Remove(e.Path);
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

    // ── the saved licence key and what device login recorded about it (license.ts) ──
    // The same files as the Node and Python SDKs and the clearcote CLI, so all of them share one saved key.

    /// Where <c>clearcote login</c> stores the key: ~/.clearcote/license.key.
    public static string LicenseKeyPath() => Path.Combine(Native.ClearcoteDir, "license.key");

    /// Save a licence key for every later launch (owner-only permissions where the OS supports it). Returns the path.
    public static string SaveLicenseKey(string key)
    {
        var k = (key ?? "").Trim();
        if (k.Length == 0) throw new LicenseError("Empty licence key.", "LICENSE_EMPTY");
        var p = LicenseKeyPath();
        Directory.CreateDirectory(Native.ClearcoteDir);
        WriteOwnerOnly(p, k + "\n");
        return p;
    }

    /// Remove the saved key (and what device login recorded about it). True when the key file was removed.
    public static bool RemoveLicenseKey()
    {
        RemoveLicenseMeta();
        var p = LicenseKeyPath();
        if (!File.Exists(p)) return false;
        try { File.Delete(p); } catch { return false; }
        return true;
    }

    /// Where the key a launch would use comes from ("option", "env", "file" or "none"), and the key masked
    /// (its first 7 and last 4 characters), never the key itself.
    public static LicenseKeySourceInfo LicenseKeySource(string? @explicit = null)
    {
        static string Mask(string k) => k.Length > 12 ? $"{k[..7]}\u2026{k[^4..]}" : "\u2026";
        if (!string.IsNullOrWhiteSpace(@explicit)) return new("option", Mask(@explicit.Trim()));
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_KEY");
        if (!string.IsNullOrWhiteSpace(env)) return new("env", Mask(env.Trim()));
        try
        {
            var p = LicenseKeyPath();
            if (File.Exists(p))
            {
                var v = File.ReadAllText(p).Trim();
                if (v.Length > 0) return new("file", Mask(v));
            }
        }
        catch { /* ignore */ }
        return new("none", null);
    }

    // What `clearcote login --device` learned about the key it saved. No licence endpoint reports a key's expiry, so
    // the device-token answer is the one source. Bound to the key by its SHA-256, so a key saved later (pasted, or set
    // in CLEARCOTE_LICENSE_KEY) is never described by another key's record.
    private const string ExpiryNote = "as the site reported it at `clearcote login --device`; no licence endpoint reports a key's expiry";
    private const string ExpiryUnknownNote = "not known: only `clearcote login --device` records it, and no licence endpoint reports a key's expiry";

    /// ~/.clearcote/license.meta.json: the plan and expiry device login recorded for the saved key.
    public static string LicenseMetaPath() => Path.Combine(Native.ClearcoteDir, "license.meta.json");

    private static string KeyDigest(string key) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(key.Trim()))).ToLowerInvariant();

    /// Record the plan and expiry the device login returned for <paramref name="key"/> (never the key itself).
    /// Returns the path.
    public static string SaveLicenseMeta(string key, string? plan = null, string? expiresAt = null)
    {
        var p = LicenseMetaPath();
        Directory.CreateDirectory(Native.ClearcoteDir);
        var record = new
        {
            key_sha256 = KeyDigest(key), plan, expires_at = expiresAt, source = "device-login",
            recorded_at = DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", System.Globalization.CultureInfo.InvariantCulture),
        };
        WriteOwnerOnly(p, JsonSerializer.Serialize(record) + "\n");
        return p;
    }

    /// Remove the device-login record; with <paramref name="unlessKey"/>, only when it describes a different key.
    public static bool RemoveLicenseMeta(string? unlessKey = null)
    {
        var p = LicenseMetaPath();
        if (!File.Exists(p)) return false;
        if (unlessKey is not null)
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(p));
                if (doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("key_sha256", out var h)
                    && h.ValueKind == JsonValueKind.String && h.GetString() == KeyDigest(unlessKey)) return false;
            }
            catch { /* unreadable: replace it */ }
        }
        try { File.Delete(p); } catch { return false; }
        return true;
    }

    /// The expiry of the key a launch would use (license.ts licenseExpiry): source "device-login" when device login
    /// recorded it for this very key (<see cref="LicenseExpiryInfo.ExpiresAt"/> null = no expiry); source "unknown" for any
    /// other key; null without a key.
    public static LicenseExpiryInfo? LicenseExpiry(string? @explicit = null)
    {
        var key = ResolveLicenseKey(@explicit);
        if (key is null) return null;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(LicenseMetaPath()));
            var r = doc.RootElement;
            if (r.ValueKind == JsonValueKind.Object && r.TryGetProperty("key_sha256", out var h) && h.ValueKind == JsonValueKind.String
                && h.GetString() == KeyDigest(key))
            {
                string? S(string k) => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
                return new LicenseExpiryInfo("device-login", S("expires_at"), S("recorded_at"), ExpiryNote);
            }
        }
        catch { /* no record */ }
        return new LicenseExpiryInfo("unknown", null, null, ExpiryUnknownNote);
    }

    // Owner-only (0600) where the OS supports it: created so before the content goes in.
    private static void WriteOwnerOnly(string path, string content)
    {
        if (OperatingSystem.IsWindows()) { File.WriteAllText(path, content); return; }
        using (var fs = new FileStream(path, new FileStreamOptions
        {
            Mode = FileMode.Create, Access = FileAccess.Write, UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
        }))
        using (var w = new StreamWriter(fs, new UTF8Encoding(false))) w.Write(content);
        try { File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite); } catch { /* not supported on this filesystem */ }
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

    // ── lease API base ───────────────────────────────────────────────────────
    // The lease calls (checkout, heartbeat, check-in, seats) go to the licence lease server, with the website
    // as the fallback when it cannot be reached. Every other licence route (the PRO download) keeps ApiBase.
    // Internal, not const: tests point them at local servers.
    internal static string DefaultLeaseBase = "https://lease.clearcotelabs.com";
    internal static string FallbackLeaseBase = DefaultApiBase;
    /// How long the process sends the lease calls straight to the fallback after the lease server failed to answer.
    internal static TimeSpan PrimarySkipFor = TimeSpan.FromSeconds(60);
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, long> _primaryDownUntil = new();

    /// Forget that the lease server failed to answer (tests).
    internal static void ForgetLeaseServerDown() => _primaryDownUntil.Clear();

    /// Where a lease call goes, and (default route only) where it goes when that base cannot be reached.
    internal sealed record LeaseRoute(string Primary, string? Fallback);

    /// The lease calls' route. Anything explicit is used alone, with no fallback: the caller's LicenseApiBase,
    /// else CLEARCOTE_LICENSE_API (staging, tests, self-hosting), else CLEARCOTE_LEASE_API. Otherwise the
    /// licence lease server, falling back to the website.
    internal static LeaseRoute LeaseRouteFor(LicenseOptions opts)
    {
        foreach (var b in new[] { opts.LicenseApiBase, Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_API"),
                                  Environment.GetEnvironmentVariable("CLEARCOTE_LEASE_API") })
            if (!string.IsNullOrWhiteSpace(b)) return new LeaseRoute(b.Trim().TrimEnd('/'), null);
        return new LeaseRoute(DefaultLeaseBase.TrimEnd('/'), FallbackLeaseBase.TrimEnd('/'));
    }

    // A failure to answer, not an answer: the call goes to the fallback. Anything the lease server itself
    // answers with JSON (200, 401, 403, 409, 426, 429, ...) is final.
    private static bool IsNetworkError(Exception e) =>
        e is HttpRequestException or OperationCanceledException or IOException or System.Security.Authentication.AuthenticationException;

    private static bool IsJson(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        try { using var _ = JsonDocument.Parse(text); return true; }
        catch (JsonException) { return false; }
    }

    /// The lease server's answer is not its own: a gateway/CDN error, or a challenge page instead of JSON.
    private static async Task<bool> NotAnsweredAsync(HttpResponseMessage res)
    {
        var status = (int)res.StatusCode;
        if (status is 502 or 503 or 504 or (>= 520 and <= 527) or 530) return true;
        if (status < 400) return false;
        var text = "";
        try { text = await res.Content.ReadAsStringAsync().ConfigureAwait(false); }
        catch { /* no body */ }
        return ChallengeReason(res, text) is not null && !IsJson(text);
    }

    /// Send one lease call over <paramref name="route"/>. On the default route a lease server that does not
    /// answer (network error, timeout, 502/503/504/520-527/530, or a challenge page) gets the call sent once to
    /// the fallback, and is skipped for <see cref="PrimarySkipFor"/> after that.
    /// <paramref name="via"/>: the proxy the call goes through, if any: a proxied route's skip is kept apart from
    /// the direct one (the lease server can be unreachable from one network path and not the other).
    internal static async Task<HttpResponseMessage> SendLeaseAsync(LeaseRoute route, Func<string, Task<HttpResponseMessage>> send, ProxySpec? via = null)
    {
        if (route.Fallback is null) return await send(route.Primary).ConfigureAwait(false);
        var key = $"{route.Primary}|{via?.ServerString ?? ""}";
        if (_primaryDownUntil.TryGetValue(key, out var until) && Environment.TickCount64 < until)
            return await send(route.Fallback).ConfigureAwait(false);
        HttpResponseMessage res;
        try { res = await send(route.Primary).ConfigureAwait(false); }
        catch (Exception e) when (IsNetworkError(e))
        {
            _primaryDownUntil[key] = Environment.TickCount64 + (long)PrimarySkipFor.TotalMilliseconds;
            return await send(route.Fallback).ConfigureAwait(false);
        }
        if (!await NotAnsweredAsync(res).ConfigureAwait(false)) return res;
        res.Dispose();
        _primaryDownUntil[key] = Environment.TickCount64 + (long)PrimarySkipFor.TotalMilliseconds;
        return await send(route.Fallback).ConfigureAwait(false);
    }

    // ── offline token cache (best-effort grace) ──────────────────────────────
    private static string KeyId(string licenseKey) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(licenseKey))).ToLowerInvariant()[..16];

    private static string CachePath(string licenseKey) => Path.Combine(Native.ClearcoteDir, $"lease-{KeyId(licenseKey)}.json");

    // A paid per-browser lease's newest run-token, for a launch while the licence server cannot be reached. Its
    // own file: older SDKs read lease-<hash>.json and would hand a per-browser token to another browser.
    private static string GracePath(string licenseKey) => Path.Combine(Native.ClearcoteDir, $"grace-{KeyId(licenseKey)}.json");

    private static (string token, long exp)? ReadGrace(string licenseKey)
    {
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(GracePath(licenseKey)));
            var root = doc.RootElement;
            if (root.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String
                && root.TryGetProperty("exp", out var e) && e.TryGetInt64(out var exp))
                return (t.GetString()!, exp);
        }
        catch { /* missing or unreadable */ }
        return null;
    }

    /// Keep a paid per-browser run-token as the grace token. Never replaces a newer one (by iat) with an older
    /// one, so a slow heartbeat cannot roll it back. Written to a temporary file and renamed into place.
    private static readonly object _graceLock = new();   // two browsers' heartbeats: compare and write as one step

    internal static void WriteGrace(string licenseKey, string token, long exp)
    {
        lock (_graceLock)
        {
            try
            {
                if (ReadGrace(licenseKey) is { } held && (TokenIat(token) ?? 0) < (TokenIat(held.token) ?? 0)) return;
                WriteAtomic(GracePath(licenseKey), JsonSerializer.Serialize(new { token, exp }));
            }
            catch { /* best-effort, like the lease cache */ }
        }
    }

    /// Write <paramref name="path"/> through a temporary file renamed into place, so another process never reads
    /// half of it. Throws on failure (the temporary file is removed).
    private static void WriteAtomic(string path, string content)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var tmp = $"{path}.tmp-{Guid.NewGuid():N}";
        File.WriteAllText(tmp, content);
        for (var attempt = 0; ; attempt++)
        {
            try { File.Move(tmp, path, overwrite: true); return; }
            catch (Exception e) when (OperatingSystem.IsWindows() && attempt < 9 && e is IOException or UnauthorizedAccessException)
            {
                Thread.Sleep(50); // Windows: a reader has the old one open this instant
            }
            catch { try { File.Delete(tmp); } catch { } throw; }
        }
    }

    // The on-disk cache now also carries lease_id (for the exit checkin). A LEGACY cache written by an
    // older SDK (token+exp only) is still honored — leaseId is simply null then. machine: the file says
    // scope "machine", which this SDK writes only for a checkout that offered the browser scope and got the
    // machine lease. Only such a file lets a launch skip its checkout; an unmarked one (an older SDK wrote it,
    // never asking for a per-browser lease) is offline grace only.
    private static (string token, long exp, string? leaseId, bool machine)? ReadCache(string licenseKey)
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
                var machine = root.TryGetProperty("scope", out var sc) && sc.ValueKind == JsonValueKind.String && sc.GetString() == "machine";
                return (t.GetString()!, exp, lid, machine);
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

    // token/exp/lease_id exactly as older SDKs read them; scope "machine" only for a confirmed machine lease.
    internal static void WriteCache(string licenseKey, string token, long exp, string? leaseId, bool machine)
    {
        try
        {
            // Atomically: another process reading it mid-write would see no cache and check out a lease of its own.
            WriteAtomic(CachePath(licenseKey), machine
                ? JsonSerializer.Serialize(new { token, exp, lease_id = leaseId, scope = "machine" })
                : JsonSerializer.Serialize(new { token, exp, lease_id = leaseId }));
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

    /// Why an error answer is a firewall/bot challenge rather than the licence server's verdict, or null.
    /// The licence server answers every error with JSON. A Vercel challenge sets <c>x-vercel-mitigated</c> and
    /// serves an HTML page titled "Vercel Security Checkpoint"; any other non-JSON 403/429 comes from
    /// something in front of the server too, so it is not a revoke or a concurrency verdict either.
    internal static string? ChallengeReason(int status, string? contentType, string text, string? mitigated = null)
    {
        if (status < 400) return null;
        if (!string.IsNullOrEmpty(mitigated)) return $"x-vercel-mitigated: {mitigated}";
        if (text.Contains("Vercel Security Checkpoint", StringComparison.Ordinal)) return "Vercel Security Checkpoint";
        if ((status == 403 || status == 429) && !string.IsNullOrWhiteSpace(text))
        {
            try { using var _ = JsonDocument.Parse(text); return null; }
            catch (JsonException)
            {
                var ct = (contentType ?? "").Split(';')[0].Trim();
                return $"non-JSON {(ct.Length > 0 ? ct : "body")}";
            }
        }
        return null;
    }

    internal static string? ChallengeReason(HttpResponseMessage res, string text)
    {
        var mitigated = res.Headers.TryGetValues("x-vercel-mitigated", out var v) ? string.Join(",", v) : null;
        return ChallengeReason((int)res.StatusCode, res.Content.Headers.ContentType?.ToString(), text, mitigated);
    }

    private static async Task ThrowForStatusAsync(HttpResponseMessage res)
    {
        string? error = null, code = null;
        var text = "";
        try { text = await res.Content.ReadAsStringAsync().ConfigureAwait(false); }
        catch { /* no body */ }
        var status = (int)res.StatusCode;
        if (ChallengeReason(res, text) is { } why)
            throw new LicenseServerChallengeError(
                "The licence server could not be reached: a firewall in front of it answered with a security challenge " +
                $"(HTTP {status}, {why}) instead of a licence verdict, so your licence was not checked (it is not revoked). " +
                "This is usually temporary and tied to this machine's network: retry later, or send the licence calls " +
                "through your proxy (LicenseThroughProxy = true).") { Status = status };
        try
        {
            using var doc = JsonDocument.Parse(text);
            if (doc.RootElement.TryGetProperty("error", out var e)) error = e.GetString();
            if (doc.RootElement.TryGetProperty("code", out var c)) code = c.GetString();
        }
        catch { /* ignore */ }
        var msg = error ?? $"License request failed ({status}).";
        // This IP address's request budget at the licence server, not the licence's browser limit.
        if (status == 429 && code == RateLimitedCode)
            throw new LicenseNoVerdictError(
                $"{msg} The licence server limits how many requests one IP address sends; this is not your licence's " +
                "browser limit. Retry in a minute.", RateLimitedCode, "rate limited") { Status = status };
        if (status == 429 || code == "CONCURRENCY_LIMIT_EXCEEDED") throw new ConcurrencyLimitError(msg) { Status = status };
        if (status == 403 || code == "LICENSE_REVOKED" || code == "LICENSE_EXPIRED")
            throw new LicenseRevokedError(msg) { Status = status };
        if (status >= 500) throw new LicenseNoVerdictError(msg, code ?? $"HTTP_{status}", $"HTTP {status}") { Status = status };
        throw new LicenseError(msg, code ?? $"HTTP_{status}") { Status = status };
    }

    private const string RateLimitedCode = "RATE_LIMITED";

    /// The waits before each retry of a checkout the licence server rate-limited (tests shrink them).
    internal static TimeSpan[] RateLimitBackoff = { TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(4) };

    private static async Task<bool> IsRateLimitedAsync(HttpResponseMessage res)
    {
        if ((int)res.StatusCode != 429) return false;
        try
        {
            using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            return doc.RootElement.ValueKind == JsonValueKind.Object
                && doc.RootElement.TryGetProperty("code", out var c) && c.ValueKind == JsonValueKind.String
                && c.GetString() == RateLimitedCode;
        }
        catch { return false; }
    }

    /// Why a licence call ended without the licence server's verdict (unreachable, a 5xx, a challenge page,
    /// rate limited), or null for a verdict (revoked, over the limit, invalid, upgrade required, ...).
    private static string? NoVerdict(Exception e) => e switch
    {
        LicenseServerChallengeError => "security challenge",
        LicenseNoVerdictError n => n.Reason,
        _ => null,
    };

    // Browsers started on a cached run-token while the licence server could not be reached, per licence key.
    // The next checkout for the key reports them (uncounted_launches) and a 200 settles them.
    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, int> _uncounted = new();

    private static int UncountedLaunches(string licenseKey) => _uncounted.TryGetValue(licenseKey, out var n) ? n : 0;

    // Notices the licence server sent with a checkout, each printed once per process.
    private static readonly HashSet<string> _noticesShown = new();

    /// Print a successful checkout's <c>notice</c> (once per process).
    private static void ShowNotice(string answer, bool quiet)
    {
        if (quiet) return;
        string? notice = null;
        try
        {
            using var doc = JsonDocument.Parse(answer);
            if (doc.RootElement.ValueKind == JsonValueKind.Object && doc.RootElement.TryGetProperty("notice", out var n)
                && n.ValueKind == JsonValueKind.String)
                notice = System.Text.RegularExpressions.Regex.Replace(n.GetString()!, @"\p{Cc}+", " ").Trim();
        }
        catch { /* no notice */ }
        if (string.IsNullOrEmpty(notice)) return;
        lock (_noticesShown) if (!_noticesShown.Add(notice)) return;
        Console.Error.WriteLine($"[clearcote] [license] {notice}");
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
        private readonly string _key, _instanceId;
        private readonly LeaseRoute _route;
        private readonly string? _sdkVersion;
        private readonly Func<Task<string?>>? _engineVersion;
        private readonly bool _quiet;
        private readonly ProxySpec? _proxy;
        private readonly SemaphoreSlim _gate = new(1, 1);
        private readonly SemaphoreSlim _refreshGate = new(1, 1);
        private readonly TokenFileSet _tokenFiles = new();
        // Run-token files of the browsers started uncounted (each keeps the token it started with).
        private readonly TokenFileSet _uncountedFiles = new();
        private volatile string? _token;
        private long _exp;
        private string? _leaseId;
        private string? _engineResolved;   // memoized ("" once resolved-empty)
        private int _hbSec = 270;
        private bool _owner;               // only the cold-checkout owner heartbeats + checks in
        // The held lease is a confirmed machine lease (a checkout that offered the browser scope got it, or a
        // cache marked so): only then does the cache it writes let other processes skip their checkout.
        private bool _machineConfirmed;
        private CancellationTokenSource? _hbCts;
        private int _refs;
        // Learned from the first checkout: "browser" means every launch holds its own lease.
        private volatile string _scope = "machine";
        // The plan of this process's latest per-browser lease: null before the first, "free", or a paid plan.
        private volatile string? _browserPlan;
        private readonly System.Collections.Concurrent.ConcurrentDictionary<BrowserLease, byte> _browsers = new();

        public MachineLease(string key, LeaseRoute route, string instanceId, string? sdkVersion,
            Func<Task<string?>>? engineVersion, bool quiet, ProxySpec? proxy = null)
        {
            _key = key; _route = route; _instanceId = instanceId;
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
            LeaseSession? uncounted = null;
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                if (_scope != "browser" && !Valid())
                {
                    var cached = ReadCache(_key);
                    if (cached is { } c && c.machine && c.exp > NowSec() + 60)
                    {
                        // cross-process reuse: another process's owner keeps the slot alive. Only from a file
                        // marked machine: an older SDK's file says nothing about the lease scope, so check out.
                        SetToken(c.token); _exp = c.exp; _leaseId = c.leaseId; _owner = false; _machineConfirmed = true;
                    }
                    else
                    {
                        var launchId = NewLaunchId();
                        CheckoutData? d = null;
                        try { d = await CheckoutAsync(launchId, preferGrace: true).ConfigureAwait(false); }
                        catch (LicenseError e) when (NoVerdict(e) is not null)
                        {
                            // No verdict before this process has learned the lease scope: a paid per-browser licence
                            // (its grace token says so) still starts this browser, uncounted. Otherwise as before.
                            uncounted = Uncounted(NoVerdict(e)!);
                            if (uncounted is null) throw;
                        }
                        if (uncounted is not null) { /* no lease: nothing to share, heartbeat or check in */ }
                        else if (d is not null)
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

            if (uncounted is not null) return uncounted;
            if (pending is not null) return StartBrowser(pending, pendingLaunch!);
            if (_scope == "browser") return await AcquireBrowserAsync().ConfigureAwait(false);

            await RefreshIfBehindEngineAsync().ConfigureAwait(false);
            // The refresh's checkout said the licence is per-browser now: this launch takes its own lease.
            if (_scope == "browser") return await AcquireBrowserAsync().ConfigureAwait(false);
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
                    using var res = await PostAsync("/api/v1/lease/heartbeat",
                        new { lease_id = _leaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
                    if (res.IsSuccessStatusCode)
                    {
                        using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                        if (d.RootElement.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String)
                        {
                            SetToken(t.GetString()!);
                            if (d.RootElement.TryGetProperty("exp", out var e) && e.TryGetInt64(out var exp)) _exp = exp;
                            WriteCache(_key, _token!, _exp, _leaseId, _machineConfirmed);
                            return true;
                        }
                    }
                }
                catch { /* unreachable: the checkout below says so properly */ }
            }
            var browserScoped = await CheckoutAsync(NewLaunchId()).ConfigureAwait(false);
            if (browserScoped is not null)
            {
                // The plan turned per-browser mid-process: that checkout is nobody's, so give it straight back. The
                // next launches take their own leases.
                _scope = "browser";
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

        /// POST one checkout. Every checkout offers per-browser leases (a paid licence without a cap then counts
        /// each browser) and reports the browsers started uncounted since the last one; a 200 settles those and
        /// shows the server's notice. No <paramref name="launchId"/>: the machine lease re-taking its slot.
        private async Task<HttpResponseMessage> PostCheckoutAsync(string? launchId)
        {
            var body = new Dictionary<string, object?> { ["instance_id"] = _instanceId };
            // per-browser plans count each launch_id as its own slot; machine plans ignore it
            if (launchId is not null) body["launch_id"] = launchId;
            body["os"] = Native.OsTag;
            body["sdk_version"] = _sdkVersion;
            body["engine_version"] = await EngineVerAsync().ConfigureAwait(false);
            body["lease_scope_supported"] = new[] { "browser" };
            var uncounted = UncountedLaunches(_key);
            if (uncounted > 0) body["uncounted_launches"] = uncounted;
            var res = await PostAsync("/api/v1/lease/checkout", body).ConfigureAwait(false);
            if (res.StatusCode == System.Net.HttpStatusCode.OK)
            {
                // Launches added while this checkout was out are reported by the next one.
                if (uncounted > 0) _uncounted.AddOrUpdate(_key, 0, (_, n) => Math.Max(0, n - uncounted));
                ShowNotice(await res.Content.ReadAsStringAsync().ConfigureAwait(false), _quiet);
            }
            return res;
        }

        /// POST a checkout for one launch. Throws the backend's verdict; network errors propagate. A checkout the
        /// licence server rate-limits (RATE_LIMITED: this IP's request budget) is retried after
        /// <see cref="RateLimitBackoff"/>, then fails without a verdict.
        internal async Task<CheckoutData> CheckoutForAsync(string launchId)
        {
            for (var attempt = 0; ; attempt++)
            {
                using var res = await PostCheckoutAsync(launchId).ConfigureAwait(false);
                if (!res.IsSuccessStatusCode)
                {
                    if (attempt < RateLimitBackoff.Length && await IsRateLimitedAsync(res).ConfigureAwait(false))
                    {
                        await Task.Delay(RateLimitBackoff[attempt]).ConfigureAwait(false);
                        continue;
                    }
                    await ThrowForStatusAsync(res).ConfigureAwait(false);
                }
                using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                var root = doc.RootElement;
                return new CheckoutData(
                    root.GetProperty("token").GetString()!,
                    root.GetProperty("exp").GetInt64(),
                    root.GetProperty("lease_id").GetString()!,
                    root.TryGetProperty("heartbeat_interval_sec", out var hb) && hb.TryGetInt32(out var v) ? v : 270,
                    root.TryGetProperty("lease_scope", out var sc) && sc.ValueKind == JsonValueKind.String && sc.GetString() == "browser");
            }
        }

        /// A licence call over this lease's route (direct or through the proxy, with the default route's fallback).
        internal Task<HttpResponseMessage> PostAsync(string path, object body)
            => SendLeaseAsync(_route, b => PostJsonAsync($"{b}{path}", _key, body, _proxy), _proxy);

        /// A per-browser lease's new run-token: remember the plan, and keep a paid one as the grace token.
        internal void OnBrowserToken(string token, long? exp)
        {
            var plan = TokenPlan(token);
            _browserPlan = plan == PerBrowserPlan ? PerBrowserPlan : plan ?? "paid";
            if (plan != PerBrowserPlan && exp is { } e) WriteGrace(_key, token, e);
        }

        /// The paid grace token while it is still valid with a minute to spare, or null.
        private string? GraceToken() =>
            ReadGrace(_key) is { } g && g.exp > NowSec() + 60 && TokenPlan(g.token) != PerBrowserPlan ? g.token : null;

        /// A grace token this launch could start on (never on a licence known to be free).
        private bool UsableGrace() => _browserPlan != PerBrowserPlan && GraceToken() is not null;

        /// A per-browser checkout got no verdict (<paramref name="reason"/>). A licence known to be paid still starts
        /// this browser on a cached run-token (the grace token, else the legacy machine cache), uncounted: no lease,
        /// no heartbeat, reported by the next checkout. Null when there is no such token, or the licence is free.
        private LeaseSession? Uncounted(string reason)
        {
            if (_browserPlan == PerBrowserPlan) return null; // free stays fail-closed
            var now = NowSec();
            string? token = null;
            if (GraceToken() is { } g) token = g;
            else if (ReadCache(_key) is { } c && c.exp > now + 60) token = c.token; // never a free token
            if (token is null) return null;
            if (!_quiet)
                Console.Error.WriteLine($"[clearcote] [license] licence server unreachable ({reason}); " +
                                        "this browser starts on a cached run-token and is not counted.");
            _uncounted.AddOrUpdate(_key, 1, (_, n) => n + 1);
            // LeaseId "uncounted": there is no lease to heartbeat or check in.
            return new LeaseSession(() => token, "uncounted", () => Task.CompletedTask, () => _uncountedFiles.Bind(token));
        }

        /// Machine-lease cold checkout. Returns the checkout when the backend says it is per-browser
        /// (the caller hands it to that one launch); null when the machine lease now holds it.
        /// <paramref name="preferGrace"/>: a launch's cold checkout. Without a verdict, a usable paid grace token
        /// (this licence runs every browser on its own lease) goes before the cache's offline grace: the caller
        /// starts the browser on it, uncounted.
        private async Task<CheckoutData?> CheckoutAsync(string launchId, bool preferGrace = false)
        {
            try
            {
                var d = await CheckoutForAsync(launchId).ConfigureAwait(false);
                if (d.BrowserScope) return d;
                SetToken(d.Token);
                _exp = d.Exp;
                _leaseId = d.LeaseId;
                _hbSec = d.HeartbeatSec;
                // Offered the browser scope (with a launch_id) and got the machine lease: a confirmed machine lease.
                _machineConfirmed = true;
                WriteCache(_key, _token!, _exp, _leaseId, machine: true);
                return null;
            }
            catch (LicenseServerChallengeError e)
            {
                // Not a verdict: the server was never reached. Offline grace like a network error, and the
                // challenge itself surfaces when there is no cached token.
                if (preferGrace && UsableGrace()) throw;
                var cached = ReadCache(_key);
                if (cached is { } c && c.exp > NowSec() + 60)
                {
                    if (!_quiet) Console.Error.WriteLine($"[clearcote] [license] {e.Message} Using the cached run-token.");
                    SetToken(c.token); _exp = c.exp; _leaseId = c.leaseId; _machineConfirmed = c.machine;
                    return null;
                }
                throw;
            }
            catch (LicenseError) { throw; } // a definitive verdict must surface (never silently downgrade)
            catch (Exception e)
            {
                var cached = ReadCache(_key);
                if (cached is { } c && c.exp > NowSec() + 60 && !(preferGrace && IsNetworkError(e) && UsableGrace()))
                {
                    if (!_quiet) Console.Error.WriteLine($"[clearcote] [license] backend unreachable ({e.Message}); using cached run-token.");
                    // An older SDK's file (unmarked) is offline grace only: its heartbeats keep it unmarked.
                    SetToken(c.token); _exp = c.exp; _leaseId = c.leaseId; _machineConfirmed = c.machine;
                    return null;
                }
                var msg = $"Could not reach the license server and no valid cached token: {e.Message}";
                if (IsNetworkError(e)) throw new LicenseNoVerdictError(msg, "LICENSE_ERROR", e.Message);
                throw new LicenseError(msg);
            }
        }

        /// A new launch on a per-browser plan: its own checkout. A refusal (one browser running) throws. Without
        /// a verdict, a paid licence starts the browser uncounted on its grace token; free has no offline grace.
        private async Task<LeaseSession> AcquireBrowserAsync()
        {
            var launchId = NewLaunchId();
            CheckoutData d;
            try { d = await CheckoutForAsync(launchId).ConfigureAwait(false); }
            catch (LicenseError e) when (NoVerdict(e) is not null)
            {
                if (Uncounted(NoVerdict(e)!) is { } u) return u;
                throw;
            }
            catch (LicenseError) { throw; }
            catch (Exception e)
            {
                if (IsNetworkError(e) && Uncounted(e.Message) is { } u) return u;
                // Without the backend there is no slot for this browser.
                throw new LicenseError($"Could not reach the license server to start this browser: {e.Message}");
            }
            return StartBrowser(d, launchId);
        }

        private LeaseSession StartBrowser(CheckoutData d, string launchId)
        {
            OnBrowserToken(d.Token, d.Exp);
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
                        using var res = await PostAsync("/api/v1/lease/heartbeat",
                            new { lease_id = _leaseId, nonce = Guid.NewGuid().ToString() }).ConfigureAwait(false);
                        if ((int)res.StatusCode == 409) // reclaimed/expired -> re-checkout to keep the slot
                        {
                            using var co = await PostCheckoutAsync(null).ConfigureAwait(false);
                            if (co.IsSuccessStatusCode)
                            {
                                using var d = JsonDocument.Parse(await co.Content.ReadAsStringAsync().ConfigureAwait(false));
                                _leaseId = d.RootElement.GetProperty("lease_id").GetString()!;
                                SetToken(d.RootElement.GetProperty("token").GetString()!);
                                _exp = d.RootElement.GetProperty("exp").GetInt64();
                                WriteCache(_key, _token!, _exp, _leaseId, _machineConfirmed);
                            }
                            continue;
                        }
                        if (res.IsSuccessStatusCode)
                        {
                            using var d = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                            SetToken(d.RootElement.GetProperty("token").GetString()!);
                            _exp = d.RootElement.GetProperty("exp").GetInt64();
                            WriteCache(_key, _token!, _exp, _leaseId, _machineConfirmed);
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
            _uncountedFiles.CloseAll();
            // Per-browser leases still open at exit (a browser nobody closed): release their slots too.
            foreach (var b in _browsers.Keys.ToArray())
                try { await b.StopAsync().ConfigureAwait(false); } catch { /* best-effort */ }
            _hbCts?.Cancel();
            if (_owner && _leaseId != null)
            {
                try
                {
                    using var _ = await PostAsync("/api/v1/lease/checkin", new { lease_id = _leaseId }).ConfigureAwait(false);
                }
                catch { /* best-effort; the lease TTL reclaims it anyway */ }
            }
        }
    }

    internal sealed record CheckoutData(string Token, long Exp, string LeaseId, int HeartbeatSec, bool BrowserScope);

    // One browser's own lease on a per-browser plan (the free tier: "1 browser at a time", and a paid licence
    // without a cap, which counts every browser). Checked out for exactly one launch, heartbeated while that
    // browser runs, checked in when it closes. Its token is never written to the shared cache and never handed
    // to another launch; a paid one is kept as the grace token (grace-<hash>.json).
    private sealed class BrowserLease
    {
        private readonly MachineLease _owner;
        private readonly string _launchId;
        private readonly CancellationTokenSource _cts = new();
        private readonly TokenFileSet _tokenFiles = new();
        private volatile string _token;
        private int _stopped;
        public LeaseSession Session { get; }

        // Set this browser's rotating token AND push it into every bound run-token file (and a paid one into
        // the grace file).
        private void SetToken(string v, long? exp)
        {
            _token = v;
            _tokenFiles.Update(v);
            _owner.OnBrowserToken(v, exp);
        }

        private static long? Exp(JsonElement answer) =>
            answer.TryGetProperty("exp", out var e) && e.TryGetInt64(out var v) ? v : null;

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
                            SetToken(again.Token, again.Exp);
                            continue;
                        }
                        if (res.IsSuccessStatusCode)
                        {
                            using var d2 = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
                            if (d2.RootElement.TryGetProperty("token", out var t) && t.ValueKind == JsonValueKind.String)
                                SetToken(t.GetString()!, Exp(d2.RootElement));
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
                SetToken(t.GetString()!, Exp(d.RootElement));
                return true;
            }
            if ((int)res.StatusCode != 409) return false;
            var again = await _owner.CheckoutForAsync(_launchId).ConfigureAwait(false); // a refusal throws
            Session.LeaseId = again.LeaseId;
            SetToken(again.Token, again.Exp);
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
    /// Machine plans (a paid plan with a cap): a per-MACHINE lease shared across every launch in this process
    /// (checks out ~once per token-TTL, not once per launch); StopAsync only drops a reference.
    /// Per-browser plans (the GitHub free tier, and a paid plan without a cap, told by the backend's
    /// lease_scope "browser"): every launch checks out its own slot and StopAsync releases it, so the
    /// licence server counts every browser. Throws
    /// <see cref="ConcurrencyLimitError"/> / <see cref="LicenseRevokedError"/> / <see cref="LicenseError"/>
    /// only on a checkout the backend definitively refuses; falls back to a cached, still-valid
    /// token on a transient network failure (offline grace). A paid per-browser launch that gets no verdict
    /// (unreachable, a 5xx, a challenge, rate limited) starts on the grace token, uncounted, and the next
    /// checkout reports it; the free tier has no offline grace.
    ///
    /// <paramref name="sdkVersion"/> is the SDK PACKAGE version (checkout telemetry sdk_version).
    /// <paramref name="engineVersion"/> lazily resolves the browser build (engine_version) — invoked
    /// on a cold checkout only, so the catalog is never consulted per launch.
    public static async Task<LeaseSession?> AcquireLeaseAsync(
        LicenseOptions opts, string? sdkVersion = null, bool quiet = false,
        Func<Task<string?>>? engineVersion = null, ProxyOptions? proxy = null)
    {
        // Offline mode: the key file's token is the run token; nothing is checked out (Offline.cs).
        var off = Offline.Lease(opts.OfflineKey, quiet);
        if (off is not null) return off;
        var licenseKey = ResolveLicenseKey(opts.LicenseKey);
        if (licenseKey is null) return null; // free mode — inert

        var route = LeaseRouteFor(opts);
        var wantProxy = LicenseThroughProxyRequested(opts.LicenseThroughProxy);
        var viaProxy = wantProxy ? ProxySpec.From(proxy) : null;
        if (wantProxy && viaProxy is null && !quiet)
            Console.Error.WriteLine("[clearcote] [license] licenseThroughProxy is on but this launch has no proxy; licence calls go direct.");
        // One lease per (key, route): a direct lease and a proxied lease are different network paths,
        // so they must not share the in-process heartbeat owner. The proxy username is part of the
        // route: rotating-session proxies encode the exit in it.
        var mapKey = viaProxy is null ? licenseKey : $"{licenseKey}|{viaProxy.ServerString}|{viaProxy.Username}";
        var ml = _machineLeases.GetOrAdd(mapKey,
            _ => new MachineLease(licenseKey, route, ResolveInstanceId(), sdkVersion, engineVersion, quiet, viaProxy));
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
            using var res = await SendLeaseAsync(LeaseRouteFor(opts), async b =>
            {
                using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(15));
                using var req = new HttpRequestMessage(HttpMethod.Get, $"{b}/api/v1/lease/seats");
                req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", key);
                req.Headers.UserAgent.ParseAdd(LicenseUserAgent);
                return await client.SendAsync(req, cts.Token).ConfigureAwait(false); // the body is read in full here
            }, via).ConfigureAwait(false);
            var status = (int)res.StatusCode;
            int? used = null, limit = null;
            string? plan = null, error = null;
            var hasUsed = false;
            var text = await res.Content.ReadAsStringAsync().ConfigureAwait(false);
            if (ChallengeReason(res, text) is { } why)
                return new SessionSeats(SeatsState.Unavailable, Reason: $"licence server behind a security challenge ({why})");
            try
            {
                using var doc = JsonDocument.Parse(text);
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
        if (!string.IsNullOrEmpty(tokenFile)) outEnv[RunTokenFileEnv] = tokenFile;  // offline leases have no file
        return outEnv;
    }
}

/// Where the licence key a launch would use comes from (<see cref="License.LicenseKeySource"/>): "option", "env",
/// "file" or "none", and the key masked (null for "none").
public sealed record LicenseKeySourceInfo(string Source, string? Masked);

/// What is known about a licence key's expiry (<see cref="License.LicenseExpiry"/>). <see cref="Source"/>
/// "device-login": <see cref="ExpiresAt"/> as the site reported it at <c>clearcote login --device</c> (null = no
/// expiry), recorded at <see cref="RecordedAt"/>; "unknown": nothing recorded for this key.
public sealed record LicenseExpiryInfo(string Source, string? ExpiresAt, string? RecordedAt, string Note);
