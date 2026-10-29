"""Async mirror of _humanize.py for clearcote.async_api.

Same behaviour as the sync module, function by function: eased glides from the last cursor position,
native trusted input, click options honoured (button, click_count, delay, modifiers, position, force,
trial), field-classifying fill (text / range / date-like / native), fat-finger typos with mask-aware
correction, select_option by opening the drop-down (or clicking list-box rows), check/uncheck/
set_checked, and the Locator patch -- but every Playwright call is awaited and pacing uses
asyncio.sleep (off-protocol). The decisions come from the shared _forms module and the DOM reads go
through the isolated world (_isolated.AsyncIsolatedWorld), never page.evaluate. See _humanize.py for
the rationale and the measurements behind each step; comments here note what is async-specific.
"""

import asyncio
import collections
import functools
import math
import random
import time

from ._forms import (
    ENGINE_KEY_SEQUENCE, engine_click_params, engine_held_bits, engine_key_plan,
    classify_field, datetime_equal, engine_path_ok, fit_maxlength, formatted_equal, mask_kept, match_option,
    normalize_click_options, numeric_equal, plan_datetime, plan_select, plan_typo, pre_press_ms, range_attrs,
    range_fraction, range_nudge, range_point,
    range_sanitize, range_value_ok, segment_nodes_from_tree, typo_eligible, typos_allowed,
)
from ._humanize import (
    CURSOR_OVERLAY, ENGINE_POINTER_PATH, _ROLL_KEYS, _Native, _cdp_method_missing, _engine_clicks_mode, _host_platform,
    ENGINE_EXT_MISSING, _cdp_invalid_params, _engine_ext_mode,
    FRAMES_WAIT_MS, _boxes_overlap, _is_timeout, _needs_shift, _no_touch, _rand, _sel_kw, _shift_lag_s, _shift_lead_s,
    as_api_error,
    default_timeout_ms, engine_path_memo, trial_timeout_error, typo_rate, _MOD_KEYS,
    SELECT_COMMIT_WHY, is_navigation, select_deadline, select_left_ms,
    TAP_NOT_HUMANIZED, label_draw, native_select_args, report_fallback, select_wants, strict_mode, want_index,
)
from ._isolated import (
    ACTIVE_FIELD as ISO_ACTIVE_FIELD, ACTIVE_VALUE as ISO_ACTIVE_VALUE, COVER_CHECK_MS, COVER_RETRY_MS,
    COVER as ISO_COVER, SETTLE_MS, SETTLE_TRIES,
    CSS_BOX as ISO_CSS_BOX, DESCRIBE as ISO_DESCRIBE, FORM_CONTROL, HIT_IN_BOX as ISO_HIT_IN_BOX,
    FRAMES as ISO_FRAMES, LABEL_BY_SEL as ISO_LABEL_BY_SEL, LABEL_OF as ISO_LABEL_OF,
    LISTBOX_ROW as ISO_LISTBOX_ROW, SCROLL_BY as ISO_SCROLL_BY,
    PLATFORM as ISO_PLATFORM, VIEWPORT as ISO_VIEWPORT, async_world_for,
)
from ._motion import (
    WHEEL_NOTCH_PX, make_persona, plan_move, drag_dwell, click_hold, key_dwell, click_point, plan_ambient,
    dblclick_gap, pause_ms, timing_traits,
    platform_from_navigator, rollover_rate, wheel_notches,
    dwell_ms, move_off_delay_ms, move_off_roll, plan_dwell, plan_move_off,
    drag_hesitation, hesitate_then_drag, entry_point, enter_from_top, ENTRY_LEAD_MS,
)
from ._scroll import (
    ANCHOR_TARGET_W, SCROLLER as ISO_SCROLLER, SCROLL_RANGE as ISO_SCROLL_RANGE, anchor_needed, anchor_point, bring_into_view_async,
    in_view as _box_in, input_device as _input_device, paced_wheel_async, plan_trackpad, trackpad_traits, wheel_drift,
)
from ._attention import (
    IDLE_DRIFT_MAX_MS, TEXT_LENGTH as ISO_TEXT_LENGTH, ambient_mode, explore_notches, idle_drifts, read_pause_ms,
)
from ._hold import press_and_hold_async
from ._hzoptions import (
    _CALLER, apply_to_persona, engine_mode as _hz_engine_mode, options_of as _hz_options_of,
    report_fallback as _report_fallback, resolve as _hz_resolve, tempo_of as _hz_tempo, typo_rate_for as _hz_typo_rate,
)
from . import _frames as _fr
from . import _trace
from ._keys import (
    BLANK_POINT, FOCUSABLE, SEARCH_SLIP_RATE, SELECTION_STATE, TAB_DISTANCE, WORD_FIX_RATE, flight_ms,
    modifier_lag_ms, modifier_lead_ms, plan_gap, plan_repeat, split_chord, submit_pause_ms, tab_pause_ms,
    typing_traits,
)
from ._pacing import keep_in_view as _keep_in_view, trim_to as _trim_ambient, walk_async as _paced_walk


async def commit_select_async(page, selector, dl, press, settle_ms):
    """Async _humanize.commit_select: ``press()`` (the Enter, a coroutine) bounded by the select's
    deadline ``dl``, then ``settle_ms`` of rest. True when the commit navigated the page."""
    navs = []

    def on_request(req):
        if is_navigation(req):
            navs.append(req)
    listen = callable(getattr(page, "on", None)) and callable(getattr(page, "remove_listener", None))
    if listen:
        page.on("request", on_request)
    try:
        ms = select_left_ms(dl)
        if not ms:
            await press()
        else:
            try:
                await asyncio.wait_for(press(), ms / 1000.0)
            except asyncio.TimeoutError:
                raise trial_timeout_error(selector, dl[1], SELECT_COMMIT_WHY) from None
        await asyncio.sleep(settle_ms / 1000.0)
    finally:
        if listen:
            page.remove_listener("request", on_request)
    return bool(navs)


async def _release(coro):
    """Run a release -- a mouse button or a key coming up -- to completion even while the calling task
    is being cancelled. The finally blocks that call this already run on a first cancellation;
    asyncio.shield keeps the release going if a SECOND one arrives while it is awaited, so a cancelled
    action (asyncio.wait_for timing out, task.cancel()) never leaves a button or a key down. A button
    left down turns the next move into a drag; a key left down makes its next press an auto-repeat."""
    await asyncio.shield(coro)


def api_errors(api, fn):
    """``fn`` (a coroutine function), with every Playwright error it raises worded as ``api``'s:
    see _humanize.as_api_error ("Page.click: Timeout ...", not "Locator.wait_for: Timeout ...")."""
    @functools.wraps(fn)
    async def call(*a, **kw):
        token = _CALLER.set(api) if _CALLER.get() is None else None
        try:
            return await fn(*a, **kw)
        except Exception as e:  # noqa: BLE001
            n = as_api_error(e, api)
            if n is e:
                raise
            raise n.with_traceback(e.__traceback__) from None
        finally:
            if token is not None:
                _CALLER.reset(token)
    return call


def _is_strict(err):
    """Lane A's ClearcoteStrictError (strict mode): never swallowed by a best-effort path."""
    return type(err).__name__ == "ClearcoteStrictError"


async def attach_humanize(browser, page, humanize=False, show_cursor=False, seed=None):
    """Wrap one page's input methods + (optionally) inject the cursor overlay (async).

    ``seed`` (the fingerprint seed) selects a stable per-identity motor persona; unset ⇒ random.
    Idempotent per page (the ``_clearcote_humanized`` marker; see _humanize.attach_humanize)."""
    if show_cursor and getattr(page, "_clearcote_cursor", False) is not True:
        try:
            page._clearcote_cursor = True
        except Exception:  # noqa: BLE001
            pass

        async def inject():
            try:
                await page.evaluate(CURSOR_OVERLAY)
            except Exception:  # noqa: BLE001
                pass
        await inject()
        page.on("load", lambda _=None: asyncio.ensure_future(inject()))
        page.on("framenavigated",
                lambda f: asyncio.ensure_future(inject()) if f == page.main_frame else None)
    if humanize is None or humanize is False or getattr(page, "_clearcote_humanized", False) is True:
        return

    # Per-identity motor persona (cadence, tremor, overshoot, handedness) seeded from the fingerprint
    # so behavior is stable within a session and unlinkable across seeds. Stored on the page.
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
    # The typing identity and the caller's held keys (see _humanize.attach_humanize).
    traits = typing_traits(persona.seed)
    tp_traits = trackpad_traits(persona.seed)   # the trackpad identity (report rate, inertia decay), R2
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

    # Tracked cursor position so each move continues from where the last ended (seed off (0,0)).
    # "placed" is whether the REAL pointer has moved on this page yet: until it has, the browser's
    # cursor sits at the (0,0) origin whatever pos says. See _wheel_anchor / hdown /
    # _pointer_presence, and _humanize.py for the reasoning.
    st = {"pos": (_rand(140, 380), _rand(90, 240)), "held": False,
          "placed": False, "kb_primed": False}

    # ONE humanized action at a time on this page (async only). Two actions interleaved on one page
    # share the tracked cursor, the keyboard and the focus: asyncio.gather(page.fill("#a", A),
    # page.fill("#b", B)) returned success with a = "the q" and b holding A's text (verify-final-A,
    # S5a). Every public entry point below takes this lock; the helpers inside never call a locked
    # entry point (they use the native handles and the internal functions), so nothing deadlocks.
    # asyncio.Lock releases on cancellation, so a cancelled action does not block the next one.
    lock = asyncio.Lock()

    def _serial(fn, coords=False, after=True):
        # Inside the lock, the outermost action first runs the lazy after-action step (SC 232 lane C,
        # _pre_action: a due move-off, the attention model); `coords`: the action names a viewport point,
        # so no exploratory scroll may run before it. A trial performs no action: nothing after-action.
        async def serial(*a, **kw):
            async with lock:
                if not after or kw.get("trial") or st["depth"] > 0:
                    return await fn(*a, **kw)
                await _pre_action(coords)
                st["depth"] += 1
                try:
                    return await fn(*a, **kw)
                finally:
                    st["depth"] -= 1
                    st["last_end"] = time.monotonic()
        serial.__name__ = getattr(fn, "__name__", "serial")
        return serial

    mouse = page.mouse
    native_move, native_click, native_wheel = mouse.move, mouse.click, mouse.wheel
    # getattr, not attribute access: attach must not raise on a page-like object that lacks a
    # method we only optionally wrap. Reading mouse.down/up directly widened the attach-time
    # contract and took four previously-green async tests down with an AttributeError before
    # anything was even wrapped — humanize failing closed on attach is the one outcome this
    # module must never produce, since it silently disables every other humanization too.
    native_down = getattr(mouse, "down", None)
    native_up = getattr(mouse, "up", None)
    # The keyboard natives are captured here (the sync file captures them further down): the press
    # helpers below hold modifiers with them.
    kb = page.keyboard
    native_kb_type, native_kb_press = kb.type, kb.press
    native_kb_insert = getattr(kb, "insert_text", None)   # a bulk fill's paste (Input.insertText)
    # getattr: a page-like keyboard without down/up still gets every other humanization; it just
    # types capitals without the Shift key, as before.
    native_kb_down, native_kb_up = getattr(kb, "down", None), getattr(kb, "up", None)

    # Score hook (_trace, lane G): see the sync file -- the typed run's keys are recorded while st["keyrec"]
    # is a list; off (the default) these wrappers only forward.
    _kb_press0, _kb_down0, _kb_up0 = native_kb_press, native_kb_down, native_kb_up
    _kb_pend = {}

    def _kb_rec(key, t0, t1):
        rec = st.get("keyrec")
        if rec is not None and key not in _MOD_KEYS:
            rec.append({"key": key, "down": t0, "up": t1})

    async def native_kb_press(key, **kw):
        t0 = time.perf_counter() * 1000.0
        r = await _kb_press0(key, **kw)
        _kb_rec(key, t0, time.perf_counter() * 1000.0)
        return r

    if _kb_down0 is not None and _kb_up0 is not None:
        async def native_kb_down(key, **kw):
            t0 = time.perf_counter() * 1000.0
            r = await _kb_down0(key, **kw)
            if st.get("keyrec") is not None:
                _kb_pend[key] = t0
            return r

        async def native_kb_up(key, **kw):
            r = await _kb_up0(key, **kw)
            t0 = _kb_pend.pop(key, None)
            if t0 is not None:
                _kb_rec(key, t0, time.perf_counter() * 1000.0)
            return r

    async def _dispatch(steps, kind="glide", buttons=0):
        """Walk a planned trajectory (list of (x, y, sleep_ms)) via native trusted mouse.move +
        off-protocol asyncio.sleep. ``kind`` / ``buttons`` name the walk for the score hook (_trace, lane G:
        glide | drag (buttons 1) | ambient | hover). Native input carries button state (held-button drag works).
        Paced against a wall clock (_pacing.walk_async): samples go out at their planned time, late
        ones are skipped (never the last; no skip hops more than 40 px, or 20 px once nothing has been
        dispatched for 40 ms: _pacing.MAX_JUMP_PX / IDLE_HOP_PX), so the dispatch round trip no
        longer stretches every glide 3-5x (SC 209)."""
        async def mv(sx, sy):
            await native_move(sx, sy)
            st["placed"] = True
        vp = await _view_bounds()
        if tempo != 1.0:
            steps = [(sx, sy, sl * tempo) for sx, sy, sl in steps]
        await _paced_walk(_keep_in_view(steps, *vp) if vp else steps, mv, kind=kind, buttons=buttons)

    async def _view_bounds():
        """The viewport (innerWidth, innerHeight) a planned path is kept inside (_pacing.keep_in_view),
        read in the isolated world at most every 2 s; None when it cannot be read."""
        now = time.monotonic()
        c = st.get("vp")
        if c is None or now - c[0] > 2.0:
            try:
                wh = await async_world_for(page).evaluate(ISO_VIEWPORT)
                vp = (float(wh[0]), float(wh[1])) if wh and wh[0] and wh[1] else None
            except Exception:  # noqa: BLE001
                vp = None
            st["vp"] = c = (now, vp)
        return c[1]

    async def _glide(x, y, settle=False, target_w=24, held=False):
        # minimum-jerk sum-of-submovements path with colored noise + Fitts duration (_motion.plan_move).
        # A page the pointer has not been over yet: in over the top edge, the samples still above the view
        # not sent (_motion.entry_point / enter_from_top; pro-r43-13's rule) -- see _humanize._glide.
        frm = st["pos"]
        entering = not st["placed"] and not held and not settle
        if entering:
            vp = await _view_bounds()
            frm = entry_point((x, y), vp[0] if vp else None)
        steps = plan_move(frm, (x, y), persona, target_w=target_w, settle=settle, held=held)
        if entering:
            steps = enter_from_top(steps, frm)
        await _dispatch(steps, kind="drag" if held else "glide", buttons=1 if held else 0)
        st["pos"] = (x, y)
        st["typed"] = None   # the hand left the keyboard: no submit / Tab pause owed (_after_typing)

    def _ambient_plan(ms, box, rng=None):
        """_humanize._ambient_plan: plan_ambient from the tracked position -- or, on a page the pointer has not been over yet, from a point
        above the view with the samples still above it dropped (_motion.entry_point / enter_from_top): the drift's
        first excursion comes in over the top edge, as _glide's first move does (pro-r43-13's rule, lab row H 13).
        Planned ENTRY_LEAD_MS longer then, so the dropped lead-in (~100-200 ms) leaves the walk no shorter than
        ``ms``; the caller trims to ``ms``. ``rng`` None: unseeded (the attention model passes its own)."""
        if st["placed"]:
            return plan_ambient(st["pos"], box, persona, ms, rng)
        start = entry_point(st["pos"], box["width"], rng)
        return enter_from_top(plan_ambient(start, box, persona, ms + ENTRY_LEAD_MS, rng), start, rng)

    # Engine real-trajectory routing (PRO): send point-to-point moves/clicks through
    # Browser.dispatchPointerPath, humanizedClick before r34 (real recorded human paths for PRO, engine bezier for free,
    # tiered by the engine license gate). Drags stay native (held button); fall back to
    # the SDK bezier if the method is unavailable.
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

    async def _engine_session():
        if _eng["session"] is None:
            tsession = await page.context.new_cdp_session(page)
            info = await tsession.send("Target.getTargetInfo")
            _eng["tid"] = info["targetInfo"]["targetId"]
            b = getattr(page.context, "browser", None)
            _eng["session"] = (await b.new_browser_cdp_session()
                               if b is not None else tsession)

    def _maybe_report_ext():
        """See _humanize._maybe_report_ext: lane A's on_fallback hears once per browser that a PRO engine lacks
        the r43 extensions (the hook only; strict mode does not raise)."""
        memo = engine_path_memo(page)
        if memo.get("ext") is not False or not memo.get("method") or memo.get("ext_reported"):
            return
        memo["ext_reported"] = True
        o = _hz_options_of(page)
        fn = getattr(o, "on_fallback", None) if o is not None else None
        if fn is not None:
            fn({"api": "engine_ext", "reason": ENGINE_EXT_MISSING, "selector": None})

    async def _engine_ext_ok():
        """See _humanize._engine_ext_ok: r43 dispatchPointerPath parameters + dispatchKeySequence, asked once per
        browser (empty dispatchKeySequence: -32602 on r43, -32601 before)."""
        if not _eng["ok"] or not _engine_ext_mode():
            return False
        memo = engine_path_memo(page)
        if "ext" not in memo:
            try:
                await _engine_session()
                await _eng["session"].send(ENGINE_KEY_SEQUENCE, {"targetId": _eng["tid"], "keys": []})
                memo["ext"] = False
            except Exception as e:  # noqa: BLE001
                memo["ext"] = _cdp_invalid_params(e)
            _maybe_report_ext()
        return memo["ext"]

    async def _engine_glide(x, y, no_click, ext=None):
        """See _humanize._engine_glide (r43: `ext` parameters, fromX/fromY re-sync, no sleep after)."""
        if not _eng["ok"]:
            return False
        use_ext = bool(ext or not _engine_in_sync()) and await _engine_ext_ok()
        if not use_ext and (ext or not _engine_in_sync()):
            return False
        memo = engine_path_memo(page)
        if memo.get("method") is False:   # this browser's engine has neither name: no probe again
            _eng["ok"] = False
            return False
        try:
            await _engine_session()
            # The engine does not wait for the page: what it walks into a page that has not drawn yet is
            # lost (a fresh headless browser's first page, SC 216). Wait for two frames first; free once
            # drawn. Best effort: a wait that fails still walks (the engine route stays on).
            try:
                await async_world_for(page).evaluate(ISO_FRAMES, FRAMES_WAIT_MS, await_promise=True)
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
                    params["fromX"], params["fromY"] = float(st["pos"][0]), float(st["pos"][1])
                params.update(ext or {})
            t_send = time.monotonic()
            method = _eng.get("method") or memo.get("method")
            if method:
                await _eng["session"].send(method, params)
            else:
                # New name first; an engine older than r34 answers "wasn't found" (-32601)
                # and gets the old one. Any other error is a real failure (below). The answer is
                # remembered for the whole browser (engine_path_memo), not just this page.
                try:
                    await _eng["session"].send(ENGINE_POINTER_PATH[0], params)
                    method = ENGINE_POINTER_PATH[0]
                except Exception as e:  # noqa: BLE001
                    if not _cdp_method_missing(e):
                        raise
                    try:
                        await _eng["session"].send(ENGINE_POINTER_PATH[1], params)
                    except Exception as e2:  # noqa: BLE001
                        if _cdp_method_missing(e2):
                            memo["method"] = False
                        raise
                    method = ENGINE_POINTER_PATH[1]
                memo["method"] = method
            _eng["method"] = method
            _maybe_report_ext()
            # The engine walks the trajectory in the BROWSER process and answers before it has
            # finished. Without waiting, a following mouse.down() fires at the cursor's stale
            # position -- measured on the sync path: a move to (110,58) then a press landed at
            # (981,629), on <body> instead of the target, breaking every move-then-press
            # sequence (drag, press-and-hold, drag-and-drop). See _humanize.py.
            # r38 answers only after the walk AND the click (send returned 772 ms after a 600 ms
            # walk, 2 ms after the release, SC 210): wait only for what is left of `dur`, so an
            # engine that answers late does not add a second, idle `dur` after every click.
            if not use_ext:   # r43 answers after its last sample / release
                await asyncio.sleep(max(0.0, dur - (time.monotonic() - t_send)))
            try:
                await native_move(x, y)
            except Exception:  # noqa: BLE001
                pass
            st["pos"] = (x, y)
            st["placed"] = True
            _eng["at"] = st["pos"]
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
        # A move while the button is held is a DRAG leg: plan_drag keeps it on the line between grab
        # and drop (an aimed reach's arc took a slider handle 47 px off a 40 px track, SC 209).
        # r43 engine (SC 232 lane H): held legs take the engine route (buttons carried, answered after the walk).
        if (st["held"] and _hz_engine_mode(page, _engine_clicks_mode) != "off" and await _engine_ext_ok()
                and await _engine_glide(x, y, no_click=True,
                                        ext={"buttons": engine_held_bits(st.get("held_button"))})):
            return
        await _glide(x, y, held=st["held"])

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
            # The pre-press pause, after the pin (itself a pointermove): see _humanize.hdown. A
            # down() with no move before it (never placed) stays instant.
            await _pre_press()
        # try/finally so a raising press cannot strand held=True and make every later move a
        # phantom drag leg (hup already had this; hdown did not).
        try:
            st["held"] = True
            st["held_button"] = kw.get("button") or "left"
            await native_down(**kw)
        except Exception:
            st["held"] = False
            raise

    async def hup(**kw):
        try:
            await native_up(**kw)
        finally:
            st["held"] = False

    async def _platform():
        """The platform the page believes (the persona's), read once per page."""
        if "platform" not in st:
            plat = None
            try:
                plat = platform_from_navigator(await async_world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["platform"] = plat or _host_platform()
        return st["platform"]

    async def _pre_press():
        """The pause between the pointer coming to rest and the button going down (_forms.pre_press_ms:
        40-250 ms, right-skewed, centred per identity)."""
        await asyncio.sleep(pre_press_ms(persona.seed) * tempo / 1000.0)

    async def _press_here(x, y, hold=None):
        """Press and release the left button where the last glide left the pointer, after the
        pre-press pause. Native down/up, so nothing moves (see _humanize._press_here)."""
        hold = click_hold(persona) if hold is None else hold
        await _pre_press()
        if native_down is None or native_up is None:
            await native_click(x, y, delay=hold)
            return
        t0 = time.monotonic()
        try:
            await native_down()   # inside the try: a press in flight when cancelled was still sent
            await asyncio.sleep(_hold_rest(hold, t0))
        finally:
            await _release(native_up())   # never leave the button down, not even when cancelled

    def _hold_rest(hold_ms, t0):
        """What is left of a button hold once mouse.down() has returned (see _humanize._hold_rest): the
        hold runs from when the press was sent, not from when the page finished handling it."""
        return max(0.0, hold_ms / 1000.0 - (time.monotonic() - t0))

    async def _press_at(x, y, o):
        """Press at (x, y) -- where the last glide ended -- the way the options ask: the button, the
        click count (a double click is two presses with a human gap), the caller's hold (`delay`) or
        the persona's, and modifiers held around the whole press with a lead before it and a lag
        after. Native down/up, so nothing moves. See _humanize._press_at."""
        mods = o["modifiers"]
        held = []
        try:
            for m in mods:
                held.append(m)   # before the await: a keydown in flight when cancelled was still sent
                await native_kb_down(m)
                await asyncio.sleep(_rand(40, 110) / 1000.0)
            await _pre_press()    # the hand confirms the target before it presses
            n = o["click_count"]
            for i in range(1, n + 1):
                hold = o["delay"] if o["delay"] is not None else click_hold(persona)
                t0 = time.monotonic()
                try:
                    # inside the try: a press in flight when the task is cancelled was still sent
                    await native_down(button=o["button"], click_count=i)
                    await asyncio.sleep(_hold_rest(hold, t0))
                finally:
                    # never leave the button down -- not on an error, and not when this task is
                    # cancelled mid-hold (the next move would be a drag)
                    await _release(native_up(button=o["button"], click_count=i))
                if i < n:
                    await asyncio.sleep(dblclick_gap(persona) / 1000.0)   # the gap inside a double click: the persona's
        finally:
            if held:
                async def lift(keys=tuple(reversed(held))):
                    for m in keys:
                        await asyncio.sleep(_rand(20, 80) / 1000.0)
                        try:
                            await native_kb_up(m)   # one failed keyup must not strand the others
                        except Exception:  # noqa: BLE001
                            pass
                await _release(lift())

    async def _engine_ext_click(x, y, o):
        """See _humanize._engine_ext_click: a right/middle, double/triple or modified click walked AND pressed by an
        r43 engine, the modifier keys pressed here around it; shared by mouse.click and the targeted click (R5)."""
        ext = engine_click_params(o)
        if not ext or _hz_engine_mode(page, _engine_clicks_mode) == "off" or not await _engine_ext_ok():
            return False
        if o["modifiers"] and not (native_kb_down and native_kb_up):
            return False
        held = []
        try:
            for m in o["modifiers"]:
                held.append(m)   # before the await (see _press_at)
                await native_kb_down(m)
                await asyncio.sleep(_rand(40, 110) / 1000.0)
            return bool(await _engine_glide(x, y, no_click=False, ext=ext))
        finally:
            for m in reversed(held):
                await asyncio.sleep(_rand(20, 80) / 1000.0)
                try:
                    await native_kb_up(m)
                except Exception:  # noqa: BLE001
                    pass

    async def hclick(x, y, **kw):
        # The engine path performs ONE plain left click. Anything else -- right/middle button, a
        # double click, a caller-chosen hold, modifiers -- glides SDK-side and presses as asked.
        o = normalize_click_options(kw, await _platform())
        if engine_path_ok(o) and _hz_engine_mode(page, _engine_clicks_mode) != "off" and await _engine_glide(x, y, no_click=False):
            return
        # r43 engine (SC 232 lane H): right/middle, double/triple and modified clicks too (_engine_ext_click).
        if await _engine_ext_click(x, y, o):
            return
        await _glide(x, y)
        # A press that raises RAISES (it was swallowed here: a click whose press failed reported
        # success). Only the pin before a press (hdown) stays best-effort.
        if native_down is None or native_up is None or (o["modifiers"] and native_kb_down is None):
            await _pre_press()
            await native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                               **_sel_kw(kw, "button", "click_count"))
        else:
            await _press_at(x, y, o)
        # Not after an engine click (returned above): a glide the engine does not see would take the
        # next coordinate click off the engine path (_engine_in_sync).
        _schedule_move_off()

    async def hdblclick_xy(x, y, **kw):
        kw = dict(kw)
        kw["click_count"] = 2
        await hclick(x, y, **kw)   # the internal click: the public mouse.click is the locked one

    async def _held_glide(x, y):
        # r43 engine (SC 232 lane H): the held leg + settle walked engine-side, buttons carried.
        if (st["held"] and _hz_engine_mode(page, _engine_clicks_mode) != "off" and await _engine_ext_ok()
                and await _engine_glide(x, y, no_click=True,
                                        ext={"buttons": engine_held_bits(st.get("held_button")), "settle": True})):
            return
        await _glide(x, y, settle=True)              # held-button drag leg + seating jiggle
    page._clearcote_held_glide = _held_glide

    async def hambient(ms=1200):
        """Opt-in ambient / pre-challenge cursor entropy before the first goal action. await page.ambient_motion(ms)."""
        try:
            vp = page.viewport_size or {"width": 1280, "height": 800}
            # plan_ambient overruns its budget (whole excursions, rests drawn at 0.9-1.4x a frame):
            # cut the plan at a slow sample near ms so ambient_motion(ms) lasts about ms. On a page the
            # pointer has not been over yet the drift comes in over the top edge (_ambient_plan).
            steps = _trim_ambient(_ambient_plan(ms, {"width": vp["width"], "height": vp["height"]}), ms)
            await _dispatch(steps, kind="ambient")
            if steps:
                st["pos"] = (steps[-1][0], steps[-1][1])
        except Exception:  # noqa: BLE001
            pass

    page.ambient_motion = hambient

    # OPT-IN: no ambient burst on load by default (it interleaves with caller-driven input,
    # e.g. a slider drag). Call page.ambient_motion(ms) explicitly, or set
    # page._clearcote_auto_ambient = True to restore the per-load burst.
    page._clearcote_auto_ambient = hz.ambient == "load"
    page._clearcote_ambient = hz.ambient or "off"   # read by the attention/idle code (lane C)

    async def _auto_ambient():
        # ambient="load" (lane A's option, read defensively) is the same per-load burst as the flag.
        if getattr(page, "_clearcote_auto_ambient", False) is True or ambient_mode(page) == "load":
            try:
                await hambient(_rand(450, 950))
            except Exception:  # noqa: BLE001
                pass

    page.on("load", lambda _=None: asyncio.ensure_future(_auto_ambient()))
    # The attention model's read pause counts from the last load (read lazily by the next action).
    page.on("load", lambda _=None: st.__setitem__("load_at", time.monotonic()))

    # ------------------------------------------------------------ after-action (SC 232 lane C)
    # See the sync file: hover dwell, move-off after a click, and the attention model, all LAZY (run at the
    # start of the next action inside the page lock, or by page.settle()), never from a timer or a task.
    # Traits from the persona's salted stream (_motion.after_traits); per-action draws unseeded.
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

    async def _dwell(x, y, box, dwell):
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
        await _dispatch(steps, kind="hover")
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    def _schedule_move_off():
        """After a press: in the persona's 60-85 % of clicks a move-off comes due 200-700 ms after the
        release (see the sync file)."""
        hit = move_off_roll(persona, arng, st["mo_hits"], st["mo_n"])
        st["mo_n"] += 1
        st["mo"] = None
        if hit:
            st["mo_hits"] += 1
            st["mo"] = {"due": time.monotonic() + move_off_delay_ms(persona, arng) / 1000.0, "pos": st["pos"]}

    async def _run_move_off(wait):
        mo, st["mo"] = st["mo"], None
        if not mo or st["held"] or st["pos"] != mo["pos"]:
            return False
        left = mo["due"] - time.monotonic()
        if left > 0:
            if not wait:
                return False
            await asyncio.sleep(left)
        vw, vh = (await _view_bounds()) or (await _viewport())
        tx, ty = plan_move_off(st["pos"], {"width": vw, "height": vh}, persona, arng)
        trace.append(("move_off", st["pos"], (tx, ty)))
        await _glide(tx, ty, target_w=140)   # one coarse glide: nothing to acquire
        return True

    async def _idle_drift(ms):
        trace.append(("drift", ms))
        vw, vh = (await _view_bounds()) or (await _viewport())
        steps = _trim_ambient(_ambient_plan(ms, {"width": vw, "height": vh}, arng), ms)
        await _dispatch(steps, kind="ambient")
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    async def _attend(coords, now):
        # `now`: when the action started -- a move-off glide just made does not count as idle time
        la = st["load_at"]
        if la is not None:
            st["load_at"] = None
            try:
                n = await async_world_for(page).evaluate(ISO_TEXT_LENGTH)
            except Exception:  # noqa: BLE001
                n = 0
            pause = read_pause_ms(n, persona, arng) / 1000.0
            notches = explore_notches(arng)
            trace.append(("read", pause * 1000.0, n, notches if not coords else 0))
            if pause - (now - la) > 0.35:   # the eyes read; the hand drifts once meanwhile
                await _idle_drift(min((pause - (now - la)) * 500.0, IDLE_DRIFT_MAX_MS))
            rest = la + pause - time.monotonic()
            if rest > 0:
                await asyncio.sleep(rest)
            if notches and not coords:   # never scroll away the point a coordinate action names
                await hwheel(0, notches * (await _notch_px()))
            return
        last = st["last_end"]
        if last is not None:
            for ms in idle_drifts(now - last, arng):
                await _idle_drift(ms)

    async def _pre_action(coords):
        if st["depth"] > 0 or st["held"]:
            return
        st["depth"] += 1
        try:
            now = time.monotonic()
            await _run_move_off(False)
            if _attention_on():
                await _attend(coords, now)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the after-action model never stops the action
            pass
        finally:
            st["depth"] -= 1

    def _action(fn, coords=False):
        """An UNLOCKED public entry point (mouse.move / wheel): the after-action step first."""
        async def run(*a, **kw):
            if st["depth"] > 0 or kw.get("trial"):
                return await fn(*a, **kw)
            await _pre_action(coords)
            st["depth"] += 1
            try:
                return await fn(*a, **kw)
            finally:
                st["depth"] -= 1
                st["last_end"] = time.monotonic()
        run.__name__ = getattr(fn, "__name__", "action")
        return run

    def _touch(fn):
        async def run(*a, **kw):
            try:
                return await fn(*a, **kw)
            finally:
                st["last_end"] = time.monotonic()
        return run

    async def hsettle():
        """await page.settle(): let the hand settle after the last action -- wait for a pending move-off to
        come due and make it. False when none was pending (or the pointer has moved since)."""
        async with lock:
            if st["depth"] > 0:
                return False
            st["depth"] += 1
            try:
                done = await _run_move_off(True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                done = False
            finally:
                st["depth"] -= 1
            if done:
                st["last_end"] = time.monotonic()
            return done

    def _install_after_action():
        """Wrap the unlocked public entry points (the locked ones run the step inside _serial)."""
        mouse.move = _action(mouse.move, coords=True)
        mouse.wheel = _action(mouse.wheel, coords=True)
        if native_down is not None and native_up is not None:
            mouse.down, mouse.up = _touch(mouse.down), _touch(mouse.up)
        page.ambient_motion = _serial(page.ambient_motion)
        page.settle = hsettle

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
            wh = await async_world_for(page).evaluate(ISO_VIEWPORT)
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
            # With a button down every move is a DRAG leg, so anchoring mid-drag would haul the
            # grabbed element across the page instead of scrolling under it.
            if not anchor_needed(st["pos"], vw, vh, st["placed"], st["held"]):
                return
            # Reading position: upper-middle, gaussian-spread (_scroll.anchor_point, shared by all 3 SDKs).
            ax, ay = anchor_point(vw, vh)
            await _glide(ax, ay, target_w=ANCHOR_TARGET_W)   # coarse placement, not a target acquisition
        except Exception:  # noqa: BLE001 - never let the anchor stop the scroll
            pass

    async def _notch_px():
        """Pixels per wheel notch for the platform the page believes, read once per page."""
        if "notch" not in st:
            plat = None
            try:
                plat = platform_from_navigator(await async_world_for(page).evaluate(ISO_PLATFORM))
            except Exception:  # noqa: BLE001
                pass
            st["notch"] = WHEEL_NOTCH_PX[plat or _host_platform()]
        return st["notch"]

    async def _device():
        """"trackpad" or "mouse" for this page (see _humanize._device)."""
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
            st["device"] = _input_device(await _platform(), opt if isinstance(opt, str) else None)
        return st["device"]

    async def _tsleep(sec):
        await asyncio.sleep(sec * tempo)

    def _scroll_io():
        """The humanizer, as _scroll.bring_into_view_async sees it."""
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
            async def scroll_range():
                r = await async_world_for(page).evaluate(ISO_SCROLL_RANGE)
                return r if isinstance(r, list) and len(r) == 4 else None
        return _IO

    async def _hwheel_run(delta_x, delta_y, send_wheel):
        # Whole notches only: a CDP wheel event always reports wheelDelta +-120, so the eased
        # sub-notch slices this replaced were events no mouse emits (see _humanize.hwheel). A trackpad
        # persona swipes the exact distance instead (_scroll.plan_trackpad).
        await _wheel_anchor()
        if await _device() == "trackpad":
            await paced_wheel_async(plan_trackpad(delta_x, delta_y, tp_traits), send_wheel, asyncio.sleep)
            return
        notch = await _notch_px()
        plan = ([(0, notch if delta_y > 0 else -notch)] * wheel_notches(delta_y, notch)
                + [(notch if delta_x > 0 else -notch, 0)] * wheel_notches(delta_x, notch))
        burst = random.randint(2, 5)
        for k, (ex, ey) in enumerate(plan):
            await send_wheel(ex, ey)
            if k == len(plan) - 1:
                break
            burst -= 1
            if burst == 0:
                await asyncio.sleep(_rand(110, 320) * tempo / 1000.0)  # the finger resets for the next flick
                burst = random.randint(2, 5)
            else:
                await asyncio.sleep(_rand(18, 55) / 1000.0)
            if random.random() < 0.07:
                await asyncio.sleep(_rand(150, 450) * tempo / 1000.0)
            # A hand resting on the mouse does not hold it perfectly still through a long scroll, so
            # wheel events sharing one identical clientX/clientY are a script signature. A couple
            # of px, so the scroll stays over the same element (and never while a button is down,
            # where a move is a drag leg).
            if len(plan) >= 6 and not st["held"] and random.random() < 0.06:
                try:
                    dx, dy = wheel_drift()
                    await native_move(st["pos"][0] + dx, st["pos"][1] + dy)
                    st["pos"] = (st["pos"][0] + dx, st["pos"][1] + dy)
                except Exception:  # noqa: BLE001
                    pass

    async def hwheel(delta_x, delta_y):
        """mouse.wheel: _hwheel_run, with the score hook (_trace, lane G) recording every wheel event this call
        sends and its send time, scored as "wheel" (off: the natives only)."""
        rec = [] if _trace.enabled() else None
        if rec is None:
            return await _hwheel_run(delta_x, delta_y, native_wheel)
        t0 = time.perf_counter()

        async def send_wheel(ex, ey):
            await native_wheel(ex, ey)
            rec.append({"t": (time.perf_counter() - t0) * 1000.0, "dx": ex, "dy": ey, "mode": 0})
        try:
            return await _hwheel_run(delta_x, delta_y, send_wheel)
        finally:
            if rec:
                _trace.record("wheel", rec, {"device": st.get("device"), "dx": delta_x, "dy": delta_y})

    mouse.move, mouse.click, mouse.wheel = hmove, _serial(hclick, coords=True), hwheel
    if getattr(mouse, "dblclick", None) is not None:
        mouse.dblclick = _serial(hdblclick_xy, coords=True)   # stock dblclick teleported and pressed for ~0 ms
    # down/up wrapped only to TRACK held state for hmove; the presses stay native.
    mouse.down, mouse.up = hdown, hup

    # ----------------------------------------------------------------- keyboard
    async def _pointer_presence():
        """Give a keyboard-only session a mouse, once, before its first keystroke.

        Selector-driven typing already glides (page.type/fill/press go through _focus_click), but a
        bare page.keyboard.type/press types into whatever is focused — from a session in which the
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
            await native_kb_press(ch, delay=_kdwell())
            return
        except Exception:  # noqa: BLE001
            pass
        # Outside the catch: in strict mode lane A's report raises, and that must reach the caller.
        _fallback("Keyboard.press", "insertText: a character keyboard.press cannot map")
        try:
            await native_kb_type(ch)
        except Exception:  # noqa: BLE001
            pass

    async def _active_value():
        """The focused field's current value, read in the isolated world (None if unknown)."""
        try:
            return await async_world_for(page).evaluate(ISO_ACTIVE_VALUE)
        except Exception:  # noqa: BLE001
            return None

    def _flight_s(prev=None, ch=None):
        """One keyup->keydown flight of the persona's cadence (_keys.flight_ms), in seconds."""
        return flight_ms(traits, prev, ch, random.random) / 1000.0

    async def _backspaces(n):
        for k in range(n):
            await native_kb_press("Backspace", delay=_kdwell())
            if k < n - 1:
                await asyncio.sleep(_rand(70, 150) / 1000.0)

    def _landed(before, now):
        """How many characters arrived since ``before`` (None = cannot tell)."""
        if before is None or now is None:
            return None
        return len(now) - len(before)

    async def _restore(target):
        """Bring the focused field back to ``target`` (what it held before a slip) from wherever a word
        delete left it: Backspace to the common prefix, then type the rest. Nothing when unreadable."""
        got = await _active_value()
        if got is None or got == target:
            return
        k = 0
        while k < min(len(got), len(target)) and got[k] == target[k]:
            k += 1
        if len(got) - k > 64 or len(target) - k > 64:
            return
        if len(got) > k:
            await _backspaces(len(got) - k)
            await asyncio.sleep(_rand(80, 200) / 1000.0)
        await _emit_keys(target[k:])

    async def _emit_keys(chars, prev=None):
        """``chars`` typed in order, one human flight apart, every capital / shifted symbol behind a real
        ShiftLeft held across a run of them (see _humanize._emit_keys; SC 239). The flags move before the
        awaits, as in the main loop: a keydown in flight when cancelled was still sent."""
        n = len(chars)
        shift = False
        try:
            for j, c in enumerate(chars):
                if prev is not None:
                    await asyncio.sleep(_flight_s(prev, c))
                if (_needs_shift(c) and not shift and "Shift" not in held_keys
                        and native_kb_down is not None and native_kb_up is not None):
                    shift = True
                    await native_kb_down("Shift")
                    await asyncio.sleep(_shift_lead_s())
                await _emit_key(c)
                if shift and (j == n - 1 or not _needs_shift(chars[j + 1])):
                    await asyncio.sleep(_shift_lag_s())
                    shift = False
                    await native_kb_up("Shift")
                prev = c
        finally:
            if shift:   # never leave Shift held (nor on cancellation)
                try:
                    await _release(native_kb_up("Shift"))
                except Exception:  # noqa: BLE001
                    pass

    async def _word_delete():
        """The word-delete chord the ENGINE's editing honours: Alt+Backspace on macOS, else Ctrl."""
        await _press_key(("Alt" if _host_platform() == "macos" else "Control") + "+Backspace")

    async def _do_typo(text, i, tp, leave=False):
        """Make the planned slip at text[i] and correct it. Returns True when it already typed
        text[i] itself (the doubled-key case) -- or left a substitution uncorrected (``leave``: a search
        field) --, so the caller must not type it again.

        Only what actually LANDED is deleted: an input mask or a maxlength can swallow the wrong key,
        and a Backspace pressed for a key that never arrived deletes a real character (the
        letter-typo-in-a-numeric-mask failure). Measured before and after the slip, in the isolated
        world, one read each -- typos are 0.5-4 % of keys, so this costs nearly nothing.

        A substitution noticed inside a word is fixed with the word-delete chord and the word typed
        again in WORD_FIX_RATE of the cases (when the field can be read to put it back exactly)."""
        before = await _active_value()
        kind = tp["kind"]
        if kind == "sub":
            await _emit_key(tp["wrong"])
            got = _landed(before, await _active_value())
            if got is not None and got <= 0:
                return False                     # swallowed: nothing to correct
            if leave:
                return True                      # the slip stays: the wrong key took this place
            late = text[i + 1:i + 1 + tp.get("late", 0)]
            await _emit_keys(late, prev=tp["wrong"])      # late notice: a key or two more
            typed = 1 + len(late)
            await asyncio.sleep(_rand(140, 340) / 1000.0)  # noticing
            ws = max(text.rfind(" ", 0, i), text.rfind("\n", 0, i)) + 1
            # a WORD (letters only: no word delete in a code, a zip or a number), two keys of it on screen
            if (before is not None and i - ws + typed >= 2 and native_kb_down is not None
                    and all(c.isalpha() for c in text[ws:i + 1]) and random.random() < WORD_FIX_RATE):
                await _word_delete()
                await asyncio.sleep(_rand(120, 260) / 1000.0)
                await _restore(before)
                await asyncio.sleep(_rand(80, 200) / 1000.0)
                return False
            got = _landed(before, await _active_value())
            await _backspaces(typed if got is None else max(0, min(typed, got)))
            await asyncio.sleep(_rand(80, 200) / 1000.0)
            return False
        if kind == "double":
            await _emit_key(text[i])
            await asyncio.sleep(_rand(45, 95) / 1000.0)      # the bounce comes fast
            await _emit_key(text[i])
            got = _landed(before, await _active_value())
            await asyncio.sleep(_rand(140, 340) / 1000.0)
            extra = 1 if got is None else max(0, got - 1)
            if extra:
                await _backspaces(extra)
                await asyncio.sleep(_rand(80, 200) / 1000.0)
            return True
        if kind == "swap":
            await _emit_key(text[i + 1])
            await asyncio.sleep(_flight_s(text[i + 1], text[i]))
            await _emit_key(text[i])
            got = _landed(before, await _active_value())
            await asyncio.sleep(_rand(140, 340) / 1000.0)
            await _backspaces(2 if got is None else max(0, min(2, got)))
            await asyncio.sleep(_rand(80, 200) / 1000.0)
            return False
        return False

    async def _typing_field(field):
        """What the typo gate needs about the field keys go to: the caller's description, else
        the focused element read in the isolated world."""
        if field is not None:
            return field
        try:
            return await async_world_for(page).evaluate(ISO_ACTIVE_FIELD)
        except Exception:  # noqa: BLE001
            return None

    async def _engine_type(text, floor):
        """See _humanize._engine_type: one Browser.dispatchKeySequence (r43) with lane B's dwell and lane D's flight."""
        keys = engine_key_plan(text, _kdwell,
                               lambda ch, nxt: max(floor, plan_gap(traits, ch, nxt, random.random,
                                                                   getattr(persona, "tempo", 1.0),
                                                                   getattr(persona, "pause_scale", 1.0))),
                               _needs_shift)
        if not keys:
            return False
        try:
            await _engine_session()
            await _eng["session"].send(ENGINE_KEY_SEQUENCE, {"targetId": _eng["tid"], "keys": keys})
        except Exception:  # noqa: BLE001
            return False
        _trace_engine_typed(keys, floor)
        return True

    def _trace_engine_typed(keys, floor):
        """See _humanize._trace_engine_typed: one ``typed`` line of the planned down/up times (R3c)."""
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

    async def _human_type_text(text, field=None, typos=True, floor_ms=0.0, exact=True):
        """Type text key-by-key with human timing through Playwright's native (trusted) keyboard.

        Capitals and shifted symbols go behind a real ShiftLeft held across the run; unshifted pairs
        roll over; fat-finger typos are planned by _forms.plan_typo and never happen where a slip is
        a failure rather than a slip (see _humanize._human_type_text). Cadence: _keys.plan_gap (bigram
        flight from the persona's WPM), ``floor_ms`` the caller's delay as the shortest gap; ``exact``
        False lets a search-field substitution stay uncorrected."""
        text = str(text)
        n = len(text)
        shift = False
        ext_shift = "Shift" in held_keys
        rate = _typo_rate() if typos else 0.0
        fld = await _typing_field(field) if rate > 0 else None
        allow = rate > 0 and typos_allowed(fld)
        search = bool(allow and not exact and fld and (fld.get("type") or "").lower() == "search")
        floor = max(0.0, float(floor_ms or 0.0))
        # The rolled-over pairs, drawn up front at lane B's rollover_rate (see _humanize._human_type_text).
        roll_rate = rollover_rate(persona)
        rolls = frozenset(i for i in range(n - 1)
                          if floor <= 0 and native_kb_down and native_kb_up and text[i] in _ROLL_KEYS
                          and text[i + 1] in _ROLL_KEYS and text[i] != text[i + 1] and random.random() < roll_rate)
        # r43 engine (SC 232 lane H): a run with no slip to plan, no caller-held Shift and NO ROLLOVER goes out as
        # ONE dispatchKeySequence (see the sync file: the command cannot overlap keys, R3b); never with engine "off" (R3a).
        if (not allow and n > 1 and not ext_shift and not rolls and _hz_engine_mode(page, _engine_clicks_mode) != "off"
                and await _engine_ext_ok() and await _engine_type(text, floor)):
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
                    shift = True   # before the await: a keydown in flight when cancelled was still sent
                    await native_kb_down("Shift")
                    await asyncio.sleep(_shift_lead_s())
                typo = False
                consumed = False
                if allow and not need and not shift and typo_eligible(ch):
                    tp = plan_typo(text, i, random, rate)
                    if tp is not None:
                        typo = True
                        leave = search and tp["kind"] == "sub" and random.random() < SEARCH_SLIP_RATE
                        try:
                            consumed = await _do_typo(text, i, tp, leave)
                        except Exception as e:  # noqa: BLE001
                            if _is_strict(e):
                                raise
                # ROLLOVER: the next key goes down before this one comes up (see
                # _humanize._human_type_text). The pairs were drawn up front (``rolls``).
                nxt = text[i + 1] if i + 1 < n else None
                d1 = _kdwell()
                if (nxt is not None and not typo and not shift and native_kb_down and native_kb_up
                        and i in rolls and d1 - 8 >= 12):
                    overlap = _rand(12, min(55, d1 - 8))
                    # A key counts as down from the moment its keydown is SENT: a cancellation that
                    # lands while that call is awaited does not unsend it (measured on r37: a key left
                    # down after a cancel mid-call). Likewise it stops counting once its keyup is sent.
                    down = []
                    try:
                        down.append(ch)
                        await native_kb_down(ch)
                        await asyncio.sleep((d1 - overlap) / 1000.0)
                        down.append(nxt)
                        await native_kb_down(nxt)
                        await asyncio.sleep(overlap / 1000.0)
                        down.remove(ch)
                        await native_kb_up(ch)
                        await asyncio.sleep(max(5.0, _kdwell() - overlap) / 1000.0)
                        down.remove(nxt)
                        await native_kb_up(nxt)
                    except BaseException as e:  # noqa: BLE001
                        # Lift whatever is still down -- also when this task is CANCELLED mid-rollover
                        # (CancelledError is not an Exception, so the sync file's handler would miss
                        # it and strand the key). A cancellation is re-raised; an error ends the typing,
                        # as in the sync file. (A coroutine being closed may not await: re-raise.)
                        if isinstance(e, GeneratorExit):
                            raise

                        async def lift(keys=tuple(down)):
                            for k in keys:
                                try:
                                    await native_kb_up(k)
                                except Exception:  # noqa: BLE001
                                    pass
                        await _release(lift())
                        if not isinstance(e, Exception):
                            raise
                        break
                    i += 1          # nxt is typed; the cadence below runs as if after it
                    ch = nxt
                elif not consumed:
                    try:
                        await _emit_key(ch)
                    except Exception as e:  # noqa: BLE001
                        if _is_strict(e):
                            raise
                        break
                if shift and (i == n - 1 or not _needs_shift(text[i + 1])):
                    await asyncio.sleep(_shift_lag_s())
                    shift = False   # before the await: the keyup is sent even if cancelled mid-call
                    await native_kb_up("Shift")
                if i < n - 1:
                    await asyncio.sleep(max(floor, plan_gap(traits, ch, text[i + 1], random.random,
                                         getattr(persona, "tempo", 1.0), getattr(persona, "pause_scale", 1.0))) / 1000.0)
                i += 1
        finally:
            if shift:  # never leave Shift held for the caller's next keystroke (nor on cancellation)
                try:
                    await _release(native_kb_up("Shift"))
                except Exception:  # noqa: BLE001
                    pass
            if n:
                st["typed"] = time.monotonic()   # a following Enter / Tab waits (_after_typing)
            if outer_rec is None and st.get("keyrec") is not None:
                _trace.record("typed", st.pop("keyrec"), {"n": n, "typos": allow, "floor_ms": floor})

    async def _after_typing(key):
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
            await asyncio.sleep(rest)

    async def _hold_key(k, delay=None, repeat=1):
        """One key: pressed with the persona's hold (or the caller's ``delay``); with ``repeat`` > 1 HELD
        the way an OS auto-repeats it -- one keydown, the machine's repeat delay (250-500 ms, a persona
        trait), then a keydown with repeat=true at its repeat interval (25-40 ms, likewise; Playwright
        sends a key that is already down as an autoRepeat keydown), ``repeat`` keydowns in all, then one
        keyup."""
        repeat = max(1, int(repeat or 1))
        if repeat == 1 or native_kb_down is None or native_kb_up is None:
            for j in range(repeat):
                await native_kb_press(k, delay=delay if delay and delay > 0 else _kdwell())
                if j < repeat - 1:
                    await asyncio.sleep(_rand(60, 150) / 1000.0)
            return
        rp = plan_repeat(traits, random.random)
        try:
            await native_kb_down(k)   # inside the try: a keydown in flight when cancelled was still sent
            await asyncio.sleep(rp.first_ms / 1000.0)
            for j in range(repeat - 1):
                if j:
                    await asyncio.sleep(rp.gap_ms / 1000.0)
                await native_kb_down(k)
            await asyncio.sleep(rp.release_ms / 1000.0)
        finally:
            await _release(native_kb_up(k))   # never leave the key held (also when cancelled)

    async def _press_key(key, delay=None, repeat=1):
        """keyboard.press, humanized: a chord ("Control+a", "ControlOrMeta+Shift+ArrowLeft") is split
        into its modifiers and its key -- each modifier goes down in order, 40-110 ms before the next,
        the key is held, 20-80 ms later the modifiers come up in reverse order. As one Playwright press
        the modifier and the key went down within ~1 ms of each other. A modifier the caller already
        holds (keyboard.down) is not pressed again. Playwright's key names are kept (ControlOrMeta is
        resolved by Playwright, for the engine's platform)."""
        mods, k = split_chord(key)
        mods = [m for m in mods if m not in held_keys]
        if not mods:
            return await _hold_key(k, delay, repeat)
        if native_kb_down is None or native_kb_up is None:
            return await native_kb_press(key, delay=delay if delay and delay > 0 else _kdwell())
        down = []
        try:
            for m in mods:
                down.append(m)
                await native_kb_down(m)
                await asyncio.sleep(modifier_lead_ms(random.random) / 1000.0)
            await _hold_key(k, delay, repeat)
            await asyncio.sleep(modifier_lag_ms(random.random) / 1000.0)
        finally:
            for j, m in enumerate(reversed(down)):
                if j:
                    await asyncio.sleep(_rand(10, 40) / 1000.0)
                try:
                    await _release(native_kb_up(m))   # one failed keyup must not strand the others
                except Exception:  # noqa: BLE001
                    pass

    async def hkb_type(text, **kw):
        await _pointer_presence()
        await _human_type_text(text, floor_ms=kw.get("delay") or 0.0, exact=False)

    async def hkb_press(key, **kw):
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
        await _pointer_presence()
        repeat = kw.pop("repeat", 1)
        await _after_typing(key)
        d = kw.get("delay")
        return await _press_key(key, d if d and d > 0 else None, repeat)

    async def hkb_down(key):
        """keyboard.down, tracked: the chord model and typing honour a modifier the caller holds, and
        hkb_up gives the key at least a persona hold."""
        await _pointer_presence()
        if key not in held_keys:
            held_keys[key] = (time.monotonic(), _kdwell())
        return await native_kb_down(key)

    async def hkb_up(key):
        t = held_keys.pop(key, None)
        if t is not None:
            rest = t[1] / 1000.0 - (time.monotonic() - t[0])
            if rest > 0:
                await asyncio.sleep(rest)   # a down() then an up() at once: no finger lifts a key in 0 ms
        return await native_kb_up(key)

    kb.type = _serial(hkb_type)
    kb.press = _serial(hkb_press)
    if native_kb_down is not None and native_kb_up is not None:
        kb.down, kb.up = _serial(hkb_down), _serial(hkb_up)

    # ------------------------------------------------------- page-level targeted helpers
    # Points tried when resolving a control through the browser's hit test: a floating label or an
    # icon often sits over a field's CENTRE, so the edges get a chance too.
    probe_points = ((0.5, 0.5), (0.82, 0.5), (0.18, 0.5), (0.5, 0.8), (0.5, 0.2))

    def _aim_w(box):
        """The Fitts target width of a glide onto ``box``: its smaller side (floor 6 px). Every targeted
        glide passed 24 whatever the box, so duration did not follow the target's size."""
        return max(6.0, min(float(box["width"]), float(box["height"])))

    async def _proven_click_point(box, sel=None):
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
            if await _hit_in_box(box, x, y, sel) is True:
                return (x, y)
        return None

    async def _describe(box, want=FORM_CONTROL):
        """The control filling ``box`` (Playwright's resolved box for a locator), read in the isolated
        world through the browser's own hit test (DOM.getNodeForLocation in DOCUMENT coordinates, then
        DESCRIBE on that node). The element found must fill the SAME box (±1.5 px), or it is not the
        one the locator means; None when no probe point proves that."""
        if not box or box["width"] <= 0 or box["height"] <= 0:
            return None
        w = async_world_for(page)
        scroll = await w.scroll()   # once: the hit test wants document coordinates
        for fx, fy in probe_points:
            nid = await w.node_at(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy, scroll)
            if nid is None:
                continue
            info = await w.call_on_node(nid, ISO_DESCRIBE, {"want": want})
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

    async def _redescribe(info, want=FORM_CONTROL):
        """Describe the SAME element again, from the node the first hit test found: no new
        Playwright handle (see _box_of), so nothing the page can see. None if it is gone."""
        again = await async_world_for(page).call_on_node(info["node"], ISO_DESCRIBE, {"want": want})
        if again:
            again["node"] = info["node"]
            again["probe"] = info.get("probe", (0.5, 0.5))
        return again

    async def _in_view(box):
        vw, vh = await _viewport()
        return box["x"] >= 0 and box["y"] >= 0 and box["x"] + box["width"] <= vw and box["y"] + box["height"] <= vh

    def _rect_box(r):
        if not r or not r.get("rect"):
            return None
        x, y, w, h = r["rect"]
        return {"x": x, "y": y, "width": w, "height": h} if w > 0 and h > 0 else None

    async def _css_box(selector):
        """The box of a plain-CSS selector's first match, read (and scrolled into view) in the
        isolated world: not even the one bounding_box() handle, and its textContent preview, is made.
        Used only when Playwright counts exactly the elements the browser's own querySelectorAll
        finds -- Playwright's CSS also pierces open shadow roots, so equal counts mean the same
        elements in the same order. None otherwise (get_by_role, text=, `>>` chains, :has-text()...:
        then Playwright's bounding_box). See _humanize._css_box."""
        if not isinstance(selector, str) or not selector:
            return None
        w = async_world_for(page)
        r = await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return None
        try:
            if await page.locator(selector).count() != r["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        box = _rect_box(r)
        if box and not (await _in_view(box) and await _hit_ok(box, selector)):
            # scrollIntoViewIfNeeded also scrolls a scrolling container that clips it, and does
            # nothing when it already shows (then the cover is an overlay: the click's own check)
            box = _rect_box(await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        return box

    async def _hit_in_box(box, x, y, sel=None):
        """Does a press at viewport (x, y) reach the element filling ``box``? True / False, or None
        when it cannot be told here (CDP is unavailable). A point inside a child frame is answered
        by the <iframe> element that holds it."""
        w = async_world_for(page)
        nid = await w.node_at(x, y)
        if nid is None:
            return None
        b = {"x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}
        if sel:
            b["sel"] = sel   # a plain-CSS selector: the exact answer (see HIT_IN_BOX)
        res = await w.call_on_node(nid, ISO_HIT_IN_BOX, b)
        return None if res is None else bool(res)

    async def _hit_ok(box, sel=None):
        """Does the element filling ``box`` own its centre -- not clipped by a scrolling container,
        not covered? The browser's own hit test, read in the isolated world."""
        return await _hit_in_box(box, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, sel) is True

    async def _scroll_window_to(box):
        """Scroll the WINDOW so ``box`` sits centred where it was off-screen, the way Playwright's
        scroll centres it, and return where the box is now (moved by exactly the scrolled distance) --
        or None. Needs no handle: the box is already known."""
        vw, vh = await _viewport()
        if box["width"] > vw or box["height"] > vh:
            return None
        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        dx = cx - vw / 2 if box["x"] < 0 or box["x"] + box["width"] > vw else 0
        dy = cy - vh / 2 if box["y"] < 0 or box["y"] + box["height"] > vh else 0
        moved = await async_world_for(page).evaluate(ISO_SCROLL_BY, [dx, dy])
        if not isinstance(moved, list) or len(moved) != 2:
            return None
        return {"x": box["x"] - moved[0], "y": box["y"] - moved[1], "width": box["width"], "height": box["height"]}


    async def _css_plain(selector):
        if not isinstance(selector, str) or not selector:
            return False
        r = await async_world_for(page).evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})
        if not r or not r.get("n"):
            return False
        try:
            return await page.locator(selector).count() == r["n"]
        except Exception:  # noqa: BLE001
            return False

    async def _wheel_into_view(loc, selector, css, box):
        """See _humanize._wheel_into_view."""
        w = async_world_for(page)

        s0 = await w.scroll()

        async def box_fn():
            if css:
                return _rect_box(await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False}))
            # no new Playwright handle per read (each one's preview reads textContent in the page's
            # world): the box the caller already has, moved by how far the window has scrolled since
            sx, sy = await w.scroll()
            return {"x": box["x"] - (sx - s0[0]), "y": box["y"] - (sy - s0[1]),
                    "width": box["width"], "height": box["height"]}

        async def scroller_fn():
            r = await w.evaluate(ISO_SCROLLER, {"sel": selector})
            return r if isinstance(r, dict) and r.get("rect") else None

        got, ok, notches = await bring_into_view_async(_scroll_io(), box_fn, scroller_fn=scroller_fn if css else None)
        if not ok:
            return None, ("notch_budget" if notches >= 60 else "not_scrollable"), got
        if not (await _in_view(got) and await _hit_ok(got, selector)):
            return None, "covered", got
        return got, None, got

    async def _needs_scroll(box, selector, css):
        if not await _in_view(box):
            return True
        if css:
            r = await async_world_for(page).evaluate(ISO_SCROLLER, {"sel": selector})
            if isinstance(r, dict) and r.get("rect"):
                x, y, cw, ch = r["rect"]
                return not _box_in(box, (x, y, cw, ch))
        return False

    async def _box_of(loc, timeout, force=False, selector=None, wheel=True):
        """Wait the way Playwright would, with the CALLER's timeout (None = the page default), and
        return the box, in view, brought there with the WHEEL; the programmatic scroll is only the
        reported fallback (see _humanize._box_of)."""
        if not await loc.is_visible():
            if force:
                raise _Native("forced on a hidden target")
            await loc.wait_for(state="visible", timeout=timeout)
        css = await _css_plain(selector)
        w = async_world_for(page)
        box = _rect_box(await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False})) if css \
            else await loc.bounding_box()
        if not box:
            return box
        if wheel and await _needs_scroll(box, selector, css):
            got, why, last = await _wheel_into_view(loc, selector, css, box)
            if got is not None:
                return got
            if why != "covered":
                _fallback("scroll_into_view", "scroll into view: " + why)
            box = last or box
        if await _in_view(box) and await _hit_ok(box, selector):
            return box
        if css:
            return _rect_box(await w.evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": True}))
        moved = await _scroll_window_to(box) if not await _in_view(box) else None
        if moved and await _in_view(moved) and await _hit_ok(moved, selector):
            return moved
        await loc.scroll_into_view_if_needed(timeout=timeout)
        return await loc.bounding_box()

    async def _focused(loc):
        """Does ``loc`` hold the keyboard focus? Asked through Playwright's utility-world selector
        engine (`:focus`), so it works for every element and every locator and the page sees nothing.
        The isolated hit test only knew form controls, so a focused BUTTON read as unfocused and got
        clicked before a press -- two activations."""
        try:
            return await loc.and_(page.locator(":focus")).count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def _is_focused(selector):
        return await _focused(page.locator(selector).first)

    async def _text_entry(loc, timeout, selector=None):
        """(box, info) when ``loc`` is a visible text-entry control a person would click into before
        typing, else None. Anything else -- a button, a link, a hidden helper textarea, a slider -- is
        left to Playwright, which focuses it without a click (a click on a button activates it; on a
        slider it moves it)."""
        if not await loc.is_visible():
            return None
        box = await _box_of(loc, timeout, selector=selector)
        info = await _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            return box, info
        return None

    async def _focus_click(box, at=None, loc=None, selector=None):
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
        await asyncio.sleep(0.05)
        info = await _describe(box)
        if info is None:
            again = await _css_box(selector) if selector else None
            if again is None and loc is not None:
                again = await loc.bounding_box()
            if not again or abs(again["x"] - box["x"]) > 1 or abs(again["y"] - box["y"]) > 1:
                return False
        elif info.get("disabled"):
            return False
        pt = at if at is not None else click_point(box, st["pos"], persona)  # 2D gaussian toward centre
        pt = (float(round(pt[0])), float(round(pt[1])))   # the pixel the hit test answered for
        await _glide(pt[0], pt[1], target_w=_aim_w(box))
        await _press_here(pt[0], pt[1])
        return True

    async def _key(k, n=1, gap=(60, 150)):
        for j in range(n):
            await native_kb_press(k, delay=_kdwell())
            if j < n - 1:
                await asyncio.sleep(pause_ms(persona, *gap) / 1000.0)

    async def _select_all():
        """Ctrl+A (Cmd+A on a Mac engine) the way a hand does it: the modifier goes down a moment
        before the A, the A is held, the modifier comes up just after (as one Playwright chord the two
        went down within ~1 ms). ControlOrMeta lets Playwright pick the chord the ENGINE's editing
        honours: a Meta+A sent to a Linux engine selects nothing."""
        if native_kb_down is None or native_kb_up is None:
            await _key("ControlOrMeta+a")
            return
        try:
            await native_kb_down("ControlOrMeta")   # inside the try: sent even if cancelled mid-call
            await asyncio.sleep(_rand(40, 110) / 1000.0)
            await native_kb_press("a", delay=_kdwell())
            await asyncio.sleep(_rand(20, 80) / 1000.0)
        finally:
            await _release(native_kb_up("ControlOrMeta"))   # also when cancelled mid-chord

    async def _clear_focused(read=None):
        """Select all, then Backspace -- each key HELD like a finger holds it (never ~1 ms). If the
        field is still not empty (``read`` -- the field's value -- says so), it is emptied from the
        end, one Backspace per character (≤ 64)."""
        read = read or _active_value
        await _select_all()
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        await _key("Backspace")
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        left = await read()
        if left:
            if len(left) > 64:
                raise RuntimeError("field would not clear")
            await _key("End")
            await asyncio.sleep(_rand(80, 180) / 1000.0)
            await _key("Backspace", len(left), gap=(55, 120))
            await asyncio.sleep(_rand(60, 160) / 1000.0)
            if await read():
                raise RuntimeError("field would not clear")

    # getattr: a page-like object without one of these still gets every OTHER humanization (the
    # async fakes in the test suite implement only a few page methods).
    native_page_click = getattr(page, "click", None)
    native_page_hover = getattr(page, "hover", None)
    native_page_type = getattr(page, "type", None)
    native_page_fill = getattr(page, "fill", None)
    native_page_press = getattr(page, "press", None)
    native_page_dblclick = getattr(page, "dblclick", None)
    native_page_select_option = getattr(page, "select_option", None)

    async def _tab_distance(info, selector=None):
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
            return await async_world_for(page).call_on_node(info["node"], TAB_DISTANCE, arg)
        except Exception:  # noqa: BLE001
            return None

    async def _tab_steps(d):
        """Press Tab ``d`` times (Shift+Tab when ``d`` < 0), each with a hold, the first after the
        persona's Tab pause when it follows typing, the rest 110-260 ms apart (the eye follows the
        focus ring)."""
        key = "Tab" if d > 0 else "Shift+Tab"
        for j in range(abs(d)):
            if j == 0:
                await _after_typing(key)
            else:
                await asyncio.sleep(_rand(110, 260) / 1000.0)
            await _press_key(key)

    async def _tab_into(loc, info, selector=None, need_empty=False, force=False):
        """Tab into the control ``info`` instead of clicking it, the way a person moves between
        adjacent fields: when focus sits in a TEXT field 1-3 Tab stops before it (or 1 after: Shift+Tab),
        in the persona's share of cases (60-85 %, _keys.typing_traits.tab_pref). ``force``: any focused
        element, up to 3 stops either way, no roll (a button a press goes to). ``need_empty``: only an
        empty field -- Tab selects a field's text, and type() appends where a click leaves the caret.
        True once focus is verified there; False (nothing pressed, or focus went elsewhere) = click."""
        if native_kb_press is None or (need_empty and info and info.get("value")):
            return False
        td = await _tab_distance(info, selector)
        if not td or td.get("dist") is None or td.get("focused"):
            return False
        d = td["dist"]
        if force:
            if not (1 <= abs(d) <= 3):
                return False
        else:
            if not td.get("fromText") or not (1 <= d <= 3 or d == -1) or random.random() >= _tab_pref():
                return False
        await _tab_steps(d)
        await asyncio.sleep(_rand(40, 120) / 1000.0)
        return await _focused(loc)

    async def _click_into(selector, timeout, tab=True):
        """Make ``selector`` the focus the way a person does -- Tab from the field before it (_tab_into)
        or a click --, unless it already is. Only text-entry controls are clicked; for anything else, and
        when the click did not actually put the caret there (an overlay took it), the caller goes
        native. Tab only into an EMPTY field: type() appends at the caret, and Tab selects the text."""
        loc = page.locator(selector).first
        if await _focused(loc):
            return
        te = await _text_entry(loc, timeout, selector)
        if te is None:
            raise _Native("not a visible text field")
        if tab and await _tab_into(loc, te[1], selector, need_empty=True):
            return
        if not await _focus_click(te[0], _proven_point(*te), loc, selector) or not await _focused(loc):
            raise _Native("the click did not put the caret in the field")
        await asyncio.sleep(_rand(40, 120) / 1000.0)

    async def _tab_via_field(loc, info, selector, td):
        """See _humanize._tab_via_field: a click into the text stop 1-3 before the control (or 1 after),
        then Tab across; True once focus is verified on it."""
        near = (td or {}).get("near")
        if not near or not near.get("rect") or native_kb_press is None:
            return False
        r = near["rect"]
        nbox = {"x": float(r[0]), "y": float(r[1]), "width": float(r[2]), "height": float(r[3])}
        if not await _focus_click(nbox):
            return False
        await asyncio.sleep(_rand(40, 120) / 1000.0)
        return await _tab_into(loc, info, selector, force=True)

    async def _tab_from_start(loc, info, selector):
        """See _humanize._tab_from_start: nothing focused -- one Tab enters the page, then the control is
        reached when it is within 3 stops of the focus ring."""
        if native_kb_press is None:
            return False
        await _press_key("Tab")
        await asyncio.sleep(_rand(110, 260) / 1000.0)   # the eye finds the focus ring
        if await _focused(loc):
            return True
        return await _tab_into(loc, info, selector, force=True)

    async def _focus_target(selector, timeout):
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
        if await _focused(loc):
            return True
        box = await _box_of(loc, timeout, selector=selector)
        info = await _describe(box, FOCUSABLE) if box else None
        if info is None:
            raise _Native("the target cannot be described (no box)")
        td = await _tab_distance(info, selector)
        act = td is None or bool(td.get("activatable"))   # unknown: never risk an activating click
        if td and td.get("dist") is not None and 1 <= abs(td["dist"]) <= 3:
            if act or random.random() < _tab_pref():
                if await _tab_into(loc, info, selector, force=True):
                    return True
        if not act and not info.get("disabled"):
            if await _focus_click(box, _proven_point(box, info), loc, selector) and await _focused(loc):
                await asyncio.sleep(_rand(40, 120) / 1000.0)
                return True
            raise _Native("the focus click did not focus it")
        if act and td and not info.get("disabled"):
            # SC 232 lab triage (10-16): see _humanize._focus_target
            if await _tab_via_field(loc, info, selector, td):
                return True
            if td.get("noAnchor") and await _tab_from_start(loc, info, selector):
                return True
        x, y = click_point(box, st["pos"], persona)
        await _glide(float(round(x)), float(round(y)))   # the hand goes to it; the focus has to be given
        return False

    async def htype(selector, text, **options):
        try:
            await _click_into(selector, options.get("timeout"))
            await _human_type_text(text, floor_ms=options.get("delay") or 0.0, exact=False)
            return None
        except _Native as e:
            why = str(e)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.type", why, selector)
        # outside the try: a native call that raises must raise ONCE, not be retried by the handler
        return await native_page_type(selector, text, **options)

    async def _fill_text(loc, box, value, info, selector=None):
        """Type ``value`` into a text control and PROVE it landed. The value is read through
        Playwright's utility world (`input_value`): it sees the control the locator means, wherever
        focus is, and the page cannot observe it. A field whose value cannot be read, or a click that
        did not put the caret in it (an overlay took it), is left to Playwright -- "could not check"
        is never treated as success."""
        is_ce = bool(info and info.get("ce")) or (
            info is None and ((await loc.get_attribute("contenteditable")) or "false") != "false")
        # What the field can hold (maxlength), and bulk values inserted after the click: see
        # _humanize._fill_text.
        if is_ce:
            want, limit = value, (None, None, None)
        elif info is not None:
            limit = (info.get("maxlength"), info.get("tag"), info.get("type"))
            want = fit_maxlength(value, *limit)
        else:
            limit = (await loc.get_attribute("maxlength"), await _classify_tag(loc),
                     (await loc.get_attribute("type")) or "")
            want = fit_maxlength(value, *limit)
        bulk = len(want) > 200
        if bulk and native_kb_insert is None:
            return False
        # From the field before it, a person Tabs (persona share, focus verified); else a click.
        if not (info is not None and await _tab_into(loc, info, selector)):
            if not await _focus_click(box, _proven_point(box, info) if box else None, loc, selector):
                return False
            await asyncio.sleep(_rand(40, 120) / 1000.0)
            if not await _focused(loc):
                return False

        async def read():
            try:
                return await loc.input_value(timeout=COVER_RETRY_MS)
            except Exception:  # noqa: BLE001
                return None

        async def put(text, typos=True):
            if bulk:
                await asyncio.sleep(_rand(120, 320) / 1000.0)   # the paste shortcut, after the click
                await native_kb_insert(text)
            else:
                await _human_type_text(text, field=info, typos=typos)

        if is_ce:
            cur = info.get("value") if info else None
            if cur:
                await _clear_focused()
            await put(want)
            return True   # contenteditable: Playwright's fill sets text the same way; no value to read
        cur = await read()
        if cur is None:
            return False
        if cur:  # a person does not select-all an empty field
            await _clear_focused(read)
        await put(want)
        got = await read()
        if got is None:
            return False
        if got == want or formatted_equal(got, want) or mask_kept(got, want, *limit):
            return True   # exact, re-punctuated, or re-punctuated and full (see _humanize._fill_text)
        if bulk:
            return False   # an inserted value that did not land: Playwright's fill, not a second paste
        # Wrong value (an autocompleter interfered, a key was lost): one clean retry without slips,
        # then give the field to Playwright -- a correct value beats pretty input.
        await _clear_focused(read)
        await put(want, typos=False)
        got = await read()
        return got is not None and (got == want or formatted_equal(got, want) or mask_kept(got, want, *limit))

    async def _fill_range(loc, box, info, value):
        """A range input: drag the thumb (or click the track) to the value, then arrow-nudge to it
        exactly (see _humanize._fill_range)."""
        if info is None:
            info = {"min": await loc.get_attribute("min"), "max": await loc.get_attribute("max"),
                    "step": await loc.get_attribute("step"), "value": await loc.input_value(),
                    "dir": "ltr", "wm": ""}
        if not range_value_ok(value, info.get("min"), info.get("max"), info.get("step")):
            # Off the step, out of range, not a number: Playwright raises "Malformed value" for these,
            # so does this route (by leaving them to it) -- never a silently snapped or clamped value.
            return False
        if (info.get("wm") or "").startswith("vertical"):
            return False
        rtl = info.get("dir") == "rtl"
        lo, hi, step = range_attrs(info.get("min"), info.get("max"), info.get("step"))
        target = range_sanitize(value, info.get("min"), info.get("max"), info.get("step"))

        async def now():
            try:
                return float(await loc.input_value())
            except (TypeError, ValueError):
                return None

        cur = await now()
        if cur is not None and numeric_equal(cur, target):
            return True
        tx, ty = range_point(box, range_fraction(target, lo, hi), rtl)
        ty += random.gauss(0, box["height"] * 0.08)
        if cur is not None and native_down and native_up and random.random() < 0.65:
            # Grab the thumb where it is and slide it: what most hands do with a slider.
            cx, cy = range_point(box, range_fraction(cur, lo, hi), rtl)
            cy += random.gauss(0, box["height"] * 0.08)
            await _glide(cx, cy, target_w=16)
            await _pre_press()
            grab_ms, release_ms = drag_dwell(persona)
            x0 = range_point(box, range_fraction(cur, lo, hi), rtl)[0]
            span = abs(range_point(box, 1.0, False)[0] - range_point(box, 0.0, False)[0])
            step_px = span * (step / (hi - lo)) if (step and hi > lo) else 1.0

            async def moved():
                v = await now()
                return None if v is None else (range_point(box, range_fraction(v, lo, hi), rtl)[0] - x0, 0.0)

            st["held"] = True
            try:
                await native_down()   # inside the try: a press in flight when cancelled was still sent
                # closed loop (drag_by): past the value by a few px, read the value, correct
                await _drag_by_core(cx, cy, tx - x0, 0.0, moved, max(1.0, step_px / 2.0), grab_ms)
                await asyncio.sleep(release_ms / 1000.0)
            finally:
                st["held"] = False
                await _release(native_up())   # the thumb is let go even when cancelled mid-drag
        else:
            await _glide(tx, ty, target_w=16)
            await _press_here(tx, ty)
        for _ in range(3):
            v = await now()
            if v is None:
                return False
            if numeric_equal(v, target):
                return True
            key, n = range_nudge(v, target, step)
            if key is None or n <= 0 or n > 40:
                break
            if rtl:
                key = "ArrowLeft" if key == "ArrowRight" else "ArrowRight"
            await asyncio.sleep(_rand(150, 350) / 1000.0)
            await _key(key, n)
        v = await now()
        return v is not None and numeric_equal(v, target)

    async def _fill_datetime(loc, box, info, value):
        """A native date / time / month / week / datetime-local editor: click into its first field
        and type each field in the order the editor itself shows them (read from its UA shadow tree
        over CDP -- the order follows the browser, never assumed). See _humanize._fill_datetime."""
        if not info or not info.get("node"):
            return False
        kind = info.get("type")
        w = async_world_for(page)
        tree = await w.send("DOM.describeNode", {"backendNodeId": info["node"], "depth": -1, "pierce": True})
        if not tree or "node" not in tree:
            return False
        segs = segment_nodes_from_tree(tree["node"])
        plan = plan_datetime(kind, [s for s, _ in segs], value, current=info.get("value"))
        if not plan or not segs:
            return False
        px, py, fw = None, None, 12.0
        bm = await w.send("DOM.getBoxModel", {"backendNodeId": segs[0][1]}) if segs[0][1] else None
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
        await _glide(px, py, target_w=fw)
        await _press_here(px, py)
        await asyncio.sleep(pause_ms(persona, 150, 350) / 1000.0)
        for a in plan:
            if a[0] == "type":
                # the text typing model, rollover included, no typos (see _humanize._fill_datetime)
                await _human_type_text(a[1], typos=False)
            else:
                await _key(a[1], a[2] if len(a) > 2 else 1, gap=(70, 170))
            await asyncio.sleep(pause_ms(persona, 60, 220) / 1000.0)   # between fields: the eye checks what it typed
        return datetime_equal(kind, await loc.input_value(), value)

    async def _classify_tag(loc):
        """'textarea' | 'input' | None, through Playwright's utility-world queries."""
        for tag in ("textarea", "input"):
            if await loc.locator("xpath=self::%s" % tag).count():
                return tag
        return None

    async def _classify_fallback(loc):
        """Classify through Playwright's utility-world queries when the hit test could not prove the
        element (it is covered, or inside a frame): attributes only, nothing the page can see."""
        for tag in ("textarea", "select"):
            if await loc.locator("xpath=self::%s" % tag).count():
                return classify_field(tag)
        if await loc.locator("xpath=self::input").count():
            return classify_field("input", await loc.get_attribute("type"))
        ce = await loc.get_attribute("contenteditable")
        return classify_field("div", None, ce is not None and ce.lower() != "false")

    async def hfill(selector, value, **options):
        why = "the value is not a string"
        try:
            if isinstance(value, str):
                timeout = options.get("timeout")   # None: the page's default, as Playwright would use
                loc = page.locator(selector).first
                box = await _box_of(loc, timeout, options.get("force"), selector)
                info = await _describe(box)
                kind = (classify_field(info["tag"], info["type"], info["ce"]) if info
                        else await _classify_fallback(loc))
                why = ("the field is disabled or read-only" if info and (info.get("disabled") or info.get("readonly"))
                       else "the %s route did not verify" % kind if kind in ("text", "range", "datetime")
                       else "no humane route for a %s field" % kind)
                if not (info and (info.get("disabled") or info.get("readonly"))):
                    if kind == "text" and await _fill_text(loc, box, value, info, selector):
                        return None
                    if kind == "range" and await _fill_range(loc, box, info, value):
                        return None
                    if kind == "datetime" and await _fill_datetime(loc, box, info, value):
                        return None
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = str(e) if isinstance(e, _Native) else "error: %s" % (e,)
        # Native: color (Playwright sets it -- the OS dialog is no route), unfillable types (Playwright
        # raises, as before humanize), and anything the humane route could not prove it did right.
        _report_fallback(page, "Page.fill", why, selector)
        return await native_page_fill(selector, value, **options)

    async def hdblclick(selector, **options):
        o = dict(options)
        o.setdefault("click_count", 2)
        return await _targeted_click(native_page_dblclick, selector, o, no_click=False, native_opts=options)

    native_page_focus = getattr(page, "focus", None)

    def _fallback(api, reason, selector=None):
        """A humanized route hands this call to Playwright: lane A's report (strict mode raises
        ClearcoteStrictError and nothing is sent; otherwise on_fallback hears it). Never swallowed."""
        return _report_fallback(page, api, reason, selector)

    async def _focus_for_press(selector, timeout):
        """Give ``selector`` the focus before a key goes to it. A text field: Tab from the field before
        it (only when empty: Tab selects the text) or a click into it. Anything else -- a button, a
        link, a tabindex container -- through _focus_target (Tab within 3 stops, or a click that only
        focuses); an activatable target out of Tab reach gets the pointer over it and the focus from
        Playwright (reported), and the key is still pressed with a human hold."""
        loc = page.locator(selector).first
        if await _focused(loc):
            return
        if not await loc.is_visible():
            raise _Native("not visible")   # Playwright waits for it
        box = await _box_of(loc, timeout, selector=selector)
        info = await _describe(box)
        if info and classify_field(info["tag"], info["type"], info["ce"]) == "text":
            if await _tab_into(loc, info, selector, need_empty=True):
                return
            if not await _focus_click(box, _proven_point(box, info), loc, selector) or not await _focused(loc):
                raise _Native("the click did not put the caret in the field")
            await asyncio.sleep(_rand(40, 120) / 1000.0)
            return
        if await _focus_target(selector, timeout):
            return
        if native_page_focus is None:
            raise _Native("no humane focus route (out of Tab reach, no page.focus)")
        _fallback("Page.press", "focus: an activatable target out of Tab reach")
        await native_page_focus(selector, **({"timeout": timeout} if timeout is not None else {}))
        await asyncio.sleep(_rand(40, 120) / 1000.0)

    async def hpress(selector, key, **options):
        """page.press (and Locator.press): the target gets the focus the way a person gives it
        (_focus_for_press: never Playwright's focus-and-press in 0 ms), then the key is pressed through
        the keyboard model -- a chord modifier first, the persona's hold (or the caller's `delay`),
        ``repeat=n`` as OS auto-repeat, and the submit / Tab pause after typing."""
        repeat = options.pop("repeat", 1)
        try:
            await _focus_for_press(selector, options.get("timeout"))
            await _after_typing(key)
            d = options.get("delay")
            await _press_key(key, d if d and d > 0 else None, repeat)
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
            r = await native_page_press(selector, key, **options)
        return r

    async def hfocus(selector, **options):
        """page.focus / Locator.focus: Tab or a click (_focus_target), verified; Playwright's focus for
        an activatable target out of Tab reach (reported) and for anything the humane route cannot do."""
        try:
            if await _focus_target(selector, options.get("timeout")):
                return None
            _fallback("Page.focus", "an activatable target out of Tab reach")
        except _Native:
            _fallback("Page.focus", "native focus")
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
        return await native_page_focus(selector, **options)

    async def _blur(selector, options):
        """Locator.blur, humanized: a focused element is left the way the persona leaves fields -- Tab
        to the next stop, or a click on a blank point beside it (_keys.BLANK_POINT; Tab when there is
        none). True when focus is verified gone; False = Playwright's blur (nothing to blur, or the
        humane route did not take the focus away)."""
        loc = page.locator(selector).first
        if not await _focused(loc):
            return False
        if traits.blur_style == "click" and native_down is not None and native_up is not None:
            box = await _box_of(loc, options.get("timeout"), selector=selector)
            vw, vh = await _viewport()
            pt = await async_world_for(page).evaluate(BLANK_POINT, {"box": box, "vw": vw, "vh": vh}) if box else None
            if isinstance(pt, list) and len(pt) == 2:
                x, y = float(round(pt[0] + random.gauss(0, 3))), float(round(pt[1] + random.gauss(0, 3)))
                await _glide(x, y, target_w=60)
                await _press_here(x, y)
                await asyncio.sleep(_rand(40, 120) / 1000.0)
                if not await _focused(loc):
                    return True
        await _after_typing("Tab")
        await _press_key("Tab")
        await asyncio.sleep(_rand(40, 120) / 1000.0)
        return not await _focused(loc)

    async def _selected_all():
        s = await async_world_for(page).evaluate(SELECTION_STATE)
        return isinstance(s, list) and len(s) == 3 and min(s[0], s[1]) == 0 and max(s[0], s[1]) == s[2]

    async def _select_text(selector, options):
        """Locator.select_text, humanized: a text control's text selected the way the persona does it --
        a triple click (an <input>), or the keys: End then Shift+Home (Ctrl+End, Ctrl+Shift+Home in a
        textarea; Cmd+A on macOS, where Home/End do not move the caret). Verified in the isolated world
        (SELECTION_STATE), one select-all retry; False = Playwright's select_text."""
        loc = page.locator(selector).first
        box = await _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = await _describe(box) if box else None
        if not info or info.get("disabled") or classify_field(info["tag"], info["type"], info["ce"]) != "text":
            return False
        tag = info["tag"]
        if traits.select_style == "triple" and tag == "input" and native_down is not None and native_up is not None:
            at = _proven_point(box, info)
            x, y = at if at is not None else click_point(box, st["pos"], persona)
            x, y = float(round(x)), float(round(y))
            await _glide(x, y)
            await _press_at(x, y, normalize_click_options({"click_count": 3}, await _platform()))
        else:
            if not await _focused(loc):
                if not await _focus_click(box, _proven_point(box, info), loc, selector) or not await _focused(loc):
                    return False
                await asyncio.sleep(_rand(120, 300) / 1000.0)
            if _host_platform() == "macos":
                await _press_key("ControlOrMeta+a")
            elif tag == "input":
                await _press_key("End")
                await asyncio.sleep(_rand(80, 200) / 1000.0)
                await _press_key("Shift+Home")
            else:
                await _press_key("ControlOrMeta+End")
                await asyncio.sleep(_rand(80, 200) / 1000.0)
                await _press_key("ControlOrMeta+Shift+Home")
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        if await _selected_all():
            return True
        await _select_all()
        await asyncio.sleep(_rand(60, 160) / 1000.0)
        return await _selected_all()

    def _guarded(api, fn, native):
        """Run the humane ``fn`` (True = done); Playwright's ``native`` when it declines or fails
        (a timeout propagates)."""
        async def call(selector, **options):
            try:
                if await fn(selector, options):
                    return None
            except _Native:
                pass
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise
            _fallback(api, "native")
            return await native(selector, **options)
        return call

    page._clearcote_blur = _serial(_guarded(
        "Locator.blur", _blur, lambda s, **o: _LOCATOR_NATIVE["blur"](page.locator(s).first, **o)))
    page._clearcote_select_text = _serial(_guarded(
        "Locator.select_text", _select_text,
        lambda s, **o: _LOCATOR_NATIVE["select_text"](page.locator(s).first, **o)))

    async def _open_and_pick(selector, plan, at=None, dl=None):
        """Open the drop-down with a click, move its highlight with the planned keys, commit with
        Enter: one trusted input+change. While the popup is open its keys never reach the page
        (measured on r37: value unchanged until Enter). The opening click is the full humanized click
        -- including the covered-point check -- at the point the hit test proved when the centre is
        covered (``at``, a Playwright `position`), so a floating label over the select's centre
        cannot swallow it and leave the letters to change a closed select one per key. ``dl`` is the
        select's deadline (select_deadline); True when the commit navigated the page (_commit_pick)."""
        await _targeted_click(native_page_click, selector, {"position": at} if at else {}, no_click=False,
                              native_opts=None, move_off=False)
        # Every pause here is the persona's (timing_traits.pause_scale): the windows were one fixed
        # set for every identity.
        await asyncio.sleep(pause_ms(persona, 220, 480) / 1000.0)        # the list opens; the eye finds the option
        pace = timing_traits(persona).pause_scale * tempo   # the persona's deliberation x the preset
        for ch in plan["keys"]:
            await native_kb_press(ch, delay=_kdwell())
            await asyncio.sleep(max(0.04, random.gauss(0.10, 0.035) * pace))   # well inside type-ahead's 1 s window
        n = plan["arrows"]
        if n:
            await asyncio.sleep(pause_ms(persona, 120, 300) / 1000.0)
            await _arrow_run("ArrowDown" if n > 0 else "ArrowUp", abs(n))
        await asyncio.sleep(pause_ms(persona, 150, 400) / 1000.0)
        return await _commit_pick(selector, dl)

    async def _commit_pick(selector, dl):
        """Enter with the persona's hold, bounded by the select's deadline (commit_select_async)."""
        return await commit_select_async(page, selector, dl,
                                         lambda: native_kb_press("Enter", delay=_kdwell()),
                                         pause_ms(persona, 80, 160))

    async def _arrow_run(key, n):
        """``n`` presses of an arrow key in an OPEN drop-down, held through the OS auto-repeat model
        (_hold_key; see _humanize._arrow_run)."""
        if n < 8:
            await _key(key, n)
            return
        await _hold_key(key, None, n)

    async def _listbox_row(info, i):
        return await async_world_for(page).call_on_node(info["node"], ISO_LISTBOX_ROW, {"i": i})

    async def _settled_row(info, i):
        """The list box's state once its scroll has come to rest. A wheel notch scrolls the box
        SMOOTHLY over a few frames, and a read taken mid-animation shows the row where it was, not
        where it is going: the old loop then wheeled the wrong way and oscillated (verifier B). Two
        equal reads 50 ms apart = at rest."""
        prev = await _listbox_row(info, i)
        for _ in range(30):
            await asyncio.sleep(0.05)
            cur = await _listbox_row(info, i)
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

    async def _scroll_row_into_view(info, i):
        """Wheel the list box until row ``i`` shows: the pointer goes over the box once, then as many
        notches as the distance needs -- one at first, to learn how far a notch scrolls this box --
        each burst read only once the scroll has settled. (top, bottom, state), or None when it
        cannot be brought into view (the wheel does not move the box, or it is at its end)."""
        s = await _settled_row(info, i)
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
                await _glide(bx + bw * _rand(0.3, 0.7), s["top"] + room * _rand(0.3, 0.7), target_w=max(20, room))
                await asyncio.sleep(_rand(80, 200) / 1000.0)
                over = True
            n = 1 if not per else max(1, min(8, int(abs(need) // per) + (1 if abs(need) % per > per * 0.25 else 0)))
            notch = await _notch_px()
            for j in range(n):
                await native_wheel(0, notch if need > 0 else -notch)
                notches += 1
                if j < n - 1:
                    await asyncio.sleep(_rand(30, 70) / 1000.0)
            before = s["st"]
            s = await _settled_row(info, i)
            if s is None:
                return None
            moved = abs(s["st"] - before)
            if moved < 0.5:
                return None   # the wheel did not scroll the box
            per = moved / n
            span = _row_span(s)
        return (span[0], span[1], s) if span else None

    async def _select_listbox(loc, box, info, idx):
        """A list box (multiple or size > 1): click the rows -- the first plainly (which also drops
        any other selection), the rest with Ctrl/Cmd held, scrolling the box with the wheel to bring
        a row into view the way a person does."""
        want = sorted(idx)
        opts = info["options"]
        if set(want) == {i for i, o in enumerate(opts) if o.get("selected")}:
            return [opts[i]["value"] for i in want]
        for k, i in enumerate(want):
            got = await _scroll_row_into_view(info, i)
            if got is None:
                return None
            top, bottom, s = got
            r = s["row"]
            px = _rand(r[0] + r[2] * 0.12, r[0] + min(r[2] * 0.6, 140))
            py = (top + bottom) / 2 + random.gauss(0, max(0.5, (bottom - top) * 0.12))
            py = min(max(py, top + 1), bottom - 1)
            await _glide(px, py, target_w=bottom - top)
            if k and native_kb_down is not None:
                # ControlOrMeta: Playwright sends the add-to-selection key the ENGINE honours
                await _press_at(px, py, normalize_click_options({"modifiers": ["ControlOrMeta"]}))
            else:
                await _press_here(px, py)
            await asyncio.sleep(_rand(150, 350) / 1000.0)
        final = await _listbox_row(info, want[0])
        if not final:
            return None
        got = [i for i, sel in enumerate(final["sel"]) if sel]
        return [opts[i]["value"] for i in want] if got == want else None

    async def _select_humanized(selector, value, options):
        """Pick options the way a person does (see _open_and_pick / _select_listbox). Returns the
        selected values like Playwright, or None when the humane route cannot apply or cannot be
        shown to have worked -- the caller then runs stock select_option."""
        if options.get("element") is not None:
            return None
        # {value} / {label} / {index} option dicts (and lists of them): wanted options like bare strings,
        # as in Node (selectWants); they used to go native (_humanize.select_wants).
        wants = select_wants(value, options.get("index"), options.get("label"))
        if not wants:
            return None
        dl = select_deadline(options, page)
        loc = page.locator(selector).first
        box = await _box_of(loc, options.get("timeout"), options.get("force"), selector)
        info = await _describe(box, "select")
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
            return await _select_listbox(loc, box, info, idx)
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
        if await _open_and_pick(selector, plan, at, dl):
            return [opts[t]["value"]]   # the change handler navigated: nothing left to read back
        if await loc.input_value(timeout=select_left_ms(dl)) == opts[t]["value"]:
            return [opts[t]["value"]]
        # One correction from wherever the highlight landed (arrows only), then give up to native.
        again = await _redescribe(info, "select")
        if not again:
            return None
        fix = plan_select(labels, again.get("selectedIndex", -1), t, max_prefix=0)
        if fix is None:
            return None
        if await _open_and_pick(selector, fix, at, dl):
            return [opts[t]["value"]]
        return [opts[t]["value"]] if await loc.input_value(timeout=select_left_ms(dl)) == opts[t]["value"] else None

    async def hselect_option(selector, value=None, **options):
        """page.select_option, humanized (see _select_humanized)."""
        try:
            res = await _select_humanized(selector, value, options)
            if res is not None:
                return res
            why = _select_why(value, options)
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e):
                raise
            why = "error: %s" % (e,)
        _report_fallback(page, "Page.select_option", why, selector)
        value, options = native_select_args(value, options)   # option dicts: Playwright Python takes none
        return await native_page_select_option(selector, value, **options)

    async def _label_aim(selector, loc, timeout):
        """See _humanize._label_aim: the <label> box to click instead of the input's own box (always
        for a hidden custom checkbox, 40 % of the time for a visible one), or None."""
        w = async_world_for(page)
        if await loc.is_visible():
            box = await _box_of(loc, timeout, selector=selector)
            info = await _describe(box) if box else None
            if not info or info.get("tag") != "input" or info.get("type") not in ("checkbox", "radio"):
                return None
            lab = await w.call_on_node(info["node"], ISO_LABEL_OF)
            if not lab or not lab.get("label") or lab.get("hidden"):
                return None
            if not label_draw(persona):   # the persona's share; THIS click's choice is unseeded
                return None
            lb = lab["label"]
            r = lb.get("text")
            # a wrapping label's text is its content clear of the input's box; no text, or an aim box still over the
            # input: the input is the only target (see _humanize._label_aim; SC 239)
            if r is None and lb.get("contains"):
                return None
            r = r or lb["rect"]
            lbox = {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}
            if lbox["width"] < 2 or lbox["height"] < 2 or _boxes_overlap(lbox, lab.get("rect")):
                return None
            # aim inside the text, hit-test the LABEL ELEMENT's box (see _humanize._label_aim; SC 232 triage)
            lr = lab["label"]["rect"]
            lbox["hit"] = {"x": lr[0], "y": lr[1], "width": lr[2], "height": lr[3]}
            return lbox
        if not isinstance(selector, str) or not selector:
            return None
        lab = await w.evaluate(ISO_LABEL_BY_SEL, {"sel": selector})
        if not lab or not lab.get("n") or not lab.get("hidden") or not lab.get("label"):
            return None
        try:
            if await page.locator(selector).count() != lab["n"]:
                return None
        except Exception:  # noqa: BLE001
            return None
        r = lab["label"]["rect"]
        lbox = {"x": r[0], "y": r[1], "width": r[2], "height": r[3]}
        if lbox["width"] < 2 or lbox["height"] < 2:
            return None
        if not await _in_view(lbox):
            lbox = await _scroll_window_to(lbox) or lbox
        return lbox if await _in_view(lbox) else None

    async def _click_label(lbox):
        """One humanized click inside a label's box where the hit test proves the label owns the point;
        False when it does not (the caller aims at the input)."""
        x, y = click_point(lbox, st["pos"], persona)
        x, y = float(round(x)), float(round(y))
        await asyncio.sleep(0.05)
        if await _hit_in_box(lbox.get("hit") or lbox, x, y) is False:   # the label element's box
            return False
        await _glide(x, y)
        await _press_at(x, y, normalize_click_options({}, await _platform()))
        return True

    async def _set_checked(orig, selector, checked, options):
        """check / uncheck / set_checked: a humanized click when the state must change, then the
        state verified. `trial` performs no action: the state is read (a non-checkbox raises, as in
        Playwright) and, when a click would be needed, its checks run here (_trial)."""
        loc = page.locator(selector).first
        if options.get("trial"):
            if await loc.is_checked(timeout=options.get("timeout")) == checked:
                return None
            return await _trial(selector, options, no_click=False)
        # Errors propagate as Playwright's own (not a checkbox, timed out): the click below owns its
        # native fallback, so nothing here may click a second time.
        if await loc.is_checked(timeout=options.get("timeout")) == checked:
            return None
        clicked = False
        if not options.get("force") and not options.get("position"):
            try:
                lbox = await _label_aim(selector, loc, options.get("timeout"))
                clicked = bool(lbox) and await _click_label(lbox)
            except Exception as e:  # noqa: BLE001
                if _is_timeout(e):
                    raise
                clicked = False
        if not clicked:
            await _targeted_click(native_page_click, selector, dict(options), no_click=False, native_opts=None)
        if await loc.is_checked() == checked:
            return None
        # One click did not change it (a page that cancels the click): Playwright's check raises here
        # rather than clicking again, and so does this.
        from playwright.async_api import Error as PlaywrightError
        raise PlaywrightError("Clicking the checkbox did not change its state")

    async def _reaches_point(box, x, y, sel=None):
        """The covered-point check, through the browser's own hit test read in the isolated world:
        nothing is dispatched and the pointer does not move. It replaced a Playwright TRIAL action,
        which moves the REAL pointer onto the element and back and performs a press with its events
        blocked (verifier B: a mousemove inside the target, a 450-500 px jump back, then the glide,
        and a pointerdown window capture listeners could see). A covered point is looked at again
        for COVER_CHECK_MS (an overlay fading out); a point inside a child frame is answered by its
        <iframe> element. None when the hit test cannot run. See _humanize._reaches_point."""
        deadline = asyncio.get_running_loop().time() + COVER_CHECK_MS / 1000.0
        hit = await _hit_in_box(box, x, y, sel)
        while hit is False and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.08)
            hit = await _hit_in_box(box, x, y, sel)
        return hit

    def _same_box(a, b):
        return (abs(a["x"] - b["x"]) <= 1 and abs(a["y"] - b["y"]) <= 1
                and abs(a["width"] - b["width"]) <= 1 and abs(a["height"] - b["height"]) <= 1)

    async def _settled_box(loc, timeout, selector):
        """The element's box once it has stopped moving (two reads ~100 ms apart within 1 px, for up to
        SETTLE_MS), read like _box_of, with the wheel; None when it never settles or has no box. See
        _humanize._settled_box."""
        clock = asyncio.get_running_loop().time
        deadline = clock() + SETTLE_MS / 1000.0
        prev = await _box_of(loc, timeout, False, selector)
        while prev and clock() < deadline:
            await asyncio.sleep(0.1)
            nxt = await _box_of(loc, timeout, False, selector)
            if not nxt:
                return None
            if _same_box(prev, nxt):
                return nxt
            prev = nxt
        return None

    async def _uncover(box, x, y):
        """Uncover a target that is not moving yet fails the hit test (r53 /audit, seed 2, 2026-10-28):
        glide off a cover the pointer rests on (a hover menu it opened), or wheel the target out from
        under a fixed or sticky bar. True when it did one of them. See _humanize._uncover."""
        c = await async_world_for(page).evaluate(ISO_COVER, {"x": x, "y": y, "b": {
            "x": box["x"], "y": box["y"], "width": box["width"], "height": box["height"]}})
        if not isinstance(c, dict) or not isinstance(c.get("rect"), list) or len(c["rect"]) != 4:
            return False
        cx, cy, cw, ch = (float(v) for v in c["rect"])
        vw, vh = await _viewport()
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
            await _glide(tx, ty)
            await asyncio.sleep(_rand(250, 450) / 1000.0)   # a hover menu's close transition
            return True
        # A cover filling most of the viewport is a modal or a cookie wall, not a bar to scroll out from under.
        if c.get("fixed") and cw * ch <= 0.8 * vw * vh:
            dy = box["y"] + box["height"] / 2 - vh * _rand(0.3, 0.5)
            if abs(dy) < 1:
                return False
            await hwheel(0, dy)   # the internal wheel: the click already holds the lock
            return True
        return False

    async def _position_point(box, o):
        """The viewport point of Playwright's `position` (relative to the PADDING box)."""
        info = await _describe(box, None)
        bl, bt = (info.get("bl", 0), info.get("bt", 0)) if info else (0, 0)
        return box["x"] + bl + o["position"]["x"], box["y"] + bt + o["position"]["y"]

    async def _trial(selector, options, no_click):
        """`trial=True`, answered by the SDK: Playwright's actionability checks (visible, enabled
        unless hover, stable, the click point receives the press) retried until the timeout, then a
        Playwright TimeoutError; no pointer move and no press. See _humanize._trial."""
        o = normalize_click_options(options, await _platform())
        timeout = options.get("timeout")
        ms = float(timeout) if timeout is not None else default_timeout_ms(page)
        clock = asyncio.get_running_loop().time
        end = clock() + ms / 1000.0 if ms > 0 else None
        loc = page.locator(selector).first

        def left():   # what is left of the timeout for a Playwright wait (0 there: no limit)
            return 0 if end is None else max(1, int((end - clock()) * 1000.0))

        if o["force"]:
            await loc.wait_for(state="attached", timeout=left())
            return None
        while True:
            why = "element is not visible"
            # waits for visible; raises Playwright's timeout. No wheel: a trial must not move the pointer.
            box = await _box_of(loc, left(), selector=selector, wheel=False)
            if box:
                if not no_click and not await loc.is_enabled():
                    why = "element is not enabled"
                else:
                    if o["position"]:
                        x, y = await _position_point(box, o)
                    else:
                        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    await asyncio.sleep(0.05)   # stable: 50 ms on, the element still fills `box`
                    if await _hit_in_box(box, x, y, selector) is not False:
                        return None
                    why = ("element is not stable, or another element would receive the pointer at "
                           "(%d, %d)" % (x, y))
            if end is not None and clock() >= end:
                raise trial_timeout_error(selector, ms, why)
            await asyncio.sleep(0.1 if end is None else max(0.0, min(0.1, end - clock())))

    async def _targeted_click(orig, selector, options, no_click, native_opts, dwell=None, move_off=True):
        """The humanized form of page.click / dblclick / hover (and the click inside check/uncheck).

        Every Playwright option is honoured: `button`, `click_count`, `delay` (the hold) and
        `modifiers` shape the press; `position` (relative to the PADDING box) replaces the persona's
        point; `force` skips the hit test; `trial` performs NO action: its checks run here (_trial).
        A Playwright TimeoutError propagates: falling back would wait the full timeout again."""
        o = normalize_click_options(options, await _platform())

        aim = []   # where Playwright's own click lands, once the box is known (_native)

        async def _native():
            if orig is None:
                raise RuntimeError("humanized click could not run")
            try:
                return await orig(selector, **(native_opts if native_opts is not None else options))
            finally:
                # Native click/hover genuinely move the browser pointer to the element: record it,
                # or the next wheel anchor glides away from the element the click just left -- and
                # the engine path walks from its own stale point (verifier A #6): see _humanize.
                st["placed"] = True
                _eng["at"] = None
                if aim:
                    st["pos"] = aim[-1]

        if o["trial"]:
            return await _trial(selector, options, no_click)
        try:
            timeout = options.get("timeout")   # None: the page's default, as Playwright would use
            loc = page.locator(selector).first
            # Playwright skips actionability with force: no wait then -- a target it would click
            # hidden goes native at once instead of timing out first.
            box = await _box_of(loc, timeout, o["force"], selector)
            if not box:
                raise _Native("the element has no box")
            if not o["force"] and not await loc.is_enabled():
                raise _Native("the element is not enabled")

            async def aim_at(b):
                aim.append((b["x"] + b["width"] / 2, b["y"] + b["height"] / 2))
                if o["position"]:
                    px, py = await _position_point(b, o)
                    aim.append((px, py))
                else:
                    px, py = click_point(b, st["pos"], persona)
                # Whole pixels: the hit test below answers for the pixel DOM.getNodeForLocation rounds
                # to, so the press goes to that pixel too. A press at 971.6 next to a badge ending at 972
                # was proven at 972 and landed on the badge, reported as a success (verifier B, SC 210).
                return float(round(px)), float(round(py))

            x, y = await aim_at(box)
            if not o["force"] and orig is not None:
                # Stability, then covered-by: 50 ms on, the element must still fill `box` (an
                # animation in flight does not) and own the click point -- no trusted press at a
                # point some overlay owns. Either failing goes native: Playwright waits for it.
                await asyncio.sleep(0.05)
                hit = await _reaches_point(box, x, y, selector)
                # A target still moving (a scroll settling, a layout shift) is re-aimed at the box it
                # settles at, and a covered one is uncovered first (_uncover), rather than handing the
                # click to Playwright (see _humanize._targeted_click); what stays covered is handled below.
                tries = 0
                while hit is False and tries < SETTLE_TRIES:
                    tries += 1
                    nxt = await _settled_box(loc, timeout, selector)
                    if not nxt:
                        break
                    if _same_box(nxt, box):
                        if not await _uncover(nxt, x, y):
                            break
                        nxt = await _settled_box(loc, timeout, selector)
                        if not nxt:
                            break
                    box = nxt
                    x, y = await aim_at(box)
                    hit = await _reaches_point(box, x, y, selector)
                if hit is False and not o["position"]:
                    # The persona's point is covered (a badge, an icon, a floating label over the
                    # centre). Before giving the click to Playwright -- which waited out its timeout on
                    # 5-7 of 16 badge-covered buttons (EVAL B) -- aim at the first probe point the hit
                    # test proves reaches the element, the way _describe finds a covered field.
                    alt = await _proven_click_point(box, selector)
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
                # Opt-in (engine "all"): the engine walks the approach and clicks the proven point -- a plain
                # left click on the r42 route (in sync, or re-synced by an r43 engine), a right / double /
                # modified click through the r43 extensions as mouse.click does (R5). Otherwise: the planner.
                if engine_path_ok(o) and await _engine_glide(x, y, no_click=False):
                    return None
                if await _engine_ext_click(x, y, o):
                    return None
            if no_click and o["modifiers"]:
                # hover with modifiers: Playwright holds them across the move
                held = []
                try:
                    for m in o["modifiers"]:
                        held.append(m)   # before the await (see _press_at)
                        await native_kb_down(m)
                        await asyncio.sleep(_rand(40, 110) / 1000.0)
                    await _glide(x, y, target_w=_aim_w(box))
                    await _dwell(x, y, box, dwell)
                finally:
                    if held:
                        async def lift(keys=tuple(reversed(held))):
                            for m in keys:
                                await asyncio.sleep(_rand(20, 80) / 1000.0)
                                try:
                                    await native_kb_up(m)   # one failed keyup must not strand the others
                                except Exception:  # noqa: BLE001
                                    pass
                        await _release(lift())
                return None
            await _glide(x, y, target_w=_aim_w(box))
            if no_click:
                await _dwell(x, y, box, dwell)   # the hand rests on what it hovered (SC 232 lane C)
            else:
                if native_down is None or native_up is None:
                    await _pre_press()
                    await native_click(x, y, delay=o["delay"] if o["delay"] is not None else click_hold(persona),
                                       button=o["button"], click_count=o["click_count"])
                else:
                    await _press_at(x, y, o)
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
        return await _native()

    def _make_targeted(orig, no_click):
        async def wrapped(selector, **options):
            return await _targeted_click(orig, selector, options, no_click, None)
        return wrapped

    # The public entry points, each serialised by the page's lock (see `lock` above). The functions
    # behind them call one another only through these internal names, never through page.*.
    if native_page_type is not None:
        page.type = api_errors("Page.type", _serial(htype))
    if native_page_fill is not None:
        page.fill = api_errors("Page.fill", _serial(hfill))
    if native_page_dblclick is not None:
        page.dblclick = api_errors("Page.dblclick", _serial(hdblclick))
    if native_page_press is not None:
        page.press = api_errors("Page.press", _serial(hpress))
    if native_page_focus is not None:
        page.focus = api_errors("Page.focus", _serial(hfocus))
    if native_page_select_option is not None:
        page.select_option = api_errors("Page.select_option", _serial(hselect_option))
    for name, want in (("check", True), ("uncheck", False)):
        orig_c = getattr(page, name, None)
        if orig_c is not None:
            setattr(page, name, api_errors("Page." + name, _serial(
                (lambda o, w: lambda selector, **opts: _set_checked(o, selector, w, opts))(orig_c, want))))
    orig_sc = getattr(page, "set_checked", None)
    if orig_sc is not None:
        async def hset_checked(selector, checked, **opts):
            async def orig(s, **o):
                return await orig_sc(s, checked, **o)
            return await _set_checked(orig, selector, bool(checked), opts)
        page.set_checked = api_errors("Page.set_checked", _serial(hset_checked))
    if native_page_click is not None:
        page.click = api_errors("Page.click", _serial(_make_targeted(native_page_click, False)))
    if native_page_hover is not None:
        async def _hover(selector, dwell=None, **options):
            # `dwell` (ms; 0: none) is the SDK's, not Playwright's: never forwarded to a native hover
            return await _targeted_click(native_page_hover, selector, options, True, None, dwell=dwell)
        page.hover = api_errors("Page.hover", _serial(_hover))
    # Locator.drag_to(trial=True) answers its checks here too (see the Locator patch).
    page._clearcote_trial = _serial(_trial, after=False)

    # press_and_hold (_hold.py): see the sync file. It holds the page lock for the whole hold (one
    # action at a time); inside it uses page.mouse (not locked) and the hooks below.
    page._clearcote_box_of = lambda loc, timeout, selector=None: _box_of(loc, timeout, False, selector)
    page._clearcote_pointer = lambda: st["pos"]
    # ... and for its press point: a glide with the box's own Fitts width, the covered-point check
    # (the same isolated hit test as a click, retried COVER_CHECK_MS), the probe-proven point when the
    # chosen one is covered, and the border widths that make `position` padding-box relative like click's.
    page._clearcote_aim_glide = lambda x, y, box: _glide(x, y, target_w=_aim_w(box))
    page._clearcote_reaches_point = _reaches_point
    page._clearcote_proven_point = _proven_click_point

    async def _border_of(box):
        info = await _describe(box, None)
        return (float(info.get("bl", 0) or 0), float(info.get("bt", 0) or 0)) if info else (0.0, 0.0)
    page._clearcote_border_of = _border_of

    async def _nudge(x, y):
        await native_move(x, y)
        st["pos"] = (x, y)
        st["placed"] = True
    page._clearcote_nudge = _nudge
    # the frame route (_frames) clears a field with the same held chord as the page routes
    page._clearcote_select_all = _select_all
    page._clearcote_hold = _serial(press_and_hold_async)
    page.press_and_hold = api_errors("Page.press_and_hold", lambda selector, duration=None, **kw: page._clearcote_hold(
        page.locator(selector).first, duration, **kw))

    # ------------------------------------------------------------- drags (see _humanize: same steps)
    def _loc_selector(loc):
        try:
            return loc._impl_obj._selector
        except Exception:  # noqa: BLE001
            return None

    async def _peek_box(loc, selector):
        if await _css_plain(selector):
            return _rect_box(await async_world_for(page).evaluate(ISO_CSS_BOX, {"sel": selector, "scroll": False}))
        return await loc.bounding_box()

    async def _aim(box, position, force, selector):
        if position:
            x, y = await _position_point(box, {"position": position})
        else:
            x, y = click_point(box, st["pos"], persona)
        x, y = float(round(x)), float(round(y))
        if force:
            return x, y
        await asyncio.sleep(0.05)
        hit = await _reaches_point(box, x, y, selector)
        if hit is None or hit:
            return x, y
        if position:
            return None
        for fx, fy in probe_points[1:]:
            px = float(round(box["x"] + box["width"] * fx))
            py = float(round(box["y"] + box["height"] * fy))
            if await _hit_in_box(box, px, py, selector) is True:
                return px, py
        return None

    async def _creep(frm, toward, grab_ms):
        steps = drag_hesitation(frm, toward, persona, dur_ms=grab_ms / tempo)   # _dispatch scales by tempo
        await _dispatch(steps, kind="drag", buttons=1)
        if steps:
            st["pos"] = (steps[-1][0], steps[-1][1])

    async def _held_drag(frm, to, grab_ms, settle=True):
        steps, _ = hesitate_then_drag(frm, to, persona, grab_ms / tempo, settle=settle)   # _dispatch scales by tempo
        await _dispatch(steps, kind="drag", buttons=1)
        st["pos"] = (to[0], to[1])

    async def _drag(src, dst, opts):
        """See _humanize._drag."""
        timeout = opts.get("timeout")
        force = bool(opts.get("force"))
        ssel, dsel = _loc_selector(src), _loc_selector(dst)
        sbox = await _box_of(src, timeout, force, ssel)
        if not sbox:
            raise _Native("the source has no box")
        sp = await _aim(sbox, opts.get("source_position"), force, ssel)
        if sp is None:
            raise _Native("the source point is covered (no probe point reaches it)")
        if not await dst.is_visible():
            if force:
                raise _Native("the target is hidden and force is set")
            await dst.wait_for(state="visible", timeout=timeout)
        tbox = await _peek_box(dst, dsel)
        if not tbox:
            raise _Native("the target has no box")
        tp = None
        if await _in_view(tbox):
            tp = await _aim(tbox, opts.get("target_position"), force, dsel)
            if tp is None:
                raise _Native("the target point is covered (no probe point reaches it)")
        if native_down is None or native_up is None:
            raise _Native("no mouse.down / mouse.up to drag with")
        grab_ms, release_ms = drag_dwell(persona)
        await _glide(sp[0], sp[1], target_w=max(6.0, min(sbox["width"], sbox["height"])))
        await _pre_press()
        st["held"] = True
        try:
            await native_down()
            if tp is None:
                await _creep(sp, (tbox["x"] + tbox["width"] / 2, tbox["y"] + tbox["height"] / 2), grab_ms)
                got, ok, _ = await bring_into_view_async(_scroll_io(), lambda: _peek_box(dst, dsel))
                if not ok or not got:
                    _fallback("Locator.drag_to", "scroll into view: target_out_of_view")
                    await dst.scroll_into_view_if_needed(timeout=timeout)
                    got = await _peek_box(dst, dsel)
                if not got:
                    raise RuntimeError("drag target vanished")
                pos = opts.get("target_position")
                tp = await _position_point(got, {"position": pos}) if pos else click_point(got, st["pos"], persona)
                tp = (float(round(tp[0])), float(round(tp[1])))
                await _glide(tp[0], tp[1], settle=True, held=True)
            else:
                await _held_drag(sp, tp, grab_ms)
            await asyncio.sleep(release_ms / 1000.0)
        finally:
            st["held"] = False
            await _release(native_up())
        return True

    async def _settled_read(read):
        a = await read()
        for _ in range(12):
            await asyncio.sleep(0.05)
            b = await read()
            if a is not None and b is not None and abs(a[0] - b[0]) < 0.01 and abs(a[1] - b[1]) < 0.01:
                return b
            a = b
        return a if a is not None else (0.0, 0.0)

    async def _drag_by_core(px, py, dx, dy, read, tol=2.0, grab_ms=None):
        """See _humanize._drag_by_core."""
        d = (dx * dx + dy * dy) ** 0.5
        if grab_ms is None:
            grab_ms = drag_dwell(persona)[0]
        if d < 0.5:
            st["drag_legs"] = 0
            return await _settled_read(read)
        ux, uy = dx / d, dy / d
        past = _rand(3.0, 8.0)
        await _held_drag((px, py), (px + dx + ux * past, py + dy + uy * past), grab_ms, settle=False)
        legs = 1
        while True:
            await asyncio.sleep(_rand(60, 140) / 1000.0)
            got = await _settled_read(read)
            ex, ey = dx - got[0], dy - got[1]
            if (ex * ex + ey * ey) ** 0.5 <= tol or legs >= 3:
                break
            await _glide(st["pos"][0] + ex, st["pos"][1] + ey, settle=True, held=True)
            legs += 1
        st["drag_legs"] = legs
        return got

    async def _drag_by(loc, dx, dy, opts):
        """See _humanize._drag_by (``read`` may be a plain or an async callable)."""
        timeout = opts.get("timeout")
        force = bool(opts.get("force"))
        sel = _loc_selector(loc)
        box = await _box_of(loc, timeout, force, sel)
        if not box:
            raise RuntimeError("drag_by: the element has no box (not rendered)")
        p0 = await _aim(box, opts.get("position"), force, sel)
        if p0 is None:
            raise RuntimeError("drag_by: another element would receive the press")
        user_read = opts.get("read")
        c0 = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        async def read():
            if user_read is not None:
                v = user_read()
                if asyncio.iscoroutine(v) or isinstance(v, asyncio.Future):
                    v = await v
                if v is None:
                    return None
                if isinstance(v, (int, float)):
                    return (float(v), 0.0)
                return (float(v[0]), float(v[1]))
            b = await _peek_box(loc, sel)
            if not b:
                return None
            return (b["x"] + b["width"] / 2 - c0[0], b["y"] + b["height"] / 2 - c0[1])

        grab_ms, release_ms = drag_dwell(persona)
        await _glide(p0[0], p0[1], target_w=max(6.0, min(box["width"], box["height"])))
        await _pre_press()
        st["held"] = True
        try:
            await native_down()
            got = await _drag_by_core(p0[0], p0[1], float(dx), float(dy), read,
                                      float(opts.get("tolerance") or 2.0), grab_ms)
            page._clearcote_drag_legs = st.get("drag_legs")
            await asyncio.sleep(release_ms / 1000.0)
        finally:
            st["held"] = False
            await _release(native_up())
        return got

    page._clearcote_drag = _serial(_drag)
    page._clearcote_drag_by = _serial(_drag_by)
    native_page_dnd = getattr(page, "drag_and_drop", None)

    async def hdrag_and_drop(source, target, **opts):
        """page.drag_and_drop, humanized like Locator.drag_to (see _humanize.hdrag_and_drop)."""
        if opts.get("trial"):
            for sel, pos in ((source, opts.get("source_position")), (target, opts.get("target_position"))):
                o = {k: opts[k] for k in ("force", "timeout") if opts.get(k) is not None}
                if pos:
                    o["position"] = pos
                await _trial(sel, o, True)
            return None
        why = None
        try:
            src, dst = page.locator(source), page.locator(target)
            if await src.count() <= 1 and await dst.count() <= 1:
                await _drag(src.first, dst.first, opts)
                return None
        except _Native as e:
            why = str(e) or "the humane drag cannot apply"
        except Exception as e:  # noqa: BLE001
            if _is_timeout(e) or _is_strict(e):
                raise
            why = "error: %s" % (e,)
        if why:   # more than one match is not a fallback: Playwright raises its own strict-mode error
            _report_fallback(page, "Page.drag_and_drop", why, source)
        return await native_page_dnd(source, target, **opts)

    if native_page_dnd is not None:
        page.drag_and_drop = api_errors("Page.drag_and_drop", _serial(hdrag_and_drop))

    async def hdrag_by(selector, dx, dy, **opts):
        return await _drag_by(page.locator(selector).first, dx, dy, opts)

    page.drag_by = api_errors("Page.drag_by", _serial(hdrag_by))

    # The after-action step first, then the per-call switch outermost: humanize=False goes straight
    # to the stock call and skips the after-action step too.
    _install_after_action()
    _wire_per_call_off(page, st, _stock, _stock_mouse, _stock_kb)

    # Marker the Locator-class patch keys off: humanize is active for THIS page.
    page._clearcote_humanized = True
    _patch_locator_class()


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
    async def call(*a, humanize=None, **kw):
        if humanize is False:
            for k in _SDK_ONLY_KW:   # the SDK's own keywords (hover dwell=): Playwright's call takes none of them
                kw.pop(k, None)
            n = max(1, int(kw.pop("repeat", 1) or 1))   # press(repeat=n): n stock presses
            try:
                r = None
                for _ in range(n):
                    r = await stock(*a, **kw)
                return r
            finally:
                if after is not None:
                    after(a)
        return await fn(*a, **kw)
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
            async def s_down(*a, _s=s, **kw):   # stock press, but the held state stays tracked
                st["held"] = True
                try:
                    return await _s(*a, **kw)
                except Exception:
                    st["held"] = False
                    raise
            setattr(mouse, n, _per_call(s_down, cur))
        elif n == "up":
            async def s_up(*a, _s=s, **kw):
                try:
                    return await _s(*a, **kw)
                finally:
                    st["held"] = False
            setattr(mouse, n, _per_call(s_up, cur))
        else:
            setattr(mouse, n, _per_call(s, cur, moved if n in ("move", "click", "dblclick") else None))
    page._clearcote_stock_mouse = dict(stock_mouse)
    if getattr(page, "drag_by", None) is not None:   # no Playwright drag_by: humanize=False is the plain stock drag
        async def _stock_page_drag_by(selector, dx, dy, **kw):
            return await _stock_drag_by(page.locator(selector).first, dx, dy, **kw)
        page.drag_by = _per_call(_stock_page_drag_by, page.drag_by)
    kb = page.keyboard
    for n, s in stock_kb.items():
        cur = getattr(kb, n, None)
        if s is not None and cur is not None and cur is not s:
            setattr(kb, n, _per_call(s, cur))


async def _stock_drag_by(self, dx, dy, **kw):
    """Locator.drag_by(..., humanize=False): there is no Playwright drag_by, so the stock mouse does the plain
    thing -- press at ``position`` (else the box centre), one move by (dx, dy), release -- and the asked delta
    is returned (no read, no correction)."""
    page = self.page
    m = getattr(page, "_clearcote_stock_mouse", None) or {}
    box = await self.bounding_box(timeout=kw.get("timeout"))
    if not box:
        raise RuntimeError("drag_by: the element has no box (not rendered)")
    pos = kw.get("position")
    x = box["x"] + (pos["x"] if pos else box["width"] / 2)
    y = box["y"] + (pos["y"] if pos else box["height"] / 2)
    move, down, up = (m.get("move") or page.mouse.move), (m.get("down") or page.mouse.down), (m.get("up") or page.mouse.up)
    await move(x, y)
    await down()
    try:
        await move(x + dx, y + dy)
    finally:
        await up()
    return (float(dx), float(dy))


def _locator_off(stock, fn):
    """A Locator method that takes humanize=False: the stock Playwright method, keyword stripped."""
    @functools.wraps(fn)
    async def call(self, *a, humanize=None, **kw):
        if humanize is False:
            return await stock(self, *a, **kw)
        return await fn(self, *a, **kw)
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


# --------------------------------------------------------------------------- locator patch
_async_locator_patched = False


# Playwright's own async Locator methods the humanized ones fall back to (blur / select_text), by name.
_LOCATOR_NATIVE = {}


def _patch_locator_class():
    """Patch the async Locator class once so locator.* interactions route through the humanized
    page.* methods. A method routes only when the locator's page has humanize active AND the locator
    targets the main frame (not through a frame locator) AND it matches at most one element (locator
    actions are strict); anything else runs the original Playwright method. Once routed, every error
    propagates: the page-level wrappers own the native fallback. Composes safely if another library
    has already patched Locator — we chain to whatever was there before."""
    global _async_locator_patched
    if _async_locator_patched:
        return
    _async_locator_patched = True
    _fr.patch_frame_class(is_async=True)   # child-frame selector actions -> the frame route
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

    async def _strict(self):
        """Locator actions are STRICT in Playwright: more than one match is an error. The page-level
        methods they route through take the first match, so check the count here and let the native
        call raise its own strict-mode error when it is not exactly one."""
        try:
            return await self.count() <= 1
        except Exception:  # noqa: BLE001
            return False

    def _sel(self):
        return self._impl_obj._selector

    # The options each page-level humanized method understands (see _humanize._patch_locator_class):
    # everything the caller passed that a page method accepts travels with it.
    _CLICK = ("button", "click_count", "delay", "force", "modifiers", "no_wait_after", "position",
              "timeout", "trial")
    _HOVER = ("force", "modifiers", "no_wait_after", "position", "timeout", "trial", "dwell")
    _DBL = ("button", "delay", "force", "modifiers", "no_wait_after", "position", "timeout", "trial")
    _FILL = ("force", "no_wait_after", "timeout")
    _TYPE = ("delay", "timeout")      # delay: the shortest gap between two keys (humanized typing)
    _PRESS = ("delay", "no_wait_after", "timeout", "repeat")   # repeat: the SDK's OS auto-repeat
    _SELECT = ("index", "label", "element", "force", "no_wait_after", "timeout")
    _CHECK = ("force", "no_wait_after", "position", "timeout", "trial")

    def _fwd(kw, names=("timeout",)):
        return {k: kw[k] for k in names if k in kw and kw[k] is not None}

    async def _froute(self, action, args, kw):
        """A locator inside a child frame (FrameLocator, Frame.locator) on a humanized page: through the
        frame route (_frames.aroute: the frame's own isolated world for the hit test, the page's humanized
        mouse and keyboard for the input). (False, None) when it does not apply -- the native method
        runs, and the route has reported why."""
        if not (_fr.humanized(self.page) and _fr.frame_locator_of(self)) or not await _strict(self):
            return False, None
        r = await _fr.aroute(self, action, args, kw)
        return (False, None) if r is _fr.NATIVE else (True, r)

    async def _route(self, fn, frame_routed=True):
        # Once routed, every error propagates: the page-level wrappers own the native fallback, so
        # retrying the original locator method here ran the action a second (or third) time -- a
        # checkbox whose click is cancelled was clicked three times.
        if _on(self) and await _strict(self):
            return True, await fn()
        # A frame locator: the frame route (_froute, lane F) runs next and reports its own fallbacks; only an
        # action without a frame route (tap) reports here.
        why = None if frame_routed else _frame_why(self)
        if why:
            _report_fallback(self.page, "Locator", why, _sel(self))
        return False, None

    async def fill(self, value, **kw):
        done, r = await _route(self, lambda: self.page.fill(_sel(self), value, **_fwd(kw, _FILL)))
        if done:
            return r
        done, r = await _froute(self, 'fill', (value,), kw)
        return r if done else await o_fill(self, value, **kw)

    async def click(self, **kw):
        done, r = await _route(self, lambda: self.page.click(_sel(self), **_fwd(kw, _CLICK)))
        if done:
            return r
        done, r = await _froute(self, 'click', (), kw)
        return r if done else await o_click(self, **kw)

    async def type_(self, text, **kw):
        done, r = await _route(self, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        if done:
            return r
        done, r = await _froute(self, 'type', (text,), kw)
        return r if done else await o_type(self, text, **kw)

    async def dblclick(self, **kw):
        done, r = await _route(self, lambda: self.page.dblclick(_sel(self), **_fwd(kw, _DBL)))
        if done:
            return r
        done, r = await _froute(self, 'dblclick', (), kw)
        return r if done else await o_dblclick(self, **kw)

    async def hover(self, **kw):
        done, r = await _route(self, lambda: self.page.hover(_sel(self), **_fwd(kw, _HOVER)))
        if done:
            return r
        done, r = await _froute(self, 'hover', (), kw)
        # `dwell` is the SDK's: Playwright's own hover does not take it
        return r if done else await o_hover(self, **{k: v for k, v in kw.items() if k != "dwell"})

    async def press(self, key, **kw):
        done, r = await _route(self, lambda: self.page.press(_sel(self), key, **_fwd(kw, _PRESS)))
        if done:
            return r
        # repeat= is the SDK's (OS auto-repeat on the humanized route); the frame route presses once
        n = max(1, int(kw.pop("repeat", 1) or 1))
        if n == 1:
            done, r = await _froute(self, 'press', (key,), kw)
            if done:
                return r
        for _ in range(n):   # not humanized: n separate Playwright presses
            r = await o_press(self, key, **kw)
        return r

    async def focus(self, **kw):
        done, r = await _route(self, lambda: self.page.focus(_sel(self), **_fwd(kw, ("timeout",))))
        return r if done else await o_focus(self, **kw)

    async def blur(self, **kw):
        fn = getattr(self.page, "_clearcote_blur", None)
        if fn is not None:
            done, r = await _route(self, lambda: fn(_sel(self), **_fwd(kw, ("timeout",))))
            if done:
                return r
        return await o_blur(self, **kw)

    async def select_text(self, **kw):
        fn = getattr(self.page, "_clearcote_select_text", None)
        if fn is not None:
            done, r = await _route(self, lambda: fn(_sel(self), **_fwd(kw, ("force", "timeout"))))
            if done:
                return r
        return await o_select_text(self, **kw)

    async def press_sequentially(self, text, **kw):
        done, r = await _route(self, lambda: self.page.type(_sel(self), text, **_fwd(kw, _TYPE)))
        if done:
            return r
        done, r = await _froute(self, 'press_sequentially', (text,), kw)
        return r if done else await o_press_seq(self, text, **kw)

    async def clear(self, **kw):
        done, r = await _route(self, lambda: self.page.fill(_sel(self), "", **_fwd(kw, _FILL)))
        if done:
            return r
        done, r = await _froute(self, 'clear', (), kw)
        return r if done else await o_clear(self, **kw)

    async def tap(self, **kw):
        if _no_touch(self):   # Playwright's own "does not support tap" error (see _humanize)
            return await o_tap(self, **kw)
        if _on(self):
            # A touch context: there is no humanized touch sequence yet, so the tap goes out as the humanized
            # MOUSE click. Lane A's report: strict mode raises ClearcoteStrictError (a Playwright error) and
            # nothing is sent; otherwise on_fallback hears it and the click runs as before.
            report_fallback(self.page, "Locator.tap", TAP_NOT_HUMANIZED + ": sent as a humanized mouse click",
                            _sel(self))
        done, r = await _route(self, lambda: self.page.click(_sel(self), **_fwd(kw, _HOVER)), frame_routed=False)
        return r if done else await o_tap(self, **kw)

    async def check(self, **kw):
        done, r = await _route(self, lambda: self.page.check(_sel(self), **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = await _froute(self, 'check', (), kw)
        return r if done else await o_check(self, **kw)

    async def uncheck(self, **kw):
        done, r = await _route(self, lambda: self.page.uncheck(_sel(self), **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = await _froute(self, 'uncheck', (), kw)
        return r if done else await o_uncheck(self, **kw)

    async def set_checked(self, checked, **kw):
        done, r = await _route(self, lambda: self.page.set_checked(_sel(self), checked, **_fwd(kw, _CHECK)))
        if done:
            return r
        done, r = await _froute(self, 'set_checked', (checked,), kw)
        return r if done else await o_set_checked(self, checked, **kw)

    async def select_option(self, value=None, **kw):
        """Delegate to the humanized page.select_option, like every other method here: one
        implementation behind the page method is what stops the two entry points drifting apart."""
        done, r = await _route(self, lambda: self.page.select_option(_sel(self), value, **_fwd(kw, _SELECT)))
        if done:
            return r
        done, r = await _froute(self, "select_option", (value,), kw)
        if done:
            return r
        value, kw = native_select_args(value, kw)   # option dicts: Playwright Python takes none
        return await o_select_option(self, value, **kw)

    async def drag_to(self, target, **kw):
        if kw.get("trial") and _on(self) and _on(target) and await _strict(self) and await _strict(target):
            # Playwright's drag trial moves the pointer onto the source and presses there (SC 209);
            # the SDK runs the checks of both ends instead: visible, stable, receives the pointer.
            trial = getattr(self.page, "_clearcote_trial", None)
            if trial is not None:
                for loc, pos in ((self, kw.get("source_position")), (target, kw.get("target_position"))):
                    opts = {k: kw[k] for k in ("force", "timeout") if kw.get(k) is not None}
                    if pos:
                        opts["position"] = pos
                    await trial(_sel(loc), opts, True)
                return None
        if not _on(self) and not kw.get("trial") and _frame_why(self):
            _report_fallback(self.page, "Locator.drag_to", _frame_why(self), _sel(self))
        if not kw.get("trial") and _on(self) and _on(target) and await _strict(self) and await _strict(target):
            drag = getattr(self.page, "_clearcote_drag", None)
            if drag is not None:
                why = "the humane drag cannot apply"
                try:
                    await drag(self, target, kw)
                    return None
                except _Native as e:
                    why = str(e) or why
                except Exception as e:  # noqa: BLE001
                    if _is_timeout(e) or _is_strict(e):
                        raise
                    why = "error: %s" % (e,)
                # a covered end, no CDP, a vanished element: Playwright's own drag (lane A's report first)
                _report_fallback(self.page, "Locator.drag_to", why, _sel(self))
        return await o_drag_to(self, target, **kw)

    # Each worded as Playwright words the Locator method (api_errors).
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

    async def drag_by(self, dx, dy, **kw):
        """New, not a Playwright method: drag this element by (dx, dy) px, closed loop; returns the
        achieved (dx, dy). See the sync Locator.drag_by."""
        fn = getattr(self.page, "_clearcote_drag_by", None)
        if fn is None:
            raise RuntimeError("drag_by needs humanize=True")
        return await fn(self, dx, dy, kw)

    Locator.drag_by = api_errors("Locator.drag_by", _locator_off(_stock_drag_by, drag_by))
    Locator.select_option = api_errors("Locator.select_option", _locator_off(o_select_option, select_option))
    # New, not a Playwright method: press and hold this element (_hold.py), under the page lock when the
    # page is humanized. Works on frame locators too: it presses at viewport coordinates.
    async def press_and_hold(self, duration=None, **kw):
        hold = getattr(self.page, "_clearcote_hold", None) or press_and_hold_async
        return await hold(self, duration, **kw)
    Locator.press_and_hold = api_errors("Locator.press_and_hold", press_and_hold)
    if o_set_checked is not None:
        Locator.set_checked = api_errors("Locator.set_checked", _locator_off(o_set_checked, set_checked))
    if o_focus is not None:
        Locator.focus = api_errors("Locator.focus", _locator_off(o_focus, focus))
    if o_blur is not None:
        Locator.blur = api_errors("Locator.blur", _locator_off(o_blur, blur))
    if o_select_text is not None:
        Locator.select_text = api_errors("Locator.select_text", _locator_off(o_select_text, select_text))


async def install_humanize_on_context(context, humanize=False, show_cursor=False, browser=None, seed=None):
    humanize = _hz_resolve(humanize) or False   # True | "strict" | HumanizeOptions | dict; raises on a bad one
    if not humanize and not show_cursor:
        return
    # Every page the context has and opens (popups included), with or without a Browser: a persistent
    # context has none (see _humanize.install_humanize_on_context; mirrors Node).
    b = browser or getattr(context, "browser", None)
    if show_cursor:
        try:
            await context.add_init_script(CURSOR_OVERLAY)
        except Exception:  # noqa: BLE001
            pass
    if getattr(context, "_clearcote_hooked", False) is not True:
        try:
            context._clearcote_hooked = True
        except Exception:  # noqa: BLE001
            pass
        context.on("page", lambda p: asyncio.ensure_future(attach_humanize(b, p, humanize, show_cursor, seed)))
    for p in list(getattr(context, "pages", None) or ()):
        await attach_humanize(b, p, humanize, show_cursor, seed)


async def install_humanize(browser, humanize=False, show_cursor=False, seed=None):
    humanize = _hz_resolve(humanize) or False   # see _humanize.install_humanize
    if not humanize and not show_cursor:
        return
    orig_new_page = browser.new_page
    orig_new_context = browser.new_context

    async def new_page(**kw):
        page = await orig_new_page(**kw)
        await attach_humanize(browser, page, humanize, show_cursor, seed)
        # the implicit context browser.new_page made inside Playwright: hook it so its popups attach
        ctx = getattr(page, "context", None)
        if ctx is not None:
            try:
                await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
            except Exception:  # noqa: BLE001
                pass
        return page

    async def new_context(**kw):
        ctx = await orig_new_context(**kw)
        await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        return ctx

    browser.new_page = new_page
    browser.new_context = new_context
