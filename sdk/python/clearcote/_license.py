"""Floating-concurrency licensing client (opt-in), mirroring the Node SDK.

When a license key is present, the SDK checks out one of the license's N
concurrency slots from the backend, receives a short-lived Ed25519 run-token,
and injects it into the engine as CLEARCOTE_RUN_TOKEN. A background heartbeat
keeps the slot alive + rotates the token; on close the slot is released. With no
license key this is entirely inert (free mode). See PRIVATE-SDK-LICENSING-PLAN.md.

Concurrency is per-MACHINE on capped paid licences: the backend dedups by a stable instance_id, so one
machine holds exactly one slot regardless of how many browsers it runs. To avoid
hammering the backend, the lease is **shared across all launches in a process**
(one checkout per token-TTL, not one per launch) — see ``_MachineLease``. The free tier and paid
licences without a cap get one lease per browser instead (``lease_scope: "browser"``) — see
``_BrowserLease``.

The lease calls go to the licence lease server, and to the site when that cannot be reached (see
``_lease_bases``); every other call (the PRO download, device login) stays on the site.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib import request, error

from ._net import proxied_request, to_proxy_spec

DEFAULT_API_BASE = "https://www.clearcotelabs.com"
# The licence lease server. The lease calls (checkout, heartbeat, checkin, seats) go there first and to
# DEFAULT_API_BASE when it cannot be reached; every other route stays on DEFAULT_API_BASE.
DEFAULT_LEASE_API_BASE = "https://lease.clearcotelabs.com"
_RUN_TOKEN_ENV = "CLEARCOTE_RUN_TOKEN"
# Seconds of headroom kept before a token's exp: reuse it only while it's still
# valid with this much slack, so an in-flight launch never ships an expiring token.
_SKEW_SEC = 60


class LicenseError(RuntimeError):
    code = "LICENSE_ERROR"
    status = None  # the licence server's HTTP status, when it answered one

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status


class ConcurrencyLimitError(LicenseError):
    code = "CONCURRENCY_LIMIT_EXCEEDED"


class LicenseRevokedError(LicenseError):
    code = "LICENSE_REVOKED"


class LicenseServerChallengeError(LicenseError):
    """The licence server never answered: something in front of it (a CDN firewall or bot check, e.g. the
    "Vercel Security Checkpoint") returned a challenge page instead of a licence verdict. Says nothing about
    the licence itself, which is NOT revoked or over its limit. Retryable: such challenges are usually
    temporary and tied to this machine's network; ``license_through_proxy`` sends the calls another way."""
    code = "LICENSE_SERVER_CHALLENGE"
    retryable = True


def resolve_license_key(explicit: str | None = None) -> str | None:
    """explicit > CLEARCOTE_LICENSE_KEY env > ~/.clearcote/license.key."""
    if explicit and explicit.strip():
        return explicit.strip()
    env = os.environ.get("CLEARCOTE_LICENSE_KEY", "")
    if env.strip():
        return env.strip()
    try:
        p = Path.home() / ".clearcote" / "license.key"
        if p.exists():
            v = p.read_text().strip()
            if v:
                return v
    except OSError:
        pass
    return None


def license_through_proxy_requested(opt=None, env=None) -> bool:
    """Whether licence calls should use the launch proxy: explicit option, else the
    CLEARCOTE_LICENSE_THROUGH_PROXY env switch (1/true/yes/on)."""
    if opt is not None:
        return bool(opt)
    env = os.environ if env is None else env
    return str(env.get("CLEARCOTE_LICENSE_THROUGH_PROXY") or "").strip().lower() in ("1", "true", "yes", "on")


def resolve_instance_id() -> str:
    """A STABLE per-machine id so a restart REUSES its concurrency slot instead of spawning a
    second lease (the backend dedupes a machine's own prior live lease on re-checkout). Order:
    CLEARCOTE_INSTANCE_ID env > ~/.clearcote/instance_id file > a freshly generated id (persisted
    for next time). Falls back to an ephemeral id if the file can't be written — in containers with
    an ephemeral filesystem, set CLEARCOTE_INSTANCE_ID per replica to keep it stable."""
    env = os.environ.get("CLEARCOTE_INSTANCE_ID", "")
    if env.strip():
        return env.strip()
    p = Path.home() / ".clearcote" / "instance_id"
    try:
        if p.exists():
            v = p.read_text().strip()
            if v:
                return v
    except OSError:
        pass
    new_id = str(uuid.uuid4())
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_id + "\n")
    except OSError:
        pass  # ephemeral fallback — this run gets a fresh id; set CLEARCOTE_INSTANCE_ID to persist
    return new_id


def _api_base(api_base: str | None) -> str:
    return (api_base or os.environ.get("CLEARCOTE_LICENSE_API") or DEFAULT_API_BASE).rstrip("/")


def _lease_bases(api_base: str | None) -> tuple:
    """``(primary, fallback)`` for the lease calls only (checkout, heartbeat, checkin, seats).

    A base set explicitly is used alone, with no fallback: the ``api_base`` argument, else
    CLEARCOTE_LICENSE_API (staging, tests, self-hosting), else CLEARCOTE_LEASE_API. By default the licence
    lease server, with DEFAULT_API_BASE as the fallback when it cannot be reached (see _with_fallback)."""
    explicit = (api_base or os.environ.get("CLEARCOTE_LICENSE_API")
                or os.environ.get("CLEARCOTE_LEASE_API") or "").strip()
    if explicit:
        return explicit.rstrip("/"), None
    return DEFAULT_LEASE_API_BASE.rstrip("/"), DEFAULT_API_BASE.rstrip("/")


# Statuses a proxy or CDN in front of the lease server answers when the server itself is down or unreachable.
_GATEWAY_STATUSES = frozenset({502, 503, 504, 530, *range(520, 528)})
# After the primary lease base gave no answer of its own, this process sends lease calls straight to the
# fallback for this many seconds, then tries the primary again.
_PRIMARY_RETRY_SEC = 60.0
_primary_down_until: dict = {}  # (primary base, route) -> time.monotonic() until which the primary is skipped
_BREAKER_LOCK = threading.Lock()
# Waits before a checkout answered RATE_LIMITED (this IP's request budget, not the licence's limit) is sent
# again; after the last one it counts as no verdict.
_RATE_LIMIT_RETRY_SEC = (1.0, 2.0, 4.0)


def _with_fallback(primary: str, fallback: str | None, route, call, unanswered):
    """``call(base)`` on the primary lease base, and once more on ``fallback`` when the primary gave no answer
    of its own: a network error (DNS, refused or reset connection, TLS, timeout), a gateway status, or a
    challenge page (``unanswered(result)``). Any other answer from the primary (200, 401, 403, 409, 426,
    429, ...) is final. ``route`` keeps the direct path and each proxy apart in the per-process breaker."""
    if not fallback:
        return call(primary)
    key = (primary, route)
    with _BREAKER_LOCK:
        skip = _primary_down_until.get(key, 0.0) > time.monotonic()
    if not skip:
        try:
            out = call(primary)
            if not unanswered(out):
                return out
        except Exception:  # noqa: BLE001 — unreachable: the fallback below says what is wrong, if it is too
            pass
        with _BREAKER_LOCK:
            _primary_down_until[key] = time.monotonic() + _PRIMARY_RETRY_SEC
    return call(fallback)


def _lease_unanswered(out) -> bool:
    """A ``_post`` result that is not the lease server's own answer: a gateway status or a challenge page."""
    status, body = out
    return status in _GATEWAY_STATUSES or (isinstance(body, dict) and bool(body.get(_CHALLENGE)))


def _os_tag() -> str:
    return {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "unknown")


def _key_hash(license_key: str) -> str:
    return hashlib.sha256(license_key.encode()).hexdigest()[:16]


def _cache_path(license_key: str) -> Path:
    return Path.home() / ".clearcote" / f"lease-{_key_hash(license_key)}.json"


def _read_cache(license_key: str):
    """The on-disk shared token cache: {token, exp, lease_id, scope}. Enables cross-process
    reuse on one machine — a second process picks up a still-valid token instead of
    checking out again (only one this SDK marked ``scope: "machine"``, see _MachineLease.ensure).
    Older caches without lease_id or scope are still honored as the offline fallback.

    A per-browser (free-tier) token is never returned, even one an older SDK wrote: it belongs to
    the one browser it was checked out for, and reusing it would start another browser without a
    slot. Paid tokens are reused exactly as before."""
    try:
        d = json.loads(_cache_path(license_key).read_text())
        if isinstance(d.get("token"), str) and isinstance(d.get("exp"), (int, float)):
            if token_plan(d["token"]) == _PER_BROWSER_PLAN:
                return None
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return None


# The plan whose tokens are per browser. Only used to keep such tokens out of the shared cache.
_PER_BROWSER_PLAN = "free"


def token_plan(token: str) -> str | None:
    """The plan a run-token was minted for, read from its payload WITHOUT verifying it (routing only)."""
    try:
        body = (token or "").split(".")[0]
        body += "=" * (-len(body) % 4)
        plan = json.loads(base64.urlsafe_b64decode(body.encode()).decode("utf-8")).get("plan")
        return plan if isinstance(plan, str) else None
    except Exception:  # noqa: BLE001 — junk token: no plan
        return None


def token_iat(token: str) -> int | None:
    """When a run-token was minted (its ``iat``, epoch seconds), read from its payload WITHOUT verifying it."""
    try:
        body = (token or "").split(".")[0]
        body += "=" * (-len(body) % 4)
        iat = json.loads(base64.urlsafe_b64decode(body.encode()).decode("utf-8")).get("iat")
        return int(iat) if isinstance(iat, (int, float)) and not isinstance(iat, bool) else None
    except Exception:  # noqa: BLE001 — junk token: unknown
        return None


# What the PRO engine refuses a launch with when its run-token is older than the newest one it has accepted.
STALE_TOKEN_REFUSAL = "older than the last one accepted"


def engine_hwm(env=None) -> int:
    """The newest run-token ``iat`` the PRO engine has accepted for this OS user (0 if none/unreadable).

    The engine (patch 990, clock-rollback guard) keeps it in ``$LOCALAPPDATA/.clearcote/.cc_hwm``, else
    ``$HOME/.clearcote/.cc_hwm``, as a decimal number, and refuses any token with a lower ``iat``. It is per
    OS user, not per licence or process: another SDK process, the hosted-browser gateway or a run with a
    different key can all move it past a token this process still holds. Looked up exactly as the engine
    does it, env var by env var."""
    env = os.environ if env is None else env
    base_dir = env.get("LOCALAPPDATA") or env.get("HOME")
    if not base_dir:
        return 0
    try:
        with open(os.path.join(base_dir, ".clearcote", ".cc_hwm"), encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def new_launch_id() -> str:
    """One id per browser launch: the backend counts every launch_id as its own slot on per-browser plans."""
    return uuid.uuid4().hex


def _write_cache(license_key: str, token: str, exp: float, lease_id: str | None = None,
                 scope: str | None = "machine") -> None:
    """Only ever a machine lease's token. ``scope: "machine"`` says a checkout answer of the licence server
    made it one (``scope=None``: a lease kept alive from an unmarked file, which says nothing about that);
    older SDKs read token/exp/lease_id and ignore it."""
    d = {"token": token, "exp": exp, "lease_id": lease_id}
    if scope:
        d["scope"] = scope
    try:
        p = _cache_path(license_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d))
    except OSError:
        pass


def _grace_path(license_key: str) -> Path:
    return Path.home() / ".clearcote" / f"grace-{_key_hash(license_key)}.json"


def _read_grace(license_key: str):
    """The grace token, ``{token, exp}``: the newest PAID per-browser run-token this machine was given. It lets
    a paid launch start, uncounted, when the licence server gives no verdict. Never a free token."""
    try:
        d = json.loads(_grace_path(license_key).read_text())
        if isinstance(d.get("token"), str) and isinstance(d.get("exp"), (int, float)):
            if token_plan(d["token"]) == _PER_BROWSER_PLAN:
                return None
            return d
    except (OSError, ValueError, AttributeError):
        pass
    return None


_GRACE_LOCK = threading.Lock()


def _write_grace(license_key: str, token, exp) -> None:
    """Keep a paid per-browser run-token as the grace token. Its own file, never lease-<hash>.json: older SDKs
    on this machine read that one and would reuse a token that belongs to one browser. A token minted before
    the one kept (a lower iat) never replaces it. Free tokens are never kept."""
    if not isinstance(token, str) or not token or token_plan(token) == _PER_BROWSER_PLAN:
        return
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return
    with _GRACE_LOCK:
        try:
            p = _grace_path(license_key)
            try:
                kept = json.loads(p.read_text())
            except (OSError, ValueError):
                kept = None
            kept_iat = token_iat(kept.get("token")) if isinstance(kept, dict) else None
            new_iat = token_iat(token)
            if kept_iat is not None and (new_iat is None or new_iat < kept_iat):
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"token": token, "exp": exp}))
        except OSError:
            pass


# Launches this process started on a cached run-token while the licence server gave no verdict, per licence key,
# not yet reported. The next checkout reports them (uncounted_launches) and a 200 takes them off.
_uncounted_launches: dict = {}
_UNCOUNTED_LOCK = threading.Lock()


def _uncounted_add(license_key: str) -> None:
    with _UNCOUNTED_LOCK:
        _uncounted_launches[license_key] = _uncounted_launches.get(license_key, 0) + 1


def _uncounted_pending(license_key: str) -> int:
    with _UNCOUNTED_LOCK:
        return _uncounted_launches.get(license_key, 0)


def _uncounted_reported(license_key: str, n: int) -> None:
    """A checkout that reported ``n`` uncounted launches got a 200. Launches started uncounted meanwhile stay."""
    if not n:
        return
    with _UNCOUNTED_LOCK:
        left = _uncounted_launches.get(license_key, 0) - n
        if left > 0:
            _uncounted_launches[license_key] = left
        else:
            _uncounted_launches.pop(license_key, None)


_notices_shown: set = set()
_NOTICE_LOCK = threading.Lock()


def _show_notice(answer: dict, quiet: bool) -> None:
    """Print a checkout answer's ``notice`` (stderr), each one once per process."""
    notice = answer.get("notice") if isinstance(answer, dict) else None
    if quiet or not isinstance(notice, str) or not notice.strip():
        return
    with _NOTICE_LOCK:
        if notice in _notices_shown:
            return
        _notices_shown.add(notice)
    sys.stderr.write(f"[clearcote] [license] {notice.strip()}\n")


def _no_verdict(exc: BaseException) -> bool:
    """A licence call that ended without the licence server's verdict on the licence: a network error, a 5xx, a
    challenge page in front of the server, or this IP's request budget (RATE_LIMITED)."""
    if isinstance(exc, LicenseServerChallengeError):
        return True
    if isinstance(exc, LicenseError):
        return exc.code == "RATE_LIMITED" or (exc.status or 0) >= 500
    return True


def _no_verdict_reason(exc: BaseException) -> str:
    """A short reason for the stderr line of a launch that starts uncounted (worded as the other SDKs do)."""
    if isinstance(exc, LicenseServerChallengeError):
        return f"security challenge, HTTP {exc.status or '?'}"
    if isinstance(exc, LicenseError):
        return "rate limited" if exc.code == "RATE_LIMITED" else f"HTTP {exc.status or exc.code}"
    return str(getattr(exc, "reason", None) or exc)


def _user_agent() -> str:
    """Names this SDK and its version on every licence call, so the licence server's logs can tell SDK
    builds apart, and tell clients that are not an SDK from ours. Spelled "User-Agent" by the callers:
    the proxied path then replaces its own default instead of sending a second header. Resolved per
    call because this module is imported before the package sets __version__."""
    try:
        from . import __version__
    except ImportError:
        return "clearcote-sdk-python"
    return f"clearcote-sdk-python/{__version__}"


# The key _post puts in the payload it returns when the answer was a challenge page, not the licence
# server's own JSON. Private: the licence server never sends it.
_CHALLENGE = "_challenge"


def _challenge_reason(status: int, content_type: str, text: str, mitigated: str = "") -> str | None:
    """Why an error answer is a firewall/bot challenge rather than the licence server's verdict, or None.

    The licence server answers every error with JSON. A Vercel challenge sets ``x-vercel-mitigated`` and
    serves an HTML page titled "Vercel Security Checkpoint"; any other HTML (or non-JSON) 403/429 comes
    from something in front of the server too, so it is not a revoke or a concurrency verdict either."""
    if status < 400:
        return None
    if mitigated:
        return f"x-vercel-mitigated: {mitigated}"
    if "Vercel Security Checkpoint" in text:
        return "Vercel Security Checkpoint"
    if status in (403, 429) and text.strip():
        try:
            json.loads(text)
            return None
        except ValueError:
            return f"non-JSON {content_type.split(';')[0].strip() or 'body'}"
    return None


def _post(url: str, license_key: str, body: dict, timeout: float = 15.0, proxy=None):
    if proxy:
        # license_through_proxy: through the launch proxy (HTTP CONNECT or SOCKS5). The direct path
        # below is unchanged from before the option existed.
        res = proxied_request(url, method="POST", body=json.dumps(body), timeout=30.0, proxy=proxy,
                              headers={"authorization": f"Bearer {license_key}",
                                       "content-type": "application/json",
                                       "User-Agent": _user_agent()})
        h = getattr(res, "headers", None) or {}
        why = _challenge_reason(res.status, h.get("content-type", ""), res.text(), h.get("x-vercel-mitigated", ""))
        if why:
            return res.status, {_CHALLENGE: why}
        try:
            payload = res.json() if res.text().strip() else {}
        except ValueError:
            payload = {}
        return res.status, payload if isinstance(payload, dict) else {}
    data = json.dumps(body).encode()
    req = request.Request(url, data=data, method="POST", headers={
        "authorization": f"Bearer {license_key}",
        "content-type": "application/json",
        "User-Agent": _user_agent(),
    })
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except error.HTTPError as e:  # non-2xx
        text = e.read().decode(errors="replace")
        why = _challenge_reason(e.code, e.headers.get("content-type") or "", text,
                                e.headers.get("x-vercel-mitigated") or "")
        if why:
            return e.code, {_CHALLENGE: why}
        try:
            payload = json.loads(text or "{}")
        except ValueError:
            payload = {}
        return e.code, payload if isinstance(payload, dict) else {}


def _raise_for_status(status: int, body: dict):
    if body.get(_CHALLENGE):
        raise LicenseServerChallengeError(
            f"The licence server could not be reached: a firewall in front of it answered with a security "
            f"challenge (HTTP {status}, {body[_CHALLENGE]}) instead of a licence verdict, so your licence "
            f"was not checked (it is not revoked). This is usually temporary and tied to this machine's network: retry later, or send "
            f"the licence calls through your proxy (license_through_proxy=True).",
            "LICENSE_SERVER_CHALLENGE", status)
    msg = body.get("error") or f"License request failed ({status})."
    code = body.get("code")
    if code == "RATE_LIMITED":
        # This IP's request budget at the licence server is spent: not the licence's concurrency limit.
        raise LicenseError(
            f"{body.get('error') or 'Rate limit exceeded.'} The licence server is limiting requests from this "
            f"network (RATE_LIMITED); this is not the licence's concurrency limit. Retry in a minute.",
            "RATE_LIMITED", status)
    if status == 429 or code == "CONCURRENCY_LIMIT_EXCEEDED":
        raise ConcurrencyLimitError(msg, "CONCURRENCY_LIMIT_EXCEEDED", status)
    if status == 403 or code in ("LICENSE_REVOKED", "LICENSE_EXPIRED"):
        raise LicenseRevokedError(msg, "LICENSE_REVOKED", status)
    raise LicenseError(msg, code or f"HTTP_{status}", status)


# ---------------------------------------------------------------------------
# Run-token files (engine online-enforcement opt-in)
# ---------------------------------------------------------------------------

class _TokenFileSet:
    """Mirror a lease's rotating run-token into one file per launch (CLEARCOTE_RUN_TOKEN_FILE).

    A supporting engine (152 r23+) re-reads the run-token from this file and stops a running FREE
    browser once the token stops advancing (the SDK can only advance it by heartbeating, which the
    backend gates). This mirrors a lease's rotating token into one file per launch and removes it on
    close. Older engines ignore the file (they read CLEARCOTE_RUN_TOKEN once at launch), so it is
    purely additive — nothing breaks if the engine does not support it. Every write is best-effort:
    a failure is non-fatal because the launch still carries CLEARCOTE_RUN_TOKEN.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()

    def bind(self, current: str):
        """Create a token file seeded with ``current``; returns ``(path, release)``. The file is kept
        updated by :meth:`update` until ``release()`` removes it."""
        path = os.path.join(tempfile.gettempdir(), f"clearcote-rt-{uuid.uuid4()}.tok")
        self._write_one(path, current)
        with self._lock:
            self._paths.add(path)

        def release() -> None:
            with self._lock:
                self._paths.discard(path)
            try:
                os.remove(path)
            except OSError:
                pass  # already gone

        return path, release

    def update(self, token: str) -> None:
        """Rewrite every live file with the freshly-rotated token."""
        with self._lock:
            paths = list(self._paths)
        for p in paths:
            self._write_one(p, token)

    def close_all(self) -> None:
        """Remove every file (lease shutdown)."""
        with self._lock:
            paths = list(self._paths)
            self._paths.clear()
        for p in paths:
            try:
                os.remove(p)
            except OSError:
                pass

    @staticmethod
    def _write_one(path: str, token: str) -> None:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError:
            pass  # the launch still has CLEARCOTE_RUN_TOKEN


# ---------------------------------------------------------------------------
# Process-shared, per-machine lease
# ---------------------------------------------------------------------------

class _MachineLease:
    """One shared lease per (process, license key).

    Concurrency is per-MACHINE (the backend dedups by instance_id), so re-checking
    out on every launch is redundant — the machine already holds its one slot. This
    checks out at most once per token-TTL and lets every launch in the process share
    the same run-token, cutting backend calls from O(launches) to O(TTL windows).

    Only the process that performs the cold checkout runs the heartbeat + does the
    single checkin at exit; processes that reuse a still-valid on-disk token make no
    backend calls at all (an owner elsewhere, or the token TTL, keeps the slot).
    """

    def __init__(self, key: str, base: str, instance_id: str,
                 sdk_version: str | None, quiet: bool, engine_version=None, proxy=None, fallback=None):
        self._key = key
        self._proxy = proxy  # proxy spec when license_through_proxy is on, else None (direct)
        self._base = base
        self._fallback = fallback  # the default bases only: where a call the lease server cannot answer goes
        self._instance_id = instance_id
        self._sdk_version = sdk_version          # SDK PACKAGE version (e.g. "0.17.1")
        # Resolved browser build (e.g. "150.0.7871.114"). May be a str or a zero-arg callable that
        # resolves it lazily (so the catalog is only consulted on a cold checkout, never per launch);
        # memoized in _engine_resolved. Telemetry only — never gates the lease.
        self._engine_version = engine_version
        self._engine_resolved: str | None = None
        self._quiet = quiet
        self._lock = threading.RLock()
        self._token: str | None = None
        # Follows this lease's rotating token into one file per launch (CLEARCOTE_RUN_TOKEN_FILE).
        self._token_files = _TokenFileSet()
        self.exp: float = 0.0
        self.lease_id: str | None = None
        self._hb_sec = 270
        self._owner = False          # only the cold-checkout owner heartbeats/checkins
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._refs = 0
        # Learned from the first checkout: "browser" means every launch holds its own lease.
        self.scope = "machine"
        self._pending_browser = None     # (checkout body, launch_id) made by ensure() for one launch
        self._browsers: set = set()      # live _BrowserLease objects, released at exit if still open
        self._browser_plan = None        # plan of the last per-browser lease ("free" never falls back)
        # The licence server said this lease is a machine lease (a checkout answer, or a cache marked so):
        # only then is the cache this process writes marked scope "machine" (see _write_cache).
        self._machine_known = False

    @property
    def token(self) -> str | None:
        return self._token

    def _cache_scope(self):
        return "machine" if self._machine_known else None

    @token.setter
    def token(self, v: str | None) -> None:
        self._token = v
        if v:
            self._token_files.update(v)  # rotation follows the lease's token automatically

    def bind_launch(self):
        """Bind a per-launch run-token file that follows this lease's rotating token. Returns
        ``(file_path, release)``; call ``release()`` when the browser closes. A supporting engine
        (r23+) re-reads it so revoke/check-in/over-limit stops a running free browser; older engines
        ignore it."""
        return self._token_files.bind(self._token or "")

    def _send(self, path: str, body: dict):
        """POST a lease call (``path``, e.g. "/api/v1/lease/heartbeat"): through the launch proxy when
        license_through_proxy is on, else the unchanged direct path. On the default bases a call the lease
        server could not answer goes once to the fallback base (see _with_fallback)."""
        def call(base):
            if self._proxy:
                return _post(f"{base}{path}", self._key, body, proxy=self._proxy)
            return _post(f"{base}{path}", self._key, body)

        route = (self._proxy["server"], self._proxy.get("username")) if self._proxy else None
        return _with_fallback(self._base, self._fallback, route, call, _lease_unanswered)

    def _valid(self) -> bool:
        return bool(self.token) and self.exp > time.time() + _SKEW_SEC

    def _engine_ver(self):
        """Resolved engine version for telemetry — memoized, resolved at most once. A callable is
        invoked on the first (cold-checkout) use only; any failure yields None (field omitted)."""
        if self._engine_resolved is None:
            ev = self._engine_version
            try:
                self._engine_resolved = (ev() if callable(ev) else ev) or ""
            except Exception:  # noqa: BLE001 — telemetry must never break a launch
                self._engine_resolved = ""
        return self._engine_resolved or None

    def ensure(self):
        """Make a usable run-token available with the fewest possible backend calls.
        Raises on a definitive limit/revoke verdict (cold checkout path only).

        Returns None, or the calling launch's uncounted handle when the cold checkout got no verdict and
        a paid grace token is at hand (see _uncounted)."""
        with self._lock:
            if self.scope == "browser":
                return None  # per-browser plan: nothing is shared; acquire() checks out per launch
            if self._valid():
                return None  # already holding a live token (owner or reusing) — zero calls
            cached = _read_cache(self._key)
            if cached and cached.get("scope") == "machine" and cached["exp"] > time.time() + _SKEW_SEC:
                # cross-process reuse: another process's owner is keeping the slot alive. Only a cache this
                # SDK marked as a machine lease: an older SDK's file may belong to a licence that now counts
                # every browser, so it gets a checkout first (and stays the fallback if that fails).
                self.token = cached["token"]
                self.exp = float(cached["exp"])
                self.lease_id = cached.get("lease_id")
                self._owner = False
                self._machine_known = True
                return None  # NO checkout, NO heartbeat
            # cold: this process owns the slot, the heartbeat, and the exit checkin.
            uncounted = self._checkout(fail_open=True)
            if uncounted is not None or self.scope == "browser":
                return uncounted  # the checkout belongs to the launch that asked; see acquire()
            self._owner = True
            self._start_heartbeat()
        return None

    def _post_checkout(self, launch_id: str | None):
        """POST one checkout. Every checkout offers per-browser leases and reports the launches this process
        started uncounted since the last successful one; a 200 takes those off and shows its notice."""
        body = {"instance_id": self._instance_id}
        if launch_id:
            # per-browser plans count each launch_id as its own slot; machine plans ignore it
            body["launch_id"] = launch_id
        body.update({"os": _os_tag(), "sdk_version": self._sdk_version, "engine_version": self._engine_ver(),
                     "lease_scope_supported": ["browser"]})
        uncounted = _uncounted_pending(self._key)
        if uncounted:
            body["uncounted_launches"] = uncounted
        status, answer = self._send("/api/v1/lease/checkout", body)
        if status == 200:
            _uncounted_reported(self._key, uncounted)
            _show_notice(answer, self._quiet)
        return status, answer

    def checkout_for(self, launch_id: str) -> dict:
        """POST a checkout for one launch. Raises the backend's verdict; network errors propagate.
        RATE_LIMITED (this IP's request budget, not the licence's limit) is sent again after each wait in
        _RATE_LIMIT_RETRY_SEC first."""
        for wait in (*_RATE_LIMIT_RETRY_SEC, None):
            status, body = self._post_checkout(launch_id)
            if wait is None or not (status == 429 and body.get("code") == "RATE_LIMITED"):
                break
            time.sleep(wait)
        if status != 200:
            _raise_for_status(status, body)
        return body

    def _checkout(self, fail_open: bool = False):
        """A cold checkout. With ``fail_open`` (a launch waits on it) a checkout that got no verdict returns
        that launch's uncounted handle when a paid grace token is at hand; otherwise returns None."""
        launch_id = new_launch_id()
        try:
            body = self.checkout_for(launch_id)
        except LicenseError as e:
            if not _no_verdict(e):
                raise  # a definitive verdict must surface (never silently downgrade)
            uncounted = self._uncounted(e, legacy=False) if fail_open else None
            if uncounted is not None:
                return uncounted
            if not isinstance(e, LicenseServerChallengeError) and e.code != "RATE_LIMITED":
                raise  # a 5xx on a machine lease: raised, as before
            # Not a verdict: the server was never reached, or this IP's request budget is spent. Offline grace
            # like a network error, and the error itself surfaces when there is no cached token.
            cached = _read_cache(self._key)
            if cached and cached["exp"] > time.time() + _SKEW_SEC:
                if not self._quiet:
                    sys.stderr.write(f"[clearcote] [license] {e} Using the cached run-token.\n")
                self.token = cached["token"]
                self.exp = float(cached["exp"])
                self.lease_id = cached.get("lease_id")
                self._machine_known = cached.get("scope") == "machine"
                return None
            raise
        except Exception as e:  # noqa: BLE001 — network/other: offline grace on a cached token
            uncounted = self._uncounted(e, legacy=False) if fail_open else None
            if uncounted is not None:
                return uncounted
            cached = _read_cache(self._key)
            if cached and cached["exp"] > time.time() + _SKEW_SEC:
                if not self._quiet:
                    sys.stderr.write(f"[clearcote] [license] backend unreachable ({e}); using cached run-token.\n")
                self.token = cached["token"]
                self.exp = float(cached["exp"])
                self.lease_id = cached.get("lease_id")
                self._machine_known = cached.get("scope") == "machine"
                return None
            raise LicenseError(f"Could not reach the license server and no valid cached token: {e}")
        if body.get("lease_scope") == "browser":
            # Per-browser plan: this checkout is the calling launch's own slot. Never shared, never cached.
            self.scope = "browser"
            self._pending_browser = (body, launch_id)
            return None
        self.token = body["token"]
        self.exp = float(body["exp"])
        self.lease_id = body["lease_id"]
        self._hb_sec = int(body.get("heartbeat_interval_sec") or 270)
        self._machine_known = True
        _write_cache(self._key, self.token, self.exp, self.lease_id)
        return None

    def _uncounted(self, exc: BaseException, legacy: bool = True):
        """Fail open, paid only. A per-browser checkout ended without a verdict (``exc``, see _no_verdict): the
        launch starts on a cached paid run-token instead, with no lease, no heartbeat and no check-in, and the
        next checkout reports it (uncounted_launches). The grace token first, else (``legacy``) the machine
        cache's token. None when neither is valid, or when this process has seen the licence as free: the
        free tier stays fail-closed."""
        if self._browser_plan == _PER_BROWSER_PLAN:
            return None
        floor = time.time() + _SKEW_SEC
        kept = _read_grace(self._key)
        if not (kept and kept["exp"] > floor) and legacy:
            kept = _read_cache(self._key)
        if not (kept and kept["exp"] > floor):
            return None
        _uncounted_add(self._key)
        if not self._quiet:
            sys.stderr.write(f"[clearcote] [license] licence server unreachable ({_no_verdict_reason(exc)}); "
                             "this browser starts on a cached run-token and is not counted.\n")
        lease = _UncountedLease(self, kept["token"])
        with self._lock:
            self._browsers.add(lease)  # its token file is removed at exit if the browser is never closed
        return lease

    def _start_heartbeat(self) -> None:
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(max(5, self._hb_sec),), daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self, interval: int) -> None:
        while not self._stop.wait(interval):
            try:
                status, body = self._send("/api/v1/lease/heartbeat",
                                          {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if status == 409:  # reclaimed/expired -> re-checkout to keep the slot
                    st2, b2 = self._post_checkout(None)
                    if st2 == 200 and b2.get("lease_scope") == "browser":
                        # Not a machine lease (no launch asked for it): give it back, retry on the next beat.
                        self._send("/api/v1/lease/checkin", {"lease_id": b2.get("lease_id")})
                    elif st2 == 200:
                        with self._lock:
                            self.lease_id = b2["lease_id"]
                            self.token = b2["token"]
                            self.exp = float(b2["exp"])
                            self._machine_known = True
                        _write_cache(self._key, b2["token"], b2["exp"], b2["lease_id"])
                elif status == 200 and body.get("token"):
                    with self._lock:
                        self.token = body["token"]
                        self.exp = float(body["exp"])
                    _write_cache(self._key, body["token"], body["exp"], self.lease_id, self._cache_scope())
            except Exception:  # noqa: BLE001 — transient; offline grace until token exp
                pass

    def refresh_if_behind_engine(self, force: bool = False) -> bool:
        """Mint a fresh token when the one held is older than the newest the PRO engine has accepted here.

        The engine refuses such a token outright ("older than the last one accepted"), and a token reused
        from the cache or from memory for its 24 h life can be: the mark is per OS user, so anything else
        launching with a newer token moves it (another process, the hosted-browser gateway, another key).
        Returns True when the token was replaced. ``force`` skips the comparison: the engine has just
        refused this token (a race this check could not see coming)."""
        with self._lock:
            if self.scope == "browser" or not self.token:
                return False
            if not force:
                iat, hwm = token_iat(self.token), engine_hwm()
                if not iat or not hwm or iat >= hwm:
                    return False
            return self._fresh_token()

    def _fresh_token(self) -> bool:
        """Replace the token with a freshly minted one. First by heartbeating the lease this process knows
        (its own, or the one another process's owner keeps alive): same lease, nothing revoked, so two
        processes never knock each other's lease out. If there is no lease id, or the lease is gone, check
        out again, like a cold ensure(). A definitive refusal (limit, revoked) raises."""
        if self.lease_id:
            try:
                status, body = self._send("/api/v1/lease/heartbeat",
                                          {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
            except Exception:  # noqa: BLE001 — unreachable: a checkout below says so properly
                status, body = 0, {}
            if status == 200 and body.get("token"):
                self.token = body["token"]
                self.exp = float(body["exp"])
                _write_cache(self._key, self.token, self.exp, self.lease_id, self._cache_scope())
                return True
        self._checkout()
        if self.scope == "browser":
            return False
        if not self._owner:  # the slot is this process's now: keep it alive, check it in at exit
            self._owner = True
            self._start_heartbeat()
        return True

    def acquire(self):
        """Ensure a live token, bump the refcount, return a per-launch handle.

        On a per-browser plan (told by the backend's lease_scope "browser") every launch instead gets
        its own checked-out lease, released when that browser closes."""
        if self.scope != "browser":
            uncounted = self.ensure()
            if uncounted is not None:
                return uncounted  # paid per-browser licence, no verdict from the server: see _uncounted
            self.refresh_if_behind_engine()
        with self._lock:
            if self.scope != "browser":
                self._refs += 1
                return _LeaseHandle(self)
            # Exactly one launch takes the checkout ensure() made; every other launch checks out its own.
            pending, self._pending_browser = self._pending_browser, None
        if pending:
            return self._start_browser(*pending)
        return self._acquire_browser()

    def _acquire_browser(self):
        launch_id = new_launch_id()
        try:
            body = self.checkout_for(launch_id)
        except LicenseError as e:
            uncounted = self._uncounted(e) if _no_verdict(e) else None
            if uncounted is None:
                raise
            return uncounted
        except Exception as e:  # noqa: BLE001 — without the backend there is no slot (a paid licence: see _uncounted)
            uncounted = self._uncounted(e)
            if uncounted is None:
                raise LicenseError(f"Could not reach the license server to start this browser: {e}")
            return uncounted
        return self._start_browser(body, launch_id)

    def _start_browser(self, body: dict, launch_id: str):
        lease = _BrowserLease(self, body, launch_id)
        with self._lock:
            self._browser_plan = token_plan(body.get("token"))
            self._browsers.add(lease)
        return lease

    def _forget_browser(self, lease) -> None:
        with self._lock:
            self._browsers.discard(lease)

    def release(self) -> None:
        """A per-launch handle closed. We do NOT checkin here — the machine slot is
        held for the process lifetime (any launch may reuse it) and released once at
        exit. This is what removes the per-launch checkin churn."""
        with self._lock:
            if self._refs > 0:
                self._refs -= 1

    def shutdown(self) -> None:
        """Stop the heartbeat and release the slot once, at process exit."""
        self._token_files.close_all()  # remove any launch token files still bound
        # Per-browser leases still open at exit (a browser nobody closed): release their slots now.
        with self._lock:
            open_browsers = list(self._browsers)
        for b in open_browsers:
            b.stop(wait=True)
        self._stop.set()
        if self._owner and self.lease_id:
            try:
                self._send("/api/v1/lease/checkin", {"lease_id": self.lease_id})
            except Exception:  # noqa: BLE001 — best-effort; TTL reclaims it anyway
                pass


class _LeaseHandle:
    """Per-launch handle over the process-shared machine lease. API-compatible with
    the previous LeaseSession: exposes ``.token`` (live) and ``.stop()``."""

    __slots__ = ("_ml",)

    def __init__(self, ml: "_MachineLease"):
        self._ml = ml

    @property
    def token(self) -> str | None:
        return self._ml.token

    def bind_launch(self):
        """Bind a per-launch run-token file that follows the shared machine lease's rotating token.
        Returns ``(file_path, release)``."""
        return self._ml.bind_launch()

    def refresh_token(self) -> bool:
        """The engine just refused this token as older than one it accepted: mint a fresh one."""
        return self._ml.refresh_if_behind_engine(force=True)

    def stop(self) -> None:
        self._ml.release()


class _BrowserLease:
    """One browser's own lease on a per-browser plan (the free tier: "1 browser at a time"; paid licences
    without a cap: every browser counted).

    Checked out for exactly one launch, heartbeated while that browser runs, checked in when it
    closes. Its token is never written to the shared cache and never handed to another launch; a paid
    one is kept as the grace token (_write_grace). Same surface as _LeaseHandle: ``.token`` and ``.stop()``."""

    def __init__(self, owner: "_MachineLease", body: dict, launch_id: str):
        self._owner = owner
        self._launch_id = launch_id
        self._lock = threading.Lock()
        # Follows this browser's rotating token into one file per launch (CLEARCOTE_RUN_TOKEN_FILE).
        self._token_files = _TokenFileSet()
        self._token: str = body["token"]
        self.lease_id: str = body["lease_id"]
        _write_grace(owner._key, self._token, body.get("exp"))  # paid tokens only
        self._stopped = threading.Event()
        interval = max(5, int(body.get("heartbeat_interval_sec") or 120))
        self._thread = threading.Thread(target=self._loop, args=(interval,), daemon=True)
        self._thread.start()

    @property
    def token(self) -> str:
        return self._token

    @token.setter
    def token(self, v: str) -> None:
        self._token = v
        self._token_files.update(v)  # rotation follows the browser's token automatically

    def bind_launch(self):
        """Bind a per-launch run-token file that follows this browser's rotating token. Returns
        ``(file_path, release)``."""
        return self._token_files.bind(self._token)

    def _took(self, body: dict) -> None:
        """Adopt the token a heartbeat or re-checkout returned (and keep a paid one as the grace token)."""
        self.token = body["token"]
        _write_grace(self._owner._key, body["token"], body.get("exp"))

    def refresh_token(self) -> bool:
        """The engine just refused this token as older than one it accepted: mint a fresh one by heartbeating
        this browser's lease, or, if the lease is gone, by re-taking the slot as the SAME launch."""
        with self._lock:
            status, body = self._owner._send("/api/v1/lease/heartbeat",
                                             {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
            if status == 200 and body.get("token"):
                self._took(body)
                return True
            if status != 409:
                return False
            b2 = self._owner.checkout_for(self._launch_id)  # a refusal (another browser holds it) raises
            self.lease_id = b2["lease_id"]
            self._took(b2)
            return True

    def _loop(self, interval: int) -> None:
        while not self._stopped.wait(interval):
            try:
                status, body = self._owner._send("/api/v1/lease/heartbeat",
                                                 {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if self._stopped.is_set():
                    return
                if status == 409:
                    # Reclaimed (e.g. missed beats): re-take the slot as the SAME launch, which the backend
                    # treats as this browser's own lease. Refused if another browser took the slot meanwhile.
                    b2 = self._owner.checkout_for(self._launch_id)
                    with self._lock:
                        self.lease_id = b2["lease_id"]
                        self._took(b2)
                elif status == 200 and body.get("token"):
                    with self._lock:
                        self._took(body)
                # Anything else (RATE_LIMITED, a 5xx, ...): the next beat tries again.
            except Exception:  # noqa: BLE001 — transient; the next beat retries, the lease TTL is the backstop
                pass

    def stop(self, wait: bool = False) -> None:
        """Release this browser's slot (idempotent). Called from Playwright's close/disconnected event,
        so the check-in runs on its own thread instead of blocking the event handler; ``wait=True``
        (process exit) does it inline."""
        with self._lock:
            if self._stopped.is_set():
                return
            self._stopped.set()
            lease_id = self.lease_id
        self._token_files.close_all()  # remove any launch token files still bound to this browser
        self._owner._forget_browser(self)

        def checkin():
            try:
                self._owner._send("/api/v1/lease/checkin", {"lease_id": lease_id})
            except Exception:  # noqa: BLE001 — best-effort; the lease TTL reclaims it
                pass

        if wait:
            checkin()
        else:
            # Not a daemon: the interpreter waits for the check-in instead of dropping it at exit.
            threading.Thread(target=checkin, daemon=False).start()


class _UncountedLease:
    """A paid browser started on a cached run-token while the licence server gave no verdict (see
    _MachineLease._uncounted). It holds no lease: no heartbeat, no check-in, and the next checkout reports
    it instead. Same surface as _LeaseHandle: ``.token`` and ``.stop()``."""

    refresh_token = None  # nothing to refresh without the licence server

    def __init__(self, owner: "_MachineLease", token: str):
        self._owner = owner
        self.token = token
        self._token_files = _TokenFileSet()

    def bind_launch(self):
        """Bind a per-launch run-token file holding this launch's token. Returns ``(file_path, release)``."""
        return self._token_files.bind(self.token)

    def stop(self, wait: bool = False) -> None:
        self._token_files.close_all()
        self._owner._forget_browser(self)


_MACHINE_LEASES: dict[str, _MachineLease] = {}
_REG_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


def _shutdown_all() -> None:
    for ml in list(_MACHINE_LEASES.values()):
        try:
            ml.shutdown()
        except Exception:  # noqa: BLE001
            pass


def acquire_lease(license_key: str | None = None, api_base: str | None = None,
                  sdk_version: str | None = None, quiet: bool = False,
                  engine_version=None, license_through_proxy=None, proxy=None):
    """Acquire a concurrency lease for one launch. Returns None in free mode (no key).

    Machine plans (paid licences with a concurrency cap): a per-MACHINE lease, shared across every
    launch in this process; a handle's stop() only drops a reference. Per-browser plans (the GitHub
    free tier, and paid licences without a cap; told by the backend's lease_scope "browser"): every
    launch checks out its own slot and stop() releases it, so on the free tier a second browser is
    refused while one runs. A paid per-browser launch the licence server gives no verdict for
    (unreachable, 5xx, a challenge, RATE_LIMITED) starts uncounted on the grace token instead.

    For machine plans: returns the shared handle. The backend is contacted at most once per
    token-TTL (not once per launch); subsequent launches reuse the shared token with
    zero calls. Raises ConcurrencyLimitError / LicenseRevokedError / LicenseError only
    on a cold checkout that the backend definitively refuses; falls back to a cached,
    still-valid token on a transient network failure (offline grace).

    ``license_through_proxy=True`` (or CLEARCOTE_LICENSE_THROUGH_PROXY=1) sends checkout, heartbeat
    and checkin through ``proxy`` (the launch's own proxy) instead of directly from this machine."""
    key = resolve_license_key(license_key)
    if not key:
        return None  # free mode — inert

    base, fallback = _lease_bases(api_base)
    wants_proxy = license_through_proxy_requested(license_through_proxy)
    via = to_proxy_spec(proxy) if (wants_proxy and proxy) else None
    if wants_proxy and not via and not quiet:
        sys.stderr.write("[clearcote] [license] license_through_proxy is on but this launch has no "
                         "proxy; licence calls go direct.\n")
    # One lease per (key, route): a direct lease and a proxied lease are different network paths, so
    # they must not share the in-process heartbeat owner. The proxy username is part of the route:
    # many residential gateways select the exit (country/session) by username on one server.
    map_key = f"{key}|{via['server']}|{via.get('username') or ''}" if via else key
    global _ATEXIT_REGISTERED
    with _REG_LOCK:
        ml = _MACHINE_LEASES.get(map_key)
        if ml is None:
            ml = _MachineLease(key, base, resolve_instance_id(), sdk_version, quiet,
                               engine_version=engine_version, proxy=via, fallback=fallback)
            _MACHINE_LEASES[map_key] = ml
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_all)
            _ATEXIT_REGISTERED = True
    return ml.acquire()  # network/checkout happens here, outside the registry lock


def inject_run_token(pw_kwargs: dict, token: str, token_file: str | None = None) -> None:
    """Merge CLEARCOTE_RUN_TOKEN into pw_kwargs['env'] (base defaults to os.environ).

    When ``token_file`` is given, also set CLEARCOTE_RUN_TOKEN_FILE. A supporting engine (r23+)
    re-reads that file so revoke/check-in/over-limit stops a running free browser; older engines
    ignore it. Additive: the launch-time token above is unchanged."""
    env = dict(pw_kwargs.get("env") or os.environ)
    env[_RUN_TOKEN_ENV] = token
    if token_file:
        env[f"{_RUN_TOKEN_ENV}_FILE"] = token_file
    pw_kwargs["env"] = env


# ---------------------------------------------------------------------------
# Seats + key storage (used by the `clearcote` CLI)
# ---------------------------------------------------------------------------

def get_session_seats(license_key: str | None = None, api_base: str | None = None, proxy=None,
                      license_through_proxy=None) -> dict:
    """Seats in use on a licence right now (live leases), without checking one out.

    Returns ``{"state": "ok", "used": n, "limit": n|None, "plan"?: str}`` or
    ``{"state": "no-key"|"invalid"|"unavailable", "reason"?: str}``. Never cached and never raises:
    an unreachable backend, or an older backend without the endpoint, reports ``unavailable`` with
    the reason rather than a guessed number."""
    key = resolve_license_key(license_key)
    if not key:
        return {"state": "no-key"}
    try:
        via = to_proxy_spec(proxy) if (license_through_proxy_requested(license_through_proxy) and proxy) else None

        def call(base):
            return proxied_request(f"{base}/api/v1/lease/seats", method="GET",
                                   headers={"authorization": f"Bearer {key}", "User-Agent": _user_agent()},
                                   timeout=15.0, proxy=via)

        def unanswered(r):
            rh = getattr(r, "headers", None) or {}
            return r.status in _GATEWAY_STATUSES or bool(_challenge_reason(
                r.status, rh.get("content-type", ""), r.text(), rh.get("x-vercel-mitigated", "")))

        res = _with_fallback(*_lease_bases(api_base), (via["server"], via.get("username")) if via else None,
                             call, unanswered)
        try:
            body = res.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        used = body.get("used")
        if res.ok and isinstance(used, (int, float)) and not isinstance(used, bool):
            out = {"state": "ok", "used": used, "limit": body.get("limit")}
            if body.get("plan") is not None:
                out["plan"] = body["plan"]
            return out
        h = getattr(res, "headers", None) or {}
        why = _challenge_reason(res.status, h.get("content-type", ""), res.text(), h.get("x-vercel-mitigated", ""))
        if why:
            return {"state": "unavailable", "reason": f"licence server behind a security challenge ({why})"}
        if res.status in (401, 403):
            return {"state": "invalid", "reason": body.get("error") or f"HTTP {res.status}"}
        if res.status == 404:
            return {"state": "unavailable", "reason": "this licence server does not report seats yet"}
        return {"state": "unavailable", "reason": body.get("error") or f"HTTP {res.status}"}
    except Exception as e:  # noqa: BLE001
        reason = getattr(e, "reason", None) or e
        return {"state": "unavailable", "reason": f"licence server unreachable ({reason})"}


def license_key_path() -> str:
    """Where ``clearcote login`` stores the key: ~/.clearcote/license.key."""
    return str(Path.home() / ".clearcote" / "license.key")


def save_license_key(key: str) -> str:
    """Save a licence key for every later launch (owner-only permissions where the OS supports it).
    Returns the path written."""
    k = (key or "").strip()
    if not k:
        raise LicenseError("Empty licence key.", "LICENSE_EMPTY")
    p = Path(license_key_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(k + "\n")
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass  # not supported on this filesystem
    return str(p)


def remove_license_key() -> bool:
    """Remove the saved key (and what device login recorded about it). Returns True when the key
    file was removed."""
    remove_license_meta()
    p = Path(license_key_path())
    if not p.exists():
        return False
    p.unlink()
    return True


# What `clearcote login --device` learned about the key it saved. No licence endpoint reports a key's
# expiry (GET /api/v1/lease/seats answers used/limit/plan only), so the device-token answer is the one
# source; `clearcote info` reports it as such. Bound to the key by its SHA-256, so a key saved later
# (pasted, or set in CLEARCOTE_LICENSE_KEY) is never described by another key's record.
_EXPIRY_NOTE = ("as the site reported it at `clearcote login --device`; no licence endpoint reports a "
                "key's expiry")
_EXPIRY_UNKNOWN_NOTE = ("not known: only `clearcote login --device` records it, and no licence endpoint "
                        "reports a key's expiry")


def license_meta_path() -> str:
    return str(Path.home() / ".clearcote" / "license.meta.json")


def _key_digest(key: str) -> str:
    return hashlib.sha256(key.strip().encode()).hexdigest()


def save_license_meta(key: str, plan=None, expires_at=None) -> str:
    """Record the plan and expiry the device login returned for ``key`` (never the key itself)."""
    p = Path(license_meta_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    record = {"key_sha256": _key_digest(key), "plan": plan, "expires_at": expires_at,
              "source": "device-login",
              "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record) + "\n")
    try:
        os.chmod(str(p), 0o600)
    except OSError:
        pass
    return str(p)


def remove_license_meta(unless_key: str | None = None) -> bool:
    """Remove the device-login record; with ``unless_key``, only when it describes a different key."""
    p = Path(license_meta_path())
    if not p.exists():
        return False
    if unless_key is not None:
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("key_sha256") == _key_digest(unless_key):
                return False
        except (OSError, ValueError, AttributeError):
            pass
    try:
        p.unlink()
    except OSError:
        return False
    return True


def license_expiry(explicit: str | None = None) -> dict | None:
    """What is known about when the key a launch would use expires, for ``clearcote info``.

    ``{"expiresAt": iso|None, "source": "device-login", "recordedAt": iso, "note": ...}`` when
    device login recorded it for this very key (``expiresAt`` None = no expiry);
    ``{"source": "unknown", "note": ...}`` for any other key; None without a key."""
    key = resolve_license_key(explicit)
    if not key:
        return None
    try:
        rec = json.loads(Path(license_meta_path()).read_text(encoding="utf-8"))
        if isinstance(rec, dict) and rec.get("key_sha256") == _key_digest(key):
            return {"expiresAt": rec.get("expires_at"), "source": "device-login",
                    "recordedAt": rec.get("recorded_at"), "note": _EXPIRY_NOTE}
    except (OSError, ValueError):
        pass
    return {"source": "unknown", "note": _EXPIRY_UNKNOWN_NOTE}


def _mask(k: str) -> str:
    return f"{k[:7]}\u2026{k[-4:]}" if len(k) > 12 else "\u2026"


def license_key_source(explicit: str | None = None) -> dict:
    """Where the key a launch would use comes from, without revealing it:
    ``{"source": "option"|"env"|"file"|"none", "masked"?: "cc_lic_\u2026abcd"}``."""
    if explicit and explicit.strip():
        return {"source": "option", "masked": _mask(explicit.strip())}
    env = os.environ.get("CLEARCOTE_LICENSE_KEY", "")
    if env.strip():
        return {"source": "env", "masked": _mask(env.strip())}
    try:
        p = Path(license_key_path())
        if p.exists():
            v = p.read_text(encoding="utf-8").strip()
            if v:
                return {"source": "file", "masked": _mask(v)}
    except OSError:
        pass
    return {"source": "none"}
