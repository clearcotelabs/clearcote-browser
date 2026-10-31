// Pure keyboard planning for humanized typing (lane D of SC 232): a faithful port of the Python SDK's
// clearcote/_keys.py and the Node SDK's keys.ts. Nothing here touches a page: given a persona seed and an
// RNG it decides how long a hand waits between two keys (a bigram flight model on the persona's WPM), how a
// chord splits into modifiers and key, how an OS auto-repeats a held key, and the pause before Enter or Tab
// after typing. sdk/testdata/humanize_typing_vectors.json pins the three SDKs to identical answers.
//
// The typing identity comes from a SALTED side stream of the persona seed (Mulberry32(seed ^ salt)), like
// RolloverRate and ShapeTraitsOf, so MakePersona's draw order -- every seed's motor identity -- is unchanged.
// The seed fixes the TRAITS only: every per-key draw comes from an unseeded RNG at the call site.

namespace Clearcote;

/// <summary>Per-identity typing signature, stable per seed (<see cref="Keys.TypingTraitsOf"/>).</summary>
/// <remarks><c>RepeatFirstMs</c> / <c>RepeatGapMs</c>: the OS key auto-repeat delay (252..498) and interval (26..39),
/// a machine setting and so one per identity (verifier A's R1, SC 232); <see cref="Keys.PlanRepeat"/> adds +-2 / +-1 ms.</remarks>
public sealed record TypingTraits(
    double Wpm, double SameFinger, double SameHand, double Alternate, double TabPref, double TypoRate,
    double SubmitCenterMs, double TabCenterMs, string SelectStyle, string BlurStyle,
    double RepeatFirstMs, double RepeatGapMs);

/// <summary>OS auto-repeat timing for one held key.</summary>
public sealed record RepeatPlan(double FirstMs, double GapMs, double ReleaseMs);

public static class Keys
{
    private const uint KeysSalt = 0xD3A2646C; // own stream (was 0x27D4EB2F = the aim salt)
    private const double NominalDwellMs = 80;
    public const double MinFlightMs = 25;
    // Calibration of the flight base (SC 232 triage, 10-16): the drawn WPM is the REALIZED speed on neutral English text --
    // the class mix of consecutive key pairs on a 500-pair English sample (same finger 6 %, same hand 21 %, alternating
    // 34 %, 39 % with a space or punctuation on one side; ~27 % of the same-finger / same-hand pairs are row jumps, x1.15),
    // the flight noise's mean exp(0.35^2 / 2) and ~48 ms of pauses per character. Same constants as _keys.py / keys.ts.
    private const double EnSameFinger = 0.06, EnSameHand = 0.21, EnAlternate = 0.34, EnNone = 0.39;
    private const double EnRowJump = 1.04;
    private const double EnPauseMs = 48.0;
    private static readonly double LogNormalMean = Math.Exp(0.35 * 0.35 / 2);
    /// <summary>Share of whole-word slip corrections made with the word-delete chord and a retype.</summary>
    public const double WordFixRate = 0.30;
    /// <summary>Share of substitutions in a type=search field left uncorrected (never on fill).</summary>
    public const double SearchSlipRate = 0.10;

    private static double Lerp(double a, double b, double t) => a + (b - a) * t;

    /// <summary>The typing identity of persona seed <paramref name="seed"/>. Draw order as _keys.typing_traits.</summary>
    public static TypingTraits TypingTraitsOf(uint seed)
    {
        Rng r = Motion.Mulberry32(seed ^ KeysSalt);
        double wpm = Lerp(28, 75, r());
        double sameFinger = Lerp(1.5, 1.9, r());
        double sameHand = Lerp(1.1, 1.3, r());
        double alternate = Lerp(0.85, 1.0, r());
        double tabPref = Lerp(0.60, 0.85, r());
        double typoRate = Lerp(0.005, 0.04, r());
        double submitCenter = Lerp(400, 750, r());
        double tabCenter = Lerp(190, 330, r());
        string select = r() < 0.55 ? "triple" : "keys";
        string blur = r() < 0.5 ? "tab" : "click";
        double repeatFirst = Lerp(252, 498, r());   // the two repeat draws came last (R1): every earlier trait is unchanged
        double repeatGap = Lerp(26, 39, r());
        return new TypingTraits(wpm, sameFinger, sameHand, alternate, tabPref, typoRate, submitCenter, tabCenter, select, blur,
            repeatFirst, repeatGap);
    }

    private static readonly string[] Rows = { "`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./" };
    private static readonly int[][] Fingers =
    {
        new[] { 0, 0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7 },
        new[] { 0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7, 7 },
        new[] { 0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7 },
        new[] { 0, 1, 2, 3, 3, 4, 4, 5, 6, 7 },
    };
    private const string Shifted = "~!@#$%^&*()_+{}|:\"<>?";
    private const string ShiftBase = "`1234567890-=[]\\;',./";
    private static readonly Dictionary<char, (int Finger, int Row)> FingerMap = BuildFingerMap();

    private static Dictionary<char, (int, int)> BuildFingerMap()
    {
        var m = new Dictionary<char, (int, int)>();
        for (int row = 0; row < Rows.Length; row++)
            for (int i = 0; i < Rows[row].Length; i++) m[Rows[row][i]] = (Fingers[row][i], row);
        return m;
    }

    /// <summary>(finger 0..7, row 0..3) of the key that types <paramref name="ch"/>, or null.</summary>
    public static (int Finger, int Row)? FingerOf(string? ch)
    {
        if (ch is null || ch.Length != 1) return null;
        char c = char.ToLowerInvariant(ch[0]);
        int j = Shifted.IndexOf(c);
        if (j >= 0) c = ShiftBase[j];
        return FingerMap.TryGetValue(c, out var f) ? f : null;
    }

    /// <summary>How the hand gets from <paramref name="prev"/> to <paramref name="ch"/>, and whether it is a
    /// two-row reach on the same hand.</summary>
    public static (string Class, bool Jump) BigramClass(string? prev, string? ch)
    {
        var a = FingerOf(prev);
        var b = FingerOf(ch);
        if (a is null || b is null) return ("none", false);
        if (a.Value.Finger == b.Value.Finger) return ("same_finger", Math.Abs(a.Value.Row - b.Value.Row) >= 2);
        if ((a.Value.Finger < 4) == (b.Value.Finger < 4)) return ("same_hand", Math.Abs(a.Value.Row - b.Value.Row) >= 2);
        return ("alternate", false);
    }

    private static bool IsWs(string? c) => c is " " or "\t" or "\n";

    /// <summary>ROLLOVER by bigram class (SC 299): the next key goes down before this one comes up only when another
    /// finger is free to press it. The same finger cannot (it has to lift first), a finger of the same hand rolls less
    /// often than the other hand, and the thumb's space rolls as before. The factors keep the persona's overall share on
    /// English text (0.34 x 1.35 + 0.21 x 0.65 + 0.39 x 1.0 + 0.06 x 0 = 0.99 of the class-free rate). Measured on r54 /
    /// SDK 0.67.1 (SC 298/299): the class-free 12-38 % rollover typed same-finger pairs as overlaps, which pulled
    /// same-finger and same-hand pairs down to the alternating level. Same values as Python ROLLOVER_CLASS_FACTOR.</summary>
    public static double RolloverClassFactor(string cls) => cls switch
    {
        "same_finger" => 0.0, "same_hand" => 0.65, "alternate" => 1.35, _ => 1.0,
    };

    /// <summary>The largest per-pair rollover chance.</summary>
    public const double RolloverPairCap = 1.0;

    /// <summary>The chance that the pair <paramref name="prev"/> -> <paramref name="ch"/> is typed with rollover, for a
    /// persona whose class-free rollover share is <paramref name="rate"/> (<see cref="Motion.RolloverRate"/>):
    /// <paramref name="rate"/> x the pair's class factor, at most <see cref="RolloverPairCap"/>. Python
    /// <c>rollover_pair_rate</c>.</summary>
    public static double RolloverPairRate(double rate, string? prev, string? ch)
        => Math.Min(RolloverPairCap, rate * RolloverClassFactor(BigramClass(prev, ch).Class));

    /// <summary>How much this persona's finger factors and the flight noise stretch a neutral flight on English text
    /// (_keys.english_inflation).</summary>
    public static double EnglishInflation(TypingTraits t)
        => (EnSameFinger * t.SameFinger * EnRowJump + EnSameHand * t.SameHand * EnRowJump
            + EnAlternate * t.Alternate + EnNone) * LogNormalMean;

    /// <summary>The neutral bigram's flight, calibrated so the mean keydown interval on English text is 60000 / (wpm x 5)
    /// (_keys.base_flight_ms).</summary>
    public static double BaseFlightMs(TypingTraits t)
        => Math.Max(30, (60000 / (t.Wpm * 5) - NominalDwellMs - EnPauseMs) / EnglishInflation(t));

    /// <summary>Flight (ms) from key <paramref name="prev"/> to key <paramref name="ch"/>; draw order as _keys.flight_ms.</summary>
    public static double FlightMs(TypingTraits t, string? prev, string? ch, Rng rng)
    {
        var (cls, jump) = BigramClass(prev, ch);
        double f = cls switch { "same_finger" => t.SameFinger, "same_hand" => t.SameHand, "alternate" => t.Alternate, _ => 1 };
        if (jump) f *= 1.15;
        double ms = BaseFlightMs(t) * f * Math.Exp(0.35 * Motion.GaussFrom(rng));
        if (prev is not null && IsWs(prev) && !IsWs(ch)) ms += Lerp(40, 120, rng());
        if (prev is { Length: 1 } && ".,;:!?".IndexOf(prev[0]) >= 0) ms += Lerp(80, 250, rng());
        return Math.Max(MinFlightMs, ms);
    }

    /// <summary>The whole sleep between key <paramref name="prev"/> coming up and key <paramref name="ch"/> going down.</summary>
    /// <remarks><paramref name="tempo"/> scales the whole gap and <paramref name="pauseScale"/> the thinking-pause
    /// rate (lane A's presets; 1 = as planned).</remarks>
    public static double PlanGap(TypingTraits t, string? prev, string? ch, Rng rng, double tempo = 1, double pauseScale = 1)
    {
        double ms = FlightMs(t, prev, ch, rng);
        if (prev is not null && IsWs(prev)) ms += Lerp(20, 100, rng());
        if (rng() < 0.06 * pauseScale) ms += Lerp(180, 450, rng());
        return ms * tempo;
    }

    /// <summary>Playwright's key string split into (modifiers, key): "Shift++" -> (["Shift"], "+").</summary>
    public static (string[] Mods, string Key) SplitChord(string key)
    {
        if (key == "+") return (Array.Empty<string>(), "+");
        if (key.EndsWith("++", StringComparison.Ordinal)) return (key[..^2].Split('+'), "+");
        var parts = key.Split('+');
        return (parts[..^1], parts[^1]);
    }

    public static double ModifierLeadMs(Rng rng) => Lerp(40, 110, rng());
    public static double ModifierLagMs(Rng rng) => Lerp(20, 80, rng());

    /// <summary>OS auto-repeat timing for one held key: the persona's repeat delay and interval (a machine setting,
    /// <see cref="TypingTraitsOf"/>) seen through +-2 / +-1 ms of timer jitter per run, and the hand's release. Draw
    /// order: first jitter, gap jitter, release (_keys.plan_repeat).</summary>
    public static RepeatPlan PlanRepeat(TypingTraits t, Rng rng)
    {
        double first = t.RepeatFirstMs + Lerp(-2, 2, rng());
        double gap = t.RepeatGapMs + Lerp(-1, 1, rng());
        double release = Lerp(20, 60, rng());
        return new RepeatPlan(first, gap, release);
    }

    public static double SubmitPauseMs(TypingTraits t, Rng rng) => Math.Min(900, Math.Max(250, t.SubmitCenterMs + (rng() - 0.5) * 300));
    public static double TabPauseMs(TypingTraits t, Rng rng) => Math.Min(400, Math.Max(120, t.TabCenterMs + (rng() - 0.5) * 140));

    // ------------------------------------------------------- isolated-world reads (same as _keys.py)
    internal const string Focusable = "a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], "
        + "[contenteditable], audio[controls], video[controls]";

    internal const string TabDistance = @"function (b) {
  const up = n => n.parentElement || (n.parentNode && n.parentNode.nodeType === 11 && n.parentNode.host) || null;
  const FOC = '" + Focusable + @"';
  const same = r => Math.abs(r.x - b.x) <= 1.5 && Math.abs(r.y - b.y) <= 1.5 &&
    Math.abs(r.width - b.width) <= 1.5 && Math.abs(r.height - b.height) <= 1.5;
  let t = null;
  if (b.sel) { try { t = document.querySelector(b.sel); } catch (e) { t = null; } }
  if (!t) {
    for (let e = this.nodeType === 1 ? this : this.parentElement, n = 0; e && n < 64; e = up(e), n++)
      if (e.matches && e.matches(FOC)) { t = e; break; }
  }
  if (!t || !t.matches(FOC) || !same(t.getBoundingClientRect())) return null;
  const ti = e => { const v = parseInt(e.getAttribute('tabindex'), 10); return isNaN(v) ? 0 : v; };
  const vis = e => { const s = getComputedStyle(e); return s.visibility !== 'hidden' && s.display !== 'none' && e.getClientRects().length > 0; };
  const tabbable = e => !e.disabled && !(e.tagName === 'INPUT' && e.type === 'hidden') && ti(e) >= 0 &&
    !(e.tagName === 'A' && !e.hasAttribute('href')) && vis(e);
  const all = Array.from(document.querySelectorAll(FOC)).filter(tabbable);
  const pos = all.filter(e => ti(e) > 0).sort((x, y) => ti(x) - ti(y));
  const order = pos.concat(all.filter(e => ti(e) <= 0));
  let a = document.activeElement;
  while (a && a.shadowRoot && a.shadowRoot.activeElement) a = a.shadowRoot.activeElement;
  const ai = a ? order.indexOf(a) : -1, tix = order.indexOf(t);
  const VALUE_TYPES = ['checkbox', 'radio', 'button', 'submit', 'reset', 'file', 'image', 'range', 'color', 'hidden'];
  const text = e => !!e && ((e.tagName === 'INPUT' && VALUE_TYPES.indexOf(e.type) < 0) || e.tagName === 'TEXTAREA' || !!e.isContentEditable);
  const role = t.getAttribute('role') || '';
  const act = ['BUTTON', 'A', 'SUMMARY', 'SELECT'].indexOf(t.tagName) >= 0 ||
    (t.tagName === 'INPUT' && VALUE_TYPES.indexOf(t.type) >= 0) ||
    ['button', 'link', 'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'option'].indexOf(role) >= 0;
  let near = null;
  if (tix >= 0) for (const off of [1, 2, 3, -1]) {
    const c = order[tix - off];
    if (!c || c === a || !text(c) || c.disabled) continue;
    const q = c.getBoundingClientRect();
    if (q.width >= 2 && q.height >= 2 && q.left >= 0 && q.top >= 0 && q.right <= innerWidth && q.bottom <= innerHeight) {
      near = { off, rect: [q.x, q.y, q.width, q.height] }; break; }
  }
  return { dist: ai >= 0 && tix >= 0 ? tix - ai : null, fromText: text(a), focused: a === t,
    activatable: act, text: text(t), tag: t.tagName.toLowerCase(), type: t.tagName === 'INPUT' ? t.type : '',
    n: order.length, noAnchor: ai < 0, near }; }";

    internal const string BlankPoint = @"(a) => { const b = a.box, FOC = '" + Focusable + @", label, [onclick], [role]';
  const cands = [];
  for (const dy of [28, 56, 96]) cands.push([b.x + b.width * 0.5, b.y + b.height + dy]);
  for (const dx of [32, 72, 128]) cands.push([b.x + b.width + dx, b.y + b.height * 0.5]);
  for (const dx of [32, 72]) cands.push([b.x - dx, b.y + b.height * 0.5]);
  for (const dy of [40, 80]) cands.push([b.x + b.width * 0.5, b.y - dy]);
  const PLAIN = ['HTML', 'BODY', 'DIV', 'SECTION', 'MAIN', 'FORM', 'ARTICLE', 'P', 'SPAN', 'FIELDSET', 'UL', 'LI', 'HEADER', 'FOOTER', 'NAV', 'ASIDE', 'TD', 'TR', 'TABLE'];
  for (const [x, y] of cands) {
    if (x < 4 || y < 4 || x > a.vw - 4 || y > a.vh - 4) continue;
    const e = document.elementFromPoint(x, y);
    if (!e) continue;
    if (PLAIN.indexOf(e.tagName) < 0 || e.isContentEditable) continue;
    if (e.closest(FOC)) continue;
    return [x, y];
  }
  return null; }";

    internal const string SelectionState = @"() => { let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  if (typeof e.selectionStart === 'number' && typeof e.value === 'string')
    return [e.selectionStart, e.selectionEnd, e.value.length];
  if (e.isContentEditable) { const s = getSelection(); return [0, s ? s.toString().length : 0, (e.textContent || '').length]; }
  return null; }";
}
