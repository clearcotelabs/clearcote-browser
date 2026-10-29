#!/usr/bin/env python3
"""clearcote CDP-server entrypoint.

Launches the clearcote stealth Chromium — HEADFUL on a virtual X display (Xvfb) by default, so
a real headed browser avoids the headless-mode tells some detectors probe; set CC_HEADLESS=1 to
force the old pure-headless mode — with a DevTools/CDP endpoint reachable on 0.0.0.0:$CC_PORT
(default 9222), so any Playwright / Puppeteer / browser-use / Crawl4AI / Stagehand client
attaches over CDP and keeps its own automation code. The persona is configured entirely from
CC_* env vars.

  docker run -d --rm --shm-size=1g -p 127.0.0.1:9222:9222 teamflatearth/clearcote
  # then, from the host:  playwright.chromium.connect_over_cdp("http://localhost:9222")

Modern Chrome binds the DevTools endpoint to 127.0.0.1 only (a security restriction;
--remote-debugging-address is ignored), so we run a tiny socat TCP proxy to publish it.

Chrome runs WITH its sandbox whenever the container allows it (sandbox_check.py): run the container with
--security-opt seccomp=<the profile at /etc/clearcote/seccomp.json>. Without it, --no-sandbox, and a log
line saying why.
"""
import base64  # r51+: the persona token's payload carries the display (see _persona_token_display)
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from clearcote import executable_path
from clearcote._fingerprint import fingerprint_args
from clearcote._fonts import linux_font_env
from clearcote._fingerprint import persona_platform
from clearcote._launchopts import merge_feature_flags, serve_infobar_args, web_bluetooth_args
try:  # r51+: server-side personas (the signed persona token); an older SDK in the image has no module
    from clearcote import _persona as _persona_mod
except ImportError:
    _persona_mod = None
try:
    from clearcote._launchopts import mesa_egl_available
except ImportError:  # an image built with an SDK older than this entrypoint: same check, inline
    def mesa_egl_available():
        import glob
        dirs = ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/usr/lib")
        has = lambda name: any(os.path.exists(os.path.join(d, name)) for d in dirs)  # noqa: E731
        return (has("libEGL.so.1") and has("libEGL_mesa.so.0")
                and (has("dri/swrast_dri.so") or has("dri/kms_swrast_dri.so")
                     or any(glob.glob(os.path.join(d, "libgallium-*.so")) for d in dirs)))
try:
    from clearcote._containerseed import container_seed
except ImportError:  # an image built with an SDK older than this entrypoint: same behaviour, inline
    def container_seed(env_value, profile_dir):
        import secrets
        if env_value is not None:
            return str(env_value), "env"
        path = os.path.join(profile_dir, ".clearcote-seed")
        try:
            with open(path, encoding="utf-8") as handle:
                saved = handle.read().strip()
            if saved:
                return saved, "saved"
        except OSError:
            pass
        seed = "cc-" + secrets.token_hex(8)
        try:
            os.makedirs(profile_dir, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(seed + "\n")
        except OSError:
            pass
        return seed, "new"

PROFILE_DIR = os.environ.get("CC_PROFILE_DIR", "/tmp/cc-profile")

# CC_SECRETS_FILE: a JSON file of {"CLEARCOTE_LICENSE_KEY": ..., "CC_PROXY": ...}, read once and deleted.
# Anything passed with `docker run -e` is shown by `docker inspect` to whoever can reach the Docker daemon;
# the SDK's macOS launch() copies this file into the container instead (docker cp), so a licence key or a
# proxy password stays out of the container's configuration. Only these two names are taken from it.
_SECRET_NAMES = ("CLEARCOTE_LICENSE_KEY", "CC_PROXY")
_secrets_file = os.environ.get("CC_SECRETS_FILE", "").strip()
if _secrets_file:
    try:
        with open(_secrets_file, encoding="utf-8") as _fh:
            _secrets = json.load(_fh)
        if not isinstance(_secrets, dict):
            raise ValueError("not a JSON object")
        for _name in _SECRET_NAMES:
            if isinstance(_secrets.get(_name), str) and _secrets[_name]:
                os.environ[_name] = _secrets[_name]
    except Exception as exc:  # noqa: BLE001 -- a licensed caller must not silently get the free engine
        print("[clearcote] ERROR: CC_SECRETS_FILE=%s could not be read (%s: %s). Refusing to start."
              % (_secrets_file, type(exc).__name__, exc), flush=True)
        raise SystemExit(1)
    finally:
        try:
            os.remove(_secrets_file)
        except OSError:
            pass

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

# A PRO engine refuses to run without a run token (the licence gate reads CLEARCOTE_RUN_TOKEN once at
# startup), and this entrypoint starts chrome itself, so the lease is checked out here, as early as
# possible: the persona display below runs the engine's validator before Xvfb starts, and a key that
# cannot lease fails the container before anything else is set up. The lease lives as long as this
# process: chrome runs as its CHILD, so the heartbeat keeps the slot while the browser runs and releases
# it when it stops (exec'ing chrome used to kill the heartbeat). The token is also mirrored into a
# per-launch file (CLEARCOTE_RUN_TOKEN_FILE) that follows the lease's rotation: a supporting engine
# (152 r23+) re-reads it and stops a running FREE browser once the token stops advancing.
_lease = None
_token_file = None
_release_token_file = None
if _license:
    try:
        from clearcote._license import acquire_lease

        _lease = acquire_lease(_license, quiet=False)
        if _lease and _lease.token:
            try:
                _token_file, _release_token_file = _lease.bind_launch()
            except Exception as exc:  # noqa: BLE001 -- an older SDK has no bind_launch; PRO still runs
                print("[clearcote] WARNING: this SDK cannot keep the run-token fresh (%s: %s); a "
                      "FREE licence needs a newer clearcote package." % (type(exc).__name__, exc),
                      flush=True)
            print("[clearcote] licence lease acquired", flush=True)
        else:
            print("[clearcote] WARNING: no lease returned for this key; the PRO engine will "
                  "refuse to start.", flush=True)
    except Exception as exc:  # noqa: BLE001 -- surface the reason, never launch a doomed browser
        print("[clearcote] ERROR: could not lease a run token (%s: %s). The PRO engine will not "
              "start. Check the key, the plan's concurrency limit, and outbound network access."
              % (type(exc).__name__, exc), flush=True)
        raise SystemExit(1)


def _engine_revision(path):
    """The PRO revision of an auto-downloaded engine from its path (".../pro-155.0.8059.39-r47/..."), or
    None for a path that does not carry one (a caller's own binary, the free build)."""
    m = re.search(r"-r(\d+)(?=[/\\]|$)", str(path or ""))
    return int(m.group(1)) if m else None


def _engine_has_switch(name):
    try:
        from clearcote._launchopts import engine_supports_switch
    except ImportError:  # an image built with an SDK older than this entrypoint: the same probe
        from proxy_relay import engine_supports_switch
    try:
        return bool(engine_supports_switch(exe, name))
    except Exception:  # noqa: BLE001 -- a probe failure means "no", never a container that will not boot
        return False

# The seed is the identity. Without CC_FINGERPRINT each container now gets its own random one, kept
# in the profile directory (it used to be the fixed "clearcote-docker", so every such container was
# the same device and any two could be linked). See clearcote._containerseed.
_seed, _seed_source = container_seed(os.environ.get("CC_FINGERPRINT"), PROFILE_DIR)
if _seed_source == "new":
    print("[clearcote] persona seed: %s (new for this container, kept in %s; set CC_FINGERPRINT "
          "to choose one)" % (_seed, PROFILE_DIR), flush=True)
elif _seed_source == "saved":
    print("[clearcote] persona seed: %s (this container's, from %s)" % (_seed, PROFILE_DIR), flush=True)
opts = {
    "fingerprint": _seed,
    "platform": os.environ.get("CC_PLATFORM", "linux"),
}
# Env -> fingerprint option. The option names are the SDK's own launch() kwargs (see
# _fingerprint.FINGERPRINT_KEYS), so anything documented for the SDK is reachable from a container
# without a code change here. The maps are split by TYPE because fingerprint_args does not treat
# every option as a string: numbers end up in switch values that the engine parses with
# StringToInt/StringToDouble, and the booleans below are tested with `is`/truthiness, so a raw
# environment string would either be silently wrong or silently inert.
_ENV_TO_OPT = {
    "CC_BRAND": "brand", "CC_BRAND_VERSION": "brand_version",
    "CC_PLATFORM_VERSION": "platform_version",
    "CC_ACCEPT_LANGUAGE": "accept_language", "CC_TIMEZONE": "timezone",
    "CC_GPU_VENDOR": "gpu_vendor", "CC_GPU_RENDERER": "gpu_renderer",
    "CC_TLS_PROFILE": "tls_profile",
    "CC_LOCATION": "location",        # "<lat>,<lon>" — keep it in the CC_TIMEZONE's country
    "CC_WEBRTC_IP": "webrtc_ip",      # srflx candidate the engine fabricates (set it to the egress IP)
    "CC_WEBRTC_MDNS": "webrtc_mdns",  # only "off" does anything: concealment on is Chrome's default
    # A real captured fingerprint (tools/fingerprint-collect) — mount the .json and point at it, or
    # pass the JSON itself. Its fields beat the seed-derived persona; absent fields fall back to it.
    "CC_FINGERPRINT_PROFILE": "fingerprint_profile",
}
_ENV_TO_INT = {
    "CC_HARDWARE_CONCURRENCY": "hardware_concurrency",
    "CC_DEVICE_MEMORY": "device_memory",          # navigator.deviceMemory, GB (4/8/16/32)
    "CC_COLOR_DEPTH": "color_depth",
    # 0 is a REAL value here (a mouse-only desktop), not "unset" — which is why these are coerced
    # and emitted on value rather than on truthiness.
    "CC_MAX_TOUCH_POINTS": "max_touch_points",
    "CC_STORAGE_QUOTA": "storage_quota",          # navigator.storage.estimate().quota, MEGABYTES
}
_ENV_TO_FLOAT = {"CC_DEVICE_PIXEL_RATIO": "device_pixel_ratio"}
_ENV_TO_BOOL = {
    "CC_LIGHT_STEALTH": "light_stealth",
    "CC_DISABLE_GPU_FINGERPRINT": "disable_gpu_fingerprint",
    # The three below are FALSE-TRIGGERED: fingerprint_args tests them with `is False`, because
    # "on" is both the Chromium default and real Chrome's behaviour, so only turning them OFF emits
    # a switch. They must therefore arrive as real booleans — the string "false" is truthy, so a raw
    # passthrough would look configured and do nothing at all.
    "CC_FINGERPRINT_NOISE": "fingerprint_noise",
    "CC_CANVAS_NOISE": "canvas_noise",
    "CC_GPU_STRING_SPOOF": "gpu_string_spoof",
}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _as_bool(raw):
    text = str(raw).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(raw)


# A typo in one CC_* var must never stop the container coming up: complain loudly on stdout and
# leave that single option at its default, rather than taking the whole server down with it.
for _map, _cast, _what in ((_ENV_TO_OPT, str, "string"),
                           (_ENV_TO_INT, int, "whole number"),
                           (_ENV_TO_FLOAT, float, "number"),
                           (_ENV_TO_BOOL, _as_bool, "boolean (1/0/true/false/yes/no)")):
    for env_key, opt_key in _map.items():
        raw = os.environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            opts[opt_key] = _cast(raw)   # False and 0 are values, so this assigns unconditionally
        except (TypeError, ValueError):
            print("[clearcote] WARNING: %s=%r is not a %s; ignoring it." % (env_key, raw, _what),
                  flush=True)

# ---------------------------------------------------------------------------------------------
# Screen coherence. Three things claim a display and they used to disagree: Xvfb is created at a
# FIXED CC_SCREEN, while --fingerprint=<seed> makes the ENGINE derive screen AND avail from the seed
# (measured: one seed gives 3840x2160 / avail 3840x2120, another 1536x864 / 1536x824). So a page
# read a 4K screen off a 1920x1080 display, and the window fitted neither.
#
# Two ways to agree, and the seed's own display is the default:
#
# * PERSONA DISPLAY (default when CC_SCREEN is unset and a seed persona is served): the engine's
#   validator (--clearcote-validate-persona, the same call fingerprint-audit dumps with) prints the
#   screen, work area and DPR the seed derives, Xvfb is created at exactly that size, and the engine
#   serves its own numbers -- no --fingerprint-screen-* pin at all. So a seed is the same machine
#   in this container as in a native launch: measured before this (fingerprint-audit, 2026-10-21),
#   the fixed 1920x1080 pin made 13 of 30 Linux seeds and 27 of 30 Windows seeds a different
#   machine than natively (4K seeds clamped to 1920x1080; avail 1040 against the engine's 1032),
#   and every container the same screen size. Costs one validator run (~1 s) before Xvfb starts.
# * PINNED (CC_SCREEN set, or a persona the validator cannot describe: light_stealth, an imported
#   profile, an android claim, a window/screen switch in CC_EXTRA_ARGS, an engine without the
#   validator): CC_SCREEN sizes Xvfb and is pinned onto the persona through --fingerprint-screen-*
#   / --fingerprint-avail-*, which patch 140 checks BEFORE the persona and returns early on -- so
#   the override wins and the claimed screen is the real one. The same switches are honoured
#   without --fingerprint too, so this still holds under CC_LIGHT_STEALTH.
# CC_PIN_SCREEN=0 opts out of both and hands the screen back to the seed on the default display,
# restoring the old disagreement.
# ---------------------------------------------------------------------------------------------
def _persona_display(seed):
    """(screen_w, screen_h, avail_w, avail_h, dpr) the engine derives for `seed` under the persona
    switches of this launch, read from the engine's own validator, or None when the engine cannot say
    (no validator in this build, a bad seed, a timeout). The validator prints one REPRO line per seed
    and exits before any window opens; a lone seed is its COUNT mode (summary only), so a companion
    seed rides along and its line is dropped."""
    try:
        seed_i = int(str(seed).strip())
    except ValueError:
        return None
    if _persona_mod is not None and _persona_mod.engine_wants_persona_token(exe):
        return _persona_token_display()  # r51+: the validator left the engine; the token says what it serves
    companion = seed_i + 1 if seed_i < 2 ** 63 - 1 else seed_i - 1
    udd = tempfile.mkdtemp(prefix="cc-persona-")
    out = os.path.join(udd, "report.txt")
    persona_switches = [a for a in fingerprint_args(dict(opts))
                        if str(a).startswith(("--fingerprint", "--canvas-bridge-"))]
    cmd = [exe, "--clearcote-validate-persona=%d,%d" % (seed_i, companion),
           "--clearcote-validate-persona-out=" + out, "--user-data-dir=" + udd, "--no-first-run",
           "--headless=new", "--no-sandbox"] + persona_switches
    venv = dict(os.environ)
    venv.pop("CLEARCOTE_LICENSE_KEY", None)  # the engine only ever sees the run token
    if _lease and _lease.token:
        venv["CLEARCOTE_RUN_TOKEN"] = _lease.token
    try:
        subprocess.run(cmd, env=venv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        text = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
    except (OSError, subprocess.SubprocessError):
        text = ""
    finally:
        subprocess.run(["rm", "-rf", udd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for line in text.splitlines():
        if not line.startswith("REPRO "):
            continue
        kv = dict(tok.split("=", 1) for tok in line[6:].split() if "=" in tok)
        if kv.get("seed") != str(seed_i):
            continue
        try:
            sw, sh = (int(x) for x in kv["screen"].split("x"))
            aw, ah = (int(x) for x in kv["avail"].split("x"))
            dpr = float(kv.get("dpr", "1") or 1)
            if sw > 0 and sh > 0 and 0 < aw <= sw and 0 < ah <= sh and dpr > 0:
                return sw, sh, aw, ah, dpr
        except (KeyError, ValueError):
            return None
    return None


def _persona_token_display():
    """r51+: the screen, work area and DPR the engine will serve, read from the persona token the service signs for
    this launch's switches (the same token the launch below reuses from the SDK's cache). None when there is no
    lease, when the service cannot be reached, or when the token does not carry a display."""
    if not (_lease and _lease.token):
        return None
    try:
        _args, penv = _persona_mod.apply(exe, fingerprint_args(dict(opts)), {}, _lease)
        token = penv.get(_persona_mod.TOKEN_ENV)
        if not token:
            import gzip
            token = gzip.decompress(base64.b64decode(penv[_persona_mod.TOKEN_GZ_ENV])).decode("ascii")
        body = token.split(".")[0]
        body += "=" * (-len(body) % 4)
        p = json.loads(base64.urlsafe_b64decode(body.encode()).decode("utf-8"))["p"]["persona"]
        sw, sh = int(p["screen_width"]), int(p["screen_height"])
        aw, ah = int(p["avail_screen_width"]), int(p["avail_screen_height"])
        dpr = float(p.get("device_pixel_ratio") or 1)
        if sw > 0 and sh > 0 and 0 < aw <= sw and 0 < ah <= sh and dpr > 0:
            return sw, sh, aw, ah, dpr
    except Exception as exc:  # noqa: BLE001 -- the launch itself reports a persona-service failure
        print("[clearcote] persona display: %s: %s" % (type(exc).__name__, exc), flush=True)
    return None


def _parse_screen(value):
    """CC_SCREEN ("<w>x<h>[x<depth>]") -> (width, height, depth). Never raises: an unusable value
    falls back to the documented default instead of stopping the container from starting."""
    parts = str(value).strip().lower().split("x")
    try:
        width, height = int(parts[0]), int(parts[1])
        depth = int(parts[2]) if len(parts) > 2 else 24
        if width > 0 and height > 0 and depth > 0:
            return width, height, depth
    except (IndexError, ValueError):
        pass
    print("[clearcote] WARNING: CC_SCREEN=%r is not <w>x<h>[x<depth>]; using 1920x1080x24."
          % value, flush=True)
    return 1920, 1080, 24


_cc_screen = os.environ.get("CC_SCREEN")
screen_w, screen_h, screen_depth = _parse_screen(_cc_screen or "1920x1080x24")
persona_display = None  # set below when the seed's own display is used
extra = os.environ.get("CC_EXTRA_ARGS", "").split()

# --disable-runtime-suppression (engine r32+; the SDKs' stock_runtime option, which they pass in CC_EXTRA_ARGS)
# gives automation clients Chromium's own DevTools Runtime behaviour back. As in a launch on a host, it stays only
# on an engine that has it: an older engine would ignore it without a word.
_STOCK_RUNTIME = "--disable-runtime-suppression"
_stock_runtime = _STOCK_RUNTIME in extra
if _stock_runtime:
    try:
        from clearcote._launchopts import engine_supports_switch as _has_switch
    except ImportError:  # an image built with an SDK older than this entrypoint: the same probe
        from proxy_relay import engine_supports_switch as _has_switch
    if not _has_switch(exe, _STOCK_RUNTIME[2:]):
        extra = [a for a in extra if a != _STOCK_RUNTIME]
        _stock_runtime = False
        print("[clearcote] WARNING: stock_runtime (--disable-runtime-suppression) needs an engine from r32 on; "
              "this engine does not support it, so chrome starts without it.", flush=True)

# --enable-runtime-bindings (engine r54+; the SDKs' runtime_bindings option): only the Runtime.addBinding part of
# the above, so expose_function()/expose_binding() survive navigation. The same gate; redundant next to it.
_RUNTIME_BINDINGS = "--enable-runtime-bindings"
if _RUNTIME_BINDINGS in extra:
    try:
        from clearcote._launchopts import engine_supports_switch as _has_switch
    except ImportError:
        from proxy_relay import engine_supports_switch as _has_switch
    if _stock_runtime or not _has_switch(exe, _RUNTIME_BINDINGS[2:]):
        extra = [a for a in extra if a != _RUNTIME_BINDINGS]
        if not _stock_runtime:
            print("[clearcote] WARNING: runtime_bindings (--enable-runtime-bindings) needs an engine from r54 on; "
                  "this engine does not support it, so chrome starts without it.", flush=True)

# Same discipline as the SDK's _geometry.caller_sized_the_window: ANY window/screen switch the
# caller wrote themselves means hands off the entire block. Half-honouring it -- their window inside
# our claimed screen, or our window inside their claimed screen -- rebuilds the very contradiction
# this is here to remove. Spelled out locally rather than imported so that an older SDK baked into
# the image cannot turn a missing helper into a container that will not boot.
_CALLER_GEOMETRY_FLAGS = ("--window-size", "--window-position", "--start-maximized")
_CALLER_GEOMETRY_PREFIXES = ("--fingerprint-screen-", "--fingerprint-avail-")


def _caller_owns_geometry(argv):
    return any(str(a).split("=", 1)[0] in _CALLER_GEOMETRY_FLAGS
               or str(a).startswith(_CALLER_GEOMETRY_PREFIXES) for a in argv)


# The strip a real desktop reserves for its taskbar/panel, per persona OS. It is only ever taken off
# the BOTTOM, because the engine reports availLeft/availTop as 0 whenever a persona is active
# (patch 140) and exposes no switch to move them -- so a reduction a real OS puts at the TOP would
# claim a strip the coordinates say is not there.
#   windows  48px taskbar, the Windows 11 bar (the audit corpus: 48 on 57 % of Windows machines, 40
#            on 25 %, and 40 is exactly the Windows 10 bar). 40px when the persona claims Windows
#            10 (CC_PLATFORM_VERSION "10.x"): a 40px strip under a Windows 11 platformVersion was a
#            readable contradiction (27 of 30 Docker seeds, fingerprint-audit 2026-10-21). The
#            SDK's _LIGHT_STEALTH_PROFILES rows keep their own inset.
#   macos    25px menu bar. Imperfect, knowingly: a real Mac reports availTop=25 and this says 0, so
#            the strip reads as bottom-docked (a Dock, not the menu bar). Still the better of the
#            two answers available -- no Mac hides the menu bar, so avail == screen would be the
#            stranger claim -- and the residual is an engine limit, not a choice made here.
#   linux    none. A Linux panel sits on top (GNOME) about as often as at the bottom (KDE/XFCE), so
#            with availTop pinned to 0 either guess is a coin flip; avail == screen is *true* of the
#            container's own bare X display, is internally consistent, and is what 78 of 432 real
#            desktop captures in the audit corpus report anyway. This is the default persona's path.
_AVAIL_INSET = {"windows": 48, "macos": 25, "linux": 0}


def _avail_inset(platform, platform_version):
    if platform == "windows" and str(platform_version or "").strip().startswith("10."):
        return 40
    return _AVAIL_INSET.get(platform, 0)



avail_w, avail_h = screen_w, screen_h
pin_screen = os.environ.get("CC_PIN_SCREEN", "").strip().lower() not in _FALSE
if not pin_screen:
    print("[clearcote] screen: NOT pinned (CC_PIN_SCREEN=0) -- the seed's screen and the real "
          "%dx%d display will disagree." % (screen_w, screen_h), flush=True)
elif opts.get("platform") == "android":
    # A phone persona's screen is phone-shaped and fingerprint_args sizes a phone window to match;
    # pinning a desktop display over that would contradict the mobile UA it has just claimed.
    pin_screen = False
    print("[clearcote] screen: not pinned (an android persona keeps its own mobile display)",
          flush=True)
elif _caller_owns_geometry(extra):
    pin_screen = False
    print("[clearcote] screen: not pinned (CC_EXTRA_ARGS sets window/screen geometry itself)",
          flush=True)
elif (not _cc_screen and not opts.get("light_stealth") and not opts.get("fingerprint_profile")
      and (persona_display := _persona_display(_seed))):
    # The seed's own display: Xvfb takes the persona's screen (in device pixels: the engine reports
    # CSS pixels and the persona's DPR), the work area is the engine's, and nothing is pinned.
    _sw, _sh, _aw, _ah, _dpr = persona_display
    screen_w, screen_h = int(round(_sw * _dpr)), int(round(_sh * _dpr))
    avail_w, avail_h = _aw, _ah
    print("[clearcote] screen: the persona's own, %dx%d (avail %dx%d, DPR %g) -- the virtual display "
          "is created at that size and the engine serves it unpinned" % (_sw, _sh, _aw, _ah, _dpr),
          flush=True)
else:
    if not _cc_screen and not opts.get("light_stealth") and not opts.get("fingerprint_profile"):
        print("[clearcote] WARNING: the engine's validator could not describe seed %s; pinning the "
              "default 1920x1080 display onto the persona instead (set CC_SCREEN to choose one)."
              % _seed, flush=True)
    avail_h = screen_h - _avail_inset(opts.get("platform"), opts.get("platform_version"))
    opts["screen_width"], opts["screen_height"] = screen_w, screen_h
    opts["avail_width"], opts["avail_height"] = avail_w, avail_h
    # colour depth is deliberately NOT pinned from CC_SCREEN's third field: Chrome reports 24 on a
    # 24- AND a 32-bit X visual, so forwarding the Xvfb number would turn a routine "x32" into a
    # screen.colorDepth no desktop Chrome emits. Set CC_COLOR_DEPTH to override it explicitly.
    print("[clearcote] screen: %dx%d claimed, avail %dx%d" % (screen_w, screen_h, avail_w, avail_h),
          flush=True)
xvfb_screen = "%dx%dx%d" % (screen_w, screen_h, screen_depth)

# Widevine CDM -- seeded for every desktop persona (windows, linux, macos). A build branded Google
# Chrome that carries no CDM reads as a Chromium build to any EME probe (audit: "a build branded
# Google Chrome carries Google's Widevine CDM"); it used to be seeded for Windows claims only, and
# fingerprint-audit (2026-10-21) found the Linux persona answering requestMediaKeySystemAccess with
# NotSupportedError on 30 of 30 seeds while the same engine launched natively, and genuine Linux
# Chrome, report widevine. An android claim keeps the old default (the .so here is the desktop CDM).
# Force either way with CC_WIDEVINE=1 / CC_WIDEVINE=0.
#
# The CDM fetched is host-shaped (libwidevinecdm.so here), not persona-shaped -- correct, since a
# Linux binary can only load a .so. Cached in the engine volume rather than $HOME so it is fetched
# once, not per container. Best-effort: DRM must never stop the server coming up.
_wv = os.environ.get("CC_WIDEVINE")
_wv_on = (_wv not in ("0", "false", "no")) if _wv else (opts.get("platform") in ("windows", "linux", "macos"))
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
# Web Bluetooth's default follows the BUILD platform, so a Linux container serving a desktop
# persona reports navigator.usb/serial/hid but NOT navigator.bluetooth -- a combination no real
# desktop Chrome produces. web_bluetooth_args() enables it for a desktop CLAIM and disables it
# for a linux claim, where genuine Chrome has none either; the host is never consulted. The SDK's
# launch() adds this already; this entrypoint builds its own argv, so it has to ask for it too.
port = os.environ.get("CC_PORT", "9222")               # externally exposed port
internal = os.environ.get("CC_INTERNAL_PORT", "9223")  # chrome's loopback DevTools port

# Fill the claimed work area with the REAL window. There is no window manager in the container, so
# nothing maximizes Chrome for us and it would otherwise open at its default size inside the screen
# just claimed -- inner << outer << avail, the same gap the SDK closes over CDP with
# fit_window_to_persona (which needs a Playwright page this entrypoint does not have). Sizing the
# window to exactly avail instead makes the engine report outer == avail and screenX/screenY == 0
# (patch 140 clamps outer to avail and derives the origin from the leftover room): a maximized
# window, settled at launch rather than corrected after the first navigation.
#
# --window-position is not itself page-observable -- the engine synthesizes screenX/Y -- it only
# keeps the real surface wholly on the virtual display. Skipped entirely when fingerprint_args has
# already sized the window (light_stealth's own fit), so the SDK's choice is never fought.
#
# Engine r39+ opens a persona's window itself (components/ungoogled/persona_window.h): inside the
# work area pinned above, at a size, position and state derived from the seed -- maximized for most
# seeds, a restored window for the rest -- so every container no longer shows the same maximized
# 1920x1040 window. Then this entrypoint passes no window switch at all (a --window-size would also
# force every popup to that size). Older engines, light_stealth (no persona) and an explicit
# CC_EXTRA_ARGS window keep the old behaviour.
def _engine_places_persona_window(argv):
    try:
        from clearcote._geometry import engine_places_persona_window
    except ImportError:  # an image built with an SDK older than this entrypoint
        return False
    return engine_places_persona_window(exe, argv)


window_args = []
if pin_screen and not _engine_places_persona_window(list(args) + list(extra)):
    window_args.append("--window-position=0,0")
    if not any(str(a).startswith("--window-size=") for a in args):
        window_args.append("--window-size=%d,%d" % (avail_w, avail_h))
    print("[clearcote] window: %dx%d @0,0" % (avail_w, avail_h), flush=True)
elif pin_screen:
    print("[clearcote] window: the persona's own, opened by the engine inside the claimed work area",
          flush=True)

# Proxy (CC_PROXY="[scheme://][user:pass@]host:port"). launch() hands a proxy to its automation driver; this
# entrypoint exec's chrome itself, so it has to become switches. A proxy that needs a password is logged in
# to the way launch() does it on a host (proxy_relay.plan): by the engine itself when it has the switch
# (--proxy-auth for http/https, --socks5-credentials for SOCKS5 -- the licensed builds), otherwise through a
# relay on this container's loopback that adds the login on the way out, standing in for the driver that
# answers the 407 challenge on a host. Before, the password of an http(s) proxy was dropped with a warning:
# the browser was challenged, nothing answered, and every request failed.
#
# This is the one place that DOES refuse to start, and deliberately: continuing without the proxy
# would send the very traffic the operator wanted proxied straight out of the container's own IP.
# Only a container that sets CC_PROXY can reach it, so no configuration that boots today can break.
proxy_args = []
_proxy = os.environ.get("CC_PROXY", "").strip()
_proxy_server = _proxy_auth = _proxy_relay = None
if _proxy:
    try:
        import proxy_relay
        from clearcote._launchopts import quic_args, webrtc_default_deny_args

        _scheme, _host, _pport, _user, _pw = proxy_relay.split_proxy(_proxy)
        _proxy_server = proxy_relay.server_url(_scheme, _host, _pport)  # never the credentials
        proxy_args, _proxy_auth, _proxy_relay = proxy_relay.plan(_proxy, exe)
        # Behind a proxy real Chrome cannot use QUIC (an HTTP/SOCKS proxy carries only TCP), and
        # WebRTC's non-proxied UDP would egress around it -- the same two defaults launch() applies.
        # Scoped to the proxied path so an unproxied container's behaviour is untouched.
        proxy_args += quic_args({"server": _proxy_server}) + webrtc_default_deny_args(proxy_args + extra)
        print("[clearcote] proxy: %s" % _proxy_server, flush=True)  # server only -- never the creds
        if _proxy_auth:
            print("[clearcote] proxy login: %s" % ("by the engine" if _proxy_auth == "engine" else
                                                   "through a relay on this container's loopback"), flush=True)
    except Exception as exc:  # noqa: BLE001 -- never fall back to an unproxied browser silently
        print("[clearcote] ERROR: CC_PROXY could not be applied (%s: %s). Refusing to start rather "
              "than send traffic direct from the container's own IP."
              % (type(exc).__name__, exc), flush=True)
        raise SystemExit(1)

# publish the loopback-only DevTools endpoint: 0.0.0.0:$port -> 127.0.0.1:$internal
subprocess.Popen(
    ["socat", f"TCP-LISTEN:{port},fork,reuseaddr,bind=0.0.0.0", f"TCP:127.0.0.1:{internal}"]
)

# Display mode: default is HEADFUL on a virtual X display (Xvfb) — a real headed browser avoids
# the headless-mode tells some detectors probe. Set CC_HEADLESS=1 to force pure-headless (no Xvfb).
# The container has no GPU; which software backend renders WebGL depends on the persona (below).
headless = os.environ.get("CC_HEADLESS", "").strip().lower() in ("1", "true", "yes")
mode_args = []
if headless:
    mode_args = ["--headless=new"]
    print("[clearcote] display: pure headless (CC_HEADLESS set)", flush=True)
else:
    display = os.environ.get("DISPLAY") or ":99"
    if not os.environ.get("DISPLAY"):  # start our own Xvfb only if the host didn't provide a display
        # xvfb_screen is the SAME CC_SCREEN the persona's claimed screen was pinned to above; that
        # shared parse is the whole point, so the display and the claim cannot drift apart again.
        # A container keeps /tmp across `docker restart` (and every --restart policy), so the previous
        # run's Xvfb lock and socket are still there: the new Xvfb exits "Server is already active for
        # display", Chrome then exits "Missing X server", and the container dies. Nothing can still own
        # them -- this process tree has only just started -- so remove them first.
        num = display.lstrip(":").split(".")[0]
        for stale in ("/tmp/.X%s-lock" % num, "/tmp/.X11-unix/X%s" % num):
            try:
                os.remove(stale)
            except FileNotFoundError:
                pass
        subprocess.Popen(["Xvfb", display, "-screen", "0", xvfb_screen, "-nolisten", "tcp", "-ac"])
        sock = "/tmp/.X11-unix/X" + display.lstrip(":").split(".")[0]
        for _ in range(100):  # wait up to ~10s for the virtual display to come up
            if os.path.exists(sock):
                break
            time.sleep(0.1)
    os.environ["DISPLAY"] = display  # inherited by chrome via `env` below
    print(f"[clearcote] display: headful on Xvfb {display}", flush=True)

# The container has no GPU, so WebGL renders in software -- through the backend whose limits match the
# GPU the persona NAMES (measured 2026-10-06 against real captures):
# * a Linux persona names "Mesa Intel ... OpenGL 4.6". SwiftShader gives it 8192 textures, 4096 vertex
#   uniform vectors and SwiftShader shader text, which no Mesa machine reports; Mesa's own software GL
#   (llvmpipe, shipped in this image) gives 16384 / 1024 / GLSL like real Mesa machines. Headful (the
#   default) it runs over the Xvfb display; under CC_HEADLESS there is no display, so it runs over EGL
#   (libegl1 in this image): the same limits, with the shader text of an OpenGL ES context.
# * a Windows persona names Direct3D11. SwiftShader gives the Direct3D11-like 4096 vertex uniform vectors
#   (Mesa's GL clamps them to 1024) and, with the HLSL dialect below, matching shader translations.
# --enable-unsafe-swiftshader stays on: if Mesa cannot start over the display, WebGL falls back rather than
# vanishing. Over EGL there is no such fallback, hence the check that Mesa's EGL is installed.
# CC_EXTRA_ARGS comes last on the line, so a --use-angle= there still wins.
if persona_platform(opts) == "linux" and not headless:
    gpu_args = ["--use-gl=angle", "--use-angle=gl", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
elif persona_platform(opts) == "linux" and mesa_egl_available():
    gpu_args = ["--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist", "--enable-unsafe-swiftshader"]
else:
    gpu_args = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
# WebGPU on the GPU-less host. Real Windows and macOS Chrome on the persona's GPU always has a WebGPU
# adapter; this container had none (requestAdapter() null on 30 of 30 Windows seeds, fingerprint-audit
# 2026-10-21). From engine r47 (pro-r47-04) a software adapter wears the persona -- identity, feature
# list, limits, not a fallback -- but only when the launcher asks for one with the switches below.
# On an OLDER engine those switches expose a raw SwiftShader adapter (architecture "swiftshader",
# isFallbackAdapter true) under the persona's renderer string, the louder tell, so they are gated:
# the engine path names its revision (auto-downloaded engines live in ".../pro-<build>-r<N>/"), and
# from r48 the binary also carries the --fingerprint-software-webgpu switch (its "off" value keeps the
# software adapter real). A linux claim keeps the Mesa backend's own answer, like real Linux Chrome.
if persona_platform(opts) in ("windows", "macos"):
    _rev = _engine_revision(exe)
    if (_rev is not None and _rev >= 47) or _engine_has_switch("fingerprint-software-webgpu"):
        gpu_args += ["--use-webgpu-adapter=swiftshader", "--enable-unsafe-webgpu"]
        print("[clearcote] WebGPU: software adapter wearing the persona (engine r%s)"
              % (_rev if _rev is not None else "48+"), flush=True)
    else:
        print("[clearcote] WebGPU: none (a %s claim gets a persona WebGPU adapter from engine r47 on)"
              % persona_platform(opts), flush=True)
_BACKENDS = {"--use-angle=gl": "Mesa GL (llvmpipe)", "--use-angle=gl-egl": "Mesa GL over EGL (llvmpipe)"}
print("[clearcote] WebGL backend: %s" % next((v for k, v in _BACKENDS.items() if k in gpu_args), "SwiftShader"),
      flush=True)

# Chrome's sandbox: on when this container lets Chrome create the user, PID and network namespaces it needs
# (sandbox_check.probe makes the same calls first). Docker's default seccomp profile refuses them, and Chrome
# would then abort, so a container started without the profile at /etc/clearcote/seccomp.json gets
# --no-sandbox and one log line saying why and how to turn the sandbox on: whatever booted before still boots.
try:
    import sandbox_check

    sandbox, _sandbox_line = sandbox_check.decide(args + extra, os.geteuid())
except ImportError:  # a custom image that copied serve.py without its probe
    sandbox, _sandbox_line = False, "[clearcote] sandbox: OFF, Chrome runs with --no-sandbox: sandbox_check.py is missing"
print(_sandbox_line, flush=True)

base_args = (["--no-sandbox"] if not sandbox and "--no-sandbox" not in args + extra else []) + [
    "--disable-dev-shm-usage",
] + gpu_args + [
    f"--remote-debugging-port={internal}", "--remote-allow-origins=*",
    "--user-data-dir=%s" % PROFILE_DIR,
] + mode_args + window_args + args + web_bluetooth_args(persona_platform(opts)) + proxy_args + extra
# --no-sandbox is on Chromium's "unsupported command-line flag" list, and the warning bar it raises sits
# on the first tab: 56px off that tab's innerHeight, a frame (outer - inner) no real Chrome has. With the
# sandbox on there is no such flag and no bar. Without it, the SDK's serve() keeps the bar off the same way:
# --disable-infobars headless, --test-type headful.
base_args += serve_infobar_args(headless, base_args)
# Chromium keeps only the LAST --enable-features / --disable-features on the line rather than
# concatenating them, so the layers here would silently clobber each other: web_bluetooth_args
# enables one, CC_WEBRTC_MDNS=off disables another, and CC_EXTRA_ARGS may carry the caller's own.
# Collapse each into a single flag, exactly as the SDK's _prepare does before launching.
cmd = [exe] + merge_feature_flags(base_args)

env = dict(os.environ)
try:  # point FONTCONFIG_FILE at the bundled Windows-font clones (+ CLEARCOTE_*FONT_DIRS)
    font_env = linux_font_env(exe, args=cmd)  # --disable-genuine-font-faces in CC_EXTRA_ARGS keeps the rules
except TypeError:  # an image built with an SDK older than this entrypoint
    font_env = linux_font_env(exe)
env.update(font_env)

# Shader dialect -- enabled ONLY for a Windows persona on this Linux host, the same condition as the
# Widevine seeding above and for the same reason: it is where the absence is a measured
# contradiction. The persona advertises a Direct3D renderer, but ANGLE's Vulkan backend answers
# getTranslatedShaderSource() with a SPIR-V dump, so a page reads the renderer string and the
# dialect beside it and sees two different graphics backends (audit: "ANGLE's translated shader is
# written in the dialect the renderer string implies").
#
# A Linux persona is NOT flagged for this, so the default container behaviour is unchanged. Force
# either way with CC_SHADER_DIALECT=hlsl / =0.
#
# Rendering is unaffected either way -- only the debug-extension query changes. Engines older than
# 151 r15 ignore the variable and keep reporting their real dialect.
_sd = os.environ.get("CC_SHADER_DIALECT")
if _sd:
    if _sd not in ("0", "false", "no"):
        env["CLEARCOTE_SHADER_DIALECT"] = _sd
        # Forcing it on a persona that does NOT claim Direct3D makes things worse, not better: the
        # renderer would name OpenGL while the dialect says HLSL. Honoured (a custom CC_GPU_RENDERER
        # may legitimately name D3D) but never silently.
        if opts.get("platform") != "windows":
            print("[clearcote] WARNING: CC_SHADER_DIALECT=%s with a %s persona. HLSL is only "
                  "coherent next to a Direct3D renderer string; on a persona that names OpenGL "
                  "this creates the contradiction it is meant to remove."
                  % (_sd, opts.get("platform")), flush=True)
elif opts.get("platform") == "windows":
    env["CLEARCOTE_SHADER_DIALECT"] = "hlsl"
if env.get("CLEARCOTE_SHADER_DIALECT"):
    print("[clearcote] shader dialect: %s" % env["CLEARCOTE_SHADER_DIALECT"], flush=True)

# The lease itself was checked out right after the engine was resolved (the persona display asks the
# engine's validator for the seed's screen before Xvfb starts, and a PRO validator needs the token too);
# here it only reaches chrome's environment.
if _lease and _lease.token:
    env["CLEARCOTE_RUN_TOKEN"] = _lease.token
    if _token_file:
        env["CLEARCOTE_RUN_TOKEN_FILE"] = _token_file

# fingerprint_profile may be the capture JSON inline (tens of KB), which would bury every other log
# line in the container -- summarise it instead of printing it. "socat", not "proxy", since CC_PROXY
# now makes that word mean the upstream network proxy.
_shown = dict(opts)
if _shown.get("fingerprint_profile"):
    _shown["fingerprint_profile"] = "<%d bytes>" % len(str(_shown["fingerprint_profile"]))
print(f"[clearcote] CDP endpoint on 0.0.0.0:{port} (socat -> chrome 127.0.0.1:{internal}) | persona={_shown}", flush=True)

try:
    _idle_limit = int(os.environ.get("CC_IDLE_EXIT_SECONDS", "0") or 0)
except ValueError:
    print("[clearcote] WARNING: CC_IDLE_EXIT_SECONDS=%r is not a whole number; ignoring it."
          % os.environ.get("CC_IDLE_EXIT_SECONDS"), flush=True)
    _idle_limit = 0

# Engine patch 1021 ("env mode"), the same last step a launch on a host takes (clearcote._personaenv): on an
# engine that implements --persona-from-env, the persona switches -- seed, overrides, proxy credentials --
# leave chrome's command line and travel in CLEARCOTE_PERSONA_ARGS. A container's processes show up in the
# Linux host's process table, where any user can read a command line. CLEARCOTE_PERSONA_ENV=0 (what the SDKs
# pass for persona_env=False) keeps the command line, as does an older engine or an image built with an SDK
# that predates the module.
try:
    from clearcote import _personaenv
except ImportError:
    _personaenv = None
_persona_env = False
if _persona_mod is not None and _lease and _lease.token:
    # r51+: the signed persona token in CLEARCOTE_PERSONA_TOKEN (a captured profile leaves the command line for
    # its hash). Nothing changes on an engine before r51. A refusal or an unreachable service stops the container
    # with the reason: the engine cannot start without a persona.
    _argv, env = _persona_mod.apply(exe, cmd[1:], env, _lease)
    cmd = [exe] + _argv
    if _persona_mod.TOKEN_ENV in env or _persona_mod.TOKEN_GZ_ENV in env:
        print("[clearcote] persona: signed by the persona service (r51+)", flush=True)
if _personaenv is not None:
    _argv, _env = _personaenv.apply(exe, cmd[1:], env)
    _persona_env = _env is not env  # a new environment only when the switches moved
    cmd, env = [exe] + _argv, _env
    if _persona_env:
        print("[clearcote] persona: in CLEARCOTE_PERSONA_ARGS, off chrome's command line", flush=True)

# SERVE_PROTOCOL: what this entrypoint does with the SDK's settings. The image carries it as the label
# com.clearcotelabs.serve-protocol, so the SDK knows before it starts a container whether the image takes
# CC_SECRETS_FILE and CC_IDLE_EXIT_SECONDS (2), or only plain variables (no label: older images), and whether
# it runs Chrome's sandbox when the container allows it (3: the SDK then passes the seccomp profile; it is not
# given to an older image, whose Chrome runs with --no-sandbox anyway). The serve-state line says what was
# actually applied -- the engine tier that resolved, the proxy, the idle exit, how a proxy's password is
# answered, the sandbox -- so the SDK can refuse a container that did not do what it was asked instead of
# handing back a browser on the free engine, one that goes direct when a proxy was asked for, or one whose
# every request a proxy turns away.
SERVE_PROTOCOL = 3
_applied = {
    "protocol": SERVE_PROTOCOL,
    "engine": "licensed" if "/pro-" in exe.replace(os.sep, "/") else "open",
    "proxy": _proxy_server,
    "proxy_auth": _proxy_auth,  # how a proxy's password is answered: "engine", "relay" (None: no password)
    "idle_exit": _idle_limit if _idle_limit > 0 else 0,
    "secrets_file": bool(_secrets_file),
    "sandbox": sandbox,
    "persona_env": _persona_env,  # 1021: the persona travels in the environment, not on chrome's command line
    "stock_runtime": _stock_runtime,  # r32: --disable-runtime-suppression is on chrome's command line
    "runtime_bindings": _RUNTIME_BINDINGS in extra,  # r54: --enable-runtime-bindings is on chrome's command line
}
print("[clearcote] serve-state %s" % json.dumps(_applied, sort_keys=True), flush=True)

chrome = subprocess.Popen(cmd, env=env)


# CC_IDLE_EXIT_SECONDS: stop once no CDP client has been connected for this long, counted from the moment
# Chrome's DevTools endpoint first answers (idle_exit.py). The SDK's macOS launch() sets it (with --rm), so a
# container whose owner died -- Ctrl-C, a kill, a crash -- stops on its own, gives its licence seat back and
# is removed, instead of running forever. Unset or 0: off.
if _idle_limit > 0:
    import idle_exit

    def _stop_chrome():
        try:
            chrome.terminate()
        except Exception:  # noqa: BLE001 -- already gone
            pass

    threading.Thread(target=idle_exit.watch, daemon=True, kwargs={
        "limit": _idle_limit, "alive": lambda: chrome.poll() is None,
        "ready": lambda: idle_exit.cdp_ready(internal), "clients": lambda: idle_exit.cdp_clients(port),
        "stop": _stop_chrome, "log": lambda m: print(m, flush=True)}).start()
    print("[clearcote] idle exit: after %d s with no CDP client, counted from when CDP answers" % _idle_limit,
          flush=True)


def _forward(signum, _frame):
    # `docker stop` sends SIGTERM to this process (PID 1): pass it on so chrome shuts down cleanly,
    # then fall through to the lease release below once it has exited.
    try:
        chrome.send_signal(signum)
    except Exception:  # noqa: BLE001 -- chrome may already be gone
        pass


for _sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_sig, _forward)

code = chrome.wait()


def _release_lease():
    if _release_token_file is not None:
        try:
            _release_token_file()  # remove the per-launch run-token file
        except Exception:  # noqa: BLE001 -- best-effort; it lives in the container's tmpdir
            pass
    if _lease is None:
        return
    try:
        stop = getattr(_lease, "stop")
        try:
            stop(wait=True)  # a per-browser lease: check the slot in before exiting
        except TypeError:
            stop()  # a machine-shared lease: the SDK's exit hook checks it in
        print("[clearcote] licence lease released", flush=True)
    except Exception as exc:  # noqa: BLE001 -- best-effort; the lease TTL reclaims it
        print("[clearcote] lease release failed (%r); the slot frees on its own shortly." % exc, flush=True)


_release_lease()
sys.exit(code if code >= 0 else 128 - code)
