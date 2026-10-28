namespace Clearcote;

/// A pinned (or runtime-resolved) Clearcote browser release the SDK downloads and verifies.
/// The sha256 is the single trust anchor for the auto-download.
public sealed record ReleaseInfo
{
    /// Git tag / release the binary comes from.
    public required string Tag { get; init; }
    /// Chromium version the build is based on.
    public required string Version { get; init; }
    /// Release asset (archive) file name.
    public required string Asset { get; init; }
    /// Direct download URL for the asset.
    public required string Url { get; init; }
    /// SHA-256 of the archive — verified after download; mismatch is a hard failure.
    public required string Sha256 { get; init; }
    /// SHA-256 of the inner browser binary — verified after extraction (defense in depth).
    public required string ExeSha256 { get; init; }
    /// SHA-256 of chrome.dll on Windows — verified before every managed launch when present.
    public string ChromeDllSha256 { get; init; } = "";
    /// Expected archive size in bytes (for progress display only).
    public required long Size { get; init; }
    /// Platform tag the binary runs on ("windows" | "linux").
    public required string Os { get; init; }
    /// Archive format of the asset ("zip" | "tar.xz").
    public required string Archive { get; init; }
    /// Inner browser-binary file name (chrome.exe on Windows, chrome on Linux).
    public required string Binary { get; init; }
    /// The "-&lt;glob&gt;" marker that identifies this platform's asset (e.g. "windows-x64").
    public required string AssetGlob { get; init; }
    /// True when discovered via the GitHub API (un-pinned) rather than baked into the SDK.
    public bool Unpinned { get; init; }
    /// URL of SHA256SUMS.txt.asc (auto-update only) for optional GPG verification.
    public string? AscUrl { get; init; }
    /// URL of the public signing key (auto-update only).
    public string? KeyUrl { get; init; }
}

/// Pinned Clearcote releases the SDK downloads and verifies — one pin per platform. Bumping to a new
/// browser build = updating the entry here (and the package Version). The sha256 is the trust anchor.
public static class Release
{
    public static readonly ReleaseInfo Windows = new()
    {
        Tag = "v0.1.0-pre.25",
        Version = "151.0.7922.108",
        Asset = "clearcote-151.0.7922.108-windows-x64.zip",
        Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.25/clearcote-151.0.7922.108-windows-x64.zip",
        Sha256 = "a3e3455c4c2da6187c9224563d99651a0cfd1a2bac5f70c2f9a19366c4c541cd",
        ExeSha256 = "e9c88e826534200e62b6544144ff3801c63065edb8940ff45e533918322e8dc0",
        Size = 204317302,
        Os = "windows",
        Archive = "zip",
        Binary = "chrome.exe",
        AssetGlob = "windows-x64",
    };

    public static readonly ReleaseInfo Linux = new()
    {
        Tag = "v0.1.0-pre.25",
        Version = "151.0.7922.108",
        Asset = "clearcote-151.0.7922.108-linux-x64.tar.xz",
        Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.25/clearcote-151.0.7922.108-linux-x64.tar.xz",
        Sha256 = "6c06f95783410d09799828b93f7bb26ac8174354b87f580827c8bcf1f2226b7e",
        ExeSha256 = "6261bae468ced0b7b1fe0f71d73344cb6feca2d9abf4a1da285ba4f572c546cc",
        Size = 122845968,
        Os = "linux",
        Archive = "tar.xz",
        Binary = "chrome",
        AssetGlob = "linux-x64",
    };

    /// OS tag -> pinned release.
    public static readonly IReadOnlyDictionary<string, ReleaseInfo> Platforms =
        new Dictionary<string, ReleaseInfo> { ["windows"] = Windows, ["linux"] = Linux };

    /// The pinned release for the given platform (default: this OS), or null if unsupported.
    public static ReleaseInfo? PlatformRelease(string? os = null)
        => Platforms.TryGetValue(os ?? Native.OsTag, out var r) ? r : null;

    /// OS tag -> the PRO catalog / /download/pro <c>platform</c> key. Wider than <see cref="Platforms"/>:
    /// macOS has a native PRO build (Apple Silicon, a Chromium.app bundle) but no free pinned release, so it
    /// is here and not there. The PRO build is resolved by the site at run time; there is nothing to pin.
    public static readonly IReadOnlyDictionary<string, string> ProPlatforms =
        new Dictionary<string, string> { ["windows"] = "windows", ["linux"] = "linux", ["macos"] = "macos" };

    /// CPU architectures the native macOS PRO build runs on: Apple Silicon only. An Intel Mac uses the
    /// Docker image instead.
    public static readonly IReadOnlyList<string> MacosArches = new[] { "arm64" };

    /// The PRO download platform key for an OS ("windows" / "linux" / "macos"; default: this machine), or
    /// null if there is no PRO build for it. A Mac that is not Apple Silicon throws: the build exists, just
    /// not for this CPU, and "unsupported OS" would send the caller looking in the wrong place.
    public static string? ProPlatformKey(string? os = null, string? arch = null)
    {
        if (!ProPlatforms.TryGetValue(os ?? Native.OsTag, out var key)) return null;
        var cpu = arch ?? Native.Arch;
        if (key == "macos" && !MacosArches.Contains(cpu))
            throw new PlatformNotSupportedException(
                $"The native macOS Clearcote PRO build is for Apple Silicon (arm64) only; this Mac reports {(cpu.Length > 0 ? cpu : "an unknown CPU")}. " +
                "Use the Docker image instead (Docker = true), or run on an Apple Silicon Mac.");
        return key;
    }

    /// True on an Apple Silicon Mac, where the native PRO build runs.
    public static bool NativeMacosSupported(string? os = null, string? arch = null)
        => (os ?? Native.OsTag) == "macos" && MacosArches.Contains(arch ?? Native.Arch);

    /// The pin for the CURRENT platform. Falls back to the Windows entry on an unsupported OS so error
    /// messaging still has a version to quote; download/guard paths branch on PlatformRelease().
    public static readonly ReleaseInfo Current = PlatformRelease() ?? Windows;

    /// GitHub repo (owner/name) the releases come from — used by the opt-in auto-update resolver.
    public const string Repo = "clearcotelabs/clearcote-browser";

    /// Clearcote's release-signing key fingerprint, pinned out-of-band (durable autoUpdate trust anchor).
    public const string SigningKeyFpr = "CA96F185F96A693AEDB3AC1FCB00D851B7A86B0F";

    // ── Version catalog ──────────────────────────────────────────────────────
    // Source of truth for "which browser majors exist and what tier each is". Fetched at runtime so a
    // NEW release becomes switchable (LaunchAsync(new(){ Version = "150" })) without an SDK bump. FREE
    // builds are public on GitHub and carry url+sha256; PRO builds (license-gated, not yet public)
    // advertise existence ONLY — the actual download routes through the authenticated /download/pro.

    /// Public version catalog URL the SDK fetches; the bundled snapshot is the offline fallback.
    public const string CatalogUrl = "https://www.clearcotelabs.com/api/v1/versions";

    /// Offline fallback snapshot — keep in sync with published releases. Only list builds that are
    /// actually DOWNLOADABLE: when a new build (e.g. the 150 PRO) goes live, add it to the live catalog
    /// (/api/v1/versions) — no SDK republish needed — and to this snapshot on the next SDK release.
    public static readonly Catalog CatalogFallback = new()
    {
        Schema = 1,
        Builds = new List<CatalogBuild>
        {
            new()
            {
                Major = 149, Version = "149.0.7827.114", Tier = "free", Tag = "v0.1.0-pre.22",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Asset = "clearcote-149.0.7827.114-windows-x64.zip", Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-windows-x64.zip", Sha256 = "f40c83049aae13d8c0322e9822eeb2d7236fddd5e7915304eb6f0f5f1f79fcb6", ExeSha256 = "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a", Size = 242661515, Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Asset = "clearcote-149.0.7827.114-linux-x64.tar.xz", Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.22/clearcote-149.0.7827.114-linux-x64.tar.xz", Sha256 = "6625971f51318acb6adc301fcc18dcf790c3de0ba2b313be7182eff180a8ec53", ExeSha256 = "aea54f3c1b5bfc43b4c42aff00d41df49393575950df7c644cc580f72462db4a", Size = 146834232, Archive = "tar.xz", Binary = "chrome" },
                },
            },
            new()
            {
                Major = 150, Version = "150.0.7871.114", Tier = "free", Tag = "v0.1.0-pre.23",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Asset = "clearcote-150.0.7871.114-windows-x64.zip", Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-windows-x64.zip", Sha256 = "93fc03c45b931d8d82f714814318892929f44dd671b0993788332071d53f3135", ExeSha256 = "f49b0d6bc5a08857e34f951ddc456abc643283ae45ff330ee7c2c39cd75b4869", Size = 245936073, Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Asset = "clearcote-150.0.7871.114-linux-x64.tar.xz", Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.23/clearcote-150.0.7871.114-linux-x64.tar.xz", Sha256 = "92ebd3de6fbf2c1349d3eb3c210952fcdb8dc9ed35655a35297cd68431fc8ef2", ExeSha256 = "cc192460484b7dcb1de8df3b3b7bea6f78962d560b6347012d9dbedd3f66720d", Size = 149463344, Archive = "tar.xz", Binary = "chrome" },
                },
            },
            new()
            {
                Major = 151, Version = "151.0.7922.108", Tier = "free", Tag = "v0.1.0-pre.25",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Asset = Windows.Asset, Url = Windows.Url, Sha256 = Windows.Sha256, ExeSha256 = Windows.ExeSha256, Size = Windows.Size, Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Asset = Linux.Asset, Url = Linux.Url, Sha256 = Linux.Sha256, ExeSha256 = Linux.ExeSha256, Size = Linux.Size, Archive = "tar.xz", Binary = "chrome" },
                },
            },
        },
    };
}

/// One platform's download info within a <see cref="CatalogBuild"/>. FREE builds carry Url+Sha256; PRO
/// builds leave them null (the download routes through the authenticated /download/pro).
public sealed record CatalogPlatform
{
    public string? Asset { get; init; }
    public string? Url { get; init; }
    public string? Sha256 { get; init; }
    public string? ExeSha256 { get; init; }
    public long Size { get; init; }
    public string Archive { get; init; } = "zip";
    public string Binary { get; init; } = "chrome";
}

/// One published build in the catalog.
public sealed record CatalogBuild
{
    public int Major { get; init; }
    public string Version { get; init; } = "";
    /// "free" (public on GitHub) or "pro" (license-gated).
    public string Tier { get; init; } = "free";
    public string Tag { get; init; } = "";
    public Dictionary<string, CatalogPlatform> Platforms { get; init; } = new();
}

/// The public version catalog (the SDK fetches this to answer Version selectors).
public sealed record Catalog
{
    public int Schema { get; init; } = 1;
    public List<CatalogBuild> Builds { get; init; } = new();
}
