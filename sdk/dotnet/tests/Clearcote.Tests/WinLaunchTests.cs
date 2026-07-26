using Xunit;

namespace Clearcote.Tests;

public class WinLaunchTests : IDisposable
{
    // These tests write real trees on disk (WarmFiles reads files; the AV-race recovery copies a whole
    // browser directory), so every tree is registered here and removed when xunit disposes the
    // per-test instance. Without it each run leaked a cc-warm-, a cc-br- and a clearcote-recover- tree.
    private readonly List<string> _temp = new();

    private string TempDir(string prefix)
    {
        var dir = TestTemp.Create(prefix);
        _temp.Add(dir);
        return dir;
    }

    public void Dispose()
    {
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    [Fact]
    public void WarmFiles_reads_a_tree_without_throwing()
    {
        var dir = TempDir("cc-warm-");
        File.WriteAllBytes(Path.Combine(dir, "chrome.exe"), new byte[1000]);
        Directory.CreateDirectory(Path.Combine(dir, "locales"));
        File.WriteAllBytes(Path.Combine(dir, "locales", "en-US.pak"), new byte[500]);
        WinLaunch.WarmFiles(dir);                                   // no throw
        WinLaunch.WarmFiles(Path.Combine(dir, "does-not-exist"));   // no throw (no-op)
    }

    [Theory]
    [InlineData("browserType.launch: spawn UNKNOWN", true)]
    [InlineData("The application has failed to start because its side-by-side configuration is incorrect", true)]
    [InlineData("Timeout 30000ms exceeded", false)]
    public void IsWinLaunchRace_classifies(string message, bool expected)
        => Assert.Equal(expected, WinLaunch.IsWinLaunchRace(new Exception(message)));

    [Fact]
    public void IsWinLaunchRace_on_plain_string()
        => Assert.False(WinLaunch.IsWinLaunchRace("net::ERR_CONNECTION_REFUSED"));

    [Fact]
    public async Task WinAvRetry_passthrough_off_windows()
    {
        using var _ = new Sandbox().Os("linux");
        var n = 0;
        var r = await WinLaunch.WinAvRetryAsync(_ => { n++; return Task.FromResult("/x/chrome"); }, "/x/chrome");
        Assert.Equal("/x/chrome", r);
        Assert.Equal(1, n);
    }

    [Fact]
    public async Task WinAvRetry_reraises_non_race_immediately_on_windows()
    {
        using var _ = new Sandbox().Os("windows");
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            WinLaunch.WinAvRetryAsync<string>(_ => throw new InvalidOperationException("Timeout 30000ms exceeded"), @"C:\x\chrome.exe"));
        Assert.Contains("Timeout", ex.Message);
    }

    [Fact]
    public async Task WinAvRetry_recovers_from_a_fresh_copy_on_windows()
    {
        using var _ = new Sandbox().Os("windows");
        var browser = Path.Combine(TempDir("cc-br-"), "browser");
        Directory.CreateDirectory(browser);
        var exe = Path.Combine(browser, "chrome.exe");
        File.WriteAllText(exe, "x");

        // Throw the SxS race for the original path, succeed for any recovered (fresh) path.
        var result = await WinLaunch.WinAvRetryAsync(exePath =>
        {
            if (exePath == exe) throw new Exception("browserType.launch: spawn UNKNOWN");
            return Task.FromResult(exePath);
        }, exe);

        // The recovery copy is a full browser tree the SDK put in its own temp dir
        // (<temp>/clearcote-recover-X/browser/chrome.exe) — register it before asserting, so a failed
        // assertion cannot leak it either.
        _temp.Add(Path.GetDirectoryName(Path.GetDirectoryName(result))!);

        Assert.NotEqual(exe, result);
        Assert.Equal("chrome.exe", Path.GetFileName(result));
        Assert.True(File.Exists(result)); // the fresh copy exists on disk
    }
}
