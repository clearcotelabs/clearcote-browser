using System.Globalization;
using System.Net.Http.Headers;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

/// Options of the profile service client: selection, plus the licence and endpoint.
public class ProfileSourceOptions : SelectOptions
{
    /// Licence key; defaults to CLEARCOTE_LICENSE_KEY.
    public string? LicenseKey { get; set; }
    /// Service base URL; defaults to CLEARCOTE_PROFILE_API, then CLEARCOTE_LICENSE_API, then https://www.clearcotelabs.com.
    public string? ApiBase { get; set; }
    /// Skip the network entirely and use only what is already cached.
    public bool? Offline { get; set; }
}

/// A profile resolved end to end: the captured fingerprint and how it was picked.
public sealed record ResolvedProfile(JsonObject Profile, ProfileSelection Selection);

/// <summary>
/// Fetching personas from the profile service, and caching them locally. Port of the Node SDK's
/// <c>profilesource.ts</c>.
/// </summary>
/// <remarks>
/// <para>Selection runs CLIENT-SIDE (see <see cref="ProfileLib"/>) because it needs facts only this machine has:
/// the real GPU, the real display, the engine's actual Chromium major. The server cannot see any of those, so it
/// returns candidates for a described host and the SDK picks among them.</para>
/// <para>The service caps DISTINCT profiles per license per day, and re-fetching one already held is free. That is
/// why this caches aggressively on disk (<c>&lt;CLEARCOTE_PROFILE_DIR&gt;/library</c>, shared with the Node and
/// Python SDKs): a sticky identity must resolve to the same profile every launch without spending budget or
/// requiring the network.</para>
/// </remarks>
public static class ProfileSource
{
    internal const string DefaultApiBase = "https://www.clearcotelabs.com";

    private static string ApiBase(string? baseUrl)
    {
        var b = new[] { baseUrl, Environment.GetEnvironmentVariable("CLEARCOTE_PROFILE_API"), Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_API") }
            .FirstOrDefault(s => !string.IsNullOrEmpty(s)) ?? DefaultApiBase;
        return b.TrimEnd('/');
    }

    /// <c>&lt;CLEARCOTE_PROFILE_DIR&gt;/library</c>, created on demand.
    internal static string CacheDir()
    {
        var d = Path.Combine(Profile.ProfileDir, "library");
        Directory.CreateDirectory(d);
        return d;
    }

    /// This host's OS as the os_family values the corpus stores ("windows", "macos", "linux").
    public static string HostOsFamily() => Native.OsTag;

    // ── host facts ─────────────────────────────────────────────────────────────────────────────
    //
    // The GPU and display can only be read by actually rendering something, so they come from a one-off
    // no-persona launch. That is expensive (a browser start), so the result is cached on disk and keyed by the
    // engine binary — a different build may report a different GL string.

    private static readonly TimeSpan HostCacheMaxAge = TimeSpan.FromDays(30);

    private static string HostCachePath() => Path.Combine(CacheDir(), "host.json");

    /// One cache key per BINARY, not per spelling of its path (as the Python SDK keys it): the same file spelled
    /// with other separators, through "." segments or through a symlink must not re-measure the host, which is
    /// the one path that launches a second browser.
    internal static string ExeKey(string exe)
    {
        string full;
        try
        {
            full = Path.GetFullPath(exe);
            try { full = new FileInfo(full).ResolveLinkTarget(returnFinalTarget: true)?.FullName ?? full; } catch { }
        }
        catch { full = exe; }
        return OperatingSystem.IsWindows() ? full.ToLowerInvariant() : full;
    }

    /// The cached facts for <paramref name="exe"/>, or null (none, another binary, or older than 30 days).
    public static HostFacts? ReadCachedHost(string exe)
    {
        try
        {
            if (JsonNode.Parse(File.ReadAllText(HostCachePath())) is not JsonObject c) return null;
            if (c["_exe"] is not JsonValue e || !e.TryGetValue<string>(out var cachedExe) || ExeKey(cachedExe) != ExeKey(exe))
                return null;   // different binary: re-measure
            // Host hardware does not change often, but a monitor or driver swap does; a month is a reasonable
            // compromise between staleness and paying for a launch every run. "_at" is epoch seconds (Python's
            // spelling, which this SDK writes) or an ISO time (Node's).
            DateTimeOffset at;
            if (ProfileJson.Num(c["_at"]) is double secs) at = DateTimeOffset.FromUnixTimeMilliseconds((long)(secs * 1000));
            else if (c["_at"] is JsonValue a2 && a2.TryGetValue<string>(out var iso)
                     && DateTimeOffset.TryParse(iso, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out var parsed)) at = parsed;
            else return null;
            if (DateTimeOffset.UtcNow - at > HostCacheMaxAge) return null;
            return ProfileJson.HostFrom(c);
        }
        catch
        {
            return null;
        }
    }

    /// Cache <paramref name="facts"/> for <paramref name="exe"/>. Never throws.
    public static void WriteCachedHost(string exe, HostFacts facts)
    {
        try
        {
            var o = ProfileJson.HostTo(facts);
            o["_exe"] = ExeKey(exe);
            o["_at"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
            File.WriteAllText(HostCachePath(), o.ToJsonString());
        }
        catch
        {
            /* a cache write failure must never fail a launch */
        }
    }

    /// The in-page probe, as an expression. Reads the REAL GPU and display — the probe launch applies no persona,
    /// so what it sees is the host itself.
    public const string ProbeExpression = """
        (() => {
          let renderer = "";
          try {
            const gl = document.createElement("canvas").getContext("webgl");
            const dbg = gl && gl.getExtension("WEBGL_debug_renderer_info");
            if (gl && dbg) renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
          } catch (e) { /* headless with no GPU: leave blank, scored as unknown rather than guessed */ }
          return {
            renderer: renderer,
            screen_width: screen.width,
            screen_height: screen.height,
            device_pixel_ratio: devicePixelRatio,
            hardware_concurrency: navigator.hardwareConcurrency,
            device_memory: navigator.deviceMemory,
          };
        })()
        """;

    /// <summary>
    /// Read the host's real GPU/display by rendering in the engine itself, with no persona applied. Cached per
    /// binary for 30 days (see <see cref="ReadCachedHost"/>).
    /// </summary>
    /// <remarks>
    /// <paramref name="browserMajor"/> is a FALLBACK only. The major is read from the running browser when
    /// possible, because that is the only authoritative source: the caller may have pinned a Version, set
    /// CLEARCOTE_BROWSER_VERSION, or supplied their own ExecutablePath, and in all of those the SDK's pinned release
    /// describes a different binary than the one about to run. Filtering profiles against the wrong major would
    /// silently produce a persona that contradicts the engine.
    /// </remarks>
    public static async Task<HostFacts> MeasureHostAsync(Func<LaunchOptions, Task<IBrowser>> launch, string exe, int browserMajor)
    {
        var cached = ReadCachedHost(exe);
        if (cached is not null) return cached;

        // Geoip = false: the probe measures the host, not the egress; the default lookup would only cost time.
        var browser = await launch(new LaunchOptions { ExecutablePath = exe, Headless = true, Quiet = true, Geoip = false }).ConfigureAwait(false);
        try
        {
            var ctx = await browser.NewContextAsync().ConfigureAwait(false);
            var page = await ctx.NewPageAsync().ConfigureAwait(false);
            // about:blank is enough: WebGL and screen do not need an origin.
            await page.GotoAsync("about:blank").ConfigureAwait(false);
            var facts = JsonNode.Parse((await page.EvaluateAsync<JsonElement>(ProbeExpression).ConfigureAwait(false)).GetRawText()) as JsonObject
                        ?? new JsonObject();
            var out_ = new HostFacts
            {
                OsFamily = HostOsFamily(),
                BrowserMajor = EngineMajor(browser, browserMajor),
                GpuVendor = ProfileLib.GpuVendorClass(ProfileJson.Str(facts["renderer"])),
                ScreenWidth = ProfileJson.Int(facts["screen_width"]) ?? 0,
                ScreenHeight = ProfileJson.Int(facts["screen_height"]) ?? 0,
                DevicePixelRatio = ProfileJson.Num(facts["device_pixel_ratio"]),
                HardwareConcurrency = ProfileJson.Int(facts["hardware_concurrency"]),
                DeviceMemory = ProfileJson.Int(facts["device_memory"]),
            };
            WriteCachedHost(exe, out_);
            return out_;
        }
        finally
        {
            try { await browser.CloseAsync().ConfigureAwait(false); } catch { }
            // The caller's own launch checks out its seat next: on a one-browser plan the probe's seat must be back first.
            await Clearcote.LeaseReleasedAsync(browser).ConfigureAwait(false);
        }
    }

    /// The engine's OWN major wins over the caller's guess. Playwright reports it as "150.0.7871.114"; the
    /// fallback is kept when the shape is unexpected.
    internal static int EngineMajor(IBrowser browser, int fallback)
    {
        try
        {
            var m = Regex.Match(browser.Version ?? "", @"(\d+)\.");
            return m.Success ? int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture) : fallback;
        }
        catch
        {
            return fallback;
        }
    }

    // ── service client ─────────────────────────────────────────────────────────────────────────

    private static string AuthKey(string? licenseKey)
    {
        var key = !string.IsNullOrEmpty(licenseKey) ? licenseKey : Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_KEY");
        if (string.IsNullOrEmpty(key))
            throw new InvalidOperationException(
                "the profile library needs a license key — pass LicenseKey or set CLEARCOTE_LICENSE_KEY " +
                "(profiles are served to PRO licenses with an active subscription)");
        return key;
    }

    private static async Task<(int Status, string Body)> GetAsync(string url, string key)
    {
        using var http = SdkHttp.Create();
        http.Timeout = TimeSpan.FromSeconds(30);
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", key);
        using var res = await http.SendAsync(req).ConfigureAwait(false);
        return ((int)res.StatusCode, await res.Content.ReadAsStringAsync().ConfigureAwait(false));
    }

    private static string Head(string s, int n) => s.Length <= n ? s : s[..n];

    /// Candidate index for a described host. Cached so repeated launches (and an unreachable service) do not
    /// re-ask: a failed request serves the cached index rather than failing the launch.
    public static async Task<List<ProfileIndexEntry>> FetchIndexAsync(HostFacts host, ProfileSourceOptions? options = null)
    {
        options ??= new ProfileSourceOptions();
        var os = (options.Os ?? host.OsFamily ?? "").ToLowerInvariant();
        var cachePath = Path.Combine(CacheDir(), $"index-{os}-{host.BrowserMajor}.json");
        List<ProfileIndexEntry> Cached() => ProfileJson.Entries(JsonNode.Parse(File.ReadAllText(cachePath)));

        if (options.Offline == true)
        {
            if (!File.Exists(cachePath)) throw new InvalidOperationException($"offline: no cached profile index for {os}/{host.BrowserMajor}");
            return Cached();
        }

        var key = AuthKey(options.LicenseKey);
        var url = $"{ApiBase(options.ApiBase)}/api/v1/profiles?os={Uri.EscapeDataString(os)}&major={host.BrowserMajor}";
        int status;
        string body;
        try
        {
            (status, body) = await GetAsync(url, key).ConfigureAwait(false);
        }
        catch when (File.Exists(cachePath))
        {
            return Cached();   // unreachable: a cached index is far better than no persona (as the Python SDK)
        }
        if (status < 200 || status > 299)
        {
            // Serve stale rather than fail the launch — a cached index is far better than no persona.
            if (File.Exists(cachePath)) return Cached();
            throw new InvalidOperationException($"profile index request failed ({status}): {Head(body, 200)}");
        }
        var candidates = (JsonNode.Parse(body) as JsonObject)?["candidates"] as JsonArray;
        if (candidates is null) return new List<ProfileIndexEntry>();
        try { File.WriteAllText(cachePath, candidates.ToJsonString()); } catch { /* non-fatal */ }
        return ProfileJson.Entries(candidates);
    }

    /// Fetch one profile by id, preferring the on-disk copy — a repeat costs no distinct budget, but not spending a
    /// request at all is better still.
    public static async Task<JsonObject> FetchProfileAsync(string id, ProfileSourceOptions? options = null)
    {
        options ??= new ProfileSourceOptions();
        // The id comes from the service's index (or a cached one): never let it name a file outside the cache.
        if (id.Contains('/') || id.Contains('\\') || id.Contains("..")) throw new ArgumentException($"invalid profile id '{id}'");
        var cachePath = Path.Combine(CacheDir(), $"profile-{id}.json");
        if (File.Exists(cachePath))
        {
            try { if (JsonNode.Parse(File.ReadAllText(cachePath)) is JsonObject cached) return cached; }
            catch { /* refetch */ }
        }
        if (options.Offline == true) throw new InvalidOperationException($"offline: profile {id} is not cached");

        var (status, body) = await GetAsync($"{ApiBase(options.ApiBase)}/api/v1/profiles?id={Uri.EscapeDataString(id)}",
            AuthKey(options.LicenseKey)).ConfigureAwait(false);
        if (status < 200 || status > 299)
            throw new InvalidOperationException($"profile fetch failed ({status}): {Head(body, 200)}");
        if ((JsonNode.Parse(body) as JsonObject)?["profile"] is not JsonObject profile)
            throw new InvalidOperationException($"profile fetch returned no profile for {id}");
        try { File.WriteAllText(cachePath, profile.ToJsonString()); } catch { /* non-fatal */ }
        return profile;
    }

    /// Resolve a persona end to end: index -&gt; score against this host -&gt; fetch the winner.
    public static async Task<ResolvedProfile> ResolveAutoProfileAsync(HostFacts host, ProfileSourceOptions? options = null)
    {
        var index = await FetchIndexAsync(host, options).ConfigureAwait(false);
        var selection = ProfileLib.SelectProfile(index, host, options);
        var profile = await FetchProfileAsync(selection.Entry.Id, options).ConfigureAwait(false);
        return new ResolvedProfile(profile, selection);
    }
}

/// The corpus's snake_case JSON for index entries and host facts, read leniently (a missing or non-numeric field
/// is unknown, never an exception), so files written by any of the three SDKs and the service all read.
internal static class ProfileJson
{
    internal static string? Str(JsonNode? n) => n is JsonValue v
        ? (v.TryGetValue<string>(out var s) ? s : v.ToJsonString())
        : n?.ToJsonString();

    // Through the JSON text: a value built in code (an int) and one parsed from a file (a JsonElement) read alike.
    internal static double? Num(JsonNode? n) =>
        n is JsonValue v && v.GetValueKind() == JsonValueKind.Number
        && double.TryParse(v.ToJsonString(), NumberStyles.Float, CultureInfo.InvariantCulture, out var d) ? d : null;

    internal static int? Int(JsonNode? n) => Num(n) is double d && !double.IsNaN(d) && !double.IsInfinity(d) ? (int)d : null;

    internal static ProfileIndexEntry? EntryFrom(JsonNode? node)
    {
        if (node is not JsonObject o || Str(o["id"]) is not { Length: > 0 } id) return null;
        return new ProfileIndexEntry
        {
            Id = id,
            OsFamily = Str(o["os_family"]),
            OsMajor = Str(o["os_major"]),
            BrowserMajor = Int(o["browser_major"]),
            GpuVendor = Str(o["gpu_vendor"]),
            GpuTier = Int(o["gpu_tier"]),
            Renderer = Str(o["renderer"]),
            ScreenWidth = Int(o["screen_width"]) ?? 0,
            ScreenHeight = Int(o["screen_height"]) ?? 0,
            DevicePixelRatio = Num(o["device_pixel_ratio"]),
            HardwareConcurrency = Int(o["hardware_concurrency"]),
            DeviceMemory = Int(o["device_memory"]),
            EncodedSize = Int(o["encoded_size"]),
            CapturedAt = o["captured_at"] is JsonValue c && c.TryGetValue<string>(out var at) ? at : null,
        };
    }

    internal static List<ProfileIndexEntry> Entries(JsonNode? node)
        => node is JsonArray a ? a.Select(EntryFrom).Where(e => e is not null).Select(e => e!).ToList() : new List<ProfileIndexEntry>();

    internal static JsonObject EntryTo(ProfileIndexEntry e)
    {
        var o = new JsonObject { ["id"] = e.Id, ["os_family"] = e.OsFamily, ["browser_major"] = e.BrowserMajor, ["gpu_vendor"] = e.GpuVendor };
        if (e.OsMajor is not null) o["os_major"] = e.OsMajor;
        if (e.GpuTier is not null) o["gpu_tier"] = e.GpuTier;
        if (e.Renderer is not null) o["renderer"] = e.Renderer;
        o["screen_width"] = e.ScreenWidth;
        o["screen_height"] = e.ScreenHeight;
        if (e.DevicePixelRatio is not null) o["device_pixel_ratio"] = e.DevicePixelRatio;
        if (e.HardwareConcurrency is not null) o["hardware_concurrency"] = e.HardwareConcurrency;
        if (e.DeviceMemory is not null) o["device_memory"] = e.DeviceMemory;
        if (e.EncodedSize is not null) o["encoded_size"] = e.EncodedSize;
        if (e.CapturedAt is not null) o["captured_at"] = e.CapturedAt;
        return o;
    }

    internal static HostFacts HostFrom(JsonObject o) => new()
    {
        OsFamily = Str(o["os_family"]),
        BrowserMajor = Int(o["browser_major"]) ?? 0,
        GpuVendor = Str(o["gpu_vendor"]),
        GpuTier = Int(o["gpu_tier"]),
        ScreenWidth = Int(o["screen_width"]) ?? 0,
        ScreenHeight = Int(o["screen_height"]) ?? 0,
        DevicePixelRatio = Num(o["device_pixel_ratio"]),
        HardwareConcurrency = Int(o["hardware_concurrency"]),
        DeviceMemory = Int(o["device_memory"]),
    };

    internal static JsonObject HostTo(HostFacts h)
    {
        var o = new JsonObject { ["os_family"] = h.OsFamily, ["browser_major"] = h.BrowserMajor, ["gpu_vendor"] = h.GpuVendor };
        if (h.GpuTier is not null) o["gpu_tier"] = h.GpuTier;
        o["screen_width"] = h.ScreenWidth;
        o["screen_height"] = h.ScreenHeight;
        o["device_pixel_ratio"] = h.DevicePixelRatio;
        o["hardware_concurrency"] = h.HardwareConcurrency;
        o["device_memory"] = h.DeviceMemory;
        return o;
    }
}
