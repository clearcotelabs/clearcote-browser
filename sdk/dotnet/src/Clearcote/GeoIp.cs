using System.Diagnostics;
using System.Globalization;
using System.IO.Compression;
using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;
using MaxMind.Db;

namespace Clearcote;

/// Geo for an egress IP.
public sealed record GeoInfo(string? Ip, string? Country, string? Timezone, string? AcceptLanguage, string? Location);

/// Outcome of a geo resolution: the geo, or why there is none.
public sealed record GeoResult(GeoInfo? Geo, string? Reason, long ElapsedMs);

/// Thrown by a launch with <see cref="LaunchOptions.Geoip"/> when the region could not be resolved.
///
/// Failing closed is the point: continuing would launch with the host's clock and a default
/// language — UTC + en-US on most servers — which is exactly the mismatch geoip exists to prevent.
/// Set both <see cref="FingerprintOptions.Timezone"/> and <see cref="FingerprintOptions.AcceptLanguage"/>
/// to launch anyway when the lookup is unavailable.
public sealed class GeoipException : Exception
{
    public string Code => "GEOIP_UNRESOLVED";
    public GeoipException(string message) : base(message) { }
}

/// Region lookup for the connection a launch will use (ports geoip.ts).
///
/// The exit IP comes from a small IP echo THROUGH the proxy (HTTP or SOCKS5), never from the host, or the
/// timezone and locale would describe the wrong place. That IP is then looked up in daijro's offline
/// "geoip-all-in-one" MaxMind database (GPL-3.0 data, ~52 MB download on first use, cached in the same place
/// the Python and Node SDKs use). The whole resolution is bounded by CLEARCOTE_GEOIP_TIMEOUT_SECONDS
/// (default 20).
///
/// Settings: CLEARCOTE_GEOIP_ECHO_URL — your own IP-echo endpoint(s), comma-separated, instead of the public
/// ones (api.ipify.org, icanhazip.com); the answer is the IP as plain text, or JSON with an <c>ip</c> (or
/// <c>query</c>) field. CLEARCOTE_GEOIP_IPAPI=1 — also ask ip-api.com when the database has no answer (e.g.
/// while it is still downloading). Off by default: ip-api.com's free endpoint is for non-commercial use only
/// and allows 45 requests a minute per IP.
public static class GeoIp
{
    /// Public IP-echo services with no usage limit, tried in order; CLEARCOTE_GEOIP_ECHO_URL replaces them.
    internal static string[] IpEchoUrls = { "http://api.ipify.org", "http://icanhazip.com" };
    internal static string IpApiUrl = "http://ip-api.com/json/?fields=status,message,countryCode,timezone,lat,lon,query";
    internal static string MmdbUrl = "https://github.com/daijro/geoip-all-in-one/releases/latest/download/geoip-aio-all.mmdb.zip";
    private const int MmdbMaxAgeDays = 30;
    public const string EchoUrlEnv = "CLEARCOTE_GEOIP_ECHO_URL";
    public const string IpApiEnv = "CLEARCOTE_GEOIP_IPAPI";

    private static string? Env(IDictionary<string, string?>? env, string key)
        => env is null ? Environment.GetEnvironmentVariable(key) : env.TryGetValue(key, out var v) ? v : null;

    /// The IP-echo endpoints, in order: CLEARCOTE_GEOIP_ECHO_URL (comma-separated) when set, else the public defaults.
    internal static string[] EchoUrls(IDictionary<string, string?>? env = null)
    {
        var custom = (Env(env, EchoUrlEnv) ?? "").Split(',').Select(u => u.Trim()).Where(u => u.Length > 0).ToArray();
        return custom.Length > 0 ? custom : IpEchoUrls.ToArray();
    }

    /// Whether CLEARCOTE_GEOIP_IPAPI opts in to the ip-api.com fallback (1/true/yes/on).
    internal static bool IpApiEnabled(IDictionary<string, string?>? env = null)
        => (Env(env, IpApiEnv) ?? "").Trim().ToLowerInvariant() is "1" or "true" or "yes" or "on";

    /// The IP an echo answered: plain text (first word), or JSON with an <c>ip</c> or <c>query</c> field.
    internal static string IpFromEcho(string? text)
    {
        var t = (text ?? "").Trim();
        if (t.StartsWith('{'))
        {
            try
            {
                using var doc = JsonDocument.Parse(t);
                var r = doc.RootElement;
                t = r.ValueKind == JsonValueKind.Object && (r.TryGetProperty("ip", out var v) || r.TryGetProperty("query", out v))
                    && v.ValueKind == JsonValueKind.String ? v.GetString() ?? "" : "";
            }
            catch (JsonException) { return ""; }
        }
        return t.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries).FirstOrDefault() ?? "";
    }

    /// Where the geoip database is cached (CLEARCOTE_CACHE/geoip when set) — the same place the Python and Node
    /// SDKs use, so one download serves all three.
    public static string GeoCacheRoot()
    {
        var cache = Environment.GetEnvironmentVariable("CLEARCOTE_CACHE");
        if (!string.IsNullOrEmpty(cache)) return Path.Join(cache, "geoip");
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (OperatingSystem.IsWindows())
        {
            var local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
            return Path.Join(string.IsNullOrEmpty(local) ? Path.Join(home, "AppData", "Local") : local, "clearcote", "geoip");
        }
        if (OperatingSystem.IsMacOS()) return Path.Join(home, "Library", "Caches", "clearcote", "geoip");
        var xdg = Environment.GetEnvironmentVariable("XDG_CACHE_HOME");
        return Path.Join(string.IsNullOrEmpty(xdg) ? Path.Join(home, ".cache") : xdg, "clearcote", "geoip");
    }

    /// Why a lookup had no database: "downloading" (first use, still going) or "unavailable".
    internal sealed class DbInfo { public string? Db { get; set; } }

    private static readonly object MmdbLock = new();
    private static Task<string?>? _mmdbInflight;

    private static Task<string?> EnsureMmdbAsync(bool quiet)
    {
        var file = Path.Join(GeoCacheRoot(), "geoip-aio-all.mmdb");
        if (File.Exists(file) && (DateTime.UtcNow - File.GetLastWriteTimeUtc(file)).TotalDays < MmdbMaxAgeDays)
            return Task.FromResult<string?>(file);
        lock (MmdbLock)
        {
            // One download per process: a launch that runs out of budget leaves it going for the next one.
            if (_mmdbInflight is null || _mmdbInflight.IsCompleted) _mmdbInflight = Task.Run(() => DownloadMmdbAsync(file, quiet));
            return _mmdbInflight;
        }
    }

    private static async Task<string?> DownloadMmdbAsync(string file, bool quiet)
    {
        var zip = Path.Join(Path.GetTempPath(), $"cc-geoip-{Environment.ProcessId}-{Guid.NewGuid():N}.zip");
        var part = $"{file}.part-{Environment.ProcessId}";
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(file)!);
            if (!quiet) Console.Error.WriteLine("[clearcote] geoip: downloading the geoip-all-in-one database (~52 MB, first run only)");
            using (var http = new HttpClient { Timeout = TimeSpan.FromSeconds(120) })
            {
                http.DefaultRequestHeaders.UserAgent.ParseAdd("clearcote-sdk");
                await using var src = await http.GetStreamAsync(MmdbUrl).ConfigureAwait(false);
                await using var dst = File.Create(zip);
                await src.CopyToAsync(dst).ConfigureAwait(false);
            }
            using (var archive = ZipFile.OpenRead(zip))
            {
                var entry = archive.Entries.FirstOrDefault(e => e.Name.EndsWith(".mmdb", StringComparison.OrdinalIgnoreCase))
                            ?? throw new InvalidDataException("no .mmdb in archive");
                entry.ExtractToFile(part, overwrite: true);
            }
            File.Move(part, file, overwrite: true);  // atomic: a waiter never opens a half-written file
            if (!quiet) Console.Error.WriteLine("[clearcote] geoip: database ready");
            return file;
        }
        catch (Exception e)
        {
            if (!quiet) Console.Error.WriteLine($"[clearcote] geoip: database fetch failed ({e.GetType().Name}: {e.Message})");
            return File.Exists(file) ? file : null;  // a stale database still beats none
        }
        finally
        {
            foreach (var f in new[] { zip, part })
            {
                try { File.Delete(f); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
            }
        }
    }

    private static readonly object ReaderLock = new();
    private static Reader? _reader;
    private static string? _readerPath;

    private static object? Field(Dictionary<string, object> rec, string a, string b)
        => rec.TryGetValue(a, out var o) && o is Dictionary<string, object> d && d.TryGetValue(b, out var v) ? v : null;

    private static double? Number(object? o) => o switch
    {
        double d => d, float f => f, int i => i, long l => l, uint u => u, ulong ul => ul,
        decimal m => (double)m, _ => null,
    };

    private static async Task<GeoInfo?> MmdbLookupAsync(string ip, Stopwatch sw, int budget, bool quiet, DbInfo info)
    {
        var left = budget - (int)sw.ElapsedMilliseconds;
        if (left <= 0) return null;
        var ensure = EnsureMmdbAsync(quiet);
        if (await Task.WhenAny(ensure, Task.Delay(left)).ConfigureAwait(false) != ensure)
        {
            info.Db = "downloading";
            return null;
        }
        var path = await ensure.ConfigureAwait(false);
        if (path is null)
        {
            info.Db = "unavailable";
            return null;
        }
        try
        {
            Reader reader;
            lock (ReaderLock)
            {
                if (_reader is null || _readerPath != path)
                {
                    _reader?.Dispose();
                    // Read into memory, as the Node SDK does: no open handle keeps a refreshed download from replacing it.
                    _reader = new Reader(path, FileAccessMode.Memory);
                    _readerPath = path;
                }
                reader = _reader;
            }
            var rec = reader.Find<Dictionary<string, object>>(IPAddress.Parse(ip));
            if (rec is null) return null;
            var country = Field(rec, "country", "iso_code") as string;
            var lat = Number(Field(rec, "location", "latitude"));
            var lon = Number(Field(rec, "location", "longitude"));
            var tz = Field(rec, "location", "time_zone") as string;
            if (string.IsNullOrEmpty(tz) && lat is null) return null;
            return new GeoInfo(ip, country?.ToUpperInvariant(), string.IsNullOrEmpty(tz) ? null : tz, AcceptLanguageForCountry(country),
                lat is not null && lon is not null
                    ? $"{lat.Value.ToString(CultureInfo.InvariantCulture)},{lon.Value.ToString(CultureInfo.InvariantCulture)}"
                    : null);
        }
        catch (Exception e)
        {
            if (!quiet) Console.Error.WriteLine($"[clearcote] geoip: mmdb read failed ({e.GetType().Name}: {e.Message})");
            return null;
        }
    }

    /// The database lookup, replaceable in tests.
    internal static Func<string, Stopwatch, int, bool, DbInfo, Task<GeoInfo?>> DbLookup = MmdbLookupAsync;

    private static readonly Regex Ipv4 = new(@"^(?:\d{1,3}\.){3}\d{1,3}$");
    private static readonly Regex Ipv6 = new(@"^[0-9a-fA-F:]+$");

    /// Whole-resolution budget in ms: CLEARCOTE_GEOIP_TIMEOUT_SECONDS (seconds, &gt; 0), default 20s.
    public static int TimeoutMs(string? envValue = null)
    {
        var raw = (envValue ?? Environment.GetEnvironmentVariable("CLEARCOTE_GEOIP_TIMEOUT_SECONDS") ?? "").Trim();
        return raw.Length > 0 && double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var n)
               && double.IsFinite(n) && n > 0
            ? (int)Math.Round(n * 1000)
            : 20_000;
    }

    private static bool LooksLikeIp(string s) => Ipv4.IsMatch(s) || (s.Contains(':') && Ipv6.IsMatch(s));

    private static async Task<string> GetTextAsync(HttpClient client, string url, int timeoutMs)
    {
        using var cts = new CancellationTokenSource(Math.Max(1, timeoutMs));
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.UserAgent.ParseAdd("clearcote-sdk");
        using var res = await client.SendAsync(req, cts.Token).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode) throw new HttpRequestException($"HTTP {(int)res.StatusCode}");
        return (await res.Content.ReadAsStringAsync(cts.Token).ConfigureAwait(false)).Trim();
    }

    private static async Task<string?> ExitIpAsync(HttpClient client, Stopwatch sw, int budget)
    {
        foreach (var url in EchoUrls())
        {
            var left = budget - (int)sw.ElapsedMilliseconds;
            if (left <= 0) break;
            try
            {
                var ip = IpFromEcho(await GetTextAsync(client, url, Math.Min(left, 8000)).ConfigureAwait(false));
                if (LooksLikeIp(ip)) return ip;
            }
            catch { /* try next */ }
        }
        return null;
    }

    private static async Task<GeoInfo?> IpApiAsync(HttpClient client, Stopwatch sw, int budget)
    {
        var left = budget - (int)sw.ElapsedMilliseconds;
        if (left <= 0) return null;
        try
        {
            using var doc = JsonDocument.Parse(await GetTextAsync(client, IpApiUrl, Math.Min(left, 8000)).ConfigureAwait(false));
            var r = doc.RootElement;
            string? S(string k) => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
            if (S("status") != "success") return null;
            var cc = S("countryCode");
            string? loc = r.TryGetProperty("lat", out var lat) && lat.ValueKind == JsonValueKind.Number
                          && r.TryGetProperty("lon", out var lon) && lon.ValueKind == JsonValueKind.Number
                ? $"{lat.GetDouble().ToString(CultureInfo.InvariantCulture)},{lon.GetDouble().ToString(CultureInfo.InvariantCulture)}"
                : null;
            return new GeoInfo(S("query"), cc, S("timezone"), AcceptLanguageForCountry(cc), loc);
        }
        catch { return null; }
    }

    /// Resolve geo for the egress (through <paramref name="proxy"/> if given — HTTP or SOCKS5 — else
    /// direct), reporting why it failed. Never throws. Bounded by <paramref name="timeoutMs"/>
    /// (default <see cref="TimeoutMs"/>).
    public static async Task<GeoResult> ResolveDetailedAsync(ProxyOptions? proxy = null, bool quiet = false, int? timeoutMs = null)
    {
        var sw = Stopwatch.StartNew();
        var budget = timeoutMs ?? TimeoutMs();
        ProxySpec? spec;
        try { spec = ProxySpec.From(proxy); }
        catch (Exception e) { return new GeoResult(null, $"invalid proxy ({e.Message})", sw.ElapsedMilliseconds); }

        using var client = ProxiedHttp.Create(spec);
        var ip = await ExitIpAsync(client, sw, budget).ConfigureAwait(false);
        var info = new DbInfo();
        var geo = ip is not null ? await DbLookup(ip, sw, budget, quiet, info).ConfigureAwait(false) : null;
        var ipapi = IpApiEnabled();
        // Opt-in only: ip-api answers with the exit IP AND its geo in one round-trip through the proxy, but its
        // free endpoint is for non-commercial use.
        if (geo is not { Timezone.Length: > 0 } && ipapi) geo = await IpApiAsync(client, sw, budget).ConfigureAwait(false) ?? geo;
        if (geo is { Timezone.Length: > 0 })
        {
            if (!quiet) Console.Error.WriteLine($"[clearcote] geoip: {geo.Ip} -> {geo.Country} tz={geo.Timezone} lang={geo.AcceptLanguage}");
            return new GeoResult(geo, null, sw.ElapsedMilliseconds);
        }
        var dbReason = info.Db == "downloading"
            ? "the geoip database is still downloading (~52 MB, first use only); launch again in a moment or raise CLEARCOTE_GEOIP_TIMEOUT_SECONDS"
            : "the geoip database could not be downloaded";
        var reason = ip is not null && info.Db is not null && !ipapi
            ? $"{dbReason} -- or set {IpApiEnv}=1 to ask ip-api.com (non-commercial use only)"
            // The per-fetch cancellation timers tick in whole milliseconds, so a fetch cut off at the budget can hand
            // control back with the stopwatch still reading budget-1 ms: that is the timeout, not a dead echo.
            : sw.ElapsedMilliseconds >= budget - 1
                ? $"timed out after {Math.Round(budget / 1000.0)}s (CLEARCOTE_GEOIP_TIMEOUT_SECONDS)"
                : ip is null
                    ? $"could not determine the exit IP{(spec is not null ? " through the proxy" : "")}"
                    : geo is not null
                        ? $"no timezone for exit IP {ip}"
                        : $"no geo data for exit IP {ip}";
        return new GeoResult(geo, reason, sw.ElapsedMilliseconds);
    }

    /// Resolve geo for the egress. Never throws — returns null on failure.
    public static async Task<GeoInfo?> ResolveAsync(ProxyOptions? proxy = null, bool quiet = false, int? timeoutMs = null)
        => (await ResolveDetailedAsync(proxy, quiet, timeoutMs).ConfigureAwait(false)).Geo;

    /// Look the proxy's exit IP up once more. Returns the new IP when it differs from <paramref name="firstIp"/>
    /// (same address family: an IPv4 answer after an IPv6 one is a different echo, not a rotation), else
    /// null. Never throws.
    public static async Task<string?> CheckEgressDriftAsync(ProxyOptions? proxy, string? firstIp, int timeoutMs = 8000)
    {
        if (string.IsNullOrEmpty(proxy?.Server) || string.IsNullOrEmpty(firstIp)) return null;
        try
        {
            using var client = ProxiedHttp.Create(ProxySpec.From(proxy));
            var ip = await ExitIpAsync(client, Stopwatch.StartNew(), timeoutMs).ConfigureAwait(false);
            if (ip is not null && ip != firstIp && ip.Contains(':') == firstIp.Contains(':')) return ip;
        }
        catch (Exception) { /* never fails the launch */ }
        return null;
    }

    internal static string EgressDriftMessage(string firstIp, string newIp)
        => $"the proxy's exit IP changed between two lookups at launch ({firstIp} -> {newIp}): it rotates, so the " +
           $"timezone, language and WebRTC IP taken from {firstIp} will not match the exit sites see. Use a " +
           "sticky session (most providers take a session id in the proxy username).";

    // geoip resolves the exit once; a proxy that hands out a new exit per connection makes that answer
    // stale at once. Checked in the background (it never blocks or fails the launch), one warning.
    private static void WarnOnEgressDrift(ProxyOptions? proxy, string? firstIp, bool quiet)
    {
        if (quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN")) ||
            string.IsNullOrEmpty(proxy?.Server) || string.IsNullOrEmpty(firstIp)) return;
        _ = Task.Run(async () =>
        {
            var newIp = await CheckEgressDriftAsync(proxy, firstIp).ConfigureAwait(false);
            if (newIp is not null) Console.Error.WriteLine($"clearcote: warning: {EgressDriftMessage(firstIp!, newIp)}");
        });
    }

    /// Fill unset Timezone/AcceptLanguage/Location/WebrtcIp on <paramref name="fp"/> from the exit-IP geo.
    ///
    /// FAILS CLOSED: throws <see cref="GeoipException"/> when the region cannot be resolved, unless the
    /// caller set BOTH Timezone and AcceptLanguage explicitly (then it warns and returns).
    /// Test seam: the resolver <see cref="ApplyAsync"/> asks (<see cref="ResolveDetailedAsync"/>).
    internal static Func<ProxyOptions?, bool, int?, Task<GeoResult>> Resolver = ResolveDetailedAsync;

    public static async Task ApplyAsync(FingerprintOptions fp, ProxyOptions? proxy, bool quiet = false, int? timeoutMs = null)
    {
        var result = await Resolver(proxy, quiet, timeoutMs).ConfigureAwait(false);
        var geo = result.Geo;
        if (geo is null || string.IsNullOrEmpty(geo.Timezone))
        {
            if (!string.IsNullOrEmpty(fp.Timezone) && !string.IsNullOrEmpty(fp.AcceptLanguage))
            {
                if (!quiet) Console.Error.WriteLine($"clearcote: geoip could not resolve the region ({result.Reason}); using the explicit timezone and acceptLanguage.");
                return;
            }
            var whose = string.IsNullOrEmpty(proxy?.Server) ? "connection's" : "proxy's";
            throw new GeoipException(
                $"geoip: could not resolve the {whose} region ({result.Reason}). " +
                "Launching anyway would use this machine's clock and a default language. Fix the proxy, raise " +
                "CLEARCOTE_GEOIP_TIMEOUT_SECONDS, or pass Timezone and AcceptLanguage explicitly.");
        }
        if (string.IsNullOrEmpty(fp.Timezone)) fp.Timezone = geo.Timezone;
        if (string.IsNullOrEmpty(fp.AcceptLanguage) && !string.IsNullOrEmpty(geo.AcceptLanguage)) fp.AcceptLanguage = geo.AcceptLanguage;
        if (string.IsNullOrEmpty(fp.Location) && !string.IsNullOrEmpty(geo.Location)) fp.Location = geo.Location;
        if (string.IsNullOrEmpty(fp.WebrtcIp) && !string.IsNullOrEmpty(geo.Ip)) fp.WebrtcIp = geo.Ip;
        WarnOnEgressDrift(proxy, geo.Ip, quiet);
    }

    // country (ISO-3166 alpha-2) -> the OS locale a machine there most plausibly runs. ONE tag, not a
    // list: the launch expands it the way Chrome does (DE -> de-DE -> UI "de", languages de-DE,de,en-US,en;
    // CA -> en-CA -> en-GB,en-US,en; MY -> ms-MY -> Intl "ms" with en-US,en). See Languages.ResolveLanguages.
    // Multi-language countries take the majority language (BE nl, CH de, CA en, IN en).
    public static readonly IReadOnlyDictionary<string, string> CountryLang = new Dictionary<string, string>
    {
        ["US"] = "en-US", ["GB"] = "en-GB", ["CA"] = "en-CA", ["AU"] = "en-AU", ["NZ"] = "en-NZ", ["IE"] = "en-IE",
        ["IN"] = "en-IN", ["ZA"] = "en-ZA", ["SG"] = "en-SG", ["PH"] = "en-PH",
        ["DE"] = "de-DE", ["AT"] = "de-AT", ["CH"] = "de-CH", ["FR"] = "fr-FR", ["BE"] = "nl-BE", ["NL"] = "nl-NL",
        ["ES"] = "es-ES", ["MX"] = "es-MX", ["AR"] = "es-AR", ["CL"] = "es-CL", ["CO"] = "es-CO", ["PE"] = "es-PE",
        ["VE"] = "es-VE", ["EC"] = "es-EC", ["UY"] = "es-UY", ["PT"] = "pt-PT", ["BR"] = "pt-BR",
        ["IT"] = "it-IT", ["PL"] = "pl-PL", ["RU"] = "ru-RU", ["UA"] = "uk-UA", ["SE"] = "sv-SE", ["NO"] = "nb-NO",
        ["DK"] = "da-DK", ["FI"] = "fi-FI", ["CZ"] = "cs-CZ", ["SK"] = "sk-SK", ["RO"] = "ro-RO", ["HU"] = "hu-HU",
        ["GR"] = "el-GR", ["BG"] = "bg-BG", ["HR"] = "hr-HR", ["SI"] = "sl-SI", ["RS"] = "sr-RS", ["LT"] = "lt-LT",
        ["LV"] = "lv-LV", ["EE"] = "et-EE", ["TR"] = "tr-TR", ["IL"] = "he-IL", ["SA"] = "ar-SA", ["AE"] = "ar-AE",
        ["EG"] = "ar-EG", ["JP"] = "ja-JP", ["KR"] = "ko-KR", ["CN"] = "zh-CN", ["HK"] = "zh-HK", ["TW"] = "zh-TW",
        ["TH"] = "th-TH", ["VN"] = "vi-VN", ["ID"] = "id-ID", ["MY"] = "ms-MY",
    };

    /// The OS-locale tag for an ISO country code ("DE" -> "de-DE"), en-US when unknown. It is a valid one-tag
    /// Accept-Language; the launch turns it into Chrome's own language list.
    public static string AcceptLanguageForCountry(string? cc)
        => cc is { Length: > 0 } && CountryLang.TryGetValue(cc.ToUpperInvariant(), out var v) ? v : "en-US";
}
