namespace Clearcote;

/// Windows first-launch antivirus-scan race work-around (ports download.warmFiles + index.winAvRetry).
///
/// A just-extracted, unsigned chrome.exe can fail with "spawn UNKNOWN" / "side-by-side configuration
/// is incorrect" while real-time AV scans chrome_elf.dll (the SxS assembly member the exe's manifest
/// depends on), and Windows caches that failure against the PATH — so retries from the same path keep
/// failing. warmFiles pre-scans to close the race; WinAvRetry warms + backs off + retries, then
/// relaunches from a pristine copy on a fresh temp path. No-op off Windows.
public static class WinLaunch
{
    private static bool IsWindows => Native.OsTag == "windows";

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

    /// Launch via <paramref name="doLaunch"/>(exe), working around the Windows first-launch AV race:
    /// warm + back off + retry a few times, then relaunch from a pristine copy on a fresh temp path.
    /// Pass-through on non-Windows.
    public static async Task<T> WinAvRetryAsync<T>(Func<string, Task<T>> doLaunch, string exe)
    {
        if (!IsWindows) return await doLaunch(exe).ConfigureAwait(false);
        for (var i = 0; i < 3; i++)
        {
            try { return await doLaunch(exe).ConfigureAwait(false); }
            catch (Exception err)
            {
                if (!IsWinLaunchRace(err)) throw;
                WarmFiles(Path.GetDirectoryName(exe)!);
                await Task.Delay(800 * (i + 1)).ConfigureAwait(false);
            }
        }
        // The in-place SxS activation-context poison never clears; relaunch from a fresh copy.
        var recover = Path.Combine(
            Directory.CreateTempSubdirectory("clearcote-recover-").FullName, "browser");
        CopyDir(Path.GetDirectoryName(exe)!, recover);
        WarmFiles(recover);
        return await doLaunch(Path.Combine(recover, Path.GetFileName(exe))).ConfigureAwait(false);
    }

    internal static void CopyDir(string src, string dest)
    {
        Directory.CreateDirectory(dest);
        foreach (var dir in Directory.GetDirectories(src, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(dir.Replace(src, dest));
        foreach (var file in Directory.GetFiles(src, "*", SearchOption.AllDirectories))
            File.Copy(file, file.Replace(src, dest), overwrite: true);
    }
}
