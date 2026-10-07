"""Launch-time coherence warnings.

The SDK already defaults the safe things (strips --enable-automation, denies WebRTC leak, disables
Privacy Sandbox, matches the persona to the build). What it CAN'T fix is an operator passing an
incoherent or missing-recommended combination - a proxy with no geo, a spoofed OS the host can't
font-match, a GPU string that contradicts the platform, etc. coherence_warnings() spots those at
launch() and emit_coherence_warnings() prints an actionable line to stderr.

Never blocks the launch; suppressible with quiet=True or CLEARCOTE_NO_WARN=1. coherence_warnings()
is a pure function (no I/O) so it is trivially unit-testable.
"""

import os
import sys

from ._fingerprint import is_fingerprint_passthrough

_SOFTWARE_GPU = ("swiftshader", "llvmpipe", "microsoft basic render", "software adapter", "software")
_seen_notes = set()  # fire-once per process for low-severity NOTE codes
# WARN codes about the HOST rather than one launch's options: said once per process, like a note, so a
# program that launches in a loop is told once.
_ONCE_PER_PROCESS = frozenset({"linux-persona-windows-host"})


def _proxy_server(proxy):
    if not proxy:
        return ""
    if isinstance(proxy, dict):
        return str(proxy.get("server") or "")
    return str(proxy)


def _host_family(host):
    if host.startswith("win"):
        return "windows"
    if host == "darwin" or host.startswith("mac"):
        return "macos"
    if host.startswith("linux"):
        return "linux"
    return None


def _gpu_incoherent(renderer, platform):
    r = renderer.lower()
    if platform == "macos" and ("direct3d" in r or "d3d" in r):
        return "macOS uses Metal/OpenGL, never Direct3D"
    if platform == "windows" and "metal" in r:
        return "Windows uses Direct3D/ANGLE, never Metal"
    if platform == "linux" and ("direct3d" in r or "d3d" in r or "metal" in r):
        return "Linux uses OpenGL/Vulkan, never Direct3D/Metal"
    return None


def coherence_warnings(opts, host_platform=None, build_major=None):
    """Return a list of {severity, code, message} for incoherent/missing-recommended options.
    `opts` is the resolved option dict (fingerprint kwargs + proxy/geoip/headless/_user_args,
    plus `_font_reach`: the (claimed, reachable) font counts _prepare measured, or None)."""
    host = host_platform or sys.platform
    build_major = str(build_major) if build_major is not None else "150"
    out = []
    def warn(code, msg): out.append({"severity": "warn", "code": code, "message": msg})
    def note(code, msg): out.append({"severity": "note", "code": code, "message": msg})

    server = _proxy_server(opts.get("proxy"))
    geoip = bool(opts.get("geoip"))
    tz, lang = opts.get("timezone"), opts.get("accept_language")
    platform = opts.get("platform")
    brand, bver = opts.get("brand"), opts.get("brand_version")
    gpu_r, gpu_v = opts.get("gpu_renderer"), opts.get("gpu_vendor")
    profile = opts.get("fingerprint_profile")
    dgf, noise = opts.get("disable_gpu_fingerprint"), opts.get("fingerprint_noise")
    gpu_string_spoof, canvas_noise = opts.get("gpu_string_spoof"), opts.get("canvas_noise")
    headless = opts.get("headless")
    bridge = opts.get("canvas_bridge")
    bridge_on = bool(bridge.get("url")) if isinstance(bridge, dict) else bool(bridge)
    user_args = opts.get("_user_args") or []

    # --- proxy / geo coherence ---
    if server and not geoip and not tz and not lang:
        warn("proxy-no-geo",
             "proxy set without geoip and no timezone/accept_language - the browser's timezone and "
             "language will reflect THIS host, not the proxy's exit region (a geo-mismatch tell). "
             "Pass geoip=True, or set timezone + accept_language.")
    # No SOCKS + geoip warning: geoip resolves through socks5 (with credentials) since 0.29.0, and
    # a scheme it cannot use fails the launch with a GeoipError that names it.

    # --- persona / cross-signal coherence ---
    fam = _host_family(host)
    if platform and fam and platform != fam and not profile:
        warn("platform-host-fonts",
             "platform=%r but this host is %s and no fingerprint_profile supplies that OS's "
             "fonts/metrics - font, canvas and font-list hashes will be host-native and won't match a "
             "real %s Chrome. Use a fingerprint_profile captured on %s, or set platform=%r."
             % (platform, fam, platform, platform, fam))
    # On a Windows host the GPU runs through Direct3D 11, which clamps the WebGL and WebGPU limits (vertex
    # uniform vectors, a 16384 maximum texture size, ...). A persona can lower a limit, never lift one past the
    # driver's, so a Linux claim here reports limits no real Linux machine has. Pass-through claims nothing.
    # Only a local launch gets here: a Docker launch runs on Linux in its container, a cloud one remotely.
    if (fam == "windows" and str(platform or "").strip().lower() == "linux"
            and not is_fingerprint_passthrough(opts.get("fingerprint"))):
        warn("linux-persona-windows-host",
             "a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL), which no "
             "real Linux machine has. Use platform='windows' on Windows, or run Linux personas on Linux or in "
             "Docker (docker=True).")
    if gpu_r and platform:
        why = _gpu_incoherent(gpu_r, platform)
        if why:
            warn("gpu-platform",
                 "gpu_renderer is incoherent with platform=%r (%s): %r." % (platform, why, gpu_r))
    if gpu_r and any(s in gpu_r.lower() for s in _SOFTWARE_GPU):
        warn("gpu-software",
             "gpu_renderer is a SOFTWARE renderer (%r) - a real consumer machine reports a hardware "
             "GPU. Pin a real GPU string, or use the canvas bridge / a real-GPU host." % gpu_r)
    if brand and str(brand).lower() not in ("chrome", "google chrome"):
        warn("brand-mismatch",
             "brand=%r is advertised in UA-CH, but the binary's TLS/JA4 and engine are Chrome %s - a "
             "UA-vs-transport mismatch strict detectors cross-check. Keep brand=chrome." % (brand, build_major))
    if bver and str(bver).split(".")[0] != build_major:
        warn("version-mismatch",
             "brand_version major %s differs from the build's Chrome %s - JA4/UA-CH version desync. "
             "Align brand_version to %s (or omit it)." % (str(bver).split(".")[0], build_major, build_major))

    # A persona's font list is bounded by what this machine can actually render: the engine
    # reports host INTERSECT list, and a listed-but-uninstalled family still measures absent.
    # That is the safety property -- but it also means a rich donor imported onto a lean host
    # quietly collapses. A 1,513-font profile on a 115-font host yields ~65 fonts and, until
    # now, said nothing, leaving the caller believing they present an identity they do not.
    reach = opts.get("_font_reach")
    if reach and reach[0] >= 40 and reach[1] < reach[0] // 2:
        warn("persona-fonts-unreachable",
             "the fingerprint_profile claims %d fonts but this host can render only %d of them - "
             "the persona's font identity is not achievable here, and what a site sees is that "
             "intersection, not the donor's list. Fonts are the highest-entropy surface measured "
             "(9.45 bits; 35%% of machines are unique on it), so this is a large divergence from "
             "the captured machine. Install the missing families, or use a profile captured on a "
             "host like this one (profile=\"auto\" selects for host coherence)."
             % (reach[0], reach[1]))

    # --- render coherence ---
    # The GPU string can be made real in two different ways, and each leaves a DIFFERENT amount of
    # noise behind, so the advice differs. Fire neither once the operator has already turned the
    # relevant noise off, or the warning contradicts the setting they just made.
    noise_off = noise is False
    # canvas_noise=False is NOT an escape hatch here: under disable_gpu_fingerprint the WebGL
    # readPixels farble stands down but a WebGL canvas's toDataURL/toBlob farble does not (it keys
    # on fingerprint_noise, not canvas_noise), so two reads of one buffer disagree. Measured 2026-09-15.
    if dgf and not noise_off:
        warn("gpu-noise",
             "disable_gpu_fingerprint presents the REAL GPU, but per-eTLD farble still perturbs the "
             "canvas readbacks - noise on otherwise-real pixels is itself a tell. On a WebGL canvas "
             "this mode stands down the readPixels farble but not the toDataURL/toBlob farble, so "
             "two reads of one drawing buffer disagree. fingerprint_noise=False is the fix; "
             "canvas_noise=False silences only the 2D canvas and leaves the WebGL export noised.")
    elif gpu_string_spoof is False and not dgf and not noise_off:
        warn("gpu-noise-string",
             "gpu_string_spoof=False reports the REAL WebGL vendor/renderer, but per-eTLD farble "
             "still perturbs BOTH the canvas 2D readback and gl.readPixels - real string over "
             "noised pixels is itself a tell. fingerprint_noise=False clears both; canvas_noise="
             "False clears only the canvas 2D half and leaves readPixels noised.")
    if gpu_string_spoof is False and not dgf:
        note("gpu-string-only",
             "gpu_string_spoof=False reports the REAL WebGL vendor/renderer while the persona keeps "
             "supplying the getParameter limits, the extension list and the shader precision "
             "formats - AND navigator.gpu (WebGPU) still reports the persona GPU, which WebGL no "
             "longer matches. The GPU name, its capabilities and its WebGPU identity now come from "
             "different sources, and WebGL-vs-WebGPU is readable in one page. That is the contract "
             "of the narrow switch (it moves the WebGL string and nothing else); use "
             "disable_gpu_fingerprint=True to take the whole GPU surface real together.")
    if canvas_noise is False:
        note("canvas-noise-toblob",
             "canvas_noise=False silences canvas 2D getImageData and toDataURL. On engines before "
             "152 r21, canvas.toBlob() / OffscreenCanvas.convertToBlob() were not covered by any "
             "noise switch and could disagree with toDataURL - avoid canvas_noise=False there where "
             "toBlob is scored. From 152 r21 the switch covers the blob exits too and all three "
             "agree byte-for-byte.")
    if headless is not False and not bridge_on and not dgf and not profile:
        note("headless-render",
             "headless with no canvas_bridge/disable_gpu_fingerprint/fingerprint_profile - canvas and "
             "WebGL may render on software here while the persona claims a hardware GPU (a render-vs-"
             "string mismatch on canvas-scored sites). Use canvas_bridge, disable_gpu_fingerprint, or a "
             "real-GPU host.")
    if bridge_on and not gpu_r and not gpu_v and not profile:
        note("bridge-no-gpu",
             "canvas_bridge is set but gpu_vendor/gpu_renderer aren't pinned - the WebGL renderer "
             "string may not match the bridge node's pixels. Set them to the bridge node's GPU.")

    # --- automation hygiene ---
    if any("--enable-automation" in str(a) or str(a).startswith("--remote-debugging-port")
           for a in user_args):
        warn("automation-arg",
             "your args re-introduce an automation flag (--enable-automation / --remote-debugging-port) "
             "the SDK strips by default - a strong webdriver/CDP tell.")
    if opts.get("devtools") or any(str(a).startswith("--auto-open-devtools-for-tabs") for a in user_args):
        warn("devtools-open",
             "DevTools is set to open (devtools=True / --auto-open-devtools-for-tabs). Pages can detect "
             "an open DevTools (debugger and console timing probes; a docked panel also makes "
             "innerWidth/innerHeight disagree with outerWidth/outerHeight). Leave it closed for real runs.")
    if opts.get("user_agent") or any(str(a).startswith("--user-agent=") for a in user_args):
        warn("custom-user-agent",
             "a custom user agent (user_agent= / --user-agent) replaces only the User-Agent string: "
             "navigator.userAgentData, the Sec-CH-UA headers, navigator.platform and the rest of the "
             "persona keep describing the persona, so a different OS or version in the string is a "
             "one-line mismatch. Use platform=, brand= and brand_version= to change what the browser "
             "claims.")
    out.extend(_cdp_exposure(_switch_value(user_args, "--remote-debugging-address"),
                             _switch_value(user_args, "--remote-allow-origins")))
    return out


def _switch_value(args, name):
    """The value of the LAST ``name=value`` in ``args`` (Chromium keeps the last), else None."""
    value = None
    for a in args or ():
        a = str(a)
        if a.startswith(name + "="):
            value = a.split("=", 1)[1]
    return value


def _is_loopback(host):
    h = str(host or "").strip().strip("[]").lower()
    return h in ("localhost", "::1") or h.startswith("127.")


def _cdp_exposure(bind_address, allow_origins):
    """Warnings for a DevTools endpoint reachable beyond this machine or from any web page."""
    out = []
    if bind_address is not None and not _is_loopback(bind_address):
        out.append({"severity": "warn", "code": "cdp-public-bind", "message":
                    "the DevTools endpoint is bound to %s, not loopback: anyone who can reach that port "
                    "can drive the browser, read its cookies and run code in its pages. Keep it on "
                    "127.0.0.1 and tunnel to it if you need remote access." % bind_address})
    if allow_origins is not None and any(o.strip() == "*" for o in str(allow_origins).split(",")):
        out.append({"severity": "warn", "code": "cdp-any-origin", "message":
                    "--remote-allow-origins=* lets any web page this browser (or any browser on this "
                    "machine) opens connect to the DevTools endpoint and take it over. List the origins "
                    "you need instead."})
    return out


def serve_exposure_warnings(host, allow_origins):
    """The cdp-public-bind / cdp-any-origin warnings for ``serve(host=, allow_origins=)``."""
    return _cdp_exposure(host, allow_origins)


def emit_warnings(warnings, quiet=False):
    """Print a list of {severity, code, message} to stderr (unless quiet or CLEARCOTE_NO_WARN)."""
    if quiet or os.environ.get("CLEARCOTE_NO_WARN"):
        return
    for w in warnings:
        label = "warning" if w["severity"] == "warn" else "note"
        print("clearcote: %s: %s" % (label, w["message"]), file=sys.stderr, flush=True)


# Engine-behaviour advisories. These are not a property of the options - they hold for every
# launch - so they do NOT belong in coherence_warnings(), whose contract is "a coherent default is
# silent". They are emitted here instead, once per process, under the same quiet/CLEARCOTE_NO_WARN.
_ENGINE_NOTES = (
    ("cdp-console-events",
     "the engine does not forward console or page-error events to automation clients: "
     "page.on('console') and page.on('pageerror') receive nothing, by design, as part of the "
     "protection against automation-presence probes. In-page window.onerror and "
     "unhandledrejection handlers fire normally. To capture console output, collect it in-page "
     "and read it back with page.evaluate()."),
)


def emit_coherence_warnings(opts, quiet=False, host_platform=None, build_major=None):
    """Print coherence warnings to stderr (unless quiet=True or CLEARCOTE_NO_WARN is set).
    NOTE-level lines (and the host warnings in _ONCE_PER_PROCESS) fire at most once per process; the
    other WARN-level lines fire every launch."""
    if quiet or os.environ.get("CLEARCOTE_NO_WARN"):
        return
    for w in coherence_warnings(opts, host_platform=host_platform, build_major=build_major):
        if w["severity"] == "note" or w["code"] in _ONCE_PER_PROCESS:
            if w["code"] in _seen_notes:
                continue
            _seen_notes.add(w["code"])
        label = "warning" if w["severity"] == "warn" else "note"
        print("clearcote: %s: %s" % (label, w["message"]), file=sys.stderr, flush=True)
    for code, message in _ENGINE_NOTES:
        if code in _seen_notes:
            continue
        _seen_notes.add(code)
        print("clearcote: note: %s" % message, file=sys.stderr, flush=True)
