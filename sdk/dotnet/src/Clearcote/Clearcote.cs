using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// Playwright drop-in for the Clearcote anti-fingerprint Chromium build.
///
/// <see cref="LaunchAsync"/> returns a standard Microsoft.Playwright <see cref="IBrowser"/> backed by
/// the verified Clearcote binary (auto-downloaded + SHA-256 checked). With a PRO license key it pulls
/// the license-gated build and checks out a floating-concurrency lease, injecting the run-token the
/// engine gate requires. With no key it is the free build and never contacts the license backend.
public static class Clearcote
{
    /// This SDK's version (kept in lockstep with the npm/PyPI SDKs).
    public const string Version = "0.20.0";

    private static readonly SemaphoreSlim PwLock = new(1, 1);
    private static IPlaywright? _pw;

    private static async Task<IPlaywright> PlaywrightAsync()
    {
        if (_pw is not null) return _pw;
        await PwLock.WaitAsync().ConfigureAwait(false);
        try { return _pw ??= await Playwright.CreateAsync().ConfigureAwait(false); }
        finally { PwLock.Release(); }
    }

    /// Resolve the chrome binary path: explicit ExecutablePath &gt; CLEARCOTE_BINARY env &gt; PRO (when
    /// licensed) &gt; free auto-download. Downloads + SHA-256-verifies as needed.
    public static async Task<string> ExecutablePathAsync(LaunchOptions? options = null)
    {
        options ??= new LaunchOptions();
        if (!string.IsNullOrEmpty(options.ExecutablePath)) return options.ExecutablePath;
        var envBin = Environment.GetEnvironmentVariable("CLEARCOTE_BINARY");
        if (!string.IsNullOrEmpty(envBin)) return envBin;
        var key = License.ResolveLicenseKey(options.LicenseKey);
        var version = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        if (!string.IsNullOrEmpty(version))
            // Explicit version selector: validate against the catalog FIRST (clear error if it doesn't
            // exist or needs a license), then route free (GitHub) vs pro (authenticated route).
            return await Download.EnsureVersionAsync(version, key, options.LicenseApiBase, options.CacheDir, options.Quiet).ConfigureAwait(false);
        if (key is not null)
            return await Download.ProEnsureBinaryAsync(key,
                new ProDownloadOptions { ApiBase = options.LicenseApiBase, CacheDir = options.CacheDir, Quiet = options.Quiet }).ConfigureAwait(false);
        return await Download.EnsureBinaryAsync(
            new DownloadOptions { CacheDir = options.CacheDir, Quiet = options.Quiet, AutoUpdate = options.AutoUpdate }).ConfigureAwait(false);
    }

    /// Pre-fetch + verify the FREE binary without launching (returns its path).
    public static Task<string> DownloadAsync(DownloadOptions? options = null)
        => Download.EnsureBinaryAsync(options);

    /// Launch Clearcote and return a standard Playwright <see cref="IBrowser"/>.
    public static async Task<IBrowser> LaunchAsync(LaunchOptions? options = null)
    {
        options ??= new LaunchOptions();
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        var args = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null)).ConfigureAwait(false);
        var env = lease is not null ? License.WithRunToken(lease.Token, options.Env) : options.Env;

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        var browser = await WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchAsync(new BrowserTypeLaunchOptions
        {
            ExecutablePath = exePath,
            Args = args,
            Headless = options.Headless,
            Channel = options.Channel,
            SlowMo = options.SlowMo,
            IgnoreDefaultArgs = options.IgnoreDefaultArgs ?? new[] { "--enable-automation" },
            Env = env,
            Proxy = ToPwProxy(proxy),
        }), exe).ConfigureAwait(false);

        if (lease is not null) browser.Disconnected += (_, _) => { _ = lease.StopAsync(); };
        return browser;
    }

    /// Launch a persistent context (a saved profile dir) and return a Playwright <see cref="IBrowserContext"/>.
    public static async Task<IBrowserContext> LaunchPersistentContextAsync(string userDataDir, LaunchOptions? options = null)
    {
        options ??= new LaunchOptions();
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        var args = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null)).ConfigureAwait(false);
        var env = lease is not null ? License.WithRunToken(lease.Token, options.Env) : options.Env;

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        var context = await WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchPersistentContextAsync(userDataDir,
            new BrowserTypeLaunchPersistentContextOptions
            {
                ExecutablePath = exePath,
                Args = args,
                Headless = options.Headless,
                Channel = options.Channel,
                SlowMo = options.SlowMo,
                IgnoreDefaultArgs = options.IgnoreDefaultArgs ?? new[] { "--enable-automation" },
                Env = env,
                Proxy = ToPwProxy(proxy),
                // Headed with no explicit viewport -> real window size (matches launch()).
                ViewportSize = options.Headless == false ? ViewportSize.NoViewport : null,
            }), exe).ConfigureAwait(false);

        if (lease is not null) context.Close += (_, _) => { _ = lease.StopAsync(); };
        return context;
    }

    /// Launch a standing, stealthy CDP endpoint (a direct engine spawn, not through Playwright) any
    /// Playwright/Puppeteer/CDP client can attach to via ConnectOverCDP. Returns a <see cref="Server"/>.
    public static async Task<Server> ServeAsync(ServeOptions? options = null)
    {
        options ??= new ServeOptions();
        var host = string.IsNullOrEmpty(options.Host) ? "127.0.0.1" : options.Host;
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        var engineArgs = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy);

        var port = options.Port ?? FreePort();
        var ownUdd = string.IsNullOrEmpty(options.UserDataDir);
        var userDataDir = ownUdd ? Directory.CreateTempSubdirectory("clearcote-serve-").FullName : options.UserDataDir!;
        var origins = options.AllowOrigins ?? $"http://{host}:{port},http://localhost:{port}";
        var cdpArgs = new List<string>
        {
            $"--remote-debugging-port={port}",
            $"--remote-debugging-address={host}",
            $"--remote-allow-origins={origins}",
            $"--user-data-dir={userDataDir}",
        };
        if (options.Headless != false) cdpArgs.Add("--headless=new");
        if (!string.IsNullOrEmpty(options.Proxy?.Server)) cdpArgs.Add($"--proxy-server={options.Proxy!.Server}");

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null)).ConfigureAwait(false);

        var proc = await WinLaunch.WinAvRetryAsync(exePath =>
        {
            var psi = new ProcessStartInfo(exePath) { UseShellExecute = false };
            foreach (var a in engineArgs.Concat(cdpArgs)) psi.ArgumentList.Add(a);
            if (lease is not null) psi.Environment[License.RunTokenEnv] = lease.Token;
            var p = Process.Start(psi) ?? throw new Exception("clearcote serve: failed to start the engine process.");
            return Task.FromResult(p);
        }, exe).ConfigureAwait(false);

        // Readiness poll — wait for the CDP endpoint to answer /json/version.
        var deadline = DateTime.UtcNow.AddMilliseconds(options.ReadyTimeoutMs);
        var ready = false;
        using (var probe = SdkHttp.Create())
        {
            while (DateTime.UtcNow < deadline)
            {
                if (proc.HasExited) break;
                try { using var _ = await probe.GetAsync($"http://{host}:{port}/json/version").ConfigureAwait(false); ready = true; break; }
                catch { await Task.Delay(250).ConfigureAwait(false); }
            }
        }
        if (!ready)
        {
            try { proc.Kill(true); } catch { }
            if (lease is not null) { try { await lease.StopAsync().ConfigureAwait(false); } catch { } }
            if (ownUdd) { try { Directory.Delete(userDataDir, true); } catch { } }
            throw new Exception($"clearcote serve: CDP endpoint at http://{host}:{port} did not come up within {options.ReadyTimeoutMs}ms");
        }

        var srv = new Server(proc, host, port, userDataDir, ownUdd, lease);
        AppDomain.CurrentDomain.ProcessExit += (_, _) => { try { srv.CloseAsync().GetAwaiter().GetResult(); } catch { } };
        if (!options.Quiet) Console.Error.WriteLine($"[clearcote] serve: CDP endpoint ready at {srv.CdpUrl}");
        return srv;
    }

    // ── internals ────────────────────────────────────────────────────────────

    /// fpArgs + extArgs + proxyArgs + quic + (privacy-sandbox unless disabled==false) + webrtc-deny,
    /// then userArgs appended last, then feature-flags collapsed. Mirrors index.ts assembleArgs.
    internal static List<string> AssembleArgs(List<string> fpArgs, List<string> extArgs, List<string> proxyArgs,
        bool? disablePrivacySandbox, string? webrtcIp, IReadOnlyList<string> userArgs, ProxyOptions? proxyForQuic)
    {
        var baseList = new List<string>();
        baseList.AddRange(fpArgs);
        baseList.AddRange(extArgs);
        baseList.AddRange(proxyArgs);
        baseList.AddRange(LaunchOpts.QuicArgs(proxyForQuic));
        if (disablePrivacySandbox != false) baseList.AddRange(LaunchOpts.PrivacySandboxArgs());
        baseList.AddRange(LaunchOpts.WebrtcDefaultDenyArgs(baseList.Concat(userArgs), webrtcIp));
        return LaunchOpts.MergeFeatureFlags(baseList.Concat(userArgs));
    }

    private static Proxy? ToPwProxy(ProxyOptions? p)
        => p?.Server is { Length: > 0 } server
            ? new Proxy { Server = server, Username = p.Username, Password = p.Password, Bypass = p.Bypass }
            : null;

    private static void EnsureRunnableHere(string exe)
    {
        if (!File.Exists(exe))
            throw new FileNotFoundException($"Clearcote binary not found at '{exe}'. Set ExecutablePath / CLEARCOTE_BINARY, or let the SDK auto-download it.", exe);
        var isWinExe = exe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase);
        if (isWinExe && !Native.IsWindows)
            throw new PlatformNotSupportedException($"'{exe}' is a Windows binary but this is not Windows.");
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}

/// A running Clearcote CDP endpoint (from <see cref="Clearcote.ServeAsync"/>). Attach a client with
/// ConnectOverCDP(<see cref="CdpUrl"/>). Call <see cref="CloseAsync"/> to stop it.
public sealed class Server
{
    private readonly Process _proc;
    private readonly string _userDataDir;
    private readonly bool _ownUdd;
    private readonly LeaseSession? _lease;

    internal Server(Process proc, string host, int port, string userDataDir, bool ownUdd, LeaseSession? lease)
    {
        _proc = proc; Host = host; Port = port; _userDataDir = userDataDir; _ownUdd = ownUdd; _lease = lease;
    }

    public string Host { get; }
    public int Port { get; }

    /// The CDP base URL, e.g. "http://127.0.0.1:9222" — pass to ConnectOverCDP.
    public string CdpUrl => $"http://{Host}:{Port}";

    /// The browser-level WebSocket debugger URL (from /json/version), or null if unreachable.
    public async Task<string?> WsUrlAsync()
    {
        try
        {
            using var c = SdkHttp.Create();
            using var doc = JsonDocument.Parse(await c.GetStringAsync($"{CdpUrl}/json/version").ConfigureAwait(false));
            return doc.RootElement.TryGetProperty("webSocketDebuggerUrl", out var w) ? w.GetString() : null;
        }
        catch { return null; }
    }

    public bool IsAlive { get { try { return !_proc.HasExited; } catch { return false; } } }

    /// Stop the engine, release the lease (best-effort), and remove an owned temp profile dir.
    public async Task CloseAsync()
    {
        try { if (!_proc.HasExited) _proc.Kill(true); } catch { }
        if (_lease is not null) { try { await _lease.StopAsync().ConfigureAwait(false); } catch { } }
        if (_ownUdd) { try { Directory.Delete(_userDataDir, true); } catch { } }
    }
}
