"""Server-side personas (engine r51+): fetch the signed persona token for a launch and hand it to the engine.

From r51 the engine derives no persona of its own. The launcher asks the persona service for one -- with the
run token it just leased, the persona switches it is about to launch with (the *selector*) and the one fact of
this machine the generator needs (the host GPU vendor) -- and passes the signed answer to the browser in
``CLEARCOTE_PERSONA_TOKEN``, next to ``CLEARCOTE_RUN_TOKEN``. Every launch gets one, persona or not: a selector
without a seed yields the persona the engine computed for seed 0 up to r50, so a launch with no fingerprint
behaves exactly as before. The engine checks the token's signature and that it was issued to this licence and
lease for exactly these switches on this machine; it refuses to start without one.

Engines before r51 ignore the variable and keep deriving personas themselves, so this module does nothing for
them (``engine_wants_persona_token``), and the SDK keeps working with either.

A captured profile (``fingerprint_profile``) no longer rides on the command line: its exact JSON text goes to
the service, which hashes it, generates from it and names the hash in the token's selector; the launch carries
``--fingerprint-profile-sha256=<hash>`` so the engine can bind the two. The 24,000-byte argv limit on profiles
is gone with it.

The selector list below mirrors the engine's (``components/ungoogled/persona_token.cc``) and the service's
(``services/cc-persona/src/selector.mjs``): a switch added to one must be added to the others.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

from ._license import LicenseError, _user_agent
from ._net import proxied_request

TOKEN_ENV = "CLEARCOTE_PERSONA_TOKEN"
TOKEN_GZ_ENV = "CLEARCOTE_PERSONA_TOKEN_GZ"
API_ENV = "CLEARCOTE_PERSONA_API"  # comma-separated base URLs; tests and self-hosting
MARKER_SWITCH = "cc-persona-host-facts"  # only an r51+ engine implements it
DEFAULT_HOSTS = ("https://persona.clearcotelabs.com", "https://persona-us.clearcotelabs.com")
# Over this many bytes the token goes gzip + base64 in TOKEN_GZ_ENV (a Windows environment variable holds
# at most 32,767 characters; a macOS persona lists 180 voices).
ENV_INLINE_LIMIT = 30000
REQUEST_TIMEOUT = 8.0
SKEW_SEC = 60

SELECTOR_SWITCHES = frozenset((
    "fingerprint", "fingerprint-platform", "fingerprint-brand", "fingerprint-brand-version",
    "fingerprint-schema", "fingerprint-gpu-vendor", "fingerprint-gpu-renderer", "fingerprint-gpu-draw",
    "fingerprint-gpu-backend-real", "fingerprint-hardware-concurrency", "fingerprint-device-pixel-ratio",
    "fingerprint-color-scheme", "fingerprint-reduced-motion", "fingerprint-webauthn", "fingerprint-bluetooth",
    "fingerprint-webgpu-vendor", "fingerprint-webgpu-architecture", "disable-gpu-string-spoof",
    "disable-gpu-fingerprint", "disable-fingerprint-voices", "canvas-bridge-url", "fingerprint-profile-id",
    "fingerprint-profile-sha256",
))
# The switches the engine's host-GPU-vendor detection reads; the host-facts run carries exactly these.
HOST_FACT_SWITCHES = frozenset((
    "headless", "disable-gpu", "enable-gpu", "use-gl", "use-angle", "use-adapter-luid", "canvas-bridge-url",
    "fingerprint-gpu-host-vendor",
))
PROFILE_SWITCH = "fingerprint-profile"
PROFILE_SHA_SWITCH = "fingerprint-profile-sha256"

_LOCK = threading.Lock()
_TOKENS: dict[str, tuple[str, float]] = {}   # cache key -> (token, exp)
_HOST_FACTS: dict[tuple, dict] = {}


class PersonaServiceError(LicenseError):
    """The persona service refused the launch or could not be reached."""


def _switch(arg):
    if not isinstance(arg, str) or not arg.startswith("--") or len(arg) == 2:
        return None
    name, _sep, value = arg[2:].partition("=")
    return (name, value) if name else None


def engine_wants_persona_token(exe, supports=None) -> bool:
    """Whether ``exe`` is an r51+ engine, which starts only with a persona token."""
    if supports is None:
        from ._launchopts import engine_supports_switch as supports
    return bool(supports(exe, MARKER_SWITCH))


def selector_from_args(args):
    """``(sel, profile_json, args)``: the selector the generator reads from ``args`` (last value wins, as
    Chromium keeps the last copy), the captured profile's exact JSON text when ``--fingerprint-profile`` is
    present, and ``args`` with that switch removed and ``--fingerprint-profile-sha256`` added in its place."""
    sel: dict[str, str] = {}
    profile_json = None
    out = []
    for arg in args:
        s = _switch(arg)
        if s and s[0] == PROFILE_SWITCH:
            raw = base64.b64decode(s[1])
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError):
                pass  # an uncompressed profile
            profile_json = raw.decode("utf-8")
            continue
        if s and s[0] in SELECTOR_SWITCHES:
            sel[s[0]] = "1" if s[0] == "canvas-bridge-url" else s[1]
        out.append(arg)
    if profile_json is not None:
        sel.pop(PROFILE_SHA_SWITCH, None)
        out = [a for a in out if not (_switch(a) and _switch(a)[0] == PROFILE_SHA_SWITCH)]
        sha = hashlib.sha256(profile_json.encode("utf-8")).hexdigest()
        sel[PROFILE_SHA_SWITCH] = sha
        out.append(f"--{PROFILE_SHA_SWITCH}={sha}")
    return sel, profile_json, out


def _facts_cache_path():
    base = os.environ.get("LOCALAPPDATA") if sys.platform == "win32" else os.environ.get("HOME")
    return os.path.join(base, ".clearcote", "persona-host-facts.json") if base else None


def host_facts(exe, args, run_token, runner=None):
    """The facts of this machine the generator needs, as the engine itself reports them for a launch with
    these switches: ``chrome --cc-persona-host-facts`` prints ``{"gpu_vendor": ...}`` and exits. Cached per
    (binary, the switches that influence the answer) in memory and in ``~/.clearcote``."""
    relevant = tuple(sorted(a for a in args if _switch(a) and _switch(a)[0] in HOST_FACT_SWITCHES))
    try:
        st = os.stat(exe)
        key = (os.path.abspath(exe), st.st_size, int(st.st_mtime), relevant)
    except OSError:
        key = (str(exe), 0, 0, relevant)
    with _LOCK:
        if key in _HOST_FACTS:
            return dict(_HOST_FACTS[key])
    disk = _facts_cache_path()
    skey = json.dumps(list(key))
    if disk:
        try:
            with open(disk, encoding="utf-8") as f:
                stored = json.load(f).get(skey)
            if isinstance(stored, dict) and isinstance(stored.get("gpu_vendor"), str):
                with _LOCK:
                    _HOST_FACTS[key] = stored
                return dict(stored)
        except (OSError, ValueError):
            pass
    if runner is None:
        def runner(cmd, env):
            return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30).stdout
    env = dict(os.environ)
    env["CLEARCOTE_RUN_TOKEN"] = run_token  # the probe runs after the licence gate, like every launch
    env.pop(TOKEN_ENV, None)
    env.pop(TOKEN_GZ_ENV, None)
    try:
        text = runner([exe, f"--{MARKER_SWITCH}", *relevant], env)
        facts = json.loads(text.strip().splitlines()[-1])
        if not isinstance(facts.get("gpu_vendor"), str):
            raise ValueError("no gpu_vendor")
    except Exception as e:  # noqa: BLE001
        raise PersonaServiceError(f"the engine did not report its host facts ({e})") from None
    facts = {"gpu_vendor": facts["gpu_vendor"]}
    with _LOCK:
        _HOST_FACTS[key] = facts
    if disk:
        try:
            os.makedirs(os.path.dirname(disk), exist_ok=True)
            try:
                with open(disk, encoding="utf-8") as f:
                    all_facts = json.load(f)
            except (OSError, ValueError):
                all_facts = {}
            all_facts[skey] = facts
            with open(disk, "w", encoding="utf-8") as f:
                json.dump(all_facts, f)
        except OSError:
            pass
    return dict(facts)


def persona_hosts():
    raw = os.environ.get(API_ENV, "").strip()
    if raw:
        return tuple(h.strip().rstrip("/") for h in raw.split(",") if h.strip())
    return DEFAULT_HOSTS


def fetch_persona_token(run_token, sel, host, profile_json=None, proxy=None, hosts=None, timeout=REQUEST_TIMEOUT):
    """POST the request to the first persona host that answers; ``(token, exp)``. Raises PersonaServiceError
    with the service's reason, or with every host's failure when none answered."""
    body = {"token": run_token, "sel": sel, "host": host}
    if profile_json is not None:
        body["profile_json"] = profile_json
    payload = json.dumps(body)
    failures = []
    for base in hosts or persona_hosts():
        try:
            res = proxied_request(f"{base}/v1/persona", method="POST",
                                  headers={"content-type": "application/json", "User-Agent": _user_agent()},
                                  body=payload, timeout=timeout, proxy=proxy)
        except Exception as e:  # noqa: BLE001 -- network: try the next host
            failures.append(f"{base}: {e}")
            continue
        try:
            answer = res.json()
        except ValueError:
            answer = {}
        if res.ok and isinstance(answer, dict) and isinstance(answer.get("persona_token"), str):
            return answer["persona_token"], float(answer.get("exp") or 0)
        reason = answer.get("error") if isinstance(answer, dict) else None
        reason = reason or f"HTTP {res.status}"
        if res.status in (401, 403, 429) or 400 <= res.status < 500:
            raise PersonaServiceError(f"the persona service refused this launch: {reason}")
        failures.append(f"{base}: {reason}")
    raise PersonaServiceError("no persona service answered (" + "; ".join(failures) + "). The engine cannot "
                              "start without a persona; check outbound HTTPS to persona.clearcotelabs.com.")


def _lease_fields(lease):
    ml = getattr(lease, "_ml", None)
    lease_id = getattr(lease, "lease_id", None) or getattr(ml, "lease_id", None)
    proxy = getattr(lease, "_proxy", None) or getattr(ml, "_proxy", None)
    return lease_id, proxy


def _cache_key(lease_id, sel, host):
    return hashlib.sha256(json.dumps([lease_id, sel, host], sort_keys=True).encode()).hexdigest()


def persona_token_for(lease, sel, host, profile_json=None, fetch=None):
    """A valid persona token for this lease + selector + host, from the cache or the service."""
    lease_id, proxy = _lease_fields(lease)
    key = _cache_key(lease_id, sel, host)
    now = time.time()
    with _LOCK:
        hit = _TOKENS.get(key)
        if hit and hit[1] > now + SKEW_SEC:
            return hit[0]
    fetch = fetch or fetch_persona_token
    token, exp = fetch(lease.token, sel, host, profile_json=profile_json, proxy=proxy)
    with _LOCK:
        _TOKENS[key] = (token, exp)
    return token


def inject_persona_token(env: dict, token: str) -> dict:
    """Put the token in ``env`` the way the engine reads it (gzip + base64 when long)."""
    env.pop(TOKEN_ENV, None)
    env.pop(TOKEN_GZ_ENV, None)
    if len(token) > ENV_INLINE_LIMIT:
        env[TOKEN_GZ_ENV] = base64.b64encode(gzip.compress(token.encode("ascii"), 9)).decode("ascii")
    else:
        env[TOKEN_ENV] = token
    return env


def apply(exe, args, env, lease, supports=None, runner=None, fetch=None):
    """``(args, env)`` for a launch of ``exe``: on an r51+ engine with a lease, the launch gets its persona
    token (and a profile moves off the command line); otherwise both come back unchanged."""
    args = list(args or [])
    if lease is None or not getattr(lease, "token", None) or not engine_wants_persona_token(exe, supports):
        return args, env
    sel, profile_json, args = selector_from_args(args)
    facts = host_facts(exe, args, lease.token, runner=runner)
    token = persona_token_for(lease, sel, facts, profile_json=profile_json, fetch=fetch)
    env = dict(os.environ if env is None else env)
    return args, inject_persona_token(env, token)


def apply_to_pw_kwargs(exe, args, pw_kwargs, lease, **kw):
    """:func:`apply` for a Playwright launch: returns the args and updates ``pw_kwargs["env"]`` in place."""
    new_args, env = apply(exe, args, pw_kwargs.get("env"), lease, **kw)
    if env is not None:
        pw_kwargs["env"] = env
    return new_args


def clear_caches():
    with _LOCK:
        _TOKENS.clear()
        _HOST_FACTS.clear()
