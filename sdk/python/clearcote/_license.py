"""Floating-concurrency licensing client (opt-in), mirroring the Node SDK.

When a license key is present, the SDK checks out one of the license's N
concurrency slots from the backend, receives a short-lived Ed25519 run-token,
and injects it into the engine as CLEARCOTE_RUN_TOKEN. A background heartbeat
keeps the slot alive + rotates the token; on close the slot is released. With no
license key this is entirely inert (free mode). See PRIVATE-SDK-LICENSING-PLAN.md.
"""
from __future__ import annotations

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


def _api_base(api_base: str | None) -> str:
    return (api_base or os.environ.get("CLEARCOTE_LICENSE_API") or DEFAULT_API_BASE).rstrip("/")


def _os_tag() -> str:
    return {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "unknown")


def _cache_path(license_key: str) -> Path:
    h = hashlib.sha256(license_key.encode()).hexdigest()[:16]
    return Path.home() / ".clearcote" / f"lease-{h}.json"


def _read_cache(license_key: str):
    try:
        d = json.loads(_cache_path(license_key).read_text())
        if isinstance(d.get("token"), str) and isinstance(d.get("exp"), (int, float)):
            return d
    except (OSError, ValueError):
        pass
    return None


def _write_cache(license_key: str, token: str, exp: int) -> None:
    try:
        p = _cache_path(license_key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"token": token, "exp": exp}))
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


class LeaseSession:
    """A live concurrency lease. Call ``stop()`` on close to free the slot."""

    def __init__(self, license_key: str, base: str, lease_id: str, token: str,
                 instance_id: str, hb_sec: int, sdk_version: str | None, quiet: bool):
        self._key = license_key
        self._base = base
        self._instance_id = instance_id
        self._sdk_version = sdk_version
        self._quiet = quiet
        self.lease_id = lease_id
        self.token = token
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat_loop, args=(max(5, hb_sec),), daemon=True)
        self._thread.start()

    def _heartbeat_loop(self, interval: int):
        while not self._stop.wait(interval):
            try:
                status, body = _post(f"{self._base}/api/v1/lease/heartbeat", self._key,
                                      {"lease_id": self.lease_id, "nonce": str(uuid.uuid4())})
                if status == 409:  # reclaimed/expired -> re-checkout to keep the slot
                    st2, b2 = _post(f"{self._base}/api/v1/lease/checkout", self._key,
                                    {"instance_id": self._instance_id, "os": _os_tag(),
                                     "sdk_version": self._sdk_version})
                    if st2 == 200:
                        self.lease_id = b2["lease_id"]
                        self.token = b2["token"]
                        _write_cache(self._key, b2["token"], b2["exp"])
                elif status == 200 and body.get("token"):
                    self.token = body["token"]
                    _write_cache(self._key, body["token"], body["exp"])
            except Exception:  # noqa: BLE001 — transient; offline grace until token exp
                pass

    def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        try:
            _post(f"{self._base}/api/v1/lease/checkin", self._key, {"lease_id": self.lease_id})
        except Exception:  # noqa: BLE001 — best-effort; TTL reclaims it anyway
            pass


def acquire_lease(license_key: str | None = None, api_base: str | None = None,
                  sdk_version: str | None = None, quiet: bool = False):
    """Acquire a concurrency lease if a license key is configured, else return None.

    Raises ConcurrencyLimitError / LicenseRevokedError / LicenseError when a key
    IS present but the backend refuses. Falls back to a cached, still-valid token
    on a transient network failure (offline grace).
    """
    key = resolve_license_key(license_key)
    if not key:
        return None  # free mode — inert

    base = _api_base(api_base)
    instance_id = str(uuid.uuid4())
    try:
        status, body = _post(f"{base}/api/v1/lease/checkout", key,
                             {"instance_id": instance_id, "os": _os_tag(), "sdk_version": sdk_version})
        if status != 200:
            _raise_for_status(status, body)
        _write_cache(key, body["token"], body["exp"])
    except LicenseError:
        raise  # a definitive verdict must surface (never silently downgrade)
    except Exception as e:  # noqa: BLE001 — network/other: try cached token
        cached = _read_cache(key)
        if cached and cached["exp"] > time.time() + 60:
            if not quiet:
                sys.stderr.write(f"[clearcote] [license] backend unreachable ({e}); using cached run-token.\n")
            s = LeaseSession.__new__(LeaseSession)
            s.token, s.lease_id, s._stop = cached["token"], "cached", threading.Event()
            s._stop.set()
            s.stop = lambda: None  # type: ignore[assignment]
            return s
        raise LicenseError(f"Could not reach the license server and no valid cached token: {e}")

    return LeaseSession(key, base, body["lease_id"], body["token"], instance_id,
                        int(body.get("heartbeat_interval_sec") or 30), sdk_version, quiet)


def inject_run_token(pw_kwargs: dict, token: str) -> None:
    """Merge CLEARCOTE_RUN_TOKEN into pw_kwargs['env'] (base defaults to os.environ)."""
    env = dict(pw_kwargs.get("env") or os.environ)
    env[_RUN_TOKEN_ENV] = token
    pw_kwargs["env"] = env
