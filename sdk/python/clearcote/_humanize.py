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

import math
import random
import time

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


def attach_humanize(browser, page, humanize=False, show_cursor=False):
    """Wrap one page's input methods + (optionally) inject the cursor overlay."""
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

    # Tracked cursor position so each move continues from where the last one ended.
    # Seed at a plausible on-page spot (not 0,0) so the first glide doesn't start in the corner.
    st = {"pos": (_rand(140, 380), _rand(90, 240))}

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel

    def _submove(x0, y0, x1, y1, jitter):
        """One ballistic sub-movement to (x1,y1): a slightly bowed bezier walked with a MIN-JERK
        velocity profile (the human reach profile: fast rise, slow settle). Native (trusted) moves
        the whole way, so a button held via mouse.down() stays pressed across it (drag). Returns
        False if a native move raised (page gone)."""
        dx, dy = x1 - x0, y1 - y0
        dist = math.hypot(dx, dy)
        steps = int(max(6, min(30, dist / 16)))
        # unit perpendicular, to bow the path off the straight line
        nx, ny = (-dy / dist, dx / dist) if dist > 1e-6 else (0.0, 0.0)
        bow = (random.random() * 0.18 - 0.09) * dist
        cp1 = (x0 + dx * 0.33 + nx * bow, y0 + dy * 0.33 + ny * bow)
        cp2 = (x0 + dx * 0.66 + nx * bow, y0 + dy * 0.66 + ny * bow)
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * t * (10 - 15 * t + 6 * t * t)   # min-jerk easing (not symmetric smoothstep)
            mt = 1.0 - e
            bx = mt*mt*mt*x0 + 3*mt*mt*e*cp1[0] + 3*mt*e*e*cp2[0] + e*e*e*x1
            by = mt*mt*mt*y0 + 3*mt*mt*e*cp1[1] + 3*mt*e*e*cp2[1] + e*e*e*y1
            try:
                native_move(bx + random.gauss(0, jitter), by + random.gauss(0, jitter))
            except Exception:  # noqa: BLE001
                return False
            time.sleep(_rand(7, 18) / 1000.0)  # off-protocol pacing, ~60fps-ish
        return True

    def _glide(x, y, jitter=0.6):
        """Move to (x, y) as a SUM OF SUB-MOVEMENTS — a ballistic primary that slightly over/under-
        shoots, then a corrective sub-movement onto the target — so the velocity profile is
        multi-peak (real neuromotor motion) instead of one symmetric bell. Continuous (starts at the
        last tracked position, lands exactly on target). Native input the whole way, so a button held
        via mouse.down() stays pressed across the glide — the same path powers a free move and a
        held-button drag."""
        x0, y0 = st["pos"]
        dist = math.hypot(x - x0, y - y0)
        if dist > 60:
            frac = _rand(0.82, 0.96)                       # primary covers ~82-96% of the distance
            spread = min(14.0, dist * 0.05)                # with a small over/undershoot
            ox = x0 + (x - x0) * frac + random.gauss(0, spread)
            oy = y0 + (y - y0) * frac + random.gauss(0, spread)
            if _submove(x0, y0, ox, oy, jitter):
                time.sleep(_rand(12, 40) / 1000.0)         # inter-submovement gap (motor re-planning)
                _submove(ox, oy, x, y, jitter * 0.6)       # corrective sub-movement
        else:
            _submove(x0, y0, x, y, jitter)
        try:
            native_move(x, y)                              # exact landing (no jitter)
        except Exception:  # noqa: BLE001
            pass
        st["pos"] = (x, y)

    def hmove(x, y, **kw):
        _glide(x, y)

    def hclick(x, y, **kw):
        _glide(x, y)
        time.sleep(_rand(40, 130) / 1000.0)    # brief dwell before pressing, like a human
        try:
            native_click(x, y)
        except Exception:  # noqa: BLE001
            pass

    def hwheel(delta_x, delta_y):
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
        if px != delta_x or py != delta_y:
            native_wheel(delta_x - px, delta_y - py)  # exact total

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel

    # ----------------------------------------------------------------- keyboard
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press

    def _human_type_text(text):
        """Type text key-by-key with human timing. Each char goes through Playwright's native
        keyboard (trusted; shift/symbols handled by the engine), so this stays isTrusted=True."""
        n = len(text)
        for i, ch in enumerate(text):
            # occasional fat-finger on alnum chars, then notice + correct
            if ch.isascii() and ch.isalnum() and random.random() < 0.02:
                try:
                    native_kb_type(_nearby_key(ch))
                    time.sleep(_rand(120, 300) / 1000.0)
                    native_kb_press("Backspace")
                    time.sleep(_rand(80, 200) / 1000.0)
                except Exception:  # noqa: BLE001
                    pass
            try:
                native_kb_type(ch)
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
        _human_type_text(text)

    kb.type = hkb_type

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
        return (box["x"] + box["width"] * _rand(0.3, 0.7),
                box["y"] + box["height"] * _rand(0.3, 0.7))

    def _focus_click(selector, timeout):
        pt = _point_for(selector, timeout)
        if not pt:
            return False
        _glide(pt[0], pt[1])
        time.sleep(_rand(40, 130) / 1000.0)
        native_click(pt[0], pt[1])
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
            native_kb_press(key)
            return None
        except Exception:  # noqa: BLE001
            return native_page_press(selector, key, **options)

    page.type = htype
    page.fill = hfill
    page.dblclick = hdblclick
    page.press = hpress

    def _make_targeted(orig, no_click):
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
                    return orig(selector, **options)
                box = loc.bounding_box()
                if not box:
                    return orig(selector, **options)
                # stability: a box that's still moving = an animation in flight -> let PW settle it
                time.sleep(0.05)
                box2 = loc.bounding_box()
                if not box2 or abs(box2["x"] - box["x"]) > 1 or abs(box2["y"] - box["y"]) > 1:
                    return orig(selector, **options)
                box = box2
                x = box["x"] + box["width"] * _rand(0.3, 0.7)
                y = box["y"] + box["height"] * _rand(0.3, 0.7)
                # covered-by: don't fire a trusted click at a point some overlay owns
                try:
                    handle = loc.element_handle()
                    if handle and page.evaluate(
                        "([x, y, el]) => { const t = document.elementFromPoint(x, y);"
                        " return !(t && (t === el || el.contains(t) || t.contains(el))); }",
                        [x, y, handle],
                    ):
                        return orig(selector, **options)  # covered -> let PW wait for it on top
                except Exception:  # noqa: BLE001
                    pass
                _glide(x, y)
                if not no_click:
                    time.sleep(_rand(40, 130) / 1000.0)
                    native_click(x, y)
                return None
            except Exception:  # noqa: BLE001
                return orig(selector, **options)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


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

    def drag_to(self, target, **kw):
        if _on(self):
            try:
                page = self.page
                sb = self.bounding_box()
                tb = target.bounding_box()
                if sb and tb:
                    sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
                    tx, ty = tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2
                    page.mouse.move(sx, sy)
                    time.sleep(_rand(100, 200) / 1000.0)
                    page.mouse.down()                     # native -> button held across the glide
                    time.sleep(_rand(80, 150) / 1000.0)
                    page.mouse.move(tx, ty)               # humanized, held-button drag
                    time.sleep(_rand(80, 150) / 1000.0)
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


def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None):
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
        context.on("page", lambda p: attach_humanize(b, p, humanize, show_cursor))
        for p in context.pages:
            attach_humanize(b, p, humanize, show_cursor)


def install_humanize(browser, humanize=False, show_cursor=False):
    """Install humanize/show_cursor on a browser: wrap new_page/new_context so every page is covered."""
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    def new_page(**kw):
        page = orig_new_page(**kw)
        attach_humanize(browser, page, humanize, show_cursor)
        return page

    def new_context(**kw):
        ctx = orig_new_context(**kw)
        install_humanize_on_context(ctx, humanize, show_cursor, browser)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
