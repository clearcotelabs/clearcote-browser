using Xunit;

namespace Clearcote.Tests;

// PRO revision pinning: Version = "r7" / "150.0.7871.114-r7" selects a licensed REBUILD. Revisions
// are the same Chromium version rebuilt, so they never appear in the public catalog — the SDK must
// recognise the shape and route straight to the authenticated PRO download, requiring a license.
public class ProRevisionTests
{
    [Theory]
    [InlineData("r7")]
    [InlineData("r3")]
    [InlineData("r99")]
    [InlineData("R7")]
    [InlineData("150.0.7871.114-r7")]
    [InlineData("150.0.7871.114-R9")]
    [InlineData("  r7  ")]
    public void Detects_revision_selectors(string sel) =>
        Assert.True(Download.IsProRevisionSelector(sel));

    [Theory]
    [InlineData("150")]
    [InlineData("150.0.7871.114")]
    [InlineData("latest")]
    [InlineData("")]
    [InlineData(null)]
    [InlineData("r")]
    [InlineData("r7x")]
    [InlineData("149.0.7827.114")]
    public void Rejects_non_revision_selectors(string? sel) =>
        Assert.False(Download.IsProRevisionSelector(sel));

    [Fact]
    public async Task Version_qualified_revision_reports_its_version_for_telemetry()
    {
        var v = await Download.ResolvedEngineVersionAsync("150.0.7871.114-r7", hasLicense: true);
        Assert.Equal("150.0.7871.114", v);
    }

    [Fact]
    public async Task Bare_revision_reports_the_baseline_version_for_telemetry()
    {
        var v = await Download.ResolvedEngineVersionAsync("r7", hasLicense: true);
        Assert.Equal(Release.Current.Version, v);
    }

    [Fact]
    public async Task Revision_without_license_fails_fast_with_a_clear_message()
    {
        // No license key -> must throw BEFORE any network, naming it a PRO revision.
        var ex = await Assert.ThrowsAsync<Exception>(() =>
            Download.EnsureVersionAsync("150.0.7871.114-r7", licenseKey: null));
        Assert.Contains("PRO revision", ex.Message);
        Assert.Contains("license", ex.Message);
    }
}
