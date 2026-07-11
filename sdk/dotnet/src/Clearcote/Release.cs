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
        Tag = "v0.1.0-pre.21",
        Version = "149.0.7827.114",
        Asset = "clearcote-149.0.7827.114-windows-x64.zip",
        Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.21/clearcote-149.0.7827.114-windows-x64.zip",
        Sha256 = "79b03d2d875b374970b2d54eae54f77070eba06b6a446dc163420854ec068c4d",
        ExeSha256 = "09a9f5ed46be45b54babc91872256fcdd5ef61cef6bf65cbec3928cbb38ee17a",
        Size = 242655762,
        Os = "windows",
        Archive = "zip",
        Binary = "chrome.exe",
        AssetGlob = "windows-x64",
    };

    public static readonly ReleaseInfo Linux = new()
    {
        Tag = "v0.1.0-pre.21",
        Version = "149.0.7827.114",
        Asset = "clearcote-149.0.7827.114-linux-x64.tar.xz",
        Url = "https://github.com/clearcotelabs/clearcote-browser/releases/download/v0.1.0-pre.21/clearcote-149.0.7827.114-linux-x64.tar.xz",
        Sha256 = "5e7241a3e90033bc84f6079821829e99a6e6f0f6479eaa291d8b6590363aa292",
        ExeSha256 = "dd5aef845b47f63ebf84d769cc349dae69178639fe5c703fc52779c5a0606cce",
        Size = 146851212,
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

    /// Offline fallback snapshot — keep in sync with published releases.
    public static readonly Catalog CatalogFallback = new()
    {
        Schema = 1,
        Builds = new List<CatalogBuild>
        {
            new()
            {
                Major = 149, Version = "149.0.7827.114", Tier = "free", Tag = "v0.1.0-pre.21",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Asset = Windows.Asset, Url = Windows.Url, Sha256 = Windows.Sha256, ExeSha256 = Windows.ExeSha256, Size = Windows.Size, Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Asset = Linux.Asset, Url = Linux.Url, Sha256 = Linux.Sha256, ExeSha256 = Linux.ExeSha256, Size = Linux.Size, Archive = "tar.xz", Binary = "chrome" },
                },
            },
            new()
            {
                // PRO — existence advertised for validation; download requires a license via /download/pro.
                Major = 150, Version = "150.0.7871.115", Tier = "pro", Tag = "pro-150.0.7871.115",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Archive = "tar.xz", Binary = "chrome" },
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
