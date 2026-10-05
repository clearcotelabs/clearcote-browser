"""Clearcote Cloud: hosted browsers, agent runs, profiles, recordings, events, hand-off and webhooks.

The same SDK runs a browser on this machine or on Clearcote's servers, and one flag picks which::

    from clearcote import launch

    browser = launch(cloud=True, country="us", humanize=True)   # or leave cloud unset and export
    page = browser.new_page()                                   # CLEARCOTE_CLOUD=1
    ...                                                         # the same Playwright Browser
    browser.close()                                             # disconnects and ends the session

Everything else the hosted API does is on :class:`Cloud` (and :class:`AsyncCloud`)::

    from clearcote.cloud import Cloud

    cloud = Cloud()   # CLEARCOTE_API_KEY; CLEARCOTE_API_URL points it at another server
    run = cloud.runs.create("Find the price of the cheapest plan", url="https://example.com",
                            schema={"type": "object", "properties": {"price": {"type": "string"}}})
    print(run["status"], run["result"]["output"])

Conventions:

* Standard library only: ``urllib`` for HTTP, ``hmac`` for webhook signatures. Nothing to install.
* Every method returns the parsed JSON body of the endpoint it calls (``None`` for an empty body).
* Every non-2xx answer raises :class:`CloudError` carrying the server's own message and code.
* Options use the Python names (``proxy_session``, ``timeout_sec``); the SDK sends the API's
  (``proxySession``, ``timeoutSec``). Values pass through unchanged and the API validates them.

Mirrors the Node SDK's ``cloud.ts``: same resources, same defaults, same errors, same CLI output.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import inspect
import json
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

__all__ = [
    "DEFAULT_API_URL",
    "LOCAL_ONLY_OPTIONS",
    "RUN_FIELDS",
    "SDK_SIDE_OPTIONS",
    "SESSION_FIELDS",
    "TERMINAL_RUN_STATUSES",
    "AsyncCloud",
    "Cloud",
    "CloudError",
    "CloudTimeoutError",
    "cloud_requested",
    "cookies_from_state",
    "filter_cookies",
    "session_body",
    "verify_webhook",
]

DEFAULT_API_URL = "https://www.clearcotelabs.com"

# A run is finished in exactly these states. "waiting_for_human" is NOT one of them: the run is
# paused on a hand-off and resumes once the person marks it done (or the hand-off times out).
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "cancelled", "expired")

_TRUTHY = ("1", "true", "yes")

# Errors worth another poll rather than giving up on a long wait: no connection, rate limited, or a
# gateway in front of the API restarting. Anything else (401, 404, ...) will not fix itself.
_TRANSIENT = (0, 429, 502, 503, 504)
_MAX_TRANSIENT = 4

NO_API_KEY = ("no Clearcote API key: pass api_key=... or set CLEARCOTE_API_KEY "
              "(create a key in the Clearcote dashboard)")

# Plain http:// is accepted only for these hosts (a local dev control plane): anywhere else the API key
# would cross the network unencrypted.
_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _check_base_url(base):
    """``base`` (already stripped of trailing slashes) if it is an https:// URL, or an http:// URL of
    this machine; ValueError naming the reason otherwise."""
    try:
        parts = urllib.parse.urlsplit(base)
        host = (parts.hostname or "").lower()
    except ValueError:
        parts, host = None, ""
    scheme = parts.scheme.lower() if parts else ""
    if scheme not in ("https", "http") or not host:
        raise ValueError(f"the API URL must start with https:// (got {base!r})")
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"the API URL must use https:// (got http://{host}): over plain http the API key would travel "
            "unencrypted. http:// is only accepted for this machine (127.0.0.1, ::1, localhost)")
    return base


# ── errors ───────────────────────────────────────────────────────────────────────────────────────

class CloudError(Exception):
    """A request the hosted API refused or could not answer.

    ``status`` is the HTTP status (0 when the server could not be reached), ``code`` the API's
    machine-readable code when it sent one (``"PROFILE_IN_USE"``, ``"NOT_READY"``, ...; otherwise
    None) and ``message`` the API's own explanation, which is also ``str(error)``.
    """

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def __repr__(self):
        return f"CloudError(status={self.status!r}, code={self.code!r}, message={self.message!r})"


class CloudTimeoutError(TimeoutError):
    """A wait ran out of time. The run (or hand-off) carries on on the server; ``last`` holds the
    last view the SDK read, so its ``id`` and ``status`` are at hand."""

    def __init__(self, message, last=None):
        super().__init__(message)
        self.last = last


# ── local or cloud ───────────────────────────────────────────────────────────────────────────────

def cloud_requested(cloud=None):
    """Whether a launch should go to the cloud.

    ``cloud=None`` (the default) defers to the environment: ``CLEARCOTE_CLOUD`` set to ``1``,
    ``true`` or ``yes`` means cloud, anything else local. An explicit ``True``/``False`` (or a
    :class:`Cloud` client, which means cloud) always wins over the environment.
    """
    if cloud is None:
        return os.environ.get("CLEARCOTE_CLOUD", "").strip().lower() in _TRUTHY
    if isinstance(cloud, str):  # "0"/"false" from a config file must not mean cloud
        return cloud.strip().lower() in _TRUTHY
    return bool(cloud)


# Python keyword -> POST /api/v1/browsers field. Values pass through as given (proxy and profile
# are normalised below); the API validates them and answers 400 with a message that says what is
# wrong, so the SDK does not keep a second copy of the server's rules that could drift.
SESSION_FIELDS = {
    "fingerprint": "fingerprint",
    "identity": "identity",
    "platform": "platform",
    "brand": "brand",
    "timezone": "timezone",
    "locale": "locale",
    "accept_language": "locale",  # the local launch's name for the same thing
    "geoip": "geoip",
    "headless": "headless",
    "light_stealth": "lightStealth",
    "proxy": "proxy",
    "country": "country",
    "state": "state",
    "city": "city",
    "proxy_session": "proxySession",
    "timeout_sec": "timeoutSec",
    "idle_timeout_sec": "idleTimeoutSec",
    "max_gb": "maxGb",
    "version": "version",
    "profile": "profile",
    "url": "url",
    "adblock": "adblock",
    "solve_sliders": "solveSliders",
    "solve_checkboxes": "solveCheckboxes",
    "keep_alive": "keepAlive",
    "record": "record",
    "note": "note",
    "worker": "worker",
}

# Run-only fields of POST /api/v1/runs (task, url, schema and secrets are named parameters).
RUN_FIELDS = {
    "max_steps": "maxSteps",
    "handoff": "handoff",
    "handoff_timeout_sec": "handoffTimeoutSec",
}

# Options a cloud launch() handles on THIS side and never sends: input humanization runs in the SDK
# exactly as it does for a local browser, timeout/slow_mo are Playwright's connect_over_cdp options
# (in its units, milliseconds), and api_key/api_url pick the account and server.
_CONNECT_OPTIONS = ("timeout", "slow_mo")
SDK_SIDE_OPTIONS = ("humanize", "show_cursor", "quiet", "api_key", "api_url") + _CONNECT_OPTIONS
# How long a cloud launch waits for the connect when the caller gives no timeout (Playwright's own
# default is 30 s). The browser starts as the client connects, and a launch with a country can take
# over 30 s; a launch that fails is answered at once, so the longer wait only covers one in progress.
_CONNECT_TIMEOUT_MS = 120_000


def _connect_options(sdk):
    """connect_over_cdp's keyword arguments: the caller's timeout and slow_mo (a timeout of 0, no
    limit, included), and the cloud default timeout when the caller gave none."""
    connect = {"timeout": _CONNECT_TIMEOUT_MS}
    connect.update({k: sdk[k] for k in _CONNECT_OPTIONS if sdk.get(k) is not None})
    return connect


def _local_only_options():
    """Everything launch() accepts that only makes sense for a browser on this machine.

    Derived from the local launch surface (the persona switches, the agent switches, the binary,
    licence, profile-directory and Playwright launch options) so an option added there is
    classified here too; tests/test_cloud.py checks the two lists still cover each other.
    """
    from ._agent import AGENT_KEYS
    from ._fingerprint import FINGERPRINT_KEYS

    persona = tuple(k for k in FINGERPRINT_KEYS if k not in SESSION_FIELDS)
    launch_side = (
        "executable_path", "args", "ignore_default_args", "user_data_dir", "ephemeral_profile",
        "extensions", "portable_profile", "encryption_key", "disable_privacy_sandbox", "socks5_udp",
        "shader_dialect", "widevine", "profile_select", "cache_dir", "auto_update", "release_channel",
        "allow_third_party_cookies", "transparent_proxy", "license_key", "license_api_base",
        "license_through_proxy", "env", "devtools", "downloads_path", "traces_dir",
        "chromium_sandbox", "channel", "handle_sigint", "handle_sigterm", "handle_sighup",
        "firefox_user_prefs", "artifacts_dir",
    )
    return persona + tuple(AGENT_KEYS) + launch_side


LOCAL_ONLY_OPTIONS = _local_only_options()

_USER_DATA_DIR_MSG = (
    "user_data_dir is not available for cloud browsers: a cloud browser keeps its cookies in a cloud "
    "profile, so pass profile=\"name\" instead (Cloud().profiles.sync can fill one from a local "
    "profile directory)")


def _not_available(name, run):
    if name == "user_data_dir":
        return ValueError(_USER_DATA_DIR_MSG)
    return ValueError(f"{name} is not available for cloud {'runs' if run else 'browsers'}")


def _cloud_proxy(value):
    """``"managed"`` (the residential pool), your own proxy as a URL with the credentials inline, or
    a ``{server, username, password}`` dict. The API wants the credentials as separate fields, so a
    URL's ``user:pass@`` is split out (it rejects credentials inside ``server``)."""
    from ._net import to_proxy_spec

    if isinstance(value, str) and value.strip() in ("managed", "direct"):
        return value.strip()
    if isinstance(value, dict):
        extra = sorted(k for k, v in value.items() if v is not None and k not in ("server", "username", "password"))
        if extra:
            raise ValueError(f"proxy.{extra[0]} is not available for cloud browsers")
    elif not isinstance(value, str):
        # ValueError, not TypeError: every bad cloud option raises the same exception class.
        raise ValueError('proxy must be "managed", a proxy URL, or {"server", "username", "password"}')  # noqa: TRY004
    spec = to_proxy_spec(value)
    if not spec:
        raise ValueError('proxy must be "managed", a proxy URL, or {"server", "username", "password"}')
    return spec


def _cloud_profile(value):
    """A cloud profile is a NAMED COOKIE STORE on the server: ``"name"`` loads it, and
    ``{"name": ..., "persist": True}`` also saves it back when the session ends. The local launch's
    ``profile`` (a saved persona, or ``"auto"``) means something else, so those are refused rather
    than silently creating a cloud profile with that name."""
    name = value.get("name") if isinstance(value, dict) else value
    if isinstance(value, dict):
        extra = sorted(k for k in value if k not in ("name", "persist"))
        if extra:
            raise ValueError(f"profile.{extra[0]} is not a cloud profile field (use name and persist)")
    elif not isinstance(value, str):
        raise ValueError("a saved local Profile cannot be used for a cloud browser; pass the name of a "  # noqa: TRY004
                         "cloud profile (see Cloud().profiles)")
    if name == "auto":
        raise ValueError('profile="auto" picks a local persona; for a stable cloud device pass identity=..., '
                         "and for cookies a cloud profile name")
    return dict(value) if isinstance(value, dict) else value


def session_body(options, run=False):
    """Map launch()/create() keyword options to the API's JSON body.

    Raises ValueError naming the first option a cloud browser (or run) cannot take. ``None`` values
    are left out, so an optional setting can be passed through unconditionally.
    """
    fields = dict(SESSION_FIELDS, **RUN_FIELDS) if run else SESSION_FIELDS
    if options.get("locale") is not None and options.get("accept_language") is not None:
        raise ValueError("pass locale or accept_language, not both")
    body = {}
    for key, value in options.items():
        if key not in fields:
            raise _not_available(key, run)
        if value is None:
            continue
        if key == "proxy":
            value = _cloud_proxy(value)
        elif key == "profile":
            value = _cloud_profile(value)
        body[fields[key]] = value
    return body


def run_body(task, url=None, schema=None, secrets=None, options=None):
    """The POST /api/v1/runs body: the task, where it starts, what to return, and the browser."""
    body = {"task": task}
    if url is not None:
        body["url"] = url
    if schema is not None:
        body["schema"] = schema
    if secrets is not None:
        body["secrets"] = secrets
    body.update(session_body(options or {}, run=True))
    return body


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand 3xx answers back instead of following them. The recording endpoint answers 302 to a
    presigned storage URL, and following it here would send the API key to that storage host."""

    def redirect_request(self, *_a, **_k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _user_agent():
    from ._license import _user_agent as ua
    return ua()


def _api_error(status, payload, reason=""):
    try:
        data = json.loads(payload.decode("utf-8")) if payload else None
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), dict):  # {error: {message, code}}
        data = dict(data["error"], code=data["error"].get("code", data.get("code")))
    if isinstance(data, dict) and (data.get("error") or data.get("message")):
        code = data.get("code")
        return CloudError(status, code if isinstance(code, str) else None,
                          str(data.get("error") or data.get("message")))
    text = payload.decode("utf-8", "replace").strip() if payload else ""
    return CloudError(status, None, text[:300] if text else f"HTTP {status} {reason}".strip())


def _parse_json(status, payload):
    if not payload or not payload.strip():
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except ValueError:
        raise CloudError(status, None, "the API answered with something that is not JSON") from None


def _q(segment):
    """One URL path segment (ids and profile names are user input)."""
    return urllib.parse.quote(str(segment), safe="")


def _compact(d):
    return {k: v for k, v in d.items() if v is not None}


class _Http:
    def __init__(self, api_key, base_url, timeout):
        self._key = api_key
        self.base_url = base_url
        self.timeout = timeout

    def url(self, path, query=None):
        q = [(k, ("1" if v is True else "0" if v is False else v)) for k, v in (query or {}).items() if v is not None]
        return self.base_url + path + ("?" + urllib.parse.urlencode(q) if q else "")

    def raw(self, method, path, body=None, query=None):
        """(status, lower-cased headers, body bytes). 3xx comes back as is; 4xx/5xx raise."""
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._key}", "Accept": "application/json",
                   "User-Agent": _user_agent()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url(path, query), data=data, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as e:
            try:
                payload = e.read() if e.fp is not None else b""
            except (OSError, http.client.HTTPException):  # cut while reading the error body
                payload = b""
            hdrs = {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
            if 300 <= e.code < 400:
                return e.code, hdrs, payload
            raise _api_error(e.code, payload, e.reason) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            # refused, DNS, TLS, socket timeout; HTTPException: a connection cut mid-answer
            # (IncompleteRead, BadStatusLine), which a long wait retries like any network failure
            reason = getattr(e, "reason", None) or e
            raise CloudError(0, "NETWORK", f"could not reach {self.base_url}: {reason!s}"[:500]) from None

    def call(self, method, path, body=None, query=None):
        status, headers, payload = self.raw(method, path, body, query)
        if 300 <= status < 400:
            raise CloudError(status, None, f"unexpected redirect to {headers.get('location')}")
        return _parse_json(status, payload)


# ── polling ──────────────────────────────────────────────────────────────────────────────────────

def _announce_handoff(view):
    """Default report when a run pauses for a person: without it a waiting run just looks stuck."""
    h = view.get("handoff") or {}
    sys.stderr.write(
        f"[clearcote] run {view.get('id')} is waiting for a human"
        f"{(' (' + h['reason'] + ')') if h.get('reason') else ''}: "
        f"{h.get('liveUrl') or 'open it in the Clearcote dashboard'}\n")
    sys.stderr.flush()


class _Watch:
    """The decisions a wait makes on every poll, shared by the sync and async clients: has it
    finished, should the caller hear about it, is an error worth another try, how long to sleep."""

    def __init__(self, what, timeout, done, key):
        self.what = what
        self.timeout = timeout
        self.deadline = None if timeout is None else time.monotonic() + timeout
        self._done = done
        self._key = key
        self._last_key = object()
        self.failures = 0
        self.last = None

    def seen(self, view):
        """Record one view: (finished, changed since the last view)."""
        self.failures = 0
        self.last = view
        key = self._key(view)
        changed = key != self._last_key
        self._last_key = key
        return self._done(view), changed

    def retry(self, err):
        """True to poll again after ``err``; False means re-raise it."""
        if isinstance(err, CloudError) and err.status in _TRANSIENT and self.failures < _MAX_TRANSIENT:
            self.failures += 1
            return True
        return False

    def pause(self, poll):
        """Seconds to sleep before the next poll; raises CloudTimeoutError once out of time."""
        if self.deadline is None:
            return poll
        left = self.deadline - time.monotonic()
        if left <= 0:
            status = (self.last or {}).get("status")
            raise CloudTimeoutError(
                f"{self.what} is still {status or 'not finished'} after {self.timeout:g}s; "
                "it carries on on the server", last=self.last)
        return min(poll, left)


def _run_watch(run_id, timeout):
    def key(v):
        h = v.get("handoff") or {}
        return v.get("status"), h.get("state"), h.get("since")
    return _Watch(f"run {run_id}", timeout, lambda v: v.get("status") in TERMINAL_RUN_STATUSES, key)


def _handoff_watch(session_id, timeout):
    def waiting(v):
        return (v.get("handoff") or {}).get("state") == "waiting"
    return _Watch(f"the hand-off of {session_id}", timeout, lambda v: not waiting(v),
                  lambda v: (v.get("handoff") or {}).get("state"))


def _require_handoff(session_id, view, first):
    """A session with no hand-off at all reads as "not waiting", so wait_handoff would return at once
    as if a person had finished. On the first read that is a mistake (no handoff() was requested):
    say so instead. A done or timed-out hand-off still returns."""
    if first and not (view or {}).get("handoff"):
        raise CloudError(200, "NO_HANDOFF", f"no hand-off was requested for session {session_id} "
                                            "(request one with browsers.handoff first)")


def _run_notifier(on_update):
    if on_update is not None:
        return on_update

    def default(view):
        if view.get("status") == "waiting_for_human":
            _announce_handoff(view)
    return default


# ── resources ────────────────────────────────────────────────────────────────────────────────────

class Browsers:
    """Hosted browser sessions: ``/api/v1/browsers``."""

    def __init__(self, http):
        self._http = http

    def create(self, **options):
        """Start a session; returns ``{id, connectUrl, expiresAt, ...}``. Takes the cloud launch
        options (``country``, ``identity``, ``profile``, ``record``, ...). To get a connected
        Playwright browser in one step use ``clearcote.launch(cloud=True, ...)`` instead."""
        return self._create(session_body(options))

    def _create(self, body):
        return self._http.call("POST", "/api/v1/browsers", body)

    def get(self, session_id):
        return self._http.call("GET", f"/api/v1/browsers/{_q(session_id)}")

    def list(self, status=None, note=None, limit=None, before=None):
        """``{balanceEur, sessions: [...]}``, newest first. ``status`` may be a list."""
        if isinstance(status, (list, tuple)):
            status = ",".join(status)
        return self._http.call("GET", "/api/v1/browsers",
                               query={"status": status, "note": note, "limit": limit, "before": before})

    def stop(self, session_id):
        return self._http.call("DELETE", f"/api/v1/browsers/{_q(session_id)}")

    def live(self, session_id, control=False):
        """A live-view WebSocket for a running session (``control=True`` may also drive it)."""
        return self._http.call("GET", f"/api/v1/browsers/{_q(session_id)}/live",
                               query={"control": "1" if control else None})

    def share(self, session_id, control=None, minutes=None, recording=None):
        """A link anyone can open: the live view (``control=True`` to let them drive) or, with
        ``recording=True``, the session's recording."""
        return self._http.call("POST", f"/api/v1/browsers/{_q(session_id)}/share",
                               _compact({"control": control, "minutes": minutes, "recording": recording}))

    def handoff(self, session_id, reason=None, timeout_sec=None):
        """Hand a running session to a person: ``{state: "waiting", liveUrl, expiresAt, ...}``."""
        return self._http.call("POST", f"/api/v1/browsers/{_q(session_id)}/handoff",
                               _compact({"reason": reason, "timeoutSec": timeout_sec}))

    def handoff_done(self, session_id):
        """Mark a waiting hand-off done (what the live page's "I'm done" button does)."""
        return self._http.call("POST", f"/api/v1/browsers/{_q(session_id)}/handoff/done", {})

    def wait_handoff(self, session_id, timeout=None, poll=2.0):
        """Poll until the session's hand-off is no longer waiting (done, or timed out on the
        server); returns the session view. ``timeout`` (seconds) raises CloudTimeoutError. A session
        with no hand-off at all raises CloudError ``NO_HANDOFF`` rather than returning at once."""
        watch = _handoff_watch(session_id, timeout)
        while True:
            try:
                view = self.get(session_id)
                _require_handoff(session_id, view, watch.last is None)
                done, _changed = watch.seen(view)
            except CloudError as e:
                if not watch.retry(e):
                    raise
                time.sleep(watch.pause(poll))
                continue
            if done:
                return watch.last
            time.sleep(watch.pause(poll))

    def events(self, session_id, after=0, limit=None):
        """One page of the session's event timeline: ``{events: [{seq, at, type, data}], next}``.
        Pass ``next`` back as ``after`` for the following page; it is None at the end."""
        return self._http.call("GET", f"/api/v1/browsers/{_q(session_id)}/events",
                               query={"after": after, "limit": limit})

    def recording_url(self, session_id):
        """A short-lived URL of the session's MP4. Raises CloudError 409 ``NOT_READY`` while it is
        still being processed, and 404 when the session was not recorded."""
        status, headers, payload = self._http.raw("GET", f"/api/v1/browsers/{_q(session_id)}/recording")
        url = None
        if 300 <= status < 400 and headers.get("location"):
            url = urllib.parse.urljoin(self._http.base_url + "/", headers["location"])
        else:
            data = _parse_json(status, payload)
            if isinstance(data, dict) and data.get("url"):
                url = str(data["url"])
        # only a web URL: urllib would also open file:// (and download_recording would copy a local file)
        if not url or urllib.parse.urlsplit(url).scheme.lower() not in ("https", "http"):
            raise CloudError(status, None, "the API did not answer with a recording URL")
        return url

    def download_recording(self, session_id, path):
        """Save the session's recording to ``path``; returns ``path``. The storage URL is presigned,
        so the API key is NOT sent to it."""
        url = self.recording_url(session_id)
        part = f"{path}.part"
        req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
        try:
            with urllib.request.urlopen(req, timeout=self._http.timeout) as resp, open(part, "wb") as fh:
                shutil.copyfileobj(resp, fh, 1 << 20)
            os.replace(part, path)
        except urllib.error.HTTPError as e:
            _remove(part)
            raise CloudError(e.code, None, f"downloading the recording failed: HTTP {e.code}") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            _remove(part)
            raise CloudError(0, "NETWORK", f"downloading the recording failed: {getattr(e, 'reason', e)}") from None
        return path


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Runs:
    """Agent runs: a task in, JSON out. ``/api/v1/runs``."""

    def __init__(self, http):
        self._http = http

    def create(self, task, url=None, schema=None, secrets=None, wait=True, timeout=None, poll=1.5,
               on_update=None, **options):
        """Start a run. With ``wait=True`` (the default) poll until it finishes and return the run
        (``status``, ``result`` with ``output``, ``costEur``, ...); otherwise return the create
        answer at once.

        ``options`` are the browser options of a cloud launch (``country``, ``identity``,
        ``profile``, ``record``, ...) plus ``max_steps``, ``handoff`` and ``handoff_timeout_sec``.
        ``on_update(run)`` is called whenever the status or the hand-off changes; without it a run
        that pauses for a person is reported on stderr with its live link. ``timeout`` (seconds)
        raises CloudTimeoutError, and the run carries on on the server.
        """
        created = self._http.call("POST", "/api/v1/runs", run_body(task, url, schema, secrets, options))
        if not wait:
            return created
        return self.wait(created["id"], timeout=timeout, poll=poll, on_update=on_update)

    def get(self, run_id):
        return self._http.call("GET", f"/api/v1/runs/{_q(run_id)}")

    def list(self, limit=None, before=None):
        """``{runs: [...]}``, newest first."""
        return self._http.call("GET", "/api/v1/runs", query={"limit": limit, "before": before})

    def cancel(self, run_id):
        return self._http.call("DELETE", f"/api/v1/runs/{_q(run_id)}")

    def wait(self, run_id, timeout=None, poll=1.5, on_update=None):
        """Poll GET /api/v1/runs/{id} until the run is succeeded, failed, cancelled or expired."""
        watch = _run_watch(run_id, timeout)
        notify = _run_notifier(on_update)
        while True:
            try:
                view = self.get(run_id)
            except CloudError as e:
                if not watch.retry(e):
                    raise
                time.sleep(watch.pause(poll))
                continue
            done, changed = watch.seen(view)
            if changed:
                notify(view)
            if done:
                return view
            time.sleep(watch.pause(poll))


class Profiles:
    """Cloud profiles (named cookie stores): ``/api/v1/browsers/profiles``."""

    def __init__(self, http):
        self._http = http

    def list(self):
        return self._http.call("GET", "/api/v1/browsers/profiles")

    def get(self, name):
        """``{name, cookies, domains, bytes, storage, updatedAt}``; never the cookie values."""
        return self._http.call("GET", f"/api/v1/browsers/profiles/{_q(name)}")

    def delete(self, name):
        return self._http.call("DELETE", f"/api/v1/browsers/profiles/{_q(name)}")

    def import_cookies(self, name, cookies, mode="merge"):
        """Upload cookies (CDP or Playwright shape) into a profile, creating it if needed.
        ``mode="replace"`` drops what the profile had first. 409 ``PROFILE_IN_USE`` while a live
        session saves to it."""
        return self._http.call("PUT", f"/api/v1/browsers/profiles/{_q(name)}/cookies",
                               {"cookies": list(cookies), "mode": mode})

    def sync(self, name, from_profile=None, from_cdp=None, from_file=None, login_url=None,
             domains=None, all_domains=False, replace=False, confirm=None):
        """Copy a logged-in state into a cloud profile. Exactly one source:

        * ``from_profile=DIR``: a local Chrome/Clearcote profile directory, read by a headless
          Clearcote started on it;
        * ``from_cdp=URL``: a browser you already run with remote debugging (``http://127.0.0.1:9222``);
        * ``from_file=PATH``: a Playwright storage-state file, or a JSON array of cookies;
        * ``login_url=URL``: a visible Clearcote on a throwaway profile opens the page, you sign in,
          and ``confirm()`` returns (default: press Enter in this terminal).

        Only the cookies a browser would use on ``domains`` are uploaded: those of each domain, of
        its subdomains, and of its parent domains (``.example.com`` for ``www.example.com``).
        Uploading every cookie needs ``all_domains=True``: with neither, nothing is read and
        ValueError is raised.
        """
        sources = [k for k, v in (("from_profile", from_profile), ("from_cdp", from_cdp),
                                  ("from_file", from_file), ("login_url", login_url)) if v]
        if len(sources) != 1:
            raise ValueError("pass exactly one of from_profile, from_cdp, from_file or login_url")
        if isinstance(domains, str):
            domains = [domains]
        domains = [d for d in (domains or []) if d and str(d).strip()]
        if domains and all_domains:
            raise ValueError("pass domains or all_domains, not both")
        if not domains and not all_domains:
            raise ValueError(NEED_DOMAINS)
        if from_file:
            cookies = read_cookies_from_file(from_file)
        elif from_cdp:
            cookies = read_cookies_from_cdp(from_cdp)
        elif from_profile:
            cookies = read_cookies_from_profile(from_profile)
        else:
            cookies = read_cookies_by_login(login_url, confirm=confirm)
        picked = cookies if all_domains else filter_cookies(cookies, domains)
        if not picked:
            where = "anywhere" if all_domains else "for " + ", ".join(domains)
            raise ValueError(f"no cookies found {where}; nothing was uploaded")
        return self.import_cookies(name, [normalize_cookie(c) for c in picked],
                                   mode="replace" if replace else "merge")


NEED_DOMAINS = ("choose the cookies to upload: pass domains=[...] (each also covers its subdomains and "
                "the parent-domain cookies a browser sends it), or all_domains=True to upload every cookie")


class Webhooks:
    """Signed event deliveries to your HTTPS endpoint: ``/api/v1/webhooks``."""

    def __init__(self, http):
        self._http = http

    def create(self, url, events=None, description=None):
        """Register an endpoint; the answer carries ``secret`` (``whsec_...``), shown only here."""
        return self._http.call("POST", "/api/v1/webhooks", _compact({
            "url": url, "events": list(events) if events is not None else None, "description": description}))

    def list(self):
        return self._http.call("GET", "/api/v1/webhooks")

    def delete(self, webhook_id):
        return self._http.call("DELETE", f"/api/v1/webhooks/{_q(webhook_id)}")

    def test(self, webhook_id):
        """Send a ``ping`` event to the endpoint."""
        return self._http.call("POST", f"/api/v1/webhooks/{_q(webhook_id)}/test", {})


class Cloud:
    """The hosted API, one client: ``browsers``, ``runs``, ``profiles`` and ``webhooks``.

    ``api_key`` defaults to ``CLEARCOTE_API_KEY`` and ``base_url`` to ``CLEARCOTE_API_URL``, then
    ``https://www.clearcotelabs.com``. ``timeout`` is per HTTP request, in seconds.
    """

    def __init__(self, api_key=None, base_url=None, timeout=30.0):
        key = str(api_key or os.environ.get("CLEARCOTE_API_KEY") or "").strip()
        if not key:
            raise ValueError(NO_API_KEY)
        base = str(base_url or os.environ.get("CLEARCOTE_API_URL") or DEFAULT_API_URL).strip().rstrip("/")
        self.base_url = _check_base_url(base)
        self._http = _Http(key, base, timeout)
        self.browsers = Browsers(self._http)
        self.runs = Runs(self._http)
        self.profiles = Profiles(self._http)
        self.webhooks = Webhooks(self._http)

    def __repr__(self):  # never the key
        return f"Cloud(base_url={self.base_url!r})"


# ── async ────────────────────────────────────────────────────────────────────────────────────────

class _AsyncResource:
    """Every method of a sync resource, run off the event loop (urllib blocks)."""

    def __init__(self, sync):
        self._sync = sync

    def __getattr__(self, name):
        fn = getattr(self._sync, name)
        if name.startswith("_") or not callable(fn):
            return fn

        async def call(*args, **kwargs):
            return await asyncio.to_thread(fn, *args, **kwargs)
        call.__name__ = name
        call.__doc__ = fn.__doc__
        return call


async def _maybe_await(value):
    if inspect.isawaitable(value):
        await value


class _AsyncBrowsers(_AsyncResource):
    async def wait_handoff(self, session_id, timeout=None, poll=2.0):
        watch = _handoff_watch(session_id, timeout)
        while True:
            try:
                view = await asyncio.to_thread(self._sync.get, session_id)
                _require_handoff(session_id, view, watch.last is None)
                done, _changed = watch.seen(view)
            except CloudError as e:
                if not watch.retry(e):
                    raise
                await asyncio.sleep(watch.pause(poll))
                continue
            if done:
                return watch.last
            await asyncio.sleep(watch.pause(poll))


class _AsyncRuns(_AsyncResource):
    async def create(self, task, url=None, schema=None, secrets=None, wait=True, timeout=None, poll=1.5,
                     on_update=None, **options):
        created = await asyncio.to_thread(self._sync.create, task, url, schema, secrets, False, **options)
        if not wait:
            return created
        return await self.wait(created["id"], timeout=timeout, poll=poll, on_update=on_update)

    async def wait(self, run_id, timeout=None, poll=1.5, on_update=None):
        """``on_update`` may be a plain function or a coroutine function."""
        watch = _run_watch(run_id, timeout)
        notify = _run_notifier(on_update)
        while True:
            try:
                view = await asyncio.to_thread(self._sync.get, run_id)
            except CloudError as e:
                if not watch.retry(e):
                    raise
                await asyncio.sleep(watch.pause(poll))
                continue
            done, changed = watch.seen(view)
            if changed:
                await _maybe_await(notify(view))
            if done:
                return view
            await asyncio.sleep(watch.pause(poll))


class AsyncCloud:
    """:class:`Cloud` for asyncio code: the same resources and methods, each one awaitable.

    Blocking work (HTTP, reading cookies from a local browser) runs in a worker thread; waits poll
    with ``asyncio.sleep`` so a long run never ties up a thread.
    """

    def __init__(self, api_key=None, base_url=None, timeout=30.0):
        self._sync = Cloud(api_key=api_key, base_url=base_url, timeout=timeout)
        self.base_url = self._sync.base_url
        self.browsers = _AsyncBrowsers(self._sync.browsers)
        self.runs = _AsyncRuns(self._sync.runs)
        self.profiles = _AsyncResource(self._sync.profiles)
        self.webhooks = _AsyncResource(self._sync.webhooks)

    def __repr__(self):
        return f"AsyncCloud(base_url={self.base_url!r})"


# ── webhooks: signature check ────────────────────────────────────────────────────────────────────

# A real header is ~80 bytes (one t, one or two v1). Anything near this is not a Clearcote signature.
_MAX_SIGNATURE_HEADER = 4096


def verify_webhook(raw_body, signature_header, secret, tolerance_sec=300, now=None):
    """Check a webhook delivery and return its parsed JSON event.

    ``raw_body`` must be the request body EXACTLY as received (bytes or str) — re-serialised JSON
    does not match the signature. ``signature_header`` is the ``Clearcote-Signature`` header,
    ``t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>``; more than one ``v1`` may be
    present (while a secret rotates) and any one matching is enough. Comparisons are constant-time.
    A timestamp more than ``tolerance_sec`` away from now is refused, so a captured delivery cannot
    be replayed later (``tolerance_sec=None`` turns that off). Raises ValueError on any failure.
    """
    if secret is None or not (secret.strip() if isinstance(secret, str) else secret):
        # str(None) is "None": an unset secret (os.environ.get(...) -> None) would otherwise verify
        # anything signed with the key "None", which anyone can compute.
        raise ValueError("verify_webhook needs the signing secret of the endpoint (whsec_...), got none")
    body = raw_body.encode("utf-8") if isinstance(raw_body, str) else bytes(raw_body)
    header = str(signature_header or "")
    if len(header) > _MAX_SIGNATURE_HEADER:
        raise ValueError("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)")
    stamp, signatures = None, []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key.strip() == "t":
            stamp = value.strip()
        elif key.strip() == "v1" and value.strip():
            signatures.append(value.strip().lower())
    if not stamp or not (stamp.isascii() and stamp.isdigit()) or not signatures:
        raise ValueError("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)")
    key = secret if isinstance(secret, (bytes, bytearray)) else str(secret).encode("utf-8")
    expected = hmac.new(bytes(key), stamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest().encode("ascii")
    if not any(hmac.compare_digest(expected, s.encode("utf-8")) for s in signatures):
        raise ValueError("webhook signature does not match")
    if tolerance_sec is not None:
        current = time.time() if now is None else now
        if abs(current - int(stamp)) > tolerance_sec:
            raise ValueError("webhook timestamp is outside the tolerance window (a replay, or a clock that is off)")
    return json.loads(body.decode("utf-8"))


# ── cookies: profile sync ────────────────────────────────────────────────────────────────────────

# What PUT .../cookies keeps of a cookie (CookieParam). The CDP shape carries more (size, priority,
# sourceScheme, ...); the API drops those anyway, so they are not uploaded at all.
_COOKIE_FIELDS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def normalize_cookie(cookie):
    return {k: cookie[k] for k in _COOKIE_FIELDS if k in cookie and cookie[k] is not None}


def _bare_domain(d):
    return str(d or "").strip().lower().lstrip(".")


def _domain_matches(cookie_domain, allowed):
    """``cookie_domain`` is ``allowed``, a subdomain of it, or a PARENT domain of it (a ``.example.com``
    cookie is sent to ``www.example.com`` too, so signing in there needs it). A parent must still have a
    dot of its own: a cookie on a bare suffix (``com``) never matches."""
    if cookie_domain == allowed or cookie_domain.endswith("." + allowed):
        return True
    return "." in cookie_domain and allowed.endswith("." + cookie_domain)


def filter_cookies(cookies, domains):
    """The cookies a browser would use on ``domains``: those whose domain is one of them, a subdomain
    of one, or a parent domain of one (``.example.com`` for ``www.example.com``; never a bare suffix
    such as ``com``). A leading dot on either side is ignored, and ``example.com`` does not match
    ``badexample.com``."""
    allowed = [a for a in (_bare_domain(d) for d in domains) if a]
    out = []
    for c in cookies:
        d = _bare_domain(c.get("domain"))
        if d and any(_domain_matches(d, a) for a in allowed):
            out.append(c)
    return out


def cookies_from_state(data):
    """The cookies in a Playwright storage state (``{"cookies": [...], "origins": [...]}``), a CDP
    ``{"cookies": [...]}`` answer, or a plain JSON array of cookies."""
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list):  # a file's content: ValueError like any other bad input
        raise ValueError(  # noqa: TRY004
            "expected a Playwright storage state ({\"cookies\": [...]}) or a JSON array of cookies")
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            raise ValueError("every cookie needs at least a name and a domain")
    return cookies


def read_cookies_from_file(path):
    with open(path, encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except ValueError as e:
            raise ValueError(f"{path} is not JSON: {e}") from None
    return cookies_from_state(data)


def _browser_ws_url(endpoint):
    """The browser-level WebSocket of a CDP endpoint given as ws(s):// or http(s):// (resolved via
    /json/version, as connect_over_cdp does)."""
    url = str(endpoint).strip()
    if url.startswith(("ws://", "wss://")):
        return url
    if not url.startswith(("http://", "https://")):
        raise ValueError("from_cdp must be an http(s):// or ws(s):// CDP endpoint, e.g. http://127.0.0.1:9222")
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/json/version", timeout=10) as r:
            ws = json.load(r).get("webSocketDebuggerUrl")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ValueError(f"no CDP endpoint answered at {url}: {getattr(e, 'reason', e)}") from None
    if not ws:
        raise ValueError(f"{url}/json/version did not name a webSocketDebuggerUrl")
    return ws


def _cookies_over_ws(ws_url):
    """Storage.getCookies on the browser target: every cookie of the default context."""
    from ._cdpws import CdpConnection

    conn = CdpConnection(ws_url, timeout=30.0)
    try:
        return list(conn.send("Storage.getCookies").get("cookies") or [])
    finally:
        conn.close()


def _cookies_via_playwright(endpoint):
    """For a wss:// endpoint (the stdlib CDP client speaks plain ws only): Playwright's
    connect_over_cdp, on its own thread so this works inside an asyncio loop too. Closing a CDP
    connection only disconnects; the browser keeps running."""
    out, err = [], []

    def work():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(endpoint)
                try:
                    out.extend(browser.new_browser_cdp_session().send("Storage.getCookies").get("cookies") or [])
                finally:
                    browser.close()
        except BaseException as e:  # noqa: BLE001 -- re-raised on the caller's thread
            err.append(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join()
    if err:
        raise err[0]
    return out


def read_cookies_from_cdp(endpoint):
    """Every cookie of a running browser's default context, over CDP."""
    ws = _browser_ws_url(endpoint)
    if ws.startswith("ws://"):
        return _cookies_over_ws(ws)
    return _cookies_via_playwright(endpoint)


def read_cookies_from_profile(user_data_dir):
    """Start a headless Clearcote on ``user_data_dir`` (a raw CDP endpoint via serve(): no
    Playwright, nothing written but what Chrome itself writes), read its cookies, stop it."""
    from . import serve

    if not os.path.isdir(user_data_dir):
        raise ValueError(f"{user_data_dir} is not a directory")
    srv = serve(user_data_dir=user_data_dir, headless=True, quiet=True)
    try:
        ws = srv.ws_url
        if not ws:
            raise RuntimeError("the local browser did not open its CDP endpoint")
        return _cookies_over_ws(ws)
    finally:
        srv.close()


LOGIN_PROMPT = "Sign in in the browser window that just opened, then press Enter here to upload the cookies."


def _wait_for_enter():
    if not sys.stdin or not sys.stdin.isatty():
        raise ValueError("login needs an interactive terminal to confirm the sign-in (pass confirm= to wait another way)")
    sys.stderr.write(LOGIN_PROMPT + " ")
    sys.stderr.flush()
    sys.stdin.readline()


def read_cookies_by_login(url, confirm=None):
    """Open ``url`` in a VISIBLE Clearcote on a throwaway profile, wait for ``confirm()`` (default:
    Enter in this terminal), then read the cookies. The profile directory is deleted afterwards."""
    from . import serve
    from ._cdpws import CdpConnection

    srv = serve(headless=False, quiet=True)
    try:
        ws = srv.ws_url
        if not ws:
            raise RuntimeError("the local browser did not open its CDP endpoint")
        conn = CdpConnection(ws, timeout=30.0)
        try:
            conn.send("Target.createTarget", {"url": url})
        finally:
            conn.close()
        (confirm or _wait_for_enter)()
        if not srv.is_alive():
            raise RuntimeError("the browser was closed before the cookies were read; nothing was uploaded")
        return _cookies_over_ws(srv.ws_url or ws)
    finally:
        srv.close()


# ── launch(cloud=True) ───────────────────────────────────────────────────────────────────────────

def _prepare_launch(kwargs, persistent=False, user_data_dir=None):
    """Split launch kwargs into the SDK-side options and the API body. Pure: raises ValueError for
    an option a cloud browser cannot take before anything is created."""
    opts = dict(kwargs)
    sdk = {k: opts.pop(k) for k in SDK_SIDE_OPTIONS if k in opts}
    if persistent:
        if user_data_dir is not None:
            raise ValueError(_USER_DATA_DIR_MSG)
        profile = opts.get("profile")
        if not profile:
            raise ValueError('launch_persistent_context(cloud=True) needs profile="name": the cloud profile '
                             "whose cookies it loads and saves back when it closes")
        opts["profile"] = ({"name": profile, "persist": True} if isinstance(profile, str)
                           else dict({"persist": True}, **profile) if isinstance(profile, dict) else profile)
    return sdk, session_body(opts)


def _client_for(cloud, sdk):
    if isinstance(cloud, Cloud):
        return cloud
    if isinstance(cloud, AsyncCloud):
        return cloud._sync
    return Cloud(api_key=sdk.get("api_key"), base_url=sdk.get("api_url"))


def _connect_url(created):
    url = (created or {}).get("connectUrl")
    if not url:
        raise CloudError(200, None, f"the API created session {(created or {}).get('id')} but sent no connectUrl")
    return url


def _session_info(created):
    """What the returned browser carries as ``cloud_session``: the create answer minus the
    single-use connect URL (it holds a token and is spent once connected)."""
    return {k: v for k, v in created.items() if k != "connectUrl"}


def _stop_quietly(client, created):
    try:
        if created and created.get("id"):
            client.browsers.stop(created["id"])
    except Exception:  # noqa: BLE001, S110 -- best-effort: the gateway ends it when the client goes anyway
        pass


def _motor_seed(body):
    """The humanizer's persona seed: the fingerprint, else the identity label, so one identity
    keeps one hand across sessions (a local launch seeds from the fingerprint the same way)."""
    return body.get("fingerprint", body.get("identity"))


def launch_cloud(cloud, kwargs, persistent=False, user_data_dir=None):
    """``clearcote.launch(cloud=...)``: create the session, connect over CDP, humanize as a local
    launch does, and return the Playwright Browser (or, ``persistent``, its profile context).

    Nothing is left behind when any step after the create fails: the connection is closed and the
    session is stopped (DELETE), so a failed launch never keeps a billed browser running."""
    from . import _install_headed_viewport, _playwright
    from ._humanize import install_humanize, install_humanize_on_context

    sdk, body = _prepare_launch(kwargs, persistent, user_data_dir)
    client = _client_for(cloud, sdk)
    created = client.browsers._create(body)
    connect = _connect_options(sdk)
    browser = disconnect = None
    try:
        browser = _playwright().chromium.connect_over_cdp(_connect_url(created), **connect)
        disconnect = browser.close
        browser.cloud_session = _session_info(created)
        keep_alive = body.get("keepAlive") is True

        def close(*args, **kw):
            # Disconnect first (the gateway ends a session whose client goes), then say so
            # explicitly. A keep-alive session is left running on purpose: stop it with
            # Cloud().browsers.stop(id).
            try:
                return disconnect(*args, **kw)
            finally:
                if not keep_alive:
                    _stop_quietly(client, created)

        browser.close = close
        # A new context would otherwise get Playwright's emulated 1280x720 viewport on top of the
        # real window: the impossible-window tell a local launch avoids the same way.
        _install_headed_viewport(browser)
        seed = _motor_seed(body)
        humanize, show_cursor = sdk.get("humanize", False), sdk.get("show_cursor", False)
        for ctx in browser.contexts:  # the session's default context, and any tab already open in it
            install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        install_humanize(browser, humanize, show_cursor, seed=seed)
        if not persistent:
            return browser
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        context.cloud_session = browser.cloud_session

        def close_context(*_a, **_kw):  # closing the profile context closes (and saves) the session
            return browser.close()

        context.close = close_context
        return context
    except BaseException:
        if disconnect is not None:
            try:
                disconnect()
            except Exception:  # noqa: BLE001, S110 -- the setup error is the one worth raising
                pass
        _stop_quietly(client, created)
        raise


async def launch_cloud_async(cloud, kwargs, persistent=False, user_data_dir=None):
    """Async twin of :func:`launch_cloud` (``clearcote.async_api.launch(cloud=...)``). The browser
    owns its Playwright driver, stopped on ``close()``; a failed launch stops the driver and the
    session."""
    from ._humanize_async import install_humanize, install_humanize_on_context
    from .async_api import _bind_driver, _install_headed_viewport, _start_driver

    sdk, body = _prepare_launch(kwargs, persistent, user_data_dir)
    client = _client_for(cloud, sdk)
    connect = _connect_options(sdk)
    # The driver first: when it cannot start (Playwright missing), no session has been created yet.
    pw = await _start_driver()
    created = disconnect = None
    try:
        created = await asyncio.to_thread(client.browsers._create, body)
        browser = await pw.chromium.connect_over_cdp(_connect_url(created), **connect)
        disconnect = browser.close
        _bind_driver(browser, pw)  # close() also stops this browser's own Playwright driver
        browser.cloud_session = _session_info(created)
        keep_alive = body.get("keepAlive") is True
        orig_close = browser.close

        async def close(*args, **kw):
            try:
                return await orig_close(*args, **kw)
            finally:
                if not keep_alive:
                    await asyncio.to_thread(_stop_quietly, client, created)

        browser.close = close
        _install_headed_viewport(browser)
        seed = _motor_seed(body)
        humanize, show_cursor = sdk.get("humanize", False), sdk.get("show_cursor", False)
        for ctx in browser.contexts:
            await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        await install_humanize(browser, humanize, show_cursor, seed=seed)
        if not persistent:
            return browser
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        context.cloud_session = browser.cloud_session

        async def close_context(*_a, **_kw):
            return await browser.close()

        context.close = close_context
        return context
    except BaseException:
        for step in (disconnect, pw.stop):
            try:
                if step is not None:
                    await step()
            except Exception:  # noqa: BLE001, S110 -- the launch error is the one worth raising
                pass
        if created is not None:
            await asyncio.to_thread(_stop_quietly, client, created)
        raise
