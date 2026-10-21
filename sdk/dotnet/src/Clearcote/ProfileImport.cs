using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Clearcote;

/// A file <see cref="ProfileImport.ImportDirectory"/> could not use, and why.
public sealed record ImportSkip(string File, string Reason);

/// What <see cref="ProfileImport.ImportDirectory"/> found.
public sealed class ImportResult
{
    public List<ProfileIndexEntry> Index { get; } = new();
    /// Files that were not usable, with the reason — surfaced rather than silently dropped, so a half-converted
    /// directory is visible instead of quietly producing a thin pool.
    public List<ImportSkip> Skipped { get; } = new();
}

/// <summary>
/// Generic profile importer: turn a DIRECTORY of clearcote-profile JSON into a selectable index. Port of the Node
/// SDK's <c>profileimport.ts</c>.
/// </summary>
/// <remarks>
/// WE ARE AN IMPORTER, NOT A HOST. Clearcote ships no third-party fingerprints and redistributes none. A source is
/// just a folder of clearcote-profile JSON on the user's own machine, however it got there: a public dataset the
/// user fetched and converted locally (tools/fingerprint-collect/convert_dataset.py), your own captures from
/// tools/fingerprint-collect, the licensed profile service (see <see cref="ProfileSource"/>), or any other producer
/// that emits the same schema. Keeping the format as the ONLY contract is what makes this generic.
/// </remarks>
public static class ProfileImport
{
    private const RegexOptions I = RegexOptions.IgnoreCase | RegexOptions.CultureInvariant;

    // JavaScript truthiness of a JSON value.
    private static bool Truthy(JsonNode? n) => n switch
    {
        null => false,
        JsonValue v when v.TryGetValue<bool>(out var b) => b,
        JsonValue v when v.TryGetValue<string>(out var s) => s.Length > 0,
        JsonValue v => ProfileJson.Num(v) is double d && d != 0 && !double.IsNaN(d),
        _ => true,
    };

    private static string Text(JsonNode? n) => n is null ? "" : ProfileJson.Str(n) ?? "";

    /// Parse a clearcote-profile blob into an index entry. Returns null when the file is not one.
    public static ProfileIndexEntry? IndexEntryFromProfile(string id, JsonObject profile)
    {
        var nav = profile["navigator"] as JsonObject ?? new JsonObject();
        var screen = profile["screen"] as JsonObject ?? new JsonObject();
        var debug = ((profile["webgl"] as JsonObject)?["webgl1"] as JsonObject)?["debug"] as JsonObject ?? new JsonObject();
        var meta = profile["meta"] as JsonObject ?? new JsonObject();

        var ua = Text(nav["user_agent"]);
        var platform = Text(nav["platform"]);
        // UA-CH. Checked FIRST because it is the only source present on some real inputs: converted dataset records
        // carry `uadata.platform` while leaving `user_agent` and `platform` null (the converter deliberately drops the
        // browser version). Reading only the UA string silently skipped every one of those.
        var uadata = nav["uadata"] as JsonObject ?? new JsonObject();
        var highEntropy = uadata["high_entropy"] as JsonObject;
        var chPlatform = Text(uadata["platform"] ?? highEntropy?["platform"]);
        var chMobile = Truthy(uadata["mobile"] ?? highEntropy?["mobile"]);

        string os;
        if (chMobile || Regex.IsMatch(chPlatform, "android", I) || Regex.IsMatch(ua, "android", I)) os = "android";
        else if (Regex.IsMatch(chPlatform, "windows|win", I) || Regex.IsMatch(platform, "win", I) || Regex.IsMatch(ua, "Windows", I)) os = "windows";
        else if (Regex.IsMatch(chPlatform, "macos|mac", I) || Regex.IsMatch(platform, "mac", I) || Regex.IsMatch(ua, "Mac OS X", I)) os = "macos";
        else if (Regex.IsMatch(chPlatform, "linux|chrome ?os|x11", I) || Regex.IsMatch(platform, "linux|x11", I) || Regex.IsMatch(ua, "Linux", I)) os = "linux";
        else return null;   // unclassifiable: better to skip than to mislabel and mis-serve it

        // Version is OPTIONAL by design. Hardware-only imports (e.g. converted dataset records) carry no browser
        // version and inherit the engine's — see ProfileIndexEntry.BrowserMajor.
        var m = Regex.Match(ua, @"Chrome/(\d+)");
        int? browserMajor = m.Success && int.TryParse(m.Groups[1].Value, out var major) ? major : null;

        var renderer = debug["UNMASKED_RENDERER_WEBGL"] is { } r ? ProfileJson.Str(r) : null;

        return new ProfileIndexEntry
        {
            Id = id,
            OsFamily = os,
            BrowserMajor = browserMajor,
            GpuVendor = ProfileLib.GpuVendorClass(renderer),
            Renderer = renderer,
            ScreenWidth = ProfileJson.Int(screen["width"]) ?? 0,
            ScreenHeight = ProfileJson.Int(screen["height"]) ?? 0,
            DevicePixelRatio = ProfileJson.Num(screen["device_pixel_ratio"]),
            HardwareConcurrency = ProfileJson.Int(profile["hardware_concurrency"]),
            DeviceMemory = ProfileJson.Int(profile["device_memory"]),
            // The launch cost, measured exactly as Fingerprint.EncodeProfile encodes it for --fingerprint-profile, so
            // an entry that would blow the argv limit is excluded at selection time rather than failing at spawn.
            EncodedSize = Fingerprint.EncodeProfile(profile).Length,
            CapturedAt = meta["captured_at"] is JsonValue c && c.TryGetValue<string>(out var at) ? at : null,
        };
    }

    /// Index every <c>*.json</c> in a directory. Reads each file once: for 10k profiles that is a few seconds and a
    /// lot of I/O, so callers cache the result (see <see cref="ProfileAuto.LoadLocalIndex"/>) rather than re-scanning
    /// per launch.
    public static ImportResult ImportDirectory(string dir)
    {
        if (!Directory.Exists(dir)) throw new DirectoryNotFoundException($"profile source is not a directory: {dir}");
        var result = new ImportResult();
        foreach (var path in Directory.GetFiles(dir).OrderBy(p => p, StringComparer.Ordinal))
        {
            var f = Path.GetFileName(path);
            if (!f.EndsWith(".json", StringComparison.Ordinal)) continue;
            JsonNode? parsed;
            try
            {
                parsed = JsonNode.Parse(File.ReadAllText(path));
            }
            catch (Exception e)
            {
                var msg = e.Message;
                result.Skipped.Add(new ImportSkip(f, $"unreadable JSON: {(msg.Length > 80 ? msg[..80] : msg)}"));
                continue;
            }
            // A clearcote-profile always has navigator; without it this is some other kind of file. The check is on
            // the SECTION, not on any field inside it — a converted record has `navigator` with null
            // user_agent/platform and is perfectly usable via uadata.
            if (parsed is not JsonObject obj || !Truthy(obj["navigator"]))
            {
                result.Skipped.Add(new ImportSkip(f, "not a clearcote-profile (no navigator)"));
                continue;
            }
            var entry = IndexEntryFromProfile(f[..^5], obj);
            if (entry is null)
            {
                result.Skipped.Add(new ImportSkip(f, "could not classify os_family"));
                continue;
            }
            result.Index.Add(entry);
        }
        return result;
    }

    /// Load one profile from an imported directory, by the id <see cref="ImportDirectory"/> assigned it.
    public static JsonObject LoadImportedProfile(string dir, string id)
    {
        // The id is a filename stem this class produced, but it can reach here from a cached index, so treat it as
        // untrusted: reject anything that could escape the source directory.
        if (id.Contains('/') || id.Contains('\\') || id.Contains("..")) throw new ArgumentException($"invalid profile id '{id}'");
        var path = Path.Combine(dir, id + ".json");
        if (!File.Exists(path)) throw new FileNotFoundException($"profile '{id}' not found in {dir}", path);
        return JsonNode.Parse(File.ReadAllText(path)) as JsonObject
               ?? throw new InvalidOperationException($"profile '{id}' in {dir} is not a JSON object");
    }
}
