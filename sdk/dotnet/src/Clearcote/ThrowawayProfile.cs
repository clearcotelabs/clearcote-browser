using System.Diagnostics;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// A throwaway profile directory (<see cref="Clearcote.LaunchEphemeralProfileAsync"/>) and its
/// deletion — once the browser that used it has EXITED.
/// </summary>
/// <remarks>
/// <para>
/// NEVER UNDER A LIVE BROWSER. Playwright raises <c>Close</c> when the browser's pipe drops, which on
/// Linux is ~100 ms BEFORE the browser process exits, and Chrome keeps writing the profile until then.
/// Deleting it at that point either fails or succeeds and Chrome writes the directory back
/// (Default/, Local State, first_party_sets.db) after the cleanup has marked itself done — the Python
/// and Node SDKs leaked a clearcote-run-* directory per release smoke run that way. A process that
/// exited with its browser still open leaked every time (3 of 3 on Linux): the delete ran while the
/// browser was still up, and the browser wrote the profile back as it shut down after us.
/// </para>
/// <para>
/// So the <c>Close</c> handler first waits for the browser's process to exit (its pid is in the
/// profile, see <see cref="BrowserProcessFiles"/>), and <c>ProcessExit</c> closes a context that is
/// still open before deleting. Playwright raises <c>Close</c> before <c>CloseAsync</c> completes, so
/// the profile is gone by the time <c>CloseAsync</c> returns.
/// </para>
/// <para>
/// THE RETRY IS NOT DEFENSIVE PADDING: on Windows the browser can hold handles under the profile for
/// a moment after it closes, so a single removal silently fails and the directory leaks.
/// </para>
/// </remarks>
internal sealed class ThrowawayProfile
{
    private readonly int? _pid;
    private readonly string? _socketDir;
    private readonly object _gate = new();
    private EventHandler? _onProcessExit;

    public string Dir { get; }
    public bool Done { get; private set; }

    /// <summary>Read once the browser runs on <paramref name="dir"/>: its pid and socket links exist only then.</summary>
    public ThrowawayProfile(string dir)
    {
        Dir = dir;
        (_pid, _socketDir) = BrowserProcessFiles(dir);
    }

    /// <summary>
    /// The pid of the browser running on <paramref name="userDataDir"/> and its singleton-socket
    /// directory, read from the symlinks Chrome keeps in its profile on Linux and macOS while it runs:
    /// <c>SingletonLock</c> -> "&lt;hostname&gt;-&lt;pid&gt;", <c>SingletonSocket</c> ->
    /// "&lt;tmp&gt;/org.chromium.Chromium.XXXXXX/SingletonSocket" (that directory leaks when the browser
    /// is killed rather than closed). Windows has neither: nothing to wait for, and there its file
    /// locks make an early delete fail rather than succeed.
    /// </summary>
    internal static (int? Pid, string? SocketDir) BrowserProcessFiles(string userDataDir)
    {
        if (OperatingSystem.IsWindows()) return (null, null);
        int? pid = null;
        string? socketDir = null;
        try
        {
            var target = new FileInfo(Path.Combine(userDataDir, "SingletonLock")).LinkTarget;
            if (target is not null && int.TryParse(target[(target.LastIndexOf('-') + 1)..], out var n) && n > 0) pid = n;
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        try
        {
            var target = new FileInfo(Path.Combine(userDataDir, "SingletonSocket")).LinkTarget;
            var dir = target is null ? null : Path.GetDirectoryName(target);
            // Only ever Chrome's own temp directory: the path comes out of a file.
            var name = dir is null ? "" : Path.GetFileName(dir);
            if (name.StartsWith("org.chromium.Chromium.", StringComparison.Ordinal) || name.StartsWith("com.google.Chrome.", StringComparison.Ordinal))
                socketDir = dir;
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        return (pid, socketDir);
    }

    /// <summary>True once process <paramref name="pid"/> has exited (or there is none); false if it still runs at the deadline.</summary>
    internal static bool WaitForExit(int? pid, TimeSpan timeout)
    {
        if (pid is null) return true;
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            try
            {
                using var p = Process.GetProcessById(pid.Value);
                if (p.HasExited) return true;
            }
            catch (ArgumentException) { return true; }          // not running
            catch (InvalidOperationException) { return true; }
            if (DateTime.UtcNow >= deadline) return false;
            Thread.Sleep(50);
        }
    }

    /// <summary>
    /// Delete the profile (and the browser's socket directory) once the browser has exited. False
    /// while it still runs — a later trigger retries — or when the delete keeps failing.
    /// <paramref name="final"/> (process exit, no later trigger): delete anyway after the wait.
    /// </summary>
    public bool Remove(bool final = false)
    {
        lock (_gate)
        {
            if (Done) return true;
            if (!WaitForExit(_pid, TimeSpan.FromSeconds(final ? 5 : 10)) && !final) return false;
            if (_socketDir is not null) { try { Directory.Delete(_socketDir, recursive: true); } catch { } }
            for (var attempt = 0; attempt < 6; attempt++)
            {
                try { Directory.Delete(Dir, recursive: true); }
                // DirectoryNotFoundException included: it is thrown for ANY entry that vanished during
                // the walk, not only for the directory itself — so only the check below means gone.
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                if (!Directory.Exists(Dir))
                {
                    Done = true;
                    if (_onProcessExit is not null) AppDomain.CurrentDomain.ProcessExit -= _onProcessExit;
                    return true;
                }
                if (attempt < 5) Thread.Sleep(250 * (attempt + 1));
            }
            return false;
        }
    }

    /// <summary>
    /// Delete the profile when <paramref name="context"/> closes, and at process exit — closing a
    /// context still open first, so its browser is not writing the profile while it is deleted.
    /// </summary>
    public void Attach(IBrowserContext context)
    {
        context.Close += (_, _) => Remove();
        _onProcessExit = (_, _) =>
        {
            if (Done) return;
            try { context.CloseAsync().Wait(TimeSpan.FromSeconds(10)); } catch { }
            Remove(final: true);
        };
        AppDomain.CurrentDomain.ProcessExit += _onProcessExit;
    }
}
