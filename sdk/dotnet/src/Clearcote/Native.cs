using System.Runtime.InteropServices;

namespace Clearcote;

/// Platform + path helpers, mirroring the Node/Python SDKs' per-OS behavior.
internal static class Native
{
    public static bool IsWindows => RuntimeInformation.IsOSPlatform(OSPlatform.Windows);
    public static bool IsLinux => RuntimeInformation.IsOSPlatform(OSPlatform.Linux);
    public static bool IsMac => RuntimeInformation.IsOSPlatform(OSPlatform.OSX);

    /// Test seam: when set, overrides the reported OS (mirrors the Node/Python suites stubbing platform).
    internal static string? OsTagOverride;

    /// "windows" | "linux" | "macos" | "unknown" — matches the Node osTag()/Python plat mapping.
    public static string OsTag =>
        OsTagOverride ?? (IsWindows ? "windows" : IsLinux ? "linux" : IsMac ? "macos" : "unknown");

    /// Home dir. Reads HOME then USERPROFILE (so tests can redirect it, like the Node suite does).
    public static string HomeDir =>
        FirstNonEmpty(
            Environment.GetEnvironmentVariable("HOME"),
            Environment.GetEnvironmentVariable("USERPROFILE"))
        ?? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);

    /// ~/.clearcote — where the license key, instance id, and lease cache live.
    public static string ClearcoteDir => Path.Combine(HomeDir, ".clearcote");

    /// Per-OS binary cache root (CLEARCOTE_CACHE overrides), matching download.ts defaultCacheRoot().
    public static string CacheRoot()
    {
        var env = Environment.GetEnvironmentVariable("CLEARCOTE_CACHE");
        if (!string.IsNullOrEmpty(env)) return env;
        if (IsWindows)
        {
            var local = FirstNonEmpty(Environment.GetEnvironmentVariable("LOCALAPPDATA"))
                        ?? Path.Combine(HomeDir, "AppData", "Local");
            return Path.Combine(local, "clearcote", "Cache");
        }
        if (IsMac) return Path.Combine(HomeDir, "Library", "Caches", "clearcote");
        var xdg = FirstNonEmpty(Environment.GetEnvironmentVariable("XDG_CACHE_HOME"))
                  ?? Path.Combine(HomeDir, ".cache");
        return Path.Combine(xdg, "clearcote");
    }

    private static string? FirstNonEmpty(params string?[] values)
    {
        foreach (var v in values)
            if (!string.IsNullOrEmpty(v)) return v;
        return null;
    }
}

/// Test seam for HTTP: when <see cref="HandlerOverride"/> is set, every SDK HTTP call routes through
/// it — the .NET equivalent of the Node suite swapping global fetch. Left null in production.
internal static class SdkHttp
{
    internal static HttpMessageHandler? HandlerOverride;

    public static HttpClient Create()
    {
        var handler = HandlerOverride;
        return handler is null
            ? new HttpClient(new SocketsHttpHandler { AllowAutoRedirect = true })
            : new HttpClient(handler, disposeHandler: false);
    }
}
