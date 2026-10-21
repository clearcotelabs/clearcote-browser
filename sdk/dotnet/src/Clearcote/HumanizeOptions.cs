using System;
using System.Reflection;
using System.Runtime.CompilerServices;
using Microsoft.Playwright;

namespace Clearcote;

/// <summary>
/// The options of <see cref="Humanize.Attach(IPage, HumanizeOptions?, object?)"/>, with the meaning the Python and Node
/// SDKs give their <c>humanize=</c> options. <c>new HumanizeOptions()</c> is the 0.51.0 behaviour, and no option draws
/// from the persona's RNG stream: the same seed keeps the same motor persona whatever is set here.
/// </summary>
public sealed record HumanizeOptions
{
    /// <summary>"careful" | "default" | "quick": every planned duration (glides, presses, key dwell and gaps) x1.4 /
    /// x1.0 / x0.7, and the rate of thinking pauses scaled the same way.</summary>
    public string Preset { get; init; } = "default";

    /// <summary>Throw <see cref="HumanizeStrictException"/> instead of handing an action to Playwright's own call.</summary>
    public bool Strict { get; init; }

    /// <summary>"auto" | "all" | "coords" | "off": which clicks the engine's pointer path (Browser.dispatchPointerPath)
    /// walks. "auto" reads <see cref="Humanize.EngineClicksEnv"/> (default "coords"); "coords" coordinate clicks
    /// (<c>page.HumanClickAsync(x, y)</c>) only; "all" targeted selector / locator clicks too; "off" never, and no
    /// engine typed run (Browser.dispatchKeySequence) either: everything stays on the SDK planner.</summary>
    public string Engine { get; init; } = "auto";

    /// <summary>"off" | "load" | "idle" (null = "off"): pointer activity without a target, read by the ambient code.</summary>
    public string? Ambient { get; init; }

    /// <summary>Per-character slip rate 0..1; null = <see cref="Humanize.TypoRateEnv"/>, else 0.02.</summary>
    public double? TypoRate { get; init; }

    /// <summary>"left" | "right"; null = the persona's.</summary>
    public string? Handedness { get; init; }

    /// <summary>The pointer's report rate (Hz) in place of the persona's.</summary>
    public int? DeviceHz { get; init; }

    /// <summary>"mouse" | "trackpad"; null = the persona's.</summary>
    public string? InputDevice { get; init; }

    /// <summary>Called each time an action is handed to Playwright's own call (not in strict mode). Throwing from it
    /// stops the stock call.</summary>
    public Action<HumanizeFallbackEvent>? OnFallback { get; init; }

    /// <summary>The duration multiplier of <see cref="Preset"/>.</summary>
    public double Tempo => Preset switch { "careful" => 1.4, "quick" => 0.7, _ => 1.0 };

    /// <summary>Strict mode, all else default (Python's <c>humanize="strict"</c>).</summary>
    public static HumanizeOptions StrictMode => new() { Strict = true };

    /// <summary>Throws <see cref="ArgumentException"/> on a value the other SDKs would refuse.</summary>
    public void Validate()
    {
        static void OneOf(string name, string? v, params string[] allowed)
        {
            if (v is not null && Array.IndexOf(allowed, v) < 0)
                throw new ArgumentException($"humanize {name} must be {string.Join(", ", allowed)}, not \"{v}\"", name);
        }
        OneOf(nameof(Preset), Preset, "careful", "default", "quick");
        OneOf(nameof(Engine), Engine, "auto", "all", "coords", "off");
        OneOf(nameof(Ambient), Ambient, "off", "load", "idle");
        OneOf(nameof(Handedness), Handedness, "left", "right");
        OneOf(nameof(InputDevice), InputDevice, "mouse", "trackpad");
        if (Preset is null) throw new ArgumentException("humanize Preset must be set", nameof(Preset));
        if (TypoRate is { } t && !(t >= 0 && t <= 1)) throw new ArgumentException("humanize TypoRate must be from 0 to 1", nameof(TypoRate));
        if (DeviceHz is { } h && (h < 30 || h > 8000)) throw new ArgumentException("humanize DeviceHz must be from 30 to 8000", nameof(DeviceHz));
    }
}

/// <summary>One action handed to Playwright's own call: the method, why, and the locator it was for.</summary>
public sealed record HumanizeFallbackEvent(string Api, string Reason, string? Selector);

/// <summary>
/// Thrown in strict mode (<see cref="HumanizeOptions.Strict"/>) where a humanized method would have handed the action
/// to Playwright's own (stock, untimed) call. A <see cref="PlaywrightException"/>, so code that already catches
/// Playwright's errors catches it too.
/// </summary>
public sealed class HumanizeStrictException : PlaywrightException
{
    public string Api { get; }
    public string Reason { get; }
    public string? Selector { get; }

    public HumanizeStrictException(string api, string reason, string? selector)
        : base($"{api}: humanize strict: {(selector is null ? "the action" : "\"" + selector + "\"")} would fall back to a stock Playwright call ({reason})")
    {
        Api = api;
        Reason = reason;
        Selector = selector;
    }
}

public static partial class Humanize
{
    private static readonly ConditionalWeakTable<IPage, HumanizeOptions> Options = new();
    // The preset's tempo per persona object (each attached page gets its own persona copy).
    private static readonly ConditionalWeakTable<Persona, StrongBox<double>> PersonaTempo = new();

    /// <summary>
    /// Attach a motor persona and the humanize options to a page. <paramref name="options"/> null is
    /// <c>new HumanizeOptions()</c>; <paramref name="seed"/> as for <see cref="Attach(IPage, object?)"/>. The options
    /// never change the persona's draws: they are applied on a copy (timing windows x tempo, DeviceHz).
    /// </summary>
    public static void Attach(IPage page, HumanizeOptions? options, object? seed = null)
    {
        ArgumentNullException.ThrowIfNull(page);
        var o = options ?? new HumanizeOptions();
        o.Validate();
        Options.AddOrUpdate(page, o);
        Personas.AddOrUpdate(page, ApplyOptions(Motion.MakePersona(seed), o));
        // As Attach(page, seed): the attention read pause counts from the last load, and Ambient = "load" runs
        // its burst after it (SC 232 lane C).
        HookLoad(page);
    }

    /// <summary>The page's humanize options (null when none were attached: the defaults apply).</summary>
    public static HumanizeOptions? OptionsFor(IPage page)
    {
        ArgumentNullException.ThrowIfNull(page);
        return Options.TryGetValue(page, out var o) ? o : null;
    }

    private static double Tempo(IPage page) => Options.TryGetValue(page, out var o) ? o.Tempo : 1.0;

    /// <summary>The preset's tempo on a persona copy made by <see cref="ApplyOptions"/> (1.0 on any other persona,
    /// so every vector is unchanged): Motion's timing medians and Attention's read pause are scaled by it.</summary>
    internal static double TempoOf(Persona p) => PersonaTempo.TryGetValue(p, out var t) ? t.Value : 1.0;

    /// <summary>A copy of <paramref name="p"/> with the options applied: no RNG draw, so the seed's persona is unchanged
    /// apart from what the options say (the timing windows scaled by the preset, DeviceHz replaced).</summary>
    internal static Persona ApplyOptions(Persona p, HumanizeOptions o)
    {
        double t = o.Tempo;
        if (t == 1.0 && o.DeviceHz is null && o.Handedness is null) return p;
        var copy = (Persona)typeof(object).GetMethod("MemberwiseClone", BindingFlags.Instance | BindingFlags.NonPublic)!
            .Invoke(p, null)!;
        void Set(string name, double v) => typeof(Persona).GetProperty(name)!.SetValue(copy, v);
        if (t != 1.0)
        {
            foreach (var name in new[] { nameof(Persona.GrabMinMs), nameof(Persona.GrabMaxMs), nameof(Persona.ReleaseMinMs),
                nameof(Persona.ReleaseMaxMs), nameof(Persona.ClickHoldMinMs), nameof(Persona.ClickHoldMaxMs),
                nameof(Persona.KeyDwellMinMs), nameof(Persona.KeyDwellMaxMs) })
                Set(name, (double)typeof(Persona).GetProperty(name)!.GetValue(copy)! * t);
            PersonaTempo.AddOrUpdate(copy, new StrongBox<double>(t));
        }
        if (o.DeviceHz is { } hz) Set(nameof(Persona.DeviceHz), hz);
        // the persona's handedness is the sign of its approach bias (path bow side, click side bias): the option
        // fixes the sign and keeps the drawn magnitude ("right" = positive)
        if (o.Handedness is "left" or "right")
            Set(nameof(Persona.ApproachBias), o.Handedness == "right" ? Math.Abs(copy.ApproachBias) : -Math.Abs(copy.ApproachBias));
        return copy;
    }

    /// <summary>
    /// One humanized action is about to be handed to Playwright's own call: tell <see cref="HumanizeOptions.OnFallback"/>
    /// (its exception propagates and stops the stock call), or, in strict mode, throw <see cref="HumanizeStrictException"/>.
    /// Returns true so a call site reads <c>goNative = Report(...)</c>.
    /// </summary>
    private static bool Report(IPage page, string api, string why, string? selector = null)
    {
        FallbackTrace?.Invoke(api + ": " + why);
        if (Options.TryGetValue(page, out var o))
        {
            if (o.Strict) throw new HumanizeStrictException(api, why, selector);
            o.OnFallback?.Invoke(new HumanizeFallbackEvent(api, why, selector));
        }
        return true;
    }

    /// <summary>Errors that leave a humanized route as they are: Playwright's timeout, and strict mode's refusal.</summary>
    private static bool Propagates(Exception e) => IsTimeoutError(e) || e is HumanizeStrictException;

    private static string? SelectorOf(ILocator loc)
    {
        try { return loc.ToString(); } catch { return null; }
    }
}
