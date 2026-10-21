using System.Reflection;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// A saved, launchable persona: a set of launch options (the fingerprint persona — seed, GPU, brand, … — AND
/// its <see cref="CanvasBridgeOptions"/>) under a name you can persist and re-launch as one coherent identity.
/// Port of the Node SDK's <c>profile.ts</c>.
/// <code>
/// new Profile("acct-1", new LaunchOptions
/// {
///     Fingerprint = "acct-1",
///     GpuVendor = "Google Inc. (Intel)",
///     GpuRenderer = "ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
///     CanvasBridge = new() { Url = "ws://127.0.0.1:9099", Auth = "user:secret" },
/// }).Save();
///
/// var browser = await Profile.Load("acct-1").LaunchAsync(new() { Headless = false });
/// // or: await Clearcote.LaunchAsync(new() { LocalProfile = "acct-1" });
/// </code>
/// </summary>
/// <remarks>
/// <para>Profiles are plain JSON at <c>~/.clearcote/profiles/&lt;name&gt;.json</c> (override the directory with
/// CLEARCOTE_PROFILE_DIR), in the same format the Node and Python SDKs read and write: <c>{"name", "options"}</c>,
/// the options keyed by the SDK option names. Fingerprint option keys are normalized on load (a Python-written
/// profile's <c>gpu_renderer</c> reads as <c>gpuRenderer</c>), and every option key maps onto the
/// <see cref="LaunchOptions"/> property of the same name in any of the three spellings.</para>
/// <para>SECURITY: a profile is a plaintext file that may hold credentials (e.g. <c>canvasBridge.auth</c>) — it is
/// written 0600 but is NOT encrypted; do not commit or share it. Treat profile names and profile files as trusted
/// input (a loaded profile can set any launch option).</para>
/// </remarks>
public sealed class Profile
{
    private static readonly Regex SafeName = new("^[A-Za-z0-9][A-Za-z0-9._-]*$");

    /// Directory saved profiles live in: CLEARCOTE_PROFILE_DIR, else ~/.clearcote/profiles.
    public static string ProfileDir =>
        Environment.GetEnvironmentVariable("CLEARCOTE_PROFILE_DIR") is { Length: > 0 } dir
            ? dir
            : System.IO.Path.Combine(Native.ClearcoteDir, "profiles");

    public string Name { get; set; }

    /// The saved options, keyed as in the file (fingerprint keys camelCase).
    public JsonObject Options { get; }

    public Profile(string name, JsonObject? options = null)
    {
        Name = name;
        Options = options is null ? new JsonObject() : (JsonObject)options.DeepClone();
    }

    /// A profile holding every option <paramref name="options"/> sets (the properties that differ from a new
    /// <see cref="LaunchOptions"/>), under their camelCase names, as the Node SDK writes them.
    public Profile(string name, LaunchOptions options) : this(name) => Set(options);

    /// Merge in more options; returns this for chaining.
    public Profile Set(JsonObject options)
    {
        foreach (var (k, v) in options) Options[k] = v?.DeepClone();
        return this;
    }

    /// Merge in every option <paramref name="options"/> sets; returns this for chaining.
    public Profile Set(LaunchOptions options) => Set(ProfileOptionsMap.ToJson(options));

    /// <c>~/.clearcote/profiles/&lt;name&gt;.json</c> (or the explicit path the name resolves to).
    public string Path => ProfilePath(Name);

    /// Persist as JSON (defaults to <see cref="Path"/>). Returns the file path. The directory is created 0700 and
    /// the file written 0600 (it may hold secrets).
    public string Save(string? path = null)
    {
        var dest = path ?? Path;
        var parent = System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(dest))!;
        Directory.CreateDirectory(parent);
        var json = new JsonObject { ["name"] = Name, ["options"] = Options.DeepClone() }
            .ToJsonString(new JsonSerializerOptions { WriteIndented = true, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping }) + "\n";
        if (OperatingSystem.IsWindows())
        {
            File.WriteAllText(dest, json);
        }
        else
        {
            // private (0600) from the start, so a credential is never briefly world-readable
            using (var fs = new FileStream(dest, new FileStreamOptions
                   {
                       Mode = FileMode.Create, Access = FileAccess.Write,
                       UnixCreateMode = UnixFileMode.UserRead | UnixFileMode.UserWrite,
                   }))
            using (var w = new StreamWriter(fs))
                w.Write(json);
            try
            {
                File.SetUnixFileMode(parent, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
                File.SetUnixFileMode(dest, UnixFileMode.UserRead | UnixFileMode.UserWrite);
            }
            catch { /* best-effort */ }
        }
        return dest;
    }

    /// Load a saved profile by name (under <see cref="ProfileDir"/>) or by explicit path.
    public static Profile Load(string nameOrPath)
    {
        var data = JsonNode.Parse(File.ReadAllText(ProfilePath(nameOrPath))) as JsonObject ?? new JsonObject();
        var baseName = nameOrPath.Split('/', '\\')[^1];
        var name = data["name"] is JsonValue n && n.TryGetValue<string>(out var s) && s.Length > 0
            ? s
            : baseName.EndsWith(".json", StringComparison.Ordinal) ? baseName[..^5] : baseName;
        return new Profile(name, NormalizeKeys(data["options"] as JsonObject ?? new JsonObject()));
    }

    /// Names of the saved profiles under <see cref="ProfileDir"/>.
    public static IReadOnlyList<string> ListProfiles()
    {
        var dir = ProfileDir;
        if (!Directory.Exists(dir)) return Array.Empty<string>();
        return Directory.GetFiles(dir, "*.json")
            .Select(f => System.IO.Path.GetFileName(f)[..^5])
            .OrderBy(n => n, StringComparer.Ordinal)
            .ToList();
    }

    /// These options as a <see cref="LaunchOptions"/>. An option this SDK has no property for, or a value it
    /// cannot read, is left out.
    public LaunchOptions ToLaunchOptions()
    {
        var o = new LaunchOptions();
        ProfileOptionsMap.Apply(o, Options, onlyUnset: false);
        return o;
    }

    /// Launch this persona; the options set in <paramref name="overrides"/> win over the saved ones.
    public Task<IBrowser> LaunchAsync(LaunchOptions? overrides = null)
        => global::Clearcote.Clearcote.LaunchAsync(With(overrides));

    /// Launch this persona on a throwaway profile directory (see <see cref="Clearcote.LaunchEphemeralProfileAsync"/>).
    public Task<IBrowserContext> LaunchEphemeralProfileAsync(LaunchOptions? overrides = null)
        => global::Clearcote.Clearcote.LaunchEphemeralProfileAsync(With(overrides));

    /// Launch this persona with a persistent profile directory.
    public Task<IBrowserContext> LaunchPersistentContextAsync(string userDataDir, LaunchOptions? overrides = null)
        => global::Clearcote.Clearcote.LaunchPersistentContextAsync(userDataDir, With(overrides));

    private LaunchOptions With(LaunchOptions? overrides)
    {
        var o = (LaunchOptions)(overrides ?? new LaunchOptions()).Clone();
        o.LocalProfile = this;
        return o;
    }

    public override string ToString() => $"Profile(\"{Name}\", {Options.Count} options)";

    internal static string ProfilePath(string nameOrPath)
    {
        // a path (has a separator, or ends in .json) is used verbatim; a bare name must be a safe slug (no
        // separators, no "..") so an untrusted name cannot traverse out of the profile directory.
        if (nameOrPath.Contains(System.IO.Path.DirectorySeparatorChar) || nameOrPath.Contains('/')
            || nameOrPath.EndsWith(".json", StringComparison.Ordinal))
            return nameOrPath;
        if (!SafeName.IsMatch(nameOrPath))
            throw new ArgumentException($"invalid profile name '{nameOrPath}' — use [A-Za-z0-9._-] (or pass an explicit path)");
        return System.IO.Path.Combine(ProfileDir, nameOrPath + ".json");
    }

    // FingerprintOptions property names, camelCase: Node's FINGERPRINT_KEYS.
    private static readonly HashSet<string> FingerprintKeys = typeof(FingerprintOptions)
        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .Select(p => char.ToLowerInvariant(p.Name[0]) + p.Name[1..])
        .ToHashSet(StringComparer.Ordinal);

    /// Map snake_case fingerprint keys (e.g. a Python-written profile) to camelCase, as the Node SDK does.
    internal static JsonObject NormalizeKeys(JsonObject options)
    {
        var o = new JsonObject();
        foreach (var (k, v) in options)
        {
            var camel = Regex.Replace(k, "_([a-z0-9])", m => m.Groups[1].Value.ToUpperInvariant());
            o[FingerprintKeys.Contains(camel) ? camel : k] = v?.DeepClone();
        }
        return o;
    }
}

/// <summary>
/// The local launch's persona selector (<see cref="LaunchOptions.LocalProfile"/>): a saved <see cref="Profile"/>
/// by name or path, a <see cref="Profile"/> instance, or <c>"auto"</c>.
/// </summary>
/// <remarks>
/// <c>"auto"</c> is special: instead of a saved option set, it resolves a REAL CAPTURED FINGERPRINT for this machine
/// — the licensed profile service first, a local imported directory as backup — and applies it as
/// <see cref="FingerprintOptions.FingerprintProfile"/> with NO seed (see <see cref="ProfileAuto"/>). A string
/// converts: <c>LocalProfile = "acct-1"</c>, <c>LocalProfile = "auto"</c>.
/// </remarks>
public sealed class LocalProfile
{
    private LocalProfile(string? name, Profile? saved) { Name = name; Saved = saved; }

    /// The name or path given (null for a <see cref="Profile"/> instance).
    public string? Name { get; }
    /// The <see cref="Profile"/> instance given (null for a name or path).
    public Profile? Saved { get; }
    /// <c>"auto"</c>: resolve a captured fingerprint for this host.
    public bool IsAuto => Saved is null && Name == "auto";

    /// <c>"auto"</c>.
    public static LocalProfile Auto { get; } = new("auto", null);

    public static implicit operator LocalProfile(string nameOrPath)
    {
        if (string.IsNullOrEmpty(nameOrPath)) throw new ArgumentException("a local profile needs a name, a path or \"auto\"");
        return nameOrPath == "auto" ? Auto : new LocalProfile(nameOrPath, null);
    }

    public static implicit operator LocalProfile(Profile profile)
        => new(null, profile ?? throw new ArgumentNullException(nameof(profile)));

    /// The saved (camelCase-normalized) options this selects. Never called for "auto".
    internal JsonObject ResolveOptions() => Saved is not null ? Profile.NormalizeKeys(Saved.Options) : Profile.Load(Name!).Options;

    public override string ToString() => Saved is not null ? Saved.Name : Name!;
}

/// Saved profile options (JSON, any SDK's spelling) to and from <see cref="LaunchOptions"/> properties.
internal static class ProfileOptionsMap
{
    // Never read from or written to a saved profile: the selector itself, and the cloud's cookie store and client.
    private static readonly string[] Skipped = { "LocalProfile", "ProfileSelect", "Profile", "CloudClient" };

    private static readonly Dictionary<string, PropertyInfo> Props = typeof(LaunchOptions)
        .GetProperties(BindingFlags.Public | BindingFlags.Instance)
        .Where(p => p.CanWrite && !Skipped.Contains(p.Name))
        .ToDictionary(p => p.Name.ToLowerInvariant(), StringComparer.Ordinal);

    private static readonly JsonSerializerOptions Read = new()
    {
        PropertyNameCaseInsensitive = true,
        Converters = { new JsonStringEnumConverter() },
    };

    private static readonly JsonSerializerOptions Write = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        Converters = { new JsonStringEnumConverter(JsonNamingPolicy.CamelCase) },
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    // "gpu_renderer", "gpuRenderer" and "GpuRenderer" all name GpuRenderer.
    private static string Squash(string key) => key.Replace("_", "").ToLowerInvariant();

    /// Set <paramref name="target"/>'s properties from <paramref name="saved"/>. <paramref name="onlyUnset"/>: only
    /// those still at their default (the caller's explicit options win). Returns the keys that were not used: no
    /// property of that name, or a value it cannot hold.
    internal static List<string> Apply(LaunchOptions target, JsonObject saved, bool onlyUnset)
    {
        var unused = new List<string>();
        var defaults = new LaunchOptions();
        foreach (var (key, node) in saved)
        {
            var squashed = Squash(key);
            if (node is null || squashed is "profile" or "profileselect") continue;   // the caller's selector wins
            if (!Props.TryGetValue(squashed, out var p)) { unused.Add(key); continue; }
            if (onlyUnset && !Equals(p.GetValue(target), p.GetValue(defaults))) continue;
            if (!TryConvert(node, p.PropertyType, out var value)) { unused.Add(key); continue; }
            p.SetValue(target, value);
        }
        return unused;
    }

    private static bool TryConvert(JsonNode node, Type type, out object? value)
    {
        value = null;
        try
        {
            var t = Nullable.GetUnderlyingType(type) ?? type;
            if (t == typeof(object))
            {
                value = node is JsonValue v && v.TryGetValue<string>(out var str) ? str : node.DeepClone();   // FingerprintProfile: a path/JSON string or the object
                return true;
            }
            if (t == typeof(string) && node is JsonValue sv)
            {
                value = sv.TryGetValue<string>(out var s) ? s : sv.ToJsonString();   // a numeric seed reads as its text
                return true;
            }
            if (t == typeof(ProxyOptions) && node is JsonValue pv && pv.TryGetValue<string>(out var server))
            {
                value = new ProxyOptions { Server = server };
                return true;
            }
            value = node.Deserialize(type, Read);
            return true;
        }
        catch (Exception)
        {
            return false;   // a value this SDK cannot hold: reported, never a launch failure
        }
    }

    /// The options <paramref name="o"/> sets (the properties that differ from a new LaunchOptions), camelCase.
    internal static JsonObject ToJson(LaunchOptions o)
    {
        var json = new JsonObject();
        var defaults = new LaunchOptions();
        foreach (var p in Props.Values.OrderBy(p => p.MetadataToken))
        {
            var value = p.GetValue(o);
            if (value is null || Equals(value, p.GetValue(defaults))) continue;
            var key = char.ToLowerInvariant(p.Name[0]) + p.Name[1..];
            json[key] = value is JsonNode n ? n.DeepClone() : JsonSerializer.SerializeToNode(value, value.GetType(), Write);
        }
        return json;
    }

    /// Profile = a saved persona: its options fill whatever the caller left unset. ("auto" is not a saved option
    /// set — it resolves once the binary is known, in <see cref="ProfileAuto.ApplyAsync"/> — so it never reaches
    /// Profile.Load.) Every local entry point runs this, via Clearcote.PrepareAsync.
    internal static void MergeSaved(LaunchOptions o)
    {
        var local = ProfileAuto.LocalProfileOf(o);
        if (local is null || local.IsAuto) return;
        var unused = Apply(o, local.ResolveOptions(), onlyUnset: true);
        if (unused.Count > 0 && !o.Quiet)
            Console.Error.WriteLine($"[clearcote] [profile] saved profile '{local}': ignored {string.Join(", ", unused)} " +
                                    "(no LaunchOptions property of that name, or a value it cannot hold)");
    }
}
