"""Saved, launchable personas.

A :class:`Profile` bundles a set of launch options — the fingerprint persona (seed, GPU,
brand, …) **and** its ``canvas_bridge`` config — under a name you can persist and re-launch
as one coherent identity:

    from clearcote import Profile

    Profile("acct-1", {
        "fingerprint": "acct-1",
        "gpu_vendor": "Google Inc. (Intel)",
        "gpu_renderer": "ANGLE (Intel, Intel(R) UHD Graphics ... D3D11)",
        "canvas_bridge": {"url": "ws://127.0.0.1:9099", "auth": "user:secret"},
    }).save()

    browser = Profile.load("acct-1").launch(headless=False)   # or: launch(profile="acct-1")

Profiles are plain JSON at ``~/.clearcote/profiles/<name>.json`` (override the dir with
``CLEARCOTE_PROFILE_DIR``). The persona's claimed GPU, the bridge endpoint, and the bridge's
GPU-keyed cache stay coherent because they travel together in one file.

Fingerprint option keys are normalized on load, so a profile written by the Node SDK
(camelCase) loads correctly here (snake_case) and vice versa.

SECURITY: a profile is a plaintext file that may hold credentials (e.g. ``canvas_bridge.auth``)
— it is written ``0600`` but is NOT encrypted; do not commit or share it. Treat profile *names*
and profile *files* as trusted input (a loaded profile can set any launch option).
"""

import json
import os
import re

from ._fingerprint import FINGERPRINT_KEYS

PROFILE_DIR = os.environ.get(
    "CLEARCOTE_PROFILE_DIR", os.path.join(os.path.expanduser("~"), ".clearcote", "profiles"))

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_CAMEL = re.compile(r"([A-Z])")


def _to_snake(key):
    return _CAMEL.sub(lambda m: "_" + m.group(1).lower(), key)


def _normalize_keys(options):
    """Map camelCase fingerprint keys (e.g. a Node-written profile) to this SDK's snake_case."""
    out = {}
    for key, value in options.items():
        snake = _to_snake(key)
        out[snake if snake in FINGERPRINT_KEYS else key] = value
    return out


def _profile_path(name_or_path):
    """A bare name resolves to ``PROFILE_DIR/<name>.json``; a path (has a separator or a ``.json``
    suffix) is used verbatim. Bare names must be safe slugs — no separators, no ``..`` — so an
    untrusted name can't traverse out of PROFILE_DIR."""
    looks_like_path = (
        os.sep in name_or_path
        or (os.altsep and os.altsep in name_or_path)
        or name_or_path.endswith(".json")
    )
    if looks_like_path:
        return name_or_path
    if not _SAFE_NAME.match(name_or_path):
        raise ValueError(
            "invalid profile name %r — use [A-Za-z0-9._-] (or pass an explicit path)" % name_or_path)
    return os.path.join(PROFILE_DIR, name_or_path + ".json")


class Profile:
    """A named bundle of :func:`clearcote.launch` options (a saved persona)."""

    def __init__(self, name, options=None):
        self.name = name
        self.options = dict(options or {})

    def set(self, **options):
        """Merge in more options; returns self for chaining."""
        self.options.update(options)
        return self

    @property
    def path(self):
        return _profile_path(self.name)

    def save(self, path=None):
        """Persist as JSON (defaults to ``~/.clearcote/profiles/<name>.json``). Returns the path.

        The directory is created ``0700`` and the file written ``0600`` (it may hold secrets)."""
        dest = path or self.path
        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        # write private (0600) from the start so a credential is never briefly world-readable
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(dest, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"name": self.name, "options": self.options}, handle, indent=2)
        return dest

    @classmethod
    def load(cls, name_or_path):
        """Load a saved profile by name (under PROFILE_DIR) or by explicit path."""
        with open(_profile_path(name_or_path), encoding="utf-8") as handle:
            data = json.load(handle)
        name = data.get("name") or os.path.basename(name_or_path)
        if name.endswith(".json"):
            name = name[:-5]
        return cls(name, _normalize_keys(data.get("options") or {}))

    def launch(self, **overrides):
        """Launch this persona; explicit ``overrides`` win over the saved options."""
        from . import launch
        return launch(**{**self.options, **overrides})

    def launch_persistent_context(self, user_data_dir, **overrides):
        """Launch this persona with a persistent profile directory."""
        from . import launch_persistent_context
        return launch_persistent_context(user_data_dir, **{**self.options, **overrides})

    def __repr__(self):
        return "Profile(%r, %d options)" % (self.name, len(self.options))


def list_profiles():
    """Names of the saved profiles under ``CLEARCOTE_PROFILE_DIR``."""
    if not os.path.isdir(PROFILE_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".json"))


def load_profile(name_or_path):
    """Convenience wrapper for :meth:`Profile.load`."""
    return Profile.load(name_or_path)


def resolve_profile_options(profile):
    """Return the saved (snake_case-normalized) options for a ``profile`` kwarg."""
    if isinstance(profile, Profile):
        return _normalize_keys(profile.options)
    return dict(Profile.load(profile).options)
