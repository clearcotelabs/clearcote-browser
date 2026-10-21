using System.Text;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="LaunchOptions.PortableProfile"/> / <see cref="LaunchOptions.EncryptionKey"/>: the cookie encryption
/// key kept with the profile (Node's portableProfile / encryptionKey), on every local launch. The launch tests run
/// the real launches against a stand-in engine that writes down its command line and exits, so they are POSIX-only.
/// </summary>
public class PortableProfileTests : IDisposable
{
    private readonly Sandbox _sb = new();
    private readonly List<string> _dirs = new();

    public PortableProfileTests()
    {
        _sb.TempHome();
        _sb.Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_CLOUD", null).Env("CLEARCOTE_DOCKER", null)
            .Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null).Env(PersonaEnv.OptOutEnv, null);
    }

    public void Dispose()
    {
        _sb.Dispose();
        foreach (var d in _dirs) TestTemp.Remove(d);
    }

    private string Dir(string prefix)
    {
        var d = TestTemp.Create(prefix);
        _dirs.Add(d);
        return d;
    }

    private string Standin()
    {
        var exe = Path.Combine(Dir("cc-pp-engine-"), "chrome");
        File.WriteAllBytes(exe, Encoding.Latin1.GetBytes("#!/bin/sh\nd=$(dirname \"$0\")\nprintf '%s\\n' \"$@\" > \"$d/argv\"\nexit 1\n"));
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private async Task<List<string>> Argv(string entry, Action<LaunchOptions> set)
    {
        var exe = Standin();
        LaunchOptions o = entry == "serve" ? new ServeOptions { ReadyTimeoutMs = 20000 } : new LaunchOptions();
        o.ExecutablePath = exe; o.Headless = true; o.Quiet = true; o.Timeout = 20000;
        set(o);
        var e = entry switch
        {
            "launch" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchAsync(o)),
            "persistent" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchPersistentContextAsync(Dir("cc-pp-udd-"), o)),
            "ephemeral" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.LaunchEphemeralProfileAsync(o)),
            "serve" => await Assert.ThrowsAnyAsync<Exception>(() => Clearcote.ServeAsync((ServeOptions)o)),
            _ => throw new ArgumentException(entry),
        };
        var argv = Path.Combine(Path.GetDirectoryName(exe)!, "argv");
        Assert.True(File.Exists(argv), $"the stand-in engine never ran: {e}");
        return File.ReadAllLines(argv).ToList();
    }

    [Theory]
    [InlineData("launch")]
    [InlineData("persistent")]
    [InlineData("ephemeral")]
    [InlineData("serve")]
    public async Task Each_mode_reaches_the_engine_on_every_local_launch(string entry)
    {
        if (OperatingSystem.IsWindows()) return;
        var none = await Argv(entry, _ => { });
        Assert.DoesNotContain("--portable-profile", none);
        Assert.DoesNotContain(none, a => a.StartsWith("--profile-encryption-key", StringComparison.Ordinal));

        var portable = await Argv(entry, o => o.PortableProfile = true);
        Assert.Single(portable, "--portable-profile");

        // an explicit key wins over the generated-file mode
        var keyed = await Argv(entry, o => { o.PortableProfile = true; o.EncryptionKey = "s3cret"; });
        Assert.Single(keyed, "--profile-encryption-key=s3cret");
        Assert.DoesNotContain("--portable-profile", keyed);
    }

    [Fact]
    public void Options_are_copied_and_local_only()
    {
        var o = new LaunchOptions { PortableProfile = true, EncryptionKey = "k" };
        var copy = (LaunchOptions)o.Clone();
        Assert.True(copy.PortableProfile);
        Assert.Equal("k", copy.EncryptionKey);
        // a cloud browser and the Docker image refuse them by name rather than dropping them
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.PortableProfile));
        Assert.Contains(CloudLaunch.LocalOnlyOptions, p => p.Name == nameof(LaunchOptions.EncryptionKey));
        Assert.Contains(DockerLaunch.RefusedOptions, p => p.Name == nameof(LaunchOptions.PortableProfile));
        Assert.Contains(DockerLaunch.RefusedOptions, p => p.Name == nameof(LaunchOptions.EncryptionKey));
    }
}
