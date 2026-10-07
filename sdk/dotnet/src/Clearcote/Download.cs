using System.Diagnostics;
using System.IO.Compression;
using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Clearcote;

/// Options for resolving the free binary.
public class DownloadOptions
{
    /// Override the cache directory (default: per-OS user cache dir).
    public string? CacheDir { get; set; }
    /// Suppress progress logging.
    public bool Quiet { get; set; }
    /// Opt in to downloading the LATEST GitHub release instead of the pinned one (also CLEARCOTE_AUTO_UPDATE=1).
    public bool? AutoUpdate { get; set; }
}

/// Options for <see cref="Download.ProEnsureBinaryAsync"/>.
public class ProDownloadOptions
{
    /// License API base (default: CLEARCOTE_LICENSE_API env or clearcotelabs.com).
    public string? ApiBase { get; set; }
    /// Override the cache directory (default: per-OS user cache dir).
    public string? CacheDir { get; set; }
    /// Suppress progress logging.
    public bool Quiet { get; set; }
    /// Request a specific PRO major/version; the server returns the newest match.
    public string? Version { get; set; }
    /// Release channel. <see cref="ReleaseChannel.Preview"/> gets the newest build for this platform,
    /// preview or stable. Null = CLEARCOTE_RELEASE_CHANNEL, else stable.
    public ReleaseChannel? ReleaseChannel { get; set; }
}

/// PRO release channel.
public enum ReleaseChannel
{
    /// The default: the newest stable build.
    Stable,
    /// The newest build available for this platform — a newer preview when one exists, otherwise stable.
    Preview,
}

/// A resolved version plan: a free release to download, or a pro version to fetch via the licensed route.
public sealed record VersionPlan(string Kind, ReleaseInfo? Rel, string? Version);

/// Resolve the Clearcote browser binary: download, verify (SHA-256), extract to a per-version cache,
/// return the chrome path. Ports download.ts.
public static class Download
{
    private static void Log(bool quiet, string msg) { if (!quiet) Console.Error.WriteLine($"[clearcote] {msg}"); }

    private static bool AutoUpdateRequested(bool? opt)
    {
        if (opt.HasValue) return opt.Value;
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_AUTO_UPDATE");
        return env is "1" or "true";
    }

    private static string? FindFile(string dir, string name)
    {
        var stack = new Stack<string>();
        stack.Push(dir);
        while (stack.Count > 0)
        {
            var cur = stack.Pop();
            string[] entries;
            try { entries = Directory.GetFileSystemEntries(cur); } catch { continue; }
            foreach (var e in entries)
            {
                if (Directory.Exists(e)) stack.Push(e);
                else if (string.Equals(Path.GetFileName(e), name, StringComparison.OrdinalIgnoreCase)) return e;
            }
        }
        return null;
    }

    /// Name of the file-size snapshot written next to an extracted tree at install time.
    public const string Manifest = ".manifest.json";

    // Files Chromium cannot start without. A tree missing any of these does not fail at launch with
    // a usable error — it CHECK-crashes inside the browser process before Playwright can attach,
    // e.g. a missing icudtl.dat dies with "Invalid file descriptor to ICU data received" / "Check
    // failed: result" and exit code 0xC0000003. Cheap to stat, so this list is checked on EVERY
    // resolve; the full manifest is checked once per process.
    private static readonly string[] CriticalWin =
        { "chrome.exe", "chrome.dll", "chrome_elf.dll", "icudtl.dat", "snapshot_blob.bin", "resources.pak" };
    private static readonly string[] CriticalOther = { "icudtl.dat", "snapshot_blob.bin", "resources.pak" };

    private static string[] CriticalNames() => OperatingSystem.IsWindows() ? CriticalWin : CriticalOther;

    // Browser dirs whose full manifest already verified in this process.
    private static readonly HashSet<string> Scanned = new(StringComparer.OrdinalIgnoreCase);

    /// Snapshot every extracted file's size next to the tree, so a LATER launch can tell a healthy
    /// install from one that antivirus, a full disk, or an interrupted copy has since eaten.
    public static void WriteManifest(string @base, string browserDir)
    {
        var files = new Dictionary<string, long>(StringComparer.Ordinal);
        try
        {
            foreach (var p in Directory.EnumerateFiles(browserDir, "*", SearchOption.AllDirectories))
            {
                try
                {
                    var rel = Path.GetRelativePath(browserDir, p).Replace(Path.DirectorySeparatorChar, '/');
                    files[rel] = new FileInfo(p).Length;
                }
                catch { /* best-effort */ }
            }
            // Written to a temporary file and renamed into place, so no reader ever sees half a manifest.
            var tmp = Path.Combine(@base, $"{Manifest}.tmp-{Convert.ToHexString(RandomNumberGenerator.GetBytes(4)).ToLowerInvariant()}");
            try
            {
                File.WriteAllText(tmp, JsonSerializer.Serialize(new { files }));
                for (var attempt = 0; ; attempt++)
                {
                    try { File.Move(tmp, Path.Combine(@base, Manifest), overwrite: true); return; }
                    catch (Exception e) when (OperatingSystem.IsWindows() && attempt < 9 && e is IOException or UnauthorizedAccessException)
                    {
                        Thread.Sleep(50); // Windows: a reader has the old one open this instant
                    }
                }
            }
            catch { TryDelete(tmp); throw; }
        }
        catch { /* a manifest is an optimisation, never a reason to fail an install */ }
    }

    /// Return "&lt;file&gt; — &lt;problem&gt;" for every missing / truncated entry.
    private static List<string> CheckNames(string browserDir, IEnumerable<string> names,
                                           IReadOnlyDictionary<string, long>? sizes = null)
    {
        var problems = new List<string>();
        foreach (var name in names)
        {
            var p = Path.Combine(browserDir, name.Replace('/', Path.DirectorySeparatorChar));
            long got;
            try
            {
                var info = new FileInfo(p);
                if (!info.Exists) { problems.Add($"{name} — missing"); continue; }
                got = info.Length;
            }
            catch { problems.Add($"{name} — missing"); continue; }

            if (sizes is not null && sizes.TryGetValue(name, out var want))
            {
                if (got != want) problems.Add($"{name} — {got:N0} bytes, expected {want:N0}");
            }
            else if (sizes is null && got == 0)
            {
                problems.Add($"{name} — empty (0 bytes)");
            }
        }
        return problems;
    }

    /// Check an extracted browser tree; returns the problems found (empty when healthy).
    ///
    /// Always checks the critical files (a few stats). When <paramref name="base"/> holds a manifest
    /// written at install time, also checks every recorded file's exact size — once per process per
    /// tree by default, since a full tree is ~700 files; pass <paramref name="full"/> to force it.
    public static List<string> VerifyInstall(string browserDir, string? @base = null, bool? full = null)
    {
        var problems = CheckNames(browserDir, CriticalNames());
        if (problems.Count > 0) return problems;

        var manifest = Path.Combine(@base ?? Path.GetDirectoryName(browserDir.TrimEnd(Path.DirectorySeparatorChar))!, Manifest);
        var key = Path.GetFullPath(browserDir);
        bool memoised;
        lock (Scanned) memoised = Scanned.Contains(key);
        if (full == false || (full is null && memoised) || !File.Exists(manifest)) return problems;

        Dictionary<string, long> sizes;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
            sizes = new Dictionary<string, long>(StringComparer.Ordinal);
            if (doc.RootElement.TryGetProperty("files", out var files))
                foreach (var f in files.EnumerateObject()) sizes[f.Name] = f.Value.GetInt64();
        }
        catch { return problems; }

        var found = CheckNames(browserDir, sizes.Keys, sizes);
        if (found.Count == 0) lock (Scanned) Scanned.Add(key);
        return found;
    }

    /// The message a user actually needs when the tree is damaged: what is wrong, and how to fix it
    /// — instead of the browser CHECK-crashing during startup with an ICU stack trace.
    public static Exception BrokenInstallError(string browserDir, IReadOnlyList<string> problems, bool repairable = true)
    {
        var shown = string.Join("\n", problems.Take(8).Select(p => $"    {p}"));
        var more = problems.Count > 8 ? $"\n    ... and {problems.Count - 8} more" : "";
        var fix = repairable
            ? $"Delete the folder and let Clearcote re-download it:\n    {Path.GetDirectoryName(browserDir.TrimEnd(Path.DirectorySeparatorChar))}\n"
            : "Re-create this browser directory from a complete copy (or unset CLEARCOTE_BINARY /\nExecutablePath and let Clearcote download and verify its own build).\n";
        return new Exception(
            $"Clearcote browser install is incomplete or corrupted:\n    {browserDir}\n{shown}{more}\n\n" +
            "The browser cannot start without these files — it would crash during startup with an\n" +
            "ICU / 'Check failed' error before Playwright can attach.\n\n" +
            fix + "\n" +
            "Common causes: antivirus quarantined a file, the disk filled up mid-install, or the\n" +
            "directory was copied by something that did not finish (e.g. a bundled app copying it out\n" +
            "of a packaged-app temp dir). Excluding the Clearcote cache directory from real-time\n" +
            "antivirus scanning prevents a repeat.");
    }

    /// Validate the tree around a caller-supplied binary (ExecutablePath / CLEARCOTE_BINARY — e.g. a
    /// browser bundled into a packaged app). Throws with a clear message when it is damaged.
    ///
    /// Deliberately lenient about LAYOUT, strict about COMPLETENESS: a caller may point at something
    /// that is not a flat Chromium tree at all (installed Google Chrome keeps its DLLs in a versioned
    /// subfolder), and refusing to launch that would be a false alarm. So the check only engages once
    /// the directory looks flat — at least one non-binary payload file sits next to the exe — and then
    /// every other payload file is required. That is exactly the damaged-copy case.
    public static void CheckInstall(string? exe)
    {
        if (string.IsNullOrEmpty(exe) || !File.Exists(exe)) return; // the launcher reports this better
        var browserDir = Path.GetDirectoryName(Path.GetFullPath(exe))!;
        var payload = CriticalNames().Where(n => !n.StartsWith("chrome.ex", StringComparison.OrdinalIgnoreCase));
        if (!payload.Any(n => File.Exists(Path.Combine(browserDir, n)))) return; // not a flat tree
        var problems = VerifyInstall(browserDir);
        if (problems.Count > 0) throw BrokenInstallError(browserDir, problems, repairable: false);
    }

    /// The cached browser path for an install base, or null when absent or damaged.
    ///
    /// A damaged tree returns null (after wiping it) so the caller re-downloads: the ".verified"
    /// marker only records that the archive hashed correctly AT INSTALL TIME, and files can be eaten
    /// afterwards.
    public static string? CachedBinary(string @base, string binary, bool quiet = false) => Cached(@base, binary, quiet, repair: true);

    /// <paramref name="repair"/> also moves a damaged tree out of the way; false when not holding the
    /// install lock, so only the process that will re-install touches it.
    private static string? Cached(string @base, string binary, bool quiet, bool repair, bool? full = null)
    {
        if (!File.Exists(Path.Combine(@base, ".verified"))) return null;
        var browserDir = Path.Combine(@base, "browser");
        var cached = FindFile(browserDir, binary);
        // The binary itself can be gone too: antivirus quarantine, or browser/ deleted by hand.
        var problems = cached is not null ? VerifyInstall(browserDir, @base, full) : new List<string> { $"{binary} — missing" };
        if (cached is not null && problems.Count == 0) return cached;
        if (!repair) return null;

        Log(quiet, $"cached browser is damaged ({problems[0]}) — re-downloading");
        // Moved aside first: if a running browser still holds its files, this stops here with "close it", with
        // the tree still marked, instead of leaving an unmarked tree that no later install can move.
        MoveAside(@base, browserDir);
        TryDelete(Path.Combine(@base, ".verified"));
        TryDelete(Path.Combine(@base, Manifest));
        lock (Scanned) Scanned.Remove(Path.GetFullPath(browserDir));
        return null;
    }

    private static async Task<string> Sha256FileAsync(string file)
    {
        await using var fs = File.OpenRead(file);
        var hash = await SHA256.HashDataAsync(fs).ConfigureAwait(false);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static async Task DownloadToAsync(string url, string dest, long expectedSize, bool quiet)
    {
        using var client = SdkHttp.Create();
        using var res = await client.GetAsync(url, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
            throw new Exception($"Clearcote download failed: HTTP {(int)res.StatusCode} {res.ReasonPhrase} for {url}");
        var total = res.Content.Headers.ContentLength ?? expectedSize;
        await using var input = await res.Content.ReadAsStreamAsync().ConfigureAwait(false);
        await using var output = File.Create(dest);
        var buf = new byte[1 << 20];
        long seen = 0;
        var lastPct = -1;
        int n;
        while ((n = await input.ReadAsync(buf).ConfigureAwait(false)) > 0)
        {
            await output.WriteAsync(buf.AsMemory(0, n)).ConfigureAwait(false);
            seen += n;
            if (!quiet && total > 0)
            {
                var pct = (int)(seen * 100 / total);
                if (pct != lastPct && pct % 5 == 0)
                {
                    lastPct = pct;
                    Console.Error.Write($"\r[clearcote] downloading {pct}% ({seen / 1_000_000}/{total / 1_000_000} MB)");
                }
            }
        }
        if (!quiet) Console.Error.Write("\n");
    }

    private static async Task<string> FetchTextAsync(string url)
    {
        using var client = SdkHttp.Create();
        client.DefaultRequestHeaders.UserAgent.ParseAdd("clearcote-sdk");
        client.Timeout = TimeSpan.FromSeconds(30);
        return await client.GetStringAsync(url).ConfigureAwait(false);
    }

    private static (string? Zip, string? Exe) ParseSums(string text, string assetName, string binary)
    {
        string? zip = null, exe = null;
        foreach (var raw in text.Split('\n'))
        {
            var m = Regex.Match(raw.Trim(), @"^([0-9a-fA-F]{64})\s+[*]?(.+)$");
            if (!m.Success) continue;
            var basename = Regex.Split(m.Groups[2].Value, @"[\\/]").Last();
            if (basename == assetName) zip = m.Groups[1].Value.ToLowerInvariant();
            else if (basename == binary) exe = m.Groups[1].Value.ToLowerInvariant();
        }
        return (zip, exe);
    }

    private static async Task<ReleaseInfo?> ResolveLatestAsync(bool quiet)
    {
        var pin = Release.PlatformRelease();
        if (pin is null) return null;
        var assetRe = new Regex($@"^clearcote-.*-{Regex.Escape(pin.AssetGlob)}\.(?:zip|tar\.xz)$");
        var verRe = new Regex($@"^clearcote-(.+)-{Regex.Escape(pin.AssetGlob)}\.(?:zip|tar\.xz)$");
        JsonElement list;
        try
        {
            using var client = SdkHttp.Create();
            client.DefaultRequestHeaders.UserAgent.ParseAdd("clearcote-sdk");
            client.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
            client.Timeout = TimeSpan.FromSeconds(30);
            var json = await client.GetStringAsync(
                $"https://api.github.com/repos/{Release.Repo}/releases?per_page=30").ConfigureAwait(false);
            list = JsonDocument.Parse(json).RootElement.Clone();
        }
        catch (Exception e)
        {
            Log(quiet, $"auto-update: couldn't reach GitHub ({e.Message}); using pinned {Release.Current.Tag}");
            return null;
        }
        var releases = list.EnumerateArray()
            .Where(r => r.ValueKind == JsonValueKind.Object && !(r.TryGetProperty("draft", out var d) && d.GetBoolean()))
            .OrderByDescending(r => r.TryGetProperty("published_at", out var p) ? p.GetString() ?? "" : "");
        foreach (var r in releases)
        {
            if (!r.TryGetProperty("assets", out var assets) || assets.ValueKind != JsonValueKind.Array) continue;
            JsonElement? asset = null, sums = null, asc = null, key = null;
            foreach (var a in assets.EnumerateArray())
            {
                var name = a.GetProperty("name").GetString() ?? "";
                if (asset is null && assetRe.IsMatch(name)) asset = a;
                if (name == "SHA256SUMS.txt") sums = a;
                if (name == "SHA256SUMS.txt.asc") asc = a;
                if (name == "clearcote-signing-key.asc") key = a;
            }
            if (asset is null || sums is null) continue;
            (string? Zip, string? Exe) parsed;
            try { parsed = ParseSums(await FetchTextAsync(sums.Value.GetProperty("browser_download_url").GetString()!),
                asset.Value.GetProperty("name").GetString()!, pin.Binary); }
            catch { continue; }
            if (parsed.Zip is null) continue;
            var assetName = asset.Value.GetProperty("name").GetString()!;
            var vm = verRe.Match(assetName);
            return pin with
            {
                Tag = r.GetProperty("tag_name").GetString()!,
                Version = vm.Success ? vm.Groups[1].Value : r.GetProperty("tag_name").GetString()!,
                Asset = assetName,
                Url = asset.Value.GetProperty("browser_download_url").GetString()!,
                Sha256 = parsed.Zip,
                ExeSha256 = parsed.Exe ?? "",
                Size = asset.Value.TryGetProperty("size", out var sz) ? sz.GetInt64() : 0,
                Unpinned = true,
                AscUrl = asc?.GetProperty("browser_download_url").GetString(),
                KeyUrl = key?.GetProperty("browser_download_url").GetString(),
            };
        }
        return null;
    }

    private static bool HasGpg()
    {
        try
        {
            using var p = Process.Start(new ProcessStartInfo("gpg", "--version")
            { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false });
            p!.WaitForExit();
            return p.ExitCode == 0;
        }
        catch { return false; }
    }

    private static int Gpg(string home, string[] args, out string stdout)
    {
        var psi = new ProcessStartInfo("gpg") { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        psi.ArgumentList.Add("--homedir"); psi.ArgumentList.Add(home); psi.ArgumentList.Add("--batch");
        foreach (var a in args) psi.ArgumentList.Add(a);
        using var p = Process.Start(psi)!;
        stdout = p.StandardOutput.ReadToEnd();
        p.WaitForExit();
        return p.ExitCode;
    }

    private static async Task<string> GpgVerifyAsync(ReleaseInfo rel, string sumsBody, string tmp, bool quiet)
    {
        if (rel.AscUrl is null || rel.KeyUrl is null) return "skipped";
        if (!HasGpg()) { Log(quiet, "auto-update: gpg not found — skipping signature check (zip is still SHA-256-verified)"); return "skipped"; }
        var home = Directory.CreateTempSubdirectory("ccgpg-").FullName;
        try
        {
            var keyPath = Path.Combine(home, "key.asc");
            var sumsPath = Path.Combine(home, "SHA256SUMS.txt");
            var ascPath = Path.Combine(home, "SHA256SUMS.txt.asc");
            await File.WriteAllTextAsync(sumsPath, sumsBody).ConfigureAwait(false);
            await File.WriteAllTextAsync(keyPath, await FetchTextAsync(rel.KeyUrl).ConfigureAwait(false)).ConfigureAwait(false);
            await File.WriteAllTextAsync(ascPath, await FetchTextAsync(rel.AscUrl).ConfigureAwait(false)).ConfigureAwait(false);
            if (Gpg(home, new[] { "--import", keyPath }, out _) != 0) return "failed";
            Gpg(home, new[] { "--with-colons", "--fingerprint" }, out var shown);
            var fprs = shown.Split('\n').Where(l => l.StartsWith("fpr:")).Select(l => l.Split(':')[9]);
            if (!fprs.Contains(Release.SigningKeyFpr)) { Log(quiet, $"auto-update: signing key fingerprint mismatch (expected {Release.SigningKeyFpr})"); return "failed"; }
            return Gpg(home, new[] { "--verify", ascPath, sumsPath }, out _) == 0 ? "ok" : "failed";
        }
        catch { return "failed"; }
        finally { try { Directory.Delete(home, true); } catch { } }
    }

    // Install lock. One protocol, the same in Python (download.py), Node (download.ts) and .NET
    // (Download.cs) -- change all three together. Any mix of processes installing one build into
    // <cache>/<tag> takes turns:
    //  1. The lock is the file <tag>/.install-lock, created with O_CREAT|O_EXCL (atomic on every OS and in
    //     all three languages; Node has no flock). It holds JSON: pid, start, host, boot, pidns, nonce, sdk,
    //     created. "start" is the process start time where the SDK can read it ("linux:" from /proc; "win:"
    //     the creation time, Python and .NET), else null; "created" is when the lock was written (ms, Unix).
    //  2. The holder touches the file (mtime) every 5 s while it works, from a thread of its own.
    //  3. Waiters poll every 0.25 s. A record from this machine (same host, boot id and PID namespace) is stale
    //     when its pid is dead, when that pid now belongs to a newer process (another start time; on Windows
    //     created after the lock), or -- only without a boot id (Windows, macOS) -- when it was written before
    //     the machine last started. A pid confirmed to be the holder is never stale, however old its heartbeat.
    //     When that cannot be confirmed, and for any other lock: stale once it (mtime + content) has not changed
    //     for 120 s by the waiter's own clock -- 10 s when not valid JSON or its mtime is over 15 min old -- so
    //     clocks that disagree do not matter. A stale lock is broken while holding <tag>/.install-lock.break
    //     (also O_EXCL; a breaker unchanged for 30 s by the waiter's own clock is removed): if the lock is still
    //     exactly what was judged stale, delete it; then delete the breaker.
    //  4. The holder re-checks the cache and uses a verified build if one is there now; a tree marked
    //     .verified that fails the check (files or the binary gone) is moved aside by the holder and only then
    //     unmarked -- if its files are in use, the install stops at once ("close it and try again"). Otherwise
    //     it downloads and extracts into <tag>/.tmp-<nonce>/. Then, only while the lock is still its own: it
    //     uses a tree that appeared meanwhile and passes the full check, else moves browser/ aside
    //     (<tag>/.trash-*; after taking over a holder not confirmed dead, it waits within the install wait while
    //     that tree is still in use), moves its tree to <tag>/browser, checks the lock and writes .manifest.json
    //     (temporary file, then rename), checks the lock once more and only then writes .verified. A tree that
    //     passes the check is never moved or written over by an install. Leftovers (.tmp-*, .trash-*, a breaker
    //     unchanged throughout the install) are swept.
    //  5. Release deletes the lock only while it still holds our nonce. Waiting gives up after 30 min with an
    //     error that names the holder (pid, host) and the lock file.

    /// Name of the install-lock file inside a build directory.
    public const string InstallLockFile = ".install-lock";

    // Install-lock timings (see the protocol above). Tests may shorten the mutable ones.
    internal static readonly TimeSpan LockPoll = TimeSpan.FromMilliseconds(250);
    internal static TimeSpan LockHeartbeat = TimeSpan.FromSeconds(5);
    internal static TimeSpan LockStaleAfter = TimeSpan.FromSeconds(120);
    internal static TimeSpan LockUnreadableAfter = TimeSpan.FromSeconds(10);
    internal static readonly TimeSpan LockAncientAfter = TimeSpan.FromMinutes(15);
    internal static TimeSpan LockBreakerStaleAfter = TimeSpan.FromSeconds(30);
    internal static TimeSpan InUsePoll = TimeSpan.FromSeconds(1);
    private const long BootMarginMs = 5 * 60_000; // the clock can still be corrected in the first minutes after a start
    private const long ReuseMarginMs = 2_000;
    internal static readonly TimeSpan InstallWait = TimeSpan.FromMinutes(30);

    /// Test seam: called right before an install writes .verified. Left null in production.
    internal static Action? BeforeVerifiedHook;

    /// Test seam: called before each move of a browser tree; may throw, as a move of a tree in use does.
    internal static Action<string, string>? BeforeMoveDirHook;

    private enum LockState { Gone, Busy, Ok, Invalid }

    private sealed record LockRecord(long? Pid, string? Start, string? Host, string? Boot, string? PidNs, string? Nonce, string? Sdk, long? Created);

    /// Another process took the lock over, or changed the build meanwhile: check again under the lock.
    private sealed class LockLostException : Exception { }

    internal static string? BootId()
    {
        try
        {
            var s = File.ReadAllText("/proc/sys/kernel/random/boot_id").Trim();
            return s.Length > 0 ? s : null;
        }
        catch { return null; }
    }

    internal static string? PidNamespace()
    {
        try { return new FileInfo("/proc/self/ns/pid").LinkTarget; }
        catch { return null; }
    }

    /// When this machine last started (ms, Unix time), or null.
    internal static long? BootTimeMs()
    {
        try
        {
            if (OperatingSystem.IsWindows()) return DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() - Environment.TickCount64;
            foreach (var line in File.ReadLines("/proc/stat"))
                if (line.StartsWith("btime ", StringComparison.Ordinal)) return long.Parse(line[6..].Trim()) * 1000;
        }
        catch { }
        return null;
    }

    /// A marker that differs once <paramref name="pid"/> belongs to another process: "linux:&lt;start ticks&gt;"
    /// from /proc, or "win:&lt;creation time, ms Unix&gt;" on Windows; null when it cannot be read.
    internal static string? ProcessStart(long pid)
    {
        if (OperatingSystem.IsWindows())
        {
            try
            {
                using var p = Process.GetProcessById((int)pid);
                return $"win:{new DateTimeOffset(p.StartTime.ToUniversalTime()).ToUnixTimeMilliseconds()}";
            }
            catch { return null; }
        }
        try
        {
            var stat = File.ReadAllText($"/proc/{pid}/stat");
            var f = stat[(stat.LastIndexOf(')') + 2)..].Split(' ', StringSplitOptions.RemoveEmptyEntries);
            return f.Length > 19 ? $"linux:{f[19]}" : null;
        }
        catch { return null; }
    }

    private static bool PidAlive(long pid)
    {
        if (pid > int.MaxValue) return false;
        try
        {
            using var p = Process.GetProcessById((int)pid);
            return !p.HasExited;
        }
        catch (ArgumentException) { return false; } // no such process
        catch { return true; }                       // it exists, but cannot be inspected
    }

    private static (LockState State, LockRecord? Rec, DateTime MtimeUtc, string? Raw) ReadLock(string path)
    {
        DateTime mtime;
        string raw;
        try
        {
            var info = new FileInfo(path);
            if (!info.Exists) return (LockState.Gone, null, default, null);
            mtime = info.LastWriteTimeUtc;
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(fs);
            raw = reader.ReadToEnd();
        }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException) { return (LockState.Gone, null, default, null); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { return (LockState.Busy, null, default, null); }
        try
        {
            using var doc = JsonDocument.Parse(raw);
            var r = doc.RootElement;
            if (r.ValueKind != JsonValueKind.Object) return (LockState.Invalid, null, mtime, raw);
            string? Str(string k) => r.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
            long? pid = r.TryGetProperty("pid", out var p) && p.ValueKind == JsonValueKind.Number && p.TryGetInt64(out var pv) ? pv : null;
            long? created = r.TryGetProperty("created", out var c) && c.ValueKind == JsonValueKind.Number && c.TryGetInt64(out var cv) ? cv : null;
            return (LockState.Ok, new LockRecord(pid, Str("start"), Str("host"), Str("boot"), Str("pidns"), Str("nonce"), Str("sdk"), created), mtime, raw);
        }
        catch (JsonException) { return (LockState.Invalid, null, mtime, raw); }
    }

    /// The record is from this machine (same host, boot id and PID namespace) and names a usable pid.
    private static bool FromHere(LockRecord rec) =>
        rec.Host is { } host && string.Equals(host, System.Net.Dns.GetHostName(), StringComparison.OrdinalIgnoreCase)
        && rec.Boot == BootId() && rec.PidNs == PidNamespace() && rec.Pid is > 0 and < (1L << 31);

    /// For a record from this machine: its process still runs (and the pid was not reused).
    private enum Owner { Gone, Alive, Unknown }

    /// For a record from this machine: gone, alive (certainly still the holder) or unknown.
    private static Owner OwnerState(LockRecord rec)
    {
        // written before this machine last started (a boot id, where there is one, tells that already)
        if (rec.Boot is null && rec.Created is { } created && BootTimeMs() is { } boot && created < boot - BootMarginMs)
            return Owner.Gone; // written before this machine last started
        var pid = rec.Pid!.Value;
        if (!PidAlive(pid)) return Owner.Gone;
        var now = ProcessStart(pid);
        var start = rec.Start;
        if (now is not null && now.StartsWith("linux:", StringComparison.Ordinal))
        {
            if (start is not null && start.StartsWith("linux:", StringComparison.Ordinal)) return now == start ? Owner.Alive : Owner.Gone;
            return Owner.Unknown;
        }
        if (now is not null && now.StartsWith("win:", StringComparison.Ordinal) && long.TryParse(now[4..], out var createdNow))
        {
            long? reference = start is not null && start.StartsWith("win:", StringComparison.Ordinal) && long.TryParse(start[4..], out var s)
                ? s : rec.Created;
            if (reference is null) return Owner.Unknown;
            return createdNow > reference + ReuseMarginMs ? Owner.Gone : Owner.Alive; // a newer process has the pid
        }
        return Owner.Unknown;
    }

    /// <paramref name="unchangedFor"/>: how long the lock has stayed exactly as it is, by this process's own clock.
    private static bool LockStale(LockRecord? rec, DateTime mtimeUtc, TimeSpan unchangedFor)
    {
        if (rec is not null && FromHere(rec))
        {
            var owner = OwnerState(rec);
            if (owner != Owner.Unknown) return owner == Owner.Gone;
        }
        var quick = rec is null || DateTime.UtcNow - mtimeUtc > LockAncientAfter;
        var needed = quick && LockUnreadableAfter < LockStaleAfter ? LockUnreadableAfter : LockStaleAfter;
        return unchangedFor >= needed;
    }

    /// Delete the lock if it is still exactly what was judged stale, under the breaker file. True when the
    /// lock is gone afterwards.
    private static (DateTime, long)? FileKey(string path)
    {
        try
        {
            var info = new FileInfo(path);
            return info.Exists ? (info.LastWriteTimeUtc, info.Length) : null;
        }
        catch { return null; }
    }

    /// What a breaker file looked like, and since when (this process's own clock).
    private sealed class BreakerWatch
    {
        public (DateTime, long)? Key;
        public long At = Stopwatch.GetTimestamp();
    }

    /// A breaker is held for milliseconds: one that stays exactly the same for the breaker window, by this
    /// process's own clock, was left by a process that died holding it. Its timestamp alone proves nothing.
    private static void ClearStaleBreaker(string breaker, BreakerWatch watch)
    {
        var key = FileKey(breaker);
        if (key is null || key != watch.Key)
        {
            watch.Key = key;
            watch.At = Stopwatch.GetTimestamp();
        }
        else if (Stopwatch.GetElapsedTime(watch.At) >= LockBreakerStaleAfter)
        {
            TryDelete(breaker);
            watch.Key = null;
        }
    }

    private static bool BreakStaleLock(string path, (LockState, DateTime, string?) judged, BreakerWatch watch)
    {
        var breaker = path + ".break";
        try
        {
            using (new FileStream(breaker, FileMode.CreateNew, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete)) { }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            ClearStaleBreaker(breaker, watch);
            return false;
        }
        try
        {
            var (state, _, mtime, raw) = ReadLock(path);
            if ((state, mtime, raw) == judged)
            {
                File.Delete(path);
                return true;
            }
            return state == LockState.Gone;
        }
        catch { return false; }
        finally { TryDelete(breaker); }
    }

    private static string LockHolder(LockRecord? rec)
    {
        if (rec?.Pid is null or 0) return "a process that left no details";
        var sdk = rec.Sdk switch { "python" => "Python", "node" => "Node", "dotnet" => ".NET", null => "unknown", var s => s };
        return $"process {rec.Pid} on {(string.IsNullOrEmpty(rec.Host) ? "an unknown machine" : rec.Host)} (Clearcote {sdk} SDK)";
    }

    private static string WaitWords(TimeSpan t)
    {
        var (n, unit) = t.TotalSeconds < 120
            ? (Math.Max(1, (int)Math.Round(t.TotalSeconds)), "second")
            : ((int)Math.Round(t.TotalMinutes), "minute");
        return $"{n} {unit}{(n == 1 ? "" : "s")}";
    }

    /// The install lock while this process holds it: heartbeat timer, ownership check, release.
    internal sealed class InstallLock : IDisposable
    {
        public string LockPath { get; }
        public string Nonce { get; }
        private volatile bool _lost;
        private readonly Timer _timer;

        /// Taken over from a holder that was not confirmed dead (it may still hold its files for a while).
        public bool TookOver { get; }

        internal InstallLock(string lockPath, string nonce, bool tookOver = false)
        {
            LockPath = lockPath;
            Nonce = nonce;
            TookOver = tookOver;
            _timer = new Timer(_ => Beat(), null, LockHeartbeat, LockHeartbeat);
        }

        /// False once the lock file no longer carries our nonce (another process took the lock over).
        public bool Held()
        {
            if (_lost) return false;
            var (state, rec, _, _) = ReadLock(LockPath);
            if (state != LockState.Busy && rec?.Nonce != Nonce) _lost = true;
            return !_lost;
        }

        /// Touch the lock file now (the heartbeat timer does this every few seconds too).
        public void Beat()
        {
            if (!Held()) return;
            try { File.SetLastWriteTimeUtc(LockPath, DateTime.UtcNow); } catch { }
        }

        public void Dispose()
        {
            _timer.Dispose();
            for (var i = 0; i < 40; i++) // another process may be reading the file this instant (Windows refuses the delete)
            {
                var (state, rec, _, _) = ReadLock(LockPath);
                if (state == LockState.Gone || (state != LockState.Busy && rec?.Nonce != Nonce)) return;
                if (state == LockState.Ok)
                {
                    try { File.Delete(LockPath); return; }
                    catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
                }
                Thread.Sleep(50);
            }
        }
    }

    /// Wait for, then take, the install lock of build directory <paramref name="base"/> (see the protocol above).
    internal static async Task<InstallLock> AcquireInstallLockAsync(string @base, TimeSpan? timeout = null, bool quiet = true)
    {
        Directory.CreateDirectory(@base);
        var path = Path.Combine(@base, InstallLockFile);
        var nonce = Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
        var body = JsonSerializer.SerializeToUtf8Bytes(new
        {
            pid = Environment.ProcessId, start = ProcessStart(Environment.ProcessId), host = System.Net.Dns.GetHostName(),
            boot = BootId(), pidns = PidNamespace(), nonce, sdk = "dotnet", created = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
        });
        var wait = timeout ?? InstallWait;
        var clock = Stopwatch.StartNew(); // our own clock: the lock's mtime may come from a machine whose clock disagrees
        var told = false;
        var refused = 0;
        (LockState, DateTime, string?) seen = default; // the lock as last read, and since when it has looked like that
        var seenAt = TimeSpan.Zero;
        var breakerWatch = new BreakerWatch();
        var tookOver = false;
        while (true)
        {
            FileStream? fs = null;
            Exception? refusal = null;
            try { fs = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete); }
            catch (Exception e) when ((e is IOException && e is not DirectoryNotFoundException) || e is UnauthorizedAccessException)
            {
                refusal = e; // it exists -- or, on Windows, the previous lock file is still being deleted
            }
            if (fs is not null)
            {
                try { using (fs) fs.Write(body); }
                catch { TryDelete(path); throw; }
                return new InstallLock(path, nonce, tookOver);
            }
            tookOver = false;
            var (state, rec, mtime, raw) = ReadLock(path);
            if (state != LockState.Gone) refused = 0;
            else if (++refused > 40) throw refusal!; // no lock file, yet we may not create one: not a race, a real error
            var now = clock.Elapsed;
            if ((state, mtime, raw) != seen)
            {
                seen = (state, mtime, raw);
                seenAt = now;
            }
            if ((state is LockState.Ok or LockState.Invalid) && LockStale(rec, mtime, now - seenAt) && BreakStaleLock(path, seen, breakerWatch))
            {
                Log(quiet, $"removed an abandoned install lock ({LockHolder(rec)})");
                tookOver = !(rec is not null && FromHere(rec) && OwnerState(rec) == Owner.Gone);
                continue;
            }
            if (now >= wait)
            {
                Owner? here = state == LockState.Ok && rec is not null && FromHere(rec) ? OwnerState(rec) : null;
                var note = here switch
                {
                    Owner.Alive => ", which is still running",
                    Owner.Unknown => "; that process number may now belong to another program",
                    _ => "",
                };
                throw new TimeoutException(
                    $"Gave up after {WaitWords(wait)} waiting for another program to finish installing the Clearcote browser in\n    {@base}\n" +
                    $"The other installer is {LockHolder(rec)}{note}. If no Clearcote program is installing this build, delete this file and try again:\n    {path}");
            }
            if (!told && state == LockState.Ok)
            {
                Log(quiet, $"another program is installing this browser build ({LockHolder(rec)}); waiting for it");
                told = true;
            }
            await Task.Delay(LockPoll).ConfigureAwait(false);
        }
    }

    private static void MoveDir(string src, string dst)
    {
        for (var attempt = 0; ; attempt++)
        {
            try
            {
                BeforeMoveDirHook?.Invoke(src, dst);
                Directory.Move(src, dst);
                return;
            }
            catch (Exception e) when (OperatingSystem.IsWindows() && attempt < 9
                                      && (e is UnauthorizedAccessException || (e is IOException && e is not DirectoryNotFoundException)))
            {
                Thread.Sleep(200); // Windows: a scanner can hold a file in the fresh tree for a moment
            }
        }
    }

    /// Move a tree that must be replaced out of the way, then delete it if nothing is using it.
    /// A tree that must be moved aside is in use (Windows will not move a folder whose files are open).
    private sealed class InUseException : IOException
    {
        public InUseException(string message, Exception inner) : base(message, inner) { }
    }

    private static void MoveAside(string @base, string browserDir)
    {
        var trash = Path.Combine(@base, ".trash-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(6)).ToLowerInvariant());
        try { MoveDir(browserDir, trash); }
        catch (DirectoryNotFoundException) { return; }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw new InUseException(
                $"Clearcote cannot replace the browser files in\n    {browserDir}\nbecause another program is still using them ({e.Message}).\n" +
                "Close every program that uses this Clearcote browser, then try again.", e);
        }
        try { Directory.Delete(trash, recursive: true); } catch { /* what is still in use stays until the next install sweeps it */ }
    }

    /// Move an unverified browser/ aside. After taking over a holder that was not confirmed dead (it was
    /// suspended), its half-installed tree can stay in use until it resumes and finds the lock gone: wait for
    /// that, within the install wait, instead of failing. In any other case an in-use tree fails at once.
    private static async Task MoveAsideWhenFreeAsync(string @base, string browserDir, InstallLock held, bool quiet)
    {
        var started = Stopwatch.GetTimestamp();
        var told = false;
        while (true)
        {
            try
            {
                MoveAside(@base, browserDir);
                return;
            }
            catch (InUseException) when (held.TookOver && Stopwatch.GetElapsedTime(started) < InstallWait)
            {
                if (!held.Held()) throw new LockLostException();
                if (!told)
                {
                    Log(quiet, $"waiting for the installer that lost the lock to let go of {browserDir}");
                    told = true;
                }
            }
            await Task.Delay(InUsePoll).ConfigureAwait(false);
        }
    }

    /// Remove what an install that stopped part-way left behind. Only called by the lock holder.
    /// Returns the breaker file's state now (and when, by this process's clock). Given its state at the start of
    /// the install, removes a breaker that has not changed since, once that is the breaker window or more: one
    /// left by a process killed while it broke a stale lock.
    private static BreakerWatch SweepLeftovers(string @base, string? asset = null, BreakerWatch? breakerSeen = null)
    {
        var breaker = Path.Combine(@base, InstallLockFile + ".break");
        var seen = new BreakerWatch { Key = FileKey(breaker) };
        try
        {
            foreach (var dir in Directory.EnumerateDirectories(@base))
            {
                var name = Path.GetFileName(dir);
                if (name == ".incoming" || name.StartsWith(".tmp-", StringComparison.Ordinal) || name.StartsWith(".trash-", StringComparison.Ordinal))
                    try { Directory.Delete(dir, recursive: true); } catch { /* best-effort */ }
            }
        }
        catch { /* best-effort */ }
        try
        {
            foreach (var file in Directory.EnumerateFiles(@base, Manifest + ".tmp-*")) TryDelete(file);
        }
        catch { /* best-effort */ }
        if (breakerSeen is not null && seen.Key is not null && seen.Key == breakerSeen.Key
            && Stopwatch.GetElapsedTime(breakerSeen.At, seen.At) >= LockBreakerStaleAfter)
            TryDelete(breaker);
        if (asset is not null) TryDelete(Path.Combine(@base, asset)); // where earlier versions downloaded the archive
        return seen;
    }

    /// Download + verify a resolved release into <paramref name="base"/>; return the extracted browser path.
    ///
    /// Holds the build's install lock: a second process that missed the cache at the same moment waits, then
    /// uses the tree the first one finished instead of downloading over it.
    private static async Task<string> FetchAndVerifyAsync(ReleaseInfo rel, string @base, bool quiet)
    {
        for (var attempt = 0; attempt < 3; attempt++)
        {
            using var held = await AcquireInstallLockAsync(@base, null, quiet).ConfigureAwait(false);
            var cached = CachedBinary(@base, rel.Binary, quiet); // holding the lock: may repair
            if (cached is not null)
            {
                Log(quiet, $"installed by another process meanwhile: {cached}");
                return cached;
            }
            try { return await InstallLockedAsync(rel, @base, quiet, held).ConfigureAwait(false); }
            catch (LockLostException) { }
            catch when (!held.Held()) { }
            Log(quiet, "another process took over or changed this install; checking again");
        }
        throw new Exception($"Clearcote could not install {rel.Tag}: other processes kept taking over the install in {@base}");
    }

    /// The install itself; the caller holds the install lock. Everything is written under base/.tmp-&lt;nonce&gt;
    /// first, and the finished tree is moved into place only while the lock is still ours.
    private static async Task<string> InstallLockedAsync(ReleaseInfo rel, string @base, bool quiet, InstallLock held)
    {
        var breakerSeen = SweepLeftovers(@base, rel.Asset);
        var tmp = Path.Combine(@base, ".tmp-" + held.Nonce);
        Directory.CreateDirectory(tmp);
        try { return await InstallIntoAsync(rel, @base, tmp, quiet, held, breakerSeen).ConfigureAwait(false); }
        finally { try { Directory.Delete(tmp, recursive: true); } catch { /* best-effort */ } }
    }

    private static async Task<string> InstallIntoAsync(ReleaseInfo rel, string @base, string tmp, bool quiet, InstallLock held,
                                                       BreakerWatch? breakerSeen = null)
    {
        var browserDir = Path.Combine(@base, "browser");
        var zipPath = Path.Combine(tmp, rel.Asset);

        Log(quiet, $"fetching Clearcote {rel.Version} ({rel.Tag}{(rel.Unpinned ? ", latest" : "")}, ~{rel.Size / 1_000_000} MB)");
        await DownloadToAsync(rel.Url, zipPath, rel.Size, quiet).ConfigureAwait(false);

        Log(quiet, "verifying SHA-256");
        var got = await Sha256FileAsync(zipPath).ConfigureAwait(false);
        if (!string.Equals(got, rel.Sha256, StringComparison.OrdinalIgnoreCase))
        {
            TryDelete(zipPath);
            throw new Exception($"Clearcote archive SHA-256 mismatch — refusing to use it.\n  expected {rel.Sha256}\n  got      {got}");
        }

        if (rel.Unpinned && rel.AscUrl is not null)
        {
            var sumsBody = "";
            try { sumsBody = await FetchTextAsync($"https://github.com/{Release.Repo}/releases/download/{rel.Tag}/SHA256SUMS.txt").ConfigureAwait(false); } catch { }
            if (sumsBody.Length > 0)
            {
                var verdict = await GpgVerifyAsync(rel, sumsBody, @base, quiet).ConfigureAwait(false);
                if (verdict == "failed") { TryDelete(zipPath); throw new Exception($"Clearcote {rel.Tag}: GPG signature verification FAILED against the pinned key {Release.SigningKeyFpr} — refusing to use it."); }
                if (verdict == "ok") Log(quiet, $"auto-update: GPG signature OK (key {Release.SigningKeyFpr})");
            }
        }

        Log(quiet, "extracting");
        // Extract next to the target, then move the finished tree into place, so browser/ only ever appears
        // once fully written (no partial tree a concurrent launch could pick up).
        var incoming = Path.Combine(tmp, "browser");
        Directory.CreateDirectory(incoming);
        if (rel.Asset.EndsWith(".tar.xz") || rel.Archive == "tar.xz")
            RunTar(zipPath, incoming);           // Node has no stdlib xz; the system tar auto-detects .xz
        else
            ZipFile.ExtractToDirectory(zipPath, incoming);
        TryDelete(zipPath); // reclaim disk; keep only the extracted tree

        var exe = FindFile(incoming, rel.Binary) ?? throw new Exception($"Clearcote archive verified but {rel.Binary} was not found inside it.");
        if (!string.IsNullOrEmpty(rel.ExeSha256))
        {
            var exeHash = await Sha256FileAsync(exe).ConfigureAwait(false);
            if (!string.Equals(exeHash, rel.ExeSha256, StringComparison.OrdinalIgnoreCase))
                throw new Exception($"Clearcote {rel.Binary} SHA-256 mismatch — refusing to use it.\n  expected {rel.ExeSha256}\n  got      {exeHash}");
        }

        if (!OperatingSystem.IsWindows())
        {
            try { File.SetUnixFileMode(exe, (UnixFileMode)0b111_101_101); } catch { } // 0755
            var sandbox = Path.Combine(Path.GetDirectoryName(exe)!, "chrome-sandbox");
            if (File.Exists(sandbox)) { try { File.SetUnixFileMode(sandbox, (UnixFileMode)0b100_111_101_101); } catch { } } // 4755
        }

        // Move the tree into place only while the lock is still ours, and never over a tree that passes the full
        // check: one that appeared since the cache check (another installer finished it) may already be running,
        // so use it. One marked verified that fails the check is damaged, and moved aside (we hold the lock).
        held.Beat();
        if (!held.Held()) throw new LockLostException();
        var done = Cached(@base, rel.Binary, quiet, repair: true, full: true);
        if (done is not null)
        {
            Log(quiet, $"installed by another process meanwhile: {done}");
            return done;
        }
        if (File.Exists(Path.Combine(@base, ".verified"))) throw new LockLostException(); // marked verified meanwhile by another process: start over
        TryDelete(Path.Combine(@base, Manifest));
        if (Directory.Exists(browserDir) || File.Exists(browserDir))
            await MoveAsideWhenFreeAsync(@base, browserDir, held, quiet).ConfigureAwait(false); // not verified: an install that stopped part-way
        MoveDir(incoming, browserDir);
        exe = Path.Combine(browserDir, Path.GetRelativePath(incoming, exe));

        if (OperatingSystem.IsWindows())
            WinLaunch.WarmFiles(browserDir); // close the chrome_elf.dll first-launch AV race

        // Record the finished tree BEFORE the .verified marker, so a launch never sees "verified"
        // with no manifest to check it against -- and only while the lock is still ours, so a late manifest
        // never lands on a tree the new holder is putting in place.
        if (!held.Held()) throw new LockLostException();
        WriteManifest(@base, browserDir);
        // Mark it verified only while the lock is still ours: a process that took the lock over meanwhile then
        // finds an unverified tree it may replace, not a verified one it must leave alone.
        BeforeVerifiedHook?.Invoke();
        if (!held.Held()) throw new LockLostException();
        await File.WriteAllTextAsync(Path.Combine(@base, ".verified"), rel.Sha256 + "\n").ConfigureAwait(false);
        SweepLeftovers(@base, breakerSeen: breakerSeen);
        Log(quiet, $"ready: {exe}");
        return exe;
    }

    private static void RunTar(string archive, string destDir)
    {
        var psi = new ProcessStartInfo("tar") { UseShellExecute = false };
        psi.ArgumentList.Add("-xf"); psi.ArgumentList.Add(archive);
        psi.ArgumentList.Add("-C"); psi.ArgumentList.Add(destDir);
        using var p = Process.Start(psi) ?? throw new Exception("Clearcote: failed to start `tar` to extract the archive.");
        p.WaitForExit();
        if (p.ExitCode != 0) throw new Exception($"Clearcote: `tar` extraction failed (exit {p.ExitCode}).");
    }

    private static void TryDelete(string path) { try { File.Delete(path); } catch { } }

    /// Resolve the release channel: explicit option &gt; CLEARCOTE_RELEASE_CHANNEL &gt; stable.
    /// Unknown env values throw, so a typo never silently selects a different build.
    public static ReleaseChannel ResolveReleaseChannel(ReleaseChannel? @explicit = null, string? envValue = null)
    {
        if (@explicit is { } e)
        {
            if (!Enum.IsDefined(e)) throw new ArgumentException($"Unknown release channel '{(int)e}'. Use Stable or Preview.");
            return e;
        }
        return ParseReleaseChannel(envValue ?? Environment.GetEnvironmentVariable("CLEARCOTE_RELEASE_CHANNEL"));
    }

    /// Parse "stable" / "preview" (any case, trimmed; empty = stable). Anything else throws.
    public static ReleaseChannel ParseReleaseChannel(string? value)
    {
        var raw = (value ?? "").Trim().ToLowerInvariant();
        if (raw is "" or "stable") return ReleaseChannel.Stable;
        if (raw == "preview") return ReleaseChannel.Preview;
        throw new ArgumentException($"Unknown release channel '{value}'. Use \"stable\" or \"preview\".");
    }

    /// The PRO download URL for a platform, version selector and channel. <c>&amp;channel=preview</c> is
    /// only appended for preview, so older servers see an unchanged request for stable.
    public static string ProDownloadUrl(string baseUrl, string plat, string? version = null, ReleaseChannel channel = ReleaseChannel.Stable)
    {
        var u = $"{baseUrl.TrimEnd('/')}/api/v1/download/pro?platform={plat}";
        if (!string.IsNullOrEmpty(version)) u += $"&version={Uri.EscapeDataString(version)}";
        if (channel == ReleaseChannel.Preview) u += "&channel=preview";
        return u;
    }

    /// Download + verify the PRO (license-gated) browser and return its chrome path. Throws on any
    /// failure — a licensed caller must get the PRO build, never a silent free fall-back.
    public static async Task<string> ProEnsureBinaryAsync(string licenseKey, ProDownloadOptions? opts = null)
    {
        opts ??= new ProDownloadOptions();
        var baseUrl = (opts.ApiBase ?? Environment.GetEnvironmentVariable("CLEARCOTE_LICENSE_API") ?? "https://www.clearcotelabs.com").TrimEnd('/');
        var plat = Native.IsWindows ? "windows" : Native.IsLinux ? "linux" : null;
        if (plat is null) throw new Exception("Clearcote PRO ships Windows x64 and Linux x64 only.");

        var channel = ResolveReleaseChannel(opts.ReleaseChannel);
        using var client = SdkHttp.Create();
        var proUrl = ProDownloadUrl(baseUrl, plat, opts.Version, channel);
        var req = new HttpRequestMessage(HttpMethod.Get, proUrl);
        req.Headers.Authorization = new AuthenticationHeaderValue("Bearer", licenseKey);
        req.Headers.UserAgent.ParseAdd("clearcote-sdk");
        using var res = await client.SendAsync(req).ConfigureAwait(false);
        if (!res.IsSuccessStatusCode)
        {
            var body = (await res.Content.ReadAsStringAsync().ConfigureAwait(false));
            if (body.Length > 200) body = body[..200];
            throw new Exception($"Clearcote PRO download not authorized (HTTP {(int)res.StatusCode}): {body}\n" +
                                "Check your license key and that your plan is active.");
        }
        using var doc = JsonDocument.Parse(await res.Content.ReadAsStringAsync().ConfigureAwait(false));
        var meta = doc.RootElement;
        string? Get(string k) => meta.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        var resolvedChannel = Get("resolved_channel");
        if (channel == ReleaseChannel.Preview && !string.IsNullOrEmpty(resolvedChannel) && resolvedChannel != "preview")
            Log(opts.Quiet, $"release channel: preview requested, no newer preview for {plat} -> {resolvedChannel} {Get("tag")}".Trim());
        var url = Get("url"); var sha = Get("sha256");
        if (string.IsNullOrEmpty(url) || string.IsNullOrEmpty(sha))
            throw new Exception($"Clearcote PRO build is not currently available for {plat} (the server returned no download).");

        var version = Get("version") ?? "";
        var rel = new ReleaseInfo
        {
            Tag = Get("tag") ?? $"pro-{version}",
            Version = version,
            Asset = Get("asset") ?? $"clearcote-pro-{version}-{plat}-x64.{(plat == "windows" ? "zip" : "tar.xz")}",
            Url = url!,
            Sha256 = sha!,
            ExeSha256 = Get("exe_sha256") ?? "",
            Size = meta.TryGetProperty("size", out var s) && s.TryGetInt64(out var sv) ? sv : 0,
            Os = plat,
            Archive = Get("archive") ?? (plat == "windows" ? "zip" : "tar.xz"),
            Binary = Get("binary") ?? (plat == "windows" ? "chrome.exe" : "chrome"),
            AssetGlob = $"{plat}-x64",
            Unpinned = false, // pinned -> sha256-only verify (no GPG), like the free pin
        };

        var @base = Path.Combine(opts.CacheDir ?? Native.CacheRoot(), rel.Tag);
        {
            var cached = Cached(@base, rel.Binary, opts.Quiet, repair: false);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, opts.Quiet).ConfigureAwait(false);
    }

    // ── Version catalog resolver ─────────────────────────────────────────────
    private static readonly JsonSerializerOptions CatalogJsonOpts = new() { PropertyNameCaseInsensitive = true };

    private static string? PlatKey() => Native.IsWindows ? "windows" : Native.IsLinux ? "linux" : null;

    /// Compare version strings numerically: "150.0.7871.115" > "149.0.7827.114".
    private static int VerCmp(string a, string b)
    {
        int[] Parts(string v) => Regex.Matches(v ?? "", @"\d+").Take(4).Select(m => int.Parse(m.Value)).ToArray();
        var x = Parts(a); var y = Parts(b);
        for (var i = 0; i < 4; i++)
        {
            var d = (i < x.Length ? x[i] : 0) - (i < y.Length ? y[i] : 0);
            if (d != 0) return d;
        }
        return 0;
    }

    private static async Task<Catalog> FetchCatalogAsync(bool quiet)
    {
        try
        {
            var json = await FetchTextAsync(Release.CatalogUrl).ConfigureAwait(false);
            var cat = JsonSerializer.Deserialize<Catalog>(json, CatalogJsonOpts);
            if (cat is { Builds.Count: > 0 }) return cat;
        }
        catch (Exception e)
        {
            Log(quiet, $"version catalog unreachable ({e.Message}); using the bundled snapshot");
        }
        return Release.CatalogFallback;
    }

    /// Resolve a version selector against the public catalog, VALIDATING that it exists (and is
    /// reachable for its tier) BEFORE any download — so a bad request fails fast with a helpful message
    /// instead of getting stuck. <paramref name="selector"/> may be a bare major ("150"), an exact
    /// version, or "latest". Throws when the version doesn't exist for this OS, or when it's a PRO build
    /// and <paramref name="hasLicense"/> is false.
    public static async Task<VersionPlan> ResolveVersionAsync(string selector, bool hasLicense, bool quiet = false)
    {
        var cat = await FetchCatalogAsync(quiet).ConfigureAwait(false);
        return ResolveFromCatalog(cat, selector, hasLicense);
    }

    /// Best-effort resolved browser build (version string) this launch will run, for lease TELEMETRY
    /// only. Never throws (a launch must never fail over telemetry). An exact "X.Y.Z.W" selector is
    /// returned as-is (no network); a bare major / "latest" / empty is resolved against the catalog
    /// (empty -&gt; newest usable, matching the binary path). Any failure falls back to the pinned build.
    public static async Task<string?> ResolvedEngineVersionAsync(string? selector, bool hasLicense, bool quiet = true)
    {
        try
        {
            var sel = (selector ?? "").Trim();
            if (System.Text.RegularExpressions.Regex.IsMatch(sel, @"^\d+(?:\.\d+){3}$")) return sel;
            if (IsProRevisionSelector(sel))
            {
                // "150.0.7871.114-r7" -> the version; bare "r7" -> the pinned baseline version.
                var m = Regex.Match(sel, @"^(\d+(?:\.\d+){3})-r\d+$", RegexOptions.IgnoreCase);
                return m.Success ? m.Groups[1].Value : Release.Current.Version;
            }
            var plan = await ResolveVersionAsync(string.IsNullOrEmpty(sel) ? "latest" : sel, hasLicense, quiet).ConfigureAwait(false);
            return plan.Kind == "pro" ? plan.Version : plan.Rel?.Version;
        }
        catch { return Release.Current.Version; }
    }

    /// Pure validate-first resolution against an in-memory catalog (no I/O).
    public static VersionPlan ResolveFromCatalog(Catalog cat, string selector, bool hasLicense)
    {
        var plat = PlatKey() ?? throw new Exception("Clearcote ships Windows x64 and Linux x64 only.");
        var builds = cat.Builds.Where(b => b.Platforms.ContainsKey(plat)).ToList();
        var sel = (selector ?? "").Trim();

        List<CatalogBuild> cands;
        if (Regex.IsMatch(sel, "^(latest|newest)$", RegexOptions.IgnoreCase))
            cands = builds.Where(b => b.Tier == "free" || hasLicense).ToList(); // newest ACCESSIBLE
        else if (Regex.IsMatch(sel, @"^\d+$"))
            cands = builds.Where(b => b.Major.ToString() == sel).ToList();       // bare major
        else
            cands = builds.Where(b => b.Version == sel).ToList();                // exact version

        if (cands.Count == 0)
        {
            var avail = string.Join(", ", builds.Select(b => $"{b.Version} ({b.Tier})"));
            throw new Exception($"No Clearcote build matches version '{selector}' for {plat}. Available: {(avail.Length > 0 ? avail : "none")}.");
        }
        var pick = cands.Aggregate((a, b) => VerCmp(b.Version, a.Version) > 0 ? b : a);

        if (pick.Tier == "pro" && !hasLicense)
        {
            var free = string.Join(", ", builds.Where(b => b.Tier == "free").Select(b => b.Version));
            throw new Exception(
                $"Clearcote {pick.Version} is a PRO build and isn't public yet — set a license key (CLEARCOTE_LICENSE_KEY, or pass LicenseKey) to use it.\n" +
                $"  Free versions you can use without a key: {(free.Length > 0 ? free : "none")}.");
        }
        if (pick.Tier == "pro") return new VersionPlan("pro", null, pick.Version);

        var p = pick.Platforms[plat];
        if (string.IsNullOrEmpty(p.Url) || string.IsNullOrEmpty(p.Sha256))
            throw new Exception($"Clearcote {pick.Version} is marked free but the catalog has no download for {plat}.");
        var rel = new ReleaseInfo
        {
            Tag = string.IsNullOrEmpty(pick.Tag) ? $"v-{pick.Version}" : pick.Tag,
            Version = pick.Version,
            Asset = p.Asset ?? $"clearcote-{pick.Version}-{plat}-x64.{(p.Archive == "zip" ? "zip" : "tar.xz")}",
            Url = p.Url!,
            Sha256 = p.Sha256!,
            ExeSha256 = p.ExeSha256 ?? "",
            Size = p.Size,
            Os = plat,
            Archive = p.Archive,
            Binary = p.Binary,
            AssetGlob = $"{plat}-x64",
            Unpinned = false, // catalog sha256 is the trust anchor -> sha256-only verify, like a pin
        };
        return new VersionPlan("free", rel, null);
    }

    /// True when the selector pins a specific PRO REBUILD, e.g. "r7" or "150.0.7871.114-r7".
    /// Revisions are the same Chromium version rebuilt, so they never appear in the public version
    /// catalog (ResolveFromCatalog would reject them) and are PRO-only. The authenticated download
    /// route (which knows PRO_CATALOG_JSON) resolves the revision; the SDK just recognises the shape.
    public static bool IsProRevisionSelector(string? selector) =>
        Regex.IsMatch((selector ?? "").Trim(), @"(?:^|-)r\d+$", RegexOptions.IgnoreCase);

    /// Resolve a version selector to a downloaded, verified binary path (free from GitHub, pro via the licensed route).
    /// <paramref name="releaseChannel"/> is forwarded to the PRO route (null = CLEARCOTE_RELEASE_CHANNEL)
    /// and validated here, so a typo throws on the pinned-version path too.
    public static async Task<string> EnsureVersionAsync(string selector, string? licenseKey = null,
        string? apiBase = null, string? cacheDir = null, bool quiet = false, ReleaseChannel? releaseChannel = null)
    {
        var channel = ResolveReleaseChannel(releaseChannel);
        // A PRO revision pin ("r7" / "150.0.7871.114-r7") isn't in the public catalog — it's a
        // licensed rebuild. Route it straight to the PRO download (which resolves the revision).
        if (IsProRevisionSelector(selector))
        {
            if (string.IsNullOrEmpty(licenseKey))
                throw new Exception(
                    $"Clearcote '{selector}' is a PRO revision — set a license key (CLEARCOTE_LICENSE_KEY, or pass LicenseKey) to pin it.");
            return await ProEnsureBinaryAsync(licenseKey!,
                new ProDownloadOptions { ApiBase = apiBase, CacheDir = cacheDir, Quiet = quiet, Version = selector, ReleaseChannel = channel }).ConfigureAwait(false);
        }

        var plan = await ResolveVersionAsync(selector, !string.IsNullOrEmpty(licenseKey), quiet).ConfigureAwait(false);
        if (plan.Kind == "pro")
            return await ProEnsureBinaryAsync(licenseKey!,
                new ProDownloadOptions { ApiBase = apiBase, CacheDir = cacheDir, Quiet = quiet, Version = plan.Version, ReleaseChannel = channel }).ConfigureAwait(false);

        var rel = plan.Rel!;
        var @base = Path.Combine(cacheDir ?? Native.CacheRoot(), rel.Tag);
        {
            var cached = Cached(@base, rel.Binary, quiet, repair: false);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, quiet).ConfigureAwait(false);
    }

    /// Ensure the free Clearcote binary is present and verified; return the chrome path. Cached per tag.
    public static async Task<string> EnsureBinaryAsync(DownloadOptions? opts = null)
    {
        opts ??= new DownloadOptions();
        var cacheRoot = opts.CacheDir ?? Native.CacheRoot();

        ReleaseInfo rel;
        if (AutoUpdateRequested(opts.AutoUpdate))
        {
            var latest = await ResolveLatestAsync(opts.Quiet).ConfigureAwait(false);
            rel = latest is not null && latest.Tag == Release.Current.Tag
                ? Release.Current with { Unpinned = false }  // newest IS pinned — use the audited hashes
                : latest ?? Release.Current with { Unpinned = false };
        }
        else
        {
            rel = Release.Current with { Unpinned = false };
        }

        var @base = Path.Combine(cacheRoot, rel.Tag);
        {
            var cached = Cached(@base, rel.Binary, opts.Quiet, repair: false);
            if (cached is not null) return cached;
        }
        return await FetchAndVerifyAsync(rel, @base, opts.Quiet).ConfigureAwait(false);
    }
}
