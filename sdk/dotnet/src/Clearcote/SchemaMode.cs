using System.Text.Json;
using System.Text.Json.Nodes;

namespace Clearcote;

/// <summary>
/// Persona schema per profile folder: schema 8 for NEW profiles, the schema a profile was made with kept for profiles
/// that already have a history (SDK 0.70.6). Matches sdk/node/src/schemamode.ts and _schemamode.py.
/// </summary>
/// <remarks>
/// Schema 8 is the persona service's Windows machine model re-weighted on the 2026-11 /audit corpus (screen sizes and
/// scaling, memory, CPU/memory pairs and touchscreens closer to real Windows machines; gen-156-s8b). The service default
/// stays 7, and switching an EXISTING profile would give it a new screen, memory and touch value on its next launch, so
/// the schema is decided once per folder and remembered in <c>&lt;userDataDir&gt;/clearcote.json</c> (key "schema"):
/// <list type="bullet">
/// <item><see cref="FingerprintOptions.PersonaSchema"/> set, or <c>--fingerprint-schema=N</c> in Args: used, and
/// remembered for later launches that leave it unset.</item>
/// <item>unset, folder remembered: its remembered schema.</item>
/// <item>unset, new folder (missing, empty, or no "Local State" / "Default" yet): 8 when the engine takes its persona
/// from the service (156 r51+), else 7; remembered either way.</item>
/// <item>unset, folder with Chrome data but nothing remembered (made before 0.70.6): 7, remembered.</item>
/// </list>
/// A folder on 7 launches exactly as before (no switch is added). Throwaway launches are new by definition. A
/// pass-through launch (Fingerprint = "off") is left alone.
/// </remarks>
public static class SchemaMode
{
    public const int NewProfileSchema = 8;
    public const int EarlierProfileSchema = 7;  // the persona service's default since 2026-10-26

    static int? Norm(string? s) => s is { Length: 1 } && s[0] is >= '1' and <= '9' ? s[0] - '0' : null;

    /// The schema remembered in the folder, or null.
    public static int? Read(string userDataDir)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(Path.Combine(userDataDir, NoiseMode.ProfileStateFile))) as JsonObject;
            return node?["schema"] is JsonValue v && v.TryGetValue<string>(out var s) ? Norm(s) : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            return null;
        }
    }

    /// Remember the schema, keeping any other keys in the file. Throws on an unwritable folder.
    public static void Write(string userDataDir, int schema)
    {
        var path = Path.Combine(userDataDir, NoiseMode.ProfileStateFile);
        var value = schema.ToString(System.Globalization.CultureInfo.InvariantCulture);
        JsonObject s = new();
        try { if (JsonNode.Parse(File.ReadAllText(path)) is JsonObject cur) s = cur; }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException or JsonException) { }
        if (s["schema"] is JsonValue v && v.TryGetValue<string>(out var old) && old == value) return;
        s["schema"] = value;
        File.WriteAllText(path, s.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n");
    }

    /// The last <c>--fingerprint-schema=N</c> in a switch list, or null.
    public static int? SchemaInArgs(IEnumerable<string>? args)
    {
        string? found = null;
        foreach (var a in args ?? Array.Empty<string>())
            if (a is not null && a.StartsWith("--fingerprint-schema=", StringComparison.Ordinal)) found = a["--fingerprint-schema=".Length..];
        return found is null ? null : Norm(found);
    }

    /// <summary>The <see cref="FingerprintOptions.PersonaSchema"/> for this launch: the value to set, or null to add
    /// nothing. <paramref name="userDataDir"/> null = a launch with no folder, which is new. Remembers the schema in the
    /// folder, fail-soft. Returns an explicit <paramref name="requested"/> unchanged; null when <paramref name="args"/>
    /// already pin a schema or the schema is 7 (the service default: the launch stays byte-identical to an earlier SDK's).</summary>
    public static int? Resolve(int? requested, string? userDataDir, string? exe = null, IEnumerable<string>? args = null,
        bool passthrough = false, bool quiet = false, Func<string?, bool>? supports = null)
    {
        if (passthrough) return requested;
        // The persona service (156 r51+) is where schema 8 lives; an older engine derives its own identity.
        supports ??= e => LaunchOpts.EngineSupportsSwitch(e, LaunchOpts.PersonaServiceMarker);
        var pinned = SchemaInArgs(args);
        int schema;
        if (requested is int r) schema = r;
        else if (pinned is int p) schema = p;
        else
        {
            var remembered = userDataDir is null ? null : Read(userDataDir);
            if (remembered is int m) schema = m;
            else if (userDataDir is not null && NoiseMode.HasChromeData(userDataDir)) schema = EarlierProfileSchema;  // made before 0.70.6
            else schema = supports(exe) ? NewProfileSchema : EarlierProfileSchema;
        }
        if (userDataDir is not null)
        {
            try
            {
                Directory.CreateDirectory(userDataDir);
                Write(userDataDir, schema);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                if (!quiet) Console.Error.WriteLine($"[clearcote] could not remember the persona schema in {userDataDir} (continuing): {e.Message}");
            }
        }
        if (requested is not null) return requested;
        if (pinned is not null || schema == EarlierProfileSchema) return null;
        return schema;
    }

    /// Set <see cref="FingerprintOptions.PersonaSchema"/> on the prepared copy of the options, once the engine is known.
    internal static void Apply(LaunchOptions o, string? userDataDir, string exe)
    {
        var ps = Resolve(o.PersonaSchema, userDataDir, exe, o.Args,
            passthrough: Fingerprint.IsFingerprintPassthrough(o.Fingerprint), quiet: o.Quiet);
        if (ps is not null) o.PersonaSchema = ps;
    }
}
