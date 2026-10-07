using System.Diagnostics;
using System.Text.Json;
using Xunit;

namespace Clearcote.Tests;

// Separate processes installing one build at once, through the public PRO download path against a local
// fake of the download route (a small fake archive, no real browser). Field failure this guards: right after
// a new PRO build shipped, two processes on one host both found it missing and installed into the same folder
// at the same time; one removed the browser files the other was already running and died at startup.
//
// The lock is shared with the Python and Node SDKs, so the second test plays the other SDK's part by writing
// the lock file exactly as they do. Uses only public API, so it also runs against a build without the lock.
public class InstallLockProcessTests : IDisposable
{
    private readonly List<string> _temp = new();

    public void Dispose()
    {
        foreach (var dir in _temp) TestTemp.Remove(dir);
    }

    private string Cache()
    {
        var dir = TestTemp.Create("cc-lockp-");
        _temp.Add(dir);
        return dir;
    }

    private static string DotnetHost()
    {
        var env = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
        var self = Environment.ProcessPath;
        return self is not null && Path.GetFileNameWithoutExtension(self).Equals("dotnet", StringComparison.OrdinalIgnoreCase) ? self : "dotnet";
    }

    private sealed record ChildResult(int Code, string Out, string Err, string? Path);

    /// Program.Main's "install-child" mode in a process of its own.
    private static async Task<ChildResult> RunChildAsync(string apiBase, string cache)
    {
        var psi = new ProcessStartInfo(DotnetHost())
        {
            UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true,
            ArgumentList = { typeof(Program).Assembly.Location, "install-child", apiBase, cache },
        };
        using var p = Process.Start(psi)!;
        var stdout = p.StandardOutput.ReadToEndAsync();
        var stderr = p.StandardError.ReadToEndAsync();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(120));
        try { await p.WaitForExitAsync(cts.Token); }
        catch (OperationCanceledException) { p.Kill(entireProcessTree: true); }
        var output = await stdout;
        var line = output.Split('\n').Select(l => l.Trim()).LastOrDefault(l => l.StartsWith('{'));
        var path = line is null ? null : JsonDocument.Parse(line).RootElement.GetProperty("path").GetString();
        return new ChildResult(p.HasExited ? p.ExitCode : -1, output, await stderr, path);
    }

    [Fact]
    public async Task Two_processes_install_one_build_once()
    {
        if (OperatingSystem.IsMacOS()) return; // the PRO route serves Windows and Linux
        var cache = Cache();
        await using var srv = new FakeBuildServer(metaBarrier: 2, archiveDelayMs: 1000); // both reach the cache check together
        var results = await Task.WhenAll(RunChildAsync(srv.Url, cache), RunChildAsync(srv.Url, cache));
        foreach (var r in results) Assert.True(r.Code == 0, $"installer exited {r.Code}:\n{r.Err}");
        Assert.NotNull(results[0].Path);
        Assert.Equal(results[0].Path, results[1].Path);
        Assert.Equal(1, srv.ArchiveHits); // one download; the other process waited and used it
        Assert.Equal(new[] { ".manifest.json", ".verified", "browser" },
                     FakeBuildServer.Entries(Path.Combine(cache, FakeBuildServer.Tag))); // no lock or temp left
    }

    [Fact]
    public async Task Waits_for_another_sdks_install_and_uses_it()
    {
        if (OperatingSystem.IsMacOS()) return;
        var cache = Cache();
        var @base = Path.Combine(cache, FakeBuildServer.Tag);
        Directory.CreateDirectory(@base);
        var lockFile = Path.Combine(@base, ".install-lock");
        File.WriteAllText(lockFile, JsonSerializer.Serialize(new // what the Python / Node SDK writes; this process plays it
        {
            pid = Environment.ProcessId, host = System.Net.Dns.GetHostName(), boot = BootId(), pidns = PidNamespace(),
            nonce = new string('a', 32), sdk = "python", created = 0,
        }));
        await using var srv = new FakeBuildServer(archiveDelayMs: 500);
        var child = RunChildAsync(srv.Url, cache);
        await srv.MetaSeen.Task.WaitAsync(TimeSpan.FromSeconds(60));
        await Task.Delay(1000); // the child has found the build missing by now
        var exe = FakeBuildServer.VerifiedTree(@base); // the other SDK finishes its install...
        File.Delete(lockFile); // ...and releases the lock
        var r = await child;
        Assert.True(r.Code == 0, $"installer exited {r.Code}:\n{r.Err}");
        Assert.Equal(exe, r.Path);
        Assert.Equal(0, srv.ArchiveHits); // it waited instead of downloading over the other install
        Assert.Equal(new[] { ".manifest.json", ".verified", "browser" }, FakeBuildServer.Entries(@base));
    }

    private static string? BootId()
    {
        try { var s = File.ReadAllText("/proc/sys/kernel/random/boot_id").Trim(); return s.Length > 0 ? s : null; }
        catch { return null; }
    }

    private static string? PidNamespace()
    {
        try { return new FileInfo("/proc/self/ns/pid").LinkTarget; }
        catch { return null; }
    }
}
