#!/usr/bin/env python3
r"""
clearcote stealth-coherence gate
================================

Launches the built ``chrome.exe`` and asserts that the persona/farble layer is
*internally coherent* — i.e. that it does not betray itself in ways a strict
fingerprint-coherence check can see. Every assertion is **baseline-independent**
(it compares the browser against *itself* across contexts/origins, not against an
external "known good" value), so the gate needs no network, no reference corpus,
and no stock-Chrome install to run — which is what makes it a reliable CI gate
rather than a flaky one.

Why this exists
---------------
A real Chromium is *coherent*: text metrics land on a fixed sub-pixel grid, the
same string measures identically on the main thread and in a worker, layout-rect
APIs agree with each other, and a given machine renders the same bytes on every
site. A naive farble layer breaks one of those invariants and becomes trivially
detectable — *without* the detector knowing the "true" value. This gate locks in
those invariants so a regression (e.g. a farble that only hooks the window
context, leaving workers un-noised) **fails the build instead of shipping**.

The checks (all self-referential)
----------------------------------
  measuretext-grid     every measureText width is an exact multiple of 1/512 px
                       (dpr=1). A uniform sub-grid scale pushes them off-grid.
  worker-vs-main       measureText for the same string is identical on the main
                       thread and inside an OffscreenCanvas Worker. A farble that
                       only hooks the window context diverges here.
  bcr-vs-range         element.getBoundingClientRect().left equals
                       Range.getClientRects()[0].left for the same node, and both
                       sit on the 1/512 grid. Inconsistent rect farbling diverges.
  origin-invariant     canvas2d + WebGL readback hashes are identical across two
                       different registrable domains in the same session (a real
                       machine renders the same everywhere; per-domain-keyed noise
                       does not).
  webgl-webgpu-vendor-match
                       the WebGPU adapter vendor agrees with the WebGL
                       UNMASKED_VENDOR family (both name the same GPU vendor).

Expected-state contract
------------------------
Each check is either REQUIRED (must pass — the contract) or a KNOWN_GAP (a tell we
have not closed yet, tracked with its fix location). The gate **fails** (exit 1)
if any REQUIRED check fails (a regression) OR if any KNOWN_GAP check now *passes*
(it got fixed — promote it to REQUIRED so it can never silently regress again).
This way the documented gaps don't block today's releases, but the engine fixes
are enforced forward the moment they land.

USAGE
-----
  py -3 scripts/stealth_coherence.py                 # gate the default binary
  py -3 scripts/stealth_coherence.py --binary C:\clearcote\chrome.exe
  py -3 scripts/stealth_coherence.py --baseline      # also sanity-check stock Chrome passes
  py -3 scripts/stealth_coherence.py --headed --json out.json
  py -3 scripts/stealth_coherence.py --selftest      # validate the check logic, no binary needed

EXIT CODES
----------
  0  contract satisfied (all REQUIRED pass; no KNOWN_GAP unexpectedly fixed)
  1  contract violation (a regression, or a fixed gap awaiting promotion)
  2  missing dependency / binary not found
  3  the gate itself crashed
"""
import argparse
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# Two reserved-TLD origins with DIFFERENT registrable domains (a.test vs b.test).
# Served entirely from memory via Playwright request interception, so the gate is
# fully offline and deterministic. https scheme => secure context (WebGPU / UA-CH).
ORIGIN_A = "https://coherence-a.test/"
ORIGIN_B = "https://probe-b.test/"
PAGE_HTML = "<!doctype html><html><head><meta charset=utf-8><title>coherence</title></head><body></body></html>"

# --- the expected-state contract (see module docstring) ----------------------
# REQUIRED checks must pass; KNOWN_GAPS are tracked-but-tolerated tells. When a
# KNOWN_GAP starts passing, MOVE it into REQUIRED (the gate will tell you to).
REQUIRED = {
    "webgl-webgpu-vendor-match",
    # Promoted 2026-06-30 after the engine fix landed and this gate verified the flip.
    # Enforced forward now: a regression fails the build.
    "measuretext-grid",   # measureText metrics truthful/on-grid (patch 060: noise factor 0)
    "worker-vs-main",     # main thread == OffscreenCanvas worker (patch 060)
    "bcr-vs-range",       # getBoundingClientRect == Range rects, on-grid (patch 050: clientRects offset 0)
}
KNOWN_GAPS = {
    "origin-invariant": "canvas/WebGL readback is keyed by registrable domain, so one identity renders differently per site. Left domain-keyed by design (2026-06-30): per-site noise-unlinkability is retained. Flipping to persona-keyed (patch 001) is only warranted if the usage model becomes one-profile-per-identity.",
}

STRS = ["A", "WWWWWWWWWW", "mmmmmmmmmm", "iiiiiiiiii", "Coherence 0123"]

PROBE_JS = r"""async () => {
  const H = (s) => { let h=5381; for (let i=0;i<s.length;i++) h=((h<<5)+h+s.charCodeAt(i))>>>0; return h>>>0; };
  const Hb = (u8) => { let h=2166136261>>>0; for (let i=0;i<u8.length;i++){ h^=u8[i]; h=Math.imul(h,16777619)>>>0; } return h>>>0; };
  const STRS = __STRS__;
  const out = { origin: location.origin, secureContext: window.isSecureContext };

  const mctx = document.createElement('canvas').getContext('2d'); mctx.font = '12px Arial';
  out.measureMain = {}; for (const s of STRS) out.measureMain[s] = mctx.measureText(s).width;

  out.measureWorker = await (async () => {
    try {
      const code = "self.onmessage=function(){try{var c=new OffscreenCanvas(64,32);var x=c.getContext('2d');x.font='12px Arial';var S=" + JSON.stringify(STRS) + ";var r={};for(var i=0;i<S.length;i++){r[S[i]]=x.measureText(S[i]).width;}self.postMessage({ok:r});}catch(e){self.postMessage({err:''+e});}};";
      const w = new Worker(URL.createObjectURL(new Blob([code], {type:'application/javascript'})));
      const res = await new Promise((rs, rj) => { const t=setTimeout(()=>rj(new Error('timeout')),5000); w.onmessage=e=>{clearTimeout(t);rs(e.data);}; w.postMessage(0); });
      w.terminate();
      return res.err ? ('ERR:'+res.err) : res.ok;
    } catch (e) { return 'ERR:'+e; }
  })();

  out.canvasHash = (() => {
    const c=document.createElement('canvas'); c.width=200; c.height=60; const x=c.getContext('2d');
    x.textBaseline='top'; x.font='16px Arial'; x.fillStyle='#069'; x.fillText('coherence gate',8,8);
    x.fillStyle='rgba(120,200,40,0.7)'; x.fillText('coherence gate',10,18);
    x.beginPath(); x.arc(150,30,18,0,Math.PI*1.6); x.strokeStyle='#a30'; x.stroke();
    return H(c.toDataURL());
  })();

  out.webglRenderHash = (() => {
    try {
      const c=document.createElement('canvas'); c.width=96; c.height=96;
      const gl=c.getContext('webgl',{preserveDrawingBuffer:true,antialias:false}); if(!gl) return 'no-webgl';
      const vs=gl.createShader(gl.VERTEX_SHADER); gl.shaderSource(vs,'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}'); gl.compileShader(vs);
      const fs=gl.createShader(gl.FRAGMENT_SHADER); gl.shaderSource(fs,'precision highp float;void main(){float x=gl_FragCoord.x,y=gl_FragCoord.y;float v=sin(x*0.137)*cos(y*0.071)+pow(abs(sin(x*y*0.0001)),1.3);gl_FragColor=vec4(fract(v*43758.5453),fract(sin(x*12.9+y*78.2)*43758.5),fract(cos(x*0.017+y*0.015)*24634.6),1.0);}'); gl.compileShader(fs);
      const pr=gl.createProgram(); gl.attachShader(pr,vs); gl.attachShader(pr,fs); gl.linkProgram(pr); gl.useProgram(pr);
      const b=gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER,b); gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
      const loc=gl.getAttribLocation(pr,'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
      gl.viewport(0,0,96,96); gl.drawArrays(gl.TRIANGLES,0,3);
      const px=new Uint8Array(96*96*4); gl.readPixels(0,0,96,96,gl.RGBA,gl.UNSIGNED_BYTE,px); return Hb(px);
    } catch (e) { return 'ERR:'+e; }
  })();

  out.rects = (() => {
    const d=document.createElement('div'); d.style.cssText='position:absolute;top:10px;left:10px;font:16px Arial;white-space:nowrap'; d.textContent='WWWWWiiiii';
    document.body.appendChild(d); const rng=document.createRange(); rng.selectNodeContents(d);
    const cr=rng.getClientRects()[0]; const bcr=d.getBoundingClientRect();
    const r={ bcrLeft:bcr.left, rangeLeft:cr?cr.left:null, bcrWidth:bcr.width, rangeWidth:cr?cr.width:null }; d.remove(); return r;
  })();

  out.webgl = (() => { try { const gl=document.createElement('canvas').getContext('webgl'); const x=gl.getExtension('WEBGL_debug_renderer_info');
    return { vendor: gl.getParameter(x.UNMASKED_VENDOR_WEBGL), renderer: gl.getParameter(x.UNMASKED_RENDERER_WEBGL) }; } catch(e){ return 'ERR:'+e; } })();
  out.webgpu = await (async () => { try { if(!navigator.gpu) return 'no-webgpu'; const a=await navigator.gpu.requestAdapter(); if(!a) return 'no-adapter';
    const info=a.info||(a.requestAdapterInfo?await a.requestAdapterInfo():{}); return { vendor:info.vendor, architecture:info.architecture, features:Array.from(a.features||[]).sort() }; } catch(e){ return 'ERR:'+e; } })();

  return out;
}""".replace("__STRS__", json.dumps(STRS))


# --- pure check logic (unit-testable without a browser) ----------------------

def on_grid(w, n=512, tol=1e-6):
    """True if w is (within tol) an exact multiple of 1/n px — Chrome's dyadic grid."""
    return isinstance(w, (int, float)) and abs(w * n - round(w * n)) < tol


def evaluate(a, b):
    """Run every coherence check over origin-A probe `a` and origin-B probe `b`.

    Returns a list of (check_id, passed, detail) tuples. Each check is self-referential
    (no external baseline). `b` is only needed for the cross-origin check.
    """
    checks = []

    # measuretext-grid: all main-thread widths on the 1/512 grid
    mm = a.get("measureMain", {})
    off = {s: w for s, w in mm.items() if not on_grid(w)}
    checks.append(("measuretext-grid", len(off) == 0,
                   "all on 1/512 grid" if not off else "off-grid: " + ", ".join(f"{s}={mm[s]}" for s in off)))

    # worker-vs-main: identical measureText in window vs worker
    mw = a.get("measureWorker")
    if not isinstance(mw, dict):
        checks.append(("worker-vs-main", False, f"worker probe failed: {mw}"))
    else:
        diff = {s: (mm.get(s), mw.get(s)) for s in mm if abs((mm.get(s) or 0) - (mw.get(s) or 0)) > 1e-9}
        checks.append(("worker-vs-main", len(diff) == 0,
                       "main == worker" if not diff else "diverge: " + ", ".join(f"{s} {v[0]}!={v[1]}" for s, v in diff.items())))

    # bcr-vs-range: BCR.left == Range.left and both on-grid
    r = a.get("rects", {})
    bl, rl = r.get("bcrLeft"), r.get("rangeLeft")
    if bl is None or rl is None:
        checks.append(("bcr-vs-range", False, f"rect probe incomplete: {r}"))
    else:
        agree = abs(bl - rl) < 1e-6
        grid = on_grid(bl) and on_grid(rl)
        checks.append(("bcr-vs-range", agree and grid,
                       "BCR == Range, on-grid" if (agree and grid)
                       else f"bcrLeft={bl} rangeLeft={rl} agree={agree} on_grid={grid}"))

    # origin-invariant: same canvas + webgl render bytes across two registrable domains
    ca, cb = a.get("canvasHash"), b.get("canvasHash")
    ga, gb = a.get("webglRenderHash"), b.get("webglRenderHash")
    canvas_inv = ca == cb
    webgl_inv = ga == gb
    checks.append(("origin-invariant", canvas_inv and webgl_inv,
                   "canvas + webgl identical across origins"
                   if (canvas_inv and webgl_inv)
                   else f"canvas {ca}{'==' if canvas_inv else '!='}{cb}; webgl {ga}{'==' if webgl_inv else '!='}{gb}"))

    # webgl-webgpu-vendor-match: WebGPU vendor family agrees with WebGL UNMASKED_VENDOR
    wg = a.get("webgpu")
    wl = a.get("webgl")
    if not isinstance(wg, dict) or not isinstance(wl, dict):
        checks.append(("webgl-webgpu-vendor-match", False, f"webgpu={wg} webgl={wl}"))
    else:
        gpu_vendor = str(wg.get("vendor", "")).lower()
        gl_vendor = str(wl.get("vendor", "")).lower()
        match = bool(gpu_vendor) and gpu_vendor in gl_vendor
        checks.append(("webgl-webgpu-vendor-match", match,
                       f"webgpu '{wg.get('vendor')}' coheres with webgl '{wl.get('vendor')}'" if match
                       else f"MISMATCH webgpu '{wg.get('vendor')}' vs webgl '{wl.get('vendor')}'"))

    return checks


def verdict(checks):
    """Apply the expected-state contract. Returns (ok, rows) where rows is a list of
    (check_id, status_symbol, detail) and ok=False on any contract violation."""
    ok = True
    rows = []
    for cid, passed, detail in checks:
        if cid in REQUIRED:
            if passed:
                rows.append((cid, "PASS", detail))
            else:
                ok = False
                rows.append((cid, "FAIL", f"REQUIRED check failed (regression): {detail}"))
        elif cid in KNOWN_GAPS:
            if passed:
                ok = False  # fixed! must be promoted so it can't silently regress
                rows.append((cid, "FIXED", f"now PASSES — promote to REQUIRED in stealth_coherence.py. ({detail})"))
            else:
                rows.append((cid, "KNOWN", f"known gap: {KNOWN_GAPS[cid]}"))
        else:
            # an unrecognized check defaults to required (fail-closed)
            if passed:
                rows.append((cid, "PASS", detail))
            else:
                ok = False
                rows.append((cid, "FAIL", f"untracked check failed: {detail}"))
    return ok, rows


# --- browser driving ---------------------------------------------------------

def _default_binary():
    for env in ("CLEARCOTE_BINARY", "CLEARCOAT_BINARY"):
        if os.environ.get(env):
            return os.environ[env]
    for c in [os.path.join(HERE, "win-x64", "chrome.exe"),
              os.path.join(HERE, "..", "win-x64", "chrome.exe"),
              os.path.join(os.getcwd(), "win-x64", "chrome.exe"),
              r"C:\clearcote\chrome.exe"]:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.join(HERE, "win-x64", "chrome.exe")


def norm_seed(seed):
    s = str(seed)
    if s.lstrip("-").isdigit():
        return s
    return str(int(hashlib.sha256(s.encode()).hexdigest(), 16) % (2 ** 31))


def _probe_origin(context, url):
    page = context.new_page()
    # Serve the (empty) page for the made-up https origin entirely from memory.
    page.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=PAGE_HTML))
    page.set_default_timeout(30000)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(200)
    data = page.evaluate(PROBE_JS)
    page.close()
    return data


def run_gate(binary, seed, platform, headed, baseline):
    from playwright.sync_api import sync_playwright

    cc_args = [
        f"--fingerprint={norm_seed(seed)}",
        f"--fingerprint-platform={platform}",
        "--fingerprint-brand=chrome",
        "--no-sandbox", "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    out = {"binary": binary, "seed": str(seed), "platform": platform, "stamp": time.strftime("%Y%m%d_%H%M%S")}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=binary, headless=not headed, args=cc_args, timeout=60000)
        try:
            ctx = browser.new_context()
            a = _probe_origin(ctx, ORIGIN_A)
            b = _probe_origin(ctx, ORIGIN_B)
            ctx.close()
        finally:
            browser.close()
    out["origin_a"], out["origin_b"] = a, b
    out["checks"] = [(cid, passed, detail) for cid, passed, detail in evaluate(a, b)]

    if baseline:
        # Sanity: stock Chrome must PASS every check — proves the gate's notion of
        # "coherent" matches a real browser (and that a failure is clearcote's, not ours).
        with sync_playwright() as pw:
            br = pw.chromium.launch(channel="chrome", headless=not headed,
                                    args=["--no-first-run", "--no-default-browser-check"], timeout=60000)
            try:
                ctx = br.new_context()
                ba = _probe_origin(ctx, ORIGIN_A)
                bb = _probe_origin(ctx, ORIGIN_B)
                ctx.close()
            finally:
                br.close()
        out["baseline_checks"] = [(cid, passed, detail) for cid, passed, detail in evaluate(ba, bb)]
    return out


SYM = {"PASS": "[PASS] ", "FAIL": "[FAIL] ", "KNOWN": "[gap]  ", "FIXED": "[FIXED]"}


def print_report(out):
    print("=" * 72)
    print("clearcote stealth-coherence gate")
    print("binary :", out["binary"])
    print("seed   :", out["seed"], "| platform:", out["platform"])
    print("=" * 72)
    ok, rows = verdict(out["checks"])
    width = max(len(cid) for cid, *_ in rows)
    for cid, status, detail in rows:
        print(f"  {SYM.get(status, status):8}{cid.ljust(width)}  {detail}")
    if "baseline_checks" in out:
        bok, _ = verdict(out["baseline_checks"])
        b_fail = [cid for cid, passed, _ in out["baseline_checks"] if not passed]
        print("-" * 72)
        if b_fail:
            print(f"  ! baseline (stock Chrome) FAILED: {b_fail} — the gate's reference logic or"
                  f" environment is suspect; investigate before trusting clearcote results.")
        else:
            print("  baseline (stock Chrome): all checks pass (gate logic validated against real Chrome)")
    print("-" * 72)
    print("RESULT:", "OK — contract satisfied" if ok else "CONTRACT VIOLATION (see FAIL / FIXED rows)")
    return ok


# --- selftest (no binary) ----------------------------------------------------

def selftest():
    """Validate the pure check logic against synthetic fixtures."""
    fails = []

    if not on_grid(8.00390625):
        fails.append("on_grid: 8.00390625 (=4098/512) should be on-grid")
    if on_grid(8.003883495877192):
        fails.append("on_grid: 8.003883... should be off-grid")

    good = {
        "measureMain": {s: round(10 * 512) / 512 for s in STRS},  # all on-grid
        "measureWorker": {s: round(10 * 512) / 512 for s in STRS},
        "rects": {"bcrLeft": 10.0, "rangeLeft": 10.0, "bcrWidth": 50.0, "rangeWidth": 50.0},
        "canvasHash": 111, "webglRenderHash": 222,
        "webgl": {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, ...)"},
        "webgpu": {"vendor": "intel", "architecture": "gen-12-lp", "features": []},
    }
    good_b = dict(good)  # same hashes => origin-invariant
    res = {cid: passed for cid, passed, _ in evaluate(good, good_b)}
    for cid in ("measuretext-grid", "worker-vs-main", "bcr-vs-range", "origin-invariant", "webgl-webgpu-vendor-match"):
        if not res.get(cid):
            fails.append(f"coherent fixture should pass {cid}, got {res.get(cid)}")

    bad = json.loads(json.dumps(good))
    bad["measureMain"]["A"] = 8.003883495877192            # off-grid
    bad["measureWorker"]["A"] = 8.00390625                 # worker disagrees
    bad["rects"]["rangeLeft"] = 9.999177932739258          # bcr != range
    bad_b = json.loads(json.dumps(good)); bad_b["canvasHash"] = 999  # per-origin divergence
    bad["webgpu"]["vendor"] = "nvidia"                     # vendor mismatch vs Intel webgl
    res2 = {cid: passed for cid, passed, _ in evaluate(bad, bad_b)}
    for cid in ("measuretext-grid", "worker-vs-main", "bcr-vs-range", "origin-invariant", "webgl-webgpu-vendor-match"):
        if res2.get(cid):
            fails.append(f"incoherent fixture should FAIL {cid}, but it passed")

    # contract: coherent fixture => the known gaps "pass" => verdict must FLAG them (not ok)
    ok_all_pass, _ = verdict([(cid, True, "") for cid in (REQUIRED | set(KNOWN_GAPS))])
    if ok_all_pass:
        fails.append("verdict: all-pass should be a violation (known gaps need promotion)")
    ok_today, _ = verdict([(cid, cid in REQUIRED, "") for cid in (REQUIRED | set(KNOWN_GAPS))])
    if not ok_today:
        fails.append("verdict: REQUIRED-pass + KNOWN_GAPS-fail should satisfy the contract")

    if fails:
        print("SELFTEST FAILED:")
        for f in fails:
            print("  -", f)
        return False
    print(f"SELFTEST OK ({len(STRS)} strings; {len(REQUIRED)} required, {len(KNOWN_GAPS)} known-gap checks)")
    return True


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="clearcote stealth-coherence gate")
    ap.add_argument("--binary", default=_default_binary())
    ap.add_argument("--seed", default="coherence-gate")
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--baseline", action="store_true", help="also run stock Chrome (channel=chrome) and assert it passes every check")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--json", default=None, help="write full results JSON to this path")
    ap.add_argument("--selftest", action="store_true", help="validate the check logic without launching a binary")
    a = ap.parse_args()

    if a.selftest:
        sys.exit(0 if selftest() else 1)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[X] dependency missing:  py -3 -m pip install playwright")
        sys.exit(2)
    if not os.path.exists(a.binary):
        print(f"[X] chrome.exe not found: {a.binary}  (set CLEARCOTE_BINARY or pass --binary)")
        sys.exit(2)

    try:
        out = run_gate(a.binary, a.seed, a.platform, a.headed, a.baseline)
    except Exception as e:  # noqa: BLE001
        import traceback
        print("\n[X] gate crashed:")
        traceback.print_exc()
        print(f"    ({type(e).__name__}: {e})")
        sys.exit(3)

    ok = print_report(out)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, default=str)
        print("written ->", a.json)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
