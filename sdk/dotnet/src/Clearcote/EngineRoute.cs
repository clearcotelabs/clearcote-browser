using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using Microsoft.Playwright;

namespace Clearcote;

// The engine route (PRO): point-to-point clicks walked by the engine itself through Browser.dispatchPointerPath
// (Browser.humanizedClick before r34; real recorded human paths for PRO, engine bezier for free, tiered by the
// engine licence gate), and on an r43 engine also right/double/modified clicks, held drag legs and slip-free typed
// runs (Browser.dispatchKeySequence). A port of sdk/node/src/humanize.ts (sendEngineCommand, engineSession,
// engineExtOk, engineGlide, engineExtClick, engineType) and sdk/python/clearcote/_humanize.py. Anything the engine
// cannot take -- an older engine, a browser with no CDP, a Firefox -- falls back to the SDK planner.
public static partial class Humanize
{
    /// <summary>
    /// The environment variable that says which clicks the engine pointer path (Browser.dispatchPointerPath) may
    /// walk when <see cref="HumanizeOptions.Engine"/> is "auto": "off" (also 0/false/no) never; "all" (also
    /// "selector") coordinate clicks AND targeted <c>HumanClickAsync(selector)</c> / locator clicks; anything else
    /// (the default) coordinate clicks only. Same values as Python and Node.
    /// </summary>
    public const string EngineClicksEnv = "CLEARCOTE_HUMANIZE_ENGINE_CLICKS";

    /// <summary>
    /// The environment variable that keeps the r43 engine extensions unused when set to "off" (also 0/false/no):
    /// no fromX/fromY re-sync, no engine-side right/double/modified clicks, held legs or typed runs (r42 behaviour).
    /// </summary>
    public const string EngineExtEnv = "CLEARCOTE_HUMANIZE_ENGINE_EXT";

    // The engine's pointer-path command. r34 renamed Browser.humanizedClick to Browser.dispatchPointerPath:
    // "humanizedClick" is known vocabulary. r33 and older only know the old name, so the new one goes first and the
    // old one is the fallback for a method-not-found only.
    internal const string PointerPath = "Browser.dispatchPointerPath";
    internal const string PointerPathLegacy = "Browser.humanizedClick";

    /// <summary>What <see cref="HumanizeOptions.OnFallback"/> hears (api "engine_ext"), once per browser, when a PRO
    /// engine (it has the pointer path) lacks the r43 extensions.</summary>
    internal const string EngineExtMissing = "the engine has no r43 extensions (Browser.dispatchKeySequence): the SDK planner walks and types";

    // The longest an engine walk waits for the page to draw (IsolatedWorld.Frames): measured 111-812 ms on a fresh
    // headless browser's first page (r40, SC 216). A page that never draws (a hidden tab) walks after it.
    private const int FramesWaitMs = 1500;

    // Which name each browser answered to, so only its first call can pay for a miss. NoEnginePath: the engine knew
    // neither name -- remembered too, so a new page of that browser does not probe both again. Keyed by the IBrowser
    // (or the context when there is none: the command name is a property of the engine).
    private static readonly ConditionalWeakTable<object, Dictionary<string, string>> EngineMethodNames = new();
    private const string NoEnginePath = "";
    // Which browsers' engines take the r43 extensions (asked once per browser), and which have been reported.
    private static readonly ConditionalWeakTable<object, StrongBox<bool>> EngineExt = new();
    private static readonly ConditionalWeakTable<object, object> EngineExtReported = new();

    // Per page: the browser-level CDP session, the page's target id, whether the route is still on, and where the
    // engine last left the pointer (null: it has not walked; Stale: something else has moved the pointer since).
    private sealed class EngineState
    {
        public ICDPSession? Session;
        public object? Key;
        public string? TargetId;
        public bool Ok = true;
        public (double X, double Y)? At;
        public bool Stale;
    }
    private static readonly ConditionalWeakTable<IPage, EngineState> Engines = new();
    private static EngineState EngineOf(IPage page) => Engines.GetValue(page, _ => new EngineState());

    /// <summary>Which clicks the engine may walk per <see cref="EngineClicksEnv"/>: "off" | "coords" | "all".</summary>
    internal static string EngineClicksMode()
    {
        var v = (Environment.GetEnvironmentVariable(EngineClicksEnv) ?? "").Trim().ToLowerInvariant();
        if (v is "0" or "off" or "false" or "no" or "none") return "off";
        if (v is "all" or "selector" or "selectors") return "all";
        return "coords";
    }

    /// <summary>False when <see cref="EngineExtEnv"/> turns the r43 extensions off.</summary>
    internal static bool EngineExtMode()
    {
        var v = (Environment.GetEnvironmentVariable(EngineExtEnv) ?? "").Trim().ToLowerInvariant();
        return v is not ("0" or "off" or "false" or "no" or "none");
    }

    /// <summary>The engine click mode of a page: its <see cref="HumanizeOptions.Engine"/> unless "auto", else the
    /// environment's (<see cref="EngineClicksMode"/>).</summary>
    internal static string EngineModeFor(IPage page)
        => Options.TryGetValue(page, out var o) && o.Engine is "off" or "coords" or "all" ? o.Engine : EngineClicksMode();

    // A CDP method-not-found: the engine predates the name. Playwright keeps only the message
    // ("Protocol error (X): 'X' wasn't found").
    internal static bool IsMethodNotFound(Exception e)
    {
        var m = e.Message ?? "";
        return m.Contains("-32601", StringComparison.Ordinal) || m.Contains("wasn't found", StringComparison.Ordinal)
            || m.Contains("method not found", StringComparison.OrdinalIgnoreCase);
    }

    // CDP's "invalid params" (-32602) and nothing else: what an r43 engine answers to an EMPTY dispatchKeySequence --
    // the code when the message carries one, else the exact -32602 wording ("Invalid parameters", crdtp's, after
    // Playwright's "Protocol error (method): " prefix). Any other failure of the probe (a proxy's "Invalid target",
    // "Invalid session id") is NOT an r43 engine: a browser memoised as r43 by mistake would get fromX/fromY on an
    // out-of-sync plain click, which a pre-r43 engine ignores while walking from its own stale point -- the teleport
    // the re-sync exists to remove (verifier A's R4, SC 232).
    private static readonly Regex CdpInvalidParams = new(@"(?:^|: )Invalid parameters(?![A-Za-z])", RegexOptions.CultureInvariant);
    internal static bool IsInvalidParams(Exception e)
    {
        if (IsMethodNotFound(e)) return false;
        var m = e.Message ?? "";
        return m.Contains("-32602", StringComparison.Ordinal) || CdpInvalidParams.IsMatch(m);
    }

    /// <summary>Did this browser's engine already say it has no pointer-path command?</summary>
    internal static bool EngineHasNoPointerPath(object? cacheKey)
        => cacheKey is not null && EngineMethodNames.TryGetValue(cacheKey, out var n)
           && n.TryGetValue(PointerPath, out var known) && known == NoEnginePath;

    /// <summary>
    /// Send <paramref name="method"/>, or <paramref name="legacy"/> when the engine predates the rename, and remember
    /// per <paramref name="cacheKey"/> (a browser, or the context when there is none) which one answered -- or that
    /// neither did, after which every call fails at once without a round trip. Any error other than method-not-found
    /// propagates as-is: retrying a real failure under the old name would only replay it.
    /// </summary>
    internal static async Task<JsonElement?> SendEngineCommandAsync(ICDPSession session, object cacheKey, string method,
        string legacy, Dictionary<string, object> args)
    {
        var names = EngineMethodNames.GetValue(cacheKey, _ => new Dictionary<string, string>());
        string? known;
        lock (names) names.TryGetValue(method, out known);
        if (known == NoEnginePath) throw new PlaywrightException($"'{method}' wasn't found");
        if (known is not null) return await session.SendAsync(known, args).ConfigureAwait(false);
        try
        {
            var r = await session.SendAsync(method, args).ConfigureAwait(false);
            lock (names) names[method] = method;
            return r;
        }
        catch (Exception e) when (IsMethodNotFound(e))
        {
            // below: the old name
        }
        JsonElement? r2;
        try
        {
            r2 = await session.SendAsync(legacy, args).ConfigureAwait(false);
        }
        catch (Exception e) when (IsMethodNotFound(e))
        {
            lock (names) names[method] = NoEnginePath;
            throw;
        }
        lock (names) names[method] = legacy;
        return r2;
    }

    // The engine walks from ITS OWN last position on this page, not from where an SDK glide left the pointer.
    // Measured on r38 (SC 210): an engine click at (300,300), an SDK glide to (800,550), then a coordinate click at
    // (400,200) through the engine -- its first sample was at (300,300), a 559 px teleport from where the pointer
    // was. So the engine walks only while nothing else has moved the pointer since its own last walk (before its
    // first walk: while nothing has moved it at all).
    private static bool EngineInSync(EngineState eng, PointerState st)
        => !eng.Stale && (eng.At is not { } at ? !st.Known : st.X == at.X && st.Y == at.Y);

    /// <summary>Something other than the engine moved the pointer (a native fallback click): the engine must not walk
    /// from its own stale point next (verifier A #6).</summary>
    private static void EngineOutOfSync(IPage page)
    {
        if (!Engines.TryGetValue(page, out var eng)) return;
        eng.At = null;
        eng.Stale = true;
    }

    // The browser that owns the page's engine, or the context when there is none (a persistent context on older
    // Playwright): the command name is learnt once per browser.
    private static object EngineKeyOf(IPage page)
    {
        var ctx = page.Context;
        return (object?)ctx.Browser ?? ctx;
    }

    private static async Task EngineSessionAsync(IPage page, EngineState eng)
    {
        if (eng.Session is not null) return;
        var ctx = page.Context;
        var tsession = await ctx.NewCDPSessionAsync(page).ConfigureAwait(false);
        var info = await tsession.SendAsync("Target.getTargetInfo").ConfigureAwait(false);
        eng.TargetId = info!.Value.GetProperty("targetInfo").GetProperty("targetId").GetString();
        var br = ctx.Browser;
        eng.Key = (object?)br ?? ctx;
        eng.Session = br is not null ? await br.NewBrowserCDPSessionAsync().ConfigureAwait(false) : tsession;
    }

    // Lane A's OnFallback hears ONCE per browser that a PRO engine (it has the pointer path) lacks the r43
    // extensions. The hook only: the SDK planner does the work, so this is not a native fallback and strict mode
    // does not throw.
    private static void MaybeReportExt(IPage page, EngineState eng)
    {
        if (eng.Key is not { } key || !EngineExt.TryGetValue(key, out var ext) || ext.Value) return;
        if (!EngineMethodNames.TryGetValue(key, out var names)) return;
        string? known;
        lock (names) names.TryGetValue(PointerPath, out known);
        if (string.IsNullOrEmpty(known)) return;
        lock (EngineExtReported)
        {
            if (EngineExtReported.TryGetValue(key, out _)) return;
            EngineExtReported.Add(key, true);
        }
        FallbackTrace?.Invoke("engine_ext: " + EngineExtMissing);
        if (Options.TryGetValue(page, out var o)) o.OnFallback?.Invoke(new HumanizeFallbackEvent("engine_ext", EngineExtMissing, null));
    }

    // r43 engine (SC 232 lane H): dispatchPointerPath takes fromX/fromY, button, clickCount, modifiers, buttons,
    // settle and answers after its last event; dispatchKeySequence exists. Asked once per browser, only when needed:
    // an empty dispatchKeySequence answers -32602 (invalid params) on r43 and -32601 (not found) before; anything
    // else counts as no (an engine that accepts no keys is not r43).
    private static async Task<bool> EngineExtOkAsync(IPage page)
    {
        var eng = EngineOf(page);
        if (!eng.Ok || !EngineExtMode()) return false;
        try { await EngineSessionAsync(page, eng).ConfigureAwait(false); }
        catch (Exception) { return false; }
        if (EngineExt.TryGetValue(eng.Key!, out var known)) return known.Value;
        bool ok;
        try
        {
            await eng.Session!.SendAsync(Forms.EngineKeySequence, new Dictionary<string, object>
            {
                ["targetId"] = eng.TargetId!,
                ["keys"] = Array.Empty<object>(),
            }).ConfigureAwait(false);
            ok = false;
        }
        catch (Exception e)
        {
            ok = IsInvalidParams(e);
        }
        EngineExt.AddOrUpdate(eng.Key!, new StrongBox<bool>(ok));
        MaybeReportExt(page, eng);
        return ok;
    }

    /// <summary>
    /// One engine pointer path to (x, y). <paramref name="ext"/>: r43 parameters (button, clickCount, modifiers,
    /// buttons, settle), only sent to an engine that takes them; on such an engine the path also starts where THIS
    /// SDK left the pointer (fromX/fromY) instead of requiring the engine to be in sync, and no sleep follows (it
    /// answers after its last event). A plain in-sync left click never probes: the r42 route is unchanged. False
    /// (nothing sent, or the route failed and is now off for the page): the caller's planner does the work.
    /// </summary>
    private static async Task<bool> EngineGlideAsync(IPage page, PointerState st, double x, double y, bool noClick,
        Dictionary<string, object>? ext = null)
    {
        var eng = EngineOf(page);
        if (!eng.Ok) return false;
        bool needExt = ext is not null || !EngineInSync(eng, st);
        bool useExt = needExt && await EngineExtOkAsync(page).ConfigureAwait(false);
        if (!useExt && needExt) return false;
        try
        {
            // This browser's engine already said it has neither command: no sessions, no probe.
            if (EngineHasNoPointerPath(eng.Key ?? EngineKeyOf(page))) { eng.Ok = false; return false; }
            await EngineSessionAsync(page, eng).ConfigureAwait(false);
            // The engine does not wait for the page: what it walks into a page that has not drawn yet is lost (a
            // fresh headless browser's first page, SC 216). Wait for two frames first; free once drawn. Best effort.
            await IsolatedWorld.For(page).EvaluatePromiseAsync(IsolatedWorld.Frames, FramesWaitMs).ConfigureAwait(false);
            double dx = x - st.X, dy = y - st.Y;
            double dist = Math.Sqrt(dx * dx + dy * dy);
            double dur = Math.Min(1.10, Math.Max(0.28, 0.30 + dist / 1700)) * (0.85 + 0.30 * Rnd.NextDouble()) * Tempo(page);
            var sent = Stopwatch.StartNew();
            var args = new Dictionary<string, object>
            {
                ["targetId"] = eng.TargetId!, ["x"] = x, ["y"] = y, ["duration"] = dur, ["noClick"] = noClick,
            };
            if (useExt)
            {
                // r43: start where this SDK left the pointer; before any move the engine enters over the top edge itself.
                if (st.Known && !EngineInSync(eng, st)) { args["fromX"] = st.X; args["fromY"] = st.Y; }
                foreach (var kv in ext ?? new Dictionary<string, object>()) args[kv.Key] = kv.Value;
            }
            await SendEngineCommandAsync(eng.Session!, eng.Key!, PointerPath, PointerPathLegacy, args).ConfigureAwait(false);
            MaybeReportExt(page, eng);
            // The engine walks the trajectory in the BROWSER process. r38 answers only after the walk AND the click
            // (send returned 772 ms after a 600 ms walk, 2 ms after the release, SC 210): wait only for what is left
            // of `dur`, so an engine that answers late does not add a second, idle `dur` after every click. r43
            // answers after its last event: nothing left to wait for.
            if (!useExt)
            {
                int left = (int)Math.Round(dur * 1000 - sent.Elapsed.TotalMilliseconds);
                if (left > 0) await Task.Delay(left).ConfigureAwait(false);
            }
            // keep Playwright's own cursor position in sync, so a following press lands where the engine left it
            try { await page.Mouse.MoveAsync((float)x, (float)y).ConfigureAwait(false); }
            catch (Exception) { /* best effort */ }
            st.X = x;
            st.Y = y;
            st.Known = true;
            eng.At = (x, y);
            eng.Stale = false;
            return true;
        }
        catch (Exception)
        {
            eng.Ok = false;
            return false;
        }
    }

    /// <summary>
    /// r43 engine (SC 232 lane H): a right/middle, double/triple or modified click walked AND pressed engine-side
    /// (dispatchPointerPath button / clickCount / modifiers); the modifier KEYS are pressed here around it (lead
    /// before, lag after), as <see cref="PressAtAsync"/> does. False when the engine cannot take it (a caller-chosen
    /// hold, no r43 extensions, engine "off"): the planner presses then.
    /// </summary>
    private static async Task<bool> EngineExtClickAsync(IPage page, PointerState st, double x, double y, NormalizedClick o)
    {
        var ext = Forms.EngineClickParams(o);
        if (ext is null || EngineModeFor(page) == "off" || !await EngineExtOkAsync(page).ConfigureAwait(false)) return false;
        var held = new List<string>();
        try
        {
            foreach (var m in o.Modifiers)
            {
                await page.Keyboard.DownAsync(m).ConfigureAwait(false);
                held.Add(m);
                await Task.Delay((int)Rand(40, 110)).ConfigureAwait(false);
            }
            return await EngineGlideAsync(page, st, x, y, false, ext).ConfigureAwait(false);
        }
        finally
        {
            for (int k = held.Count - 1; k >= 0; k--)
            {
                await Task.Delay((int)Rand(20, 80)).ConfigureAwait(false);
                try { await page.Keyboard.UpAsync(held[k]).ConfigureAwait(false); }
                catch (PlaywrightException) { /* best effort */ }
            }
        }
    }

    /// <summary>
    /// A coordinate click through the engine: the r42 route for a plain left click while the engine is in sync,
    /// else (r43) the extensions. False: the caller glides and presses SDK-side.
    /// </summary>
    private static async Task<bool> EngineClickAsync(IPage page, PointerState st, double x, double y, NormalizedClick o)
    {
        if (Forms.EnginePathOk(o) && EngineModeFor(page) != "off"
            && await EngineGlideAsync(page, st, x, y, false).ConfigureAwait(false)) return true;
        return await EngineExtClickAsync(page, st, x, y, o).ConfigureAwait(false);
    }

    /// <summary>
    /// r43 engine (SC 232 lane H): a leg walked while <paramref name="button"/> is held, engine-side, the buttons
    /// carried on every sample (and the seating jiggle with <paramref name="settle"/>). False when the engine cannot
    /// take it (engine "off", no r43 extensions): the caller walks the held leg natively.
    /// </summary>
    internal static async Task<bool> EngineHeldLegAsync(IPage page, double x, double y, string? button = "left", bool settle = false)
    {
        if (EngineModeFor(page) == "off" || !await EngineExtOkAsync(page).ConfigureAwait(false)) return false;
        var ext = new Dictionary<string, object> { ["buttons"] = Forms.EngineHeldBits(button) };
        if (settle) ext["settle"] = true;
        return await EngineGlideAsync(page, StateFor(page), x, y, true, ext).ConfigureAwait(false);
    }

    /// <summary>
    /// Type <paramref name="text"/> with ONE Browser.dispatchKeySequence (r43, SC 232 lane H): the persona's key dwell
    /// and the bigram flight per key (the caller's delay as the floor), paced in the browser. False when a character
    /// has no plain US key or the call fails up front (nothing typed yet: the per-key path types it).
    /// </summary>
    private static async Task<bool> EngineTypeAsync(IPage page, Persona p, string text, double floor)
    {
        var tr = TraitsFor(page);
        var keys = Forms.EngineKeyPlan(text, () => Motion.KeyDwell(p),
            (ch, nx) => Math.Max(floor, Keys.PlanGap(tr, ch, nx, Unseeded, Tempo(page), Tempo(page))), NeedsShift);
        if (keys is null) return false;
        var eng = EngineOf(page);
        try
        {
            await EngineSessionAsync(page, eng).ConfigureAwait(false);
            await eng.Session!.SendAsync(Forms.EngineKeySequence, new Dictionary<string, object>
            {
                ["targetId"] = eng.TargetId!,
                ["keys"] = keys,
            }).ConfigureAwait(false);
        }
        catch (Exception)
        {
            return false;
        }
        TraceEngineTyped(page, keys, floor);
        return true;
    }

    // The score hook (lane G) hears an engine typed run as one "typed" line of the PLANNED down/up times (the engine
    // paces them in the browser; the per-key path records its dispatched ones). Inside an outer traced run the keys
    // join that run's record instead (verifier A's R3c).
    private static void TraceEngineTyped(IPage page, List<Dictionary<string, object>> keys, double floor)
    {
        if (!HumanizeTrace.Enabled) return;
        var outer = KeysOf(page).Rec;
        double t = outer is not null ? Stopwatch.GetElapsedTime(KeyRecEpoch).TotalMilliseconds : 0;
        var rec = new List<KeySample>(keys.Count);
        foreach (var k in keys)
        {
            double down = t;
            double up = down + Convert.ToDouble(k["dwellMs"], System.Globalization.CultureInfo.InvariantCulture);
            rec.Add(new KeySample((string)k["key"], down, up));
            t = up + Convert.ToDouble(k["gapMs"], System.Globalization.CultureInfo.InvariantCulture);
        }
        if (outer is not null) outer.AddRange(rec);
        else HumanizeTrace.Record("typed", rec, new Dictionary<string, object?>
        {
            ["n"] = keys.Count, ["typos"] = false, ["floor_ms"] = floor, ["engine"] = true,
        });
    }
}
