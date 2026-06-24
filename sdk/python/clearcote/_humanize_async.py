"""Async mirror of _humanize.py for clearcote.async_api.

Same behavior as the sync version — eased/bowed cubic-bezier glides from the last cursor
position (no snap-back), native trusted input, optional red cursor overlay — but every Playwright
call is awaited and pacing uses asyncio.sleep. See _humanize.py for the rationale of each step.
"""

import asyncio
import math
import random

from ._humanize import CURSOR_OVERLAY, _rand


async def attach_humanize(browser, page, humanize=False, show_cursor=False):
    """Wrap one page's input methods + (optionally) inject the cursor overlay (async)."""
    if show_cursor:
        async def inject():
            try:
                await page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        await inject()
        page.on("load", lambda _=None: asyncio.ensure_future(inject()))
        page.on("framenavigated",
                lambda f: asyncio.ensure_future(inject()) if f == page.main_frame else None)
    if not humanize:
        return

    # Tracked cursor position so each move continues from where the last ended (seed off (0,0)).
    st = {"pos": (_rand(140, 380), _rand(90, 240))}
    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel

    async def _glide(x, y, jitter=0.6):
        x0, y0 = st["pos"]
        dx, dy = x - x0, y - y0
        dist = math.hypot(dx, dy)
        steps = int(max(10, min(38, dist / 14)))
        nx, ny = (-dy / dist, dx / dist) if dist > 1e-6 else (0.0, 0.0)
        bow = (random.random() * 0.22 - 0.11) * dist
        cp1 = (x0 + dx * 0.33 + nx * bow, y0 + dy * 0.33 + ny * bow)
        cp2 = (x0 + dx * 0.66 + nx * bow, y0 + dy * 0.66 + ny * bow)
        for i in range(1, steps + 1):
            t = i / steps
            e = t * t * (3 - 2 * t)
            mt = 1.0 - e
            bx = mt*mt*mt*x0 + 3*mt*mt*e*cp1[0] + 3*mt*e*e*cp2[0] + e*e*e*x
            by = mt*mt*mt*y0 + 3*mt*mt*e*cp1[1] + 3*mt*e*e*cp2[1] + e*e*e*y
            try:
                await native_move(bx + random.gauss(0, jitter), by + random.gauss(0, jitter))
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(_rand(7, 20) / 1000.0)
        try:
            await native_move(x, y)
        except Exception:  # noqa: BLE001
            pass
        st["pos"] = (x, y)

    async def hmove(x, y, **kw):
        await _glide(x, y)

    async def hclick(x, y, **kw):
        await _glide(x, y)
        await asyncio.sleep(_rand(40, 130) / 1000.0)
        try:
            await native_click(x, y)
        except Exception:  # noqa: BLE001
            pass

    async def hwheel(delta_x, delta_y):
        steps = max(5, min(20, round((abs(delta_x) + abs(delta_y)) / 80)))
        px = py = 0
        for i in range(1, steps + 1):
            t = i / steps
            f = t * t * (3 - 2 * t)
            nx, ny = round(delta_x * f), round(delta_y * f)
            await native_wheel(nx - px, ny - py)
            px, py = nx, ny
            await asyncio.sleep(_rand(12, 45) / 1000.0)
        if px != delta_x or py != delta_y:
            await native_wheel(delta_x - px, delta_y - py)

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel

    def _make_targeted(orig, no_click):
        async def wrapped(selector, **options):
            try:
                timeout = options.get("timeout", 30000)
                loc = page.locator(selector).first
                await loc.wait_for(state="visible", timeout=timeout)
                await loc.scroll_into_view_if_needed(timeout=timeout)
                if not await loc.is_enabled():
                    return await orig(selector, **options)
                box = await loc.bounding_box()
                if not box:
                    return await orig(selector, **options)
                await asyncio.sleep(0.05)
                box2 = await loc.bounding_box()
                if not box2 or abs(box2["x"] - box["x"]) > 1 or abs(box2["y"] - box["y"]) > 1:
                    return await orig(selector, **options)
                box = box2
                x = box["x"] + box["width"] * _rand(0.3, 0.7)
                y = box["y"] + box["height"] * _rand(0.3, 0.7)
                try:
                    handle = await loc.element_handle()
                    if handle and await page.evaluate(
                        "([x, y, el]) => { const t = document.elementFromPoint(x, y);"
                        " return !(t && (t === el || el.contains(t) || t.contains(el))); }",
                        [x, y, handle],
                    ):
                        return await orig(selector, **options)
                except Exception:  # noqa: BLE001
                    pass
                await _glide(x, y)
                if not no_click:
                    await asyncio.sleep(_rand(40, 130) / 1000.0)
                    await native_click(x, y)
                return None
            except Exception:  # noqa: BLE001
                return await orig(selector, **options)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)


async def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None):
    if not humanize and not show_cursor:
        return
    b = browser or context.browser
    if show_cursor:
        try:
            await context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if b is not None:
        context.on("page", lambda p: asyncio.ensure_future(attach_humanize(b, p, humanize, show_cursor)))
        for p in context.pages:
            await attach_humanize(b, p, humanize, show_cursor)


async def install_humanize(browser, humanize=False, show_cursor=False):
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    async def new_page(**kw):
        page = await orig_new_page(**kw)
        await attach_humanize(browser, page, humanize, show_cursor)
        return page

    async def new_context(**kw):
        ctx = await orig_new_context(**kw)
        await install_humanize_on_context(ctx, humanize, show_cursor, browser)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
