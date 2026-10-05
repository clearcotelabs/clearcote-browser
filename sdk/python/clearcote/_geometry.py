"""Headless window geometry.

Headed launches take their geometry from the real display and the SDK keeps the page on it
(``no_viewport``), so ``screen`` / ``avail`` / ``inner`` / ``outer`` agree by construction. Headless
has no display, and what it reports depends on whether the engine's persona machinery is running.
Regime 1 was measured on 149.0.7827.114/linux-x64, regime 2 on win-x64 149 and 153 (below).

REGIME 1 — a persona is active (``--fingerprint=<seed>`` on the command line).
    The engine spoofs ``screen`` AND ``avail`` from the seed, including a taskbar
    (seed A -> 1920x1080 / avail 1920x1040, seed B -> 2560x1440 / 1400, seed C -> 1600x900 / 860),
    and its values BEAT a CDP screen override — so the SDK must not try to set screen here, it would
    silently lose. What it must not do either is leave Playwright's emulated viewport on: that gives
    ``inner`` 1280x720 inside an ``outer`` of 1920x1040, i.e. 640px of window unaccounted for by any
    frame a real browser has. So: ``no_viewport`` (``inner`` tracks the real window) plus one window
    resize to the persona's own work area, which yields a maximized window::

        screen 1920x1080   avail 1920x1040   inner 1920x919   outer 1920x1007   frame (0, 88)

    That frame delta is the engine's own, and it matches real captures (dx 0 maximized / 16 floating,
    dy 87-95 on Windows) far better than anything the SDK could impose.

REGIME 2 — no persona (the default seedless launch, ``light_stealth``, which drops
``--fingerprint`` deliberately, and a ``fingerprint_profile`` / ``profile="auto"`` launch without a
seed — measured: an imported profile only supplies screen/avail when a persona is ALSO running, so
without a seed its display is inert and this regime applies. When a profile is present its own screen
is used instead of a corpus pick.).
    Nothing spoofs ``screen``, so a page sees the 800x600 headless surface, and Playwright's
    emulated viewport then synthesizes a window on top of it::

        screen 1280x720   avail 1280x720   inner 1280x720   outer 1288x851   <- outer > screen

    A window larger than its own screen is not a statistical tell but an impossible state, readable
    in two property lookups. It was present on every headless shape reachable through the SDK
    (default viewport, explicit viewport, ``no_viewport``, ``no_viewport`` + ``--window-size``).
    The fix is ``--screen-info``, which makes the headless DISPLAY a real-machine size (with a
    taskbar on Windows) before the first document exists, then the same ``no_viewport`` + work-area
    fit as regime 1. Measured on 153.0.8010.36/win-x64, persistent and non-persistent contexts::

        screen 1920x1080  avail 1920x1040  inner 1904x911  outer 1920x1040   (popups clamp inside it)

    This used to be a CDP screen override (Playwright's ``screen`` option) with the viewport sized as
    screen minus a hardcoded engine frame. Two faults: the override forces ``avail == screen`` (no
    taskbar, a minority shape), and the frame around an emulated viewport is per-platform — 8x131 on
    linux-x64 but 16x134 on win-x64 (measured on 149.0.7827.114 and 153.0.8010.36) — so on Windows
    the window landed 8x3 px past the screen edge. With a real display nothing is sized against the
    frame. (The engine's ``--fingerprint-screen-*`` switches are no substitute: device-width media
    queries keep answering 800x600.)

    PARITY: the seed still selects the same screen row in every SDK (``headless_geometry``); only
    how it is applied changed.

PROVENANCE of the regime-2 table: the ``audit_profiles`` corpus (real captures from the public
fingerprint audit), desktop rows whose geometry is self-consistent and which are not themselves
emulated-viewport captures, counted by distinct /24 so one busy machine cannot skew it. Regenerate
with ``scripts/derive-headless-geometry.cjs``. Three deliberate deviations, because the corpus
samples an audit site's visitors rather than the web:

* macOS rows dropped — they report ``color_depth`` 30, which this engine cannot spoof, so a macOS
  screen size would contradict the depth the page actually reads.
* Non-1.0 device-pixel-ratio rows dropped (e.g. the common Windows ``1536x864 @1.25``). DPR is
  reachable over CDP, but scaling changes what the rasterizer produces, and an unverified DPR is a
  worse trade than a slightly narrower screen pool.
* Ultrawide ``3440x1440`` capped to weight 2 (the corpus has 12 distinct /24s on it — developers
  over-represent ultrawides; uncapped it would be picked for ~1 launch in 5).
"""

import asyncio
import base64
import gzip
import hashlib
import json
import logging
import sys
import time

logger = logging.getLogger("clearcote")

# ---------------------------------------------------------------------------
# The linux-x64 engine's window frame around an EMULATED viewport: outer = inner + (WIDTH, HEIGHT).
#
# Measured with no persona on 149.0.7827.114/linux-x64 (same 8/131 on 150), constant across every
# viewport probed. win-x64 draws 16/134 instead, which is why launch no longer sizes anything
# against these (see the module docstring). They remain the shared cross-SDK constants behind
# ``headless_geometry``'s viewport and the floor a usable imported-profile screen must clear.
# ---------------------------------------------------------------------------
ENGINE_FRAME_WIDTH = 8
ENGINE_FRAME_HEIGHT = 131

# Windows' taskbar at 100% scaling; the engine's persona table uses the same 40px.
WINDOWS_TASKBAR_HEIGHT = 40

# (screen_width, screen_height, weight, os_hint) — weight is distinct /24s in the corpus.
HEADLESS_SCREEN_PROFILES = (
    (1920, 1080, 24, "windows"),
    (2560, 1440, 13, "windows"),
    (1920, 1200, 6, "linux"),
    (1366, 768, 3, "windows"),
    (1600, 900, 3, "linux"),
    (3440, 1440, 2, "windows"),   # capped from 12 (see module docstring)
    (3840, 2160, 2, "windows"),
    (1680, 1050, 2, "windows"),
)

# Geometry the caller may have chosen; any of them means hands off.
_CALLER_GEOMETRY_KEYS = ("viewport", "no_viewport", "screen")
# Window flags that mean the caller sized the window themselves (the fit skips its resize).
_CALLER_WINDOW_FLAGS = ("--window-size", "--window-position", "--start-maximized")
# Switches that mean the caller set the headless display themselves.
_CALLER_DISPLAY_FLAGS = ("--screen-info",)


def persona_active(args):
    """Whether ``--fingerprint=<seed>`` is on the command line, i.e. the engine will spoof
    ``screen``/``avail`` itself (regime 1). ``light_stealth`` drops that switch on purpose, so this
    is False for it even though a seed was passed to the SDK."""
    return any(str(a).startswith("--fingerprint=") for a in (args or []))


def _pick(seed):
    """Weighted, deterministic choice from ``HEADLESS_SCREEN_PROFILES``.

    Same construction as ``_light_stealth_values``: the full sha256 digest as a big integer, so the
    Python, Node and .NET SDKs select the identical row for a given seed. An unset seed maps to a
    fixed key rather than randomness, so a seedless launch stays reproducible.
    """
    key = str(seed if seed not in (None, "") else "clearcote-headless-geometry")
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    total = sum(row[2] for row in HEADLESS_SCREEN_PROFILES)
    point = h % total
    for row in HEADLESS_SCREEN_PROFILES:
        point -= row[2]
        if point < 0:
            return row
    return HEADLESS_SCREEN_PROFILES[-1]  # unreachable; keeps the return total


_PROFILE_FLAG = "--fingerprint-profile="


def profile_screen_from_args(args):
    """The imported profile's own screen, as ``(width, height)``, or None.

    Reads the value off ``--fingerprint-profile`` (gzip+base64 of the capture JSON) — the single form
    every SDK has in hand here, whatever the caller passed (path, dict or JSON string). Best-effort by
    design: a profile the engine can still use must never fail a launch just because this could not
    read a screen out of it, so every failure returns None and the corpus table is used instead.

    A screen too small to hold the engine's frame is rejected for the same reason the corpus table has
    no tiny rows: the leftover viewport would not lay out a desktop site.
    """
    for arg in (args or []):
        arg = str(arg)
        if not arg.startswith(_PROFILE_FLAG):
            continue
        try:
            raw = gzip.decompress(base64.b64decode(arg[len(_PROFILE_FLAG):]))
            screen = json.loads(raw).get("screen") or {}
            width, height = int(screen["width"]), int(screen["height"])
        except Exception:  # noqa: BLE001
            return None
        if width - ENGINE_FRAME_WIDTH < 1024 or height - ENGINE_FRAME_HEIGHT < 600:
            logger.debug("profile screen %dx%d is too small to size a viewport against", width, height)
            return None
        return (width, height)
    return None


def _geometry_for(screen):
    """Context geometry for a given screen: viewport = screen minus the engine's frame."""
    width, height = screen
    return {
        "screen": {"width": width, "height": height},
        "viewport": {"width": width - ENGINE_FRAME_WIDTH, "height": height - ENGINE_FRAME_HEIGHT},
    }


def headless_geometry(seed=None):
    """A seed's regime-2 screen row: ``{"screen": {...}, "viewport": {...}}``.

    The viewport is the screen minus the linux engine frame. This is the cross-SDK parity contract
    (see the PARITY vector in the tests); launch takes only the screen from it.
    """
    sw, sh, _weight, _os = _pick(seed)
    return _geometry_for((sw, sh))


def _switch_value(args, name):
    """The value of the last ``--name=value`` on the command line, or None."""
    prefix = f"--{name}="
    found = None
    for a in (args or []):
        a = str(a)
        if a.startswith(prefix):
            found = a[len(prefix):]
    return found


def _switch_int(args, name):
    try:
        v = int(_switch_value(args, name))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


_HOST_PLATFORM = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "windows")


def headless_display(seed=None, args=None, light_stealth=False):
    """The regime-2 headless display, as ``{"width", "height", "avail_width", "avail_height"}``.

    In order: an explicit ``--fingerprint-screen-width/height`` (already spoofed into ``screen.*``
    by the engine, so the display must agree), an imported profile's screen, with ``light_stealth``
    (``serve()`` only) the lightStealth row that also supplies the seed's DPR, and otherwise the
    seed's corpus row (``headless_geometry``, identical in every SDK). A Windows platform gets a
    taskbar; others report ``avail == screen``, which real captures do too. Everything else is read
    off the command line, where the platform always is (it defaults to the host OS).
    """
    windows = (_switch_value(args, "fingerprint-platform") or _HOST_PLATFORM) == "windows"

    def with_taskbar(width, height):
        return {"width": width, "height": height, "avail_width": width,
                "avail_height": height - WINDOWS_TASKBAR_HEIGHT if windows else height}

    sw, sh = _switch_int(args, "fingerprint-screen-width"), _switch_int(args, "fingerprint-screen-height")
    if sw and sh:
        d = with_taskbar(sw, sh)
        d["avail_width"] = min(_switch_int(args, "fingerprint-avail-width") or d["avail_width"], sw)
        d["avail_height"] = min(_switch_int(args, "fingerprint-avail-height") or d["avail_height"], sh)
        return d
    from_profile = profile_screen_from_args(args)
    if from_profile:
        return with_taskbar(*from_profile)
    if light_stealth:
        from ._fingerprint import _light_stealth_screen
        d = _light_stealth_screen(seed)
        return d if windows else with_taskbar(d["width"], d["height"])
    sw, sh, _weight, _os = _pick(seed)
    return with_taskbar(sw, sh)


def screen_info_switch(display):
    """``--screen-info`` for a display: its size plus the work-area insets the taskbar takes."""
    insets = "".join(
        f" {k}={v}" for k, v in (("workAreaRight", display["width"] - display["avail_width"]),
                                 ("workAreaBottom", display["height"] - display["avail_height"]))
        if v > 0)
    return f"--screen-info={{{display['width']}x{display['height']}{insets}}}"


def apply_headless_geometry(pw_kwargs, seed=None, args=None):
    """Default a headless launch's geometry in place. Returns what was applied, or None.

    Both regimes set ``no_viewport`` (``inner`` tracks the real window) and leave the window to be
    fitted to the work area on the first page (see ``fit_window_to_work_area``). The result's
    ``"args"`` are switches to APPEND to the command line — in regime 2 the ones that give the
    browser its headless display; the fit and skip checks keep reading the caller's own ``args``.

    ``{"mode": "persona", "args": []}`` is regime 1. ``{"mode": "display", "display": ..., "args":
    [...]}`` is regime 2; ``display`` is None when the caller passed their own ``--screen-info``.
    A caller's own window switch keeps their window (the display is still set under it).

    Skipped when the launch is headed (the real window is already coherent) and when the caller
    expressed ANY geometry intent. ``headless`` unset means headless, matching Playwright.
    """
    if pw_kwargs.get("headless") is False:
        return None
    if any(k in pw_kwargs for k in _CALLER_GEOMETRY_KEYS):
        return None
    pw_kwargs["no_viewport"] = True
    if persona_active(args):
        return {"mode": "persona", "args": []}
    display = None if caller_set_the_display(args) else headless_display(seed, args)
    extra = [screen_info_switch(display)] if display else []
    if not caller_sized_the_window(args):
        extra.append("--window-position=0,0")
    return {"mode": "display", "display": display, "args": extra}


def caller_sized_the_window(args):
    """True when the caller passed their own window geometry flag."""
    return any(str(a).split("=", 1)[0] in _CALLER_WINDOW_FLAGS for a in (args or []))


def caller_set_the_display(args):
    """True when the caller passed their own headless display switch."""
    return any(str(a).split("=", 1)[0] in _CALLER_DISPLAY_FLAGS for a in (args or []))


# A plain expression, NOT "() => [...]": Playwright's JS/.NET bindings evaluate an
# arrow-function string to a function object rather than calling it, which silently broke the
# Node port. An expression behaves the same in every binding.
_WORKAREA_JS = "[screen.availWidth, screen.availHeight]"
_OUTER_JS = "[outerWidth, outerHeight]"


def _plausible(area):
    """Guard against fitting the window to a nonsense work area: with no display set the page
    reports the headless default (800x600), and 'maximizing' to that would be worse than leaving
    the window alone."""
    return bool(area) and len(area) == 2 and area[0] >= 1024 and area[1] >= 600


def _bounds(width, height):
    return {"left": 0, "top": 0, "width": int(width), "height": int(height)}


def _fit_plan(avail, outer, room=None):
    """The bounds correction, given what the window reported after the first attempt.

    Requested bounds and reported ``outerHeight`` are NOT always the same quantity: on 149 the
    window reported 33px less than the bounds height it was given, and on 150 a persona's window
    does (33px in a launch, 89 in serve(); without a persona it reports the bounds exactly). Fitting
    bounds to the work area then lands short of maximized (real maximized captures have
    ``outer == avail``). Rather than hardcode it, measure the shortfall and add it back — that
    self-tunes if the engine changes.

    ``room`` is the space from the window's position to the work area's far edges. On an axis where
    the window is smaller than that, the correction never takes the bounds past the edge: only a
    window that fills the work area needs bounds beyond it (the persona engine then clamps the window
    to the work area), and an engine without a persona does not clamp at all.

    Returns None when nothing needs correcting, else the ``(width, height)`` to request. Never asks
    for MORE than the shortfall.
    """
    dw = avail[0] - outer[0]
    dh = avail[1] - outer[1]
    if dw <= 0 and dh <= 0:
        return None
    plan = [avail[0] + max(dw, 0), avail[1] + max(dh, 0)]
    for i in (0, 1):
        if room and avail[i] < room[i]:
            plan[i] = min(plan[i], room[i])
    return tuple(plan)


# How long a window resize may take to reach the page before a read is taken as it stands.
_SETTLE_S = 1.0
_SETTLE_POLL_S = 0.02


def _same_size(a, b):
    return a[0] == b[0] and a[1] == b[1]


def _settled_outer(read, before, want, timeout=None, sleep=time.sleep):
    """Read ``[outerWidth, outerHeight]`` until it reflects the last ``Browser.setWindowBounds``.

    The call returns before the page has the new window size, so a read straight after it can still
    report the previous one (measured on 150/linux-x64: for tens of milliseconds). Correcting
    from such a read once asked for 1440+495 px on a 1920 px screen. Settled means the asked-for
    size, or a size other than ``before`` that is still there one poll later.

    Returns ``(outer, settled)``; ``settled`` is False when neither happened within ``timeout``.
    """
    deadline = time.monotonic() + (_SETTLE_S if timeout is None else timeout)
    outer = read()
    while True:
        if _same_size(outer, want):
            return outer, True
        if time.monotonic() >= deadline:
            return outer, False
        sleep(_SETTLE_POLL_S)
        nxt = read()
        if not _same_size(outer, before) and _same_size(nxt, outer):
            return outer, True
        outer = nxt


async def _settled_outer_async(read, before, want, timeout=None):
    """Async mirror of ``_settled_outer`` (``read`` is a coroutine function)."""
    deadline = time.monotonic() + (_SETTLE_S if timeout is None else timeout)
    outer = await read()
    while True:
        if _same_size(outer, want):
            return outer, True
        if time.monotonic() >= deadline:
            return outer, False
        await asyncio.sleep(_SETTLE_POLL_S)
        nxt = await read()
        if not _same_size(outer, before) and _same_size(nxt, outer):
            return outer, True
        outer = nxt


def _fit_window(set_bounds, read, target, room, timeout=None):
    """Give the window ``target``, then correct what the engine reports (see ``_fit_plan``), on
    settled reads only: a correction is planned from a size the page has actually seen, and kept only
    when the page then sees a window no larger than ``target``; otherwise the bounds go back to
    ``target``. With no settled read there is nothing to correct from, and ``target`` lies inside the
    work area. Returns the last outer size read."""
    w, h = target
    before = read()
    set_bounds(w, h)
    outer, settled = _settled_outer(read, before, target, timeout)
    plan = _fit_plan(target, outer, room) if settled else None
    if not plan:
        return outer
    set_bounds(*plan)
    fixed, settled = _settled_outer(read, outer, plan, timeout)
    # Overshooting would trade one impossible geometry for another (outer > target).
    if settled and fixed[0] <= w and fixed[1] <= h:
        return fixed
    logger.debug("window fit correction not kept (%r for %r); reverting", fixed, target)
    set_bounds(w, h)
    return _settled_outer(read, fixed, target, timeout)[0]


async def _fit_window_async(set_bounds, read, target, room, timeout=None):
    """Async mirror of ``_fit_window`` (``set_bounds`` and ``read`` are coroutine functions)."""
    w, h = target
    before = await read()
    await set_bounds(w, h)
    outer, settled = await _settled_outer_async(read, before, target, timeout)
    plan = _fit_plan(target, outer, room) if settled else None
    if not plan:
        return outer
    await set_bounds(*plan)
    fixed, settled = await _settled_outer_async(read, outer, plan, timeout)
    if settled and fixed[0] <= w and fixed[1] <= h:
        return fixed
    logger.debug("window fit correction not kept (%r for %r); reverting", fixed, target)
    await set_bounds(w, h)
    return (await _settled_outer_async(read, fixed, target, timeout))[0]


def fit_window_to_work_area(page, args=None):
    """Size the headless window to the display's work area — the persona's (regime 1) or the one
    ``--screen-info`` set (regime 2) — so the page reports a maximized window (``outer == avail``)
    instead of the headless default window sitting inside a much larger screen.

    Note ``--start-maximized`` and CDP ``windowState: "maximized"`` are both no-ops in headless
    (measured — the window stays at its default size), which is why this sets explicit bounds.

    Returns the ``(width, height)`` the window ended up reporting, or None if skipped. Never raises:
    a geometry improvement must not be able to fail a launch.
    """
    if caller_sized_the_window(args):
        return None
    try:
        avail = page.evaluate(_WORKAREA_JS)
        if not _plausible(avail):
            logger.debug("skipping window fit: implausible work area %r", avail)
            return None
        cdp = page.context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        target = (avail[0], avail[1])
        outer = _fit_window(
            lambda w, h: cdp.send("Browser.setWindowBounds",
                                  {"windowId": window_id, "bounds": _bounds(w, h)}),
            lambda: page.evaluate(_OUTER_JS), target, target)
        return (outer[0], outer[1])
    except Exception as exc:  # noqa: BLE001
        logger.debug("window fit skipped: %s", exc)
        return None


async def fit_window_to_work_area_async(page, args=None):
    """Async mirror of ``fit_window_to_work_area``."""
    if caller_sized_the_window(args):
        return None
    try:
        avail = await page.evaluate(_WORKAREA_JS)
        if not _plausible(avail):
            logger.debug("skipping window fit: implausible work area %r", avail)
            return None
        cdp = await page.context.new_cdp_session(page)
        window = await cdp.send("Browser.getWindowForTarget")
        window_id = window["windowId"]
        target = (avail[0], avail[1])

        async def set_bounds(w, h):
            await cdp.send("Browser.setWindowBounds",
                           {"windowId": window_id, "bounds": _bounds(w, h)})

        async def read():
            return await page.evaluate(_OUTER_JS)

        outer = await _fit_window_async(set_bounds, read, target, target)
        return (outer[0], outer[1])
    except Exception as exc:  # noqa: BLE001
        logger.debug("window fit skipped: %s", exc)
        return None


def geometry_is_coherent(screen, avail, inner, outer):
    """``inner <= outer <= avail <= screen`` on both axes — the chain a real window satisfies.

    Exposed because it is the actual invariant under test; takes ``(width, height)`` pairs.
    """
    return (
        inner[0] <= outer[0] and inner[1] <= outer[1]
        and outer[0] <= avail[0] and outer[1] <= avail[1]
        and avail[0] <= screen[0] and avail[1] <= screen[1]
    )


# ---------------------------------------------------------------------------------------------------
# serve(): a raw CDP endpoint. Port of the Node SDK's section of the same name.
#
# Everything above rides on Playwright: ``no_viewport`` is a context option and the fit runs on each
# context's first page. A raw endpoint has neither, and a CDP emulation override would not survive
# either, being scoped to the session that set it. What every target and every client of a served
# browser inherits is the headless DISPLAY and the real WINDOW, both browser-level. Untouched, a
# served headless browser reports the 800x600 surface as its screen (measured on 153/win-x64).
#
# REGIME 2 (no persona): ``--screen-info``, as in launch, makes the headless display a real-machine
# size before the first document exists, so screen.*, device-width media queries, window clamping
# and popup placement all agree. The one difference from launch is which row: serve() takes a
# light_stealth seed's own row (its DPR's pair), launch the cross-SDK one.
#
# REGIME 1 (persona): the persona picks its display inside the engine, so it is only known once a
# page can be asked. ``Emulation.updateScreen`` (headless-only, browser-level, outlives the session
# that sent it) then resizes the headless display to match.
#
# BOTH: one ``Browser.setWindowBounds`` puts the first window on the work area, and
# ``--window-position`` puts later windows at the work-area origin. Deliberately no ``--window-size``:
# it forces every popup to that size, ignoring the window.open() features real Chrome honours.
# ---------------------------------------------------------------------------------------------------

def served_geometry(engine_args, seed=None, light_stealth=False, headless=True):
    """What serve() adds for a headless launch, or None to leave geometry alone (headed, or the
    caller passed a window or display switch of their own; the SDK's own android ``--window-size``
    counts, since a phone persona sizes itself).

    Returns ``{"persona": bool, "display": dict|None, "args": [...]}``.
    """
    if not headless or caller_sized_the_window(engine_args) or caller_set_the_display(engine_args):
        return None
    origin = "--window-position=0,0"
    if persona_active(engine_args):
        return {"persona": True, "display": None, "args": [origin]}
    display = headless_display(seed, engine_args, light_stealth=light_stealth)
    return {"persona": False, "display": display, "args": [screen_info_switch(display), origin]}


def validate_window_size(window_size):
    """``window_size`` as ``(width, height)`` whole CSS px in 100-10000, or None. Accepts a
    ``{"width", "height"}`` dict or a pair; anything else raises ``TypeError``."""
    if window_size is None:
        return None
    if isinstance(window_size, dict):
        pair = (window_size.get("width"), window_size.get("height"))
    elif isinstance(window_size, (tuple, list)) and len(window_size) == 2:
        pair = tuple(window_size)
    else:
        pair = None
    if not pair or not all(isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 10000
                           for v in pair):
        raise TypeError(
            "clearcote serve: window_size must be {'width', 'height'} in whole CSS px, 100-10000")
    return pair


_DISPLAY_JS = ("[screen.width, screen.height, screen.availLeft, screen.availTop, "
               "screen.availWidth, screen.availHeight]")


def fit_window_over_cdp(cdp, persona, window_size=None, settle_timeout=None):
    """Put the served browser's first window on its display's work area (or ``window_size``, clamped
    into it); under a persona, first make the headless display the persona's own.

    Over a browser-level CDP connection (anything with ``send(method, params, session_id)``), on the
    first page target. Only Target/Emulation/Browser commands plus Runtime.evaluate reads (never
    Runtime.enable) on that page, and every change is browser-level, so nothing lingers on the page
    when the connection closes.

    Returns ``{"display": {...}, "outer": (w, h)}``, or None when it could not act (no page, an
    implausible display, or an engine without ``Emulation.updateScreen`` under a persona, whose
    window cannot outgrow 800x600 without it). Never raises. ``settle_timeout`` (seconds) bounds the
    wait for each resize to reach the page (``_settled_outer``).
    """
    session_id = None
    try:
        targets = cdp.send("Target.getTargets").get("targetInfos") or []
        page = next((t for t in targets if t.get("type") == "page"), None)
        if not page:
            return None
        session_id = cdp.send("Target.attachToTarget",
                              {"targetId": page["targetId"], "flatten": True})["sessionId"]

        def read(expression):
            res = cdp.send("Runtime.evaluate",
                           {"expression": expression, "returnByValue": True}, session_id)
            return res["result"]["value"]

        sw, sh, al, at, aw, ah = read(_DISPLAY_JS)
        if not _plausible([aw, ah]) or aw > sw or ah > sh:
            return None
        if persona:
            screens = cdp.send("Emulation.getScreenInfos").get("screenInfos") or []
            primary = next((s for s in screens if s.get("isPrimary")), screens[0] if screens else None)
            if not primary:
                return None
            cdp.send("Emulation.updateScreen", {
                "screenId": primary["id"], "left": 0, "top": 0, "width": sw, "height": sh,
                "workAreaInsets": {"left": al, "top": at,
                                   "right": sw - al - aw, "bottom": sh - at - ah},
            })
        # The asked-for size, never past the work area; a window smaller than it sits 10px in, like
        # Chrome's own first placement.
        w = min(int(round(window_size[0])), aw) if window_size else aw
        h = min(int(round(window_size[1])), ah) if window_size else ah
        left, top = al + min(10, aw - w), at + min(10, ah - h)
        window_id = cdp.send("Browser.getWindowForTarget",
                             {"targetId": page["targetId"]})["windowId"]

        def set_bounds(bw, bh):
            cdp.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": {
                "left": left, "top": top, "width": int(round(bw)), "height": int(round(bh))}})

        # Same as fit_window_to_work_area: an engine that reports less than the bounds it was given
        # gets the shortfall added back, an overshoot is reverted, and only settled reads count.
        outer = _fit_window(set_bounds, lambda: read(_OUTER_JS), (w, h),
                            (al + aw - left, at + ah - top), settle_timeout)
        return {"display": {"width": sw, "height": sh, "avail_width": aw, "avail_height": ah},
                "outer": (outer[0], outer[1])}
    except Exception as exc:  # noqa: BLE001
        logger.debug("served window fit skipped: %s", exc)
        return None
    finally:
        if session_id:
            try:
                cdp.send("Target.detachFromTarget", {"sessionId": session_id})
            except Exception:  # noqa: BLE001
                pass


def fit_served_window(ws_url, persona, window_size=None, timeout=5.0):
    """``fit_window_over_cdp`` on the SDK's own short connection to a served browser. Never raises."""
    if not ws_url:
        return None
    from ._cdpws import CdpConnection
    cdp = None
    try:
        cdp = CdpConnection(ws_url, timeout=timeout)
        return fit_window_over_cdp(cdp, persona, window_size)
    except Exception as exc:  # noqa: BLE001
        logger.debug("served window fit skipped: %s", exc)
        return None
    finally:
        if cdp:
            cdp.close()
