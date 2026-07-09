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


class Server:
    """Handle for a standing clearcote CDP endpoint. Use ``.cdp_url`` with any CDP client."""

    def __init__(self, process, host, port, user_data_dir, own_udd):
        self.process = process
        self.host = host
        self.port = port
        self.user_data_dir = user_data_dir
        self._own_udd = own_udd
        self._closed = False

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

    def is_alive(self) -> bool:
        return self.process.poll() is None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.process.terminate()
            self.process.wait(timeout=10)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        if self._own_udd:
            shutil.rmtree(self.user_data_dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        self.close()


def serve(port=None, host="127.0.0.1", allow_origins=None, user_data_dir=None,
          headless=True, ready_timeout=30.0, quiet=False, **kwargs):
    """Launch clearcote and expose a raw CDP endpoint; return a :class:`Server`.

    ``port``          bound port (default: a free ephemeral port; pass 9222 for the conventional one).
    ``host``          bind address — keep it loopback (default 127.0.0.1) for stealth + safety.
    ``allow_origins`` CDP ``--remote-allow-origins`` value (default: the loopback origins only). Pass
                      ``"*"`` only for trusted local use.
    ``user_data_dir`` persistent profile dir (default: a fresh temp dir, removed on close).
    ``headless``      run headless (default True; pass False for a visible window).
    All other kwargs (fingerprint, platform, proxy, geoip, timezone, ...) are the persona options
    ``launch()`` accepts.
    """
    # Lazy import to avoid a circular import at module load (this module is imported by __init__).
    from . import _prepare, _win_av_retry
    from ._fonts import linux_font_env

    kwargs.pop("headless", None)  # serve() drives headless directly via --headless=new
    # Build the full stealth arg set exactly like launch() does (fingerprint + privacy-sandbox +
    # webrtc leak-proofing + proxy + feature-merge + geoip), then launch the binary ourselves.
    exe, args, pw_kwargs, _humanize, _show = _prepare(kwargs)

    port = int(port) if port else _free_port()
    own_udd = user_data_dir is None
    if own_udd:
        user_data_dir = tempfile.mkdtemp(prefix="clearcote-serve-")
    origins = allow_origins if allow_origins is not None else \
        "http://%s:%d,http://localhost:%d" % (host, port, port)

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

    env = dict(os.environ)
    env.update(linux_font_env(exe))  # Linux: FONTCONFIG_FILE -> bundled font clones (no-op elsewhere)

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
    if not ready:
        try:
            proc.kill()
        except Exception:
            pass
        if own_udd:
            shutil.rmtree(user_data_dir, ignore_errors=True)
        raise RuntimeError(
            "clearcote serve: CDP endpoint at http://%s:%d did not come up within %.0fs"
            % (host, port, ready_timeout))

    srv = Server(proc, host, port, user_data_dir, own_udd)
    atexit.register(srv.close)
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

