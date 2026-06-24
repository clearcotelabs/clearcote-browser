"""Humanized input + cursor visualization (Python).

``humanize=True``   — routes page.click/hover/mouse.move/mouse.click along an eased, slightly
  bowed cubic-bezier path that starts from the *last* cursor position (continuous — no snap back
  to the top-left corner between moves), dispatched as native trusted input (isTrusted=True,
  navigator.webdriver stays False); eases mouse.wheel SDK-side.
``show_cursor=True`` — injects a red cursor dot that follows the real mousemove events.

Note: the engine's ``Browser.humanizedClick`` CDP command always starts its path from the
document origin (0,0), so chained moves visibly flick back to the corner. We therefore build the
path SDK-side from the tracked cursor position instead, which needs no special binary.
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


def _rand(a, b):
    return a + random.random() * (b - a)


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

    def _glide(x, y, jitter=0.6):
        """Move from the tracked position to (x, y) along an eased, slightly bowed cubic-bezier
        path, emitting native (trusted) mousemove events the whole way. No snap-back."""
        x0, y0 = st["pos"]
        dx, dy = x - x0, y - y0
        dist = math.hypot(dx, dy)
        steps = int(max(10, min(38, dist / 14)))
        # unit perpendicular, to bow the path off the straight line
        nx, ny = (-dy / dist, dx / dist) if dist > 1e-6 else (0.0, 0.0)
        bow = (random.random() * 0.22 - 0.11) * dist
        cp1 = (x0 + dx * 0.33 + nx * bow, y0 + dy * 0.33 + ny * bow)
        cp2 = (x0 + dx * 0.66 + nx * bow, y0 + dy * 0.66 + ny * bow)
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)           # smoothstep: slow out of start, slow into target
            mt = 1.0 - e
            bx = mt*mt*mt*x0 + 3*mt*mt*e*cp1[0] + 3*mt*e*e*cp2[0] + e*e*e*x
            by = mt*mt*mt*y0 + 3*mt*mt*e*cp1[1] + 3*mt*e*e*cp2[1] + e*e*e*y
            try:
                native_move(bx + random.gauss(0, jitter), by + random.gauss(0, jitter))
            except Exception:  # noqa: BLE001
                break
            time.sleep(_rand(7, 20) / 1000.0)  # off-protocol pacing, ~60fps-ish
        try:
            native_move(x, y)                  # exact landing
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
        steps = max(5, min(20, round((abs(delta_x) + abs(delta_y)) / 80)))
        px = py = 0
        for i in range(1, steps + 1):
            t = i / steps
            f = t * t * (3 - 2 * t)
            nx, ny = round(delta_x * f), round(delta_y * f)
            native_wheel(nx - px, ny - py)
            px, py = nx, ny
            # local sleep, NOT page.wait_for_timeout: the latter is a CDP round-trip per call,
            # so it emits protocol traffic that bot-detectors (e.g. reCAPTCHA) can score — a
            # self-inflicted tell inside the humanize path. time.sleep is off-protocol.
            time.sleep(_rand(12, 45) / 1000.0)
        if px != delta_x or py != delta_y:
            native_wheel(delta_x - px, delta_y - py)

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel

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
