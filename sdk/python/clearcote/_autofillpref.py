"""Autofill on, as in Chrome.

The engine's own defaults (r54) have "Save and fill addresses" and "Save and fill payment methods" off
(``autofill.profile_enabled`` / ``autofill.credit_card_enabled`` = false); Chrome 156 has both on. With
addresses off, the browser also never remembers what was typed into single fields (emails, names), so a
profile behaved unlike a real Chrome. Measured on r54 (10-30): rows in Default/Web Data ``autofill`` after a
form submit: Chrome 2, r54 0, r54 with these prefs seeded 2.

Seeded into ``<user_data_dir>/Default/Preferences`` before every launch on a profile directory the SDK hands
the browser (launch_persistent_context, launch()'s throwaway profile, serve()). Only a missing key is set: a
profile where someone turned autofill off keeps it off. Mirrors sdk/node/src/autofillpref.ts and the .NET
AutofillPrefs.cs (and the Profile Manager's electron/autofillpref.ts).
"""
from __future__ import annotations

import json
import os
import sys

AUTOFILL_DEFAULTS = {"profile_enabled": True, "credit_card_enabled": True}


def seed_autofill_prefs(user_data_dir):
    """Merge Chrome's autofill defaults into ``<user_data_dir>/Default/Preferences`` where they are missing.

    Returns the keys it set. A Preferences file that cannot be parsed is left alone."""
    d = os.path.join(user_data_dir, "Default")
    path = os.path.join(d, "Preferences")
    prefs = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            prefs = json.load(f)
        if not isinstance(prefs, dict):
            return []
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return []  # the browser's file, not ours to replace
    autofill = prefs.get("autofill")
    if not isinstance(autofill, dict):
        autofill = {}
    keys = [k for k in AUTOFILL_DEFAULTS if k not in autofill]
    if not keys:
        return []
    for k in keys:
        autofill[k] = AUTOFILL_DEFAULTS[k]
    prefs["autofill"] = autofill
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(prefs, ensure_ascii=False, separators=(",", ":")))
    return keys


def apply_autofill_prefs(user_data_dir, quiet=False):
    """:func:`seed_autofill_prefs` at launch: never fails a launch (an unwritable profile fails in Chrome anyway)."""
    try:
        seed_autofill_prefs(user_data_dir)
    except Exception as e:  # noqa: BLE001 — best-effort, like the other profile seeding
        if not quiet:
            sys.stderr.write("[clearcote] could not turn autofill on in %s (continuing): %s\n" % (user_data_dir, e))
