using System.Text.RegularExpressions;
namespace Clearcote;

/// Proxy descriptor (mirrors the Playwright proxy shape the Node SDK reads).
public sealed class ProxyOptions
{
    public string? Server { get; set; }
    public string? Username { get; set; }
    public string? Password { get; set; }
    public string? Bypass { get; set; }
}

/// Default/always-on Chromium arg helpers (ports launchopts.ts).
public static class LaunchOpts
{
    /// Privacy-Sandbox features Clearcote disables by default (a stock, un-enrolled Chrome profile).
    /// WebUSB is deliberately excluded: it is a device API, not a Privacy Sandbox feature, and it
    /// ships alongside Web Serial/WebHID/Web Bluetooth under identical gating. Disabling only
    /// WebUSB produced a device-API family split no real Chromium exhibits.
    public static readonly string[] PrivacySandboxFeatures =
    {
        "BrowsingTopics", "BrowsingTopicsDocumentAPI", "Fledge", "InterestGroupStorage",
        "PrivateAggregationApi", "SharedStorageAPI", "FencedFrames",
    };

    public static List<string> PrivacySandboxArgs()
        => new() { $"--disable-features={string.Join(",", PrivacySandboxFeatures)}" };

    /// Playwright puts its own <c>--disable-features</c> list on every launch, BEFORE the caller's
    /// args, and Chromium keeps only the last <c>--disable-features</c> on the line — so the SDK
    /// re-emits that list itself, merged into its own single switch, minus
    /// <see cref="PageVisiblePlaywrightFeatures"/> (see <see cref="PlaywrightFeatureOverrideArgs"/>).
    /// The list differs between Playwright releases, so the driver the app ships
    /// (<c>.playwright/package</c>) is read at launch; this copy — Microsoft.Playwright 1.49, the
    /// version this package references — is only the fallback.
    public static readonly string[] PlaywrightDisabledFeatures =
    {
        "ImprovedCookieControls", "LazyFrameLoading", "GlobalMediaControls", "DestroyProfileOnBrowserClose",
        "MediaRouter", "DialMediaRouteProvider", "AcceptCHFrame", "AutoExpandDetailsElement",
        "CertificateTransparencyComponentUpdater", "AvoidUnnecessaryBeforeUnloadCheckSync", "Translate",
        "HttpsUpgrades", "PaintHolding", "ThirdPartyStoragePartitioning", "LensOverlay", "PlzDedicatedWorker",
    };

    /// Entries of Playwright's list that a web page or server can observe, kept ENABLED as in genuine
    /// Chrome 154 (all four default on). The rest stay off: they keep Playwright's automation stable
    /// or only touch browser UI and background services.
    /// ThirdPartyStoragePartitioning: every Chrome since 115 partitions third-party storage, whatever
    /// the user's cookie settings; with it off a cross-site iframe reads the storage its site wrote as
    /// a top-level page, or — with third-party cookies blocked, the engine default — gets a
    /// SecurityError from localStorage. Measured 2026-10-06 (r30, genuine Chrome 154.0.8037.98 on the
    /// same host): genuine gives the iframe an empty partition with cookies allowed AND blocked, and
    /// genuine launched with Playwright's defaults reproduces both clearcote results exactly.
    /// AcceptCHFrame: ACCEPT_CH client hints reach the server on the first request. HttpsUpgrades: an
    /// http:// navigation is tried over https first (a route handler for http:// can now see the
    /// https:// attempt; Args "--disable-features=HttpsUpgrades" turns it back off). LazyFrameLoading:
    /// loading="lazy" iframes load lazily — it no longer exists in Chromium 154, so re-enabling it
    /// is a no-op there, as are 1.49's PlzDedicatedWorker, AutoExpandDetailsElement and
    /// ImprovedCookieControls.
    public static readonly IReadOnlySet<string> PageVisiblePlaywrightFeatures = new HashSet<string>(StringComparer.Ordinal)
        { "ThirdPartyStoragePartitioning", "AcceptCHFrame", "HttpsUpgrades", "LazyFrameLoading" };

    private static readonly Regex PwAssign = new(@"\bdisabledFeatures\s*=", RegexOptions.Compiled);
    private static readonly Regex PwArray = new(@"^\s*(?:\([^)]*\)\s*=>\s*)?\[([\s\S]*?)\]", RegexOptions.Compiled);
    // A parse must contain one of these to be trusted; a reshaped source falls back to the copy.
    private static readonly string[] PwKnown = { "MediaRouter", "Translate", "ThirdPartyStoragePartitioning", "DestroyProfileOnBrowserClose" };
    private static readonly Regex PwLiteral = new(@"[""'`]--disable-features=([A-Za-z0-9_,]+)[""'`]", RegexOptions.Compiled);
    private static readonly Regex PwTernary = new(@"\w+\s*\?\s*[""'][^""']*[""']\s*:\s*[""'][^""']*[""']", RegexOptions.Compiled);
    private static readonly Regex PwComment = new(@"//[^\n]*", RegexOptions.Compiled);
    private static readonly Regex PwName = new(@"[""']([A-Za-z0-9_]+)[""']", RegexOptions.Compiled);

    /// The <c>--disable-features</c> list in a Playwright chromiumSwitches source (array form, bundled
    /// or not, or the older literal-string form); null when it is not there.
    internal static string[]? ParsePlaywrightDisabledFeatures(string? source)
    {
        if (string.IsNullOrEmpty(source)) return null;
        var names = Array.Empty<string>();
        var assign = PwAssign.Match(source);
        if (assign.Success)
        {
            // Strip comments BEFORE looking for the closing bracket: a "]" in a comment must not end it.
            var start = assign.Index + assign.Length;
            var tail = PwComment.Replace(source.Substring(start, Math.Min(6000, source.Length - start)), "");
            var arr = PwArray.Match(tail);
            if (arr.Success)
                names = PwName.Matches(PwTernary.Replace(arr.Groups[1].Value, "")).Select(m => m.Groups[1].Value).ToArray();
        }
        if (names.Length == 0)
        {
            var lit = PwLiteral.Match(source);
            if (lit.Success) names = lit.Groups[1].Value.Split(',', StringSplitOptions.RemoveEmptyEntries);
        }
        return names.Any(n => PwKnown.Contains(n)) ? names : null;
    }

    private static readonly Lazy<(string[]? Features, bool ScreenshotSurface)> InstalledPw = new(() =>
    {
        try
        {
            foreach (var root in new[] { AppContext.BaseDirectory, Path.GetDirectoryName(typeof(Microsoft.Playwright.IPlaywright).Assembly.Location) })
            {
                if (string.IsNullOrEmpty(root)) continue;
                var lib = Path.Combine(root, ".playwright", "package", "lib");
                foreach (var rel in new[] { Path.Combine("server", "chromium", "chromiumSwitches.js"), "coreBundle.js" })
                {
                    var path = Path.Combine(lib, rel);
                    if (!File.Exists(path)) continue;
                    var src = File.ReadAllText(path);
                    return (ParsePlaywrightDisabledFeatures(src), src.Contains("CDPScreenshotNewSurface", StringComparison.Ordinal));
                }
            }
        }
        catch { /* unreadable: the 1.49 copy is used */ }
        return (null, false);
    });

    /// The list the app's Playwright driver puts on a Chromium launch, or null when it cannot be read.
    internal static string[]? InstalledPlaywrightDisabledFeatures() => InstalledPw.Value.Features;

    /// Switches that replace Playwright's own <c>--disable-features</c> without
    /// <see cref="PageVisiblePlaywrightFeatures"/>. Only for launches Playwright starts (Launch /
    /// LaunchPersistentContext); Serve starts Chromium itself and never carries Playwright's list.
    /// Nothing is re-emitted for a switch the caller's IgnoreDefaultArgs drops (Playwright drops only
    /// exact matches, so any other value leaves its list in place and it still needs replacing). Also
    /// re-emits Playwright's <c>--enable-features=CDPScreenshotNewSurface</c> when its driver passes
    /// it (unless PLAYWRIGHT_LEGACY_SCREENSHOT is set), since the SDK's own --enable-features would
    /// otherwise replace that too.
    public static List<string> PlaywrightFeatureOverrideArgs(IReadOnlyList<string>? ignoreDefaultArgs = null,
        IReadOnlyList<string>? playwrightFeatures = null, bool? screenshotSurface = null)
    {
        var features = (playwrightFeatures ?? InstalledPlaywrightDisabledFeatures() ?? PlaywrightDisabledFeatures).ToArray();
        var ignored = ignoreDefaultArgs ?? Array.Empty<string>();
        var outList = new List<string>();
        if (!ignored.Contains($"--disable-features={string.Join(",", features)}"))
        {
            var keep = features.Where(f => !PageVisiblePlaywrightFeatures.Contains(f)).ToArray();
            if (keep.Length > 0) outList.Add($"--disable-features={string.Join(",", keep)}");
        }
        const string screenshotSwitch = "--enable-features=CDPScreenshotNewSurface";
        var surface = screenshotSurface ?? (InstalledPw.Value.ScreenshotSurface
            && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("PLAYWRIGHT_LEGACY_SCREENSHOT")));
        if (surface && !ignored.Contains(screenshotSwitch)) outList.Add(screenshotSwitch);
        return outList;
    }

    /// Chromium keeps only the LAST --enable-features / --disable-features; collapse all occurrences
    /// into one of each (order-preserving for the rest, de-duped values).
    public static List<string> MergeFeatureFlags(IEnumerable<string> args)
    {
        var enabled = new List<string>();
        var disabled = new List<string>();
        var rest = new List<string>();
        foreach (var a in args)
        {
            if (a.StartsWith("--enable-features="))
                enabled.AddRange(a["--enable-features=".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries));
            else if (a.StartsWith("--disable-features="))
                disabled.AddRange(a["--disable-features=".Length..].Split(',', StringSplitOptions.RemoveEmptyEntries));
            else rest.Add(a);
        }
        if (enabled.Count > 0) rest.Add($"--enable-features={string.Join(",", enabled.Distinct())}");
        if (disabled.Count > 0) rest.Add($"--disable-features={string.Join(",", disabled.Distinct())}");
        return rest;
    }

    /// Disable QUIC/HTTP-3 when a proxy is set (a SOCKS5/HTTP proxy carries only TCP; no UDP around it).
    public static List<string> QuicArgs(ProxyOptions? proxy)
        => proxy is not null && !string.IsNullOrEmpty(proxy.Server) ? new() { "--disable-quic" } : new();

    /// Carry WebRTC's UDP through the SOCKS5 proxy with UDP ASSOCIATE (RFC 1928 section 7) instead
    /// of letting it egress on the host's own path.
    ///
    /// <para>This is the transport <see cref="WebrtcDefaultDenyArgs"/> asks for. That default sets
    /// disable_non_proxied_udp, which on stock Chromium means "no UDP at all" because stock
    /// Chromium cannot proxy a datagram — so peer connections that genuinely need UDP simply fail.
    /// With this option the engine opens a UDP association through the proxy and relays every
    /// datagram over it, so UDP works AND still leaves from the proxy's address. The two compose:
    /// measured against the proxy's own log, the association is established with the deny policy in
    /// force, so enabling this does not require weakening the policy.</para>
    ///
    /// <para>Emitted only for a socks5:// proxy. UDP ASSOCIATE is a SOCKS5 command — SOCKS4 has no
    /// equivalent and an HTTP proxy carries only TCP — so with any other scheme the switch would be
    /// accepted and silently do nothing, which is worse than not sending it.</para>
    ///
    /// <para>Needs a PRO engine 151 r17+; older binaries ignore the switch.</para>
    public static List<string> Socks5UdpArgs(bool socks5Udp, ProxyOptions? proxy)
    {
        if (!socks5Udp) return new();
        var server = proxy?.Server?.Trim() ?? string.Empty;
        return server.StartsWith("socks5", StringComparison.OrdinalIgnoreCase)
            ? new() { "--socks5-udp" }
            : new();
    }

    /// Default WebRTC to deny non-proxied UDP, so no UDP can egress around the proxy.
    ///
    /// This used to be skipped whenever a webrtcIp was set, on the theory that the engine's srflx
    /// fabrication already covered WebRTC. It does not — fabrication rewrites what the browser
    /// reports (beating a page that reads the candidate), while this policy stops UDP leaving the
    /// machine (beating a server that watches where packets arrive from). A page using
    /// iceTransportPolicy: "relay" forces TURN, TURN prefers UDP, and an HTTP/SOCKS proxy carries
    /// only TCP — so the UDP left on the host's own path and the TURN server read the real public
    /// address off the packet. Only an explicit caller policy suppresses this now.
    ///
    /// Trade-off, not a free win: peer connections that genuinely need UDP will not establish.
    /// Callers who need working WebRTC through a proxy want a transport that carries UDP (SOCKS5
    /// with UDP ASSOCIATE, or a full tunnel) and can set their own policy to opt out.
    /// webrtcIp is accepted and ignored, for call-site compatibility.
    /// <summary>Switches to expose <c>navigator.bluetooth</c>, matching the platform the page is
    /// TOLD it is (empty off Linux, and empty under a Linux claim).</summary>
    /// <remarks>
    /// Web Bluetooth is compiled into the engine but its *default* follows the build platform:
    /// Chromium's runtime_enabled_features.json5 gives WebBluetooth status "stable" on Win/Mac/Android/
    /// ChromeOS and lets Linux fall through to "default": "experimental", and content_features.cc
    /// declares kWebBluetooth FEATURE_DISABLED_BY_DEFAULT. So the same persona is a tell in opposite
    /// directions depending on which build is running: a Linux build serving a Windows persona
    /// reports navigator.usb, navigator.serial and navigator.hid but NOT navigator.bluetooth - a
    /// combination no real Windows Chrome produces - while a Windows build serving a Linux claim
    /// reports navigator.bluetooth, which genuine Chrome on Linux does not have.
    ///
    /// Hence the switch is derived from the CLAIM alone and the host is never consulted: whichever
    /// way the build's default falls, one of the two switches lands the page on the right answer,
    /// and the one that agrees with the default is a no-op. Reading the host OS here was the
    /// original bug, and reading it to decide whether to bother was the half-fix: skipping the
    /// disable off Linux left a Windows host serving a Linux persona exposing the API (measured on
    /// the r30 Windows build).
    ///
    /// Verified against Chromium 150's bluetooth.idl: getDevices() is gated on WebBluetoothGetDevices
    /// and requestLEScan()/onadvertisementreceived on WebBluetoothScanning, both "experimental", so
    /// real stable Chrome exposes exactly {constructor, getAvailability, requestDevice} - which is what
    /// the enable switch produces. getAvailability() resolves false and requestDevice() rejects
    /// NotFoundError on a machine with no adapter, matching a real desktop without Bluetooth hardware.
    /// </remarks>
    /// <summary>Platforms whose stable Chrome ships Web Bluetooth.</summary>
    public static readonly string[] WebBluetoothPlatforms =
        { "windows", "macos", "mac", "android", "chromeos" };

    public static List<string> WebBluetoothArgs(string? claimedPlatform = null)
    {
        // Gate on the CLAIMED platform, never the host.
        var claimed = (claimedPlatform ?? Fingerprint.HostPlatform).Trim().ToLowerInvariant();
        if (System.Array.IndexOf(WebBluetoothPlatforms, claimed) >= 0)
            return new List<string> { "--enable-features=WebBluetooth" };
        // A Linux claim has no navigator.bluetooth on genuine Chrome, whatever this build defaults to.
        return new List<string> { "--disable-features=WebBluetooth" };
    }

    public static List<string> WebrtcDefaultDenyArgs(IEnumerable<string> args, string? webrtcIp = null)
    {
        if (args.Any(a => a.StartsWith("--webrtc-ip-handling-policy")
                          || a.StartsWith("--force-webrtc-ip-handling-policy")))
            return new();
        return new() { "--webrtc-ip-handling-policy=disable_non_proxied_udp" };
    }

    /// --load-extension + --disable-extensions-except (both needed), only when paths are given.
    public static List<string> ExtensionArgs(IReadOnlyList<string>? paths)
    {
        if (paths is null || paths.Count == 0) return new();
        var joined = string.Join(",", paths);
        return new() { $"--load-extension={joined}", $"--disable-extensions-except={joined}" };
    }

    /// The credentials a proxy descriptor carries, and its server with any userinfo removed.
    /// socks5://user:pass@host:1080 is the shape most proxy providers hand out, so credentials
    /// written into the URL count exactly like <see cref="ProxyOptions.Username"/> /
    /// <see cref="ProxyOptions.Password"/> (which win when both are given, as in
    /// <see cref="ProxySpec.From(ProxyOptions?)"/>). Userinfo is percent-decoded, like a browser
    /// reads it, and split at the LAST '@' of the authority, like a URL parser, so an unescaped '@'
    /// in a password survives.
    public static (string Server, string Username, string Password) ProxyCredentials(ProxyOptions proxy)
    {
        var raw = proxy.Server?.Trim() ?? string.Empty;
        string server = raw, urlUser = string.Empty, urlPass = string.Empty;
        var m = Regex.Match(raw, "^([a-zA-Z][a-zA-Z0-9+.-]*://)([^/?#]*)(.*)$", RegexOptions.Singleline);
        var at = m.Success ? m.Groups[2].Value.LastIndexOf('@') : -1;
        if (at >= 0)
        {
            var info = m.Groups[2].Value[..at];
            var colon = info.IndexOf(':');
            urlUser = Uri.UnescapeDataString(colon < 0 ? info : info[..colon]);
            urlPass = colon < 0 ? string.Empty : Uri.UnescapeDataString(info[(colon + 1)..]);
            server = m.Groups[1].Value + m.Groups[2].Value[(at + 1)..] + m.Groups[3].Value;
        }
        return (server,
            string.IsNullOrEmpty(proxy.Username) ? urlUser : proxy.Username,
            string.IsNullOrEmpty(proxy.Password) ? urlPass : proxy.Password);
    }

    /// Playwright rejects credentials in its SOCKS proxy descriptor, so a
    /// socks5://user:pass@host:port proxy is routed through --proxy-server instead and the
    /// credentials handed to the engine via --socks5-credentials. Clearcote implements RFC 1929
    /// username/password authentication, which stock Chromium does not, so no local relay is
    /// needed. Credentials count wherever they were written (see <see cref="ProxyCredentials"/>):
    /// reading only the fields used to hand a URL-credentialed SOCKS5 proxy to Playwright, which
    /// rebuilds the server as scheme://host:port, and the browser offered the proxy no
    /// authentication at all. Everything else passes through, minus any userinfo in the server.
    /// Returns the extra args + the (possibly nulled) proxy to hand to Playwright.
    public static (List<string> Args, ProxyOptions? Proxy) ResolveProxy(ProxyOptions? proxy)
    {
        if (proxy is null) return (new(), null);
        var (server, user, pass) = ProxyCredentials(proxy);
        var isSocks = server.StartsWith("socks", StringComparison.OrdinalIgnoreCase);
        var hasCreds = user.Length > 0 || pass.Length > 0;
        if (isSocks && hasCreds)
        {
            // `server` has no userinfo; the engine takes it via its own switch. Userinfo left in
            // --proxy-server is rejected by Chromium's proxy parser and the entry dropped: every
            // request then fails with ERR_NO_SUPPORTED_PROXIES (measured on r27).
            var args = new List<string> { $"--proxy-server={server}", $"--socks5-credentials={user}:{pass}" };
            if (!string.IsNullOrWhiteSpace(proxy.Bypass)) args.Add($"--proxy-bypass-list={proxy.Bypass.Trim()}");
            return (args, null);
        }
        // Left to Playwright. It drops userinfo from the server, so credentials written there are
        // handed over as the fields it reads.
        if (server != (proxy.Server?.Trim() ?? string.Empty))
            return (new(), new ProxyOptions
            {
                Server = server,
                Username = user.Length > 0 ? user : null,
                Password = pass.Length > 0 ? pass : null,
                Bypass = proxy.Bypass,
            });
        return (new(), proxy);
    }

    /// Keep the profile's cookie encryption key with the profile instead of the OS keystore, so the
    /// whole user data directory can be copied to another machine. `encryptionKey` derives the key
    /// from a caller-supplied secret and writes nothing to disk; `portableProfile` generates one and
    /// stores it in the profile.
    public static List<string> PortableArgs(bool portableProfile = false, string? encryptionKey = null)
    {
        if (!string.IsNullOrEmpty(encryptionKey)) return new() { $"--profile-encryption-key={encryptionKey}" };
        return portableProfile ? new() { "--portable-profile" } : new();
    }

    // ── GPU launch defaults ──────────────────────────────────────────────────

    /// Playwright launch defaults the SDK removes.
    ///
    /// <para><c>--enable-automation</c> keeps the engine's AutomationControlled feature off.
    /// <c>--enable-unsafe-swiftshader</c> is added by Playwright (1.49+) to every Chromium launch; it lets
    /// WebGL fall back to SwiftShader software rendering, which real Chrome no longer does for WebGL.
    /// Stripping it on its own is NOT safe: on a GPU-less Linux host a HEADED launch then has no WebGL
    /// at all, so it is only removed together with <see cref="GpuBlocklistArgs"/>.
    /// <c>--hide-scrollbars</c> is added by Playwright to every HEADLESS launch; with it the page measures
    /// 0 px scrollbars, while genuine Chrome shows 15 px on Windows -- headed OR plain <c>--headless=new</c>
    /// (measured on Chrome 154). Stripping it only restores Chrome's own default; it is a no-op headed.
    /// A caller's own IgnoreDefaultArgs always wins.</para>
    public static readonly IReadOnlyList<string> DefaultIgnoredArgs = new[] { "--enable-automation", "--enable-unsafe-swiftshader", "--hide-scrollbars" };

    /// <c>--ignore-gpu-blocklist</c> for headed launches and for every launch on Windows.
    ///
    /// <para>Headed on a host without a usable GPU (a VPS under Xvfb), Chromium's blocklist disables
    /// WebGL once the SwiftShader fallback flag is gone; this lets WebGL run anyway. On Windows the
    /// blocklist also refuses WebGPU on the Microsoft Basic Render Driver of GPU-less VMs. Headless
    /// Linux already renders WebGL through SwiftShader, so nothing is added there. Never duplicated
    /// when the caller already passes it.</para>
    public static List<string> GpuBlocklistArgs(bool headed, string? osTag = null, IEnumerable<string>? userArgs = null)
    {
        var isWindows = (osTag ?? Native.OsTag) == "windows";
        if (!headed && !isWindows) return new();
        if (userArgs?.Contains("--ignore-gpu-blocklist") == true) return new();
        return new() { "--ignore-gpu-blocklist" };
    }

    /// Whether <c>DISPLAY</c> names an X server this process can reach. ANGLE's OpenGL backend opens
    /// an X display even for a headless browser: measured on a GPU-less Linux host,
    /// <c>--use-angle=gl</c> without one leaves WebGL disabled ("Could not open the default X
    /// display"); with one (an Xvfb) it renders through Mesa. A local <c>:N</c> display is checked
    /// for its socket; a <c>host:N</c> display is trusted.
    public static bool XDisplayAvailable(string? display = null)
    {
        var disp = (display ?? System.Environment.GetEnvironmentVariable("DISPLAY") ?? "").Trim();
        if (disp.Length == 0) return false;
        if (disp.StartsWith(':'))
        {
            var num = disp[1..].Split('.')[0];
            return num.Length > 0 && num.All(char.IsAsciiDigit) && System.IO.File.Exists("/tmp/.X11-unix/X" + num);
        }
        return true;
    }

    /// Library directories searched for Mesa's EGL (Debian/Ubuntu multiarch, Fedora/RHEL lib64, Arch).
    public static readonly IReadOnlyList<string> EglLibDirs = new[]
    {
        "/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/lib/x86_64-linux-gnu", "/lib/aarch64-linux-gnu",
        "/usr/lib64", "/lib64", "/usr/lib", "/lib",
    };

    /// Whether Mesa can render WebGL over EGL with no X display (<c>--use-angle=gl-egl</c>). Measured
    /// on a GPU-less Linux host: with no display, <c>gl-egl</c> renders through
    /// Mesa's llvmpipe with the same limits as the X display path (16384 textures, 1024 vertex uniform
    /// vectors), and genuine Chrome on that path reports the same. It needs the system
    /// <c>libEGL.so.1</c>, Mesa's EGL vendor library and a software rasterizer driver. Without libEGL
    /// the browser has no WebGL at all, not even the SwiftShader fallback, so this answers true only
    /// when all three are present.
    public static bool MesaEglAvailable(IEnumerable<string>? libDirs = null)
    {
        var dirs = (libDirs ?? EglLibDirs).ToList();
        bool Has(string name) => dirs.Any(d => System.IO.File.Exists(System.IO.Path.Combine(d, name)));
        if (!Has("libEGL.so.1") || !Has("libEGL_mesa.so.0")) return false;
        if (Has("dri/swrast_dri.so") || Has("dri/kms_swrast_dri.so")) return true;
        // Mesa 24.2+ keeps its drivers in libgallium-<version>.so
        return dirs.Any(d =>
        {
            try { return System.IO.Directory.Exists(d) && System.IO.Directory.EnumerateFiles(d, "libgallium-*.so").Any(); }
            catch (System.Exception) { return false; }
        });
    }

    /// The ANGLE backend whose WebGL limits match the platform the page is TOLD it is, on a Linux
    /// host (mirrors Python's gpu_backend_args; measured 2026-10-06). A Windows
    /// claim names Direct3D11: ANGLE's OpenGL backend (Mesa, what a headed launch gets) clamps
    /// MAX_VERTEX_UNIFORM_VECTORS to 1024, which no real Direct3D11 Intel machine reports (they
    /// report 4096), so it gets <c>--use-angle=swiftshader-webgl</c> (4096; the HLSL shader dialect
    /// makes its translations match). A Linux claim names Mesa/OpenGL: headless Chromium renders
    /// WebGL through SwiftShader (8192 textures, 4096 vertex uniforms), so with an X display
    /// reachable it gets <c>--use-angle=gl</c> (Mesa: 16384 / 1024 / GLSL). Headed launches already
    /// get Mesa. Headless with no display gets <c>--use-angle=gl-egl</c> when the host has Mesa's EGL
    /// (<see cref="MesaEglAvailable"/>): Mesa again, with the same limits and the shader text of an
    /// OpenGL ES context; without it, headless stays on SwiftShader. A null claim (pass-through), a host
    /// other than Linux, or a caller's own <c>--use-angle=</c> / <c>--use-gl=</c> adds nothing.
    /// <paramref name="mesaEgl"/> null probes this host; tests pass true/false.
    public static List<string> GpuBackendArgs(string? claimedPlatform, bool headed, string? osTag = null,
                                              IEnumerable<string>? userArgs = null, string? display = null,
                                              bool? mesaEgl = null)
    {
        if (claimedPlatform is null || (osTag ?? Native.OsTag) != "linux") return new();
        var user = userArgs?.ToList() ?? new List<string>();
        if (user.Any(a => a.StartsWith("--use-angle=", StringComparison.Ordinal) || a.StartsWith("--use-gl=", StringComparison.Ordinal)))
            return new();
        var claim = claimedPlatform.Trim().ToLowerInvariant();
        if (claim == "windows") return new() { "--use-angle=swiftshader-webgl" };
        if (claim == "linux" && !headed)
        {
            string backend;
            if (XDisplayAvailable(display)) backend = "--use-angle=gl";
            else if (mesaEgl ?? MesaEglAvailable()) backend = "--use-angle=gl-egl";
            else return new();
            return user.Contains("--ignore-gpu-blocklist") ? new() { backend } : new() { backend, "--ignore-gpu-blocklist" };
        }
        return new();
    }

    // ── engine capability probe ─────────────────────────────────────────────

    private static readonly System.Collections.Concurrent.ConcurrentDictionary<string, bool> SwitchCache = new();

    /// Whether the engine binary implements a command-line switch, by searching it for the
    /// NUL-delimited literal <c>"\0name\0"</c> (a switch constant in the string table). The delimiters
    /// keep it from matching a longer literal. On Windows the switches live in chrome.dll next to the
    /// launcher; elsewhere in the executable. Cached per (path, size, mtime). Any failure answers false.
    public static bool EngineSupportsSwitch(string? exe, string name)
    {
        try
        {
            if (string.IsNullOrEmpty(exe)) return false;
            var file = exe;
            if (Native.IsWindows)
            {
                var dll = Path.Combine(Path.GetDirectoryName(exe) ?? "", "chrome.dll");
                if (File.Exists(dll)) file = dll;
            }
            var fi = new FileInfo(file);
            if (!fi.Exists) return false;
            var key = $"{fi.FullName}|{name}|{fi.Length}|{fi.LastWriteTimeUtc.Ticks}";
            if (SwitchCache.TryGetValue(key, out var cached)) return cached;
            var needle = System.Text.Encoding.Latin1.GetBytes($"\0{name}\0");
            var found = false;
            using (var fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 1 << 16))
            {
                var chunk = new byte[8 * 1024 * 1024];
                var carry = 0; // bytes kept from the previous chunk's tail, at chunk[0..carry]
                int n;
                while ((n = fs.Read(chunk, carry, chunk.Length - carry)) > 0)
                {
                    var len = carry + n;
                    if (chunk.AsSpan(0, len).IndexOf(needle) >= 0) { found = true; break; }
                    carry = Math.Min(needle.Length - 1, len);
                    Buffer.BlockCopy(chunk, len - carry, chunk, 0, carry);
                }
            }
            SwitchCache[key] = found;
            return found;
        }
        catch { return false; }
    }

    // ── new engine switches (152 r22+) ───────────────────────────────────────

    /// Switches introduced in engine 152 r22, with the option that asked for each. Gated per binary.
    public static readonly IReadOnlyDictionary<string, string> GatedEngineSwitches = new Dictionary<string, string>
    {
        ["--fingerprint-passthrough"] = "Fingerprint = \"off\" (pass-through debug mode)",
        ["--disable-fingerprint-voices"] = "FingerprintVoices = false",
        ["--allow-third-party-cookies"] = "AllowThirdPartyCookies = true",
        ["--transparent-proxy"] = "TransparentProxy = true",
    };

    /// Drop any 152 r22+ switch the engine that will run does not implement, with a warning.
    /// Chromium ignores unknown switches silently, so an older engine would otherwise launch without
    /// the feature and without saying so.
    public static (List<string> Args, List<string> Warnings) GateEngineSwitches(string? exe, IEnumerable<string> args, bool quiet = false)
    {
        var warnings = new List<string>();
        var outArgs = new List<string>();
        foreach (var a in args)
        {
            var name = a.Split('=', 2)[0];
            if (!GatedEngineSwitches.TryGetValue(name, out var what) || EngineSupportsSwitch(exe, name[2..]))
            {
                outArgs.Add(a);
                continue;
            }
            warnings.Add($"clearcote: {what} needs engine 152 r22 or newer; this engine ignores it, so it was not applied.");
        }
        if (!quiet) foreach (var w in warnings) Console.Error.WriteLine(w);
        return (outArgs, warnings);
    }

    /// Launch switches for the non-fingerprint engine options. <c>TransparentProxy</c> is only
    /// meaningful with a proxy (it hides the <c>Proxy-Connection</c> header and proxy-shaped timing);
    /// without one it is dropped with a note.
    public static List<string> EngineExtrasArgs(bool? allowThirdPartyCookies, bool? transparentProxy, ProxyOptions? proxy, bool quiet = false)
    {
        var args = new List<string>();
        if (allowThirdPartyCookies == true) args.Add("--allow-third-party-cookies");
        if (transparentProxy == true)
        {
            if (!string.IsNullOrEmpty(proxy?.Server)) args.Add("--transparent-proxy");
            else if (!quiet) Console.Error.WriteLine("clearcote: transparentProxy has no effect without a proxy; ignored.");
        }
        return args;
    }

    /// Serve as root on Linux needs --no-sandbox (unless the caller already passed it): serve spawns
    /// the binary itself, so Playwright's own --no-sandbox is missing and Chromium refuses to start.
    public static bool ServeNeedsNoSandbox(string osTag, uint? uid, IEnumerable<string> args)
        => osTag == "linux" && uid == 0 && !args.Contains("--no-sandbox");

    /// The switch that keeps the engine's "unsupported command-line flag" warning bar off a served
    /// browser. Any flag on Chromium's list raises it, --no-sandbox among them (which serve adds as
    /// root on Linux), and it lands on the first tab: 56px off that tab's innerHeight, a frame
    /// (outer - inner) no other tab and no real Chrome has. Launch never shows it: Playwright starts
    /// that browser without a startup window, and passes --disable-infobars to a persistent context.
    /// Headless: --disable-infobars, the same switch. Chromium honours it only in headless, where it
    /// suppresses infobars and nothing else, so every headless serve gets it.
    /// Headed: Chromium ignores --disable-infobars. The one switch that drops the warning is
    /// --test-type, which also turns on test-harness behaviour (chrome.test in extension pages, no
    /// component extensions with background pages, no OS integration for installed web apps), none of
    /// it visible to a page. So only with --no-sandbox, the flag root on Linux cannot run without, and
    /// bare: --test-type=webdriver would also waive Payment Request's user-interaction check.
    public static IReadOnlyList<string> ServeInfobarArgs(bool headless, IEnumerable<string> args)
    {
        var all = args.ToList();
        bool Has(string sw) => all.Any(a => a == sw || a.StartsWith(sw + "=", StringComparison.Ordinal));
        if (headless) return Has("--disable-infobars") ? Array.Empty<string>() : new[] { "--disable-infobars" };
        return Has("--no-sandbox") && !Has("--test-type") ? new[] { "--test-type" } : Array.Empty<string>();
    }

    [System.Runtime.InteropServices.DllImport("libc", EntryPoint = "geteuid")]
    private static extern uint GetEuid();

    /// Effective uid on Unix, null elsewhere or when it cannot be read.
    internal static uint? EffectiveUid()
    {
        if (Native.IsWindows) return null;
        try { return GetEuid(); } catch { return null; }
    }
}
