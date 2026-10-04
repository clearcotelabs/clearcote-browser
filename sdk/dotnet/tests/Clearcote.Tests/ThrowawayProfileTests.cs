using System.Diagnostics;
using Microsoft.Playwright;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// The throwaway profile of <see cref="Clearcote.LaunchEphemeralProfileAsync"/> is deleted only once
/// the browser that used it has exited. Deleting it on the Close event — ~100 ms before the browser
/// process exits on Linux — let Chrome write it back after the cleanup had marked itself done, and a
/// process that exited with its browser open leaked its profile every time (3 of 3 on Linux).
/// The pid/socket tests are POSIX-only: Chrome keeps those links in the profile on Linux and macOS.
/// </summary>
public class ThrowawayProfileTests
{
    private static string NewProfile() => TestTemp.Create("cc-throwaway-");

    /// A stand-in browser process, reaped by .NET when it exits.
    private static Process StartSleeper() =>
        Process.Start(new ProcessStartInfo("sleep", "60") { UseShellExecute = false })!;

    [Fact]
    public void Remove_deletes_the_profile_and_reports_it_gone()
    {
        var dir = NewProfile();
        Directory.CreateDirectory(Path.Combine(dir, "Default"));
        File.WriteAllText(Path.Combine(dir, "Local State"), "{}");
        var profile = new ThrowawayProfile(dir);
        Assert.True(profile.Remove());
        Assert.True(profile.Done);
        Assert.False(Directory.Exists(dir));
    }

    [Fact]
    public void The_pid_is_the_last_field_of_the_SingletonLock_link_whatever_the_hostname()
    {
        if (OperatingSystem.IsWindows()) return;
        var dir = NewProfile();
        try
        {
            File.CreateSymbolicLink(Path.Combine(dir, "SingletonLock"), "build-host-01-eu-3138138");
            Assert.Equal(3138138, ThrowawayProfile.BrowserProcessFiles(dir).Pid);
        }
        finally { TestTemp.Remove(dir); }
    }

    [Fact]
    public async Task Remove_waits_for_the_browser_process_to_exit_before_deleting()
    {
        if (OperatingSystem.IsWindows()) return;
        var dir = NewProfile();
        using var browser = StartSleeper();
        try
        {
            File.CreateSymbolicLink(Path.Combine(dir, "SingletonLock"), $"test-host-{browser.Id}");
            var profile = new ThrowawayProfile(dir);
            var removing = Task.Run(() => profile.Remove());
            await Task.Delay(400);
            Assert.False(removing.IsCompleted);  // still running: deleting now is what leaked
            Assert.True(Directory.Exists(dir));
            browser.Kill();
            await browser.WaitForExitAsync();
            Assert.True(await removing.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.False(Directory.Exists(dir));
        }
        finally
        {
            try { browser.Kill(); } catch { }
            TestTemp.Remove(dir);
        }
    }

    [Fact]
    public void Remove_takes_the_browsers_singleton_socket_directory_with_it()
    {
        if (OperatingSystem.IsWindows()) return;
        var dir = NewProfile();
        var sock = Directory.CreateTempSubdirectory("org.chromium.Chromium.").FullName;
        File.WriteAllText(Path.Combine(sock, "SingletonSocket"), "");
        File.CreateSymbolicLink(Path.Combine(dir, "SingletonSocket"), Path.Combine(sock, "SingletonSocket"));
        try
        {
            Assert.True(new ThrowawayProfile(dir).Remove());
            Assert.False(Directory.Exists(sock));
        }
        finally { TestTemp.Remove(sock); TestTemp.Remove(dir); }
    }

    [Fact]
    public void A_SingletonSocket_link_to_anything_else_is_never_followed()
    {
        if (OperatingSystem.IsWindows()) return;
        var dir = NewProfile();
        var precious = TestTemp.Create("cc-precious-");
        File.WriteAllText(Path.Combine(precious, "SingletonSocket"), "");
        File.CreateSymbolicLink(Path.Combine(dir, "SingletonSocket"), Path.Combine(precious, "SingletonSocket"));
        try
        {
            Assert.True(new ThrowawayProfile(dir).Remove());
            Assert.True(File.Exists(Path.Combine(precious, "SingletonSocket")));
        }
        finally { TestTemp.Remove(precious); TestTemp.Remove(dir); }
    }

    /// Live: the real Close sequence. A handler subscribed after the SDK's runs after it, so what it
    /// sees is what the SDK's cleanup left behind: the old cleanup had deleted the profile while the
    /// browser was still running. Skipped unless CLEARCOTE_LIVE_ENGINE points at a chrome binary.
    /// (Reads the pid itself rather than through ThrowawayProfile, so it also runs on the old code.)
    [Fact]
    public async Task LaunchEphemeralProfile_deletes_the_profile_only_after_the_browser_has_exited()
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe) || OperatingSystem.IsWindows()) return;
        var temp = TestTemp.Create("cc-throwaway-live-");
        using var sb = new Sandbox().Env("TMPDIR", temp);
        try
        {
            var context = await Clearcote.LaunchEphemeralProfileAsync(new LaunchOptions
            {
                ExecutablePath = exe, Args = new[] { "--no-sandbox" }, Quiet = true,
            });
            var dir = Assert.Single(Directory.GetDirectories(temp, "clearcote-run-*"));
            var link = new FileInfo(Path.Combine(dir, "SingletonLock")).LinkTarget!;
            var pid = int.Parse(link[(link.LastIndexOf('-') + 1)..]);
            bool? deletedUnderALiveBrowser = null;
            context.Close += (_, _) => deletedUnderALiveBrowser = !Directory.Exists(dir) && IsRunning(pid);
            await context.CloseAsync();
            Assert.False(deletedUnderALiveBrowser);
            Assert.False(Directory.Exists(dir));
        }
        finally { TestTemp.Remove(temp); }
    }

    private static bool IsRunning(int pid)
    {
        try { using var p = Process.GetProcessById(pid); return !p.HasExited; }
        catch (ArgumentException) { return false; }
        catch (InvalidOperationException) { return false; }
    }
}
