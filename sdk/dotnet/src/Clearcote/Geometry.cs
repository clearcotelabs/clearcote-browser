using System.IO.Compression;
using System.Net.WebSockets;
using System.Numerics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// Headless window geometry. Port of the Python SDK's <c>_geometry.py</c> and the Node SDK's
/// <c>geometry.ts</c>; the profile table, the seed hashing and the skip rules are identical, so a seed
/// selects the same persona in every SDK.
/// </summary>
/// <remarks>
/// <para>
/// Headed launches take their geometry from the real display and the SDK keeps the page on it
/// (<c>ViewportSize.NoViewport</c>), so screen/avail/inner/outer agree by construction. Headless has
/// no display, and what it reports depends on whether the engine's persona machinery is running.
/// Regime 1 was measured on 149.0.7827.114/linux-x64, regime 2 on win-x64 149 and 153 (below).
/// </para>
/// <para>
/// REGIME 1 — a persona is active (<c>--fingerprint=&lt;seed&gt;</c> on the command line, or the persona
/// transport's <c>--persona-from-env</c> marker once the seed has moved to the environment). The engine
/// spoofs screen AND avail from the seed, including a taskbar (seed A -> 1920x1080 / avail 1920x1040,
/// seed B -> 2560x1440 / 1400, seed C -> 1600x900 / 860), and its values BEAT a CDP screen override —
/// so the SDK must not try to set screen here, it would silently lose. Leaving Playwright's emulated
/// viewport on is just as wrong: inner 1280x720 inside an outer of 1920x1040 leaves 640px of window
/// unaccounted for by any frame a real browser has. So NoViewport plus one window resize into the
/// persona's own work area, which lands a maximized window:
/// <c>screen 1920x1080, avail 1920x1040, inner 1920x952, outer 1920x1040, frame (0, 88)</c>.
/// The seed's <c>--screen-info</c> row still goes on the command line, on every platform: the persona's
/// own display wins over it (measured on r32 Linux and r33 Windows), so it is a fallback for a persona
/// that does not engage, never a contradiction.
/// </para>
/// <para>
/// REGIME 2 — no persona (the default seedless launch, and <c>LightStealth</c>, which drops
/// <c>--fingerprint</c> deliberately). Nothing spoofs screen, so a page sees the 800x600 headless
/// surface, and Playwright's emulated viewport then synthesizes a window on top of it:
/// <c>screen 1280x720, avail 1280x720, inner 1280x720, outer 1288x851</c> — a window LARGER than its
/// own screen, an impossible state readable in two property lookups, and it was present on every
/// headless shape reachable through the SDK. The fix is <c>--screen-info</c>, which makes the
/// headless DISPLAY a real-machine size (with a taskbar on Windows) before the first document
/// exists, then the same NoViewport + work-area fit as regime 1. Measured on 153.0.8010.36/win-x64:
/// <c>screen 1920x1080, avail 1920x1040, inner 1904x911, outer 1920x1040</c>, popups clamped inside it.
/// </para>
/// <para>
/// This used to be a CDP screen override with the viewport sized as screen minus a hardcoded engine
/// frame. Two faults: the override forces avail == screen (no taskbar, a minority shape), and the
/// frame around an emulated viewport is per-platform — 8x131 on linux-x64 but 16x134 on win-x64
/// (measured on 149.0.7827.114 and 153.0.8010.36) — so on Windows the window landed 8x3 px past the
/// screen edge. With a real display nothing is sized against the frame. (The engine's
/// <c>--fingerprint-screen-*</c> switches are no substitute: device-width media queries keep
/// answering 800x600.) Because the display is a command-line switch, it also reaches pages that
/// <see cref="Clearcote.LaunchAsync"/> callers create themselves.
/// </para>
/// <para>
/// PARITY: the seed still selects the same screen row in every SDK (<see cref="HeadlessGeometry"/>);
/// only how it is applied changed.
/// </para>
/// <para>
/// PROVENANCE of the regime-2 table: the <c>audit_profiles</c> corpus (real captures from the public
/// fingerprint audit), desktop rows whose geometry is self-consistent and which are not themselves
/// emulated-viewport captures, counted by distinct /24 so one busy machine cannot skew it. macOS rows
/// are dropped (color_depth 30, which this engine cannot spoof), non-1.0 DPR rows are dropped
/// (scaling changes what the rasterizer produces), and ultrawide 3440x1440 is capped to weight 2
/// (12 distinct /24s in the corpus — developers over-represent ultrawides).
/// </para>
/// </remarks>
public static class Geometry
{
    /// <summary>
    /// The linux-x64 engine's frame width around an EMULATED viewport: <c>outerWidth = innerWidth + 8</c>.
    /// Measured with no persona on 149.0.7827.114/linux-x64 (same on 150). win-x64 draws 16x134
    /// instead, which is why launches no longer size anything against these (see the class remarks).
    /// They remain the shared cross-SDK constants behind <see cref="HeadlessGeometry"/>'s viewport and
    /// the floor a usable imported-profile screen must clear.
    /// </summary>
    public const int EngineFrameWidth = 8;

    /// <summary>The linux-x64 engine's frame height around an emulated viewport: <c>+ 131</c>.</summary>
    public const int EngineFrameHeight = 131;

    /// <summary>Windows' taskbar at 100% scaling; the engine's persona table uses the same 40px.</summary>
    public const int WindowsTaskbarHeight = 40;

    /// <summary>(ScreenWidth, ScreenHeight, Weight, OsHint) — Weight is distinct /24s in the corpus.</summary>
    public static readonly (int Width, int Height, int Weight, string Os)[] HeadlessScreenProfiles =
    {
        (1920, 1080, 24, "windows"),
        (2560, 1440, 13, "windows"),
        (1920, 1200, 6, "linux"),
        (1366, 768, 3, "windows"),
        (1600, 900, 3, "linux"),
        (3440, 1440, 2, "windows"),   // capped from 12 (see the class remarks)
        (3840, 2160, 2, "windows"),
        (1680, 1050, 2, "windows"),
    };

    // Window flags that mean the caller sized the window themselves (the fit skips its resize).
    private static readonly string[] CallerWindowFlags =
        { "--window-size", "--window-position", "--start-maximized" };

    // The subset that fixes the window's SIZE. A bare --window-position only places it: the SDK still
    // gives the browser a realistic display and skips only the fit (SC 180: the hosted gateway passed
    // --window-position=10,10 and lost the display with it, so every session showed the 800x600 surface).
    private static readonly string[] CallerSizeFlags = { "--window-size", "--start-maximized" };

    // The persona transport's marker (PersonaEnv, SDK 0.41+): the seed left argv for the environment.
    private const string PersonaEnvSwitch = "--" + PersonaEnv.FromEnvSwitch;

    private static readonly string[] CallerDisplayFlags = { "--screen-info" };

    /// <summary>
    /// Whether a persona is active for this launch, i.e. the engine spoofs screen/avail itself
    /// (regime 1): <c>--fingerprint=&lt;seed&gt;</c> on the command line, or the persona transport's
    /// <c>--persona-from-env</c> marker when the seed already moved to the environment
    /// (<see cref="PersonaEnv"/>). <c>LightStealth</c> drops the seed on purpose, so this is false for
    /// it even though a seed was passed to the SDK.
    /// </summary>
    public static bool PersonaActive(IEnumerable<string>? args) =>
        args?.Any(a =>
        {
            var s = a ?? "";
            return s.StartsWith("--fingerprint=", StringComparison.Ordinal)
                || s == PersonaEnvSwitch
                || s.StartsWith(PersonaEnvSwitch + "=", StringComparison.Ordinal);
        }) == true;

    /// <summary>
    /// True when the caller passed their own window geometry flag (size, position or maximize): the
    /// window is theirs, so the SDK neither moves nor fits it.
    /// </summary>
    public static bool CallerSizedTheWindow(IEnumerable<string>? args) =>
        args?.Any(a => CallerWindowFlags.Contains((a ?? "").Split('=')[0])) == true;

    /// <summary>
    /// True when the caller fixed the window's SIZE (<c>--window-size</c> / <c>--start-maximized</c>),
    /// as opposed to only placing it with <c>--window-position</c>.
    /// </summary>
    public static bool CallerFixedTheSize(IEnumerable<string>? args) =>
        args?.Any(a => CallerSizeFlags.Contains((a ?? "").Split('=')[0])) == true;

    /// <summary>True when the caller passed their own headless display switch.</summary>
    public static bool CallerSetTheDisplay(IEnumerable<string>? args) =>
        args?.Any(a => CallerDisplayFlags.Contains((a ?? "").Split('=')[0])) == true;

    /// <summary>
    /// Whether a HEADED launch gets the first-page window fit: a Linux host (no window manager under
    /// Xvfb/Docker, so nothing else maximizes the window) and no window switch of the caller's own.
    /// </summary>
    /// <remarks>
    /// Measured (SC 180): <c>--start-maximized</c> is a no-op there, and Chrome's 945x1060 default
    /// window stays at (10,10); CDP <c>Browser.setWindowBounds</c> before the first navigation gives
    /// <c>outer == avail</c> at (0,0) with zero resize events. Other headed platforms keep the plain
    /// NoViewport behaviour: their window manager sizes the window.
    /// </remarks>
    /// <param name="args">The caller's command line.</param>
    /// <param name="linuxHost">Null reads this host; tests pass true/false.</param>
    public static bool HeadedLinuxFit(IEnumerable<string>? args, bool? linuxHost = null) =>
        (linuxHost ?? OperatingSystem.IsLinux()) && !CallerSizedTheWindow(args);

    /// <summary>
    /// Weighted, deterministic choice from <see cref="HeadlessScreenProfiles"/>. Same construction as
    /// <c>LightStealthValues</c>: the full sha256 digest as a big integer, so Python, Node and .NET
    /// select the identical row for a seed. An unset seed maps to a fixed key rather than randomness,
    /// so a seedless launch stays reproducible.
    /// </summary>
    private static (int Width, int Height, int Weight, string Os) Pick(string? seed)
    {
        var key = string.IsNullOrEmpty(seed) ? "clearcote-headless-geometry" : seed;
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(key));
        var big = new BigInteger(digest, isUnsigned: true, isBigEndian: true);
        var total = HeadlessScreenProfiles.Sum(p => p.Weight);
        var point = (int)(big % total);
        foreach (var row in HeadlessScreenProfiles)
        {
            point -= row.Weight;
            if (point < 0) return row;
        }
        return HeadlessScreenProfiles[^1];
    }

    /// <summary>Context geometry for a given screen: viewport = screen minus the engine's frame.</summary>
    private static (ScreenSize Screen, ViewportSize Viewport) GeometryFor(int width, int height) => (
        new ScreenSize { Width = width, Height = height },
        new ViewportSize { Width = width - EngineFrameWidth, Height = height - EngineFrameHeight }
    );

    private const string ProfileFlag = "--fingerprint-profile=";

    /// <summary>The imported profile's own screen, or null.</summary>
    /// <remarks>
    /// Measured: <c>--fingerprint-profile</c> supplies screen/avail only when a persona
    /// (<c>--fingerprint=</c>) is ALSO running. Without a seed the profile's display is inert, so the
    /// SDK's override is all the page sees — and taking it from the profile keeps the imported identity
    /// instead of giving every seedless profile launch the same corpus screen. Reads the value off the
    /// switch (gzip+base64 of the capture JSON), the one form every SDK has in hand here. Best-effort:
    /// failures return null and the corpus table is used. A screen too small to hold the engine's frame
    /// is rejected — which is what keeps a <c>profile="auto"</c> capture made on a headless host, whose
    /// screen can be the 800x600 surface, from becoming the persona's display.
    /// </remarks>
    public static (int Width, int Height)? ProfileScreenFromArgs(IEnumerable<string>? args)
    {
        foreach (var raw in args ?? Array.Empty<string>())
        {
            var arg = raw ?? "";
            if (!arg.StartsWith(ProfileFlag, StringComparison.Ordinal)) continue;
            try
            {
                var packed = Convert.FromBase64String(arg[ProfileFlag.Length..]);
                using var input = new MemoryStream(packed);
                using var gz = new GZipStream(input, CompressionMode.Decompress);
                using var doc = JsonDocument.Parse(gz);
                if (!doc.RootElement.TryGetProperty("screen", out var screen)) return null;
                if (!screen.TryGetProperty("width", out var w) || !screen.TryGetProperty("height", out var h))
                    return null;
                var width = w.GetInt32();
                var height = h.GetInt32();
                if (width - EngineFrameWidth < 1024 || height - EngineFrameHeight < 600) return null;
                return (width, height);
            }
            catch (Exception)
            {
                return null;
            }
        }
        return null;
    }

    /// <summary>
    /// A seed's regime-2 screen row, with the viewport sized as screen minus the linux engine frame.
    /// This is the cross-SDK parity contract; launches take only the screen from it (the display).
    /// </summary>
    public static (ScreenSize Screen, ViewportSize Viewport) HeadlessGeometry(string? seed = null)
    {
        var row = Pick(seed);
        return GeometryFor(row.Width, row.Height);
    }

    /// <summary>A headless display in CSS px, its work area anchored top-left (taskbar at the bottom).</summary>
    public sealed record Display(int Width, int Height, int AvailWidth, int AvailHeight);

    private static string? SwitchValue(IEnumerable<string>? args, string name)
    {
        var prefix = $"--{name}=";
        string? found = null;
        foreach (var a in args ?? Array.Empty<string>())
            if ((a ?? "").StartsWith(prefix, StringComparison.Ordinal)) found = a![prefix.Length..];
        return found;
    }

    private static int? SwitchInt(IEnumerable<string>? args, string name) =>
        int.TryParse(SwitchValue(args, name), out var v) && v > 0 ? v : null;

    private static string HostPlatform =>
        OperatingSystem.IsLinux() ? "linux" : OperatingSystem.IsMacOS() ? "macos" : "windows";

    /// <summary>The regime-2 headless display for a launch's command line.</summary>
    /// <remarks>
    /// In order: an explicit <c>--fingerprint-screen-width/height</c> (already spoofed into screen.*
    /// by the engine, so the display must agree), an imported profile's screen, and otherwise the
    /// seed's corpus row (<see cref="HeadlessGeometry"/>, identical in every SDK). A Windows platform
    /// gets a taskbar; others report avail == screen, which real captures do too. Everything is read
    /// off the command line, where the platform always is (it defaults to the host OS).
    /// </remarks>
    public static Display HeadlessDisplay(string? seed, IEnumerable<string>? args) =>
        HeadlessDisplay(seed, args, lightStealth: false);

    /// <summary>
    /// <see cref="HeadlessDisplay(string?, IEnumerable{string}?)"/>, and with <paramref name="lightStealth"/>
    /// (ServeAsync only) the LightStealth row that also supplies the seed's DPR, ahead of the corpus row.
    /// </summary>
    internal static Display HeadlessDisplay(string? seed, IEnumerable<string>? args, bool lightStealth)
    {
        var windows = (SwitchValue(args, "fingerprint-platform") ?? HostPlatform) == "windows";
        Display WithTaskbar(int w, int h) => new(w, h, w, windows ? h - WindowsTaskbarHeight : h);

        if (SwitchInt(args, "fingerprint-screen-width") is int sw && SwitchInt(args, "fingerprint-screen-height") is int sh)
        {
            var d = WithTaskbar(sw, sh);
            return d with
            {
                AvailWidth = Math.Min(SwitchInt(args, "fingerprint-avail-width") ?? d.AvailWidth, sw),
                AvailHeight = Math.Min(SwitchInt(args, "fingerprint-avail-height") ?? d.AvailHeight, sh),
            };
        }
        if (ProfileScreenFromArgs(args) is { } fromProfile) return WithTaskbar(fromProfile.Width, fromProfile.Height);
        if (lightStealth)
        {
            var d = Fingerprint.LightStealthScreen(seed);
            return windows ? d : WithTaskbar(d.Width, d.Height);
        }
        var row = Pick(seed);
        return WithTaskbar(row.Width, row.Height);
    }

    /// <summary><c>--screen-info</c> for a display: its size plus the work-area insets the taskbar takes.</summary>
    public static string ScreenInfoSwitch(Display d)
    {
        var insets = new (string Key, int Value)[]
        {
            ("workAreaRight", d.Width - d.AvailWidth), ("workAreaBottom", d.Height - d.AvailHeight),
        }.Where(i => i.Value > 0).Select(i => $" {i.Key}={i.Value}");
        return $"--screen-info={{{d.Width}x{d.Height}{string.Concat(insets)}}}";
    }

    /// <summary>Which regime a launch falls into.</summary>
    public enum Mode
    {
        /// <summary>Headed, or the caller chose their own geometry — nothing to do.</summary>
        None,
        /// <summary>Regime 1: NoViewport, and the window still needs fitting to the persona's work area.</summary>
        Persona,
        /// <summary>The earlier regime 2 (a CDP screen override); only <see cref="Resolve"/> returns it.</summary>
        Profile,
        /// <summary>Regime 2: the SDK's headless display (<see cref="HeadlessPlan.Args"/>) plus NoViewport and a window fit.</summary>
        Display,
    }

    /// <summary>What a headless launch should do; see <see cref="ResolveHeadless"/>.</summary>
    /// <param name="Mode">The regime, or <see cref="Mode.None"/> to leave geometry alone.</param>
    /// <param name="Display">Regime 2's display, or null (other regimes, or the caller passed their own <c>--screen-info</c>).</param>
    /// <param name="Args">Switches to APPEND to the command line. Keep passing the caller's own args to the fit.</param>
    public sealed record HeadlessPlan(Mode Mode, Display? Display, string[] Args);

    /// <summary>Resolve the geometry a headless launch should use.</summary>
    /// <remarks>
    /// <see cref="Mode.None"/> when the launch is headed (the real window is already coherent) or the
    /// caller set ViewportSize / ScreenSize themselves; a null <paramref name="headless"/> means
    /// headless, matching Playwright. Otherwise the context takes NoViewport and the window is fitted
    /// to the work area (<see cref="InstallWindowFixupAsync"/>). A caller's own <c>--screen-info</c>
    /// keeps their display; a caller's own window switch keeps their window (the display is still set).
    /// Under a persona the seed's display still goes on the command line, on every platform: the
    /// engine's persona display wins over it, so it only ever serves a persona that does not engage.
    /// </remarks>
    public static HeadlessPlan ResolveHeadless(
        bool? headless,
        string? seed,
        IEnumerable<string>? args,
        bool callerSetGeometry)
    {
        if (headless == false || callerSetGeometry) return new(Mode.None, null, Array.Empty<string>());
        if (PersonaActive(args))
        {
            // Headless otherwise starts with an 800x600/host-shaped display even though Blink exposes
            // the persona screen. Give the compositor the persona display before fullscreen, popups,
            // or moveTo can observe it. The persona's own display wins over --screen-info on every
            // platform (measured on r32 Linux and r33 Windows: the seed's screen shows, the switch is
            // ignored), so the SDK's row is a fallback for a persona that does not engage, never a
            // contradiction. An explicit caller --screen-info always wins.
            var personaDisplay = CallerSetTheDisplay(args) ? null : HeadlessDisplay(seed, args);
            return new(Mode.Persona, null, personaDisplay is null
                ? Array.Empty<string>()
                : new[] { ScreenInfoSwitch(personaDisplay) });
        }
        var display = CallerSetTheDisplay(args) ? null : HeadlessDisplay(seed, args);
        var extra = new List<string>();
        if (display is not null) extra.Add(ScreenInfoSwitch(display));
        if (!CallerSizedTheWindow(args)) extra.Add("--window-position=0,0");
        return new(Mode.Display, display, extra.ToArray());
    }

    /// <summary>The earlier regime-2 geometry (a CDP screen override sized against the linux frame).</summary>
    [Obsolete("Superseded by ResolveHeadless: regime 2 now sets a real headless display (--screen-info), " +
              "since the engine frame this sized against differs per platform.")]
    public static (Mode Mode, ScreenSize? Screen, ViewportSize? Viewport) Resolve(
        bool? headless,
        string? seed,
        IEnumerable<string>? args,
        bool callerSetGeometry)
    {
        if (headless == false) return (Mode.None, null, null);
        if (callerSetGeometry) return (Mode.None, null, null);
        if (PersonaActive(args)) return (Mode.Persona, null, null);
        // An imported profile carries its own display; prefer it over a corpus pick.
        var fromProfile = ProfileScreenFromArgs(args);
        var (screen, viewport) = fromProfile is not null
            ? GeometryFor(fromProfile.Value.Width, fromProfile.Value.Height)
            : HeadlessGeometry(seed);
        return (Mode.Profile, screen, viewport);
    }

    // A plain expression, NOT "() => [...]": Playwright evaluates an arrow-function string to a
    // function object rather than calling it (which silently broke the Node port once).
    private const string WorkareaJs = "[screen.availWidth, screen.availHeight]";
    private const string OuterJs = "[outerWidth, outerHeight]";

    /// <summary>
    /// A work area only an undisplayed headless browser reports (the 800x600 default) is not worth fitting to.
    /// </summary>
    private static bool Plausible(int[]? area) =>
        area is { Length: 2 } && area[0] >= 1024 && area[1] >= 600;

    private static Dictionary<string, object> Bounds(int width, int height) => new()
    {
        ["left"] = 0, ["top"] = 0, ["width"] = width, ["height"] = height,
    };

    /// <summary>
    /// The bounds correction, given what the window reported after the first attempt, or null when
    /// nothing needs correcting.
    /// </summary>
    /// <remarks>
    /// Requested bounds and reported outerHeight are not always the same quantity: on 149 the window
    /// reported 33px less than the bounds height it was given, and on 150 a persona's window does (33px
    /// in a launch, 89 in ServeAsync; without a persona it reports the bounds exactly). Fitting bounds
    /// to the work area then lands short of maximized (real maximized captures have outer == avail).
    /// Rather than hardcode it, measure the shortfall and add it back — that self-tunes if the engine
    /// changes. Never asks for more than the shortfall.
    /// </remarks>
    public static (int Width, int Height)? FitPlan(int[] avail, int[] outer) => FitPlan(avail, outer, null);

    /// <summary><see cref="FitPlan(int[], int[])"/>, never past the work area for a window smaller than it.</summary>
    /// <param name="avail">The size asked for.</param>
    /// <param name="outer">What the window reported.</param>
    /// <param name="room">The space from the window's position to the work area's far edges. On an axis
    /// where the window is smaller than that, the correction never takes the bounds past the edge: only a
    /// window that fills the work area needs bounds beyond it (the persona engine then clamps the window
    /// to the work area), and an engine without a persona does not clamp at all.</param>
    public static (int Width, int Height)? FitPlan(int[] avail, int[] outer, int[]? room)
    {
        var dw = avail[0] - outer[0];
        var dh = avail[1] - outer[1];
        if (dw <= 0 && dh <= 0) return null;
        int Capped(int want, int i) => room is not null && avail[i] < room[i] ? Math.Min(want, room[i]) : want;
        return (Capped(avail[0] + Math.Max(dw, 0), 0), Capped(avail[1] + Math.Max(dh, 0), 1));
    }

    /// <summary>How long a window resize may take to reach the page before a read is taken as it stands.</summary>
    internal const int SettleMs = 1000;
    private const int SettlePollMs = 20;

    private static bool SameSize(int[] a, int[] b) => a[0] == b[0] && a[1] == b[1];

    /// <summary>
    /// Read <c>[outerWidth, outerHeight]</c> until it reflects the last <c>Browser.setWindowBounds</c>.
    /// </summary>
    /// <remarks>
    /// The call returns before the page has the new window size, so a read straight after it can still
    /// report the previous one (measured on 150/linux-x64: for tens of milliseconds). Correcting
    /// from such a read once asked for 1440+495 px on a 1920 px screen. Settled means the asked-for
    /// size, or a size other than <paramref name="before"/> that is still there one poll later;
    /// <c>Settled</c> is false when neither happened within <paramref name="timeoutMs"/>.
    /// </remarks>
    internal static async Task<(int[] Outer, bool Settled)> SettledOuterAsync(
        Func<Task<int[]>> read, int[] before, int[] want, int timeoutMs = SettleMs)
    {
        var deadline = Environment.TickCount64 + timeoutMs;
        var outer = await read().ConfigureAwait(false);
        while (true)
        {
            if (SameSize(outer, want)) return (outer, true);
            if (Environment.TickCount64 >= deadline) return (outer, false);
            await Task.Delay(SettlePollMs).ConfigureAwait(false);
            var next = await read().ConfigureAwait(false);
            if (!SameSize(outer, before) && SameSize(next, outer)) return (outer, true);
            outer = next;
        }
    }

    /// <summary>
    /// Give the window <paramref name="target"/>, then correct what the engine reports (see
    /// <see cref="FitPlan(int[], int[], int[])"/>), on settled reads only: a correction is planned from a
    /// size the page has actually seen, and kept only when the page then sees a window no larger than
    /// the target; otherwise the bounds go back to the target. With no settled read there is nothing to
    /// correct from, and the target lies inside the work area. Returns the last outer size read.
    /// </summary>
    private static async Task<int[]> FitWindowAsync(
        Func<int, int, Task> setBounds, Func<Task<int[]>> read, int[] target, int[] room, int settleMs)
    {
        var (w, h) = (target[0], target[1]);
        var before = await read().ConfigureAwait(false);
        await setBounds(w, h).ConfigureAwait(false);
        var first = await SettledOuterAsync(read, before, target, settleMs).ConfigureAwait(false);
        var plan = first.Settled ? FitPlan(target, first.Outer, room) : null;
        if (plan is null) return first.Outer;
        await setBounds(plan.Value.Width, plan.Value.Height).ConfigureAwait(false);
        var fixedUp = await SettledOuterAsync(read, first.Outer, new[] { plan.Value.Width, plan.Value.Height }, settleMs)
            .ConfigureAwait(false);
        // Overshooting would trade one impossible geometry for another (outer > target).
        if (fixedUp.Settled && fixedUp.Outer[0] <= w && fixedUp.Outer[1] <= h) return fixedUp.Outer;
        await setBounds(w, h).ConfigureAwait(false);
        return (await SettledOuterAsync(read, fixedUp.Outer, target, settleMs).ConfigureAwait(false)).Outer;
    }

    /// <summary>Old name of <see cref="FitWindowToWorkAreaAsync"/>, which now serves both regimes.</summary>
    [Obsolete("Renamed FitWindowToWorkAreaAsync.")]
    public static Task<(int Width, int Height)?> FitWindowToPersonaAsync(
        IPage page, IEnumerable<string>? args = null) => FitWindowToWorkAreaAsync(page, args);

    /// <summary>
    /// Size the window to the display's work area — the persona's (regime 1) or the one
    /// <c>--screen-info</c> set (regime 2) — so the page reports a maximized window (outer == avail)
    /// instead of the default window sitting inside a much larger screen. Returns the reported outer
    /// size, or null if skipped (a caller's own window switch, size OR position, keeps their window).
    /// </summary>
    /// <remarks>
    /// Headless, and headed on Linux without a window manager (Xvfb, every Docker/VPS headful run):
    /// there <c>--start-maximized</c> and CDP <c>windowState: "maximized"</c> are both no-ops
    /// (measured — the window stays Chrome's 945x1060 default at (10,10)), which is why this sets
    /// explicit bounds. A <see cref="Clearcote.LaunchAsync"/> caller can use it on a page created with
    /// <c>ViewportSize = ViewportSize.NoViewport</c>. Never throws: a geometry improvement must not be
    /// able to fail a launch.
    /// </remarks>
    public static async Task<(int Width, int Height)?> FitWindowToWorkAreaAsync(
        IPage page, IEnumerable<string>? args = null)
    {
        if (CallerSizedTheWindow(args)) return null;
        try
        {
            var avail = await page.EvaluateAsync<int[]>(WorkareaJs).ConfigureAwait(false);
            if (!Plausible(avail)) return null;
            var cdp = await page.Context.NewCDPSessionAsync(page).ConfigureAwait(false);
            var target = await cdp.SendAsync("Browser.getWindowForTarget").ConfigureAwait(false);
            var windowId = target!.Value.GetProperty("windowId").GetInt32();

            var outer = await FitWindowAsync(
                (w, h) => cdp.SendAsync("Browser.setWindowBounds", new Dictionary<string, object>
                {
                    ["windowId"] = windowId, ["bounds"] = Bounds(w, h),
                }),
                () => page.EvaluateAsync<int[]>(OuterJs),
                avail, avail, SettleMs).ConfigureAwait(false);
            return (outer[0], outer[1]);
        }
        catch (Exception)
        {
            return null;   // deliberately silent: never fail a launch over geometry
        }
    }

    /// <summary>
    /// The earlier regime 2: move the real window to (0, 0). Returns the position applied, or null if skipped.
    /// </summary>
    /// <remarks>
    /// Regime 2 leaves the real window at the headless default position — measured (10, 10) — while
    /// the emulated viewport makes outerWidth/Height span the whole spoofed screen.
    /// <c>screenX + outerWidth</c> then exceeds <c>screen.width</c>: the window hangs 10px past the
    /// screen edge on both axes. Only 6% of real single-display captures do that, so it is a weak but
    /// free tell. The move costs one CDP call and leaves the emulated viewport untouched (verified:
    /// inner/outer unchanged, screenX/Y become 0). Never throws.
    /// </remarks>
    [Obsolete("No longer used by the SDK: regime 2 starts windows at the origin with --window-position.")]
    public static async Task<(int X, int Y)?> MoveWindowToOriginAsync(
        IPage page, IEnumerable<string>? args = null)
    {
        if (CallerSizedTheWindow(args)) return null;
        try
        {
            var cdp = await page.Context.NewCDPSessionAsync(page).ConfigureAwait(false);
            var target = await cdp.SendAsync("Browser.getWindowForTarget").ConfigureAwait(false);
            var windowId = target!.Value.GetProperty("windowId").GetInt32();
            // left/top only — sending width/height here would fight the emulated viewport.
            await cdp.SendAsync("Browser.setWindowBounds", new Dictionary<string, object>
            {
                ["windowId"] = windowId,
                ["bounds"] = new Dictionary<string, object> { ["left"] = 0, ["top"] = 0 },
            }).ConfigureAwait(false);
            return (0, 0);
        }
        catch (Exception)
        {
            return null;
        }
    }

    /// <summary>
    /// Apply the headless window fit once, using the context's existing page (a persistent context
    /// always has one) or its first new page: fit to the display's work area. <paramref name="args"/>
    /// are the caller's, so a window switch of theirs is respected.
    /// </summary>
    /// <param name="context">The context whose window to fit.</param>
    /// <param name="args">The caller's command line.</param>
    /// <param name="persona">Leave true. false is the earlier regime-2 origin move, kept for compatibility.</param>
    public static async Task InstallWindowFixupAsync(
        IBrowserContext context, IEnumerable<string>? args = null, bool persona = true)
    {
        var page = context.Pages.Count > 0
            ? context.Pages[0]
            : await context.NewPageAsync().ConfigureAwait(false);
        if (persona) await FitWindowToWorkAreaAsync(page, args).ConfigureAwait(false);
#pragma warning disable CS0618   // the documented compatibility path
        else await MoveWindowToOriginAsync(page, args).ConfigureAwait(false);
#pragma warning restore CS0618
    }

    /// <summary>
    /// Apply the regime-2 screen override to a persistent context, and keep applying it to pages
    /// opened later.
    /// </summary>
    /// <remarks>
    /// <para>
    /// Playwright .NET 1.49 accepts <c>ScreenSize</c> on <c>NewContextAsync</c> but SILENTLY DROPS it
    /// on <c>LaunchPersistentContextAsync</c> (verified against engine 149.0.7827.114: screen came
    /// back equal to the viewport, so <c>outer</c> exceeded <c>screen</c> — exactly the geometry this
    /// module exists to prevent). The Python and Node bindings send it on both, which is why only
    /// this SDK needs the workaround.
    /// </para>
    /// <para>
    /// So issue the override directly: the same <c>Emulation.setDeviceMetricsOverride</c> call
    /// Playwright uses for the viewport, with <c>screenWidth/screenHeight</c> added. Verified to
    /// survive navigations and to fire no resize event. It is per-TARGET — a second tab does not
    /// inherit it — so new pages get it too, via the context's Page event.
    /// </para>
    /// </remarks>
    [Obsolete("No longer used by the SDK: regime 2 sets a real headless display (--screen-info) instead.")]
    public static async Task InstallScreenOverrideAsync(
        IBrowserContext context, ScreenSize screen, ViewportSize viewport)
    {
        async Task ApplyAsync(IPage page)
        {
            try
            {
                var cdp = await context.NewCDPSessionAsync(page).ConfigureAwait(false);
                await cdp.SendAsync("Emulation.setDeviceMetricsOverride", new Dictionary<string, object>
                {
                    ["width"] = viewport.Width,
                    ["height"] = viewport.Height,
                    ["screenWidth"] = screen.Width,
                    ["screenHeight"] = screen.Height,
                    ["deviceScaleFactor"] = 1,
                    ["mobile"] = false,
                }).ConfigureAwait(false);
            }
            catch (Exception)
            {
                // Never fail a launch (or a later page) over geometry.
            }
        }

        // Pages opened after the launch (including window.open from the site) need it as well. The
        // handler cannot be awaited, so it is fire-and-forget with the same swallow.
        context.Page += (_, page) => { _ = ApplyAsync(page); };
        foreach (var page in context.Pages.ToArray())
            await ApplyAsync(page).ConfigureAwait(false);
    }

    /// <summary><c>inner &lt;= outer &lt;= avail &lt;= screen</c> on both axes — the chain a real window satisfies.</summary>
    public static bool GeometryIsCoherent(int[] screen, int[] avail, int[] inner, int[] outer) =>
        inner[0] <= outer[0] && inner[1] <= outer[1]
        && outer[0] <= avail[0] && outer[1] <= avail[1]
        && avail[0] <= screen[0] && avail[1] <= screen[1];

    // ─────────────────────────────────────────────────────────────────────────────────────────────
    // ServeAsync: a raw CDP endpoint. Port of the Node SDK's serve() section of the same name.
    //
    // Everything above rides on Playwright: NoViewport is a context option and the fit runs on each
    // context's first page. A raw endpoint has neither, and a CDP emulation override would not survive
    // either, being scoped to the session that set it. What every target and every client of a served
    // browser inherits is the headless DISPLAY and the real WINDOW, both browser-level. Untouched, a
    // served headless browser reports the 800x600 surface as its screen (measured on 153/win-x64).
    //
    // REGIME 2 (no persona): --screen-info, as in a launch, makes the headless display a real-machine
    // size before the first document exists. The one difference from a launch is which row: ServeAsync
    // takes a LightStealth seed's own row (its DPR's pair), a launch the cross-SDK one.
    //
    // REGIME 1 (persona): the persona picks its display inside the engine, so it is only known once a
    // page can be asked. Emulation.updateScreen (headless-only, browser-level, outlives the session that
    // sent it) then resizes the headless display to match. The seed's --screen-info row still goes on
    // the command line: the engine's own display wins over it (measured on r32 Linux and r33 Windows),
    // so the row is a fallback for a persona that does not engage rather than a contradiction.
    //
    // BOTH: one Browser.setWindowBounds puts the first window on the work area, and --window-position
    // puts later windows at the work-area origin. Deliberately no --window-size: it forces every popup
    // to that size, ignoring the window.open() features real Chrome honours. A caller who only PLACED
    // the window (their own --window-position) keeps its place: the display is still set, the fit is
    // skipped (SC 180: the hosted gateway passed 10,10 and lost the display with it).

    /// <summary>What ServeAsync adds for a headless launch.</summary>
    /// <param name="Persona">Whether a persona is active (its own display wins over the <c>--screen-info</c> in <paramref name="Args"/>).</param>
    /// <param name="Display">The headless display <paramref name="Args"/> sets: the display without a persona, the fallback under one.</param>
    /// <param name="Args">Switches to APPEND to the command line.</param>
    /// <param name="Fit">Whether the served window fit runs; false when the caller placed the window themselves.</param>
    internal sealed record ServedPlan(bool Persona, Display Display, string[] Args, bool Fit);

    /// <summary>
    /// The served-browser geometry, or null to leave it alone: headed, or the caller passed a display
    /// switch or fixed the window's size (the SDK's own android --window-size counts: a phone sizes
    /// itself). A caller who only placed the window (<c>--window-position</c>) still gets the display:
    /// the switch says where the window goes, not how big the screen is. Only the fit is skipped then
    /// (<see cref="ServedPlan.Fit"/> false), so their position holds.
    /// </summary>
    internal static ServedPlan? ServedGeometry(
        IReadOnlyCollection<string> engineArgs, string? seed, bool lightStealth, bool headless)
    {
        if (!headless || CallerFixedTheSize(engineArgs) || CallerSetTheDisplay(engineArgs)) return null;
        var placed = CallerSizedTheWindow(engineArgs);   // a bare --window-position: keep theirs, skip the fit
        var display = HeadlessDisplay(seed, engineArgs, lightStealth);
        var args = placed
            ? new[] { ScreenInfoSwitch(display) }
            : new[] { ScreenInfoSwitch(display), "--window-position=0,0" };
        return new(PersonaActive(engineArgs), display, args, Fit: !placed);
    }

    /// <summary>Throws unless <paramref name="size"/> is null or whole CSS px in 100-10000.</summary>
    internal static void ValidateWindowSize(ViewportSize? size)
    {
        static bool Ok(int v) => v is >= 100 and <= 10000;
        if (size is not null && !(Ok(size.Width) && Ok(size.Height)))
            throw new ArgumentException("clearcote serve: WindowSize must be whole CSS px, 100-10000", nameof(size));
    }

    /// <summary>The slice of a browser-level CDP connection the served-window fit needs.</summary>
    internal interface ICdpSend
    {
        Task<JsonElement> SendAsync(string method, object? parameters = null, string? sessionId = null);
    }

    private const string DisplayJs =
        "[screen.width, screen.height, screen.availLeft, screen.availTop, screen.availWidth, screen.availHeight]";

    /// <summary>
    /// Put the served browser's first window on its display's work area (or <paramref name="windowSize"/>,
    /// clamped into it); under a persona, first make the headless display the persona's own. Only
    /// Target/Emulation/Browser commands plus Runtime.evaluate reads (never Runtime.enable) on the first
    /// page, and every change is browser-level, so nothing lingers when the connection closes.
    /// <c>settleMs</c> bounds the wait for each resize to reach the page (<see cref="SettledOuterAsync"/>).
    /// </summary>
    /// <returns>The display and the window's outer size, or null when it could not act (no page, an
    /// implausible display, or an engine without Emulation.updateScreen under a persona). Never throws.</returns>
    internal static async Task<(Display Display, int[] Outer)?> FitWindowOverCdpAsync(
        ICdpSend cdp, bool persona, ViewportSize? windowSize = null, int settleMs = SettleMs)
    {
        string? sessionId = null;
        try
        {
            var targets = await cdp.SendAsync("Target.getTargets").ConfigureAwait(false);
            var page = targets.TryGetProperty("targetInfos", out var infos)
                ? infos.EnumerateArray().FirstOrDefault(t => t.GetProperty("type").GetString() == "page")
                : default;
            if (page.ValueKind != JsonValueKind.Object) return null;
            var targetId = page.GetProperty("targetId").GetString()!;
            sessionId = (await cdp.SendAsync("Target.attachToTarget",
                new Dictionary<string, object> { ["targetId"] = targetId, ["flatten"] = true }).ConfigureAwait(false))
                .GetProperty("sessionId").GetString();

            async Task<int[]> ReadAsync(string expression)
            {
                var r = await cdp.SendAsync("Runtime.evaluate",
                    new Dictionary<string, object> { ["expression"] = expression, ["returnByValue"] = true },
                    sessionId).ConfigureAwait(false);
                return r.GetProperty("result").GetProperty("value").EnumerateArray().Select(v => (int)Math.Round(v.GetDouble())).ToArray();
            }

            var d = await ReadAsync(DisplayJs).ConfigureAwait(false);
            var (sw, sh, al, at, aw, ah) = (d[0], d[1], d[2], d[3], d[4], d[5]);
            if (!Plausible(new[] { aw, ah }) || aw > sw || ah > sh) return null;
            if (persona)
            {
                var screens = await cdp.SendAsync("Emulation.getScreenInfos").ConfigureAwait(false);
                var list = screens.TryGetProperty("screenInfos", out var s) ? s.EnumerateArray().ToList() : new();
                if (list.Count == 0) return null;
                var primary = list.FirstOrDefault(x => x.TryGetProperty("isPrimary", out var p) && p.GetBoolean());
                if (primary.ValueKind != JsonValueKind.Object) primary = list[0];
                await cdp.SendAsync("Emulation.updateScreen", new Dictionary<string, object>
                {
                    ["screenId"] = primary.GetProperty("id").GetString()!,
                    ["left"] = 0, ["top"] = 0, ["width"] = sw, ["height"] = sh,
                    ["workAreaInsets"] = new Dictionary<string, object>
                    {
                        ["left"] = al, ["top"] = at, ["right"] = sw - al - aw, ["bottom"] = sh - at - ah,
                    },
                }).ConfigureAwait(false);
            }
            // The asked-for size, never past the work area; a window smaller than it sits 10px in, like
            // Chrome's own first placement.
            var w = Math.Min(windowSize?.Width ?? aw, aw);
            var h = Math.Min(windowSize?.Height ?? ah, ah);
            var left = al + Math.Min(10, aw - w);
            var top = at + Math.Min(10, ah - h);
            var windowId = (await cdp.SendAsync("Browser.getWindowForTarget",
                new Dictionary<string, object> { ["targetId"] = targetId }).ConfigureAwait(false))
                .GetProperty("windowId").GetInt32();
            Task SetBoundsAsync(int bw, int bh) => cdp.SendAsync("Browser.setWindowBounds", new Dictionary<string, object>
            {
                ["windowId"] = windowId,
                ["bounds"] = new Dictionary<string, object> { ["left"] = left, ["top"] = top, ["width"] = bw, ["height"] = bh },
            });

            // Same as FitWindowToWorkAreaAsync: an engine that reports less than the bounds it was given
            // gets the shortfall added back, an overshoot is reverted, and only settled reads count.
            var outer = await FitWindowAsync(SetBoundsAsync, () => ReadAsync(OuterJs), new[] { w, h },
                new[] { al + aw - left, at + ah - top }, settleMs).ConfigureAwait(false);
            return (new Display(sw, sh, aw, ah), outer);
        }
        catch (Exception)
        {
            return null;   // deliberately silent: never fail a launch over geometry
        }
        finally
        {
            if (sessionId is not null)
            {
                try
                {
                    await cdp.SendAsync("Target.detachFromTarget",
                        new Dictionary<string, object> { ["sessionId"] = sessionId }).ConfigureAwait(false);
                }
                catch (Exception) { }
            }
        }
    }

    /// <summary>A minimal CDP client on <see cref="ClientWebSocket"/>: request/response only, events dropped.</summary>
    private sealed class CdpSocket : ICdpSend, IDisposable
    {
        private readonly ClientWebSocket _ws = new();
        private readonly TimeSpan _timeout;
        private int _next;

        private CdpSocket(TimeSpan timeout) => _timeout = timeout;

        public static async Task<CdpSocket> ConnectAsync(string wsUrl, TimeSpan timeout)
        {
            var c = new CdpSocket(timeout);
            using var cts = new CancellationTokenSource(timeout);
            await c._ws.ConnectAsync(new Uri(wsUrl), cts.Token).ConfigureAwait(false);
            return c;
        }

        public async Task<JsonElement> SendAsync(string method, object? parameters = null, string? sessionId = null)
        {
            var id = ++_next;
            var msg = new Dictionary<string, object> { ["id"] = id, ["method"] = method, ["params"] = parameters ?? new Dictionary<string, object>() };
            if (sessionId is not null) msg["sessionId"] = sessionId;
            using var cts = new CancellationTokenSource(_timeout);
            await _ws.SendAsync(JsonSerializer.SerializeToUtf8Bytes(msg), WebSocketMessageType.Text, true, cts.Token).ConfigureAwait(false);
            var buffer = new byte[64 * 1024];
            while (true)
            {
                using var message = new MemoryStream();
                WebSocketReceiveResult r;
                do
                {
                    r = await _ws.ReceiveAsync(buffer, cts.Token).ConfigureAwait(false);
                    if (r.MessageType == WebSocketMessageType.Close) throw new Exception("CDP connection closed");
                    message.Write(buffer, 0, r.Count);
                } while (!r.EndOfMessage);
                using var doc = JsonDocument.Parse(message.ToArray());
                var root = doc.RootElement;
                if (!root.TryGetProperty("id", out var rid) || rid.GetInt32() != id) continue;   // an event
                if (root.TryGetProperty("error", out var err)) throw new Exception(err.GetProperty("message").GetString());
                return root.TryGetProperty("result", out var result) ? result.Clone() : default;
            }
        }

        public void Dispose()
        {
            try { _ws.Abort(); } catch { }
            _ws.Dispose();
        }
    }

    /// <summary><see cref="FitWindowOverCdpAsync"/> on the SDK's own short connection. Never throws.</summary>
    internal static async Task<(Display Display, int[] Outer)?> FitServedWindowAsync(
        string? wsUrl, bool persona, ViewportSize? windowSize = null, int timeoutMs = 5000)
    {
        if (string.IsNullOrEmpty(wsUrl)) return null;
        try
        {
            using var cdp = await CdpSocket.ConnectAsync(wsUrl, TimeSpan.FromMilliseconds(timeoutMs)).ConfigureAwait(false);
            return await FitWindowOverCdpAsync(cdp, persona, windowSize).ConfigureAwait(false);
        }
        catch (Exception)
        {
            return null;
        }
    }
}
