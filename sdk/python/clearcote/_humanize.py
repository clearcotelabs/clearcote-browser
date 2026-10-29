"""Humanized input + cursor visualization (Python).

``humanize=True`` installs ONE consistent human-input standard that covers moving, clicking,
dragging, scrolling and typing — dispatched as native trusted input (isTrusted=True,
navigator.webdriver stays False). It works whether you drive the page directly or via locators:

  * page level   — page.click / page.hover / page.dblclick / page.type / page.fill / page.press,
                   page.mouse.move / mouse.click / mouse.wheel and held-button drags
                   (mouse.down → mouse.move → mouse.up), page.keyboard.type
  * locator level — locator.click / type / fill / hover / dblclick / press /
                    press_sequentially / clear / check / uncheck / tap / drag_to
  * press and hold — page.press_and_hold(selector) / locator.press_and_hold() (_hold.py)
                    (routed through the humanized page methods; main-frame locators only,
                    with a safe fall-through to native for anything exotic)

``show_cursor=True`` injects a red cursor dot that follows the real mousemove events.

Design notes:
  * Mouse moves build an eased, slightly bowed cubic-bezier path SDK-side from the *tracked*
    cursor position (continuous — no snap back to the 0,0 corner between moves) using
    Playwright's NATIVE mouse.move. Because it is native input, the button state is carried
    correctly: down() → move() → up() is a real held-button drag (sliders work) with no
    separate CDP channel to desync.
  * Typing goes key-by-key through Playwright's native keyboard (trusted, shift handled by the
    engine) with randomized inter-key timing, occasional thinking pauses, and a small
    fat-finger-then-correct chance.
"""

import collections
import functools
import math
import random
import re
import sys
import time

import os

from ._forms import (
    DEFAULT_TYPO_RATE, ENGINE_KEY_SEQUENCE, NEARBY_LETTERS, classify_field, datetime_equal, engine_click_params,
    engine_held_bits, engine_key_plan, engine_path_ok, fit_maxlength,
    formatted_equal, mask_kept, match_option, normalize_click_options, numeric_equal, plan_datetime, plan_select,
    plan_typo, pre_press_ms, range_attrs,
    range_fraction, range_nudge, range_point, range_sanitize, range_value_ok, segment_nodes_from_tree,
    typo_eligible, typos_allowed,
)
from ._isolated import (
    ACTIVE_FIELD as ISO_ACTIVE_FIELD, ACTIVE_VALUE as ISO_ACTIVE_VALUE, COVER_CHECK_MS, COVER_RETRY_MS,
    COVER as ISO_COVER, SETTLE_MS, SETTLE_TRIES,
    CSS_BOX as ISO_CSS_BOX, DESCRIBE as ISO_DESCRIBE, FORM_CONTROL, HIT_IN_BOX as ISO_HIT_IN_BOX,
    FRAMES as ISO_FRAMES, LABEL_BY_SEL as ISO_LABEL_BY_SEL, LABEL_OF as ISO_LABEL_OF,
    LISTBOX_ROW as ISO_LISTBOX_ROW, SCROLL_BY as ISO_SCROLL_BY,
    PLATFORM as ISO_PLATFORM, VIEWPORT as ISO_VIEWPORT, world_for,
)
from ._motion import (
    WHEEL_NOTCH_PX, make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient,
    dblclick_gap, pause_ms, timing_traits,
    platform_from_navigator, rollover_rate, wheel_notches,
    dwell_ms, move_off_delay_ms, move_off_roll, plan_dwell, plan_move_off,
    drag_hesitation, hesitate_then_drag, Persona, aim_traits, entry_point, enter_from_top, ENTRY_LEAD_MS,
)
from ._scroll import (
    ANCHOR_TARGET_W, SCROLLER as ISO_SCROLLER, SCROLL_RANGE as ISO_SCROLL_RANGE, anchor_needed, anchor_point, bring_into_view, in_view as _box_in,
    input_device as _input_device, paced_wheel, plan_trackpad, trackpad_traits, wheel_drift,
)
from ._attention import (
    IDLE_DRIFT_MAX_MS, TEXT_LENGTH as ISO_TEXT_LENGTH, ambient_mode, explore_notches, idle_drifts, read_pause_ms,
)
from ._hold import press_and_hold
from ._hzoptions import (
    _CALLER, ClearcoteStrictError, HumanizeOptions, apply_to_persona,
    engine_mode as _hz_engine_mode, options_of as _hz_options_of, report_fallback as _report_fallback,
    resolve as _hz_resolve,
    tempo_of as _hz_tempo, typo_rate_for as _hz_typo_rate,
)
from . import _frames as _fr
from . import _trace
from ._keys import (
    BLANK_POINT, FOCUSABLE, SEARCH_SLIP_RATE, SELECTION_STATE, TAB_DISTANCE, WORD_FIX_RATE, flight_ms,
    modifier_lag_ms, modifier_lead_ms, plan_gap, plan_repeat, split_chord, submit_pause_ms, tab_pause_ms,
    typing_traits,
)
from ._pacing import keep_in_view as _keep_in_view, trim_to as _trim_ambient, walk as _paced_walk

# IIFE so it runs both as an add_init_script source AND when passed to page.evaluate().
CURSOR_OVERLAY = """(() => {
  if (window.__clearcoteCursor) return; window.__clearcoteCursor = 1;
  const make = () => {
    if (document.getElementById('__clearcote_cursor')) return;
    const d = document.createElement('div'); d.id = '__clearcote_cursor';
    d.style.cssText = 'position:fixed;left:0;top:0;width:20px;height:20px;margin:-10px 0 0 -10px;' +
      'border-radius:50%;border:2px solid #ff3b3b;background:rgba(255,59,59,.22);' +
      'box-shadow:0 0 10px rgba(255,59,59,.6);pointer-events:none;z-index:2147483647';
    (document.body || document.documentElement).appendChild(d);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', make); else make();
  document.addEventListener('mousemove', (e) => {
    const d = document.getElementById('__clearcote_cursor');
    if (d) { d.style.left = e.clientX + 'px'; d.style.top = e.clientY + 'px'; }
  }, true);
})();"""

# Kept for callers of the old name; the map itself lives in _forms (shared with async/Node/.NET).
_NEARBY = NEARBY_LETTERS


# --------------------------------------------------------------- Playwright's error wording
# Playwright words every error after the API the caller used: "Page.click: Timeout 800ms exceeded.",
# "Locator.check: Error: Not a checkbox or radio button". A humanized call raised the error of whatever
# Playwright call it made inside (Locator.wait_for, Locator.is_checked; verifier B, SC 210) -- the
# same type, but a message a caller matching on it would not recognise. Pre-existing in 0.48.0.
_API_PREFIX = re.compile(r"^[A-Za-z_]+\.[A-Za-z_]+: ")


def as_api_error(err, api):
    """``err`` worded as Playwright words an error of ``api`` ("Page.click", "Locator.set_checked"):
    the same class, the inner call's "Locator.wait_for: " prefix replaced (or, for an error the SDK
    raised itself -- a trial that never passed, a checkbox that would not change --, added). Anything
    that is not a Playwright error is returned as it is."""
    try:
        from playwright._impl._errors import Error as PlaywrightImplError
    except Exception:  # noqa: BLE001
        return err
    if not isinstance(err, PlaywrightImplError):
        return err
    msg = err.message if isinstance(getattr(err, "message", None), str) else str(err)
    m = _API_PREFIX.match(msg)
    want = api + ": " + (msg[m.end():] if m else msg)
    if want == msg:
        return err
    try:
        new = type(err)(want)
        for k in ("_name", "_stack", "_details", "_log", "api", "reason", "selector"):
            if hasattr(err, k):
                setattr(new, k, getattr(err, k))
    except Exception:  # noqa: BLE001
        return err
    return new


def api_errors(api, fn):
    """``fn``, with every Playwright error it raises worded as ``api``'s (as_api_error)."""
    @functools.wraps(fn)
    def call(*a, **kw):
        token = _CALLER.set(api) if _CALLER.get() is None else None
        try:
            return fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            n = as_api_error(e, api)
            if n is e:
                raise
            raise n.with_traceback(e.__traceback__) from None
        finally:
            if token is not None:
                _CALLER.reset(token)
    return call


def _rand(a, b):
    return a + random.random() * (b - a)


def typo_rate(persona=None):
    """Per-character fat-finger rate: the persona's own (timing_traits, 0.5-4 % per identity; the
    global 2 % without a persona). CLEARCOTE_HUMANIZE_TYPO_RATE overrides it when set (0 disables
    typos; the test suites raise it to exercise the correction paths)."""
    v = os.environ.get("CLEARCOTE_HUMANIZE_TYPO_RATE")
    if v is None or v == "":
        return timing_traits(persona).typo_rate if persona is not None else DEFAULT_TYPO_RATE
    try:
        f = float(v)
    except ValueError:
        return DEFAULT_TYPO_RATE
    if f != f:   # NaN: not a rate (Node/.NET read it as the default too)
        return DEFAULT_TYPO_RATE
    return min(1.0, max(0.0, f))


# The characters a US-layout keyboard (Playwright's, and the engine's keyboard.getLayoutMap())
# types with Shift held: capitals and the shifted symbols.
_SHIFTED_SYMBOLS = frozenset('~!@#$%^&*()_+{}|:"<>?')


def _needs_shift(ch):
    return "A" <= ch <= "Z" or ch in _SHIFTED_SYMBOLS


def _boxes_overlap(box, rect):
    """Does ``box`` ({x, y, width, height}) cover any of ``rect`` ([x, y, w, h]) beyond a hairline? A label aim
    that still lies over the input's own box is an input click, not a label click (SC 239)."""
    if not rect or len(rect) < 4:
        return False
    return (box["x"] < rect[0] + rect[2] - 0.5 and box["x"] + box["width"] > rect[0] + 0.5
            and box["y"] < rect[1] + rect[3] - 0.5 and box["y"] + box["height"] > rect[1] + 0.5)


# The engine's trajectory command, new name first. r34 renamed it to a neutral name ("humanizedClick" is
# a known tool's vocabulary). The served /json/protocol still lists "humanizedClick" (measured on r38,
# SC 210); r40 keeps "dispatchPointerPath" out of it. The old name still works and is the only one r33
# and older know.
ENGINE_POINTER_PATH = ("Browser.dispatchPointerPath", "Browser.humanizedClick")

# The longest an engine walk waits for the page to draw (ISO_FRAMES): measured 111-812 ms on a fresh
# headless browser's first page (r40, SC 216). A page that never draws (a hidden tab) walks after it.
FRAMES_WAIT_MS = 1500


def _cdp_method_missing(err):
    """True for CDP's "method not found" (-32601), which Playwright reports as "'X' wasn't found"."""
    m = str(err)
    return "wasn't found" in m or "-32601" in m or "method not found" in m.lower()


# CDP's -32602 message is exactly "Invalid parameters" (crdtp DispatchResponse::InvalidParams), after Playwright's
# "Protocol error (method): " prefix and before any data it appends. Playwright strips the code itself.
_CDP_INVALID_PARAMS = re.compile(r"(?:^|: )Invalid parameters(?![A-Za-z])")


def _cdp_invalid_params(err):
    """True for CDP's "invalid params" (-32602) and nothing else: what an r43 engine answers to an EMPTY
    dispatchKeySequence. The code when the text carries it, else the exact -32602 wording. Any other failure of
    the probe -- a proxy's "Invalid target", "Invalid session id" -- is NOT an r43 engine: a browser memoised as
    r43 by mistake would get fromX/fromY on an out-of-sync plain click, which a pre-r43 engine ignores while
    walking from its own stale point (the teleport the re-sync exists to remove), where 0.51.0 declined the
    engine route (verifier A's R4, SC 232: any "invalid" counted)."""
    m = str(err)
    if _cdp_method_missing(err):
        return False
    return "-32602" in m or _CDP_INVALID_PARAMS.search(m) is not None


def _engine_ext_mode():
    """CLEARCOTE_HUMANIZE_ENGINE_EXT=off (also 0/false/no) keeps the r43 engine extensions unused even when the
    engine has them: no fromX/fromY re-sync, no engine drag legs, buttons or typed runs (r42 behaviour)."""
    v = (os.environ.get("CLEARCOTE_HUMANIZE_ENGINE_EXT") or "").strip().lower()
    return v not in ("0", "off", "false", "no", "none")


ENGINE_EXT_MISSING = "the engine has no r43 extensions (Browser.dispatchKeySequence): the SDK planner walks and types"


def engine_path_memo(page):
    """Which engine pointer-path name this page's BROWSER answers, shared by every page on it: a dict
    whose "method" is the name that worked, False when the engine knows neither, absent until asked.
    The engine is one per browser connection, so its answer is too: kept per page, every new page
    paid the failed Browser.dispatchPointerPath probe (-32601) again on an engine older than r34.
    Kept on the Browser (or, with no Browser handle -- a persistent context --, on the context)."""
    ctx = getattr(page, "context", None)
    owner = getattr(ctx, "browser", None) or ctx
    if owner is None:
        return {}
    owner = getattr(owner, "_impl_obj", owner)   # the same object behind every API wrapper
    memo = getattr(owner, "_cc_engine_path", None)
    if memo is None:
        memo = {}
        try:
            owner._cc_engine_path = memo
        except Exception:  # noqa: BLE001
            pass
    return memo


def default_timeout_ms(page):
    """The page's default action timeout (set_default_timeout, else the context's, else 30 s)."""
    try:
        return float(page._impl_obj._timeout_settings.timeout())
    except Exception:  # noqa: BLE001
        return 30000.0


def trial_timeout_error(selector, ms, why):
    """A Playwright TimeoutError for a trial whose checks never passed, worded like Playwright's."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    return PlaywrightTimeoutError("Timeout %dms exceeded.\nCall log:\n  - waiting for locator(%r)\n  - %s"
                                  % (ms, selector, why))


# The least a select's commit (and its read-back) is given when the humane motion already spent the
# caller's timeout: stock select_option commits at once, so a short timeout must not fail a healthy page.
SELECT_COMMIT_FLOOR_MS = 1000.0
SELECT_COMMIT_WHY = "waiting for the page to handle the selection (its change handler has not returned)"


def select_deadline(options, page):
    """(end, ms): when a humanized select_option must be done by (time.monotonic()) and the timeout
    that is, from its `timeout` or the page default, as for Playwright's own; end None for
    timeout=0 (no limit)."""
    t = options.get("timeout")
    ms = float(t) if t is not None else default_timeout_ms(page)
    return (time.monotonic() + ms / 1000.0 if ms > 0 else None), ms


def select_left_ms(dl):
    """What is left of a select deadline for one Playwright wait, never under the commit floor; 0 (no
    limit) when there is no deadline."""
    if dl is None or dl[0] is None:
        return 0
    return max(SELECT_COMMIT_FLOOR_MS, (dl[0] - time.monotonic()) * 1000.0)


def is_navigation(req):
    """A request that navigates one of the page's frames: a change handler that sets location (of the
    page, or of the frame a select lives in). Counted only around a commit; a stray one skips nothing
    but the read-back, which stock select_option never makes."""
    try:
        return bool(req.is_navigation_request())
    except Exception:  # noqa: BLE001
        return False


def bounded_sync(owner, call, ms):
    """``call(owner._impl_obj)`` -- a Playwright impl coroutine -- run through the sync object
    ``owner`` with a deadline of ``ms`` (0: none). True when it finished, False when the deadline
    passed first: the waiter is cancelled and Playwright drops the late answer. Calls like
    keyboard.press take no timeout, and a renderer blocked by the page never acknowledges them.
    None when there is no deadline or ``owner`` is not a Playwright sync wrapper: the caller makes
    the plain call."""
    sync, impl = getattr(owner, "_sync", None), getattr(owner, "_impl_obj", None)
    if not ms or sync is None or impl is None:
        return None
    import asyncio

    async def run():
        try:
            await asyncio.wait_for(call(impl), ms / 1000.0)
            return True
        except asyncio.TimeoutError:
            return False
    return sync(run())


def commit_select(page, selector, dl, delay, settle_ms, press):
    """Enter, which commits a select's pick and runs the page's change handlers, bounded by the
    select's deadline ``dl`` (select_deadline) like Playwright's own select_option. A handler that
    blocks the renderer (a sync XHR that is never answered) leaves the key unacknowledged for good:
    Playwright's TimeoutError instead. ``press()`` is the plain Enter, for a keyboard that is not
    Playwright's sync wrapper; ``delay`` its hold. Then ``settle_ms`` of rest. True when the commit
    navigated the page: the document is leaving and cannot answer a read-back (stock select_option
    returns at once there too)."""
    navs = []

    def on_request(req):
        if is_navigation(req):
            navs.append(req)
    listen = callable(getattr(page, "on", None)) and callable(getattr(page, "remove_listener", None))
    if listen:
        page.on("request", on_request)
    try:
        done = bounded_sync(page.keyboard, lambda k: k.press("Enter", delay=delay), select_left_ms(dl))
        if done is None:
            press()
        elif not done:
            raise trial_timeout_error(selector, dl[1], SELECT_COMMIT_WHY)
        if listen and callable(getattr(page, "wait_for_timeout", None)):
            page.wait_for_timeout(settle_ms)   # unlike time.sleep, delivers a navigation's request event
        else:
            time.sleep(settle_ms / 1000.0)
    finally:
        if listen:
            page.remove_listener("request", on_request)
    return bool(navs)


def _engine_clicks_mode():
    """Which clicks the engine pointer path (Browser.dispatchPointerPath) may walk, from
    CLEARCOTE_HUMANIZE_ENGINE_CLICKS: "off" (also 0/false/no) never; "all" (also "selector") coordinate
    clicks AND targeted page.click(selector) / locator.click() clicks; anything else (the default)
    coordinate clicks only, as before.

    Targeted clicks stay on the SDK planner by default (SC 210 lane S, measured on r38): the engine
    presses 9 ms after its last move (the planner's press waits 40-130 ms), and it walks from its own
    last position, which an SDK glide does not update."""
    v = (os.environ.get("CLEARCOTE_HUMANIZE_ENGINE_CLICKS") or "").strip().lower()
    if v in ("0", "off", "false", "no", "none"):
        return "off"
    if v in ("all", "selector", "selectors"):
        return "all"
    return "coords"


# Keys a person rolls over: unshifted, one physical key each. Shifted characters, Enter/Backspace and
# the typo path keep the plain one-press-at-a-time path.
_ROLL_KEYS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789 ,.;'/-=")


def _host_platform():
    """The persona platform when the page cannot say: the SDK's default persona follows the host."""
    if sys.platform.startswith("win"):
        return "windows"
    return "macos" if sys.platform == "darwin" else "linux"


def _shift_lead_s():
    """ShiftLeft keydown -> the shifted key's keydown (a person presses Shift first)."""
    return _rand(35, 120) / 1000.0


def _shift_lag_s():
    """The shifted key's keyup -> ShiftLeft keyup (Shift comes up just after the letter)."""
    return _rand(10, 70) / 1000.0


def _nearby_key(ch):
    lo = ch.lower()
    if lo in _NEARBY:
        w = random.choice(_NEARBY[lo])
        return w.upper() if ch.isupper() else w
    return ch


def _sel_kw(options, *names):
    """The subset of Playwright options a native fallback call understands."""
    return {k: options[k] for k in names if k in options}


# ------------------------------------------------------------------------- select_option wants
def select_wants(value=None, index=None, label=None):
    """Playwright's select_option arguments -> the wanted options as (kind, x) pairs, in the order
    given, or None when the humane route does not apply (nothing asked, an ElementHandle, an option
    dict naming more than one field). Mirrors Node's selectWants.

    Kinds: "value" (a bare string: value OR label, Playwright's valueOrLabel), "valueOnly" (a
    {"value": v} dict: the value alone, as Playwright's option object), "label" ({"label": l} or
    ``label=``), "index" ({"index": i} or ``index=``). Playwright Python takes no option dicts itself
    (Node does); the humanized call accepts them, and the native fallback gets them translated
    (native_select_args)."""
    out = []

    def add(kind, v):
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(x, dict):
                keys = [k for k in ("value", "label", "index") if x.get(k) is not None]
                if len(keys) != 1:
                    return False
                k = keys[0]
                if k == "index":
                    if isinstance(x["index"], bool) or not isinstance(x["index"], int):
                        return False
                    out.append(("index", x["index"]))
                elif k == "label":
                    out.append(("label", str(x["label"])))
                else:
                    out.append(("valueOnly", str(x["value"])))
            elif kind == "index":
                if isinstance(x, bool) or not isinstance(x, int):
                    return False
                out.append(("index", x))
            elif isinstance(x, str):
                out.append((kind, x))
            else:
                return False   # an ElementHandle, or anything else: native
        return True

    for kind, v in (("value", value), ("index", index), ("label", label)):
        if v is not None and not add(kind, v):
            return None
    return out or None


def want_index(opts, kind, w):
    """The option index one want resolves to, Playwright's way, or -1."""
    if kind == "valueOnly":
        for i, o in enumerate(opts):
            if o.get("value") == w:
                return i
        return -1
    return match_option(opts, **{kind: w})


def native_select_args(value, options):
    """(value, options) for Playwright's own select_option when ``value`` holds option dicts, which
    Playwright Python does not take: {"value": v} -> value= (valueOrLabel, the closest it has),
    {"label": l} -> label=, {"index": i} -> index=, merged with what the caller passed. Anything
    without a dict is returned as it is."""
    items = value if isinstance(value, (list, tuple)) else [value]
    if not any(isinstance(x, dict) for x in items):
        return value, options
    o = dict(options)
    vals, idx, labs = [], [], []
    for x in items:
        if isinstance(x, dict):
            if x.get("value") is not None:
                vals.append(str(x["value"]))
            elif x.get("label") is not None:
                labs.append(str(x["label"]))
            elif x.get("index") is not None:
                idx.append(x["index"])
        elif x is not None:
            vals.append(x)

    def merge(name, more):
        if not more:
            return
        have = o.get(name)
        have = [] if have is None else (list(have) if isinstance(have, (list, tuple)) else [have])
        o[name] = have + more
    merge("index", idx)
    merge("label", labs)
    return (vals if vals else None), o


# Checkbox clicks on the <label> (see _set_checked). The persona fixes the TRAIT -- how often this identity
# clicks a visible checkbox's label rather than its box (persona.label_share when the persona carries one,
# else 0.4) --; each click's choice is an UNSEEDED draw, like plan_move's, or every session of an identity
# would make the same choices in the same order (a cross-session fingerprint). The draw comes from a
# process-level Random of its own (seeded from OS entropy), so the global random stream is not shifted.
LABEL_SHARE = 0.4
_LABEL_RNG = random.Random()

# Keys the typed-run score trace leaves out (their down/up brackets the real keys; _motionscore.typed scores
# the characters' cadence).
_MOD_KEYS = frozenset(("Shift", "Control", "Alt", "Meta", "ControlOrMeta"))


def label_share(persona):
    """The persona's trait: the share of visible-checkbox clicks that land on the label text."""
    v = getattr(persona, "label_share", None)   # an explicit override (tests)
    if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0:
        return float(v)
    # lane B's aim traits (salted stream of the seed, 0.25-0.55); LABEL_SHARE without a persona
    return aim_traits(persona).label_share if isinstance(persona, Persona) else LABEL_SHARE


def label_draw(persona, rng=None):
    """This click: on the label? An unseeded draw against the persona's trait (``rng``: a test's own)."""
    return (rng or _LABEL_RNG).random() < label_share(persona)


# ------------------------------------------------------------------------- strict / fallback report
# The coverage paths (tap, frame routes) report through lane A's report_fallback (_hzoptions): strict mode
# raises ClearcoteStrictError, otherwise on_fallback hears the event. The last event is also kept on
# page._clearcote_last_fallback (read by tests and by callers without a hook).
TAP_NOT_HUMANIZED = "tap is not humanized for touch contexts yet"


def strict_mode(page):
    """Is strict humanize mode on for ``page`` (a fallback to native input must raise, not happen)? The page's
    HumanizeOptions decide; a stand-in page without options may set ``_clearcote_strict``."""
    o = _hz_options_of(page)
    if o is not None:
        return o.strict is True
    return getattr(page, "_clearcote_strict", False) is True


def report_fallback(page, api, reason, selector=None):
    """A humanized API is about to fall back to native input: lane A's report (strict raises, else on_fallback;
    the event is kept as ``page._clearcote_last_fallback``)."""
    if _hz_options_of(page) is None and strict_mode(page):
        raise ClearcoteStrictError(
            "humanize strict: %s would fall back to a stock Playwright call (%s)"
            % ("%r" % (selector,) if selector else "the action", reason),
            api=api, reason=reason, selector=selector)
    return _report_fallback(page, api, reason, selector)


def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay.

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random.

    IDEMPOTENT: a page reaches here twice (install_humanize wraps browser.new_page AND hooks the
    context's "page" event; a context's existing pages are attached again by a second install), and a
    second attach would wrap the already-wrapped mouse -- every sample of the outer glide walking a
    whole inner one (measured in Node: 18x). The ``_clearcote_humanized`` marker is the guard."""
    if show_cursor and getattr(page, "_clearcote_cursor", False) is not True:
        try:
            page._clearcote_cursor = True
        except Exception:  # noqa: BLE001
            pass

        def inject():
            try:
                page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        inject()
        page.on("load", lambda _=None: inject())
        page.on("framenavigated", lambda f: inject() if f == page.main_frame else None)
    if humanize is None or humanize is False or getattr(page, "_clearcote_humanized", False) is True:
        return

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is consistent within a session and unlinkable across seeds. Stored on the page so
    # the module-level Locator.drag_to patch can reach it too.
    # humanize= True | "strict" | HumanizeOptions | dict (_hzoptions). True is HumanizeOptions(): every
    # default reproduces 0.51.0, and no option draws from the persona RNG (same seed, same persona).
    hz = _hz_resolve(humanize)
    if hz is None:
        return
    page._clearcote_hz = hz
    persona = apply_to_persona(make_persona(seed), hz)
    page._clearcote_persona = persona
    tempo = hz.tempo   # preset: every planned duration x1.4 / x1.0 / x0.7
    # The stock methods, for per-call humanize=False (wired at the end of attach).
    _stock = {n: getattr(page, n, None) for n in _PAGE_METHODS}
    _stock_mouse = {n: getattr(page.mouse, n, None) for n in _MOUSE_METHODS}
    _stock_kb = {n: getattr(page.keyboard, n, None) for n in _KB_METHODS}

    # The typing identity (WPM, bigram factors, Tab preference, typo rate, pauses), from a salted side
    # stream of the persona seed (_keys.typing_traits): the persona's own draws are unchanged.
    traits = typing_traits(persona.seed)
    # The trackpad identity (report rate, inertia decay): another salted side stream of the seed (R2).
    tp_traits = trackpad_traits(persona.seed)
    # Keys the CALLER holds through keyboard.down (key -> when it went down): a chord does not press a
    # modifier that is already down, typing does not press Shift under a caller's Shift, and the key's
    # keyup waits out a human hold (hkb_down / hkb_up).
    held_keys = {}

    def _kdwell():
        """A keydown->keyup hold: lane B's key_dwell (log-normal around the persona's median, preset-scaled)."""
        return key_dwell(persona)

    def _tab_pref():
        v = getattr(persona, "tab_pref", None)
        return float(v) if isinstance(v, (int, float)) else traits.tab_pref

    def _typo_rate():
        """Per-character slip rate: lane A's typo_rate option, else lane B's typo_rate(persona)
        (CLEARCOTE_HUMANIZE_TYPO_RATE when set, else the persona's 0.5-4 %)."""
        return _hz_typo_rate(page, lambda: typo_rate(persona))

    # Tracked cursor position so each move continues from where the last one ended.
    # Seed at a plausible on-page spot (not 0,0) so the first glide doesn't start in the corner.
    # "placed" records whether the REAL pointer has been moved on this page yet: until it has, the
    # browser's cursor is still at the (0,0) origin no matter what pos claims. That distinction is
    # what a wheel event (dispatched at the current pointer), a bare mouse.down() and a
    # keyboard-only session all depend on — see _wheel_anchor / hdown / _pointer_presence.
    st = {"pos": (_rand(140, 380), _rand(90, 240)), "held": False,
          "placed": False, "kb_primed": False}

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel
    # getattr, not attribute access: attach must not raise on a page-like object that lacks a
    # method we only optionally wrap. Reading these directly widened the attach-time contract and
    # took four previously-green tests down with an AttributeError before anything was wrapped —
    # humanize failing closed on attach is the one outcome this module must never produce, because
    # it silently disables every other humanization along with it.
    native_down = getattr(mouse, "down", None)
    native_up = getattr(mouse, "up", None)

    def _dispatch(steps, kind="glide", buttons=0):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol time.sleep. ``kind`` / ``buttons`` name the walk for the score hook (_trace, lane G:
        glide | drag (buttons 1) | ambient | hover). Native input carries button state, so a button held via
        mouse.down() stays pressed across the whole path (drag).

        Paced against a WALL CLOCK (_pacing.walk): each sample goes out at its planned time, and
        late samples are skipped (never the last, and no skip hops more than 40 px, or 20 px once
        nothing has been dispatched for 40 ms: _pacing.MAX_JUMP_PX / IDLE_HOP_PX). "Move, then sleep the
        planned gap" added the ~27 ms mouse.move round trip to every 6-8 ms sample: a 1.05 s planned
        glide took 3.5-5.4 s on PRO r38 (SC 209), ambient_motion(1500) 5.7x as long."""
        def mv(sx, sy):
            native_move(sx, sy)
            st["placed"] = True
        vp = _view_bounds()
        if tempo != 1.0:
            steps = [(sx, sy, sl * tempo) for sx, sy, sl in steps]
        _paced_walk(_keep_in_view(steps, *vp) if vp else steps, mv, kind=kind, buttons=buttons)

    def _view_bounds():
        """The viewport (innerWidth, innerHeight) a planned path is kept inside (_pacing.keep_in_view:
        the planner's overshoot arcs left the window near its edges, verifier B), read in the isolated
        world at most every 2 s; None when it cannot be read."""
        now = time.monotonic()
        c = st.get("vp")
        if c is None or now - c[0] > 2.0:
            try:
                wh = world_for(page).evaluate(ISO_VIEWPORT)
                vp = (float(wh[0]), float(wh[1])) if wh and wh[0] and wh[1] else None
            except Exception:  # noqa: BLE001
                vp = None
            st["vp"] = c = (now, vp)
        return c[1]

    def _glide(x, y, settle=False, target_w=24, held=False):
        """Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective
        homing) with colored sub-pixel noise and Fitts-scaled duration (see _motion.plan_move).
        Continuous (starts at the tracked position, lands exactly on target).

        A page the pointer has not been over yet (nothing placed it): the path comes in over the top
        edge, as the engine's own first path does (pro-r43-13), and the samples still above the view
        are not sent (_motion.entry_point / enter_from_top) -- the page's first pointer event is at
        the edge, not mid-page (lab row H 13). A drag leg never starts a page: its press glided first."""
        frm = st["pos"]
        entering = not st["placed"] and not held and not settle
        if entering:
            vp = _view_bounds()
            frm = entry_point((x, y), vp[0] if vp else None)
        steps = plan_move(frm, (x, y), persona, target_w=target_w, settle=settle, held=held)
        if entering:
            steps = enter_from_top(steps, frm)
        _dispatch(steps, kind="drag" if held else "glide", buttons=1 if held else 0)
        st["pos"] = (x, y)
        st["typed"] = None   # the hand left the keyboard: no submit / Tab pause owed (_after_typing)

    def _ambient_plan(ms, box, rng=None):
        """plan_ambient from the tracked position -- or, on a page the pointer has not been over yet, from a point
        above the view with the samples still above it dropped (_motion.entry_point / enter_from_top): the drift's
        first excursion comes in over the top edge, as _glide's first move does (pro-r43-13's rule, lab row H 13).
        Planned ENTRY_LEAD_MS longer then, so the dropped lead-in (~100-200 ms) leaves the walk no shorter than
        ``ms``; the caller trims to ``ms``. ``rng`` None: unseeded (the attention model passes its own)."""
        if st["placed"]:
            return plan_ambient(st["pos"], box, persona, ms, rng)
        start = entry_point(st["pos"], box["width"], rng)
        return enter_from_top(plan_ambient(start, box, persona, ms + ENTRY_LEAD_MS, rng), start, rng)

    # --- Engine real-trajectory routing (PRO) --------------------------------
    # Route point-to-point moves/clicks through the browser engine's
    # Browser.dispatchPointerPath (humanizedClick before r34): a trusted WebMouseEvent path built in the browser
    # process from REAL recorded human trajectories (PRO) or the engine bezier
    # (free) -- tiered automatically by the engine's runtime license gate, so the
    # SDK stays tier-agnostic. One CDP call per action (far less Input-domain
    # traffic than per-step native moves); continuity is tracked engine-side.
    # Held-button DRAGS stay on the native path (the engine click can't hold a
    # button across the path -- sliders depend on it); wheel/typing unchanged.
    # Falls back to the SDK bezier (plan_move) permanently if the method is
    # unavailable (older engine, or a persistent context with no Browser handle).
    _eng = {"session": None, "tid": None, "ok": True, "method": None, "at": None}

    def _engine_in_sync():
        """The engine walks from ITS OWN last position on this page, not from where an SDK glide left
        the pointer. Measured on r38 (SC 210): an engine click at (300,300), an SDK glide to (800,550),
        then mouse.click(400,200) through the engine -- its first sample was at (300,300), a 559 px
        teleport from where the pointer was. So the engine walks only while nothing else has moved
        the pointer since its own last walk (before its first walk: while nothing has moved it)."""
        if _eng["at"] is None:
            return not st["placed"]
        return st["pos"] == _eng["at"]

    def _engine_session():
        if _eng["session"] is None:
            tsession = page.context.new_cdp_session(page)
            _eng["tid"] = tsession.send(
                "Target.getTargetInfo")["targetInfo"]["targetId"]
            b = getattr(page.context, "browser", None)
            _eng["session"] = (b.new_browser_cdp_session()
                               if b is not None else tsession)

    def _maybe_report_ext():
        """Lane A's on_fallback hears ONCE per browser that a PRO engine (it has the pointer path) lacks the r43
        extensions. The hook only: the SDK planner does the work, so this is not a native fallback and strict mode
        does not raise."""
        memo = engine_path_memo(page)
        if memo.get("ext") is not False or not memo.get("method") or memo.get("ext_reported"):
            return
        memo["ext_reported"] = True
        o = _hz_options_of(page)
        fn = getattr(o, "on_fallback", None) if o is not None else None
        if fn is not None:
            fn({"api": "engine_ext", "reason": ENGINE_EXT_MISSING, "selector": None})

    def _engine_ext_ok():
        """Does this browser's engine take the r43 dispatchPointerPath parameters and dispatchKeySequence (and
        answer after its last event)? Asked once per browser, only when needed: an empty dispatchKeySequence
        answers -32602 (invalid params) on such an engine and -32601 (not found) before r43; anything else counts
        as no (an engine that accepts no keys is not r43). Off with CLEARCOTE_HUMANIZE_ENGINE_EXT=off."""
        if not _eng["ok"] or not _engine_ext_mode():
            return False
        memo = engine_path_memo(page)
        if "ext" not in memo:
            try:
                _engine_session()
                _eng["session"].send(ENGINE_KEY_SEQUENCE, {"targetId": _eng["tid"], "keys": []})
                memo["ext"] = False
            except Exception as e:  # noqa: BLE001
                memo["ext"] = _cdp_invalid_params(e)
            _maybe_report_ext()
        return memo["ext"]

    def _engine_glide(x, y, no_click, ext=None):
        """One engine pointer path to (x, y). ``ext``: r43 parameters (button, clickCount, modifiers, buttons,
        settle), only sent to an engine that takes them. On such an engine the path also starts where THIS SDK
        left the pointer (fromX/fromY) instead of requiring the engine to be in sync, and no sleep follows (it
        answers after its last event). A plain in-sync left click never probes: the r42 route is unchanged."""
        if not _eng["ok"]:
            return False
        use_ext = bool(ext or not _engine_in_sync()) and _engine_ext_ok()
        if not use_ext and (ext or not _engine_in_sync()):
            return False
        memo = engine_path_memo(page)
        if memo.get("method") is False:   # this browser's engine has neither name: no probe again
            _eng["ok"] = False
            return False
        try:
            _engine_session()
            # The engine does not wait for the page: what it walks into a page that has not drawn yet is
            # lost (a fresh headless browser's first page, SC 216). Wait for two frames first; free once
            # drawn. Best effort: a wait that fails still walks (the engine route stays on).
            try:
                world_for(page).evaluate(ISO_FRAMES, FRAMES_WAIT_MS, await_promise=True)
            except Exception:  # noqa: BLE001
                pass
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            dur *= tempo
            params = {"targetId": _eng["tid"], "x": float(x), "y": float(y),
                      "duration": dur, "noClick": bool(no_click)}
            if use_ext:
                if st["placed"] and not _engine_in_sync():
                    # r43: start where this SDK left the pointer (a glide, a native fallback); before any
                    # move the engine enters the page over its top edge by itself (pro-r43-13).
                    params["fromX"], params["fromY"] = float(st["pos"][0]), float(st["pos"][1])
                params.update(ext or {})
            t_send = time.monotonic()
            method = _eng.get("method") or memo.get("method")
            if method:
                _eng["session"].send(method, params)
            else:
                # New name first; an engine older than r34 answers "wasn't found" (-32601)
                # and gets the old one. Any other error is a real failure (below). The answer is
                # remembered for the whole browser (engine_path_memo), not just this page.
                try:
                    _eng["session"].send(ENGINE_POINTER_PATH[0], params)
                    method = ENGINE_POINTER_PATH[0]
                except Exception as e:  # noqa: BLE001
                    if not _cdp_method_missing(e):
                        raise
                    try:
                        _eng["session"].send(ENGINE_POINTER_PATH[1], params)
                    except Exception as e2:  # noqa: BLE001
                        if _cdp_method_missing(e2):
                            memo["method"] = False
                        raise
                    method = ENGINE_POINTER_PATH[1]
                memo["method"] = method
            _eng["method"] = method
            _maybe_report_ext()
            # The engine runs the trajectory in the BROWSER process and answers this command
            # before it has finished walking it. Without waiting, a following mouse.down() fires
            # at the cursor's stale position -- measured: a move to (110,58) then a press landed
            # at (981,629), on <body> instead of the slider, so every move-then-press sequence
            # (drag, press-and-hold, drag-and-drop) acted on the wrong element.
            # r38 answers only after the walk AND the click (send returned 772 ms after a 600 ms
            # walk, 2 ms after the release, SC 210): wait only for what is left of `dur`, so an
            # engine that answers late does not add a second, idle `dur` after every click.
            if not use_ext:   # r43 answers after its last sample / release: nothing left to wait for
                time.sleep(max(0.0, dur - (time.monotonic() - t_send)))
            # Keep Playwright's own cursor position in sync so a following
            # mouse.down()/drag presses where the engine left the cursor.
            try:
                native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            _eng["at"] = st["pos"]
            return True
        except Exception:  # noqa: BLE001 - method missing / no browser session: stop trying
            _eng["ok"] = False
            return False

    def hmove(x, y, **kw):
        # A HELD BUTTON MUST STAY ON THE NATIVE PATH.
        #
        # The engine trajectory (Browser.humanizedClick) builds its own WebMouseEvents and cannot
        # hold a button across them, so a drag leg routed through it emits moves reporting
        # MouseEvent.buttons 0 in the middle of a press. That is both a contradiction no real
        # mouse produces (buttons is derived from physical state) and a functional break: a range
        # thumb ignores a move that says nothing is down. Measured on this file without the guard,
        # dragging a 420px slider: the press landed on the INPUT correctly and 349 of 354 moves
        # still reported buttons 0, so the thumb fired one input event and the value went 0 -> 1.
        #
        # A BARE MOVE IS ALSO NATIVE, for a second and independent reason: the engine walks its
        # path ASYNCHRONOUSLY in the browser process and answers before it has finished, so it
        # keeps moving the cursor AFTER the pre-press pin in hdown. Measured here on this file,
        # move to (56,120) then mouse.down(): the button was carried correctly (407 of 560 moves
        # reported buttons != 0) but the press landed on HTML at (856,213) instead of the INPUT,
        # so the thumb never received it and the value stayed 0. Re-pinning immediately before the
        # press does not help — the overrunning trajectory outlives the pin.
        #
        # _glide is still fully humanized (minimum-jerk sum-of-submovements, colored noise,
        # Fitts-scaled duration); only the engine's recorded-trajectory SOURCING is given up for
        # bare moves. hclick keeps it, because it performs the whole move+click engine-side where
        # nothing races the trajectory.
        # A move while the button is held is a DRAG leg: plan_drag keeps it on the line between grab
        # and drop (an aimed reach's arc took a slider handle 47 px off a 40 px track, SC 209).
        # r43 engine (SC 232 lane H): a held leg CAN take the engine route -- dispatchPointerPath {buttons}
        # carries the held button on every sample and answers after the last one, so neither reason above
        # holds there.
        if (st["held"] and _hz_engine_mode(page, _engine_clicks_mode) != "off" and _engine_ext_ok()
                and _engine_glide(x, y, no_click=True, ext={"buttons": engine_held_bits(st.get("held_button"))})):
            return
        _glide(x, y, held=st["held"])

    def hdown(**kw):
        # A press needs a POSITION behind it. mouse.down() with nothing having moved the pointer
        # first presses at the (0,0) origin, so a bare down -> move -> up drag grabs the corner of
        # <body> instead of the thing it meant to drag, and the slide silently does nothing. Give
        # the press a real origin: a short ambient placement, then travel to the point we are about
        # to press, so the button goes down at the END of a movement the way a hand does it.
        # DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
        #
        # An earlier revision ran an ambient walk here and pressed wherever it ended. But before
        # any real move, st["pos"] is the RANDOM SEED assigned at attach (_rand(140,380),
        # _rand(90,240)) -- not a coordinate the caller ever named -- so that turned a harmless
        # no-op press into a TRUSTED click at an arbitrary page position, which can land on a
        # link and navigate. A bare down() with no preceding move is a caller mistake; inventing
        # a target makes it a worse one. Callers wanting a humanized drag move first, and that
        # move glides and sets "placed".
        if st["placed"]:
            # Pin the cursor to the tracked target before pressing. The engine answers
            # humanizedClick before its trajectory has finished AND overruns the duration it
            # reports, so waiting that duration is not sufficient on its own -- measured on Node,
            # a move to (110,58) was still mid-flight at (416,504) when the press fired, landing
            # on <body>. Only meaningful once a real move has happened: that is what makes
            # st["pos"] correspond to where the cursor actually is.
            try:
                native_move(*st["pos"])
            except Exception:  # noqa: BLE001 - best effort: keep the press on target
                pass
            # The hand has arrived; it confirms the target before the button goes down. The pin
            # above is itself a pointermove, so the pause comes after it: pressed straight after
            # the move, a drag grab came ~21 ms after the last pointermove (measured on r38). A
            # down() with no move before it (never placed) stays instant: nothing arrived.
            _pre_press()
        # try/finally semantics so a raising press cannot strand held=True and turn every later
        # move into a phantom drag leg. hup already guarded this; hdown did not.
        try:
            st["held"] = True
            st["held_button"] = kw.get("button") or "left"
            native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    def hup(**kw):
        try:
            native_up(**kw)
        finally:
            st["held"] = False

    def _platform():
        """The platform the page believes (the persona's), read once per page."""
        if "platform" not in st:
            plat = None
            try:
                plat = platform_from_navigator(world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["platform"] = plat or _host_platform()
        return st["platform"]

    def _pre_press():
        """The pause between the pointer coming to rest and the button going down (_forms.pre_press_ms:
        40-250 ms, right-skewed, centred per identity)."""
        time.sleep(pre_press_ms(persona.seed) * tempo / 1000.0)

    def _press_here(x, y, hold=None):
        """Press and release the left button where the last glide left the pointer, after the
        pre-press pause. Native down/up, so nothing moves: Playwright's mouse.click(x, y) sends one more
        mouseMoved first, and that move -- not the glide -- was the last pointermove the page saw, ~4 ms
        before the press (the typing focus click, measured on r38)."""
        hold = click_hold(persona) if hold is None else hold
        if native_down is None or native_up is None:
            _pre_press()
            native_click(x, y, delay=hold)
            return
        _pre_press()
        t0 = time.monotonic()
        native_down()
        try:
            time.sleep(_hold_rest(hold, t0))
        finally:
            native_up()   # never leave the button down

    def _hold_rest(hold_ms, t0):
        """What is left of a button hold once mouse.down() has returned. The hold runs from when the press
        was SENT: mouse.down() returns only after the page has handled the press, and a <select> opens
        its popup inside that handling, so a hold slept after it reached the page ~30-80 ms longer than
        planned (206 ms median for the open click in the SC 209 lab, planned 60-150 ms)."""
        return max(0.0, hold_ms / 1000.0 - (time.monotonic() - t0))

    def _press_at(x, y, o):
        """Press at (x, y) -- where the last glide ended -- the way the options ask: the button,
        the click count (a double click is two presses with a human gap, not one event), the
        caller's hold (`delay`) or the persona's, and modifiers held around the whole press with a
        lead before it and a lag after, as fingers do. Native down/up, so nothing moves."""
        mods = o["modifiers"]
        held = []
        try:
            for m in mods:
                native_kb_down(m)
                held.append(m)
                time.sleep(_rand(40, 110) / 1000.0)
            _pre_press()    # the hand confirms the target before it presses
            n = o["click_count"]
            for i in range(1, n + 1):
                hold = o["delay"] if o["delay"] is not None else click_hold(persona)
                t0 = time.monotonic()
                native_down(button=o["button"], click_count=i)
                try:
                    time.sleep(_hold_rest(hold, t0))
                finally:
                    native_up(button=o["button"], click_count=i)   # never leave the button down
                if i < n:
                    time.sleep(dblclick_gap(persona) / 1000.0)   # the gap inside a double click: the persona's
        finally:
            for m in reversed(held):
                time.sleep(_rand(20, 80) / 1000.0)
                try:
                    native_kb_up(m)
                except Exception:  # noqa: BLE001
                    pass

    def _engine_ext_click(x, y, o):
        """r43 engine (SC 232 lane H): a right/middle, double/triple or modified click walked AND pressed
        engine-side (dispatchPointerPath button / clickCount / modifiers). The modifier KEYS are pressed here
        around it (lead before, lag after), as _press_at does. False when the engine cannot take it (a
        caller-chosen hold, no r43 extensions, modifiers without keyboard.down): the planner presses then.
        Shared by mouse.click and the targeted click (verifier A's R5: selector clicks never took it)."""
        ext = engine_click_params(o)
        if not ext or _hz_engine_mode(page, _engine_clicks_mode) == "off" or not _engine_ext_ok():
            return False
        if o["modifiers"] and not (native_kb_down and native_kb_up):
            return False
        held = []
        try:
            for m in o["modifiers"]:
                native_kb_down(m)
                held.append(m)
                time.sleep(_rand(40, 110) / 1000.0)
            return bool(_engine_glide(x, y, no_click=False, ext=ext))
        finally:
            for m in reversed(held):
                time.sleep(_rand(20, 80) / 1000.0)
                try:
                    native_kb_up(m)
                except Exception:  # noqa: BLE001
                    pass

    def hclick(x, y, **kw):
        # The engine path performs ONE plain left click. Anything else -- right/middle button, a
        # double click, a caller-chosen hold -- glides SDK-side and presses as asked; the old
        # wrapper dropped those options and always left-clicked once.
        o = normalize_click_options(kw, _platform())
        if engine_path_ok(o) and _hz_engine_mode(page, _engine_clicks_mode) != "off" and _engine_glide(x, y, no_click=False):
            return
        # r43 engine (SC 232 lane H): right/middle buttons, double/triple clicks and modified clicks take the
        # engine route too (_engine_ext_click).
        if _engine_ext_click(x, y, o):
            return
        _glide(x, y)
        # A press that raises RAISES (it was swallowed here: a click whose press failed reported
        # success). Only the pin before a press (hdown) stays best-effort.
        if native_down is None or native_up is None or (o["modifiers"] and native_kb_down is None):
            _pre_press()
            native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                         **_sel_kw(kw, "button", "click_count"))
        else:
            _press_at(x, y, o)
        # Not after an engine click (returned above): a glide the engine does not see would take the
        # next coordinate click off the engine path (_engine_in_sync).
        _schedule_move_off()

    def hdblclick_xy(x, y, **kw):
        kw = dict(kw)
        kw["click_count"] = 2
        hclick(x, y, **kw)

    def _held_glide(x, y):
        # r43 engine (SC 232 lane H): the held leg + seating jiggle (settle) walked engine-side, buttons carried.
        if (st["held"] and _hz_engine_mode(page, _engine_clicks_mode) != "off" and _engine_ext_ok()
                and _engine_glide(x, y, no_click=True,
                                  ext={"buttons": engine_held_bits(st.get("held_button")), "settle": True})):
            return
        _glide(x, y, settle=True)                # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor activity (idle drift + non-goal moves) so a
        behavioral collector sees pointer entropy BEFORE the first goal action. page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            # plan_ambient overruns its budget (whole excursions, rests drawn at 0.9-1.4x a frame):
            # cut the plan at a slow sample near ms so ambient_motion(ms) lasts about ms. On a page the
            # pointer has not been over yet the drift comes in over the top edge (_ambient_plan).
            steps = _trim_ambient(_ambient_plan(ms, {"width": vp["width"], "height": vp["height"]}), ms)
            _dispatch(steps, kind="ambient")
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # OPT-IN: ambient pointer entropy on page load is OFF by default. A burst that fires
    # on every load (including iframe loads mid-challenge) interleaves with whatever the
    # caller is driving and corrupts precise input like a slider drag; it is also motion
    # no real user necessarily produces at that instant. Callers that want pre-challenge
    # entropy either call page.ambient_motion(ms) explicitly at the moment they want it,
    # or re-enable the per-load burst with page._clearcote_auto_ambient = True.
    page._clearcote_auto_ambient = hz.ambient == "load"
    page._clearcote_ambient = hz.ambient or "off"   # read by the attention/idle code (lane C)

    def _auto_ambient(_=None):
        # ambient="load" (lane A's option, read defensively) is the same per-load burst as the flag.
        if getattr(page, "_clearcote_auto_ambient", False) is True or ambient_mode(page) == "load":
            try:
                hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: _auto_ambient())
    # The attention model's read pause counts from the last load (read lazily by the next action).
    page.on("load", lambda _=None: st.__setitem__("load_at", time.monotonic()))

    # ------------------------------------------------------------ after-action (SC 232 lane C)
    # What the hand does once an action has landed: it rests on a hovered element (hover dwell), drifts
    # off a clicked one (move-off), and between actions it reads and idles (_attention). All of it is
    # LAZY -- run at the start of the next public action, or by page.settle() --, never from a timer or a
    # thread, so nothing races the action in flight. The persona's traits come from a salted stream of its seed
    # (_motion.after_traits: every existing draw keeps its order); every per-action draw is unseeded.
    # Per-action draws are UNSEEDED, as in plan_move: the seed fixes the persona's traits only (after_traits);
    # a seeded per-action sequence would replay identically in every session of an identity.
    arng = random.random
    st.update({"depth": 0, "last_end": None, "load_at": None, "mo": None, "mo_hits": 0, "mo_n": 0})
    # Hidden switch for the attention model until lane A's `ambient="idle"` / `attention` option lands.
    page._clearcote_attention = False
    # Test seam: what the after-action model did, newest last (bounded).
    trace = collections.deque(maxlen=256)
    page._clearcote_after_trace = trace

    def _attention_on():
        return getattr(page, "_clearcote_attention", False) is True or ambient_mode(page) == "idle"

    def _dwell(x, y, box, dwell):
        """Rest on a hover target: OU drift + tremor within 3 px of the arrival point and inside ``box``,
        for the persona's 150-900 ms (``dwell`` ms overrides it; 0 or less: no dwell)."""
        ms = None
        if dwell is not None:
            try:
                ms = float(dwell)
            except (TypeError, ValueError):
                ms = None
            if ms is not None and not math.isfinite(ms):
                ms = None
            if ms is not None and ms <= 0:
                return
        if ms is None:
            ms = dwell_ms(persona, arng)
        steps = plan_dwell((x, y), box, persona, ms, arng)
        trace.append(("dwell", ms, len(steps)))
        _dispatch(steps, kind="hover")
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    def _schedule_move_off():
        """After a press: in the persona's 60-85 % of clicks a move-off comes due 200-700 ms after the
        release. It runs at the start of the next action (or in page.settle()) if it is due by then and
        nothing has moved the pointer since; otherwise it is dropped -- the next glide is the move-off."""
        hit = move_off_roll(persona, arng, st["mo_hits"], st["mo_n"])
        st["mo_n"] += 1
        st["mo"] = None
        if hit:
            st["mo_hits"] += 1
            st["mo"] = {"due": time.monotonic() + move_off_delay_ms(persona, arng) / 1000.0, "pos": st["pos"]}

    def _run_move_off(wait):
        """Make the pending move-off (at most once). ``wait``: sleep until it is due (page.settle())."""
        mo, st["mo"] = st["mo"], None
        if not mo or st["held"] or st["pos"] != mo["pos"]:
            return False
        left = mo["due"] - time.monotonic()
        if left > 0:
            if not wait:
                return False
            time.sleep(left)
        vw, vh = _view_bounds() or _viewport()
        tx, ty = plan_move_off(st["pos"], {"width": vw, "height": vh}, persona, arng)
        trace.append(("move_off", st["pos"], (tx, ty)))
        _glide(tx, ty, target_w=140)   # one coarse glide: nothing to acquire
        return True

    def _idle_drift(ms):
        trace.append(("drift", ms))
        vw, vh = _view_bounds() or _viewport()
        steps = _trim_ambient(_ambient_plan(ms, {"width": vw, "height": vh}, arng), ms)
        _dispatch(steps, kind="ambient")
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    def _attend(coords, now):
        """The attention model (_attention): a read pause after a load, else idle drifts after a gap."""
        # `now`: when the action started -- a move-off glide just made does not count as idle time
        la = st["load_at"]
        if la is not None:
            st["load_at"] = None
            try:
                n = world_for(page).evaluate(ISO_TEXT_LENGTH)
            except Exception:  # noqa: BLE001
                n = 0
            pause = read_pause_ms(n, persona, arng) / 1000.0
            notches = explore_notches(arng)
            trace.append(("read", pause * 1000.0, n, notches if not coords else 0))
            if pause - (now - la) > 0.35:   # the eyes read; the hand drifts once meanwhile
                _idle_drift(min((pause - (now - la)) * 500.0, IDLE_DRIFT_MAX_MS))
            rest = la + pause - time.monotonic()
            if rest > 0:
                time.sleep(rest)
            # A coordinate action names a point in the current layout: never scroll that away.
            if notches and not coords:
                hwheel(0, notches * _notch_px())
            return
        last = st["last_end"]
        if last is not None:
            for ms in idle_drifts(now - last, arng):
                _idle_drift(ms)

    def _pre_action(coords):
        if st["depth"] > 0 or st["held"]:
            return
        st["depth"] += 1
        try:
            now = time.monotonic()
            _run_move_off(False)
            if _attention_on():
                _attend(coords, now)
        except Exception:  # noqa: BLE001 - the after-action model never stops the action
            pass
        finally:
            st["depth"] -= 1

    def _action(fn, coords=False):
        """A public entry point: the lazy after-action step first (outermost call only), then the action.
        The idle gap counts from its end."""
        def run(*a, **kw):
            if st["depth"] > 0 or kw.get("trial"):   # a trial performs no action: nothing after-action either
                return fn(*a, **kw)
            _pre_action(coords)
            st["depth"] += 1
            try:
                return fn(*a, **kw)
            finally:
                st["depth"] -= 1
                st["last_end"] = time.monotonic()
        run.__name__ = getattr(fn, "__name__", "action")
        run.__doc__ = getattr(fn, "__doc__", None)
        return run

    def _touch(fn):
        """mouse.down / up: no after-action step (a press lands where the pointer is), but the idle gap
        counts from them."""
        def run(*a, **kw):
            try:
                return fn(*a, **kw)
            finally:
                st["last_end"] = time.monotonic()
        return run

    def hsettle():
        """page.settle(): let the hand settle after the last action -- wait for a pending move-off to come
        due and make it. False when none was pending (or the pointer has moved since)."""
        if st["depth"] > 0:
            return False
        st["depth"] += 1
        try:
            done = _run_move_off(True)
        except Exception:  # noqa: BLE001
            done = False
        finally:
            st["depth"] -= 1
        if done:
            st["last_end"] = time.monotonic()
        return done

    def _install_after_action():
        """Wrap the public entry points (called once, after they are all in place)."""
        for name in ("move", "click", "wheel", "dblclick"):
            if getattr(mouse, name, None) is not None:
                setattr(mouse, name, _action(getattr(mouse, name), coords=True))
        if native_down is not None and native_up is not None:
            mouse.down, mouse.up = _touch(mouse.down), _touch(mouse.up)
        for name in ("type", "press"):
            setattr(kb, name, _action(getattr(kb, name)))
        for name in ("click", "hover", "dblclick", "type", "fill", "press", "select_option", "check", "uncheck",
                     "set_checked", "press_and_hold", "ambient_motion", "focus", "drag_and_drop", "drag_by"):
            if getattr(page, name, None) is not None:
                setattr(page, name, _action(getattr(page, name)))
        page.settle = hsettle

    def _viewport():
        """The REAL viewport, in CSS px.

        Not page.viewport_size: launch() and launch_persistent_context() both force
        viewport=None on every headed page (so innerWidth tracks the real OS window instead of
        Playwright's emulated 1280x720 -- an emulated viewport on a headed window is itself a
        tell). With viewport=None, viewport_size() returns None for the life of the page, so the
        old `page.viewport_size or {1280, 800}` fell through to that constant on EVERY headed
        launch. The anchor gate below then judged "is the pointer inside the viewport" against a
        box unrelated to the window: on a maximized display a cursor legitimately at x=1400 read
        as out-of-bounds and got re-homed on every single scroll, which is exactly the behaviour
        the gate exists to prevent. innerWidth/innerHeight are the truth in both modes.
        """
        try:
            wh = world_for(page).evaluate(ISO_VIEWPORT)
            if wh and wh[0] and wh[1]:
                return float(wh[0]), float(wh[1])
        except Exception:  # noqa: BLE001
            pass
        vp = page.viewport_size or {"width": 1280, "height": 800}
        return float(vp["width"]), float(vp["height"])

    def _wheel_anchor():
        """Put the cursor over the content before scrolling it.

        A wheel event is delivered at wherever the pointer currently is, and until something has
        moved it that is the (0,0) origin — so the first scroll on a freshly loaded page arrives
        in the top-left corner, over whatever element happens to live there, which is not where a
        hand ever scrolls. Only re-home when the pointer is nowhere sensible: a person does not
        move the mouse between two scrolls of the same page, so re-anchoring on every wheel call
        would be its own tell.
        """
        try:
            vw, vh = _viewport()
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if not anchor_needed(st["pos"], vw, vh, st["placed"], st["held"]):
                return
            # Reading position: upper-middle, gaussian-spread (_scroll.anchor_point: the same numbers in
            # scroll.ts and Scroll.cs). The exact centre is itself a tell.
            ax, ay = anchor_point(vw, vh)
            _glide(ax, ay, target_w=ANCHOR_TARGET_W)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    def _notch_px():
        """Pixels per wheel notch for the platform the page believes, read once per page."""
        if "notch" not in st:
            plat = None
            try:
                plat = platform_from_navigator(world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["notch"] = WHEEL_NOTCH_PX[plat or _host_platform()]
        return st["notch"]

    def _device():
        """"trackpad" or "mouse" for this page: the caller's ``input_device`` option when the options
        object (lane A) carries one, else a trackpad for a macOS persona. Read once per page."""
        if "device" not in st:
            hzo = _hz_options_of(page)   # lane A's HumanizeOptions.input_device first (then the stand-in seams)
            opt = hzo.input_device if hzo is not None else None
            for holder in (getattr(page, "_clearcote_options", None), getattr(page, "_clearcote_humanize_options", None)):
                if opt or holder is None:
                    continue
                opt = holder.get("input_device") if isinstance(holder, dict) else getattr(holder, "input_device", None)
                if opt:
                    break
            if not isinstance(opt, str):
                opt = getattr(page, "_clearcote_input_device", None)
            st["device"] = _input_device(_platform(), opt if isinstance(opt, str) else None)
        return st["device"]

    def _tsleep(sec):
        time.sleep(sec * tempo)

    def _scroll_io():
        """The humanizer, as _scroll.bring_into_view sees it."""
        class _IO:
            rng = staticmethod(random.random)
            viewport = staticmethod(_viewport)
            pointer = staticmethod(lambda: st["pos"])
            placed = staticmethod(lambda: st["placed"])
            held = staticmethod(lambda: st["held"])
            glide = staticmethod(lambda x, y, w: _glide(x, y, target_w=w))
            wheel = staticmethod(lambda dx, dy: native_wheel(dx, dy))
            sleep = staticmethod(_tsleep)   # the preset scales the scroll-into-view pauses
            notch_px = staticmethod(_notch_px)
            device = staticmethod(_device)
            trackpad = staticmethod(lambda: tp_traits)

            @staticmethod
            def scroll_range():
                r = world_for(page).evaluate(ISO_SCROLL_RANGE)
                return r if isinstance(r, list) and len(r) == 4 else None
        return _IO

    def _hwheel_run(delta_x, delta_y, send_wheel):
        """Scroll in WHOLE NOTCHES, the only thing a wheel emits.

        A CDP wheel event always reports wheelDelta +-120 (one notch), whatever its distance. The
        eased slices this replaced (39/28/20/10/3 px for a 100 px scroll, each at wheelDelta 120,
        measured on r32/r33) were events no mouse produces: a real notch moves 100 px on Windows,
        120 on Linux, 40 on macOS. So the total is rounded to the nearest whole notch (at least
        one), and the notches come in finger-flick bursts.

        A TRACKPAD persona (_device(): the caller's ``input_device``, else macOS) scrolls the exact
        distance as a two-finger swipe instead: 2-40 px events at the persona's 60-120/s on a min-jerk
        bell, then an inertia tail (_scroll.plan_trackpad, _scroll.trackpad_traits). Playwright sends
        every wheel as pixel deltas, so a page sees deltaMode 0 either way; a notch persona's deltas are
        whole multiples of its notch."""
        _wheel_anchor()
        if _device() == "trackpad":
            paced_wheel(plan_trackpad(delta_x, delta_y, tp_traits), send_wheel, time.sleep)
            return
        notch = _notch_px()
        plan = ([(0, notch if delta_y > 0 else -notch)] * wheel_notches(delta_y, notch)
                + [(notch if delta_x > 0 else -notch, 0)] * wheel_notches(delta_x, notch))
        burst = random.randint(2, 5)
        for k, (ex, ey) in enumerate(plan):
            send_wheel(ex, ey)
            if k == len(plan) - 1:
                break
            # local sleep, NOT page.wait_for_timeout: the latter is a CDP round-trip per call,
            # so it emits protocol traffic that bot-detectors (e.g. reCAPTCHA) can score — a
            # self-inflicted tell inside the humanize path. time.sleep is off-protocol.
            burst -= 1
            if burst == 0:
                time.sleep(_rand(110, 320) * tempo / 1000.0)  # the finger resets for the next flick
                burst = random.randint(2, 5)
            else:
                time.sleep(_rand(18, 55) / 1000.0)
            if random.random() < 0.07:
                time.sleep(_rand(150, 450) * tempo / 1000.0)  # occasional mid-scroll pause (reading)
            # A hand resting on the mouse does not hold it perfectly still through a long scroll,
            # so a burst of wheel events sharing one identical clientX/clientY is a signature
            # only a script produces. A couple of px, so the scroll stays over the same element
            # (and never while a button is down, where a move is a drag leg).
            if len(plan) >= 6 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = wheel_drift()
                    native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass

    def hwheel(delta_x, delta_y):
        """mouse.wheel: _hwheel_run, with the score hook (_trace, lane G) recording every wheel event this call
        sends and its send time, scored as "wheel" (off: the natives only)."""
        rec = [] if _trace.enabled() else None
        if rec is None:
            return _hwheel_run(delta_x, delta_y, native_wheel)
        t0 = time.perf_counter()

        def send_wheel(ex, ey):
            native_wheel(ex, ey)
            rec.append({"t": (time.perf_counter() - t0) * 1000.0, "dx": ex, "dy": ey, "mode": 0})
        try:
            return _hwheel_run(delta_x, delta_y, send_wheel)
        finally:
            if rec:
                _trace.record("wheel", rec, {"device": st.get("device"), "dx": delta_x, "dy": delta_y})

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel
    if getattr(mouse, "dblclick", None) is not None:
        mouse.dblclick = hdblclick_xy   # stock dblclick teleported and pressed for ~0 ms
    # down/up are wrapped only to TRACK the held state hmove needs; the presses themselves stay
    # native and unchanged.
    mouse.down, mouse.up = hdown, hup

    # ----------------------------------------------------------------- keyboard
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press
    native_kb_insert = getattr(kb, "insert_text", None)   # a bulk fill's paste (Input.insertText)
    # getattr: a page-like keyboard without down/up still gets every other humanization; it just
    # types capitals without the Shift key, as before.
    native_kb_down, native_kb_up = getattr(kb, "down", None), getattr(kb, "up", None)

    # Score hook (_trace, lane G): while a typed run is being traced (st["keyrec"] is a list), every key the
    # natives send is recorded as {"key","down","up"} (ms; modifiers left out), and _human_type_text scores
    # the run as "typed". Off (no "keyrec", the default) the wrappers only forward.
    _kb_press0, _kb_down0, _kb_up0 = native_kb_press, native_kb_down, native_kb_up
    _kb_pend = {}

    def _kb_rec(key, t0, t1):
        rec = st.get("keyrec")
        if rec is not None and key not in _MOD_KEYS:
            rec.append({"key": key, "down": t0, "up": t1})

    def native_kb_press(key, **kw):
        t0 = time.perf_counter() * 1000.0
        r = _kb_press0(key, **kw)
        _kb_rec(key, t0, time.perf_counter() * 1000.0)
        return r

    if _kb_down0 is not None and _kb_up0 is not None:
        def native_kb_down(key, **kw):
            t0 = time.perf_counter() * 1000.0
            r = _kb_down0(key, **kw)
            if st.get("keyrec") is not None:
                _kb_pend[key] = t0
            return r

        def native_kb_up(key, **kw):
            r = _kb_up0(key, **kw)
            t0 = _kb_pend.pop(key, None)
            if t0 is not None:
                _kb_rec(key, t0, time.perf_counter() * 1000.0)
            return r

    def _pointer_presence():
        """Give a keyboard-only session a mouse, once, before its first keystroke.

        Selector-driven typing already glides (page.type/fill/press go through _focus_click), but a
        bare page.keyboard.type/press types into whatever is focused — from a session in which the
        pointer never existed. A collector reading keydowns with zero preceding pointer events sees
        input no person can produce, however good the keystroke dynamics are.

        AMBIENT ONLY, deliberately not a move toward the focused field: the caller has already put
        focus somewhere (often from script), and gliding onto a control could hover or re-target it
        and land the text in the wrong place. plan_ambient never clicks, so focus cannot change.
        """
        if st["placed"] or st["kb_primed"]:
            return
        st["kb_primed"] = True   # one attempt per page, even if the ambient burst itself fails
        try:
            hambient(_rand(280, 620))
        except Exception:  # noqa: BLE001
            pass

    def _emit_key(ch):
        """Emit one character with a human keydown->keyup DWELL. keyboard.press's `delay` IS the hold
        (keyboard.type's delay is only inter-key flight). Fall back to type() for chars press can't map."""
        try:
            native_kb_press(ch, delay=_kdwell())
            return
        except Exception:  # noqa: BLE001
            pass
        # Outside the catch: in strict mode lane A's report raises, and that must reach the caller.
        _fallback("Keyboard.press", "insertText: a character keyboard.press cannot map")
        try:
            native_kb_type(ch)
        except Exception:  # noqa: BLE001
            pass

    def _active_value():
        """The focused field's current value, read in the isolated world (None if unknown)."""
        try:
            return world_for(page).evaluate(ISO_ACTIVE_VALUE)
        except Exception:  # noqa: BLE001
            return None

    def _flight_s(prev=None, ch=None):
        """One keyup->keydown flight of the persona's cadence (_keys.flight_ms), in seconds."""
        return flight_ms(traits, prev, ch, random.random) / 1000.0

    def _backspaces(n):
        for k in range(n):
            native_kb_press("Backspace", delay=_kdwell())
            if k < n - 1:
                time.sleep(_rand(70, 150) / 1000.0)

    def _landed(before, now):
        """How many characters arrived since ``before`` (None = cannot tell)."""
        if before is None or now is None:
            return None
        return len(now) - len(before)

    def _restore(target):
        """Bring the focused field back to ``target`` (what it held before a slip) from wherever a word
        delete left it: Backspace to the common prefix, then type the rest. Nothing when unreadable."""
        got = _active_value()
        if got is None or got == target:
            return
        k = 0
        while k < min(len(got), len(target)) and got[k] == target[k]:
            k += 1
        if len(got) - k > 64 or len(target) - k > 64:
            return
        if len(got) > k:
            _backspaces(len(got) - k)
            time.sleep(_rand(80, 200) / 1000.0)
        _emit_keys(target[k:])

    def _emit_keys(chars, prev=None):
        """``chars`` typed in order, one human flight apart (``prev``: the key typed just before, for the first
        flight; None = none), every capital / shifted symbol behind a real ShiftLeft held across a run of them --
        the main loop's Shift dance (_human_type_text). _restore and the late-notice keys of _do_typo emitted
        them bare: a keydown 'M' right after Control+Backspace with no Shift down (lab contenteditable, SC 209
        row "Capitals typed with Shift held 12/14"; SC 239)."""
        n = len(chars)
        shift = False
        try:
            for j, c in enumerate(chars):
                if prev is not None:
                    time.sleep(_flight_s(prev, c))
                if (_needs_shift(c) and not shift and "Shift" not in held_keys
                        and native_kb_down is not None and native_kb_up is not None):
                    native_kb_down("Shift")
                    shift = True
                    time.sleep(_shift_lead_s())
                _emit_key(c)
                if shift and (j == n - 1 or not _needs_shift(chars[j + 1])):
                    time.sleep(_shift_lag_s())
                    native_kb_up("Shift")
                    shift = False
                prev = c
        finally:
            if shift:   # never leave Shift held
                try:
                    native_kb_up("Shift")
                except Exception:  # noqa: BLE001
                    pass

    def _word_delete():
        """The word-delete chord the ENGINE's editing honours: Alt+Backspace on macOS, else Ctrl."""
        _press_key(("Alt" if _host_platform() == "macos" else "Control") + "+Backspace")

    def _do_typo(text, i, tp, leave=False):
        """Make the planned slip at text[i] and correct it. Returns True when it already typed
        text[i] itself (the doubled-key case) -- or left a substitution uncorrected (``leave``: a search
        field) --, so the caller must not type it again.

        Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
        and a Backspace pressed for a key that never arrived deletes a real character (the
        letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated
        world, one read each -- typos are 0.5-4 % of keys, so this costs nearly nothing.

        A substitution noticed inside a word is fixed with the word-delete chord and the word typed
        again in WORD_FIX_RATE of the cases (when the field can be read to put it back exactly)."""
        before = _active_value()
        kind = tp["kind"]
        if kind == "sub":
            _emit_key(tp["wrong"])
            got = _landed(before, _active_value())
            if got is not None and got <= 0:
                return False                     # swallowed: nothing to correct
            if leave:
                return True                      # the slip stays: the wrong key took this place
            late = text[i + 1:i + 1 + tp.get("late", 0)]
            _emit_keys(late, prev=tp["wrong"])      # late notice: a key or two more
            typed = 1 + len(late)
            time.sleep(_rand(140, 340) / 1000.0)  # noticing
            ws = max(text.rfind(" ", 0, i), text.rfind("\n", 0, i)) + 1
            # a WORD (letters only: no word delete in a code, a zip or a number), two keys of it on screen
            if (before is not None and i - ws + typed >= 2 and native_kb_down is not None
                    and all(c.isalpha() for c in text[ws:i + 1]) and random.random() < WORD_FIX_RATE):
                _word_delete()
                time.sleep(_rand(120, 260) / 1000.0)
                _restore(before)
                time.sleep(_rand(80, 200) / 1000.0)
                return False
            got = _landed(before, _active_value())
            _backspaces(typed if got is None else max(0, min(typed, got)))
            time.sleep(_rand(80, 200) / 1000.0)
            return False
        if kind == "double":
            _emit_key(text[i])
            time.sleep(_rand(45, 95) / 1000.0)      # the bounce comes fast
            _emit_key(text[i])
            got = _landed(before, _active_value())
            time.sleep(_rand(140, 340) / 1000.0)
            extra = 1 if got is None else max(0, got - 1)
            if extra:
                _backspaces(extra)
                time.sleep(_rand(80, 200) / 1000.0)
            return True
        if kind == "swap":
            _emit_key(text[i + 1])
            time.sleep(_flight_s(text[i + 1], text[i]))
            _emit_key(text[i])
            got = _landed(before, _active_value())
            time.sleep(_rand(140, 340) / 1000.0)
            _backspaces(2 if got is None else max(0, min(2, got)))
            time.sleep(_rand(80, 200) / 1000.0)
            return False
        return False

    def _typing_field(field):
        """What the typo gate needs about the field keys go to: the caller's description, else
        the focused element read in the isolated world."""
        if field is not None:
            return field
        try:
            return world_for(page).evaluate(ISO_ACTIVE_FIELD)
        except Exception:  # noqa: BLE001
            return None

    def _engine_type(text, floor):
        """Type ``text`` with ONE Browser.dispatchKeySequence (r43, SC 232 lane H): lane B's key dwell and lane D's
        bigram flight per key (the caller's ``delay`` as the floor), paced in the browser. False when a character
        has no plain US key or the call fails up front (nothing typed yet: the per-key path types it)."""
        keys = engine_key_plan(text, _kdwell,
                               lambda ch, nxt: max(floor, plan_gap(traits, ch, nxt, random.random,
                                                                   getattr(persona, "tempo", 1.0),
                                                                   getattr(persona, "pause_scale", 1.0))),
                               _needs_shift)
        if not keys:
            return False
        try:
            _engine_session()
            _eng["session"].send(ENGINE_KEY_SEQUENCE, {"targetId": _eng["tid"], "keys": keys})
        except Exception:  # noqa: BLE001
            return False
        _trace_engine_typed(keys, floor)
        return True

    def _trace_engine_typed(keys, floor):
        """The score hook (lane G) hears an engine typed run as one ``typed`` line of the PLANNED down/up times
        (the engine paces them in the browser; the per-key path records its dispatched ones). Inside an outer
        traced run the keys join that run's record instead (verifier A's R3c: the engine run wrote nothing)."""
        if not _trace.enabled():
            return
        outer = st.get("keyrec")
        t = time.perf_counter() * 1000.0 if outer is not None else 0.0
        rec = []
        for k in keys:
            down = t
            up = down + float(k.get("dwellMs") or 0.0)
            rec.append({"key": k["key"], "down": down, "up": up})
            t = up + float(k.get("gapMs") or 0.0)
        if outer is not None:
            outer.extend(rec)
        else:
            _trace.record("typed", rec, {"n": len(keys), "typos": False, "floor_ms": floor, "engine": True})

    def _human_type_text(text, field=None, typos=True, floor_ms=0.0, exact=True):
        """Type text key-by-key with human timing. Each char goes through Playwright's native
        keyboard (trusted), so this stays isTrusted=True.

        Cadence (_keys.plan_gap, the same planner in Node and .NET): the persona's WPM sets a base
        flight, scaled by how the hand travels between the two keys (same finger 1.5-1.9x, same hand
        1.1-1.3x, the other hand 0.85-1.0x, a two-row reach 1.15x) and log-normal noise, plus a pause
        before a new word and after punctuation, the old whitespace pause and a 6 % thinking pause.
        ``floor_ms`` (the caller's `delay`) is the shortest gap between two keys.

        Capitals and shifted symbols are typed the way a person types them: ShiftLeft goes down a
        moment before the key and comes up just after it, held across a run of them. Playwright's
        press("A") alone sent the key with shiftKey=false and no Shift key at all (measured on r28
        with "Ab!") -- input no keyboard produces. Under a Shift the caller holds (keyboard.down) no
        second Shift is pressed.

        Fat-finger typos are planned by _forms.plan_typo at the persona's rate and never happen where a
        slip is a failure rather than a slip (passwords, e-mail, numbers, phone numbers, URLs, one-time
        codes). ``exact`` False (type / press_sequentially / keyboard.type, never fill) lets a
        substitution in a search field stay uncorrected (SEARCH_SLIP_RATE)."""
        n = len(text)
        shift = False
        ext_shift = "Shift" in held_keys
        rate = _typo_rate() if typos else 0.0
        fld = _typing_field(field) if rate > 0 else None
        allow = rate > 0 and typos_allowed(fld)
        search = bool(allow and not exact and fld and (fld.get("type") or "").lower() == "search")
        floor = max(0.0, float(floor_ms or 0.0))
        # ROLLOVER, decided up front: the pairs this run overlaps (index of the first key), one unseeded draw per
        # eligible pair at lane B's rollover_rate (12-38 % per identity). Eligible: unshifted single-key pairs
        # (_ROLL_KEYS), never a key with itself, never under a caller's delay floor. Drawn before the route is
        # chosen so the engine run below is taken exactly for the runs that would have had no rollover, and the
        # per-key path realises the persona's share on the others -- the distribution of overlaps per run is the
        # per-key path's, whichever route types it.
        roll_rate = rollover_rate(persona)
        rolls = frozenset(i for i in range(n - 1)
                          if floor <= 0 and native_kb_down and native_kb_up and text[i] in _ROLL_KEYS
                          and text[i + 1] in _ROLL_KEYS and text[i] != text[i + 1] and random.random() < roll_rate)
        # r43 engine (SC 232 lane H): a run with no slip to plan (typos off, or a field where a slip is a failure),
        # no caller-held Shift and NO ROLLOVER goes out as ONE dispatchKeySequence -- the same dwell and flight
        # draws, paced in the browser, so the rhythm carries no CDP round trips. A run the persona rolls over
        # stays per key: the command cannot overlap keys (the engine clamps gapMs at 0), and an identity whose
        # credential fields typed with zero overlap next to text fields with its 12-38 % would contradict itself
        # (verifier A's R3b). Not with engine "off" (R3a): that is the documented way to keep everything SDK-side.
        if (not allow and n > 1 and not ext_shift and not rolls and _hz_engine_mode(page, _engine_clicks_mode) != "off"
                and _engine_ext_ok() and _engine_type(text, floor)):
            st["typed"] = time.monotonic()   # a following Enter / Tab still waits (_after_typing)
            return
        # the score hook records this run's keys (the native_kb_* wrappers) and scores it as "typed" at the end
        outer_rec = st.get("keyrec")
        if outer_rec is None and _trace.enabled():
            st["keyrec"] = []
            _kb_pend.clear()
        i = 0
        try:
            while i < n:
                ch = text[i]
                need = _needs_shift(ch) and not ext_shift
                if need and not shift and native_kb_down and native_kb_up:
                    native_kb_down("Shift")
                    shift = True
                    time.sleep(_shift_lead_s())
                typo = False
                consumed = False
                if allow and not need and not shift and typo_eligible(ch):
                    tp = plan_typo(text, i, random, rate)
                    if tp is not None:
                        typo = True
                        leave = search and tp["kind"] == "sub" and random.random() < SEARCH_SLIP_RATE
                        try:
                            consumed = _do_typo(text, i, tp, leave)
                        except Exception as e:  # noqa: BLE001
                            if _is_strict(e):
                                raise
                # ROLLOVER: the next key goes down before this one comes up, the way people type fast
                # pairs. One press() per character never overlaps (0 of 32 keydowns, measured on
                # r32/r33), a keystroke-dynamics tell. The pairs were drawn up front (``rolls``, above);
                # a typo or a Shift in flight still cancels one.
                nxt = text[i + 1] if i + 1 < n else None
                d1 = _kdwell()
                if (nxt is not None and not typo and not shift and native_kb_down and native_kb_up
                        and i in rolls and d1 - 8 >= 12):
                    overlap = _rand(12, min(55, d1 - 8))
                    try:
                        native_kb_down(ch)
                        time.sleep((d1 - overlap) / 1000.0)
                        native_kb_down(nxt)
                        time.sleep(overlap / 1000.0)
                        native_kb_up(ch)
                        time.sleep(max(5.0, _kdwell() - overlap) / 1000.0)
                        native_kb_up(nxt)
                    except Exception:  # noqa: BLE001
                        for k in (ch, nxt):
                            try:
                                native_kb_up(k)
                            except Exception:  # noqa: BLE001
                                pass
                        break
                    i += 1          # nxt is typed; the cadence below runs as if after it
                    ch = nxt
                elif not consumed:
                    try:
                        _emit_key(ch)
                    except Exception as e:  # noqa: BLE001
                        if _is_strict(e):
                            raise
                        break
                if shift and (i == n - 1 or not _needs_shift(text[i + 1])):
                    time.sleep(_shift_lag_s())
                    native_kb_up("Shift")
                    shift = False
                if i < n - 1:
                    time.sleep(max(floor, plan_gap(traits, ch, text[i + 1], random.random,
                                         getattr(persona, "tempo", 1.0), getattr(persona, "pause_scale", 1.0))) / 1000.0)
                i += 1
        finally:
            if shift:  # never leave Shift held for the caller's next keystroke
                try:
                    native_kb_up("Shift")
                except Exception:  # noqa: BLE001
                    pass
            if n:
                st["typed"] = time.monotonic()   # a following Enter / Tab waits (_after_typing)
            if outer_rec is None and st.get("keyrec") is not None:
                _trace.record("typed", st.pop("keyrec"), {"n": n, "typos": allow, "floor_ms": floor})

    def _after_typing(key):
        """The pause a person makes between the last typed character and Enter (250-900 ms) or Tab
        (120-400 ms), centred per persona, less whatever the caller already spent. Once per typed run;
        a pointer action in between cancels it (_glide clears st["typed"])."""
        t = st.get("typed")
        st["typed"] = None
        if t is None:
            return
        mods, k = split_chord(key)
        if k == "Enter" and not mods:
            ms = submit_pause_ms(traits, random.random)
        elif k == "Tab" and set(mods) <= {"Shift"}:
            ms = tab_pause_ms(traits, random.random)
        else:
            return
        rest = ms / 1000.0 - (time.monotonic() - t)
        if rest > 0:
            time.sleep(rest)

    def _hold_key(k, delay=None, repeat=1):
        """One key: pressed with the persona's hold (or the caller's ``delay``); with ``repeat`` > 1 HELD
        the way an OS auto-repeats it -- one keydown, the machine's repeat delay (250-500 ms, a persona
        trait), then a keydown with repeat=true at its repeat interval (25-40 ms, likewise; Playwright
        sends a key that is already down as an autoRepeat keydown), ``repeat`` keydowns in all, then one
        keyup."""
        repeat = max(1, int(repeat or 1))
        if repeat == 1 or native_kb_down is None or native_kb_up is None:
            for j in range(repeat):
                native_kb_press(k, delay=delay if delay and delay > 0 else _kdwell())
                if j < repeat - 1:
                    time.sleep(_rand(60, 150) / 1000.0)
            return
        rp = plan_repeat(traits, random.random)
        native_kb_down(k)
        try:
            time.sleep(rp.first_ms / 1000.0)
            for j in range(repeat - 1):
                if j:
                    time.sleep(rp.gap_ms / 1000.0)
                native_kb_down(k)
            time.sleep(rp.release_ms / 1000.0)
        finally:
            native_kb_up(k)   # never leave the key held

    def _press_key(key, delay=None, repeat=1):
        """keyboard.press, humanized: a chord ("Control+a", "ControlOrMeta+Shift+ArrowLeft") is split
        into its modifiers and its key -- each modifier goes down in order, 40-110 ms before the next,
        the key is held, 20-80 ms later the modifiers come up in reverse order. As one Playwright press
        the modifier and the key went down within ~1 ms of each other. A modifier the caller already
        holds (keyboard.down) is not pressed again. Playwright's key names are kept (ControlOrMeta is
        resolved by Playwright, for the engine's platform)."""
        mods, k = split_chord(key)
        mods = [m for m in mods if m not in held_keys]
        if not mods:
            return _hold_key(k, delay, repeat)
        if native_kb_down is None or native_kb_up is None:
            return native_kb_press(key, delay=delay if delay and delay > 0 else _kdwell())
        down = []
        try:
            for m in mods:
                native_kb_down(m)
                down.append(m)
                time.sleep(modifier_lead_ms(random.random) / 1000.0)
            _hold_key(k, delay, repeat)
            time.sleep(modifier_lag_ms(random.random) / 1000.0)
        finally:
            for j, m in enumerate(reversed(down)):
                if j:
                    time.sleep(_rand(10, 40) / 1000.0)
                try:
                    native_kb_up(m)   # one failed keyup must not strand the others
                except Exception:  # noqa: BLE001
                    pass

    def hkb_type(text, **kw):
        _pointer_presence()
        _human_type_text(text, floor_ms=kw.get("delay") or 0.0, exact=False)

    def hkb_press(key, **kw):
        """keyboard.press with the persona's hold, unless the caller specified their own.

        WHY THIS EXISTS. keyboard.type was humanised from the start and keyboard.press was not,
        which left the most common single-key call in any script -- Enter, Tab, Escape, arrow keys
        -- emitting a keydown and keyUp in the same instant. Measured on this machine with
        humanize ON, before this wrapper: keyboard.type held keys 58-107ms while keyboard.press
        held them 1.4-3.8ms. A finger cannot press and release a key in under a millisecond, and
        a detector reading the shortest dwell in a session sees the press path, not the type path
        -- so one un-humanised call undid the whole keyboard persona.

        The caller's own `delay` wins: press(key, delay=...) is the documented way to hold a key
        for a specific time (game input, long-press UI), and silently overriding it would break
        that. Only the DEFAULT changes, from "no hold at all" to "a human hold". A chord is pressed
        modifier first (_press_key); ``repeat=n`` holds the key through n auto-repeat keydowns; Enter
        or Tab right after typing waits the persona's submit / Tab pause first (_after_typing)."""
        _pointer_presence()
        repeat = kw.pop("repeat", 1)
        _after_typing(key)
        d = kw.get("delay")
        return _press_key(key, d if d and d > 0 else None, repeat)

    def hkb_down(key):
        """keyboard.down, tracked: the chord model and typing honour a modifier the caller holds, and
        hkb_up gives the key at least a persona hold."""
        _pointer_presence()
        if key not in held_keys:
            held_keys[key] = (time.monotonic(), _kdwell())
        return native_kb_down(key)

    def hkb_up(key):
        t = held_keys.pop(key, None)
        if t is not None:
            rest = t[1] / 1000.0 - (time.monotonic() - t[0])
            if rest > 0:
                time.sleep(rest)   # a down() then an up() at once: no finger lifts a key in 0 ms
        return native_kb_up(key)

    kb.type = hkb_type
    kb.press = hkb_press
    if native_kb_down is not None and native_kb_up is not None:
        kb.down, kb.up = hkb_down, hkb_up

    # ------------------------------------------------------- page-level targeted helpers
    # Points tried when resolving a control through the browser's hit test: a floating label or an
    # icon often sits over a field's CENTRE, so the edges get a chance too.
    probe_points = ((0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2))

    def _aim_w(box):
        """The Fitts target width of a glide onto ``box``: its smaller side (floor 6 px). Every targeted
        glide passed 24 whatever the box, so duration did not follow the target's size."""
        return max(6.0, min(float(box["width"]), float(box["height"])))

    def _proven_click_point(box, sel=None):
        """When the persona's point is covered (a badge, a floating label over the centre): the first
        probe point (edges, then top/bottom) the isolated hit test proves reaches the element, as a
        whole pixel inside the box; None when none does (the caller falls back)."""
        for fx, fy in probe_points[1:]:
            # spread around the probe (3 % of the width, 8 % of the height): a fixed probe pixel would land
            # every covered click on the same spot
            x = box["x"] + box["width"] * fx + random.gauss(0, max(1.5, box["width"] * 0.03))
            y = box["y"] + box["height"] * fy + random.gauss(0, max(1.0, box["height"] * 0.08))
            x = float(round(min(max(x, box["x"] + 1), box["x"] + box["width"] - 1)))
            y = float(round(min(max(y, box["y"] + 1), box["y"] + box["height"] - 1)))
            if _hit_in_box(box, x, y, sel) is True:
                return (x, y)
        return None

    def _describe(box, want=FORM_CONTROL):
        """The control filling ``box`` (Playwright's resolved box for a locator), read in the isolated
        world through the browser's own hit test: works for every Playwright selector (get_by_role,
        get_by_label, `>> nth=`, .filter()) where document.querySelector threw and dropped the call
        to stock input. The element found must fill the SAME box, or it is not the one the locator
        means; None when no probe point proves that."""
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        w = world_for(page)
        scroll = w.scroll()   # once: the hit test wants document coordinates
        for fx, fy in probe_points:
            nid = w.node_at(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, scroll)
            if nid is None:
                continue
            info = w.call_on_node(nid, ISO_DESCRIBE, {"want": want})
            if not info or not info.get("rect"):
                continue
            rx, ry, rw, rh = info["rect"]
            if (abs(rx - box["x"]) <= 1.5 and abs(ry - box["y"]) <= 1.5
                    and abs(rw - box["width"]) <= 1.5 and abs(rh - box["height"]) <= 1.5):
                info["node"] = nid
                # The point that PROVED the control: when the centre is covered (a floating label),
                # this is where a click actually reaches it.
                info["probe"] = (fx, fy)
                return info
        return None

    def _proven_point(box, info):
        """A click point the hit test proved reaches the control, when its centre did not; None when
        the centre itself was fine (then the persona's own point is used, as for any click)."""
        if not info or info.get("probe", (0.5, 0.5)) == (0.5, 0.5):
            return None
        fx, fy = info["probe"]
        x = box["x"] + box["width"] * fx + random.gauss(0, 1.5)
        y = box["y"] + box["height"] * fy + random.gauss(0, 1.0)
        return (min(max(x, box["x"] + 1), box["x"] + box["width"] - 1),
                min(max(y, box["y"] + 1), box["y"] + box["height"] - 1))

    def _redescribe(info, want=FORM_CONTROL):
        """Describe the SAME element again, from the node the first hit test found: no new
        Playwright handle (see _box_of), so nothing the page can see. None if it is gone."""
        again = world_for(page).call_on_node(info["node"], ISO_DESCRIBE, {"want": want})
        if again:
            again["node"] = info["node"]
            again["probe"] = info.get("probe", (0.5, 0.5))
        return again

    def _in_view(box):
        vw, vh = _viewport()
        return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh

    def _rect_box(r):
        if not r or not r.get("rect"):
            return None
        x, y, w, h = r["rect"]
        return {"x": x, "y": y, "width": w, "height": h} if w > 0 and h > 0 else None

    def _css_box(selector):
        """The box of a plain-CSS selector's first match, read (and scrolled into view) in the
        isolated world: not even the one bounding_box() handle, and its textContent preview, is made.
        Used only when Playwright counts exactly the elements the browser's own querySelectorAll
        finds -- Playwright's CSS also pierces open shadow roots, so equal counts mean the same
        elements in the same order. None otherwise (get_by_role, text=, `>>` chains, :has-text()...:
        then Playwright's bounding_box)."""
        if not isinstance(selector, str) or not selector:
            return None
        w = world_for(page)
        r = w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return None
        try:
            if page.locator(selector).count() != r["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        box = _rect_box(r)
        if box and not (_in_view(box) and _hit_ok(box, selector)):
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does
            # nothing when it already shows (then the cover is an overlay: the click's own check)
            box = _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        return box

    def _hit_ok(box, sel=None):
        """Does the element filling ``box`` own its centre -- not clipped by a scrolling container,
        not covered? The browser's own hit test, read in the isolated world."""
        return _hit_in_box(box, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, sel) is True

    def _scroll_window_to(box):
        """Scroll the WINDOW so ``box`` sits centred where it was off-screen, the way Playwright's
        scroll centres it, and return where the box is now (moved by exactly the scrolled distance) --
        or None. Needs no handle: the box is already known."""
        vw, vh = _viewport()
        if box["width"] > vw or box["height"] > vh:
            return None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dx = cx - vw / 2 if box["x"] < 0 or box["x"] + box["width"] > vw else 0
        dy = cy - vh / 2 if box["y"] < 0 or box["y"] + box["height"] > vh else 0
        moved = world_for(page).evaluate(ISO_SCROLL_BY, [dx, dy])
        if not isinstance(moved, list) or len(moved) != 2:
            return None
        return {"x": box["x"] - moved[0], "y": box["y"] - moved[1], "width": box["width"], "height": box["height"]}


    def _css_plain(selector):
        """Is ``selector`` plain CSS that the browser's querySelectorAll resolves to exactly the elements
        Playwright counts (see _css_box)? Then the box and the scroller are read in the isolated world."""
        if not isinstance(selector, str) or not selector:
            return False
        r = world_for(page).evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return False
        try:
            return page.locator(selector).count() == r["n"]
        except Exception:  # noqa: BLE001
            return False

    def _wheel_into_view(loc, selector, css, box):
        """Bring the target in with the WHEEL (_scroll.bring_into_view): the box it ends at when the
        target shows and owns its centre, else None and why ("not_scrollable", "notch_budget",
        "covered"). A plain-CSS target's nearest scrollable ancestor is wheeled over when it is not the
        window; other selectors scroll the window."""
        w = world_for(page)

        s0 = w.scroll()

        def box_fn():
            if css:
                return _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False}))
            # no new Playwright handle per read (each one's preview reads textContent in the page's
            # world): the box the caller already has, moved by how far the window has scrolled since
            sx, sy = w.scroll()
            return {"x": box["x"] - (sx - s0[0]), "y": box["y"] - (sy - s0[1]),
                    "width": box["width"], "height": box["height"]}

        def scroller_fn():
            r = w.evaluate(ISO_SCROLLER, {"sel": selector})
            return r if isinstance(r, dict) and r.get("rect") else None

        got, ok, notches = bring_into_view(_scroll_io(), box_fn, scroller_fn=scroller_fn if css else None)
        if not ok:
            return None, ("notch_budget" if notches >= 60 else "not_scrollable"), got
        if not (_in_view(got) and _hit_ok(got, selector)):
            return None, "covered", got
        return got, None, got

    def _needs_scroll(box, selector, css):
        """Is ``box`` out of view -- outside the viewport, or outside the visible part of the scrolling
        container that holds it?"""
        if not _in_view(box):
            return True
        if css:
            r = world_for(page).evaluate(ISO_SCROLLER, {"sel": selector})
            if isinstance(r, dict) and r.get("rect"):
                x, y, cw, ch = r["rect"]
                return not _box_in(box, (x, y, cw, ch))
        return False

    def _box_of(loc, timeout, force=False, selector=None, wheel=True):
        """Wait the way Playwright would, with the CALLER's timeout (None = the page default; a
        hard-coded 30 s used to override set_default_timeout both ways), and return the box, in view.
        With `force` Playwright skips actionability, so nothing is waited for: a hidden target goes
        native at once instead of timing out where the native call would have acted.

        An out-of-view target is brought in with the WHEEL (_wheel_into_view): whole notches (or a
        trackpad swipe) to 25-60 % of the viewport, an undershoot and a correction, over the nested
        scrolling container when that is what clips it. The programmatic scroll (isolated
        scrollIntoViewIfNeeded, an instant window scrollBy, Playwright's scroll) is only the FALLBACK --
        a container the wheel does not move, the 60-notch budget spent, ``wheel=False`` (a trial, which
        must not move the pointer) -- and every fallback after a wheel attempt is reported (_fallback).
        Measured on r42 / 0.51.0: every humanized action on an off-screen target scrolled
        programmatically, a `scroll` event with no wheel before it, the target at the exact centre.

        Each Playwright call that resolves the element to a handle (wait_for,
        scroll_into_view_if_needed, bounding_box) makes a MAIN-world handle whose preview reads the
        element's textContent where the page can see it (verifier B: 4 such reads per humanized click,
        7 per textarea fill). So each is made only when needed, and a plain-CSS ``selector`` is read in
        the isolated world without a handle."""
        if not loc.is_visible():
            if force:
                raise _Native("forced on a hidden target")
            loc.wait_for(state="visible", timeout=timeout)
        css = _css_plain(selector)
        w = world_for(page)
        box = _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})) if css else loc.bounding_box()
        if not box:
            return box
        if wheel and _needs_scroll(box, selector, css):
            got, why, last = _wheel_into_view(loc, selector, css, box)
            if got is not None:
                return got
            if why != "covered":
                _fallback("scroll_into_view", "scroll into view: " + why)
            box = last or box
        if _in_view(box) and _hit_ok(box, selector):
            return box
        if css:
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does nothing
            # when it already shows (then the cover is an overlay: the click's own check)
            return _rect_box(w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        # Off-screen: scroll the window to it and prove it shows there (hit test), with no new handle.
        # Clipped by a scrolling container, covered, or the window scroll did not bring it out:
        # Playwright's scroll, which also scrolls containers, and a fresh box.
        moved = _scroll_window_to(box) if not _in_view(box) else None
        if moved and _in_view(moved) and _hit_ok(moved, selector):
            return moved
        loc.scroll_into_view_if_needed(timeout=timeout)
        return loc.bounding_box()

    def _focused(loc):
        """Does ``loc`` hold the keyboard focus? Asked through Playwright's utility-world selector
        engine (`:focus`), so it works for every element and every locator and the page sees nothing.
        The isolated hit test only knew form controls, so a focused BUTTON read as unfocused and got
        clicked before a press -- two activations."""
        try:
            return loc.and_(page.locator(":focus")).count() > 0
        except Exception:  # noqa: BLE001
            return False

    def _is_focused(selector):
        return _focused(page.locator(selector).first)

    def _text_entry(loc, timeout, selector=None):
        """(box, info) when ``loc`` is a visible text-entry control a person would click into before
        typing, else None. Anything else -- a button, a link, a hidden helper textarea, a slider -- is
        left to Playwright, which focuses it without a click (a click on a button activates it; on a
        slider it moves it)."""
        if not loc.is_visible():
            return None
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            return box, info
        return None

    def _focus_click(box, at=None, loc=None, selector=None):
        """Click into the control filling ``box`` (resolved, visible and in view). ``at``: the point
        the hit test proved when the centre is covered (a floating label); else the persona's point.
        Stability is checked in the isolated world -- the control must still fill the same box 50 ms
        later (an animation in flight goes native) -- not by a second bounding_box(), which the page
        could see (see _box_of).

        A control no probe point reaches -- a label or an icon covers all of it -- is clicked anyway,
        where a person clicks (a floating label passes the focus on), and the caller checks that the caret
        arrived. Its stability then comes from a fresh box: the isolated CSS read, else bounding_box().
        Requiring the hit test there sent a fully labelled field to Playwright's insertText (found by the
        .NET port)."""
        if not box:
            return False
        time.sleep(0.05)
        info = _describe(box)
        if info is None:
            again = _css_box(selector) if selector else None
            if again is None and loc is not None:
                again = loc.bounding_box()
            if not again or abs(again["x"] - box["x"]) > 1 or abs(again["y"] - box["y"]) > 1:
                return False
        elif info.get("disabled"):
            return False
        pt = at if at is not None else click_point(box, st["pos"], persona)  # 2D gaussian toward centre
        pt = (float(round(pt[0])), float(round(pt[1])))   # the pixel the hit test answered for
        _glide(pt[0], pt[1], target_w=_aim_w(box))
        _press_here(pt[0], pt[1])
        return True

    def _key(k, n=1, gap=(60, 150)):
        for j in range(n):
            native_kb_press(k, delay=_kdwell())
            if j < n - 1:
                time.sleep(pause_ms(persona, *gap) / 1000.0)

    def _select_all():
        """Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment
        before the A, the A is held, the modifier comes up just after. Sent as one Playwright chord the
        modifier and the A went down within ~1 ms of each other. ControlOrMeta lets Playwright pick the
        chord the ENGINE's editing honours: a Meta+A sent to a Linux engine selects nothing."""
        if native_kb_down is None or native_kb_up is None:
            _key("ControlOrMeta+a")
            return
        native_kb_down("ControlOrMeta")
        try:
            time.sleep(_rand(40, 110) / 1000.0)
            native_kb_press("a", delay=_kdwell())
            time.sleep(_rand(20, 80) / 1000.0)
        finally:
            native_kb_up("ControlOrMeta")

    def _clear_focused(read=None):
        """Select all, then Backspace -- each key HELD like a finger holds it. The old clear sent
        Control, a and Backspace with ~1 ms holds, which is exactly what a key-dwell check flags.
        If the field is still not empty (``read`` -- the field's value -- says so), it is emptied from
        the end, one Backspace per character."""
        read = read or _active_value
        _select_all()
        time.sleep(_rand(60, 160) / 1000.0)
        _key("Backspace")
        time.sleep(_rand(60, 160) / 1000.0)
        left = read()
        if left:
            if len(left) > 64:
                raise RuntimeError("field would not clear")
            _key("End")
            time.sleep(_rand(80, 180) / 1000.0)
            _key("Backspace", len(left), gap=(55, 120))
            time.sleep(_rand(60, 160) / 1000.0)
            if read():
                raise RuntimeError("field would not clear")

    native_page_click = page.click
    native_page_type = page.type
    native_page_fill = page.fill
    native_page_press = page.press
    native_page_dblclick = page.dblclick

    def _tab_distance(info, selector=None):
        """Where the control ``info`` (described, with its hit-test node) sits in the page's Tab order
        relative to the focused element (_keys.TAB_DISTANCE, read in the isolated world): {dist, fromText,
        focused, activatable, text, ...}, or None when either is outside the order or it cannot be read."""
        if not info or info.get("node") is None or not info.get("rect"):
            return None
        x, y, w, h = info["rect"]
        arg = {"x": x, "y": y, "width": w, "height": h}
        if isinstance(selector, str) and selector:
            arg["sel"] = selector
        try:
            return world_for(page).call_on_node(info["node"], TAB_DISTANCE, arg)
        except Exception:  # noqa: BLE001
            return None

    def _tab_steps(d):
        """Press Tab ``d`` times (Shift+Tab when ``d`` < 0), each with a hold, the first after the
        persona's Tab pause when it follows typing, the rest 110-260 ms apart (the eye follows the
        focus ring)."""
        key = "Tab" if d > 0 else "Shift+Tab"
        for j in range(abs(d)):
            if j == 0:
                _after_typing(key)
            else:
                time.sleep(_rand(110, 260) / 1000.0)
            _press_key(key)

    def _tab_into(loc, info, selector=None, need_empty=False, force=False):
        """Tab into the control ``info`` instead of clicking it, the way a person moves between
        adjacent fields: when focus sits in a TEXT field 1-3 Tab stops before it (or 1 after: Shift+Tab),
        in the persona's share of cases (60-85 %, _keys.typing_traits.tab_pref). ``force``: any focused
        element, up to 3 stops either way, no roll (a button a press goes to). ``need_empty``: only an
        empty field -- Tab selects a field's text, and type() appends where a click leaves the caret.
        True once focus is verified there; False (nothing pressed, or focus went elsewhere) = click."""
        if native_kb_press is None or (need_empty and info and info.get("value")):
            return False
        td = _tab_distance(info, selector)
        if not td or td.get("dist") is None or td.get("focused"):
            return False
        d = td["dist"]
        if force:
            if not (1 <= abs(d) <= 3):
                return False
        else:
            if not td.get("fromText") or not (1 <= d <= 3 or d == -1) or random.random() >= _tab_pref():
                return False
        _tab_steps(d)
        time.sleep(_rand(40, 120) / 1000.0)
        return _focused(loc)

    def _click_into(selector, timeout, tab=True):
        """Make ``selector`` the focus the way a person does -- Tab from the field before it (_tab_into)
        or a click --, unless it already is. Only text-entry controls are clicked; for anything else, and
        when the click did not actually put the caret there (an overlay took it), the caller goes
        native. Tab only into an EMPTY field: type() appends at the caret, and Tab selects the text."""
        loc = page.locator(selector).first
        if _focused(loc):
            return
        te = _text_entry(loc, timeout, selector)
        if te is None:
            raise _Native("not a visible text field")
        if tab and _tab_into(loc, te[1], selector, need_empty=True):
            return
        if not _focus_click(te[0], _proven_point(*te), loc, selector) or not _focused(loc):
            raise _Native("the click did not put the caret in the field")
        time.sleep(_rand(40, 120) / 1000.0)

    def _tab_via_field(loc, info, selector, td):
        """Reach the activatable control ``info``, out of Tab reach of the focus, the way a keyboard user
        does: a click into the text-entry stop 1-3 before it (or 1 after) -- ``td["near"]`` from
        TAB_DISTANCE -- then Tab across. True once focus is verified on it; False (nothing pressed, or the
        click or the Tabs went elsewhere) = the caller's next route."""
        near = (td or {}).get("near")
        if not near or not near.get("rect") or native_kb_press is None:
            return False
        r = near["rect"]
        nbox = {"x": float(r[0]), "y": float(r[1]), "width": float(r[2]), "height": float(r[3])}
        if not _focus_click(nbox):
            return False
        time.sleep(_rand(40, 120) / 1000.0)
        return _tab_into(loc, info, selector, force=True)

    def _tab_from_start(loc, info, selector):
        """Nothing has the focus: one Tab enters the page (its first stop, or the one after whatever had
        the focus last), then the control is reached when it is within 3 stops of the focus ring. True
        once focus is verified on it."""
        if native_kb_press is None:
            return False
        _press_key("Tab")
        time.sleep(_rand(110, 260) / 1000.0)   # the eye finds the focus ring
        if _focused(loc):
            return True
        return _tab_into(loc, info, selector, force=True)

    def _focus_target(selector, timeout):
        """Give ``selector`` -- any focusable element, a button or a link as well as a field -- the
        keyboard focus the way a person does. Tab when it is 1-3 stops from the focused element (always
        for something a click would ACTIVATE: a button, a link, a checkbox); otherwise a humanized click
        (glide + press) for what a click only focuses (a field, a tabindex container). An activatable
        target out of Tab reach is reached as a keyboard user reaches it: a click into the text field
        next to it and Tab across (_tab_via_field), or, with nothing focused, Tab in from the top and
        follow the focus ring (_tab_from_start). True when focus is verified there. False when none of
        that reached it: the pointer has glided onto it and the caller gives it the focus natively (a
        reported fallback), never the 0 ms native press."""
        loc = page.locator(selector).first
        if _focused(loc):
            return True
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box, FOCUSABLE) if box else None
        if info is None:
            raise _Native("the target cannot be described (no box)")
        td = _tab_distance(info, selector)
        act = td is None or bool(td.get("activatable"))   # unknown: never risk an activating click
        if td and td.get("dist") is not None and 1 <= abs(td["dist"]) <= 3:
            if act or random.random() < _tab_pref():
                if _tab_into(loc, info, selector, force=True):
                    return True
        if not act and not info.get("disabled"):
            if _focus_click(box, _proven_point(box, info), loc, selector) and _focused(loc):
                time.sleep(_rand(40, 120) / 1000.0)
                return True
            raise _Native("the focus click did not focus it")
        if act and td and not info.get("disabled"):
            # SC 232 lab triage (10-16): an activatable target out of Tab reach -- the usual case for a button
            # when the focus is far away or nowhere -- got the pointer over it and Playwright's focus (reported):
            # 0 of 4 presses on verifier B's buttons, 6 of 12 in the lab, on d2b9ab6.
            if _tab_via_field(loc, info, selector, td):
                return True
            if td.get("noAnchor") and _tab_from_start(loc, info, selector):
                return True
        x, y = click_point(box, st["pos"], persona)
        _glide(float(round(x)), float(round(y)))   # the hand goes to it; the focus has to be given
        return False

    def htype(selector, text, **options):
        try:
            _click_into(selector, options.get("timeout"))
            _human_type_text(text, floor_ms=options.get("delay") or 0.0, exact=False)
            return None
        except _Native as e:
            why = str(e)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.type", why, selector)
        # outside the try: a native call that raises must raise ONCE, not be retried by the handler
        return native_page_type(selector, text, **options)

    def _fill_text(loc, box, value, info, selector=None):
        """Type ``value`` into a text control and PROVE it landed. The value is read through
        Playwright's utility world (`input_value`): it sees the control the locator means, wherever
        focus is, and the page cannot observe it. A field whose value cannot be read, or a click that
        did not put the caret in it (an overlay took it), is left to Playwright -- "could not check"
        is never treated as success."""
        is_ce = bool(info and info.get("ce")) or info is None and (loc.get_attribute("contenteditable") or "false") != "false"
        # What the field can hold: a maxlength keeps the first N characters (what Playwright's
        # insertText leaves too), and a person types only those -- the field stops taking keys.
        if is_ce:
            want, limit = value, (None, None, None)
        elif info is not None:
            limit = (info.get("maxlength"), info.get("tag"), info.get("type"))
            want = fit_maxlength(value, *limit)
        else:
            limit = (loc.get_attribute("maxlength"), _classify_tag(loc), loc.get_attribute("type") or "")
            want = fit_maxlength(value, *limit)
        # Bulk values (over 200 characters) are still inserted at once -- typing thousands of keys
        # would crawl -- but the way a person pastes: glide to the field, click into it, then insert.
        bulk = len(want) > 200   # what the field can hold: a 300-character value cut to 5 is typed
        if bulk and native_kb_insert is None:
            return False
        # From the field before it, a person Tabs (persona share, focus verified); else a click.
        if not (info is not None and _tab_into(loc, info, selector)):
            if not _focus_click(box, _proven_point(box, info) if box else None, loc, selector):
                return False
            time.sleep(_rand(40, 120) / 1000.0)
            if not _focused(loc):
                return False

        def read():
            try:
                return loc.input_value(timeout=COVER_RETRY_MS)
            except Exception:  # noqa: BLE001
                return None

        def put(text, typos=True):
            if bulk:
                time.sleep(_rand(120, 320) / 1000.0)   # the paste shortcut, after the click
                native_kb_insert(text)
            else:
                _human_type_text(text, field=info, typos=typos)

        if is_ce:
            cur = info.get("value") if info else None
            if cur:
                _clear_focused()
            put(want)
            return True   # contenteditable: Playwright's fill sets text the same way; no value to read
        cur = read()
        if cur is None:
            return False
        if cur:  # a person does not select-all an empty field
            _clear_focused(read)
        put(want)
        got = read()
        if got is None:
            return False
        if got == want or formatted_equal(got, want) or mask_kept(got, want, *limit):
            # exact; a mask that only re-punctuated what was typed (a phone format); or one that
            # re-punctuated it and then stopped taking keys (MM/YY on maxlength 5): no wipe and retype
            return True
        if bulk:
            return False   # an inserted value that did not land: Playwright's fill, not a second paste
        # Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips,
        # then give the field to Playwright -- a correct value beats pretty input.
        _clear_focused(read)
        put(want, typos=False)
        got = read()
        return got is not None and (got == want or formatted_equal(got, want) or mask_kept(got, want, *limit))

    def _fill_range(loc, box, info, value):
        """A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it
        exactly. Typing digits into a slider -- what fill() did before -- changes nothing, and the
        click before it left the thumb wherever it landed, with no error."""
        if info is None:
            info = {"min": loc.get_attribute("min"), "max": loc.get_attribute("max"),
                    "step": loc.get_attribute("step"), "value": loc.input_value(), "dir": "ltr", "wm": ""}
        if not range_value_ok(value, info.get("min"), info.get("max"), info.get("step")):
            # Off the step, out of range, not a number: Playwright raises "Malformed value" for these,
            # so does this route (by leaving them to it) -- never a silently snapped or clamped value.
            return False
        if (info.get("wm") or "").startswith("vertical"):
            return False
        rtl = info.get("dir") == "rtl"
        lo, hi, step = range_attrs(info.get("min"), info.get("max"), info.get("step"))
        target = range_sanitize(value, info.get("min"), info.get("max"), info.get("step"))

        def now():
            try:
                return float(loc.input_value())
            except (TypeError, ValueError):
                return None

        cur = now()
        if cur is not None and numeric_equal(cur, target):
            return True
        tx, ty = range_point(box, range_fraction(target, lo, hi), rtl)
        ty += random.gauss(0, box["height"] * 0.08)
        if cur is not None and native_down and native_up and random.random() < 0.65:
            # Grab the thumb where it is and slide it: what most hands do with a slider.
            cx, cy = range_point(box, range_fraction(cur, lo, hi), rtl)
            cy += random.gauss(0, box["height"] * 0.08)
            _glide(cx, cy, target_w=16)
            _pre_press()
            grab_ms, release_ms = drag_dwell(persona)
            x0 = range_point(box, range_fraction(cur, lo, hi), rtl)[0]
            span = abs(range_point(box, 1.0, False)[0] - range_point(box, 0.0, False)[0])
            step_px = span * (step / (hi - lo)) if (step and hi > lo) else 1.0

            def moved():
                v = now()
                return None if v is None else (range_point(box, range_fraction(v, lo, hi), rtl)[0] - x0, 0.0)

            native_down()
            st["held"] = True
            try:
                # closed loop (drag_by): past the value by a few px, read the value, correct
                _drag_by_core(cx, cy, tx - x0, 0.0, moved, max(1.0, step_px / 2.0), grab_ms)
                time.sleep(release_ms / 1000.0)
            finally:
                native_up()
                st["held"] = False
        else:
            _glide(tx, ty, target_w=16)
            _press_here(tx, ty)
        for _ in range(3):
            v = now()
            if v is None:
                return False
            if numeric_equal(v, target):
                return True
            key, n = range_nudge(v, target, step)
            if key is None or n <= 0 or n > 40:
                break
            if rtl:
                key = "ArrowLeft" if key == "ArrowRight" else "ArrowRight"
            time.sleep(_rand(150, 350) / 1000.0)
            _key(key, n)
        v = now()
        return v is not None and numeric_equal(v, target)

    def _fill_datetime(loc, box, info, value):
        """A native date / time / month / week / datetime-local editor: click into its first field
        and type each field in the order the editor itself shows them (read from its UA shadow tree
        over CDP -- the order follows the browser, not the persona, so it is never assumed)."""
        if not info or not info.get("node"):
            return False
        kind = info.get("type")
        w = world_for(page)
        tree = w.send("DOM.describeNode", {"backendNodeId": info["node"], "depth": -1, "pierce": True})
        if not tree or "node" not in tree:
            return False
        segs = segment_nodes_from_tree(tree["node"])
        plan = plan_datetime(kind, [s for s, _ in segs], value, current=info.get("value"))
        if not plan or not segs:
            return False
        px, py, fw = None, None, 12.0
        bm = w.send("DOM.getBoxModel", {"backendNodeId": segs[0][1]}) if segs[0][1] else None
        if bm and bm.get("model"):
            c = bm["model"]["content"]
            x0, y0, x1, y1 = min(c[0::2]), min(c[1::2]), max(c[0::2]), max(c[1::2])
            # trust the field box only when it lies inside the input's own box
            if box["x"] - 1 <= x0 < x1 <= box["x"] + box["width"] + 1 and box["y"] - 1 <= y0 < y1 <= box["y"] + box["height"] + 1:
                px = _rand(x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75)
                py = (y0 + y1) / 2 + random.gauss(0, max(0.5, (y1 - y0) * 0.1))
                fw = max(6.0, x1 - x0)
        if px is None:
            px = box["x"] + min(14.0, box["width"] * 0.12)
            py = box["y"] + box["height"] / 2
        _glide(px, py, target_w=fw)
        _press_here(px, py)
        time.sleep(pause_ms(persona, 150, 350) / 1000.0)
        for a in plan:
            if a[0] == "type":
                # The same typing model as text (persona dwell, gaussian gaps, thinking pauses,
                # rollover): the old flat 60-150 ms gap had a CV of 0.24 and no rollover at all. A
                # segment moves on by itself after its last digit, and keys go down in order, so a
                # rolled-over digit still lands in the right field (checked on r38, all five types).
                # No typos: a slip in a date is a wrong date.
                _human_type_text(a[1], typos=False)
            else:
                _key(a[1], a[2] if len(a) > 2 else 1, gap=(70, 170))
            time.sleep(pause_ms(persona, 60, 220) / 1000.0)   # between fields: the eye checks what it typed
        return datetime_equal(kind, loc.input_value(), value)

    def _classify_tag(loc):
        """'textarea' | 'input' | None, through Playwright's utility-world queries."""
        for tag in ("textarea", "input"):
            if loc.locator("xpath=self::%s" % tag).count():
                return tag
        return None

    def _classify_fallback(loc):
        """Classify through Playwright's utility-world queries when the hit test could not prove the
        element (it is covered, or inside a frame): attributes only, nothing the page can see."""
        for tag in ("textarea", "select"):
            if loc.locator("xpath=self::%s" % tag).count():
                return classify_field(tag)
        if loc.locator("xpath=self::input").count():
            return classify_field("input", loc.get_attribute("type"))
        ce = loc.get_attribute("contenteditable")
        return classify_field("div", None, ce is not None and ce.lower() != "false")

    def hfill(selector, value, **options):
        why = "the value is not a string"
        try:
            if isinstance(value, str):
                timeout = options.get("timeout")
                loc = page.locator(selector).first
                box = _box_of(loc, timeout, options.get("force"), selector)
                info = _describe(box)
                kind = (classify_field(info["tag"], info["type"], info["ce"]) if info
                        else _classify_fallback(loc))
                why = ("the field is disabled or read-only" if info and (info.get("disabled") or info.get("readonly"))
                       else "the %s route did not verify" % kind if kind in ("text", "range", "datetime")
                       else "no humane route for a %s field" % kind)
                if not (info and (info.get("disabled") or info.get("readonly"))):
                    if kind == "text" and _fill_text(loc, box, value, info, selector):
                        return None
                    if kind == "range" and _fill_range(loc, box, info, value):
                        return None
                    if kind == "datetime" and _fill_datetime(loc, box, info, value):
                        return None
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = str(e) if isinstance(e, _Native) else "error: %s" % (e,)
        # Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
        # raises, as before humanize), and anything the humane route could not prove it did right.
        _report_fallback(page, "Page.fill", why, selector)
        return native_page_fill(selector, value, **options)

    def hdblclick(selector, **options):
        o = dict(options)
        o.setdefault("click_count", 2)
        return _targeted_click(native_page_dblclick, selector, o, no_click=False, native_opts=options)

    native_page_focus = getattr(page, "focus", None)

    def _fallback(api, reason, selector=None):
        """A humanized route hands this call to Playwright: lane A's report (strict mode raises
        ClearcoteStrictError and nothing is sent; otherwise on_fallback hears it). Never swallowed."""
        return _report_fallback(page, api, reason, selector)

    def _focus_for_press(selector, timeout):
        """Give ``selector`` the focus before a key goes to it. A text field: Tab from the field before
        it (only when empty: Tab selects the text) or a click into it. Anything else -- a button, a
        link, a tabindex container -- through _focus_target (Tab within 3 stops, or a click that only
        focuses); an activatable target out of Tab reach gets the pointer over it and the focus from
        Playwright (reported), and the key is still pressed with a human hold."""
        loc = page.locator(selector).first
        if _focused(loc):
            return
        if not loc.is_visible():
            raise _Native("not visible")   # Playwright waits for it
        box = _box_of(loc, timeout, selector=selector)
        info = _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            if _tab_into(loc, info, selector, need_empty=True):
                return
            if not _focus_click(box, _proven_point(box, info), loc, selector) or not _focused(loc):
                raise _Native("the click did not put the caret in the field")
            time.sleep(_rand(40, 120) / 1000.0)
            return
        if _focus_target(selector, timeout):
            return
        if native_page_focus is None:
            raise _Native("no humane focus route (out of Tab reach, no page.focus)")
        _fallback("Page.press", "focus: an activatable target out of Tab reach")
        native_page_focus(selector, **({"timeout": timeout} if timeout is not None else {}))
        time.sleep(_rand(40, 120) / 1000.0)

    def hpress(selector, key, **options):
        """page.press (and Locator.press): the target gets the focus the way a person gives it
        (_focus_for_press: never Playwright's focus-and-press in 0 ms), then the key is pressed through
        the keyboard model -- a chord modifier first, the persona's hold (or the caller's `delay`),
        ``repeat=n`` as OS auto-repeat, and the submit / Tab pause after typing."""
        repeat = options.pop("repeat", 1)
        try:
            _focus_for_press(selector, options.get("timeout"))
            _after_typing(key)
            d = options.get("delay")
            _press_key(key, d if d and d > 0 else None, repeat)
            return None
        except _Native as e:
            why = str(e)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.press", why, selector)
        r = None
        for _ in range(max(1, int(repeat or 1))):
            r = native_page_press(selector, key, **options)
        return r

    def hfocus(selector, **options):
        """page.focus / Locator.focus: Tab or a click (_focus_target), verified; Playwright's focus for
        an activatable target out of Tab reach (reported) and for anything the humane route cannot do."""
        try:
            if _focus_target(selector, options.get("timeout")):
                return None
            _fallback("Page.focus", "an activatable target out of Tab reach")
        except _Native:
            _fallback("Page.focus", "native focus")
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return native_page_focus(selector, **options)

    def _blur(selector, options):
        """Locator.blur, humanized: a focused element is left the way the persona leaves fields -- Tab
        to the next stop, or a click on a blank point beside it (_keys.BLANK_POINT; Tab when there is
        none). True when focus is verified gone; False = Playwright's blur (nothing to blur, or the
        humane route did not take the focus away)."""
        loc = page.locator(selector).first
        if not _focused(loc):
            return False
        if traits.blur_style == "click" and native_down is not None and native_up is not None:
            box = _box_of(loc, options.get("timeout"), selector=selector)
            vw, vh = _viewport()
            pt = world_for(page).evaluate(BLANK_POINT, {"box": box, "vw": vw, "vh": vh}) if box else None
            if isinstance(pt, list) and len(pt) == 2:
                x, y = float(round(pt[0] + random.gauss(0, 3))), float(round(pt[1] + random.gauss(0, 3)))
                _glide(x, y, target_w=60)
                _press_here(x, y)
                time.sleep(_rand(40, 120) / 1000.0)
                if not _focused(loc):
                    return True
        _after_typing("Tab")
        _press_key("Tab")
        time.sleep(_rand(40, 120) / 1000.0)
        return not _focused(loc)

    def _selected_all():
        s = world_for(page).evaluate(SELECTION_STATE)
        return isinstance(s, list) and len(s) == 3 and min(s[0], s[1]) == 0 and max(s[0], s[1]) == s[2]

    def _select_text(selector, options):
        """Locator.select_text, humanized: a text control's text selected the way the persona does it --
        a triple click (an <input>), or the keys: End then Shift+Home (Ctrl+End, Ctrl+Shift+Home in a
        textarea; Cmd+A on macOS, where Home/End do not move the caret). Verified in the isolated world
        (SELECTION_STATE), one select-all retry; False = Playwright's select_text."""
        loc = page.locator(selector).first
        box = _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = _describe(box) if box else None
        if not info or info.get("disabled") or classify_field(info["tag"], info["type"], info["ce"]) != "text":
            return False
        tag = info["tag"]
        if traits.select_style == "triple" and tag == "input" and native_down is not None and native_up is not None:
            at = _proven_point(box, info)
            x, y = at if at is not None else click_point(box, st["pos"], persona)
            x, y = float(round(x)), float(round(y))
            _glide(x, y)
            _press_at(x, y, normalize_click_options({"click_count": 3}, _platform()))
        else:
            if not _focused(loc):
                if not _focus_click(box, _proven_point(box, info), loc, selector) or not _focused(loc):
                    return False
                time.sleep(_rand(120, 300) / 1000.0)
            if _host_platform() == "macos":
                _press_key("ControlOrMeta+a")
            elif tag == "input":
                _press_key("End")
                time.sleep(_rand(80, 200) / 1000.0)
                _press_key("Shift+Home")
            else:
                _press_key("ControlOrMeta+End")
                time.sleep(_rand(80, 200) / 1000.0)
                _press_key("ControlOrMeta+Shift+Home")
        time.sleep(_rand(60, 160) / 1000.0)
        if _selected_all():
            return True
        _select_all()
        time.sleep(_rand(60, 160) / 1000.0)
        return _selected_all()

    def _guarded(api, fn, native):
        """Run the humane ``fn`` (True = done); Playwright's ``native`` when it declines or fails
        (a timeout propagates)."""
        def call(selector, **options):
            try:
                if fn(selector, options):
                    return None
            except _Native:
                pass
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise
            _fallback(api, "native")
            return native(selector, **options)
        return call

    page._clearcote_blur = _guarded(
        "Locator.blur", _blur, lambda s, **o: _LOCATOR_NATIVE["blur"](page.locator(s).first, **o))
    page._clearcote_select_text = _guarded(
        "Locator.select_text", _select_text, lambda s, **o: _LOCATOR_NATIVE["select_text"](page.locator(s).first, **o))

    # getattr for the same reason as mouse.down/up above: a page-like object that does not
    # implement select_option must still get every OTHER humanization, not lose all of it to
    # an AttributeError raised while wiring this one up.
    native_page_select_option = getattr(page, "select_option", None)

    def _open_and_pick(selector, plan, at=None, dl=None):
        """Open the drop-down with a click, move its highlight with the planned keys, commit with
        Enter: one trusted input+change, the sequence a mouse-and-keyboard user produces. While the
        popup is open its keys never reach the page (measured on r37: value unchanged until Enter).
        The opening click is the full humanized click -- including the covered-point check -- at the
        point the hit test proved when the centre is covered (``at``, a Playwright `position`), so a
        floating label over the select's centre cannot swallow it and leave the letters to change a
        closed select one per key. ``dl`` is the select's deadline (select_deadline); True when the
        commit navigated the page (_commit_pick)."""
        # move_off=False: the popup is open now and this sequence owns the pointer until the pick lands.
        _targeted_click(native_page_click, selector, {"position": at} if at else {}, no_click=False,
                        native_opts=None, move_off=False)
        # Every pause here is the persona's (timing_traits.pause_scale): the windows were one fixed
        # set for every identity.
        time.sleep(pause_ms(persona, 220, 480) / 1000.0)        # the list opens; the eye finds the option
        pace = timing_traits(persona).pause_scale * tempo   # the persona's deliberation x the preset
        for ch in plan["keys"]:
            native_kb_press(ch, delay=_kdwell())
            time.sleep(max(0.04, random.gauss(0.10, 0.035) * pace))   # well inside type-ahead's 1 s window
        n = plan["arrows"]
        if n:
            time.sleep(pause_ms(persona, 120, 300) / 1000.0)
            _arrow_run("ArrowDown" if n > 0 else "ArrowUp", abs(n))
        time.sleep(pause_ms(persona, 150, 400) / 1000.0)
        return _commit_pick(selector, dl)

    def _commit_pick(selector, dl):
        """Enter with the persona's hold, bounded by the select's deadline (commit_select)."""
        dwell = _kdwell()
        return commit_select(page, selector, dl, dwell, pause_ms(persona, 80, 160),
                             lambda: native_kb_press("Enter", delay=dwell))

    def _arrow_run(key, n):
        """``n`` presses of an arrow key in an OPEN drop-down. A long run is HELD, the way a person
        crosses a long list: one press, the keyboard's repeat delay, then auto-repeat at its rate
        (_hold_key, _keys.plan_repeat). Pressed one by one at a human ~180 ms each, 370 rows took 68 s
        (verifier B). The open popup takes these keys, so the page sees none of them, only how long the
        pick took."""
        if n < 8:
            _key(key, n)
            return
        _hold_key(key, None, n)

    def _listbox_row(info, i):
        return world_for(page).call_on_node(info["node"], ISO_LISTBOX_ROW, {"i": i})

    def _settled_row(info, i):
        """The list box's state once its scroll has come to rest. A wheel notch scrolls the box
        SMOOTHLY over a few frames, and a read taken mid-animation shows the row where it was, not
        where it is going: the old loop then wheeled the wrong way and oscillated (verifier B:
        scrollTop 120 -> 240 -> 323 -> 203 -> 83 -> 0, twice, with the row in view at 120, then a
        native, untrusted fallback). Two equal reads 50 ms apart = at rest."""
        prev = _listbox_row(info, i)
        for _ in range(30):
            time.sleep(0.05)
            cur = _listbox_row(info, i)
            if cur is None or prev is None:
                return cur
            if abs(cur["st"] - prev["st"]) < 0.5 and abs(cur["row"][1] - prev["row"][1]) < 0.5:
                return cur
            prev = cur
        return prev

    def _row_span(s):
        """The visible vertical span (top, bottom) of the row, or None when too little of it shows.
        A row cut off by the box edge is still a target a person clicks when most of it shows."""
        x, y, w, h = s["row"]
        if w <= 0 or h <= 0:
            return None
        top, bottom = max(y, s["top"]), min(y + h, s["bottom"])
        return (top, bottom) if bottom - top >= min(h, max(6.0, h * 0.6)) else None

    def _scroll_row_into_view(info, i):
        """Wheel the list box until row ``i`` shows: the pointer goes over the box once, then as many
        notches as the distance needs -- one at first, to learn how far a notch scrolls this box --
        each burst read only once the scroll has settled. (top, bottom, state), or None when it
        cannot be brought into view (the wheel does not move the box, or it is at its end)."""
        s = _settled_row(info, i)
        if s is None:
            return None
        span, per, over, notches = _row_span(s), None, False, 0
        while span is None and notches < 60:
            x, y, w, h = s["row"]
            room = s["bottom"] - s["top"]
            margin = _rand(2, max(3, min(12, (room - h) * 0.4)))
            need = (y + h) - s["bottom"] + margin if y + h > s["bottom"] else y - s["top"] - margin
            if (need > 0 and s["st"] >= s["max"] - 0.5) or (need < 0 and s["st"] <= 0.5):
                return None
            if not over:
                bx, by, bw, bh = s["box"]
                _glide(bx + bw * _rand(0.3, 0.7), s["top"] + room * _rand(0.3, 0.7), target_w=max(20, room))
                time.sleep(_rand(80, 200) / 1000.0)
                over = True
            n = 1 if not per else max(1, min(8, int(abs(need) // per) + (1 if abs(need) % per > per * 0.25 else 0)))
            for j in range(n):
                native_wheel(0, _notch_px() if need > 0 else -_notch_px())
                notches += 1
                if j < n - 1:
                    time.sleep(_rand(30, 70) / 1000.0)
            before = s["st"]
            s = _settled_row(info, i)
            if s is None:
                return None
            moved = abs(s["st"] - before)
            if moved < 0.5:
                return None   # the wheel did not scroll the box
            per = moved / n
            span = _row_span(s)
        return (span[0], span[1], s) if span else None

    def _select_listbox(loc, box, info, idx):
        """A list box (multiple or size > 1): click the rows -- the first plainly (which also drops
        any other selection), the rest with Ctrl/Cmd held, scrolling the box with the wheel to bring
        a row into view the way a person does."""
        want = sorted(idx)
        opts = info["options"]
        if set(want) == {i for i, o in enumerate(opts) if o.get("selected")}:
            return [opts[i]["value"] for i in want]
        for k, i in enumerate(want):
            got = _scroll_row_into_view(info, i)
            if got is None:
                return None
            top, bottom, s = got
            r = s["row"]
            px = _rand(r[0] + r[2] * 0.12, r[0] + min(r[2] * 0.6, 140))
            py = (top + bottom) / 2 + random.gauss(0, max(0.5, (bottom - top) * 0.12))
            py = min(max(py, top + 1), bottom - 1)
            _glide(px, py, target_w=bottom - top)
            if k and native_kb_down is not None:
                # ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
                _press_at(px, py, normalize_click_options({"modifiers": ["ControlOrMeta"]}))
            else:
                _press_here(px, py)
            time.sleep(_rand(150, 350) / 1000.0)
        final = _listbox_row(info, want[0])
        if not final:
            return None
        got = [i for i, sel in enumerate(final["sel"]) if sel]
        return [opts[i]["value"] for i in want] if got == want else None

    def _select_humanized(selector, value, options):
        """Pick options the way a person does (see _open_and_pick / _select_listbox). Returns the
        selected values like Playwright, or None when the humane route cannot apply or cannot be
        shown to have worked -- the caller then runs stock select_option."""
        if options.get("element") is not None:
            return None
        # {value} / {label} / {index} option dicts (and lists of them) are wanted options like the
        # bare strings, as in Node (selectWants); they used to go native, and Playwright Python does
        # not even take them.
        wants = select_wants(value, options.get("index"), options.get("label"))
        if not wants:
            return None
        dl = select_deadline(options, page)
        loc = page.locator(selector).first
        box = _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = _describe(box, "select")
        if not info or info.get("tag") != "select" or info.get("disabled"):
            return None
        opts = info["options"]
        idx = []
        for kind, w in wants:
            i = want_index(opts, kind, w)
            if i < 0 or opts[i].get("disabled"):
                return None   # not there (yet) or disabled: Playwright waits / reports it
            if i not in idx:
                idx.append(i)
        if info.get("multiple") or (info.get("size") or 0) > 1:
            if not info.get("multiple"):
                idx = [min(idx)]
            return _select_listbox(loc, box, info, idx)
        t = min(idx)   # Playwright: the FIRST option (document order) matching any wanted one
        if info.get("selectedIndex") == t:
            return [opts[t]["value"]]
        labels = [None if o.get("disabled") else (o.get("label") or "") for o in opts]
        plan = plan_select(labels, info.get("selectedIndex", -1), t)
        if plan is None:
            return None
        pt = _proven_point(box, info)
        at = ({"x": pt[0] - box["x"] - info.get("bl", 0), "y": pt[1] - box["y"] - info.get("bt", 0)}
              if pt else None)
        if _open_and_pick(selector, plan, at, dl):
            return [opts[t]["value"]]   # the change handler navigated: nothing left to read back
        if loc.input_value(timeout=select_left_ms(dl)) == opts[t]["value"]:
            return [opts[t]["value"]]
        # One correction from wherever the highlight landed (arrows only), then give up to native.
        again = _redescribe(info, "select")
        if not again:
            return None
        fix = plan_select(labels, again.get("selectedIndex", -1), t, max_prefix=0)
        if fix is None:
            return None
        if _open_and_pick(selector, fix, at, dl):
            return [opts[t]["value"]]
        return [opts[t]["value"]] if loc.input_value(timeout=select_left_ms(dl)) == opts[t]["value"] else None

    def hselect_option(selector, value=None, **options):
        """page.select_option, humanized (see _select_humanized)."""
        try:
            res = _select_humanized(selector, value, options)
            if res is not None:
                return res
            why = _select_why(value, options)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.select_option", why, selector)
        value, options = native_select_args(value, options)   # option dicts: Playwright Python takes none
        return native_page_select_option(selector, value, **options)

    def _label_aim(selector, loc, timeout):
        """Where a person clicks a checkbox / radio when not on its own box: its <label>. A HIDDEN
        input (a custom checkbox: zero-size, opacity 0, clipped) has no box to click at all, so its
        label is the only target (its ::before box is just part of the label's box); a visible one
        gets its label TEXT 40 % of the time (label_rng). None = aim at the input itself, as before:
        no label, no CDP, a non-CSS selector for a hidden input, the 60 %."""
        w = world_for(page)
        if loc.is_visible():
            box = _box_of(loc, timeout, selector=selector)
            info = _describe(box) if box else None
            if not info or info.get("tag") != "input" or info.get("type") not in ("checkbox", "radio"):
                return None
            lab = w.call_on_node(info["node"], ISO_LABEL_OF)
            if not lab or not lab.get("label") or lab.get("hidden"):
                return None
            if not label_draw(persona):   # the persona's share; THIS click's choice is unseeded
                return None
            lb = lab["label"]
            r = lb.get("text")
            # A label that WRAPS its control: LABEL_OF's text rect is the content clear of the input's box (SC 239 box
            # probe, 10-17: the first content rect IS the input's box, and taking it for the text put every "label"
            # click on the input -- lab F_label_visible 0/60, verifier B 0/20). No text left (a label holding only
            # the control), or an aim box still over the input's box: the input is the only target.
            if r is None and lb.get("contains"):
                return None
            r = r or lb["rect"]
            lbox = {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}
            if lbox["width"] < 2 or lbox["height"] < 2 or _boxes_overlap(lbox, lab.get("rect")):
                return None
            # The click AIMS inside the label's text, but the hit test is asked about the LABEL ELEMENT's box:
            # HIT_IN_BOX proves "the node at (x, y) is the element filling box b", and no element fills the text's
            # own rect. Asked with the text rect it failed on every visible label, so the click went to the input:
            # 0 of 20 label clicks (verifier B) and 0 of 18 (the lab) on d2b9ab6 (SC 232 triage, 10-16).
            lr = lab["label"]["rect"]
            lbox["hit"] = {"x": lr[0], "y": lr[1], "width": lr[2], "height": lr[3]}
            return lbox
        # Not visible: Playwright would wait for it and time out. A plain-CSS selector can still be
        # resolved in the isolated world (Playwright must count the same elements, as _css_box).
        if not isinstance(selector, str) or not selector:
            return None
        lab = w.evaluate(ISO_LABEL_BY_SEL, {"sel": selector})
        if not lab or not lab.get("n") or not lab.get("hidden") or not lab.get("label"):
            return None
        try:
            if page.locator(selector).count() != lab["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        r = lab["label"]["rect"]
        lbox = {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}
        if lbox["width"] < 2 or lbox["height"] < 2:
            return None
        if not _in_view(lbox):
            lbox = _scroll_window_to(lbox) or lbox
        return lbox if _in_view(lbox) else None

    def _click_label(lbox):
        """One humanized click inside ``lbox`` (a label's box), where the hit test proves the label owns
        the point. False when it does not (something covers it): the caller aims at the input."""
        x, y = click_point(lbox, st["pos"], persona)
        x, y = float(round(x)), float(round(y))
        time.sleep(0.05)
        if _hit_in_box(lbox.get("hit") or lbox, x, y) is False:   # the label element's box (see _label_aim)
            return False
        _glide(x, y)
        _press_at(x, y, normalize_click_options({}, _platform()))
        return True

    def _set_checked(orig, selector, checked, options):
        """check / uncheck / set_checked: a humanized click when the state must change, then the
        state verified -- Playwright raises when a click does not change it, so do we (by falling
        through to the native call, which raises with its own message). `trial` performs no action:
        the state is read (a non-checkbox raises, as in Playwright) and, when a click would be needed,
        its checks run here (_trial) -- not Playwright's trial, which moves the pointer and presses.

        The click lands on the input's box, or on its <label> (_label_aim): always for a hidden custom
        checkbox (the box click waited for it to show and timed out), 40 % of the time otherwise."""
        loc = page.locator(selector).first
        if options.get("trial"):
            if loc.is_checked(timeout=options.get("timeout")) == checked:
                return None
            return _trial(selector, options, no_click=False)
        # Errors propagate as Playwright's own (not a checkbox, timed out): the click below owns its
        # native fallback, so nothing here may click a second time.
        if loc.is_checked(timeout=options.get("timeout")) == checked:
            return None
        clicked = False
        if not options.get("force") and not options.get("position"):
            try:
                lbox = _label_aim(selector, loc, options.get("timeout"))
                clicked = bool(lbox) and _click_label(lbox)
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise
                clicked = False
        if not clicked:
            _targeted_click(native_page_click, selector, dict(options), no_click=False, native_opts=None)
        if loc.is_checked() == checked:
            return None
        # One click did not change it (a page that cancels the click): Playwright's check raises here
        # rather than clicking again, and so does this.
        from playwright.sync_api import Error as PlaywrightError
        raise PlaywrightError("Clicking the checkbox did not change its state")   # worded per API (api_errors)

    page.type = api_errors("Page.type", htype)
    page.fill = api_errors("Page.fill", hfill)
    page.dblclick = api_errors("Page.dblclick", hdblclick)
    page.press = api_errors("Page.press", hpress)
    if native_page_focus is not None:
        page.focus = api_errors("Page.focus", hfocus)
    if native_page_select_option is not None:
        page.select_option = api_errors("Page.select_option", hselect_option)
    for name, want in (("check", True), ("uncheck", False)):
        orig_c = getattr(page, name, None)
        if orig_c is not None:
            setattr(page, name, api_errors("Page." + name, (lambda o, w: lambda selector, **opts: _set_checked(
                o, selector, w, opts))(orig_c, want)))
    orig_sc = getattr(page, "set_checked", None)
    if orig_sc is not None:
        page.set_checked = api_errors("Page.set_checked", lambda selector, checked, **opts: _set_checked(
            lambda s, **o: orig_sc(s, checked, **o), selector, bool(checked), opts))

    def _hit_in_box(box, x, y, sel=None):
        """Does a press at viewport (x, y) reach the element filling ``box``? True / False, or None
        when it cannot be told here (CDP is unavailable). A point inside a child frame is answered
        by the <iframe> element that holds it (IsolatedWorld.node_at)."""
        w = world_for(page)
        nid = w.node_at(x, y)
        if nid is None:
            return None
        b = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
        if sel:
            b["sel"] = sel   # a plain-CSS selector: whether the hit is THAT element (see HIT_IN_BOX)
        res = w.call_on_node(nid, ISO_HIT_IN_BOX, b)
        return None if res is None else bool(res)

    def _reaches_point(box, x, y, sel=None):
        """The covered-point check, through the browser's own hit test read in the isolated world:
        nothing is dispatched and the pointer does not move. It replaced a Playwright TRIAL action,
        which moves the REAL pointer onto the element and back and performs a press with its events
        blocked: verifier B saw, before every humanized click since 0.46.0, a mousemove inside the
        target, a 450-500 px jump back, then the glide, and a pointerdown/pointerup/click (left, even
        for a right click) that window capture listeners could see. A covered point is looked at
        again for COVER_CHECK_MS (an overlay fading out). A point inside a child frame is answered by
        the <iframe> element that holds it (IsolatedWorld.node_at), so no Playwright trial runs for it
        either. False when covered; None when the hit test cannot run (no CDP): the caller goes native."""
        deadline = time.monotonic() + COVER_CHECK_MS / 1000.0
        hit = _hit_in_box(box, x, y, sel)
        while hit is False and time.monotonic() < deadline:
            time.sleep(0.08)
            hit = _hit_in_box(box, x, y, sel)
        return hit

    def _same_box(a, b):
        return (abs(a["x"] - b["x"]) <= 1 and abs(a["y"] - b["y"]) <= 1
                and abs(a["width"] - b["width"]) <= 1 and abs(a["height"] - b["height"]) <= 1)

    def _settled_box(loc, timeout, selector):
        """The element's box once it has stopped moving: two reads ~100 ms apart within 1 px, for up to
        SETTLE_MS. None when it never settles or has no box. Read like _box_of (the isolated CSS read, else
        bounding_box), with the wheel, so a target the settling scroll left out of view is brought back in."""
        deadline = time.monotonic() + SETTLE_MS / 1000.0
        prev = _box_of(loc, timeout, False, selector)
        while prev and time.monotonic() < deadline:
            time.sleep(0.1)
            nxt = _box_of(loc, timeout, False, selector)
            if not nxt:
                return None
            if _same_box(prev, nxt):
                return nxt
            prev = nxt
        return None

    def _uncover(box, x, y):
        """A target that is not moving yet fails the hit test is covered. Two covers a person deals with
        without thinking, which used to send the click to Playwright (r53 /audit, seed 2, 2026-10-28):
         - something the pointer itself opened: it rested on a header link and a hover menu dropped over
           the target. The hand moves off it (below it where there is room, else above) and the menu closes;
         - a fixed or sticky bar (a site header) over a target the scroll left under it: the wheel brings
           the target down into the band a reader looks at.
        True when it did one of them (the caller re-reads the box and hit-tests again)."""
        c = world_for(page).evaluate(ISO_COVER, {"x": x, "y": y, "b": {
            "x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}})
        if not isinstance(c, dict) or not isinstance(c.get("rect"), list) or len(c["rect"]) != 4:
            return False
        cx, cy, cw, ch = (float(v) for v in c["rect"])
        vw, vh = _viewport()
        px, py = st["pos"]
        if cx <= px <= cx + cw and cy <= py <= cy + ch:
            # Off its edge by 6-80 px where the viewport has room (a mega menu can leave only a strip),
            # below first.
            room_below, room_above = vh - 4 - (cy + ch + 6), cy - 6 - 4
            if room_below >= 0:
                ty = cy + ch + 6 + _rand(0, min(74, room_below))
            elif room_above >= 0:
                ty = cy - 6 - _rand(0, min(74, room_above))
            else:
                return False
            tx = min(vw - 8, max(8, box["x"] + box["width"] / 2 + _rand(-60, 60)))
            _glide(tx, ty)
            time.sleep(_rand(250, 450) / 1000.0)   # a hover menu's close transition
            return True
        # A cover filling most of the viewport is a modal or a cookie wall, not a bar to scroll out from under.
        if c.get("fixed") and cw * ch <= 0.8 * vw * vh:
            dy = box["y"] + box["height"] / 2 - vh * _rand(0.3, 0.5)
            if abs(dy) < 1:
                return False
            hwheel(0, dy)   # not mouse.wheel: no after-action step inside the click
            return True
        return False

    def _position_point(box, o):
        """The viewport point of Playwright's `position` (relative to the PADDING box: the border
        widths are added)."""
        info = _describe(box, None)
        bl, bt = (info.get("bl", 0), info.get("bt", 0)) if info else (0, 0)
        return box["x"] + bl + o["position"]["x"], box["y"] + bt + o["position"]["y"]

    def _trial(selector, options, no_click):
        """`trial=True`, answered by the SDK: Playwright's actionability checks -- visible, enabled
        (not for hover), stable, and the click point receives the press (the browser's own hit test,
        read in the isolated world) -- retried until the caller's timeout (or the page default), then
        a Playwright TimeoutError. NO pointer move and NO press: Playwright's own trial moves the real
        pointer onto the target and dispatches pointerdown/pointerup/click that page listeners
        registered before Playwright's blocker see (SC 209: 6/6). The point checked is Playwright's:
        `position`, else the centre. Scrolling the element into view is part of the checks, as in
        Playwright. With `force` the checks are skipped: only the element must exist."""
        o = normalize_click_options(options, _platform())
        timeout = options.get("timeout")
        ms = float(timeout) if timeout is not None else default_timeout_ms(page)
        end = time.monotonic() + ms / 1000.0 if ms > 0 else None
        loc = page.locator(selector).first

        def left():   # what is left of the timeout for a Playwright wait (0 there: no limit)
            return 0 if end is None else max(1, int((end - time.monotonic()) * 1000.0))

        if o["force"]:
            loc.wait_for(state="attached", timeout=left())
            return None
        while True:
            why = "element is not visible"
            # waits for visible; raises Playwright's timeout. No wheel: a trial must not move the pointer.
            box = _box_of(loc, left(), selector=selector, wheel=False)
            if box:
                if not no_click and not loc.is_enabled():
                    why = "element is not enabled"
                else:
                    if o["position"]:
                        x, y = _position_point(box, o)
                    else:
                        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    time.sleep(0.05)   # stable: 50 ms on, the element still fills `box` (HIT_IN_BOX)
                    if _hit_in_box(box, x, y, selector) is not False:
                        return None    # reached (None: the hit test cannot run here -- nothing to add)
                    why = ("element is not stable, or another element would receive the pointer at "
                           "(%d, %d)" % (x, y))
            if end is not None and time.monotonic() >= end:
                raise trial_timeout_error(selector, ms, why)
            time.sleep(0.1 if end is None else max(0.0, min(0.1, end - time.monotonic())))

    def _targeted_click(orig, selector, options, no_click, native_opts, dwell=None, move_off=True):
        """The humanized form of page.click / dblclick / hover (and the click inside check/uncheck).

        Every Playwright option is honoured: `button`, `click_count`, `delay` (the hold) and
        `modifiers` shape the press; `position` replaces the persona's point; `force` skips the
        hit test; `trial` performs NO action: its checks run here (_trial), with no pointer move and
        no press. The old wrapper forwarded only `timeout`, so a right click became a left click and
        a trial clicked."""
        o = normalize_click_options(options, _platform())

        aim = []   # where Playwright's own click lands, once the box is known (_native)

        def _native():
            if orig is None:
                raise RuntimeError("humanized click could not run")
            try:
                return orig(selector, **(native_opts if native_opts is not None else options))
            finally:
                # Native click/hover genuinely move the browser pointer to the element. Leaving
                # st["placed"] False afterwards made the next wheel anchor believe the pointer was
                # still unplaced, so it glided away from the element the click had just left.
                st["placed"] = True
                # ... and away from where the engine walk left it: the engine path must not walk
                # from its own stale point (_engine_in_sync; verifier A #6). The next SDK glide
                # starts where Playwright pressed: `position`, else the centre of the box.
                _eng["at"] = None
                if aim:
                    st["pos"] = aim[-1]

        if o["trial"]:
            return _trial(selector, options, no_click)
        try:
            timeout = options.get("timeout")   # None: the page's default, as Playwright would use
            loc = page.locator(selector).first
            # Playwright skips actionability with force: no wait then -- a target it would click
            # hidden goes native at once instead of timing out first.
            box = _box_of(loc, timeout, o["force"], selector)
            if not box:
                raise _Native("the element has no box")
            if not o["force"] and not loc.is_enabled():
                raise _Native("the element is not enabled")

            def aim_at(b):
                aim.append((b["x"] + b["width"] / 2, b["y"] + b["height"] / 2))
                if o["position"]:
                    px, py = _position_point(b, o)
                    aim.append((px, py))
                else:
                    px, py = click_point(b, st["pos"], persona)
                # Whole pixels: the hit test below answers for the pixel DOM.getNodeForLocation rounds
                # to, so the press goes to that pixel too. A press at 971.6 next to a badge ending at 972
                # was proven at 972 and landed on the badge, reported as a success (verifier B, SC 210).
                return float(round(px)), float(round(py))

            x, y = aim_at(box)
            if not o["force"] and orig is not None:
                # Stability, then covered-by: 50 ms on, the element must still fill `box` (an
                # animation in flight does not) and own the click point -- no trusted press at a
                # point some overlay owns. Either failing goes native: Playwright waits for it.
                time.sleep(0.05)
                hit = _reaches_point(box, x, y, selector)
                # A target still moving (the scroll that brought it in settling, a layout shift) fails
                # the test with the box it had. Re-aim at the box it settles at rather than hand the
                # click to Playwright, whose press and release land in the same millisecond with a
                # one-report jump from rest (/audit pointer-press-dwell-duration, pointer-jump-from-rest).
                # A target that is covered rather than moving is uncovered first (_uncover); what stays
                # covered is handled below.
                tries = 0
                while hit is False and tries < SETTLE_TRIES:
                    tries += 1
                    nxt = _settled_box(loc, timeout, selector)
                    if not nxt:
                        break
                    if _same_box(nxt, box):
                        if not _uncover(nxt, x, y):
                            break
                        nxt = _settled_box(loc, timeout, selector)
                        if not nxt:
                            break
                    box = nxt
                    x, y = aim_at(box)
                    hit = _reaches_point(box, x, y, selector)
                if hit is False and not o["position"]:
                    # The persona's point is covered (a badge, an icon, a floating label over the
                    # centre). Before giving the click to Playwright -- which waited out its timeout on
                    # 5-7 of 16 badge-covered buttons (EVAL B) -- aim at the first probe point the hit
                    # test proves reaches the element, the way _describe finds a covered field.
                    alt = _proven_click_point(box, selector)
                    if alt is None:
                        raise _Native("the click point does not reach the element (covered, moving or clipped)")
                    x, y = alt
                    aim.append((x, y))
                elif not hit:
                    raise _Native("the hit test cannot run here" if hit is None else
                                  "the click point does not reach the element (covered, moving or clipped)")
            if o["modifiers"] and native_kb_down is None:
                raise _Native("modifiers need keyboard.down")
            if not no_click and not o["force"] and orig is not None and _hz_engine_mode(page, _engine_clicks_mode) == "all":
                # Opt-in (engine "all" / CLEARCOTE_HUMANIZE_ENGINE_CLICKS=all): the engine walks the approach
                # and clicks the point the hit test above proved. A plain left click takes the r42 route (while
                # the engine knows where the pointer is, _engine_in_sync; an r43 engine re-syncs from the SDK's
                # position, fromX/fromY); a right / double / modified click takes the r43 extensions, as
                # mouse.click does (_engine_ext_click; verifier A's R5). Otherwise: the planner.
                if engine_path_ok(o) and _engine_glide(x, y, no_click=False):
                    return None
                if _engine_ext_click(x, y, o):
                    return None
            if no_click and o["modifiers"]:
                # hover with modifiers: Playwright holds them across the move
                held = []
                try:
                    for m in o["modifiers"]:
                        native_kb_down(m)
                        held.append(m)
                        time.sleep(_rand(40, 110) / 1000.0)
                    _glide(x, y, target_w=_aim_w(box))
                    _dwell(x, y, box, dwell)
                finally:
                    for m in reversed(held):
                        time.sleep(_rand(20, 80) / 1000.0)
                        try:
                            native_kb_up(m)   # one failed keyup must not strand the others
                        except Exception:  # noqa: BLE001
                            pass
                return None
            _glide(x, y, target_w=_aim_w(box))
            if no_click:
                _dwell(x, y, box, dwell)   # the hand rests on what it hovered (SC 232 lane C)
            else:
                if native_down is None or native_up is None:
                    _pre_press()
                    native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                                 button=o["button"], click_count=o["click_count"])
                else:
                    _press_at(x, y, o)
                if move_off:
                    _schedule_move_off()
            return None
        except _Native as e:
            why = str(e)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.hover" if no_click else "Page.click", why, selector)
        # outside the try, so a native call that raises is not run a second time by the handler
        return _native()

    def _make_targeted(orig, no_click):
        def wrapped(selector, **options):
            return _targeted_click(orig, selector, options, no_click, None)
        return wrapped

    page.click = api_errors("Page.click", _make_targeted(page.click, False))
    native_page_hover = page.hover

    def _hover(selector, dwell=None, **options):
        # `dwell` (ms; 0: none) is the SDK's, not Playwright's: never forwarded to a native hover
        return _targeted_click(native_page_hover, selector, options, True, None, dwell=dwell)
    page.hover = api_errors("Page.hover", _hover)
    # Locator.drag_to(trial=True) answers its checks here too (see the Locator patch).
    page._clearcote_trial = _trial

    # press_and_hold (_hold.py) runs on the public page.mouse (so its glide and pre-press pause are
    # these wrappers'), and takes three things from here: the box helper (wait, wheel scroll, hit-test
    # proof), where the pointer is (for the persona's click point), and a move that does NOT glide --
    # the held hand's one-pixel drift, which a planned drag leg would turn into a walk.
    page._clearcote_box_of = lambda loc, timeout, selector=None: _box_of(loc, timeout, False, selector)
    page._clearcote_pointer = lambda: st["pos"]
    # ... and for its press point: a glide with the box's own Fitts width, the covered-point check
    # (the same isolated hit test as a click, retried COVER_CHECK_MS), the probe-proven point when the
    # chosen one is covered, and the border widths that make `position` padding-box relative like click's.
    page._clearcote_aim_glide = lambda x, y, box: _glide(x, y, target_w=_aim_w(box))
    page._clearcote_reaches_point = _reaches_point
    page._clearcote_proven_point = _proven_click_point

    def _border_of(box):
        info = _describe(box, None)
        return (float(info.get("bl", 0) or 0), float(info.get("bt", 0) or 0)) if info else (0.0, 0.0)
    page._clearcote_border_of = _border_of

    def _nudge(x, y):
        native_move(x, y)
        st["pos"] = (x, y)
        st["placed"] = True
    page._clearcote_nudge = _nudge
    # the frame route (_frames) clears a field with the same held chord as the page routes
    page._clearcote_select_all = _select_all
    page.press_and_hold = api_errors("Page.press_and_hold", lambda selector, duration=None, **kw: press_and_hold(
        page.locator(selector).first, duration, **kw))

    # ------------------------------------------------------------- drags (drag_to / drag_and_drop / drag_by)
    def _loc_selector(loc):
        try:
            return loc._impl_obj._selector
        except Exception:  # noqa: BLE001
            return None

    def _peek_box(loc, selector):
        """The box where it is now (no wait, no scroll): the isolated CSS read, else bounding_box()."""
        if _css_plain(selector):
            return _rect_box(world_for(page).evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False}))
        return loc.bounding_box()

    def _aim(box, position, force, selector):
        """The press point inside ``box``: Playwright's ``position`` (padding-box relative, as click),
        else the persona's click point. Unless ``force``, the browser's hit test must prove it reaches
        the element (retried like a click for COVER_CHECK_MS); a covered point tries the probe points
        (an icon over the centre); None when nothing reaches it (the caller goes native)."""
        if position:
            x, y = _position_point(box, {"position": position})
        else:
            x, y = click_point(box, st["pos"], persona)
        x, y = float(round(x)), float(round(y))
        if force:
            return x, y
        time.sleep(0.05)   # stable: 50 ms on, still the same box
        hit = _reaches_point(box, x, y, selector)
        if hit is None or hit:
            return x, y
        if position:
            return None
        for fx, fy in probe_points[1:]:
            px = float(round(box["x"] + box["width"] * fx))
            py = float(round(box["y"] + box["height"] * fy))
            if _hit_in_box(box, px, py, selector) is True:
                return px, py
        return None

    def _creep(frm, toward, grab_ms):
        """The drag-start hesitation alone (the drop point not known yet): after the press the hand
        holds, creeping < 8 px toward where it is about to go (_motion.drag_hesitation)."""
        steps = drag_hesitation(frm, toward, persona, dur_ms=grab_ms / tempo)   # _dispatch scales by tempo
        _dispatch(steps, kind="drag", buttons=1)
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    def _held_drag(frm, to, grab_ms, settle=True):
        """Hesitation + the held leg in one walk (_motion.hesitate_then_drag): inside 8 px of the press
        for the persona's grab window (100-300 ms), then the drag leg to ``to``."""
        steps, _ = hesitate_then_drag(frm, to, persona, grab_ms / tempo, settle=settle)   # _dispatch scales by tempo
        _dispatch(steps, kind="drag", buttons=1)
        st["pos"] = (to[0], to[1])

    def _drag(src, dst, opts):
        """The humanized drag of locator ``src`` onto locator ``dst`` (Locator.drag_to,
        page.drag_and_drop). Honours ``source_position`` / ``target_position`` (relative to the padding
        box, like click), ``force`` (no hit tests) and ``timeout`` (for each end to become visible).
        Both ends are hit tested like a click; a covered end tries the probe points, then raises
        _Native for the caller's native fallback. Before 0.52 the drag used the exact box centres, no
        hit test, and ignored every option. True when done."""
        timeout = opts.get("timeout")
        force = bool(opts.get("force"))
        ssel, dsel = _loc_selector(src), _loc_selector(dst)
        sbox = _box_of(src, timeout, force, ssel)
        if not sbox:
            raise _Native("the source has no box")
        sp = _aim(sbox, opts.get("source_position"), force, ssel)
        if sp is None:
            raise _Native("the source point is covered (no probe point reaches it)")
        if not dst.is_visible():
            if force:
                raise _Native("the target is hidden and force is set")
            dst.wait_for(state="visible", timeout=timeout)
        tbox = _peek_box(dst, dsel)
        if not tbox:
            raise _Native("the target has no box")
        tp = None
        if _in_view(tbox):
            tp = _aim(tbox, opts.get("target_position"), force, dsel)
            if tp is None:
                raise _Native("the target point is covered (no probe point reaches it)")
        if native_down is None or native_up is None:
            raise _Native("no mouse.down / mouse.up to drag with")
        grab_ms, release_ms = drag_dwell(persona)
        _glide(sp[0], sp[1], target_w=max(6.0, min(sbox["width"], sbox["height"])))
        _pre_press()
        st["held"] = True
        try:
            native_down()
            if tp is None:
                _creep(sp, (tbox["x"] + tbox["width"] / 2, tbox["y"] + tbox["height"] / 2), grab_ms)
                # the drop zone is out of view: the free hand scrolls it in while the other holds
                got, ok, _ = bring_into_view(_scroll_io(), lambda: _peek_box(dst, dsel))
                if not ok or not got:
                    _fallback("Locator.drag_to", "scroll into view: target_out_of_view")
                    dst.scroll_into_view_if_needed(timeout=timeout)
                    got = _peek_box(dst, dsel)
                if not got:
                    raise RuntimeError("drag target vanished")
                pos = opts.get("target_position")
                tp = _position_point(got, {"position": pos}) if pos else click_point(got, st["pos"], persona)
                tp = (float(round(tp[0])), float(round(tp[1])))
                _glide(tp[0], tp[1], settle=True, held=True)
            else:
                _held_drag(sp, tp, grab_ms)
            time.sleep(release_ms / 1000.0)
        finally:
            try:
                native_up()
            finally:
                st["held"] = False
        return True

    def _settled_read(read):
        """``read()`` once two reads 50 ms apart agree (a widget animating to the pointer has not)."""
        a = read()
        for _ in range(12):
            time.sleep(0.05)
            b = read()
            if a is not None and b is not None and abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01:
                return b
            a = b
        return a if a is not None else (0.0, 0.0)

    def _drag_by_core(px, py, dx, dy, read, tol=2.0, grab_ms=None):
        """Closed-loop held drag from (px, py), already pressed: creep, glide PAST the target by 3-8 px
        (a hand overshoots a slider and comes back), read where the dragged thing actually is (``read()``
        -> achieved (dx, dy)), correct up to twice with a settle in between, and return the achieved
        delta. Legs: 1-3 (kept in st["drag_legs"])."""
        d = (dx * dx + dy * dy) ** 0.5
        if grab_ms is None:
            grab_ms = drag_dwell(persona)[0]
        if d < 0.5:
            st["drag_legs"] = 0
            return _settled_read(read)
        ux, uy = dx / d, dy / d
        past = _rand(3.0, 8.0)
        _held_drag((px, py), (px + dx + ux * past, py + dy + uy * past), grab_ms, settle=False)
        legs = 1
        got = (0.0, 0.0)
        while True:
            time.sleep(_rand(60, 140) / 1000.0)     # the hand settles; the widget catches up
            got = _settled_read(read)
            ex, ey = dx - got[0], dy - got[1]
            if (ex * ex + ey * ey) ** 0.5 <= tol or legs >= 3:
                break
            _glide(st["pos"][0] + ex, st["pos"][1] + ey, settle=True, held=True)
            legs += 1
        st["drag_legs"] = legs
        return got

    def _drag_by(loc, dx, dy, opts):
        """locator.drag_by / page.drag_by: press on the element (``position`` or the persona's point),
        drag it by (dx, dy) closed-loop (_drag_by_core), release. ``read``: a callable returning the
        achieved (dx, dy) (or the x delta alone); default: how far the element's own box moved.
        Returns the final achieved (dx, dy)."""
        timeout = opts.get("timeout")
        force = bool(opts.get("force"))
        sel = _loc_selector(loc)
        box = _box_of(loc, timeout, force, sel)
        if not box:
            raise RuntimeError("drag_by: the element has no box (not rendered)")
        p0 = _aim(box, opts.get("position"), force, sel)
        if p0 is None:
            raise RuntimeError("drag_by: another element would receive the press")
        user_read = opts.get("read")
        c0 = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        def read():
            if user_read is not None:
                v = user_read()
                if v is None:
                    return None
                if isinstance(v, (int, float)):
                    return (float(v), 0.0)
                return (float(v[0]), float(v[1]))
            b = _peek_box(loc, sel)
            if not b:
                return None
            return (b["x"] + b["width"] / 2 - c0[0], b["y"] + b["height"] / 2 - c0[1])

        grab_ms, release_ms = drag_dwell(persona)
        _glide(p0[0], p0[1], target_w=max(6.0, min(box["width"], box["height"])))
        _pre_press()
        st["held"] = True
        try:
            native_down()
            got = _drag_by_core(p0[0], p0[1], float(dx), float(dy), read, float(opts.get("tolerance") or 2.0),
                                grab_ms)
            page._clearcote_drag_legs = st.get("drag_legs")
            time.sleep(release_ms / 1000.0)
        finally:
            try:
                native_up()
            finally:
                st["held"] = False
        return got

    page._clearcote_drag = _drag
    page._clearcote_drag_by = _drag_by
    native_page_dnd = getattr(page, "drag_and_drop", None)

    def hdrag_and_drop(source, target, **opts):
        """page.drag_and_drop, humanized like Locator.drag_to (the first match of each selector; more
        than one match goes to the native call, which raises Playwright's strict-mode error)."""
        if opts.get("trial"):
            for sel, pos in ((source, opts.get("source_position")), (target, opts.get("target_position"))):
                o = {k: opts[k] for k in ("force", "timeout") if opts.get(k) is not None}
                if pos:
                    o["position"] = pos
                _trial(sel, o, True)
            return None
        why = None
        try:
            src, dst = page.locator(source), page.locator(target)
            if src.count() <= 1 and dst.count() <= 1:
                _drag(src.first, dst.first, opts)
                return None
        except _Native as e:
            why = str(e) or "the humane drag cannot apply"
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e) or _is_strict(e):
                raise
            why = "error: %s" % (e,)
        if why:   # more than one match is not a fallback: Playwright raises its own strict-mode error
            _report_fallback(page, "Page.drag_and_drop", why, source)
        return native_page_dnd(source, target, **opts)

    if native_page_dnd is not None:
        page.drag_and_drop = api_errors("Page.drag_and_drop", hdrag_and_drop)

    def hdrag_by(selector, dx, dy, **opts):
        """page.drag_by(selector, dx, dy): see Locator.drag_by."""
        return _drag_by(page.locator(selector).first, dx, dy, opts)

    page.drag_by = api_errors("Page.drag_by", hdrag_by)

    # The after-action step first, then the per-call switch outermost: humanize=False goes straight
    # to the stock call and skips the after-action step too.
    _install_after_action()
    _wire_per_call_off(page, st, _stock, _stock_mouse, _stock_kb)

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


def _is_strict(err):
    """Lane A's ClearcoteStrictError (strict mode): never swallowed by a best-effort path."""
    return isinstance(err, ClearcoteStrictError)


# ------------------------------------------------------------------ per-call humanize=False
# Every wrapped page / mouse / keyboard method takes humanize=False: the stock Playwright call, with the
# keyword stripped. Wired outermost, after attach has wrapped everything (_wire_per_call_off).
_PAGE_METHODS = ("click", "hover", "dblclick", "type", "fill", "press", "select_option", "check", "uncheck",
                 "set_checked", "focus", "drag_and_drop")
_MOUSE_METHODS = ("move", "click", "wheel", "dblclick", "down", "up")
_KB_METHODS = ("type", "press", "down", "up")


_SDK_ONLY_KW = ("dwell",)


def _per_call(stock, fn, after=None):
    @functools.wraps(fn)
    def call(*a, humanize=None, **kw):
        if humanize is False:
            for k in _SDK_ONLY_KW:   # the SDK's own keywords (hover dwell=): Playwright's call takes none of them
                kw.pop(k, None)
            n = max(1, int(kw.pop("repeat", 1) or 1))   # press(repeat=n): n stock presses
            try:
                r = None
                for _ in range(n):
                    r = stock(*a, **kw)
                return r
            finally:
                if after is not None:
                    after(a)
        return fn(*a, **kw)
    call._clearcote_stock = stock
    return call


def _wire_per_call_off(page, st, stock, stock_mouse, stock_kb):
    def placed(a):
        st["placed"] = True

    def moved(a):
        st["placed"] = True
        if len(a) >= 2:
            st["pos"] = (a[0], a[1])

    for n, s in stock.items():
        cur = getattr(page, n, None)
        if s is not None and cur is not None and cur is not s:
            setattr(page, n, _per_call(s, cur, placed if n in ("click", "hover", "dblclick") else None))
    mouse = page.mouse
    for n, s in stock_mouse.items():
        cur = getattr(mouse, n, None)
        if s is None or cur is None or cur is s:
            continue
        if n == "down":
            def s_down(*a, _s=s, **kw):   # stock press, but the held state stays tracked
                st["held"] = True
                try:
                    return _s(*a, **kw)
                except Exception:
                    st["held"] = False
                    raise
            setattr(mouse, n, _per_call(s_down, cur))
        elif n == "up":
            def s_up(*a, _s=s, **kw):
                try:
                    return _s(*a, **kw)
                finally:
                    st["held"] = False
            setattr(mouse, n, _per_call(s_up, cur))
        else:
            setattr(mouse, n, _per_call(s, cur, moved if n in ("move", "click", "dblclick") else None))
    page._clearcote_stock_mouse = dict(stock_mouse)
    if getattr(page, "drag_by", None) is not None:   # no Playwright drag_by: humanize=False is the plain stock drag
        page.drag_by = _per_call(lambda selector, dx, dy, **kw: _stock_drag_by(page.locator(selector).first, dx, dy, **kw),
                                 page.drag_by)
    kb = page.keyboard
    for n, s in stock_kb.items():
        cur = getattr(kb, n, None)
        if s is not None and cur is not None and cur is not s:
            setattr(kb, n, _per_call(s, cur))


def _stock_drag_by(self, dx, dy, **kw):
    """Locator.drag_by(..., humanize=False): there is no Playwright drag_by, so the stock mouse does the plain
    thing -- press at ``position`` (else the box centre), one move by (dx, dy), release -- and the asked delta
    is returned (no read, no correction)."""
    page = self.page
    m = getattr(page, "_clearcote_stock_mouse", None) or {}
    box = self.bounding_box(timeout=kw.get("timeout"))
    if not box:
        raise RuntimeError("drag_by: the element has no box (not rendered)")
    pos = kw.get("position")
    x = box["x"] + (pos["x"] if pos else box["width"] / 2)
    y = box["y"] + (pos["y"] if pos else box["height"] / 2)
    move, down, up = (m.get("move") or page.mouse.move), (m.get("down") or page.mouse.down), (m.get("up") or page.mouse.up)
    move(x, y)
    down()
    try:
        move(x + dx, y + dy)
    finally:
        up()
    return (float(dx), float(dy))


def _locator_off(stock, fn):
    """A Locator method that takes humanize=False: the stock Playwright method, keyword stripped."""
    @functools.wraps(fn)
    def call(self, *a, humanize=None, **kw):
        if humanize is False:
            return stock(self, *a, **kw)
        return fn(self, *a, **kw)
    return call


def _select_why(value, options):
    """Why select_option went to Playwright when the humane route returned nothing."""
    if options.get("element") is not None:
        return "element handles go to Playwright"
    for v in (value, options.get("index"), options.get("label")):
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if isinstance(x, dict):
                return "option dicts ({value, label, index}) go to Playwright"
            if x is not None and not isinstance(x, (str, int)):
                return "element handles go to Playwright"
    return "no humane route, or the pick could not be shown to have worked"


def _frame_why(loc):
    """Why a locator on a humanized page is not routed: it enters or lives in a child frame. None when
    the page is not humanized (not a fallback) or the locator is routable."""
    try:
        if not getattr(loc.page, "_clearcote_humanized", False):
            return None
        if "internal:control=enter-frame" in (loc._impl_obj._selector or ""):
            return "frame locators go to Playwright"
        if loc._impl_obj._frame.parent_frame is not None:
            return "a locator in a child frame goes to Playwright"
    except Exception:  # noqa: BLE001
        return None
    return None


class _Native(Exception):
    """Internal: the humane route cannot apply here; run the native Playwright call instead."""


def _no_touch(loc):
    """Is ``loc``'s page in a context WITHOUT touch (no hasTouch), where Playwright's tap raises? A
    page-like object whose context cannot be read counts as touch-capable (the tap is routed)."""
    try:
        return loc.page.context._impl_obj._options.get("hasTouch") is not True
    except Exception:  # noqa: BLE001
        return False


def _is_timeout(err):
    """A Playwright timeout: the element never became actionable. Falling back to the native call
    after one would wait the FULL timeout again (a missing element took 2-3x `timeout` to fail), so
    timeouts propagate instead."""
    return type(err).__name__ == "TimeoutError"

# --------------------------------------------------------------------------- locator patch
_locator_patched = False
# Playwright's own Locator methods the humanized ones fall back to (blur / select_text), by name.
_LOCATOR_NATIVE = {}


def _patch_locator_class():
    """Patch the sync Locator class once so locator.* interactions route through the humanized
    page.* methods. Each method is a no-op unless the locator's page has humanize active AND the
    locator targets the main frame; anything else (frame locators, exotic selectors, errors)
    falls straight through to the original Playwright behaviour. Composes safely if another
    library has already patched Locator — we chain to whatever was there before."""
    global _locator_patched
    if _locator_patched:
        return
    _locator_patched = True
    _fr.patch_frame_class(is_async=False)   # child-frame selector actions -> the frame route
    try:
        from playwright.sync_api._generated import Locator
    except Exception:  # noqa: BLE001
        return

    o_fill = Locator.fill
    o_click = Locator.click
    o_type = Locator.type
    o_dblclick = Locator.dblclick
    o_hover = Locator.hover
    o_press = Locator.press
    o_press_seq = Locator.press_sequentially
    o_clear = Locator.clear
    o_tap = Locator.tap
    o_check = Locator.check
    o_uncheck = Locator.uncheck
    o_drag_to = Locator.drag_to
    o_select_option = Locator.select_option
    o_set_checked = getattr(Locator, "set_checked", None)
    o_focus = getattr(Locator, "focus", None)
    o_blur = getattr(Locator, "blur", None)
    o_select_text = getattr(Locator, "select_text", None)
    _LOCATOR_NATIVE.update(blur=o_blur, select_text=o_select_text)

    def _on(self):
        # humanize active for this page?
        if not getattr(self.page, "_clearcote_humanized", False):
            return False
        # main-frame only: frame-locator selectors don't round-trip through page.* reliably,
        # so let those use native Playwright (which handles frames correctly). A FrameLocator's
        # child locator keeps the MAIN frame as its _frame and enters the iframe through its
        # selector, so the selector is what tells: on the humanized path its prefilled field was
        # never cleared ("old" + "new" = "oldnew").
        try:
            if "internal:control=enter-frame" in (self._impl_obj._selector or ""):
                return False
            return self._impl_obj._frame.parent_frame is None
        except Exception:  # noqa: BLE001
            return False

    def _strict(self):
        """Locator actions are STRICT in Playwright: more than one match is an error. The page-level
        methods they route through take the first match, so check the count here and let the native
        call raise its own strict-mode error when it is not exactly one."""
        try:
            return self.count() <= 1
        except Exception:  # noqa: BLE001
            return False

    def _sel(self):
        return self._impl_obj._selector

    # The options each page-level humanized method understands. Everything the caller passed that a
    # page method accepts travels with it: the old _fwd sent ONLY `timeout`, which turned
    # click(button="right") into a left click and click(trial=True) into a real one.
    _CLICK = ("button", "click_count", "delay", "force", "modifiers", "no_wait_after", "position",
              "timeout", "trial")
    _HOVER = ("force", "modifiers", "no_wait_after", "position", "timeout", "trial", "dwell")
    _DBL = ("button", "delay", "force", "modifiers", "no_wait_after", "position", "timeout", "trial")
    _FILL = ("force", "no_wait_after", "timeout")
    # `delay` travels: humanized typing keeps it as the shortest gap between two keys (it was dropped).
    _TYPE = ("delay", "timeout")
    # `repeat` is the SDK's (OS auto-repeat, hpress); Playwright's own press does not take it.
    _PRESS = ("delay", "no_wait_after", "timeout", "repeat")
    _SELECT = ("index", "label", "element", "force", "no_wait_after", "timeout")
    _CHECK = ("force", "no_wait_after", "position", "timeout", "trial")

    def _fwd(kw, names=("timeout",)):
        return {k: kw[k] for k in names if k in kw and kw[k] is not None}

    def _froute(self, action, args, kw):
        """A locator inside a child frame (FrameLocator, Frame.locator) on a humanized page: through the
        frame route (_frames.route: the frame's own isolated world for the hit test, the page's humanized
        mouse and keyboard for the input). (False, None) when it does not apply -- the native method
        runs, and the route has reported why."""
        if not (_fr.humanized(self.page) and _fr.frame_locator_of(self)) or not _strict(self):
            return False, None
        r = _fr.route(self, action, args, kw)
        return (False, None) if r is _fr.NATIVE else (True, r)

    def _route(self, kw, fn, frame_routed=True):
        # Once routed, every error propagates: the page-level wrappers own the native fallback, so
        # retrying the original locator method here ran the action a second (or third) time -- a
        # checkbox whose click is cancelled was clicked three times.
        if _on(self) and _strict(self):
            return True, fn()
        # A frame locator: the frame route (_froute, lane F) runs next and reports its own fallbacks; only an
        # action without a frame route (tap) reports here.
        why = None if frame_routed else _frame_why(self)
        if why:
            _report_fallback(self.page, "Locator", why, _sel(self))
        return False, None

    def fill(self, value, **kw):
        done, r = _route(self, kw, lambda: self.page.fill(_sel(self), value, **_fwd(kw, _FILL)))
        if done:
            return r
        done, r = _froute(self, 'fill', (value,), kw)
        return r if done else o_fill(self, value, **kw)

    def click(self, **kw):
        done, r = _route(self, kw, lambda: self.page.click(_sel(self), **_fwd(kw, _CLICK)))
        if done:
            return r
        done, r = _froute(self, 'click', (), kw)
        return r if done else o_click(self, **kw)

    def type_(self, text, **kw):
        done, r = _route(self, kw, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        if done:
            return r
        done, r = _froute(self, 'type', (text,), kw)
        return r if done else o_type(self, text, **kw)

    def dblclick(self, **kw):
        done, r = _route(self, kw, lambda: self.page.dblclick(_sel(self), **_fwd(kw, _DBL)))
        if done:
            return r
        done, r = _froute(self, 'dblclick', (), kw)
        return r if done else o_dblclick(self, **kw)

    def hover(self, **kw):
        done, r = _route(self, kw, lambda: self.page.hover(_sel(self), **_fwd(kw, _HOVER)))
        if done:
            return r
        done, r = _froute(self, 'hover', (), kw)
        # `dwell` is the SDK's: Playwright's own hover does not take it
        return r if done else o_hover(self, **{k: v for k, v in kw.items() if k != "dwell"})

    def press(self, key, **kw):
        done, r = _route(self, kw, lambda: self.page.press(_sel(self), key, **_fwd(kw, _PRESS)))
        if done:
            return r
        # repeat= is the SDK's (OS auto-repeat on the humanized route); the frame route presses once
        n = max(1, int(kw.pop("repeat", 1) or 1))
        if n == 1:
            done, r = _froute(self, 'press', (key,), kw)
            if done:
                return r
        for _ in range(n):   # not humanized: n separate Playwright presses
            r = o_press(self, key, **kw)
        return r

    def focus(self, **kw):
        done, r = _route(self, kw, lambda: self.page.focus(_sel(self), **_fwd(kw, ("timeout",))))
        return r if done else o_focus(self, **kw)

    def blur(self, **kw):
        fn = getattr(self.page, "_clearcote_blur", None)
        if fn is not None:
            done, r = _route(self, kw, lambda: fn(_sel(self), **_fwd(kw, ("timeout",))))
            if done:
                return r
        return o_blur(self, **kw)

    def select_text(self, **kw):
        fn = getattr(self.page, "_clearcote_select_text", None)
        if fn is not None:
            done, r = _route(self, kw, lambda: fn(_sel(self), **_fwd(kw, ("force", "timeout"))))
            if done:
                return r
        return o_select_text(self, **kw)

    def press_sequentially(self, text, **kw):
        done, r = _route(self, kw, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        if done:
            return r
        done, r = _froute(self, 'press_sequentially', (text,), kw)
        return r if done else o_press_seq(self, text, **kw)

    def clear(self, **kw):
        done, r = _route(self, kw, lambda: self.page.fill(_sel(self), "", **_fwd(kw, _FILL)))
        if done:
            return r
        done, r = _froute(self, 'clear', (), kw)
        return r if done else o_clear(self, **kw)

    def tap(self, **kw):
        if _no_touch(self):
            # Playwright's tap raises at once on a context without hasTouch ("The page does not
            # support tap"); routed as a click it clicked instead, or waited out the timeout for a
            # missing element and raised a TimeoutError (SC 210 error parity).
            return o_tap(self, **kw)
        if _on(self):
            # A touch context: there is no humanized touch sequence yet, so the tap goes out as the humanized
            # MOUSE click. Lane A's report: strict mode raises ClearcoteStrictError (a Playwright error) and
            # nothing is sent; otherwise on_fallback hears it and the click runs as before.
            report_fallback(self.page, "Locator.tap", TAP_NOT_HUMANIZED + ": sent as a humanized mouse click",
                            _sel(self))
        done, r = _route(self, kw, lambda: self.page.click(_sel(self), **_fwd(kw, _HOVER)), frame_routed=False)
        return r if done else o_tap(self, **kw)

    def check(self, **kw):
        done, r = _route(self, kw, lambda: self.page.check(_sel(self), **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = _froute(self, 'check', (), kw)
        return r if done else o_check(self, **kw)

    def uncheck(self, **kw):
        done, r = _route(self, kw, lambda: self.page.uncheck(_sel(self), **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = _froute(self, 'uncheck', (), kw)
        return r if done else o_uncheck(self, **kw)

    def set_checked(self, checked, **kw):
        done, r = _route(self, kw, lambda: self.page.set_checked(_sel(self), checked, **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = _froute(self, 'set_checked', (checked,), kw)
        return r if done else o_set_checked(self, checked, **kw)

    def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here: one
        implementation behind the page method is what stops the two entry points drifting apart
        (which is how page.select_option once stayed untrusted while the locator call was not)."""
        done, r = _route(self, kw, lambda: self.page.select_option(_sel(self), value, **_fwd(kw, _SELECT)))
        if done:
            return r
        done, r = _froute(self, "select_option", (value,), kw)
        if done:
            return r
        value, kw = native_select_args(value, kw)   # option dicts: Playwright Python takes none
        return o_select_option(self, value, **kw)

    def drag_to(self, target, **kw):
        if kw.get("trial") and _on(self) and _on(target) and _strict(self) and _strict(target):
            # Playwright's drag trial moves the pointer onto the source and presses there (SC 209);
            # the SDK runs the checks of both ends instead: visible, stable, receives the pointer.
            trial = getattr(self.page, "_clearcote_trial", None)
            if trial is not None:
                for loc, pos in ((self, kw.get("source_position")), (target, kw.get("target_position"))):
                    opts = {k: kw[k] for k in ("force", "timeout") if kw.get(k) is not None}
                    if pos:
                        opts["position"] = pos
                    trial(_sel(loc), opts, True)
                return None
        if not _on(self) and not kw.get("trial") and _frame_why(self):
            _report_fallback(self.page, "Locator.drag_to", _frame_why(self), _sel(self))
        if not kw.get("trial") and _on(self) and _on(target) and _strict(self) and _strict(target):
            drag = getattr(self.page, "_clearcote_drag", None)
            if drag is not None:
                why = "the humane drag cannot apply"
                try:
                    drag(self, target, kw)
                    return None
                except _Native as e:
                    why = str(e) or why
                except Exception as e:  # noqa: BLE001
                    if _is_timeout(e) or _is_strict(e):
                        raise
                    why = "error: %s" % (e,)
                # a covered end, no CDP, a vanished element: Playwright's own drag (lane A's report first)
                _report_fallback(self.page, "Locator.drag_to", why, _sel(self))
        return o_drag_to(self, target, **kw)

    # Each worded as Playwright words the Locator method (api_errors): routed through page.*, an
    # error read "Page.click: ..." or "Locator.wait_for: ...".
    Locator.fill = api_errors("Locator.fill", _locator_off(o_fill, fill))
    Locator.click = api_errors("Locator.click", _locator_off(o_click, click))
    Locator.type = api_errors("Locator.type", _locator_off(o_type, type_))
    Locator.dblclick = api_errors("Locator.dblclick", _locator_off(o_dblclick, dblclick))
    Locator.hover = api_errors("Locator.hover", _locator_off(o_hover, hover))
    Locator.press = api_errors("Locator.press", _locator_off(o_press, press))
    Locator.press_sequentially = api_errors("Locator.press_sequentially", _locator_off(o_press_seq, press_sequentially))
    Locator.clear = api_errors("Locator.clear", _locator_off(o_clear, clear))
    Locator.tap = api_errors("Locator.tap", _locator_off(o_tap, tap))
    Locator.check = api_errors("Locator.check", _locator_off(o_check, check))
    Locator.uncheck = api_errors("Locator.uncheck", _locator_off(o_uncheck, uncheck))
    Locator.drag_to = api_errors("Locator.drag_to", _locator_off(o_drag_to, drag_to))

    def drag_by(self, dx, dy, **kw):
        """New, not a Playwright method: drag this element by (dx, dy) px, closed loop (glide past,
        read where it went, correct up to twice), and return the achieved (dx, dy). ``read``: a
        callable returning the achieved delta (default: the element's own box movement); ``position``,
        ``force``, ``timeout``, ``tolerance`` (px, default 2). Needs a humanized page."""
        fn = getattr(self.page, "_clearcote_drag_by", None)
        if fn is None:
            raise RuntimeError("drag_by needs humanize=True")
        return fn(self, dx, dy, kw)

    Locator.drag_by = api_errors("Locator.drag_by", _locator_off(_stock_drag_by, drag_by))
    Locator.select_option = api_errors("Locator.select_option", _locator_off(o_select_option, select_option))
    # New, not a Playwright method: press and hold this element (_hold.py). Works on frame locators too:
    # it presses at viewport coordinates through page.mouse.
    Locator.press_and_hold = api_errors(
        "Locator.press_and_hold", lambda self, duration=None, **kw: press_and_hold(self, duration, **kw))
    if o_set_checked is not None:
        Locator.set_checked = api_errors("Locator.set_checked", _locator_off(o_set_checked, set_checked))
    if o_focus is not None:
        Locator.focus = api_errors("Locator.focus", _locator_off(o_focus, focus))
    if o_blur is not None:
        Locator.blur = api_errors("Locator.blur", _locator_off(o_blur, blur))
    if o_select_text is not None:
        Locator.select_text = api_errors("Locator.select_text", _locator_off(o_select_text, select_text))


def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    """Install humanize/show_cursor on a single context: every page it has now and every page it
    opens later (a popup, window.open, a link with target=_blank) -- through its "page" event.

    Attaches whether or not the context has a Browser (mirrors Node's installHumanizeOnContext): a
    persistent context has none (context.browser is None; launch_persistent_context), and this used to
    attach only when a Browser was found, so humanize was silently off there. attach_humanize needs
    only the page; the engine route takes the browser from the page's context when there is one and
    the page's own CDP session when there is not. Hooking a context twice is a no-op (the marker), and
    attach_humanize is idempotent per page."""
    humanize = _hz_resolve(humanize) or False   # True | "strict" | HumanizeOptions | dict; raises on a bad one
    if not humanize and not show_cursor:
        return
    b = browser or getattr(context, "browser", None)
    if show_cursor:
        try:
            context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if getattr(context, "_clearcote_hooked", False) is not True:
        try:
            context._clearcote_hooked = True
        except Exception:  # noqa: BLE001
            pass
        context.on("page", lambda p: attach_humanize(b, p, humanize, show_cursor, seed))
    for p in getattr(context, "pages", None) or ():
        attach_humanize(b, p, humanize, show_cursor, seed)


def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    """Install humanize/show_cursor on a browser: wrap new_page/new_context so every page is covered.

    ``seed`` (the fingerprint seed) selects the stable per-identity motor persona. ``humanize``: True,
    "strict", a HumanizeOptions or a dict of its fields (_hzoptions)."""
    humanize = _hz_resolve(humanize) or False
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    def new_page(**kw):
        page = orig_new_page(**kw)
        attach_humanize(browser, page, humanize, show_cursor, seed)
        # browser.new_page makes its context INSIDE Playwright (not through the wrapped new_context
        # below), so hook that implicit context too: a popup it opens is a page the "page" event
        # delivers, and nothing else sees it.
        ctx = getattr(page, "context", None)
        if ctx is not None:
            try:
                install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
            except Exception:  # noqa: BLE001
                pass
        return page

    def new_context(**kw):
        ctx = orig_new_context(**kw)
        install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
