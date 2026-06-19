#!/usr/bin/env python3
"""Verify that clearcote actually loads a fingerprint profile — that the surfaces a page reads
match what the profile says they should be.

Launches the clearcote binary with ``--fingerprint-profile`` (gzip+base64-encoding the profile
exactly as the SDK does), probes the fingerprint surfaces in-page, and compares each to the
profile. Prints a PASS/FAIL table and exits non-zero if anything important diverges.

    pip install playwright          # (one-time) python -m playwright install is NOT needed; we use the clearcote binary
    python verify_profile.py --executable /path/to/clearcote/chrome.exe profile.json

Use it on a profile you captured (collect.html) or one from the curated library
(https://github.com/clearcotelabs/clearcote-profiles).
"""
import argparse
import base64
import gzip
import json
import sys

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    raise SystemExit("Needs Playwright:  pip install playwright")

PROBE = r"""() => {
  const out = {};
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory;
  out.screen = { width: screen.width, height: screen.height,
                 colorDepth: screen.colorDepth, dpr: window.devicePixelRatio };
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const dbg = gl.getExtension('WEBGL_debug_renderer_info');
    out.glVendor = gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL);
    out.glRenderer = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
    out.maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
    out.redBits = gl.getParameter(gl.RED_BITS);
    out.extCount = (gl.getSupportedExtensions() || []).length;
  } catch (e) { out.glError = String(e); }
  try { out.audioSampleRate = new (window.AudioContext || window.webkitAudioContext)().sampleRate; } catch (e) {}
  out.voiceCount = (window.speechSynthesis ? speechSynthesis.getVoices().length : 0);
  out.colorGamut = ['rec2020', 'p3', 'srgb'].find(g => matchMedia('(color-gamut: ' + g + ')').matches) || null;
  out.fontProbe = {};
  return out;
}"""


def expected(profile):
    """Derive the values a correctly-loaded profile should produce."""
    w1 = (profile.get("webgl") or {}).get("webgl1") or {}
    params = w1.get("parameters") or {}
    debug = w1.get("debug") or {}
    screen = profile.get("screen") or {}
    audio = profile.get("audio") or {}
    speech = profile.get("speech") or []
    dm = profile.get("device_memory")
    css = profile.get("css") or {}
    gamut = next((g for g in ("rec2020", "p3", "srgb") if css.get("color-gamut:" + g)), None) \
        or (css.get("color-gamut") if css.get("color-gamut") in ("srgb", "p3", "rec2020") else None)
    exp = {
        "hardwareConcurrency": profile.get("hardware_concurrency"),
        # navigator.deviceMemory is spec-clamped to a max of 8
        "deviceMemory": (min(dm, 8) if isinstance(dm, (int, float)) else None),
        "screen.width": screen.get("width"),
        "screen.height": screen.get("height"),
        "glRenderer": debug.get("UNMASKED_RENDERER_WEBGL"),
        "glVendor": debug.get("UNMASKED_VENDOR_WEBGL"),
        "maxTextureSize": params.get("MAX_TEXTURE_SIZE"),
        "redBits": params.get("RED_BITS"),
        "audioSampleRate": audio.get("BaseAudioContextSampleRate"),
        # an empty profile voice list falls back to clearcote's default Windows trio (3)
        "voiceCount": (len(speech) if speech else 3),
        "colorGamut": gamut,
    }
    return {k: v for k, v in exp.items() if v is not None}


def actual(probe):
    return {
        "hardwareConcurrency": probe.get("hardwareConcurrency"),
        "deviceMemory": probe.get("deviceMemory"),
        "screen.width": (probe.get("screen") or {}).get("width"),
        "screen.height": (probe.get("screen") or {}).get("height"),
        "glRenderer": probe.get("glRenderer"),
        "glVendor": probe.get("glVendor"),
        "maxTextureSize": probe.get("maxTextureSize"),
        "redBits": probe.get("redBits"),
        "audioSampleRate": probe.get("audioSampleRate"),
        "voiceCount": probe.get("voiceCount"),
        "colorGamut": probe.get("colorGamut"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("profile", help="path to a clearcote-profile JSON")
    ap.add_argument("--executable", required=True, help="path to the clearcote chrome(.exe) binary")
    ap.add_argument("--fingerprint", default="verify-seed", help="--fingerprint seed (fallback for absent fields)")
    ap.add_argument("--url", default="https://example.com", help="page to probe on")
    ap.add_argument("--show-fonts", type=int, default=10, help="how many of the profile's fonts to probe")
    args = ap.parse_args()

    with open(args.profile, encoding="utf-8") as fh:
        profile = json.load(fh)
    b64 = base64.b64encode(gzip.compress(json.dumps(profile).encode("utf-8"), 9)).decode("ascii")
    fonts = ((profile.get("fonts") or {}).get("detected") or [])[:args.show_fonts]

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=args.executable, headless=False,
            args=["--fingerprint=" + args.fingerprint, "--fingerprint-profile=" + b64,
                  "--no-first-run", "--no-default-browser-check", "--no-sandbox"],
            ignore_default_args=["--enable-automation"], timeout=60000)
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(400)  # let async voice list populate
        probe = page.evaluate(PROBE)
        font_hits = page.evaluate(
            """(fonts) => { const s = document.createElement('span');
              s.style.cssText='position:absolute;left:-9999px;font-size:72px'; s.textContent='mmmwwwlli';
              document.body.appendChild(s); const B=['monospace','serif','sans-serif'], base={};
              for (const b of B){ s.style.fontFamily=b; base[b]=[s.offsetWidth,s.offsetHeight]; }
              const hit=[]; for (const f of fonts){ for (const b of B){ s.style.fontFamily="'"+f+"',"+b;
                if (s.offsetWidth!==base[b][0]||s.offsetHeight!==base[b][1]){hit.push(f);break;} } }
              s.remove(); return hit; }""", fonts)
        browser.close()

    exp, act = expected(profile), actual(probe)
    print("profile: %s" % args.profile)
    print("renderer: %s\n" % (probe.get("glRenderer") or probe.get("glError")))
    print("  %-22s %-30s %-30s %s" % ("surface", "expected", "actual", ""))
    print("  " + "-" * 84)
    failed = 0
    for key in exp:
        e, a = exp[key], act.get(key)
        ok = (e == a)
        if not ok:
            failed += 1
        print("  %-22s %-30s %-30s %s" % (key, str(e)[:30], str(a)[:30], "PASS" if ok else "FAIL"))
    # fonts are informational: present iff in the profile AND installed on this machine
    if fonts:
        print("\n  fonts: %d/%d probed profile fonts detected present (%s)" %
              (len(font_hits), len(fonts), ", ".join(font_hits[:8]) or "none — none of the sample are installed locally"))
    print("\n%s" % ("VERIFIED: clearcote is loading the profile." if not failed
                    else "MISMATCH: %d surface(s) did not match the profile." % failed))
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
