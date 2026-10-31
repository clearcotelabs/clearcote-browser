using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Clearcote;

/// <summary>
/// Autofill on, as in Chrome. The engine's own defaults (r54) have "Save and fill addresses" and "Save and fill
/// payment methods" off (<c>autofill.profile_enabled</c> / <c>autofill.credit_card_enabled</c> = false); Chrome 156
/// has both on. With addresses off, the browser also never remembers what was typed into single fields (emails,
/// names), so a profile behaved unlike a real Chrome. Measured on r54 (10-30): rows in Default/Web Data
/// <c>autofill</c> after a form submit: Chrome 2, r54 0, r54 with these prefs seeded 2.
/// </summary>
/// <remarks>Seeded into &lt;userDataDir&gt;/Default/Preferences before every launch on a profile directory the SDK
/// hands the browser (LaunchPersistentContextAsync, LaunchAsync's throwaway profile, ServeAsync). Only a missing key
/// is set: a profile where someone turned autofill off keeps it off. The .NET twin of sdk/node/src/autofillpref.ts
/// and sdk/python/clearcote/_autofillpref.py (and the Profile Manager's electron/autofillpref.ts).</remarks>
internal static class AutofillPrefs
{
    internal static readonly (string Key, bool Value)[] Defaults =
    {
        ("profile_enabled", true),
        ("credit_card_enabled", true),
    };

    private static readonly JsonSerializerOptions Compact = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    /// Merge Chrome's autofill defaults into &lt;userDataDir&gt;/Default/Preferences where they are missing.
    /// Returns the keys it set. A Preferences file that cannot be parsed is left alone.
    internal static List<string> Seed(string userDataDir)
    {
        var dir = Path.Combine(userDataDir, "Default");
        var file = Path.Combine(dir, "Preferences");
        JsonObject prefs;
        if (File.Exists(file))
        {
            try
            {
                if (JsonNode.Parse(File.ReadAllText(file)) is not JsonObject parsed) return new();
                prefs = parsed;
            }
            catch (Exception e) when (e is JsonException or IOException or UnauthorizedAccessException or ArgumentException or InvalidOperationException)
            {
                return new();   // the browser's file, not ours to replace
            }
        }
        else if (Directory.Exists(file))
        {
            return new();
        }
        else
        {
            prefs = new JsonObject();
        }
        if (prefs["autofill"] is not JsonObject autofill)
        {
            autofill = new JsonObject();
            prefs["autofill"] = autofill;
        }
        var set = Defaults.Where(d => !autofill.ContainsKey(d.Key)).ToList();
        if (set.Count == 0) return new();
        foreach (var (key, value) in set) autofill[key] = value;
        Directory.CreateDirectory(dir);
        File.WriteAllText(file, prefs.ToJsonString(Compact));
        return set.Select(d => d.Key).ToList();
    }

    /// <see cref="Seed"/> at launch: never fails a launch (an unwritable profile fails in Chrome anyway).
    internal static void Apply(string userDataDir, bool quiet)
    {
        try { Seed(userDataDir); }
        catch (Exception e)
        {
            if (!quiet) Console.Error.WriteLine($"[clearcote] could not turn autofill on in {userDataDir} (continuing): {e.Message}");
        }
    }
}
