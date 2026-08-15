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
    /// Override the default `--enable-automation` strip (Playwright ignoreDefaultArgs).
    public IReadOnlyList<string>? IgnoreDefaultArgs { get; set; }
    /// Emulated viewport for the context. Leave unset to take the SDK's default: NoViewport when
    /// headed or when a persona owns the screen, otherwise a screen-fitted viewport (see
    /// <see cref="Geometry"/>). Setting either this or <see cref="ScreenSize"/> turns the default off
    /// entirely and passes both through as given.
    public ViewportSize? ViewportSize { get; set; }
    /// Emulated screen size (CDP screenWidth/screenHeight) for the context. See
    /// <see cref="ViewportSize"/> for how it interacts with the SDK default.
    public ScreenSize? ScreenSize { get; set; }
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
}
