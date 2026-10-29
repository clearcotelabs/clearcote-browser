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
public static partial class Clearcote
{
    /// This SDK's version (kept in lockstep with the npm/PyPI SDKs).
    public const string Version = "0.65.1";

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
        Offline.RequireOfflineBinary(options.OfflineKey, options.ExecutablePath);  // offline mode never downloads
        return await ResolveBinaryAsync(options).ConfigureAwait(false);
    }

    // ExecutablePath > CLEARCOTE_BINARY > catalog version > PRO > free (index.ts executablePath).
    private static async Task<string> ResolveBinaryAsync(LaunchOptions options)
    {
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

    /// Pre-fetch + verify the Clearcote binary without launching (returns its path). Resolved like a launch:
    /// CLEARCOTE_BINARY, then <see cref="DownloadOptions.Version"/> (or CLEARCOTE_BROWSER_VERSION: "150",
    /// "150.0.7871.115", "latest", a PRO rebuild "150.0.7871.114-r7" / "r7"; PRO-tier versions need a licence key),
    /// then the PRO build when a licence key resolves (<see cref="DownloadOptions.LicenseKey"/> &gt;
    /// CLEARCOTE_LICENSE_KEY &gt; ~/.clearcote/license.key) on its <see cref="DownloadOptions.ReleaseChannel"/>,
    /// else the free build.
    public static Task<string> DownloadAsync(DownloadOptions? options = null)
    {
        options ??= new DownloadOptions();
        return ResolveBinaryAsync(new LaunchOptions
        {
            CacheDir = options.CacheDir, Quiet = options.Quiet, AutoUpdate = options.AutoUpdate, Version = options.Version,
            ReleaseChannel = options.ReleaseChannel, LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase,
        });
    }

    /// The fonts a launch of the build at <paramref name="executablePath"/> would see on this host: the font
    /// section of Node's and Python's <c>clearcote info</c> (this SDK has no CLI). On Linux: whether the build
    /// bundles its Windows font clones, which of 29 scripts the fonts can draw (bundle +
    /// <paramref name="fontDirs"/> / CLEARCOTE_FONT_DIRS + CLEARCOTE_FALLBACK_FONT_DIRS) and which are missing,
    /// and which key Windows families your own fonts provide for real. Off Linux the browser uses the host's
    /// own fonts and the report says so. Reads files only: nothing starts and nothing is downloaded.
    /// <c>report.ToString()</c> is the text the CLIs print.
    public static FontReport FontReportFor(string executablePath, IEnumerable<string>? fontDirs = null)
    {
        ArgumentException.ThrowIfNullOrEmpty(executablePath);
        return Fonts.Report(executablePath, fontDirs);
    }

    /// <see cref="FontReportFor"/> for the build a launch with <paramref name="options"/> would run, resolved as
    /// <see cref="ExecutablePathAsync"/> does (so it downloads and verifies that build when it is not cached
    /// yet), with <see cref="LaunchOptions.FontDirs"/> as the launch would take them.
    public static async Task<FontReport> FontReportAsync(LaunchOptions? options = null)
    {
        options ??= new LaunchOptions();
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        return Fonts.Report(exe, options.FontDirs);
    }

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
    /// The same two lines apply to a HEADED launch on a Linux host without a window manager (Xvfb,
    /// every Docker/VPS headful run), where Chrome's 945x1060 default window otherwise stays at
    /// (10,10); the persistent and ephemeral-profile launches fit it on the first page for you
    /// (<see cref="Geometry.HeadedLinuxFit(IEnumerable{string}?, bool?, string?)"/>).
    /// On an r39+ engine with a persona (<see cref="Geometry.EnginePlacesPersonaWindow"/>) the engine
    /// opens the persona's own display and window, so this sets no display switch and the fit line is
    /// left out: <c>ViewportSize = ViewportSize.NoViewport</c> alone gives the persona's window.
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
    /// MACOS. With a licence key on an Apple Silicon Mac, this runs the native macOS PRO build (a
    /// <c>Chromium.app</c> bundle, downloaded and verified like the other platforms). Its fonts and canvas
    /// text metrics stay host-native, so only Platform = "macos" (the default there) is coherent; another
    /// platform without a FingerprintProfile captured on that OS warns. Without a licence key (the free build
    /// has no macOS binary), or on an Intel Mac, this starts the Clearcote Docker image instead
    /// (teamflatearth/clearcote:sdk-&lt;version&gt;; <see cref="LaunchOptions.DockerImage"/> or
    /// CLEARCOTE_DOCKER_IMAGE picks another) and returns the same Playwright <see cref="IBrowser"/>, connected
    /// to it over CDP; CloseAsync stops the container. The persona options, Headless, Proxy, Args, Version and
    /// the licence key go to the container; an option it cannot take is refused by name.
    /// <see cref="DockerUnavailableException"/> says so when Docker is missing or not running.
    /// <see cref="LaunchOptions.Docker"/> = true (or CLEARCOTE_DOCKER=1) uses the container on any OS, a
    /// licensed Mac included; false (or CLEARCOTE_DOCKER=0) never does. <see cref="DockerContainerOf"/> names
    /// the container.
    /// </para>
    /// </remarks>
    public static async Task<IBrowser> LaunchAsync(LaunchOptions? options = null)
    {
        if (CloudLaunch.Requested(options)) return await CloudLaunch.LaunchBrowserAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        // macOS without the native PRO build: the Clearcote Docker image, connected over CDP.
        if (DockerLaunch.Requested(options)) return await DockerLaunch.LaunchBrowserAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        var hz = HumanizeLaunch.Of(options, cloud: false);   // Humanize / ShowCursor: a bad option throws before anything starts
        CheckPlaywrightPassThrough(options);
        var prepared = await PrepareLaunchAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        options = prepared.Options;
        var egressDrift = prepared.EgressDrift;
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        DropAutoWebrtcIp(options, prepared.Geo);  // open build + proxy: no default WebRTC IP
        // LocalProfile = "auto": a captured fingerprint for this host, once the binary is known (ProfileAuto.ApplyAsync).
        await ProfileAuto.ApplyAsync(options, exe).ConfigureAwait(false);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease
        Crlset.WarnNoProfile(options, "LaunchAsync");  // incognito: no profile to seed a CRLSet into

        // http(s) credentials go to the engine's --proxy-auth only when THIS engine implements it (r19+).
        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy, LaunchOpts.EngineSupportsSwitch(exe, "proxy-auth"));
        LaunchOpts.WarnUnsupportedEngineOptions(exe, options, options.Proxy, options.Quiet);
        // Credentials moved to the engine: Playwright's request API gets a local credential-less forwarder
        // instead of no proxy at all, which sent it from this machine's IP (RequestProxy). It starts just
        // before the browser; its bypass-list override is an engine arg, so it is known now.
        var reqUpstream = proxy is null ? RequestProxy.RequestProxyUpstream(options.Proxy, proxyArgs) : null;
        if (reqUpstream is not null) proxyArgs.AddRange(RequestProxy.RequestProxyArgs(proxyArgs));
        var args = AssembleArgs(Agent.WithPersona(options), ExtensionAndPortableArgs(options),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false));
        EmitCoherenceWarnings(options, exe, args, options.Headless, PassThroughDevtools(options.PlaywrightLaunchOptions), userAgent: null);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        // Offline mode: no licence key in play, so nothing below reaches the network (Offline.cs).
        var licKey = Offline.OfflineKeyPath(options.OfflineKey) is null ? License.ResolveLicenseKey(options.LicenseKey) : null;
        // Widevine: on by default for a licensed launch (licKey: the key that selects the PRO build, which claims
        // the Google Chrome brand) on an engine with --widevine-cdm-path, which loads the CDM without a profile;
        // opt-in for a free one. Bounded + fail-soft by default; before the lease, so a slow fetch holds no slot.
        args.AddRange(await Widevine.LaunchArgsAsync(exe, options.Widevine, licKey is not null, options.Args, options.Quiet).ConfigureAwait(false));
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy, OfflineKey = options.OfflineKey },
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

        // Headless: the display is browser-wide, so it applies even here (see the geometry caveat). An r39+
        // engine with a persona opens the persona's display and window itself: no display switch then.
        var display = Geometry.ResolveHeadless(options.Headless, options.Fingerprint, args, callerSetGeometry: false, exe: exe);
        // Engine 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the
        // engine reads them from there (see PersonaEnv). Probed once; every attempt's env gets the payload.
        // r51+: the signed persona token, fetched once; each attempt's env carries it (Persona.cs).
        var personaLaunch = await PersonaToken.PrepareAsync(exe, args.Concat(display.Args), lease,
            options.LicenseThroughProxy == true ? ProxySpec.From(options.Proxy) : null, runTokenFile: launchToken?.File).ConfigureAwait(false);
        var persona = PersonaEnv.Plan(exe, personaLaunch.Args, options.PersonaEnv);

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        var reqProxy = reqUpstream is not null ? RequestProxy.Route(options.Proxy, proxyArgs, options.Quiet) : null;
        if (reqProxy is not null) proxy = reqProxy.PlaywrightProxy;
        IBrowser browser;
        try
        {
            browser = await License.ReleaseLeaseOnFailureAsync(lease, () => License.RetryOnStaleRunTokenAsync(lease, () =>
                WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchAsync(new BrowserTypeLaunchOptions(options.PlaywrightLaunchOptions ?? new())
                {
                    ExecutablePath = exePath,
                    Args = persona.Args.ToArray(),
                    Headless = options.Headless,
                    Channel = options.Channel,
                    SlowMo = options.SlowMo,
                    Timeout = options.Timeout,
                    IgnoreDefaultArgs = options.IgnoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs.ToArray(),
                    Env = persona.Env(personaLaunch.Env(envFor())),
                    Proxy = ToPwProxy(proxy),
                }), exe)), launchToken).ConfigureAwait(false);
        }
        catch
        {
            if (reqProxy is not null) await reqProxy.CloseAsync().ConfigureAwait(false);   // the browser never came up
            throw;
        }
        if (reqProxy is not null) browser.Disconnected += (_, _) => { _ = reqProxy.CloseAsync(); };

        // Release the concurrency slot + remove the run-token file when the browser closes.
        if (lease is not null)
        {
            // The stop is kept so a caller that must hand the seat on (the profile "auto" probe launch) can wait for it.
            var released = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            LeaseReleases.AddOrUpdate(browser, released.Task);
            browser.Disconnected += (_, _) =>
            {
                launchToken?.Release();
                _ = lease.StopAsync().ContinueWith(_ => released.TrySetResult(), TaskScheduler.Default);
            };
        }
        await egressDrift.ConfigureAwait(false);   // a rotating proxy's warning lands before the launch returns
        // Every page and context it makes (and their popups) gets the persona of the fingerprint seed.
        return (hz with { Seed = options.Fingerprint }).Wrap(browser);
    }

    static readonly System.Runtime.CompilerServices.ConditionalWeakTable<IBrowser, Task> LeaseReleases = new();

    /// Completes once the seat of a browser from <see cref="LaunchAsync"/> has been handed back after it closed
    /// (at once for a browser that held no seat), bounded by <paramref name="timeoutMs"/>.
    internal static async Task LeaseReleasedAsync(IBrowser browser, int timeoutMs = 10_000)
    {
        if (!LeaseReleases.TryGetValue(browser, out var released)) return;
        await Task.WhenAny(released, Task.Delay(timeoutMs)).ConfigureAwait(false);
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
    /// <para>
    /// MACOS. Without the native PRO build this runs the Clearcote Docker image, as <see cref="LaunchAsync"/> does,
    /// and returns the container browser's own context (the image keeps its profile in the container); closing it
    /// stops the container. <see cref="DockerContainerOf"/> names the container.
    /// </para>
    /// </remarks>
    public static async Task<IBrowserContext> LaunchEphemeralProfileAsync(LaunchOptions? options = null)
    {
        if (CloudLaunch.Requested(options))
            return await CloudLaunch.LaunchContextAsync(options ?? new LaunchOptions(), persistent: false, userDataDir: null).ConfigureAwait(false);
        // macOS without the native PRO build: the Clearcote Docker image, like LaunchAsync (index.ts launch()).
        if (DockerLaunch.Requested(options))
            return await DockerLaunch.LaunchContextAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
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
        var hz = HumanizeLaunch.Of(options, cloud: false);   // Humanize / ShowCursor: a bad option throws before anything starts
        CheckPlaywrightPassThrough(options, context: true);
        var prepared = await PrepareLaunchAsync(options ?? new LaunchOptions()).ConfigureAwait(false);
        options = prepared.Options;
        var egressDrift = prepared.EgressDrift;
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        DropAutoWebrtcIp(options, prepared.Geo);  // open build + proxy: no default WebRTC IP
        // LocalProfile = "auto": a captured fingerprint for this host, once the binary is known (ProfileAuto.ApplyAsync).
        await ProfileAuto.ApplyAsync(options, exe).ConfigureAwait(false);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease

        // Widevine ASKED FOR (Widevine = true or CLEARCOTE_WIDEVINE=1): seed the CDM into this profile and let the
        // component updater register it, so an engine without --widevine-cdm-path loads it too. Best-effort: a
        // failure says so (unless Quiet) and the launch goes on without DRM. The licensed DEFAULT only uses the
        // switch (below). IgnoreDefaultArgs then carries --disable-component-update on top of the SDK's strip.
        var userArgs = options.Args ?? Array.Empty<string>();
        var ignoreDefaultArgs = options.IgnoreDefaultArgs ?? LaunchOpts.DefaultIgnoredArgs;
        if (Widevine.Mode(options.Widevine, licensed: false) == WidevineMode.Explicit)
        {
            try
            {
                await Widevine.SeedAsync(userDataDir, quiet: options.Quiet).ConfigureAwait(false);
                (ignoreDefaultArgs, userArgs) = Widevine.ComponentUpdaterArgs(options.IgnoreDefaultArgs, options.Args, options.Quiet);
            }
            catch (Exception e)
            {
                if (!options.Quiet) Console.Error.WriteLine($"[clearcote] [widevine] setup failed (continuing without DRM): {e.Message}");
            }
        }
        // CRLSet (opt-in): Chrome's revocation list, which the engine never downloads. Seeded into this profile with the
        // component updater un-suppressed; the launch then waits until it is live (see Crlset). Never throws.
        var (crlsetPlan, crlsetIgnore) = await Crlset.ApplyLaunchAsync(userDataDir, options, ignoreDefaultArgs).ConfigureAwait(false);
        ignoreDefaultArgs = crlsetIgnore;

        // http(s) credentials go to the engine's --proxy-auth only when THIS engine implements it (r19+).
        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy, LaunchOpts.EngineSupportsSwitch(exe, "proxy-auth"));
        LaunchOpts.WarnUnsupportedEngineOptions(exe, options, options.Proxy, options.Quiet);
        // As LaunchAsync: the request API reaches the proxy through a local forwarder (RequestProxy).
        var reqUpstream = proxy is null ? RequestProxy.RequestProxyUpstream(options.Proxy, proxyArgs) : null;
        if (reqUpstream is not null) proxyArgs.AddRange(RequestProxy.RequestProxyArgs(proxyArgs));
        var args = AssembleArgs(Agent.WithPersona(options), ExtensionAndPortableArgs(options),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, userArgs, options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false));
        EmitCoherenceWarnings(options, exe, args, options.Headless, PassThroughDevtools(options.PlaywrightContextOptions),
            options.PlaywrightContextOptions?.UserAgent);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        // Offline mode: no licence key in play, so nothing below reaches the network (Offline.cs).
        var licKey = Offline.OfflineKeyPath(options.OfflineKey) is null ? License.ResolveLicenseKey(options.LicenseKey) : null;
        // Widevine: on by default for a licensed launch (licKey selects the PRO build) on an r32+ engine; asked for,
        // best-effort as always. Before the lease, so a slow fetch holds no slot.
        args.AddRange(await Widevine.LaunchArgsAsync(exe, options.Widevine, licKey is not null, options.Args, options.Quiet).ConfigureAwait(false));
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy, OfflineKey = options.OfflineKey },
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

        // An r39+ engine with a persona opens the persona's display and window itself (Mode.PersonaNative):
        // no display switch and no fit; under an explicit viewport the window is reconciled with it.
        var geometry = Geometry.ResolveHeadless(
            options.Headless, options.Fingerprint, args,
            callerSetGeometry: options.ViewportSize is not null || options.ScreenSize is not null,
            exe: exe, callerViewport: options.ViewportSize);
        // Headed Linux with no window manager (Xvfb: every Docker/VPS headful run) leaves Chrome's
        // 945x1060 default window at (10,10); the same first-page fit as headless maximizes it. A
        // caller's own viewport or window switch keeps theirs; other headed platforms keep NoViewport alone,
        // and so does an r39+ persona window (the engine sizes it at creation).
        var headedFit = options.Headless == false && options.ViewportSize is null && Geometry.HeadedLinuxFit(args, null, exe);
        // Regime 2 appends the headless display; the fit below keeps reading the caller's args. Then engine
        // 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the engine
        // reads them from there (see PersonaEnv). Probed once; every attempt's env gets the payload.
        // r51+: the signed persona token, fetched once; each attempt's env carries it (Persona.cs).
        var personaLaunch = await PersonaToken.PrepareAsync(exe, args.Concat(geometry.Args), lease,
            options.LicenseThroughProxy == true ? ProxySpec.From(options.Proxy) : null, runTokenFile: launchToken?.File).ConfigureAwait(false);
        var persona = PersonaEnv.Plan(exe, personaLaunch.Args, options.PersonaEnv);

        var pw = await PlaywrightAsync().ConfigureAwait(false);
        // The CRLSet readiness probe's port must bypass a Playwright-handled proxy, so its server starts first.
        var crlsetArgs = crlsetPlan?.BeforeLaunch(proxy) ?? new List<string>();
        // After the probe: the forwarder is a proxy the browser never uses, so it needs no bypass for the probe.
        var reqProxy = reqUpstream is not null ? RequestProxy.Route(options.Proxy, proxyArgs, options.Quiet) : null;
        if (reqProxy is not null) proxy = reqProxy.PlaywrightProxy;
        IBrowserContext context;
        try
        {
            context = await Crlset.CloseOnFailureAsync(crlsetPlan, () => License.ReleaseLeaseOnFailureAsync(lease, () => License.RetryOnStaleRunTokenAsync(lease, () =>
                WinLaunch.WinAvRetryAsync(exePath => pw.Chromium.LaunchPersistentContextAsync(userDataDir,
                new BrowserTypeLaunchPersistentContextOptions(options.PlaywrightContextOptions ?? new())
                {
                    ExecutablePath = exePath,
                    Args = persona.Args.Concat(crlsetArgs).ToArray(),
                    Headless = options.Headless,
                    Channel = options.Channel,
                    SlowMo = options.SlowMo,
                    Timeout = options.Timeout,
                    IgnoreDefaultArgs = ignoreDefaultArgs.ToArray(),  // the caller's, else the SDK's strip (+ component updater on when Widevine is asked for)
                    Env = persona.Env(personaLaunch.Env(envFor())),
                    Proxy = ToPwProxy(proxy),
                    // Headed with no explicit viewport -> real window size (matches launch()).
                    // Headless -> the persona's display (regime 1) or the SDK's (regime 2), and the window
                    // is fitted to its work area below; an r39+ persona window as the engine opened it.
                    // See Geometry.
                    ViewportSize = Geometry.ContextViewport(geometry, options.Headless, options.ViewportSize, options.ScreenSize),
                    ScreenSize = options.ScreenSize,
                    // r32+: the persona's colour scheme reaches the page instead of Playwright's light.
                    ColorScheme = ColorSchemeDefault.Resolve(options.ColorScheme, exe),
                }), exe)), launchToken)).ConfigureAwait(false);
        }
        catch
        {
            if (reqProxy is not null) await reqProxy.CloseAsync().ConfigureAwait(false);   // the browser never came up
            throw;
        }
        if (reqProxy is not null) context.Close += (_, _) => { _ = reqProxy.CloseAsync(); };

        // Release the concurrency slot + remove the run-token file when the context closes.
        if (lease is not null) context.Close += (_, _) => { _ = lease.StopAsync(); launchToken?.Release(); };
        // Before anything touches the first tab: it must stay idle on about:blank until the CRLSet is live.
        if (crlsetPlan is not null) await crlsetPlan.WaitAsync(context).ConfigureAwait(false);
        // Both headless regimes, and headed Linux: maximize into the display's work area. It never
        // throws, so a launch cannot fail on it, and it runs while the context is still on about:blank,
        // so the caller's page never observes a resize. An r39+ persona window gets no fit: under an
        // explicit viewport only the reconcile (normal state, viewport + frame, kept on the work area).
        if (geometry.Mode != Geometry.Mode.None || headedFit)
            await Geometry.InstallWindowFixupAsync(context, args, geometry).ConfigureAwait(false);
        await egressDrift.ConfigureAwait(false);   // a rotating proxy's warning lands before the launch returns
        // Every page (and popup) of the context gets the persona of the fingerprint seed.
        Humanize.Install(context, hz with { Seed = options.Fingerprint });
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
        var prepared = await PrepareLaunchAsync(options ?? new ServeOptions()).ConfigureAwait(false);
        options = prepared.Options;
        var egressDrift = prepared.EgressDrift;
        var host = string.IsNullOrEmpty(options.Host) ? "127.0.0.1" : options.Host;
        var exe = await ExecutablePathAsync(options).ConfigureAwait(false);
        EnsureRunnableHere(exe);
        DropAutoWebrtcIp(options, prepared.Geo);  // open build + proxy: no default WebRTC IP
        // LocalProfile = "auto": a captured fingerprint for this host, once the binary is known (ProfileAuto.ApplyAsync).
        await ProfileAuto.ApplyAsync(options, exe).ConfigureAwait(false);
        var fontDirs = Fonts.CheckFontDirs(options.FontDirs);  // Linux fontconfig dirs: a typo throws before the lease
        Crlset.WarnNoProfile(options, "ServeAsync");  // a raw CDP endpoint: no readiness wait to hold it

        // passProxy: what is left for a plain --proxy-server once credentials went to the engine's own switches
        // (--socks5-credentials, or --proxy-auth on an engine that implements it).
        var (proxyArgs, proxy) = LaunchOpts.ResolveProxy(options.Proxy, LaunchOpts.EngineSupportsSwitch(exe, "proxy-auth"));
        LaunchOpts.WarnUnsupportedEngineOptions(exe, options, options.Proxy, options.Quiet);
        // A client attached over CDP runs Playwright's request API in its own process with no proxy of its
        // own (RequestProxy): nothing here can route it, so say so.
        if (!options.Quiet && RequestProxy.RequestProxyUpstream(options.Proxy, proxyArgs) is not null)
            Console.Error.WriteLine(RequestProxy.ServeWarning);
        // serve launches the binary directly, so Playwright's SwiftShader default is never added; the
        // blocklist rule still applies to a headed endpoint and on Windows.
        var engineArgs = AssembleArgs(Agent.WithPersona(options), ExtensionAndPortableArgs(options),
            proxyArgs, options.DisablePrivacySandbox, options.WebrtcIp, options.Args ?? Array.Empty<string>(), options.Proxy, options.Socks5Udp,
            Extras(options, exe, headed: options.Headless == false, viaPlaywright: false));

        var port = options.Port ?? FreePort();
        var ownUdd = string.IsNullOrEmpty(options.UserDataDir);
        var userDataDir = ownUdd ? Directory.CreateTempSubdirectory("clearcote-serve-").FullName : options.UserDataDir!;
        var origins = options.AllowOrigins ?? $"http://{host}:{port},http://localhost:{port}";
        // A non-loopback bind or a "*" origin list hands the browser to whoever can reach the port.
        EmitCoherenceWarnings(options, exe, engineArgs, options.Headless != false, devtools: false, userAgent: null);
        LaunchWarnings.Emit(LaunchWarnings.ForServe(host, origins), options.Quiet);
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
            headless: options.Headless != false, exe: exe, windowSize: options.WindowSize);
        if (geometry is not null) cdpArgs.AddRange(geometry.Args);

        var licVersion = options.Version ?? Environment.GetEnvironmentVariable("CLEARCOTE_BROWSER_VERSION");
        // Offline mode: no licence key in play, so nothing below reaches the network (Offline.cs).
        var licKey = Offline.OfflineKeyPath(options.OfflineKey) is null ? License.ResolveLicenseKey(options.LicenseKey) : null;
        // Widevine: as LaunchAsync (on by default for a licensed launch on an r32+ engine, opt-in otherwise).
        cdpArgs.AddRange(await Widevine.LaunchArgsAsync(exe, options.Widevine, licKey is not null, options.Args, options.Quiet).ConfigureAwait(false));
        var lease = await License.AcquireLeaseAsync(
            new LicenseOptions { LicenseKey = options.LicenseKey, LicenseApiBase = options.LicenseApiBase, LicenseThroughProxy = options.LicenseThroughProxy, OfflineKey = options.OfflineKey },
            Version, options.Quiet,   // sdk_version = the SDK PACKAGE version
            () => Download.ResolvedEngineVersionAsync(licVersion, licKey is not null), options.Proxy).ConfigureAwait(false);
        // Bind a per-launch run-token file (r23+ engine online-enforcement opt-in); passed ALONGSIDE
        // CLEARCOTE_RUN_TOKEN. Inert in free mode.
        var launchToken = lease?.BindLaunch();
        // Engine 1021: the persona switches leave the browser's argv for CLEARCOTE_PERSONA_ARGS when the
        // engine reads them from there (see PersonaEnv). Last, so everything above still reads them from
        // engineArgs; cdpArgs never carry one and stay as they are.
        // r51+: the signed persona token (Persona.cs), fetched once before the launch.
        var personaLaunch = await PersonaToken.PrepareAsync(exe, engineArgs, lease,
            options.LicenseThroughProxy == true ? ProxySpec.From(options.Proxy) : null, runTokenFile: launchToken?.File).ConfigureAwait(false);
        var persona = PersonaEnv.Plan(exe, personaLaunch.Args, options.PersonaEnv);

        var proc = await License.ReleaseLeaseOnFailureAsync(lease, () => WinLaunch.WinAvRetryAsync(exePath =>
        {
            var psi = new ProcessStartInfo(exePath) { UseShellExecute = false };
            foreach (var a in persona.Args.Concat(cdpArgs)) psi.ArgumentList.Add(a);
            if (persona.Payload is not null) psi.Environment[PersonaEnv.EnvVar] = persona.Payload;
            foreach (var kv in personaLaunch.Vars) psi.Environment[kv.Key] = kv.Value;
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
        // display onto the persona's). Its own connection, closed again; never fails the launch. A
        // caller's own --window-position keeps its place (Fit false: the display was still set). An r39+
        // persona window (Native) is fitted only to an explicit WindowSize, at its own position.
        if (geometry is not null && geometry.Fit)
            await Geometry.FitServedWindowAsync(await srv.WsUrlAsync().ConfigureAwait(false), geometry.Persona, options.WindowSize,
                native: geometry.Native).ConfigureAwait(false);
        if (!options.Quiet) Console.Error.WriteLine($"[clearcote] serve: CDP endpoint ready at {srv.CdpUrl}");
        await egressDrift.ConfigureAwait(false);   // a rotating proxy's warning lands before the launch returns
        return srv;
    }

    /// One CDP endpoint, many identities: an HTTP/WebSocket front on <see cref="MultiplexOptions.Port"/>
    /// (default 9222) that starts one <see cref="ServeAsync"/> browser per identity, chosen by the
    /// connection URL (<c>ConnectOverCDPAsync("http://127.0.0.1:9222?fingerprint=acct-1&amp;platform=windows")</c>).
    /// See <see cref="MultiplexServer"/> and the comment at the top of Multiplex.cs.
    public static Task<MultiplexServer> ServeMultiplexAsync(MultiplexOptions? options = null)
        => MultiplexServer.StartAsync(options);

    // ── internals ────────────────────────────────────────────────────────────

    /// Copy the caller's options (never mutate them), layer a saved LocalProfile under them, and apply geoip
    /// (on by default; <c>Geoip = true</c> fails closed BEFORE the binary is resolved or any browser starts).
    internal static async Task<T> PrepareAsync<T>(T options) where T : LaunchOptions
        => (await PrepareLaunchAsync(options).ConfigureAwait(false)).Options;

    /// <see cref="PrepareAsync"/>, plus the geoip egress-drift check it started (a completed task without geoip):
    /// the launch awaits it before it returns, as the Node and Python SDKs do. <c>Geo</c>: what geoip did, for
    /// <see cref="DropAutoWebrtcIp"/> once the binary is resolved.
    internal static async Task<(T Options, Task EgressDrift, GeoIp.GeoipApplied Geo)> PrepareLaunchAsync<T>(T options) where T : LaunchOptions
    {
        var copy = (T)options.Clone();
        // LocalProfile = a saved persona: its options fill what the caller left unset ("auto" waits for the binary).
        ProfileOptionsMap.MergeSaved(copy);
        // geoip is on by default: the proxy's exit, or this machine's own IP without a proxy (GeoIp.Mode).
        var geo = await GeoIp.ApplyModeAsync(copy, copy.Proxy, GeoIp.Mode(copy.Geoip), copy.Quiet).ConfigureAwait(false);
        return (copy, geo.EgressDrift, geo);
    }

    /// Open build + proxy: no WebRTC IP from the DEFAULT geoip lookup (<see cref="GeoIp.DropAutoWebrtcIp"/>). Licensed
    /// = a licence key resolves (the PRO build) or offline mode (index.ts <c>!!pro || offline</c>).
    internal static void DropAutoWebrtcIp(LaunchOptions o, GeoIp.GeoipApplied geo)
        => GeoIp.DropAutoWebrtcIp(o, geo, o.Proxy,
            licensed: Offline.OfflineKeyPath(o.OfflineKey) is not null || License.ResolveLicenseKey(o.LicenseKey) is not null);

    // ── launch lane: shared steps of LaunchAsync / LaunchPersistentContextAsync / ServeAsync ──

    /// --load-extension (+ --disable-extensions-except), then the profile-portability switch (index.ts
    /// [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)]).
    internal static List<string> ExtensionAndPortableArgs(LaunchOptions o)
        => LaunchOpts.ExtensionArgs(o.Extensions).Concat(LaunchOpts.PortableArgs(o.PortableProfile == true, o.EncryptionKey)).ToList();

    /// The coherence warnings (and once-per-process notes) for a local launch: the persona after geoip, the
    /// caller's proxy and args, and the Playwright pass-through's Devtools / UserAgent. <paramref name="args"/>:
    /// the assembled browser args, to tell whether this browser runs with the stock DevTools Runtime.
    private static void EmitCoherenceWarnings(LaunchOptions o, string exe, IEnumerable<string> args, bool? headless, bool devtools, string? userAgent)
        => LaunchWarnings.EmitCoherence(
            LaunchWarnings.Coherence(o, o.Proxy, o.Geoip == true, headless, o.Args, devtools, userAgent),
            o.Quiet, stockRuntime: LaunchOpts.StockRuntimeRunning(exe, args.Concat(o.Args ?? Array.Empty<string>())));

#pragma warning disable CS0612, CS0618   // Devtools is deprecated in Playwright, and still honoured: it opens DevTools
    private static bool PassThroughDevtools(BrowserTypeLaunchOptions? o) => o?.Devtools == true;
    private static bool PassThroughDevtools(BrowserTypeLaunchPersistentContextOptions? o) => o?.Devtools == true;
#pragma warning restore CS0612, CS0618

    /// The fields <see cref="LaunchOptions"/> owns, by their Playwright name: the pass-through objects must not set them.
    private static readonly string[] OwnedLaunchFields =
        { "ExecutablePath", "Args", "Env", "Headless", "Channel", "SlowMo", "Timeout", "IgnoreDefaultArgs", "Proxy" };
    private static readonly string[] OwnedContextFields = OwnedLaunchFields.Concat(new[] { "ViewportSize", "ScreenSize", "ColorScheme" }).ToArray();

    /// Throw, naming it, when <see cref="LaunchOptions.PlaywrightLaunchOptions"/> (or, for a persistent context,
    /// <see cref="LaunchOptions.PlaywrightContextOptions"/>) sets a field LaunchOptions has its own property for:
    /// the SDK fills those itself, and one set in two places would silently lose one of them.
    internal static void CheckPlaywrightPassThrough(LaunchOptions? o, bool context = false)
    {
        object? pass = context ? o?.PlaywrightContextOptions : o?.PlaywrightLaunchOptions;
        if (pass is null) return;
        var name = context ? nameof(LaunchOptions.PlaywrightContextOptions) : nameof(LaunchOptions.PlaywrightLaunchOptions);
        foreach (var field in context ? OwnedContextFields : OwnedLaunchFields)
        {
            if (pass.GetType().GetProperty(field)?.GetValue(pass) is not null)
                throw new ArgumentException(
                    $"{name}.{field} is set: set LaunchOptions.{field} instead (the SDK fills {field} on the options it passes to Playwright).");
        }
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
            // Engine r47+ (pro-r47-04) on a Linux host: a software WebGPU adapter that wears the persona
            // under a Windows or macOS claim. Gated on the engine (an older one would show a raw SwiftShader
            // adapter), and never a switch twice: Playwright's own --enable-unsafe-swiftshader counts when kept.
            baseList.AddRange(LaunchOpts.SoftwareWebgpuArgs(
                extra.Exe,
                fpArgs.Find(a => a.StartsWith(platFlag, System.StringComparison.Ordinal))?[platFlag.Length..],
                null,
                baseList.Concat(userArgs).Concat(
                    extra.ViaPlaywright && LaunchOpts.PlaywrightKeepsUnsafeSwiftshader(extra.IgnoreDefaultArgs)
                        ? new[] { "--enable-unsafe-swiftshader" } : System.Array.Empty<string>())));
            // macOS: the build is unsigned, so every launch would prompt for the "Chromium Safe Storage"
            // keychain item. See MacosKeychainArgs.
            baseList.AddRange(LaunchOpts.MacosKeychainArgs(null, userArgs));
        }
        var merged = LaunchOpts.MergeFeatureFlags(baseList.Concat(userArgs));
        // Last: drop 152 r22+ switches this engine does not implement (with a warning), wherever they came from.
        return extra is null ? merged : LaunchOpts.GateEngineSwitches(extra.Exe, merged, extra.Quiet).Args;
    }


    private static Proxy? ToPwProxy(ProxyOptions? p)
        => p?.Server is { Length: > 0 } server
            ? new Proxy { Server = server, Username = p.Username, Password = p.Password, Bypass = p.Bypass }
            : null;

    /// Refuse a binary this OS has no build for -- but never one the caller named: an existing ExecutablePath /
    /// CLEARCOTE_BINARY is theirs to run on any OS, whatever its name (a .exe under Wine, say; download.ts
    /// ensureRunnableHere). macOS has a native PRO build (Apple Silicon), which the PRO download resolves itself.
    internal static void EnsureRunnableHere(string exe)
    {
        if (!string.IsNullOrEmpty(exe) && File.Exists(exe)) return;
        string? pro;
        try { pro = Release.ProPlatformKey(); }
        catch (PlatformNotSupportedException) { pro = null; }   // an Intel Mac: no build for this CPU
        if (Release.PlatformRelease() is null && pro is null)
            throw new PlatformNotSupportedException(
                $"Clearcote {Release.Current.Version} ships Windows x64 and Linux x64 binaries — there is no build for '{Native.OsTag}'.\n" +
                "Run on Windows or Linux, or set ExecutablePath to a compatible binary.\n" +
                $"(The binary downloaded and verified fine; it is cached at: {exe})");
        throw new FileNotFoundException($"Clearcote binary not found at '{exe}'. Set ExecutablePath / CLEARCOTE_BINARY, or let the SDK auto-download it.", exe);
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
    /// The engine's process id (null once it can no longer be read).
    public int? Pid { get { try { return _proc.Id; } catch { return null; } } }

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

    /// How long CloseAsync waits for the engine to exit on its own after the graceful stop, before it kills it.
    internal static TimeSpan CloseGrace = TimeSpan.FromSeconds(10);

    /// Stop the engine and wait for it to exit, release the lease (best-effort), then remove the temp
    /// profile ServeAsync made and the engine's singleton-socket directory. Closes once.
    /// <remarks>
    /// The stop is graceful first, as in the Node SDK's <c>close()</c>: SIGTERM on Linux and macOS, which Chrome
    /// answers by shutting down cleanly (it writes its profile back and unlinks its singleton socket); after 10 s
    /// still running, the engine and its child processes are killed. On Windows there is no signal: Node's
    /// <c>kill("SIGTERM")</c> is TerminateProcess there, so this kills the engine's process tree at once, as before.
    /// </remarks>
    public Task CloseAsync()
    {
        lock (_gate) return _closing ??= CloseCoreAsync();
    }

    [System.Runtime.InteropServices.DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int SysKill(int pid, int sig);

    private async Task<bool> WaitForExitAsync(TimeSpan timeout)
    {
        try
        {
            using var cts = new CancellationTokenSource(timeout);
            await _proc.WaitForExitAsync(cts.Token).ConfigureAwait(false);
            return true;
        }
        catch { return IsExited(); }
    }

    private bool IsExited() { try { return _proc.HasExited; } catch { return true; } }

    private async Task CloseCoreAsync()
    {
        var socketDir = _socketDir ?? ThrowawayProfile.BrowserProcessFiles(_userDataDir).SocketDir;
        var exited = IsExited();
        if (!exited && !Native.IsWindows)
        {
            try { SysKill(_proc.Id, 15); } catch { }   // SIGTERM: Chrome shuts down cleanly
            exited = await WaitForExitAsync(CloseGrace).ConfigureAwait(false);
        }
        if (!exited)
        {
            try { _proc.Kill(entireProcessTree: true); } catch { }
            // still running at the deadline: remove what can be removed all the same
            await WaitForExitAsync(Native.IsWindows ? TimeSpan.FromSeconds(10) : TimeSpan.FromSeconds(5)).ConfigureAwait(false);
        }
        // Release the concurrency slot (best-effort) + remove the run-token file.
        if (_lease is not null) { try { await _lease.StopAsync().ConfigureAwait(false); } catch { } }
        _launchToken?.Release();
        if (socketDir is not null) { try { Directory.Delete(socketDir, recursive: true); } catch { } }
        if (_ownUdd) ThrowawayProfile.DeleteDirectory(_userDataDir);
        AppDomain.CurrentDomain.ProcessExit -= _onProcessExit;  // one per ServeAsync would otherwise pile up
    }
}
