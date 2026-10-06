"""``clearcote login --device``: sign in from a browser instead of pasting a key (OAuth 2.0 device
authorization grant, RFC 8628, shaped to the Clearcote site's endpoints).

    POST {base}/api/v1/device/code   {"client_name", "client_version"}
      -> {"device_code", "user_code", "verification_uri", "verification_uri_complete",
          "expires_in", "interval"}
    POST {base}/api/v1/device/token  {"device_code"}
      -> 200 {"license_key", "plan", "expires_at"}
       | 400 {"error": "authorization_pending" | "slow_down" | "expired_token" | "access_denied"
                       | "invalid_request"}
       | 429 (rate limited)

``base`` is the licence server the rest of the SDK uses (CLEARCOTE_LICENSE_API, else
https://www.clearcotelabs.com). The key that comes back is saved exactly where and how
``clearcote login <key>`` saves one, and is never printed. Mirrors devicelogin.ts in the Node SDK.
"""
from __future__ import annotations

import json
import time

from ._license import _api_base, _user_agent
from ._net import proxied_request

CLIENT_NAME = "clearcote-python"
# RFC 8628 section 3.5: on slow_down the client adds 5 seconds to its polling interval, for good.
SLOW_DOWN_STEP = 5
# When the server sends no interval. Also the floor: a server answering 0 must not make us spin.
DEFAULT_INTERVAL = 5
MIN_INTERVAL = 1


class DeviceLoginError(RuntimeError):
    """The device login ended without a key. ``code`` is the server's error code (``expired_token``,
    ``access_denied``, ``invalid_request``) or one of ours (``unreachable``, ``unsupported``,
    ``bad_response``)."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def _post(base: str, path: str, body: dict, timeout: float):
    """(status, payload dict, headers). Raises OSError/ValueError-family errors on network trouble."""
    res = proxied_request(f"{base}{path}", method="POST", body=json.dumps(body), timeout=timeout,
                          headers={"content-type": "application/json", "accept": "application/json",
                                   "User-Agent": _user_agent()})
    try:
        payload = res.json() if res.text().strip() else {}
    except ValueError:
        payload = {}
    return res.status, payload if isinstance(payload, dict) else {}, res.headers


def _seconds(value, default):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n == n and n >= 0 else default  # NaN / negative -> default


def request_code(api_base: str | None = None, client_version: str | None = None, timeout: float = 15.0) -> dict:
    """Start a device login. Returns the server's answer with ``expires_in`` and ``interval`` as
    numbers (interval at least MIN_INTERVAL) and ``verification_uri_complete`` filled in from
    ``verification_uri`` when the server left it out."""
    base = _api_base(api_base)
    if client_version is None:
        from . import __version__ as client_version
    try:
        status, body, _h = _post(base, "/api/v1/device/code",
                                 {"client_name": CLIENT_NAME, "client_version": client_version}, timeout)
    except Exception as e:  # noqa: BLE001 -- any transport failure means "could not reach it"
        reason = getattr(e, "reason", None) or e
        raise DeviceLoginError(f"could not reach the licence server at {base} ({reason})", "unreachable") from None
    if status in (404, 405):
        raise DeviceLoginError(
            f"the licence server at {base} does not support device login (HTTP {status}). "
            "Paste a key instead: clearcote login <key>", "unsupported")
    if status != 200:
        detail = body.get("error_description") or body.get("error") or f"HTTP {status}"
        raise DeviceLoginError(f"the licence server did not start a device login ({detail})", "bad_response")
    missing = [k for k in ("device_code", "user_code", "verification_uri") if not body.get(k)]
    if missing:
        raise DeviceLoginError(f"the licence server's device login answer is missing {', '.join(missing)}",
                               "bad_response")
    out = dict(body)
    out["expires_in"] = _seconds(body.get("expires_in"), 600)
    out["interval"] = max(MIN_INTERVAL, _seconds(body.get("interval"), DEFAULT_INTERVAL))
    out["verification_uri_complete"] = body.get("verification_uri_complete") or body["verification_uri"]
    return out


def poll_for_key(code: dict, api_base: str | None = None, sleep=time.sleep, clock=time.monotonic,
                 on_retry=None, timeout: float = 15.0) -> dict:
    """Poll the token endpoint until the user approves (returns ``{"license_key", "plan",
    "expires_at"}``), denies, or the code expires (both raise DeviceLoginError).

    authorization_pending keeps the interval; slow_down and HTTP 429 add 5 seconds to it for the rest
    of the login. A network error or a 5xx is retried at the same interval until the code expires
    (``on_retry(reason)`` is told each time). KeyboardInterrupt from ``sleep`` propagates."""
    base = _api_base(api_base)
    interval = code["interval"]
    deadline = clock() + code["expires_in"]
    while True:
        if clock() >= deadline:  # the server would answer expired_token now: no need to ask
            raise DeviceLoginError("the code expired before it was approved", "expired_token")
        sleep(interval)
        try:
            status, body, headers = _post(base, "/api/v1/device/token", {"device_code": code["device_code"]},
                                          timeout)
        except Exception as e:  # noqa: BLE001 -- transient: the code is still valid on the server
            if on_retry:
                on_retry(f"licence server unreachable ({getattr(e, 'reason', None) or e})")
            continue
        if status == 200:
            key = body.get("license_key")
            if not isinstance(key, str) or not key.strip():
                raise DeviceLoginError("the licence server approved the login but sent no licence key",
                                       "bad_response")
            return {"license_key": key.strip(), "plan": body.get("plan"), "expires_at": body.get("expires_at")}
        error = body.get("error")
        if status == 429 or error == "slow_down":
            interval += SLOW_DOWN_STEP
            retry_after = _seconds((headers or {}).get("retry-after"), 0)
            interval = max(interval, retry_after)
            continue
        if error == "authorization_pending":
            continue
        if error == "expired_token":
            raise DeviceLoginError("the code expired before it was approved", "expired_token")
        if error == "access_denied":
            raise DeviceLoginError("the sign-in was denied in the browser", "access_denied")
        if error == "invalid_request":
            raise DeviceLoginError("the licence server rejected the device login request (invalid_request)",
                                   "invalid_request")
        if status >= 500:
            if on_retry:
                on_retry(f"licence server error (HTTP {status})")
            continue
        raise DeviceLoginError(f"unexpected answer from the licence server ({error or f'HTTP {status}'})",
                               "bad_response")
