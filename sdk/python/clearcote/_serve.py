"""clearcote serve — launch the stealth engine with a RAW CDP endpoint that any existing
automation stack attaches to over ``connect_over_cdp`` **without changing its code**:

    from clearcote import serve
    srv = serve(fingerprint="seed-1", platform="windows")   # returns a Server handle
    # Playwright : p.chromium.connect_over_cdp(srv.cdp_url)
    # Puppeteer  : puppeteer.connect({ browserURL: srv.cdp_url })
    # browser-use / Crawl4AI / Stagehand : point the CDP endpoint at srv.cdp_url
    srv.close()

This is the "drop-in for the whole agent ecosystem" mode: unlike ``launch()`` (which spawns and
*owns* a Playwright browser), ``serve()`` leaves a standing browser you attach to.

WHY IT STAYS STEALTHY (the important part):
  * The binary is launched **directly** — NOT through Playwright/Puppeteer — so the
    ``--enable-automation`` flag those frameworks add is **never present**. That flag is what
    flips ``navigator.webdriver`` and the AutomationControlled tells; without it, webdriver stays
    ``false`` and the persona is intact.
  * Attaching a CDP client later adds **no launch flags** (you can't change flags on a running
    browser), so the persona you served with is exactly what the page sees.
  * The engine's ``Runtime.enable`` neutralization (patch 110) keeps the attached CDP client
    undetectable to the page — the classic "CDP is driving me" leak does not fire.
  * The debug port binds to **loopback** with an **origin allowlist**, so a web page cannot reach
    or hijack it.
Net: an ordinary Chrome to the page, an open CDP channel to your tools.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


#: Seconds close() gives the browser to exit after SIGTERM before it kills it.
_CLOSE_GRACE = 10.0


def _stop_browser(process, grace):
    """Stop a browser serve() started and wait for it to exit: SIGTERM (TerminateProcess on
    Windows), then SIGKILL once ``grace`` seconds have passed. True once it has exited."""
    for stop, timeout in ((process.terminate, grace), (process.kill, 5.0)):
        try:
            stop()
        except Exception:  # noqa: BLE001 -- already gone
            pass
        try:
            process.wait(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001 -- TimeoutExpired: still running
            pass
    return False


class Server:
    """Handle for a standing clearcote CDP endpoint. Use ``.cdp_url`` with any CDP client.

    close() WAITS FOR THE BROWSER TO EXIT, then removes what it leaves in the temp directory: the
    profile serve() made, and on Linux and macOS the browser's singleton-socket directory
    (org.chromium.Chromium.*), whichever profile it ran on. A browser stopped with a signal never
    removes that directory itself: every serve() + close() left one behind.
    """

    def __init__(self, process, host, port, user_data_dir, own_udd, lease=None, launch_token=None):
        from . import _browser_process_files

        self.process = process
        self.host = host
        self.port = port
        self.user_data_dir = user_data_dir
        self._own_udd = own_udd
        self._lease = lease  # licence handle, released in close(); None in free mode
        self._launch_token = launch_token  # (file, release) for CLEARCOTE_RUN_TOKEN_FILE, or None
        self._closed = False
        # Now, while the browser runs: one that shuts down cleanly unlinks it from the profile.
        self._socket_dir = _browser_process_files(user_data_dir)[1]

    @property
    def cdp_url(self) -> str:
        """The HTTP CDP base — pass to ``connect_over_cdp`` / ``puppeteer.connect({browserURL})``."""
        return "http://%s:%d" % (self.host, self.port)

    @property
    def ws_url(self):
        """The browser-level WebSocket URL (for clients that want ``connect({browserWSEndpoint})``)."""
        try:
            with urllib.request.urlopen(self.cdp_url + "/json/version", timeout=5) as r:
                return json.load(r).get("webSocketDebuggerUrl")
        except Exception:
            return None

    @property
    def pid(self):
        """OS process id of the browser."""
        return getattr(self.process, "pid", None)

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def close(self):
        """Stop the browser and wait for it to exit (SIGTERM, then SIGKILL after 10 s), release the
        licence slot, then remove the temp profile serve() made and the browser's socket directory."""
        if self._closed:
            return
        self._closed = True
        from . import _browser_process_files, _profile_dir_remover

        socket_dir = self._socket_dir or _browser_process_files(self.user_data_dir)[1]
        _stop_browser(self.process, _CLOSE_GRACE)
        if self._lease:
            try:
                self._lease.stop()  # release the concurrency slot back to the plan
            except Exception:  # noqa: BLE001 -- licence teardown must never break shutdown
                pass
        if self._launch_token:
            try:
                self._launch_token[1]()  # remove the run-token file
            except Exception:  # noqa: BLE001 -- best-effort; the file is in a temp dir
                pass
        if self._own_udd:
            # retried while the browser's handles outlive it (Windows); a directory that will not go
            # is left to the interpreter-exit sweep
            _profile_dir_remover(self.user_data_dir, socket_dir=socket_dir)()
        elif socket_dir:
            shutil.rmtree(socket_dir, ignore_errors=True)
        atexit.unregister(self.close)  # one per serve() would otherwise pile up

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


def serve(port=None, host="127.0.0.1", allow_origins=None, user_data_dir=None,
          headless=True, ready_timeout=30.0, quiet=False, window_size=None, **kwargs):
    """Launch clearcote and expose a raw CDP endpoint; return a :class:`Server`.

    ``port``          bound port (default: a free ephemeral port; pass 9222 for the conventional one).
    ``host``          bind address — keep it loopback (default 127.0.0.1) for stealth + safety.
    ``allow_origins`` CDP ``--remote-allow-origins`` value (default: the loopback origins only). Pass
                      ``"*"`` only for trusted local use.
    ``user_data_dir`` persistent profile dir (default: a fresh temp dir, removed on close).
    ``headless``      run headless (default True; pass False for a visible window).
    ``window_size``   headless: the outer window size, ``{"width", "height"}`` (or a pair) in CSS px,
                      clamped to the display's work area so the window can never be larger than its
                      screen. Default: the whole work area (a maximized window). Ignored when headed,
                      and when ``args`` carries a window or display switch (the caller then owns
                      geometry).
    All other kwargs (fingerprint, platform, proxy, geoip, timezone, ...) are the persona options
    ``launch()`` accepts.
    """
    # Lazy import to avoid a circular import at module load (this module is imported by __init__).
    from . import _acquire_lease_from_kwargs, _prepare_or_release, _win_av_retry
    from ._fonts import linux_font_env, linux_locale_env
    from ._geometry import fit_served_window, served_geometry, validate_window_size
    from ._launchopts import serve_needs_no_sandbox
    from ._warnings import emit_warnings, serve_exposure_warnings

    window_size = validate_window_size(window_size)
    light_stealth = bool(kwargs.get("light_stealth"))

    kwargs.pop("headless", None)  # serve() drives headless directly via --headless=new
    # ...so _prepare cannot see it: tell it explicitly (the GPU-blocklist rule depends on headed).
    # serve launches the binary directly, so Playwright's SwiftShader default is never added here.
    kwargs["_cc_headed"] = not headless
    # License (opt-in, inert in free mode). This MUST run before _prepare: it converts license_key
    # into the _cc_pro tuple _prepare needs to select the gated binary. Without it serve() silently
    # dropped the key and launched the FREE engine for a licensed caller -- and even had it resolved
    # the PRO engine, the gate would have refused to start without CLEARCOTE_RUN_TOKEN below.
    # launch()/launch_persistent_context() and Node's serve() have always done this.
    lease = _acquire_lease_from_kwargs(kwargs)
    # Build the full stealth arg set exactly like launch() does (fingerprint + privacy-sandbox +
    # webrtc leak-proofing + proxy + feature-merge + geoip), then launch the binary ourselves.
    exe, args, pw_kwargs, _humanize, _show, _seed = _prepare_or_release(kwargs, lease)

    port = int(port) if port else _free_port()
    own_udd = user_data_dir is None
    if own_udd:
        user_data_dir = tempfile.mkdtemp(prefix="clearcote-serve-")
    origins = allow_origins if allow_origins is not None else \
        "http://%s:%d,http://localhost:%d" % (host, port, port)
    # A non-loopback bind or a "*" origin list hands the browser to whoever can reach the port.
    emit_warnings(serve_exposure_warnings(host, origins), quiet=quiet)

    cdp = [
        "--remote-debugging-port=%d" % port,
        "--remote-debugging-address=%s" % host,
        "--remote-allow-origins=%s" % origins,
        "--user-data-dir=%s" % user_data_dir,
    ]
    if headless:
        cdp.append("--headless=new")
    # A Playwright proxy dict can't be applied without Playwright; forward the server as
    # --proxy-server for the direct launch (authed proxies: answer Fetch.authRequired over CDP,
    # or use launch()/launch_persistent_context() which handle proxy auth for you).
    prox = pw_kwargs.get("proxy")
    if isinstance(prox, dict) and prox.get("server"):
        cdp.append("--proxy-server=%s" % prox["server"])
    # Chromium refuses to start as root without --no-sandbox, and serve spawns the binary itself, so
    # Playwright's own --no-sandbox is missing: `clearcote serve` in a root container just timed out.
    if serve_needs_no_sandbox(sys.platform, getattr(os, "getuid", lambda: None)(), args):
        cdp.append("--no-sandbox")
    # Headless geometry for a raw endpoint: the display and window are set browser-wide, since no
    # client's context options or CDP overrides would reach every page (see _geometry).
    geometry = served_geometry(args, _seed, light_stealth=light_stealth, headless=headless)
    if geometry:
        cdp.extend(geometry["args"])

    launch_token = lease.bind_launch() if lease else None  # (file, release) or None; r23+ opt-in
    env = dict(os.environ)
    env.update(linux_font_env(exe))  # Linux: FONTCONFIG_FILE -> bundled font clones (no-op elsewhere)
    env.update(linux_locale_env(args))  # Linux: UI locale from --lang (no-op elsewhere)
    if lease and lease.token:
        # The PRO engine's gate reads this once at startup and exits if it is missing or invalid.
        env["CLEARCOTE_RUN_TOKEN"] = lease.token
        if launch_token:
            # A supporting engine (r23+) re-reads this file so revoke/check-in/over-limit stops a
            # running free browser. Older engines ignore it. Additive: the token above is unchanged.
            env["CLEARCOTE_RUN_TOKEN_FILE"] = launch_token[0]

    # Launched DIRECTLY (no automation framework) -> no --enable-automation -> webdriver stays false.
    # Wrap in _win_av_retry so a just-extracted binary survives the Windows SxS/AV first-launch race
    # ("spawn UNKNOWN"), same as launch(): warm + back off + retry, then recover from a fresh copy.
    proc = _win_av_retry(
        lambda e: subprocess.Popen([e] + args + cdp, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        exe)

    deadline = time.time() + ready_timeout
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            urllib.request.urlopen("http://%s:%d/json/version" % (host, port), timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.25)
    srv = Server(proc, host, port, user_data_dir, own_udd, lease=lease, launch_token=launch_token)
    if not ready:
        srv.close()  # the browser, the licence slot, the run-token file and the temp directories
        raise RuntimeError(
            "clearcote serve: CDP endpoint at http://%s:%d did not come up within %.0fs"
            % (host, port, ready_timeout))
    atexit.register(srv.close)
    # Before any client attaches: the window onto the work area (and, under a persona, the headless
    # display onto the persona's). Its own connection, closed again; never fails the launch.
    if geometry:
        fit_served_window(srv.ws_url, geometry["persona"], window_size)
    if not quiet:
        sys.stderr.write(
            "[clearcote] CDP endpoint ready: %s\n"
            "            attach any client: connect_over_cdp(%r) / puppeteer.connect({browserURL})\n"
            % (srv.cdp_url, srv.cdp_url))
    return srv


def _parse_proxy(s):
    """Parse ``[scheme://][user:pass@]host:port`` into Playwright's proxy dict."""
    from urllib.parse import urlparse
    u = urlparse(s if "://" in s else "http://" + s)
    server = "%s://%s%s" % (u.scheme or "http", u.hostname or "", ":%d" % u.port if u.port else "")
    d = {"server": server}
    if u.username:
        d["username"] = u.username
    if u.password:
        d["password"] = u.password
    return d


def _cli_main(argv=None):
    """``clearcote-serve`` — start a standing stealth CDP endpoint and block until Ctrl-C.
    Prints the CDP URL to stdout (scriptable); logs go to stderr."""
    import argparse

    ap = argparse.ArgumentParser(
        prog="clearcote-serve",
        description="Launch clearcote with a raw CDP endpoint any Playwright / Puppeteer / "
                    "browser-use / Crawl4AI client attaches to over connect_over_cdp. Stays "
                    "stealthy: launched directly (no --enable-automation), navigator.webdriver=false, "
                    "Runtime.enable neutralized, port bound to loopback with an origin allowlist.")
    ap.add_argument("--port", type=int, default=9222, help="CDP port (default 9222; use 0 for an ephemeral port)")
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default loopback — keep it local for safety)")
    ap.add_argument("--allow-origins", default=None,
                    help="--remote-allow-origins value (default: the loopback origins only; '*' for trusted local use)")
    ap.add_argument("--headed", action="store_true", help="show a visible window (default: headless)")
    ap.add_argument("--user-data-dir", default=None, help="persistent profile dir (default: a temp dir removed on exit)")
    ap.add_argument("--fingerprint", default=None, help="persona seed (stable identity across launches)")
    ap.add_argument("--platform", default=None, choices=["windows", "linux", "macos", "android"])
    ap.add_argument("--brand", default=None, help="Chrome | Edge | Opera | Vivaldi")
    ap.add_argument("--proxy", default=None, help="proxy, e.g. http://user:pass@host:port")
    ap.add_argument("--timezone", default=None, help="IANA timezone, e.g. America/New_York")
    ap.add_argument("--accept-language", dest="accept_language", default=None, help="e.g. en-US,en")
    ap.add_argument("--geoip", action="store_true", help="derive timezone/locale/WebRTC IP from the proxy exit IP")
    ap.add_argument("--executable", default=None, help="path to the clearcote chrome binary (optional; auto-downloaded)")
    a = ap.parse_args(argv)

    persona = {}
    for k in ("fingerprint", "platform", "brand", "timezone", "accept_language"):
        v = getattr(a, k)
        if v is not None:
            persona[k] = v
    if a.geoip:
        persona["geoip"] = True
    if a.executable:
        persona["executable_path"] = a.executable
    if a.proxy:
        persona["proxy"] = _parse_proxy(a.proxy)

    srv = serve(port=(a.port or None), host=a.host, allow_origins=a.allow_origins,
                user_data_dir=a.user_data_dir, headless=not a.headed, **persona)
    print(srv.cdp_url, flush=True)  # stdout: just the URL, so callers can capture it
    sys.stderr.write("[clearcote] serving — press Ctrl-C to stop.\n")
    try:
        while srv.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
    return 0

