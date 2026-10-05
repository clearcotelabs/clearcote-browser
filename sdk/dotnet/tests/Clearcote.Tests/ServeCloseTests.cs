using System.Net.Sockets;
using System.Text.Json;
using System.Text.RegularExpressions;
using Xunit;

namespace Clearcote.Tests;

/// <summary>
/// <see cref="Server.CloseAsync"/> waits for the engine to exit, then removes what it leaves in the temp
/// directory: the profile ServeAsync made, and on Linux and macOS the engine's singleton-socket directory.
/// </summary>
/// <remarks>
/// WHY: CloseAsync killed the engine and returned at once, deleting the profile without waiting for the
/// engine to go. A killed engine never removes its org.chromium.Chromium.* socket directory, so every
/// ServeAsync + CloseAsync on Linux left one in the temp directory (measured with the open build, in all
/// three SDKs; the live ServeGeometryTests left three per run).
/// The stand-in engine does what Chrome does there: links its pid and socket from the profile and keeps
/// the socket in a temp directory of its own, which it leaves behind when it is stopped. It is a shell
/// script, so those tests are POSIX-only. The last test runs a real engine (CLEARCOTE_TEST_BINARY or
/// CLEARCOTE_LIVE_ENGINE).
/// </remarks>
public class ServeCloseTests
{
    private const string Standin = """
        #!/bin/sh
        udd=
        for a in "$@"; do case "$a" in --user-data-dir=*) udd="${a#--user-data-dir=}" ;; esac; done
        sock=$(mktemp -d "${TMPDIR:-/tmp}/org.chromium.Chromium.XXXXXX") || exit 1
        : > "$sock/SingletonSocket"
        ln -s "$sock/SingletonSocket" "$udd/SingletonSocket"
        ln -s "standin-host-$$" "$udd/SingletonLock"
        shutdown() { i=0; while [ $i -lt 10 ]; do mkdir -p "$udd/Default" && : > "$udd/Default/Shutdown $i"; i=$((i + 1)); sleep 0.05; done; exit 0; }
        trap shutdown TERM
        while :; do sleep 0.05; done

        """;

    /// <summary>
    /// A temp directory of the test's own that ServeAsync and its engine see as theirs (anything left in
    /// it leaked); no licence (it would take a real lease), no saved profiles, no warnings. Short: a real
    /// engine's socket path (&lt;temp&gt;/org.chromium.Chromium.XXXXXX/SingletonSocket) has to fit a Unix
    /// socket address, 107 bytes, and the engine exits at once when it does not.
    /// </summary>
    private static (Sandbox Sandbox, string Temp) Isolated()
    {
        var sb = new Sandbox();
        sb.TempHome();   // before TMPDIR moves: HOME is not part of what is checked
        var temp = TestTemp.Create("cc-sc-");
        sb.Env("TMPDIR", temp).Env("TMP", temp).Env("TEMP", temp).Env("CLEARCOTE_NO_WARN", "1")
            .Env("CLEARCOTE_LICENSE_KEY", null).Env("CLEARCOTE_BINARY", null).Env("CLEARCOTE_BROWSER_VERSION", null);
        Assert.Null(License.ResolveLicenseKey(null));   // harness: no ServeAsync here takes a real lease
        return (sb, temp);
    }

    private static string WriteStandin()
    {
        var dir = TestTemp.Create("cc-engine-");
        var exe = Path.Combine(dir, "chrome");
        File.WriteAllText(exe, Standin.Replace("\r\n", "\n"));
        File.SetUnixFileMode(exe, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        return exe;
    }

    private static string[] Left(string temp) => Directory.EnumerateFileSystemEntries(temp).Select(Path.GetFileName).ToArray()!;

    /// <summary>
    /// Stands in for the engine's CDP endpoint, which ServeAsync polls until it answers. Like the
    /// engine's, it answers once the browser is up, its singleton included: the profile has a
    /// SingletonLock link (in <paramref name="profile"/>, else in the profile ServeAsync made in temp).
    /// </summary>
    private sealed class CdpEndpoint(string temp, string? profile = null) : LocalServer
    {
        private bool Up() =>
            (profile is not null ? new[] { profile } : Directory.GetDirectories(temp, "clearcote-serve-*"))
            .Any(d => new FileInfo(Path.Combine(d, "SingletonLock")).LinkTarget is not null);

        protected override async Task HandleAsync(NetworkStream s, CancellationToken ct)
        {
            if (await ReadHeadAsync(s, ct) is null) return;
            for (var i = 0; i < 500 && !Up(); i++) await Task.Delay(10, ct);
            await s.WriteAsync("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"u8.ToArray(), ct);
        }
    }

    [Fact]
    public async Task CloseAsync_waits_for_the_engine_to_exit_then_removes_its_profile_and_socket_directory()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = WriteStandin();
        var (sb, temp) = Isolated();
        try
        {
            await using var cdp = new CdpEndpoint(temp);
            var srv = await Clearcote.ServeAsync(new ServeOptions { ExecutablePath = exe, Port = cdp.Port, Quiet = true });
            // the harness: a profile and the engine's socket directory, both in this test's temp directory
            Assert.Equal(new[] { "clearcote-serve-*", "org.chromium.Chromium.*" },
                Left(temp).Select(n => Regex.Replace(n, "[^-.]+$", "*")).Order().ToArray());
            await srv.CloseAsync();
            Assert.False(srv.IsAlive);
            await Task.Delay(700);   // an engine still shutting down would have written its profile back by now
            Assert.Empty(Left(temp));
        }
        finally { sb.Dispose(); TestTemp.Remove(temp); TestTemp.Remove(Path.GetDirectoryName(exe)!); }
    }

    [Fact]
    public async Task CloseAsync_keeps_a_profile_that_is_the_callers_and_removes_the_socket_directory_it_links_to()
    {
        if (OperatingSystem.IsWindows()) return;
        var exe = WriteStandin();
        var profile = TestTemp.Create("cc-profile-");
        var (sb, temp) = Isolated();
        try
        {
            await using var cdp = new CdpEndpoint(temp, profile);
            var srv = await Clearcote.ServeAsync(new ServeOptions { ExecutablePath = exe, Port = cdp.Port, Quiet = true, UserDataDir = profile });
            await srv.CloseAsync();
            Assert.Empty(Left(temp));
            Assert.NotNull(new FileInfo(Path.Combine(profile, "SingletonLock")).LinkTarget);   // the caller's profile, untouched
        }
        finally { sb.Dispose(); TestTemp.Remove(temp); TestTemp.Remove(profile); TestTemp.Remove(Path.GetDirectoryName(exe)!); }
    }

    [Fact]
    public async Task CloseAsync_with_a_real_engine_leaves_nothing_in_the_temp_directory()
    {
        var exe = Environment.GetEnvironmentVariable("CLEARCOTE_TEST_BINARY") is { Length: > 0 } b ? b
            : Environment.GetEnvironmentVariable("CLEARCOTE_LIVE_ENGINE");
        if (string.IsNullOrEmpty(exe)) return;
        var (sb, temp) = Isolated();
        try
        {
            var srv = await Clearcote.ServeAsync(new ServeOptions { ExecutablePath = exe, Quiet = true });
            using (var http = new HttpClient())
            using (var version = JsonDocument.Parse(await http.GetStringAsync($"{srv.CdpUrl}/json/version")))
                Assert.Contains("Chrom", version.RootElement.GetProperty("Browser").GetString());
            await srv.CloseAsync();
            Assert.False(srv.IsAlive);
            await Task.Delay(1000);
            Assert.Empty(Left(temp));
        }
        finally { sb.Dispose(); TestTemp.Remove(temp); }
    }
}
