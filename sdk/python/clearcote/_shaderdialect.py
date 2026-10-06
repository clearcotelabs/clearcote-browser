"""HLSL shader dialect for a Windows persona on a non-Windows host.

``WEBGL_debug_shaders.getTranslatedShaderSource()`` returns whatever ANGLE's active backend
produced, and Chrome exposes that extension to every page. A Windows persona advertises a
Direct3D11 renderer, but on a Linux host the Vulkan backend answers with a SPIR-V dump, so the
renderer string and the dialect beside it contradict each other -- a combination no real machine
produces. ``shader_dialect="hlsl"`` makes the engine re-translate the shader to HLSL for that query
alone -- rendering is untouched, and the result is byte-identical to what the Windows build reports.

ON BY DEFAULT for a Windows claim on a non-Windows host (the Docker entrypoint has done this for
CC_PLATFORM=windows since 0.31). Measured 2026-10-06 on a GPU-less Linux host with r30: a Windows
persona answered the query with SwiftShader's SPIR-V text next to "Direct3D11"; with the dialect set
it answered in HLSL. The re-translation is a different code path from the one that rendered, so a
shader the HLSL translator rejects falls back to the backend's own output for that shader -- the
state every launch was in before, so the default cannot make a page see anything worse. Pass
``shader_dialect=False`` to turn it off. On a Windows host the D3D11 backend already answers in HLSL
and nothing is set; a Linux or macOS claim never gets HLSL by default, nor does a launch that shows
the real GPU string (disable_gpu_fingerprint / gpu_string_spoof=False) or a custom non-Direct3D
renderer.

Delivered as an environment variable because the code lives in the GPU process, which does not
receive the fingerprint switches. Requires a PRO engine built with the option (151 r15+); older
binaries ignore the variable.
"""

import os
import sys

ENV_VAR = "CLEARCOTE_SHADER_DIALECT"
_VALID = ("hlsl",)
_OFF = ("", "0", "off", "false", "no", "none")


#: Switches under which the renderer string is the host's real GPU (SwiftShader, Mesa, ...), not the
#: persona's Direct3D11 one -- HLSL beside it would be the contradiction this option removes.
_REAL_GPU_STRING_SWITCHES = ("--disable-gpu-fingerprint", "--disable-gpu-string-spoof")


def default_shader_dialect(args, host_platform=None):
    """The dialect a launch gets when the caller did not choose one: ``"hlsl"`` when the built
    command line claims Windows (``--fingerprint-platform=windows``) with a Direct3D renderer string
    and the host is not Windows, else None. No default when the page sees the real GPU string
    (``--disable-gpu-fingerprint`` / ``--disable-gpu-string-spoof``) or a custom
    ``--fingerprint-gpu-renderer`` that does not name Direct3D."""
    host = sys.platform if host_platform is None else host_platform
    if host == "win32":
        return None
    claimed = None
    renderer = None
    for arg in args or ():
        if not isinstance(arg, str):
            continue
        if arg.startswith("--fingerprint-platform="):
            claimed = arg.split("=", 1)[1].strip().lower()  # the last one wins, as in Chromium
        elif arg.startswith("--fingerprint-gpu-renderer="):
            renderer = arg.split("=", 1)[1]
        elif arg.split("=", 1)[0] in _REAL_GPU_STRING_SWITCHES:
            return None
    if claimed != "windows":
        return None
    if renderer is not None and "direct3d" not in renderer.lower():
        return None
    return "hlsl"


def resolve_shader_dialect(value, args=None, host_platform=None):
    """None -> the default for this claim/host; False or an "off" spelling -> None; otherwise the
    normalised dialect (ValueError for an unknown one, so a typo cannot look like it worked)."""
    if value is None:
        return default_shader_dialect(args, host_platform)
    if value is False or str(value).strip().lower() in _OFF:
        return None
    dialect = str(value).strip().lower()
    if dialect not in _VALID:
        raise ValueError(
            "shader_dialect must be one of %s (got %r)" % (", ".join(_VALID), value))
    return dialect


def shader_dialect_env(value, args, env, host_platform=None):
    """Set ``CLEARCOTE_SHADER_DIALECT`` in the dict ``env`` (in place) and return it.

    An explicit ``value`` always wins; the default never overrides a variable the caller already
    exported themselves."""
    dialect = resolve_shader_dialect(value, args, host_platform)
    if dialect and (value is not None or ENV_VAR not in env):
        env[ENV_VAR] = dialect
    return env


def apply_shader_dialect(value, pw_kwargs, args=None, host_platform=None):
    """Merge ``CLEARCOTE_SHADER_DIALECT`` into ``pw_kwargs['env']``.

    No-op when nothing applies (no explicit dialect, and not a Windows claim on a non-Windows host),
    so the default launch env is left untouched. Playwright replaces the child env when ``env`` is
    set, so ``os.environ`` is the base when nothing has built the env yet.

    Call this AFTER ``apply_font_env``: that one rebuilds the dict from ``os.environ`` and would
    drop this variable if it ran second.
    """
    dialect = resolve_shader_dialect(value, args, host_platform)
    if not dialect:
        return
    base = pw_kwargs.get("env") or os.environ
    if value is None and ENV_VAR in base:
        return  # the caller exported their own choice; the default does not override it
    merged = dict(base)
    merged[ENV_VAR] = dialect
    pw_kwargs["env"] = merged
