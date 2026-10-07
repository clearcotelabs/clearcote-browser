using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// A Linux persona on a Windows host. There the GPU runs through Direct3D 11, which clamps the WebGL/WebGPU
/// limits, so a Linux claim reports limits no real Linux machine has. The SDK says so once per process, for a
/// local launch only: a Docker launch runs on Linux in its container and a cloud launch runs remotely, and
/// pass-through (no persona) claims nothing. Mirrors the Linux-persona tests of the Python and Node suites.
/// </summary>
/// <remarks>
/// The launch tests run the real LaunchAsync, LaunchPersistentContextAsync and ServeAsync with the host mocked
/// as Windows (Native.OsTagOverride) against a stand-in engine that exits at once: the launch then fails, after
/// its warnings, which is all these tests need. The stand-in is a shell script, so those tests are POSIX-only.
/// </remarks>
public class LinuxPersonaWindowsHostTests : IDisposable
{
    private const string Line = "a Linux persona on a Windows host";
    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();
    private readonly TextWriter _savedErr = Console.Error;
    private readonly StringWriter _err = new();

    public LinuxPersonaWindowsHostTests()
    {
        // No licence (a launch would take a real lease), no cloud key, warnings on.
        _sb.TempHome();
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_API_KEY", null).Env("CLEARCOTE_NO_WARN", null)
            .Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null).Env("CLEARCOTE_BINARY", null)
            .Env("CLEARCOTE_BROWSER_VERSION", null);
        LaunchWarnings.ForgetSaid();
        Console.SetError(TextWriter.Synchronized(_err));
    }

    public void Dispose()
    {
        Console.SetError(_savedErr);
        LaunchWarnings.ForgetSaid();
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private int Said() => _err.ToString().Split(Line).Length - 1;

    private static HashSet<string> Codes(FingerprintOptions o, string host = "windows")
        => LaunchWarnings.ForPersonaHost(o, host).Select(w => w.Code).ToHashSet();

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    /// A stand-in engine that exits at once (with pass-through, so Fingerprint = "off" is applied, not gated away).
    private string Standin()
    {
        var exe = Path.Combine(Dir("cc-lpw-engine-"), "chrome");
        File.WriteAllText(exe, "#!/bin/sh\nexit 1\n# \0fingerprint-passthrough\0\n");
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private static LaunchOptions Opts(string exe, string? platform, string? fingerprint) => new()
    {
        ExecutablePath = exe, Platform = platform, Fingerprint = fingerprint, Headless = true, Timeout = 20000,
    };

    // ── the warning ──────────────────────────────────────────────────────────

    [Fact]
    public void Flags_a_linux_claim_on_a_windows_host()
    {
        Assert.Contains(LaunchWarnings.LinuxPersonaWindowsHost, Codes(new FingerprintOptions { Platform = "linux", Fingerprint = "17" }));
        Assert.Contains(LaunchWarnings.LinuxPersonaWindowsHost, Codes(new FingerprintOptions { Platform = " Linux " }));  // no seed: still a Linux claim
    }

    [Fact]
    public void Not_for_a_windows_claim_nor_off_windows()
    {
        Assert.Empty(Codes(new FingerprintOptions { Platform = "windows", Fingerprint = "17" }));
        Assert.Empty(Codes(new FingerprintOptions { Fingerprint = "17" }));   // no platform: the host's own
        Assert.Empty(Codes(new FingerprintOptions { Platform = "linux", Fingerprint = "17" }, "linux"));
        Assert.Empty(Codes(new FingerprintOptions { Platform = "linux", Fingerprint = "17" }, "macos"));
    }

    [Fact]
    public void Not_without_a_persona()
    {
        // Pass-through runs with no persona at all (the engine presents the real host); nothing set claims the host.
        Assert.Empty(Codes(new FingerprintOptions { Platform = "linux", Fingerprint = "off" }));
        Assert.Empty(Codes(new FingerprintOptions()));
    }

    [Fact]
    public void Is_said_once_per_process_and_quiet_neither_says_it_nor_uses_it_up()
    {
        var ws = LaunchWarnings.ForPersonaHost(new FingerprintOptions { Platform = "linux", Fingerprint = "17" }, "windows");
        LaunchWarnings.EmitOnce(ws, quiet: true);
        Assert.Equal(0, Said());
        for (var i = 0; i < 3; i++) LaunchWarnings.EmitOnce(ws, quiet: false);
        Assert.Equal(1, Said());
        var text = _err.ToString();
        Assert.Contains("clearcote: warning: a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL)", text);
        Assert.Contains("Platform = \"windows\"", text);
        Assert.Contains("Docker = true", text);
    }

    // ── the launches ─────────────────────────────────────────────────────────

    [Fact]
    public async Task A_local_launch_says_it_once()
    {
        if (OperatingSystem.IsWindows()) return;
        _sb.Os("windows");
        var exe = Standin();
        for (var i = 0; i < 2; i++)
            await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-lpw-udd-"), Opts(exe, "linux", "17")));
        Assert.Equal(1, Said());
    }

    [Fact]
    public async Task Every_local_entry_point_says_it()
    {
        if (OperatingSystem.IsWindows()) return;
        _sb.Os("windows");
        var exe = Standin();
        await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(Opts(exe, "linux", "17")));
        Assert.Equal(1, Said());
        LaunchWarnings.ForgetSaid();
        await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync(new ServeOptions
        {
            ExecutablePath = exe, Platform = "linux", Fingerprint = "17", Headless = true, ReadyTimeoutMs = 5000,
        }));
        Assert.Equal(2, Said());
    }

    [Fact]
    public async Task A_local_launch_with_a_windows_persona_or_none_does_not()
    {
        if (OperatingSystem.IsWindows()) return;
        _sb.Os("windows");
        var exe = Standin();
        foreach (var (platform, fingerprint) in new[] { ("windows", "17"), ("linux", "off"), ((string?)null, (string?)null) })
            await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-lpw-udd-"), Opts(exe, platform, fingerprint)));
        Assert.Equal(0, Said());
    }

    [Fact]
    public async Task A_docker_or_cloud_launch_does_not()
    {
        _sb.Os("windows");
        var which = DockerLaunch.Which;
        DockerLaunch.Which = () => null;   // no docker here: the Docker launch stops at its own check
        try
        {
            await Assert.ThrowsAsync<DockerUnavailableException>(() =>
                Clearcote.LaunchAsync(new LaunchOptions { Docker = true, Platform = "linux", Fingerprint = "17" }));
            await Assert.ThrowsAnyAsync<Exception>(() =>   // no API key: the cloud launch stops before any request
                Clearcote.LaunchAsync(new LaunchOptions { Cloud = true, Platform = "linux", Fingerprint = "17" }));
        }
        finally
        {
            DockerLaunch.Which = which;
        }
        Assert.Equal(0, Said());
    }
}
