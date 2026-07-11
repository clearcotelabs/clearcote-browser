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

exe = executable_path()

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

args = fingerprint_args(opts)
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
    "--user-data-dir=/tmp/cc-profile",
] + mode_args + args + extra

env = dict(os.environ)
env.update(linux_font_env(exe))  # point FONTCONFIG_FILE at the bundled Windows-font clones

print(f"[clearcote] CDP endpoint on 0.0.0.0:{port} (proxy -> chrome 127.0.0.1:{internal}) | persona={opts}", flush=True)
os.execvpe(cmd[0], cmd, env)
