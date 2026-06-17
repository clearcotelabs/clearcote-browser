"""Humanized input + cursor visualization (Python).

``humanize=True``   — routes page.click/hover/mouse.move/mouse.click through the engine's
  ``Browser.humanizedClick`` CDP command (real trusted WebMouseEvent along a cubic-bezier path,
  isTrusted=True, navigator.webdriver stays False); eases mouse.wheel SDK-side.
``show_cursor=True`` — injects a red cursor dot that follows the real mousemove events.

Requires a Clearcote binary exposing Browser.humanizedClick. On an older binary the command is
absent and humanize falls back to native Playwright input.
"""

import random

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


def _page_target_id(page):
    s = page.context.new_cdp_session(page)
    return s.send("Target.getTargetInfo")["targetInfo"]["targetId"]


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

    st = {"b": None, "tid": None, "ok": True}

    def humanized_move(x, y, no_click, duration=None):
        if not st["ok"]:
            return False
        try:
            if st["b"] is None:
                st["b"] = browser.new_browser_cdp_session()
            if st["tid"] is None:
                st["tid"] = _page_target_id(page)
            st["b"].send("Browser.humanizedClick", {
                "targetId": st["tid"], "x": round(x), "y": round(y),
                "duration": duration if duration is not None else _rand(0.45, 0.95),
                "noClick": no_click,
            })
            return True
        except Exception:  # noqa: BLE001 -- older binary lacks the command
            st["ok"] = False
            return False

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel

    def hmove(x, y, **kw):
        if not humanized_move(x, y, True):
            native_move(x, y, **kw)

    def hclick(x, y, **kw):
        if not humanized_move(x, y, False):
            native_click(x, y, **kw)

    def hwheel(delta_x, delta_y):
        steps = max(5, min(20, round((abs(delta_x) + abs(delta_y)) / 80)))
        px = py = 0
        for i in range(1, steps + 1):
            t = i / steps
            f = t * t * (3 - 2 * t)
            nx, ny = round(delta_x * f), round(delta_y * f)
            native_wheel(nx - px, ny - py)
            px, py = nx, ny
            page.wait_for_timeout(_rand(12, 45))
        if px != delta_x or py != delta_y:
            native_wheel(delta_x - px, delta_y - py)

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel

    def _make_targeted(orig, no_click):
        def wrapped(selector, **options):
            try:
                loc = page.locator(selector).first
                loc.scroll_into_view_if_needed(timeout=options.get("timeout", 30000))
                box = loc.bounding_box()
                if not box:
                    return orig(selector, **options)
                x = box["x"] + box["width"] * _rand(0.3, 0.7)
                y = box["y"] + box["height"] * _rand(0.3, 0.7)
                if humanized_move(x, y, no_click):
                    return None
                return orig(selector, **options)
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
