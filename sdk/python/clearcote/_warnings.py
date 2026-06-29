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

_SOFTWARE_GPU = ("swiftshader", "llvmpipe", "microsoft basic render", "software adapter", "software")
_seen_notes = set()  # fire-once per process for low-severity NOTE codes


def _proxy_server(proxy):
    if not proxy:
        return ""
    if isinstance(proxy, dict):
        return str(proxy.get("server") or "")
    return str(proxy)


def _is_socks(server):
    s = server.lower()
    return s.startswith("socks") or "://socks" in s


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
    `opts` is the resolved option dict (fingerprint kwargs + proxy/geoip/headless/_user_args)."""
    host = host_platform or sys.platform
    build_major = str(build_major) if build_major is not None else "149"
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
    if server and geoip and _is_socks(server):
        warn("socks-geoip",
             "geoip cannot resolve a SOCKS proxy's exit IP - timezone/language will NOT auto-match. "
             "Set timezone + accept_language (+ webrtc_ip) manually for SOCKS proxies.")

    # --- persona / cross-signal coherence ---
    fam = _host_family(host)
    if platform and fam and platform != fam and not profile:
        warn("platform-host-fonts",
             "platform=%r but this host is %s and no fingerprint_profile supplies that OS's "
             "fonts/metrics - font, canvas and font-list hashes will be host-native and won't match a "
             "real %s Chrome. Use a fingerprint_profile captured on %s, or set platform=%r."
             % (platform, fam, platform, platform, fam))
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

    # --- render coherence ---
    if dgf and noise is not False:
        warn("gpu-noise",
             "disable_gpu_fingerprint presents the REAL GPU, but per-eTLD farble still perturbs the "
             "canvas/WebGL readback - noise on otherwise-real pixels is itself a tell. Pair with "
             "fingerprint_noise=False.")
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
    return out


def emit_coherence_warnings(opts, quiet=False, host_platform=None, build_major=None):
    """Print coherence warnings to stderr (unless quiet=True or CLEARCOTE_NO_WARN is set).
    NOTE-level lines fire at most once per process; WARN-level fire every launch."""
    if quiet or os.environ.get("CLEARCOTE_NO_WARN"):
        return
    for w in coherence_warnings(opts, host_platform=host_platform, build_major=build_major):
        if w["severity"] == "note":
            if w["code"] in _seen_notes:
                continue
            _seen_notes.add(w["code"])
        label = "warning" if w["severity"] == "warn" else "note"
        print("clearcote: %s: %s" % (label, w["message"]), file=sys.stderr, flush=True)
