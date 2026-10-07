using System.ComponentModel;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Clearcote;

/// Windows launch helpers (ports download.warmFiles, index.winAvRetry and winlaunch.ts): a no-run spawn
/// probe, one reusable recovered copy per build, and removal of the temp directories a launch that never
/// started leaves behind.
///
/// Why a cached build can refuse to start in place ("spawn UNKNOWN", Windows error 14001: "the side-by-side
/// configuration is incorrect"): chrome.exe's manifest names a private assembly, the &lt;version&gt;.manifest
/// beside it, and Windows resolves that assembly against the file system as the system sees it. A process
/// running inside an MSIX-packaged app (and everything it starts) has its writes to %LOCALAPPDATA% redirected
/// into that package's private store, so a build it downloaded into the cache is visible to the process but
/// not to that check, and every launch from the cache fails. %TEMP% and the home directory are not
/// redirected. Real-time antivirus still scanning a freshly extracted chrome_elf.dll can produce the same
/// error for a while.
///
/// What used to happen: three in-place launches through Playwright, then a fresh copy of the whole browser
/// in %TEMP%\clearcote-recover-&lt;random&gt; that nothing deleted. Each failed Playwright launch also leaked its
/// two temp directories (playwright_chromiumdev_profile-*, playwright-artifacts-*): the Playwright driver's
/// spawn throws synchronously on that error, before it registers their cleanup.
///
/// Now: the copy lives at ~/.clearcote/recovered/&lt;build&gt;-&lt;hash&gt;/browser and is reused by every later
/// launch of the same build (the Python and Node SDKs compute the same key, so all three share it), the
/// in-place attempts are probed with a suspended CreateProcess instead of a Playwright launch, and an attempt
/// that still fails has exactly its own temp directories removed. <see cref="ClearRecovered"/> (and
/// `clearcote clear-cache`) deletes the recovered copies. No-op off Windows.
public static class WinLaunch
{
    private static bool IsWindows => Native.OsTag == "windows";

    internal const string RecoveredMarker = ".clearcote-recovered.json";
    private const int ErrorSxsCantGenActctx = 14001;
    private static readonly TimeSpan StalePartial = TimeSpan.FromHours(1); // an unfinished copy older than this was abandoned (a killed launch)
    private static readonly Regex ProfileInLog = new(@"--user-data-dir=(.*?playwright_chromiumdev_profile-[A-Za-z0-9]{6})");

    /// Test seam: the spawn probe the retry runs before each in-place attempt (<see cref="SpawnError"/>).
    internal static Func<string, Exception?> SpawnProbe = SpawnError;

    /// Sequentially read every file under <paramref name="dir"/> so on-access AV finishes scanning the
    /// freshly-extracted binaries BEFORE the browser launches. Best-effort, safe to call anywhere.
    public static void WarmFiles(string dir)
    {
        var buf = new byte[1 << 20];
        void Walk(string d)
        {
            string[] entries;
            try { entries = Directory.GetFileSystemEntries(d); }
            catch { return; }
            foreach (var p in entries)
            {
                if (Directory.Exists(p)) { Walk(p); continue; }
                try
                {
                    using var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                    while (fs.Read(buf, 0, buf.Length) > 0) { /* discard — the read forces the AV scan */ }
                }
                catch { /* best-effort */ }
            }
        }
        Walk(dir);
    }

    /// True if an error is the Windows first-launch SxS/AV race ("spawn unknown" / "side-by-side").
    public static bool IsWinLaunchRace(object? err)
    {
        var m = (err is Exception ex ? ex.Message : err?.ToString() ?? "").ToLowerInvariant();
        return m.Contains("spawn unknown") || m.Contains("side-by-side") || m.Contains("side by side");
    }

    /// Launch via <paramref name="doLaunch"/>(exe), working around Windows refusing to start a cached build
    /// ("spawn UNKNOWN" / "side-by-side configuration is incorrect").
    ///
    /// Inside an MSIX-packaged app a build downloaded into %LOCALAPPDATA% is invisible to the
    /// activation-context check, so it never starts in place; real-time antivirus scanning a freshly
    /// extracted chrome_elf.dll can cause the same error for a while. So: (1) launch from this build's
    /// recovered copy when one exists; else (2) probe the cached exe with a suspended CreateProcess,
    /// re-scanning + backing off up to three times, and launch it once the probe passes, removing the temp
    /// directories of an attempt that still fails; else (3) copy the build once to ~/.clearcote/recovered/
    /// and launch from there, which every later launch (.NET, Python or Node) reuses. Pass-through on
    /// non-Windows.
    public static Task<T> WinAvRetryAsync<T>(Func<string, Task<T>> doLaunch, string exe)
        => WinAvRetryAsync(doLaunch, exe, backoffMs: 800);

    internal static async Task<T> WinAvRetryAsync<T>(Func<string, Task<T>> doLaunch, string exe, int backoffMs)
    {
        if (!IsWindows) return await doLaunch(exe).ConfigureAwait(false);
        var ready = RecoveredExe(exe);
        if (ready is not null) return await LaunchAttemptAsync(doLaunch, ready).ConfigureAwait(false);
        for (var i = 0; i < 3; i++)
        {
            var failure = SpawnProbe(exe);
            if (failure is null || !IsSxsError(failure))
            {
                try { return await LaunchAttemptAsync(doLaunch, exe).ConfigureAwait(false); }
                catch (Exception err) when (IsWinLaunchRace(err)) { /* re-scan, back off, try again */ }
            }
            WarmFiles(Path.GetDirectoryName(exe)!);
            await Task.Delay(backoffMs * (i + 1)).ConfigureAwait(false);
        }
        return await LaunchAttemptAsync(doLaunch, MakeRecoveredCopy(exe, WarmFiles)).ConfigureAwait(false);
    }

    /// doLaunch(exe); when Windows could not start the process, first remove the temp directories the
    /// Playwright driver made for it (they leak otherwise, see <see cref="RemoveFailedLaunchDirs"/>).
    private static async Task<T> LaunchAttemptAsync<T>(Func<string, Task<T>> doLaunch, string exe)
    {
        var started = DateTime.UtcNow;
        try { return await doLaunch(exe).ConfigureAwait(false); }
        catch (Exception err) when (IsWinLaunchRace(err))
        {
            RemoveFailedLaunchDirs(err, started);
            throw;
        }
    }

    /// Null if Windows can create a process from <paramref name="exe"/>, else the error it raised.
    ///
    /// The process is created suspended and terminated at once, so none of its code runs: no window, no
    /// profile, no licence check. CreateProcess builds the activation context before that, which is the step
    /// that fails, so this answers exactly what the Playwright driver's spawn would hit. Null where there is
    /// nothing to probe with (not Windows): the launch itself then decides.
    internal static Exception? SpawnError(string exe)
    {
        if (!OperatingSystem.IsWindows()) return null;
        var si = new StartupInfo { cb = Marshal.SizeOf<StartupInfo>() };
        var cmd = new StringBuilder("\"" + exe + "\"");
        if (!CreateProcess(exe, cmd, IntPtr.Zero, IntPtr.Zero, false, CreateSuspended | CreateNoWindow,
                IntPtr.Zero, null, ref si, out var pi))
            return new Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            TerminateProcess(pi.hProcess, 1);
            WaitForSingleObject(pi.hProcess, 5000);
        }
        finally
        {
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
        }
        return null;
    }

    internal static bool IsSxsError(Exception err) =>
        err is Win32Exception { NativeErrorCode: ErrorSxsCantGenActctx }
        || err.Message.Contains("side-by-side", StringComparison.OrdinalIgnoreCase);

    /// ~/.clearcote/recovered: the copies of builds that could not start from the cache.
    internal static string RecoverRoot() => Path.Combine(Native.ClearcoteDir, "recovered");

    /// "&lt;build&gt;-&lt;hash&gt;": the cache build directory's name, and a hash of the source directory and the
    /// exe's size and modification time, so a rebuilt or re-downloaded build gets a fresh copy. Must match
    /// recovery_key in the Python SDK (_winlaunch.py) and recoveryKey in the Node SDK (winlaunch.ts), so all
    /// three share one copy. Throws if the exe is gone.
    internal static string RecoveryKey(string exe)
    {
        var src = Path.GetDirectoryName(Path.GetFullPath(exe))!;
        var st = new FileInfo(exe);
        if (!st.Exists) throw new FileNotFoundException("no such file", exe);
        var parent = string.Equals(Path.GetFileName(src), "browser", StringComparison.OrdinalIgnoreCase)
            ? Path.GetDirectoryName(src)!
            : src;
        var name = Regex.Replace(Path.GetFileName(parent), "[^A-Za-z0-9._-]", "_");
        if (name.Length > 60) name = name[..60];
        if (name.Length == 0) name = "browser";
        // os.path.normcase on Windows: lower case, backslashes (GetFullPath already gives backslashes there)
        var norm = OperatingSystem.IsWindows() ? src.ToLowerInvariant() : src;
        var ident = string.Create(CultureInfo.InvariantCulture, $"{norm}|{st.Length}|{UnixMs(st.LastWriteTimeUtc)}");
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(ident))).ToLowerInvariant();
        return $"{name}-{hash[..12]}";
    }

    /// Whole milliseconds since 1970, rounded down, as Python's st_mtime_ns // 1000000 and Node's
    /// Math.floor(mtimeMs).
    private static long UnixMs(DateTime utc) => new DateTimeOffset(utc).ToUnixTimeMilliseconds();

    /// The exe inside this build's recovered copy when one is complete, else null.
    internal static string? RecoveredExe(string exe)
    {
        string d;
        try { d = Path.Combine(RecoverRoot(), RecoveryKey(exe)); }
        catch { return null; }
        var cand = Path.Combine(d, "browser", Path.GetFileName(exe));
        return File.Exists(Path.Combine(d, RecoveredMarker)) && File.Exists(cand) ? cand : null;
    }

    /// Delete a browser copy unless a browser is running from it. Returns true once it is gone.
    ///
    /// Windows refuses to delete an exe while any process runs from it, so the main exe goes first: if that
    /// fails the copy is in use and is left alone; if it succeeds nothing can start from the copy any more
    /// and the rest is removed. (Renaming the directory is no test: Windows allows it while a program inside
    /// is running.)
    internal static bool DiscardCopy(string path)
    {
        var browser = Path.Combine(path, "browser");
        string[] names;
        try
        {
            names = Directory.GetFiles(browser).Select(f => Path.GetFileName(f))
                .Where(n => n.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)).ToArray();
        }
        catch { names = Array.Empty<string>(); } // no browser dir
        foreach (var n in names.OrderBy(n => !string.Equals(n, "chrome.exe", StringComparison.OrdinalIgnoreCase)))
        {
            try { File.Delete(Path.Combine(browser, n)); }
            catch (DirectoryNotFoundException) { }
            catch { return false; }
        }
        try { Directory.Delete(path, recursive: true); }
        catch { /* reported by the existence check */ }
        return !Directory.Exists(path);
    }

    private static TimeSpan Age(string path)
    {
        var d = new DirectoryInfo(path);
        return d.Exists ? DateTime.UtcNow - d.LastWriteTimeUtc : TimeSpan.Zero;
    }

    /// Remove recovered copies nothing can use any more: their source build is gone or has changed (its key
    /// no longer matches), plus copies abandoned half-way. Copies in use are skipped.
    internal static void SweepRecovered(string root, string? keep = null)
    {
        string[] dirs;
        try { dirs = Directory.GetDirectories(root); }
        catch { return; }
        foreach (var path in dirs)
        {
            var name = Path.GetFileName(path);
            if (name == keep) continue;
            string? sourceExe = null;
            try
            {
                using var meta = JsonDocument.Parse(File.ReadAllText(Path.Combine(path, RecoveredMarker)));
                var m = meta.RootElement;
                if (m.ValueKind == JsonValueKind.Object
                    && m.TryGetProperty("source", out var source) && source.ValueKind == JsonValueKind.String
                    && m.TryGetProperty("exe", out var exe) && exe.ValueKind == JsonValueKind.String)
                    sourceExe = Path.Combine(source.GetString()!, exe.GetString()!);
            }
            catch { /* no readable marker */ }
            bool stale;
            if (sourceExe is null)
            {
                // a copy still being made, or abandoned when its launch was killed
                stale = Age(path) > StalePartial;
            }
            else
            {
                try { stale = RecoveryKey(sourceExe) != name; }
                catch { stale = true; } // the build it was copied from is gone (clear-cache, or deleted by hand)
            }
            if (stale) DiscardCopy(path);
        }
    }

    /// Remove clearcote-recover-* copies older SDK versions left in the temp directory (a whole browser per
    /// launch, never deleted). Recent ones and ones in use are left alone. Set CLEARCOTE_KEEP_RECOVER=1 to
    /// keep them for debugging.
    internal static void SweepTempRecoverDirs(TimeSpan? keep = null)
    {
        if (!string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_KEEP_RECOVER"))) return;
        string[] dirs;
        try { dirs = Directory.GetDirectories(Path.GetTempPath()); }
        catch { return; }
        foreach (var path in dirs)
        {
            if (!Path.GetFileName(path).StartsWith("clearcote-recover-", StringComparison.Ordinal)) continue;
            if (Age(path) > (keep ?? TimeSpan.FromMinutes(1))) DiscardCopy(path);
        }
    }

    /// Delete every recovered copy in ~/.clearcote/recovered, as `clearcote clear-cache` does in the Python
    /// and Node SDKs. A copy a running browser uses is kept. Returns how many copies were removed, their size
    /// in bytes, and how many were kept because they are in use.
    public static (int Removed, long Bytes, int InUse) ClearRecovered()
    {
        var root = RecoverRoot();
        int removed = 0, inUse = 0;
        long bytes = 0;
        string[] dirs;
        try { dirs = Directory.GetDirectories(root).OrderBy(d => d, StringComparer.Ordinal).ToArray(); }
        catch { return (0, 0, 0); }
        foreach (var path in dirs)
        {
            var n = DirBytes(path);
            if (DiscardCopy(path)) { removed++; bytes += n; }
            else inUse++;
        }
        try { Directory.Delete(root); } // only when empty
        catch { /* still holds something */ }
        return (removed, bytes, inUse);
    }

    private static long DirBytes(string path)
    {
        try { return new DirectoryInfo(path).EnumerateFiles("*", SearchOption.AllDirectories).Sum(f => f.Length); }
        catch { return 0; }
    }

    /// Copy the build into <see cref="RecoverRoot"/> once and return the copy's exe; later launches reuse it.
    /// Concurrent launches each copy into a .partial- directory and the first to finish wins the name; the
    /// others discard theirs.
    internal static string MakeRecoveredCopy(string exe, Action<string>? warm = null)
    {
        var src = Path.GetDirectoryName(Path.GetFullPath(exe))!;
        var root = RecoverRoot();
        Directory.CreateDirectory(root);
        var key = RecoveryKey(exe);
        SweepRecovered(root, key);
        SweepTempRecoverDirs();
        var ready = RecoveredExe(exe);
        if (ready is not null) return ready;
        var final = Path.Combine(root, key);
        var part = Path.Combine(root, $"{key}.partial-{Guid.NewGuid().ToString("N")[..8]}");
        Directory.CreateDirectory(part);
        var keepPart = false;
        try
        {
            CopyDir(src, Path.Combine(part, "browser"));
            warm?.Invoke(Path.Combine(part, "browser"));
            var st = new FileInfo(exe);
            File.WriteAllText(Path.Combine(part, RecoveredMarker), JsonSerializer.Serialize(new
            {
                source = src, exe = Path.GetFileName(exe), size = st.Length, mtimeMs = UnixMs(st.LastWriteTimeUtc),
            }));
            try { Directory.Move(part, final); }
            catch
            {
                var other = RecoveredExe(exe); // another launch finished its copy first
                if (other is not null) return other;
                if (Directory.Exists(final) && DiscardCopy(final)) // an unmarked leftover held the name
                {
                    try { Directory.Move(part, final); }
                    catch { /* still blocked */ }
                }
                if (Directory.Exists(part))
                {
                    keepPart = true; // still blocked: launch from our own complete copy
                    return Path.Combine(part, "browser", Path.GetFileName(exe));
                }
            }
            return Path.Combine(final, "browser", Path.GetFileName(exe));
        }
        finally
        {
            if (!keepPart && Directory.Exists(part))
            {
                try { Directory.Delete(part, recursive: true); }
                catch { /* best-effort */ }
            }
        }
    }

    /// Remove the two temp directories a Playwright launch leaves when its process never started.
    ///
    /// The driver's spawn throws synchronously on "spawn UNKNOWN", before Playwright sets up the cleanup of
    /// the directories it has just made, so every failed attempt leaked a playwright-artifacts-* and (for
    /// LaunchAsync) a playwright_chromiumdev_profile-*. The profile is named in the error's call log. The
    /// artifacts directory is not: it is the empty one made by this attempt (after
    /// <paramref name="startedUtc"/>) just before that profile, or, for a persistent launch, the only one made
    /// by this attempt. Only empty directories are removed, and nothing when the choice is ambiguous.
    internal static void RemoveFailedLaunchDirs(object? err, DateTime startedUtc)
    {
        var msg = err is Exception ex ? ex.Message : err?.ToString() ?? "";
        if (!msg.Contains("<launching>")) return; // Playwright never got as far as starting the process
        string? tmp = null;
        DateTime? anchor = null;
        var m = ProfileInLog.Match(msg);
        if (m.Success)
        {
            var prof = m.Groups[1].Value;
            tmp = Path.GetDirectoryName(prof);
            if (Directory.Exists(prof))
            {
                anchor = Directory.GetCreationTimeUtc(prof);
                try { Directory.Delete(prof); } // only succeeds while empty: the browser never wrote to it
                catch { /* not empty */ }
            }
        }
        if (string.IsNullOrEmpty(tmp)) tmp = DriverTempDir();
        var now = DateTime.UtcNow;
        var slack = TimeSpan.FromMilliseconds(50);
        var found = new List<(DateTime Born, string Path)>();
        string[] dirs;
        try { dirs = Directory.GetDirectories(tmp); }
        catch { return; }
        foreach (var path in dirs)
        {
            if (!Path.GetFileName(path).StartsWith("playwright-artifacts-", StringComparison.Ordinal)) continue;
            try
            {
                var born = Directory.GetCreationTimeUtc(path);
                if (born >= startedUtc - slack && born <= now + slack && !Directory.EnumerateFileSystemEntries(path).Any())
                    found.Add((born, path));
            }
            catch { /* vanished */ }
        }
        string? pick = null;
        if (anchor is { } a)
            pick = found.Where(f => f.Born <= a + TimeSpan.FromMilliseconds(1)).OrderBy(f => f.Born).Select(f => f.Path).LastOrDefault();
        else if (found.Count == 1)
            pick = found[0].Path;
        if (pick is not null)
        {
            try { Directory.Delete(pick); }
            catch { /* not empty any more */ }
        }
    }

    /// The Playwright driver's os.tmpdir(): it is a Node process that inherits this one's environment, so on
    /// Windows TEMP, then TMP (Path.GetTempPath() reads TMP first); elsewhere TMPDIR.
    private static string DriverTempDir()
    {
        if (!OperatingSystem.IsWindows()) return Path.GetTempPath();
        foreach (var k in new[] { "TEMP", "TMP" })
        {
            var v = Environment.GetEnvironmentVariable(k);
            if (!string.IsNullOrEmpty(v)) return v;
        }
        return Path.Combine(Environment.GetEnvironmentVariable("SystemRoot") ?? @"C:\Windows", "temp");
    }

    internal static void CopyDir(string src, string dest)
    {
        Directory.CreateDirectory(dest);
        foreach (var dir in Directory.GetDirectories(src, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(dir.Replace(src, dest));
        foreach (var file in Directory.GetFiles(src, "*", SearchOption.AllDirectories))
            File.Copy(file, file.Replace(src, dest), overwrite: true);
    }

    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;

    [StructLayout(LayoutKind.Sequential)]
    private struct StartupInfo
    {
        public int cb;
        public IntPtr lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateProcessW", SetLastError = true, CharSet = CharSet.Unicode)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcess(string? applicationName, StringBuilder commandLine, IntPtr processAttributes,
        IntPtr threadAttributes, [MarshalAs(UnmanagedType.Bool)] bool inheritHandles, uint creationFlags, IntPtr environment,
        string? currentDirectory, ref StartupInfo startupInfo, out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(IntPtr process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CloseHandle(IntPtr handle);
}
