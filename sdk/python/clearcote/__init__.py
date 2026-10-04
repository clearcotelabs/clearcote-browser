"""Clearcote — Playwright drop-in (Python).

    from clearcote import launch

    browser = launch(fingerprint="seed-123", platform="windows")
    page = browser.new_page()
    page.goto("https://abrahamjuliot.github.io/creepjs/")
    browser.close()

launch() returns a Playwright browser handle backed by the verified Clearcote binary
(auto-downloaded + SHA-256 checked on first use, then cached). Every Playwright launch option
(headless, proxy, args, timeout, ...) passes through; the fingerprint kwargs map to the engine
switches.

Since 0.23.0 it launches on a throwaway PROFILE directory rather than incognito, because
incognito cannot load the Widevine CDM and its absence is itself a fingerprint on a build
branded Google Chrome. The directory is deleted on close and on interpreter exit, so no state
survives the run. ``ephemeral_profile=False`` restores the old incognito launch.

Since 0.34.0 the same call can run the browser on Clearcote's servers instead: ``launch(cloud=True)``
(or ``CLEARCOTE_CLOUD=1`` in the environment) returns the same Playwright ``Browser``, connected to
a hosted session. :mod:`clearcote.cloud` has the rest of the hosted API (agent runs, profiles,
recordings, events, hand-off, webhooks).
"""

import atexit
import os
import sys
import time
import warnings

from ._agent import AGENT_KEYS, OPENROUTER_BASE_URL, agent_args, run_agent_task
from ._fingerprint import (
    FINGERPRINT_KEYS,
    fingerprint_args,
    is_fingerprint_passthrough,
    persona_platform,
)
from ._fontpersona import ensure_persona_fonts, font_reachability
from ._fonts import apply_font_env
from ._shaderdialect import apply_shader_dialect
from ._geometry import apply_headless_geometry, fit_window_to_work_area
from ._humanize import install_humanize, install_humanize_on_context
from ._launchopts import (  # noqa: F401  (web_bluetooth_args re-exported for tests)
    DEFAULT_IGNORED_ARGS,
    GATED_ENGINE_SWITCHES,
    engine_extras_args,
    engine_supports_switch,
    gate_engine_switches,
    gpu_blocklist_args,
    serve_needs_no_sandbox,
    extension_args,
    warn_unsupported_engine_options,
    portable_args,
    merge_feature_flags,
    privacy_sandbox_args,
    quic_args,
    socks5_udp_args,
    resolve_proxy,
    web_bluetooth_args,
    webrtc_default_deny_args,
)
from ._profile import Profile, list_profiles, load_profile, resolve_profile_options
# Profile library: real captured personas, selected for coherence with THIS host.
from ._profilelib import (
    DEFAULT_MAX_ENCODED, default_sticky_key, eligible, gpu_vendor_class,
    score_profile, select_profile,
)
from ._profileimport import import_directory, index_entry_from_profile, load_imported_profile
from ._profilesource import fetch_index, fetch_profile, host_os_family, measure_host
from ._profileauto import (
    DEFAULT_LOCAL_DIR, load_local_index, local_setup_hint, resolve_auto, resolve_local,
)
from ._render import check_render_coherence
from ._warnings import emit_coherence_warnings
from ._widevine import apply_widevine_launch, fetch_widevine, seed_widevine
from ._license import (
    STALE_TOKEN_REFUSAL,
    ConcurrencyLimitError,
    LicenseError,
    LicenseRevokedError,
    acquire_lease,
    get_session_seats,
    inject_run_token,
    license_key_path,
    license_key_source,
    license_through_proxy_requested,
    remove_license_key,
    resolve_license_key,
    save_license_key,
)
from ._net import proxied_request, to_proxy_spec
from .download import (
    ensure_binary, list_cached_builds, resolve_release_channel, resolved_engine_version, warm_files,
)
from .geoip import GeoipError, geoip_timeout_seconds, resolve_geo, resolve_geo_detailed, warn_on_egress_drift
from .release import RELEASE
from ._serve import Server, serve
from .cloud import (
    AsyncCloud, Cloud, CloudError, CloudTimeoutError, cloud_requested, launch_cloud, verify_webhook,
)

__all__ = [
    "launch",
    "Cloud",
    "AsyncCloud",
    "CloudError",
    "CloudTimeoutError",
    "verify_webhook",
    "launch_persistent_context",
    "launch_agent",
    "serve",
    "Server",
    "executable_path",
    "download",
    "run_agent_task",
    "resolve_geo",
    "resolve_geo_detailed",
    "geoip_timeout_seconds",
    "GeoipError",
    "proxied_request",
    "to_proxy_spec",
    "resolve_release_channel",
    "list_cached_builds",
    "is_fingerprint_passthrough",
    "DEFAULT_IGNORED_ARGS",
    "gpu_blocklist_args",
    "gate_engine_switches",
    "engine_supports_switch",
    "serve_multiplex",
    "get_session_seats",
    "save_license_key",
    "remove_license_key",
    "license_key_source",
    "license_key_path",
    "Profile",
    "list_profiles",
    "select_profile",
    "score_profile",
    "eligible",
    "gpu_vendor_class",
    "default_sticky_key",
    "DEFAULT_MAX_ENCODED",
    "import_directory",
    "index_entry_from_profile",
    "load_imported_profile",
    "fetch_index",
    "fetch_profile",
    "host_os_family",
    "measure_host",
    "resolve_auto",
    "resolve_local",
    "load_local_index",
    "local_setup_hint",
    "DEFAULT_LOCAL_DIR",
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
__version__ = "0.36.1"

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
                    version=None, release_channel=None):
    from .download import check_install

    if executable_path:
        # Caller-supplied tree (often a browser bundled into a packaged app): we did not install it,
        # so validate it here — a half-copied tree otherwise CHECK-crashes during browser startup.
        check_install(executable_path)
        return executable_path
    env = os.environ.get("CLEARCOTE_BINARY")
    if env:
        check_install(env)
        return env
    # Validated on every download path (a typo must never silently select a different build) and
    # passed to every PRO download, pinned or not; the server lets an exact pin override it.
    channel = resolve_release_channel(release_channel)
    version = version or os.environ.get("CLEARCOTE_BROWSER_VERSION")
    if version:
        # Explicit version selector ("150" / "149.0.7827.114" / "latest"): validate against the public
        # catalog FIRST (clear error if it doesn't exist or needs a license), then route free vs pro.
        from .download import (
            _cache_root,
            _cached,
            _fetch_and_verify,
            is_pro_revision_selector,
            pro_ensure_binary,
            resolve_version,
        )

        # A PRO revision pin ("r7" / "150.0.7871.114-r7") isn't in the public catalog — it's a
        # licensed rebuild. Route it straight to the PRO download (which resolves the revision).
        if is_pro_revision_selector(version):
            if not (pro and pro[0]):
                raise ValueError(
                    f"Clearcote {version!r} is a PRO revision — set a license key "
                    "(CLEARCOTE_LICENSE_KEY, or pass license_key=...) to pin it."
                )
            return pro_ensure_binary(pro[0], api_base=(pro[1] if pro else None),
                                     cache_dir=cache_dir, quiet=quiet, version=version,
                                     release_channel=channel)

        kind, payload = resolve_version(version, has_license=bool(pro and pro[0]), quiet=quiet)
        if kind == "pro":
            return pro_ensure_binary(pro[0], api_base=(pro[1] if pro else None),
                                     cache_dir=cache_dir, quiet=quiet, version=payload,
                                     release_channel=channel)
        rel = payload  # free build resolved from the catalog
        base = os.path.join(cache_dir or _cache_root(), rel["tag"])
        cached = _cached(base, rel["binary"], quiet)
        if cached:
            return cached
        return _fetch_and_verify(rel, base, quiet)
    if pro:  # (license_key, api_base) -> the PRO (license-gated) pinned build via the site
        from .download import pro_ensure_binary
        return pro_ensure_binary(pro[0], api_base=pro[1], cache_dir=cache_dir, quiet=quiet,
                                 release_channel=channel)
    return ensure_binary(cache_dir=cache_dir, quiet=quiet, auto_update=auto_update)


def executable_path(executable_path=None, cache_dir=None, quiet=False, auto_update=None,
                    version=None, license_key=None, license_api_base=None, release_channel=None):
    """Resolve the Clearcote chrome.exe path, downloading + verifying it if needed.

    Order: explicit ``executable_path`` > ``CLEARCOTE_BINARY`` env > ``version`` selector > auto-download.
    Pass ``version="150"`` (major), ``"150.0.7871.115"`` (exact), or ``"latest"`` to pick a specific
    browser build from the catalog (a PRO-tier version needs ``license_key`` / ``CLEARCOTE_LICENSE_KEY``).
    Pin a specific PRO rebuild with ``version="150.0.7871.114-r7"`` (or bare ``"r7"``) — revisions are
    licensed builds, so a key is required.
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    ``release_channel="preview"`` (or ``CLEARCOTE_RELEASE_CHANNEL``) selects the newest PRO build,
    preview or stable.
    """
    key = resolve_license_key(license_key)
    pro = (key, license_api_base) if key else None
    return _resolve_binary(executable_path, cache_dir, quiet, auto_update, pro=pro, version=version,
                           release_channel=release_channel)


def download(cache_dir=None, quiet=False, auto_update=None, version=None, license_key=None,
             license_api_base=None, release_channel=None):
    """Pre-fetch + verify the Clearcote binary without launching. Returns the chrome.exe path.

    Pass ``version="150"`` / ``"150.0.7871.115"`` / ``"latest"`` to fetch a specific browser build
    from the catalog (PRO-tier versions need ``license_key`` / ``CLEARCOTE_LICENSE_KEY``). A PRO
    rebuild can be pinned with ``version="150.0.7871.114-r7"`` (or bare ``"r7"``).
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    ``release_channel="preview"`` (or ``CLEARCOTE_RELEASE_CHANNEL``) selects the newest PRO build.
    """
    key = resolve_license_key(license_key)
    pro = (key, license_api_base) if key else None
    return _resolve_binary(None, cache_dir, quiet, auto_update, pro=pro, version=version,
                           release_channel=release_channel)


def _guard(exe):
    from .release import platform_release
    if platform_release() is None:
        raise RuntimeError(
            f"Clearcote {RELEASE['version']} ships Windows x64 and Linux x64 binaries — there is no "
            f"build for {sys.platform!r}.\nRun on Windows or Linux, or pass executable_path=... to a "
            f"compatible binary.\n(A binary downloaded and verified fine; it is cached at: {exe})"
        )


def _apply_auto_profile(fp, exe, select, quiet=False, pro=None, lease=None):
    """Resolve ``profile="auto"`` into ``fingerprint_profile``, in place.

    Host GPU/display can only be read by rendering, so this may launch the engine once with NO
    persona and cache the result (keyed by binary, 30 days). The nested launch passes no
    ``profile``, so it cannot recurse.

    An explicit ``fingerprint_profile`` always wins — if the caller already named a profile,
    "auto" has nothing to decide and must not silently replace it.
    """
    if fp.get("fingerprint_profile") is not None:
        return
    major = int(str(RELEASE["version"]).split(".")[0])
    license_key = pro[0] if pro else None
    api_base = pro[1] if pro else None
    # THE NESTED LAUNCH NEEDS THE LICENSE TOO, and used to be given it only by accident.
    #
    # measure_host launches the SAME binary that will run the real session. On PRO that binary is
    # the gated build: with no run-token the engine gate kills it on startup and Playwright reports
    # `TargetClosedError: Target page, context or browser has been closed` — a message that says
    # nothing about licensing, from a call the caller never wrote. It only worked when the key
    # happened to be in CLEARCOTE_LICENSE_KEY (or ~/.clearcote/license.key), because launch()
    # resolves those itself; passing license_key= as a kwarg — the documented way — failed.
    #
    # `pro` is unpacked ABOVE the call for that reason. Do not move it back down.
    # ephemeral_profile=False: the host probe reads GPU + display off about:blank and needs no
    # profile, so it takes the cheap incognito path rather than creating and deleting a directory
    # on every "auto" resolution.
    # _cc_lease: run the probe on the CALLER's slot. Checking out a second one deadlocks the
    # caller against itself on a per-browser plan, and the probe is strictly inside this launch.
    # cloud=False: the probe measures THIS host, whatever CLEARCOTE_CLOUD says.
    host = measure_host(
        lambda **kw: launch(
            license_key=license_key, license_api_base=api_base, ephemeral_profile=False,
            _cc_lease=lease, cloud=False, **kw
        ),
        exe,
        major,
    )
    result = resolve_auto(host, license_key=license_key, api_base=api_base, quiet=quiet, **select)
    fp["fingerprint_profile"] = result["profile"]
    # A seed alongside a profile is the combination that fails strict scoring, and it also makes
    # profile fields apply only partially. "auto" therefore never sets one — and says so if the
    # caller supplied one, rather than silently doing something other than what was asked.
    if fp.get("fingerprint") is not None and not quiet:
        sys.stderr.write(
            '[clearcote] [profile] warning: profile="auto" with an explicit fingerprint seed — '
            "the seed engages farbling, which strict anti-bots score as tampering and which "
            "makes profile fields apply only partially. Drop `fingerprint` for the coherent "
            "path.\n"
        )


def apply_geoip(fp, proxy, quiet=False):
    """Fill unset timezone/accept_language/location/webrtc_ip on ``fp`` from the proxy's exit-IP geo.

    FAILS CLOSED: if the region cannot be resolved, raises :class:`GeoipError` instead of
    continuing on the host's clock and a default language (UTC + en-US on most servers) -- the exact
    mismatch geoip exists to prevent. A caller who set BOTH ``timezone`` and ``accept_language``
    explicitly still launches (with a warning), since nothing geoip would fill is missing."""
    geo, reason, _ms = resolve_geo_detailed(proxy, quiet=quiet)
    if not geo or not geo.get("timezone"):
        if fp.get("timezone") and fp.get("accept_language"):
            if not quiet:
                warnings.warn(f"clearcote: geoip could not resolve the region ({reason}); using the "
                              "explicit timezone and accept_language.", stacklevel=3)
            return
        whose = "proxy's" if proxy else "connection's"
        raise GeoipError(
            f"geoip: could not resolve the {whose} region ({reason}). Launching anyway would use "
            "this machine's clock and a default language. Fix the proxy, raise "
            "CLEARCOTE_GEOIP_TIMEOUT_SECONDS, or pass timezone and accept_language explicitly.")
    for opt in ("timezone", "accept_language", "location"):
        if geo.get(opt) and fp.get(opt) is None:
            fp[opt] = geo[opt]
    # make WebRTC report the proxy egress IP too, coherent with HTTP egress (engine fabricates the
    # srflx candidate at this IP; no real STUN leaves the host).
    if geo.get("ip") and fp.get("webrtc_ip") is None:
        fp["webrtc_ip"] = geo["ip"]
    # A rotating proxy changes the exit per connection, making all of the above stale at once.
    warn_on_egress_drift(proxy, geo.get("ip"), quiet=quiet)


def _prepare(kwargs):
    # profile="auto" is NOT a saved option-set — it resolves a real captured fingerprint later,
    # once the executable (and therefore the engine's Chromium major) is known. See
    # _apply_auto_profile.
    profile = kwargs.pop("profile", None)
    is_auto = profile == "auto"
    kwargs["_cc_auto_profile"] = kwargs.pop("profile_select", None) if is_auto else None
    kwargs["_cc_is_auto"] = is_auto
    # profile= a saved persona (name, path, or Profile): its options are the base layer;
    # explicit kwargs passed to launch() override them.
    if profile is not None and not is_auto:
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
    outer_lease = kwargs.pop("_cc_lease", None)  # this launch's slot; the "auto" probe reuses it
    extra_args = kwargs.pop("args", None)
    extensions = kwargs.pop("extensions", None)
    portable_profile = kwargs.pop("portable_profile", False)
    encryption_key = kwargs.pop("encryption_key", None)
    # DEFAULT FLIPPED TO FALSE — Privacy Sandbox now stays ON unless the caller asks otherwise.
    #
    # The old default disabled Topics/FLEDGE/Shared Storage/Fenced Frames, reasoning that a build
    # claiming to be de-Googled should not answer document.browsingTopics(). That reasoning was
    # sound for a de-Googled PERSONA — but the default persona is `brand="chrome"`, and real Google
    # Chrome ships every one of these. So the shipped default presented a browser that called
    # itself Google Chrome while missing an API surface Google Chrome always has.
    #
    # Measured on the live audit against 150-r10: the row "a build claiming Chrome carries the
    # Privacy Sandbox surface Chrome ships" failed as an implausible value. It is the same defect
    # class as the WebUSB split fixed in r7 — a subtractive privacy default that is coherent only
    # against a persona nobody selects by default, and a hard tell against the one they do.
    #
    # Pass disable_privacy_sandbox=True to restore the old behaviour. It is the right choice when
    # the persona genuinely is de-Googled Chromium (brand="chromium"), and the wrong one under a
    # Chrome brand — which is why it is now a decision rather than a default.
    disable_privacy_sandbox = kwargs.pop("disable_privacy_sandbox", False)
    socks5_udp = kwargs.pop("socks5_udp", False)  # relay WebRTC UDP via SOCKS5 UDP ASSOCIATE
    cache_dir = kwargs.pop("cache_dir", None)
    quiet = kwargs.pop("quiet", False)
    auto_update = kwargs.pop("auto_update", None)
    version = kwargs.pop("version", None)  # browser major/version selector (catalog-resolved)
    release_channel = kwargs.pop("release_channel", None)  # PRO "stable" | "preview"
    # Engine behaviour switches that are not part of the persona (engine 152 r22+).
    allow_third_party_cookies = kwargs.pop("allow_third_party_cookies", None)
    transparent_proxy = kwargs.pop("transparent_proxy", None)
    kwargs.pop("license_through_proxy", None)  # consumed by _acquire_lease_from_kwargs
    # serve() drives headless itself and pops it from kwargs, so it tells us explicitly.
    headed_flag = kwargs.pop("_cc_headed", None)
    headed = bool(headed_flag) if headed_flag is not None else kwargs.get("headless") is False
    passthrough = is_fingerprint_passthrough(fp.get("fingerprint"))
    proxy_opt = kwargs.get("proxy")  # captured before resolve_proxy rewrites it (for quic + warnings)
    if geoip:
        # resolve the proxy's exit-IP geo and fill any UNSET timezone/accept_language/location/
        # webrtc_ip. Raises GeoipError (before any browser starts) when the region is unresolvable.
        apply_geoip(fp, kwargs.get("proxy"), quiet=quiet)
    exe = _resolve_binary(exe_path, cache_dir, quiet, auto_update, pro=_cc_pro, version=version,
                          release_channel=release_channel)
    _guard(exe)
    # profile="auto" -> resolve a REAL captured fingerprint for this host and apply it as
    # fingerprint_profile. Deliberately does NOT set a seed: with no --fingerprint the farbling
    # machinery stays off, which is the whole reason this path survives strict scoring.
    # Done here, after `exe` is known, because both the engine's Chromium major and the host GPU
    # measurement depend on the binary that will actually run.
    # Pass-through (fingerprint="off") runs with NO persona, so "auto" has nothing to apply.
    if kwargs.pop("_cc_is_auto", False) and not passthrough:
        _apply_auto_profile(fp, exe, kwargs.pop("_cc_auto_profile", None) or {},
                            quiet=quiet, pro=_cc_pro, lease=outer_lease)
    else:
        kwargs.pop("_cc_auto_profile", None)
    # Fonts are the most identifying surface measured -- 9.45 bits of entropy, and 35% of real
    # machines carry a font set nobody else has. A seeded persona with no profile used to fall
    # back to the engine's canonical per-OS list, which is byte-identical on every install: zero
    # entropy, sitting in a conspicuous tail. Give it a real machine's list instead. No-ops when
    # a profile is already set (an explicit one, or "auto", owns the fonts), under light_stealth,
    # and when there is no seed at all -- see ensure_persona_fonts for why each is deliberate.
    if not passthrough:
        ensure_persona_fonts(fp, quiet=quiet)
    # SOCKS5-with-credentials must go through --proxy-server (Playwright rejects creds in its SOCKS
    # proxy descriptor); resolve_proxy returns proxy=None for that case so we drop it from Playwright.
    # http(s)-with-credentials goes to the engine's --proxy-auth ONLY when the binary that will run
    # actually implements it (r19+); an older engine keeps Playwright's credential handling, which
    # is slower and disables the cache but authenticates. Routing blindly would strip the
    # credentials from Playwright and hand them to a switch the engine ignores: every request 407s.
    proxy_args, proxy = resolve_proxy(kwargs.get("proxy"),
                                      engine_supports_proxy_auth=engine_supports_switch(exe, "proxy-auth"))
    warn_unsupported_engine_options(exe, fp, kwargs.get("proxy"), quiet=quiet)
    if proxy is None:
        kwargs.pop("proxy", None)
    else:
        kwargs["proxy"] = proxy
    base = (fingerprint_args(fp) + agent_args(agent) + extension_args(extensions)
            + portable_args(portable_profile, encryption_key) + proxy_args)
    base += quic_args(proxy_opt)  # behind a proxy, disable QUIC so no HTTP/3 UDP egresses around it
    # Opt-in: relay WebRTC UDP through the proxy rather than denying it outright.
    base += socks5_udp_args(socks5_udp, proxy_opt)
    # navigator.bluetooth must follow the CLAIM, in both directions: a Linux build hides it while
    # exposing usb/serial/hid (an OS-origin tell on a Windows persona), and a Windows build shows
    # it under a Linux claim, which genuine Chrome on Linux does not. The host is never consulted;
    # whichever switch agrees with this build's default is a no-op. See web_bluetooth_args.
    base += web_bluetooth_args(persona_platform(fp))
    if disable_privacy_sandbox:
        base += privacy_sandbox_args()
    user = list(extra_args or [])
    # default WebRTC to leak-proof unless the user wired a webrtc_ip / policy themselves
    base += webrtc_default_deny_args(base + user, fp.get("webrtc_ip"))
    base += engine_extras_args(allow_third_party_cookies, transparent_proxy, proxy_opt, quiet=quiet)
    # Pairs with stripping Playwright's --enable-unsafe-swiftshader (DEFAULT_IGNORED_ARGS).
    base += gpu_blocklist_args(headed, sys.platform, user)
    # collapse all --enable-features/--disable-features (ours + the user's) into one of each, else
    # Chromium keeps only the last occurrence and the rest are silently dropped.
    args = merge_feature_flags(base + user)
    # Last: drop 152 r22+ switches this engine does not implement (with a warning), wherever they
    # came from.
    args, _gate_notes = gate_engine_switches(exe, args, quiet=quiet)
    # Drop Playwright's default automation flag so the engine's AutomationControlled feature stays
    # OFF (it otherwise flips navigator.webdriver-adjacent tells), --enable-unsafe-swiftshader and the
    # headless --hide-scrollbars (see DEFAULT_IGNORED_ARGS). The control transport (--remote-debugging-pipe) is left intact.
    # Caller can override via their own ignore_default_args.
    # NOTE: launch_persistent_context sets this BEFORE the Widevine helper so that helper appends
    # --disable-component-update rather than clobbering the automation strip.
    kwargs.setdefault("ignore_default_args", list(DEFAULT_IGNORED_ARGS))
    # Surface incoherent / missing-recommended option combos the SDK can't auto-fix (stderr; gated
    # by quiet / CLEARCOTE_NO_WARN). geoip may have just filled timezone/accept_language above.
    # _font_reach is computed HERE, not inside coherence_warnings, because enumerating the
    # host's installed families is I/O and that function is documented (and tested) as pure.
    emit_coherence_warnings(
        {**fp, "proxy": proxy_opt, "geoip": geoip, "headless": kwargs.get("headless"),
         "devtools": kwargs.get("devtools"), "user_agent": kwargs.get("user_agent"),
         "_user_args": user, "_font_reach": font_reachability(fp.get("fingerprint_profile"))},
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


def _headless_geometry_kwargs(pw_kwargs, seed, args=None):
    """The headless geometry defaults for this launch, or None if they don't apply.

    ``apply_headless_geometry`` mutates, and ``chromium.launch()`` accepts no ``no_viewport`` (a
    context option), so probe a copy and carry the result to the context.
    """
    return apply_headless_geometry(dict(pw_kwargs), seed, args)


def _with_geometry_args(args, geom):
    """The command line plus the headless display switches ``geom`` asks for (regime 2)."""
    return list(args) + list((geom or {}).get("args") or [])


def _install_window_fixup(container, args):
    """Fit the headless window to the display's work area once, on the first page.

    A persistent context already owns a page, so act immediately; a browser-level context does not,
    so defer to its first ``new_page``. Idempotent — later tabs share the window. ``args`` are the
    caller's, so a window switch of theirs is respected.
    """
    done = []

    def fit(page):
        if done:
            return page
        done.append(True)
        fit_window_to_work_area(page, args)
        return page

    pages = getattr(container, "pages", None)
    if pages:
        return fit(pages[0])
    orig_new_page = container.new_page

    def new_page(**kw):
        return fit(orig_new_page(**kw))

    container.new_page = new_page
    return None


def _install_headless_geometry(browser, args=None):
    """Default a headless browser's new pages/contexts to ``no_viewport`` plus a window fit.

    ``chromium.launch()`` accepts no context options, so the default has to ride on
    ``new_page``/``new_context`` — the same shape as ``_install_headed_viewport``. Each new context
    is a new window, so each also gets the window fit (to the persona's work area, or the display
    ``--screen-info`` set). A caller who passes any of ``viewport`` / ``no_viewport`` / ``screen``
    per call keeps full control.
    """
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    def _merge(kw):
        if not any(k in kw for k in ("viewport", "no_viewport", "screen")):
            kw["no_viewport"] = True
        return kw

    def new_page(**kw):
        page = orig_new_page(**_merge(kw))
        fit_window_to_work_area(page, args)
        return page

    def new_context(**kw):
        context = orig_new_context(**_merge(kw))
        _install_window_fixup(context, args)
        return context

    browser.new_page, browser.new_context = new_page, new_context


def _install_ephemeral_profile_cleanup(context, user_data_dir):
    """Delete the throwaway profile directory -- once the browser that used it has EXITED.

    THE DIRECTORY IS THE COST OF THE PERSISTENT DEFAULT, so it has to be paid back reliably.
    A Chromium profile is 5-50MB and this session's audit found 570 leaked browser directories
    on one developer machine from earlier tooling — the failure mode is silent until a disk
    fills, which is exactly when it is most expensive.

    NEVER UNDER A LIVE BROWSER. Playwright fires ``close`` when the browser's pipe drops, which on
    Linux is ~100 ms BEFORE the browser process exits, and Chrome keeps writing the profile until
    then. Deleting it from that handler either raised mid-walk or succeeded -- and Chrome wrote the
    directory back (Default/, Local State, first_party_sets.db) after the cleanup had marked itself
    done: 2 of 10 launches on Linux, and one clearcote-run-* per release smoke run. So:
      * ``close()`` is wrapped: Playwright returns from it only once the browser process has
        exited, so the profile is removed right after, and is gone when the caller's close returns;
      * ``close`` without a close() call (a crash, the window closed) waits for the browser's
        process to exit first (its pid is in the profile, see _browser_process_files);
      * at interpreter exit, one sweep runs AFTER the driver has stopped and closed its browsers
        (see _sweep_throwaway_profiles).
    All funnel through one idempotent remove, so running twice is harmless.

    THE RETRY IS NOT DEFENSIVE PADDING — a single attempt measurably does not work. On Windows the
    browser process still holds handles under the profile directory for a short window after
    ``close()`` returns, so the first rmtree hits "being used by another process" and, with
    ignore_errors=True, fails SILENTLY. Measured on the first build of this change: the directory
    survived a close plus a 1.5s wait, reported clean, and leaked.

    So: retry with a short backoff, and only swallow the error once the attempts are spent. A
    failed cleanup must never raise into the caller's teardown — the directory is disposable,
    their traceback is not — but it must not be swallowed on the first try either, which is how
    570 directories accumulate without anyone noticing.
    """
    remove = _profile_dir_remover(user_data_dir, *_browser_process_files(user_data_dir))
    closing = {"v": False}
    orig_close = context.close

    def close(*args, **kwargs):
        closing["v"] = True
        try:
            return orig_close(*args, **kwargs)
        finally:
            remove()

    def on_close(*_a):
        if not closing["v"]:
            remove()

    context.close = close
    context.on("close", on_close)
    return remove


def _browser_process_files(user_data_dir):
    """``(pid, socket_dir)`` of the browser running on ``user_data_dir``, read from the symlinks
    Chrome keeps in its profile on Linux and macOS while it runs: SingletonLock ->
    "<hostname>-<pid>", SingletonSocket -> "<tmp>/org.chromium.Chromium.XXXXXX/SingletonSocket"
    (the socket directory leaks when the browser is killed rather than closed). Read right after
    launch, while both exist. Windows has neither: nothing to wait for, and there its file locks
    make an early delete fail rather than succeed."""
    if os.name == "nt":
        return None, None
    pid = socket_dir = None
    try:
        pid = int(os.readlink(os.path.join(user_data_dir, "SingletonLock")).rsplit("-", 1)[-1])
    except (OSError, ValueError):
        pass
    try:
        d = os.path.dirname(os.readlink(os.path.join(user_data_dir, "SingletonSocket")))
        # only ever Chrome's own temp directory: the path comes out of a file
        if os.path.basename(d).startswith(("org.chromium.Chromium.", "com.google.Chrome.")):
            socket_dir = d
    except OSError:
        pass
    return pid, socket_dir


def _wait_for_exit(pid, timeout):
    """True once process ``pid`` has exited (or there is none to wait for); False if it is still
    running after ``timeout`` seconds."""
    if pid is None or pid <= 0 or os.name == "nt":  # os.kill(pid, 0) TERMINATES it on Windows
        return True
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # EPERM: alive, and another user's (the pid was reused) -- wait out the timeout
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


_THROWAWAY_PROFILES = {}  # user_data_dir -> its remover, until the directory is gone


def _sweep_throwaway_profiles():
    """Interpreter exit: remove every throwaway profile still on disk -- a script that ended (or
    crashed) with its browser open. Registered at import, see below."""
    for remove in list(_THROWAWAY_PROFILES.values()):
        remove(final=True)


# Registered at IMPORT, so before the first launch registers _stop_quietly(_pw): atexit runs
# last-in-first-out, so this sweep runs AFTER the driver has stopped -- and closed its browsers. A
# hook registered per launch ran first instead, deleted the profile under the still-running
# browser, and the browser wrote it back while the driver closed it (5 of 5 launches on Linux).
atexit.register(_sweep_throwaway_profiles)


def _profile_dir_remover(user_data_dir, pid=None, socket_dir=None):
    """An idempotent remove of ``user_data_dir`` (and the browser's ``socket_dir``) that first waits
    for the browser ``pid`` on it to exit, then retries while the browser still holds handles under
    it (see _install_ephemeral_profile_cleanup for why). Returns True once the directory is gone;
    until then it stays registered for the interpreter-exit sweep."""
    import shutil

    done = {"v": False}

    def cleanup(*_a, final=False):
        if done["v"]:
            return True
        # Deleting under a running browser is what leaked: it writes the directory back. So not
        # yet -- a later trigger retries. At exit there is none: delete anyway after the wait.
        if not _wait_for_exit(pid, 5.0 if final else 10.0) and not final:
            return False
        if socket_dir:
            shutil.rmtree(socket_dir, ignore_errors=True)
        for attempt in range(6):
            try:
                shutil.rmtree(user_data_dir)
            except OSError:
                # FileNotFoundError included: rmtree raises it for ANY entry that vanished during
                # the walk, not only for the directory itself -- so only the check below is "gone".
                pass
            if not os.path.lexists(user_data_dir):
                done["v"] = True
                _THROWAWAY_PROFILES.pop(user_data_dir, None)
                return True
            if attempt < 5:
                time.sleep(0.25 * (attempt + 1))  # 0.25→1.5s, ~5s total
        # Out of attempts: left registered for the exit sweep (the browser is usually gone by
        # then); if that fails too the OS temp sweeper reclaims it.
        return False

    _THROWAWAY_PROFILES[user_data_dir] = cleanup
    return cleanup


def _launch_on_throwaway_profile(prefix, kwargs):
    """A persistent context on a fresh temp profile the caller never named, so never sees again:
    removed once its browser has exited -- after close(), or when it went away by itself -- and at
    interpreter exit; and at once when the launch fails, which used to leak one directory per
    failed launch."""
    import tempfile

    udd = tempfile.mkdtemp(prefix=prefix)
    try:
        # looked up at call time (module global), so tests can stand in for the real launch.
        # cloud=False: the caller already decided this is a local launch; CLEARCOTE_CLOUD must not
        # send the inner call to the cloud.
        context = launch_persistent_context(udd, cloud=False, **kwargs)
    except BaseException:
        _profile_dir_remover(udd, *_browser_process_files(udd))()
        raise
    _install_ephemeral_profile_cleanup(context, udd)
    return context


def _install_persistent_as_browser(context):
    """Make a persistent BrowserContext satisfy the code written against ``launch()``'s Browser.

    launch() has always returned a Playwright ``Browser`` and is documented as a drop-in, so the
    persistent default cannot simply hand back a ``BrowserContext``: ``browser.new_context()`` is
    ordinary Playwright and would break at the call site.

    ``new_context()`` therefore returns THE PERSISTENT CONTEXT ITSELF rather than a fresh incognito
    one. That is the deliberate part: a real incognito context would silently leave the profile
    behind and take the Widevine CDM, the component-updated state and the cookies with it — the
    caller would get back exactly the browser this change exists to stop handing them. Two calls
    returning the same context is a visible, documented compromise; quietly returning a browser
    without a profile is not.
    """
    if not hasattr(context, "new_context"):
        context.new_context = lambda **_kw: context
    if not hasattr(context, "contexts"):
        context.contexts = [context]
    return context


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
    # license_through_proxy: checkout/heartbeat/checkin through the launch's own proxy.
    license_through_proxy = kwargs.pop("license_through_proxy", None)
    lease = acquire_lease(
        license_key=license_key, api_base=license_api_base,
        sdk_version=__version__, quiet=kwargs.get("quiet", False),
        engine_version=lambda: resolved_engine_version(version_sel, has_license=bool(key)),
        license_through_proxy=license_through_proxy, proxy=kwargs.get("proxy"),
    )
    # Hand this slot to the nested host probe behind profile="auto" (see _apply_auto_profile).
    # It launches the engine once more, and used to check out a SECOND slot while this one is
    # already live -- which a per-browser plan (the free tier) refuses, so the launch deadlocked
    # against itself. _prepare pops this key; it never reaches Playwright.
    kwargs["_cc_lease"] = lease
    return lease


def _adopt_license_kwargs(kwargs, lease):
    """Consume the licence kwargs exactly as :func:`_acquire_lease_from_kwargs` would, but reuse
    ``lease`` instead of checking out a second concurrency slot. Used by the nested host probe."""
    license_key = kwargs.pop("license_key", None)
    license_api_base = kwargs.pop("license_api_base", None)
    kwargs.pop("license_through_proxy", None)
    key = resolve_license_key(license_key)
    kwargs["_cc_pro"] = (key, license_api_base) if key else None
    return lease


def _prepare_or_release(kwargs, lease):
    """_prepare, releasing the lease handle if it raises (e.g. GeoipError) so a failed launch does
    not keep a reference on the machine lease."""
    try:
        return _prepare(kwargs)
    except BaseException:
        if lease:
            try:
                lease.stop()
            except Exception:  # noqa: BLE001
                pass
        raise


def _is_stale_token_refusal(exc) -> bool:
    """The PRO engine refused the run-token as older than one it has already accepted on this machine.
    Playwright's launch error carries the engine's stderr, which is where that line lands."""
    return STALE_TOKEN_REFUSAL in str(exc)


def _retry_on_stale_run_token(lease, pw_kwargs, launch_token, start):
    """Run ``start()``; if the engine refuses the run-token as stale, mint a fresh one and launch once more.

    acquire_lease() already replaces a token it can SEE is older than the engine's mark. This covers the
    race it cannot see: another process (a parallel launch, the hosted-browser gateway, another key)
    moving the mark between that check and this launch. ``start`` reads ``pw_kwargs`` when called, so
    re-injecting the new token is all a retry needs; the bound token file follows the lease by itself."""
    try:
        return start()
    except Exception as exc:  # noqa: BLE001
        refresh = getattr(lease, "refresh_token", None)
        if refresh is None or not _is_stale_token_refusal(exc) or not refresh():
            raise
        inject_run_token(pw_kwargs, lease.token, launch_token[0] if launch_token else None)
        return start()


def _release_lease_on_failure(lease, start):
    """Run ``start()``; if the browser fails to start, release the lease before re-raising. On a
    per-browser plan (the free tier) the slot would otherwise stay taken until the lease TTL."""
    try:
        return start()
    except BaseException:
        if lease:
            try:
                lease.stop()
            except Exception:  # noqa: BLE001
                pass
        raise


def _drop_cloud_credentials(kwargs):
    """api_key/api_url only pick the account a CLOUD launch uses. A local launch ignores them, so
    code that always passes them switches between local and cloud with nothing but ``cloud``."""
    kwargs.pop("api_key", None)
    kwargs.pop("api_url", None)


def launch(cloud=None, **kwargs):
    """Launch Clearcote and return a Playwright browser handle backed by a REAL Chrome profile.

    LOCAL OR CLOUD. ``cloud=True`` runs the browser on Clearcote's servers instead and returns the
    same Playwright ``Browser``, connected over CDP, with ``humanize`` applied here exactly as for a
    local browser. ``cloud=None`` (the default) follows ``CLEARCOTE_CLOUD=1|true|yes``. The API key
    comes from ``api_key=`` or ``CLEARCOTE_API_KEY``; the cloud options (``country``, ``identity``,
    ``profile``, ``record``, ...) and the options a cloud browser cannot take are listed in
    :mod:`clearcote.cloud`. ``close()`` disconnects and ends the session.

    Fingerprint kwargs: fingerprint, platform, platform_version, brand, brand_version,
    gpu_vendor, gpu_renderer, hardware_concurrency, location, timezone, accept_language,
    webrtc_ip, disable_gpu_fingerprint. Pass geoip=True to resolve the proxy's exit-IP geo and
    auto-fill any unset timezone/accept_language/location. Pass license_key=... (or set
    CLEARCOTE_LICENSE_KEY) to check out a concurrency slot for the PRO engine. All other kwargs
    (headless, proxy, args, timeout, ...) pass through to Playwright.

    PROFILE-BACKED BY DEFAULT (changed in 0.23.0). This used to be ``chromium.launch()`` —
    incognito, no profile directory. Incognito cannot load a component-updated CDM, so
    ``requestMediaKeySystemAccess('com.widevine.alpha')`` rejected and the EME surface was a
    no-Widevine tell on a build branded Google Chrome (measured against the live audit on
    150-r10). It now launches a persistent context on a throwaway directory, so ``widevine=True``
    works here and the profile-shaped surface matches a real Chrome.

    The directory is deleted when the context closes AND on interpreter exit — nothing is left
    behind, and no state survives to the next launch, so the incognito-like isolation callers
    relied on is preserved. Pass ``user_data_dir=`` to keep a profile instead (or call
    ``launch_persistent_context`` directly), and ``ephemeral_profile=False`` to opt back out.
    """
    if cloud_requested(cloud):
        return launch_cloud(cloud, kwargs)
    _drop_cloud_credentials(kwargs)
    # ephemeral_profile=False restores the pre-0.23 incognito launch. Kept because the persistent
    # path costs a directory create+delete per launch, which a caller spawning hundreds of
    # short-lived browsers may reasonably not want to pay for a CDM they never touch.
    ephemeral = kwargs.pop("ephemeral_profile", True)
    explicit_dir = kwargs.pop("user_data_dir", None)
    if explicit_dir is not None:
        return launch_persistent_context(explicit_dir, cloud=False, **kwargs)
    if ephemeral:
        return _install_persistent_as_browser(_launch_on_throwaway_profile("clearcote-run-", kwargs))

    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    # _cc_lease (internal): the host probe behind profile="auto" runs on its caller's slot rather
    # than checking out its own. It does not own the lease, so it must not release it on close.
    reused = kwargs.pop("_cc_lease", None)
    owns_lease = reused is None
    lease = _acquire_lease_from_kwargs(kwargs) if owns_lease else _adopt_license_kwargs(kwargs, reused)
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare_or_release(
        kwargs, lease if owns_lease else None)
    apply_font_env(exe, pw_kwargs, args)  # Linux: bundled font clones + UI-locale LANGUAGE
    apply_shader_dialect(shader_dialect, pw_kwargs)  # after fonts: that helper rebuilds the env
    launch_token = lease.bind_launch() if lease else None  # (file, release) or None; r23+ opt-in
    if lease:  # inject CLEARCOTE_RUN_TOKEN (+ the r23+ opt-in token FILE) so the gate lets it launch
        inject_run_token(pw_kwargs, lease.token, launch_token[0])
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    # Headless: screen.* has to be handled alongside the viewport or the window reports
    # outer > screen (see _geometry). The display switches go on the command line; no_viewport is a
    # context option, so it rides on new_page/new_context.
    geom = None if headed else _headless_geometry_kwargs(pw_kwargs, seed, args)
    launch_args = _with_geometry_args(args, geom)
    browser = _release_lease_on_failure(lease if owns_lease else None, lambda: _retry_on_stale_run_token(
        lease, pw_kwargs, launch_token, lambda: _win_av_retry(
            lambda e: _playwright().chromium.launch(executable_path=e, args=launch_args, **pw_kwargs), exe
        )))
    if lease:  # release the concurrency slot + remove the run-token file when the browser closes
        def _on_disconnect(_b=None, _lease=lease, _lt=launch_token, _own=owns_lease):
            if _own:  # a borrowed slot belongs to the caller: only drop this launch's token file
                _lease.stop()
            _lt[1]()
        browser.on("disconnected", _on_disconnect)
    if headed:
        _install_headed_viewport(browser)
    elif geom:
        _install_headless_geometry(browser, args)
    install_humanize(browser, humanize, show_cursor, seed=seed)
    return browser


def launch_persistent_context(user_data_dir=None, cloud=None, **kwargs):
    """Launch Clearcote with a persistent profile directory; returns a Playwright
    ``BrowserContext`` (cookies/storage persist in ``user_data_dir``).

    Pass ``widevine=True`` to seed + enable the (opt-in, user-fetched) Widevine CDM so DRM/EME works
    (``requestMediaKeySystemAccess('com.widevine.alpha')`` resolves) and the EME surface matches a
    real Chrome instead of being a no-Widevine tell.

    CLOUD: ``launch_persistent_context(cloud=True, profile="name")`` returns the context of a hosted
    session that loads the cloud profile ``name`` and saves it back when the context closes (which
    also ends the session). A cloud browser has no local directory, so ``user_data_dir`` with
    ``cloud=True`` raises ValueError pointing at ``profile=``."""
    if cloud_requested(cloud):
        return launch_cloud(cloud, kwargs, persistent=True, user_data_dir=user_data_dir)
    if user_data_dir is None:
        raise TypeError("launch_persistent_context() needs a user_data_dir "
                        "(or cloud=True with profile=\"name\" for a cloud profile)")
    _drop_cloud_credentials(kwargs)
    # Set the default strip (DEFAULT_IGNORED_ARGS) BEFORE the Widevine helper so it appends
    # --disable-component-update to it rather than replacing it (which would lose the AutomationControlled
    # strip on Widevine launches).
    kwargs.setdefault("ignore_default_args", list(DEFAULT_IGNORED_ARGS))
    if kwargs.get("widevine"):
        apply_widevine_launch(user_data_dir, kwargs, quiet=kwargs.get("quiet", False))
    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    # _cc_lease (internal): a launch that borrows its caller's slot (the profile="auto" host probe
    # reaches here when ephemeral_profile is left on). It must not release a slot it does not own.
    reused = kwargs.pop("_cc_lease", None)
    owns_lease = reused is None
    lease = _acquire_lease_from_kwargs(kwargs) if owns_lease else _adopt_license_kwargs(kwargs, reused)
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare_or_release(
        kwargs, lease if owns_lease else None)
    apply_font_env(exe, pw_kwargs, args)  # Linux: bundled font clones + UI-locale LANGUAGE
    apply_shader_dialect(shader_dialect, pw_kwargs)  # after fonts: that helper rebuilds the env
    launch_token = lease.bind_launch() if lease else None  # (file, release) or None; r23+ opt-in
    if lease:  # inject CLEARCOTE_RUN_TOKEN (+ the r23+ opt-in token FILE) so the gate lets it launch
        inject_run_token(pw_kwargs, lease.token, launch_token[0])
    geom = None
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    else:  # headless: persona owns screen -> fit the window; no persona -> set the display too
        geom = apply_headless_geometry(pw_kwargs, seed, args)
    launch_args = _with_geometry_args(args, geom)
    context = _release_lease_on_failure(lease if owns_lease else None, lambda: _retry_on_stale_run_token(
        lease, pw_kwargs, launch_token, lambda: _win_av_retry(
            lambda e: _playwright().chromium.launch_persistent_context(
                user_data_dir, executable_path=e, args=launch_args, **pw_kwargs
            ),
            exe,
        )))
    if lease:  # release the concurrency slot + remove the run-token file when the context closes
        def _on_close(_c=None, _lease=lease, _lt=launch_token, _own=owns_lease):
            if _own:  # a borrowed slot belongs to the caller: only drop this launch's token file
                _lease.stop()
            _lt[1]()
        context.on("close", _on_close)
    if geom:
        _install_window_fixup(context, args)
    install_humanize_on_context(context, humanize, show_cursor, seed=seed)
    return context


def launch_agent(user_data_dir=None, **kwargs):
    """Launch Clearcote ready for the in-browser AI agent; returns a Playwright ``BrowserContext``.

    The agent drives Chrome's Actor framework, which only attaches to a REGULAR profile (not
    incognito), so this uses a persistent context: a fresh temp ``user_data_dir``, deleted when the
    context closes, unless you pass one to keep. Set ``agent_llm_key`` (+ optional
    ``agent_model``), then drive a page with ``run_agent_task()``. Use this (or
    ``launch_persistent_context``) for the agent -- plain ``launch()`` is incognito, where the
    Actor framework can't attach the tab."""
    if user_data_dir is not None:
        # the agent drives the LOCAL engine's Actor framework: never a cloud browser
        return launch_persistent_context(user_data_dir, cloud=False, **kwargs)
    return _launch_on_throwaway_profile("clearcote-agent-", kwargs)


def serve_multiplex(**kwargs):
    """One CDP endpoint, many identities: a separate clearcote browser per connection URL.

    See :func:`clearcote._multiplex.serve_multiplex`."""
    from ._multiplex import serve_multiplex as _serve_multiplex
    return _serve_multiplex(**kwargs)
