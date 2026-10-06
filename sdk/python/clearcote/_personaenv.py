"""Keep the persona off the browser's own command line (engine patch 1021, "env mode").

A browser's command line is readable by any local user (``/proc/<pid>/cmdline`` on Linux, the process
environment block on Windows), and Clearcote's carries the seed, every persona override, proxy
credentials (``--proxy-auth``, ``--socks5-credentials``) and the canvas-bridge token. An engine with
patch 1021 already moves those switches off every CHILD process's command line: they travel in the
``CLEARCOTE_PERSONA_ARGS`` environment variable, which the children inherit. With
``--persona-from-env`` the browser process reads them from that variable too, so the launcher can keep
them off the browser's own command line as well. That is what this module does.

It acts only when the engine implements ``--persona-from-env`` (probed in the binary, like every other
new engine switch). An older engine gets the switches on its command line exactly as before.

The payload format and the switch list mirror ``components/ungoogled/persona_transport.cc``: base64 of
``name\\0value\\0name\\0value\\0`` with switch names that engine accepts. The engine stops the launch on
a payload it cannot decode or on a name outside its list, so the list here must never be wider than
the engine's. Chromium keeps the LAST copy of a repeated switch on a command line, while the engine
adopts the FIRST matching entry of the payload, so each name goes in once, with its last value.

Turn it off with ``persona_env=False`` (launch options) or ``CLEARCOTE_PERSONA_ENV=0``.
"""
from __future__ import annotations

import base64
import os

ENV_VAR = "CLEARCOTE_PERSONA_ARGS"
FROM_ENV_SWITCH = "persona-from-env"
KILL_SWITCH = "disable-persona-env-transport"  # the engine's own switch back to the pre-1021 behaviour
OPT_OUT_ENV = "CLEARCOTE_PERSONA_ENV"
# The engine's cap for the variable it republishes for its children (Windows caps one environment
# variable at 32,767 characters). Above it the switches simply stay on the command line.
MAX_PAYLOAD = 30000

_NAMES = frozenset({
    "disable-canvas-noise", "disable-fingerprint-noise", "disable-fingerprint-voices",
    "disable-gpu-fingerprint", "disable-gpu-string-spoof", "proxy-auth", "socks5-credentials",
    "webrtc-ip",
})


def is_transported(name: str) -> bool:
    """Whether the engine carries switch ``name`` (no leading dashes) in the environment."""
    return (name == "fingerprint" or name.startswith("fingerprint-") or name.startswith("canvas-bridge-")
            or name in _NAMES)


def _switch(arg):
    """``(name, value)`` of a ``--name[=value]`` argument, else None (a URL or a non-switch)."""
    if not isinstance(arg, str) or not arg.startswith("--") or len(arg) == 2:
        return None
    name, _sep, value = arg[2:].partition("=")
    return (name, value) if name else None


def encode(entries) -> str:
    """The ``CLEARCOTE_PERSONA_ARGS`` value for ``[(name, value), ...]``."""
    raw = b"".join(n.encode("utf-8") + b"\0" + v.encode("utf-8") + b"\0" for n, v in entries)
    return base64.b64encode(raw).decode("ascii")


def decode(payload: str):
    """The inverse of :func:`encode` (tests and diagnostics)."""
    parts = base64.b64decode(payload).split(b"\0")
    if not parts or parts[-1] != b"" or len(parts) % 2 != 1:
        raise ValueError("malformed persona payload")
    return [(parts[i].decode("utf-8"), parts[i + 1].decode("utf-8")) for i in range(0, len(parts) - 1, 2)]


def wanted(enabled=None) -> bool:
    """``persona_env`` option: None follows ``CLEARCOTE_PERSONA_ENV`` (on unless it says 0/false/off/no)."""
    if enabled is not None:
        return bool(enabled)
    return os.environ.get(OPT_OUT_ENV, "").strip().lower() not in ("0", "false", "off", "no")


def apply(exe, args, env=None, enabled=None, supports=None):
    """Return ``(args, env)`` for a launch of ``exe``.

    When the engine implements ``--persona-from-env`` and env mode is wanted, the persona switches leave
    ``args`` and travel in ``env[CLEARCOTE_PERSONA_ARGS]``, and ``--persona-from-env`` is added. Otherwise
    both come back unchanged. ``env`` may be None (the launch inherits ``os.environ``); a changed env is a
    new dict based on it. ``supports`` is the engine probe (tests pass a stub)."""
    args = list(args or [])
    if not wanted(enabled):
        return args, env
    names = [s[0] for s in map(_switch, args) if s]
    if FROM_ENV_SWITCH in names or KILL_SWITCH in names:
        return args, env  # the caller already chose a transport, or asked the engine for the old one
    last = {}
    for i, arg in enumerate(args):
        s = _switch(arg)
        if s and is_transported(s[0]):
            last[s[0]] = i
    if not last:
        return args, env
    if supports is None:
        from ._launchopts import engine_supports_switch as supports
    if not supports(exe, FROM_ENV_SWITCH):
        return args, env
    payload = encode([_switch(args[i]) for i in sorted(last.values())])
    if len(payload) > MAX_PAYLOAD:
        return args, env
    kept = [a for a in args if not ((s := _switch(a)) and is_transported(s[0]))]
    new_env = dict(os.environ if env is None else env)
    new_env[ENV_VAR] = payload
    return kept + ["--" + FROM_ENV_SWITCH], new_env


def apply_to_pw_kwargs(exe, args, pw_kwargs, enabled=None):
    """:func:`apply` for a Playwright launch: returns the args and updates ``pw_kwargs["env"]`` in place."""
    new_args, env = apply(exe, args, pw_kwargs.get("env"), enabled)
    if env is not None:
        pw_kwargs["env"] = env
    return new_args
