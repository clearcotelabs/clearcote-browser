"""Canvas/WebGL noise mode per profile folder: one noise per profile for NEW profiles, per-site noise kept
for profiles that already have a history (SDK 0.70.5). Matches sdk/node/src/noisemode.ts.

With per-site noise (the engine default) a third-party frame (a captcha or anti-bot widget) reads a
different canvas on every site that embeds it, which no real device does; with one noise per profile it
reads one canvas everywhere, still different per profile. Switching an EXISTING profile would show every
site a new canvas on its next launch, so the mode is decided once per folder and remembered in
``<user_data_dir>/clearcote.json``:

- ``noise_per_profile`` set (True/False): used, and remembered for later launches that leave it unset.
- unset, folder remembered: its remembered mode.
- unset, new folder (missing, empty, or no Chrome data yet: no "Local State", no "Default"): per profile
  when the engine implements it (r60+), else per site; remembered either way.
- unset, folder with Chrome data but nothing remembered (made before 0.70.5): per site, remembered.

Throwaway launches are new by definition. A pass-through launch (fingerprint="off") is left alone.
"""
import json
import os
import sys

PROFILE_STATE_FILE = "clearcote.json"
_MODES = ("per-profile", "per-site")


def read_noise_mode(user_data_dir):
    """The mode remembered in the folder, or None."""
    try:
        with open(os.path.join(user_data_dir, PROFILE_STATE_FILE), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    mode = s.get("noise") if isinstance(s, dict) else None
    return mode if mode in _MODES else None


def has_chrome_data(user_data_dir):
    """True when the folder already holds a Chrome profile (the browser writes these on its first run)."""
    try:
        names = os.listdir(user_data_dir)
    except OSError:
        return False
    return "Local State" in names or "Default" in names


def write_noise_mode(user_data_dir, mode):
    """Remember the mode, keeping any other keys in the file. Raises OSError on an unwritable folder."""
    path = os.path.join(user_data_dir, PROFILE_STATE_FILE)
    s = {}
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
        if isinstance(cur, dict):
            s = cur
    except (OSError, ValueError):
        pass
    if s.get("noise") == mode:
        return
    s["noise"] = mode
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, indent=1) + "\n")


def resolve_noise_per_profile(requested, user_data_dir, exe=None, passthrough=False, quiet=False,
                              had_chrome_data=None, supports=None):
    """The noise mode for this launch, as ``noise_per_profile`` (True = one noise per profile).

    ``user_data_dir`` None = a launch with no folder (incognito), which is new. ``had_chrome_data``: the
    folder's state before this launch touched it (the SDK seeds Default/Preferences before the engine is
    known); None reads it now. Remembers the result in the folder, fail-soft."""
    if passthrough:
        return requested
    if supports is None:
        from ._launchopts import engine_supports_switch
        supports = lambda e: engine_supports_switch(e, "fingerprint-noise-per-profile")  # noqa: E731
    if requested is True or requested is False:
        per_profile = requested
    else:
        remembered = read_noise_mode(user_data_dir) if user_data_dir else None
        if remembered:
            per_profile = remembered == "per-profile"
        elif user_data_dir and (has_chrome_data(user_data_dir) if had_chrome_data is None else had_chrome_data):
            per_profile = False  # made before 0.70.5: keep its noise
        else:
            per_profile = bool(supports(exe))
    if user_data_dir:
        try:
            # A folder that does not exist yet is created here (Playwright would create it anyway): the
            # browser writes "Local State" on its first run, after which an unremembered folder reads as old.
            os.makedirs(user_data_dir, exist_ok=True)
            write_noise_mode(user_data_dir, "per-profile" if per_profile else "per-site")
        except OSError as e:
            if not quiet:
                print("[clearcote] could not remember the noise mode in %s (continuing): %s"
                      % (user_data_dir, e), file=sys.stderr)
    return per_profile
