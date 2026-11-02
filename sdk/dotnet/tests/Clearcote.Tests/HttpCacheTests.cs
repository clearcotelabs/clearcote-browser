using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// HttpCacheDir (<see cref="HttpCache"/>): one locked slot per concurrent launch, stale locks taken over, refused off
/// this machine, and --disk-cache-dir=&lt;slot&gt; reaching the engine. Mirrors sdk/node/test/httpcache.test.ts and
/// sdk/python/tests/test_httpcache.py.
/// </summary>
/// <remarks>The launch-wiring tests run a stand-in engine (a shell script, so POSIX-only, as in AutofillPrefsTests)
/// that writes its arguments next to itself and exits, so the launch fails and must free its slot.</remarks>
public class HttpCacheTests : IDisposable
{
    private readonly string _dir = TestTemp.Create("cc-httpcache-");

    public void Dispose() => TestTemp.Remove(_dir);

    private string Lock(int n) => Path.Combine(_dir, $"slot-{n}.lock");
    private string Slot(int n) => Path.Combine(_dir, $"slot-{n}");

    [Fact]
    public void Takes_slot_0_and_marks_it_with_this_pid()
    {
        var s = HttpCache.Acquire(_dir)!;
        Assert.Equal(Slot(0), s.Dir);
        Assert.True(Directory.Exists(s.Dir));
        Assert.Equal(Environment.ProcessId.ToString(), File.ReadAllText(Lock(0)));
        s.Release();
        Assert.False(File.Exists(Lock(0)));
        Assert.True(Directory.Exists(s.Dir));   // the cache itself stays for the next run
        s.Release();                            // idempotent
    }

    [Fact]
    public void Concurrent_launches_get_different_slots_and_a_released_one_is_reused()
    {
        var a = HttpCache.Acquire(_dir)!;
        var b = HttpCache.Acquire(_dir)!;
        var c = HttpCache.Acquire(_dir)!;
        Assert.Equal(new[] { Slot(0), Slot(1), Slot(2) }, new[] { a.Dir, b.Dir, c.Dir });
        b.Release();
        var d = HttpCache.Acquire(_dir)!;
        Assert.Equal(Slot(1), d.Dir);
        a.Release(); c.Release(); d.Release();
    }

    [Fact]
    public void Takes_over_the_lock_of_a_process_that_is_gone()
    {
        Directory.CreateDirectory(_dir);
        File.WriteAllText(Lock(0), "999999999");
        File.WriteAllText(Lock(1), "not a pid");
        var s = HttpCache.Acquire(_dir)!;
        Assert.Equal(Slot(0), s.Dir);
        Assert.Equal(Environment.ProcessId.ToString(), File.ReadAllText(Lock(0)));
        s.Release();
    }

    [Fact]
    public void Leaves_a_live_lock_alone_and_never_removes_one_it_no_longer_owns()
    {
        if (OperatingSystem.IsWindows()) return;   // pid 1: always alive on POSIX
        const string other = "1";
        File.WriteAllText(Lock(0), other);
        var s = HttpCache.Acquire(_dir)!;
        Assert.Equal(Slot(1), s.Dir);
        File.WriteAllText(Lock(1), other);   // taken over by someone else meanwhile
        s.Release();
        Assert.Equal(other, File.ReadAllText(Lock(1)));
    }

    [Fact]
    public void Null_when_every_slot_is_held()
    {
        if (OperatingSystem.IsWindows()) return;
        for (var n = 0; n < HttpCache.Slots; n++) File.WriteAllText(Lock(n), "1");
        Assert.Null(HttpCache.Acquire(_dir, quiet: true));
    }

    [Fact]
    public void Take_appends_disk_cache_dir_after_the_callers_args_and_leaves_the_callers_options_alone()
    {
        var mine = new LaunchOptions { HttpCacheDir = _dir, Args = new[] { "--foo" }, Headless = true };
        var (o, release) = HttpCache.Take(mine);
        Assert.Equal(new[] { "--foo", $"--disk-cache-dir={Slot(0)}" }, o.Args);
        Assert.Null(o.HttpCacheDir);
        Assert.Equal(new[] { "--foo" }, mine.Args);
        Assert.Equal(_dir, mine.HttpCacheDir);
        release();
        Assert.False(File.Exists(Lock(0)));
    }

    [Fact]
    public void Refuses_a_blank_path_and_an_explicit_disk_cache_dir()
    {
        Assert.Throws<ArgumentException>(() => HttpCache.Check("  ", null));
        var e = Assert.Throws<ArgumentException>(() =>
            HttpCache.Take(new LaunchOptions { HttpCacheDir = _dir, Args = new[] { "--disk-cache-dir=/x" } }));
        Assert.Contains("cannot be combined", e.Message);
        Assert.False(File.Exists(Lock(0)));   // refused before taking a slot
    }

    [Fact]
    public async Task LaunchAsync_has_no_profile_and_refuses_it_before_anything_starts()
    {
        using var sb = Isolated();
        var e = await Assert.ThrowsAsync<ArgumentException>(() => Clearcote.LaunchAsync(new LaunchOptions
        {
            ExecutablePath = "/nonexistent/chrome", HttpCacheDir = _dir, Cloud = false,
        }));
        Assert.Equal(HttpCache.NeedsProfile, e.Message);
        Assert.False(File.Exists(Lock(0)));
    }

    [Fact]
    public void Cloud_refuses_it()
    {
        var e = Assert.Throws<ArgumentException>(() => CloudLaunch.SessionOptionsOf(new LaunchOptions { HttpCacheDir = "/tmp/c" }));
        Assert.Equal("HttpCacheDir is not available for cloud browsers", e.Message);
    }

    [Fact]
    public void Docker_refuses_it()
    {
        var e = Assert.Throws<ArgumentException>(() => DockerLaunch.ContainerEnv(new LaunchOptions { HttpCacheDir = "/tmp/c" }));
        Assert.StartsWith("HttpCacheDir is not available when LaunchAsync runs Clearcote in Docker", e.Message);
    }

    // ── launch wiring (POSIX: a shell-script stand-in engine) ──────────────────────────────────────

    private string Standin()
    {
        var exe = Path.Combine(TestTemp.Create("cc-httpcache-standin-"), "chrome");
        File.WriteAllText(exe, "#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" >> \"$d/argv\"\nexit 1\n");
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private static Sandbox Isolated() => new Sandbox().Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_CLOUD", null)
        .Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null)
        .Env("CLEARCOTE_WIDEVINE", null).Env("CLEARCOTE_OFFLINE_KEY", null).Env("CLEARCOTE_NO_WARN", "1");

    private static string[] CacheArgs(string exe) => File.ReadAllLines(Path.Combine(Path.GetDirectoryName(exe)!, "argv"))
        .Where(a => a.StartsWith("--disk-cache-dir")).ToArray();

    [Theory]
    [InlineData("throwaway")]
    [InlineData("persistent")]
    [InlineData("serve")]
    public async Task Every_entry_point_passes_the_slot_and_a_failed_launch_frees_it(string how)
    {
        if (OperatingSystem.IsWindows()) return;
        using var sb = Isolated();
        sb.TempHome();
        var exe = Standin();
        try
        {
            var o = new LaunchOptions { ExecutablePath = exe, Headless = true, Quiet = true, Timeout = 20000, HttpCacheDir = _dir };
            Task launch = how switch
            {
                "throwaway" => Clearcote.LaunchEphemeralProfileAsync(o),
                "persistent" => Clearcote.LaunchPersistentContextAsync(Path.Combine(_dir, "..", Path.GetFileName(_dir) + "-udd"), o),
                _ => Clearcote.ServeAsync(new ServeOptions { ExecutablePath = exe, Quiet = true, ReadyTimeoutMs = 5000, HttpCacheDir = _dir }),
            };
            await Assert.ThrowsAnyAsync<Exception>(() => launch);
            Assert.Contains($"--disk-cache-dir={Slot(0)}", CacheArgs(exe));
            Assert.False(File.Exists(Lock(0)), "a failed launch must free its slot");
            Assert.Equal(_dir, o.HttpCacheDir);   // the caller's options are never changed
        }
        finally
        {
            TestTemp.Remove(Path.GetDirectoryName(exe)!);
            TestTemp.Remove(Path.Combine(Path.GetDirectoryName(_dir)!, Path.GetFileName(_dir) + "-udd"));
        }
    }
}
