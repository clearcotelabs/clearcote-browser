using System.Collections.Concurrent;

namespace Clearcote;

/// Automation-hygiene warnings for launch and serve (mirrors the automation-hygiene part of the
/// Python clearcote/_warnings.py and Node warnings.ts), and the host warning a Linux persona on Windows
/// gets. Never blocks a launch; printed to stderr unless <see cref="LaunchOptions.Quiet"/> or
/// CLEARCOTE_NO_WARN is set.
public static class LaunchWarnings
{
    /// One warning: a stable <c>Code</c> plus the message printed to stderr. <c>IsNote</c>: a lower-severity
    /// note (printed as <c>clearcote: note:</c>, once per process by <see cref="EmitCoherence"/>).
    public sealed record Warning(string Code, string Message, bool IsNote = false);

    /// Warnings for the caller's own browser <paramref name="userArgs"/>.
    public static List<Warning> ForArgs(IEnumerable<string>? userArgs) => Hygiene(userArgs, devtools: false, userAgent: null);

    // ForArgs, plus the same two warnings for the Playwright pass-through's Devtools and UserAgent.
    private static List<Warning> Hygiene(IEnumerable<string>? userArgs, bool devtools, string? userAgent)
    {
        var args = (userArgs ?? Array.Empty<string>()).Where(a => a is not null).ToArray();
        var outList = new List<Warning>();
        if (args.Any(a => a.Contains("--enable-automation", StringComparison.Ordinal) ||
                          a.StartsWith("--remote-debugging-port", StringComparison.Ordinal)))
            outList.Add(new("automation-arg",
                "your args re-introduce an automation flag (--enable-automation / --remote-debugging-port) the SDK " +
                "strips by default - a strong webdriver/CDP tell."));
        if (devtools || args.Any(a => a.StartsWith("--auto-open-devtools-for-tabs", StringComparison.Ordinal)))
            outList.Add(new("devtools-open",
                "DevTools is set to open (Devtools = true / --auto-open-devtools-for-tabs). Pages can detect an open DevTools " +
                "(debugger and console timing probes; a docked panel also makes innerWidth/innerHeight disagree " +
                "with outerWidth/outerHeight). Leave it closed for real runs."));
        if (!string.IsNullOrEmpty(userAgent) || args.Any(a => a.StartsWith("--user-agent=", StringComparison.Ordinal)))
            outList.Add(new("custom-user-agent",
                "a custom user agent (--user-agent / a context UserAgent) replaces only the User-Agent string: " +
                "navigator.userAgentData, the Sec-CH-UA headers, navigator.platform and the rest of the persona " +
                "keep describing the persona, so a different OS or version in the string is a one-line mismatch. " +
                "Use Platform, Brand and BrandVersion to change what the browser claims."));
        outList.AddRange(CdpExposure(SwitchValue(args, "--remote-debugging-address"),
                                     SwitchValue(args, "--remote-allow-origins")));
        return outList;
    }

    /// The cdp-public-bind / cdp-any-origin warnings for serve's bind address and origin list.
    public static List<Warning> ForServe(string host, string allowOrigins) => CdpExposure(host, allowOrigins);

    /// The code of the warning <see cref="ForPersonaHost"/> gives a Linux persona on a Windows host.
    public const string LinuxPersonaWindowsHost = "linux-persona-windows-host";

    /// The code of the warning <see cref="ForPersonaHost"/> gives a non-macOS persona on a Mac (the same
    /// code the Python and Node SDKs use for a persona whose OS differs from the host's).
    public const string PlatformHostFonts = "platform-host-fonts";

    /// The codes of the once-per-process warnings <see cref="LaunchOptions.StockRuntime"/> gives: on an engine
    /// without the switch, and on a cloud launch (see <see cref="LaunchOpts.StockRuntimeArgs"/>).
    public const string StockRuntimeUnsupported = "stock-runtime-unsupported";
    public const string StockRuntimeCloud = "stock-runtime-cloud";

    /// Warnings for a local launch of persona <paramref name="o"/> on host <paramref name="hostOs"/>
    /// ("windows", "linux", "macos"; default: this machine).
    /// <para>On a Windows host the GPU runs through Direct3D 11, which clamps the WebGL and WebGPU limits
    /// (vertex uniform vectors, a 16384 maximum texture size, ...). A persona can lower a limit, never lift one
    /// past the driver's, so a Linux claim there reports limits no real Linux machine has. Pass-through
    /// (Fingerprint = "off") claims nothing. Only a local launch asks: a Docker launch runs on Linux in its
    /// container, a cloud one remotely. Emit it with <see cref="EmitOnce"/>.</para>
    public static List<Warning> ForPersonaHost(FingerprintOptions o, string? hostOs = null)
    {
        var outList = new List<Warning>();
        if ((hostOs ?? Native.OsTag) == "windows" && !Fingerprint.IsFingerprintPassthrough(o.Fingerprint)
            && string.Equals(o.Platform?.Trim(), "linux", StringComparison.OrdinalIgnoreCase))
            outList.Add(new(LinuxPersonaWindowsHost,
                "a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL), which no real " +
                "Linux machine has. Use Platform = \"windows\" on Windows, or run Linux personas on Linux or in Docker " +
                "(Docker = true)."));
        // On the native macOS build fonts and canvas text metrics stay the Mac's own whatever the claim (its
        // font persona patches target Linux-only sources), so only Platform = "macos" is coherent there --
        // unless a FingerprintProfile captured on the claimed OS supplies them.
        var claimed = o.Platform?.Trim().ToLowerInvariant();
        if ((hostOs ?? Native.OsTag) == "macos" && !string.IsNullOrEmpty(claimed) && claimed != "macos"
            && o.FingerprintProfile is null && !Fingerprint.IsFingerprintPassthrough(o.Fingerprint))
            outList.Add(new(PlatformHostFonts,
                $"Platform = \"{claimed}\" but this host is macos and no FingerprintProfile supplies that OS's fonts/metrics - " +
                $"font, canvas and font-list hashes will be host-native and won't match a real {claimed} Chrome. Use a " +
                $"FingerprintProfile captured on {claimed}, or set Platform = \"macos\". On the native macOS build only " +
                "Platform = \"macos\" is coherent: its fonts and canvas text metrics always stay the Mac's own."));
        return outList;
    }

    // The codes EmitOnce has printed in this process.
    private static readonly ConcurrentDictionary<string, byte> Said = new(StringComparer.Ordinal);

    /// Tests: forget which once-per-process warnings were already printed.
    internal static void ForgetSaid() => Said.Clear();

    /// <see cref="Emit"/>, but each code at most once per process: for warnings about the HOST rather than one
    /// launch's options, so a program that launches in a loop is told once. A quiet launch neither prints one
    /// nor uses it up.
    public static void EmitOnce(IEnumerable<Warning> warnings, bool quiet)
    {
        if (quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN"))) return;
        Emit(warnings.Where(w => Said.TryAdd(w.Code, 0)).ToList(), quiet);
    }

    /// Print <paramref name="warnings"/> to stderr unless <paramref name="quiet"/> or CLEARCOTE_NO_WARN.
    public static void Emit(IEnumerable<Warning> warnings, bool quiet)
    {
        if (quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN"))) return;
        foreach (var w in warnings) Console.Error.WriteLine($"clearcote: {(w.IsNote ? "note" : "warning")}: {w.Message}");
    }

    // ── launch-time coherence (warnings.ts coherenceWarnings / _warnings.py coherence_warnings) ──

    private static readonly string[] SoftwareGpu = { "swiftshader", "llvmpipe", "microsoft basic render", "software adapter", "software" };
    // WARN codes about the HOST rather than one launch's options: said once per process, like a note.
    private static readonly string[] OncePerProcess = { LinuxPersonaWindowsHost };

    /// The code of the once-per-process note that the engine forwards no console / page-error events.
    public const string CdpConsoleEvents = "cdp-console-events";

    private static readonly (string Code, string Message)[] EngineNotes =
    {
        (CdpConsoleEvents,
            "the engine does not forward console or page-error events to automation clients: " +
            "page.Console and page.PageError receive nothing, by design, as part of the " +
            "protection against automation-presence probes. In-page window.onerror and " +
            "unhandledrejection handlers fire normally. To capture console output, collect it in-page " +
            "and read it back with page.EvaluateAsync(). An engine from r32 on forwards them with " +
            "StockRuntime = true (off by default: pages can observe part of what it restores)."),
    };

    private static string? HostFamily(string host)
    {
        var h = host.ToLowerInvariant();
        if (h.StartsWith("win", StringComparison.Ordinal)) return "windows";
        if (h == "darwin" || h.StartsWith("mac", StringComparison.Ordinal)) return "macos";
        if (h.StartsWith("linux", StringComparison.Ordinal)) return "linux";
        return null;
    }

    private static string? GpuIncoherent(string renderer, string platform)
    {
        var r = renderer.ToLowerInvariant();
        if (platform == "macos" && (r.Contains("direct3d") || r.Contains("d3d"))) return "macOS uses Metal/OpenGL, never Direct3D";
        if (platform == "windows" && r.Contains("metal")) return "Windows uses Direct3D/ANGLE, never Metal";
        if (platform == "linux" && (r.Contains("direct3d") || r.Contains("d3d") || r.Contains("metal"))) return "Linux uses OpenGL/Vulkan, never Direct3D/Metal";
        return null;
    }

    /// Incoherent or missing-recommended combinations in a local launch's resolved options (the warnings.ts
    /// <c>coherenceWarnings</c> set, plus the Python SDK's GpuStringSpoof / CanvasNoise advice). Pure: no I/O.
    /// <paramref name="o"/>: the persona (after geoip filled it); <paramref name="proxy"/>: the caller's proxy;
    /// <paramref name="userArgs"/>: the caller's Args; <paramref name="devtools"/> / <paramref name="userAgent"/>:
    /// what the Playwright pass-through options ask for. <paramref name="hostOs"/> defaults to this machine,
    /// <paramref name="buildMajor"/> to the pinned build's Chrome major. Emit them with <see cref="EmitCoherence"/>.
    public static List<Warning> Coherence(FingerprintOptions o, ProxyOptions? proxy = null, bool geoip = false, bool? headless = null,
        IEnumerable<string>? userArgs = null, bool devtools = false, string? userAgent = null, string? hostOs = null, string? buildMajor = null)
    {
        var host = hostOs ?? Native.OsTag;
        var bmajor = buildMajor ?? Release.Current.Version.Split('.')[0];
        var outList = new List<Warning>();
        void Warn(string code, string message) => outList.Add(new(code, message));
        void Note(string code, string message) => outList.Add(new(code, message, IsNote: true));

        var server = proxy?.Server ?? "";
        var platform = o.Platform?.Trim().ToLowerInvariant();
        if (platform == "mac") platform = "macos";
        if (platform == "") platform = null;
        var gpuR = string.IsNullOrEmpty(o.GpuRenderer) ? null : o.GpuRenderer;
        var gpuV = string.IsNullOrEmpty(o.GpuVendor) ? null : o.GpuVendor;
        var profile = o.FingerprintProfile is not null;
        var dgf = o.DisableGpuFingerprint == true;
        var noiseOff = o.FingerprintNoise == false;
        var bridgeOn = !string.IsNullOrEmpty(o.CanvasBridge?.Url);

        if (server.Length > 0 && !geoip && string.IsNullOrEmpty(o.Timezone) && string.IsNullOrEmpty(o.AcceptLanguage))
            Warn("proxy-no-geo",
                "proxy set without Geoip and no Timezone/AcceptLanguage - the browser's timezone and language " +
                "will reflect THIS host, not the proxy's exit region (a geo-mismatch tell). Pass Geoip = true, or " +
                "set Timezone + AcceptLanguage.");
        // No SOCKS + geoip warning: geoip resolves through socks5 (with credentials), and a scheme it cannot use
        // fails the launch with a GeoipException that names it.

        var fam = HostFamily(host);
        if (platform is not null && fam is not null && platform != fam && !profile)
            Warn(PlatformHostFonts,
                $"Platform = \"{platform}\" but this host is {fam} and no FingerprintProfile supplies that OS's " +
                "fonts/metrics - font, canvas and font-list hashes will be host-native and won't match a real " +
                $"{platform} Chrome. Use a FingerprintProfile captured on {platform}, or set Platform = \"{fam}\"." +
                // the macOS build's font persona patches target Linux-only sources: its fonts never follow the claim
                (fam == "macos" ? " On the native macOS build only Platform = \"macos\" is coherent: its fonts and canvas text metrics always stay the Mac's own." : ""));
        // On a Windows host the GPU runs through Direct3D 11, which clamps the WebGL and WebGPU limits; a Linux claim
        // there reports limits no real Linux machine has (see ForPersonaHost). Pass-through claims nothing.
        if (fam == "windows" && platform == "linux" && !Fingerprint.IsFingerprintPassthrough(o.Fingerprint))
            Warn(LinuxPersonaWindowsHost,
                "a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL), which no real " +
                "Linux machine has. Use Platform = \"windows\" on Windows, or run Linux personas on Linux or in Docker " +
                "(Docker = true).");
        if (gpuR is not null && platform is not null && GpuIncoherent(gpuR, platform) is { } why)
            Warn("gpu-platform", $"GpuRenderer is incoherent with Platform = \"{platform}\" ({why}): '{gpuR}'.");
        if (gpuR is not null && SoftwareGpu.Any(s => gpuR.ToLowerInvariant().Contains(s)))
            Warn("gpu-software",
                $"GpuRenderer is a SOFTWARE renderer ('{gpuR}') - a real consumer machine reports a hardware GPU. " +
                "Pin a real GPU string, or use the canvas bridge / a real-GPU host.");
        if (!string.IsNullOrEmpty(o.Brand) && o.Brand.ToLowerInvariant() is not ("chrome" or "google chrome"))
            Warn("brand-mismatch",
                $"Brand = \"{o.Brand}\" is advertised in UA-CH, but the binary's TLS/JA4 and engine are Chrome {bmajor} " +
                "- a UA-vs-transport mismatch strict detectors cross-check. Keep Brand = \"Chrome\".");
        if (!string.IsNullOrEmpty(o.BrandVersion) && o.BrandVersion.Split('.')[0] != bmajor)
            Warn("version-mismatch",
                $"BrandVersion major {o.BrandVersion.Split('.')[0]} differs from the build's Chrome {bmajor} - " +
                $"JA4/UA-CH version desync. Align BrandVersion to {bmajor} (or omit it).");

        // The GPU string can be made real in two different ways, and each leaves a different amount of noise behind,
        // so the advice differs. Neither fires once the operator has turned the noise off.
        if (dgf && !noiseOff)
            Warn("gpu-noise",
                "DisableGpuFingerprint presents the REAL GPU, but per-eTLD farble still perturbs the canvas/WebGL " +
                "readback - noise on otherwise-real pixels is itself a tell. Pair with FingerprintNoise = false.");
        else if (o.GpuStringSpoof == false && !dgf && !noiseOff)
            Warn("gpu-noise-string",
                "GpuStringSpoof = false reports the REAL WebGL vendor/renderer, but per-eTLD farble still perturbs " +
                "BOTH the canvas 2D readback and gl.readPixels - real string over noised pixels is itself a tell. " +
                "FingerprintNoise = false clears both; CanvasNoise = false clears only the canvas 2D half and leaves " +
                "readPixels noised.");
        if (o.GpuStringSpoof == false && !dgf)
            Note("gpu-string-only",
                "GpuStringSpoof = false reports the REAL WebGL vendor/renderer while the persona keeps supplying the " +
                "getParameter limits, the extension list and the shader precision formats - AND navigator.gpu (WebGPU) " +
                "still reports the persona GPU, which WebGL no longer matches. The GPU name, its capabilities and its " +
                "WebGPU identity now come from different sources, and WebGL-vs-WebGPU is readable in one page. That is " +
                "the contract of the narrow switch (it moves the WebGL string and nothing else); use " +
                "DisableGpuFingerprint = true to take the whole GPU surface real together.");
        if (o.CanvasNoise == false)
            Note("canvas-noise-toblob",
                "CanvasNoise = false silences canvas 2D getImageData and toDataURL. On engines before 152 r21, " +
                "canvas.toBlob() / OffscreenCanvas.convertToBlob() were not covered by any noise switch and could " +
                "disagree with toDataURL - avoid CanvasNoise = false there where toBlob is scored. From 152 r21 the " +
                "switch covers the blob exits too and all three agree byte-for-byte.");
        if (headless != false && !bridgeOn && !dgf && !profile)
            Note("headless-render",
                "headless with no CanvasBridge/DisableGpuFingerprint/FingerprintProfile - canvas and WebGL may " +
                "render on software here while the persona claims a hardware GPU (a render-vs-string mismatch on " +
                "canvas-scored sites). Use CanvasBridge, DisableGpuFingerprint, or a real-GPU host.");
        if (gpuV is not null && gpuR is null && !profile)
            Warn("gpu-vendor-alone",
                "GpuVendor is set without GpuRenderer - the renderer still comes from the persona (or this host), and a " +
                "vendor naming another GPU maker than it is a contradiction a page reads directly. Pin both, or neither.");
        if (bridgeOn && gpuR is null && gpuV is null && !profile)
            Note("bridge-no-gpu",
                "CanvasBridge is set but GpuVendor/GpuRenderer aren't pinned - the WebGL renderer string may not " +
                "match the bridge node's pixels. Set them to the bridge node's GPU.");

        // Automation hygiene: the caller's own args, and what the Playwright pass-through options ask for.
        outList.AddRange(Hygiene(userArgs, devtools, userAgent));
        return outList;
    }

    /// Print <see cref="Coherence"/> to stderr unless <paramref name="quiet"/> or CLEARCOTE_NO_WARN. Notes (and the
    /// host warning a Linux persona on Windows gets) are said once per process; the other warnings every launch.
    /// Then the engine notes, once per process: the one that the engine forwards no console events is left out
    /// when <paramref name="stockRuntime"/> (this launch's browser has <c>--disable-runtime-suppression</c>).
    public static void EmitCoherence(IEnumerable<Warning> warnings, bool quiet, bool stockRuntime = false)
    {
        if (quiet || !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("CLEARCOTE_NO_WARN"))) return;
        Emit(warnings.Where(w => !(w.IsNote || OncePerProcess.Contains(w.Code)) || Said.TryAdd(w.Code, 0)).ToList(), quiet);
        foreach (var (code, message) in EngineNotes)
        {
            if (code == CdpConsoleEvents && stockRuntime) continue;
            if (Said.TryAdd(code, 0)) Emit(new[] { new Warning(code, message, IsNote: true) }, quiet);
        }
    }

    // The value of the LAST name=value in args (Chromium keeps the last), else null.
    private static string? SwitchValue(IEnumerable<string> args, string name)
    {
        string? value = null;
        foreach (var a in args)
            if (a.StartsWith(name + "=", StringComparison.Ordinal)) value = a[(name.Length + 1)..];
        return value;
    }

    private static bool IsLoopback(string host)
    {
        var h = host.Trim().Trim('[', ']').ToLowerInvariant();
        return h == "localhost" || h == "::1" || h.StartsWith("127.", StringComparison.Ordinal);
    }

    private static List<Warning> CdpExposure(string? bindAddress, string? allowOrigins)
    {
        var outList = new List<Warning>();
        if (bindAddress is not null && !IsLoopback(bindAddress))
            outList.Add(new("cdp-public-bind",
                $"the DevTools endpoint is bound to {bindAddress}, not loopback: anyone who can reach that port can " +
                "drive the browser, read its cookies and run code in its pages. Keep it on 127.0.0.1 and tunnel to " +
                "it if you need remote access."));
        if (allowOrigins is not null && allowOrigins.Split(',').Any(o => o.Trim() == "*"))
            outList.Add(new("cdp-any-origin",
                "--remote-allow-origins=* lets any web page this browser (or any browser on this machine) opens " +
                "connect to the DevTools endpoint and take it over. List the origins you need instead."));
        return outList;
    }
}
