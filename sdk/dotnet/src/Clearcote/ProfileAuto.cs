using System.Globalization;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

/// Where an <c>"auto"</c> persona came from.
public enum ProfileOrigin
{
    /// The licensed profile service.
    Service,
    /// The local imported directory (<see cref="ProfileAuto.DefaultLocalDir"/>).
    Local,
}

/// Options of <c>LocalProfile = "auto"</c> (<see cref="LaunchOptions.ProfileSelect"/>): selection, the service, and
/// the local backup directory.
public class AutoOptions : ProfileSourceOptions
{
    /// Directory of clearcote-profile JSON used when the service is unavailable (default <see cref="ProfileAuto.DefaultLocalDir"/>).
    public string? LocalDir { get; set; }
    /// Skip the service entirely and use the local directory.
    public bool? PreferLocal { get; set; }
    /// No notes on stderr (which source was used, the fallback reason, the one-time tip).
    public bool? Quiet { get; set; }
}

/// The outcome of <see cref="ProfileAuto.ResolveAutoAsync"/>.
public sealed class AutoResult
{
    /// The captured fingerprint, applied as <see cref="FingerprintOptions.FingerprintProfile"/>.
    public JsonObject Profile { get; init; } = new();
    public ProfileSelection Selection { get; init; } = new();
    public ProfileOrigin Source { get; init; }
    /// Why the service was not used, when <see cref="Source"/> is Local.
    public string? FallbackReason { get; init; }
}

/// <summary>
/// <c>LocalProfile = "auto"</c>: resolve a persona from the best source available. Port of the Node SDK's
/// <c>profileauto.ts</c> (and of its launch wiring in <c>index.ts</c>).
/// </summary>
/// <remarks>
/// <para>ORDER: the licensed clearcote service first, a local imported directory as backup. The service is preferred
/// because its corpus is fresher, larger and curated. But it can be unreachable for reasons that are none of the
/// caller's fault — no license, an exhausted daily budget, a rate limit, a network blip, an outage — and in every
/// one of those cases a local profile is enormously better than no persona. So the fallback is by design.</para>
/// <para>THE FALLBACK IS ALWAYS ANNOUNCED. A silent downgrade would leave a caller believing they are on a fresh
/// service profile while they are on a stale local one. <see cref="AutoResult.Source"/> is set on every result and
/// a one-line reason is written to stderr unless Quiet.</para>
/// <para>Unlike the Node SDK this never downloads and converts a dataset on the fallback path (as the Python SDK):
/// with no local directory it prints how to set one up.</para>
/// </remarks>
public static class ProfileAuto
{
    /// Where an imported directory is looked for by default: CLEARCOTE_PROFILE_SOURCE, else ~/.clearcote/profiles/imported.
    public static string DefaultLocalDir =>
        Environment.GetEnvironmentVariable("CLEARCOTE_PROFILE_SOURCE") is { Length: > 0 } dir
            ? dir
            : Path.Combine(Native.ClearcoteDir, "profiles", "imported");

    /// <summary>First engine major that implements <c>--fingerprint-profile</c>.</summary>
    /// <remarks>
    /// BACKWARDS COMPATIBILITY, AND WHY THIS IS A HARD GATE. The free 149 build has no persona-profile patch at all,
    /// so it does not merely apply the profile badly — it ignores the switch entirely. Chromium discards unknown
    /// switches silently, so a 149 user who asked for a profile would launch with NO persona while believing they
    /// had one. That is strictly worse than the seed farbling 149 does support, because the failure is invisible.
    /// So a profile is never sent to an engine that cannot read it: such a launch falls back to the seed path.
    /// </remarks>
    public const int MinProfileEngineMajor = 150;

    /// Whether this engine can actually apply an imported profile.
    public static bool EngineSupportsProfiles(int? browserMajor) => browserMajor is int m && m >= MinProfileEngineMajor;

    private static void Note(bool? quiet, string msg)
    {
        if (quiet != true) Console.Error.WriteLine($"[clearcote] [profile] {msg}");
    }

    // ── local index cache ──────────────────────────────────────────────────────────────────────
    // Indexing a directory means reading every file (10k profiles is a few seconds of I/O), far too slow to repeat
    // per launch. The cache is keyed by directory and invalidated by its mtime, so adding or converting more
    // profiles is picked up without a manual step.

    private static double MtimeMs(string dir) => (Directory.GetLastWriteTimeUtc(dir) - DateTime.UnixEpoch).TotalMilliseconds;

    /// Index <paramref name="dir"/>, cached in <c>&lt;CLEARCOTE_PROFILE_DIR&gt;/library/local-index.json</c> and
    /// invalidated by the directory's mtime.
    public static IReadOnlyList<ProfileIndexEntry> LoadLocalIndex(string dir)
    {
        var mtimeMs = MtimeMs(dir);
        var path = Path.Combine(ProfileSource.CacheDir(), "local-index.json");
        if (File.Exists(path))
        {
            try
            {
                if (JsonNode.Parse(File.ReadAllText(path)) is JsonObject c && ProfileJson.Str(c["dir"]) == dir
                    && ProfileJson.Num(c["mtimeMs"]) == mtimeMs)
                    return ProfileJson.Entries(c["index"]);
            }
            catch { /* fall through and re-index */ }
        }
        var index = ProfileImport.ImportDirectory(dir).Index;
        try
        {
            var arr = new JsonArray(index.Select(e => (JsonNode?)ProfileJson.EntryTo(e)).ToArray());
            File.WriteAllText(path, new JsonObject { ["dir"] = dir, ["mtimeMs"] = mtimeMs, ["index"] = arr }.ToJsonString());
        }
        catch { /* a cache write failure must never fail a launch */ }
        return index;
    }

    // The optional-backup tip is worth saying once, not on every launch.
    private static bool _hintedThisProcess;

    /// Test seam: reset the once-per-process tip.
    internal static void ResetHint() => _hintedThisProcess = false;

    /// <summary>How to populate a local backup directory. Printed when the service is unavailable and no local
    /// directory exists — the moment the caller actually needs it.</summary>
    /// <remarks>OPTIONAL, AND NOT SOMETHING WE SHIP. Clearcote redistributes no third-party fingerprints; the dataset
    /// is fetched by the user from its own repository under its own terms, and we only read the converted format. Any
    /// directory of clearcote-profile JSON works just as well, which is why this is phrased as one example.</remarks>
    public static string LocalSetupHint(string dir) => string.Join("\n",
        "A local profile directory is optional, and gives \"auto\" a backup when the service is",
        "unavailable. Any folder of clearcote-profile JSON works — your own captures, or a",
        "third-party dataset you convert yourself. For example, using the open chrome-fingerprints",
        "dataset (fetched by you, from its own repo, under its own licence):",
        "",
        "    pip install chrome-fingerprints",
        $"    python tools/fingerprint-collect/convert_dataset.py --out \"{dir}\" --count 500",
        "",
        "Or capture your own with tools/fingerprint-collect (collect.html), or point",
        "CLEARCOTE_PROFILE_SOURCE at a directory you already have.");

    /// Resolve a persona from the local imported directory.
    public static AutoResult ResolveLocal(HostFacts host, AutoOptions? options = null)
    {
        options ??= new AutoOptions();
        var dir = options.LocalDir ?? DefaultLocalDir;
        if (!Directory.Exists(dir))
            throw new InvalidOperationException($"no local profile directory at {dir}\n\n{LocalSetupHint(dir)}");
        var index = LoadLocalIndex(dir);
        if (index.Count == 0)
            throw new InvalidOperationException($"local profile directory {dir} contains no usable clearcote-profile JSON");
        var selection = ProfileLib.SelectProfile(index, host, options);
        return new AutoResult { Profile = ProfileImport.LoadImportedProfile(dir, selection.Entry.Id), Selection = selection, Source = ProfileOrigin.Local };
    }

    private static string Reason(Exception e) => e.Message.Length > 200 ? e.Message[..200] : e.Message;

    /// <summary>Resolve <c>"auto"</c>: service first, local directory as backup.</summary>
    /// <remarks>Any service failure falls back — including 401 (no license) and 429 (rate limited or daily distinct
    /// budget exhausted), because in all of those a local persona is better than none and far better than a hard
    /// launch failure. When both fail, the error names BOTH.</remarks>
    public static async Task<AutoResult> ResolveAutoAsync(HostFacts host, AutoOptions? options = null)
    {
        options ??= new AutoOptions();
        var dir = options.LocalDir ?? DefaultLocalDir;
        if (options.PreferLocal == true)
        {
            var r = ResolveLocal(host, options);
            Note(options.Quiet, $"using local profile {r.Selection.Entry.Id} (PreferLocal)");
            return r;
        }

        string reason;
        try
        {
            var index = await ProfileSource.FetchIndexAsync(host, options).ConfigureAwait(false);
            if (index.Count == 0) throw new InvalidOperationException("service returned no candidates for this host");
            var selection = ProfileLib.SelectProfile(index, host, options);
            var profile = await ProfileSource.FetchProfileAsync(selection.Entry.Id, options).ConfigureAwait(false);
            Note(options.Quiet, $"using service profile {selection.Entry.Id} " +
                                $"(score {selection.Score.ToString("F2", CultureInfo.InvariantCulture)}, pool {selection.PoolSize}, mode {selection.Mode.ToString().ToLowerInvariant()})");
            // Mention the optional backup ONCE per process, and only while there is none configured. The useful
            // moment to learn about a fallback is before you need it — but repeating it on every launch would be
            // noise, and nagging someone who already set one up would be wrong.
            if (!_hintedThisProcess && !Directory.Exists(dir))
            {
                _hintedThisProcess = true;
                Note(options.Quiet, "tip: no local backup directory configured. If the service is ever rate limited or\n" +
                                    "unreachable, \"auto\" fails rather than falling back. Setting one up is optional:\n" +
                                    LocalSetupHint(dir));
            }
            return new AutoResult { Profile = profile, Selection = selection, Source = ProfileOrigin.Service };
        }
        catch (Exception serviceErr)
        {
            reason = Reason(serviceErr);
        }

        try
        {
            var r = ResolveLocal(host, options);
            // Both halves of the story: what failed, and what is being used instead.
            Note(options.Quiet, $"service unavailable ({reason}) — falling back to local profile {r.Selection.Entry.Id}");
            return new AutoResult { Profile = r.Profile, Selection = r.Selection, Source = r.Source, FallbackReason = reason };
        }
        catch (Exception localErr)
        {
            var noLocalDir = !Directory.Exists(dir);
            // Report BOTH failures. Surfacing only the local one would send the caller to go and fix a directory when
            // the real problem was an expired license.
            throw new InvalidOperationException(
                "LocalProfile = \"auto\" could not resolve a persona.\n" +
                $"  service: {reason}\n" +
                $"  local:   {(noLocalDir ? $"no directory at {dir}" : localErr.Message)}\n\n" +
                (noLocalDir ? LocalSetupHint(dir) : ""), localErr);
        }
    }

    // ── Windows 11 faces ───────────────────────────────────────────────────────────────────────

    /// <summary>Families every Windows 11 install has (Microsoft's Windows 11 font list marks them "Added in
    /// Windows 11").</summary>
    /// <remarks>The capture collector never probed them, so no corpus donor carries them; a Windows 11 donor would
    /// otherwise hide fonts no Windows 11 machine lacks (a font set with HoloLens MDL2 Assets but without Segoe Fluent
    /// Icons reads as Windows 10, contradicting a Windows 11 platformVersion). Cascadia Code/Mono are deliberately
    /// absent: they ship per user with Windows Terminal. So is the bare "Segoe UI Variable": genuine Chrome 154 on
    /// Windows 11 reports that family absent (only the Display/Small/Text faces resolve). Listing a family never
    /// over-claims on a Windows host (host ∩ list).</remarks>
    public static readonly IReadOnlyList<string> Windows11Faces = new[]
    {
        "Segoe Fluent Icons", "Segoe UI Variable Display", "Segoe UI Variable Small", "Segoe UI Variable Text",
    };

    /// True when a UA-CH platformVersion claims Windows 11 (major &gt;= 13).
    public static bool ClaimsWindows11(string? platformVersion)
    {
        var head = (platformVersion ?? "").Trim().Split('.')[0];
        return Regex.IsMatch(head, "^[0-9]+$") && double.Parse(head, CultureInfo.InvariantCulture) >= 13;
    }

    // JavaScript `a || b || c` over JSON values, as a string.
    private static string FirstTruthy(params JsonNode?[] nodes)
    {
        foreach (var n in nodes)
        {
            if (n is null) continue;
            if (n is JsonValue v && v.TryGetValue<string>(out var s)) { if (s.Length > 0) return s; continue; }
            if (n is JsonValue b && b.TryGetValue<bool>(out var flag)) { if (flag) return "true"; continue; }
            if (n is JsonValue num && ProfileJson.Num(num) is double d) { if (d != 0) return ProfileJson.Str(num)!; continue; }
            return n.ToJsonString();
        }
        return "";
    }

    /// A captured profile whose fonts gain <see cref="Windows11Faces"/> when it claims Windows 11. For profiles the
    /// SDK picked itself (<c>"auto"</c>); returns the input unchanged otherwise, and a copy when it changes anything,
    /// so a cached corpus entry is never mutated.
    public static JsonNode? ProfileWithWindows11Faces(JsonNode? profile)
    {
        if (profile is not JsonObject p) return profile;
        var nav = p["navigator"] as JsonObject;
        var uadata = nav?["uadata"] as JsonObject;
        var hints = uadata?["high_entropy"] as JsonObject;
        var platform = FirstTruthy(hints?["platform"], uadata?["platform"], nav?["platform"]);
        var detected = (p["fonts"] as JsonObject)?["detected"] as JsonArray;
        var version = hints?["platformVersion"] is { } pv ? ProfileJson.Str(pv) : null;
        if (!platform.ToLowerInvariant().StartsWith("win", StringComparison.Ordinal) || detected is null || !ClaimsWindows11(version))
            return profile;
        var have = detected.Select(f => (f is null ? "null" : ProfileJson.Str(f) ?? "").ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
        var copy = (JsonObject)p.DeepClone();
        var list = (JsonArray)((JsonObject)copy["fonts"]!)["detected"]!;
        foreach (var face in Windows11Faces)
            if (!have.Contains(face.ToLowerInvariant())) list.Add(face);
        return copy;
    }

    // ── launch wiring (index.ts applyProfileAuto / applyAutoProfile) ───────────────────────────

    /// Test seams: the nested host-probe launch, the host measurement, and the resolver.
    internal static Func<LaunchOptions, Task<IBrowser>> ProbeLaunch { get; set; } = o => global::Clearcote.Clearcote.LaunchAsync(o);
    internal static Func<Func<LaunchOptions, Task<IBrowser>>, string, int, Task<HostFacts>> MeasureHost { get; set; } = ProfileSource.MeasureHostAsync;
    internal static Func<HostFacts, AutoOptions, Task<AutoResult>> Resolve { get; set; } = ResolveAutoAsync;

    internal static void ResetSeams()
    {
        ProbeLaunch = o => global::Clearcote.Clearcote.LaunchAsync(o);
        MeasureHost = ProfileSource.MeasureHostAsync;
        Resolve = ResolveAutoAsync;
    }

    /// The local launch's persona selector: <see cref="LaunchOptions.LocalProfile"/>, else a
    /// <see cref="LaunchOptions.Profile"/> named "auto" (the Node spelling, <c>Profile = "auto"</c>; a cloud
    /// profile can never be called that, so it is unambiguous). Any other <see cref="LaunchOptions.Profile"/> names a
    /// cloud cookie store and stays a cloud-only option.
    internal static LocalProfile? LocalProfileOf(LaunchOptions o)
        => o.LocalProfile ?? (o.Profile is { Name: "auto", Persist: null } ? LocalProfile.Auto : null);

    /// The SDK's pinned engine major: the fallback when the probe cannot read the running one.
    private static int PinnedMajor()
    {
        var v = (Native.IsWindows ? Release.Windows : Release.Linux).Version;
        return int.TryParse(v.Split('.')[0], NumberStyles.Integer, CultureInfo.InvariantCulture, out var m) ? m : 0;
    }

    /// <summary>"auto" -&gt; resolve a REAL captured fingerprint for this host and apply it as FingerprintProfile, in
    /// place on the launch's own copy of the options. Called once <paramref name="exe"/> is known, because both the
    /// engine's Chromium major and the host GPU measurement depend on the binary that will actually run.</summary>
    /// <remarks>Deliberately does NOT set a seed: with no --fingerprint the farbling machinery stays off, which is the
    /// whole reason this path survives strict scoring. Pass-through (Fingerprint = "off") runs with NO persona, so
    /// "auto" has nothing to apply; an explicit FingerprintProfile always wins.</remarks>
    internal static async Task ApplyAsync(LaunchOptions o, string exe)
    {
        if (LocalProfileOf(o) is not { IsAuto: true } || Fingerprint.IsFingerprintPassthrough(o.Fingerprint)) return;
        var s = o.ProfileSelect;
        var opts = new AutoOptions
        {
            Quiet = s?.Quiet ?? o.Quiet,
            LicenseKey = s?.LicenseKey ?? License.ResolveLicenseKey(o.LicenseKey),
            // The profile service lives on the same backend as licensing, so a caller who overrode one has overridden
            // both; CLEARCOTE_PROFILE_API is still preferred over the default when neither is set.
            ApiBase = s?.ApiBase ?? o.LicenseApiBase,
            Offline = s?.Offline, LocalDir = s?.LocalDir, PreferLocal = s?.PreferLocal,
            Mode = s?.Mode, Key = s?.Key, TopN = s?.TopN, Os = s?.Os, MaxEncodedSize = s?.MaxEncodedSize,
        };
        await ApplyAutoProfileAsync(o, exe, opts).ConfigureAwait(false);
    }

    /// Resolve "auto" into a FingerprintProfile. Host GPU/display can only be read by rendering, so this may launch
    /// the engine once with NO persona and cache the result (keyed by binary, 30 days). The nested launch sets no
    /// LocalProfile, so it cannot recurse.
    private static async Task ApplyAutoProfileAsync(LaunchOptions o, string exe, AutoOptions opts)
    {
        // An explicit FingerprintProfile always wins: the caller already named one, so "auto" has nothing to decide.
        if (o.FingerprintProfile is not null) return;
        // THE NESTED LAUNCH NEEDS THE LICENSE TOO. It launches the SAME binary the real session will use; on PRO that
        // is the gated build, and with no run-token the engine gate kills it at startup — surfacing as a closed
        // target that names neither licensing nor a call the caller wrote. The incognito LaunchAsync: the probe reads
        // GPU/display off about:blank and needs no profile directory. Cloud and Docker off: the probe measures THIS
        // host. Widevine off: the CDM would only cost the probe a fetch. Geoip off: the probe measures the host, not
        // the egress, and the default lookup would only cost it time.
        var host = await MeasureHost(probe =>
        {
            probe.LicenseKey = opts.LicenseKey;
            probe.LicenseApiBase = opts.ApiBase;
            probe.Cloud = false;
            probe.Docker = false;
            probe.Widevine = false;
            probe.Geoip = false;
            return ProbeLaunch(probe);
        }, exe, PinnedMajor()).ConfigureAwait(false);

        // BACKWARDS COMPATIBILITY. The free 149 engine has no persona-profile patch, and Chromium discards unknown
        // switches silently — so sending it a profile would leave the user with NO persona while believing they had
        // one. Fall back to the seed path that engine does support, and say so.
        if (!EngineSupportsProfiles(host.BrowserMajor))
        {
            // A stable per-machine seed, so this degrades to a CONSISTENT identity rather than a new one per launch.
            o.Fingerprint ??= ProfileLib.DefaultStickyKey();
            if (opts.Quiet != true)
                Console.Error.WriteLine(
                    $"[clearcote] [profile] engine {host.BrowserMajor} does not support imported profiles " +
                    $"(added in {MinProfileEngineMajor}) — using the seed persona instead. " +
                    "Upgrade with Version = \"150\" (PRO) for the profile path.");
            return;
        }

        var result = await Resolve(host, opts).ConfigureAwait(false);
        // The corpus never probed the Windows 11 system fonts; a Windows 11 donor gets them back.
        o.FingerprintProfile = ProfileWithWindows11Faces(result.Profile);
        // A seed alongside a profile is the combination that fails strict scoring, and it also makes profile fields
        // apply only partially. "auto" therefore never sets one — and says so if the caller supplied one.
        if (o.Fingerprint is not null && opts.Quiet != true)
            Console.Error.WriteLine(
                "[clearcote] [profile] warning: LocalProfile = \"auto\" with an explicit Fingerprint seed — the " +
                "seed engages farbling, which strict anti-bots score as tampering and which makes " +
                "profile fields apply only partially. Drop Fingerprint for the coherent path.");
    }
}
