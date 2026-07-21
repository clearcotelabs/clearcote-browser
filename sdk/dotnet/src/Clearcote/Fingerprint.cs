using System.Globalization;
using System.IO.Compression;
using System.Numerics;
using System.Security.Cryptography;
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
    /// navigator.deviceMemory in GB (spec-clamps to 8 — larger values report as 8).
    public int? DeviceMemory { get; set; }
    /// screen.width in CSS px. NOTE: spoofing screen dimensions is a reliable block trigger on strict
    /// anti-bots (a faked screen cannot be reconciled with the real window/render surface), so this is
    /// opt-in and is NOT part of the LightStealth preset. Best when the host's real display matches.
    public int? ScreenWidth { get; set; }
    /// screen.height in CSS px (see the caveat on <see cref="ScreenWidth"/>).
    public int? ScreenHeight { get; set; }
    /// screen.availWidth in CSS px (see the caveat on <see cref="ScreenWidth"/>).
    public int? AvailWidth { get; set; }
    /// screen.availHeight in CSS px (see the caveat on <see cref="ScreenWidth"/>).
    public int? AvailHeight { get; set; }
    /// screen.colorDepth (e.g. 24).
    public int? ColorDepth { get; set; }
    /// window.devicePixelRatio (e.g. 1, 1.25, 1.5).
    public double? DevicePixelRatio { get; set; }
    /// navigator.maxTouchPoints (0 on a mouse-only desktop).
    public int? MaxTouchPoints { get; set; }
    /// Light-stealth preset: spoof a coherent, seed-derived bundle of the metadata axes that SURVIVE
    /// strict anti-bot checks — HardwareConcurrency, DeviceMemory, ColorDepth, DevicePixelRatio,
    /// MaxTouchPoints — applied via the native override switches ONLY (never the --fingerprint persona
    /// machinery / farbling that strict anti-bots detect). Rendering (canvas/WebGL/audio/fonts), TLS,
    /// and the real Chrome version are all left untouched, so the identity stays coherent and passes.
    /// Screen dimensions are deliberately NOT spoofed (a faked screen is a reliable block trigger) —
    /// set ScreenWidth/etc. explicitly to opt in. An explicit field wins over the preset.
    public bool? LightStealth { get; set; }
    /// Geolocation as "lat,lng" (only returned when the page is granted permission).
    public string? Location { get; set; }
    /// IANA timezone, e.g. "America/New_York".
    public string? Timezone { get; set; }
    /// Accept-Language / navigator.languages, e.g. "en-US,en" (sets both header and navigator coherently).
    public string? AcceptLanguage { get; set; }
    /// WebRTC egress IP to report (typically your proxy's public IP).
    public string? WebrtcIp { get; set; }
    /// WebRTC host-candidate mDNS concealment. Real Chrome hides local host candidates behind an
    /// &lt;uuid&gt;.local name so a page opening an RTCPeerConnection cannot read the LAN address; that
    /// is the default here too. Set "off" only if you need routable raw host candidates (LAN/P2P) —
    /// it re-exposes the private IP to every page. Requires an engine built with enable_mdns.
    public string? WebrtcMdns { get; set; }
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

    /// Shallow copy, so applying the LightStealth preset never mutates the caller's object.
    internal FingerprintOptions Clone() => (FingerprintOptions)MemberwiseClone();
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

    // Coherent Windows-plausible desktop/laptop metadata bundles, indexed by a hash of the seed:
    // (screen_w, screen_h, avail_w, avail_h, dpr, color_depth, device_memory_gb, hw_concurrency).
    // LightStealth uses ONLY dpr/color_depth/device_memory/hw_concurrency (screen stays real).
    private static readonly (int Sw, int Sh, int Aw, int Ah, double Dpr, int Cd, int Mem, int Hw)[] LightStealthProfiles =
    {
        (1920, 1080, 1920, 1040, 1.0, 24, 8, 8),
        (1920, 1080, 1920, 1040, 1.0, 24, 16, 12),
        (1920, 1080, 1920, 1040, 1.0, 24, 16, 16),
        (2560, 1440, 2560, 1400, 1.0, 24, 16, 16),
        (2560, 1440, 2560, 1400, 1.5, 24, 16, 12),
        (1536, 864, 1536, 824, 1.25, 24, 8, 8),
        (1536, 864, 1536, 824, 1.25, 24, 16, 12),
        (1366, 768, 1366, 728, 1.0, 24, 8, 4),
        (1366, 768, 1366, 728, 1.0, 24, 4, 4),
        (1440, 900, 1440, 860, 1.0, 24, 8, 8),
        (1600, 900, 1600, 860, 1.0, 24, 8, 8),
        (1680, 1050, 1680, 1010, 1.0, 24, 8, 8),
        (1920, 1200, 1920, 1160, 1.0, 24, 16, 12),
        (3840, 2160, 3840, 2120, 1.0, 24, 32, 16),
    };

    /// Deterministic, coherent metadata bundle for LightStealth: spoofs ONLY the axes that survive
    /// strict anti-bot checks (HardwareConcurrency, DeviceMemory, ColorDepth, DevicePixelRatio,
    /// MaxTouchPoints), never screen. The seed->row mapping matches the Python/Node SDKs (full sha256
    /// digest as a big integer mod N).
    public static FingerprintOptions LightStealthValues(string? seed)
    {
        var key = string.IsNullOrEmpty(seed) ? "clearcote-light-stealth" : seed;
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(key));
        var big = new BigInteger(digest, isUnsigned: true, isBigEndian: true);
        var idx = (int)(big % LightStealthProfiles.Length);
        var row = LightStealthProfiles[idx];
        return new FingerprintOptions
        {
            DevicePixelRatio = row.Dpr,
            ColorDepth = row.Cd,
            DeviceMemory = row.Mem,
            HardwareConcurrency = row.Hw,
            MaxTouchPoints = 0,
            Brand = "chrome",
        };
    }

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
        // Apply the LightStealth preset first, on a copy (explicit fields win; never emit --fingerprint).
        if (o.LightStealth == true)
        {
            o = o.Clone();
            var preset = LightStealthValues(o.Fingerprint);
            o.HardwareConcurrency ??= preset.HardwareConcurrency;
            o.DeviceMemory ??= preset.DeviceMemory;
            o.ColorDepth ??= preset.ColorDepth;
            o.DevicePixelRatio ??= preset.DevicePixelRatio;
            o.MaxTouchPoints ??= preset.MaxTouchPoints;
            if (string.IsNullOrEmpty(o.Brand)) o.Brand = preset.Brand;
            o.Fingerprint = null; // never emit --fingerprint, so the persona machinery never engages
        }

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
        // Native metadata overrides (flag > persona > real). Read directly by the getters — no
        // --fingerprint persona machinery — so they are safe to spoof individually or via LightStealth.
        Set("fingerprint-device-memory", o.DeviceMemory);
        Set("fingerprint-screen-width", o.ScreenWidth);
        Set("fingerprint-screen-height", o.ScreenHeight);
        Set("fingerprint-avail-width", o.AvailWidth);
        Set("fingerprint-avail-height", o.AvailHeight);
        Set("fingerprint-color-depth", o.ColorDepth);
        Set("fingerprint-device-pixel-ratio", o.DevicePixelRatio);
        Set("fingerprint-max-touch-points", o.MaxTouchPoints);
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
        // Only "off" is meaningful — concealment ON is both the Chromium default and real Chrome's
        // behaviour, so there is nothing to emit for "on". Uses Chromium's own feature flag rather
        // than a clearcote switch: the mDNS responder is created behind kWebRtcHideLocalIpsWithMdns,
        // so disabling the feature means no responder is built and host candidates are signalled as
        // raw IPs. MergeFeatureFlags folds this into any other --disable-features value.
        if (string.Equals(o.WebrtcMdns, "off", StringComparison.OrdinalIgnoreCase))
            args.Add("--disable-features=WebRtcHideLocalIpsWithMdns");
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
