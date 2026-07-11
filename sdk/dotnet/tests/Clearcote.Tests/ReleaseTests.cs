using Xunit;

namespace Clearcote.Tests;

public class ReleaseTests
{
    private static void CheckPin(ReleaseInfo p)
    {
        foreach (var v in new[] { p.Tag, p.Version, p.Asset, p.Url, p.Sha256, p.ExeSha256, p.Os, p.Archive, p.Binary, p.AssetGlob })
            Assert.False(string.IsNullOrEmpty(v));
        Assert.Matches("^[0-9a-f]{64}$", p.Sha256);
        Assert.Matches("^[0-9a-f]{64}$", p.ExeSha256);
        Assert.True(p.Size > 0);
        Assert.Contains(p.Version, p.Asset);
        Assert.Contains(p.AssetGlob, p.Asset);
        Assert.True(p.Asset.EndsWith(".zip") || p.Asset.EndsWith(".tar.xz"));
        Assert.Equal($"https://github.com/{Release.Repo}/releases/download/{p.Tag}/{p.Asset}", p.Url);
        Assert.Matches(@"^v\d+\.\d+\.\d+", p.Tag);
        Assert.Matches(@"^\d+\.\d+\.\d+\.\d+$", p.Version);
    }

    [Fact]
    public void All_pins_are_well_formed()
    {
        Assert.Equal(new HashSet<string> { "windows", "linux" }, Release.Platforms.Keys.ToHashSet());
        foreach (var p in Release.Platforms.Values) CheckPin(p);
    }

    [Fact]
    public void Windows_pin_shape()
    {
        Assert.Equal("windows", Release.Windows.Os);
        Assert.Equal("zip", Release.Windows.Archive);
        Assert.Equal("chrome.exe", Release.Windows.Binary);
        Assert.EndsWith("-windows-x64.zip", Release.Windows.Asset);
    }

    [Fact]
    public void Linux_pin_shape()
    {
        Assert.Equal("linux", Release.Linux.Os);
        Assert.Equal("tar.xz", Release.Linux.Archive);
        Assert.Equal("chrome", Release.Linux.Binary);
        Assert.EndsWith("-linux-x64.tar.xz", Release.Linux.Asset);
    }

    [Fact]
    public void PlatformRelease_selects_by_os()
    {
        Assert.Same(Release.Windows, Release.PlatformRelease("windows"));
        Assert.Same(Release.Linux, Release.PlatformRelease("linux"));
        Assert.Null(Release.PlatformRelease("darwin"));
        Assert.Same(Release.PlatformRelease() ?? Release.Windows, Release.Current);
    }

    [Fact]
    public void Signing_key_fingerprint_is_40_hex_upper()
        => Assert.Matches("^[0-9A-F]{40}$", Release.SigningKeyFpr);
}
