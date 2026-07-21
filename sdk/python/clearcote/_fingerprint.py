"""Map Clearcote fingerprint kwargs to Chromium command-line switches.

Switch names mirror components/ungoogled/ungoogled_switches.cc
(see patches/000-fingerprint-switches.patch).
"""

import base64
import gzip
import hashlib
import json
import os
import sys

# kwargs accepted by launch()/launch_persistent_context() that are fingerprint options
# (everything else is passed straight through to Playwright).
FINGERPRINT_KEYS = (
    "fingerprint",
    "platform",
    "platform_version",
    "brand",
    "brand_version",
    "gpu_vendor",
    "gpu_renderer",
    "hardware_concurrency",
    # clearcote-light-stealth: individual native metadata overrides (no persona machinery).
    "device_memory",
    "screen_width",
    "screen_height",
    "avail_width",
    "avail_height",
    "color_depth",
    "device_pixel_ratio",
    "max_touch_points",
    "light_stealth",
    "location",
    "timezone",
    "accept_language",
    "webrtc_ip",
    "webrtc_mdns",
    "disable_gpu_fingerprint",
    "fingerprint_noise",
    "fingerprint_profile",
    "storage_quota",
    "canvas_bridge",
    "tls_profile",
)

# kwarg -> switch name (without leading "--"). disable_gpu_fingerprint is a boolean flag,
# handled separately below.
_FLAGS = {
    "fingerprint": "fingerprint",
    "platform": "fingerprint-platform",
    "platform_version": "fingerprint-platform-version",
    "brand": "fingerprint-brand",
    "brand_version": "fingerprint-brand-version",
    "gpu_vendor": "fingerprint-gpu-vendor",
    "gpu_renderer": "fingerprint-gpu-renderer",
    "hardware_concurrency": "fingerprint-hardware-concurrency",
    # clearcote-light-stealth: direct native-override switches (flag > persona > real).
    "device_memory": "fingerprint-device-memory",
    "screen_width": "fingerprint-screen-width",
    "screen_height": "fingerprint-screen-height",
    "avail_width": "fingerprint-avail-width",
    "avail_height": "fingerprint-avail-height",
    "color_depth": "fingerprint-color-depth",
    "device_pixel_ratio": "fingerprint-device-pixel-ratio",
    "max_touch_points": "fingerprint-max-touch-points",
    "location": "fingerprint-location",
    "timezone": "timezone",
    "webrtc_ip": "webrtc-ip",
    # navigator.storage.estimate().quota in MEGABYTES (a tiny/ephemeral quota reads as a test
    # machine / incognito; set a realistic on-disk value, e.g. 250000 for ~244 GB).
    "storage_quota": "fingerprint-storage-quota",
    # Direct metadata overrides (the "light" path): each beats the persona, which beats the real
    # host value. Emitted whenever the value is not None -- including 0, which is a real value for
    # max_touch_points (a non-touch desktop) rather than "unset".
}


def clean_accept_language(value):
    """Normalize an Accept-Language value for Chromium's ``--accept-lang``: a plain comma-separated
    tag list with NO ``;q=`` weights or spaces (Chromium adds the q-weights to the header itself; a
    ``;`` in the switch value trips a DCHECK and crashes the renderer)."""
    tags = [t.split(";")[0].strip() for t in str(value).split(",")]
    return ",".join(t for t in tags if t)


def encode_profile(value):
    """Encode a captured clearcote-profile for the ``--fingerprint-profile`` switch.

    ``value`` may be a path to a ``.json`` file, a ``dict``, or a JSON string. The profile is
    gzip+base64 encoded so the full capture (≈40 KB) stays well within Chromium's command-line
    length limit (gzip ~6x). The engine base64-decodes + gunzips + parses it and overrides the
    seed-derived persona with the imported values."""
    if isinstance(value, dict):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str) and os.path.isfile(value):
        with open(value, "rb") as handle:
            raw = handle.read()
    else:
        raw = str(value).encode("utf-8")  # assume a JSON string
    return base64.b64encode(gzip.compress(raw, 9)).decode("ascii")


def _profile_accept_language(value):
    """Best-effort: derive an Accept-Language from an imported profile's navigator.languages
    (path / dict / JSON string), so an imported identity keeps the donor's language order."""
    try:
        if isinstance(value, dict):
            obj = value
        elif isinstance(value, (bytes, bytearray)):
            obj = json.loads(value)
        elif isinstance(value, str) and os.path.isfile(value):
            with open(value, encoding="utf-8") as handle:
                obj = json.load(handle)
        else:
            obj = json.loads(value)
    except (ValueError, OSError):
        return None
    langs = (obj.get("navigator") or {}).get("languages") if isinstance(obj, dict) else None
    if isinstance(langs, list) and langs:
        return ",".join(str(tag) for tag in langs)
    return None


def _major_from_version(value):
    """Parse the leading integer (the major) out of a version string like '120.0.6099.109' -> 120.
    Returns None if there's no leading integer."""
    if value is None:
        return None
    head = str(value).strip().split(".")[0]
    return int(head) if head.isdigit() else None


def resolve_tls_profile(value, opts):
    """Resolve the ``tls_profile`` option to a concrete ``--fingerprint-tls-profile`` value, or None.

    Keeps the TLS ClientHello coherent with the persona's *claimed* Chromium major (the network layer
    follows the UA instead of always emitting the build's native TLS):

    - ``"match-persona"`` / ``"auto"`` (default): follow the persona's claimed Chrome major, taken
      from ``brand_version``. With no ``brand_version`` set, the persona claims the browser's native
      version, so this returns ``None`` (native TLS — already coherent).
    - ``None`` / ``""`` / ``"native"`` / ``"off"``: ``None`` (native TLS, no switch).
    - ``"chrome-<major>"`` or an int/str major (e.g. ``120``): pinned to that major.

    Any unrecognized value resolves to ``None`` — the engine never breaks the handshake on a bad
    value, and neither do we. Chromium-core only (Chrome/Edge/Brave/Opera share the ClientHello; the
    brand differs only in headers/UA-CH, not TLS), so the resolved value is always ``chrome-<major>``.
    """
    if value in (None, "", "native", "off"):
        return None
    if value in ("match-persona", "auto"):
        major = _major_from_version(opts.get("brand_version"))
        return f"chrome-{major}" if major else None
    if isinstance(value, int):
        return f"chrome-{value}"
    text = str(value).strip().lower()
    if text.startswith("chrome-") and text[len("chrome-"):].isdigit():
        return text
    if text.isdigit():
        return f"chrome-{text}"
    return None


# Primary Accept-Language tag -> a plausible IANA timezone, so the default persona's timezone is
# coherent with its locale instead of leaking the host's (often UTC on servers/containers). Not
# geo-truth — set geoip=True (resolve the proxy exit-IP) or an explicit timezone= for accuracy.
_LOCALE_TZ = {
    "en-US": "America/New_York", "en-CA": "America/Toronto", "en-GB": "Europe/London",
    "en-AU": "Australia/Sydney", "en-NZ": "Pacific/Auckland", "en-IE": "Europe/Dublin",
    "de-DE": "Europe/Berlin", "de-AT": "Europe/Vienna", "fr-FR": "Europe/Paris",
    "es-ES": "Europe/Madrid", "es-MX": "America/Mexico_City", "it-IT": "Europe/Rome",
    "nl-NL": "Europe/Amsterdam", "pt-BR": "America/Sao_Paulo", "pt-PT": "Europe/Lisbon",
    "pl-PL": "Europe/Warsaw", "sv-SE": "Europe/Stockholm", "ja-JP": "Asia/Tokyo",
    "ko-KR": "Asia/Seoul", "zh-CN": "Asia/Shanghai", "zh-TW": "Asia/Taipei",
    "ru-RU": "Europe/Moscow", "tr-TR": "Europe/Istanbul", "ar-SA": "Asia/Riyadh",
    "hi-IN": "Asia/Kolkata", "id-ID": "Asia/Jakarta",
}


def _default_timezone(primary_lang):
    """A plausible IANA timezone for a primary Accept-Language tag (``en-US`` -> ``America/New_York``),
    so the default persona's timezone is coherent with its locale rather than leaking the host's UTC.
    Falls back by language subtag, then to America/New_York (matching the en-US Accept-Language default)."""
    if not primary_lang:
        return None
    tag = primary_lang.strip()
    if tag in _LOCALE_TZ:
        return _LOCALE_TZ[tag]
    lang = tag.split("-")[0].lower()
    for key, tz in _LOCALE_TZ.items():
        if key.lower().startswith(lang + "-"):
            return tz
    return "America/New_York"


# Coherent Windows-plausible desktop/laptop metadata bundles:
# (screen_w, screen_h, avail_w, avail_h, dpr, color_depth, device_memory_gb, hw_concurrency).
# screen_* are LOGICAL (CSS) px; avail_h subtracts a ~40px taskbar; dpr matches the
# scaling that produces that logical size; all mouse-only desktops (max_touch=0).
_LIGHT_STEALTH_PROFILES = (
    (1920, 1080, 1920, 1040, 1.0, 24, 8, 8),     # FHD desktop
    (1920, 1080, 1920, 1040, 1.0, 24, 16, 12),   # FHD desktop, mid
    (1920, 1080, 1920, 1040, 1.0, 24, 16, 16),   # FHD desktop, high
    (2560, 1440, 2560, 1400, 1.0, 24, 16, 16),   # QHD desktop
    (2560, 1440, 2560, 1400, 1.5, 24, 16, 12),   # 4K @150% -> logical QHD
    (1536, 864, 1536, 824, 1.25, 24, 8, 8),      # FHD laptop @125%
    (1536, 864, 1536, 824, 1.25, 24, 16, 12),    # FHD laptop @125%, mid
    (1366, 768, 1366, 728, 1.0, 24, 8, 4),       # HD laptop
    (1366, 768, 1366, 728, 1.0, 24, 4, 4),       # HD laptop, budget
    (1440, 900, 1440, 860, 1.0, 24, 8, 8),       # 16:10 laptop
    (1600, 900, 1600, 860, 1.0, 24, 8, 8),       # HD+ desktop
    (1680, 1050, 1680, 1010, 1.0, 24, 8, 8),     # 16:10 desktop
    (1920, 1200, 1920, 1160, 1.0, 24, 16, 12),   # 16:10 FHD+ desktop
    (3840, 2160, 3840, 2120, 1.0, 24, 32, 16),   # 4K native @100%
)


def _light_stealth_values(seed):
    """Deterministic, coherent metadata bundle applied via the NATIVE override switches only
    (never --fingerprint), so the persona machinery / farble hooks that strict anti-bots detect
    stay dormant and rendering surfaces (canvas / WebGL / audio / fonts) stay at the real host
    values -- which is what passes.

    Spoofs ONLY the metadata axes that survive strict anti-bot checks: hardware_concurrency,
    device_memory, color_depth, device_pixel_ratio, max_touch_points. It deliberately does NOT
    spoof screen / avail dimensions -- a faked screen size cannot be reconciled with the real
    window/render surface and is a reliable block trigger -- so screen stays REAL by default.
    Opt into a screen spoof by passing screen_width=/screen_height=/avail_width=/avail_height=
    explicitly (best when the host's real display actually matches)."""
    key = str(seed if seed not in (None, "") else "clearcote-light-stealth")
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    _sw, _sh, _aw, _ah, dpr, depth, mem, hw = _LIGHT_STEALTH_PROFILES[h % len(_LIGHT_STEALTH_PROFILES)]
    # Present the browser's REAL version -- do NOT spoof brand_version. A Chrome-major lie moves
    # the UA-CH version AND (via tls_profile="match-persona") the TLS ClientHello off the genuine
    # binary, while the binary's real JS/engine surface still reflects its true version -- another
    # coherence tell strict anti-bots block. Callers can still pass brand_version= to opt into it.
    return {
        "device_pixel_ratio": dpr, "color_depth": depth, "device_memory": mem,
        "hardware_concurrency": hw, "max_touch_points": 0,
        "brand": "chrome",
    }


def fingerprint_args(opts):
    """Build the Chromium switches for a dict of fingerprint options."""
    args = []
    opts = dict(opts)  # clearcote-light-stealth: never mutate the caller's fp dict
    if opts.get("light_stealth"):
        # Fill in a coherent metadata bundle via native override switches. setdefault
        # semantics: an explicit caller kwarg (e.g. device_memory=16) wins over the preset.
        for _k, _v in _light_stealth_values(opts.get("fingerprint")).items():
            if opts.get(_k) in (None, ""):
                opts[_k] = _v
        # CRITICAL: never emit --fingerprint, so CurrentPersona()/farble never engage;
        # every value then takes the C++ flag > real path (no persona machinery).
        opts.pop("fingerprint", None)
        # Size the REAL window to fit the spoofed screen so inner <= outer <= avail <=
        # screen (window.outer*/screenX/Y stay coherent). Emitted in base args -> a
        # user-supplied --window-size still wins (merged after these).
        try:
            args.append("--window-size={},{}".format(int(opts["avail_width"]), int(opts["avail_height"])))
        except (KeyError, TypeError, ValueError):
            pass
    for key, flag in _FLAGS.items():
        value = opts.get(key)
        if value is not None and value != "":
            args.append(f"--{flag}={value}")
    # Only "off" is meaningful: concealment ON is both the Chromium default and real Chrome's
    # behaviour, so there is nothing to emit for "on".
    #
    # This uses Chromium's OWN feature flag rather than a clearcote switch. The mDNS responder is
    # created in PeerConnectionDependencyFactory behind kWebRtcHideLocalIpsWithMdns; disabling the
    # feature means no responder is built and host candidates are signalled as raw IPs. Verified
    # end-to-end: with the flag, host candidates come back as 192.168.x.x; without it, <uuid>.local.
    # merge_feature_flags folds this into any other --disable-features value.
    if str(opts.get("webrtc_mdns") or "").lower() == "off":
        args.append("--disable-features=WebRtcHideLocalIpsWithMdns")
    # Default the persona platform to the HOST OS, so it's coherent with the binary the SDK ships for
    # this machine (Windows binary -> windows persona, Linux binary -> linux persona) rather than a
    # seed-derived OS that could vary. Override explicitly via platform="windows"/"linux"/"macos".
    if not opts.get("platform"):
        host = {"win32": "windows", "linux": "linux", "darwin": "macos"}.get(sys.platform, "windows")
        args.append(f"--fingerprint-platform={host}")
    # clearcote presents as Google Chrome (its UA string says "Chrome/<v>"), so default the
    # UA-CH brand to "chrome" — otherwise navigator.userAgentData advertises only "Chromium",
    # a UA/UA-CH mismatch some bot detectors flag. Override via brand="edge" etc.
    if not opts.get("brand"):
        args.append("--fingerprint-brand=chrome")
    accept_language = opts.get("accept_language")
    if not accept_language and opts.get("fingerprint_profile"):
        accept_language = _profile_accept_language(opts["fingerprint_profile"])
    if not accept_language:
        # Always send a coherent Accept-Language. Without --accept-lang Chromium falls back to the
        # build/OS locale, which can leak a language that mismatches the proxy's country/timezone
        # (e.g. en-GB on a US IP) — a geo-inconsistency tell. en-US,en is the common Chrome default;
        # set accept_language (or geoip) to match the proxy region.
        accept_language = "en-US,en"
    clean_lang = clean_accept_language(accept_language)
    args.append(f"--accept-lang={clean_lang}")
    # Also pin the UI/ICU locale to the PRIMARY Accept-Language tag, so Intl.DateTimeFormat /
    # NumberFormat / Collator (main thread AND workers) resolve to the same locale as
    # navigator.language. Without --lang, Chromium falls back to the build/OS locale (e.g. en-GB on an
    # en-US persona) -- a locale-incoherence tell (navigator.language=en-US but Intl=en-GB).
    primary_lang = clean_lang.split(",")[0]
    if primary_lang:
        args.append(f"--lang={primary_lang}")
    # Default the timezone to one coherent with the persona locale when none is set (and geoip didn't
    # resolve one), so a server/container run doesn't leak the host's UTC (a datacenter tell) while
    # navigator.language says e.g. en-US. geoip=True or an explicit timezone= override this.
    if not opts.get("timezone"):
        default_tz = _default_timezone(primary_lang)
        if default_tz:
            args.append(f"--timezone={default_tz}")
    # disable_gpu_fingerprint=True presents the machine's REAL GPU instead of a spoofed one: WebGL
    # UNMASKED_VENDOR/RENDERER, the getParameter table, and the canvas/WebGL render all report the
    # genuine host backend. The most coherent setting vs strict tampering classifiers — the GPU
    # string and the rendered pixels match, so there's no GPU spoof to catch. Composes with
    # fingerprint_profile (the profile still supplies screen/fonts/audio/hardware, but the real host
    # GPU is kept, not the profile's GPU which the host can't render); pair with fingerprint_noise=
    # False so the readback isn't perturbed. Trade-off: personas on one machine share the GPU/canvas
    # identity (linkable) — best for single-identity/per-host use.
    if opts.get("disable_gpu_fingerprint"):
        args.append("--disable-gpu-fingerprint")
    # fingerprint_noise=False turns OFF the per-eTLD+1 farbling noise (canvas/WebGL/audio/
    # client-rects) so those surfaces return natural, unperturbed values — useful when a site's
    # anti-bot ML scores the noise pattern as "tampered". Identity spoofs
    # (UA/screen/GPU/persona) stay on. Default (unset/True) keeps the noise.
    if opts.get("fingerprint_noise") is False:
        args.append("--disable-fingerprint-noise")
    # fingerprint_profile imports a real captured fingerprint (path/dict/JSON) — see
    # tools/fingerprint-collect. Its fields override the seed-derived persona; absent fields
    # fall back to the seed, so partial profiles stay coherent.
    profile = opts.get("fingerprint_profile")
    if profile:
        args.append(f"--fingerprint-profile={encode_profile(profile)}")
    # canvas_bridge forwards canvas/WebGL readbacks to a remote real-GPU host. Passed as a dict:
    # {"url": "ws://host:port", "auth": "user:secret", "mode": "off|all|allow|deny",
    #  "allow": [...eTLD+1], "deny": [...eTLD+1], "fallback": "block|local"}. Enabling it (url set)
    # requires --no-sandbox (the bridge opens its socket from the renderer). Latency note: a
    # synchronous readback is a network round-trip on the renderer thread; the engine prefetches+
    # caches deferred/animated reads, and fallback="local" serves a cold miss locally instead of
    # stalling. Use mode to restrict bridging to origins where canvas coherence is actually scored.
    cb = opts.get("canvas_bridge")
    if cb and cb.get("url"):
        args.append(f"--canvas-bridge-url={cb['url']}")
        if cb.get("auth"):
            args.append(f"--canvas-bridge-auth={cb['auth']}")
        if cb.get("mode"):
            args.append(f"--canvas-bridge-mode={cb['mode']}")
        if cb.get("allow"):
            args.append("--canvas-bridge-allow=" + ",".join(cb["allow"]))
        if cb.get("deny"):
            args.append("--canvas-bridge-deny=" + ",".join(cb["deny"]))
        if cb.get("fallback"):
            args.append(f"--canvas-bridge-fallback={cb['fallback']}")
        if "--no-sandbox" not in args:
            args.append("--no-sandbox")
    # tls_profile keeps the TLS ClientHello coherent with the persona's claimed Chrome version
    # (the network layer follows the UA). Default "match-persona" follows brand_version; an explicit
    # "chrome-<major>" pins it; "native"/off/unset leaves the build's native TLS. Only the
    # version-variant ClientHello fields (post-quantum key-share group, ALPS codepoint) change —
    # cipher list, version bounds, and per-connection extension permutation stay real-Chrome.
    tls_switch = resolve_tls_profile(opts.get("tls_profile", "match-persona"), opts)
    if tls_switch:
        args.append(f"--fingerprint-tls-profile={tls_switch}")
    # platform="android" is a best-effort MOBILE persona (Android UA + Sec-CH-UA-Mobile, touch,
    # pointer:coarse, mobile screen/DPR, Mali/Adreno WebGL, plugins=0, mobile viewport). The mobile
    # viewport needs a phone-sized window (Chromium's ~500px min width floor still applies); auto-set
    # one — a caller-supplied --window-size (in args) overrides it. On a desktop engine the GPU
    # render + fine geometry stay desktop (documented residual tells); pair with canvas_bridge.
    if opts.get("platform") == "android":
        args.append("--window-size=412,915")
    return args
