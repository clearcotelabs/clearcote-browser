"""Persona font lists, and the warning for a persona this host cannot reach.

WHY FONTS AND NOT SOMETHING ELSE. Measured over the capture corpus, the font set is the most
identifying surface by a wide margin -- 9.45 bits of entropy, and 35.0% of real machines carry a
font set nobody else has. The WebGL renderer string is 7.25 bits / 1.7% unique, screen width
3.29 bits / 1.4%. Canvas and GPU put a machine in a bucket; a font list frequently names it.

WHAT THE ENGINE ALREADY DOES. For every family a page asks for, Blink decides present/absent in
FontCache::GetFontPlatformData (third_party/blink/renderer/platform/fonts/font_cache.cc:301, the
persona block at :335-390). It picks a reference list -- the canonical per-OS set
(GetPlatformFonts, :134) or, when a profile was imported, THAT PROFILE'S OWN font list
(:345-350) -- and reports anything outside it absent. What the page finally sees is therefore
host INTERSECT list: a listed font that is not installed still fails to load and measures
absent, so a list can never over-claim.

WHAT WAS MISSING. A seeded launch with no profile fell back to the canonical set, which is
byte-identical on every install: it contributes zero entropy while sitting in a conspicuous
tail (two different personas produced identical font fingerprints on both Windows hosts we
measured). This module makes sure a seeded persona always carries a REAL machine's font list
instead, drawn from the local profile corpus -- diversity per persona, realism because the
lists come from real machines, and safety because the host intersection still bounds it.

Everything here is best effort. A missing corpus, an unreadable profile or a host whose fonts
cannot be enumerated must never fail a launch; each of those simply skips the improvement.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Any, Optional

#: Injecting a font list is only worth doing above this size -- a donor with a handful of fonts
#: is more likely a broken capture than a real machine.
_MIN_DONOR_FONTS = 40
#: A donor is "reachable enough" when this share of its fonts can render on this host.
_GOOD_REACH = 0.60
#: Bounded I/O: how many seeded candidates to try before accepting the best one seen.
_MAX_CANDIDATES = 8

#: Families every Windows 11 install has (Microsoft's Windows 11 font list marks them "Added in
#: Windows 11"). The capture collector never probed them, so no corpus donor list carries them -- a
#: seeded Windows persona, which always claims Windows 11 (platformVersion 15.0.0 or 19.0.0), then
#: hid fonts no Windows 11 machine lacks. Worse, 87% of donor lists DO carry HoloLens MDL2 Assets,
#: and CreepJS reads "HoloLens + Segoe MDL2 + Bahnschrift + Ink Free without Segoe Fluent Icons" as
#: Windows 10, which it then flags against a Windows 11 platformVersion ("lied platform version").
#: Cascadia Code/Mono are deliberately absent: they ship per user with Windows Terminal, so their
#: absence is normal. So is the bare "Segoe UI Variable": genuine Chrome 154 on Windows 11 reports
#: that family absent (only the Display/Small/Text faces resolve), and on a Linux host the alias
#: table would map it to a stand-in. Listing a family never over-claims on a Windows host -- the
#: engine reports host INTERSECT list.
WINDOWS11_FACES = ("Segoe Fluent Icons", "Segoe UI Variable Display", "Segoe UI Variable Small",
                   "Segoe UI Variable Text")


def claims_windows11(platform_version) -> bool:
    """True when a Windows persona with this UA-CH platformVersion claims Windows 11 (major >= 13).
    None/empty means the engine's own draw, which is always Windows 11 (15.0.0 or 19.0.0)."""
    if platform_version in (None, ""):
        return True
    head = str(platform_version).strip().split(".")[0]
    return head.isdigit() and int(head) >= 13


def with_windows11_faces(fonts: list) -> list:
    """``fonts`` plus any WINDOWS11_FACES it lacks (order kept, nothing duplicated)."""
    have = {str(f).lower() for f in fonts}
    return list(fonts) + [f for f in WINDOWS11_FACES if f.lower() not in have]


def profile_with_windows11_faces(profile: Any) -> Any:
    """A captured profile (dict) whose fonts gain WINDOWS11_FACES when it claims Windows 11.

    For profiles the SDK picked itself (profile="auto"); a profile the caller passed is never touched.
    Returns the input unchanged unless it is a Windows profile with a font list and a Windows 11
    platformVersion; otherwise a copy, so a cached corpus entry is never mutated."""
    if not isinstance(profile, dict):
        return profile
    nav = profile.get("navigator") if isinstance(profile.get("navigator"), dict) else {}
    uadata = nav.get("uadata") if isinstance(nav.get("uadata"), dict) else {}
    hints = uadata.get("high_entropy") if isinstance(uadata.get("high_entropy"), dict) else {}
    platform = str(hints.get("platform") or uadata.get("platform") or nav.get("platform") or "")
    version = hints.get("platformVersion")
    fonts = profile.get("fonts") if isinstance(profile.get("fonts"), dict) else None
    detected = fonts.get("detected") if fonts else None
    if (not platform.lower().startswith("win") or not isinstance(detected, list)
            or not version or not claims_windows11(version)):
        return profile
    out = dict(profile)
    out["fonts"] = dict(fonts)
    out["fonts"]["detected"] = with_windows11_faces(detected)
    return out


_host_families: Optional[frozenset] = None
_host_families_done = False
_picked: dict = {}          # (seed, platform) -> list[str], one pick per process
_noted: set = set()         # fire-once notes


def _note(quiet: bool, code: str, msg: str) -> None:
    if quiet or os.environ.get("CLEARCOTE_NO_WARN") or code in _noted:
        return
    _noted.add(code)
    sys.stderr.write("[clearcote] [fonts] %s\n" % msg)


# --------------------------------------------------------------------------------------- host

def _families_windows() -> Optional[frozenset]:
    try:
        import winreg
    except ImportError:
        return None
    names = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(
                hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        try:
            for i in range(winreg.QueryInfoKey(key)[1]):
                names.append(winreg.EnumValue(key, i)[0])
        except OSError:
            pass
    if not names:
        return None
    fams = set()
    for value in names:
        # "Segoe UI Semibold (TrueType)", "Arial Bold & Arial Bold Italic (TrueType)"
        value = re.sub(r"\s*\((?:TrueType|OpenType|VGA res|All res)[^)]*\)\s*$", "", value)
        for part in value.split("&"):
            part = part.strip()
            if part:
                fams.add(part.lower())
    return frozenset(fams)


def _families_fontconfig() -> Optional[frozenset]:
    try:
        proc = subprocess.run(["fc-list", ":", "family"], capture_output=True,
                              text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    fams = set()
    for line in proc.stdout.splitlines():
        for part in line.split(","):
            part = part.strip()
            if part:
                fams.add(part.lower())
    return frozenset(fams) or None


def host_font_families() -> Optional[frozenset]:
    """Lower-cased family names installed on THIS host, or None when it cannot be determined.

    Windows reads the font registry (no subprocess); Linux/BSD shells out to fc-list once.
    macOS returns None deliberately: the cheap enumerations there (globbing the font
    directories) give file names rather than family names, and the accurate one
    (`system_profiler SPFontsDataType`) takes seconds -- too slow for a launch path. Callers
    must treat None as "unknown", never as "no fonts".
    """
    global _host_families, _host_families_done
    if _host_families_done:
        return _host_families
    _host_families_done = True
    try:
        if sys.platform == "win32":
            _host_families = _families_windows()
        elif sys.platform == "darwin":
            _host_families = None
        else:
            _host_families = _families_fontconfig()
    except Exception:
        _host_families = None
    return _host_families


#: Families the engine lets through regardless of the reference list -- IsBasicFont(),
#: font_cache.cc:207. They resolve on every OS, so they are always reachable.
_BASIC = frozenset(x.lower() for x in (
    "-webkit-standard", "system-ui", "Arial", "Courier New", "Courier", "Helvetica",
    "Helvetica Neue", "Roboto", "Sans", "Times New Roman", "Times", "cursive", "fantasy",
    "monospace", "sans-serif", "serif", "math", "BlinkMacSystemFont"))


def reachable_count(claimed, host_families) -> int:
    """How many of `claimed` can actually render here (host INTERSECT list, plus basics)."""
    n = 0
    for f in claimed:
        low = str(f).lower()
        if low in _BASIC or low in host_families:
            n += 1
    return n


# ------------------------------------------------------------------------------------- picking

def profile_fonts(value) -> list:
    """`fonts.detected` out of a fingerprint_profile given as dict / path / JSON string."""
    try:
        if isinstance(value, dict):
            obj = value
        elif isinstance(value, (bytes, bytearray)):
            obj = json.loads(value)
        elif isinstance(value, str) and os.path.isfile(value):
            with open(value, encoding="utf-8") as handle:
                obj = json.load(handle)
        else:
            obj = json.loads(value)
    except (ValueError, OSError, TypeError):
        return []
    if not isinstance(obj, dict):
        return []
    detected = (obj.get("fonts") or {}).get("detected")
    return [f for f in detected if isinstance(f, str)] if isinstance(detected, list) else []


def _seeded_order(seed: str, entries: list) -> list:
    """Deterministic per-seed ordering, so one persona always draws the same donor."""
    def rank(entry):
        h = hashlib.sha256(("%s|%s" % (seed, entry.get("id", ""))).encode("utf-8"))
        return h.digest()
    return sorted(entries, key=rank)


def _pick_font_list(seed: str, platform: str, quiet: bool) -> Optional[list]:
    """A real machine's font list for this seed, or None when the corpus is unavailable."""
    from ._profileauto import DEFAULT_LOCAL_DIR, load_local_index
    from ._profileimport import load_imported_profile

    directory = DEFAULT_LOCAL_DIR
    if not os.path.isdir(directory):
        _note(quiet, "no-corpus",
              "no local profile corpus at %s, so this seeded persona falls back to the engine's "
              "canonical %s font list -- identical on every install and therefore worth zero "
              "entropy. Point CLEARCOTE_PROFILE_SOURCE at a profile directory, or pass "
              'profile="auto" / fingerprint_profile=..., to carry a real machine\'s font set.'
              % (directory, platform))
        return None
    entries = [e for e in load_local_index(directory)
               if (e.get("os_family") or "").lower() == platform]
    if not entries:
        return None

    host = host_font_families()
    best, best_reach = None, -1.0
    for entry in _seeded_order(seed, entries)[:_MAX_CANDIDATES]:
        try:
            fonts = profile_fonts(load_imported_profile(directory, entry["id"]))
        except Exception:
            continue
        if len(fonts) < _MIN_DONOR_FONTS:
            continue
        if host is None:
            return fonts          # cannot score reachability; the first valid donor it is
        reach = reachable_count(fonts, host) / float(len(fonts))
        if reach > best_reach:
            best, best_reach = fonts, reach
        if reach >= _GOOD_REACH:
            break                 # good enough; stop reading files
    return best


# ---------------------------------------------------------------------------------------- API

def ensure_persona_fonts(fp: dict, quiet: bool = False) -> None:
    """Give a seeded persona a real machine's font list (plan Fix 1). Mutates `fp` in place.

    Each no-op below is a decision, not a shortcut:

    * ``fingerprint_profile`` already set -- an explicit profile, or one resolved by
      ``profile="auto"``, owns the font list. Overwriting it would silently hand the caller an
      identity other than the one they asked for. Same precedence rule as _apply_auto_profile.
    * ``light_stealth`` -- that preset deliberately emits no ``--fingerprint`` at all so the
      persona machinery never engages (_fingerprint.py, the light_stealth branch of
      fingerprint_args). Attaching a profile would re-engage CurrentPersona() and break the
      shipped contract of the preset.
    * no seed -- with neither ``--fingerprint`` nor ``--fingerprint-profile`` the engine leaves
      font resolution completely untouched (font_cache.cc:316). A caller who passed nothing
      must keep the real host behaviour, so we do not invent a persona for them.
    * no corpus / unreadable donor -- best effort only. Never fail a launch over fonts.
    """
    try:
        if fp.get("fingerprint_profile") is not None:
            return
        if fp.get("light_stealth"):
            return
        seed = fp.get("fingerprint")
        if seed in (None, ""):
            return
        platform = (fp.get("platform")
                    or {"win32": "windows", "linux": "linux",
                        "darwin": "macos"}.get(sys.platform, "windows"))
        if platform not in ("windows", "macos", "linux"):
            return                # android personas have no canonical desktop font set
        key = (str(seed), platform)
        if key not in _picked:
            _picked[key] = _pick_font_list(str(seed), platform, quiet)
        fonts = _picked[key]
        if fonts and platform == "windows" and claims_windows11(fp.get("platform_version")):
            fonts = with_windows11_faces(fonts)
        if fonts:
            fp["fingerprint_profile"] = {"fonts": {"detected": list(fonts)}}
    except Exception:
        return  # fonts are an improvement, never a launch requirement


def font_reachability(profile: Any) -> Optional[tuple]:
    """``(claimed, reachable)`` for a fingerprint_profile, or None when it cannot be judged.

    Used by the coherence warnings (plan Fix 4): importing a 1,513-font persona onto a
    115-font host silently yields ~65 fonts and says nothing, which leaves the caller believing
    they present an identity they do not.
    """
    claimed = profile_fonts(profile)
    if not claimed:
        return None
    host = host_font_families()
    if host is None:
        return None
    return len(claimed), reachable_count(claimed, host)
