// A shared HTTP cache for runs that start from a fresh profile (LaunchOptions.HttpCacheDir).
//
// A fresh profile starts with an empty disk cache, so every run downloads the site's scripts, styles and images
// again through the proxy. Pointing Chromium's --disk-cache-dir at a folder that outlives the run keeps those
// files while cookies and storage stay new. Measured on r56 (11-02, github.com + bbc.com/news through a counting
// proxy): about 8 MB of proxy traffic per run fresh, 0.36-0.44 MB once the cache was warm.
//
// Chromium locks its cache: of several browsers on one cache folder at once, only the first gets it, the others
// run on an empty in-memory cache. So the folder is split into slots (<dir>/slot-0, slot-1, ...) and each launch
// takes the first one no other live launch holds, marked by <dir>/slot-N.lock holding the owner's pid. A lock
// whose process is gone is taken over. Node (httpcache.ts) and Python (_httpcache.py) use the same layout, so the
// three can share one folder.
//
// Only a launch with a profile has a disk cache: incognito keeps it in memory (measured: --disk-cache-dir on an
// incognito launch left Cache_Data empty and saved nothing), so LaunchAsync (no profile) refuses the option.

using System.Text.RegularExpressions;

namespace Clearcote;

/// A slot held by this process: <see cref="Dir"/> goes to --disk-cache-dir, <see cref="Release"/> frees it (idempotent).
internal sealed class HttpCacheSlot
{
    private readonly string _lock;
    private readonly EventHandler _onExit;
    private int _done;

    internal HttpCacheSlot(string dir, string lockFile)
    {
        Dir = dir;
        _lock = lockFile;
        _onExit = (_, _) => Release();
        AppDomain.CurrentDomain.ProcessExit += _onExit;
    }

    internal string Dir { get; }

    internal void Release()
    {
        if (Interlocked.Exchange(ref _done, 1) == 1) return;
        AppDomain.CurrentDomain.ProcessExit -= _onExit;
        try { if (File.ReadAllText(_lock).Trim() == Environment.ProcessId.ToString()) File.Delete(_lock); }
        catch { /* gone */ }
    }
}

internal static class HttpCache
{
    /// How many slots one folder offers: the most browsers that can share it at once.
    internal const int Slots = 64;

    internal const string NeedsProfile =
        "HttpCacheDir needs a profile: LaunchAsync starts without one and keeps its HTTP cache in memory. " +
        "Use LaunchEphemeralProfileAsync (a fresh profile per run), LaunchPersistentContextAsync or ServeAsync";

    private static readonly Regex DiskCacheArg = new("^--disk-cache-dir(=|$)", RegexOptions.CultureInvariant);

    private static bool TryLock(string file)
    {
        try
        {
            using var fs = new FileStream(file, FileMode.CreateNew, FileAccess.Write, FileShare.Read);
            var pid = System.Text.Encoding.ASCII.GetBytes(Environment.ProcessId.ToString());
            fs.Write(pid, 0, pid.Length);
            return true;
        }
        catch (IOException) when (File.Exists(file)) { return false; }
    }

    private static int Owner(string file)
    {
        try { return int.TryParse(File.ReadAllText(file).Trim(), out var pid) ? pid : 0; }
        catch { return 0; }
    }

    /// Hold the first free slot of <paramref name="root"/>, creating the folder. Throws when the folder cannot be
    /// created; null (with one warning) when every slot is held by a live process.
    internal static HttpCacheSlot? Acquire(string root, bool quiet = false)
    {
        var baseDir = Path.GetFullPath(root);
        Directory.CreateDirectory(baseDir);
        for (var n = 0; n < Slots; n++)
        {
            var lockFile = Path.Combine(baseDir, $"slot-{n}.lock");
            var held = TryLock(lockFile);
            if (!held)
            {
                if (DockerLaunch.PidAlive(Owner(lockFile))) continue;
                try { File.Delete(lockFile); } catch { }   // a crashed run's lock
                held = TryLock(lockFile);
                if (!held) continue;                        // another launch took it over first
            }
            var dir = Path.Combine(baseDir, $"slot-{n}");
            Directory.CreateDirectory(dir);
            return new HttpCacheSlot(dir, lockFile);
        }
        if (!quiet && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN")) && Interlocked.Exchange(ref _warnedFull, 1) == 0)
            Console.Error.WriteLine($"clearcote: warning: HttpCacheDir: all {Slots} slots of {baseDir} are in use; this launch runs without the shared cache");
        return null;
    }

    private static int _warnedFull;

    /// Validate <see cref="LaunchOptions.HttpCacheDir"/> against the caller's own Args (before anything starts).
    internal static string? Check(string? httpCacheDir, IReadOnlyList<string>? args)
    {
        if (httpCacheDir is null) return null;
        if (string.IsNullOrWhiteSpace(httpCacheDir)) throw new ArgumentException("HttpCacheDir must be a folder path");
        if ((args ?? Array.Empty<string>()).Any(a => DiskCacheArg.IsMatch(a)))
            throw new ArgumentException("HttpCacheDir and an explicit --disk-cache-dir in Args cannot be combined; set one of them");
        return httpCacheDir;
    }

    /// Take a slot for a launch whose options may carry HttpCacheDir: a copy of the options comes back without it
    /// and with --disk-cache-dir=&lt;slot&gt; appended to Args, plus the release to call once the browser has exited.
    internal static (T Options, Action Release) Take<T>(T options) where T : LaunchOptions
    {
        var root = Check(options.HttpCacheDir, options.Args);
        if (root is null) return (options, () => { });
        var copy = (T)options.Clone();
        copy.HttpCacheDir = null;
        var slot = Acquire(root, options.Quiet);
        if (slot is null) return (copy, () => { });
        copy.Args = (options.Args ?? Array.Empty<string>()).Append($"--disk-cache-dir={slot.Dir}").ToList();
        return (copy, slot.Release);
    }

    /// Run a local launch with the slot HttpCacheDir asks for; <paramref name="onClose"/> wires the release to the
    /// returned browser/context/server, and a failed launch releases at once.
    internal static async Task<TResult> RunAsync<T, TResult>(T? options, Func<T?, Task<TResult>> launch, Action<TResult, Action> onClose)
        where T : LaunchOptions
    {
        if (options?.HttpCacheDir is null) return await launch(options).ConfigureAwait(false);
        var (opts, release) = Take(options);
        TResult result;
        try { result = await launch(opts).ConfigureAwait(false); }
        catch { release(); throw; }
        onClose(result, release);
        return result;
    }
}
