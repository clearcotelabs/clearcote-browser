"""Fetching personas from the licensed profile service, and caching them locally.

Port of the Node SDK's ``profilesource.ts``.

Selection runs CLIENT-SIDE (see :mod:`_profilelib`) because it needs facts only this machine has:
the real GPU, the real display, the engine's actual Chromium major. The server cannot see any of
those, so it returns candidates for a described host and the SDK picks among them.

The service caps DISTINCT profiles per license per day, and re-fetching one already held is free.
That is why this module caches aggressively on disk: a sticky identity must resolve to the same
profile every launch without spending budget or needing the network.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from ._profilelib import gpu_vendor_class

DEFAULT_API_BASE = "https://www.clearcotelabs.com"

#: In-page probe reading the REAL GPU and display — this launch applies no persona.
PROBE_EXPR = """(() => {
  let renderer = "";
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const dbg = gl && gl.getExtension("WEBGL_debug_renderer_info");
    if (gl && dbg) renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
  } catch (e) {}
  return {
    renderer: renderer,
    screen_width: screen.width,
    screen_height: screen.height,
    device_pixel_ratio: devicePixelRatio,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: navigator.deviceMemory,
  };
})()"""


def _api_base(base: Optional[str] = None) -> str:
    return (
        base
        or os.environ.get("CLEARCOTE_PROFILE_API")
        or os.environ.get("CLEARCOTE_LICENSE_API")
        or DEFAULT_API_BASE
    ).rstrip("/")


def profile_cache_dir() -> str:
    """``<CLEARCOTE_PROFILE_DIR>/library``, created on demand."""
    root = os.environ.get("CLEARCOTE_PROFILE_DIR") or os.path.join(
        os.path.expanduser("~"), ".clearcote", "profiles"
    )
    d = os.path.join(root, "library")
    os.makedirs(d, exist_ok=True)
    return d


def host_os_family() -> str:
    """Map the Python platform onto the ``os_family`` values the corpus stores."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _auth_headers(license_key: Optional[str]) -> dict[str, str]:
    key = license_key or os.environ.get("CLEARCOTE_LICENSE_KEY") or ""
    if not key:
        raise ValueError(
            "the profile library needs a license key — pass license_key or set "
            "CLEARCOTE_LICENSE_KEY (profiles are served to PRO licenses with an active "
            "subscription)"
        )
    return {"Authorization": f"Bearer {key}"}


def _get_json(url: str, headers: dict[str, str], timeout: int = 30) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base
        return json.loads(resp.read().decode("utf-8"))


def fetch_index(
    host: dict[str, Any],
    license_key: Optional[str] = None,
    api_base: Optional[str] = None,
    offline: bool = False,
    os_override: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Candidate index for a described host. Cached so repeated launches do not re-ask."""
    os_family = (os_override or host.get("os_family") or "").lower()
    major = host.get("browser_major")
    cache_path = os.path.join(profile_cache_dir(), f"index-{os_family}-{major}.json")

    if offline:
        if not os.path.isfile(cache_path):
            raise ValueError(f"offline: no cached profile index for {os_family}/{major}")
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    url = f"{_api_base(api_base)}/api/v1/profiles?os={os_family}&major={major}"
    try:
        data = _get_json(url, _auth_headers(license_key))
    except Exception:
        # Serve stale rather than fail the launch — a cached index beats no persona.
        if os.path.isfile(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        raise

    candidates = data.get("candidates") or []
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(candidates, f)
    except OSError:
        pass
    return candidates


def fetch_profile(
    pid: str,
    license_key: Optional[str] = None,
    api_base: Optional[str] = None,
    offline: bool = False,
) -> dict[str, Any]:
    """Fetch one profile by id, preferring the on-disk copy.

    A repeat costs no distinct budget at the service, but not spending a request at all is
    better still.
    """
    cache_path = os.path.join(profile_cache_dir(), f"profile-{pid}.json")
    if os.path.isfile(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass  # refetch

    if offline:
        raise ValueError(f"offline: profile {pid} is not cached")

    data = _get_json(f"{_api_base(api_base)}/api/v1/profiles?id={pid}", _auth_headers(license_key))
    profile = data.get("profile") or {}
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(profile, f)
    except OSError:
        pass
    return profile


# --- host facts -------------------------------------------------------------
# GPU and display can only be read by rendering, so they come from a one-off no-persona launch.
# That is expensive, so the result is cached on disk, keyed by the engine binary — a different
# build may report a different GL string.

_HOST_CACHE_MAX_AGE = 30 * 86400


def _host_cache_path() -> str:
    return os.path.join(profile_cache_dir(), "host.json")


def _exe_key(exe: str) -> str:
    """One cache key per BINARY, not per spelling of its path.

    The caller's literal string used to be the key, so the same file spelled with backslashes
    and with forward slashes (or ``dir/./chrome``, or through a symlink) missed
    the cache and re-measured the host -- which is the one path that launches a second browser.
    """
    try:
        return os.path.normcase(os.path.realpath(exe))
    except OSError:
        return os.path.normcase(exe)


def _cached_at(v: Any) -> Optional[float]:
    """``_at`` as epoch seconds, or None when unreadable.

    host.json is shared by the three SDKs: this one (and .NET) writes epoch seconds, Node writes an
    ISO time. Reading only the number made a Node-written cache raise TypeError and fail
    ``profile="auto"`` outright, instead of being reused or, at worst, re-measured.
    """
    from datetime import datetime, timezone

    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def read_cached_host(exe: str) -> Optional[dict[str, Any]]:
    import time

    try:
        with open(_host_cache_path(), "r", encoding="utf-8") as f:
            c = json.load(f)
        if not isinstance(c, dict) or c.get("_exe") != _exe_key(exe):
            return None  # different binary: re-measure
        at = _cached_at(c.get("_at"))
        if at is None or time.time() - at > _HOST_CACHE_MAX_AGE:
            return None  # unreadable or stale: a miss, never a launch failure
        return c
    except (OSError, ValueError):
        return None


def write_cached_host(exe: str, facts: dict[str, Any]) -> None:
    import time

    try:
        with open(_host_cache_path(), "w", encoding="utf-8") as f:
            json.dump({**facts, "_exe": _exe_key(exe), "_at": time.time()}, f)
    except OSError:
        pass  # a cache write failure must never fail a launch


def _engine_major(browser, fallback: int) -> int:
    """The Chromium major of the binary that is actually running.

    ``browser_major`` is a HARD filter on profile selection, and the value handed in is the SDK's
    PINNED release -- which is whatever build this SDK ships by default, not the engine the caller
    pointed at (``executable_path=``, ``version=``, a PRO revision). Ask the running browser.
    """
    try:
        v = str(getattr(browser, "version", "") or "")
        digits = v.split(".")[0].strip()
        return int(digits) if digits.isdigit() else fallback
    except Exception:  # noqa: BLE001 - never fail a launch over a version string
        return fallback


def measure_host(launch_fn, exe: str, browser_major: int) -> dict[str, Any]:
    """Read the host's real GPU/display by rendering in the engine itself, no persona applied."""
    cached = read_cached_host(exe)
    if cached:
        return cached

    browser = launch_fn(executable_path=exe, headless=True, quiet=True)
    try:
        page = browser.new_context().new_page()
        page.goto("about:blank")  # WebGL and screen need no origin
        facts = page.evaluate(PROBE_EXPR)
        out = {
            "os_family": host_os_family(),
            "browser_major": _engine_major(browser, browser_major),
            "gpu_vendor": gpu_vendor_class(facts.get("renderer")),
            "screen_width": facts.get("screen_width"),
            "screen_height": facts.get("screen_height"),
            "device_pixel_ratio": facts.get("device_pixel_ratio"),
            "hardware_concurrency": facts.get("hardware_concurrency"),
            "device_memory": facts.get("device_memory"),
        }
        write_cached_host(exe, out)
        return out
    finally:
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
