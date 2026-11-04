using Xunit;

namespace Clearcote.Tests;

/// <summary>The persona host-facts probe says why it got no answer (CR-2VFYCD). Mirrors tests/test_hostfacts_probe.py.</summary>
public class HostFactsProbeTests
{
    private const string Refusal = "[clearcote] no valid license lease for this run. Set a license key (CLEARCOTE_LICENSE_KEY) via the SDK and check your plan's concurrency limit.\r\n";

    [Fact]
    public void ReturnsStdoutOfACleanRun() =>
        Assert.Contains("nvidia", PersonaToken.DescribeProbe("{\"gpu_vendor\":\"nvidia\"}\n", "", 0));

    [Fact]
    public void NamesTheEnginesRefusal()
    {
        var e = Assert.Throws<InvalidOperationException>(() => PersonaToken.DescribeProbe("", Refusal, 1));
        Assert.StartsWith("it exited with code 1: no valid license lease for this run.", e.Message);
    }

    [Fact]
    public void NamesACleanExitWithoutOutput() =>
        Assert.Equal("it exited without output", Assert.Throws<InvalidOperationException>(() => PersonaToken.DescribeProbe(" \n", "", 0)).Message);

    [Fact]
    public void RetriesFromARecoveredCopyOnTheWindowsRace()
    {
        var home = Path.Combine(Path.GetTempPath(), "cc-probe-" + Guid.NewGuid().ToString("N"));
        var exe = Path.Combine(home, "cache", "chrome.exe");
        Directory.CreateDirectory(Path.GetDirectoryName(exe)!);
        File.WriteAllText(exe, "x");
        using var sb = new Sandbox();
        sb.TempHome();
        try
        {
            var seen = new List<string>();
            var got = PersonaToken.SpawnProbe(exe, e =>
            {
                seen.Add(e);
                if (e == exe) throw new System.ComponentModel.Win32Exception(14001, "The application has failed to start because its side-by-side configuration is incorrect.");
                return "ok";
            }, windows: true);
            Assert.Equal("ok", got);
            Assert.Equal(2, seen.Count);
            Assert.NotEqual(exe, seen[1]);
        }
        finally { try { Directory.Delete(home, true); } catch { /* best effort */ } }
    }

    [Fact]
    public void ANonRaceFailureIsNotRetried()
    {
        var calls = 0;
        Assert.Throws<FileNotFoundException>(() => PersonaToken.SpawnProbe<string>("C:/nope/chrome.exe", _ => { calls++; throw new FileNotFoundException("gone"); }, windows: true));
        Assert.Equal(1, calls);
    }
}
