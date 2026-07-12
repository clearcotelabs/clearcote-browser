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
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib import request, error

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
    checking out again. Older caches without lease_id are still honored."""
    try:
        d = json.loads(_cache_path(license_key).read_text())
        if isinstance(d.get("token"), str) and isinstance(d.get("exp"), (int, float)):
            return d
    except (OSError, ValueError):
        pass
    return None


def _write_cache(license_key: str, token: str, exp: float, lease_id: str | None = None) -> None:
    try:
        p = _cache_path(license_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"token": token, "exp": exp, "lease_id": lease_id}))
    except OSError:
        pass


def _post(url: str, license_key: str, body: dict, timeout: float = 15.0):
    data = json.dumps(body).encode()
    req = request.Request(url, data=data, method="POST", headers={
        "authorization": f"Bearer {license_key}",
        "content-type": "application/json",
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
                 sdk_version: str | None, quiet: bool):
        self._key = key
        self._base = base
        self._instance_id = instance_id
        self._sdk_version = sdk_version
        self._quiet = quiet
        self._lock = threading.RLock()
        self.token: str | None = None
        self.exp: float = 0.0
        self.lease_id: str | None = None
        self._hb_sec = 270
        self._owner = False          # only the cold-checkout owner heartbeats/checkins
        self._hb_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._refs = 0

    def _valid(self) -> bool:
        return bool(self.token) and self.exp > time.time() + _SKEW_SEC

    def ensure(self) -> None:
        """Make a usable run-token available with the fewest possible backend calls.
        Raises on a definitive limit/revoke verdict (cold checkout path only)."""
        with self._lock:
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
            self._owner = True
            self._start_heartbeat()

    def _checkout(self) -> None:
        try:
            status, body = _post(f"{self._base}/api/v1/lease/checkout", self._key,
                                 {"instance_id": self._instance_id, "os": _os_tag(),
                                  "sdk_version": self._sdk_version})
            if status != 200:
                _raise_for_status(status, body)
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
                status, body = _post(f"{self._base}/api/v1/lease/heartbeat", self._key,
                                      {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if status == 409:  # reclaimed/expired -> re-checkout to keep the slot
                    st2, b2 = _post(f"{self._base}/api/v1/lease/checkout", self._key,
                                    {"instance_id": self._instance_id, "os": _os_tag(),
                                     "sdk_version": self._sdk_version})
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

    def acquire(self):
        """Ensure a live token, bump the refcount, return a per-launch handle."""
        self.ensure()
        with self._lock:
            self._refs += 1
        return _LeaseHandle(self)

    def release(self) -> None:
        """A per-launch handle closed. We do NOT checkin here — the machine slot is
        held for the process lifetime (any launch may reuse it) and released once at
        exit. This is what removes the per-launch checkin churn."""
        with self._lock:
            if self._refs > 0:
                self._refs -= 1

    def shutdown(self) -> None:
        """Stop the heartbeat and release the slot once, at process exit."""
        self._stop.set()
        if self._owner and self.lease_id:
            try:
                _post(f"{self._base}/api/v1/lease/checkin", self._key, {"lease_id": self.lease_id})
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

    def stop(self) -> None:
        self._ml.release()


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
                  sdk_version: str | None = None, quiet: bool = False):
    """Acquire a per-MACHINE concurrency lease, shared across every launch in this
    process. Returns None in free mode. The backend is contacted at most once per
    token-TTL (not once per launch); subsequent launches reuse the shared token with
    zero calls. Raises ConcurrencyLimitError / LicenseRevokedError / LicenseError only
    on a cold checkout that the backend definitively refuses; falls back to a cached,
    still-valid token on a transient network failure (offline grace)."""
    key = resolve_license_key(license_key)
    if not key:
        return None  # free mode — inert

    base = _api_base(api_base)
    global _ATEXIT_REGISTERED
    with _REG_LOCK:
        ml = _MACHINE_LEASES.get(key)
        if ml is None:
            ml = _MachineLease(key, base, resolve_instance_id(), sdk_version, quiet)
            _MACHINE_LEASES[key] = ml
        if not _ATEXIT_REGISTERED:
            atexit.register(_shutdown_all)
            _ATEXIT_REGISTERED = True
    return ml.acquire()  # network/checkout happens here, outside the registry lock


def inject_run_token(pw_kwargs: dict, token: str) -> None:
    """Merge CLEARCOTE_RUN_TOKEN into pw_kwargs['env'] (base defaults to os.environ)."""
    env = dict(pw_kwargs.get("env") or os.environ)
    env[_RUN_TOKEN_ENV] = token
    pw_kwargs["env"] = env
