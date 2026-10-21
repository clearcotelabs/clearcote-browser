using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Playwright;

namespace Clearcote;

/// What one launch does about the CRLSet (see <see cref="Crlset"/>).
public enum CrlsetMode
{
    /// Nothing (the default).
    Off,
    /// Seed it and hold the launch until the engine has it live.
    Wait,
    /// Seed it without waiting: live once the first tab has been idle for a moment.
    NoWait,
}

/// <summary>
/// Chrome's revocation list (CRLSet) for an engine that never receives one. The .NET twin of the Python
/// <c>_crlset.py</c>: same rules, environment variables, cache layout and warnings.
/// </summary>
/// <remarks>
/// <para>Real Chrome downloads a CRLSet through its component updater: Google's list of revoked and blocked
/// certificate keys, which is how a CA distrust reaches the browser within hours (the eMudhra key compromise of
/// 2026-10-17 shipped as CRLSet 10849). A Clearcote engine never gets it: the updater URL is domain-substituted to a
/// dead host, and Playwright launches with <c>--disable-component-update</c> anyway. So every engine since 149 kept
/// trusting those intermediates.</para>
/// <para>The engine still knows how to use one. A launch with the CRLSet on (1) SEEDS it into the profile
/// (&lt;userDataDir&gt;/CertificateRevocation/&lt;sequence&gt;/crl-set plus a manifest; Chrome only checks that the
/// file exists, and an expired one still blocks everything it lists), (2) UN-SUPPRESSES the component updater
/// (without it the seeded file is never read; the updater makes no network request on this engine), and (3) WAITS
/// until the CRLSet is live before the caller gets the browser: Chrome loads it with tasks that only run once the
/// first tab has been idle for a moment, so a caller who navigates straight away would stay unprotected for
/// minutes. The first tab stays on about:blank until it applies.</para>
/// <para>Readiness signal: applying a CRLSet changes the cert verifier, and the network service then closes its
/// idle pooled connections (all of them, plain HTTP included). The probe keeps one keep-alive HTTP connection to a
/// local server on 127.0.0.1 and waits for that connection to be dropped and re-opened. Not every drop is the
/// CRLSet (a network change at launch, seen on Linux, drops them too), and the CRLSet is read only once startup
/// completes, so only a drop after the <see cref="StartupHistogram"/> histogram appears counts (see
/// ReadinessState). It adds about 2 s to a launch.</para>
/// <para>OPT-IN: <see cref="LaunchOptions.Crlset"/> = true (or CLEARCOTE_CRLSET=1). Where the CRLSet comes from,
/// first match wins: <see cref="LaunchOptions.CrlsetPath"/> (a crl-set file or a component directory),
/// CLEARCOTE_CRLSET_FILE, then the newest copy in the cache (CLEARCOTE_CRLSET_DIR, else ~/.clearcote/crlset). An
/// empty cache is filled at launch from Google's component updater, the same Omaha service the Widevine fetch uses
/// (sha256 from the HTTPS update response), waiting at most CLEARCOTE_CRLSET_FETCH_TIMEOUT (default 15 s); a cache
/// older than 6 h is refreshed in the background while the launch uses it. CLEARCOTE_CRLSET_URL points the fetch at
/// a mirror of the raw crl-set file instead. The cache layout (&lt;sequence&gt;/crl-set) is shared by the Python,
/// Node and .NET SDKs. Never fails a launch: a problem is said once on stderr (unless Quiet or CLEARCOTE_NO_WARN)
/// and the launch goes on.</para>
/// </remarks>
public static class Crlset
{
    /// The profile directory Chrome's CRLSet component lives in.
    public const string ComponentDir = "CertificateRevocation";
    /// The CRLSet file name, in the profile and in the cache.
    public const string FileName = "crl-set";
    /// Chrome's component-updater app id for the CRLSet.
    public const string AppId = "hfnkpimlhhgieaddgfemjhofmfblmnib";
    /// 1/true/on/yes: on (wait); nowait: seed without waiting; anything else: off. An explicit option wins.
    public const string CrlsetEnv = "CLEARCOTE_CRLSET";
    /// The readiness wait in seconds (default 15); <see cref="LaunchOptions.CrlsetTimeout"/> wins.
    public const string TimeoutEnv = "CLEARCOTE_CRLSET_TIMEOUT";
    /// How long a launch with an empty cache waits for the first download, in seconds (default 15).
    public const string FetchTimeoutEnv = "CLEARCOTE_CRLSET_FETCH_TIMEOUT";
    public const double DefaultTimeoutSeconds = 15;
    public const double FetchTimeoutSeconds = 15;
    /// In the cache root: its mtime is the last fetch attempt (a failed one counts).
    public const string StampFile = ".last-check";
    /// A cache whose last fetch attempt is older than this is refreshed in the background.
    public static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(6);
    internal const string ProbeJs = "u => fetch(u, {cache: 'no-store'}).then(r => r.status, e => 0)";
    /// Recorded once, when browser startup completes (the CRLSet loader runs only after it).
    public const string StartupHistogram = "Startup.AfterStartupTaskCount";
    /// Startup was complete before the wait began: the CRLSet's drop may be past, so live after this grace.
    public const double AlreadyStartedGraceSeconds = 1.5;

    /// A monotonic clock in seconds (Python's time.monotonic()).
    internal static double Now() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;

    private static readonly string[] Truthy = { "1", "true", "on", "yes" };

    // ── warnings ───────────────────────────────────────────────────────────────

    private static readonly ConcurrentDictionary<string, byte> Warned = new(StringComparer.Ordinal);

    /// Tests: forget which warnings were printed.
    internal static void ForgetWarnings() => Warned.Clear();

    internal static void WarnOnce(string key, string message, bool quiet)
    {
        if (quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN")) || !Warned.TryAdd(key, 0)) return;
        Console.Error.WriteLine("clearcote: crlset: " + message);
    }

    private static string Describe(Exception e)
    {
        var inner = e is AggregateException { InnerException: { } i } ? i : e;
        var text = (inner.Message ?? "").Trim().Split('\n')[0];
        if (text.Length > 160) text = text[..160];
        return $"{inner.GetType().Name}: {text}";
    }

    // ── the rules ──────────────────────────────────────────────────────────────

    /// <summary>The mode of one launch and the CRLSet path it names, if any.</summary>
    /// <remarks><paramref name="option"/> (<see cref="LaunchOptions.Crlset"/>) false -> Off, whatever else is set.
    /// A <paramref name="path"/> (<see cref="LaunchOptions.CrlsetPath"/>) or option true -> Wait (NoWait with
    /// <paramref name="noWait"/> true). <paramref name="noWait"/> true alone -> NoWait. Nothing set follows
    /// CLEARCOTE_CRLSET (1/true/on/yes -> Wait, nowait -> NoWait, anything else -> Off).</remarks>
    public static (CrlsetMode Mode, string? Path) Mode(bool? option, string? path = null, bool? noWait = null, Func<string, string?>? env = null)
    {
        if (option == false) return (CrlsetMode.Off, null);
        var p = string.IsNullOrEmpty(path) ? null : path;
        if (option == true || p is not null || noWait == true)
            return (noWait == true ? CrlsetMode.NoWait : CrlsetMode.Wait, p);
        var value = ((env ?? Environment.GetEnvironmentVariable)(CrlsetEnv) ?? "").Trim().ToLowerInvariant();
        if (value == "nowait") return (CrlsetMode.NoWait, null);
        return (Truthy.Contains(value) ? CrlsetMode.Wait : CrlsetMode.Off, null);
    }

    /// The readiness wait: <paramref name="option"/>, else CLEARCOTE_CRLSET_TIMEOUT, else 15 s.
    public static double TimeoutSeconds(double? option = null, Func<string, string?>? env = null)
    {
        if (option is double o && o >= 0) return o;
        return EnvSeconds(TimeoutEnv, DefaultTimeoutSeconds, env);
    }

    private static double EnvSeconds(string name, double fallback, Func<string, string?>? env = null)
    {
        var raw = ((env ?? Environment.GetEnvironmentVariable)(name) ?? "").Trim();
        return double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) && v >= 0 && !double.IsInfinity(v)
            ? v : fallback;
    }

    // ── the file ───────────────────────────────────────────────────────────────

    /// The cache root: CLEARCOTE_CRLSET_DIR, else ~/.clearcote/crlset (shared with the Python and Node SDKs).
    public static string CacheRoot() =>
        Environment.GetEnvironmentVariable("CLEARCOTE_CRLSET_DIR") is { Length: > 0 } d ? d : Path.Combine(Native.ClearcoteDir, "crlset");

    /// <summary>The JSON header of a CRLSet file (a little-endian uint16 length, then JSON).</summary>
    /// <exception cref="InvalidDataException">Not a CRLSet: too short, bad JSON, ContentType is not "CRLSet", or no
    /// integer Sequence.</exception>
    public static JsonObject ParseHeader(byte[] data)
    {
        ArgumentNullException.ThrowIfNull(data);
        if (data.Length < 2) throw new InvalidDataException("not a CRLSet: too short");
        int length = data[0] | (data[1] << 8);
        JsonNode? header;
        try { header = JsonNode.Parse(Encoding.UTF8.GetString(data, 2, Math.Min(length, data.Length - 2))); }
        catch (Exception e) when (e is JsonException or ArgumentException)
        {
            throw new InvalidDataException($"not a CRLSet: bad header ({e.Message})");
        }
        if (header is not JsonObject obj || obj["ContentType"] is not JsonValue ct || !ct.TryGetValue<string>(out var s) || s != "CRLSet")
            throw new InvalidDataException("not a CRLSet: ContentType is not CRLSet");
        if (SequenceOf(obj) is null) throw new InvalidDataException("not a CRLSet: no Sequence");
        return obj;
    }

    private static long? SequenceOf(JsonObject header) =>
        header["Sequence"] is JsonValue v && v.GetValueKind() == JsonValueKind.Number && v.TryGetValue<long>(out var n) ? n : null;

    /// The Sequence of a CRLSet file's contents (see <see cref="ParseHeader"/>).
    public static long Sequence(byte[] data) => SequenceOf(ParseHeader(data))!.Value;

    /// A crl-set file path from a file or a component directory (&lt;dir&gt;/crl-set or the newest &lt;dir&gt;/&lt;ver&gt;/crl-set).
    internal static string AsCrlsetFile(string path)
    {
        if (File.Exists(path)) return path;
        var direct = Path.Combine(path, FileName);
        if (File.Exists(direct)) return direct;
        var versions = new List<(long Seq, string Path)>();
        if (Directory.Exists(path))
            foreach (var dir in Directory.EnumerateDirectories(path))
            {
                if (Path.GetFileName(dir).StartsWith('.')) continue;   // a download in progress
                var candidate = Path.Combine(dir, FileName);
                if (!File.Exists(candidate)) continue;
                try { versions.Add((Sequence(File.ReadAllBytes(candidate)), candidate)); }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidDataException) { }
            }
        if (versions.Count == 0) throw new InvalidDataException($"no crl-set file under {path}");
        return versions.OrderByDescending(v => v.Seq).ThenByDescending(v => v.Path, StringComparer.Ordinal).First().Path;
    }

    /// <summary>Copy a CRLSet (a crl-set file, or Chrome's <c>CertificateRevocation</c> directory) into the cache as
    /// &lt;cache&gt;/&lt;sequence&gt;/crl-set. Returns that path.</summary>
    public static string Install(string source)
    {
        var data = File.ReadAllBytes(AsCrlsetFile(source));
        var sequence = Sequence(data);
        var destDir = Path.Combine(CacheRoot(), sequence.ToString(CultureInfo.InvariantCulture));
        Directory.CreateDirectory(destDir);
        var dest = Path.Combine(destDir, FileName);
        var tmp = dest + "." + Convert.ToHexString(RandomNumberGenerator.GetBytes(4)).ToLowerInvariant() + ".tmp";
        File.WriteAllBytes(tmp, data);
        File.Move(tmp, dest, overwrite: true);
        return dest;
    }

    /// The crl-set file to seed, or null: <paramref name="explicitPath"/> &gt; CLEARCOTE_CRLSET_FILE &gt; the newest cached.
    /// An explicit path or CLEARCOTE_CRLSET_FILE that holds no CRLSet throws (<see cref="InvalidDataException"/>).
    public static string? Find(string? explicitPath = null)
    {
        foreach (var path in new[] { explicitPath, Environment.GetEnvironmentVariable("CLEARCOTE_CRLSET_FILE") })
            if (!string.IsNullOrEmpty(path)) return AsCrlsetFile(path);
        var root = CacheRoot();
        if (!Directory.Exists(root)) return null;
        try { return AsCrlsetFile(root); }
        catch (InvalidDataException) { return null; }
    }

    // ── the fetch ──────────────────────────────────────────────────────────────

    private static void TouchStamp()
    {
        var root = CacheRoot();
        Directory.CreateDirectory(root);
        File.WriteAllText(Path.Combine(root, StampFile), "");
        File.SetLastWriteTimeUtc(Path.Combine(root, StampFile), DateTime.UtcNow);
    }

    internal static bool StampFresh()
    {
        try
        {
            var path = Path.Combine(CacheRoot(), StampFile);
            return File.Exists(path) && DateTime.UtcNow - File.GetLastWriteTimeUtc(path) < RefreshInterval;
        }
        catch { return false; }
    }

    private static async Task<byte[]> GetBytesAsync(string url, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromSeconds(60));
        using var client = Widevine.Client();
        using var res = await client.GetAsync(url, cts.Token).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode) throw new HttpRequestException($"CRLSet download HTTP {(int)res.StatusCode}");
        return await res.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
    }

    /// The current CRLSet from Google's component updater, verified against the sha256 of the HTTPS update
    /// response (the package itself may come over plain HTTP from edgedl).
    internal static async Task<byte[]> DownloadFromGoogleAsync(CancellationToken ct)
    {
        string json;
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
        {
            cts.CancelAfter(TimeSpan.FromSeconds(60));
            using var client = Widevine.Client();
            using var req = new HttpRequestMessage(HttpMethod.Post, Widevine.OmahaUrl)
            {
                Content = new StringContent(Widevine.OmahaRequestBody(AppId), Encoding.UTF8, "application/json"),
            };
            using var res = await client.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode) throw new HttpRequestException($"CRLSet update check HTTP {(int)res.StatusCode}");
            json = await res.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
        }
        var (url, sha256, _) = Widevine.ParseUpdate(json);
        var blob = await GetBytesAsync(url, ct).ConfigureAwait(false);
        if (string.IsNullOrEmpty(sha256) || !string.Equals(Convert.ToHexString(SHA256.HashData(blob)), sha256, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("CRLSet sha256 mismatch - refusing to install");
        using var zip = new ZipArchive(new MemoryStream(Widevine.Crx3ToZip(blob)), ZipArchiveMode.Read);
        var entry = zip.GetEntry(FileName) ?? throw new InvalidDataException("the CRLSet package has no crl-set file");
        using var stream = entry.Open();
        using var buf = new MemoryStream();
        await stream.CopyToAsync(buf, ct).ConfigureAwait(false);
        return buf.ToArray();
    }

    /// <summary>Download the current CRLSet into the cache and return its path: from Google's component updater, or
    /// from <paramref name="url"/> / CLEARCOTE_CRLSET_URL (a mirror serving the raw crl-set file).</summary>
    /// <remarks>The attempt is recorded in the cache's stamp first, success or not, so a failing network is not asked
    /// again for <see cref="RefreshInterval"/>.</remarks>
    public static async Task<string> FetchAsync(string? url = null, CancellationToken cancellationToken = default)
    {
        url = string.IsNullOrEmpty(url) ? Environment.GetEnvironmentVariable("CLEARCOTE_CRLSET_URL") : url;
        TouchStamp();   // a failed attempt counts too: no retry storm against Google
        var data = !string.IsNullOrEmpty(url)
            ? await GetBytesAsync(url, cancellationToken).ConfigureAwait(false)
            : await DownloadFromGoogleAsync(cancellationToken).ConfigureAwait(false);
        ParseHeader(data);
        var tmpDir = Path.Combine(CacheRoot(), ".download-" + Environment.ProcessId.ToString(CultureInfo.InvariantCulture) + "-"
            + Convert.ToHexString(RandomNumberGenerator.GetBytes(4)).ToLowerInvariant());
        Directory.CreateDirectory(tmpDir);
        try
        {
            var tmp = Path.Combine(tmpDir, FileName);
            await File.WriteAllBytesAsync(tmp, data, cancellationToken).ConfigureAwait(false);
            return Install(tmp);
        }
        finally
        {
            try { Directory.Delete(tmpDir, recursive: true); } catch { /* best-effort */ }
        }
    }

    private static readonly object RefreshGate = new();
    private static Task? _refresh;

    /// Tests: stands in for the background refresh.
    internal static Action? RefreshOverride;

    /// Tests: the background refresh in flight, if any.
    internal static Task? RefreshInFlight() { lock (RefreshGate) return _refresh; }

    /// At most one refresh per process at a time; the launch goes on with the cached copy.
    private static void RefreshInBackground()
    {
        if (RefreshOverride is { } fake) { fake(); return; }
        lock (RefreshGate)
        {
            if (_refresh is { IsCompleted: false }) return;
            _refresh = Task.Run(async () =>
            {
                try { await FetchAsync().ConfigureAwait(false); }
                catch { /* the cached copy still serves; the next stale launch retries */ }
            });
        }
    }

    /// An empty cache: fetch now, waiting at most CLEARCOTE_CRLSET_FETCH_TIMEOUT. The path, or null (warned once).
    private static async Task<string?> FetchFirstAsync(bool quiet)
    {
        var wait = EnvSeconds(FetchTimeoutEnv, FetchTimeoutSeconds);
        var job = Task.Run(() => FetchAsync());
        var finished = await Task.WhenAny(job, Task.Delay(TimeSpan.FromSeconds(wait))).ConfigureAwait(false);
        if (finished != job)
        {
            _ = job.ContinueWith(t => _ = t.Exception, TaskScheduler.Default);   // observed: it goes on for the next launch
            WarnOnce("fetch", $"no CRLSet cached and the first download took over {wait.ToString(CultureInfo.InvariantCulture)} s; launching " +
                              "without one (it is still being fetched for the next launch)", quiet);
            return null;
        }
        try { return await job.ConfigureAwait(false); }
        catch (Exception e)
        {
            WarnOnce("fetch", $"could not fetch a CRLSet ({Describe(e)}); launching without one", quiet);
            return null;
        }
    }

    /// <see cref="Find"/>, filling an empty cache from Google first and refreshing a stale one in the background.
    /// Null when there is none to be had (warned once).
    internal static async Task<string?> ResolveAsync(string? explicitPath, bool quiet)
    {
        var found = Find(explicitPath);
        if (!string.IsNullOrEmpty(explicitPath) || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_CRLSET_FILE")))
            return found;
        if (found is null) return await FetchFirstAsync(quiet).ConfigureAwait(false);
        if (!StampFresh()) RefreshInBackground();
        return found;
    }

    // ── the profile ────────────────────────────────────────────────────────────

    /// <summary>Place the CRLSet in a profile where the component installer finds it
    /// (&lt;userDataDir&gt;/CertificateRevocation/&lt;sequence&gt;/crl-set + manifest.json). Keeps a newer one that is
    /// already there (a reused profile). Returns the sequence now in the profile.</summary>
    public static long Seed(string userDataDir, string crlsetFile)
    {
        var data = File.ReadAllBytes(crlsetFile);
        var sequence = Sequence(data);
        var baseDir = Path.Combine(userDataDir, ComponentDir);
        long? present = null;
        if (Directory.Exists(baseDir))
            foreach (var dir in Directory.EnumerateDirectories(baseDir))
            {
                var name = Path.GetFileName(dir);
                if (name.Length > 0 && name.All(char.IsAsciiDigit) && long.TryParse(name, NumberStyles.None, CultureInfo.InvariantCulture, out var n)
                    && File.Exists(Path.Combine(dir, FileName)))
                    present = Math.Max(present ?? n, n);
            }
        if (present is long p && p >= sequence) return p;
        var seq = sequence.ToString(CultureInfo.InvariantCulture);
        var versionDir = Path.Combine(baseDir, seq);
        Directory.CreateDirectory(versionDir);
        File.WriteAllBytes(Path.Combine(versionDir, FileName), data);
        File.WriteAllText(Path.Combine(versionDir, "manifest.json"),
            "{\"manifest_version\": 2, \"name\": \"crl-set-clearcote.data\", \"version\": \"" + seq + "\"}");
        return sequence;
    }

    /// Playwright's <c>--disable-component-update</c> no longer passed (as a requested Widevine launch does): the
    /// caller's IgnoreDefaultArgs, else the SDK's strip, plus that flag.
    internal static List<string> UnsuppressComponentUpdater(IReadOnlyList<string>? ignoreDefaultArgs)
    {
        var ignore = (ignoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs).ToList();
        if (!ignore.Contains("--disable-component-update")) ignore.Add("--disable-component-update");
        return ignore;
    }

    /// <summary>Seed the profile and un-suppress the updater for one persistent launch.</summary>
    /// <remarks>Returns the readiness wait when the launch must wait (else null) and the IgnoreDefaultArgs to launch
    /// with. Never throws: revocation data is best-effort and must not stop a launch.</remarks>
    internal static async Task<(Readiness? Plan, IReadOnlyList<string> IgnoreDefaultArgs)> ApplyLaunchAsync(string userDataDir,
        LaunchOptions o, IReadOnlyList<string> ignoreDefaultArgs)
    {
        try
        {
            var (mode, explicitPath) = Mode(o.Crlset, o.CrlsetPath, o.CrlsetNoWait);
            if (mode == CrlsetMode.Off) return (null, ignoreDefaultArgs);
            string? file;
            try { file = await ResolveAsync(explicitPath, o.Quiet).ConfigureAwait(false); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidDataException or ArgumentException)
            {
                WarnOnce("find", $"no usable CRLSet ({e.Message}); launching without one", o.Quiet);
                return (null, ignoreDefaultArgs);
            }
            if (file is null) return (null, ignoreDefaultArgs);   // ResolveAsync warned
            var sequence = Seed(userDataDir, file);
            var ignore = UnsuppressComponentUpdater(ignoreDefaultArgs);
            if (mode == CrlsetMode.NoWait) return (null, ignore);
            return (new Readiness(TimeoutSeconds(o.CrlsetTimeout), sequence, o.Quiet), ignore);
        }
        catch (Exception e)
        {
            WarnOnce("seed", $"seeding failed ({Describe(e)}); launching without one", o.Quiet);
            return (null, ignoreDefaultArgs);
        }
    }

    /// A launch without a profile to seed (<paramref name="where"/>: LaunchAsync, ServeAsync): say so when the caller
    /// asked for the CRLSet with an option (not the environment, as the Python SDK), and launch without one.
    internal static void WarnNoProfile(LaunchOptions o, string where)
    {
        if (o.Crlset != true && string.IsNullOrEmpty(o.CrlsetPath) && o.CrlsetNoWait != true) return;
        if (o.Quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN"))) return;
        Console.Error.WriteLine($"clearcote: crlset: {where} has no profile to seed a CRLSet into; use LaunchEphemeralProfileAsync or " +
                                "LaunchPersistentContextAsync. Launching without one");
    }

    /// Run the launch; stop the readiness probe's server if it throws.
    internal static async Task<T> CloseOnFailureAsync<T>(Readiness? plan, Func<Task<T>> launch)
    {
        try { return await launch().ConfigureAwait(false); }
        catch
        {
            plan?.Close();
            throw;
        }
    }

    /// <summary>Launch args that send <paramref name="rule"/> around a Playwright-handled proxy, else none.</summary>
    /// <remarks>Playwright adds <c>&lt;-loopback&gt;</c> after the caller's bypass rules, and Chrome evaluates bypass
    /// rules last-first, so a rule added to the proxy's Bypass loses and the local probe went to the user's proxy as
    /// <c>CONNECT 127.0.0.1:&lt;port&gt;</c> (seen live). Chrome keeps the last of a repeated switch and Playwright puts
    /// the caller's args after its own, so this repeats Playwright's list with <paramref name="rule"/> LAST: only that
    /// host:port goes direct; other loopback traffic still goes through the proxy. A proxy the SDK hands to the engine
    /// itself (credentials, see LaunchOpts.ResolveProxy) has no <c>&lt;-loopback&gt;</c> and needs nothing.</remarks>
    internal static List<string> ProxyBypassArgs(ProxyOptions? playwrightProxy, string rule)
    {
        if (playwrightProxy is null || string.IsNullOrEmpty(playwrightProxy.Server)) return new List<string>();
        var rules = (playwrightProxy.Bypass ?? "").Split(',').Select(r => r.Trim()).Where(r => r.Length > 0)
            .Select(r => r.StartsWith('.') ? "*" + r : r)   // as Playwright does
            .ToList();
        if (string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"))
            && !rules.Contains("<-loopback>"))
            rules.Add("<-loopback>");
        rules.Add(rule);
        return new List<string> { "--proxy-bypass-list=" + string.Join(';', rules) };
    }

    // ── readiness ──────────────────────────────────────────────────────────────

    /// Plain HTTP on 127.0.0.1 that counts the connections the browser opens to it. Every request is answered
    /// 204 (CORS open, empty body) on a keep-alive connection, so the browser pools it.
    internal sealed class ProbeServer : IDisposable, IProbeConnections
    {
        private static readonly byte[] Response = Encoding.ASCII.GetBytes(
            "HTTP/1.1 204 No Content\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 0\r\n\r\n");
        private readonly TcpListener _listener;
        private readonly CancellationTokenSource _stop = new();
        private readonly List<double> _opened = new();   // Now() of each connection

        public ProbeServer()
        {
            _listener = new TcpListener(IPAddress.Loopback, 0);
            _listener.Start();
            Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
            _ = Task.Run(AcceptLoopAsync);
        }

        public int Port { get; }
        public string HostPort => "127.0.0.1:" + Port.ToString(CultureInfo.InvariantCulture);
        public string Url => "http://" + HostPort + "/";
        public int Count { get { lock (_opened) return _opened.Count; } }

        /// A connection other than the first was opened after <paramref name="moment"/>: the pooled one was dropped.
        public bool ReopenedAfter(double moment) { lock (_opened) return _opened.Skip(1).Any(t => t > moment); }

        private async Task AcceptLoopAsync()
        {
            while (!_stop.IsCancellationRequested)
            {
                TcpClient client;
                try { client = await _listener.AcceptTcpClientAsync(_stop.Token).ConfigureAwait(false); }
                catch { return; }   // stopped
                _ = Task.Run(() => ServeAsync(client));
            }
        }

        private async Task ServeAsync(TcpClient client)
        {
            using (client)
            {
                try
                {
                    lock (_opened) _opened.Add(Now());
                    var stream = client.GetStream();
                    var buf = new byte[8192];
                    var pending = new List<byte>();
                    while (!_stop.IsCancellationRequested)
                    {
                        var n = await stream.ReadAsync(buf, _stop.Token).ConfigureAwait(false);
                        if (n == 0) return;
                        pending.AddRange(buf.AsSpan(0, n).ToArray());
                        while (TakeRequest(pending))
                        {
                            await stream.WriteAsync(Response, _stop.Token).ConfigureAwait(false);
                            await stream.FlushAsync(_stop.Token).ConfigureAwait(false);
                        }
                        if (pending.Count > 64 * 1024) return;   // not a request we answer
                    }
                }
                catch { /* the browser dropped the connection, or the server stopped */ }
            }
        }

        /// Remove one complete request (headers + a Content-Length body) from <paramref name="pending"/>.
        private static bool TakeRequest(List<byte> pending)
        {
            var span = pending.ToArray().AsSpan();
            var end = span.IndexOf("\r\n\r\n"u8);
            if (end < 0) return false;
            var head = Encoding.ASCII.GetString(span[..end]);
            var body = 0;
            foreach (var line in head.Split("\r\n"))
                if (line.StartsWith("content-length:", StringComparison.OrdinalIgnoreCase)
                    && int.TryParse(line["content-length:".Length..].Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out var len))
                    body = len;
            var total = end + 4 + body;
            if (span.Length < total) return false;
            pending.RemoveRange(0, total);
            return true;
        }

        public void Dispose()
        {
            if (_stop.IsCancellationRequested) return;
            _stop.Cancel();
            try { _listener.Stop(); } catch { }
        }
    }

    /// What <see cref="ReadinessState"/> reads from the probe server.
    internal interface IProbeConnections
    {
        int Count { get; }
        bool ReopenedAfter(double moment);
    }

    /// <summary>When the CRLSet is live, from the probe's connections and the startup histogram.</summary>
    /// <remarks>Not every dropped connection is the CRLSet: a network change (seen on Linux at launch) drops them too.
    /// The CRLSet is read only after startup completes (its loader is a BEST_EFFORT task), so only a drop seen after
    /// the histogram appeared counts. If startup was already complete at the first check, the drop may have happened
    /// before the probe connected: a short grace, then it counts as live. Without the histogram (CDP refused it) the
    /// first drop counts, as before.</remarks>
    internal sealed class ReadinessState
    {
        public ReadinessState(IProbeConnections server) => Server = server;

        public IProbeConnections Server { get; }
        /// <see cref="Now"/> when the histogram was first seen.
        public double? StartedAt { get; private set; }
        public bool AlreadyStarted { get; private set; }
        /// False once the browser refused Browser.getHistogram.
        public bool Histogram { get; set; } = true;

        public void NoteStartup(bool present, bool firstCheck, double now)
        {
            if (StartedAt is null && present)
            {
                StartedAt = now;
                AlreadyStarted = firstCheck;
            }
        }

        public bool Ready(double now)
        {
            if (!Histogram) return Server.Count >= 2;
            if (StartedAt is not double started) return false;
            if (Server.ReopenedAfter(started)) return true;
            return AlreadyStarted && now - started >= AlreadyStartedGraceSeconds;
        }
    }

    /// <summary>The wait for a seeded CRLSet: <see cref="BeforeLaunch"/> (the probe server, so its port can bypass
    /// the proxy), then <see cref="WaitAsync"/> once the context is up, or <see cref="Close"/> if the launch failed.</summary>
    internal sealed class Readiness
    {
        public Readiness(double timeoutSeconds, long sequence, bool quiet)
        {
            TimeoutSeconds = timeoutSeconds;
            Sequence = sequence;
            Quiet = quiet;
        }

        public double TimeoutSeconds { get; }
        public long Sequence { get; }
        public bool Quiet { get; }
        internal ProbeServer? Server { get; private set; }

        /// Start the probe server; returns the launch args to append (the proxy bypass for its port).
        public List<string> BeforeLaunch(ProxyOptions? playwrightProxy)
        {
            try
            {
                Server = new ProbeServer();
                return ProxyBypassArgs(playwrightProxy, Server.HostPort);
            }
            catch (Exception e)
            {
                WarnOnce("probe", $"readiness probe unavailable ({Describe(e)})", Quiet);
                Close();
                return new List<string>();
            }
        }

        /// Hold the first tab on about:blank until the CRLSet is live. True when confirmed; false on timeout or error
        /// (warned once). Never throws.
        public async Task<bool> WaitAsync(IBrowserContext context)
        {
            var server = Server;
            if (server is null) return false;
            var deadline = Now() + TimeoutSeconds;
            var state = new ReadinessState(server);
            ICDPSession? cdp = null;
            try
            {
                var page = context.Pages.Count > 0 ? context.Pages[0] : await context.NewPageAsync().ConfigureAwait(false);
                cdp = await context.NewCDPSessionAsync(page).ConfigureAwait(false);
                var first = true;
                while (Now() < deadline)
                {
                    if (state.Histogram && state.StartedAt is null)   // checked BEFORE the probe: see ReadinessState
                    {
                        try
                        {
                            await cdp.SendAsync("Browser.getHistogram", new Dictionary<string, object> { ["name"] = StartupHistogram }).ConfigureAwait(false);
                            state.NoteStartup(true, first, Now());
                        }
                        catch (Exception e)   // not recorded yet, or the method is refused
                        {
                            if (!(e.Message ?? "").Contains("cannot find histogram", StringComparison.OrdinalIgnoreCase)) state.Histogram = false;
                        }
                    }
                    first = false;
                    await page.EvaluateAsync<int>(ProbeJs, server.Url).ConfigureAwait(false);
                    if (state.Ready(Now())) return true;
                    await Task.Delay(50).ConfigureAwait(false);
                }
                WarnOnce("timeout", $"CRLSet not confirmed live within {TimeoutSeconds.ToString(CultureInfo.InvariantCulture)} s; the browser may " +
                                    "still trust revoked certificates until it loads", Quiet);
                return false;
            }
            catch (Exception e)
            {
                WarnOnce("wait", $"readiness check failed ({Describe(e)})", Quiet);
                return false;
            }
            finally
            {
                if (cdp is not null)
                {
                    try { await cdp.DetachAsync().ConfigureAwait(false); }
                    catch { /* the browser may be gone */ }
                }
                Close();
            }
        }

        public void Close()
        {
            Server?.Dispose();
            Server = null;
        }
    }
}
