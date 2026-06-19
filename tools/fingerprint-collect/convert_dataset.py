#!/usr/bin/env python3
"""Convert the open-source chrome-fingerprints dataset into clearcote-profile JSON.

The dataset (https://github.com/Vinyzu/chrome-fingerprints) ships 10,000 real Windows-Chrome
fingerprints with strings interned as integer indices. This tool resolves the interning and
remaps each record into the clearcote-profile schema (the same one tools/fingerprint-collect
captures), so every record is usable via the SDK's ``fingerprint_profile`` option or the engine's
``--fingerprint-profile`` switch.

WHAT IT IMPORTS — the version-INDEPENDENT hardware identity: GPU (WebGL unmasked vendor/renderer
+ the GL/GL2 MAX_* limits + bit depths/ranges), screen geometry, fonts, speech voices, Web Audio
metadata, CPU/memory, keyboard layout.

WHAT IT SKIPS BY DEFAULT — the Chrome VERSION. The dataset records are Chrome ~114/115; the
clearcote binary is 149. Importing the dataset's ``uaFullVersion`` would make
navigator.userAgentData disagree with the real UA string (a coherence tell), so the converter
leaves the version to clearcote's native 149. Pass --include-version to import it anyway (only
sensible if your binary's major version matches the dataset).

USAGE
  pip install chrome-fingerprints          # provides the dataset + tables
  python convert_dataset.py --out ./profiles --count 100
  python convert_dataset.py --index 0 --stdout            # one record to stdout
  # or point at a checkout:
  python convert_dataset.py --dataset /path/to/chrome_fingerprints --out ./profiles

Then, with the SDK:
  # Python
  from clearcote import launch
  launch(fingerprint="seed-1", fingerprint_profile="./profiles/profile-00000.json")
  # Node
  launch({ fingerprint: "seed-1", fingerprintProfile: "./profiles/profile-00000.json" })
"""
import argparse
import importlib.util
import json
import lzma
import os
import re
import sys

# ---- WebGL key map: dataset camelCase -> clearcote GL constant name (the engine reads these) ----
# Explicit where the naive transform would be wrong (RenderBuffer->RENDERBUFFER, 3D, acronyms) or
# where the engine specifically consumes the value. WebGL2 keys carry a trailing "2" in the dataset
# (stripped before lookup); a handful use a "Webgl" suffix.
_WEBGL_EXPLICIT = {
    "maxTextureSize": "MAX_TEXTURE_SIZE",
    "maxCubeMapTextureSize": "MAX_CUBE_MAP_TEXTURE_SIZE",
    "maxRenderBufferSize": "MAX_RENDERBUFFER_SIZE",
    "maxVaryingVectors": "MAX_VARYING_VECTORS",
    "maxVertexUniformVectors": "MAX_VERTEX_UNIFORM_VECTORS",
    "maxFragmentUniformVectors": "MAX_FRAGMENT_UNIFORM_VECTORS",
    "maxCombinedTextureImageUnits": "MAX_COMBINED_TEXTURE_IMAGE_UNITS",
    "maxTextureImageUnits": "MAX_TEXTURE_IMAGE_UNITS",
    "maxVertexAttribs": "MAX_VERTEX_ATTRIBS",
    "maxVertexTextureImageUnits": "MAX_VERTEX_TEXTURE_IMAGE_UNITS",
    "maxViewportDims": "MAX_VIEWPORT_DIMS",
    "aliasedLineWidthRange": "ALIASED_LINE_WIDTH_RANGE",
    "aliasedPointSizeRange": "ALIASED_POINT_SIZE_RANGE",
    "alphaBits": "ALPHA_BITS", "blueBits": "BLUE_BITS", "depthBits": "DEPTH_BITS",
    "greenBits": "GREEN_BITS", "redBits": "RED_BITS", "stencilBits": "STENCIL_BITS",
    "subpixelBits": "SUBPIXEL_BITS", "sampleBuffers": "SAMPLE_BUFFERS", "samples": "SAMPLES",
    "maxColorAttachmentsWebgl": "MAX_COLOR_ATTACHMENTS",
    "maxDrawBuffersWebgl": "MAX_DRAW_BUFFERS",
    # WebGL2 (dataset key WITHOUT the trailing "2"):
    "max3DTextureSize": "MAX_3D_TEXTURE_SIZE",
    "maxArrayTextureLayers": "MAX_ARRAY_TEXTURE_LAYERS",
    "maxDrawBuffers": "MAX_DRAW_BUFFERS",
    "maxColorAttachments": "MAX_COLOR_ATTACHMENTS",
    "maxSamples": "MAX_SAMPLES",
    "maxVertexUniformBlocks": "MAX_VERTEX_UNIFORM_BLOCKS",
    "maxFragmentUniformBlocks": "MAX_FRAGMENT_UNIFORM_BLOCKS",
    "maxCombinedUniformBlocks": "MAX_COMBINED_UNIFORM_BLOCKS",
    "maxUniformBufferBindings": "MAX_UNIFORM_BUFFER_BINDINGS",
    "maxVertexUniformComponents": "MAX_VERTEX_UNIFORM_COMPONENTS",
    "maxFragmentUniformComponents": "MAX_FRAGMENT_UNIFORM_COMPONENTS",
    "maxTextureLodBias": "MAX_TEXTURE_LOD_BIAS",
    "maxElementIndex": "MAX_ELEMENT_INDEX",
    "max3dTextureSize": "MAX_3D_TEXTURE_SIZE",  # case guard
}


def _camel_to_screaming(name):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    s = re.sub(r"(?<=[A-Za-z])(?=[0-9])", "_", s)
    return s.upper()


def _to_gl_const(base):
    if base in _WEBGL_EXPLICIT:
        return _WEBGL_EXPLICIT[base]
    if base.endswith("Webgl"):
        base = base[:-5]
    return _camel_to_screaming(base)


def _num(v):
    """Coerce a numeric string to int/float; pass everything else through unchanged."""
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v
    return v


def _conv_value(v):
    """Dataset values: numeric strings -> numbers; {"0":a,"1":b} range objects -> [a, b]."""
    if isinstance(v, dict) and v and all(k.isdigit() for k in v):
        return [_num(v[str(i)]) for i in range(len(v))]
    return _num(v)


# ----------------------------- dataset loading -----------------------------

def find_dataset(explicit):
    """Return the directory holding fingerprints.json.xz + vars.py."""
    candidates = []
    if explicit:
        candidates += [explicit, os.path.join(explicit, "chrome_fingerprints")]
    spec = importlib.util.find_spec("chrome_fingerprints")
    if spec and spec.submodule_search_locations:
        candidates += list(spec.submodule_search_locations)
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "fingerprints.json.xz")):
            return c
    sys.exit(
        "Could not locate the chrome-fingerprints dataset.\n"
        "Install it (`pip install chrome-fingerprints`) or pass --dataset <dir> pointing at the\n"
        "chrome_fingerprints package directory (the one containing fingerprints.json.xz + vars.py)."
    )


def load_tables(pkg_dir):
    """Load vars.py directly (bypassing the package __init__, which needs orjson/dacite)."""
    spec = importlib.util.spec_from_file_location("_cf_vars", os.path.join(pkg_dir, "vars.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_records(pkg_dir):
    with lzma.open(os.path.join(pkg_dir, "fingerprints.json.xz"), "rb") as f:
        return json.loads(f.read())


# ----------------------------- per-section mappers -----------------------------

def map_navigator(nav, include_version):
    plat = nav.get("platform") or {}
    he = {
        "platform": plat.get("name"),
        "platformVersion": plat.get("version"),
        "architecture": plat.get("architecture"),
        "bitness": plat.get("bitness"),
        "model": plat.get("model", ""),
        "wow64": plat.get("wow64", False),
        "mobile": False,
    }
    out = {
        "app_codename": nav.get("app_codename"),
        "app_name": nav.get("app_name"),
        "app_version": nav.get("app_version"),
        "product": nav.get("product"),
        "product_sub": nav.get("product_sub"),
        "vendor": nav.get("vendor"),
        "vendor_sub": nav.get("vendor_sub"),
        "pdf_viewer_enabled": nav.get("pdf_viewer_enabled"),
        "uadata": {"platform": plat.get("name"), "mobile": False, "high_entropy": he},
    }
    if include_version:
        # Only coherent if the clearcote binary's major version matches the dataset's.
        he["uaFullVersion"] = nav.get("full_version")
        he["fullVersionList"] = nav.get("full_version_list")
        he["brands"] = nav.get("brands")
        out["uadata"]["brands"] = nav.get("brands")
    return out


def map_webgl(webgl, tables):
    exts = webgl.get("extensions") or []
    exts2 = webgl.get("extensions2") or []
    resolve_ext = lambda lst: [tables.webgl_extensions[i] if isinstance(i, int) else i for i in lst]
    w1_params, w2_params = {}, {}
    for key, val in (webgl.get("properties") or {}).items():
        cval = _conv_value(val)
        if key.endswith("2"):
            w2_params[_to_gl_const(key[:-1])] = cval
        else:
            w1_params[_to_gl_const(key)] = cval
    debug = {
        "VENDOR": webgl.get("vendor"),
        "RENDERER": webgl.get("renderer"),
        "VERSION": webgl.get("version"),
        "SHADING_LANGUAGE_VERSION": webgl.get("shading_language"),
        "UNMASKED_VENDOR_WEBGL": webgl.get("unmasked_vendor"),
        "UNMASKED_RENDERER_WEBGL": webgl.get("unmasked_renderer"),
    }
    debug2 = dict(debug)
    debug2["VERSION"] = webgl.get("version2")
    debug2["SHADING_LANGUAGE_VERSION"] = webgl.get("shading_language2")
    return {
        "webgl1": {"parameters": w1_params, "extensions": resolve_ext(exts), "debug": debug},
        "webgl2": {"parameters": w2_params, "extensions": resolve_ext(exts2), "debug": debug2},
    }


def map_speech(speech, tables):
    out = []
    for s in speech or []:
        idx = s.get("voice_uri")
        uri = tables.voice_uris[idx] if isinstance(idx, int) and idx < len(tables.voice_uris) else idx
        out.append({
            "voice_uri": uri, "name": uri, "lang": s.get("lang"),
            "local_service": s.get("local_service"), "default": s.get("default"),
        })
    return out


def resolve_fonts(fonts, tables):
    return [tables.fonts[i] if (isinstance(i, int) and i < len(tables.fonts)) else i for i in (fonts or [])]


def map_keyboard(kb, tables):
    out = {}
    for key, idx in (kb or {}).items():
        out[key] = tables.keyboard_codes[idx] if isinstance(idx, int) and idx < len(tables.keyboard_codes) else idx
    return out


def convert(rec, tables, include_version=False):
    return {
        "meta": {"schema_version": 1, "source": "chrome-fingerprints",
                 "captured_at": None, "chrome_version": None},
        "hardware_concurrency": rec.get("hardware_concurrency"),
        "device_memory": rec.get("device_memory"),
        "do_not_track": rec.get("do_not_track"),
        "navigator": map_navigator(rec.get("navigator") or {}, include_version),
        "screen": rec.get("screen") or {},   # dataset screen keys already match the clearcote schema
        "webgl": map_webgl(rec.get("webgl") or {}, tables),
        "audio": {k: _num(v) for k, v in (rec.get("audio") or {}).items()},
        "speech": map_speech(rec.get("speech"), tables),
        "fonts": {"detected": resolve_fonts(rec.get("fonts"), tables), "probed": None},
        "css": rec.get("css") or {},
        "keyboard": map_keyboard(rec.get("keyboard"), tables),
        "webgpu": rec.get("webgpu"),
        "webrtc": rec.get("webrtc"),
        "network": None,
    }


def main():
    ap = argparse.ArgumentParser(description="Convert chrome-fingerprints records to clearcote-profile JSON.")
    ap.add_argument("--dataset", help="Path to the chrome_fingerprints package dir (auto-detected if installed).")
    ap.add_argument("--out", default="./clearcote-profiles", help="Output directory (default ./clearcote-profiles).")
    ap.add_argument("--count", type=int, default=0, help="Convert the first N records (0 = all 10000).")
    ap.add_argument("--index", type=int, help="Convert a single record by index.")
    ap.add_argument("--stdout", action="store_true", help="With --index, print to stdout instead of writing a file.")
    ap.add_argument("--include-version", action="store_true",
                    help="Also import the dataset's Chrome version (mismatches the 149 binary; not recommended).")
    args = ap.parse_args()

    pkg = find_dataset(args.dataset)
    tables = load_tables(pkg)
    records = load_records(pkg)
    print(f"dataset: {pkg} ({len(records)} records)", file=sys.stderr)

    if args.index is not None:
        prof = convert(records[args.index], tables, args.include_version)
        text = json.dumps(prof, indent=1, ensure_ascii=False)
        if args.stdout:
            print(text)
        else:
            os.makedirs(args.out, exist_ok=True)
            path = os.path.join(args.out, f"profile-{args.index:05d}.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"wrote {path}", file=sys.stderr)
        return

    n = len(records) if args.count <= 0 else min(args.count, len(records))
    os.makedirs(args.out, exist_ok=True)
    for i in range(n):
        prof = convert(records[i], tables, args.include_version)
        with open(os.path.join(args.out, f"profile-{i:05d}.json"), "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)
    print(f"wrote {n} profiles to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
