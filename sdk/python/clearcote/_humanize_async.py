"""Async mirror of _humanize.py for clearcote.async_api.

Same behavior as the sync version — eased/bowed cubic-bezier glides from the last cursor
position (no snap-back), native trusted input, optional red cursor overlay — but every Playwright
call is awaited and pacing uses asyncio.sleep. See _humanize.py for the rationale of each step.
"""

import asyncio
import random

from ._humanize import CURSOR_OVERLAY, _rand
from ._motion import make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient


async def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay (async).

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random."""
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

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is stable within a session and unlinkable across seeds. Stored on the page.
    persona = make_persona(seed)
    page._clearcote_persona = persona

    # Tracked cursor position so each move continues from where the last ended (seed off (0,0)).
    st = {"pos": (_rand(140, 380), _rand(90, 240))}
    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel

    async def _dispatch(steps):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol asyncio.sleep. Native input carries button state (held-button drag works)."""
        for sx, sy, sl in steps:
            try:
                await native_move(sx, sy)
            except Exception:  # noqa: BLE001
                break
            await asyncio.sleep(sl / 1000.0)

    async def _glide(x, y, settle=False, target_w=24):
        # minimum-jerk sum-of-submovements path with colored noise + Fitts duration (_motion.plan_move)
        await _dispatch(plan_move(st["pos"], (x, y), persona, target_w=target_w, settle=settle))
        st["pos"] = (x, y)

    # Engine real-trajectory routing (PRO): send point-to-point moves/clicks through
    # Browser.humanizedClick (real recorded human paths for PRO, engine bezier for free,
    # tiered by the engine license gate). Drags stay native (held button); fall back to
    # the SDK bezier if the method is unavailable.
    _eng = {"session": None, "tid": None, "ok": True}

    async def _engine_glide(x, y, no_click):
        if not _eng["ok"]:
            return False
        try:
            if _eng["session"] is None:
                tsession = await page.context.new_cdp_session(page)
                info = await tsession.send("Target.getTargetInfo")
                _eng["tid"] = info["targetInfo"]["targetId"]
                b = getattr(page.context, "browser", None)
                _eng["session"] = (await b.new_browser_cdp_session()
                                   if b is not None else tsession)
            dx, dy = x - st["pos"][0], y - st["pos"][1]
            dist = (dx * dx + dy * dy) ** 0.5
            dur = min(1.10, max(0.28, 0.30 + dist / 1700.0)) * (0.85 + 0.30 * random.random())
            await _eng["session"].send("Browser.humanizedClick", {
                "targetId": _eng["tid"], "x": float(x), "y": float(y),
                "duration": dur, "noClick": bool(no_click)})
            try:
                await native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            return True
        except Exception:  # noqa: BLE001
            _eng["ok"] = False
            return False

    async def hmove(x, y, **kw):
        if not await _engine_glide(x, y, no_click=True):
            await _glide(x, y)

    async def hclick(x, y, **kw):
        if await _engine_glide(x, y, no_click=False):
            return
        await _glide(x, y)
        await asyncio.sleep(_rand(40, 130) / 1000.0)
        try:
            await native_click(x, y, delay=click_hold(persona))  # mousedown->mouseup HOLD (~60-150ms)
        except Exception:  # noqa: BLE001
            pass

    async def _held_glide(x, y):
        await _glide(x, y, settle=True)              # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    async def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor entropy before the first goal action. await page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            steps = plan_ambient(st["pos"], {"width": vp["width"], "height": vp["height"]}, persona, ms)
            await _dispatch(steps)
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # DEFAULT under humanize (no longer opt-in): fire a short ambient burst on every load.
    page._clearcote_auto_ambient = True

    async def _auto_ambient():
        if getattr(page, "_clearcote_auto_ambient", False):
            try:
                await hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: asyncio.ensure_future(_auto_ambient()))

    async def hwheel(delta_x, delta_y):
        steps = max(5, min(24, round((abs(delta_x) + abs(delta_y)) / 60)))
        px = py = 0
        for i in range(1, steps + 1):
            t = i / steps
            f = 1 - (1 - t) ** 2.2            # ease-OUT inertia (fast flick -> slow settle)
            nx, ny = round(delta_x * f), round(delta_y * f)
            await native_wheel(nx - px, ny - py)
            px, py = nx, ny
            await asyncio.sleep(_rand(10, 38) / 1000.0)
            if random.random() < 0.07:
                await asyncio.sleep(_rand(40, 120) / 1000.0)
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
                x, y = click_point(box, st["pos"], persona)
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
                    await native_click(x, y, delay=click_hold(persona))
                return None
            except Exception:  # noqa: BLE001
                return await orig(selector, **options)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)

    # ---- keystroke dynamics (async; mirrors _humanize.py) ----
    kb = page.keyboard
    native_kb_type = kb.type
    native_fill, native_type = page.fill, page.type

    async def _emit_key(ch):
        """One character with a human keydown->keyup DWELL (keyboard.press's delay = the hold)."""
        try:
            await kb.press(ch, delay=key_dwell(persona))
        except Exception:  # noqa: BLE001
            try:
                await native_kb_type(ch)
            except Exception:  # noqa: BLE001
                pass

    async def _type_humanized(text):
        text = str(text)
        n = len(text)
        for i, ch in enumerate(text):
            try:
                await _emit_key(ch)
            except Exception:  # noqa: BLE001
                break
            if i < n - 1:
                d = max(0.025, random.gauss(0.085, 0.045))
                if ch in " \t\n":
                    d += _rand(0.02, 0.10)
                if random.random() < 0.05:
                    d += _rand(0.18, 0.5)
                await asyncio.sleep(d)

    async def _focus(selector, timeout):
        loc = page.locator(selector).first
        await loc.wait_for(state="visible", timeout=timeout)
        await loc.scroll_into_view_if_needed(timeout=timeout)
        await loc.click(timeout=timeout)
        return loc

    async def hfill(selector, value, **kw):
        # bulk values: keep fill fast/atomic (humanizing 1000s of chars would crawl)
        if len(str(value)) > 200:
            return await native_fill(selector, value, **kw)
        try:
            timeout = kw.get("timeout", 30000)
            await _focus(selector, timeout)
            await kb.press("ControlOrMeta+a")
            await kb.press("Delete")
            await _type_humanized(value)
            return None
        except Exception:  # noqa: BLE001
            return await native_fill(selector, value, **kw)

    async def htype(selector, text, **kw):
        try:
            await _focus(selector, kw.get("timeout", 30000))
            await _type_humanized(text)
            return None
        except Exception:  # noqa: BLE001
            return await native_type(selector, text, **kw)

    async def hkbtype(text, **kw):
        await _type_humanized(text)

    page.fill, page.type, kb.type = hfill, htype, hkbtype

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


# --------------------------------------------------------------------------- locator patch
_async_locator_patched = False


def _patch_locator_class():
    """Patch the async Locator class once so locator.* interactions route through the humanized
    page.* methods. Each method is a no-op unless the locator's page has humanize active AND the
    locator targets the main frame; anything else (frame locators, exotic selectors, errors)
    falls straight through to the original Playwright behaviour. Composes safely if another
    library has already patched Locator — we chain to whatever was there before."""
    global _async_locator_patched
    if _async_locator_patched:
        return
    _async_locator_patched = True
    try:
        from playwright.async_api._generated import Locator
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

    async def fill(self, value, **kw):
        if _on(self):
            try:
                return await self.page.fill(_sel(self), value, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_fill(self, value, **kw)

    async def click(self, **kw):
        if _on(self):
            try:
                return await self.page.click(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_click(self, **kw)

    async def type_(self, text, **kw):
        if _on(self):
            try:
                return await self.page.type(_sel(self), text, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_type(self, text, **kw)

    async def dblclick(self, **kw):
        if _on(self):
            try:
                return await self.page.dblclick(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_dblclick(self, **kw)

    async def hover(self, **kw):
        if _on(self):
            try:
                return await self.page.hover(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_hover(self, **kw)

    async def press(self, key, **kw):
        if _on(self):
            try:
                return await self.page.press(_sel(self), key, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_press(self, key, **kw)

    async def press_sequentially(self, text, **kw):
        if _on(self):
            try:
                return await self.page.type(_sel(self), text, **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_press_seq(self, text, **kw)

    async def clear(self, **kw):
        if _on(self):
            try:
                return await self.page.fill(_sel(self), "", **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_clear(self, **kw)

    async def tap(self, **kw):
        if _on(self):
            try:
                return await self.page.click(_sel(self), **_fwd(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_tap(self, **kw)

    async def check(self, **kw):
        if _on(self):
            try:
                if not await self.is_checked():
                    return await self.page.click(_sel(self), **_fwd(kw))
                return None
            except Exception:  # noqa: BLE001
                pass
        return await o_check(self, **kw)

    async def uncheck(self, **kw):
        if _on(self):
            try:
                if await self.is_checked():
                    return await self.page.click(_sel(self), **_fwd(kw))
                return None
            except Exception:  # noqa: BLE001
                pass
        return await o_uncheck(self, **kw)

    async def drag_to(self, target, **kw):
        if _on(self):
            try:
                page = self.page
                sb = await self.bounding_box()
                tb = await target.bounding_box()
                if sb and tb:
                    sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
                    tx, ty = tb["x"] + tb["width"] / 2, tb["y"] + tb["height"] / 2
                    # Human endpoint dynamics (the two worst slider tells): grab hesitation AFTER
                    # pressing + a settle dwell BEFORE releasing, from the page's motor persona.
                    persona = getattr(page, "_clearcote_persona", None)
                    grab_ms, release_ms = drag_dwell(persona) if persona else (_rand(130, 360), _rand(90, 230))
                    await page.mouse.move(sx, sy)
                    await asyncio.sleep(_rand(100, 200) / 1000.0)
                    await page.mouse.down()                     # native -> button held across the glide
                    await asyncio.sleep(grab_ms / 1000.0)       # grab hesitation
                    held_glide = getattr(page, "_clearcote_held_glide", None)
                    if held_glide:
                        await held_glide(tx, ty)                # humanized held-button drag + seating jiggle
                    else:
                        await page.mouse.move(tx, ty)
                    await asyncio.sleep(release_ms / 1000.0)     # pre-release settle before letting go
                    await page.mouse.up()
                    return None
            except Exception:  # noqa: BLE001
                pass
        return await o_drag_to(self, target, **kw)

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


async def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    if not humanize and not show_cursor:
        return
    b = browser or context.browser
    if show_cursor:
        try:
            await context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if b is not None:
        context.on("page", lambda p: asyncio.ensure_future(attach_humanize(b, p, humanize, show_cursor, seed)))
        for p in context.pages:
            await attach_humanize(b, p, humanize, show_cursor, seed)


async def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    async def new_page(**kw):
        page = await orig_new_page(**kw)
        await attach_humanize(browser, page, humanize, show_cursor, seed)
        return page

    async def new_context(**kw):
        ctx = await orig_new_context(**kw)
        await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
