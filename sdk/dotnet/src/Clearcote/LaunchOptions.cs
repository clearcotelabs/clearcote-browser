using Microsoft.Playwright;

namespace Clearcote;

/// Options for <see cref="Clearcote.LaunchAsync"/> / <see cref="Clearcote.LaunchPersistentContextAsync"/>.
/// Combines the persona (<see cref="FingerprintOptions"/>), the license options, binary-resolution
/// options, and the Playwright pass-through knobs the SDK understands.
public class LaunchOptions : FingerprintOptions
{
    // ── license (opt-in PRO) ─────────────────────────────────────────────────
    /// License key ("cc_lic_..."). Resolved from this &gt; CLEARCOTE_LICENSE_KEY env &gt; ~/.clearcote/license.key.
    public string? LicenseKey { get; set; }
    /// License backend base URL (default: CLEARCOTE_LICENSE_API env or clearcotelabs.com).
    public string? LicenseApiBase { get; set; }
    /// Send the licence calls (lease checkout / heartbeat / check-in) through <see cref="Proxy"/>
    /// instead of directly from this machine. Off by default. Null = CLEARCOTE_LICENSE_THROUGH_PROXY.
    /// No effect without a proxy.
    public bool? LicenseThroughProxy { get; set; }
    /// PRO release channel. <c>Preview</c> selects the newest build for
    /// this platform (a newer preview when one exists, otherwise stable). An exact <see cref="Version"/>
    /// pin overrides it. Null = CLEARCOTE_RELEASE_CHANNEL ("stable" | "preview"; anything else throws),
    /// else stable.
    public ReleaseChannel? ReleaseChannel { get; set; }

    // ── geoip ────────────────────────────────────────────────────────────────
    /// Fill unset Timezone / AcceptLanguage / Location / WebrtcIp from the exit IP's region, looked up
    /// THROUGH <see cref="Proxy"/> (HTTP or SOCKS5). Bounded by CLEARCOTE_GEOIP_TIMEOUT_SECONDS
    /// (default 20). Fails closed with <see cref="GeoipException"/> before any browser starts, unless
    /// both Timezone and AcceptLanguage were set explicitly (then it warns and launches).
    public bool Geoip { get; set; }

    // ── engine behaviour switches (engine 152 r22+; dropped with a warning on older engines) ──
    /// Allow third-party cookies, as stock Chrome does. The de-Googled base blocks them by default,
    /// which breaks embedded flows (reCAPTCHA, SSO sign-in, payment challenges). Default off.
    public bool? AllowThirdPartyCookies { get; set; }
    /// Hide proxy use from origins and pages: send <c>Connection</c> instead of
    /// <c>Proxy-Connection</c> on plain-HTTP requests through an HTTP proxy, and report proxied
    /// connection timing like a reused connection. Requires <see cref="Proxy"/>.
    public bool? TransparentProxy { get; set; }

    // ── binary resolution ────────────────────────────────────────────────────
    /// Explicit chrome binary path (wins over everything, incl. CLEARCOTE_BINARY and the auto-download).
    public string? ExecutablePath { get; set; }
    /// Override the download cache dir.
    public string? CacheDir { get; set; }
    /// Resolve + download the LATEST GitHub release instead of the pinned one (free build only).
    public bool? AutoUpdate { get; set; }
    /// Select a specific browser build from the catalog: a bare major ("150"), an exact version
    /// ("150.0.7871.115"), or "latest". Validated before download; PRO-tier versions need a license.
    /// Pin a specific PRO rebuild with "150.0.7871.114-r7" (or bare "r7"), which also needs a license.
    /// Also set via CLEARCOTE_BROWSER_VERSION.
    public string? Version { get; set; }
    /// Suppress SDK progress/warning logging.
    public bool Quiet { get; set; }

    // ── Playwright pass-through / SDK arg knobs ──────────────────────────────
    /// Headless mode. Null = Playwright default (headless). Set false for a headed window.
    public bool? Headless { get; set; }
    /// Proxy (credentialed SOCKS5 is auto-rerouted to --proxy-server; QUIC is disabled when a proxy is set).
    public ProxyOptions? Proxy { get; set; }
    /// Extra Chromium args appended last (after the SDK's persona + default args).
    public IReadOnlyList<string>? Args { get; set; }
    /// Unpacked extension dirs (--load-extension + --disable-extensions-except).
    public IReadOnlyList<string>? Extensions { get; set; }
    /// Set true to DISABLE the Privacy-Sandbox features (Topics/FLEDGE/Shared Storage/Fenced Frames).
    /// Default false since 0.23.0: real Google Chrome ships all of them, and the default persona
    /// claims to be Google Chrome, so disabling them was a coherence tell rather than a privacy win.
    /// Set true only when the persona genuinely is de-Googled Chromium.
    public bool? DisablePrivacySandbox { get; set; }
    /// Environment variables for the browser process (the SDK adds CLEARCOTE_RUN_TOKEN when licensed).
    public IDictionary<string, string>? Env { get; set; }

    /// Report ANGLE's translated shader in this dialect for
    /// <c>WEBGL_debug_shaders.getTranslatedShaderSource()</c>. Only "hlsl" is understood.
    ///
    /// <para>Makes a Windows persona on a Linux host report HLSL, matching the Direct3D renderer
    /// string it already advertises — without it the Vulkan backend answers with SPIR-V and the two
    /// values contradict each other. Rendering is unaffected.</para>
    ///
    /// <para>OFF by default: the re-translation is a different code path from the one that
    /// rendered, so a shader the real backend accepts but the HLSL translator rejects falls back to
    /// the honest dialect. Turn it on if you hit this specific check. Needs a PRO engine 151 r15+.</para>
    public string? ShaderDialect { get; set; }
    /// Relay WebRTC's UDP through the SOCKS5 proxy using UDP ASSOCIATE, instead of letting it
    /// egress on the host's own path.
    ///
    /// <para>By default clearcote denies non-proxied UDP, which keeps UDP from leaking around the
    /// proxy but also means peer connections that need UDP never establish — stock Chromium cannot
    /// proxy a datagram. Turn this on to get working UDP that still leaves from the proxy's
    /// address.</para>
    ///
    /// <para>Applies only to a socks5:// proxy; ignored otherwise. Needs a PRO engine 151 r17+, and
    /// a proxy that actually permits the ASSOCIATE command.</para>
    public bool Socks5Udp { get; set; }
    /// Browser channel (e.g. "chrome") passed to Playwright, if any.
    public string? Channel { get; set; }
    /// Slow down operations by N ms (Playwright slowMo).
    public float? SlowMo { get; set; }
    /// Playwright timeout in ms (0 = no limit). For a local launch, how long to wait for the browser
    /// to start (Playwright's default 30 000); for a cloud launch, how long to wait for the connect
    /// (default 120 000 there: the hosted browser starts as you connect).
    public float? Timeout { get; set; }
    /// Override the default strip of Playwright's <c>--enable-automation</c>,
    /// <c>--enable-unsafe-swiftshader</c> and <c>--hide-scrollbars</c> (Playwright ignoreDefaultArgs). See <see cref="LaunchOpts.DefaultIgnoredArgs"/>.
    public IReadOnlyList<string>? IgnoreDefaultArgs { get; set; }
    /// Emulated viewport for the context. Leave unset to take the SDK's default: NoViewport when
    /// headed or when a persona owns the screen, otherwise a screen-fitted viewport (see
    /// <see cref="Geometry"/>). Setting either this or <see cref="ScreenSize"/> turns the default off
    /// entirely and passes both through as given.
    public ViewportSize? ViewportSize { get; set; }
    /// Emulated screen size (CDP screenWidth/screenHeight) for the context. See
    /// <see cref="ViewportSize"/> for how it interacts with the SDK default.
    public ScreenSize? ScreenSize { get; set; }

    // ── local or cloud ───────────────────────────────────────────────────────
    // A cloud launch takes the persona options that also exist for a hosted browser (Fingerprint,
    // Platform, Brand, Timezone, AcceptLanguage, LightStealth, Geoip, Headless, Proxy, Version) plus the
    // ones below, and refuses every option only a browser on this machine can take, naming it. A local
    // launch ignores everything in this section, so code that always sets ApiKey switches with nothing
    // but Cloud (or CLEARCOTE_CLOUD).

    /// True runs the browser on Clearcote's servers and returns the same Playwright types. Null follows
    /// CLEARCOTE_CLOUD (1, true or yes means cloud); an explicit value always wins over it.
    public bool? Cloud { get; set; }
    /// A configured <see cref="global::Clearcote.Cloud"/> client to launch with (implies cloud unless
    /// <see cref="Cloud"/> is false). Otherwise one is made from <see cref="ApiKey"/> / <see cref="ApiUrl"/>.
    public Cloud? CloudClient { get; set; }
    /// Cloud API key; defaults to CLEARCOTE_API_KEY.
    public string? ApiKey { get; set; }
    /// Cloud API base URL; defaults to CLEARCOTE_API_URL, then https://www.clearcotelabs.com.
    public string? ApiUrl { get; set; }
    /// Cloud: a stable device label; the same identity gets the same device on every session.
    public string? Identity { get; set; }
    /// Cloud: the exit country of the included residential connection (ISO code, e.g. "us").
    public string? Country { get; set; }
    /// Cloud: the exit region within <see cref="Country"/>.
    public string? State { get; set; }
    /// Cloud: the exit city within <see cref="Country"/>.
    public string? City { get; set; }
    /// Cloud: keep the same exit IP across sessions that share this label.
    public string? ProxySession { get; set; }
    /// Cloud: the longest the session may run, in seconds.
    public int? TimeoutSec { get; set; }
    /// Cloud: end the session after this many seconds without a client.
    public int? IdleTimeoutSec { get; set; }
    /// Cloud: stop the session once it has used this much traffic.
    public double? MaxGb { get; set; }
    /// Cloud: a named cookie store on the server. A name loads it; <c>Persist = true</c> also saves it
    /// back when the session ends (LaunchPersistentContextAsync does that by default).
    public CloudProfile? Profile { get; set; }
    /// Cloud: the page the session opens on.
    public string? Url { get; set; }
    /// Cloud: block ads and trackers on the hosted browser.
    public bool? Adblock { get; set; }
    /// Cloud: drag slide-to-verify challenges automatically (the server's default is on); false turns it off.
    public bool? SolveSliders { get; set; }
    /// Cloud: click "verify you are human" checkboxes automatically (the server's default is on); false turns it off.
    public bool? SolveCheckboxes { get; set; }
    /// Cloud: keep the session running after this client disconnects; stop it with
    /// <c>cloud.Browsers.StopAsync(id)</c>.
    public bool? KeepAlive { get; set; }
    /// Cloud: record the session (see <c>cloud.Browsers.DownloadRecordingAsync</c>).
    public bool? Record { get; set; }
    /// Cloud: a free-text note shown with the session in the dashboard and in Browsers.ListAsync.
    public string? Note { get; set; }
    /// Cloud: run on this worker.
    public string? Worker { get; set; }
}

/// Options for <see cref="Clearcote.ServeAsync"/> — a standing, stealthy CDP endpoint.
public class ServeOptions : LaunchOptions
{
    /// CDP port. Default: a free ephemeral port.
    public int? Port { get; set; }
    /// Bind address. Default "127.0.0.1".
    public string? Host { get; set; }
    /// --remote-allow-origins value. Default: loopback origins only.
    public string? AllowOrigins { get; set; }
    /// Profile dir. Default: a fresh temp dir, removed on close.
    public string? UserDataDir { get; set; }
    /// How long to wait for the CDP endpoint to come up, in ms. Default 30000.
    public int ReadyTimeoutMs { get; set; } = 30000;
    /// Headless: the outer window size in CSS px (100-10000), clamped to the display's work area so
    /// the window can never be larger than its screen. Default: the whole work area (a maximized
    /// window). Ignored when headed, and when Args carries a window or display switch (the caller then
    /// owns geometry).
    public ViewportSize? WindowSize { get; set; }
}
