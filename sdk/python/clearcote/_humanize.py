"""Humanized input + cursor visualization (Python).

``humanize=True`` installs ONE consistent human-input standard that covers moving, clicking,
dragging, scrolling and typing — dispatched as native trusted input (isTrusted=True,
navigator.webdriver stays False). It works whether you drive the page directly or via locators:

  * page level   — page.click / page.hover / page.dblclick / page.type / page.fill / page.press,
                   page.mouse.move / mouse.click / mouse.wheel and held-button drags
                   (mouse.down → mouse.move → mouse.up), page.keyboard.type
  * locator level — locator.click / type / fill / hover / dblclick / press /
                    press_sequentially / clear / check / uncheck / tap / drag_to
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

import random
import time

from ._motion import make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient

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

# Clearcote ships a Windows x64 binary, so Control is the correct select-all modifier.
_SELECT_ALL = "Control+a"

# Compact keyboard-adjacency map for realistic fat-finger typos.
_NEARBY = {
    "a": "sqwz", "b": "vghn", "c": "xdfv", "d": "sfecx", "e": "wrsdf", "f": "dgrtcv",
    "g": "fhtyb", "h": "gjybn", "i": "ujko", "j": "hkunm", "k": "jloi", "l": "kop",
    "m": "njk", "n": "bhjm", "o": "iklp", "p": "ol", "q": "wa", "r": "edft",
    "s": "awedxz", "t": "rfgy", "u": "yhji", "v": "cfgb", "w": "qase", "x": "zsdc",
    "y": "tghu", "z": "asx",
}


def _rand(a, b):
    return a + random.random() * (b - a)


def _nearby_key(ch):
    lo = ch.lower()
    if lo in _NEARBY:
        w = random.choice(_NEARBY[lo])
        return w.upper() if ch.isupper() else w
    return ch


def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay.

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random."""
    if show_cursor:
        def inject():
            try:
                page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        inject()
        page.on("load", lambda _=None: inject())
        page.on("framenavigated", lambda f: inject() if f == page.main_frame else None)
    if not humanize:
        return

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is consistent within a session and unlinkable across seeds. Stored on the page so
    # the module-level Locator.drag_to patch can reach it too.
    persona = make_persona(seed)
    page._clearcote_persona = persona

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

    def _dispatch(steps):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol time.sleep. Native input carries button state, so a button held via
        mouse.down() stays pressed across the whole path (drag)."""
        for sx, sy, sl in steps:
            try:
                native_move(sx, sy)
            except Exception:  # noqa: BLE001
                break
            st["placed"] = True
            time.sleep(sl / 1000.0)

    def _glide(x, y, settle=False, target_w=24):
        """Move to (x, y) as a minimum-jerk sum-of-submovements path (primary ballistic + corrective
        homing) with colored sub-pixel noise and Fitts-scaled duration (see _motion.plan_move).
        Continuous (starts at the tracked position, lands exactly on target)."""
        _dispatch(plan_move(st["pos"], (x, y), persona, target_w=target_w, settle=settle))
        st["pos"] = (x, y)

    # --- Engine real-trajectory routing (PRO) --------------------------------
    # Route point-to-point moves/clicks through the browser engine's
    # Browser.humanizedClick: a trusted WebMouseEvent path built in the browser
    # process from REAL recorded human trajectories (PRO) or the engine bezier
    # (free) -- tiered automatically by the engine's runtime license gate, so the
    # SDK stays tier-agnostic. One CDP call per action (far less Input-domain
    # traffic than per-step native moves); continuity is tracked engine-side.
    # Held-button DRAGS stay on the native path (the engine click can't hold a
    # button across the path -- sliders depend on it); wheel/typing unchanged.
    # Falls back to the SDK bezier (plan_move) permanently if the method is
    # unavailable (older engine, or a persistent context with no Browser handle).
    _eng = {"session": None, "tid": None, "ok": True}

    def _engine_glide(x, y, no_click):
        if not _eng["ok"]:
            return False
        try:
            if _eng["session"] is None:
                tsession = page.context.new_cdp_session(page)
                _eng["tid"] = tsession.send(
                    "Target.getTargetInfo")["targetInfo"]["targetId"]
                b = getattr(page.context, "browser", None)
                _eng["session"] = (b.new_browser_cdp_session()
                                   if b is not None else tsession)
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            _eng["session"].send("Browser.humanizedClick", {
                "targetId": _eng["tid"], "x": float(x), "y": float(y),
                "duration": dur, "noClick": bool(no_click)})
            # The engine runs the trajectory in the BROWSER process and answers this command
            # before it has finished walking it. Without waiting, a following mouse.down() fires
            # at the cursor's stale position -- measured: a move to (110,58) then a press landed
            # at (981,629), on <body> instead of the slider, so every move-then-press sequence
            # (drag, press-and-hold, drag-and-drop) acted on the wrong element.
            time.sleep(dur)
            # Keep Playwright's own cursor position in sync so a following
            # mouse.down()/drag presses where the engine left the cursor.
            try:
                native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
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
        _glide(x, y)

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
        # try/finally semantics so a raising press cannot strand held=True and turn every later
        # move into a phantom drag leg. hup already guarded this; hdown did not.
        try:
            st["held"] = True
            native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    def hup(**kw):
        try:
            native_up(**kw)
        finally:
            st["held"] = False

    def hclick(x, y, **kw):
        if _engine_glide(x, y, no_click=False):
            return
        _glide(x, y)
        time.sleep(_rand(40, 130) / 1000.0)    # brief dwell before pressing, like a human
        try:
            # delay = mousedown->mouseup HOLD (human ~60-150ms); without it Playwright releases in ~2ms.
            native_click(x, y, delay=click_hold(persona))
        except Exception:  # noqa: BLE001
            pass

    def _held_glide(x, y):
        _glide(x, y, settle=True)                # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor activity (idle drift + non-goal moves) so a
        behavioral collector sees pointer entropy BEFORE the first goal action. page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            steps = plan_ambient(st["pos"], {"width": vp["width"], "height": vp["height"]}, persona, ms)
            _dispatch(steps)
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # DEFAULT under humanize (no longer opt-in): every page load fires a short burst
    # of ambient pointer entropy automatically, so a behavioral collector sees human
    # mouse activity on the page before the first goal action. Disable per-page with
    # page._clearcote_auto_ambient = False.
    page._clearcote_auto_ambient = True

    def _auto_ambient(_=None):
        if getattr(page, "_clearcote_auto_ambient", False):
            try:
                hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: _auto_ambient())

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
            wh = page.evaluate("() => [innerWidth, innerHeight]")
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
            x, y = st["pos"]
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if st["held"] or (st["placed"] and 8 <= x <= vw - 8 and 8 <= y <= vh - 8):
                return
            # Reading position: upper-middle, gaussian-spread. The exact centre is itself a tell —
            # nobody parks the pointer on the centre pixel of the viewport.
            ax = min(max(vw * 0.5 + random.gauss(0, vw * 0.10), vw * 0.12), vw * 0.88)
            ay = min(max(vh * 0.34 + random.gauss(0, vh * 0.09), vh * 0.10), vh * 0.62)
            _glide(ax, ay, target_w=140)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    def hwheel(delta_x, delta_y):
        _wheel_anchor()
        steps = max(5, min(24, round((abs(delta_x) + abs(delta_y)) / 60)))
        px = py = 0
        for i in range(1, steps + 1):
            t = i / steps
            f = 1 - (1 - t) ** 2.2           # ease-OUT: a fast flick decaying to a slow inertial settle
            nx, ny = round(delta_x * f), round(delta_y * f)
            native_wheel(nx - px, ny - py)
            px, py = nx, ny
            # local sleep, NOT page.wait_for_timeout: the latter is a CDP round-trip per call,
            # so it emits protocol traffic that bot-detectors (e.g. reCAPTCHA) can score — a
            # self-inflicted tell inside the humanize path. time.sleep is off-protocol.
            time.sleep(_rand(10, 38) / 1000.0)
            if random.random() < 0.07:
                time.sleep(_rand(40, 120) / 1000.0)   # occasional mid-scroll pause (reading)
            # A hand resting on the mouse does not hold it perfectly still through a long scroll,
            # so a burst of 20 wheel events sharing one identical clientX/clientY is a signature
            # only a script produces. A couple of px, so the scroll stays over the same element
            # (and never while a button is down, where a move is a drag leg).
            if steps >= 10 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = random.gauss(0, 1.5), random.gauss(0, 1.2)
                    native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass
        if px != delta_x or py != delta_y:
            native_wheel(delta_x - px, delta_y - py)  # exact total

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel
    # down/up are wrapped only to TRACK the held state hmove needs; the presses themselves stay
    # native and unchanged.
    mouse.down, mouse.up = hdown, hup

    # ----------------------------------------------------------------- keyboard
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press

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
            native_kb_press(ch, delay=key_dwell(persona))
        except Exception:  # noqa: BLE001
            try:
                native_kb_type(ch)
            except Exception:  # noqa: BLE001
                pass

    def _human_type_text(text):
        """Type text key-by-key with human timing. Each char goes through Playwright's native
        keyboard (trusted; shift/symbols handled by the engine), so this stays isTrusted=True."""
        n = len(text)
        for i, ch in enumerate(text):
            # occasional fat-finger on alnum chars, then notice + correct
            if ch.isascii() and ch.isalnum() and random.random() < 0.02:
                try:
                    _emit_key(_nearby_key(ch))
                    time.sleep(_rand(120, 300) / 1000.0)
                    native_kb_press("Backspace", delay=key_dwell(persona))
                    time.sleep(_rand(80, 200) / 1000.0)
                except Exception:  # noqa: BLE001
                    pass
            try:
                _emit_key(ch)
            except Exception:  # noqa: BLE001
                break
            if i < n - 1:
                # gaussian inter-key cadence (a realistic distribution, not a uniform band)
                d = max(0.025, random.gauss(0.085, 0.045))   # ~85ms +- 45ms, floored at 25ms
                if ch in " \t\n":
                    d += _rand(0.02, 0.10)                    # slight pause at word boundaries
                if random.random() < 0.06:
                    d += _rand(0.18, 0.45)                    # occasional thinking pause
                time.sleep(d)

    def hkb_type(text, **kw):
        _pointer_presence()
        _human_type_text(text)

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
        that. Only the DEFAULT changes, from "no hold at all" to "a human hold".
        """
        _pointer_presence()
        if "delay" not in kw:
            kw["delay"] = key_dwell(persona)
        return native_kb_press(key, **kw)

    kb.type = hkb_type
    kb.press = hkb_press

    # ------------------------------------------------------- page-level targeted helpers
    def _is_focused(selector):
        try:
            return bool(page.evaluate(
                "(s) => { const e = document.querySelector(s);"
                " return !!e && e === document.activeElement; }",
                selector))
        except Exception:  # noqa: BLE001
            return False

    def _point_for(selector, timeout):
        """Resolve a humane click point inside the element, after actionability waits."""
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout)
        loc.scroll_into_view_if_needed(timeout=timeout)
        if not loc.is_enabled():
            return None
        box = loc.bounding_box()
        if not box:
            return None
        # stability: a box that's still moving = an animation in flight -> let PW settle it
        time.sleep(0.05)
        box2 = loc.bounding_box()
        if not box2 or abs(box2["x"] - box["x"]) > 1 or abs(box2["y"] - box["y"]) > 1:
            return None
        box = box2
        return click_point(box, st["pos"], persona)  # 2D gaussian toward center, nudged to approach side

    def _focus_click(selector, timeout):
        pt = _point_for(selector, timeout)
        if not pt:
            return False
        _glide(pt[0], pt[1])
        time.sleep(_rand(40, 130) / 1000.0)
        native_click(pt[0], pt[1], delay=click_hold(persona))
        return True

    native_page_type = page.type
    native_page_fill = page.fill
    native_page_press = page.press
    native_page_dblclick = page.dblclick

    def htype(selector, text, **options):
        try:
            timeout = options.get("timeout", 30000)
            if not _is_focused(selector):
                if not _focus_click(selector, timeout):
                    return native_page_type(selector, text, **options)
                time.sleep(_rand(40, 120) / 1000.0)
            _human_type_text(text)
            return None
        except Exception:  # noqa: BLE001
            return native_page_type(selector, text, **options)

    def hfill(selector, value, **options):
        try:
            timeout = options.get("timeout", 30000)
            # bulk values: keep fill fast/atomic (humanizing 1000s of chars would crawl)
            if len(value) > 200:
                return native_page_fill(selector, value, **options)
            if not _focus_click(selector, timeout):
                return native_page_fill(selector, value, **options)
            time.sleep(_rand(40, 120) / 1000.0)
            try:
                native_kb_press(_SELECT_ALL)
                time.sleep(_rand(30, 80) / 1000.0)
                native_kb_press("Backspace")
                time.sleep(_rand(40, 120) / 1000.0)
            except Exception:  # noqa: BLE001
                pass
            _human_type_text(value)
            return None
        except Exception:  # noqa: BLE001
            return native_page_fill(selector, value, **options)

    def hdblclick(selector, **options):
        try:
            timeout = options.get("timeout", 30000)
            pt = _point_for(selector, timeout)
            if not pt:
                return native_page_dblclick(selector, **options)
            _glide(pt[0], pt[1])
            time.sleep(_rand(40, 130) / 1000.0)
            native_click(pt[0], pt[1], click_count=2, delay=_rand(40, 90))
            return None
        except Exception:  # noqa: BLE001
            return native_page_dblclick(selector, **options)

    def hpress(selector, key, **options):
        try:
            timeout = options.get("timeout", 30000)
            if not _is_focused(selector):
                if not _focus_click(selector, timeout):
                    return native_page_press(selector, key, **options)
                time.sleep(_rand(40, 120) / 1000.0)
            # The persona's hold, for the same reason hkb_press above carries one: this is the
            # path Locator.press delegates to, so without it every locator.press() in a script
            # emitted a zero-length keypress while the typed text around it looked human.
            native_kb_press(key, delay=key_dwell(persona))
            return None
        except Exception:  # noqa: BLE001
            return native_page_press(selector, key, **options)

    # getattr for the same reason as mouse.down/up above: a page-like object that does not
    # implement select_option must still get every OTHER humanization, not lose all of it to
    # an AttributeError raised while wiring this one up.
    native_page_select_option = getattr(page, "select_option", None)

    def hselect_option(selector, value=None, **options):
        """page.select_option, humanized the same way page.click/type/fill are.

        Only Locator.select_option used to be patched, so page.select_option(...) went straight to
        Playwright, which assigns the value and dispatches input+change FROM SCRIPT — isTrusted
        false. That is not hypothetical: the identical selection made through a locator passes the
        'interaction-select-change-trust' check on clearcotelabs.com/audit and this path fails it.

        Glide over the <select> first (a person looks at a control before choosing from it) but do
        NOT click it: an open native popup takes the arrow keys for itself, and the trusted route
        below depends on the closed-select stepping behaviour.
        """
        try:
            pt = _point_for(selector, options.get("timeout", 30000))
            if pt:
                _glide(pt[0], pt[1])
                time.sleep(_rand(60, 160) / 1000.0)
            picked = _select_by_keyboard(page, selector, value, options)
            if picked is not None:
                return picked
        except Exception:  # noqa: BLE001
            pass
        return native_page_select_option(selector, value, **options)

    page.type = htype
    page.fill = hfill
    page.dblclick = hdblclick
    page.press = hpress
    if native_page_select_option is not None:
        page.select_option = hselect_option

    def _make_targeted(orig, no_click):
        def _native(selector, options):
            """Fall back to native Playwright, and RECORD that the real cursor moved.

            Native click/hover genuinely move the browser pointer to the element. Leaving
            st["placed"] False afterwards made the next wheel anchor believe the pointer was
            still unplaced, so it glided away from the element the click had just left — a jump
            from a button to the middle of the page between clicking and scrolling, which is
            precisely the non-human motion this module exists to avoid.
            """
            try:
                return _native(selector, options)
            finally:
                st["placed"] = True

        def wrapped(selector, **options):
            # Actionability pre-flight before a TRUSTED humanized click: a native Playwright click
            # waits for visible+enabled+stable+receives-events; the humanized path dispatches a real
            # OS-level event at a point, so without these checks it could fire under a cookie
            # banner/overlay or mid-animation. Every check falls back to the native click (which has
            # its own actionability waits), so this only ever improves — it never regresses.
            try:
                timeout = options.get("timeout", 30000)
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.scroll_into_view_if_needed(timeout=timeout)
                if not loc.is_enabled():
                    return _native(selector, options)
                box = loc.bounding_box()
                if not box:
                    return _native(selector, options)
                # stability: a box that's still moving = an animation in flight -> let PW settle it
                time.sleep(0.05)
                box2 = loc.bounding_box()
                if not box2 or abs(box2["x"] - box["x"]) > 1 or abs(box2["y"] - box["y"]) > 1:
                    return _native(selector, options)
                box = box2
                x, y = click_point(box, st["pos"], persona)
                # covered-by: don't fire a trusted click at a point some overlay owns
                try:
                    handle = loc.element_handle()
                    if handle and page.evaluate(
                        "([x, y, el]) => { const t = document.elementFromPoint(x, y);"
                        " return !(t && (t === el || el.contains(t) || t.contains(el))); }",
                        [x, y, handle],
                    ):
                        return _native(selector, options)  # covered -> let PW wait for it on top
                except Exception:  # noqa: BLE001
                    pass
                _glide(x, y)
                if not no_click:
                    time.sleep(_rand(40, 130) / 1000.0)
                    native_click(x, y, delay=click_hold(persona))
                return None
            except Exception:  # noqa: BLE001
                return _native(selector, options)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


def _select_by_keyboard(page, selector, value, kw):
    """Choose a <select> option with the keyboard, so the ENGINE fires the events.

    Playwright's selectOption assigns the value and dispatches input+change from script. Those
    arrive with isTrusted false, which is the single most reliable dropdown tell there is -- the
    engine cannot produce an untrusted change, so a page that reads the flag knows the selection
    was not made by a person. Measured against this SDK before this existed: 1 of 1 change events
    untrusted.

    A <select> that has focus and is CLOSED steps through its options on ArrowUp/ArrowDown, and the
    browser emits input then change itself, trusted, exactly as it does for a mouse. So the fix is
    not to forge better events, it is to stop forging them.

    Returns the selected values (what select_option returns) or None whenever the keyboard route
    cannot be shown to have worked -- a value that is not a plain option, a multi-select, or a
    platform where arrows open the popup instead of stepping (macOS does this). The caller then
    falls back to native selectOption. Verifying selectedIndex afterwards rather than assuming is
    what makes that fallback safe: a silently wrong selection would be worse than an untrusted one.

    Module scope, not inside attach_humanize: it needs nothing from the per-page closure, and
    keeping the trusted route in exactly one function is what stops the page-level and
    locator-level entry points drifting apart again -- that drift is how page.select_option ended
    up untrusted while the identical locator call was not.
    """
    # `element=` points at an ElementHandle rather than naming an option, so there is nothing to
    # resolve by hand and native is correct.
    if kw.get("element"):
        return None
    one = lambda v: v[0] if isinstance(v, (list, tuple)) and len(v) == 1 else v
    by, wanted = None, None
    if kw.get("index") is not None and not isinstance(kw["index"], (list, tuple)):
        by, wanted = "index", int(kw["index"])
    elif kw.get("label") is not None and isinstance(one(kw["label"]), str):
        by, wanted = "label", one(kw["label"])
    elif isinstance(one(value), str):
        by, wanted = "value", one(value)
    if by is None:
        return None
    plan = page.evaluate(
        """(a) => { const s = document.querySelector(a.sel);
             if (!s || s.multiple || s.disabled) return null;
             const os = [...s.options];
             let i = -1;
             if (a.by === 'index') i = (a.want >= 0 && a.want < os.length) ? a.want : -1;
             else if (a.by === 'label') i = os.findIndex(o => (o.label || o.textContent || '').trim() === String(a.want).trim());
             else i = os.findIndex(o => o.value === a.want);
             if (i < 0 || os[i].disabled) return null;
             return { to: i, from: s.selectedIndex, ret: os[i].value }; }""",
        {"sel": selector, "by": by, "want": wanted},
    )
    if not plan:
        return None
    if plan["to"] == plan["from"]:
        return [plan["ret"]]          # already selected; nothing to do, nothing to forge
    page.focus(selector, **({"timeout": kw["timeout"]} if kw.get("timeout") is not None else {}))
    time.sleep(_rand(60, 160) / 1000.0)
    step = "ArrowDown" if plan["to"] > plan["from"] else "ArrowUp"
    for _ in range(abs(plan["to"] - plan["from"])):
        # page.keyboard.press, NOT a native handle: going through the page means the hold comes
        # from the humanised press wrapper rather than being reapplied here.
        page.keyboard.press(step)
        time.sleep(_rand(45, 120) / 1000.0)
    got = page.evaluate(
        "(s) => { const e = document.querySelector(s); return e ? e.selectedIndex : -1; }",
        selector,
    )
    return [plan["ret"]] if got == plan["to"] else None


# --------------------------------------------------------------------------- locator patch
_locator_patched = False


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

    def _on(self):
        # humanize active for this page?
        if not getattr(self.page, "_clearcote_humanized", False):
            return False
        # main-frame only: frame-locator selectors don't round-trip through page.* reliably,
        # so let those use native Playwright (which handles frames correctly).
        try:
            return self._impl_obj._frame.parent_frame is None
        except Exception:  # noqa: BLE001
            return False

    def _sel(self):
        return self._impl_obj._selector

    def _fwd(kw):
        out = {}
        if "timeout" in kw:
            out["timeout"] = kw["timeout"]
        return out

    def _fwd_select(kw):
        """select_option names its option with index/label/element, so unlike every other method
        here those have to travel with the timeout or the page-level call selects the wrong thing."""
        out = _fwd(kw)
        for k in ("index", "label", "element"):
            if kw.get(k) is not None:
                out[k] = kw[k]
        return out

    def fill(self, value, **kw):
        if _on(self):
            try:
                return self.page.fill(_sel(self), value, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_fill(self, value, **kw)

    def click(self, **kw):
        if _on(self):
            try:
                return self.page.click(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_click(self, **kw)

    def type_(self, text, **kw):
        if _on(self):
            try:
                return self.page.type(_sel(self), text, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_type(self, text, **kw)

    def dblclick(self, **kw):
        if _on(self):
            try:
                return self.page.dblclick(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_dblclick(self, **kw)

    def hover(self, **kw):
        if _on(self):
            try:
                return self.page.hover(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_hover(self, **kw)

    def press(self, key, **kw):
        if _on(self):
            try:
                return self.page.press(_sel(self), key, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_press(self, key, **kw)

    def press_sequentially(self, text, **kw):
        if _on(self):
            try:
                return self.page.type(_sel(self), text, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_press_seq(self, text, **kw)

    def clear(self, **kw):
        if _on(self):
            try:
                return self.page.fill(_sel(self), "", **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_clear(self, **kw)

    def tap(self, **kw):
        if _on(self):
            try:
                return self.page.click(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_tap(self, **kw)

    def check(self, **kw):
        if _on(self):
            try:
                if not self.is_checked():
                    return self.page.click(_sel(self), **_fwd(kw))
                return None
            except Exception:  # noqa: BLE001
                pass
        return o_check(self, **kw)

    def uncheck(self, **kw):
        if _on(self):
            try:
                if self.is_checked():
                    return self.page.click(_sel(self), **_fwd(kw))
                return None
            except Exception:  # noqa: BLE001
                pass
        return o_uncheck(self, **kw)

    def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here.

        The trusted keyboard route (_select_by_keyboard) used to live in this method only, which is
        why page.select_option(...) stayed untrusted; keeping one implementation behind the page
        method is what stops the two entry points drifting again — and it gains the locator path
        the glide to the <select> that it never had."""
        if _on(self):
            try:
                return self.page.select_option(_sel(self), value, **_fwd_select(kw))
            except Exception:  # noqa: BLE001
                pass
        return o_select_option(self, value, **kw)

    def drag_to(self, target, **kw):
        if _on(self):
            try:
                page = self.page
                sb = self.bounding_box()
                tb = target.bounding_box()
                if sb and tb:
                    sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
                    tx, ty = tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2
                    # Human endpoint dynamics (the two worst slider tells): grab hesitation AFTER
                    # pressing + a settle dwell BEFORE releasing, from the page's motor persona.
                    persona = getattr(page, "_clearcote_persona", None)
                    grab_ms, release_ms = drag_dwell(persona) if persona else (_rand(130, 360), _rand(90, 230))
                    page.mouse.move(sx, sy)
                    time.sleep(_rand(100, 200) / 1000.0)
                    page.mouse.down()                     # native -> button held across the glide
                    time.sleep(grab_ms / 1000.0)          # grab hesitation
                    held_glide = getattr(page, "_clearcote_held_glide", None)
                    if held_glide:
                        held_glide(tx, ty)                # humanized held-button drag + seating jiggle (settle)
                    else:
                        page.mouse.move(tx, ty)
                    time.sleep(release_ms / 1000.0)       # pre-release settle before letting go
                    page.mouse.up()
                    return None
            except Exception:  # noqa: BLE001
                pass
        return o_drag_to(self, target, **kw)

    Locator.fill = fill
    Locator.click = click
    Locator.type = type_
    Locator.dblclick = dblclick
    Locator.hover = hover
    Locator.press = press
    Locator.press_sequentially = press_sequentially
    Locator.clear = clear
    Locator.tap = tap
    Locator.check = check
    Locator.uncheck = uncheck
    Locator.drag_to = drag_to
    Locator.select_option = select_option


def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    """Install humanize/show_cursor on a single context (used for launch_persistent_context)."""
    if not humanize and not show_cursor:
        return
    b = browser or context.browser
    if show_cursor:
        try:
            context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if b is not None:
        context.on("page", lambda p: attach_humanize(b, p, humanize, show_cursor, seed))
        for p in context.pages:
            attach_humanize(b, p, humanize, show_cursor, seed)


def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    """Install humanize/show_cursor on a browser: wrap new_page/new_context so every page is covered.

    ``seed`` (the fingerprint seed) selects the stable per-identity motor persona."""
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    def new_page(**kw):
        page = orig_new_page(**kw)
        attach_humanize(browser, page, humanize, show_cursor, seed)
        return page

    def new_context(**kw):
        ctx = orig_new_context(**kw)
        install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
