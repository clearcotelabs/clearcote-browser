"""real_computer: show parts of THIS computer instead of the persona's generated values (one identity per real desktop).

Three parts, each read from the operating system (never from a browser probe: a headless probe sees a virtual screen):

- ``screen``: the primary display in CSS px (size, work area, device_pixel_ratio)
- ``touch``: navigator.maxTouchPoints (Windows: GetSystemMetrics(SM_MAXIMUMTOUCHES); macOS: 0, as Chrome reports)
- ``languages``: the system display language, expanded the way Chrome writes its list (en-GB -> en-GB,en-US,en)

Measured 2026-11-21/22 on fingerprint.com behind residential proxies, paired against the default on the same seed and
exit: this computer's touch value lowered the tampering score in 25 of 27 pairs (a value this computer does not have
did not), and the real monitor in 16 of 16. Off by default: every profile on one computer then shares these values,
which links profiles more easily, and a server or container has no real display.

A value the caller set always wins. Languages follow a proxy's exit when one is set with geoip on. In pass-through
(``fingerprint="off"``) the engine already shows this computer, so nothing is added. What cannot be read is left to the
persona, and the launch says so once. Same rules and parsers as the Node and .NET SDKs.
"""

import os
import re
import subprocess
import sys

REAL_COMPUTER_WARN = "real-computer-partial"
_TIMEOUT = 8

_WIN_PS = " ".join([
    "$ErrorActionPreference='Stop';",
    "Add-Type -AssemblyName System.Windows.Forms;",
    "Add-Type -Namespace CcHost -Name U -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware(); [DllImport(\"user32.dll\")] public static extern int GetSystemMetrics(int n);';",
    "[void][CcHost.U]::SetProcessDPIAware();",
    "$s=[System.Windows.Forms.Screen]::PrimaryScreen;",
    "$d=96; try { $v=(Get-ItemProperty 'HKCU:\\Control Panel\\Desktop\\WindowMetrics' -Name AppliedDPI).AppliedDPI; if ($v) { $d=$v } } catch {};",
    "\"$($s.Bounds.Width) $($s.Bounds.Height) $($s.WorkingArea.Width) $($s.WorkingArea.Height) $d $([CcHost.U]::GetSystemMetrics(95)) $((Get-UICulture).Name)\"",
])

_MAC_JXA = (
    "ObjC.import('AppKit'); var s = $.NSScreen.mainScreen; var f = s.frame, v = s.visibleFrame; "
    "var l = $.NSLocale.preferredLanguages.count > 0 ? ObjC.unwrap($.NSLocale.preferredLanguages.objectAtIndex(0)) : ''; "
    "[f.size.width, f.size.height, v.size.width, v.size.height, s.backingScaleFactor, l].join(' ')"
)

_TAG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_NONE = {"display": None, "touch": None, "language": None}


def _run(args):
    try:
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(args, capture_output=True, text=True, timeout=_TIMEOUT, **kw)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return float("nan")


def _finite(*xs):
    return all(x == x and x not in (float("inf"), float("-inf")) for x in xs)


def plausible_display(d):
    """At least 640x480 CSS px, a work area inside it (at least half of it), a ratio from 1 to 4; else None."""
    if not d:
        return None
    v = [d["width"], d["height"], d["avail_width"], d["avail_height"], d["device_pixel_ratio"]]
    ok = (_finite(*v) and all(x > 0 for x in v)
          and 640 <= d["width"] <= 16384 and 480 <= d["height"] <= 16384
          and d["avail_width"] <= d["width"] and d["avail_height"] <= d["height"]
          and d["avail_width"] >= d["width"] / 2 and d["avail_height"] >= d["height"] / 2
          and 1 <= d["device_pixel_ratio"] <= 4)
    return d if ok else None


def _touch(n):
    return int(n) if _finite(n) and n == int(n) and 0 <= n <= 256 else None


def _lang(s):
    t = str(s or "").strip().replace("_", "-").split(".")[0]
    return t if t and _TAG.match(t) and t.lower() != "c" else None


def _ratio(r):
    return round(r * 100) / 100


def parse_windows(out):
    """Windows: "boundsW boundsH workW workH appliedDpi touches uiCulture" (sizes in physical px)."""
    f = (out or "").strip().split()
    n = [_num(x) for x in f[:6]]
    while len(n) < 6:
        n.append(float("nan"))
    r = _ratio(n[4] / 96) if _finite(n[4]) and n[4] > 0 else float("nan")
    display = None
    if _finite(n[0], n[1], n[2], n[3], r):
        display = plausible_display({"width": round(n[0] / r), "height": round(n[1] / r), "avail_width": round(n[2] / r),
                                     "avail_height": round(n[3] / r), "device_pixel_ratio": r})
    return {"display": display, "touch": _touch(n[5]) if len(f) > 5 else None, "language": _lang(f[6] if len(f) > 6 else None)}


def parse_mac(out):
    """macOS: "frameW frameH visibleW visibleH scale language" (points = CSS px). Chrome on macOS reports 0 touch points."""
    f = (out or "").strip().split()
    n = [_num(x) for x in f[:5]]
    display = None
    if len(n) == 5 and _finite(*n):
        display = plausible_display({"width": round(n[0]), "height": round(n[1]), "avail_width": round(n[2]),
                                     "avail_height": round(n[3]), "device_pixel_ratio": _ratio(n[4])})
    return {"display": display, "touch": 0, "language": _lang(f[5] if len(f) > 5 else None)}


def parse_xrandr(out):
    """Linux: the primary output of `xrandr --current`, else the first connected one with a mode; work area = screen."""
    lines = [l for l in (out or "").split("\n") if " connected " in l]
    line = next((l for l in lines if " connected primary " in l), None) or next(
        (l for l in lines if re.search(r"\d+x\d+\+\d+\+\d+", l)), None)
    m = re.search(r"(\d+)x(\d+)\+\d+\+\d+", line or "")
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    return plausible_display({"width": w, "height": h, "avail_width": w, "avail_height": h, "device_pixel_ratio": 1})


_cached = None


def read_host_facts(platform=None, env=None):
    """This computer's display, touch points and display language, read once per process (a part that cannot be read
    is None)."""
    global _cached
    if _cached is not None:
        return _cached
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform.startswith("win"):
        out = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", _WIN_PS])
        facts = parse_windows(out) if out else dict(_NONE)
    elif platform == "darwin":
        out = _run(["osascript", "-l", "JavaScript", "-e", _MAC_JXA])
        facts = parse_mac(out) if out else {"display": None, "touch": 0, "language": None}
    else:
        language = _lang((env.get("LANGUAGE") or "").split(":")[0] or env.get("LC_ALL") or env.get("LC_MESSAGES") or env.get("LANG"))
        if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
            facts = {"display": None, "touch": None, "language": language}
        else:
            out = _run(["xrandr", "--current"])
            facts = {"display": parse_xrandr(out) if out else None, "touch": None, "language": language}
    _cached = facts
    return facts


def reset_host_facts():
    """Tests: forget the cached facts."""
    global _cached
    _cached = None


def real_computer_parts(v):
    """Which parts an option value turns on: True = all; a dict picks (screen / touch / languages)."""
    if v is True:
        return {"screen": True, "touch": True, "languages": True}
    if isinstance(v, dict):
        return {k: v.get(k) is True for k in ("screen", "touch", "languages")}
    return {"screen": False, "touch": False, "languages": False}


def with_real_computer(fp, proxied=False, quiet=False, read=None):
    """Return a copy of the fingerprint options with real_computer applied (the input is not changed). ``proxied``: a
    proxy is set and geoip is on, so the exit decides the languages."""
    parts = real_computer_parts(fp.get("real_computer"))
    if not any(parts.values()):
        return fp
    if str(fp.get("fingerprint") or "").strip().lower() == "off":
        return fp
    want_screen = parts["screen"] and fp.get("screen_width") is None and fp.get("screen_height") is None
    want_touch = parts["touch"] and fp.get("max_touch_points") is None
    want_lang = parts["languages"] and not fp.get("accept_language") and not proxied
    if not (want_screen or want_touch or want_lang):
        return fp
    facts = (read or read_host_facts)()
    out = dict(fp)
    missing = []
    if want_screen:
        d = facts.get("display")
        if d:
            out.update(screen_width=d["width"], screen_height=d["height"], avail_width=d["avail_width"],
                       avail_height=d["avail_height"], device_pixel_ratio=d["device_pixel_ratio"])
        else:
            missing.append("screen")
    if want_touch:
        if facts.get("touch") is not None:
            out["max_touch_points"] = facts["touch"]
        else:
            missing.append("touch")
    if want_lang:
        if facts.get("language"):
            out["accept_language"] = facts["language"]
        else:
            missing.append("languages")
    if missing:
        from ._warnings import warn_once
        warn_once(REAL_COMPUTER_WARN,
                  "real_computer: this computer's %s could not be read (no display server, or it did not answer), so the "
                  "profile keeps its generated value there." % " and ".join(missing), quiet=quiet)
    return out
