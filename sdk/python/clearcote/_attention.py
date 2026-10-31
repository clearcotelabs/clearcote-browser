"""The attention model (SC 232 lane C): what a reader does BETWEEN the actions a script asks for.

Pure planning, no Playwright: the humanizers (``_humanize.py`` / ``_humanize_async.py``) call these at
the start of the NEXT action -- never from a timer or a background thread, so nothing races the page's
input lock -- and sleep / dispatch what they return. Same values in ``attention.ts`` and ``Attention.cs``.

* After a ``load`` the first action waits a READ PAUSE of 0.8-4 s, scaled by how much text the page shows
  (measured in the isolated world, capped at READ_CAP_CHARS), and in EXPLORE_RATE of loads rolls the wheel
  1-3 notches first (the eye wanders down the page before the hand acts).
* When more than IDLE_GAP_S pass between two actions, the hand idles: 1-2 ambient drifts of 300-900 ms
  (``plan_ambient`` trimmed by ``_pacing.trim_to``) before the next action.

ON by default since 0.68.0 (SC 299). Measured on r54 / SDK 0.67.1 (SC 298/299): with the model off, the first
input came 0.21-0.31 s after the load on every persona, as fast as a stock Playwright script. Off with the
option ``attention=False``; the page flag ``page._clearcote_attention`` (True / False) overrides both ways.
"""
from __future__ import annotations

import math
import random
from typing import Callable, Optional

from ._hzoptions import options_of
from ._motion import Persona, _clamp, _lerp, _tempo, after_traits

Rng = Callable[[], float]

# The read pause band (ms) and the text length that saturates it.
READ_MIN_MS = 800.0
READ_MAX_MS = 4000.0
READ_CAP_CHARS = 20000
# Share of loads followed by 1-3 exploratory wheel notches before the first action.
EXPLORE_RATE = 0.30
# A gap between two actions longer than this is idle time the hand fills with a drift or two.
IDLE_GAP_S = 1.5
IDLE_DRIFT_MIN_MS = 300.0
IDLE_DRIFT_MAX_MS = 900.0

# Visible text length of the document, capped (innerText is what a reader sees; the cap bounds the read on
# a long page and the measurement's cost). Run in the isolated world like every other humanize read.
TEXT_LENGTH = ("() => { const b = document.body; if (!b) return 0; let t = ''; try { t = b.innerText || ''; }"
               " catch (e) { t = b.textContent || ''; } return Math.min(%d, t.length); }" % READ_CAP_CHARS)


def read_pause_ms(text_len, p: Persona, rng: Optional[Rng] = None) -> float:
    """How long the reader looks at a freshly loaded page before the first action: READ_MIN_MS for an
    empty page, READ_MAX_MS from READ_CAP_CHARS of text (square-root growth in between: the first lines
    are what decide where to click), x the persona's read tempo x 0.85..1.15, kept in the band."""
    if rng is None:
        rng = random.random
    try:
        n = float(text_len or 0)
    except (TypeError, ValueError):
        n = 0.0
    frac = math.sqrt(_clamp(n / READ_CAP_CHARS, 0.0, 1.0))
    t = _tempo(p)   # the preset (careful / quick) scales the read pause and its band
    ms = (READ_MIN_MS + (READ_MAX_MS - READ_MIN_MS) * frac) * after_traits(p).read_tempo * t * _lerp(0.85, 1.15, rng())
    return _clamp(ms, READ_MIN_MS * t, READ_MAX_MS * t)


def explore_notches(rng: Optional[Rng] = None) -> int:
    """0 (most loads) or 1-3 exploratory wheel notches down, in EXPLORE_RATE of loads."""
    if rng is None:
        rng = random.random
    if rng() >= EXPLORE_RATE:
        return 0
    return 1 + int(rng() * 3)


def idle_drifts(gap_s, rng: Optional[Rng] = None):
    """The ambient drifts (each's length in ms) the hand makes after ``gap_s`` seconds without an action:
    none under IDLE_GAP_S, else 1-2 of IDLE_DRIFT_MIN_MS..IDLE_DRIFT_MAX_MS."""
    if rng is None:
        rng = random.random
    if gap_s is None or gap_s <= IDLE_GAP_S:
        return []
    n = 1 + (1 if rng() < 0.5 else 0)
    return [_lerp(IDLE_DRIFT_MIN_MS, IDLE_DRIFT_MAX_MS, rng()) for _ in range(n)]


def option(page, name, default=None):
    """Read a humanize option off the page: the page's ``HumanizeOptions`` (``_hzoptions.options_of``), else a
    stand-in ``page._clearcote_options`` (dict or object, the tests' seam), else ``default``. Never raises."""
    opts = options_of(page)
    if opts is None:
        opts = getattr(page, "_clearcote_options", None)
    if opts is None:
        return default
    try:
        if isinstance(opts, dict):
            return opts.get(name, default)
        return getattr(opts, name, default)
    except Exception:  # noqa: BLE001
        return default


def attention_enabled(page) -> bool:
    """Is the attention model on for this page: the page flag ``_clearcote_attention`` when it is True or False,
    else on unless the ``attention`` option is False -- or when ``ambient="idle"`` asks for it."""
    flag = page.__dict__.get("_clearcote_attention") if hasattr(page, "__dict__") else None
    if flag is True or flag is False:
        return flag
    if ambient_mode(page) == "idle":
        return True
    return option(page, "attention", None) is not False


def ambient_mode(page) -> str:
    """The page's ambient mode: lane A's ``ambient`` option ("off" | "load" | "idle"), else "off". A
    truthy ``attention`` option maps to "idle"."""
    v = option(page, "ambient", None)
    if isinstance(v, str) and v.strip().lower() in ("off", "load", "idle"):
        return v.strip().lower()
    if option(page, "attention", False):
        return "idle"
    return "off"
