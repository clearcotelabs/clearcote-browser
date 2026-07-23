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
    /// Set false to KEEP the Privacy-Sandbox features (by default the SDK disables them, like a stock profile).
    public bool? DisablePrivacySandbox { get; set; }
    /// Environment variables for the browser process (the SDK adds CLEARCOTE_RUN_TOKEN when licensed).
    public IDictionary<string, string>? Env { get; set; }
    /// Browser channel (e.g. "chrome") passed to Playwright, if any.
    public string? Channel { get; set; }
    /// Slow down operations by N ms (Playwright slowMo).
    public float? SlowMo { get; set; }
    /// Override the default `--enable-automation` strip (Playwright ignoreDefaultArgs).
    public IReadOnlyList<string>? IgnoreDefaultArgs { get; set; }
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
