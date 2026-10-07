using System.Diagnostics;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Two installers of the same build at once must not download over each other: one installs, the other
// waits and uses that tree. The lock is one protocol shared with the Python and Node SDKs (see
// Download.cs): an O_EXCL lock file with an owner record, a heartbeat, and stale-lock recovery.
public class InstallLockTests : IDisposable
{
    private readonly List<string> _temp = new();

    public void Dispose()
    {
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    private string Cache()
    {
        var dir = TestTemp.Create("cc-lock-");
        _temp.Add(dir);
        return dir;
    }

    private string Base() => Path.Combine(Cache(), FakeBuildServer.Tag);

    private static string LockPath(string @base) => Path.Combine(@base, Download.InstallLockFile);

    private static JsonElement ReadLock(string @base) => JsonDocument.Parse(File.ReadAllText(LockPath(@base))).RootElement;

    /// A lock file as any of the three SDKs writes it; <paramref name="age"/> back-dates its heartbeat.
    private static void WriteLock(string @base, TimeSpan age = default, long? pid = null, string? host = null,
                                  string sdk = "node", string nonce = "ffffffffffffffffffffffffffffffff")
    {
        Directory.CreateDirectory(@base);
        File.WriteAllText(LockPath(@base), JsonSerializer.Serialize(new
        {
            pid = pid ?? Environment.ProcessId, host = host ?? System.Net.Dns.GetHostName(), boot = Download.BootId(),
            pidns = Download.PidNamespace(), nonce, sdk, created = 0,
        }));
        if (age > TimeSpan.Zero) File.SetLastWriteTimeUtc(LockPath(@base), DateTime.UtcNow - age);
    }

    [Fact]
    public async Task Acquire_writes_an_owner_record_and_release_removes_it()
    {
        var @base = Base();
        var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(1));
        var rec = ReadLock(@base);
        Assert.Equal(Environment.ProcessId, rec.GetProperty("pid").GetInt32());
        Assert.Equal(held.Nonce, rec.GetProperty("nonce").GetString());
        Assert.Equal("dotnet", rec.GetProperty("sdk").GetString());
        Assert.Equal(System.Net.Dns.GetHostName(), rec.GetProperty("host").GetString());
        Assert.True(held.Held());
        held.Dispose();
        Assert.Empty(Directory.EnumerateFileSystemEntries(@base)); // no lock file, no breaker
        (await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(1))).Dispose(); // free again
    }

    [Fact]
    public async Task A_held_lock_makes_the_next_installer_wait_then_give_up_plainly()
    {
        var @base = Base();
        using var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(1));
        var sw = Stopwatch.StartNew();
        var err = await Assert.ThrowsAsync<TimeoutException>(() => Download.AcquireInstallLockAsync(@base, TimeSpan.FromMilliseconds(600)));
        Assert.True(sw.ElapsedMilliseconds >= 500); // it waited
        Assert.StartsWith("Gave up after 1 second waiting for another program to finish installing", err.Message);
        Assert.Contains($"process {Environment.ProcessId} on {System.Net.Dns.GetHostName()} (Clearcote .NET SDK)", err.Message);
        Assert.Contains(LockPath(@base), err.Message); // what to delete if nothing else is installing
    }

    [Fact]
    public async Task A_lock_left_by_a_dead_process_on_this_machine_is_taken_over_at_once()
    {
        int gone;
        var psi = OperatingSystem.IsWindows() ? new ProcessStartInfo("cmd.exe", "/c exit") : new ProcessStartInfo("true");
        psi.UseShellExecute = false;
        using (var p = Process.Start(psi)!)
        {
            p.WaitForExit();
            gone = p.Id;
        }
        var @base = Base();
        WriteLock(@base, pid: gone); // fresh heartbeat: only the pid tells it is dead
        var sw = Stopwatch.StartNew();
        using (var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(10)))
        {
            Assert.True(sw.ElapsedMilliseconds < 3000);
            Assert.True(held.Held());
        }
        Assert.Empty(Directory.EnumerateFileSystemEntries(@base));
    }

    [Fact]
    public async Task A_lock_without_a_heartbeat_is_taken_over()
    {
        // A holder on another machine or in another container cannot be asked whether it is alive: its
        // heartbeat (the lock file's mtime) decides.
        var @base = Base();
        WriteLock(@base, Download.LockStaleAfter + TimeSpan.FromSeconds(5), pid: 1, host: "another-machine");
        using var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(10));
        Assert.True(held.Held());
    }

    [Fact]
    public async Task A_live_lock_from_another_machine_is_respected()
    {
        var @base = Base();
        WriteLock(@base, TimeSpan.FromSeconds(5), pid: 1, host: "another-machine", sdk: "python");
        var err = await Assert.ThrowsAsync<TimeoutException>(() => Download.AcquireInstallLockAsync(@base, TimeSpan.FromMilliseconds(600)));
        Assert.Contains("process 1 on another-machine (Clearcote Python SDK)", err.Message);
        Assert.Equal("another-machine", ReadLock(@base).GetProperty("host").GetString()); // not broken
    }

    [Fact]
    public async Task A_lock_of_a_live_process_here_is_respected()
    {
        var @base = Base();
        WriteLock(@base); // this very process: alive
        var err = await Assert.ThrowsAsync<TimeoutException>(() => Download.AcquireInstallLockAsync(@base, TimeSpan.FromMilliseconds(600)));
        Assert.Contains("Clearcote Node SDK", err.Message);
    }

    [Fact]
    public async Task A_half_written_lock_is_taken_over_only_after_its_grace_period()
    {
        var @base = Base();
        Directory.CreateDirectory(@base);
        File.WriteAllText(LockPath(@base), ""); // created, owner record not written yet
        var err = await Assert.ThrowsAsync<TimeoutException>(() => Download.AcquireInstallLockAsync(@base, TimeSpan.FromMilliseconds(600)));
        Assert.Contains("a process that left no details", err.Message);
        File.SetLastWriteTimeUtc(LockPath(@base), DateTime.UtcNow - Download.LockUnreadableAfter - TimeSpan.FromSeconds(5));
        (await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(10))).Dispose();
    }

    [Fact]
    public async Task A_breaker_left_by_a_crashed_process_is_cleared()
    {
        var @base = Base();
        WriteLock(@base, Download.LockStaleAfter + TimeSpan.FromSeconds(5), host: "another-machine");
        var breaker = LockPath(@base) + ".break";
        File.WriteAllText(breaker, "");
        File.SetLastWriteTimeUtc(breaker, DateTime.UtcNow - Download.LockBreakerStaleAfter - TimeSpan.FromSeconds(5));
        (await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(10))).Dispose();
        Assert.Empty(Directory.EnumerateFileSystemEntries(@base));
    }

    [Fact]
    public async Task A_holder_that_lost_its_lock_does_not_delete_the_new_one()
    {
        var @base = Base();
        var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(1));
        WriteLock(@base, nonce: new string('e', 32)); // another process judged ours stale and took over
        Assert.False(held.Held());
        held.Dispose();
        Assert.Equal(new string('e', 32), ReadLock(@base).GetProperty("nonce").GetString());
    }

    // ── installs under the lock (the PRO route serves Windows and Linux) ──────────────────────────────

    [Fact]
    public async Task Waits_for_the_holder_then_uses_its_build_without_downloading()
    {
        if (OperatingSystem.IsMacOS()) return;
        var cache = Cache();
        var @base = Path.Combine(cache, FakeBuildServer.Tag);
        var held = await Download.AcquireInstallLockAsync(@base, TimeSpan.FromSeconds(1)); // another installer is busy
        await using var srv = new FakeBuildServer();
        var install = Download.ProEnsureBinaryAsync("test-key", new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true });
        await srv.MetaSeen.Task.WaitAsync(TimeSpan.FromSeconds(20));
        await Task.Delay(700); // the installer is waiting on the lock by now
        var exe = FakeBuildServer.VerifiedTree(@base); // the holder finishes...
        held.Dispose(); // ...and lets go
        Assert.Equal(exe, await install.WaitAsync(TimeSpan.FromSeconds(30)));
        Assert.Equal(0, srv.ArchiveHits); // used the holder's build: no second download
        Assert.Equal(new[] { ".manifest.json", ".verified", "browser" }, FakeBuildServer.Entries(@base));
    }

    [Fact]
    public async Task A_failed_install_leaves_no_lock_or_temp_files()
    {
        if (OperatingSystem.IsMacOS()) return;
        var cache = Cache();
        await using var srv = new FakeBuildServer(sha: new string('0', 64));
        var err = await Assert.ThrowsAnyAsync<Exception>(() =>
            Download.ProEnsureBinaryAsync("test-key", new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true }));
        Assert.Contains("SHA-256 mismatch", err.Message);
        Assert.Empty(FakeBuildServer.Entries(Path.Combine(cache, FakeBuildServer.Tag)));
    }

    [Fact]
    public async Task Leftovers_of_an_install_that_stopped_part_way_are_cleared()
    {
        if (OperatingSystem.IsMacOS()) return;
        var cache = Cache();
        var @base = Path.Combine(cache, FakeBuildServer.Tag);
        foreach (var leftover in new[] { ".tmp-0123", ".incoming", Path.Combine("browser", "half") })
            Directory.CreateDirectory(Path.Combine(@base, leftover));
        WriteLock(@base, Download.LockStaleAfter + TimeSpan.FromSeconds(5), host: "another-machine"); // its installer died
        await using var srv = new FakeBuildServer();
        var exe = await Download.ProEnsureBinaryAsync("test-key", new ProDownloadOptions { ApiBase = srv.Url, CacheDir = cache, Quiet = true });
        Assert.True(File.Exists(exe));
        Assert.Equal(new[] { ".manifest.json", ".verified", "browser" }, FakeBuildServer.Entries(@base));
        Assert.False(Directory.Exists(Path.Combine(@base, "browser", "half")));
    }
}
