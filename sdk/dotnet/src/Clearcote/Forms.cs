using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Clearcote;

// Pure planning for humanized form input: a 1:1 port of sdk/python/clearcote/_forms.py (Node:
// sdk/node/src/forms.ts). Nothing here touches a page: it decides WHAT a person would press for a form
// control, and Humanize decides HOW (glides, holds, cadence). Keeping the decisions pure is what lets one
// set of test vectors (sdk/testdata/humanize_forms_vectors.json) pin Python, Node and .NET to identical
// answers.
//
// What a person does, measured on r37 headless (SC 197 forms probes, 2026-10-11):
//   * <select> (a drop-down): a click opens the popup; letters typed while it is open move the popup's
//     highlight and change NOTHING on the page; Enter commits -> exactly one trusted input + change.
//     Typing on a focused but CLOSED select instead fires one change per letter, and stepping it with
//     arrows fires one change per row.
//   * list boxes (size > 1 or multiple): a click on a row selects it, Ctrl+click adds a row.
//   * <input type=range>: a click on the track jumps to that fraction, arrows step by `step`.
//   * date / time / month / week / datetime-local: the editor's segments come in the BROWSER's order,
//     so the order is read from the editor itself, never assumed. Numeric segments take digits and move
//     on after a full field; the year never moves on by itself (ArrowRight does); a month NAME field
//     (type=month) ignores digits and steps on arrows; the ampm field takes A / P.

/// <summary>What the typo gate knows about a field (any part may be missing). <see cref="Frame"/>: focus is
/// inside a frame the isolated world cannot read (the gate then fails closed).</summary>
internal sealed record FieldTraits(string? Type = null, string? InputMode = null, string? Autocomplete = null, bool Frame = false);

/// <summary>A planned fat-finger slip: "sub" (Wrong typed instead, noticed after Late more keys),
/// "double" (the key pressed twice) or "swap" (the next key typed first).</summary>
internal sealed record TypoPlan(string Kind, string? Wrong = null, int Late = 0);

/// <summary>Click options after normalisation (Playwright's click/dblclick/hover options).</summary>
internal sealed record NormalizedClick(
    string Button, int ClickCount, IReadOnlyList<string> Modifiers, double? Delay,
    (double X, double Y)? Position, bool Force, bool Trial);

/// <summary>Keys that move an OPEN drop-down's highlight: letters to type, then signed ArrowDown presses.</summary>
internal sealed record SelectPlan(string Keys, int Arrows);

/// <summary>One &lt;option&gt; as the form routes see it.</summary>
internal sealed record OptionInfo(string? Value, string? Label, bool Disabled = false, bool Selected = false);

/// <summary>A parsed date-like value. Absent parts are null, so record equality is Python's dict equality.</summary>
internal sealed record DateParts(
    int? Year = null, int? Month = null, int? Day = null, int? Week = null,
    int? Hour = null, int? Minute = null, int? Second = null, int? Millisecond = null);

/// <summary>One key action for a date-like editor: ("type", digits) or ("key", name[, count]).</summary>
internal sealed record DtAction(string Op, string Arg, int? Count = null)
{
    public override string ToString() => Count is int n ? $"({Op},{Arg},{n})" : $"({Op},{Arg})";
}

internal static class Forms
{
    // ------------------------------------------------------------------------- field classes
    // The fill routes. Playwright's own split (kInputTypesToSetValue / kInputTypesToTypeInto): text-like
    // types are typed into, value types have their value set, everything else raises.
    internal static readonly IReadOnlySet<string> TextTypes =
        new HashSet<string> { "", "text", "search", "email", "password", "tel", "url", "number" };
    internal static readonly IReadOnlySet<string> DatetimeTypes =
        new HashSet<string> { "date", "time", "month", "week", "datetime-local" };
    // color: Playwright sets it; the only humane route is the OS colour dialog, which no page sees and
    // no script can drive -- so native, exactly as Playwright behaves.
    internal static readonly IReadOnlySet<string> NativeValueTypes = new HashSet<string> { "color" };
    private static readonly IReadOnlySet<string> UnfillableInputs =
        new HashSet<string> { "checkbox", "radio", "file", "submit", "button", "image", "reset", "hidden" };

    /// <summary>"text" | "range" | "datetime" | "native" | "unfillable" for one element. "native" = let
    /// Playwright set the value; "unfillable" = let Playwright's fill raise exactly as it would have.</summary>
    public static string ClassifyField(string? tag, string? inputType = null, bool contenteditable = false)
    {
        var t0 = (tag ?? "").ToLowerInvariant();
        if (contenteditable && t0 != "input" && t0 != "select") return "text";
        if (t0 == "textarea") return "text";
        if (t0 != "input") return "unfillable";
        var t = (inputType ?? "").ToLowerInvariant();
        if (TextTypes.Contains(t)) return "text";
        if (t == "range") return "range";
        if (DatetimeTypes.Contains(t)) return "datetime";
        if (NativeValueTypes.Contains(t)) return "native";
        // An unknown type is a text input to the browser (HTML: invalid type -> text).
        return UnfillableInputs.Contains(t) ? "unfillable" : "text";
    }

    // ----------------------------------------------------------------------------------- typos
    // No fat-finger typos where a wrong character is not a harmless slip but a validation failure or a
    // locked field: credentials, numbers, addresses, one-time codes.
    internal static readonly IReadOnlySet<string> NoTypoTypes =
        new HashSet<string> { "password", "email", "number", "tel", "url" };
    internal static readonly IReadOnlySet<string> NoTypoInputModes =
        new HashSet<string> { "numeric", "decimal", "tel", "email", "url" };
    internal static readonly IReadOnlySet<string> NoTypoAutocomplete =
        new HashSet<string> { "one-time-code", "cc-number", "cc-csc", "cc-exp", "current-password", "new-password" };

    // Compact QWERTY adjacency for letter typos; digits slip to a NEIGHBOURING DIGIT on the number row
    // (a digit never turns into a letter, and never into itself).
    internal static readonly IReadOnlyDictionary<string, string> NearbyLetters = new Dictionary<string, string>
    {
        ["a"] = "sqwz", ["b"] = "vghn", ["c"] = "xdfv", ["d"] = "sfecx", ["e"] = "wrsdf", ["f"] = "dgrtcv",
        ["g"] = "fhtyb", ["h"] = "gjybn", ["i"] = "ujko", ["j"] = "hkunm", ["k"] = "jloi", ["l"] = "kop",
        ["m"] = "njk", ["n"] = "bhjm", ["o"] = "iklp", ["p"] = "ol", ["q"] = "wa", ["r"] = "edft",
        ["s"] = "awedxz", ["t"] = "rfgy", ["u"] = "yhji", ["v"] = "cfgb", ["w"] = "qase", ["x"] = "zsdc",
        ["y"] = "tghu", ["z"] = "asx",
    };
    internal static readonly IReadOnlyDictionary<string, string> NearbyDigits = new Dictionary<string, string>
    {
        ["1"] = "2", ["2"] = "13", ["3"] = "24", ["4"] = "35", ["5"] = "46", ["6"] = "57", ["7"] = "68",
        ["8"] = "79", ["9"] = "80", ["0"] = "9",
    };

    public const double DefaultTypoRate = 0.02;

    /// <summary>May a fat-finger typo happen in this field? Unknown fields (null) allow typos.</summary>
    public static bool TyposAllowed(FieldTraits? field)
    {
        if (field is null) return true;
        if (field.Frame) return false;   // focus is inside a frame we cannot read: fail closed
        var t = (field.Type ?? "").ToLowerInvariant();
        if (NoTypoTypes.Contains(t)) return false;
        if (NoTypoInputModes.Contains((field.InputMode ?? "").ToLowerInvariant())) return false;
        var ac = (field.Autocomplete ?? "").ToLowerInvariant()
            .Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        return !ac.Any(NoTypoAutocomplete.Contains);
    }

    /// <summary>Characters a typo may replace: unshifted ASCII letters and digits (one character).</summary>
    public static bool TypoEligible(string? ch)
        => ch is { Length: 1 } && ((ch[0] >= 'a' && ch[0] <= 'z') || (ch[0] >= '0' && ch[0] <= '9'));

    private static string Choice(string seq, Func<double> rng)
        => seq[Math.Min(seq.Length - 1, (int)(rng() * seq.Length))].ToString();

    /// <summary>A neighbouring key for <paramref name="ch"/> (letter -> letter, digit -> digit), or null.</summary>
    public static string? NearbyKey(string ch, Func<double>? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        if (NearbyLetters.TryGetValue(ch, out var l)) return Choice(l, rng);
        if (NearbyDigits.TryGetValue(ch, out var d)) return Choice(d, rng);
        return null;
    }

    /// <summary>The characters of <paramref name="text"/> as the typing loop sees them: one per code point.</summary>
    public static string[] Chars(string text) => text.EnumerateRunes().Select(r => r.ToString()).ToArray();

    /// <inheritdoc cref="PlanTypo(IReadOnlyList{string},int,Func{double},double)"/>
    public static TypoPlan? PlanTypo(string text, int i, Func<double>? rng = null, double rate = DefaultTypoRate)
        => PlanTypo(Chars(text), i, rng, rate);

    /// <summary>Decide whether character <paramref name="i"/> gets a typo, and which kind: null, or
    /// sub (a neighbouring key, noticed after 0-2 more correct characters), double (pressed twice; one
    /// Backspace) or swap (the next character typed first; two Backspaces). The humanize layer still
    /// checks that each wrong key actually landed in the field before it deletes anything.</summary>
    public static TypoPlan? PlanTypo(IReadOnlyList<string> text, int i, Func<double>? rng = null, double rate = DefaultTypoRate)
    {
        rng ??= Random.Shared.NextDouble;
        var ch = text[i];
        if (!TypoEligible(ch) || rate <= 0 || rng() >= rate) return null;
        double r = rng();
        string? nxt = i + 1 < text.Count ? text[i + 1] : null;
        if (r < 0.15 && nxt is not null && TypoEligible(nxt) && nxt != ch) return new TypoPlan("swap");
        if (r < 0.30) return new TypoPlan("double");
        int late = 0;
        if (rng() < 0.3)
        {
            // Late notice: only over characters that are themselves plain unshifted keys, so the retyping
            // after the Backspaces stays on the simple path.
            int k = rng() < 0.7 ? 1 : 2;
            var tail = text.Skip(i + 1).Take(k).ToList();
            if (tail.Count == k && tail.All(c => TypoEligible(c) || c == " ")) late = k;
        }
        return new TypoPlan("sub", NearbyKey(ch, rng), late);
    }

    /// <summary>Is <paramref name="got"/> just <paramref name="want"/> re-punctuated by an input mask -- the
    /// same letters and digits in the same order, with separators, spaces or case changed ("(555) 123-4567"
    /// for "5551234567")? A value only a formatter changed counts as typed right; a lost or an extra key
    /// does not. Typing it again would not change what the formatter does.</summary>
    public static bool FormattedEqual(string? got, string? want)
    {
        if (got is null || want is null) return false;
        var a = Chars(got).Where(IsAlnum).Select(CaseFold).ToList();
        var b = Chars(want).Where(IsAlnum).Select(CaseFold).ToList();
        return b.Count > 0 && a.SequenceEqual(b);
    }

    // Python's str.isalnum() for one code point: a letter or a digit of any script.
    private static bool IsAlnum(string ch) => Rune.TryGetRuneAt(ch, 0, out var r) && (Rune.IsLetterOrDigit(r) || Rune.IsNumber(r));

    // ------------------------------------------------------------------------------- maxlength
    /// <summary>The input types whose maxlength the browser enforces on what is typed or inserted (plus textarea).</summary>
    public static readonly IReadOnlySet<string> MaxlengthTypes = new HashSet<string> { "", "text", "search", "url", "tel", "email", "password" };

    /// <summary>
    /// The part of <paramref name="value"/> an EMPTY control with <paramref name="maxlength"/> ends up holding:
    /// what Playwright's fill (Input.insertText) leaves there, and what a person types before the field stops
    /// taking keys. maxlength counts UTF-16 code units, as Blink does; a character that would be cut in half
    /// (beyond the BMP) is dropped whole. A negative or missing maxlength, a contenteditable and a type that
    /// ignores maxlength keep the whole value. Port of _forms.fit_maxlength (r38: fill("1234567") on
    /// maxlength=5 typed seven digits, wiped the field, typed seven again, then fell back to Playwright).
    /// </summary>
    public static string FitMaxlength(string value, int? maxlength, string? tag = "input", string? inputType = "")
    {
        int n = maxlength ?? -1;
        var t = (tag ?? "").ToLowerInvariant();
        if (n < 0 || (t != "input" && t != "textarea")) return value;
        if (t == "input" && !MaxlengthTypes.Contains((inputType ?? "").ToLowerInvariant())) return value;
        int units = 0;
        var sb = new StringBuilder();
        foreach (var r in value.EnumerateRunes())
        {
            units += r.Utf16SequenceLength;
            if (units > n) return sb.ToString();
            sb.Append(r.ToString());
        }
        return value;
    }

    /// <summary>
    /// Is <paramref name="got"/> what a formatting mask leaves of <paramref name="want"/> once the field stops
    /// taking keys -- what a person typing <paramref name="want"/> ends up with too, so typing it again (or
    /// Playwright's fill, which the same mask cuts the same way) changes nothing? Its letters and digits are a
    /// shorter prefix of <paramref name="want"/>'s (<see cref="FormattedEqual"/> covers the whole value) AND
    /// either the field is full (<paramref name="maxlength"/> UTF-16 units: MM/YY on maxlength 5 holds "12/34"
    /// of "12345") or the page ADDED punctuation that was not typed there ("1234 AB" of "1234abx"). A field
    /// that only changes case ("abc12" of "ABC123") or strips separators ("555123456" of "555-123-4567") shows
    /// no mask that cuts, so a missing last letter or digit there is a lost key. A value cut short with no
    /// punctuation added and room left is a lost key: false, and the caller types it again. Port of
    /// _forms.mask_kept (verifier B, SC 210: MM/YY and a 12-place phone mask were typed, wiped with Ctrl+A
    /// Backspace and typed again).
    /// </summary>
    public static bool MaskKept(string? got, string? want, int? maxlength = null, string? tag = "input", string? inputType = "")
    {
        if (string.IsNullOrEmpty(got) || want is null) return false;
        var a = Chars(got).Where(IsAlnum).Select(CaseFold).ToList();
        var b = Chars(want).Where(IsAlnum).Select(CaseFold).ToList();
        if (a.Count == 0 || a.Count >= b.Count || !a.SequenceEqual(b.Take(a.Count))) return false;
        int n = maxlength ?? -1;
        var t = (tag ?? "").ToLowerInvariant();
        if (n >= 0 && (t == "textarea" || (t == "input" && MaxlengthTypes.Contains((inputType ?? "").ToLowerInvariant())))
            && got.Length >= n) return true;   // string length is UTF-16 units, as maxlength counts
        var g = SepRuns(got);
        var w = SepRuns(want);
        return g.Select((run, i) => Chars(run).Any(c => !w[i].Contains(c, StringComparison.Ordinal))).Any(x => x);
    }

    // The separators of s by place: run i stands before its i-th letter or digit, the last trails ("12/3" -> "", "", "/", "").
    private static List<string> SepRuns(string s)
    {
        var runs = new List<string>();
        var cur = new StringBuilder();
        foreach (var c in Chars(s))
        {
            if (IsAlnum(c)) { runs.Add(cur.ToString()); cur.Clear(); }
            else cur.Append(c);
        }
        runs.Add(cur.ToString());
        return runs;
    }

    // ------------------------------------------------------------------------------ pre-press pause
    /// <summary>Lower bound of the pause between the pointer coming to rest and the button going down (ms).</summary>
    public const double PrePressMinMs = 40;
    /// <summary>Upper bound of the pre-press pause (ms).</summary>
    public const double PrePressMaxMs = 250;
    // Salt for the per-identity centre (same value in _forms.py and forms.ts).
    private const uint PrePressSalt = 0x5BD1E995;

    /// <summary>The identity's typical pre-press pause (ms), 70..135, stable per persona seed.</summary>
    public static double PrePressCenter(uint seed)
    {
        double r = Motion.Mulberry32(seed ^ PrePressSalt)();
        return 70 + (135 - 70) * r;
    }

    /// <summary>
    /// One pre-press pause (ms): a log-normal above the 40 ms floor with its median at the identity's centre
    /// (sigma 0.55, right-skewed); a draw past 250 ms is drawn again (up to four times, then capped). Port of
    /// _forms.pre_press_ms. Measured on r38 before it: the focus click before typing pressed ~4 ms after the
    /// last pointermove, a drag grab ~21 ms.
    /// </summary>
    public static double PrePressMs(uint seed, Rng? rng = null)
    {
        rng ??= Random.Shared.NextDouble;
        double c = PrePressCenter(seed) - PrePressMinMs;
        for (int k = 0; k < 4; k++)
        {
            double ms = PrePressMinMs + c * Math.Exp(0.55 * Motion.GaussFrom(rng));
            if (ms <= PrePressMaxMs) return ms;
        }
        return PrePressMaxMs;
    }

    // ------------------------------------------------------------------------------ select-all
    /// <summary>The select-all chord a person on <paramref name="platform"/> uses: Cmd+A on a Mac, Ctrl+A elsewhere.</summary>
    public static string SelectAllCombo(string? platform)
        => (platform ?? "").ToLowerInvariant() is "macos" or "mac" or "darwin" ? "Meta+a" : "Control+a";

    // ------------------------------------------------------------------------------ click options
    private static readonly string[] KnownModifiers = { "Alt", "Control", "ControlOrMeta", "Meta", "Shift" };

    /// <summary>Playwright click/dblclick/hover options -> what the humanized press needs. "ControlOrMeta"
    /// is kept as is: Playwright resolves it to the key the ENGINE's platform uses.</summary>
    public static NormalizedClick NormalizeClickOptions(
        string? button = null, int? clickCount = null, double? delay = null, IEnumerable<string>? modifiers = null,
        (double X, double Y)? position = null, bool force = false, bool trial = false)
    {
        var b = string.IsNullOrEmpty(button) ? "left" : button;
        if (b != "left" && b != "right" && b != "middle") b = "left";
        int count = clickCount is int c && c != 0 ? c : 1;
        var mods = new List<string>();
        foreach (var m in modifiers ?? Array.Empty<string>())
            if (KnownModifiers.Contains(m) && !mods.Contains(m)) mods.Add(m);
        // delay=0 is Playwright's DEFAULT ("no hold"), not a request for a 1 ms press: it, and anything
        // non-positive or non-finite, means "use the persona's hold" (a 1-4 ms button hold is a tell, and a
        // negative hold left the button down: Task.Delay(-1) waits forever).
        if (delay is double d && (!double.IsFinite(d) || d <= 0)) delay = null;
        return new NormalizedClick(b, Math.Max(1, count), mods, delay, position, force, trial);
    }

    /// <summary>The engine pointer path performs one plain left click and nothing else.</summary>
    public static bool EnginePathOk(NormalizedClick o)
        => o.Button == "left" && o.ClickCount == 1 && o.Modifiers.Count == 0 && o.Delay is null;

    // r43 engine (SC 232 lane H): Browser.dispatchPointerPath also takes fromX/fromY, button, clickCount, modifiers,
    // buttons (held) and settle, and answers after its last event; Browser.dispatchKeySequence types a paced run.
    internal const string EngineKeySequence = "Browser.dispatchKeySequence";
    private static readonly Dictionary<string, int> CdpModifierBits = new() { ["Alt"] = 1, ["Control"] = 2, ["Meta"] = 4, ["Shift"] = 8 };
    private static readonly Dictionary<string, int> ButtonBits = new() { ["left"] = 1, ["right"] = 2, ["middle"] = 4 };

    /// <summary>Extra dispatchPointerPath parameters for a click an r43 engine performs itself (empty = plain left
    /// click), or null (a caller-chosen hold, an unknown button, more than 3 clicks). Modifier KEYS are still pressed
    /// by the SDK around the call (the engine only sets the event flags). Same as Node engineClickParams.</summary>
    internal static Dictionary<string, object>? EngineClickParams(NormalizedClick o)
    {
        if (o.Delay is not null || !ButtonBits.ContainsKey(o.Button) || o.ClickCount < 1 || o.ClickCount > 3) return null;
        var p = new Dictionary<string, object>();
        if (o.Button != "left") p["button"] = o.Button;
        if (o.ClickCount != 1) p["clickCount"] = o.ClickCount;
        int bits = 0;
        foreach (var m in o.Modifiers) bits |= CdpModifierBits.TryGetValue(m, out var b) ? b : 0;
        if (bits != 0) p["modifiers"] = bits;
        return p;
    }

    /// <summary>dispatchPointerPath <c>buttons</c> for a held button name.</summary>
    internal static int EngineHeldBits(string? button) => ButtonBits.TryGetValue(string.IsNullOrEmpty(button) ? "left" : button, out var b) ? b : 1;

    // US-layout DOM codes for the punctuation keys (letters, digits and space need none: the engine knows them).
    private static readonly Dictionary<string, string> UsCodes = new()
    {
        [","] = "Comma", ["."] = "Period", ["/"] = "Slash", [";"] = "Semicolon", ["'"] = "Quote", ["["] = "BracketLeft",
        ["]"] = "BracketRight", ["-"] = "Minus", ["="] = "Equal", ["`"] = "Backquote", ["\\"] = "Backslash",
        ["<"] = "Comma", [">"] = "Period", ["?"] = "Slash", [":"] = "Semicolon", ["\""] = "Quote", ["{"] = "BracketLeft",
        ["}"] = "BracketRight", ["_"] = "Minus", ["+"] = "Equal", ["~"] = "Backquote", ["|"] = "Backslash",
        ["!"] = "Digit1", ["@"] = "Digit2", ["#"] = "Digit3", ["$"] = "Digit4", ["%"] = "Digit5", ["^"] = "Digit6",
        ["&"] = "Digit7", ["*"] = "Digit8", ["("] = "Digit9", [")"] = "Digit0",
    };

    /// <summary>Browser.dispatchKeySequence keys for <paramref name="text"/> (US layout), or null when a character has
    /// no plain key. <paramref name="gapMs"/>(ch, next) is the caller's cadence model (lane D's bigram flight), after
    /// every key but the last; Shift (modifiers 8) for the characters <paramref name="needsShift"/> says. Same as Node
    /// engineKeyPlan.</summary>
    internal static List<Dictionary<string, object>>? EngineKeyPlan(string text, Func<double> dwellMs,
        Func<string, string, double> gapMs, Func<string, bool> needsShift)
    {
        var chars = Chars(text);
        var keys = new List<Dictionary<string, object>>(chars.Length);
        for (int i = 0; i < chars.Length; i++)
        {
            var ch = chars[i];
            Dictionary<string, object> k;
            if (ch == "\n") k = new() { ["key"] = "Enter", ["code"] = "Enter" };
            else if (ch == "\t") k = new() { ["key"] = "Tab", ["code"] = "Tab" };
            else if (ch.Length == 1 && (char.IsAsciiLetterOrDigit(ch[0]) || ch[0] == ' ')) k = new() { ["key"] = ch };
            else if (UsCodes.TryGetValue(ch, out var code)) k = new() { ["key"] = ch, ["code"] = code };
            else return null;
            k["dwellMs"] = dwellMs();
            k["gapMs"] = i < chars.Length - 1 ? gapMs(ch, chars[i + 1]) : 0.0;
            if (needsShift(ch)) k["modifiers"] = 8;
            keys.Add(k);
        }
        return keys;
    }

    // ------------------------------------------------------------------- <select> type-ahead plan
    /// <summary>Blink compares FoldCase()d labels with leading whitespace stripped (type_ahead.cc).</summary>
    internal static string Fold(string? s) => CaseFold((s ?? "").TrimStart());

    // Python's str.casefold(): lower case plus the one full folding a Latin label can hit (ß -> ss).
    private static string CaseFold(string s) => s.ToLowerInvariant().Replace("ß", "ss", StringComparison.Ordinal);

    // A space is typeable too ("Item 371" of 400 "Item NNN" rows): without it the prefix stopped at "item"
    // and the other 370 rows were arrows. It is never the FIRST key (see PlanSelect).
    internal const string Typeable = "abcdefghijklmnopqrstuvwxyz0123456789 ";

    /// <summary>Where Blink's TypeAhead (kMatchPrefix | kCycleFirstChar) lands after <paramref name="keys"/>.
    /// <paramref name="labels"/>: one per option, null for a disabled option (never matches).
    /// <paramref name="start"/>: the highlighted index when typing begins (-1 = none). Returns the landing
    /// index (unchanged if no match). A first key, or the same key again, searches for that ONE character
    /// starting AFTER the current item; a mixed buffer searches for the whole buffer starting AT it.</summary>
    public static int TypeaheadSimulate(IReadOnlyList<string?> labels, int start, string keys)
    {
        int n = labels.Count;
        if (n == 0) return start;
        var folded = labels.Select(l => l is null ? null : Fold(l)).ToArray();
        var buf = "";
        string? repeating = null;
        int cur = start;
        foreach (var k in keys)
        {
            var c = CaseFold(k.ToString());
            buf += c;
            int offset = 1;
            string prefix;
            if (c == repeating) prefix = c;
            else
            {
                prefix = buf;
                if (buf.Length > 1) { repeating = null; offset = 0; }
                else repeating = c;
            }
            int idx = ((cur >= 0 ? cur : 0) + offset) % n;
            for (int j = 0; j < n; j++)
            {
                var lab = folded[idx];
                if (!string.IsNullOrEmpty(lab) && lab.StartsWith(prefix, StringComparison.Ordinal))
                {
                    cur = idx;
                    break;
                }
                idx = (idx + 1) % n;
            }
        }
        return cur;
    }

    /// <summary>Arrow presses from row <paramref name="frm"/> to row <paramref name="to"/> in the popup,
    /// which skips disabled rows (signed). Each press lands on the next enabled row, so the count is the
    /// enabled rows passed through INCLUDING the target: (frm, to] going down, [to, frm) going up -- the
    /// start row itself never counts, which is what makes a move away from a disabled current row right.</summary>
    internal static int? EnabledSteps(IReadOnlyList<string?> labels, int frm, int to)
    {
        if (frm == to) return 0;
        if (labels[to] is null) return null;
        int steps = 0;
        if (to > frm)
        {
            for (int i = frm + 1; i <= to; i++) if (labels[i] is not null) steps++;
            return steps;
        }
        for (int i = to; i < frm; i++) if (labels[i] is not null) steps++;
        return -steps;
    }

    /// <summary>Keys that move an OPEN drop-down's highlight from <paramref name="current"/> to
    /// <paramref name="target"/> with the fewest presses: the shortest typeable prefix type-ahead lands on
    /// exactly, else the cheapest (prefix, then arrows) combination, else arrows alone. Null when the
    /// target is disabled (no route selects it).</summary>
    public static SelectPlan? PlanSelect(IReadOnlyList<string?> labels, int current, int target, int maxPrefix = 24)
    {
        if (!(target >= 0 && target < labels.Count) || labels[target] is null) return null;
        SelectPlan? best = null;
        int start = current >= 0 && current < labels.Count ? current : -1;
        int? @base = start >= 0 ? EnabledSteps(labels, start, target) : null;
        if (@base is int b) best = new SelectPlan("", b);
        else if (start < 0)
        {
            // No selection: the popup highlights the first row; arrows count from there.
            int first = -1;
            for (int i = 0; i < labels.Count; i++) if (labels[i] is not null) { first = i; break; }
            if (first >= 0 && EnabledSteps(labels, first, target) is int st0) best = new SelectPlan("", st0);
        }
        var label = Fold(labels[target]);
        var prefix = "";
        foreach (var ch in label.Length > maxPrefix ? label[..Math.Max(0, maxPrefix)] : label)
        {
            if (!Typeable.Contains(ch) || (ch == ' ' && prefix.Length == 0)) break;
            prefix += ch;
            int land = TypeaheadSimulate(labels, start, prefix);
            if (land < 0 || labels[land] is null) continue;
            if (EnabledSteps(labels, land, target) is not int st) continue;
            int cost = prefix.Length + Math.Abs(st);
            int? bestCost = best is null ? null : best.Keys.Length + Math.Abs(best.Arrows);
            // fewest presses; on a tie, typing the name outright beats typing and then arrowing
            if (best is null || cost < bestCost || (cost == bestCost && st == 0 && best.Arrows != 0))
                best = new SelectPlan(prefix, st);
            if (st == 0) break;
        }
        return best;
    }

    /// <summary>Playwright's option matching for ONE wanted option: a string <paramref name="value"/>
    /// matches an option's value OR its label; <paramref name="label"/> the label; <paramref name="index"/>
    /// the position. Returns the index or -1.</summary>
    public static int MatchOption(IReadOnlyList<OptionInfo> options, string? value = null, int? index = null, string? label = null)
    {
        if (index is int ix) return ix >= 0 && ix < options.Count ? ix : -1;
        for (int i = 0; i < options.Count; i++)
        {
            var o = options[i];
            if (label is not null)
            {
                if (o.Label == label) return i;
            }
            else if (value is not null && (o.Value == value || o.Label == value)) return i;
        }
        return -1;
    }

    // --------------------------------------------------------------------------------- range
    internal static double Num(string? v, double def)
    {
        if (v is null) return def;
        if (!double.TryParse(v.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var f)) return def;
        return double.IsNaN(f) ? def : f;   // NaN -> default
    }

    /// <summary>HTML defaults: min 0, max 100, step 1 ("any" = no snapping, null); max below min -> max = min.</summary>
    public static (double Lo, double Hi, double? Step) RangeAttrs(string? min = null, string? max = null, string? step = null)
    {
        double lo = Num(min, 0.0), hi = Num(max, 100.0);
        if (hi < lo) hi = lo;
        double? st;
        if (step is not null && step.Trim().ToLowerInvariant() == "any") st = null;
        else
        {
            double s = Num(step, 1.0);
            st = s <= 0 ? 1.0 : s;
        }
        return (lo, hi, st);
    }

    /// <summary>The value the browser will hold after being asked for <paramref name="value"/> (clamped,
    /// step-aligned from min, rounded to the nearest step; a tie goes up, as Blink's StepRange does).</summary>
    public static double RangeSanitize(string? value, string? min = null, string? max = null, string? step = null)
    {
        var (lo, hi, _) = RangeAttrs(min, max, step);
        return RangeSanitize(Num(value, (lo + hi) / 2), min, max, step);
    }

    /// <inheritdoc cref="RangeSanitize(string,string,string,string)"/>
    public static double RangeSanitize(double value, string? min = null, string? max = null, string? step = null)
    {
        var (lo, hi, st) = RangeAttrs(min, max, step);
        double v = Math.Min(Math.Max(double.IsNaN(value) ? (lo + hi) / 2 : value, lo), hi);
        if (st is double s && s != 0)
        {
            // Blink steps in Decimal; the epsilon keeps binary floats from rounding 0.35/0.1 =
            // 3.4999999999999996 down to 3 where the browser gets 4. The step count is capped at the last
            // step inside max -- computed, never walked down in a loop: a page can serve max=123456789
            // step=1e-9, where subtracting one step leaves the double unchanged forever.
            double q = (v - lo) / s, last = (hi - lo) / s;
            if (!double.IsFinite(q) || !double.IsFinite(last)) return v;   // a step too small to count in doubles
            double n = Math.Min(Math.Floor(q + 0.5 + 1e-9), Math.Floor(last + 1e-9));
            v = lo + n * s;
        }
        return v;
    }

    /// <summary>Would the browser keep <paramref name="value"/> exactly? Playwright's fill sets the value and
    /// raises "Malformed value" when the browser changed it (off the step, out of range, not a number), so
    /// the humane route only takes values that pass and leaves the rest to Playwright to reject.</summary>
    public static bool RangeValueOk(string? value, string? min = null, string? max = null, string? step = null)
    {
        if (value is null || !double.TryParse(value.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var f))
            return false;
        if (!double.IsFinite(f)) return false;
        return NumericEqual(RangeSanitize(f, min, max, step), f);
    }

    /// <summary>Where <paramref name="value"/> sits between <paramref name="lo"/> and <paramref name="hi"/> (0..1).</summary>
    public static double RangeFraction(double value, double lo, double hi)
    {
        if (hi < lo) hi = lo;
        return hi == lo ? 0.0 : (value - lo) / (hi - lo);
    }

    /// <summary>Chromium's default slider thumb is ~16 px wide; the track's usable span is inset by half of it.</summary>
    public const double RangeThumbInset = 8.0;

    /// <summary>Where on a horizontal range input the thumb sits at <paramref name="fraction"/> (CSS px).</summary>
    public static (double X, double Y) RangePoint(Box box, double fraction, bool rtl = false, double inset = RangeThumbInset)
    {
        double w = box.Width;
        double ins = Math.Min(inset, w / 2.0);
        double f = rtl ? 1.0 - fraction : fraction;
        return (box.X + ins + f * (w - 2 * ins), box.Y + box.Height / 2.0);
    }

    /// <summary>Arrow presses from <paramref name="current"/> to <paramref name="target"/>: (ArrowRight |
    /// ArrowLeft, count), or (null, 0). ArrowRight raises a horizontal LTR range by one step.</summary>
    public static (string? Key, int Count) RangeNudge(double current, double target, double? step)
    {
        if (step is not double s || s <= 0 || Math.Abs(target - current) < 1e-9) return (null, 0);
        int n = (int)Math.Round(Math.Abs(target - current) / s, MidpointRounding.ToEven);   // Python round()
        return (target > current ? "ArrowRight" : "ArrowLeft", n);
    }

    /// <summary>Equal as numbers (to 1e-6); false when either side is not a number.</summary>
    public static bool NumericEqual(string? a, string? b)
    {
        if (a is null || b is null) return false;
        if (!double.TryParse(a.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var x)) return false;
        if (!double.TryParse(b.Trim(), NumberStyles.Float, CultureInfo.InvariantCulture, out var y)) return false;
        return Math.Abs(x - y) < 1e-6;
    }

    /// <inheritdoc cref="NumericEqual(string,string)"/>
    public static bool NumericEqual(double a, double b) => Math.Abs(a - b) < 1e-6;

    // ----------------------------------------------------------------- date / time / month / week
    private static readonly Regex DateRe = new(@"\A([0-9]{4,6})-([0-9]{2})-([0-9]{2})\z", RegexOptions.CultureInvariant);
    private static readonly Regex MonthRe = new(@"\A([0-9]{4,6})-([0-9]{2})\z", RegexOptions.CultureInvariant);
    private static readonly Regex WeekRe = new(@"\A([0-9]{4,6})-W([0-9]{2})\z", RegexOptions.CultureInvariant);
    // Python's `.` stops at a newline; so does this group.
    private static readonly Regex DtlRe = new(@"\A([0-9]{4,6})-([0-9]{2})-([0-9]{2})[T ]([^\n]+)\z", RegexOptions.CultureInvariant);
    private static readonly Regex TimeRe = new(@"\A([0-9]{2}):([0-9]{2})(?::([0-9]{2})(?:\.([0-9]{1,3}))?)?\z", RegexOptions.CultureInvariant);

    private static int I(Group g) => int.Parse(g.Value, CultureInfo.InvariantCulture);

    /// <summary>Parse an HTML date-like value into its parts, or null if it is not valid for <paramref name="kind"/>.</summary>
    public static DateParts? ParseDatetimeValue(string? kind, string? value)
    {
        var v = (value ?? "").Trim();
        Match m;
        switch (kind)
        {
            case "date":
                m = DateRe.Match(v);
                if (!m.Success) return null;
                { int y = I(m.Groups[1]), mo = I(m.Groups[2]), d = I(m.Groups[3]);
                  return mo >= 1 && mo <= 12 && d >= 1 && d <= 31 ? new DateParts(Year: y, Month: mo, Day: d) : null; }
            case "month":
                m = MonthRe.Match(v);
                if (!m.Success) return null;
                { int y = I(m.Groups[1]), mo = I(m.Groups[2]);
                  return mo >= 1 && mo <= 12 ? new DateParts(Year: y, Month: mo) : null; }
            case "week":
                m = WeekRe.Match(v);
                if (!m.Success) return null;
                { int y = I(m.Groups[1]), w = I(m.Groups[2]);
                  return w >= 1 && w <= 53 ? new DateParts(Year: y, Week: w) : null; }
            case "time":
                return ParseTime(v);
            case "datetime-local":
                m = DtlRe.Match(v);
                if (!m.Success) return null;
                {
                    var t = ParseTime(m.Groups[4].Value);
                    int y = I(m.Groups[1]), mo = I(m.Groups[2]), d = I(m.Groups[3]);
                    if (t is null || !(mo >= 1 && mo <= 12 && d >= 1 && d <= 31)) return null;
                    return t with { Year = y, Month = mo, Day = d };
                }
        }
        return null;
    }

    private static DateParts? ParseTime(string v)
    {
        var m = TimeRe.Match(v);
        if (!m.Success) return null;
        int h = I(m.Groups[1]), mi = I(m.Groups[2]);
        int s = m.Groups[3].Success ? I(m.Groups[3]) : 0;
        int ms = m.Groups[4].Success ? int.Parse(m.Groups[4].Value.PadRight(3, '0'), CultureInfo.InvariantCulture) : 0;
        if (h > 23 || mi > 59 || s > 59) return null;
        return new DateParts(Hour: h, Minute: mi, Second: s, Millisecond: ms);
    }

    /// <summary>Same moment for <paramref name="kind"/>, whatever the formatting (17:45 == 17:45:00).</summary>
    public static bool DatetimeEqual(string? kind, string? a, string? b)
    {
        var pa = ParseDatetimeValue(kind, a);
        var pb = ParseDatetimeValue(kind, b);
        return pa is not null && pa == pb;
    }

    private static string D2(int v) => v.ToString("00", CultureInfo.InvariantCulture);

    /// <summary>Key actions that type <paramref name="value"/> into a native date-like editor whose fields
    /// come in <paramref name="segments"/> order ("-field" optional: month, day, year, hour, minute, second,
    /// millisecond, ampm, week). Null when the editor cannot represent the value.</summary>
    public static List<DtAction>? PlanDatetime(string? kind, IReadOnlyList<string> segments, string? value, string? current = null)
    {
        var parts = ParseDatetimeValue(kind, value);
        if (parts is null) return null;
        var segs = segments.Select(s => s.Replace("-field", "", StringComparison.Ordinal)).ToList();
        if (segs.Count == 0) return null;
        bool haveAmpm = segs.Contains("ampm");
        if (kind is "time" or "datetime-local")
        {
            if ((parts.Second ?? 0) != 0 && !segs.Contains("second")) return null;
            if ((parts.Millisecond ?? 0) != 0 && !segs.Contains("millisecond")) return null;
        }
        var cur = !string.IsNullOrEmpty(current) ? ParseDatetimeValue(kind, current) : null;
        var output = new List<DtAction>();
        for (int k = 0; k < segs.Count; k++)
        {
            bool last = k == segs.Count - 1;
            switch (segs[k])
            {
                case "year":
                    if (parts.Year is not int y || y > 9999) return null;
                    output.Add(new DtAction("type", y.ToString("0000", CultureInfo.InvariantCulture)));
                    if (!last) output.Add(new DtAction("key", "ArrowRight"));   // the year never moves on by itself
                    break;
                case "month":
                    if (parts.Month is not int mo) return null;
                    if (kind == "month")
                    {
                        // A month NAME field: digits are ignored; it steps on arrows. From empty the first
                        // ArrowUp shows January.
                        if (cur?.Month is not int c) output.Add(new DtAction("key", "ArrowUp", mo));
                        else
                        {
                            int d = (((mo - c) % 12) + 12) % 12;
                            output.Add(d <= 6 ? new DtAction("key", "ArrowUp", d) : new DtAction("key", "ArrowDown", 12 - d));
                        }
                        if (!last) output.Add(new DtAction("key", "ArrowRight"));
                    }
                    else output.Add(new DtAction("type", D2(mo)));
                    break;
                case "day":
                    if (parts.Day is not int day) return null;
                    output.Add(new DtAction("type", D2(day)));
                    break;
                case "week":
                    if (parts.Week is not int w) return null;
                    output.Add(new DtAction("type", D2(w)));
                    break;
                case "hour":
                    if (parts.Hour is not int h) return null;
                    output.Add(new DtAction("type", D2(haveAmpm ? (h % 12 == 0 ? 12 : h % 12) : h)));
                    break;
                case "minute":
                    output.Add(new DtAction("type", D2(parts.Minute ?? 0)));
                    break;
                case "second":
                    output.Add(new DtAction("type", D2(parts.Second ?? 0)));
                    break;
                case "millisecond":
                    output.Add(new DtAction("type", (parts.Millisecond ?? 0).ToString("000", CultureInfo.InvariantCulture)));
                    break;
                case "ampm":
                    // lower case: a capital with no Shift held is input no keyboard produces
                    output.Add(new DtAction("type", (parts.Hour ?? 0) < 12 ? "a" : "p"));
                    break;
                default:
                    return null;
            }
        }
        // drop no-op arrow steps (a month already right)
        return output.Where(a => !(a.Op == "key" && a.Count == 0)).ToList();
    }

    private const string DatetimePseudoPrefix = "-webkit-datetime-edit-";

    /// <summary>The date-like editor's field names, in order, from a CDP DOM.describeNode(pierce) tree.</summary>
    public static List<string> SegmentNamesFromTree(JsonElement node)
        => SegmentNodesFromTree(node).Select(s => s.Name).ToList();

    /// <summary>Like <see cref="SegmentNamesFromTree"/> with each field's backendNodeId.</summary>
    public static List<(string Name, int? BackendNodeId)> SegmentNodesFromTree(JsonElement node)
    {
        var output = new List<(string, int?)>();
        void Walk(JsonElement n)
        {
            if (n.ValueKind != JsonValueKind.Object) return;
            if (n.TryGetProperty("attributes", out var attrs) && attrs.ValueKind == JsonValueKind.Array)
            {
                int len = attrs.GetArrayLength();
                for (int j = 0; j + 1 < len; j += 2)
                {
                    if (attrs[j].ValueKind != JsonValueKind.String || attrs[j].GetString() != "pseudo") continue;
                    var ps = attrs[j + 1].ValueKind == JsonValueKind.String ? attrs[j + 1].GetString() ?? "" : "";
                    if (ps.StartsWith(DatetimePseudoPrefix, StringComparison.Ordinal) && ps.EndsWith("-field", StringComparison.Ordinal))
                    {
                        int? id = n.TryGetProperty("backendNodeId", out var b) && b.ValueKind == JsonValueKind.Number
                            ? b.GetInt32() : null;
                        output.Add((ps[DatetimePseudoPrefix.Length..], id));
                    }
                }
            }
            foreach (var key in new[] { "shadowRoots", "children" })
                if (n.TryGetProperty(key, out var kids) && kids.ValueKind == JsonValueKind.Array)
                    foreach (var c in kids.EnumerateArray()) Walk(c);
        }
        Walk(node);
        return output;
    }
}
