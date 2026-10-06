"""launch() on macOS: run the Clearcote Docker image and connect to it.

There is no native Clearcote build for macOS. Rather than fail there, ``launch()`` starts the
published Clearcote image (``teamflatearth/clearcote:sdk-<this SDK's version>``, a linux/amd64 image
that Docker Desktop runs on Intel and Apple silicon alike), waits for its CDP endpoint, connects to it
with Playwright and returns the same Playwright ``Browser`` a local launch returns. ``close()``
disconnects and stops the container; nothing is left running.

When it applies:
  * ``docker=False`` (or ``CLEARCOTE_DOCKER=0``) turns it off: launch() behaves as before, which on
    macOS means "pass executable_path= a compatible binary".
  * ``docker=True`` (or ``CLEARCOTE_DOCKER=1``) turns it on on any OS that has Docker.
  * Unset: on for macOS, unless the caller named a binary (``executable_path`` / CLEARCOTE_BINARY).
  * ``CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1`` makes this module treat the host as macOS. It exists so the
    real Docker path can be exercised end to end on a Linux or Windows machine; it is not an option.

The container is configured through the image's own CC_* environment variables (docker/serve.py), so
only the options the image understands are accepted; anything else raises ValueError naming it.
Values travel in the docker CLI's environment (``-e NAME`` without a value), never on its command
line. The licence key and the proxy URL are not passed as variables at all: they are copied into the
container as a file serve.py reads once and deletes, so `docker inspect` does not show them (anyone who
can reach the Docker daemon can still read a running container's files and memory). The CDP port is
published on 127.0.0.1 only: it is full control of the browser.

Nothing outlives its owner: the container is created with --rm, stops itself once no CDP client has been
connected for 30 s (CLEARCOTE_DOCKER_IDLE_EXIT), and the next launch on the machine removes any whose
owner process is gone. close() stops it at once.
"""
from __future__ import annotations

import atexit
import collections
import http.client
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time

DEFAULT_REPOSITORY = "teamflatearth/clearcote"
TEST_ONLY_ASSUME_MACOS = "CLEARCOTE_TEST_ONLY_ASSUME_MACOS"
LABEL = "com.clearcotelabs.sdk-launch=1"
# Who started a container: sweep_stale() removes the ones whose owner process is gone.
OWNER_HOST_LABEL = "com.clearcotelabs.owner-host"
OWNER_PID_LABEL = "com.clearcotelabs.owner-pid"
# A container stops itself once no CDP client has been connected for this long (serve.py's
# CC_IDLE_EXIT_SECONDS). CLEARCOTE_DOCKER_IDLE_EXIT overrides it; 0 turns it off.
DEFAULT_IDLE_EXIT_S = 30
DEFAULT_CACHE_VOLUME = "clearcote-cache"
# The licence key and the proxy URL (it may carry a password) reach the container as this file, copied in
# with `docker cp` and deleted by serve.py once read, not as -e variables `docker inspect` would show.
SECRET_ENV = ("CLEARCOTE_LICENSE_KEY", "CC_PROXY")
SECRETS_FILE = "/tmp/clearcote-secrets.json"
IMAGE_UID = 10001  # the image's user (cc)
_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("0", "false", "no", "off")
# The image starts a virtual display, Chromium and (with a licence) fetches the licensed engine on its
# first run: well past Playwright's 30 s connect default, so the wait for CDP gets its own budget.
_READY_TIMEOUT_S = 180.0
_CONNECT_TIMEOUT_MS = 120_000

INSTALL_URL = "https://docs.docker.com/desktop/setup/install/mac-install/"
_OFF_HINT = ("To launch without Docker, pass docker=False (or set CLEARCOTE_DOCKER=0) and executable_path= "
             "a compatible Clearcote binary.")


class DockerUnavailableError(RuntimeError):
    """launch() needs Docker on this host (macOS has no native Clearcote build) and it is missing or
    not running."""


# ── option mapping: launch() keyword -> the image's CC_* variable ──────────────────────────────────
_STR_ENV = {
    "fingerprint": "CC_FINGERPRINT", "platform": "CC_PLATFORM", "platform_version": "CC_PLATFORM_VERSION",
    "brand": "CC_BRAND", "brand_version": "CC_BRAND_VERSION", "gpu_vendor": "CC_GPU_VENDOR",
    "gpu_renderer": "CC_GPU_RENDERER", "timezone": "CC_TIMEZONE", "accept_language": "CC_ACCEPT_LANGUAGE",
    "webrtc_ip": "CC_WEBRTC_IP", "webrtc_mdns": "CC_WEBRTC_MDNS", "tls_profile": "CC_TLS_PROFILE",
    "version": "CC_VERSION",
}
_INT_ENV = {
    "hardware_concurrency": "CC_HARDWARE_CONCURRENCY", "device_memory": "CC_DEVICE_MEMORY",
    "color_depth": "CC_COLOR_DEPTH", "max_touch_points": "CC_MAX_TOUCH_POINTS",
    "storage_quota": "CC_STORAGE_QUOTA",
}
_FLOAT_ENV = {"device_pixel_ratio": "CC_DEVICE_PIXEL_RATIO"}
_BOOL_ENV = {
    "light_stealth": "CC_LIGHT_STEALTH", "disable_gpu_fingerprint": "CC_DISABLE_GPU_FINGERPRINT",
    "fingerprint_noise": "CC_FINGERPRINT_NOISE", "canvas_noise": "CC_CANVAS_NOISE",
    "gpu_string_spoof": "CC_GPU_STRING_SPOOF",
}
# Handled specially below, or on this side of the connection.
_SPECIAL = ("location", "fingerprint_profile", "headless", "proxy", "args", "license_key", "license_api_base")
SDK_SIDE = ("timeout", "slow_mo", "humanize", "show_cursor", "quiet", "docker", "docker_image",
            "ephemeral_profile")
ACCEPTED = tuple(_STR_ENV) + tuple(_INT_ENV) + tuple(_FLOAT_ENV) + tuple(_BOOL_ENV) + _SPECIAL + SDK_SIDE


def host_is_macos() -> bool:
    return sys.platform == "darwin" or os.environ.get(TEST_ONLY_ASSUME_MACOS, "").strip() == "1"


def docker_requested(docker=None, kwargs=None) -> bool:
    """Whether this launch runs in the Clearcote Docker image (see the module docstring)."""
    if docker is not None:
        if isinstance(docker, str):  # "0"/"false" from a config file must not mean on
            return docker.strip().lower() in _TRUTHY
        return bool(docker)
    if (kwargs or {}).get("executable_path"):
        return False  # the caller named a binary: theirs to run
    env = os.environ.get("CLEARCOTE_DOCKER", "").strip().lower()
    if env in _FALSY:
        return False
    if env in _TRUTHY:
        return True
    if os.environ.get("CLEARCOTE_BINARY"):
        return False
    return host_is_macos()


def default_image() -> str:
    from . import __version__
    return os.environ.get("CLEARCOTE_DOCKER_IMAGE") or f"{DEFAULT_REPOSITORY}:sdk-{__version__}"


def _proxy_url(proxy) -> str:
    if isinstance(proxy, str):
        return proxy
    if not isinstance(proxy, dict) or not proxy.get("server"):
        raise ValueError("proxy must be a URL or {'server': ..., 'username': ..., 'password': ...}")
    server = str(proxy["server"])
    scheme, sep, rest = server.partition("://")
    if not sep:
        scheme, rest = "http", server
    user, pw = proxy.get("username"), proxy.get("password")
    if user or pw:
        from urllib.parse import quote
        rest = f"{quote(str(user or ''), safe='')}:{quote(str(pw or ''), safe='')}@{rest}"
    return f"{scheme}://{rest}"


def container_env(kwargs: dict) -> dict:
    """The CC_* (and licence) variables the image is started with, from launch()'s keywords. Raises
    ValueError naming the first option the image cannot take."""
    unknown = sorted(k for k, v in kwargs.items() if k not in ACCEPTED and v is not None)
    if unknown:
        raise ValueError(
            f"{unknown[0]} is not available when launch() runs Clearcote in Docker (there is no native "
            f"macOS build). The container takes: {', '.join(sorted(set(ACCEPTED) - set(SDK_SIDE)))}. "
            + _OFF_HINT)
    env = {}
    for k, name in _STR_ENV.items():
        if kwargs.get(k) is not None:
            if isinstance(kwargs[k], bool):
                raise ValueError(f"{k}={kwargs[k]!r} is not available when launch() runs Clearcote in Docker")
            env[name] = str(kwargs[k])
    for k, name in _INT_ENV.items():
        if kwargs.get(k) is not None:
            env[name] = str(int(kwargs[k]))
    for k, name in _FLOAT_ENV.items():
        if kwargs.get(k) is not None:
            env[name] = repr(float(kwargs[k]))
    for k, name in _BOOL_ENV.items():
        if kwargs.get(k) is not None:
            env[name] = "1" if kwargs[k] else "0"  # False is a value (canvas_noise=False turns it off)
    loc = kwargs.get("location")
    if loc is not None:
        env["CC_LOCATION"] = loc if isinstance(loc, str) else ",".join(str(x) for x in loc)
    prof = kwargs.get("fingerprint_profile")
    if prof is not None:
        if isinstance(prof, dict):
            env["CC_FINGERPRINT_PROFILE"] = json.dumps(prof, separators=(",", ":"))
        elif isinstance(prof, str) and os.path.isfile(prof):  # a path on THIS machine: send its content
            with open(prof, encoding="utf-8") as fh:
                env["CC_FINGERPRINT_PROFILE"] = fh.read()
        else:
            env["CC_FINGERPRINT_PROFILE"] = str(prof)
    if kwargs.get("headless") is True:  # unset or False: the image's default, headed on its own display
        env["CC_HEADLESS"] = "1"
    if kwargs.get("proxy"):
        env["CC_PROXY"] = _proxy_url(kwargs["proxy"])
    args = kwargs.get("args")
    if args:
        bad = [a for a in args if not str(a) or any(c.isspace() for c in str(a))]
        if bad:
            raise ValueError(f"args {bad[0]!r}: an argument with whitespace cannot be passed to the Docker image")
        env["CC_EXTRA_ARGS"] = " ".join(str(a) for a in args)
    from ._license import resolve_license_key
    key = resolve_license_key(kwargs.get("license_key"))  # as a local launch: option > env > saved key
    if key:
        env["CLEARCOTE_LICENSE_KEY"] = key
    if kwargs.get("license_api_base"):
        env["CLEARCOTE_LICENSE_API"] = str(kwargs["license_api_base"])
    return env


# ── the docker CLI ─────────────────────────────────────────────────────────────────────────────────

def _docker_cli():
    """The docker executable, or None. A seam: tests replace it."""
    return shutil.which("docker")


def _run(argv, env=None, timeout=120.0, input=None):
    """Run the docker CLI -> (returncode, stdout, stderr). ``input`` (bytes) goes to its stdin. A seam:
    tests replace it."""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout, input=input,
                           env=None if env is None else {**os.environ, **env})
    except subprocess.TimeoutExpired:
        return 124, "", f"`{' '.join(argv[:2])}` timed out after {timeout:.0f} s"
    except OSError as e:
        return 127, "", str(e)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def _first_line(text):
    return next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")


def check_docker() -> str:
    """The docker executable, or DockerUnavailableError saying what to do."""
    exe = _docker_cli()
    if not exe:
        raise DockerUnavailableError(
            "Clearcote has no native macOS build, so on macOS launch() runs it in Docker, but the "
            f"`docker` command was not found. Install Docker Desktop ({INSTALL_URL}), start it, and try "
            "again. " + _OFF_HINT)
    code, _out, err = _run([exe, "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if code != 0:
        raise DockerUnavailableError(
            "Clearcote has no native macOS build, so on macOS launch() runs it in Docker, but Docker is "
            f"not running (`docker info`: {_first_line(err) or f'exit {code}'}). Start Docker Desktop and "
            "try again. " + _OFF_HINT)
    return exe


def idle_exit_seconds() -> int:
    """How long a launched container waits with no CDP client before it stops itself:
    CLEARCOTE_DOCKER_IDLE_EXIT (seconds, 0 = never), default DEFAULT_IDLE_EXIT_S."""
    raw = os.environ.get("CLEARCOTE_DOCKER_IDLE_EXIT", "").strip()
    if not raw:
        return DEFAULT_IDLE_EXIT_S
    try:
        return max(0, int(raw))
    except ValueError:
        raise ValueError(f"CLEARCOTE_DOCKER_IDLE_EXIT={raw!r} is not a whole number of seconds") from None


def cache_volume() -> str:
    """The named volume a licensed container keeps its engine in (shared by every launch, so it is
    downloaded once): CLEARCOTE_DOCKER_CACHE_VOLUME, default clearcote-cache."""
    return os.environ.get("CLEARCOTE_DOCKER_CACHE_VOLUME", "").strip() or DEFAULT_CACHE_VOLUME


def _pid_alive(pid) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def sweep_stale(exe) -> list:
    """Stop and remove containers an earlier launch on this machine left behind: labelled as ours, with an
    owner process that no longer exists (killed, crashed). They would stop on their own after the idle
    period; this frees their licence seat now. Containers of live processes, and of other machines sharing
    the Docker daemon, are left alone. Returns the ids removed."""
    fmt = "{{.ID}}\t{{.Label \"%s\"}}\t{{.Label \"%s\"}}" % (OWNER_HOST_LABEL, OWNER_PID_LABEL)
    code, out, _err = _run([exe, "ps", "-a", "--filter", f"label={LABEL}", "--format", fmt], timeout=30)
    if code != 0:
        return []
    here = socket.gethostname()
    stale = []
    for line in (out or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3 and parts[1] == here and parts[2].isdigit() and not _pid_alive(int(parts[2])):
            stale.append(parts[0])
    if stale:
        _run([exe, "stop", "--time", "10", *stale], timeout=120)
        _run([exe, "rm", "-f", "-v", *stale], timeout=60)
    return stale


_LIVE: dict = {}  # container id -> docker executable, for the exit-time sweep
_LIVE_LOCK = threading.Lock()


def _remove(exe, cid):
    # stop first: SIGTERM lets the image release a licence seat. The container was created with --rm, so
    # stopping it removes it and its anonymous volume (the image declares VOLUME /opt/xdg-cache: ~0.5 GB);
    # rm -f -v is for a container that did not go away. A named volume is never removed.
    _run([exe, "stop", "--time", "10", cid], timeout=60)
    _run([exe, "rm", "-f", "-v", cid], timeout=60)
    with _LIVE_LOCK:
        _LIVE.pop(cid, None)


@atexit.register
def _sweep():
    with _LIVE_LOCK:
        live = list(_LIVE.items())
    for cid, exe in live:
        _remove(exe, cid)


class _LogTail:
    """``docker logs --follow`` of a starting container, kept in memory: the container is created with
    --rm, so once it has stopped its logs cannot be asked for any more."""

    def __init__(self, exe, cid):
        self._lines = collections.deque(maxlen=40)
        try:
            self._proc = subprocess.Popen([exe, "logs", "--follow", cid], stdout=subprocess.PIPE,
                                          stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except OSError:
            self._proc = None
            return
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        for raw in iter(self._proc.stdout.readline, b""):
            self._lines.append(raw.decode("utf-8", "replace").rstrip())

    def text(self, wait=3.0):
        if self._proc is not None:
            self._thread.join(wait)  # the container is gone: the follower ends with its last line
        return "\n".join(self._lines)

    def stop(self):
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


def _follow_logs(exe, cid):
    """A seam: tests replace it."""
    return _LogTail(exe, cid)


def _published_port(exe, cid) -> int:
    code, out, err = _run([exe, "port", cid, "9222/tcp"], timeout=30)
    for line in (out or "").splitlines():
        host, _, port = line.strip().rpartition(":")
        if port.isdigit():
            return int(port)
    raise RuntimeError(f"could not read the container's published CDP port ({_first_line(err) or out!r})")


def _cdp_ready(port, timeout=2.0) -> bool:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/json/version")
        res = conn.getresponse()
        body = res.read()
        return res.status == 200 and b"webSocketDebuggerUrl" in body
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def _wait_ready(exe, cid, port, deadline_s, logs, sleep=time.sleep):
    deadline = time.monotonic() + deadline_s
    while True:
        if _cdp_ready(port):
            return
        code, out, _err = _run([exe, "inspect", "-f", "{{.State.Running}}", cid], timeout=30)
        if code != 0 or out.strip() != "true":  # stopped (and, with --rm, already gone)
            raise RuntimeError("the Clearcote container stopped before its browser came up:\n"
                               + (logs.text().strip() or "(it left no log)"))
        if time.monotonic() >= deadline:
            raise TimeoutError(f"the Clearcote container's CDP endpoint did not answer within {deadline_s:.0f} s")
        sleep(0.25)


_VOLUME_INIT_RACE = re.compile(r"volumes/[^/\s]+/_data\S*: (?:file exists|no such file)", re.I)
_NOT_PUBLISHED = re.compile(r"manifest unknown|manifest for .* not found|not found: manifest|pull access denied|"
                            r"repository does not exist", re.I)


def _create_failed(image, code, err):
    detail = (err or "").strip() or f"exit {code}"
    if _NOT_PUBLISHED.search(detail):
        default = image == f"{DEFAULT_REPOSITORY}:sdk-{_sdk_version()}"
        why = ("Images are published a few minutes after each SDK release (after the PyPI package they are "
               "built from), so a brand-new SDK can be ahead of its image: try again shortly, or set "
               f"CLEARCOTE_DOCKER_IMAGE={DEFAULT_REPOSITORY}:latest to run the newest published image."
               if default else "Check the name, or unset CLEARCOTE_DOCKER_IMAGE / docker_image= to use the default.")
        return RuntimeError(f"the Clearcote image {image} is not available ({_first_line(detail)}). {why}")
    return RuntimeError(f"`docker create {image}` failed: {detail}\n(set CLEARCOTE_DOCKER_IMAGE or "
                        "docker_image= to use another image)")


def _sdk_version():
    from . import __version__
    return __version__


def secrets_tar(secrets: dict) -> bytes:
    """A tar holding SECRETS_FILE's JSON, owned by the image's user (uid 10001) and readable by it only,
    for ``docker cp -``: serve.py reads it once and deletes it."""
    data = json.dumps(secrets).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        info = tarfile.TarInfo(os.path.basename(SECRETS_FILE))
        info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o600, IMAGE_UID, IMAGE_UID, int(time.time())
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def start_container(kwargs: dict, quiet=False) -> dict:
    """Start the image and wait for its CDP endpoint -> {"id", "image", "endpoint", "exe"}. Nothing is
    left running when this raises.

    The container is created with --rm and stops itself once no CDP client has been connected for
    idle_exit_seconds() (serve.py's CC_IDLE_EXIT_SECONDS), so one whose owner died -- Ctrl-C, a kill, a
    crash -- gives its licence seat back and disappears on its own; the next launch on this machine also
    sweeps any that are still there (sweep_stale). It carries the owner's host name and pid as labels for
    that sweep. The licence key and the proxy URL are copied in as a file rather than passed with -e, so
    they are not part of the container's configuration (`docker inspect`)."""
    env = container_env(kwargs)
    exe = check_docker()
    sweep_stale(exe)
    image = kwargs.get("docker_image") or default_image()
    secrets = {k: env.pop(k) for k in SECRET_ENV if k in env}
    idle = idle_exit_seconds()
    if idle:
        env["CC_IDLE_EXIT_SECONDS"] = str(idle)
    if secrets:
        env["CC_SECRETS_FILE"] = SECRETS_FILE
    argv = [exe, "create", "--rm", "--platform", "linux/amd64", "--shm-size", "1g", "-p", "127.0.0.1::9222",
            "--label", LABEL, "--label", f"{OWNER_HOST_LABEL}={socket.gethostname()}",
            "--label", f"{OWNER_PID_LABEL}={os.getpid()}"]
    for name in sorted(env):
        argv += ["-e", name]  # the value comes from the CLI's environment, never its command line
    if "CLEARCOTE_LICENSE_KEY" in secrets:  # the licensed engine is fetched once into this volume
        argv += ["-v", f"{cache_volume()}:/opt/xdg-cache"]
    argv.append(image)
    if not quiet:
        sys.stderr.write(f"[clearcote] no native macOS build: starting the Clearcote Docker image {image} "
                         "(the first run downloads it)\n")
        sys.stderr.flush()
    for attempt in range(4):
        code, out, err = _run(argv, env=env, timeout=1800)
        # Two launches creating their first container on a NEW shared volume at once collide in Docker's
        # own volume initialisation ("failed to mkdir .../volumes/<name>/_data/...: file exists"); the
        # loser succeeds a moment later, once the volume has been filled.
        if code == 0 or attempt == 3 or not _VOLUME_INIT_RACE.search(err or ""):
            break
        time.sleep(1.0 + attempt)
    cid = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
    if code != 0 or not cid:
        raise _create_failed(image, code, err)
    with _LIVE_LOCK:
        _LIVE[cid] = exe
    logs = None
    try:
        if secrets:
            code, _o, err = _run([exe, "cp", "-", f"{cid}:{os.path.dirname(SECRETS_FILE)}"], timeout=60,
                                 input=secrets_tar(secrets))
            if code != 0:
                raise RuntimeError(f"could not copy the licence/proxy settings into the container: {_first_line(err)}")
        code, _o, err = _run([exe, "start", cid], timeout=120)
        if code != 0:
            raise RuntimeError(f"`docker start` failed: {(err or '').strip() or f'exit {code}'}")
        logs = _follow_logs(exe, cid)
        port = _published_port(exe, cid)
        timeout_ms = kwargs.get("timeout")
        _wait_ready(exe, cid, port, _READY_TIMEOUT_S if not timeout_ms else max(timeout_ms / 1000.0, 1.0), logs)
    except BaseException:
        _remove(exe, cid)
        raise
    finally:
        if logs is not None:
            logs.stop()
    return {"id": cid, "image": image, "endpoint": f"http://127.0.0.1:{port}", "exe": exe}


def _connect_options(kwargs):
    connect = {"timeout": _CONNECT_TIMEOUT_MS}
    connect.update({k: kwargs[k] for k in ("timeout", "slow_mo") if kwargs.get(k) is not None})
    return connect


def _info(container):
    return {k: container[k] for k in ("id", "image", "endpoint")}


def launch_docker(kwargs: dict):
    """``clearcote.launch()`` on macOS (see the module docstring): the Playwright Browser of a Clearcote
    container. ``close()`` disconnects and stops the container."""
    from . import _install_headed_viewport, _playwright
    from ._humanize import install_humanize, install_humanize_on_context

    container = start_container(kwargs, quiet=kwargs.get("quiet", False))
    disconnect = None
    try:
        browser = _playwright().chromium.connect_over_cdp(container["endpoint"], **_connect_options(kwargs))
        disconnect = browser.close
        browser.docker_container = _info(container)

        def close(*args, **kw):
            try:
                return disconnect(*args, **kw)
            finally:
                _remove(container["exe"], container["id"])

        browser.close = close
        # The window is real (on the image's virtual display, sized to the persona's screen): an
        # emulated 1280x720 viewport on top of it is the impossible-window tell a local launch avoids.
        _install_headed_viewport(browser)
        seed = kwargs.get("fingerprint")
        humanize, show_cursor = kwargs.get("humanize", False), kwargs.get("show_cursor", False)
        for ctx in browser.contexts:
            install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        install_humanize(browser, humanize, show_cursor, seed=seed)
        return browser
    except BaseException:
        if disconnect is not None:
            try:
                disconnect()
            except Exception:  # noqa: BLE001, S110 -- the setup error is the one worth raising
                pass
        _remove(container["exe"], container["id"])
        raise


async def launch_docker_async(kwargs: dict):
    """Async twin of :func:`launch_docker` (``clearcote.async_api.launch()`` on macOS)."""
    import asyncio

    from ._humanize_async import install_humanize, install_humanize_on_context
    from .async_api import _bind_driver, _install_headed_viewport, _start_driver

    container = await asyncio.to_thread(start_container, kwargs, kwargs.get("quiet", False))
    pw = disconnect = None
    try:
        pw = await _start_driver()
        browser = await pw.chromium.connect_over_cdp(container["endpoint"], **_connect_options(kwargs))
        disconnect = browser.close
        _bind_driver(browser, pw)  # close() also stops this browser's own Playwright driver
        browser.docker_container = _info(container)
        orig_close = browser.close

        async def close(*args, **kw):
            try:
                return await orig_close(*args, **kw)
            finally:
                await asyncio.to_thread(_remove, container["exe"], container["id"])

        browser.close = close
        _install_headed_viewport(browser)
        seed = kwargs.get("fingerprint")
        humanize, show_cursor = kwargs.get("humanize", False), kwargs.get("show_cursor", False)
        for ctx in browser.contexts:
            await install_humanize_on_context(ctx, humanize, show_cursor, browser, seed)
        await install_humanize(browser, humanize, show_cursor, seed=seed)
        return browser
    except BaseException:
        for step in (disconnect, pw.stop if pw is not None else None):
            try:
                if step is not None:
                    await step()
            except Exception:  # noqa: BLE001, S110
                pass
        await asyncio.to_thread(_remove, container["exe"], container["id"])
        raise
