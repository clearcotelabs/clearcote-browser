using System.Text.Json;
using System.Text.Json.Nodes;

namespace Clearcote;

/// <summary>
/// Canvas/WebGL noise mode per profile folder: one noise per profile for NEW profiles, per-site noise kept for
/// profiles that already have a history (SDK 0.70.5). Matches sdk/node/src/noisemode.ts and _noisemode.py.
/// </summary>
/// <remarks>
/// With per-site noise (the engine default) a third-party frame (a captcha or anti-bot widget) reads a different
/// canvas on every site that embeds it, which no real device does; with one noise per profile it reads one canvas
/// everywhere, still different per profile. Switching an EXISTING profile would show every site a new canvas on its
/// next launch, so the mode is decided once per folder and remembered in <c>&lt;userDataDir&gt;/clearcote.json</c>:
/// <list type="bullet">
/// <item><see cref="FingerprintOptions.NoisePerProfile"/> set: used, and remembered for later launches that leave it unset.</item>
/// <item>unset, folder remembered: its remembered mode.</item>
/// <item>unset, new folder (missing, empty, or no "Local State" / "Default" yet): per profile when the engine
/// implements it (r60+), else per site; remembered either way.</item>
/// <item>unset, folder with Chrome data but nothing remembered (made before 0.70.5): per site, remembered.</item>
/// </list>
/// Throwaway launches are new by definition. A pass-through launch (Fingerprint = "off") is left alone.
/// </remarks>
public static class NoiseMode
{
    public const string ProfileStateFile = "clearcote.json";
    public const string PerProfile = "per-profile";
    public const string PerSite = "per-site";

    /// The mode remembered in the folder, or null.
    public static string? Read(string userDataDir)
    {
        try
        {
            var node = JsonNode.Parse(File.ReadAllText(Path.Combine(userDataDir, ProfileStateFile))) as JsonObject;
            var mode = node?["noise"] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
            return mode is PerProfile or PerSite ? mode : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException)
        {
            return null;
        }
    }

    /// True when the folder already holds a Chrome profile (the browser writes these on its first run).
    public static bool HasChromeData(string userDataDir)
        => Directory.Exists(userDataDir)
           && (File.Exists(Path.Combine(userDataDir, "Local State")) || Directory.Exists(Path.Combine(userDataDir, "Default")));

    /// Remember the mode, keeping any other keys in the file. Throws on an unwritable folder.
    public static void Write(string userDataDir, string mode)
    {
        var path = Path.Combine(userDataDir, ProfileStateFile);
        JsonObject s = new();
        try { if (JsonNode.Parse(File.ReadAllText(path)) is JsonObject cur) s = cur; }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException or JsonException) { }
        if (s["noise"] is JsonValue v && v.TryGetValue<string>(out var old) && old == mode) return;
        s["noise"] = mode;
        File.WriteAllText(path, s.ToJsonString(new JsonSerializerOptions { WriteIndented = true }) + "\n");
    }

    /// <summary>The noise mode for this launch (true = one noise per profile). <paramref name="userDataDir"/> null = a
    /// launch with no folder, which is new. Remembers the result in the folder, fail-soft.</summary>
    public static bool? Resolve(bool? requested, string? userDataDir, string? exe = null, bool passthrough = false,
        bool quiet = false, Func<string?, bool>? supports = null)
    {
        if (passthrough) return requested;
        supports ??= e => LaunchOpts.EngineSupportsSwitch(e, "fingerprint-noise-per-profile");
        bool perProfile;
        if (requested is bool r) perProfile = r;
        else
        {
            var remembered = userDataDir is null ? null : Read(userDataDir);
            if (remembered is not null) perProfile = remembered == PerProfile;
            else if (userDataDir is not null && HasChromeData(userDataDir)) perProfile = false;  // made before 0.70.5
            else perProfile = supports(exe);
        }
        if (userDataDir is not null)
        {
            try
            {
                // A folder that does not exist yet is created here (Playwright would create it anyway): the browser
                // writes "Local State" on its first run, after which an unremembered folder would read as old.
                Directory.CreateDirectory(userDataDir);
                Write(userDataDir, perProfile ? PerProfile : PerSite);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                if (!quiet) Console.Error.WriteLine($"[clearcote] could not remember the noise mode in {userDataDir} (continuing): {e.Message}");
            }
        }
        return perProfile;
    }

    /// Set <see cref="FingerprintOptions.NoisePerProfile"/> on the prepared copy of the options, once the engine is known.
    internal static void Apply(LaunchOptions o, string? userDataDir, string exe)
        => o.NoisePerProfile = Resolve(o.NoisePerProfile, userDataDir, exe,
            passthrough: Fingerprint.IsFingerprintPassthrough(o.Fingerprint), quiet: o.Quiet);
}
