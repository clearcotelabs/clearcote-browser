"""Live render-backend coherence probe (#7).

The launch-time heuristic in :mod:`clearcote._warnings` flags the *config* most likely to leak a
software rasterizer (headless + no canvas bridge). This module is the *page-level* counterpart: it
reads what the page actually sees from WebGL and checks it for the two render-coherence tells a strict
detector looks for:

  1. a **software rasterizer** in the (unmasked) renderer string - SwiftShader / llvmpipe / Mesa
     OffScreen / "Microsoft Basic Render". On a "stealth" browser this is fatal: it means the GPU
     spoof did not apply, or the build is rendering on the CPU. The fix is the canvas bridge (forward
     paints to a real GPU) or running headed on a machine with a real GPU.
  2. an **incoherent vendor/renderer pair** - e.g. a renderer that names an NVIDIA GPU paired with an
     Intel/Apple vendor. A coherent persona never disagrees with itself.

It does NOT (and cannot, from inside the page) read the *real* host GPU when the persona spoofs the
unmasked strings - that is the whole point of the spoof. What it verifies is that the values the page
is allowed to see are internally coherent and not a software fallback. For the deeper "do the rendered
pixels match the claimed GPU class" check, route paints through the canvas bridge.
"""

# Shared probe JS - reads VENDOR/RENDERER + the unmasked pair via a throwaway WebGL context, with a
# graceful fallback if WebGL is unavailable. Returned to the SDK as a plain dict.
PROBE_JS = r"""
() => {
  const out = { webgl: false, webgl2: false, vendor: "", renderer: "",
                unmaskedVendor: "", unmaskedRenderer: "", maxTextureSize: 0 };
  try {
    const c = document.createElement('canvas');
    const gl2 = c.getContext('webgl2');
    const gl = gl2 || c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return out;
    out.webgl = true;
    out.webgl2 = !!gl2;
    out.vendor = gl.getParameter(gl.VENDOR) || "";
    out.renderer = gl.getParameter(gl.RENDERER) || "";
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    if (dbg) {
      out.unmaskedVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) || "";
      out.unmaskedRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || "";
    }
    out.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE) || 0;
  } catch (e) { out.error = String(e); }
  return out;
}
"""

_SOFTWARE_MARKERS = (
    "swiftshader", "google swiftshader", "llvmpipe", "softpipe",
    "mesa offscreen", "microsoft basic render", "software adapter",
)

# substring -> GPU family. Order matters only in that the first hit wins per string.
_FAMILY_KEYS = (
    ("nvidia", "nvidia"), ("geforce", "nvidia"), ("rtx", "nvidia"), ("gtx", "nvidia"), ("quadro", "nvidia"),
    ("radeon", "amd"), ("amd", "amd"), ("ati ", "amd"),
    ("intel", "intel"), ("iris", "intel"), ("uhd graphics", "intel"), ("hd graphics", "intel"),
    ("apple", "apple"), ("m1", "apple"), ("m2", "apple"), ("m3", "apple"), ("m4", "apple"),
    ("mali", "mali"), ("adreno", "adreno"), ("powervr", "powervr"),
)


def _family(s):
    """Best-effort GPU family from a vendor/renderer string ('' if unknown)."""
    s = (s or "").lower()
    for key, fam in _FAMILY_KEYS:
        if key in s:
            return fam
    return ""


def evaluate_render_info(info, claimed_gpu=None):
    """Pure analysis of a probe result dict -> coherence verdict (unit-testable, no Playwright)."""
    renderer = info.get("unmaskedRenderer") or info.get("renderer") or ""
    vendor = info.get("unmaskedVendor") or info.get("vendor") or ""
    rl, vl = renderer.lower(), vendor.lower()
    warnings = []

    has_webgl = bool(info.get("webgl"))
    if not has_webgl:
        warnings.append(
            "WebGL is unavailable - a hard tell for a real desktop browser (only headless or "
            "locked-down setups disable it)."
        )

    software = any(m in rl or m in vl for m in _SOFTWARE_MARKERS)
    if software:
        warnings.append(
            f"software rasterizer detected in the WebGL renderer ({renderer!r}) - a definitive "
            "headless/no-GPU tell. Enable the canvas bridge (canvas_bridge=...) or run headed on a "
            "machine with a real GPU."
        )

    rfam, vfam = _family(rl), _family(vl)
    if rfam and vfam and rfam != vfam:
        warnings.append(
            f"WebGL vendor and renderer disagree on GPU family (vendor~{vfam}, renderer~{rfam}) - "
            "an incoherent persona."
        )

    if claimed_gpu:
        cfam = _family(claimed_gpu)
        if cfam and rfam and cfam != rfam:
            warnings.append(
                f"the claimed GPU ({claimed_gpu!r}, family ~{cfam}) does not match the WebGL "
                f"renderer family (~{rfam})."
            )

    coherent = has_webgl and not software and not any("disagree" in w or "does not match" in w for w in warnings)
    return {
        "vendor": vendor,
        "renderer": renderer,
        "webgl": has_webgl,
        "webgl2": bool(info.get("webgl2")),
        "max_texture_size": info.get("maxTextureSize") or 0,
        "software_suspected": software,
        "coherent": coherent,
        "warnings": warnings,
    }


def check_render_coherence(page, claimed_gpu=None):
    """Probe a live Playwright ``page`` for render-backend coherence (#7).

    Returns a dict with ``vendor``/``renderer`` (the values the page actually sees),
    ``software_suspected`` (bool - a SwiftShader/llvmpipe fallback is a fatal tell), ``coherent``
    (bool), and human-readable ``warnings``. Pass ``claimed_gpu`` (the GPU string your persona is
    supposed to present) to additionally assert the rendered family matches.

    Example::

        br = clearcote.launch(fingerprint="77")
        page = br.new_page(); page.goto("about:blank")
        verdict = clearcote.check_render_coherence(page)
        assert verdict["coherent"], verdict["warnings"]
    """
    info = page.evaluate(PROBE_JS)
    return evaluate_render_info(info, claimed_gpu)
