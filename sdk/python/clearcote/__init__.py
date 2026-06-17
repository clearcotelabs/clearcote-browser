"""Clearcote — Playwright drop-in (Python).

    from clearcote import launch

    browser = launch(fingerprint="seed-123", platform="windows")
    page = browser.new_page()
    page.goto("https://abrahamjuliot.github.io/creepjs/")
    browser.close()

launch() returns a standard Playwright sync ``Browser`` backed by the verified Clearcote
binary (auto-downloaded + SHA-256 checked on first use, then cached). Every Playwright launch
option (headless, proxy, args, timeout, ...) passes through; the fingerprint kwargs map to the
engine switches.
"""

import atexit
import os
import sys

from ._fingerprint import FINGERPRINT_KEYS, fingerprint_args
from .download import ensure_binary
from .geoip import resolve_geo
from .release import RELEASE

__all__ = [
    "launch",
    "launch_persistent_context",
    "executable_path",
    "download",
    "resolve_geo",
    "RELEASE",
    "__version__",
]
__version__ = "0.2.0"

_pw = None  # the shared, lazily-started Playwright driver (one per process)


def _stop_quietly(pw):
    try:
        pw.stop()
    except Exception:  # noqa: BLE001
        pass


def _playwright():
    global _pw
    if _pw is None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "clearcote requires Playwright. Install it with:\n    pip install playwright\n"
                "(You do NOT need 'playwright install' — Clearcote uses its own browser binary.)"
            ) from exc
        _pw = sync_playwright().start()
        atexit.register(_stop_quietly, _pw)
    return _pw


def _resolve_binary(executable_path=None, cache_dir=None, quiet=False):
    if executable_path:
        return executable_path
    env = os.environ.get("CLEARCOTE_BINARY")
    if env:
        return env
    return ensure_binary(cache_dir=cache_dir, quiet=quiet)


def executable_path(executable_path=None, cache_dir=None, quiet=False):
    """Resolve the Clearcote chrome.exe path, downloading + verifying it if needed.

    Order: explicit ``executable_path`` > ``CLEARCOTE_BINARY`` env > auto-download.
    """
    return _resolve_binary(executable_path, cache_dir, quiet)


def download(cache_dir=None, quiet=False):
    """Pre-fetch + verify the Clearcote binary without launching. Returns the chrome.exe path."""
    return ensure_binary(cache_dir=cache_dir, quiet=quiet)


def _guard(exe):
    if sys.platform != "win32":
        raise RuntimeError(
            f"Clearcote {RELEASE['version']} ships a Windows x64 binary only — it cannot launch "
            f"on {sys.platform!r}.\nRun on Windows, or pass executable_path=... to a compatible "
            f"binary.\n(The binary downloaded and verified fine; it is cached at: {exe})"
        )


def _prepare(kwargs):
    geoip = kwargs.pop("geoip", False)
    fp = {k: kwargs.pop(k) for k in list(kwargs) if k in FINGERPRINT_KEYS}
    exe_path = kwargs.pop("executable_path", None)
    extra_args = kwargs.pop("args", None)
    cache_dir = kwargs.pop("cache_dir", None)
    quiet = kwargs.pop("quiet", False)
    if geoip:
        # resolve the proxy's exit-IP geo and fill any UNSET timezone/accept_language/location/webrtc_ip
        geo = resolve_geo(kwargs.get("proxy"), quiet=quiet)
        if geo:
            for opt in ("timezone", "accept_language", "location"):
                if geo.get(opt) and fp.get(opt) is None:
                    fp[opt] = geo[opt]
            # make WebRTC report the proxy egress IP too, coherent with HTTP egress (engine
            # fabricates the srflx candidate at this IP; no real STUN leaves the host).
            if geo.get("ip") and fp.get("webrtc_ip") is None:
                fp["webrtc_ip"] = geo["ip"]
    exe = _resolve_binary(exe_path, cache_dir, quiet)
    _guard(exe)
    args = fingerprint_args(fp) + list(extra_args or [])
    return exe, args, kwargs


def launch(**kwargs):
    """Launch Clearcote and return a standard Playwright sync ``Browser``.

    Fingerprint kwargs: fingerprint, platform, platform_version, brand, brand_version,
    gpu_vendor, gpu_renderer, hardware_concurrency, location, timezone, accept_language,
    webrtc_ip, disable_gpu_fingerprint. Pass geoip=True to resolve the proxy's exit-IP geo and
    auto-fill any unset timezone/accept_language/location. All other kwargs (headless, proxy,
    args, timeout, ...) pass through to Playwright's chromium.launch().
    """
    exe, args, pw_kwargs = _prepare(kwargs)
    return _playwright().chromium.launch(executable_path=exe, args=args, **pw_kwargs)


def launch_persistent_context(user_data_dir, **kwargs):
    """Launch Clearcote with a persistent profile directory; returns a Playwright
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``)."""
    exe, args, pw_kwargs = _prepare(kwargs)
    return _playwright().chromium.launch_persistent_context(
        user_data_dir, executable_path=exe, args=args, **pw_kwargs
    )
