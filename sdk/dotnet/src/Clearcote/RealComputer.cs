using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;

namespace Clearcote;

/// <summary>
/// Show parts of THIS computer instead of the persona's generated values, for one identity per real desktop.
/// <c>RealComputer = true</c> turns on every part; <c>new RealComputer { Screen = true }</c> picks parts.
/// </summary>
/// <remarks>
/// <para><b>Screen</b>: the primary display (size, work area, devicePixelRatio), read from the operating system.
/// <b>Touch</b>: navigator.maxTouchPoints as this computer reports it (Windows; 0 on macOS; Linux keeps the persona's).
/// <b>Languages</b>: the system display language, expanded the way Chrome writes it (en-GB -> en-GB,en-US,en). Behind a
/// proxy with geoip on, the proxy's exit keeps deciding the languages.</para>
/// <para>A value you set yourself (ScreenWidth, MaxTouchPoints, AcceptLanguage) always wins. Off by default: every profile
/// on one computer then shares these values, which links profiles more easily, and servers and containers have no real
/// display (the launch then keeps the generated value and says so once). Not available for cloud or Docker launches.
/// Measured on a detection service's tampering score: this computer's touch value lowered it in 25 of 27 paired runs and
/// its real monitor in 16 of 16. Same rules and parsers as the Node and Python SDKs.</para>
/// </remarks>
public sealed class RealComputer
{
    /// The primary display instead of the persona's screen.
    public bool Screen { get; init; }
    /// This computer's touch points instead of the persona's.
    public bool Touch { get; init; }
    /// The system display language instead of the persona's (not behind a proxy with geoip on).
    public bool Languages { get; init; }

    /// Every part.
    public static RealComputer All => new() { Screen = true, Touch = true, Languages = true };
    /// No part (the same as leaving the option unset).
    public static RealComputer None => new();
    /// <c>true</c> = every part, <c>false</c> = none.
    public static implicit operator RealComputer(bool on) => on ? All : None;
    /// Whether any part is on.
    public bool Any => Screen || Touch || Languages;

    // ── reading this computer ────────────────────────────────────────────────

    /// A display in CSS px.
    public sealed record HostDisplay(int Width, int Height, int AvailWidth, int AvailHeight, double DevicePixelRatio);
    /// What could be read; a part that could not is null.
    public sealed record HostFacts(HostDisplay? Display, int? Touch, string? Language);

    internal const string WarnCode = "real-computer-partial";
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(8);
    private static readonly Regex Tag = new(@"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$", RegexOptions.CultureInvariant);

    private const string WinPs =
        "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Windows.Forms; " +
        "Add-Type -Namespace CcHost -Name U -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); [DllImport(\"user32.dll\")] public static extern int GetSystemMetrics(int n);'; " +
        "[void][CcHost.U]::SetProcessDPIAware(); $s=[System.Windows.Forms.Screen]::PrimaryScreen; " +
        "$d=96; try { $v=(Get-ItemProperty 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' -Name AppliedDPI).AppliedDPI; if ($v) { $d=$v } } catch {}; " +
        "\"$($s.Bounds.Width) $($s.Bounds.Height) $($s.WorkingArea.Width) $($s.WorkingArea.Height) $d $([CcHost.U]::GetSystemMetrics(95)) $((Get-UICulture).Name)\"";

    private const string MacJxa =
        "ObjC.import('AppKit'); var s = $.NSScreen.mainScreen; var f = s.frame, v = s.visibleFrame; " +
        "var l = $.NSLocale.preferredLanguages.count > 0 ? ObjC.unwrap($.NSLocale.preferredLanguages.objectAtIndex(0)) : ''; " +
        "[f.size.width, f.size.height, v.size.width, v.size.height, s.backingScaleFactor, l].join(' ')";

    private static Task<HostFacts>? _cached;
    private static readonly object Gate = new();

    /// Tests: forget the cached facts.
    internal static void ResetForTests() { lock (Gate) _cached = null; }

    /// This computer's display, touch points and display language, read once per process.
    public static Task<HostFacts> ReadHostFactsAsync()
    {
        lock (Gate) return _cached ??= ReadCoreAsync(OperatingSystem.IsWindows() ? "win32" : OperatingSystem.IsMacOS() ? "darwin" : "linux",
            name => Environment.GetEnvironmentVariable(name));
    }

    internal static async Task<HostFacts> ReadCoreAsync(string platform, Func<string, string?> env)
    {
        if (platform == "win32")
        {
            var outp = await RunAsync("powershell.exe", new[] { "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", WinPs }).ConfigureAwait(false);
            return outp is null ? new HostFacts(null, null, null) : ParseWindows(outp);
        }
        if (platform == "darwin")
        {
            var outp = await RunAsync("osascript", new[] { "-l", "JavaScript", "-e", MacJxa }).ConfigureAwait(false);
            return outp is null ? new HostFacts(null, 0, null) : ParseMac(outp);
        }
        var language = Lang(FirstNonEmpty((env("LANGUAGE") ?? "").Split(':')[0], env("LC_ALL"), env("LC_MESSAGES"), env("LANG")));
        if (string.IsNullOrEmpty(env("DISPLAY")) && string.IsNullOrEmpty(env("WAYLAND_DISPLAY"))) return new HostFacts(null, null, language);
        var x = await RunAsync("xrandr", new[] { "--current" }).ConfigureAwait(false);
        return new HostFacts(x is null ? null : ParseXrandr(x), null, language);
    }

    private static string? FirstNonEmpty(params string?[] v) => v.FirstOrDefault(s => !string.IsNullOrWhiteSpace(s));

    private static async Task<string?> RunAsync(string file, string[] args)
    {
        try
        {
            var psi = new ProcessStartInfo(file) { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false, CreateNoWindow = true };
            foreach (var a in args) psi.ArgumentList.Add(a);
            using var p = Process.Start(psi);
            if (p is null) return null;
            var read = p.StandardOutput.ReadToEndAsync();
            _ = p.StandardError.ReadToEndAsync();
            using var cts = new CancellationTokenSource(Timeout);
            try { await p.WaitForExitAsync(cts.Token).ConfigureAwait(false); }
            catch (OperationCanceledException) { try { p.Kill(true); } catch { /* gone */ } return null; }
            return p.ExitCode == 0 ? await read.ConfigureAwait(false) : null;
        }
        catch { return null; }
    }

    private static double Num(string? s) =>
        double.TryParse(s, NumberStyles.Float, CultureInfo.InvariantCulture, out var v) ? v : double.NaN;

    private static bool Finite(params double[] v) => v.All(x => !double.IsNaN(x) && !double.IsInfinity(x));
    private static double Ratio2(double r) => Math.Round(r * 100) / 100;

    internal static string? Lang(string? s)
    {
        var t = (s ?? "").Trim().Replace('_', '-').Split('.')[0];
        return t.Length > 0 && Tag.IsMatch(t) && !t.Equals("c", StringComparison.OrdinalIgnoreCase) ? t : null;
    }

    /// At least 640x480 CSS px, a work area inside it (at least half of it), a ratio from 1 to 4; else null.
    internal static HostDisplay? Plausible(HostDisplay? d)
    {
        if (d is null) return null;
        var ok = d.Width >= 640 && d.Height >= 480 && d.Width <= 16384 && d.Height <= 16384 &&
                 d.AvailWidth > 0 && d.AvailHeight > 0 && d.AvailWidth <= d.Width && d.AvailHeight <= d.Height &&
                 d.AvailWidth >= d.Width / 2.0 && d.AvailHeight >= d.Height / 2.0 &&
                 d.DevicePixelRatio >= 1 && d.DevicePixelRatio <= 4;
        return ok ? d : null;
    }

    /// Windows: "boundsW boundsH workW workH appliedDpi touches uiCulture" (sizes in physical px).
    internal static HostFacts ParseWindows(string outp)
    {
        var f = outp.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        double At(int i) => i < f.Length ? Num(f[i]) : double.NaN;
        var dpi = At(4);
        var r = Finite(dpi) && dpi > 0 ? Ratio2(dpi / 96) : double.NaN;
        HostDisplay? display = null;
        if (Finite(At(0), At(1), At(2), At(3), r))
            display = Plausible(new HostDisplay((int)Math.Round(At(0) / r), (int)Math.Round(At(1) / r), (int)Math.Round(At(2) / r), (int)Math.Round(At(3) / r), r));
        int? touch = null;
        var t = At(5);
        if (f.Length > 5 && Finite(t) && t == Math.Floor(t) && t >= 0 && t <= 256) touch = (int)t;
        return new HostFacts(display, touch, Lang(f.Length > 6 ? f[6] : null));
    }

    /// macOS: "frameW frameH visibleW visibleH scale language" (points = CSS px). Chrome on macOS reports 0 touch points.
    internal static HostFacts ParseMac(string outp)
    {
        var f = outp.Trim().Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        HostDisplay? display = null;
        if (f.Length >= 5)
        {
            var n = f.Take(5).Select(Num).ToArray();
            if (Finite(n))
                display = Plausible(new HostDisplay((int)Math.Round(n[0]), (int)Math.Round(n[1]), (int)Math.Round(n[2]), (int)Math.Round(n[3]), Ratio2(n[4])));
        }
        return new HostFacts(display, 0, Lang(f.Length > 5 ? f[5] : null));
    }

    private static readonly Regex Mode = new(@"(\d+)x(\d+)\+\d+\+\d+", RegexOptions.CultureInvariant);

    /// Linux: the primary output of `xrandr --current`, else the first connected one with a mode; work area = screen.
    internal static HostDisplay? ParseXrandr(string outp)
    {
        var lines = outp.Split('\n').Where(l => l.Contains(" connected ", StringComparison.Ordinal)).ToList();
        var line = lines.FirstOrDefault(l => l.Contains(" connected primary ", StringComparison.Ordinal)) ?? lines.FirstOrDefault(l => Mode.IsMatch(l));
        var m = line is null ? null : Mode.Match(line);
        if (m is null || !m.Success) return null;
        int w = int.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture), h = int.Parse(m.Groups[2].Value, CultureInfo.InvariantCulture);
        return Plausible(new HostDisplay(w, h, w, h, 1));
    }

    // ── applying it to one launch ────────────────────────────────────────────

    /// Fill the screen / touch / language options of one launch (mutates <paramref name="o"/>, which is the launch's own
    /// copy). <paramref name="proxied"/>: a proxy is set and geoip is on, so the exit decides the languages.
    internal static async Task ApplyAsync(FingerprintOptions o, bool proxied, bool quiet, Func<Task<HostFacts>>? read = null)
    {
        var parts = o.RealComputer;
        if (parts is null || !parts.Any) return;
        if (Fingerprint.IsFingerprintPassthrough(o.Fingerprint)) return;
        bool wantScreen = parts.Screen && o.ScreenWidth is null && o.ScreenHeight is null;
        bool wantTouch = parts.Touch && o.MaxTouchPoints is null;
        bool wantLang = parts.Languages && string.IsNullOrEmpty(o.AcceptLanguage) && !proxied;
        if (!wantScreen && !wantTouch && !wantLang) return;
        var facts = await (read ?? ReadHostFactsAsync)().ConfigureAwait(false);
        var missing = new List<string>();
        if (wantScreen)
        {
            if (facts.Display is { } d)
            {
                o.ScreenWidth = d.Width; o.ScreenHeight = d.Height; o.AvailWidth = d.AvailWidth; o.AvailHeight = d.AvailHeight;
                o.DevicePixelRatio = d.DevicePixelRatio;
            }
            else missing.Add("screen");
        }
        if (wantTouch)
        {
            if (facts.Touch is { } t) o.MaxTouchPoints = t;
            else missing.Add("touch");
        }
        if (wantLang)
        {
            if (!string.IsNullOrEmpty(facts.Language)) o.AcceptLanguage = facts.Language;
            else missing.Add("languages");
        }
        if (missing.Count > 0)
            LaunchWarnings.EmitOnce(new[]
            {
                new LaunchWarnings.Warning(WarnCode,
                    $"RealComputer: this computer's {string.Join(" and ", missing)} could not be read (no display server, or it did not answer), " +
                    "so the profile keeps its generated value there."),
            }, quiet);
    }
}
