using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Clearcote;

/// What one launch does about Widevine: nothing, what the caller asked for, or the licensed default.
public enum WidevineMode
{
    /// No Widevine (the free default, or turned off).
    Off,
    /// Asked for: <see cref="LaunchOptions.Widevine"/> = true or CLEARCOTE_WIDEVINE=1.
    Explicit,
    /// The licensed default: on, bounded and fail-soft.
    Default,
}

/// <summary>
/// Google's Widevine CDM: fetch, verify, cache and load it, so DRM/EME works
/// (<c>requestMediaKeySystemAccess('com.widevine.alpha')</c> resolves) and the EME surface matches a real
/// Chrome instead of being a no-Widevine tell. The .NET twin of the Python <c>_widevine.py</c> and the Node
/// <c>widevine.ts</c>: same rules, names, cache layout and warning.
/// </summary>
/// <remarks>
/// <para>clearcote is open source: it ships the EME/Widevine plumbing but not Google's proprietary CDM. This
/// downloads the CDM from Google's component server on the user's machine, as a real Chrome receives it.</para>
/// <para>WHEN IT IS ON (since 0.48.0). A licensed launch (a licence key resolved: the launch that selects the
/// PRO build, which claims the Google Chrome brand) on an engine with <c>--widevine-cdm-path</c> (r32+) loads the
/// CDM by default; a free launch keeps it opt-in. <see cref="LaunchOptions.Widevine"/> = false or
/// CLEARCOTE_WIDEVINE=0 turns it off, true or CLEARCOTE_WIDEVINE=1 on for any launch; an explicit option wins
/// over the environment (<see cref="Mode"/>). The default is FAIL-SOFT and never waits on the network when a CDM
/// is cached: a cached CDM is used at once (a stale one is refreshed in the background). Only the very first
/// launch, with nothing cached, waits for the download, at most CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS (default 15,
/// clamped to 1-600); when that fails the launch goes on without Widevine and says so once per process. The
/// default path prints nothing else. A launch that ASKED for Widevine prints a note (unless quiet) and goes on
/// without DRM when it fails.</para>
/// <para>The CDM is cached (CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm); Google is asked for a newer one
/// at most once a day, and a failed check counts as one (the cached CDM keeps working). Installs take the SDK's
/// install lock in the cache directory and land with Download's directory move (retried on Windows while a
/// scanner holds the fresh tree), so concurrent first launches never see a half-extracted CDM. The version Google
/// names must look like one before it is used as a directory name.</para>
/// </remarks>
public static class Widevine
{
    /// Chrome's component-updater app id for the Widevine CDM.
    public const string AppId = "oimompecagnajdejgnnjijobebaeigek";
    /// Google's Omaha (component update) JSON endpoint.
    public const string OmahaUrl = "https://update.googleapis.com/service/update2/json";
    /// The component hint file an engine without <c>--widevine-cdm-path</c> reads from the profile.
    public const string HintFile = "latest-component-updated-widevine-cdm";
    /// 1/true/yes/on forces Widevine on, 0/false/no/off off; an explicit option wins over it.
    public const string WidevineEnv = "CLEARCOTE_WIDEVINE";
    /// How long a DEFAULT (not requested) launch waits for a first download, in seconds (clamped to 1-600).
    public const string TimeoutEnv = "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS";
    public const double DefaultTimeoutSeconds = 15;
    public const double MinTimeoutSeconds = 1, MaxTimeoutSeconds = 600;
    /// In the cache root: its mtime is the last update check (a failed one counts).
    public const string StampFile = ".last-check";
    internal const string WarnCode = "widevine-default";
    /// What a CDM version looks like; anything else never becomes a path.
    internal static readonly System.Text.RegularExpressions.Regex VersionPattern = new(@"^[0-9]+(\.[0-9]+){1,3}\z");   // ASCII digits; \z: $ also matches before a trailing newline
    /// Ask Google for a newer CDM at most once a day.
    internal static readonly TimeSpan CheckInterval = TimeSpan.FromHours(24);
    /// A failed first download is not retried for 10 minutes in this process.
    internal static readonly TimeSpan RetryAfter = TimeSpan.FromMinutes(10);
    /// How long a requested install waits for another process installing the CDM.
    internal static readonly TimeSpan LockWait = TimeSpan.FromMinutes(5);

    private static readonly string[] Truthy = { "1", "true", "yes", "on" };
    private static readonly string[] Falsy = { "0", "false", "no", "off" };

    // ── the rules ──────────────────────────────────────────────────────────────

    /// <summary>The mode of one launch.</summary>
    /// <remarks><paramref name="option"/> (<see cref="LaunchOptions.Widevine"/>) wins: true -> Explicit, false ->
    /// Off. Null follows CLEARCOTE_WIDEVINE (1/true/yes/on -> Explicit, 0/false/no/off -> Off). Otherwise a licensed
    /// launch (<paramref name="licensed"/>: a licence key resolved, which selects the PRO build) gets the Default
    /// and a free launch stays Off. <paramref name="env"/> reads a variable (default: this process's).</remarks>
    public static WidevineMode Mode(bool? option, bool licensed, Func<string, string?>? env = null)
    {
        if (option is bool b) return b ? WidevineMode.Explicit : WidevineMode.Off;
        var value = ((env ?? Environment.GetEnvironmentVariable)(WidevineEnv) ?? "").Trim().ToLowerInvariant();
        if (Truthy.Contains(value)) return WidevineMode.Explicit;
        if (Falsy.Contains(value)) return WidevineMode.Off;
        return licensed ? WidevineMode.Default : WidevineMode.Off;
    }

    /// How long a default launch waits for a first download: CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS clamped to 1-600, else
    /// 15 (an unparsable or non-positive value, or NaN, too).
    public static double TimeoutSeconds(Func<string, string?>? env = null)
    {
        var raw = ((env ?? Environment.GetEnvironmentVariable)(TimeoutEnv) ?? "").Trim();
        if (!double.TryParse(raw, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var v)
            || !(v > 0)) return DefaultTimeoutSeconds;
        return Math.Min(Math.Max(v, MinTimeoutSeconds), MaxTimeoutSeconds);
    }

    // ── platform + Omaha ───────────────────────────────────────────────────────

    private static bool Linux => Native.OsTag == "linux";
    private static bool Mac => Native.OsTag == "macos";
    /// An Apple Silicon Mac (Native.Arch is the OS's CPU, so an x64 .NET under Rosetta still counts).
    private static bool MacArm => Mac && Native.Arch == "arm64";

    /// (@os, os.platform, os.version, platform subdir, CDM file) for this OS. Linux registers the CDM via the hint
    /// file; Windows and macOS via the component-updater scan. macOS ships libwidevinecdm.dylib under mac_arm64 on
    /// Apple Silicon (the native PRO build), mac_x64 on an Intel Mac. Asked as an arm64 Mac, Google's server answers
    /// with <c>…_mac_arm64_….crx3</c>, whose library sits at _platform_specific/mac_arm64/libwidevinecdm.dylib
    /// (checked 2026-10-17, CDM 4.10.3112.0). Before this every Mac was sent the Windows CDM, which no Mac can load.
    internal static (string AtOs, string OsPlatform, string OsVersion, string Subdir, string FileName) CdmPlatform() =>
        Linux ? ("Linux", "Linux", "6.1.0", "linux_x64", "libwidevinecdm.so")
        : Mac ? ("mac", "Mac OS X", "14.0.0", MacArm ? "mac_arm64" : "mac_x64", "libwidevinecdm.dylib")
        : ("win", "Windows", "10.0.19045.0", "win_x64", "widevinecdm.dll");

    /// Omaha's (arch, nacl_arch, os.arch) for the CDM asked for: arm64 on an Apple Silicon Mac, x64 elsewhere
    /// (the Windows and Linux browsers are x64 builds).
    internal static (string Arch, string NaclArch, string OsArch) CdmArch() =>
        MacArm ? ("arm64", "arm", "arm64") : ("x64", "x86-64", "x86_64");

    // update.googleapis.com sits behind Google's edge; a bare client UA can 403. Match the OS asked for
    // (Chrome on Apple Silicon still says "Intel Mac OS X 10_15_7").
    internal static string UserAgent => Linux
        ? "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
        : Mac
            ? "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
            : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";

    /// The CDM cache root: CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm.
    public static string CacheRoot() =>
        Environment.GetEnvironmentVariable("CLEARCOTE_WIDEVINE_DIR") is { Length: > 0 } d ? d : Path.Combine(Native.ClearcoteDir, "WidevineCdm");

    /// Minimal Omaha v3.1 update check for this OS's CDM (version 0.0.0.0 -> the latest).
    public static string OmahaRequestBody() => OmahaRequestBody(AppId);

    /// The same update check for another component (<paramref name="appId"/>): the CRLSet fetch (<see cref="Crlset"/>) uses it.
    internal static string OmahaRequestBody(string appId)
    {
        var (atOs, osPlatform, osVersion, _, _) = CdmPlatform();
        var (arch, naclArch, osArch) = CdmArch();
        return new JsonObject
        {
            ["request"] = new JsonObject
            {
                ["@os"] = atOs, ["@updater"] = "clearcote", ["acceptformat"] = "crx3", ["protocol"] = "3.1",
                ["arch"] = arch, ["nacl_arch"] = naclArch, ["prodversion"] = "149.0.0.0", ["updaterversion"] = "149.0.0.0",
                ["dedup"] = "cr",
                ["os"] = new JsonObject { ["arch"] = osArch, ["platform"] = osPlatform, ["version"] = osVersion },
                ["app"] = new JsonArray(new JsonObject
                {
                    ["appid"] = appId, ["version"] = "0.0.0.0", ["updatecheck"] = new JsonObject(),
                    ["ping"] = new JsonObject { ["r"] = -2 },
                }),
            },
        }.ToJsonString();
    }

    /// (download url, sha256, version) from an Omaha JSON response: the pipelines shape or the classic one.
    public static (string Url, string Sha256, string Version) ParseUpdate(string json)
    {
        if (json.StartsWith(")]}'", StringComparison.Ordinal)) json = json.Contains('\n') ? json[(json.IndexOf('\n') + 1)..] : json[4..];
        var app = JsonNode.Parse(json)?["response"]?["app"]?[0];
        var uc = app?["updatecheck"];
        var status = uc?["status"]?.GetValue<string>();
        if (status != "ok") throw new InvalidOperationException($"Widevine update check status: {status}");
        var next = app?["nextversion"]?.GetValue<string>() ?? uc?["nextversion"]?.GetValue<string>() ?? "";
        foreach (var pl in uc?["pipelines"]?.AsArray() ?? new JsonArray())
            foreach (var op in pl?["operations"]?.AsArray() ?? new JsonArray())
            {
                var url = op?["urls"]?[0]?["url"]?.GetValue<string>();
                var sha = op?["out"]?["sha256"]?.GetValue<string>();
                if (!string.IsNullOrEmpty(url) && !string.IsNullOrEmpty(sha)) return (url, sha, next);
            }
        var codebase = uc?["urls"]?["url"]?[0]?["codebase"]?.GetValue<string>();
        var pkg = uc?["manifest"]?["packages"]?["package"]?[0];
        var name = pkg?["name"]?.GetValue<string>();
        if (!string.IsNullOrEmpty(codebase) && !string.IsNullOrEmpty(name))
            return (codebase.TrimEnd('/') + "/" + name, pkg?["hash_sha256"]?.GetValue<string>() ?? "",
                uc?["manifest"]?["version"]?.GetValue<string>() ?? "");
        throw new InvalidOperationException("could not find a CDM download URL in the update response");
    }

    /// A CRX3 file is 'Cr24' + u32 version + u32 header length + header + zip: the zip bytes.
    public static byte[] Crx3ToZip(byte[] data)
    {
        if (data.Length < 4 || Encoding.ASCII.GetString(data, 0, 4) != "Cr24") return data; // already a plain zip
        if (data.Length < 12) throw new InvalidDataException("malformed CRX3 (truncated header)");
        var headerLen = BitConverter.ToUInt32(data, 8);
        if (12L + headerLen > data.Length) throw new InvalidDataException("malformed CRX3 (header overruns buffer)");
        return data[(int)(12 + headerLen)..];
    }

    // ── the cache ──────────────────────────────────────────────────────────────

    private static string CdmBinary(string verDir)
    {
        var (_, _, _, sub, file) = CdmPlatform();
        return Path.Combine(verDir, "_platform_specific", sub, file);
    }

    /// A complete CDM directory: its manifest and this OS's CDM binary are both there.
    internal static bool ValidCdm(string? verDir) =>
        !string.IsNullOrEmpty(verDir) && File.Exists(CdmBinary(verDir)) && File.Exists(Path.Combine(verDir, "manifest.json"));

    private static (int, Version) VersionKey(string name) =>
        // "current" (no version in the update response) sorts below any real version
        System.Text.RegularExpressions.Regex.IsMatch(name, @"^\d+(\.\d+){1,3}$") && Version.TryParse(name, out var v) ? (1, v) : (0, new Version(0, 0));

    /// The newest complete CDM in the cache (<paramref name="root"/>, default <see cref="CacheRoot"/>), or null. No network.
    public static string? CachedCdm(string? root = null)
    {
        root ??= CacheRoot();
        IEnumerable<string> dirs;
        try { dirs = Directory.EnumerateDirectories(root).ToList(); }
        catch { return null; }
        return dirs.Where(d => !Path.GetFileName(d).StartsWith('.'))
            .OrderByDescending(d => VersionKey(Path.GetFileName(d)))
            .FirstOrDefault(ValidCdm);
    }

    /// Whether Google was asked for a newer CDM less than <see cref="CheckInterval"/> ago.
    private static bool StampFresh(string root)
    {
        try
        {
            var path = Path.Combine(root, StampFile);
            if (!File.Exists(path)) return false;
            var age = DateTime.UtcNow - File.GetLastWriteTimeUtc(path);
            return age > TimeSpan.FromHours(-1) && age < CheckInterval; // a stamp from the future (clock moved back) is not fresh
        }
        catch { return false; }
    }

    private static void TouchStamp(string root)
    {
        try { File.WriteAllText(Path.Combine(root, StampFile), DateTimeOffset.UtcNow.ToUnixTimeSeconds().ToString(System.Globalization.CultureInfo.InvariantCulture)); }
        catch { /* the next launch asks again */ }
    }

    /// Remove what an interrupted install left in the cache root (only under the install lock).
    private static void SweepLeftovers(string root)
    {
        try
        {
            foreach (var d in Directory.EnumerateDirectories(root))
            {
                var n = Path.GetFileName(d);
                if (n.StartsWith(".tmp-", StringComparison.Ordinal) || n.StartsWith(".trash-", StringComparison.Ordinal)) TryDeleteDir(d);
            }
        }
        catch { /* best-effort */ }
    }

    private static void TryDeleteDir(string dir)
    {
        try { if (Directory.Exists(dir)) Directory.Delete(dir, recursive: true); } catch { /* best-effort */ }
    }

    private static void Log(bool quiet, string msg) { if (!quiet) Console.Error.WriteLine($"[clearcote] [widevine] {msg}"); }

    internal static HttpClient Client()
    {
        var c = SdkHttp.Create();
        c.Timeout = Timeout.InfiniteTimeSpan; // bounded per request below
        c.DefaultRequestHeaders.UserAgent.ParseAdd(UserAgent);
        return c;
    }

    /// root/&lt;version&gt;, once <paramref name="version"/> looks like a CDM version (<c>4.10.2830.0</c>). The version comes
    /// from Google's update response: anything else (<c>../victim</c>, an absolute path) is refused before it can name,
    /// or delete, a directory. No version at all means <c>current</c>.
    internal static string VersionDir(string root, string? version)
    {
        if (string.IsNullOrEmpty(version)) return Path.Combine(root, "current");
        if (!VersionPattern.IsMatch(version))
            throw new InvalidDataException($"Widevine update response named an unexpected version \"{(version.Length > 80 ? version[..80] : version)}\" — refusing to use it");
        return Path.Combine(root, version);
    }

    /// Download, verify and install one CDM version into <paramref name="root"/>/&lt;version&gt;, holding the SDK's
    /// install lock (root/.install-lock, the protocol in Download.cs) so concurrent first launches take turns;
    /// extracts into root/.tmp-&lt;id&gt; and moves the finished tree into place with Download's directory move,
    /// which retries while a Windows scanner holds a file in the fresh tree.
    private static async Task<string> InstallAsync(string root, string url, string sha256, string version, bool quiet,
        TimeSpan lockWait, CancellationToken ct)
    {
        var verDir = VersionDir(root, version);
        Directory.CreateDirectory(root);
        using (await Download.AcquireInstallLockAsync(root, lockWait, quiet: true).ConfigureAwait(false))
        {
            if (ValidCdm(verDir)) return verDir; // another process installed it while we waited
            SweepLeftovers(root);
            Log(quiet, $"fetching CDM {(string.IsNullOrEmpty(version) ? "(latest)" : version)}");
            byte[] blob;
            using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
            {
                cts.CancelAfter(TimeSpan.FromSeconds(120));
                using var client = Client();
                using var res = await client.GetAsync(url, cts.Token).ConfigureAwait(false);
                if (!res.IsSuccessStatusCode) throw new HttpRequestException($"Widevine CDM download HTTP {(int)res.StatusCode}");
                blob = await res.Content.ReadAsByteArrayAsync(cts.Token).ConfigureAwait(false);
            }
            if (!string.Equals(Convert.ToHexString(SHA256.HashData(blob)), sha256, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Widevine CDM sha256 mismatch — refusing to install");
            var tmp = Path.Combine(root, ".tmp-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(8)).ToLowerInvariant());
            try
            {
                Directory.CreateDirectory(tmp);
                using (var zip = new ZipArchive(new MemoryStream(Crx3ToZip(blob)), ZipArchiveMode.Read))
                    zip.ExtractToDirectory(tmp); // refuses entries outside the directory
                // some CRX layouts nest the platform dir differently; surface a clear error
                if (!ValidCdm(tmp)) throw new InvalidDataException($"extracted CDM but {CdmPlatform().FileName} not at {CdmBinary(verDir)}");
                if (Directory.Exists(verDir)) // an incomplete tree (an older SDK extracted in place): move it aside
                {
                    var trash = Path.Combine(root, ".trash-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(8)).ToLowerInvariant());
                    Download.MoveDir(verDir, trash);
                    TryDeleteDir(trash);
                }
                Download.MoveDir(tmp, verDir);
            }
            finally { TryDeleteDir(tmp); }
        }
        Log(quiet, $"installed: {verDir}");
        return verDir;
    }

    /// <summary>Download + verify this OS's x64 Widevine CDM into <paramref name="dest"/> (default
    /// <see cref="CacheRoot"/>) as &lt;version&gt;/, and return that directory (manifest.json +
    /// _platform_specific/&lt;os&gt;_x64/&lt;CDM&gt;).</summary>
    /// <remarks>Always asks Google's update server for the latest version; the download is skipped when that
    /// version is already installed. A response without a sha256 is refused (the CDM is native code loaded into
    /// the browser), and so is a version that does not look like one.</remarks>
    public static Task<string> FetchAsync(string? dest = null, bool quiet = false, CancellationToken cancellationToken = default)
        => FetchCoreAsync(dest, quiet, LockWait, cancellationToken);

    private static async Task<string> FetchCoreAsync(string? dest, bool quiet, TimeSpan lockWait, CancellationToken ct)
    {
        string json;
        using (var cts = CancellationTokenSource.CreateLinkedTokenSource(ct))
        {
            cts.CancelAfter(TimeSpan.FromSeconds(60));
            using var client = Client();
            using var req = new HttpRequestMessage(HttpMethod.Post, OmahaUrl)
            {
                Content = new StringContent(OmahaRequestBody(), Encoding.UTF8, "application/json"),
            };
            using var res = await client.SendAsync(req, cts.Token).ConfigureAwait(false);
            if (!res.IsSuccessStatusCode) throw new HttpRequestException($"Widevine update check HTTP {(int)res.StatusCode}");
            json = await res.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false);
        }
        var (url, sha256, version) = ParseUpdate(json);
        var root = string.IsNullOrEmpty(dest) ? CacheRoot() : dest;
        var verDir = VersionDir(root, version);
        if (ValidCdm(verDir))
        {
            TouchStamp(root);
            Log(quiet, $"already present: {verDir}");
            return verDir;
        }
        if (string.IsNullOrEmpty(sha256))
            throw new InvalidDataException("Widevine update response had no sha256 — refusing to install an unverified CDM");
        var dir = await InstallAsync(root, url, sha256, version, quiet, lockWait, ct).ConfigureAwait(false);
        TouchStamp(root);
        return dir;
    }

    /// The CDM directory a launch should use, network only when needed: the cached CDM when Google was asked
    /// within the last day; otherwise <see cref="FetchAsync"/>. When that fails (offline, a firewall, Google
    /// refusing the request) the CDM already on disk still works and is used, and the failed check is recorded
    /// (the stamp is touched), so no launch asks again for a day. Throws only when there is no CDM at all.
    public static Task<string> ResolveAsync(bool quiet = false, CancellationToken cancellationToken = default)
        => ResolveCoreAsync(quiet, LockWait, cancellationToken);

    private static async Task<string> ResolveCoreAsync(bool quiet, TimeSpan lockWait, CancellationToken ct)
    {
        var root = CacheRoot();
        var cached = CachedCdm(root);
        if (cached is not null && StampFresh(root)) return cached;
        try { return await FetchCoreAsync(root, quiet, lockWait, ct).ConfigureAwait(false); }
        catch when (cached is not null)
        {
            TouchStamp(root);
            return cached;
        }
    }

    // ── the licensed default: no wait with a cached CDM, bounded + fail-soft without one ─

    private static readonly object DefaultGate = new();
    private static (Task<string> Task, CancellationTokenSource Cts)? _defaultJob;
    private static (Task Task, CancellationTokenSource Cts)? _refreshJob;
    private static long? _defaultFailedAtMs;   // Environment.TickCount64: a monotonic clock

    /// Tests: forget the in-flight fetch, the refresh and the last failure (and stop what is running).
    internal static void ResetDefaultState()
    {
        lock (DefaultGate)
        {
            _defaultJob?.Cts.Cancel();
            _refreshJob?.Cts.Cancel();
            _defaultJob = null;
            _refreshJob = null;
            _defaultFailedAtMs = null;
        }
    }

    /// Tests: pretend the last failure was this long ago.
    internal static void AgeLastFailure(TimeSpan by)
    {
        lock (DefaultGate) if (_defaultFailedAtMs is long t) _defaultFailedAtMs = t - (long)by.TotalMilliseconds;
    }

    /// Tests: the background refresh in flight, if any.
    internal static Task? RefreshInFlight()
    {
        lock (DefaultGate) return _refreshJob?.Task;
    }

    private static string ShortReason(Exception e)
    {
        var inner = e is AggregateException { InnerException: { } i } ? i : e;
        var text = (inner.Message ?? "").Trim().Split('\n')[0];
        if (text.Length > 160) text = text[..160];
        return text.Length > 0 ? $"{inner.GetType().Name}: {text}" : inner.GetType().Name;
    }

    private static void WarnDefault(string reason, bool quiet) =>
        LaunchWarnings.EmitOnce(new[]
        {
            new LaunchWarnings.Warning(WarnCode,
                $"Widevine is on by default for licensed launches, but the CDM could not be loaded ({reason}); " +
                "launching without it. Set CLEARCOTE_WIDEVINE=0 (or Widevine = false) to stop trying."),
        }, quiet);

    /// Ask Google whether a newer CDM exists, with nobody waiting: one at a time, quiet, on the thread pool (it never
    /// keeps the process alive). <see cref="ResolveAsync"/> records a failed check, so a broken network is asked once a day.
    private static void RefreshInBackground()
    {
        lock (DefaultGate)
        {
            if (_refreshJob is { Task.IsCompleted: false }) return;
            var cts = new CancellationTokenSource();
            _refreshJob = (Task.Run(async () =>
            {
                try { await ResolveCoreAsync(quiet: true, LockWait, cts.Token).ConfigureAwait(false); }
                catch { /* the cached CDM keeps working; nothing to report */ }
            }), cts);
        }
    }

    /// <summary>The CDM for a launch that did not ask for Widevine (the licensed default), or null.</summary>
    /// <remarks>A CDM in the cache is used AT ONCE, with no network on the launch path; when its last update check is
    /// over a day old, a background refresh asks Google for a newer one (used from the next launch on). Only with
    /// nothing cached does the launch wait for a download, at most <paramref name="timeout"/> (default
    /// <see cref="TimeoutSeconds"/>). FAIL-SOFT: never throws. When there is no CDM to use (the download failed or did
    /// not finish in time) it warns once per process (unless quiet) and answers null, so the launch goes on without
    /// DRM; a download still running at the deadline is cancelled, and a failed one is not retried for 10 minutes in
    /// this process. The fetches here are quiet: the default path prints nothing else.</remarks>
    internal static async Task<string?> DefaultCdmAsync(bool quiet, TimeSpan? timeout = null)
    {
        var root = CacheRoot();
        var cached = CachedCdm(root);
        if (cached is not null)
        {
            if (!StampFresh(root)) RefreshInBackground();
            return cached;
        }
        var wait = timeout ?? TimeSpan.FromSeconds(TimeoutSeconds());
        (Task<string> Task, CancellationTokenSource Cts) job;
        lock (DefaultGate)
        {
            if (_defaultFailedAtMs is long failed && Environment.TickCount64 - failed < (long)RetryAfter.TotalMilliseconds) return null;
            if (_defaultJob is not { Task.IsCompleted: false })
            {
                var cts = new CancellationTokenSource();
                _defaultJob = (Task.Run(() => ResolveCoreAsync(quiet: true, wait, cts.Token)), cts);
            }
            job = _defaultJob.Value;
        }
        var finished = await Task.WhenAny(job.Task, Task.Delay(wait)).ConfigureAwait(false);
        if (finished != job.Task)
        {
            lock (DefaultGate) _defaultFailedAtMs = Environment.TickCount64;
            job.Cts.Cancel(); // nothing keeps working for a launch that went on without it
            WarnDefault($"not ready within {wait.TotalSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture)} s", quiet);
            return null;
        }
        try { return await job.Task.ConfigureAwait(false); }
        catch (Exception e)
        {
            lock (DefaultGate) _defaultFailedAtMs = Environment.TickCount64;
            WarnDefault(ShortReason(e), quiet);
            return null;
        }
    }

    // ── a launch's switches ────────────────────────────────────────────────────

    /// <c>["--widevine-cdm-path=&lt;the fetched CDM&gt;"]</c> when the engine implements that switch (patch 1022),
    /// else none. Such an engine registers the CDM at startup in every profile, incognito included. Best-effort:
    /// any failure answers none and the launch goes on.
    internal static async Task<List<string>> CdmArgsAsync(string? exe, bool quiet)
    {
        try
        {
            if (!LaunchOpts.EngineSupportsSwitch(exe, "widevine-cdm-path")) return new List<string>();
            return new List<string> { "--widevine-cdm-path=" + await ResolveAsync(quiet).ConfigureAwait(false) };
        }
        catch { return new List<string>(); }
    }

    private static bool CallerSetCdmPath(IEnumerable<string>? userArgs) =>
        (userArgs ?? Array.Empty<string>()).Any(a => a.Split('=', 2)[0] == "--widevine-cdm-path");

    /// <summary>The Widevine switch for one launch, by <see cref="Mode"/>.</summary>
    /// <remarks>Explicit: <see cref="CdmArgsAsync"/>. Default: the same switch, only on an engine that implements
    /// it (r32+; an older engine keeps the old default, off), with the CDM from <see cref="DefaultCdmAsync"/>.
    /// Off: none. A <c>--widevine-cdm-path</c> in the caller's own Args is theirs: none is added then.</remarks>
    internal static async Task<List<string>> LaunchArgsAsync(string? exe, bool? option, bool licensed, IEnumerable<string>? userArgs,
        bool quiet, TimeSpan? timeout = null)
    {
        var mode = Mode(option, licensed);
        if (mode == WidevineMode.Off || CallerSetCdmPath(userArgs)) return new List<string>();
        if (mode == WidevineMode.Explicit) return await CdmArgsAsync(exe, quiet).ConfigureAwait(false);
        if (!LaunchOpts.EngineSupportsSwitch(exe, "widevine-cdm-path")) return new List<string>();
        var cdm = await DefaultCdmAsync(quiet, timeout).ConfigureAwait(false);
        return cdm is null ? new List<string>() : new List<string> { "--widevine-cdm-path=" + cdm };
    }

    // ── profile seeding (an engine without --widevine-cdm-path) ────────────────

    /// <summary>Make a persistent profile load the CDM: copy it under &lt;userDataDir&gt;/WidevineCdm/&lt;version&gt;/
    /// and write the component hint file the engine reads. Fetches the CDM first if needed (the cached one when it
    /// is current). Safe to call on every launch.</summary>
    public static async Task<string> SeedAsync(string userDataDir, string? cdmDir = null, bool quiet = false)
    {
        var src = cdmDir ?? await ResolveAsync(quiet).ConfigureAwait(false);
        var version = Path.GetFileName(src.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar));
        var wvRoot = Path.Combine(userDataDir, "WidevineCdm");
        var target = Path.Combine(wvRoot, version);
        if (!File.Exists(CdmBinary(target))) CopyTree(src, target);
        try { File.WriteAllText(Path.Combine(wvRoot, HintFile), JsonSerializer.Serialize(new { Path = target })); }
        catch { /* the hint file is what an engine without the switch reads; harmless if it cannot be written */ }
        Log(quiet, $"seeded into {wvRoot}");
        return target;
    }

    private static void CopyTree(string src, string dst)
    {
        Directory.CreateDirectory(dst);
        foreach (var dir in Directory.EnumerateDirectories(src, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(Path.Combine(dst, Path.GetRelativePath(src, dir)));
        foreach (var file in Directory.EnumerateFiles(src, "*", SearchOption.AllDirectories))
            File.Copy(file, Path.Combine(dst, Path.GetRelativePath(src, file)), overwrite: true);
    }

    /// <summary>What a requested Widevine launch on a persistent profile changes besides the switch: Playwright's
    /// <c>--disable-component-update</c> is no longer passed (the component updater registers the seeded CDM), and on
    /// Windows and macOS <c>--component-updater=fast-update</c> forces the startup scan (on Linux the hint file registers it).
    /// A caller's own <c>--component-updater</c> mode is kept.</summary>
    internal static (List<string> IgnoreDefaultArgs, List<string> Args) ComponentUpdaterArgs(IReadOnlyList<string>? ignoreDefaultArgs,
        IReadOnlyList<string>? userArgs, bool quiet = true)
    {
        var ignore = (ignoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs).ToList();
        if (!ignore.Contains("--disable-component-update")) ignore.Add("--disable-component-update");
        var args = (userArgs ?? Array.Empty<string>()).ToList();
        if (!Linux)
        {
            var mode = args.Where(a => a.Contains("component-updater", StringComparison.Ordinal)).ToList();
            if (mode.Count == 0) args.Add("--component-updater=fast-update");
            else if (!mode.Any(a => a.Contains("fast-update", StringComparison.Ordinal)))
                Log(quiet, "note: your --component-updater mode may not register the CDM; --component-updater=fast-update is needed to scan the pre-installed component");
        }
        return (ignore, args);
    }
}
