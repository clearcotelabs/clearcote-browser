"""This computer's clock against a token's nbf / exp, the way the engine checks it.

The engine refuses a run token (and a persona token) that, by THIS computer's clock, is not valid yet or already
expired. A clock far behind or ahead then shows up as a licence refusal ("no valid license lease for this run") that
no licence key fixes (CR-2VFYCD: a Windows clock a minute slow refused a free key and a Pro key alike). So before a
launch, check the token the same way and, when the engine would refuse, say the clock is off, by how much, and how
to fix it.

The servers backdate nbf by 15 minutes, so only a clock off by more than that is caught. ``iat`` is the server's
time when it minted the token, so ``iat - now`` is a lower bound on how far behind this clock runs ("at least").

Mirrors the Node SDK's clockcheck.ts, the .NET SDK's ClockCheck.cs and the Profile Manager's clockcheck.ts.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone

from ._license import LicenseError


class ClockSkewError(LicenseError):
    """The engine refuses the token because this computer's clock is off. ``skew_sec`` < 0: the clock runs behind."""

    code = "CLOCK_SKEW"

    def __init__(self, message: str, skew_sec: float):
        super().__init__(message, code="CLOCK_SKEW")
        self.skew_sec = skew_sec


_FIX = ('Set the clock to update automatically (Windows: Settings > Time & language > Date & time > '
        '"Set time automatically", then "Sync now"; macOS: System Settings > General > Date & Time; '
        'Linux: `timedatectl set-ntp true`) and launch again.')


def _times(token: str):
    try:
        body = token.split(".")[0]
        p = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if not isinstance(p, dict):
        return None

    def num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    return num(p.get("nbf")), num(p.get("iat")), num(p.get("exp"))


def format_duration(sec: float) -> str:
    """'3 min 12 s', '2 h 5 min', '1 day 4 h'."""
    s = max(1, int(round(abs(sec))))
    d, h, m, r = s // 86400, (s % 86400) // 3600, (s % 3600) // 60, s % 60
    if d:
        return f"{d} day{'' if d == 1 else 's'}" + (f" {h} h" if h else "")
    if h:
        return f"{h} h" + (f" {m} min" if m else "")
    if m:
        return f"{m} min" + (f" {r} s" if r else "")
    return f"{r} s"


def _utc(sec: float) -> str:
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def clock_skew_problem(token: str, now: float | None = None, what: str = "licence token"):
    """``(message, skew_sec)`` for a token the engine would refuse on this clock, else None."""
    t = _times(token) if token else None
    if not t:
        return None
    nbf, iat, exp = t
    now = time.time() if now is None else now
    if nbf is not None and now < nbf:
        issued = iat if iat is not None else nbf
        behind = issued - now
        return (f"This computer's clock is at least {format_duration(behind)} behind (it reads {_utc(now)}; the server "
                f"issued this {what} at {_utc(issued)}). The browser checks the {what} against this computer's clock "
                f"and refuses one that is not valid yet, whatever the licence key. {_FIX}", -behind)
    if exp is not None and now >= exp:
        ahead = now - (iat if iat is not None else exp)
        issued = f", although the server issued it at {_utc(iat)}" if iat is not None else ""
        return (f"This computer's clock looks wrong: it reads {_utc(now)}, after the moment this {what} expires "
                f"({_utc(exp)}){issued}. The browser refuses an expired {what}, whatever the licence key. {_FIX}", ahead)
    return None


def assert_clock_for(token, what: str = "licence token", now: float | None = None) -> None:
    """Raise :class:`ClockSkewError` when the engine would refuse ``token`` on this computer's clock."""
    if not token:
        return
    p = clock_skew_problem(token, now, what)
    if p:
        raise ClockSkewError(*p)
