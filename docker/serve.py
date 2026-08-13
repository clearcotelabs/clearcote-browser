#!/usr/bin/env python3
"""clearcote CDP-server entrypoint.

Launches the clearcote stealth Chromium — HEADFUL on a virtual X display (Xvfb) by default, so
a real headed browser avoids the headless-mode tells some detectors probe; set CC_HEADLESS=1 to
force the old pure-headless mode — with a DevTools/CDP endpoint reachable on 0.0.0.0:$CC_PORT
(default 9222), so any Playwright / Puppeteer / browser-use / Crawl4AI / Stagehand client
attaches over CDP and keeps its own automation code. The persona is configured entirely from
CC_* env vars.

  docker run -d -p 9222:9222 teamflatearth/clearcote
  # then, from the host:  playwright.chromium.connect_over_cdp("http://localhost:9222")

Modern Chrome binds the DevTools endpoint to 127.0.0.1 only (a security restriction;
--remote-debugging-address is ignored), so we run a tiny socat TCP proxy to publish it.
"""
import os
import subprocess
import time
from clearcote import executable_path
from clearcote._fingerprint import fingerprint_args
from clearcote._fonts import linux_font_env
from clearcote._launchopts import web_bluetooth_args

PROFILE_DIR = os.environ.get("CC_PROFILE_DIR", "/tmp/cc-profile")

# The image bakes in the FREE engine at build time. With CLEARCOTE_LICENSE_KEY set, resolve the
# licensed build instead -- passing the key and any CC_VERSION pin explicitly, because a bare
# executable_path() returns whatever is already cached, which is how a keyed container ends up
# silently running the free engine. CC_VERSION accepts a major ("151"), an exact build, or a PRO
# revision ("r14"); omit it for the newest build your licence allows.
#
# The download lands in XDG_CACHE_HOME (/opt/xdg-cache), which is declared a VOLUME so it survives
# container replacement. Mount a named volume in production or every new container re-fetches
# ~180 MB:   -v clearcote-cache:/opt/xdg-cache
_license = os.environ.get("CLEARCOTE_LICENSE_KEY") or None
_version = os.environ.get("CC_VERSION") or None
_kwargs = {}
if _license:
    _kwargs["license_key"] = _license
if _version:
    _kwargs["version"] = _version

try:
    exe = executable_path(**_kwargs)
except TypeError:
    # An SDK older than 0.16 has no version/license_key parameters. Fall back, but say so loudly:
    # silently serving the free engine to a licensed caller is the exact bug this code fixes.
    print("[clearcote] WARNING: this image's SDK predates licensed build selection, so the "
          "bundled FREE engine is being used. Rebuild the image to honour CLEARCOTE_LICENSE_KEY.",
          flush=True)
    exe = executable_path()

print("[clearcote] engine: %s (%s)" % (exe, "licensed" if _license else "free"), flush=True)

opts = {
    "fingerprint": os.environ.get("CC_FINGERPRINT", "clearcote-docker"),
    "platform": os.environ.get("CC_PLATFORM", "linux"),
}
_ENV_TO_OPT = {
    "CC_BRAND": "brand", "CC_BRAND_VERSION": "brand_version",
    "CC_ACCEPT_LANGUAGE": "accept_language", "CC_TIMEZONE": "timezone",
    "CC_HARDWARE_CONCURRENCY": "hardware_concurrency",
    "CC_GPU_VENDOR": "gpu_vendor", "CC_GPU_RENDERER": "gpu_renderer",
    "CC_TLS_PROFILE": "tls_profile", "CC_STORAGE_QUOTA": "storage_quota",
}
for env_key, opt_key in _ENV_TO_OPT.items():
    val = os.environ.get(env_key)
    if val:
        opts[opt_key] = val

# Widevine CDM -- seeded ONLY for a Windows persona on this Linux host, which is where its absence
# is a measured contradiction: a build branded Google Chrome that claims Windows and carries no CDM
# is readable by any page (audit: "a build branded Google Chrome carries Google's Widevine CDM").
# A Linux persona is NOT flagged for this, so the default container behaviour stays unchanged.
# Force either way with CC_WIDEVINE=1 / CC_WIDEVINE=0.
#
# The CDM fetched is host-shaped (libwidevinecdm.so here), not persona-shaped -- correct, since a
# Linux binary can only load a .so. Cached in the engine volume rather than $HOME so it is fetched
# once, not per container. Best-effort: DRM must never stop the server coming up.
_wv = os.environ.get("CC_WIDEVINE")
_wv_on = (_wv not in ("0", "false", "no")) if _wv else (opts.get("platform") == "windows")
if _wv_on:
    os.environ.setdefault("CLEARCOTE_WIDEVINE_DIR",
                          os.path.join(os.environ.get("XDG_CACHE_HOME", "/opt/xdg-cache"),
                                       "clearcote", "WidevineCdm"))
    try:
        from clearcote._widevine import seed_widevine

        seed_widevine(PROFILE_DIR, quiet=True)
        print("[clearcote] widevine CDM seeded", flush=True)
    except Exception as exc:  # noqa: BLE001 -- DRM is best-effort
        print("[clearcote] widevine unavailable (continuing without DRM): %r" % exc, flush=True)

args = fingerprint_args(opts)
# Web Bluetooth is runtime-disabled on Linux only, so a Linux container serving a desktop persona
# reports navigator.usb/serial/hid but NOT navigator.bluetooth -- a combination no real desktop
# Chrome produces. web_bluetooth_args() restores it (and is empty on non-Linux). The SDK's launch()
# adds this already; this entrypoint builds its own argv, so it has to ask for it too.
port = os.environ.get("CC_PORT", "9222")               # externally exposed port
internal = os.environ.get("CC_INTERNAL_PORT", "9223")  # chrome's loopback DevTools port
extra = os.environ.get("CC_EXTRA_ARGS", "").split()

# publish the loopback-only DevTools endpoint: 0.0.0.0:$port -> 127.0.0.1:$internal
subprocess.Popen(
    ["socat", f"TCP-LISTEN:{port},fork,reuseaddr,bind=0.0.0.0", f"TCP:127.0.0.1:{internal}"]
)

# Display mode: default is HEADFUL on a virtual X display (Xvfb) — a real headed browser avoids
# the headless-mode tells some detectors probe. Set CC_HEADLESS=1 to force pure-headless (no Xvfb).
# Either way the container has no GPU, so WebGL/WebGPU still go through ANGLE/SwiftShader (below).
headless = os.environ.get("CC_HEADLESS", "").strip().lower() in ("1", "true", "yes")
mode_args = []
if headless:
    mode_args = ["--headless=new"]
    print("[clearcote] display: pure headless (CC_HEADLESS set)", flush=True)
else:
    display = os.environ.get("DISPLAY") or ":99"
    if not os.environ.get("DISPLAY"):  # start our own Xvfb only if the host didn't provide a display
        screen = os.environ.get("CC_SCREEN", "1920x1080x24")
        subprocess.Popen(["Xvfb", display, "-screen", "0", screen, "-nolisten", "tcp", "-ac"])
        sock = "/tmp/.X11-unix/X" + display.lstrip(":").split(".")[0]
        for _ in range(100):  # wait up to ~10s for the virtual display to come up
            if os.path.exists(sock):
                break
            time.sleep(0.1)
    os.environ["DISPLAY"] = display  # inherited by chrome via `env` below
    print(f"[clearcote] display: headful on Xvfb {display}", flush=True)

cmd = [
    exe,
    "--no-sandbox", "--disable-dev-shm-usage",
    # container has no GPU -> ANGLE/SwiftShader so WebGL/WebGPU stay coherent
    "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    f"--remote-debugging-port={internal}", "--remote-allow-origins=*",
    "--user-data-dir=%s" % PROFILE_DIR,
] + mode_args + args + web_bluetooth_args() + extra

env = dict(os.environ)
env.update(linux_font_env(exe))  # point FONTCONFIG_FILE at the bundled Windows-font clones

# A PRO engine refuses to launch without a run token: the licence gate reads CLEARCOTE_RUN_TOKEN
# once at startup and exits if it is missing or invalid. The SDK's own launch() mints one, but this
# entrypoint exec's chrome directly, so check a lease out here and inject it.
#
# execvpe REPLACES this process, so the heartbeat thread and the atexit check-in do not survive.
# That is fine: the gate only reads the token at startup, and the lease expires on its own TTL. On
# a concurrency-limited plan it means the slot is held until that TTL rather than released at exit.
if _license:
    try:
        from clearcote._license import acquire_lease

        _lease = acquire_lease(_license, quiet=False)
        if _lease and _lease.token:
            env["CLEARCOTE_RUN_TOKEN"] = _lease.token
            print("[clearcote] licence lease acquired", flush=True)
        else:
            print("[clearcote] WARNING: no lease returned for this key; the PRO engine will "
                  "refuse to start.", flush=True)
    except Exception as exc:  # noqa: BLE001 -- surface the reason, never launch a doomed browser
        print("[clearcote] ERROR: could not lease a run token (%s: %s). The PRO engine will not "
              "start. Check the key, the plan's concurrency limit, and outbound network access."
              % (type(exc).__name__, exc), flush=True)
        raise SystemExit(1)

print(f"[clearcote] CDP endpoint on 0.0.0.0:{port} (proxy -> chrome 127.0.0.1:{internal}) | persona={opts}", flush=True)
os.execvpe(cmd[0], cmd, env)
