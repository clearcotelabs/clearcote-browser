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
    # "placed" is whether the REAL pointer has moved on this page yet: until it has, the browser's
    # cursor sits at the (0,0) origin whatever pos says. See _wheel_anchor / hdown /
    # _pointer_presence, and _humanize.py for the reasoning.
    st = {"pos": (_rand(140, 380), _rand(90, 240)), "held": False,
          "placed": False, "kb_primed": False}
    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel
    # getattr, not attribute access: attach must not raise on a page-like object that lacks a
    # method we only optionally wrap. Reading mouse.down/up directly widened the attach-time
    # contract and took four previously-green async tests down with an AttributeError before
    # anything was even wrapped — humanize failing closed on attach is the one outcome this
    # module must never produce, since it silently disables every other humanization too.
    native_down = getattr(mouse, "down", None)
    native_up = getattr(mouse, "up", None)

    async def _dispatch(steps):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol asyncio.sleep. Native input carries button state (held-button drag works)."""
        for sx, sy, sl in steps:
            try:
                await native_move(sx, sy)
            except Exception:  # noqa: BLE001
                break
            st["placed"] = True
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
            # The engine walks the trajectory in the BROWSER process and answers before it has
            # finished. Without waiting, a following mouse.down() fires at the cursor's stale
            # position -- measured on the sync path: a move to (110,58) then a press landed at
            # (981,629), on <body> instead of the target, breaking every move-then-press
            # sequence (drag, press-and-hold, drag-and-drop). See _humanize.py.
            await asyncio.sleep(dur)
            try:
                await native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            return True
        except Exception:  # noqa: BLE001
            _eng["ok"] = False
            return False

    async def hmove(x, y, **kw):
        # A BARE MOVE ALWAYS USES THE NATIVE PATH -- never the engine trajectory. Two independent
        # reasons, both measured on the sync file (see _humanize.py):
        #   * a held button cannot be carried across the engine's own WebMouseEvents, so a drag
        #     leg routed through it reports buttons 0 mid-press and a range thumb ignores it;
        #   * the engine walks its path ASYNCHRONOUSLY and answers before finishing, so it keeps
        #     moving the cursor after hdown's pre-press pin -- a move to (56,120) then down()
        #     pressed on HTML at (856,213) instead of the INPUT, and the slider stayed at 0.
        # hclick keeps engine routing: it does move+click entirely engine-side, where nothing
        # races the trajectory. Kept identical to the sync file and to Node.
        await _glide(x, y)

    async def hdown(**kw):
        # DELIBERATELY NO PLACEMENT WHEN THE POINTER HAS NEVER MOVED.
        #
        # An earlier revision ran an ambient walk here and pressed wherever it ended. But st["pos"]
        # before any real move is the RANDOM SEED assigned at attach (_rand(140,380), _rand(90,240)) —
        # not a coordinate the caller ever named — so that turned a harmless no-op press into a
        # trusted click at an arbitrary page position, which can land on a link. A bare down() with
        # no preceding move is a caller mistake; inventing a target makes it a worse one. Callers
        # who want a humanized drag move first, and that move glides and sets "placed".
        if st["placed"]:
            # Pin the cursor to the tracked target before pressing: the engine answers
            # humanizedClick before its trajectory has finished and overruns the duration it
            # reports, so the wait is not sufficient on its own. Only meaningful once a real move
            # has happened — that is what makes st["pos"] correspond to the actual cursor.
            try:
                await native_move(*st["pos"])
            except Exception:  # noqa: BLE001 - best effort: keep the press on target
                pass
        # try/finally so a raising press cannot strand held=True and make every later move a
        # phantom drag leg (hup already had this; hdown did not).
        try:
            st["held"] = True
            await native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    async def hup(**kw):
        try:
            await native_up(**kw)
        finally:
            st["held"] = False

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

    async def _viewport():
        """The REAL viewport, in CSS px.

        Not page.viewport_size: launch()/launch_persistent_context() force viewport=None on every
        headed page (so innerWidth tracks the real OS window rather than Playwright's emulated
        1280x720), and with viewport=None that property stays None for the life of the page. The
        old fallback therefore hit the 1280x800 constant on EVERY headed launch, so the anchor
        gate judged "is the pointer inside the viewport" against a box unrelated to the window and
        re-homed a correctly-placed cursor on every scroll.
        """
        try:
            wh = await page.evaluate("() => [innerWidth, innerHeight]")
            if wh and wh[0] and wh[1]:
                return float(wh[0]), float(wh[1])
        except Exception:  # noqa: BLE001
            pass
        vp = page.viewport_size or {"width": 1280, "height": 800}
        return float(vp["width"]), float(vp["height"])

    async def _wheel_anchor():
        """Put the cursor over the content before scrolling it.

        A wheel event is delivered wherever the pointer currently is, and until something has moved
        it that is the (0,0) origin — so the first scroll of a freshly loaded page arrives in the
        top-left corner, which is not where a hand ever scrolls. Only re-home when the pointer is
        nowhere sensible: a person does not move the mouse between two scrolls of the same page, so
        re-anchoring on every wheel call would be its own tell.
        """
        try:
            vw, vh = await _viewport()
            x, y = st["pos"]
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if st["held"] or (st["placed"] and 8 <= x <= vw - 8 and 8 <= y <= vh - 8):
                return
            # Reading position: upper-middle, gaussian-spread. The exact centre is itself a tell.
            ax = min(max(vw * 0.5 + random.gauss(0, vw * 0.10), vw * 0.12), vw * 0.88)
            ay = min(max(vh * 0.34 + random.gauss(0, vh * 0.09), vh * 0.10), vh * 0.62)
            await _glide(ax, ay, target_w=140)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    async def hwheel(delta_x, delta_y):
        await _wheel_anchor()
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
            # A hand resting on the mouse does not hold it perfectly still through a long scroll, so
            # 20 wheel events sharing one identical clientX/clientY is a script signature. A couple
            # of px, so the scroll stays over the same element (and never while a button is down,
            # where a move is a drag leg).
            if steps >= 10 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = random.gauss(0, 1.5), random.gauss(0, 1.2)
                    await native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass
        if px != delta_x or py != delta_y:
            await native_wheel(delta_x - px, delta_y - py)

    mouse.move, mouse.click, mouse.wheel = hmove, hclick, hwheel
    # down/up wrapped only to TRACK held state for hmove; the presses stay native.
    mouse.down, mouse.up = hdown, hup

    def _make_targeted(orig, no_click):
        async def _native(selector, options):
            """Fall back to native Playwright, and RECORD that the real cursor moved. See the
            sync file: leaving placed False here makes the next wheel anchor jump the cursor away
            from the element the fallback click just left."""
            try:
                return await _native(selector, options)
            finally:
                st["placed"] = True

        async def wrapped(selector, **options):
            try:
                timeout = options.get("timeout", 30000)
                loc = page.locator(selector).first
                await loc.wait_for(state="visible", timeout=timeout)
                await loc.scroll_into_view_if_needed(timeout=timeout)
                if not await loc.is_enabled():
                    return await _native(selector, options)
                box = await loc.bounding_box()
                if not box:
                    return await _native(selector, options)
                await asyncio.sleep(0.05)
                box2 = await loc.bounding_box()
                if not box2 or abs(box2["x"] - box["x"]) > 1 or abs(box2["y"] - box["y"]) > 1:
                    return await _native(selector, options)
                box = box2
                x, y = click_point(box, st["pos"], persona)
                try:
                    handle = await loc.element_handle()
                    if handle and await page.evaluate(
                        "([x, y, el]) => { const t = document.elementFromPoint(x, y);"
                        " return !(t && (t === el || el.contains(t) || t.contains(el))); }",
                        [x, y, handle],
                    ):
                        return await _native(selector, options)
                except Exception:  # noqa: BLE001
                    pass
                await _glide(x, y)
                if not no_click:
                    await asyncio.sleep(_rand(40, 130) / 1000.0)
                    await native_click(x, y, delay=click_hold(persona))
                return None
            except Exception:  # noqa: BLE001
                return await _native(selector, options)
        return wrapped

    page.click = _make_targeted(page.click, False)
    page.hover = _make_targeted(page.hover, True)

    # ---- keystroke dynamics (async; mirrors _humanize.py) ----
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press
    native_fill, native_type = page.fill, page.type
    # getattr for the same reason as mouse.down/up above: a page-like object that does not
    # implement select_option must still get every OTHER humanization, not lose all of it to
    # an AttributeError raised while wiring this one up.
    native_select_option = getattr(page, "select_option", None)

    async def _pointer_presence():
        """Give a keyboard-only session a mouse, once, before its first keystroke.

        Selector-driven typing already glides (page.type/fill go through _focus), but a bare
        page.keyboard.type/press types into whatever is focused — from a session in which the
        pointer never existed, i.e. keydowns with zero preceding pointer events.

        AMBIENT ONLY, deliberately not a move toward the focused field: the caller has already put
        focus somewhere (often from script), and gliding onto a control could hover or re-target it
        and land the text in the wrong place. plan_ambient never clicks, so focus cannot change.
        """
        if st["placed"] or st["kb_primed"]:
            return
        st["kb_primed"] = True   # one attempt per page, even if the ambient burst itself fails
        try:
            await hambient(_rand(280, 620))
        except Exception:  # noqa: BLE001
            pass

    async def _emit_key(ch):
        """One character with a human keydown->keyup DWELL (keyboard.press's delay = the hold)."""
        try:
            await native_kb_press(ch, delay=key_dwell(persona))
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
        await _pointer_presence()
        await _type_humanized(text)

    async def hkbpress(key, **kw):
        """keyboard.press with the persona's hold unless the caller specified their own — the sync
        wrapper's rationale applies unchanged (a press that holds ~2ms undoes the whole keyboard
        persona; press(key, delay=...) is the documented way to hold a key, so the caller wins)."""
        await _pointer_presence()
        if "delay" not in kw:
            kw["delay"] = key_dwell(persona)
        return await native_kb_press(key, **kw)

    async def hselect_option(selector, value=None, **options):
        """page.select_option, humanized the same way page.click/type/fill are.

        Playwright's selectOption assigns the value and dispatches input+change FROM SCRIPT —
        isTrusted false, which is the whole dropdown tell and is what fails the
        'interaction-select-change-trust' check on clearcotelabs.com/audit.

        Glide over the <select> first (a person looks at a control before choosing from it) but do
        NOT click it: an open native popup takes the arrow keys for itself, and the trusted route
        depends on the closed-select stepping behaviour.
        """
        try:
            timeout = options.get("timeout", 30000)
            loc = page.locator(selector).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.scroll_into_view_if_needed(timeout=timeout)
            box = await loc.bounding_box()
            if box:
                x, y = click_point(box, st["pos"], persona)
                await _glide(x, y)
                await asyncio.sleep(_rand(60, 160) / 1000.0)
            picked = await _select_by_keyboard(page, selector, value, options)
            if picked is not None:
                return picked
        except Exception:  # noqa: BLE001
            pass
        return await native_select_option(selector, value, **options)

    page.fill, page.type, kb.type = hfill, htype, hkbtype
    kb.press = hkbpress
    if native_select_option is not None:
        page.select_option = hselect_option

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


async def _select_by_keyboard(page, selector, value, kw):
    """Choose a <select> option with the keyboard, so the ENGINE fires the events (async mirror of
    the sync helper — see _humanize.py for the measurement and the fallback rationale).

    Returns the selected values (what select_option returns) or None whenever the keyboard route
    cannot be shown to have worked; the caller then falls back to native selectOption. Verifying
    selectedIndex afterwards rather than assuming is what makes that fallback safe.

    Module scope, not inside attach_humanize: it needs nothing from the per-page closure, and
    keeping the trusted route in exactly one function is what stops the page-level and
    locator-level entry points drifting apart again.
    """
    # `element=` points at an ElementHandle rather than naming an option: nothing to resolve here.
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
    plan = await page.evaluate(
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
    await page.focus(selector, **({"timeout": kw["timeout"]} if kw.get("timeout") is not None else {}))
    await asyncio.sleep(_rand(60, 160) / 1000.0)
    step = "ArrowDown" if plan["to"] > plan["from"] else "ArrowUp"
    for _ in range(abs(plan["to"] - plan["from"])):
        # page.keyboard.press, NOT a native handle: the hold then comes from the humanised press
        # wrapper rather than being reapplied here.
        await page.keyboard.press(step)
        await asyncio.sleep(_rand(45, 120) / 1000.0)
    got = await page.evaluate(
        "(s) => { const e = document.querySelector(s); return e ? e.selectedIndex : -1; }",
        selector,
    )
    return [plan["ret"]] if got == plan["to"] else None


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

    async def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here: it glides to
        the <select> and makes the selection through the keyboard so the ENGINE fires a trusted
        change (see _select_by_keyboard). One implementation behind the page method is what keeps
        the page-level and locator-level calls from drifting apart."""
        if _on(self):
            try:
                return await self.page.select_option(_sel(self), value, **_fwd_select(kw))
            except Exception:  # noqa: BLE001
                pass
        return await o_select_option(self, value, **kw)

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
    Locator.select_option = select_option


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
