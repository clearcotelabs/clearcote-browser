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
import time

from ._agent import AGENT_KEYS, OPENROUTER_BASE_URL, agent_args, run_agent_task
from ._fingerprint import FINGERPRINT_KEYS, fingerprint_args
from ._fonts import apply_font_env
from ._humanize import install_humanize, install_humanize_on_context
from ._launchopts import (
    extension_args,
    merge_feature_flags,
    privacy_sandbox_args,
    quic_args,
    resolve_proxy,
    webrtc_default_deny_args,
)
from ._profile import Profile, list_profiles, load_profile, resolve_profile_options
from ._render import check_render_coherence
from ._warnings import emit_coherence_warnings
from ._widevine import apply_widevine_launch, fetch_widevine, seed_widevine
from ._license import (
    ConcurrencyLimitError,
    LicenseError,
    LicenseRevokedError,
    acquire_lease,
    inject_run_token,
    resolve_license_key,
)
from .download import ensure_binary, resolved_engine_version, warm_files
from .geoip import resolve_geo
from .release import RELEASE
from ._serve import Server, serve

__all__ = [
    "launch",
    "launch_persistent_context",
    "launch_agent",
    "serve",
    "Server",
    "executable_path",
    "download",
    "run_agent_task",
    "resolve_geo",
    "Profile",
    "list_profiles",
    "load_profile",
    "check_render_coherence",
    "fetch_widevine",
    "seed_widevine",
    "resolve_license_key",
    "acquire_lease",
    "LicenseError",
    "ConcurrencyLimitError",
    "LicenseRevokedError",
    "OPENROUTER_BASE_URL",
    "RELEASE",
    "__version__",
]
__version__ = "0.17.1"

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


def _resolve_binary(executable_path=None, cache_dir=None, quiet=False, auto_update=None, pro=None,
                    version=None):
    if executable_path:
        return executable_path
    env = os.environ.get("CLEARCOTE_BINARY")
    if env:
        return env
    version = version or os.environ.get("CLEARCOTE_BROWSER_VERSION")
    if version:
        # Explicit version selector ("150" / "149.0.7827.114" / "latest"): validate against the public
        # catalog FIRST (clear error if it doesn't exist or needs a license), then route free vs pro.
        from .download import (
            _cache_root,
            _fetch_and_verify,
            _find,
            pro_ensure_binary,
            resolve_version,
        )

        kind, payload = resolve_version(version, has_license=bool(pro and pro[0]), quiet=quiet)
        if kind == "pro":
            return pro_ensure_binary(pro[0], api_base=(pro[1] if pro else None),
                                     cache_dir=cache_dir, quiet=quiet, version=payload)
        rel = payload  # free build resolved from the catalog
        base = os.path.join(cache_dir or _cache_root(), rel["tag"])
        if os.path.exists(os.path.join(base, ".verified")):
            cached = _find(os.path.join(base, "browser"), rel["binary"])
            if cached:
                return cached
        return _fetch_and_verify(rel, base, quiet)
    if pro:  # (license_key, api_base) -> the PRO (license-gated) pinned build via the site
        from .download import pro_ensure_binary
        return pro_ensure_binary(pro[0], api_base=pro[1], cache_dir=cache_dir, quiet=quiet)
    return ensure_binary(cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)


def executable_path(executable_path=None, cache_dir=None, quiet=False, auto_update=None,
                    version=None, license_key=None, license_api_base=None):
    """Resolve the Clearcote chrome.exe path, downloading + verifying it if needed.

    Order: explicit ``executable_path`` > ``CLEARCOTE_BINARY`` env > ``version`` selector > auto-download.
    Pass ``version="150"`` (major), ``"150.0.7871.115"`` (exact), or ``"latest"`` to pick a specific
    browser build from the catalog (a PRO-tier version needs ``license_key`` / ``CLEARCOTE_LICENSE_KEY``).
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    """
    key = resolve_license_key(license_key)
    pro = (key, license_api_base) if key else None
    return _resolve_binary(executable_path, cache_dir, quiet, auto_update, pro=pro, version=version)


def download(cache_dir=None, quiet=False, auto_update=None, version=None, license_key=None,
             license_api_base=None):
    """Pre-fetch + verify the Clearcote binary without launching. Returns the chrome.exe path.

    Pass ``version="150"`` / ``"150.0.7871.115"`` / ``"latest"`` to fetch a specific browser build
    from the catalog (PRO-tier versions need ``license_key`` / ``CLEARCOTE_LICENSE_KEY``).
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    """
    key = resolve_license_key(license_key)
    pro = (key, license_api_base) if key else None
    return _resolve_binary(None, cache_dir, quiet, auto_update, pro=pro, version=version)


def _guard(exe):
    from .release import platform_release
    if platform_release() is None:
        raise RuntimeError(
            f"Clearcote {RELEASE['version']} ships Windows x64 and Linux x64 binaries — there is no "
            f"build for {sys.platform!r}.\nRun on Windows or Linux, or pass executable_path=... to a "
            f"compatible binary.\n(A binary downloaded and verified fine; it is cached at: {exe})"
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
    # widevine= is seeded into a persistent profile by launch_persistent_context; pop it here so it
    # never leaks to Playwright from launch()/the async path (incognito can't load the component CDM).
    kwargs.pop("widevine", None)
    fp = {k: kwargs.pop(k) for k in list(kwargs) if k in FINGERPRINT_KEYS}
    agent = {k: kwargs.pop(k) for k in list(kwargs) if k in AGENT_KEYS}
    exe_path = kwargs.pop("executable_path", None)
    _cc_pro = kwargs.pop("_cc_pro", None)  # (license_key, api_base) or None -> pick PRO vs free binary
    extra_args = kwargs.pop("args", None)
    extensions = kwargs.pop("extensions", None)
    # de-Googled-coherence default: disable Privacy Sandbox + intrusive APIs (Topics/FLEDGE/WebUSB/
    # etc). Pass disable_privacy_sandbox=False to keep them.
    disable_privacy_sandbox = kwargs.pop("disable_privacy_sandbox", True)
    cache_dir = kwargs.pop("cache_dir", None)
    quiet = kwargs.pop("quiet", False)
    auto_update = kwargs.pop("auto_update", None)
    version = kwargs.pop("version", None)  # browser major/version selector (catalog-resolved)
    proxy_opt = kwargs.get("proxy")  # captured before resolve_proxy rewrites it (for quic + warnings)
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
    exe = _resolve_binary(exe_path, cache_dir, quiet, auto_update, pro=_cc_pro, version=version)
    _guard(exe)
    # SOCKS5-with-credentials must go through --proxy-server (Playwright rejects creds in its SOCKS
    # proxy descriptor); resolve_proxy returns proxy=None for that case so we drop it from Playwright.
    proxy_args, proxy = resolve_proxy(kwargs.get("proxy"))
    if proxy is None:
        kwargs.pop("proxy", None)
    else:
        kwargs["proxy"] = proxy
    base = fingerprint_args(fp) + agent_args(agent) + extension_args(extensions) + proxy_args
    base += quic_args(proxy_opt)  # behind a proxy, disable QUIC so no HTTP/3 UDP egresses around it
    if disable_privacy_sandbox:
        base += privacy_sandbox_args()
    user = list(extra_args or [])
    # default WebRTC to leak-proof unless the user wired a webrtc_ip / policy themselves
    base += webrtc_default_deny_args(base + user, fp.get("webrtc_ip"))
    # collapse all --enable-features/--disable-features (ours + the user's) into one of each, else
    # Chromium keeps only the last occurrence and the rest are silently dropped.
    args = merge_feature_flags(base + user)
    # Drop Playwright's default automation flag so the engine's AutomationControlled feature stays
    # OFF (it otherwise flips navigator.webdriver-adjacent tells). The control transport
    # (--remote-debugging-pipe) is left intact. Caller can override via their own ignore_default_args.
    # NOTE: launch_persistent_context sets this BEFORE the Widevine helper so that helper appends
    # --disable-component-update rather than clobbering the automation strip.
    kwargs.setdefault("ignore_default_args", ["--enable-automation"])
    # Surface incoherent / missing-recommended option combos the SDK can't auto-fix (stderr; gated
    # by quiet / CLEARCOTE_NO_WARN). geoip may have just filled timezone/accept_language above.
    emit_coherence_warnings(
        {**fp, "proxy": proxy_opt, "geoip": geoip, "headless": kwargs.get("headless"),
         "_user_args": user},
        quiet=quiet, build_major=str(RELEASE["version"]).split(".")[0])
    # The motor-persona seed is the EFFECTIVE fingerprint (after the profile= merge above), i.e. the
    # same value that becomes --fingerprint — not the raw pre-merge kwarg. A profile-based launch
    # thus gets the profile's stable persona instead of a random one.
    return exe, args, kwargs, humanize, show_cursor, fp.get("fingerprint")


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


def _is_win_launch_race(exc):
    m = str(exc).lower()
    return "spawn unknown" in m or "side-by-side" in m or "side by side" in m


def _win_av_retry(do_launch, exe):
    """Launch via ``do_launch(exe_path)``, working around the Windows first-launch AV-scan race.

    A just-extracted, unsigned chrome.exe can fail with "spawn UNKNOWN" / "side-by-side
    configuration is incorrect" while real-time antivirus is still scanning chrome_elf.dll (the SxS
    assembly member the exe's manifest depends on). Worse, Windows caches that negative activation
    context against the *path*, so retrying the same path keeps failing. ``warm_files`` (in
    ``ensure_binary``) pre-scans to prevent it; here we (1) re-scan + back off + retry a couple
    times, then (2) as a last resort relaunch from a pristine copy on a fresh temp path, which
    always gets a clean SxS evaluation. Pass-through on non-Windows."""
    if sys.platform != "win32":
        return do_launch(exe)
    for i in range(3):
        try:
            return do_launch(exe)
        except Exception as exc:  # noqa: BLE001
            if not _is_win_launch_race(exc):
                raise
            warm_files(os.path.dirname(exe))
            time.sleep(0.8 * (i + 1))
    # The in-place SxS activation-context poison never clears; relaunch from a fresh copy.
    import shutil
    import tempfile

    recover = os.path.join(tempfile.mkdtemp(prefix="clearcote-recover-"), "browser")
    shutil.copytree(os.path.dirname(exe), recover)
    warm_files(recover)
    return do_launch(os.path.join(recover, os.path.basename(exe)))


def _acquire_lease_from_kwargs(kwargs):
    """Pop license kwargs and acquire a concurrency lease (opt-in; None in free mode).

    Uses kwargs.get for quiet (leave it for _prepare to pop). Injects nothing here —
    the caller injects CLEARCOTE_RUN_TOKEN into pw_kwargs after apply_font_env.
    """
    license_key = kwargs.pop("license_key", None)
    license_api_base = kwargs.pop("license_api_base", None)
    # Stash the effective license (explicit > env > file) so _prepare selects the
    # PRO (gated) binary with the SAME key: licensed run -> gated build, free -> public.
    key = resolve_license_key(license_key)
    kwargs["_cc_pro"] = (key, license_api_base) if key else None
    # Telemetry split: sdk_version = the SDK PACKAGE version; engine_version = the resolved browser
    # build (respecting version="150"/"latest"/exact). The engine resolve is deferred behind a lambda
    # so the catalog is only consulted on a cold checkout (not on every launch that reuses the token).
    version_sel = kwargs.get("version") or os.environ.get("CLEARCOTE_BROWSER_VERSION")
    return acquire_lease(
        license_key=license_key, api_base=license_api_base,
        sdk_version=__version__, quiet=kwargs.get("quiet", False),
        engine_version=lambda: resolved_engine_version(version_sel, has_license=bool(key)),
    )


def launch(**kwargs):
    """Launch Clearcote and return a standard Playwright sync ``Browser``.

    Fingerprint kwargs: fingerprint, platform, platform_version, brand, brand_version,
    gpu_vendor, gpu_renderer, hardware_concurrency, location, timezone, accept_language,
    webrtc_ip, disable_gpu_fingerprint. Pass geoip=True to resolve the proxy's exit-IP geo and
    auto-fill any unset timezone/accept_language/location. Pass license_key=... (or set
    CLEARCOTE_LICENSE_KEY) to check out a concurrency slot for the PRO engine. All other kwargs
    (headless, proxy, args, timeout, ...) pass through to Playwright's chromium.launch().
    """
    lease = _acquire_lease_from_kwargs(kwargs)  # opt-in; None in free mode
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare(kwargs)
    apply_font_env(exe, pw_kwargs)  # Linux: point FONTCONFIG_FILE at the bundled font clones
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    browser = _win_av_retry(
        lambda e: _playwright().chromium.launch(executable_path=e, args=args, **pw_kwargs), exe
    )
    if lease:  # release the concurrency slot when the browser closes
        browser.on("disconnected", lambda _b=None: lease.stop())
    if headed:
        _install_headed_viewport(browser)
    install_humanize(browser, humanize, show_cursor, seed=seed)
    return browser


def launch_persistent_context(user_data_dir, **kwargs):
    """Launch Clearcote with a persistent profile directory; returns a Playwright
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``).

    Pass ``widevine=True`` to seed + enable the (opt-in, user-fetched) Widevine CDM so DRM/EME works
    (``requestMediaKeySystemAccess('com.widevine.alpha')`` resolves) and the EME surface matches a
    real Chrome instead of being a no-Widevine tell."""
    # Set the automation strip BEFORE the Widevine helper so it appends --disable-component-update to
    # ['--enable-automation'] rather than replacing it (which would lose the AutomationControlled
    # strip on Widevine launches).
    kwargs.setdefault("ignore_default_args", ["--enable-automation"])
    if kwargs.get("widevine"):
        apply_widevine_launch(user_data_dir, kwargs, quiet=kwargs.get("quiet", False))
    lease = _acquire_lease_from_kwargs(kwargs)  # opt-in; None in free mode
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare(kwargs)
    apply_font_env(exe, pw_kwargs)  # Linux: point FONTCONFIG_FILE at the bundled font clones
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    context = _win_av_retry(
        lambda e: _playwright().chromium.launch_persistent_context(
            user_data_dir, executable_path=e, args=args, **pw_kwargs
        ),
        exe,
    )
    if lease:  # release the concurrency slot when the context closes
        context.on("close", lambda _c=None: lease.stop())
    install_humanize_on_context(context, humanize, show_cursor, seed=seed)
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
