using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace Clearcote;

/// <summary>
/// Profile library: index, host-coherence scoring, and selection.
///
/// Port of the Node SDK's <c>profilelib.ts</c>. The three SDKs must stay behaviourally
/// identical — a persona chosen on one must be the persona chosen on another for the same host
/// and key, or a customer switching languages silently changes identity.
///
/// The persona path this serves is <c>--fingerprint-profile</c> WITHOUT <c>--fingerprint</c>.
/// That distinction is load-bearing: measured against a strict anti-bot device check, a seed persona
/// failed 13/13 while a profile-only persona passed. With no seed the farbling machinery never
/// engages, so canvas/WebGL/audio readbacks come back byte-identical to an unmodified browser.
///
/// SELECTION IS ABOUT COHERENCE WITH *THIS HOST*, NOT ABOUT MATCHING AN OS STRING. A profile
/// claiming an RTX 3080 on an Intel UHD host says NVIDIA in the strings and paints Intel pixels
/// — a contradiction readable in one call. So the GPU term dominates, and BrowserMajor is a HARD
/// filter: a Chrome-138 capture on a 150 engine reports UA 138 while the engine behaves like 150.
///
/// ROTATION IS PER-IDENTITY, NOT PER-LAUNCH. Always serving the single best profile makes every
/// customer on similar hardware converge on one identity, and that shared fingerprint becomes
/// its own signal. But re-rolling every launch is worse: an account whose device changes on
/// every visit is a harder tell than a slightly-suboptimal stable one. So the default (Rotate)
/// is sticky per key.
/// </summary>
public static class ProfileLib
{
    /// Windows caps a process command line at 32767 chars. The profile rides on argv as
    /// --fingerprint-profile=&lt;gzip+base64&gt;, and the other switches need room too. Exceeding
    /// this is not a soft failure — Chromium refuses to spawn — so it is enforced at SELECTION
    /// time rather than at launch.
    public const int DefaultMaxEncoded = 24000;

    public const int DefaultTopN = 25;

    /// Coarse GPU vendor class from a renderer string (ANGLE or raw GL).
    public static string GpuVendorClass(string? renderer)
    {
        var r = (renderer ?? string.Empty).ToLowerInvariant();
        if (r.Length == 0) return "unknown";
        if (r.Contains("nvidia") || r.Contains("geforce") || r.Contains("quadro")) return "nvidia";
        if (r.Contains("amd") || r.Contains("radeon") || r.Contains("ati ")) return "amd";
        if (r.Contains("intel")) return "intel";
        if (r.Contains("apple")) return "apple";
        if (r.Contains("swiftshader") || r.Contains("llvmpipe") || r.Contains("software")) return "software";
        if (r.Contains("mali") || r.Contains("adreno") || r.Contains("powervr")) return "mobile";
        return "other";
    }

    /// Ratio closeness in [0,1] — 1 when equal, decaying as the values diverge.
    /// Unknown on either side is neutral (0.5): never a bonus, never a penalty.
    private static double Closeness(double? a, double? b)
    {
        if (a is null || b is null || a <= 0 || b <= 0) return 0.5;
        var hi = Math.Max(a.Value, b.Value);
        var lo = Math.Min(a.Value, b.Value);
        return lo / hi;
    }

    /// <summary>
    /// Score a candidate against the host. Higher is better; the maximum is 1.
    ///
    /// Weights encode what a detector can actually catch, in descending order of how cheaply:
    /// GPU class (0.45) is a single-call contradiction; screen (0.25) is the documented block
    /// trigger and is ASYMMETRIC — claiming a display LARGER than the host has is the dangerous
    /// direction, so it is penalised harder; then DPR (0.10), cores+memory (0.10), freshness (0.10).
    /// </summary>
    public static double ScoreProfile(ProfileIndexEntry entry, HostFacts host)
    {
        double score = 0;

        var hv = host.GpuVendor ?? "unknown";
        var ev = entry.GpuVendor ?? "unknown";
        double gpu;
        if (ev == "software" || hv == "software") gpu = 0;           // pixels can never match the claim
        else if (ev == hv) gpu = 1;
        else if (hv == "unknown" || ev == "unknown") gpu = 0.5;
        else gpu = 0.15;                                              // different real vendor
        if (gpu == 1 && entry.GpuTier.HasValue && host.GpuTier.HasValue)
        {
            gpu = 1 - Math.Min(1.0, Math.Abs(entry.GpuTier.Value - host.GpuTier.Value) / 3.0) * 0.4;
        }
        score += 0.45 * gpu;

        double screen = 0.5;
        if (entry.ScreenWidth > 0 && entry.ScreenHeight > 0 && host.ScreenWidth > 0 && host.ScreenHeight > 0)
        {
            var fits = entry.ScreenWidth <= host.ScreenWidth && entry.ScreenHeight <= host.ScreenHeight;
            var area = Closeness(
                (double)entry.ScreenWidth * entry.ScreenHeight,
                (double)host.ScreenWidth * host.ScreenHeight);
            screen = fits ? 0.7 + 0.3 * area : 0.25 * area;
        }
        score += 0.25 * screen;

        score += 0.10 * Closeness(entry.DevicePixelRatio, host.DevicePixelRatio);
        score += 0.05 * Closeness(entry.HardwareConcurrency, host.HardwareConcurrency);
        score += 0.05 * Closeness(entry.DeviceMemory, host.DeviceMemory);

        double fresh = 0.5;
        if (!string.IsNullOrEmpty(entry.CapturedAt) &&
            DateTimeOffset.TryParse(entry.CapturedAt, CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out var dt))
        {
            var days = (DateTimeOffset.UtcNow - dt).TotalDays;
            fresh = Math.Max(0, Math.Min(1, 1 - (days - 180) / 550));
        }
        score += 0.10 * fresh;

        return score;
    }

    /// Hard filters. A candidate failing any of these is incoherent, not merely low-scoring —
    /// no weight should be able to promote it back into the pool.
    public static bool Eligible(
        ProfileIndexEntry entry, HostFacts host, string? osOverride = null, int maxEncoded = DefaultMaxEncoded)
    {
        var want = (osOverride ?? host.OsFamily ?? string.Empty).ToLowerInvariant();
        if (!string.Equals((entry.OsFamily ?? string.Empty).ToLowerInvariant(), want, StringComparison.Ordinal))
            return false;
        // A version-agnostic profile (BrowserMajor null) carries hardware identity only and
        // inherits the engine's version, so it cannot contradict it — those pass. That is the
        // converted chrome-fingerprints case: its records are Chrome ~114/115 and
        // convert_dataset.py drops the version deliberately. A profile that PINS a version and
        // gets it wrong IS a contradiction — the UA would claim 138 while the engine's
        // observable behaviour is 150 — so it is rejected however well it scores elsewhere.
        if (entry.BrowserMajor.HasValue && entry.BrowserMajor.Value != host.BrowserMajor) return false;
        if (entry.EncodedSize.HasValue && entry.EncodedSize.Value > maxEncoded) return false;
        return true;
    }

    private static uint KeyHash(string key)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(key));
        // First 4 bytes, big-endian — must match the Node/Python SDKs, which take the first 8
        // hex chars of the same digest.
        return ((uint)bytes[0] << 24) | ((uint)bytes[1] << 16) | ((uint)bytes[2] << 8) | bytes[3];
    }

    private static string? _cachedStickyKey;

    /// Platform token matching Node's <c>process.platform</c> and Python's <c>sys.platform</c>.
    /// It has to be the SAME STRING in all three SDKs: the keyless sticky key is hashed from it,
    /// so "Win32NT" here instead of "win32" would make the same machine resolve to a different
    /// persona depending on which SDK launched it.
    internal static string PlatformToken() =>
        OperatingSystem.IsWindows() ? "win32"
        : OperatingSystem.IsMacOS() ? "darwin"
        : OperatingSystem.IsLinux() ? "linux"
        : "unknown";

    /// A stable per-machine key, so keyless Rotate is consistent rather than random —
    /// and identical across the Node, Python and .NET SDKs on the same machine.
    public static string DefaultStickyKey()
    {
        if (_cachedStickyKey is not null) return _cachedStickyKey;
        var bits = new[]
        {
            Environment.GetEnvironmentVariable("CLEARCOTE_PROFILE_KEY"),
            Environment.GetEnvironmentVariable("USERNAME") ?? Environment.GetEnvironmentVariable("USER"),
            PlatformToken(),
        }.Where(b => !string.IsNullOrEmpty(b));
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(string.Join("|", bits)));
        _cachedStickyKey = Convert.ToHexString(digest).ToLowerInvariant()[..16];
        return _cachedStickyKey;
    }

    /// <summary>
    /// Pick a profile for this host.
    ///
    /// Throws when nothing is eligible rather than silently falling back to an incoherent
    /// profile — a wrong persona is worse than none, because it turns a clean browser into a
    /// contradictory one.
    /// </summary>
    public static ProfileSelection SelectProfile(
        IReadOnlyList<ProfileIndexEntry> index,
        HostFacts host,
        SelectMode mode = SelectMode.Rotate,
        string? key = null,
        int topN = DefaultTopN,
        string? osOverride = null,
        int maxEncoded = DefaultMaxEncoded,
        Random? rng = null)
    {
        var candidates = index.Where(e => Eligible(e, host, osOverride, maxEncoded)).ToList();
        if (candidates.Count == 0)
        {
            throw new InvalidOperationException(
                $"no profile matches host (os={osOverride ?? host.OsFamily}, chromium major={host.BrowserMajor}). " +
                $"The index has {index.Count} entries; none passed the os/major/size filters. " +
                "Sync a newer index, or pass an explicit FingerprintProfile.");
        }

        // Tie-break by id so ordering is deterministic across runs, machines and SDKs.
        var scored = candidates
            .Select(e => (Entry: e, Score: ScoreProfile(e, host)))
            .OrderByDescending(x => x.Score)
            .ThenBy(x => x.Entry.Id, StringComparer.Ordinal)
            .ToList();

        var poolSize = scored.Count;
        var n = Math.Max(1, Math.Min(topN, poolSize));
        var pool = scored.Take(n).ToList();

        (ProfileIndexEntry Entry, double Score) chosen = mode switch
        {
            SelectMode.Best => pool[0],
            SelectMode.Random => pool[(rng ?? Random.Shared).Next(pool.Count)],
            // Rotate: sticky per key. With no key the caller still gets a STABLE profile rather
            // than a new one each launch — an identity whose device changes every visit is a
            // worse tell than a slightly-suboptimal fixed one.
            _ => pool[(int)(KeyHash(key ?? DefaultStickyKey()) % (uint)pool.Count)],
        };

        return new ProfileSelection
        {
            Entry = chosen.Entry,
            Score = chosen.Score,
            PoolSize = poolSize,
            Mode = mode,
        };
    }

    /// <see cref="SelectProfile(IReadOnlyList{ProfileIndexEntry}, HostFacts, SelectMode, string?, int, string?, int, Random?)"/>
    /// with the options as one object (Node's <c>selectProfile(index, host, opts)</c>); unset fields take their defaults.
    public static ProfileSelection SelectProfile(IReadOnlyList<ProfileIndexEntry> index, HostFacts host, SelectOptions? options)
        => SelectProfile(index, host, options?.Mode ?? SelectMode.Rotate, options?.Key, options?.TopN ?? DefaultTopN,
            options?.Os, options?.MaxEncodedSize ?? DefaultMaxEncoded);
}

/// How <see cref="ProfileLib.SelectProfile(IReadOnlyList{ProfileIndexEntry}, HostFacts, SelectOptions?)"/> picks a
/// profile. Every field is optional.
public class SelectOptions
{
    /// Rotate (default) sticky per key; Best always top-scoring; Random fresh each call.
    public SelectMode? Mode { get; set; }
    /// Stickiness key for Rotate — an account id, proxy session, or profile name.
    public string? Key { get; set; }
    /// Candidate pool the mode picks from (default 25). Wide enough to avoid herding, narrow enough to stay coherent.
    public int? TopN { get; set; }
    /// Override the target OS (defaults to the host's).
    public string? Os { get; set; }
    /// Hard ceiling on encoded size (default 24000); entries above it cannot be launched on Windows.
    public int? MaxEncodedSize { get; set; }
}

/// How SelectProfile picks from the top-N pool.
public enum SelectMode
{
    /// Always the top-scoring profile. Max coherence; accepts herding.
    Best,
    /// Sticky per key — same key, same profile; different keys spread across the pool. Default.
    Rotate,
    /// Fresh pick each call. For throwaway single-use identities.
    Random,
}

/// One row of the profile index. Deliberately slim: selection never touches a profile blob
/// until it has picked a winner.
public sealed class ProfileIndexEntry
{
    public string Id { get; set; } = string.Empty;
    public string? OsFamily { get; set; }
    public string? OsMajor { get; set; }
    /// <summary>
    /// Chromium major of the capture, HARD-filtered against the engine's own major.
    ///
    /// <c>null</c> means VERSION-AGNOSTIC and matches any engine. That is not a loophole — it is
    /// the correct description of a hardware-only profile. Imports from the chrome-fingerprints
    /// dataset deliberately drop the browser version (its records are Chrome ~114/115) and carry
    /// only version-independent identity: GPU, screen, fonts, voices, audio, CPU/memory,
    /// keyboard. Such a profile inherits the running engine's version and cannot contradict it.
    /// </summary>
    public int? BrowserMajor { get; set; }
    /// Coarse vendor class: nvidia | amd | intel | apple | software | mobile | other | unknown.
    public string? GpuVendor { get; set; }
    /// Rough capability tier (0 = integrated, 1 = mainstream, 2 = high-end).
    public int? GpuTier { get; set; }
    public string? Renderer { get; set; }
    public int ScreenWidth { get; set; }
    public int ScreenHeight { get; set; }
    public double? DevicePixelRatio { get; set; }
    public int? HardwareConcurrency { get; set; }
    public int? DeviceMemory { get; set; }
    /// gzip+base64 length of the encoded profile — keeps the launch under the argv limit.
    public int? EncodedSize { get; set; }
    /// ISO-8601 capture time; newer is mildly preferred.
    public string? CapturedAt { get; set; }
}

/// What we know about the machine actually doing the rendering.
public sealed class HostFacts
{
    public string? OsFamily { get; set; }
    public int BrowserMajor { get; set; }
    public string? GpuVendor { get; set; }
    public int? GpuTier { get; set; }
    public int ScreenWidth { get; set; }
    public int ScreenHeight { get; set; }
    public double? DevicePixelRatio { get; set; }
    public int? HardwareConcurrency { get; set; }
    public int? DeviceMemory { get; set; }
}

/// The outcome of a selection, including the pool size so an over-narrow index is visible.
public sealed class ProfileSelection
{
    public ProfileIndexEntry Entry { get; set; } = new();
    public double Score { get; set; }
    public int PoolSize { get; set; }
    public SelectMode Mode { get; set; }
}
