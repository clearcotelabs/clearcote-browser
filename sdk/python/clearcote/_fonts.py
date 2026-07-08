"""Linux font wiring.

The Linux release bundles metric-compatible font clones (Segoe UI->Selawik, Arial->Arimo,
Times New Roman->Tinos, ...) under ``<bin_dir>/fonts/`` with a self-contained
``fonts.conf.template``. On a bare server/container the Windows families (and even the
standard fontconfig metric-alias rules) are absent, so a page asking for "Segoe UI"
collapses to a single default -- a detectable render + an absent-font tell.

At launch we materialize the template (substituting the real fonts dir + a writable cache
dir) and point FONTCONFIG_FILE at it, so the clones resolve without depending on the host's
/etc/fonts. No-op on non-Linux and on older binaries that ship no ``fonts/``.
"""

import os
import sys
import tempfile


def linux_font_env(exe_path):
    """Return ``{"FONTCONFIG_FILE": ...}`` on Linux when the font bundle is present, else ``{}``."""
    if sys.platform != "linux":
        return {}
    fonts_dir = os.path.join(os.path.dirname(exe_path), "fonts")
    template = os.path.join(fonts_dir, "fonts.conf.template")
    if not os.path.isfile(template):
        return {}
    try:
        cache_dir = os.path.join(tempfile.gettempdir(), "cc-fc-cache")
        os.makedirs(cache_dir, exist_ok=True)
        with open(template, "r", encoding="utf-8") as fh:
            conf = fh.read()
        conf = conf.replace("@FONTS_DIR@", fonts_dir).replace("@CACHE_DIR@", cache_dir)
        conf_path = os.path.join(fonts_dir, "fonts.generated.conf")
        with open(conf_path, "w", encoding="utf-8") as fh:
            fh.write(conf)
        return {"FONTCONFIG_FILE": conf_path}
    except OSError:
        return {}  # never block a launch on font wiring


def apply_font_env(exe_path, pw_kwargs):
    """Merge the bundled-font FONTCONFIG_FILE into ``pw_kwargs['env']`` for Playwright's launch.

    Playwright replaces the child env when ``env`` is set, so we include ``os.environ`` too.
    Precedence: os.environ < bundled fonts < caller-supplied env. No-op when there's nothing
    to add (leaves ``pw_kwargs`` untouched so Playwright uses the default env).
    """
    font_env = linux_font_env(exe_path)
    user_env = pw_kwargs.get("env")
    if not font_env and not user_env:
        return
    merged = dict(os.environ)
    merged.update(font_env)
    if user_env:
        merged.update(user_env)
    pw_kwargs["env"] = merged
