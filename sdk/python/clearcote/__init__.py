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
"""

import atexit
import os
import sys
import time

from ._agent import AGENT_KEYS, OPENROUTER_BASE_URL, agent_args, run_agent_task
from ._fingerprint import FINGERPRINT_KEYS, fingerprint_args
from ._fontpersona import ensure_persona_fonts, font_reachability
from ._fonts import apply_font_env
from ._shaderdialect import apply_shader_dialect
from ._geometry import apply_headless_geometry, fit_window_to_persona, move_window_to_origin
from ._humanize import install_humanize, install_humanize_on_context
from ._launchopts import (  # noqa: F401  (web_bluetooth_args re-exported for tests)
    extension_args,
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
__version__ = "0.26.3"

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
                                     cache_dir=cache_dir, quiet=quiet, version=version)

        kind, payload = resolve_version(version, has_license=bool(pro and pro[0]), quiet=quiet)
        if kind == "pro":
            return pro_ensure_binary(pro[0], api_base=(pro[1] if pro else None),
                                     cache_dir=cache_dir, quiet=quiet, version=payload)
        rel = payload  # free build resolved from the catalog
        base = os.path.join(cache_dir or _cache_root(), rel["tag"])
        cached = _cached(base, rel["binary"], quiet)
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
    Pin a specific PRO rebuild with ``version="150.0.7871.114-r7"`` (or bare ``"r7"``) — revisions are
    licensed builds, so a key is required.
    Pass ``auto_update=True`` (or set ``CLEARCOTE_AUTO_UPDATE=1``) to fetch the latest release.
    """
    key = resolve_license_key(license_key)
    pro = (key, license_api_base) if key else None
    return _resolve_binary(executable_path, cache_dir, quiet, auto_update, pro=pro, version=version)


def download(cache_dir=None, quiet=False, auto_update=None, version=None, license_key=None,
             license_api_base=None):
    """Pre-fetch + verify the Clearcote binary without launching. Returns the chrome.exe path.

    Pass ``version="150"`` / ``"150.0.7871.115"`` / ``"latest"`` to fetch a specific browser build
    from the catalog (PRO-tier versions need ``license_key`` / ``CLEARCOTE_LICENSE_KEY``). A PRO
    rebuild can be pinned with ``version="150.0.7871.114-r7"`` (or bare ``"r7"``).
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


def _apply_auto_profile(fp, exe, select, quiet=False, pro=None):
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
    host = measure_host(
        lambda **kw: launch(
            license_key=license_key, license_api_base=api_base, ephemeral_profile=False, **kw
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
    # profile="auto" -> resolve a REAL captured fingerprint for this host and apply it as
    # fingerprint_profile. Deliberately does NOT set a seed: with no --fingerprint the farbling
    # machinery stays off, which is the whole reason this path survives strict scoring.
    # Done here, after `exe` is known, because both the engine's Chromium major and the host GPU
    # measurement depend on the binary that will actually run.
    if kwargs.pop("_cc_is_auto", False):
        _apply_auto_profile(fp, exe, kwargs.pop("_cc_auto_profile", None) or {},
                            quiet=quiet, pro=_cc_pro)
    else:
        kwargs.pop("_cc_auto_profile", None)
    # Fonts are the most identifying surface measured -- 9.45 bits of entropy, and 35% of real
    # machines carry a font set nobody else has. A seeded persona with no profile used to fall
    # back to the engine's canonical per-OS list, which is byte-identical on every install: zero
    # entropy, sitting in a conspicuous tail. Give it a real machine's list instead. No-ops when
    # a profile is already set (an explicit one, or "auto", owns the fonts), under light_stealth,
    # and when there is no seed at all -- see ensure_persona_fonts for why each is deliberate.
    ensure_persona_fonts(fp, quiet=quiet)
    # SOCKS5-with-credentials must go through --proxy-server (Playwright rejects creds in its SOCKS
    # proxy descriptor); resolve_proxy returns proxy=None for that case so we drop it from Playwright.
    proxy_args, proxy = resolve_proxy(kwargs.get("proxy"))
    if proxy is None:
        kwargs.pop("proxy", None)
    else:
        kwargs["proxy"] = proxy
    base = (fingerprint_args(fp) + agent_args(agent) + extension_args(extensions)
            + portable_args(portable_profile, encryption_key) + proxy_args)
    base += quic_args(proxy_opt)  # behind a proxy, disable QUIC so no HTTP/3 UDP egresses around it
    # Opt-in: relay WebRTC UDP through the proxy rather than denying it outright.
    base += socks5_udp_args(socks5_udp, proxy_opt)
    # Linux hosts hide navigator.bluetooth while exposing usb/serial/hid — an OS-origin tell on a
    # Windows persona. Restore it (no-op off Linux). See web_bluetooth_args.
    base += web_bluetooth_args()
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
    # _font_reach is computed HERE, not inside coherence_warnings, because enumerating the
    # host's installed families is I/O and that function is documented (and tested) as pure.
    emit_coherence_warnings(
        {**fp, "proxy": proxy_opt, "geoip": geoip, "headless": kwargs.get("headless"),
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

    ``apply_headless_geometry`` mutates, and ``chromium.launch()`` accepts neither ``viewport`` nor
    ``screen`` (they are context options), so probe a copy and carry the result to the context.
    """
    return apply_headless_geometry(dict(pw_kwargs), seed, args)


def _install_window_fixup(container, args, persona):
    """Apply the headless window fixup once, on the first page.

    Persona regime: fit the window to the persona's work area. Profile regime: move the window to the
    origin so it stops overhanging the spoofed screen edge. A persistent context already owns a page,
    so act immediately; a browser-level context does not, so defer to its first ``new_page``.
    Idempotent — later tabs share the window.
    """
    done = []

    def fit(page):
        if done:
            return page
        done.append(True)
        if persona:
            fit_window_to_persona(page, args)
        else:
            move_window_to_origin(page, args)
        return page

    pages = getattr(container, "pages", None)
    if pages:
        return fit(pages[0])
    orig_new_page = container.new_page

    def new_page(**kw):
        return fit(orig_new_page(**kw))

    container.new_page = new_page
    return None


def _install_headless_geometry(browser, geom, args=None):
    """Default a headless browser's new pages/contexts to ``geom``.

    ``chromium.launch()`` accepts no context options, so the default has to ride on
    ``new_page``/``new_context`` — the same shape as ``_install_headed_viewport``. In persona mode
    each new context is a new window, so each also gets the window fit. A caller who passes any of
    ``viewport`` / ``no_viewport`` / ``screen`` per call keeps full control.
    """
    persona = geom.get("mode") == "persona"
    defaults = {"no_viewport": True} if persona else {
        k: v for k, v in geom.items() if k in ("screen", "viewport")}
    orig_new_page, orig_new_context = browser.new_page, browser.new_context

    def _merge(kw):
        if not any(k in kw for k in ("viewport", "no_viewport", "screen")):
            kw.update(defaults)
        return kw

    def new_page(**kw):
        page = orig_new_page(**_merge(kw))
        if persona:
            fit_window_to_persona(page, args)
        else:
            move_window_to_origin(page, args)
        return page

    def new_context(**kw):
        context = orig_new_context(**_merge(kw))
        _install_window_fixup(context, args, persona)
        return context

    browser.new_page, browser.new_context = new_page, new_context


def _install_ephemeral_profile_cleanup(context, user_data_dir):
    """Delete the throwaway profile directory once the context closes.

    THE DIRECTORY IS THE COST OF THE PERSISTENT DEFAULT, so it has to be paid back reliably.
    A Chromium profile is 5-50MB and this session's audit found 570 leaked browser directories
    on one developer machine from earlier tooling — the failure mode is silent until a disk
    fills, which is exactly when it is most expensive.

    Two triggers, because neither alone is enough:
      * ``close`` fires on an orderly ``context.close()``;
      * the atexit hook covers the interpreter exiting with the context still open, which is what
        a crashing script or a KeyboardInterrupt actually does.
    Both funnel through one idempotent remove, so running twice is harmless.

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
    import atexit
    import shutil
    import time

    done = {"v": False}

    def cleanup(*_a):
        if done["v"]:
            return
        for attempt in range(6):
            try:
                shutil.rmtree(user_data_dir)
                done["v"] = True
                return
            except FileNotFoundError:
                done["v"] = True  # already gone: someone else won the race, which is success
                return
            except OSError:
                if attempt == 5:
                    break
                time.sleep(0.25 * (attempt + 1))  # 0.25→1.5s, ~5s total
        # Out of attempts. Leave it for the atexit pass (the browser is usually gone by then);
        # if that fails too the OS temp sweeper reclaims it, and `done` stays False so the
        # atexit hook genuinely retries rather than short-circuiting.
        shutil.rmtree(user_data_dir, ignore_errors=True)

    context.on("close", cleanup)
    atexit.register(cleanup)
    return cleanup


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
    return acquire_lease(
        license_key=license_key, api_base=license_api_base,
        sdk_version=__version__, quiet=kwargs.get("quiet", False),
        engine_version=lambda: resolved_engine_version(version_sel, has_license=bool(key)),
    )


def launch(**kwargs):
    """Launch Clearcote and return a Playwright browser handle backed by a REAL Chrome profile.

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
    # ephemeral_profile=False restores the pre-0.23 incognito launch. Kept because the persistent
    # path costs a directory create+delete per launch, which a caller spawning hundreds of
    # short-lived browsers may reasonably not want to pay for a CDM they never touch.
    ephemeral = kwargs.pop("ephemeral_profile", True)
    explicit_dir = kwargs.pop("user_data_dir", None)
    if explicit_dir is not None:
        return launch_persistent_context(explicit_dir, **kwargs)
    if ephemeral:
        import tempfile

        udd = tempfile.mkdtemp(prefix="clearcote-run-")
        context = launch_persistent_context(udd, **kwargs)
        _install_ephemeral_profile_cleanup(context, udd)
        return _install_persistent_as_browser(context)

    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    lease = _acquire_lease_from_kwargs(kwargs)  # opt-in; None in free mode
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare(kwargs)
    apply_font_env(exe, pw_kwargs)  # Linux: point FONTCONFIG_FILE at the bundled font clones
    apply_shader_dialect(shader_dialect, pw_kwargs)  # after fonts: that helper rebuilds the env
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    headed = _headed_no_viewport(pw_kwargs)  # launch() takes no viewport kwarg -> wrap new_page/context
    # Headless: screen.* has to be overridden alongside the viewport or the window reports
    # outer > screen (see _geometry). Also a context option, so it rides on new_page/new_context.
    geom = None if headed else _headless_geometry_kwargs(pw_kwargs, seed, args)
    browser = _win_av_retry(
        lambda e: _playwright().chromium.launch(executable_path=e, args=args, **pw_kwargs), exe
    )
    if lease:  # release the concurrency slot when the browser closes
        browser.on("disconnected", lambda _b=None: lease.stop())
    if headed:
        _install_headed_viewport(browser)
    elif geom:
        _install_headless_geometry(browser, geom, args)
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
    shader_dialect = kwargs.pop("shader_dialect", None)  # popped before _prepare: not a PW option
    lease = _acquire_lease_from_kwargs(kwargs)  # opt-in; None in free mode
    # seed reflects the merged/effective fingerprint (profile-aware) -> stable motor persona
    exe, args, pw_kwargs, humanize, show_cursor, seed = _prepare(kwargs)
    apply_font_env(exe, pw_kwargs)  # Linux: point FONTCONFIG_FILE at the bundled font clones
    apply_shader_dialect(shader_dialect, pw_kwargs)  # after fonts: that helper rebuilds the env
    if lease:  # inject CLEARCOTE_RUN_TOKEN so the PRO engine gate lets the browser launch
        inject_run_token(pw_kwargs, lease.token)
    geom = None
    if _headed_no_viewport(pw_kwargs):  # no_viewport IS a valid persistent-context option
        pw_kwargs["no_viewport"] = True
    else:  # headless: persona owns screen -> fit the window; no persona -> override screen
        geom = apply_headless_geometry(pw_kwargs, seed, args)
    context = _win_av_retry(
        lambda e: _playwright().chromium.launch_persistent_context(
            user_data_dir, executable_path=e, args=args, **pw_kwargs
        ),
        exe,
    )
    if lease:  # release the concurrency slot when the context closes
        context.on("close", lambda _c=None: lease.stop())
    if geom:
        _install_window_fixup(context, args, geom.get("mode") == "persona")
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
