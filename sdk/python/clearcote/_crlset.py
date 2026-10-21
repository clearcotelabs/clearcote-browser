"""Chrome's revocation list (CRLSet) for an engine that never receives one.

Real Chrome downloads a CRLSet through its component updater: Google's list of revoked and blocked
certificate keys, which is how a CA distrust reaches the browser within hours (the eMudhra key compromise of
2026-10-17 shipped as CRLSet 10849). A Clearcote engine never gets it: the updater URL is
domain-substituted to a dead host, and Playwright launches with --disable-component-update anyway. So every
engine kept trusting those intermediates (tested on Chromium 149-155 engines, free and PRO).

The engine still knows how to use one. This module:

1. SEEDS a CRLSet into the profile (``<user_data_dir>/CertificateRevocation/<sequence>/crl-set`` plus a
   manifest). Chrome checks only that the file exists (CRLSetPolicy::VerifyInstallation), so a cached copy
   loads as is. An expired one still blocks everything it lists: expiry only turns off the "known good"
   shortcut (revocation_checker.cc).
2. UN-SUPPRESSES the component updater. Without it RegisterComponentsForUpdate never runs and the seeded
   file is never read. The updater makes no network request on this engine (NetLog, 90 s idle with
   fast-update: no update check, no DNS, nothing through the proxy).
3. WAITS until the CRLSet is live before the caller gets the browser. Chrome loads it with BEST_EFFORT tasks
   that only run once startup completes, and startup completes when the first tab has been idle for a
   moment (AfterStartupTaskUtils, 3 min failsafe). A caller who navigates straight away would keep the tab
   busy and stay unprotected for minutes. So the first tab stays on about:blank until the CRLSet applies.

Readiness signal: applying a CRLSet changes the cert verifier, and the network service then drops its
pooled TLS connections. The probe keeps one keep-alive HTTPS connection to a local server (self-signed key
in ``_crlset_probe.pem``; certificate errors are ignored, through CDP, only for the probe's lifetime) and
waits for that connection to be dropped and re-opened. Measured 6/6 on a 155 engine: the re-opened
connection's verification carries the seeded sequence, and an eMudhra host navigated right after is
ERR_CERT_REVOKED. It adds a few seconds to a launch.

OPT-IN: ``crlset=True`` (or CLEARCOTE_CRLSET=1). Where the CRLSet comes from, first match wins:
``crlset="<path>"`` (a crl-set file or a component directory), CLEARCOTE_CRLSET_FILE, then the newest
copy in the cache (CLEARCOTE_CRLSET_DIR, else ~/.clearcote/crlset). An empty cache is filled at launch
from Google's component updater, the same Omaha service the Widevine fetch uses (sha256 from the HTTPS
update response); a cache older than 6 h is refreshed in the background while the launch uses it.
CLEARCOTE_CRLSET_URL points the fetch at a mirror instead. The cache layout (``<sequence>/crl-set``) is
shared by the Python, Node and .NET SDKs.
"""
import json
import os
import shutil
import ssl
import struct
import sys
import threading
import time
import urllib.request

COMPONENT_DIR = "CertificateRevocation"
CRLSET_FILE = "crl-set"
CRLSET_APP_ID = "hfnkpimlhhgieaddgfemjhofmfblmnib"  # Chrome's CRLSet component
DEFAULT_TIMEOUT_SECONDS = 15.0  # the readiness wait (CLEARCOTE_CRLSET_TIMEOUT, crlset_timeout=)
FETCH_TIMEOUT_SECONDS = 15.0  # how long a launch waits for a first download (CLEARCOTE_CRLSET_FETCH_TIMEOUT)
REFRESH_SECONDS = 6 * 3600  # a cache older than this is refreshed in the background
STAMP_FILE = ".last-check"  # in the cache root: mtime = the last fetch attempt
PROBE_PEM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_crlset_probe.pem")

_WARNED = set()
_REFRESH = {"thread": None}


def _warn_once(key, message, quiet):
    if quiet or os.environ.get("CLEARCOTE_NO_WARN") or key in _WARNED:
        return
    _WARNED.add(key)
    print("clearcote: crlset: " + message, file=sys.stderr)


def cache_root():
    return os.environ.get("CLEARCOTE_CRLSET_DIR",
                          os.path.join(os.path.expanduser("~"), ".clearcote", "crlset"))


def parse_header(data):
    """The JSON header of a CRLSet file (little-endian uint16 length, then JSON). Raises ValueError."""
    if len(data) < 2:
        raise ValueError("not a CRLSet: too short")
    length = struct.unpack_from("<H", data, 0)[0]
    try:
        header = json.loads(data[2:2 + length])
    except ValueError as exc:
        raise ValueError("not a CRLSet: bad header (%s)" % exc)
    if not isinstance(header, dict) or header.get("ContentType") != "CRLSet":
        raise ValueError("not a CRLSet: ContentType is not CRLSet")
    if not isinstance(header.get("Sequence"), int):
        raise ValueError("not a CRLSet: no Sequence")
    return header


def _as_crlset_file(path):
    """A crl-set file path from a file or a component directory (``<dir>/crl-set`` or ``<dir>/<ver>/crl-set``)."""
    if os.path.isfile(path):
        return path
    direct = os.path.join(path, CRLSET_FILE)
    if os.path.isfile(direct):
        return direct
    versions = []
    for name in os.listdir(path) if os.path.isdir(path) else ():
        candidate = os.path.join(path, name, CRLSET_FILE)
        if os.path.isfile(candidate):
            try:
                versions.append((parse_header(_read(candidate))["Sequence"], candidate))
            except (OSError, ValueError):
                continue
    if not versions:
        raise ValueError("no crl-set file under %s" % path)
    return max(versions)[1]


def _read(path):
    with open(path, "rb") as f:
        return f.read()


def install_crlset(source):
    """Copy a CRLSet (a crl-set file, or Chrome's ``CertificateRevocation`` directory) into the cache as
    ``<cache>/<sequence>/crl-set``. Returns that path."""
    src = _as_crlset_file(source)
    data = _read(src)
    sequence = parse_header(data)["Sequence"]
    dest_dir = os.path.join(cache_root(), str(sequence))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, CRLSET_FILE)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return dest


def _download_from_google():
    """The current CRLSet from Google's component updater: (crl-set bytes). Verified against the sha256
    of the HTTPS update response (the package itself may come over plain HTTP from edgedl)."""
    import hashlib
    import io
    import zipfile

    from . import _widevine as omaha  # the Omaha client the Widevine fetch uses

    body = omaha._omaha_request_body()
    body["request"]["app"][0]["appid"] = CRLSET_APP_ID
    url, sha256, _version = omaha._parse_update(omaha._post_json(omaha.OMAHA_URL, body))
    req = urllib.request.Request(url, headers={"User-Agent": omaha._user_agent()})
    with urllib.request.urlopen(req, timeout=60) as resp:
        blob = resp.read()
    if not sha256 or hashlib.sha256(blob).hexdigest() != sha256.lower():
        raise RuntimeError("CRLSet sha256 mismatch - refusing to install")
    with zipfile.ZipFile(io.BytesIO(omaha._crx3_to_zip(blob))) as zf:
        return zf.read(CRLSET_FILE)


def _touch_stamp():
    root = cache_root()
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, STAMP_FILE), "w"):
        pass


def _stamp_fresh():
    try:
        return time.time() - os.path.getmtime(os.path.join(cache_root(), STAMP_FILE)) < REFRESH_SECONDS
    except OSError:
        return False


def fetch_crlset(url=None):
    """Download the current CRLSet into the cache and return its path: from Google's component updater,
    or from ``url`` / CLEARCOTE_CRLSET_URL (a mirror serving the raw crl-set file)."""
    url = url or os.environ.get("CLEARCOTE_CRLSET_URL")
    _touch_stamp()  # a failed attempt counts too: no retry storm against Google
    if url:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
    else:
        data = _download_from_google()
    parse_header(data)
    tmp_dir = os.path.join(cache_root(), ".download-%d-%d" % (os.getpid(), threading.get_ident()))
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, CRLSET_FILE)
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        return install_crlset(tmp)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _refresh_in_background():
    """At most one refresh per process at a time; the launch goes on with the cached copy."""
    thread = _REFRESH["thread"]
    if thread is not None and thread.is_alive():
        return

    def run():
        try:
            fetch_crlset()
        except Exception:  # noqa: BLE001 - the cached copy still serves; the next stale launch retries
            pass

    _REFRESH["thread"] = threading.Thread(target=run, name="clearcote-crlset-refresh", daemon=True)
    _REFRESH["thread"].start()


def _fetch_first(quiet):
    """An empty cache: fetch now, waiting at most CLEARCOTE_CRLSET_FETCH_TIMEOUT. Returns the path or None."""
    wait = float(os.environ.get("CLEARCOTE_CRLSET_FETCH_TIMEOUT") or FETCH_TIMEOUT_SECONDS)
    box = {}

    def run():
        try:
            box["path"] = fetch_crlset()
        except Exception as exc:  # noqa: BLE001 - reported below
            box["error"] = exc

    thread = threading.Thread(target=run, name="clearcote-crlset-fetch", daemon=True)
    thread.start()
    thread.join(wait)
    if thread.is_alive():
        _warn_once("fetch", "no CRLSet cached and the first download took over %g s; launching without "
                            "one (it is still being fetched for the next launch)" % wait, quiet)
        return None
    if "error" in box:
        exc = box["error"]
        _warn_once("fetch", "could not fetch a CRLSet (%s: %s); launching without one"
                   % (type(exc).__name__, str(exc)[:160]), quiet)
        return None
    return box["path"]


def find_crlset(explicit=None):
    """The crl-set file to seed, or None. ``explicit`` (a path) > CLEARCOTE_CRLSET_FILE > newest cached."""
    for path in (explicit, os.environ.get("CLEARCOTE_CRLSET_FILE")):
        if path:
            return _as_crlset_file(path)
    root = cache_root()
    if not os.path.isdir(root):
        return None
    try:
        return _as_crlset_file(root)
    except ValueError:
        return None


def resolve_crlset(explicit=None, quiet=False):
    """``find_crlset``, filling an empty cache from Google first and refreshing a stale one in the
    background. None when there is none to be had (warned once)."""
    found = find_crlset(explicit)
    if explicit or os.environ.get("CLEARCOTE_CRLSET_FILE"):
        return found
    if found is None:
        return _fetch_first(quiet)
    if not _stamp_fresh():
        _refresh_in_background()
    return found


def crlset_mode(value):
    """``crlset=`` (None reads CLEARCOTE_CRLSET) -> (mode, path): mode is "off" (the default), "wait"
    or "nowait"."""
    if value is None:
        env = (os.environ.get("CLEARCOTE_CRLSET") or "").strip().lower()
        if env == "nowait":
            return "nowait", None
        return ("wait" if env in ("1", "true", "on", "yes") else "off"), None
    if value is False:
        return "off", None
    if value is True:
        return "wait", None
    if value == "nowait":
        return "nowait", None
    if isinstance(value, (str, os.PathLike)):
        return "wait", os.fspath(value)
    raise ValueError("crlset= must be True, False, \"nowait\" or a path to a CRLSet, not %r" % (value,))


def seed_crlset(user_data_dir, crlset_file):
    """Place the CRLSet in the profile where the component installer finds it. Keeps a newer one that is
    already there (a reused profile). Returns the sequence now in the profile."""
    data = _read(crlset_file)
    sequence = parse_header(data)["Sequence"]
    base = os.path.join(user_data_dir, COMPONENT_DIR)
    present = []
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.isdigit() and os.path.isfile(os.path.join(base, name, CRLSET_FILE)):
                present.append(int(name))
    if present and max(present) >= sequence:
        return max(present)
    version_dir = os.path.join(base, str(sequence))
    os.makedirs(version_dir, exist_ok=True)
    with open(os.path.join(version_dir, CRLSET_FILE), "wb") as f:
        f.write(data)
    with open(os.path.join(version_dir, "manifest.json"), "w") as f:
        json.dump({"manifest_version": 2, "name": "crl-set-clearcote.data", "version": str(sequence)}, f)
    return sequence


def unsuppress_component_updater(kwargs):
    """Drop Playwright's --disable-component-update (as apply_widevine_launch does). ``True`` already
    ignores every default argument; list/unset forms get the flag added."""
    ida = kwargs.get("ignore_default_args")
    if isinstance(ida, (list, tuple)):
        ida = list(ida)
        if "--disable-component-update" not in ida:
            ida.append("--disable-component-update")
        kwargs["ignore_default_args"] = ida
    elif ida is None:
        kwargs["ignore_default_args"] = ["--disable-component-update"]


def apply_crlset_launch(user_data_dir, kwargs, quiet=False):
    """Pop ``crlset`` / ``crlset_timeout`` from kwargs, seed the profile and un-suppress the updater.
    Returns a ``CRLSetReadiness`` when the launch must wait, else None.
    Never raises: revocation data is best-effort and must not stop a launch."""
    value = kwargs.pop("crlset", None)
    timeout = kwargs.pop("crlset_timeout", None)
    try:
        mode, explicit = crlset_mode(value)
        if mode == "off":
            return None
        try:
            crlset_file = resolve_crlset(explicit, quiet=quiet)
        except (OSError, ValueError) as exc:
            _warn_once("find", "no usable CRLSet (%s); launching without one" % exc, quiet)
            return None
        if crlset_file is None:  # resolve_crlset warned
            return None
        sequence = seed_crlset(user_data_dir, crlset_file)
        unsuppress_component_updater(kwargs)
        if mode == "nowait":
            return None
        if timeout is None:
            timeout = float(os.environ.get("CLEARCOTE_CRLSET_TIMEOUT") or DEFAULT_TIMEOUT_SECONDS)
        return CRLSetReadiness(float(timeout), sequence, quiet)
    except Exception as exc:  # noqa: BLE001 - best-effort, see docstring
        _warn_once("seed", "seeding failed (%s: %s); launching without one" % (type(exc).__name__, exc), quiet)
        return None


class _ProbeServer:
    """HTTPS on 127.0.0.1 that counts the TLS connections the browser opens to it."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # keep-alive: the browser pools the connection

            def setup(self):
                super().setup()
                with server.lock:
                    server.connections += 1

            def do_GET(self):
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        self.lock = threading.Lock()
        self.connections = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(PROBE_PEM)
        self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.url = "https://127.0.0.1:%d/" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="clearcote-crlset-probe", daemon=True)
        self.thread.start()

    def count(self):
        with self.lock:
            return self.connections

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


_PROBE_JS = "u => fetch(u, {cache: 'no-store'}).then(r => r.status, e => 0)"


def proxy_bypass_args(pw_kwargs, rule):
    """Launch args that send ``rule`` around a Playwright-handled proxy, else []. Playwright adds
    ``<-loopback>`` after the caller's bypass rules, and Chrome evaluates bypass rules last-first, so a
    rule added to ``proxy["bypass"]`` loses and the local probe went to the user's proxy as
    ``CONNECT 127.0.0.1:<port>`` (seen live). Chrome keeps the last of a repeated switch and Playwright
    puts the caller's args after its own, so this repeats Playwright's list with ``rule`` LAST: only that
    host:port goes direct; other loopback traffic still goes through the proxy. A proxy the SDK hands to
    the engine itself (credentials, see resolve_proxy) has no ``<-loopback>`` and needs nothing."""
    proxy = pw_kwargs.get("proxy")
    if not proxy:
        return []
    rules = [r.strip() for r in (proxy.get("bypass") or "").split(",") if r.strip()]
    rules = ["*" + r if r.startswith(".") else r for r in rules]  # as Playwright does
    if not os.environ.get("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK") and "<-loopback>" not in rules:
        rules.append("<-loopback>")
    return ["--proxy-bypass-list=" + ";".join(rules + [rule])]


class CRLSetReadiness:
    """The wait for a seeded CRLSet: ``before_launch`` (the probe server, so its port can bypass the
    proxy), then ``wait`` once the context is up, or ``close`` if the launch failed."""

    def __init__(self, timeout, sequence, quiet=False):
        self.timeout = timeout
        self.sequence = sequence
        self.quiet = quiet
        self.server = None

    def before_launch(self, pw_kwargs):
        """Start the probe server; returns the launch args to append (the proxy bypass for its port)."""
        try:
            self.server = _ProbeServer()
            return proxy_bypass_args(pw_kwargs, self.server.url[len("https://"):].rstrip("/"))
        except Exception as exc:  # noqa: BLE001 - best-effort: launch without waiting
            _warn_once("probe", "readiness probe unavailable (%s: %s)" % (type(exc).__name__, exc), self.quiet)
            self.close()
            return []

    def wait(self, context, ignore_https_errors=False):
        """Hold the first tab on about:blank until the CRLSet is live. True when confirmed; False on timeout
        or error (warned once). See the module docstring for the signal."""
        if self.server is None:
            return False
        deadline = time.monotonic() + self.timeout
        cdp = None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            cdp = context.new_cdp_session(page)
            cdp.send("Security.setIgnoreCertificateErrors", {"ignore": True})
            while time.monotonic() < deadline:
                page.evaluate(_PROBE_JS, self.server.url)
                if self.server.count() >= 2:  # the pooled connection was dropped: the cert verifier changed
                    return True
                time.sleep(0.05)
            _warn_once("timeout", "CRLSet not confirmed live within %g s; the browser may still trust revoked "
                                  "certificates until it loads" % self.timeout, self.quiet)
            return False
        except Exception as exc:  # noqa: BLE001 - best-effort
            _warn_once("wait", "readiness check failed (%s: %s)" % (type(exc).__name__, exc), self.quiet)
            return False
        finally:
            if cdp is not None:
                try:
                    cdp.send("Security.setIgnoreCertificateErrors", {"ignore": bool(ignore_https_errors)})
                    cdp.detach()
                except Exception:  # noqa: BLE001 - the browser may be gone
                    pass
            self.close()

    async def wait_async(self, context, ignore_https_errors=False):
        """``wait`` for the async API (playwright.async_api objects)."""
        import asyncio

        if self.server is None:
            return False
        deadline = time.monotonic() + self.timeout
        cdp = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Security.setIgnoreCertificateErrors", {"ignore": True})
            while time.monotonic() < deadline:
                await page.evaluate(_PROBE_JS, self.server.url)
                if self.server.count() >= 2:
                    return True
                await asyncio.sleep(0.05)
            _warn_once("timeout", "CRLSet not confirmed live within %g s; the browser may still trust revoked "
                                  "certificates until it loads" % self.timeout, self.quiet)
            return False
        except Exception as exc:  # noqa: BLE001 - best-effort
            _warn_once("wait", "readiness check failed (%s: %s)" % (type(exc).__name__, exc), self.quiet)
            return False
        finally:
            if cdp is not None:
                try:
                    await cdp.send("Security.setIgnoreCertificateErrors", {"ignore": bool(ignore_https_errors)})
                    await cdp.detach()
                except Exception:  # noqa: BLE001 - the browser may be gone
                    pass
            self.close()

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
