using System.Globalization;
using System.IO.Compression;
using System.Text;
using System.Text.Json;

namespace Clearcote;

/// Canvas-bridge configuration — forward canvas/WebGL readbacks to a remote real-GPU host.
public sealed class CanvasBridgeOptions
{
    /// Bridge endpoint, "ws://host:port[/path]". Required to enable the bridge.
    public string? Url { get; set; }
    /// HTTP Basic credentials "user:secret"; must match the server.
    public string? Auth { get; set; }
    /// Per-origin policy: "off" | "all" (default) | "allow" | "deny".
    public string? Mode { get; set; }
    /// eTLD+1 list bridged when mode="allow".
    public string[]? Allow { get; set; }
    /// eTLD+1 list NOT bridged when mode="deny".
    public string[]? Deny { get; set; }
    /// Cold cache-miss behavior: "block" (default) | "local".
    public string? Fallback { get; set; }
}

/// The SDK's fingerprint/persona options. Mirrors the Node FingerprintOptions and maps to the
/// Clearcote Chromium command-line switches (see <see cref="Fingerprint.Args"/>).
public class FingerprintOptions
{
    /// Master fingerprint seed (per-eTLD+1 farbling root). Same seed =&gt; same identity across launches.
    public string? Fingerprint { get; set; }
    /// Spoofed OS family for UA / UA-CH ("windows" | "linux" | "macos" | "android"). "android" is a
    /// best-effort mobile persona.
    public string? Platform { get; set; }
    /// Spoofed platform version (UA-CH high-entropy).
    public string? PlatformVersion { get; set; }
    /// Browser brand for UA / UA-CH ("Chrome" | "Edge" | "Opera" | "Vivaldi" | ...).
    public string? Brand { get; set; }
    /// Brand version.
    public string? BrandVersion { get; set; }
    /// TLS network persona: "match-persona" (default) | "auto" | "native" | "off" | "chrome-&lt;major&gt;" | a major number.
    public string? TlsProfile { get; set; }
    /// WebGL UNMASKED_VENDOR string.
    public string? GpuVendor { get; set; }
    /// WebGL UNMASKED_RENDERER string.
    public string? GpuRenderer { get; set; }
    /// navigator.hardwareConcurrency.
    public int? HardwareConcurrency { get; set; }
    /// Geolocation as "lat,lng" (only returned when the page is granted permission).
    public string? Location { get; set; }
    /// IANA timezone, e.g. "America/New_York".
    public string? Timezone { get; set; }
    /// Accept-Language / navigator.languages, e.g. "en-US,en" (sets both header and navigator coherently).
    public string? AcceptLanguage { get; set; }
    /// WebRTC egress IP to report (typically your proxy's public IP).
    public string? WebrtcIp { get; set; }
    /// Present the machine's real GPU instead of a spoofed one (most coherent vs strict classifiers).
    public bool? DisableGpuFingerprint { get; set; }
    /// Set false to turn OFF per-eTLD+1 farbling noise (canvas/WebGL/audio/client-rects).
    public bool? FingerprintNoise { get; set; }
    /// Import a real captured fingerprint: a path to a .json profile, a JSON string, or an object.
    public object? FingerprintProfile { get; set; }
    /// navigator.storage.estimate().quota in MEGABYTES.
    public int? StorageQuota { get; set; }
    /// Canvas bridge config (set Url to enable; auto-adds --no-sandbox).
    public CanvasBridgeOptions? CanvasBridge { get; set; }
}

/// Maps fingerprint options to Clearcote Chromium switches (ports fingerprint.ts).
public static class Fingerprint
{
    /// Primary Accept-Language tag -> a plausible IANA timezone (so the default persona's timezone is
    /// coherent with its locale instead of leaking the host's UTC).
    private static readonly Dictionary<string, string> LocaleTz = new()
    {
        ["en-US"] = "America/New_York", ["en-CA"] = "America/Toronto", ["en-GB"] = "Europe/London",
        ["en-AU"] = "Australia/Sydney", ["en-NZ"] = "Pacific/Auckland", ["en-IE"] = "Europe/Dublin",
        ["de-DE"] = "Europe/Berlin", ["de-AT"] = "Europe/Vienna", ["fr-FR"] = "Europe/Paris",
        ["es-ES"] = "Europe/Madrid", ["es-MX"] = "America/Mexico_City", ["it-IT"] = "Europe/Rome",
        ["nl-NL"] = "Europe/Amsterdam", ["pt-BR"] = "America/Sao_Paulo", ["pt-PT"] = "Europe/Lisbon",
        ["pl-PL"] = "Europe/Warsaw", ["sv-SE"] = "Europe/Stockholm", ["ja-JP"] = "Asia/Tokyo",
        ["ko-KR"] = "Asia/Seoul", ["zh-CN"] = "Asia/Shanghai", ["zh-TW"] = "Asia/Taipei",
        ["ru-RU"] = "Europe/Moscow", ["tr-TR"] = "Europe/Istanbul", ["ar-SA"] = "Asia/Riyadh",
        ["hi-IN"] = "Asia/Kolkata", ["id-ID"] = "Asia/Jakarta",
    };

    /// Normalize an Accept-Language for Chromium's --accept-lang: a plain comma-separated tag list with
    /// NO ";q=" weights or spaces (a ";" in the switch value trips a DCHECK and crashes the renderer).
    public static string CleanAcceptLanguage(string v)
        => string.Join(",", v.Split(',')
            .Select(t => t.Split(';')[0].Trim())
            .Where(t => t.Length > 0));

    /// A plausible IANA timezone for a primary Accept-Language tag ("en-US" -> "America/New_York").
    public static string? DefaultTimezone(string primaryLang)
    {
        if (string.IsNullOrEmpty(primaryLang)) return null;
        var tag = primaryLang.Trim();
        if (LocaleTz.TryGetValue(tag, out var tz)) return tz;
        var lang = tag.Split('-')[0].ToLowerInvariant();
        foreach (var (key, value) in LocaleTz)
            if (key.ToLowerInvariant().StartsWith(lang + "-")) return value;
        return "America/New_York";
    }

    /// gzip+base64-encode a captured clearcote-profile for --fingerprint-profile. `value` may be a path
    /// to a .json file, a JSON string, or an object (serialized to JSON).
    public static string EncodeProfile(object value)
    {
        byte[] raw;
        if (value is string s)
            raw = File.Exists(s) ? File.ReadAllBytes(s) : Encoding.UTF8.GetBytes(s);
        else
            raw = JsonSerializer.SerializeToUtf8Bytes(value);
        using var ms = new MemoryStream();
        using (var gz = new GZipStream(ms, CompressionLevel.SmallestSize, leaveOpen: true))
            gz.Write(raw, 0, raw.Length);
        return Convert.ToBase64String(ms.ToArray());
    }

    /// Best-effort: derive an Accept-Language from an imported profile's navigator.languages.
    public static string? ProfileAcceptLanguage(object value)
    {
        try
        {
            string json = value switch
            {
                string s when File.Exists(s) => File.ReadAllText(s),
                string s => s,
                _ => JsonSerializer.Serialize(value),
            };
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("navigator", out var nav)
                && nav.TryGetProperty("languages", out var langs)
                && langs.ValueKind == JsonValueKind.Array && langs.GetArrayLength() > 0)
            {
                var list = langs.EnumerateArray().Select(e => e.ToString()).Where(x => x.Length > 0);
                var joined = string.Join(",", list);
                return joined.Length > 0 ? joined : null;
            }
        }
        catch { /* ignore */ }
        return null;
    }

    private static int? MajorFromVersion(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        var head = value.Trim().Split('.')[0];
        return int.TryParse(head, out var n) ? n : null;
    }

    /// Resolve tlsProfile to a concrete "chrome-&lt;major&gt;" value, or null (native TLS).
    public static string? ResolveTlsProfile(string? value, FingerprintOptions o)
    {
        if (string.IsNullOrEmpty(value) || value is "native" or "off") return null;
        if (value is "match-persona" or "auto")
        {
            var major = MajorFromVersion(o.BrandVersion);
            return major is { } m ? $"chrome-{m}" : null;
        }
        var text = value.Trim().ToLowerInvariant();
        if (System.Text.RegularExpressions.Regex.IsMatch(text, @"^chrome-\d+$")) return text;
        if (System.Text.RegularExpressions.Regex.IsMatch(text, @"^\d+$")) return $"chrome-{text}";
        return null;
    }

    private static string HostPlatform =>
        Native.OsTag switch { "windows" => "windows", "linux" => "linux", "macos" => "macos", _ => "windows" };

    /// Build the Chromium switches for a set of fingerprint options (ports fingerprintArgs()).
    public static List<string> Args(FingerprintOptions o)
    {
        var args = new List<string>();
        void Set(string flag, object? value)
        {
            if (value is null) return;
            var s = value is IFormattable f ? f.ToString(null, CultureInfo.InvariantCulture) : value.ToString();
            if (!string.IsNullOrEmpty(s)) args.Add($"--{flag}={s}");
        }

        Set("fingerprint", o.Fingerprint);
        Set("fingerprint-platform", o.Platform ?? HostPlatform);
        Set("fingerprint-platform-version", o.PlatformVersion);
        Set("fingerprint-brand", o.Brand ?? "chrome");
        Set("fingerprint-brand-version", o.BrandVersion);
        Set("fingerprint-gpu-vendor", o.GpuVendor);
        Set("fingerprint-gpu-renderer", o.GpuRenderer);
        Set("fingerprint-hardware-concurrency", o.HardwareConcurrency);
        Set("fingerprint-location", o.Location);
        Set("fingerprint-storage-quota", o.StorageQuota);
        Set("timezone", o.Timezone);

        // Always send a coherent Accept-Language: explicit > imported-profile languages > en-US,en.
        var acceptLanguage = o.AcceptLanguage;
        if (string.IsNullOrEmpty(acceptLanguage) && o.FingerprintProfile is not null)
            acceptLanguage = ProfileAcceptLanguage(o.FingerprintProfile);
        if (string.IsNullOrEmpty(acceptLanguage)) acceptLanguage = "en-US,en";
        var cleanLang = CleanAcceptLanguage(acceptLanguage);
        args.Add($"--accept-lang={cleanLang}");

        // Pin the UI/ICU locale to the primary tag so Intl resolves to the same locale as navigator.language.
        var primaryLang = cleanLang.Split(',')[0];
        if (!string.IsNullOrEmpty(primaryLang)) args.Add($"--lang={primaryLang}");

        // Default a locale-coherent timezone when none is set (avoid leaking host UTC on servers).
        if (string.IsNullOrEmpty(o.Timezone))
        {
            var tz = DefaultTimezone(primaryLang);
            if (tz is not null) args.Add($"--timezone={tz}");
        }

        Set("webrtc-ip", o.WebrtcIp);
        if (o.DisableGpuFingerprint == true) args.Add("--disable-gpu-fingerprint");
        if (o.FingerprintNoise == false) args.Add("--disable-fingerprint-noise");
        if (o.FingerprintProfile is not null) args.Add($"--fingerprint-profile={EncodeProfile(o.FingerprintProfile)}");

        if (o.CanvasBridge?.Url is { Length: > 0 } cbUrl)
        {
            var cb = o.CanvasBridge;
            args.Add($"--canvas-bridge-url={cbUrl}");
            if (!string.IsNullOrEmpty(cb.Auth)) args.Add($"--canvas-bridge-auth={cb.Auth}");
            if (!string.IsNullOrEmpty(cb.Mode)) args.Add($"--canvas-bridge-mode={cb.Mode}");
            if (cb.Allow is { Length: > 0 }) args.Add($"--canvas-bridge-allow={string.Join(",", cb.Allow)}");
            if (cb.Deny is { Length: > 0 }) args.Add($"--canvas-bridge-deny={string.Join(",", cb.Deny)}");
            if (!string.IsNullOrEmpty(cb.Fallback)) args.Add($"--canvas-bridge-fallback={cb.Fallback}");
            if (!args.Contains("--no-sandbox")) args.Add("--no-sandbox");
        }

        var tlsSwitch = ResolveTlsProfile(o.TlsProfile ?? "match-persona", o);
        if (tlsSwitch is not null) args.Add($"--fingerprint-tls-profile={tlsSwitch}");

        if (o.Platform == "android") args.Add("--window-size=412,915");
        return args;
    }
}
