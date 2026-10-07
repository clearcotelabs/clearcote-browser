using System.ComponentModel;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using Xunit;

namespace Clearcote.Tests;

/// Windows: launching a cached build that Windows refuses to start in place ("spawn UNKNOWN" / "side-by-side
/// configuration is incorrect"). Inside an MSIX-packaged app a build downloaded into %LOCALAPPDATA% is
/// invisible to the activation-context check; real-time AV scanning a fresh chrome_elf.dll causes the same
/// error for a while. WinAvRetryAsync probes the cached exe (suspended CreateProcess), re-scans + backs off,
/// and then launches from one recovered copy per build in ~/.clearcote/recovered, which later launches reuse.
///
/// The launch must leave the temp directory as it found it: the old fallback copied the whole browser to a
/// fresh %TEMP%\clearcote-recover-* on every launch and never deleted it, and every failed Playwright attempt
/// leaked a playwright_chromiumdev_profile-* and a playwright-artifacts-* directory. Mirrors the Python
/// (test_win_launch.py) and Node (win-launch.test.ts) suites.
public class WinLaunchTests : IDisposable
{
    // A fake home, temp directory and cache build, all under one base directory that is removed when xunit
    // disposes the per-test instance, together with every copy the SDK made in them.
    private readonly Sandbox _sb = new();
    private readonly string _base = TestTemp.Create("cc-winl-");
    private readonly string _home;
    private readonly string _tmp;
    private readonly string _exe;
    private readonly Func<string, Exception?> _realProbe = WinLaunch.SpawnProbe;
    private Exception? _probe; // what the spawn probe answers for the cached exe
    private int _probes;
    private int _made;

    private static readonly Exception Sxs = new Win32Exception(14001,
        "The application has failed to start because its side-by-side configuration is incorrect");

    public WinLaunchTests()
    {
        _home = Directory.CreateDirectory(Path.Combine(_base, "home")).FullName;
        _tmp = Directory.CreateDirectory(Path.Combine(_base, "tmp")).FullName;
        _sb.Env("HOME", _home).Env("USERPROFILE", _home)
            .Env("TEMP", _tmp).Env("TMP", _tmp).Env("TMPDIR", _tmp)
            .Env("CLEARCOTE_KEEP_RECOVER", null);
        var bdir = Directory.CreateDirectory(Path.Combine(_base, "cache", "pro-1.2.3-r9", "browser")).FullName;
        _exe = Path.Combine(bdir, "chrome.exe");
        File.WriteAllText(_exe, "stub");
        File.WriteAllText(Path.Combine(bdir, "1.2.3.manifest"), "<assembly/>");
        WinLaunch.SpawnProbe = _ => { _probes++; return _probe; };
    }

    public void Dispose()
    {
        WinLaunch.SpawnProbe = _realProbe;
        _sb.Dispose();
        TestTemp.Remove(_base);
    }

    private string Root => Path.Combine(_home, ".clearcote", "recovered");

    private static string[] Names(string dir) =>
        Directory.GetFileSystemEntries(dir).Select(p => Path.GetFileName(p)).OrderBy(n => n, StringComparer.Ordinal).ToArray();

    /// What Playwright does when Windows cannot start the process: make its two directories, then throw.
    private Task<string> FailingPlaywrightLaunch()
    {
        var id = (_made++).ToString("D6");
        Directory.CreateDirectory(Path.Combine(_tmp, $"playwright-artifacts-{id}"));
        var prof = Directory.CreateDirectory(Path.Combine(_tmp, $"playwright_chromiumdev_profile-{id}")).FullName;
        throw new Exception($"BrowserType.LaunchAsync: spawn UNKNOWN\nCall log:\n  - <launching> {_exe} --no-first-run --user-data-dir={prof} --remote-debugging-pipe about:blank");
    }

    [Fact]
    public void WarmFiles_reads_a_tree_without_throwing()
    {
        var dir = Directory.CreateDirectory(Path.Combine(_base, "warm")).FullName;
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
        _sb.Os("linux");
        var n = 0;
        var r = await WinLaunch.WinAvRetryAsync(_ => { n++; return Task.FromResult("/x/chrome"); }, "/x/chrome");
        Assert.Equal("/x/chrome", r);
        Assert.Equal(1, n);
        Assert.Equal(0, _probes); // no retry machinery at all
    }

    [Fact]
    public async Task WinAvRetry_reraises_non_race_immediately_on_windows()
    {
        _sb.Os("windows");
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() =>
            WinLaunch.WinAvRetryAsync<string>(_ => throw new InvalidOperationException("Timeout 30000ms exceeded"), _exe));
        Assert.Contains("Timeout", ex.Message);
    }

    [Fact]
    public async Task Launches_in_place_when_the_probe_passes()
    {
        _sb.Os("windows");
        var calls = new List<string>();
        Assert.Equal("browser", await WinLaunch.WinAvRetryAsync(e => { calls.Add(e); return Task.FromResult("browser"); }, _exe));
        Assert.Equal(new[] { _exe }, calls);
        Assert.False(Directory.Exists(Root));
    }

    [Fact]
    public async Task A_failed_attempt_leaves_no_temp_dirs_and_the_retry_starts()
    {
        _sb.Os("windows");
        var n = 0;
        var r = await WinLaunch.WinAvRetryAsync(_ => ++n < 2 ? FailingPlaywrightLaunch() : Task.FromResult("browser"), _exe, backoffMs: 1);
        Assert.Equal("browser", r);
        Assert.Empty(Names(_tmp)); // the failed attempt's two Playwright directories are gone
        Assert.Equal(2, n);        // failed once, retried, started: no recovered copy needed
        Assert.False(Directory.Exists(Root));
    }

    [Fact]
    public async Task An_unlaunchable_build_is_recovered_once_then_reused()
    {
        // The probe fails, so Playwright is never pointed at the cached exe (each failed Playwright attempt used
        // to leak two temp directories); the copy goes to ~/.clearcote/recovered, never to the temp directory,
        // and the next launch uses it straight away.
        _sb.Os("windows");
        _probe = Sxs;
        var calls = new List<string>();
        Task<string> Launch(string e)
        {
            calls.Add(e);
            return e == _exe ? FailingPlaywrightLaunch() : Task.FromResult(e);
        }
        var first = await WinLaunch.WinAvRetryAsync(Launch, _exe, backoffMs: 1);
        Assert.Empty(Names(_tmp)); // nothing in the temp directory
        Assert.Equal(new[] { first }, calls); // the cached exe itself was never launched
        var key = WinLaunch.RecoveryKey(_exe);
        Assert.Equal(Path.Combine(Root, key, "browser", "chrome.exe"), first);
        Assert.Equal(3, _probes);
        Assert.True(File.Exists(Path.Combine(Root, key, WinLaunch.RecoveredMarker)));
        Assert.True(File.Exists(Path.Combine(Root, key, "browser", "1.2.3.manifest"))); // the whole build came along

        calls.Clear();
        Assert.Equal(first, await WinLaunch.WinAvRetryAsync(Launch, _exe, backoffMs: 1));
        Assert.Equal(new[] { first }, calls); // straight to the copy: no probe, no failed attempt, no new copy
        Assert.Equal(3, _probes);
        Assert.Equal(new[] { key }, Names(Root));
    }

    [Fact]
    public async Task Failed_attempts_are_cleaned_up_before_the_recovered_copy_starts()
    {
        // The probe passes but every Playwright attempt fails anyway (antivirus still scanning, say): each
        // attempt's directories are removed, then the build is launched from its recovered copy.
        _sb.Os("windows");
        var calls = new List<string>();
        var first = await WinLaunch.WinAvRetryAsync(e =>
        {
            calls.Add(e);
            return e == _exe ? FailingPlaywrightLaunch() : Task.FromResult(e);
        }, _exe, backoffMs: 1);
        Assert.Empty(Names(_tmp)); // no Playwright leftovers, no clearcote-recover-* copy
        Assert.Equal(new[] { _exe, _exe, _exe, first }, calls); // three attempts in place, then the copy
        Assert.Equal(Path.Combine(Root, WinLaunch.RecoveryKey(_exe), "browser", "chrome.exe"), first);
    }

    [Fact]
    public async Task A_rebuilt_build_gets_a_fresh_copy_and_the_stale_one_goes()
    {
        _sb.Os("windows");
        _probe = Sxs;
        var first = await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), _exe, backoffMs: 1);
        Assert.NotEqual(_exe, first);
        var oldKey = WinLaunch.RecoveryKey(_exe);
        File.WriteAllText(_exe, "rebuilt stub"); // same path, new build
        var second = await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), _exe, backoffMs: 1);
        var newKey = WinLaunch.RecoveryKey(_exe);
        Assert.NotEqual(oldKey, newKey);
        Assert.NotEqual(first, second);
        Assert.Equal(new[] { newKey }, Names(Root));
    }

    [Fact]
    public async Task A_copy_whose_build_is_gone_is_swept()
    {
        _sb.Os("windows");
        _probe = Sxs;
        await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), _exe, backoffMs: 1);
        var oldKey = WinLaunch.RecoveryKey(_exe);
        Assert.Equal(new[] { oldKey }, Names(Root));
        var other = Directory.CreateDirectory(Path.Combine(_base, "cache2", "pro-1.2.4-r10", "browser")).FullName;
        File.WriteAllText(Path.Combine(other, "chrome.exe"), "next release");
        File.Delete(_exe); // clear-cache / a new release replaced the old build
        await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), Path.Combine(other, "chrome.exe"), backoffMs: 1);
        Assert.DoesNotContain(oldKey, Names(Root));
    }

    [Fact]
    public async Task Old_sdks_temp_copies_are_swept()
    {
        _sb.Os("windows");
        var old = Directory.CreateDirectory(Path.Combine(_tmp, "clearcote-recover-old", "browser")).FullName;
        File.WriteAllText(Path.Combine(old, "chrome.exe"), "x");
        Directory.SetLastWriteTimeUtc(Path.GetDirectoryName(old)!, DateTime.UtcNow.AddMinutes(-2));
        Directory.CreateDirectory(Path.Combine(_tmp, "clearcote-recover-new")); // possibly a concurrent old-SDK launch mid-copy
        _probe = Sxs;
        await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), _exe, backoffMs: 1);
        Assert.Equal(new[] { "clearcote-recover-new" }, Names(_tmp));
    }

    [Fact]
    public async Task A_copy_a_browser_is_running_from_is_never_deleted()
    {
        // Windows refuses to delete an exe a process runs from; that refusal is what keeps a running browser's
        // copy (renaming its directory would succeed and prove nothing).
        var copy = Path.Combine(_base, "copy");
        var browser = Directory.CreateDirectory(Path.Combine(copy, "browser")).FullName;
        var running = Path.Combine(browser, "chrome.exe");
        if (OperatingSystem.IsWindows())
        {
            var ping = Path.Combine(Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows", "System32", "PING.EXE");
            File.Copy(ping, running);
            using var p = Process.Start(new ProcessStartInfo(running, "-n 30 127.0.0.1")
            {
                UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true,
            })!;
            try
            {
                Assert.False(WinLaunch.DiscardCopy(copy));
                Assert.True(File.Exists(running));
            }
            finally
            {
                p.Kill();
                p.WaitForExit();
            }
            await Task.Delay(200);
            Assert.True(WinLaunch.DiscardCopy(copy));
        }
        else
        {
            if (Environment.UserName == "root") return; // root ignores the permission that stands in for "in use"
            File.WriteAllText(running, "x");
            File.SetUnixFileMode(browser, UnixFileMode.UserRead | UnixFileMode.UserExecute); // unlink fails, as for a running exe on Windows
            try
            {
                Assert.False(WinLaunch.DiscardCopy(copy));
                Assert.True(File.Exists(running));
            }
            finally
            {
                File.SetUnixFileMode(browser, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            }
            Assert.True(WinLaunch.DiscardCopy(copy));
        }
    }

    [Fact]
    public void SpawnError_creates_the_process_without_running_it()
    {
        if (!OperatingSystem.IsWindows()) return; // CreateProcess: nothing to probe with elsewhere
        var dir = Directory.CreateDirectory(Path.Combine(_base, "probe")).FullName;
        var exe = Path.Combine(dir, "chrome.exe");
        File.Copy(Path.Combine(Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows", "System32", "PING.EXE"), exe);
        Assert.Null(WinLaunch.SpawnError(exe));
        var missing = Assert.IsType<Win32Exception>(WinLaunch.SpawnError(Path.Combine(dir, "missing.exe")));
        Assert.Equal(2, missing.NativeErrorCode); // ERROR_FILE_NOT_FOUND: not the side-by-side error
        Assert.False(WinLaunch.IsSxsError(missing));
        Assert.True(WinLaunch.IsSxsError(Sxs));
    }

    [Fact]
    public void RecoveryKey_matches_the_Python_and_Node_SDKs()
    {
        // _winlaunch.py recovery_key and winlaunch.ts recoveryKey compute the same, so the three SDKs share one copy.
        var b = Directory.CreateDirectory(Path.Combine(_base, "pro-9.9.9-r1", "browser")).FullName;
        var exe = Path.Combine(b, "chrome.exe");
        File.WriteAllText(exe, "12345");
        File.SetLastWriteTimeUtc(exe, DateTime.UnixEpoch.AddMilliseconds(1_700_000_000_123).AddTicks(4567)); // whole ms are kept
        var ident = $"{(OperatingSystem.IsWindows() ? b.ToLowerInvariant() : b)}|5|1700000000123";
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(ident))).ToLowerInvariant()[..12];
        Assert.Equal($"pro-9.9.9-r1-{hash}", WinLaunch.RecoveryKey(exe));
    }

    [Fact]
    public async Task ClearRecovered_removes_the_recovered_copies()
    {
        _sb.Os("windows");
        _probe = Sxs;
        await WinLaunch.WinAvRetryAsync(e => Task.FromResult(e), _exe, backoffMs: 1);
        Assert.True(Directory.Exists(Root));
        var (removed, bytes, inUse) = WinLaunch.ClearRecovered();
        Assert.Equal(1, removed);
        Assert.True(bytes > 0);
        Assert.Equal(0, inUse);
        Assert.False(Directory.Exists(Root));
    }

    // ── RemoveFailedLaunchDirs ───────────────────────────────────────────────

    private string Mk(string name)
    {
        var p = Directory.CreateDirectory(Path.Combine(_tmp, name)).FullName;
        Thread.Sleep(30); // distinct creation times
        return p;
    }

    private static Exception CallLog(string udd) =>
        new($"BrowserType.LaunchAsync: spawn UNKNOWN\nCall log:\n  - <launching> C:\\c\\chrome.exe --x --user-data-dir={udd} --remote-debugging-pipe");

    [Fact]
    public void Failed_launch_removes_its_profile_and_the_artifacts_dir_made_just_before_it()
    {
        Mk("playwright-artifacts-old111");
        var started = DateTime.UtcNow;
        Thread.Sleep(30);
        Mk("playwright-artifacts-ours22");
        var prof = Mk("playwright_chromiumdev_profile-abcDEF");
        Mk("playwright-artifacts-later3"); // another launch, after our profile
        var busy = Mk("playwright-artifacts-busy44");
        File.WriteAllText(Path.Combine(busy, "download.bin"), "x");
        WinLaunch.RemoveFailedLaunchDirs(CallLog(prof), started);
        Assert.Equal(new[] { "playwright-artifacts-busy44", "playwright-artifacts-later3", "playwright-artifacts-old111" }, Names(_tmp));
    }

    [Fact]
    public void Failed_persistent_launch_removes_only_its_artifacts_dir()
    {
        var mine = Mk("my-profile");
        var started = DateTime.UtcNow;
        Thread.Sleep(30);
        Mk("playwright-artifacts-ours22");
        WinLaunch.RemoveFailedLaunchDirs(CallLog(mine), started);
        Assert.Equal(new[] { "my-profile" }, Names(_tmp)); // the caller's profile is never touched
    }

    [Fact]
    public void Removes_nothing_when_it_cannot_tell_which_directory_is_its_own()
    {
        var started = DateTime.UtcNow;
        Thread.Sleep(30);
        Mk("playwright-artifacts-aaaaaa");
        Mk("playwright-artifacts-bbbbbb"); // a concurrent launch
        WinLaunch.RemoveFailedLaunchDirs(CallLog(Path.Combine(_tmp, "clearcote-run-x")), started);
        Assert.Equal(2, Names(_tmp).Length);
    }

    [Fact]
    public void Needs_a_Playwright_call_log()
    {
        // A raw spawn (ServeAsync) never made Playwright directories: nothing of anyone else's goes.
        var started = DateTime.UtcNow;
        Thread.Sleep(30);
        Mk("playwright-artifacts-aaaaaa");
        WinLaunch.RemoveFailedLaunchDirs(Sxs, started);
        Assert.Single(Names(_tmp));
    }
}
