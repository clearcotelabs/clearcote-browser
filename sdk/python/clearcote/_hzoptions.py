"""``humanize=`` options: the object form of the switch, the fallback report and strict mode.

``humanize`` accepts ``True`` / ``False`` (as before), ``"strict"`` (``HumanizeOptions(strict=True)``), a
:class:`HumanizeOptions`, or a plain dict with the same keys (an unknown key raises). ``humanize=True`` is
exactly ``HumanizeOptions()``: every default below reproduces the behaviour of 0.51.0, and no option draws
from the persona's RNG stream, so a seed keeps the same motor persona whatever options are given.

Every place where the humanized route hands an action to Playwright's own (stock, untimed) call reports
it through :func:`report_fallback`: in the default mode ``on_fallback`` is called with
``{"api", "reason", "selector"}``; with ``strict=True`` a :class:`ClearcoteStrictError` (a subclass of
Playwright's ``Error``) is raised instead and the stock call does not run.

Shared by the sync and async humanizers; the Node (``humanize.ts``) and .NET (``Humanize.cs``) SDKs carry
the same options with the same meaning.
"""

from __future__ import annotations

import contextvars
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable, Optional

try:  # the Playwright error class a caller already catches
    from playwright._impl._errors import Error as _PlaywrightError
except Exception:  # noqa: BLE001 - playwright is a dependency; this only guards odd installs
    class _PlaywrightError(Exception):  # type: ignore[no-redef]
        pass

PRESETS = {"careful": 1.4, "default": 1.0, "quick": 0.7}
ENGINE_MODES = ("auto", "all", "coords", "off")
AMBIENT_MODES = ("off", "load", "idle")
HANDEDNESS = ("left", "right")
INPUT_DEVICES = ("mouse", "trackpad")


class ClearcoteStrictError(_PlaywrightError):
    """Raised in ``humanize={"strict": True}`` mode where the humanized route would have handed the action
    to Playwright's own (stock, untimed) call. A subclass of Playwright's ``Error``, so code that already
    catches Playwright errors catches it too. ``api``, ``reason`` and ``selector`` say which call and why."""

    def __init__(self, message, api=None, reason=None, selector=None):
        super().__init__(message)
        self.api = api
        self.reason = reason
        self.selector = selector


@dataclass
class HumanizeOptions:
    """The ``humanize=`` options (all optional; ``HumanizeOptions()`` is ``humanize=True``).

    * ``preset`` -- "careful" | "default" | "quick": every planned duration (glides, presses, key dwell and
      gaps) x1.4 / x1.0 / x0.7, and the rate of thinking pauses scaled the same way.
    * ``strict`` -- raise :class:`ClearcoteStrictError` instead of falling back to a stock Playwright call.
    * ``engine`` -- which clicks the engine's pointer path walks: "auto" (CLEARCOTE_HUMANIZE_ENGINE_CLICKS,
      else "coords"), "all", "coords", "off". Replaces the environment variable when not "auto".
    * ``ambient`` -- "off" | "load" | "idle": pointer activity without a target ("load": a short ambient
      burst on every page load, the old ``page._clearcote_auto_ambient``; "idle": idle drifts between
      actions). ``None`` = "off".
    * ``attention`` -- the reading pause after a page load (0.8-4 s, longer on a page with more text) and the
      idle drifts between actions (SC 232 lane C). On unless ``False`` (``None`` = on, since 0.68.0);
      ``ambient="idle"`` turns it on as well.
    * ``typo_rate`` -- per-character slip rate 0..1; ``None`` = CLEARCOTE_HUMANIZE_TYPO_RATE, else 0.02.
    * ``handedness`` -- "left" | "right" | ``None`` (the persona's).
    * ``device_hz`` -- the pointer's report rate (Hz) in place of the persona's; ``None`` = the persona's.
    * ``input_device`` -- "mouse" | "trackpad" | ``None`` (the persona's).
    * ``on_fallback`` -- called with ``{"api", "reason", "selector"}`` each time an action is handed to
      Playwright's own call (default mode only).
    """

    preset: str = "default"
    strict: bool = False
    engine: str = "auto"
    ambient: Optional[str] = None
    attention: Optional[bool] = None
    typo_rate: Optional[float] = None
    handedness: Optional[str] = None
    device_hz: Optional[int] = None
    input_device: Optional[str] = None
    on_fallback: Optional[Callable[[dict], Any]] = None

    def __post_init__(self):
        if self.preset not in PRESETS:
            raise ValueError("humanize preset must be 'careful', 'default' or 'quick', not %r" % (self.preset,))
        if not isinstance(self.strict, bool):
            raise TypeError("humanize strict must be True or False")
        if self.engine not in ENGINE_MODES:
            raise ValueError("humanize engine must be 'auto', 'all', 'coords' or 'off', not %r" % (self.engine,))
        if self.ambient is not None and self.ambient not in AMBIENT_MODES:
            raise ValueError("humanize ambient must be 'off', 'load' or 'idle', not %r" % (self.ambient,))
        if self.attention is not None and not isinstance(self.attention, bool):
            raise TypeError("humanize attention must be True, False or None")
        if self.typo_rate is not None:
            if isinstance(self.typo_rate, bool) or not isinstance(self.typo_rate, (int, float)) \
                    or self.typo_rate != self.typo_rate or not 0.0 <= float(self.typo_rate) <= 1.0:
                raise ValueError("humanize typo_rate must be a number from 0 to 1")
            self.typo_rate = float(self.typo_rate)
        if self.handedness is not None and self.handedness not in HANDEDNESS:
            raise ValueError("humanize handedness must be 'left' or 'right', not %r" % (self.handedness,))
        if self.device_hz is not None:
            if isinstance(self.device_hz, bool) or not isinstance(self.device_hz, int) or not 30 <= self.device_hz <= 8000:
                raise ValueError("humanize device_hz must be a whole number of Hz from 30 to 8000")
        if self.input_device is not None and self.input_device not in INPUT_DEVICES:
            raise ValueError("humanize input_device must be 'mouse' or 'trackpad', not %r" % (self.input_device,))
        if self.on_fallback is not None and not callable(self.on_fallback):
            raise TypeError("humanize on_fallback must be callable")

    @property
    def tempo(self) -> float:
        """The duration multiplier of the preset (1.4 / 1.0 / 0.7)."""
        return PRESETS[self.preset]

    def to_payload(self) -> dict:
        """The JSON-able options (no callback; unset values left out), camelCase as the cloud API names
        them -- what a cloud launch sends as ``humanizeOptions``."""
        out = {}
        names = {"typo_rate": "typoRate", "device_hz": "deviceHz", "input_device": "inputDevice"}
        for k, v in asdict(self).items():
            if k == "on_fallback" or v is None:
                continue
            out[names.get(k, k)] = v
        return out


_KEYS = tuple(f.name for f in fields(HumanizeOptions))
# camelCase spellings accepted in a dict too, so one options dict works in Python and in the cloud API
_ALIASES = {"typoRate": "typo_rate", "deviceHz": "device_hz", "inputDevice": "input_device",
            "onFallback": "on_fallback"}


def resolve(value) -> Optional[HumanizeOptions]:
    """``humanize=`` as given -> a HumanizeOptions, or None for off. Raises on a value it does not know
    (an unknown key, a bad preset): at launch, before anything is started."""
    if value is None or value is False:
        return None
    if value is True:
        return HumanizeOptions()
    if isinstance(value, HumanizeOptions):
        return value
    if isinstance(value, str):
        if value == "strict":
            return HumanizeOptions(strict=True)
        raise ValueError("humanize must be True, False, 'strict' or a HumanizeOptions/dict, not %r" % (value,))
    if isinstance(value, dict):
        kw = {}
        for k, v in value.items():
            name = _ALIASES.get(k, k)
            if name not in _KEYS:
                raise TypeError("unknown humanize option %r (known: %s)" % (k, ", ".join(_KEYS)))
            kw[name] = v
        return HumanizeOptions(**kw)
    raise TypeError("humanize must be True, False, 'strict' or a HumanizeOptions/dict, not %s" % type(value).__name__)


def normalize(value):
    """For launch(): False when off, else the resolved HumanizeOptions (validated now, not after launch)."""
    o = resolve(value)
    return False if o is None else o


# The public API a call came in through ("Locator.click" when a locator routes through page.click). A
# ContextVar, so concurrent asyncio tasks each keep their own.
_CALLER = contextvars.ContextVar("clearcote_humanize_caller", default=None)


def caller_api(api):
    """Context manager-ish token: mark ``api`` as the public entry point for nested reports."""
    return _CALLER.set(api)


def reset_caller(token):
    try:
        _CALLER.reset(token)
    except Exception:  # noqa: BLE001
        pass


def options_of(page) -> Optional[HumanizeOptions]:
    o = getattr(page, "_clearcote_hz", None)
    return o if isinstance(o, HumanizeOptions) else None   # a stand-in page may answer any attribute


def report_fallback(page, api, reason, selector=None):
    """One humanized action is about to be handed to Playwright's own call. Default mode: tell
    ``on_fallback`` (its errors propagate: raising there is how a caller stops the stock call). Strict
    mode: raise ClearcoteStrictError instead. Does nothing on a page without humanize options. Either way the
    event is kept as ``page._clearcote_last_fallback`` (the newest one; a test and hook-less caller seam)."""
    o = options_of(page)
    outer = _CALLER.get()
    api = outer or api
    event = {"api": api, "reason": reason, "selector": selector}
    try:
        page._clearcote_last_fallback = event
    except Exception:  # noqa: BLE001 - a page-like object that refuses attributes
        pass
    if o is None:
        return event
    if o.strict:
        raise ClearcoteStrictError(
            "humanize strict: %s would fall back to a stock Playwright call (%s)"
            % ("%r" % (selector,) if selector else "the action", reason),
            api=api, reason=reason, selector=selector)
    if o.on_fallback is not None:
        o.on_fallback(event)
    return event


def engine_mode(page, env_mode: Callable[[], str]) -> str:
    """The engine click mode: the page's option unless "auto", else the environment's."""
    o = options_of(page)
    if o is not None and o.engine != "auto":
        return o.engine
    return env_mode()


def typo_rate_for(page, env_rate: Callable[[], float]) -> float:
    o = options_of(page)
    if o is not None and o.typo_rate is not None:
        return o.typo_rate
    return env_rate()


def tempo_of(page) -> float:
    o = options_of(page)
    return 1.0 if o is None else o.tempo


def apply_to_persona(persona, o: HumanizeOptions):
    """The options a planner reads from the persona, set as plain attributes after make_persona (no RNG
    draw, so the seed -> persona map is unchanged). ``tempo`` scales every timing window; the windows
    themselves are scaled here so the existing planners (click_hold, key_dwell, drag_dwell) follow at once.
    Planners of other lanes read ``tempo`` / ``pause_scale`` / ``handedness`` / ``input_device`` with
    ``getattr(persona, name, default)``."""
    t = o.tempo
    persona.tempo = t
    persona.pause_scale = t
    persona.handedness = o.handedness
    if o.handedness in HANDEDNESS and hasattr(persona, "approach_bias"):
        # the persona's handedness is the sign of its approach bias (path bow side, click side bias): the option
        # fixes the sign and keeps the drawn magnitude ("right" = positive, as a right hand's bias)
        b = abs(float(persona.approach_bias))
        persona.approach_bias = b if o.handedness == "right" else -b
    persona.input_device = o.input_device
    if t != 1.0:
        for name in ("grab_min_ms", "grab_max_ms", "release_min_ms", "release_max_ms", "click_hold_min_ms",
                     "click_hold_max_ms", "key_dwell_min_ms", "key_dwell_max_ms"):
            if hasattr(persona, name):
                setattr(persona, name, getattr(persona, name) * t)
    if o.device_hz is not None:
        persona.device_hz = o.device_hz
    return persona

