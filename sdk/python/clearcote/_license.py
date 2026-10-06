"""Floating-concurrency licensing client (opt-in), mirroring the Node SDK.

When a license key is present, the SDK checks out one of the license's N
concurrency slots from the backend, receives a short-lived Ed25519 run-token,
and injects it into the engine as CLEARCOTE_RUN_TOKEN. A background heartbeat
keeps the slot alive + rotates the token; on close the slot is released. With no
license key this is entirely inert (free mode). See PRIVATE-SDK-LICENSING-PLAN.md.

Concurrency is per-MACHINE: the backend dedups by a stable instance_id, so one
machine holds exactly one slot regardless of how many browsers it runs. To avoid
hammering the backend, the lease is **shared across all launches in a process**
(one checkout per token-TTL, not one per launch) — see ``_MachineLease``.
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
_RUN_TOKEN_ENV = "CLEARCOTE_RUN_TOKEN"
# Seconds of headroom kept before a token's exp: reuse it only while it's still
# valid with this much slack, so an in-flight launch never ships an expiring token.
_SKEW_SEC = 60


class LicenseError(RuntimeError):
    code = "LICENSE_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class ConcurrencyLimitError(LicenseError):
    code = "CONCURRENCY_LIMIT_EXCEEDED"


class LicenseRevokedError(LicenseError):
    code = "LICENSE_REVOKED"


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


def _os_tag() -> str:
    return {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "unknown")


def _cache_path(license_key: str) -> Path:
    h = hashlib.sha256(license_key.encode()).hexdigest()[:16]
    return Path.home() / ".clearcote" / f"lease-{h}.json"


def _read_cache(license_key: str):
    """The on-disk shared token cache: {token, exp, lease_id}. Enables cross-process
    reuse on one machine — a second process picks up a still-valid token instead of
    checking out again. Older caches without lease_id are still honored.

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


def _write_cache(license_key: str, token: str, exp: float, lease_id: str | None = None) -> None:
    try:
        p = _cache_path(license_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"token": token, "exp": exp, "lease_id": lease_id}))
    except OSError:
        pass


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


def _post(url: str, license_key: str, body: dict, timeout: float = 15.0, proxy=None):
    if proxy:
        # license_through_proxy: through the launch proxy (HTTP CONNECT or SOCKS5). The direct path
        # below is unchanged from before the option existed.
        res = proxied_request(url, method="POST", body=json.dumps(body), timeout=30.0, proxy=proxy,
                              headers={"authorization": f"Bearer {license_key}",
                                       "content-type": "application/json",
                                       "User-Agent": _user_agent()})
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
        try:
            payload = json.loads(e.read().decode() or "{}")
        except ValueError:
            payload = {}
        return e.code, payload


def _raise_for_status(status: int, body: dict):
    msg = body.get("error") or f"License request failed ({status})."
    code = body.get("code")
    if status == 429 or code == "CONCURRENCY_LIMIT_EXCEEDED":
        raise ConcurrencyLimitError(msg, "CONCURRENCY_LIMIT_EXCEEDED")
    if status == 403 or code in ("LICENSE_REVOKED", "LICENSE_EXPIRED"):
        raise LicenseRevokedError(msg, "LICENSE_REVOKED")
    raise LicenseError(msg, code or f"HTTP_{status}")


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
                 sdk_version: str | None, quiet: bool, engine_version=None, proxy=None):
        self._key = key
        self._proxy = proxy  # proxy spec when license_through_proxy is on, else None (direct)
        self._base = base
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

    @property
    def token(self) -> str | None:
        return self._token

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

    def _send(self, url: str, body: dict):
        """POST a lease call: through the launch proxy when license_through_proxy is on, else the
        unchanged direct path."""
        if self._proxy:
            return _post(url, self._key, body, proxy=self._proxy)
        return _post(url, self._key, body)

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

    def ensure(self) -> None:
        """Make a usable run-token available with the fewest possible backend calls.
        Raises on a definitive limit/revoke verdict (cold checkout path only)."""
        with self._lock:
            if self.scope == "browser":
                return  # per-browser plan: nothing is shared; acquire() checks out per launch
            if self._valid():
                return  # already holding a live token (owner or reusing) — zero calls
            cached = _read_cache(self._key)
            if cached and cached["exp"] > time.time() + _SKEW_SEC:
                # cross-process reuse: another process's owner is keeping the slot alive.
                self.token = cached["token"]
                self.exp = float(cached["exp"])
                self.lease_id = cached.get("lease_id")
                self._owner = False
                return  # NO checkout, NO heartbeat
            # cold: this process owns the slot, the heartbeat, and the exit checkin.
            self._checkout()
            if self.scope == "browser":
                return  # the checkout belongs to the launch that asked; see acquire()
            self._owner = True
            self._start_heartbeat()

    def checkout_for(self, launch_id: str) -> dict:
        """POST a checkout for one launch. Raises the backend's verdict; network errors propagate."""
        status, body = self._send(f"{self._base}/api/v1/lease/checkout",
                                  {"instance_id": self._instance_id,
                                   # per-browser plans count each launch_id as its own slot; machine plans ignore it
                                   "launch_id": launch_id,
                                   "os": _os_tag(),
                                   "sdk_version": self._sdk_version,
                                   "engine_version": self._engine_ver()})
        if status != 200:
            _raise_for_status(status, body)
        return body

    def _checkout(self) -> None:
        launch_id = new_launch_id()
        try:
            body = self.checkout_for(launch_id)
        except LicenseError:
            raise  # a definitive verdict must surface (never silently downgrade)
        except Exception as e:  # noqa: BLE001 — network/other: offline grace on a cached token
            cached = _read_cache(self._key)
            if cached and cached["exp"] > time.time() + _SKEW_SEC:
                if not self._quiet:
                    sys.stderr.write(f"[clearcote] [license] backend unreachable ({e}); using cached run-token.\n")
                self.token = cached["token"]
                self.exp = float(cached["exp"])
                self.lease_id = cached.get("lease_id")
                return
            raise LicenseError(f"Could not reach the license server and no valid cached token: {e}")
        if body.get("lease_scope") == "browser":
            # Per-browser plan: this checkout is the calling launch's own slot. Never shared, never cached.
            self.scope = "browser"
            self._pending_browser = (body, launch_id)
            return
        self.token = body["token"]
        self.exp = float(body["exp"])
        self.lease_id = body["lease_id"]
        self._hb_sec = int(body.get("heartbeat_interval_sec") or 270)
        _write_cache(self._key, self.token, self.exp, self.lease_id)

    def _start_heartbeat(self) -> None:
        if self._hb_thread and self._hb_thread.is_alive():
            return
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, args=(max(5, self._hb_sec),), daemon=True)
        self._hb_thread.start()

    def _heartbeat_loop(self, interval: int) -> None:
        while not self._stop.wait(interval):
            try:
                status, body = self._send(f"{self._base}/api/v1/lease/heartbeat",
                                          {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if status == 409:  # reclaimed/expired -> re-checkout to keep the slot
                    st2, b2 = self._send(f"{self._base}/api/v1/lease/checkout",
                                         {"instance_id": self._instance_id, "os": _os_tag(),
                                          "sdk_version": self._sdk_version,
                                          "engine_version": self._engine_ver()})
                    if st2 == 200:
                        with self._lock:
                            self.lease_id = b2["lease_id"]
                            self.token = b2["token"]
                            self.exp = float(b2["exp"])
                        _write_cache(self._key, b2["token"], b2["exp"], b2["lease_id"])
                elif status == 200 and body.get("token"):
                    with self._lock:
                        self.token = body["token"]
                        self.exp = float(body["exp"])
                    _write_cache(self._key, body["token"], body["exp"], self.lease_id)
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
                status, body = self._send(f"{self._base}/api/v1/lease/heartbeat",
                                          {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
            except Exception:  # noqa: BLE001 — unreachable: a checkout below says so properly
                status, body = 0, {}
            if status == 200 and body.get("token"):
                self.token = body["token"]
                self.exp = float(body["exp"])
                _write_cache(self._key, self.token, self.exp, self.lease_id)
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
            self.ensure()
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
        except LicenseError:
            raise
        except Exception as e:  # noqa: BLE001 — no offline grace: without the backend there is no slot
            raise LicenseError(f"Could not reach the license server to start this browser: {e}")
        return self._start_browser(body, launch_id)

    def _start_browser(self, body: dict, launch_id: str):
        lease = _BrowserLease(self, body, launch_id)
        with self._lock:
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
                self._send(f"{self._base}/api/v1/lease/checkin", {"lease_id": self.lease_id})
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
    """One browser's own lease on a per-browser plan (the free tier: "1 browser at a time").

    Checked out for exactly one launch, heartbeated while that browser runs, checked in when it
    closes. Its token is never written to the shared cache and never handed to another launch.
    Same surface as _LeaseHandle: ``.token`` and ``.stop()``."""

    def __init__(self, owner: "_MachineLease", body: dict, launch_id: str):
        self._owner = owner
        self._launch_id = launch_id
        self._lock = threading.Lock()
        # Follows this browser's rotating token into one file per launch (CLEARCOTE_RUN_TOKEN_FILE).
        self._token_files = _TokenFileSet()
        self._token: str = body["token"]
        self.lease_id: str = body["lease_id"]
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

    def refresh_token(self) -> bool:
        """The engine just refused this token as older than one it accepted: mint a fresh one by heartbeating
        this browser's lease, or, if the lease is gone, by re-taking the slot as the SAME launch."""
        with self._lock:
            status, body = self._owner._send(f"{self._owner._base}/api/v1/lease/heartbeat",
                                             {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
            if status == 200 and body.get("token"):
                self.token = body["token"]
                return True
            if status != 409:
                return False
            b2 = self._owner.checkout_for(self._launch_id)  # a refusal (another browser holds it) raises
            self.lease_id = b2["lease_id"]
            self.token = b2["token"]
            return True

    def _loop(self, interval: int) -> None:
        while not self._stopped.wait(interval):
            try:
                status, body = self._owner._send(f"{self._owner._base}/api/v1/lease/heartbeat",
                                                 {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if self._stopped.is_set():
                    return
                if status == 409:
                    # Reclaimed (e.g. missed beats): re-take the slot as the SAME launch, which the backend
                    # treats as this browser's own lease. Refused if another browser took the slot meanwhile.
                    b2 = self._owner.checkout_for(self._launch_id)
                    with self._lock:
                        self.lease_id = b2["lease_id"]
                        self.token = b2["token"]
                elif status == 200 and body.get("token"):
                    with self._lock:
                        self.token = body["token"]
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
                self._owner._send(f"{self._owner._base}/api/v1/lease/checkin", {"lease_id": lease_id})
            except Exception:  # noqa: BLE001 — best-effort; the lease TTL reclaims it
                pass

        if wait:
            checkin()
        else:
            # Not a daemon: the interpreter waits for the check-in instead of dropping it at exit.
            threading.Thread(target=checkin, daemon=False).start()


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

    Machine plans (every paid plan): a per-MACHINE lease, shared across every launch in this
    process; a handle's stop() only drops a reference. Per-browser plans (the GitHub free tier,
    told by the backend's lease_scope "browser"): every launch checks out its own slot and stop()
    releases it, so a second browser is refused while one runs.

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

    base = _api_base(api_base)
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
                               engine_version=engine_version, proxy=via)
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
        res = proxied_request(f"{_api_base(api_base)}/api/v1/lease/seats", method="GET",
                              headers={"authorization": f"Bearer {key}", "User-Agent": _user_agent()},
                              timeout=15.0, proxy=via)
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
