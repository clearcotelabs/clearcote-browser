using Xunit;

namespace Clearcote.Tests;

// Version selector: LaunchAsync(new(){ Version = "150" }) resolves against the catalog, VALIDATING the
// build exists (and is reachable for its tier) BEFORE downloading — so a bad request fails fast with a
// helpful message instead of getting stuck. Deterministic: uses the pure ResolveFromCatalog (no I/O).
public class VersionTests
{
    private static Catalog TestCatalog() => new()
    {
        Schema = 1,
        Builds = new List<CatalogBuild>
        {
            new()
            {
                Major = 149, Version = "149.0.7827.114", Tier = "free", Tag = "v0.1.0-pre.21",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Asset = "cc-149-win.zip", Url = "https://x/cc-149-win.zip", Sha256 = new string('a', 64), Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Asset = "cc-149-linux.tar.xz", Url = "https://x/cc-149-linux.tar.xz", Sha256 = new string('b', 64), Archive = "tar.xz", Binary = "chrome" },
                },
            },
            new()
            {
                Major = 150, Version = "150.0.7871.115", Tier = "pro", Tag = "pro-150.0.7871.115",
                Platforms = new Dictionary<string, CatalogPlatform>
                {
                    ["windows"] = new() { Archive = "zip", Binary = "chrome.exe" },
                    ["linux"] = new() { Archive = "tar.xz", Binary = "chrome" },
                },
            },
        },
    };

    [Fact]
    public void Free_major_resolves_without_license()
    {
        var plan = Download.ResolveFromCatalog(TestCatalog(), "149", hasLicense: false);
        Assert.Equal("free", plan.Kind);
        Assert.Equal("149.0.7827.114", plan.Rel!.Version);
        Assert.False(string.IsNullOrEmpty(plan.Rel.Url));
        Assert.False(string.IsNullOrEmpty(plan.Rel.Sha256));
    }

    [Fact]
    public void Exact_free_version_resolves()
    {
        var plan = Download.ResolveFromCatalog(TestCatalog(), "149.0.7827.114", hasLicense: false);
        Assert.Equal("free", plan.Kind);
        Assert.Equal("149.0.7827.114", plan.Rel!.Version);
    }

    [Fact]
    public void Pro_version_without_license_errors_clearly()
    {
        var ex = Assert.Throws<Exception>(() => Download.ResolveFromCatalog(TestCatalog(), "150", hasLicense: false));
        Assert.Contains("PRO build", ex.Message);
        Assert.Contains("license", ex.Message);
    }

    [Fact]
    public void Pro_version_with_license_routes_to_pro()
    {
        var plan = Download.ResolveFromCatalog(TestCatalog(), "150", hasLicense: true);
        Assert.Equal("pro", plan.Kind);
        Assert.Equal("150.0.7871.115", plan.Version);
    }

    [Fact]
    public void Unknown_version_lists_whats_available()
    {
        var ex = Assert.Throws<Exception>(() => Download.ResolveFromCatalog(TestCatalog(), "151", hasLicense: true));
        Assert.Contains("No Clearcote build matches version '151'", ex.Message);
        Assert.Contains("Available", ex.Message);
    }

    [Fact]
    public void Latest_is_newest_accessible_build()
    {
        var noLic = Download.ResolveFromCatalog(TestCatalog(), "latest", hasLicense: false);
        Assert.Equal("free", noLic.Kind);
        Assert.Equal("149.0.7827.114", noLic.Rel!.Version); // newest FREE
        var lic = Download.ResolveFromCatalog(TestCatalog(), "latest", hasLicense: true);
        Assert.Equal("pro", lic.Kind);
        Assert.Equal("150.0.7871.115", lic.Version);        // newest overall
    }

    [Fact]
    public void Bundled_fallback_catalog_is_wellformed()
    {
        var byVer = Release.CatalogFallback.Builds.ToDictionary(b => b.Version);
        Assert.Equal("free", byVer["149.0.7827.114"].Tier);
        Assert.False(string.IsNullOrEmpty(byVer["149.0.7827.114"].Platforms["linux"].Url)); // free carries a url
        Assert.Equal("pro", byVer["150.0.7871.115"].Tier);
        Assert.Null(byVer["150.0.7871.115"].Platforms["linux"].Url); // pro advertises existence only
    }
}
