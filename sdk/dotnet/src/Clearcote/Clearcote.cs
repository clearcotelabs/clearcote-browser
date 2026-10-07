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
    public const string Version = "0.40.1";

    private static readonly SemaphoreSlim PwLock = new(1, 1);
    private static IPlaywright? _pw;

    private static async Task<IPlaywright> PlaywrightAsync()
    {
        if (_pw is not null) return _pw;
        await PwLock.WaitAsync().ConfigureAwait(false);
        try { return _pw ??= await Playwright.CreateAsync().ConfigureAwait(false); }
        finally { PwLock.Release(); }
    }

    internal static Task<IPlaywright> PlaywrightInstanceAsync() => PlaywrightAsync();

    /// The Docker container a browser from <see cref="LaunchAsync"/> runs in (macOS); null for any other browser.
    public static DockerContainer? DockerContainerOf(object browser)
        => DockerLaunch.Containers.TryGetValue(browser, out var c) ? c : null;

    /// Resolve the chrome binary path: explicit ExecutablePath &gt; CLEARCOTE_BINARY env &gt; PRO (when
    /// licensed) &gt; free auto-download. Downloads + SHA-256-verifies as needed.
    public static async Task<string> ExecutablePathAsync(LaunchOptions? options = null)
    {
        options ??= new LaunchOptions();
        if (!string.IsNullOrEmpty(options.ExecutablePath))
        {
            // Caller-supplied tree (often a browser bundled into a packaged app): we did not install
            // it, so validate it here — a half-copied tree otherwise CHECK-crashes during startup.
            Download.CheckInstall(options.ExecutablePath);
            return options.ExecutablePath;
        }
        var envBin = Environment.GetEnvironmentVariable("CLEARCOTE_BINARY");
        if (!string.IsNullOrEmpty(envBin)) { Download.CheckInstall(envBin); return envBin; }
        // Validated on every download path (option > CLEARCOTE_RELEASE_CHANNEL), so a typo throws even
        // when the build that would be fetched is free or pinned. Only the PRO route has channels.
        var channel = Download.ResolveReleaseChannel(options.ReleaseChannel);
        var key = License.ResolveLicenseKey(options.LicenseKey);
        var version = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        if (!string.IsNullOrEmpty(version))
            // Explicit version selector: validate against the catalog FIRST (clear error if it doesn't
            // exist or needs a license), then route free (GitHub) vs pro (authenticated route).
            return await Download.EnsureVersionAsync(version, key, options.LicenseApiBase, options.CacheDir, options.Quiet, channel).ConfigureAwait(false);
        if (key is not null)
            return await Download.ProEnsureBinaryAsync(key,
                new ProDownloadOptions { ApiBase = options.LicenseApiBase, CacheDir = options.CacheDir, Quiet = options.Quiet, ReleaseChannel = channel }).ConfigureAwait(false);
        return await Download.EnsureBinaryAsync(
            new DownloadOptions { CacheDir = options.CacheDir, Quiet = options.Quiet, AutoUpdate = options.AutoUpdate }).ConfigureAwait(false);
    }

    /// Pre-fetch + verify the FREE binary without launching (returns its path).
    public static Task<string> DownloadAsync(DownloadOptions? options = null)
        => Download.EnsureBinaryAsync(options);

    /// Launch Clearcote and return a standard Playwright <see cref="IBrowser"/>.
    /// <remarks>
    /// GEOMETRY CAVEAT. Only half of the headless geometry default (see <see cref="Geometry"/>) can be
    /// applied here. The headless display is a command-line switch, so it is set. The rest — NoViewport
    /// and fitting the window to the work area — needs the context, and this returns an
    /// <see cref="IBrowser"/> whose <c>NewPageAsync</c>/<c>NewContextAsync</c> the caller invokes
    /// directly (C# has no monkeypatching). A page created with default options therefore keeps
    /// Playwright's emulated 1280x720 viewport, which also overrides screen.* to 1280x720, so the
    /// window reports <c>outer &gt; screen</c> — an impossible geometry. Prefer
    /// <see cref="LaunchEphemeralProfileAsync"/> (which is the recommended path anyway, for the CDM),
    /// or do the rest yourself (the display this launch set is what the window is fitted to):
    /// <code>
    /// var page = await browser.NewPageAsync(new() { ViewportSize = ViewportSize.NoViewport });
    /// await Geometry.FitWindowToWorkAreaAsync(page);
    /// </code>
    /// The same holds for the colour scheme: on an r32+ engine the persona decides
    /// <c>prefers-color-scheme</c>, and a context created with default options emulates light over
    /// it. Pass <c>ColorScheme = ColorScheme.Null</c> to <c>NewPageAsync</c>/<c>NewContextAsync</c>
    /// (the persistent and ephemeral-profile launches do this for you).
    /// <para>
    /// CLOUD. With <c>Cloud = true</c> (or CLEARCOTE_CLOUD=1) the browser runs on Clearcote's servers
    /// instead and this returns the same Playwright <see cref="IBrowser"/>, connected over CDP; its
    /// NewPageAsync/NewContextAsync default to no emulated viewport, and CloseAsync disconnects and ends
    /// the hosted session. Options only a browser on this machine can take are refused by name before
    /// anything is created. See <see cref="Cloud"/> for the rest of the hosted API.
    /// </para>
    /// <para>
    /// MACOS. There is no native macOS build, so on macOS this starts the Clearcote Docker image
    /// (teamflatearth/clearcote:sdk-&lt;version&gt;; <see cref="LaunchOptions.DockerImage"/> or
    /// CLEARCOTE_DOCKER_IMAGE picks another) and returns the same Playwright <see cref="IBrowser"/>, connected
    /// to it over CDP; CloseAsync stops the container. The persona options, Headless, Proxy, Args, Version and
    /// the licence key go to the container; an option it cannot take is refused by name.
    /// <see cref="DockerUnavailableException"/> says so when Docker is missing or not running.
    /// <see cref="LaunchOptions.Docker"/> = false (or CLEARCOTE_DOCKER=0) turns this off; true uses the
    /// container on any OS. <see cref="DockerContainerOf"/> names the container.
    /// </para>
    /// </remarks>
    public static async Task<IBrowser> LaunchAsync(LaunchOptions? options = null)
    {
        if (CloudLaunch.Requested(options)) return await CloudLaunch.LaunchBrowserAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        // macOS (no native build): the Clearcote Docker image, connected over CDP.
        if (DockerLaunch.Requested(options)) return await DockerLaunch.LaunchBrowserAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        options = await PrepareAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        var args = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false));
        LaunchWarnings.Emit(LaunchWarnings.ForArgs(options.Args), options.Quiet);
        LaunchWarnings.EmitOnce(LaunchWarnings.ForPersonaHost(options), options.Quiet);   // a Linux persona on Windows

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null), options.Proxy).ConfigureAwait(false);
        // Bind a per-launch run-token file so a supporting engine (r23+) can stop a running free
        // browser once the token stops advancing; passed ALONGSIDE CLEARCOTE_RUN_TOKEN. Inert in free mode.
        var launchToken = lease?.BindLaunch();
        var callerEnv = Languages.ApplyLinuxLanguage(args, options.Env);  // Linux: UI locale from --lang
        // Linux: FONTCONFIG_FILE -> the bundled metric-compatible clones (+ FontDirs), as Python and Node do.
        callerEnv = Fonts.ApplyLinuxFonts(exe, fontDirs, callerEnv, options.Env, args: args);
        // Built per attempt: a launch retried after a stale-token refusal must carry the lease's fresh token.
        var envFor = () => ShaderDialect.Apply(options.ShaderDialect,  // hlsl by default for a Windows claim off Windows
            lease is not null ? License.WithRunToken(lease.Token, callerEnv, launchToken?.File) : callerEnv, args);

        // Headless: the display is browser-wide, so it applies even here (see the geometry caveat).
        var display = Geometry.ResolveHeadless(options.Headless, options.Fingerprint, args, callerSetGeometry: false);
        // Engine 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the
        // engine reads them from there (see PersonaEnv). Probed once; every attempt's env gets the payload.
        var persona = PersonaEnv.Plan(exe, args.Concat(display.Args), options.PersonaEnv);

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        var browser = await License.ReleaseLeaseOnFailureAsync(lease, () => License.RetryOnStaleRunTokenAsync(lease, () =>
            WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchAsync(new BrowserTypeLaunchOptions
            {
                ExecutablePath = exePath,
                Args = persona.Args.ToArray(),
                Headless = options.Headless,
                Channel = options.Channel,
                SlowMo = options.SlowMo,
                Timeout = options.Timeout,
                IgnoreDefaultArgs = options.IgnoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs.ToArray(),
                Env = persona.Env(envFor()),
                Proxy = ToPwProxy(proxy),
            }), exe)), launchToken).ConfigureAwait(false);

        // Release the concurrency slot + remove the run-token file when the browser closes.
        if (lease is not null) browser.Disconnected += (_, _) => { _ = lease.StopAsync(); launchToken?.Release(); };
        return browser;
    }

    /// <summary>
    /// Launch on a throwaway profile directory that is deleted when the context closes, and return
    /// a Playwright <see cref="IBrowserContext"/>.
    /// </summary>
    /// <remarks>
    /// <para>
    /// PREFER THIS OVER <see cref="LaunchAsync"/>. Incognito cannot load a component-updated CDM,
    /// so <c>requestMediaKeySystemAccess('com.widevine.alpha')</c> rejects and the EME surface is a
    /// no-Widevine tell on a build branded Google Chrome (measured against the live audit on
    /// 150-r10). A profile-backed launch can carry the CDM; incognito cannot, at all.
    /// </para>
    /// <para>
    /// The Python and Node SDKs made this the behaviour of <c>launch()</c> itself in 0.23.0. C#
    /// cannot: <see cref="IBrowser"/> and <see cref="IBrowserContext"/> are distinct interfaces
    /// with no shared base, and there is no monkeypatching to paper over the difference, so
    /// changing <see cref="LaunchAsync"/> would be a COMPILE break for every caller rather than a
    /// behaviour change. A separate method keeps existing builds working and makes the choice
    /// explicit. Both expose <c>NewPageAsync()</c>, so most call sites port by changing one word.
    /// </para>
    /// <para>
    /// The directory is removed on close — once the browser process has exited, so it is gone when
    /// <c>CloseAsync</c> returns — and at process exit, which closes a context still open first. The
    /// retry is not defensive padding: on Windows the browser holds handles under the profile for a
    /// short window after close, so a single removal silently fails and the directory leaks.
    /// </para>
    /// <para>
    /// CLOUD. With <c>Cloud = true</c> (or CLEARCOTE_CLOUD=1) this returns the fresh context of a hosted
    /// browser instead, so code written against this method moves to the cloud unedited. Closing the
    /// context ends the hosted session. A <see cref="LaunchOptions.Profile"/> loads that cloud profile's
    /// cookies (and saves them back only with <c>Persist = true</c>).
    /// </para>
    /// </remarks>
    public static async Task<IBrowserContext> LaunchEphemeralProfileAsync(LaunchOptions? options = null)
    {
        if (CloudLaunch.Requested(options))
            return await CloudLaunch.LaunchContextAsync(options ?? new LaunchOptions(), persistent: false, userDataDir: null).ConfigureAwait(false);
        var dir = Path.Combine(Path.GetTempPath(), "clearcote-run-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(dir);
        IBrowserContext context;
        try { context = await LaunchPersistentContextAsync(dir, options).ConfigureAwait(false); }
        catch
        {
            // A launch that fails (e.g. geoip failing closed) must not leak the throwaway profile dir.
            new ThrowawayProfile(dir).Remove();
            throw;
        }
        // Deleted once the browser has exited, never under it: see ThrowawayProfile.
        new ThrowawayProfile(dir).Attach(context);
        return context;
    }

    /// Launch a persistent context (a saved profile dir) and return a Playwright <see cref="IBrowserContext"/>.
    /// A cloud launch keeps its cookies in a cloud profile instead of a directory: use the overload
    /// without <paramref name="userDataDir"/>.
    public static async Task<IBrowserContext> LaunchPersistentContextAsync(string userDataDir, LaunchOptions? options = null)
    {
        if (CloudLaunch.Requested(options))
            return await CloudLaunch.LaunchContextAsync(options!, persistent: true, userDataDir).ConfigureAwait(false);
        options = await PrepareAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        var args = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false));
        LaunchWarnings.Emit(LaunchWarnings.ForArgs(options.Args), options.Quiet);
        LaunchWarnings.EmitOnce(LaunchWarnings.ForPersonaHost(options), options.Quiet);   // a Linux persona on Windows

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null), options.Proxy).ConfigureAwait(false);
        // Bind a per-launch run-token file (r23+ engine online-enforcement opt-in); passed ALONGSIDE
        // CLEARCOTE_RUN_TOKEN. Inert in free mode.
        var launchToken = lease?.BindLaunch();
        var callerEnv = Languages.ApplyLinuxLanguage(args, options.Env);  // Linux: UI locale from --lang
        // Linux: FONTCONFIG_FILE -> the bundled metric-compatible clones (+ FontDirs), as Python and Node do.
        callerEnv = Fonts.ApplyLinuxFonts(exe, fontDirs, callerEnv, options.Env, args: args);
        // Built per attempt: a launch retried after a stale-token refusal must carry the lease's fresh token.
        var envFor = () => ShaderDialect.Apply(options.ShaderDialect,  // hlsl by default for a Windows claim off Windows
            lease is not null ? License.WithRunToken(lease.Token, callerEnv, launchToken?.File) : callerEnv, args);

        var geometry = Geometry.ResolveHeadless(
            options.Headless, options.Fingerprint, args,
            callerSetGeometry: options.ViewportSize is not null || options.ScreenSize is not null);
        // Regime 2 appends the headless display; the fit below keeps reading the caller's args. Then engine
        // 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the engine
        // reads them from there (see PersonaEnv). Probed once; every attempt's env gets the payload.
        var persona = PersonaEnv.Plan(exe, args.Concat(geometry.Args), options.PersonaEnv);

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        var context = await License.ReleaseLeaseOnFailureAsync(lease, () => License.RetryOnStaleRunTokenAsync(lease, () =>
            WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchPersistentContextAsync(userDataDir,
            new BrowserTypeLaunchPersistentContextOptions
            {
                ExecutablePath = exePath,
                Args = persona.Args.ToArray(),
                Headless = options.Headless,
                Channel = options.Channel,
                SlowMo = options.SlowMo,
                Timeout = options.Timeout,
                IgnoreDefaultArgs = options.IgnoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs.ToArray(),
                Env = persona.Env(envFor()),
                Proxy = ToPwProxy(proxy),
                // Headed with no explicit viewport -> real window size (matches launch()).
                // Headless -> the persona's display (regime 1) or the SDK's (regime 2), and the window
                // is fitted to its work area below. See Geometry.
                ViewportSize = options.ViewportSize
                    ?? (options.Headless == false || geometry.Mode != Geometry.Mode.None
                        ? ViewportSize.NoViewport
                        : null),
                ScreenSize = options.ScreenSize,
                // r32+: the persona's colour scheme reaches the page instead of Playwright's light.
                ColorScheme = ColorSchemeDefault.Resolve(options.ColorScheme, exe),
            }), exe)), launchToken).ConfigureAwait(false);

        // Release the concurrency slot + remove the run-token file when the context closes.
        if (lease is not null) context.Close += (_, _) => { _ = lease.StopAsync(); launchToken?.Release(); };
        // Both regimes: maximize into the display's work area. It never throws, so a launch cannot
        // fail on it, and it runs while the context is still on about:blank, so the caller's page
        // never observes a resize.
        if (geometry.Mode != Geometry.Mode.None)
            await Geometry.InstallWindowFixupAsync(context, args).ConfigureAwait(false);
        return context;
    }

    /// A persistent CLOUD context: <c>new() { Cloud = true, Profile = "name" }</c> opens the hosted browser
    /// with that cloud profile's cookies and saves them back when the context closes (set
    /// <c>Profile = new CloudProfile { Name = "name", Persist = false }</c> to load only). Closing the
    /// context ends the hosted session. A local persistent context needs a directory: use the overload
    /// that takes one.
    public static async Task<IBrowserContext> LaunchPersistentContextAsync(LaunchOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        if (!CloudLaunch.Requested(options))
            throw new ArgumentException(
                "LaunchPersistentContextAsync needs a userDataDir (or Cloud = true with Profile = \"name\" for a cloud profile)");
        return await CloudLaunch.LaunchContextAsync(options, persistent: true, userDataDir: null).ConfigureAwait(false);
    }

    /// Launch a standing, stealthy CDP endpoint (a direct engine spawn, not through Playwright) any
    /// Playwright/Puppeteer/CDP client can attach to via ConnectOverCDP. Returns a <see cref="Server"/>.
    public static async Task<Server> ServeAsync(ServeOptions? options = null)
    {
        Geometry.ValidateWindowSize(options?.WindowSize);
        options = await PrepareAsync(options ?? new ServeOptions()).ConfigureAwait(false);
        var host = string.IsNullOrEmpty(options.Host) ? "127.0.0.1" : options.Host;
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease

        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy);
        // serve launches the binary directly, so Playwright's SwiftShader default is never added; the
        // blocklist rule still applies to a headed endpoint and on Windows.
        var engineArgs = AssembleArgs(Fingerprint.Args(options), LaunchOpts.ExtensionArgs(options.Extensions),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false, viaPlaywright: false));

        var port = options.Port ?? FreePort();
        var ownUdd = string.IsNullOrEmpty(options.UserDataDir);
        var userDataDir = ownUdd ? Directory.CreateTempSubdirectory("clearcote-serve-").FullName : options.UserDataDir!;
        var origins = options.AllowOrigins ?? $"http://{host}:{port},http://localhost:{port}";
        // A non-loopback bind or a "*" origin list hands the browser to whoever can reach the port.
        LaunchWarnings.Emit(LaunchWarnings.ForServe(host, origins).Concat(LaunchWarnings.ForArgs(options.Args)), options.Quiet);
        LaunchWarnings.EmitOnce(LaunchWarnings.ForPersonaHost(options), options.Quiet);   // a Linux persona on Windows
        var cdpArgs = new List<string>
        {
            $"--remote-debugging-port={port}",
            $"--remote-debugging-address={host}",
            $"--remote-allow-origins={origins}",
            $"--user-data-dir={userDataDir}",
        };
        if (options.Headless != false) cdpArgs.Add("--headless=new");
        // Never the caller's raw server: it comes after proxyArgs, so a second --proxy-server would
        // win, and one still carrying user:pass@ is rejected by Chromium's parser: every request
        // then fails with ERR_NO_SUPPORTED_PROXIES (measured on r27, Node serve()).
        if (!string.IsNullOrEmpty(proxy?.Server)) cdpArgs.Add($"--proxy-server={proxy!.Server}");
        // Chromium refuses to start as root without --no-sandbox, and serve spawns the binary itself,
        // so Playwright's own --no-sandbox is missing: serve in a root container just timed out.
        if (LaunchOpts.ServeNeedsNoSandbox(Native.OsTag, LaunchOpts.EffectiveUid(), engineArgs)) cdpArgs.Add("--no-sandbox");
        // ...and the warning bar that flag (or any of the caller's on Chromium's list) puts on the first tab.
        cdpArgs.AddRange(LaunchOpts.ServeInfobarArgs(options.Headless != false, engineArgs.Concat(cdpArgs)));
        // Headless geometry for a raw endpoint: the display and window are set browser-wide, since no
        // client's context options or CDP overrides would reach every page (see Geometry).
        var geometry = Geometry.ServedGeometry(engineArgs, options.Fingerprint, options.LightStealth == true,
            headless: options.Headless != false);
        if (geometry is not null) cdpArgs.AddRange(geometry.Args);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        var licKey = License.ResolveLicenseKey(options.LicenseKey);
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null), options.Proxy).ConfigureAwait(false);
        // Bind a per-launch run-token file (r23+ engine online-enforcement opt-in); passed ALONGSIDE
        // CLEARCOTE_RUN_TOKEN. Inert in free mode.
        var launchToken = lease?.BindLaunch();
        // Engine 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the
        // engine reads them from there (see PersonaEnv). Last, so everything above still reads them from
        // engineArgs; cdpArgs never carry one and stay as they are.
        var persona = PersonaEnv.Plan(exe, engineArgs, options.PersonaEnv);

        var proc = await License.ReleaseLeaseOnFailureAsync(lease, () => WinLaunch.WinAvRetryAsync(exePath =>
        {
            var psi = new ProcessStartInfo(exePath) { UseShellExecute = false };
            foreach (var a in persona.Args.Concat(cdpArgs)) psi.ArgumentList.Add(a);
            if (persona.Payload is not null) psi.Environment[PersonaEnv.EnvVar] = persona.Payload;
            if (lease is not null) psi.Environment[License.RunTokenEnv] = lease.Token;
            if (launchToken is not null) psi.Environment[License.RunTokenFileEnv] = launchToken.File;
            // serve() starts the engine itself, so the child inherits this process's environment;
            // only the one variable needs setting.
            var dialect = ShaderDialect.Resolve(options.ShaderDialect, engineArgs);
            if (dialect is not null && (options.ShaderDialect is not null || !psi.Environment.ContainsKey(ShaderDialect.EnvVar)))
                psi.Environment[ShaderDialect.EnvVar] = dialect;
            // Linux: the UI locale from --lang (engines before 153 r29 read it only from the env).
            var language = Languages.LinuxLanguageEnv(engineArgs);
            if (language is not null) psi.Environment["LANGUAGE"] = language;
            // Linux: FONTCONFIG_FILE -> the bundled metric-compatible clones (+ FontDirs).
            var fontConfig = Fonts.LinuxFontConfig(exe, fontDirs, args: engineArgs);
            if (fontConfig is not null) psi.Environment["FONTCONFIG_FILE"] = fontConfig;
            var p = Process.Start(psi) ?? throw new Exception("clearcote serve: failed to start the engine process.");
            return Task.FromResult(p);
        }, exe)).ConfigureAwait(false);

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
        var srv = new Server(proc, host, port, userDataDir, ownUdd, lease, launchToken);
        if (!ready)
        {
            await srv.CloseAsync().ConfigureAwait(false);  // the engine, the lease, the run-token file, the temp directories
            throw new Exception($"clearcote serve: CDP endpoint at http://{host}:{port} did not come up within {options.ReadyTimeoutMs}ms");
        }

        // Before any client attaches: the window onto the work area (and, under a persona, the headless
        // display onto the persona's). Its own connection, closed again; never fails the launch.
        if (geometry is not null)
            await Geometry.FitServedWindowAsync(await srv.WsUrlAsync().ConfigureAwait(false), geometry.Persona, options.WindowSize)
                .ConfigureAwait(false);
        if (!options.Quiet) Console.Error.WriteLine($"[clearcote] serve: CDP endpoint ready at {srv.CdpUrl}");
        return srv;
    }

    // ── internals ────────────────────────────────────────────────────────────

    /// Copy the caller's options (never mutate them) and apply geoip, which fails closed BEFORE the
    /// binary is resolved or any browser starts.
    internal static async Task<T> PrepareAsync<T>(T options) where T : LaunchOptions
    {
        var copy = (T)options.Clone();
        if (copy.Geoip) await GeoIp.ApplyAsync(copy, copy.Proxy, copy.Quiet).ConfigureAwait(false);
        return copy;
    }

    /// The per-launch inputs for the engine-switch extras and gating in <see cref="AssembleArgs"/>.
    // ViaPlaywright: Playwright starts this browser (Launch / LaunchPersistentContext, not Serve), so
    // its --disable-features list is on the line and is replaced with the caller's IgnoreDefaultArgs
    // taken into account; see LaunchOpts.PlaywrightFeatureOverrideArgs.
    // StockRuntime: LaunchOptions.StockRuntime (engine r32+; null follows CLEARCOTE_STOCK_RUNTIME).
    internal sealed record EngineExtras(string? Exe, bool Headed, bool Quiet, bool? AllowThirdPartyCookies, bool? TransparentProxy,
        bool ViaPlaywright = false, IReadOnlyList<string>? IgnoreDefaultArgs = null, bool? StockRuntime = null);

    private static EngineExtras Extras(LaunchOptions o, string exe, bool headed, bool viaPlaywright = true)
        => new(exe, headed, o.Quiet, o.AllowThirdPartyCookies, o.TransparentProxy, viaPlaywright, o.IgnoreDefaultArgs, o.StockRuntime);

    /// fpArgs + extArgs + proxyArgs + quic + (privacy-sandbox unless disabled==false) + webrtc-deny
    /// (+ engine extras + GPU blocklist), then userArgs appended last, then feature-flags collapsed,
    /// then 152 r22+ switches this engine lacks dropped with a warning. Mirrors index.ts assembleArgs.
    internal static List<string> AssembleArgs(List<string> fpArgs, List<string> extArgs, List<string> proxyArgs,
        bool? disablePrivacySandbox, string? webrtcIp, IReadOnlyList<string> userArgs, ProxyOptions? proxyForQuic, bool socks5Udp,
        EngineExtras? extra = null)
    {
        var baseList = new List<string>();
        baseList.AddRange(fpArgs);
        baseList.AddRange(extArgs);
        baseList.AddRange(proxyArgs);
        baseList.AddRange(LaunchOpts.QuicArgs(proxyForQuic));
        // Opt-in: relay WebRTC UDP through the proxy rather than denying it outright.
        baseList.AddRange(LaunchOpts.Socks5UdpArgs(socks5Udp, proxyForQuic));
        // navigator.bluetooth must follow the CLAIM, in both directions: a Linux build hides it
        // while exposing usb/serial/hid (an OS-origin tell on a Windows persona), and a Windows
        // build shows it under a LINUX claim, which genuine Chrome 154 on Linux does not. Read
        // back out of the already-built persona switches; the host is never consulted.
        // See LaunchOpts.WebBluetoothArgs.
        const string platFlag = "--fingerprint-platform=";
        baseList.AddRange(LaunchOpts.WebBluetoothArgs(
            fpArgs.Find(a => a.StartsWith(platFlag, System.StringComparison.Ordinal))?[platFlag.Length..]));
        // DEFAULT FLIPPED IN 0.23.0 — opt IN to disabling, rather than opt out.
        //
        // Disabling Topics/FLEDGE/Shared Storage/Fenced Frames is coherent for a de-Googled
        // persona and incoherent for the default one: Brand "chrome" claims Google Chrome, which
        // ships all of them. Measured on the live audit against 150-r10, "a build claiming Chrome
        // carries the Privacy Sandbox surface Chrome ships" failed as an implausible value — the
        // same defect class as the WebUSB split fixed in r7. Set DisablePrivacySandbox = true when
        // the persona genuinely is de-Googled Chromium.
        // Playwright disables third-party storage partitioning, which genuine Chrome never does;
        // re-emit its list without that entry as the last --disable-features.
        if (extra is { ViaPlaywright: true }) baseList.AddRange(LaunchOpts.PlaywrightFeatureOverrideArgs(extra.IgnoreDefaultArgs));
        if (disablePrivacySandbox == true) baseList.AddRange(LaunchOpts.PrivacySandboxArgs());
        baseList.AddRange(LaunchOpts.WebrtcDefaultDenyArgs(baseList.Concat(userArgs), webrtcIp));
        if (extra is not null)
        {
            baseList.AddRange(LaunchOpts.EngineExtrasArgs(extra.AllowThirdPartyCookies, extra.TransparentProxy, proxyForQuic, extra.Quiet));
            // Opt-in, engine r32+: Chromium's own DevTools Runtime behaviour, only on an engine that has the switch.
            baseList.AddRange(LaunchOpts.StockRuntimeArgs(extra.Exe, extra.StockRuntime, userArgs, extra.Quiet));
            baseList.AddRange(LaunchOpts.GpuBlocklistArgs(extra.Headed, null, userArgs));
            // On a Linux host, render WebGL through the backend whose limits match the CLAIMED
            // platform (read back from the built persona switch; absent = pass-through).
            baseList.AddRange(LaunchOpts.GpuBackendArgs(
                fpArgs.Find(a => a.StartsWith(platFlag, System.StringComparison.Ordinal))?[platFlag.Length..],
                extra.Headed, null, userArgs));
        }
        var merged = LaunchOpts.MergeFeatureFlags(baseList.Concat(userArgs));
        // Last: drop 152 r22+ switches this engine does not implement (with a warning), wherever they came from.
        return extra is null ? merged : LaunchOpts.GateEngineSwitches(extra.Exe, merged, extra.Quiet).Args;
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
/// <remarks>
/// CloseAsync WAITS FOR THE ENGINE TO EXIT, then removes what it leaves in the temp directory: the
/// profile ServeAsync made, and on Linux and macOS the engine's singleton-socket directory
/// (org.chromium.Chromium.*), whichever profile it ran on. A killed engine never removes that
/// directory itself, and CloseAsync used to kill it and return at once: every ServeAsync + CloseAsync
/// left one behind.
/// </remarks>
public sealed class Server
{
    private readonly Process _proc;
    private readonly string _userDataDir;
    private readonly bool _ownUdd;
    private readonly LeaseSession? _lease;
    private readonly LaunchToken? _launchToken;
    private readonly string? _socketDir;
    private readonly EventHandler _onProcessExit;
    private readonly object _gate = new();
    private Task? _closing;

    internal Server(Process proc, string host, int port, string userDataDir, bool ownUdd, LeaseSession? lease, LaunchToken? launchToken = null)
    {
        _proc = proc; Host = host; Port = port; _userDataDir = userDataDir; _ownUdd = ownUdd; _lease = lease; _launchToken = launchToken;
        // Now, while the engine runs: one that shuts down cleanly unlinks it from the profile.
        _socketDir = ThrowawayProfile.BrowserProcessFiles(userDataDir).SocketDir;
        _onProcessExit = (_, _) => { try { CloseAsync().GetAwaiter().GetResult(); } catch { } };
        AppDomain.CurrentDomain.ProcessExit += _onProcessExit;
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

    /// Stop the engine and wait for it to exit, release the lease (best-effort), then remove the temp
    /// profile ServeAsync made and the engine's singleton-socket directory. Closes once.
    public Task CloseAsync()
    {
        lock (_gate) return _closing ??= CloseCoreAsync();
    }

    private async Task CloseCoreAsync()
    {
        var socketDir = _socketDir ?? ThrowawayProfile.BrowserProcessFiles(_userDataDir).SocketDir;
        try { if (!_proc.HasExited) _proc.Kill(entireProcessTree: true); } catch { }
        try
        {
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await _proc.WaitForExitAsync(cts.Token).ConfigureAwait(false);
        }
        catch { }   // still running at the deadline: remove what can be removed all the same
        // Release the concurrency slot (best-effort) + remove the run-token file.
        if (_lease is not null) { try { await _lease.StopAsync().ConfigureAwait(false); } catch { } }
        _launchToken?.Release();
        if (socketDir is not null) { try { Directory.Delete(socketDir, recursive: true); } catch { } }
        if (_ownUdd) ThrowawayProfile.DeleteDirectory(_userDataDir);
        AppDomain.CurrentDomain.ProcessExit -= _onProcessExit;  // one per ServeAsync would otherwise pile up
    }
}
