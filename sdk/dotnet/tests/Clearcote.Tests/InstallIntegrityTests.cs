using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// A cached browser tree can be complete at install time and damaged later (antivirus quarantine, a
// full disk, an interrupted copy out of a packaged app). Chromium does not report that usefully — it
// CHECK-crashes during startup ("Invalid file descriptor to ICU data received") before Playwright can
// attach — so the SDK detects it itself: self-heal for trees we installed, clear error for trees the
// caller supplied.
public class InstallIntegrityTests : IDisposable
{
    private static readonly string Binary = OperatingSystem.IsWindows() ? "chrome.exe" : "chrome";
    private static readonly string[] Payload =
        { "chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak" };

    private readonly List<string> _temp = new();

    public void Dispose()
    {
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    /// A minimal but complete install base: {base}/browser + .verified + a manifest.
    private (string Base, string Browser) Tree()
    {
        var root = TestTemp.Create("cc-integrity-");
        _temp.Add(root);
        var @base = Path.Combine(root, "v-test");
        var browser = Path.Combine(@base, "browser");
        Directory.CreateDirectory(Path.Combine(browser, "locales"));
        foreach (var name in Payload.Prepend(Binary))
            File.WriteAllBytes(Path.Combine(browser, name), new byte[128]);
        File.WriteAllBytes(Path.Combine(browser, "locales", "en-US.pak"), new byte[64]);
        Download.WriteManifest(@base, browser);
        File.WriteAllText(Path.Combine(@base, ".verified"), "deadbeef\n");
        return (@base, browser);
    }

    [Fact]
    public void Healthy_tree_verifies_and_is_returned_from_cache()
    {
        var (@base, browser) = Tree();
        Assert.Empty(Download.VerifyInstall(browser, @base, full: true));
        Assert.Equal(Path.Combine(browser, Binary), Download.CachedBinary(@base, Binary, quiet: true));
    }

    [Fact]
    public void Manifest_records_every_file()
    {
        var (@base, _) = Tree();
        using var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(@base, Download.Manifest)));
        var files = doc.RootElement.GetProperty("files");
        Assert.Equal(64, files.GetProperty("locales/en-US.pak").GetInt64()); // nested keys use '/'
        Assert.Equal(128, files.GetProperty("icudtl.dat").GetInt64());
    }

    [Fact]
    public void Missing_icu_data_is_detected() // the exact field failure
    {
        var (@base, browser) = Tree();
        File.Delete(Path.Combine(browser, "icudtl.dat"));
        var problems = Download.VerifyInstall(browser, @base, full: true);
        var problem = Assert.Single(problems);
        Assert.Contains("icudtl.dat", problem);
        Assert.Contains("missing", problem);
    }

    [Fact]
    public void Truncated_payload_file_is_detected()
    {
        var (@base, browser) = Tree();
        File.WriteAllBytes(Path.Combine(browser, "icudtl.dat"), new byte[12]); // a copy that stopped part-way
        Assert.Contains("expected 128", Download.VerifyInstall(browser, @base, full: true)[0]);
    }

    [Fact]
    public void Manifest_catches_a_non_critical_file()
    {
        var (@base, browser) = Tree();
        File.Delete(Path.Combine(browser, "locales", "en-US.pak"));
        Assert.Equal(new[] { "locales/en-US.pak — missing" }, Download.VerifyInstall(browser, @base, full: true));
    }

    [Fact]
    public void Damaged_cache_is_wiped_so_the_caller_redownloads()
    {
        var (@base, browser) = Tree();
        File.Delete(Path.Combine(browser, "icudtl.dat"));
        Assert.Null(Download.CachedBinary(@base, Binary, quiet: true));
        Assert.False(Directory.Exists(browser));                          // wiped -> next resolve re-downloads
        Assert.False(File.Exists(Path.Combine(@base, ".verified")));      // and cannot short-circuit again
    }

    [Fact]
    public void Full_scan_is_memoised_but_can_be_forced()
    {
        var (@base, browser) = Tree();
        Assert.Empty(Download.VerifyInstall(browser, @base));
        File.Delete(Path.Combine(browser, "locales", "en-US.pak"));
        Assert.Empty(Download.VerifyInstall(browser, @base)); // memoised: manifest-only damage not re-checked
        Assert.Equal(new[] { "locales/en-US.pak — missing" }, Download.VerifyInstall(browser, @base, full: true));
    }

    [Fact]
    public void CheckInstall_rejects_a_damaged_caller_supplied_tree()
    {
        var (_, browser) = Tree();
        File.Delete(Path.Combine(browser, "icudtl.dat"));
        var ex = Assert.Throws<Exception>(() => Download.CheckInstall(Path.Combine(browser, Binary)));
        Assert.Contains("incomplete or corrupted", ex.Message);
    }

    [Fact]
    public void CheckInstall_accepts_a_healthy_caller_supplied_tree()
    {
        var (_, browser) = Tree();
        Download.CheckInstall(Path.Combine(browser, Binary)); // must not throw
    }

    [Fact]
    public void CheckInstall_ignores_a_non_flat_chromium_layout()
    {
        // Installed Google Chrome keeps its payload in a versioned subfolder; refusing to launch that
        // would be a false alarm, so the check only engages on a flat tree.
        var root = TestTemp.Create("cc-chrome-");
        _temp.Add(root);
        var app = Path.Combine(root, "Application");
        Directory.CreateDirectory(Path.Combine(app, "151.0.7922.108"));
        File.WriteAllBytes(Path.Combine(app, Binary), new byte[1]);
        File.WriteAllBytes(Path.Combine(app, "151.0.7922.108", "chrome.dll"), new byte[1]);
        Download.CheckInstall(Path.Combine(app, Binary)); // must not throw
    }

    [Fact]
    public void CheckInstall_ignores_a_missing_binary()
    {
        var root = TestTemp.Create("cc-absent-");
        _temp.Add(root);
        Download.CheckInstall(Path.Combine(root, "nope", Binary)); // the launcher reports this better
    }

    [Fact]
    public void Error_names_the_file_and_the_fix()
    {
        var (@base, browser) = Tree();
        var text = Download.BrokenInstallError(browser, new[] { "icudtl.dat — missing" }).Message;
        Assert.Contains("icudtl.dat", text);
        Assert.Contains(@base, text); // the folder to delete
        Assert.Contains("antivirus", text);
    }

    [Fact]
    public void Error_for_caller_supplied_tree_does_not_promise_a_redownload()
    {
        var (_, browser) = Tree();
        var text = Download.BrokenInstallError(browser, new[] { "icudtl.dat — missing" }, repairable: false).Message;
        Assert.DoesNotContain("re-download it", text);
        Assert.Contains("CLEARCOTE_BINARY", text);
    }

    [Fact]
    public void Many_problems_are_truncated()
    {
        var (_, browser) = Tree();
        var many = Enumerable.Range(0, 20).Select(i => $"f{i}.pak — missing").ToArray();
        Assert.Contains("... and 12 more", Download.BrokenInstallError(browser, many).Message);
    }
}
