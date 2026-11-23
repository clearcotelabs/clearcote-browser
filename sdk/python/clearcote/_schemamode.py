"""Persona schema per profile folder: schema 8 for NEW profiles, the schema a profile was made with kept for
profiles that already have a history (SDK 0.70.6). Matches sdk/node/src/schemamode.ts and NoiseMode-style
SchemaMode.cs.

Schema 8 is the persona service's Windows machine model re-weighted on the 2026-11 /audit corpus (screen sizes and
scaling, RAM, CPU/RAM pairs, touchscreens closer to real Windows machines; gen-156-s8b). The service's default stays
7, and switching an EXISTING profile would give it a new screen, RAM and touch value on its next launch, so the
schema is decided once per folder and remembered in ``<user_data_dir>/clearcote.json`` (key ``schema``):

- ``persona_schema`` set, or ``--fingerprint-schema=N`` in ``args``: used, and remembered for later launches that
  leave it unset.
- unset, folder remembered: its remembered schema.
- unset, new folder (missing, empty, or no Chrome data yet: no "Local State", no "Default"): 8 when the engine takes
  its persona from the service (156 r51+), else 7; remembered either way.
- unset, folder with Chrome data but nothing remembered (made before 0.70.6): 7, remembered.

A folder on 7 launches exactly as before (no switch is added). Throwaway launches are new by definition. A
pass-through launch (fingerprint="off") is left alone.
"""
import json
import os
import sys

from ._noisemode import PROFILE_STATE_FILE

NEW_PROFILE_SCHEMA = "8"
EARLIER_PROFILE_SCHEMA = "7"  # the persona service's default since 2026-10-26


def _norm(v):
    """A schema value as the engine switch spells it ("1".."9"), or None."""
    s = str(v).strip() if v is not None else ""
    return s if len(s) == 1 and s in "123456789" else None


def read_schema(user_data_dir):
    """The schema remembered in the folder, or None."""
    try:
        with open(os.path.join(user_data_dir, PROFILE_STATE_FILE), encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        return None
    return _norm(s.get("schema")) if isinstance(s, dict) else None


def write_schema(user_data_dir, schema):
    """Remember the schema, keeping any other keys in the file. Raises OSError on an unwritable folder."""
    path = os.path.join(user_data_dir, PROFILE_STATE_FILE)
    s = {}
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
        if isinstance(cur, dict):
            s = cur
    except (OSError, ValueError):
        pass
    if s.get("schema") == schema:
        return
    s["schema"] = schema
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, indent=1) + "\n")


def schema_in_args(args):
    """The last --fingerprint-schema=N in a switch list, or None."""
    found = None
    for a in args or ():
        if isinstance(a, str) and a.startswith("--fingerprint-schema="):
            found = a.split("=", 1)[1]
    return _norm(found) if found is not None else None


def resolve_persona_schema(requested, user_data_dir, exe=None, args=None, passthrough=False, quiet=False,
                           had_chrome_data=None, supports=None):
    """The ``persona_schema`` value for this launch: a string to set, or None to add nothing.

    ``user_data_dir`` None = a launch with no folder (incognito), which is new. ``had_chrome_data``: the folder's
    state before this launch touched it; None reads it now. Remembers the schema in the folder, fail-soft.
    Returns the explicit ``requested`` value unchanged when there is one, None when ``args`` already pin a
    schema or the schema is 7 (the service default: the launch stays byte-identical to an earlier SDK's)."""
    if passthrough:
        return requested
    if supports is None:
        from ._launchopts import engine_supports_switch
        # The persona service (156 r51+) is where schema 8 lives; an older engine derives its own identity.
        supports = lambda e: engine_supports_switch(e, "cc-persona-host-facts")  # noqa: E731
    explicit = _norm(requested) if requested not in (None, "") else None
    pinned = schema_in_args(args)
    if explicit:
        schema = explicit
    elif pinned:
        schema = pinned
    else:
        remembered = read_schema(user_data_dir) if user_data_dir else None
        if remembered:
            schema = remembered
        elif user_data_dir and (has_chrome_data_now(user_data_dir) if had_chrome_data is None else had_chrome_data):
            schema = EARLIER_PROFILE_SCHEMA  # made before 0.70.6: keep its machine
        else:
            schema = NEW_PROFILE_SCHEMA if supports(exe) else EARLIER_PROFILE_SCHEMA
    if user_data_dir:
        try:
            os.makedirs(user_data_dir, exist_ok=True)
            write_schema(user_data_dir, schema)
        except OSError as e:
            if not quiet:
                print("[clearcote] could not remember the persona schema in %s (continuing): %s"
                      % (user_data_dir, e), file=sys.stderr)
    if explicit:
        return requested
    if pinned or schema == EARLIER_PROFILE_SCHEMA:
        return None
    return schema


def has_chrome_data_now(user_data_dir):
    from ._noisemode import has_chrome_data
    return has_chrome_data(user_data_dir)
