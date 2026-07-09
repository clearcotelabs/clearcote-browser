#!/usr/bin/env python3
"""clearcote CDP-server entrypoint.

Launches the clearcote stealth Chromium headless with a DevTools/CDP endpoint reachable on
0.0.0.0:$CC_PORT (default 9222), so any Playwright / Puppeteer / browser-use / Crawl4AI /
Stagehand client attaches over CDP and keeps its own automation code. The persona is
configured entirely from CC_* env vars.

  docker run -d -p 9222:9222 teamflatearth/clearcote
  # then, from the host:  playwright.chromium.connect_over_cdp("http://localhost:9222")

Modern Chrome binds the DevTools endpoint to 127.0.0.1 only (a security restriction;
--remote-debugging-address is ignored), so we run a tiny socat TCP proxy to publish it.
"""
import os
import subprocess
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

cmd = [
    exe,
    "--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
    # container has no GPU -> ANGLE/SwiftShader so WebGL/WebGPU stay coherent
    "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
    f"--remote-debugging-port={internal}", "--remote-allow-origins=*",
    "--user-data-dir=/tmp/cc-profile",
] + args + extra

env = dict(os.environ)
env.update(linux_font_env(exe))  # point FONTCONFIG_FILE at the bundled Windows-font clones

print(f"[clearcote] CDP endpoint on 0.0.0.0:{port} (proxy -> chrome 127.0.0.1:{internal}) | persona={opts}", flush=True)
os.execvpe(cmd[0], cmd, env)
