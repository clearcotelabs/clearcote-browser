"""Widevine CDM fetch + profile seeding (Windows + Linux).

clearcote is a 100%-open-source build, so it ships with the EME/Widevine *plumbing* compiled in
(`enable_widevine=true`) but WITHOUT the proprietary Widevine CDM binary (Google's blob can't live
in a FOSS package). This module fetches that CDM from Google's component server at runtime —
exactly like a real Chrome receives it via the component updater — so
`navigator.requestMediaKeySystemAccess('com.widevine.alpha')` resolves (DRM streaming works, and the
EME surface matches a real Chrome instead of being a "no-Widevine" coherence tell).

    from clearcote import fetch_widevine
    cdm_dir = fetch_widevine()          # downloads + verifies into ~/.clearcote/WidevineCdm
    ctx = clearcote.launch_persistent_context("profile", widevine=True)

WHEN IT IS ON (since 0.48.0). A licensed launch (a licence key resolved: the launch that selects the
PRO build, which claims the Google Chrome brand) on an engine with ``--widevine-cdm-path`` (r32+)
loads the CDM by default. A free launch keeps it opt-in. ``widevine=False`` or CLEARCOTE_WIDEVINE=0
turns it off, ``widevine=True`` or CLEARCOTE_WIDEVINE=1 turns it on for any launch; an explicit
option wins over the environment. See :func:`widevine_mode`.

The default is FAIL-SOFT and never waits on the network when a CDM is cached: a cached CDM is used at
once (a stale one is refreshed in the background). Only the very first launch, with nothing cached,
waits for the download, at most CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS (default 15, clamped to 1-600); when
it fails (offline, a firewall, Google refusing the request) the launch goes on without Widevine and says
so once per process, on stderr. The default path prints nothing else. A launch that ASKED for Widevine
keeps the behaviour it always had: a failure prints a note (unless quiet) and the launch goes on without DRM.

The CDM is fetched once and cached (CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm); Google is
asked for a newer one at most once a day, and a failed check counts as one (the cached CDM keeps working).
Installs take the SDK's install lock in the cache directory and land with a rename (retried on Windows
while a scanner holds the fresh tree), so concurrent first launches never see a half-extracted CDM. The
version Google names must look like one (``4.10.2830.0``) before it is used as a directory name.
`seed_widevine()` writes the component hint file into a user-data-dir so an older engine loads it.
The clearcote package itself never distributes Google's CDM: it is downloaded from Google, on the
user's machine.
"""

import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import threading
import time
import urllib.request
import uuid
import zipfile

# The Widevine CDM component (Chrome's component-updater app id) + Google's Omaha JSON endpoint.
WIDEVINE_APP_ID = "oimompecagnajdejgnnjijobebaeigek"
OMAHA_URL = "https://update.googleapis.com/service/update2/json"
# update.googleapis.com is behind Google's edge; send a browser-ish UA (a bare urllib UA can 403).
# Match the request UA to the OS we ask the CDM for, so the Omaha call is coherent (_user_agent).
_UA_LINUX = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
_UA_WINDOWS = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
# Chrome on Apple Silicon still says "Intel Mac OS X 10_15_7".
_UA_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
HINT_FILE = "latest-component-updated-widevine-cdm"

# Default-on Widevine (0.48.0). Same names, values and rules in the Node and .NET SDKs.
WIDEVINE_ENV = "CLEARCOTE_WIDEVINE"  # 1/true/yes/on forces it on, 0/false/no/off off; an explicit option wins
TIMEOUT_ENV = "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS"  # how long a DEFAULT launch waits for a first download
DEFAULT_TIMEOUT_SECONDS = 15.0
MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS = 1.0, 600.0  # the variable is clamped to this range
CHECK_INTERVAL_SECONDS = 24 * 3600  # ask Google for a newer CDM at most once a day
RETRY_AFTER_SECONDS = 600.0  # a failed first download is not retried for 10 minutes in this process
STAMP_FILE = ".last-check"  # in the cache root: mtime = the last update check (a failed one counts)
VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){1,3}")  # a CDM version (ASCII digits); anything else never becomes a path
LOCK_WAIT_SECONDS = 300.0  # how long an install waits for another process installing the CDM
WARN_CODE = "widevine-default"
# warn_once code for a default launch that leaves Widevine off because the engine lacks CDM host verification.
HOST_UNVERIFIED_CODE = "widevine-host-unverified"
# Exported by a CDM host built with CDM host verification (Chrome, Edge, Brave; Clearcote from r63).
CDM_HOST_VERIFY_SYMBOL = "VerifyCdmHost_0"
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")


def _mac_arm(machine=None):
    """True when this Mac's CPU is Apple Silicon (release.host_machine, the SDK's one CPU seam)."""
    from .release import MACOS_ARCHES, host_machine

    return (machine if machine is not None else host_machine()) in MACOS_ARCHES


def _cdm_platform(plat=None, machine=None):
    """(@os, os.platform, os.version, platform_subdir, cdm_filename) for the current OS. Linux ships
    libwidevinecdm.so under linux_x64 and registers via the hint file; Windows ships widevinecdm.dll
    under win_x64 and registers via the component-updater scan (see apply_widevine_launch), and so
    does macOS: libwidevinecdm.dylib under mac_arm64 on Apple Silicon (the native PRO build), mac_x64
    on an Intel Mac. Asked as an arm64 Mac, Google's server answers with ``…_mac_arm64_….crx3``, whose
    library sits at _platform_specific/mac_arm64/libwidevinecdm.dylib (checked 2026-10-17, CDM
    4.10.3112.0). Before this every Mac was sent the Windows CDM, which no Mac can load."""
    plat = plat or sys.platform
    if plat == "linux":
        return ("Linux", "Linux", "6.1.0", "linux_x64", "libwidevinecdm.so")
    if plat == "darwin":
        sub = "mac_arm64" if _mac_arm(machine) else "mac_x64"
        return ("mac", "Mac OS X", "14.0.0", sub, "libwidevinecdm.dylib")
    return ("win", "Windows", "10.0.19045.0", "win_x64", "widevinecdm.dll")


def _cdm_arch(plat=None, machine=None):
    """Omaha's (arch, nacl_arch, os.arch) for the CDM asked for: arm64 on an Apple Silicon Mac, x64
    everywhere else (the Windows and Linux browsers are x64 builds)."""
    if (plat or sys.platform) == "darwin" and _mac_arm(machine):
        return ("arm64", "arm", "arm64")
    return ("x64", "x86-64", "x86_64")


def _user_agent(plat=None):
    """The request UA for the OS the CDM is asked for. Read per request, not once at import."""
    plat = plat or sys.platform
    if plat == "linux":
        return _UA_LINUX
    if plat == "darwin":
        return _UA_MAC
    return _UA_WINDOWS


def _cache_root():
    return os.environ.get("CLEARCOTE_WIDEVINE_DIR",
                          os.path.join(os.path.expanduser("~"), ".clearcote", "WidevineCdm"))


def _omaha_request_body():
    # Minimal Omaha v3.1 update check for this OS's Widevine CDM. version=0.0.0.0 forces the
    # server to return the latest. (No requestid randomness — keep it deterministic + simple.)
    at_os, os_platform, os_version, _sub, _fn = _cdm_platform()
    arch, nacl_arch, os_arch = _cdm_arch()
    return {
        "request": {
            "@os": at_os, "@updater": "clearcote",
            "acceptformat": "crx3", "protocol": "3.1",
            "arch": arch, "nacl_arch": nacl_arch, "prodversion": "149.0.0.0",
            "updaterversion": "149.0.0.0", "dedup": "cr",
            "os": {"arch": os_arch, "platform": os_platform, "version": os_version},
            "app": [{
                "appid": WIDEVINE_APP_ID, "version": "0.0.0.0",
                "updatecheck": {}, "ping": {"r": -2},
            }],
        }
    }


def _post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": _user_agent()})
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8", "replace")
    # strip the XSSI guard prefix Google prepends
    if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
    return json.loads(raw)


def _parse_update(resp):
    """Pull (download_url, sha256, version) from an Omaha JSON update response. Handles both the
    newer `pipelines` shape and the classic `urls`+`manifest.packages` shape."""
    app = resp["response"]["app"][0]
    uc = app["updatecheck"]
    if uc.get("status") != "ok":
        raise RuntimeError("Widevine update check status: %r" % uc.get("status"))
    # newer pipelines format (what the live server returns)
    for pl in uc.get("pipelines", []):
        for op in pl.get("operations", []):
            urls = op.get("urls") or []
            out = op.get("out") or {}
            if urls and out.get("sha256"):
                return urls[0]["url"], out["sha256"], app.get("nextversion") or uc.get("nextversion", "")
    # classic format
    base = (uc.get("urls", {}).get("url") or [{}])[0].get("codebase")
    pkg = (uc.get("manifest", {}).get("packages", {}).get("package") or [{}])[0]
    ver = uc.get("manifest", {}).get("version", "")
    if base and pkg.get("name"):
        return base.rstrip("/") + "/" + pkg["name"], pkg.get("hash_sha256", ""), ver
    raise RuntimeError("could not find a CDM download URL in the update response")


def _crx3_to_zip(data):
    """A CRX3 file is 'Cr24' + u32 version + u32 header_len + header + zip. Return the zip bytes."""
    if data[:4] != b"Cr24":
        return data  # already a plain zip
    if len(data) < 12:
        raise RuntimeError("malformed CRX3 (truncated header)")
    header_len = struct.unpack_from("<I", data, 8)[0]
    if 12 + header_len > len(data):
        raise RuntimeError("malformed CRX3 (header overruns buffer)")
    return data[12 + header_len:]


def _cdm_binary(ver_dir):
    _at, _p, _v, sub, fn = _cdm_platform()
    return os.path.join(ver_dir, "_platform_specific", sub, fn)


def _valid_cdm(ver_dir):
    """A complete CDM directory: its manifest and this OS's CDM binary are both there."""
    return bool(ver_dir) and os.path.isfile(_cdm_binary(ver_dir)) and \
        os.path.isfile(os.path.join(ver_dir, "manifest.json"))


def _version_key(name):
    try:
        return (1, tuple(int(p) for p in name.split(".")))
    except ValueError:
        return (0, ())  # "current" (no version in the update response) sorts below any real version


def cached_cdm(root=None):
    """The newest complete CDM in the cache (``root``, default the cache root), or None. No network."""
    root = root or _cache_root()
    try:
        names = [n for n in os.listdir(root) if not n.startswith(".")]
    except OSError:
        return None
    for name in sorted(names, key=_version_key, reverse=True):
        ver_dir = os.path.join(root, name)
        if _valid_cdm(ver_dir):
            return ver_dir
    return None


def _stamp_fresh(root):
    """Whether Google was asked for a newer CDM less than CHECK_INTERVAL_SECONDS ago."""
    try:
        age = time.time() - os.path.getmtime(os.path.join(root, STAMP_FILE))
    except OSError:
        return False
    return -3600 < age < CHECK_INTERVAL_SECONDS  # a stamp from the future (clock moved back) is not fresh


def _touch_stamp(root):
    try:
        with open(os.path.join(root, STAMP_FILE), "w", encoding="utf-8") as handle:
            handle.write(str(int(time.time())))
    except OSError:
        pass


def _sweep_leftovers(root):
    """Remove what an interrupted install left in the cache root. Only called under the install lock,
    so no installer is using them."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if name.startswith((".tmp-", ".trash-")):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _version_dir(root, version):
    """``root``/<version>, once ``version`` looks like a CDM version (``4.10.2830.0``). The version comes from
    Google's update response: anything else (``../victim``, an absolute path) is refused before it can name, or
    delete, a directory. No version at all means ``current``."""
    if not version:
        return os.path.join(root, "current")
    if not VERSION_RE.fullmatch(str(version)):
        raise RuntimeError("Widevine update response named an unexpected version %r — refusing to use it"
                           % (str(version)[:80],))
    return os.path.join(root, version)


def _install(root, url, sha256, version, quiet):
    """Download, verify and install one CDM version into ``root``/<version>. Holds the SDK's install
    lock (``root``/.install-lock, the protocol in download.py) so concurrent first launches take turns,
    extracts into ``root``/.tmp-<id> and renames the finished tree into place: a reader never sees a
    half-extracted CDM. The renames are download.py's, which retry while a Windows scanner holds a file in
    the fresh tree."""
    from .download import _install_lock, _rename_dir

    ver_dir = _version_dir(root, version)
    os.makedirs(root, exist_ok=True)
    with _install_lock(root, quiet=True, timeout=LOCK_WAIT_SECONDS):
        if _valid_cdm(ver_dir):  # another process installed it while we waited
            return ver_dir
        _sweep_leftovers(root)
        if not quiet:
            print("[widevine] fetching CDM %s" % (version or "(latest)"))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": _user_agent()}),
                                    timeout=120) as r:
            blob = r.read()
        if hashlib.sha256(blob).hexdigest() != sha256.lower():
            raise RuntimeError("Widevine CDM sha256 mismatch — refusing to install")
        tmp = os.path.join(root, ".tmp-" + uuid.uuid4().hex)
        try:
            os.makedirs(tmp)
            with zipfile.ZipFile(io.BytesIO(_crx3_to_zip(blob))) as zf:
                zf.extractall(tmp)
            if not _valid_cdm(tmp):
                # some CRX layouts nest the platform dir differently; surface a clear error
                raise RuntimeError("extracted CDM but the CDM binary was not at %s"
                                   % _cdm_binary(ver_dir))
            if os.path.lexists(ver_dir):  # an incomplete tree (an older SDK extracted in place): move it aside
                trash = os.path.join(root, ".trash-" + uuid.uuid4().hex)
                _rename_dir(ver_dir, trash)
                shutil.rmtree(trash, ignore_errors=True)
            _rename_dir(tmp, ver_dir)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if not quiet:
        print("[widevine] installed:", ver_dir)
    return ver_dir


def fetch_widevine(dest=None, quiet=False):
    """Download + verify the current OS's x64 Widevine CDM into ``dest`` (default
    CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm) as <version>/. Returns the versioned CDM
    directory (manifest.json + _platform_specific/<os>_x64/<cdm binary>).

    Always asks Google's update server for the latest version; the download is skipped when that
    version is already installed. Installs take the install lock and land with a rename (see
    :func:`_install`). A version that does not look like one is refused (see :func:`_version_dir`)."""
    resp = _post_json(OMAHA_URL, _omaha_request_body())
    url, sha256, version = _parse_update(resp)
    root = dest or _cache_root()
    ver_dir = _version_dir(root, version)
    if _valid_cdm(ver_dir):
        _touch_stamp(root)
        if not quiet:
            print("[widevine] already present:", ver_dir)
        return ver_dir
    # The CDM is a NATIVE library loaded into the browser process — never install it unverified. A
    # missing hash in the update response is a hard failure, not a skip (checked before downloading).
    if not sha256:
        raise RuntimeError("Widevine update response had no sha256 — refusing to install an "
                           "unverified CDM")
    ver_dir = _install(root, url, sha256, version, quiet)
    _touch_stamp(root)
    return ver_dir


def resolve_cdm(quiet=False):
    """The CDM directory a launch should use, network only when needed.

    The cached CDM when Google was asked within the last day (no network); otherwise
    :func:`fetch_widevine` (one update check, a download only for a new version). When that fails --
    offline, a firewall, Google refusing the request -- the CDM already on disk still works and is
    used, and the failed check is recorded (the stamp is touched), so no launch asks again for a day.
    Raises only when there is no CDM at all."""
    root = _cache_root()
    cached = cached_cdm(root)
    if cached and _stamp_fresh(root):
        return cached
    try:
        return fetch_widevine(quiet=quiet)  # module global, looked up per call (tests stand in for it)
    except Exception:
        if cached:
            _touch_stamp(root)
            return cached
        raise


def widevine_mode(option, licensed, env=None):
    """``"explicit"``, ``"default"`` or ``"off"`` for a launch.

    ``option`` (the ``widevine`` launch option) wins: True -> explicit, False -> off. Unset follows
    CLEARCOTE_WIDEVINE (1/true/yes/on -> explicit, 0/false/no/off -> off). Otherwise a licensed launch
    (``licensed``: a licence key resolved, which selects the PRO build) gets the default, and a free
    launch stays off. Same rules in the Node and .NET SDKs."""
    if option is not None:
        return "explicit" if option else "off"
    env = os.environ if env is None else env
    value = str(env.get(WIDEVINE_ENV) or "").strip().lower()
    if value in _TRUTHY:
        return "explicit"
    if value in _FALSY:
        return "off"
    return "default" if licensed else "off"


def timeout_seconds(env=None):
    """How long a default (not requested) launch waits for a first download: CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS,
    clamped to 1-600, else 15. An unparsable or non-positive value (or NaN) falls back to 15."""
    env = os.environ if env is None else env
    try:
        value = float(str(env.get(TIMEOUT_ENV) or "").strip())
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if not value > 0:  # also NaN
        return DEFAULT_TIMEOUT_SECONDS
    return min(max(value, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


# The default path's process state: the first download in flight (one at a time), the background refresh of a
# stale cached CDM (one at a time), and when a first download last failed.
_DEFAULT_LOCK = threading.Lock()
_DEFAULT = {"job": None, "refresh": None, "failed_at": None}


def _reset_default_state():
    """Tests: forget the in-flight fetch, the refresh and the last failure."""
    with _DEFAULT_LOCK:
        _DEFAULT["job"] = None
        _DEFAULT["refresh"] = None
        _DEFAULT["failed_at"] = None


def _warn_default(reason, quiet):
    from ._warnings import warn_once
    warn_once(WARN_CODE,
              "Widevine is on by default for licensed launches, but the CDM could not be loaded (%s); "
              "launching without it. Set CLEARCOTE_WIDEVINE=0 (or pass widevine=False) to stop trying."
              % reason, quiet=quiet)


def _refresh_in_background():
    """Ask Google whether a newer CDM exists, without anyone waiting: a daemon thread, one at a time, quiet.
    :func:`resolve_cdm` records a failed check (stamp), so a broken network is asked once a day."""
    with _DEFAULT_LOCK:
        running = _DEFAULT["refresh"]
        if running is not None and running.is_alive():
            return running

        def run():
            try:
                resolve_cdm(quiet=True)
            except Exception:  # noqa: BLE001, S110 - the cached CDM keeps working; nothing to report
                pass

        thread = threading.Thread(target=run, name="clearcote-widevine-refresh", daemon=True)
        _DEFAULT["refresh"] = thread
        thread.start()
        return thread


def default_cdm(quiet=False, timeout=None):
    """The CDM for a launch that did not ask for Widevine (the licensed default), or None.

    A CDM in the cache is used AT ONCE, with no network on the launch path; when its last update check is
    over a day old, a background thread asks Google for a newer one (it is used from the next launch on).
    Only with nothing cached does the launch wait for a download, at most ``timeout`` seconds (default
    :func:`timeout_seconds`). FAIL-SOFT: never raises. When there is no CDM to use (the download failed or
    did not finish in time) it warns once per process on stderr (unless quiet) and answers None, so the
    launch goes on without DRM; the download is not retried for RETRY_AFTER_SECONDS in this process, and one
    still running after the wait carries on in the background (a later launch uses what it installs). The
    fetches here are quiet: the default path never prints progress."""
    root = _cache_root()
    cached = cached_cdm(root)
    if cached:
        if not _stamp_fresh(root):
            _refresh_in_background()
        return cached
    wait = timeout_seconds() if timeout is None else timeout
    with _DEFAULT_LOCK:
        job = _DEFAULT["job"]
        failed_at = _DEFAULT["failed_at"]
        running = job is not None and job["thread"].is_alive()
        if failed_at is not None and time.monotonic() - failed_at < RETRY_AFTER_SECONDS:
            return None  # failed (or timed out) recently: launch at once, without waiting again
        if not running:
            job = {"result": None, "error": None}

            def run(box=job):
                try:
                    box["result"] = resolve_cdm(quiet=True)
                except BaseException as exc:  # noqa: BLE001 - reported to the waiting launch
                    box["error"] = exc

            job["thread"] = threading.Thread(target=run, name="clearcote-widevine", daemon=True)
            _DEFAULT["job"] = job
            job["thread"].start()
    job["thread"].join(wait)
    if job["thread"].is_alive():
        with _DEFAULT_LOCK:
            _DEFAULT["failed_at"] = time.monotonic()
        _warn_default("not ready within %g s" % wait, quiet)
        return None
    if job["error"] is not None:
        with _DEFAULT_LOCK:
            _DEFAULT["failed_at"] = time.monotonic()
        _warn_default(_short_reason(job["error"]), quiet)
        return None
    return job["result"]


def _short_reason(exc):
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return "%s: %s" % (type(exc).__name__, text[:160]) if text else type(exc).__name__


def apply_widevine_launch(user_data_dir, kwargs, quiet=False):
    """Make ``widevine=True`` work end-to-end on a persistent context: seed the CDM into the
    profile AND fix the launch args so the engine actually registers it.

    The component updater registers the sideloaded CDM, but (1) Playwright disables it by default
    (``--disable-component-update``) and (2) it won't scan an already-installed component without
    ``--component-updater=fast-update``. So we un-suppress it and force the startup scan. Verified:
    with these, ``requestMediaKeySystemAccess('com.widevine.alpha')`` + ``createMediaKeys()`` succeed.
    Mutates ``kwargs`` in place. DRM is best-effort: any failure here degrades to a normal (no-DRM)
    launch — it must never abort ``launch_persistent_context``."""
    try:
        seed_widevine(user_data_dir, quiet=quiet)
        # Un-suppress the component updater. Playwright's ``ignore_default_args`` may be a list, or
        # the bool form (True = ignore ALL defaults, incl. --disable-component-update, so the
        # updater is already un-suppressed). Only the list/unset forms need the flag added.
        ida = kwargs.get("ignore_default_args")
        if isinstance(ida, (list, tuple)):
            ida = list(ida)
            if "--disable-component-update" not in ida:
                ida.append("--disable-component-update")
            kwargs["ignore_default_args"] = ida
        elif ida is None:
            kwargs["ignore_default_args"] = ["--disable-component-update"]
        # Force the pre-installed-component scan. A user-supplied --component-updater mode wins, but
        # if it isn't fast-update the CDM may not register — surface that instead of silently failing.
        # Force the pre-installed-component scan on Windows and macOS (the component-updater path). On
        # Linux the CDM hint file we seeded IS the registration mechanism — read at startup regardless —
        # so no fast-update scan is needed there (verified: hint file + un-suppressed updater is enough).
        args = list(kwargs.get("args") or [])
        if sys.platform != "linux":
            existing = [a for a in args if "component-updater" in a]
            if not existing:
                args.append("--component-updater=fast-update")
            elif not any("fast-update" in a for a in existing) and not quiet:
                print("[widevine] note: your --component-updater mode may not register the CDM; "
                      "--component-updater=fast-update is needed to scan the pre-installed component")
        kwargs["args"] = args
    except Exception as exc:  # noqa: BLE001 — DRM is best-effort; never abort the launch
        if not quiet:
            print("[widevine] setup failed (continuing without DRM): %r" % exc)


def seed_widevine(user_data_dir, cdm_dir=None, quiet=False):
    """Make a persistent profile load the fetched CDM: copy it under <user_data_dir>/WidevineCdm/
    <version>/ and write the component hint file the engine reads. Fetches the CDM first if needed
    (:func:`resolve_cdm`: the cached one when it is current). No-op-safe to call on every launch."""
    src = cdm_dir or resolve_cdm(quiet=quiet)
    version = os.path.basename(src.rstrip(os.sep))
    wv_root = os.path.join(user_data_dir, "WidevineCdm")
    target = os.path.join(wv_root, version)
    _at, _p, _v, sub, fn = _cdm_platform()
    if not os.path.isfile(os.path.join(target, "_platform_specific", sub, fn)):
        import shutil
        os.makedirs(wv_root, exist_ok=True)
        shutil.copytree(src, target, dirs_exist_ok=True)
    # hint file: tells the engine where the latest component-updated CDM lives
    try:
        with open(os.path.join(wv_root, HINT_FILE), "w", encoding="utf-8") as handle:
            json.dump({"Path": target}, handle)
    except OSError:
        pass
    if not quiet:
        print("[widevine] seeded into", wv_root)
    return target


def widevine_cdm_args(exe, quiet=False):
    """``["--widevine-cdm-path=<the fetched CDM>"]`` when the engine implements that switch (patch 1022),
    else ``[]``. Such an engine registers the CDM at startup in every profile, Playwright's throwaway ones
    and incognito launches included, so DRM no longer depends on the component updater; the hint file
    that :func:`apply_widevine_launch` seeds keeps older engines working. Best-effort, like the rest of
    DRM: any failure answers ``[]`` and the launch goes on."""
    try:
        from ._launchopts import engine_supports_switch
        if not engine_supports_switch(exe, "widevine-cdm-path"):
            return []
        return ["--widevine-cdm-path=" + resolve_cdm(quiet=quiet)]
    except Exception:
        return []


def _caller_set_cdm_path(user_args):
    return any(str(a).split("=", 1)[0] == "--widevine-cdm-path" for a in (user_args or ()))


def widevine_launch_args(exe, option, licensed, user_args=(), quiet=False, timeout=None):
    """The Widevine switch for one launch, by :func:`widevine_mode`.

    explicit: :func:`widevine_cdm_args` (the behaviour ``widevine=True`` always had). default: the
    same switch, only on an engine that implements it (r32+; an older engine keeps the old default,
    off), with the CDM from :func:`default_cdm` (bounded, fail-soft, warns once). off: nothing. A
    ``--widevine-cdm-path`` the caller put in ``args`` is theirs: nothing is added then."""
    mode = widevine_mode(option, licensed)
    if mode == "off" or _caller_set_cdm_path(user_args):
        return []
    if mode == "explicit":
        return widevine_cdm_args(exe, quiet=quiet)
    from ._launchopts import engine_supports_switch
    if not engine_supports_switch(exe, "widevine-cdm-path"):
        return []
    if not cdm_host_verified(exe):
        from ._warnings import warn_once
        warn_once(HOST_UNVERIFIED_CODE,
                  "Widevine is off by default on this engine: it sends the DRM licence request with a clear client id, "
                  "which detection sites flag on Windows and macOS. It comes back on by default with an engine that has "
                  "CDM host verification (r63 and later). Pass widevine=True (or set CLEARCOTE_WIDEVINE=1) to use DRM "
                  "anyway.", quiet=quiet)
        return []
    cdm = default_cdm(quiet=quiet, timeout=timeout)
    return ["--widevine-cdm-path=" + cdm] if cdm else []


def cdm_host_verified(exe, platform=None):
    """Whether a CDM licence request from this engine carries an ENCRYPTED client id, as Chrome's does.

    Without CDM host verification (every Clearcote engine up to r62) the Widevine CDM puts its client id in the
    clear, and that names the host OS. fingerprint.com flags that on Windows (anti_detect_browser, measured 10 of 10
    sessions on r62 behind a proxy, 1 of 12 with Widevine off); a clear request from a Linux host is not flagged. So
    Linux always counts as fine; on Windows and macOS the engine must contain :data:`CDM_HOST_VERIFY_SYMBOL` (a
    NUL-delimited literal in chrome.dll / the framework binary). Same rule in the Node and .NET SDKs."""
    platform = sys.platform if platform is None else platform
    if not (platform.startswith("win") or platform == "darwin"):
        return True
    from ._launchopts import engine_supports_switch
    return engine_supports_switch(exe, CDM_HOST_VERIFY_SYMBOL)
