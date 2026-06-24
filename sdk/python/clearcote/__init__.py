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

from ._agent import AGENT_KEYS, OPENROUTER_BASE_URL, agent_args, run_agent_task
from ._fingerprint import FINGERPRINT_KEYS, fingerprint_args
from ._humanize import install_humanize, install_humanize_on_context
from ._launchopts import (
    extension_args,
    merge_feature_flags,
    privacy_sandbox_args,
    resolve_proxy,
    webrtc_default_deny_args,
)
from ._profile import Profile, list_profiles, load_profile, resolve_profile_options
from .download import ensure_binary
from .geoip import resolve_geo
from .release import RELEASE

__all__ = [
    "launch",
    "launch_persistent_context",
    "launch_agent",
    "executable_path",
    "download",
    "run_agent_task",
    "resolve_geo",
    "Profile",
    "list_profiles",
    "load_profile",
    "OPENROUTER_BASE_URL",
    "RELEASE",
    "__version__",
]
__version__ = "0.9.1"

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


def _resolve_binary(executable_path=None, cache_dir=None, quiet=False, auto_update=None):
    if executable_path:
        return executable_path
    env = os.environ.get("CLEARCOTE_BINARY")
    if env:
        return env
    return ensure_binary(cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)


def executable_path(executable_path=None, cache_dir=None, quiet=False, auto_update=None):
    """Resolve the Clearcote chrome.exe path, downloading + verifying it if needed.

    Order: explicit ``executable_path`` > ``CLEARCOTE_BINARY`` env > auto-download.
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    """
    return _resolve_binary(executable_path, cache_dir, quiet, auto_update)


def download(cache_dir=None, quiet=False, auto_update=None):
    """Pre-fetch + verify the Clearcote binary without launching. Returns the chrome.exe path.

    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    """
    return ensure_binary(cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)


def _guard(exe):
    if sys.platform != "win32":
        raise RuntimeError(
            f"Clearcote {RELEASE['version']} ships a Windows x64 binary only — it cannot launch "
            f"on {sys.platform!r}.\nRun on Windows, or pass executable_path=... to a compatible "
            f"binary.\n(The binary downloaded and verified fine; it is cached at: {exe})"
        )


def _prepare(kwargs):
    # profile= a saved persona (name, path, or Profile): its options are the base layer;
    # explicit kwargs passed to launch() override them.
    profile = kwargs.pop("profile", None)
    if profile is not None:
        for key, value in resolve_profile_options(profile).items():
            kwargs.setdefault(key, value)
    geoip = kwargs.pop("geoip", False)
    humanize = kwargs.pop("humanize", False)
    show_cursor = kwargs.pop("show_cursor", False)
    fp = {k: kwargs.pop(k) for k in list(kwargs) if k in FINGERPRINT_KEYS}
    agent = {k: kwargs.pop(k) for k in list(kwargs) if k in AGENT_KEYS}
    exe_path = kwargs.pop("executable_path", None)
    extra_args = kwargs.pop("args", None)
    extensions = kwargs.pop("extensions", None)
    # de-Googled-coherence default: disable Privacy Sandbox + intrusive APIs (Topics/FLEDGE/WebUSB/
    # etc). Pass disable_privacy_sandbox=False to keep them.
    disable_privacy_sandbox = kwargs.pop("disable_privacy_sandbox", True)
    cache_dir = kwargs.pop("cache_dir", None)
    quiet = kwargs.pop("quiet", False)
    auto_update = kwargs.pop("auto_update", None)
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
    exe = _resolve_binary(exe_path, cache_dir, quiet, auto_update)
    _guard(exe)
    # SOCKS5-with-credentials must go through --proxy-server (Playwright rejects creds in its SOCKS
    # proxy descriptor); resolve_proxy returns proxy=None for that case so we drop it from Playwright.
    proxy_args, proxy = resolve_proxy(kwargs.get("proxy"))
    if proxy is None:
        kwargs.pop("proxy", None)
    else:
        kwargs["proxy"] = proxy
    base = fingerprint_args(fp) + agent_args(agent) + extension_args(extensions) + proxy_args
    if disable_privacy_sandbox:
        base += privacy_sandbox_args()
    user = list(extra_args or [])
    # default WebRTC to leak-proof unless the user wired a webrtc_ip / policy themselves
    base += webrtc_default_deny_args(base + user, fp.get("webrtc_ip"))
    # collapse all --enable-features/--disable-features (ours + the user's) into one of each, else
    # Chromium keeps only the last occurrence and the rest are silently dropped.
    args = merge_feature_flags(base + user)
    return exe, args, kwargs, humanize, show_cursor


def _headed_no_viewport(pw_kwargs):
    """A headed launch with Playwright's default emulated viewport (1280x720) sitting on the real
    OS window makes window.innerWidth/Height disagree with the actual window — an impossible-window
    tell that defeats the engine's coherence. True when headed and no viewport was requested, so we
    default new pages/contexts to no_viewport (innerWidth then tracks the real window)."""
    return (pw_kwargs.get("headless") is False
            and "viewport" not in pw_kwargs and "no_viewport" not in pw_kwargs)


def _install_headed_viewport(browser):
    """Default a headed browser's new pages/contexts to no_viewport (unless the caller sets one)."""
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    def new_page(**kw):
        if "viewport" not in kw and "no_viewport" not in kw:
            kw["no_viewport"] = True
        return orig_new_page(**kw)

    def new_context(**kw):
        if "viewport" not in kw and "no_viewport" not in kw:
            kw["no_viewport"] = True
        return orig_new_context(**kw)

    browser.new_page, browser.new_context = new_page, new_context


def launch(**kwargs):
    """Launch Clearcote and return a standard Playwright sync ``Browser``.

    Fingerprint kwargs: fingerprint, platform, platform_version, brand, brand_version,
    gpu_vendor, gpu_renderer, hardware_concurrency, location, timezone, accept_language,
    webrtc_ip, disable_gpu_fingerprint. Pass geoip=True to resolve the proxy's exit-IP geo and
    auto-fill any unset timezone/accept_language/location. All other kwargs (headless, proxy,
    args, timeout, ...) pass through to Playwright's chromium.launch().
    """
    exe, args, pw_kwargs, humanize, show_cursor = _prepare(kwargs)
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    browser = _playwright().chromium.launch(executable_path=exe, args=args, **pw_kwargs)
    if headed:
        _install_headed_viewport(browser)
    install_humanize(browser, humanize, show_cursor)
    return browser


def launch_persistent_context(user_data_dir, **kwargs):
    """Launch Clearcote with a persistent profile directory; returns a Playwright
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``)."""
    exe, args, pw_kwargs, humanize, show_cursor = _prepare(kwargs)
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    context = _playwright().chromium.launch_persistent_context(
        user_data_dir, executable_path=exe, args=args, **pw_kwargs
    )
    install_humanize_on_context(context, humanize, show_cursor)
    return context


def launch_agent(user_data_dir=None, **kwargs):
    """Launch Clearcote ready for the in-browser AI agent; returns a Playwright ``BrowserContext``.

    The agent drives Chrome's Actor framework, which only attaches to a REGULAR profile (not
    incognito), so this uses a persistent context (a fresh temp ``user_data_dir`` unless you pass
    one). Set ``agent_llm_key`` (+ optional ``agent_model``), then drive a page with
    ``run_agent_task()``. Use this (or ``launch_persistent_context``) for the agent -- plain
    ``launch()`` is incognito, where the Actor framework can't attach the tab."""
    import tempfile

    if user_data_dir is None:
        user_data_dir = tempfile.mkdtemp(prefix="clearcote-agent-")
    return launch_persistent_context(user_data_dir, **kwargs)
