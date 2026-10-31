"""Pure keyboard planning for humanized typing (lane D of SC 232), shared by the sync and async
humanize modules and mirrored bit for bit in Node (keys.ts) and .NET (Keys.cs).

Nothing here touches a page: given a persona seed and an RNG it decides HOW LONG a hand waits between
two keys, how a chord is split into its modifier and key, how an OS auto-repeats a held key, and how
long a person pauses before Enter or Tab. The humanize modules sleep what it says. Keeping the
decisions pure is what lets one set of vectors (sdk/testdata/humanize_typing_vectors.json) pin the
three SDKs to identical answers.

The typing identity is drawn from a SALTED side stream of the persona seed (mulberry32(seed ^ salt)),
like rollover_rate and shape_traits: make_persona's draw order -- and so every existing seed's motor
identity -- is unchanged (pinned by test_keys.py).

Measured before this existed (SC 232 review, SDK 0.51.0): Node's inter-key gap was uniform 45-150 ms
while Python and .NET drew max(25, N(85, 45)) -- the same seed typed with two different cadences; every
bigram took the same time (a same-finger pair like "ed" is 1.5-1.9x slower than an alternating pair
like "en" on a real keyboard: Salthouse 1986, Feit 2016); a chord like Control+a went down as one
Playwright press (modifier and key within ~1 ms); Enter followed the last character after one key gap.
"""

from dataclasses import dataclass
import math

from ._motion import _MASK, _lerp, gauss_from, mulberry32

# Salt for the typing-identity stream (see the module docstring).
_KEYS_SALT = 0xD3A2646C   # own stream (was 0x27D4EB2F = the aim salt: WPM moved with the aim spread)

# Nominal keydown->keyup dwell subtracted from the WPM interval to get the keyup->keydown flight (the
# wrappers add the persona's own dwell on every key).
_NOMINAL_DWELL_MS = 80.0
# No two keydowns closer than this: the planner's floor, as the old max(25, N(85, 45)) had.
MIN_FLIGHT_MS = 25.0
# Calibration of the flight base (SC 232 triage, 10-16): the drawn WPM is the REALIZED speed on neutral English text.
# On such text the bigram model stretches a neutral flight by the class mix below -- shares of consecutive key pairs
# on a 500-pair English sample: same finger 6 %, same hand 21 %, alternating 34 %, 39 % with a space or punctuation on
# one side (factor 1); ~27 % of the same-finger / same-hand pairs are row jumps (x1.15) -- times the flight noise's
# mean exp(0.35^2 / 2), and the pauses (word-initial, after punctuation, after whitespace, thinking) add ~48 ms per
# character. Before the calibration the trait read 28-75 WPM but typed 23-60 (lab D_cadence on d2b9ab6: ha-1 at
# 28.9 WPM planned 529 ms per key = 23 WPM). Same constants in keys.ts and Keys.cs.
_EN_SAME_FINGER, _EN_SAME_HAND, _EN_ALTERNATE, _EN_NONE = 0.06, 0.21, 0.34, 0.39
_EN_ROW_JUMP = 1.04          # 1 + 0.15 x the row-jump share of the same-finger / same-hand pairs
_EN_PAUSE_MS = 48.0
_LOGNORMAL_MEAN = math.exp(0.35 * 0.35 / 2)


@dataclass(frozen=True)
class TypingTraits:
    """Per-identity typing signature, stable per seed (typing_traits)."""

    wpm: float              # words per minute, 28..75 (5 characters per word)
    same_finger: float      # flight factor, same finger twice, 1.5..1.9
    same_hand: float        # flight factor, same hand, other finger, 1.1..1.3
    alternate: float        # flight factor, the other hand, 0.85..1.0
    tab_pref: float         # share of adjacent-field fills that Tab instead of click, 0.60..0.85
    typo_rate: float        # per-character fat-finger rate, 0.005..0.04
    submit_center_ms: float  # centre of the pause before Enter after typing, 400..750 (draws 250..900)
    tab_center_ms: float    # centre of the pause before Tab after typing, 190..330 (draws 120..400)
    select_style: str       # "triple" (triple click) or "keys" (Home, Shift+End) for select_text
    blur_style: str         # "tab" (Tab away) or "click" (a blank point) for blur
    repeat_first_ms: float  # the OS key auto-repeat DELAY, 252..498 (a machine setting: one per identity)
    repeat_gap_ms: float    # the OS key auto-repeat INTERVAL, 26..39 (likewise; plan_repeat adds +-2 / +-1 ms)


def typing_traits(seed_int):
    """The typing identity of persona seed ``seed_int`` (Persona.seed), from its salted side stream.
    Draw order: wpm, same_finger, same_hand, alternate, tab_pref, typo_rate, submit_center, tab_center,
    select_style, blur_style, repeat_first, repeat_gap -- the same in keys.ts typingTraits and
    Keys.TypingTraitsOf. The two repeat draws came last (verifier A's R1, SC 232) so every earlier trait of
    every seed is unchanged."""
    r = mulberry32((int(seed_int) ^ _KEYS_SALT) & _MASK)
    wpm = _lerp(28.0, 75.0, r())
    same_finger = _lerp(1.5, 1.9, r())
    same_hand = _lerp(1.1, 1.3, r())
    alternate = _lerp(0.85, 1.0, r())
    tab_pref = _lerp(0.60, 0.85, r())
    typo_rate = _lerp(0.005, 0.04, r())
    submit_center = _lerp(400.0, 750.0, r())
    tab_center = _lerp(190.0, 330.0, r())
    select_style = "triple" if r() < 0.55 else "keys"
    blur_style = "tab" if r() < 0.5 else "click"
    repeat_first = _lerp(252.0, 498.0, r())
    repeat_gap = _lerp(26.0, 39.0, r())
    return TypingTraits(wpm=wpm, same_finger=same_finger, same_hand=same_hand, alternate=alternate,
                        tab_pref=tab_pref, typo_rate=typo_rate, submit_center_ms=submit_center,
                        tab_center_ms=tab_center, select_style=select_style, blur_style=blur_style,
                        repeat_first_ms=repeat_first, repeat_gap_ms=repeat_gap)


# ------------------------------------------------------------------------------ the finger map
# US QWERTY touch-typing fingers: 0-3 left pinky..index, 4-7 right index..pinky; rows 0 (digits) to 3.
_ROWS = ("`1234567890-=", "qwertyuiop[]\\", "asdfghjkl;'", "zxcvbnm,./")
_FINGERS = (
    (0, 0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7),
    (0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7, 7, 7),
    (0, 1, 2, 3, 3, 4, 4, 5, 6, 7, 7),
    (0, 1, 2, 3, 3, 4, 4, 5, 6, 7),
)
# A shifted symbol is typed on its base key.
_SHIFT_BASE = dict(zip('~!@#$%^&*()_+{}|:"<>?', "`1234567890-=[]\\;',./"))

FINGER_MAP = {}
for _row, (_keys, _fs) in enumerate(zip(_ROWS, _FINGERS)):
    for _k, _f in zip(_keys, _fs):
        FINGER_MAP[_k] = (_f, _row)


def finger_of(ch):
    """(finger 0..7, row 0..3) of the key that types ``ch``, or None (space, Enter, non-US characters).
    Capitals and shifted symbols map to their base key."""
    if not ch or len(ch) != 1:
        return None
    c = ch.lower()
    c = _SHIFT_BASE.get(c, c)
    return FINGER_MAP.get(c)


def bigram_class(prev, ch):
    """How the hand gets from ``prev`` to ``ch``: ("same_finger" | "same_hand" | "alternate" | "none",
    row_jump). row_jump: two rows or more apart on the same hand (a "ce" or "un" reach), x1.15."""
    a, b = finger_of(prev), finger_of(ch)
    if a is None or b is None:
        return "none", False
    if a[0] == b[0]:
        return "same_finger", abs(a[1] - b[1]) >= 2
    if (a[0] < 4) == (b[0] < 4):
        return "same_hand", abs(a[1] - b[1]) >= 2
    return "alternate", False


# ROLLOVER by bigram class (SC 299): the next key goes down before this one comes up only when another finger is
# free to press it. The same finger cannot (it has to lift first), a finger of the same hand rolls less often than
# the other hand, and the thumb's space rolls as before. The factors keep the persona's overall share on English
# text (0.34 x 1.35 + 0.21 x 0.65 + 0.39 x 1.0 + 0.06 x 0 = 0.99 of the class-free rate).
# Measured on r54 / SDK 0.67.1 (SC 298/299): the class-free 12-38 % rollover typed same-finger pairs as overlaps,
# a keydown interval of one dwell less the overlap, which pulled same-finger and same-hand pairs down to the
# alternating level (same-hand / alternate 0.98-1.09 against the model's 1.1-1.3 flight factors).
ROLLOVER_CLASS_FACTOR = {"same_finger": 0.0, "same_hand": 0.65, "alternate": 1.35, "none": 1.0}
ROLLOVER_PAIR_CAP = 1.0


def rollover_pair_rate(rate, prev, ch):
    """The chance that the pair ``prev`` -> ``ch`` is typed with rollover, for a persona whose class-free rollover
    share is ``rate`` (_motion.rollover_rate): ``rate`` x the pair's class factor, at most ROLLOVER_PAIR_CAP."""
    cls, _jump = bigram_class(prev, ch)
    return min(ROLLOVER_PAIR_CAP, rate * ROLLOVER_CLASS_FACTOR.get(cls, 1.0))


_PUNCT = ".,;:!?"


def english_inflation(traits):
    """How much this persona's finger factors and the flight noise stretch a neutral flight on English text
    (the class shares above): 1.0 would be a typist whose bigrams all cost the same."""
    return (_EN_SAME_FINGER * traits.same_finger * _EN_ROW_JUMP + _EN_SAME_HAND * traits.same_hand * _EN_ROW_JUMP
            + _EN_ALTERNATE * traits.alternate + _EN_NONE) * _LOGNORMAL_MEAN


def base_flight_ms(traits):
    """The keyup->keydown flight of a NEUTRAL bigram (class factor 1), set so that with the persona's finger
    factors, the flight noise, the pauses and the nominal dwell the mean keydown interval on English text is
    60000 / (wpm x 5): the drawn WPM is the realized speed. Floored at 30 ms, so ~75 WPM is the fastest the
    model types. Before 10-16 the base was the whole interval less the dwell and everything else stacked on it."""
    return max(30.0, (60000.0 / (traits.wpm * 5.0) - _NOMINAL_DWELL_MS - _EN_PAUSE_MS) / english_inflation(traits))


def flight_ms(traits, prev, ch, rng):
    """Flight (ms) from the key ``prev`` to the key ``ch``: base(WPM) x finger-distance factor x log-normal
    noise (sigma 0.35), plus a word-initial pause (40-120 ms) when ``ch`` starts a word and a pause after
    punctuation (80-250 ms). Draw order: gauss (noise), then word-initial, then punctuation -- each
    conditional draw only when its condition holds."""
    cls, jump = bigram_class(prev, ch)
    if cls == "same_finger":
        f = traits.same_finger
    elif cls == "same_hand":
        f = traits.same_hand
    elif cls == "alternate":
        f = traits.alternate
    else:
        f = 1.0
    if jump:
        f *= 1.15
    ms = base_flight_ms(traits) * f * math.exp(0.35 * gauss_from(rng))
    if prev is not None and prev in " \t\n" and not (ch in " \t\n"):
        ms += _lerp(40.0, 120.0, rng())
    if prev is not None and prev in _PUNCT:
        ms += _lerp(80.0, 250.0, rng())
    return max(MIN_FLIGHT_MS, ms)


def plan_gap(traits, prev, ch, rng, tempo=1.0, pause_scale=1.0):
    """The whole sleep (ms) between the key ``prev`` coming up and the key ``ch`` going down: flight_ms
    plus the pauses the old cadence already had -- 20-100 ms more after whitespace, and a 6 % chance of a
    180-450 ms thinking pause. Draw order after flight_ms: whitespace (conditional), then the think
    roll, then (conditional) the think length. ``tempo`` scales the whole gap and ``pause_scale`` the
    thinking-pause rate (lane A's presets: persona.tempo / persona.pause_scale; 1.0 = as planned)."""
    ms = flight_ms(traits, prev, ch, rng)
    if prev is not None and prev in " \t\n":
        ms += _lerp(20.0, 100.0, rng())
    if rng() < 0.06 * pause_scale:
        ms += _lerp(180.0, 450.0, rng())
    return ms * tempo


# ------------------------------------------------------------------------------------- slips
# Share of whole-word slip corrections made with the word-delete chord (Ctrl+Backspace; Alt+Backspace on
# macOS) and a retype of the word, instead of one Backspace per wrong key.
WORD_FIX_RATE = 0.30
# Share of fat-finger substitutions in a type=search field that are never corrected (the search runs
# with the slip in it, as people's do). Never on fill(), whose value must come out exact.
SEARCH_SLIP_RATE = 0.10


# ------------------------------------------------------------------------------------- chords
MODIFIERS = frozenset(["Shift", "Control", "Alt", "Meta", "ControlOrMeta"])


def split_chord(key):
    """Playwright's key string split into (modifiers, key): "Control+Shift+a" -> (["Control", "Shift"],
    "a"); "Shift++" -> (["Shift"], "+"); "+" -> ([], "+"); a lone modifier ("Shift") -> ([], "Shift")."""
    if key == "+":
        return [], "+"
    if key.endswith("++"):
        return key[:-2].split("+"), "+"
    parts = key.split("+")
    return parts[:-1], parts[-1]


def modifier_lead_ms(rng):
    """Modifier keydown -> the key's keydown: 40-110 ms."""
    return _lerp(40.0, 110.0, rng())


def modifier_lag_ms(rng):
    """The key's keyup -> modifier keyup: 20-80 ms."""
    return _lerp(20.0, 80.0, rng())


# ------------------------------------------------------------------------------------- repeat
@dataclass(frozen=True)
class RepeatPlan:
    first_ms: float     # the OS repeat delay: the first repeat comes 250-500 ms after the keydown
    gap_ms: float       # the repeat rate, one for the whole run: 25-40 ms
    release_ms: float   # the last repeat -> keyup: 20-60 ms


def plan_repeat(traits, rng):
    """OS auto-repeat timing for one held key. The repeat DELAY and INTERVAL are the machine's settings
    (``traits.repeat_first_ms`` / ``repeat_gap_ms``: a control-panel constant, so one per identity), seen
    through +-2 / +-1 ms of timer jitter per run; the release is the hand's. Draw order: first jitter, gap
    jitter, release. Before verifier A's R1 (SC 232) both were drawn per run (250-500 / 25-40 ms): one
    machine repeating at 27 ms and then 39 ms within a session, a device-constant contradiction any keydown
    log shows."""
    return RepeatPlan(first_ms=traits.repeat_first_ms + _lerp(-2.0, 2.0, rng()),
                      gap_ms=traits.repeat_gap_ms + _lerp(-1.0, 1.0, rng()),
                      release_ms=_lerp(20.0, 60.0, rng()))


# ------------------------------------------------------------------------------------- pauses
def submit_pause_ms(traits, rng):
    """The pause between the last typed character and Enter, 250-900 ms, centred per persona."""
    return min(900.0, max(250.0, traits.submit_center_ms + (rng() - 0.5) * 300.0))


def tab_pause_ms(traits, rng):
    """The pause between the last typed character and Tab, 120-400 ms, centred per persona."""
    return min(400.0, max(120.0, traits.tab_center_ms + (rng() - 0.5) * 140.0))


# --------------------------------------------------------------------- isolated-world reads (JS)
# The focusable elements the browser's Tab order visits.
FOCUSABLE = ("a[href], area[href], button, input, select, textarea, summary, iframe, [tabindex], "
             "[contenteditable], audio[controls], video[controls]")

# Where the element filling box ``b`` sits in the page's TAB ORDER relative to the focused element
# (`this` = the node the hit test found inside it; `b.sel` = a plain-CSS selector for it when the caller
# has one). dist: tab stops from the focused element to it (negative = Shift+Tab), null when either is
# not in the order (focus on <body>, a tabindex=-1 target, an element in a shadow root); fromText: the
# focused element is a text-entry control; activatable: a click on the target would ACTIVATE it (a
# button, a link, a checkbox, a slider), so focus must come from Tab, never a click. Positive tabindex
# values come first in ascending order, then document order, as the browser walks them.
TAB_DISTANCE = """function (b) {
  const up = n => n.parentElement || (n.parentNode && n.parentNode.nodeType === 11 && n.parentNode.host) || null;
  const FOC = '%s';
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
    n: order.length, noAnchor: ai < 0, near }; }""" % FOCUSABLE

# A blank point near box ``b`` (viewport ``vw`` x ``vh``) where a click lands on nothing interactive --
# the page background, a plain container -- the way a person clicks beside a field to leave it. Tries
# below, right, left and above the box, nearer first; null when every candidate is on a control, a
# link, a label or an element with a click handler attribute.
BLANK_POINT = """(a) => { const b = a.box, FOC = '%s, label, [onclick], [role]';
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
  return null; }""" % FOCUSABLE

# The selection inside the focused text control: [start, end, length] (a contenteditable reports the
# selected text's length against its text length), or null.
SELECTION_STATE = """() => { let e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  if (typeof e.selectionStart === 'number' && typeof e.value === 'string')
    return [e.selectionStart, e.selectionEnd, e.value.length];
  if (e.isContentEditable) { const s = getSelection(); return [0, s ? s.toString().length : 0, (e.textContent || '').length]; }
  return null; }"""
