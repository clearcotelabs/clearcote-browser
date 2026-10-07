using System.Collections.Concurrent;

namespace Clearcote;

/// Automation-hygiene warnings for launch and serve (mirrors the automation-hygiene part of the
/// Python clearcote/_warnings.py and Node warnings.ts), and the host warning a Linux persona on Windows
/// gets. Never blocks a launch; printed to stderr unless <see cref="LaunchOptions.Quiet"/> or
/// CLEARCOTE_NO_WARN is set.
public static class LaunchWarnings
{
    /// One warning: a stable <c>Code</c> plus the message printed to stderr.
    public sealed record Warning(string Code, string Message);

    /// Warnings for the caller's own browser <paramref name="userArgs"/>.
    public static List<Warning> ForArgs(IEnumerable<string>? userArgs)
    {
        var args = (userArgs ?? Array.Empty<string>()).Where(a => a is not null).ToArray();
        var outList = new List<Warning>();
        if (args.Any(a => a.Contains("--enable-automation", StringComparison.Ordinal) ||
                          a.StartsWith("--remote-debugging-port", StringComparison.Ordinal)))
            outList.Add(new("automation-arg",
                "your args re-introduce an automation flag (--enable-automation / --remote-debugging-port) the SDK " +
                "strips by default - a strong webdriver/CDP tell."));
        if (args.Any(a => a.StartsWith("--auto-open-devtools-for-tabs", StringComparison.Ordinal)))
            outList.Add(new("devtools-open",
                "DevTools is set to open (--auto-open-devtools-for-tabs). Pages can detect an open DevTools " +
                "(debugger and console timing probes; a docked panel also makes innerWidth/innerHeight disagree " +
                "with outerWidth/outerHeight). Leave it closed for real runs."));
        if (args.Any(a => a.StartsWith("--user-agent=", StringComparison.Ordinal)))
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
        foreach (var w in warnings) Console.Error.WriteLine($"clearcote: warning: {w.Message}");
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
