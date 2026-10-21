"""Generic profile importer: turn a DIRECTORY of clearcote-profile JSON into a selectable index.

Port of the Node SDK's ``profileimport.ts``.

WE ARE AN IMPORTER, NOT A HOST. Clearcote ships no third-party fingerprints and redistributes
none. A source is just a folder of clearcote-profile JSON on the user's own machine, however it
got there:

* the open chrome-fingerprints dataset, converted locally with
  ``tools/fingerprint-collect/convert_dataset.py`` — fetched by the USER from the original
  repository under its own terms, so nothing is re-published here;
* your own captures from ``tools/fingerprint-collect``;
* the licensed clearcote profile service (see :mod:`_profilesource`);
* any other producer emitting the same schema.

Keeping the FORMAT as the only contract is what makes this generic: no per-vendor adapters, no
vendored data, and no third-party archive decoder baked into three SDKs.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
from typing import Any, Optional

from ._profilelib import gpu_vendor_class


def index_entry_from_profile(pid: str, profile: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse a clearcote-profile blob into an index entry, or None if it is not one."""
    nav = profile.get("navigator") or {}
    screen = profile.get("screen") or {}
    webgl = profile.get("webgl") or {}
    gl1 = webgl.get("webgl1") or {}
    debug = gl1.get("debug") or {}
    meta = profile.get("meta") or {}

    ua = str(nav.get("user_agent") or "")
    platform = str(nav.get("platform") or "")
    # UA-CH. Checked FIRST because it is the only source present on some real inputs: converted
    # chrome-fingerprints records carry ``uadata.platform`` while leaving ``user_agent`` and
    # ``platform`` null (the converter deliberately drops the browser version). Reading only the
    # UA string silently skipped every one of those.
    uadata = nav.get("uadata") if isinstance(nav.get("uadata"), dict) else {}
    high = uadata.get("high_entropy") if isinstance(uadata.get("high_entropy"), dict) else {}
    ch_platform = uadata.get("platform")
    if ch_platform is None:
        ch_platform = high.get("platform")
    ch_platform = str(ch_platform or "")
    ch_mobile = uadata.get("mobile")
    if ch_mobile is None:
        ch_mobile = high.get("mobile")
    ch_mobile = bool(ch_mobile)

    # Same order as Node: UA-CH, then navigator.platform (stable across UA reduction), then the UA.
    if ch_mobile or re.search(r"android", ch_platform, re.I) or re.search(r"android", ua, re.I):
        os_family = "android"
    elif (re.search(r"windows|win", ch_platform, re.I) or re.search(r"win", platform, re.I)
          or re.search(r"Windows", ua, re.I)):
        os_family = "windows"
    elif (re.search(r"macos|mac", ch_platform, re.I) or re.search(r"mac", platform, re.I)
          or re.search(r"Mac OS X", ua, re.I)):
        os_family = "macos"
    elif (re.search(r"linux|chrome ?os|x11", ch_platform, re.I)
          or re.search(r"linux|x11", platform, re.I) or re.search(r"Linux", ua, re.I)):
        os_family = "linux"
    else:
        return None  # unclassifiable: skip rather than mislabel and mis-serve it

    # Version is OPTIONAL by design. Hardware-only imports (converted chrome-fingerprints records)
    # carry no browser version and inherit the engine's — see _profilelib.eligible.
    m = re.search(r"Chrome/(\d+)", ua)
    browser_major = int(m.group(1)) if m else None

    renderer = debug.get("UNMASKED_RENDERER_WEBGL")

    # The real launch cost, encoded exactly as _fingerprint.py encodes it, so an entry that would
    # blow the argv limit is excluded at SELECTION time rather than failing at spawn.
    raw = json.dumps(profile, separators=(",", ":")).encode("utf-8")
    encoded_size = len(base64.b64encode(gzip.compress(raw, 9)))

    return {
        "id": pid,
        "os_family": os_family,
        "browser_major": browser_major,
        "gpu_vendor": gpu_vendor_class(renderer),
        "renderer": renderer,
        "screen_width": screen.get("width"),
        "screen_height": screen.get("height"),
        "device_pixel_ratio": screen.get("device_pixel_ratio"),
        "hardware_concurrency": profile.get("hardware_concurrency"),
        "device_memory": profile.get("device_memory"),
        "encoded_size": encoded_size,
        "captured_at": meta.get("captured_at") if isinstance(meta.get("captured_at"), str) else None,
    }


def import_directory(directory: str) -> dict[str, Any]:
    """Index every ``*.json`` in a directory.

    Returns ``{"index": [...], "skipped": [{"file":…, "reason":…}]}``. Skips are REPORTED rather
    than swallowed, so a half-converted directory is visible instead of silently producing a
    thin pool.
    """
    if not os.path.isdir(directory):
        raise ValueError(f"profile source is not a directory: {directory}")

    index: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
        except (OSError, ValueError) as e:
            skipped.append({"file": name, "reason": f"unreadable JSON: {str(e)[:80]}"})
            continue
        # A clearcote-profile always has navigator; without it this is some other kind of file.
        if not isinstance(parsed, dict) or not parsed.get("navigator"):
            skipped.append({"file": name, "reason": "not a clearcote-profile (no navigator)"})
            continue
        entry = index_entry_from_profile(name[: -len(".json")], parsed)
        if entry is None:
            skipped.append({"file": name, "reason": "could not classify os_family"})
            continue
        index.append(entry)

    return {"index": index, "skipped": skipped}


def load_imported_profile(directory: str, pid: str) -> dict[str, Any]:
    """Load one profile from an imported directory, by the id ``import_directory`` assigned."""
    # The id is a filename stem this module produced, but it can arrive here from a cached index,
    # so treat it as untrusted: reject anything that could escape the source directory.
    if "/" in pid or "\\" in pid or ".." in pid:
        raise ValueError(f"invalid profile id '{pid}'")
    path = os.path.join(directory, f"{pid}.json")
    if not os.path.isfile(path):
        raise ValueError(f"profile '{pid}' not found in {directory}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
