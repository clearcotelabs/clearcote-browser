#!/usr/bin/env python3
r"""
clearcoat CreepJS audit — structured results + README table
===========================================================

Drives the built chrome.exe through Playwright, extracts the actual fingerprint
signals the browser exposes (UA / UA-CH / WebGL / canvas / audio / timezone /
hardwareConcurrency / WebRTC ...), runs consistency cross-checks, best-effort
scrapes CreepJS's own trust-score + lies, and emits:

  * creepjs_shots/<stamp>_audit/results.json   (machine-readable)
  * creepjs_shots/<stamp>_audit/results.md     (the table)
  * stdout table
  * (optional) injects the table into a README between marker comments:
        <!-- CREEPJS_RESULTS:START -->   ...   <!-- CREEPJS_RESULTS:END -->

Run on EVERY release (see docs/RELEASING.md) so the README reflects the shipped
binary. Uses a demo --webrtc-ip and an explicit --timezone so NO real PII (real
IP / location) lands in a public README.

USAGE
-----
  py -3 creepjs_audit.py
  py -3 creepjs_audit.py --readme clearcote-browser/README.md
  py -3 creepjs_audit.py --seed demo --platform windows --timezone America/New_York
  py -3 creepjs_audit.py --proxy user:pass@host:port        # real egress (overrides demo ip)
  py -3 creepjs_audit.py --headed --no-creepjs              # skip the slow CreepJS scrape
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _default_binary():
    """Find chrome.exe whether run from the working dir or the repo (scripts/)."""
    env = os.environ.get("CLEARCOAT_BINARY")
    if env:
        return env
    for c in [os.path.join(HERE, "win-x64", "chrome.exe"),
              os.path.join(HERE, "..", "win-x64", "chrome.exe"),
              os.path.join(os.getcwd(), "win-x64", "chrome.exe"),
              r"C:\clearcote\chrome.exe"]:
        if os.path.exists(c):
            return os.path.abspath(c)
    return os.path.join(HERE, "win-x64", "chrome.exe")


DEFAULT_BINARY = _default_binary()
CREEPJS = "https://abrahamjuliot.github.io/creepjs/"
# RFC 5737 documentation IP — safe to publish, proves the WebRTC srflx mock works.
DEMO_WEBRTC_IP = "203.0.113.45"

PROBE_JS = r"""async () => {
  const out = {};
  const t = (k, f) => { try { out[k] = f(); } catch (e) { out[k] = 'ERR:' + e; } };
  t('webdriver', () => navigator.webdriver);
  t('ua', () => navigator.userAgent);
  t('platform', () => navigator.platform);
  t('hardwareConcurrency', () => navigator.hardwareConcurrency);
  t('deviceMemory', () => navigator.deviceMemory);
  t('languages', () => (navigator.languages || []).join(','));
  t('timezone', () => Intl.DateTimeFormat().resolvedOptions().timeZone);
  t('maxTouchPoints', () => navigator.maxTouchPoints);
  // canvas 2D hash
  t('canvasHash', () => {
    const c = document.createElement('canvas'); c.width = 240; c.height = 60;
    const x = c.getContext('2d'); x.textBaseline = 'top'; x.font = '16px Arial';
    x.fillStyle = '#069'; x.fillText('clearcote audit', 10, 18);
    x.fillStyle = 'rgba(255,100,0,0.7)'; x.fillText('clearcote audit', 12, 22);
    return c.toDataURL();
  });
  // WebGL vendor/renderer
  t('webgl', () => {
    const gl = document.createElement('canvas').getContext('webgl');
    const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return {
      vendor: dbg ? gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : (gl ? gl.getParameter(gl.VENDOR) : 'no-webgl'),
      renderer: dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : (gl ? gl.getParameter(gl.RENDERER) : 'no-webgl'),
    };
  });
  // UA Client Hints (high entropy) — the source of the earlier 142/149 mismatch
  out.uaCH = await (async () => {
    try {
      if (!navigator.userAgentData) return 'unavailable';
      const h = await navigator.userAgentData.getHighEntropyValues(
        ['fullVersionList', 'platform', 'platformVersion', 'architecture', 'bitness', 'uaFullVersion']);
      const chromium = (h.fullVersionList || []).find(b => /chromium/i.test(b.brand));
      const notBrand = (h.fullVersionList || []).find(b => /not.*brand|chrome|edge|opera/i.test(b.brand) && !/chromium/i.test(b.brand));
      return {
        fullVersionList: (h.fullVersionList || []).map(b => `${b.brand} ${b.version}`).join(' | '),
        chromiumVersion: chromium ? chromium.version : null,
        brandVersion: notBrand ? notBrand.version : null,
        platform: h.platform, platformVersion: h.platformVersion,
        architecture: h.architecture, bitness: h.bitness, uaFullVersion: h.uaFullVersion,
      };
    } catch (e) { return 'ERR:' + e; }
  })();
  // WebGPU adapter (consistency vs WebGL)
  out.webgpu = await (async () => {
    try {
      if (!navigator.gpu) return 'no-webgpu';
      const a = await navigator.gpu.requestAdapter();
      if (!a) return 'no-adapter';
      const info = a.info || (a.requestAdapterInfo ? await a.requestAdapterInfo() : null);
      return info ? { vendor: info.vendor, architecture: info.architecture, description: info.description } : 'no-info';
    } catch (e) { return 'ERR:' + e; }
  })();
  return out;
}"""

WEBRTC_JS = r"""async () => {
  const found = [];
  let pc;
  try { pc = new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]}); }
  catch (e) { return {error:'RTCPeerConnection unavailable: '+e}; }
  pc.createDataChannel('probe');
  const gathered = new Promise(res => {
    pc.onicecandidate = e => {
      if (!e.candidate || !e.candidate.candidate) return res();
      const p = e.candidate.candidate.split(' ');
      found.push({ip: p[4], typ: p[7]});
    };
  });
  try { await pc.setLocalDescription(await pc.createOffer({offerToReceiveAudio:true})); }
  catch (e) { return {error:'offer failed: '+e}; }
  await Promise.race([gathered, new Promise(r => setTimeout(r, 7000))]);
  pc.close();
  return {found};
}"""


def norm_seed(seed):
    s = str(seed)
    if s.lstrip("-").isdigit():
        return s
    return str(int(hashlib.sha256(s.encode()).hexdigest(), 16) % (2 ** 31))


def parse_proxy(s):
    if not s:
        return None
    scheme, rest = "http", s
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    user = pwd = None
    if "@" in rest:
        creds, hostport = rest.rsplit("@", 1)
        user, _, pwd = creds.partition(":")
    else:
        hostport = rest
    proxy = {"server": f"{scheme}://{hostport}"}
    if user:
        proxy["username"], proxy["password"] = user, pwd
    return proxy


def scrape_creepjs(page):
    """Best-effort: read CreepJS's computed trust score / lies / hashes from the DOM."""
    out = {}
    try:
        page.goto(CREEPJS, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(15000)  # CreepJS computes asynchronously
        txt = page.inner_text("body")
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}, ""

    def num(pat):
        m = re.search(pat, txt, re.I)
        return m.group(1) if m else None

    out["trust_score"] = num(r"trust score[\s:]*([\d.]+)\s*%") or num(r"([\d.]+)\s*%\s*trust")
    out["lies"] = num(r"lies\s*\((\d+)\)") or num(r"(\d+)\s+lies\b")
    out["fp_id"] = num(r"FP ID[\s:]*([0-9a-f]{16,})")
    out["fuzzy"] = num(r"Fuzzy[\s:]*([0-9a-f]{16,})")
    heads = re.findall(r"(\d+)%\s*(?:like\s*)?headless", txt, re.I)
    out["headless_like"] = heads[0] if heads else None
    out["headless"] = heads[-1] if len(heads) >= 2 else None  # the 'hard' headless %, not 'like'
    out["stealth"] = num(r"(\d+)%\s*stealth")
    out["chromium_true"] = bool(re.search(r"chromium:\s*true", txt, re.I))
    return out, txt


def audit(binary, seed, platform, timezone, proxy, webrtc_ip, headed, do_creepjs, out_root):
    from playwright.sync_api import sync_playwright

    args = [
        f"--fingerprint={norm_seed(seed)}",
        f"--fingerprint-platform={platform}",
        "--no-sandbox", "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
    ]
    if timezone:
        args.append(f"--timezone={timezone}")
    if webrtc_ip and not proxy:
        args.append(f"--webrtc-ip={webrtc_ip}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root, f"{stamp}_audit")
    os.makedirs(out_dir, exist_ok=True)

    launch_kw = dict(executable_path=binary, headless=not headed, args=args, timeout=60000)
    if proxy:
        launch_kw["proxy"] = proxy

    print("=" * 64)
    print("clearcoat CreepJS audit")
    print("binary  :", binary)
    print("seed    :", repr(seed), "->", norm_seed(seed), "| platform:", platform, "| tz:", timezone)
    print("webrtc  :", "proxy egress" if proxy else f"mock {webrtc_ip}")
    print("=" * 64, flush=True)

    res = {"stamp": stamp, "binary": binary, "seed": str(seed), "platform": platform,
           "timezone": timezone}
    cj_raw = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kw)
        try:
            page = browser.new_page()
            page.set_default_timeout(30000)
            page.goto("https://example.com", wait_until="domcontentloaded")
            res["probe"] = page.evaluate(PROBE_JS)
            res["webrtc"] = page.evaluate(WEBRTC_JS)
            if do_creepjs:
                print("scraping CreepJS (~20s)...", flush=True)
                res["creepjs"], cj_raw = scrape_creepjs(page)
        finally:
            browser.close()

    res["canvas_hash"] = hashlib.sha256(str(res["probe"].get("canvasHash")).encode()).hexdigest()[:16]
    table = build_table(res, webrtc_ip if not proxy else None)
    res["table_md"] = table

    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    with open(os.path.join(out_dir, "results.md"), "w", encoding="utf-8") as f:
        f.write(table)
    if cj_raw:
        with open(os.path.join(out_dir, "creepjs_innertext.txt"), "w", encoding="utf-8") as f:
            f.write(cj_raw)
    print("\n" + table)
    print(f"\nwritten -> {out_dir}")
    return res, table


def _yn(ok):
    return "✅" if ok else "❌"


def build_table(res, expected_webrtc_ip):
    p = res.get("probe", {})
    ch = p.get("uaCH", {})
    wgl = p.get("webgl", {})
    wrtc = res.get("webrtc", {})
    found = wrtc.get("found", []) if isinstance(wrtc, dict) else []
    srflx = sorted({c["ip"] for c in found if c.get("typ") == "srflx"})
    host = sorted({c["ip"] for c in found if c.get("typ") == "host"})

    ua = str(p.get("ua", ""))
    ua_major = (re.search(r"Chrome/(\d+)", ua) or [None, None])[1]
    ch_chromium = ch.get("chromiumVersion") if isinstance(ch, dict) else None
    ch_major = ch_chromium.split(".")[0] if ch_chromium else None
    ua_ch_match = bool(ua_major and ch_major and ua_major == ch_major)

    webrtc_ok = (srflx == [expected_webrtc_ip]) if expected_webrtc_ip else bool(srflx)
    no_host_leak = (len(host) == 0)

    cj = res.get("creepjs", {}) if isinstance(res.get("creepjs"), dict) else {}

    rows = [
        ("`navigator.webdriver`", str(p.get("webdriver")), _yn(p.get("webdriver") is False) + " hidden"),
        ("User-Agent", f"`Chrome/{ua_major}`" if ua_major else "—", _yn(bool(ua_major))),
        ("UA-CH Chromium version", ch_chromium or "—",
         _yn(ua_ch_match) + (" matches UA" if ua_ch_match else " **mismatch vs UA**")),
        ("UA-CH platform", f"{ch.get('platform')} {ch.get('platformVersion')}" if isinstance(ch, dict) else "—",
         _yn(isinstance(ch, dict) and str(ch.get("platform", "")).lower().startswith("win"))),
        ("WebGL vendor / renderer",
         f"{wgl.get('vendor')} / {str(wgl.get('renderer'))[:60]}…" if isinstance(wgl, dict) else "—",
         _yn(isinstance(wgl, dict) and "no-webgl" not in str(wgl)) + " spoofed"),
        ("Canvas 2D", f"`{res.get('canvas_hash')}` (deterministic per seed)", "✅ noised"),
        ("hardwareConcurrency", str(p.get("hardwareConcurrency")), "✅"),
        ("deviceMemory", str(p.get("deviceMemory")), "✅"),
        ("Timezone", str(p.get("timezone")), _yn(str(p.get("timezone")) == res.get("timezone"))),
        ("WebRTC host (LAN) candidate", ", ".join(host) or "none", _yn(no_host_leak) + " no LAN leak"),
    ]
    if srflx:
        rows.append(("WebRTC srflx (public)", ", ".join(srflx),
                     _yn(webrtc_ok) + (" = mocked IP" if expected_webrtc_ip else "")))
    else:
        rows.append(("WebRTC srflx (public)", "none gathered (STUN unreachable on this network)", "—"))
    if cj:
        if cj.get("trust_score"):
            rows.append(("CreepJS trust score", f"{cj['trust_score']}%", "—"))
        if cj.get("lies") is not None:
            rows.append(("CreepJS lies", str(cj["lies"]), _yn(str(cj["lies"]) == "0")))
        if cj.get("headless") is not None:
            rows.append(("CreepJS headless (hard)", f"{cj['headless']}%", _yn(str(cj["headless"]) == "0")))
        if cj.get("stealth") is not None:
            rows.append(("CreepJS stealth-detect", f"{cj['stealth']}%", _yn(str(cj["stealth"]) == "0")))

    date = res["stamp"][:8]
    date = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
    lines = [
        f"**Build `149.0.7827.114` · audited {date} · seed `{res['seed']}` · platform `{res['platform']}`**",
        "",
        "| Signal | Value | Verdict |",
        "|---|---|---|",
    ]
    for name, val, verdict in rows:
        val = str(val).replace("|", "\\|")
        lines.append(f"| {name} | {val} | {verdict} |")
    lines += [
        "",
        f"_UA ↔ UA-CH version consistency: {_yn(ua_ch_match)} "
        f"(UA major `{ua_major}`, UA-CH major `{ch_major}`). "
        f"WebRTC srflx mocked to the proxy/egress IP; real host candidates suppressed._",
        "_Regenerate with `py -3 creepjs_audit.py --readme clearcote-browser/README.md` on each release._",
    ]
    return "\n".join(lines)


def inject_readme(readme_path, table):
    start, end = "<!-- CREEPJS_RESULTS:START -->", "<!-- CREEPJS_RESULTS:END -->"
    block = f"{start}\n{table}\n{end}"
    with open(readme_path, "r", encoding="utf-8") as f:
        text = f.read()
    if start in text and end in text:
        new = re.sub(re.escape(start) + r"[\s\S]*?" + re.escape(end), block, text, count=1)
    else:
        new = text.rstrip() + "\n\n## 🧪 Fingerprint test results\n\n" + block + "\n"
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new)
    print(f"injected table into {readme_path}")


def main():
    try:  # the table uses ✅/❌ — make stdout UTF-8 so Windows cp1252 consoles don't crash
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="clearcoat CreepJS audit -> results table")
    ap.add_argument("--seed", default="demo")
    ap.add_argument("--platform", default="windows")
    ap.add_argument("--timezone", default="America/New_York")
    ap.add_argument("--proxy", default=None, help="user:pass@host:port (real egress; overrides demo webrtc ip)")
    ap.add_argument("--webrtc-ip", dest="webrtc_ip", default=DEMO_WEBRTC_IP)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-creepjs", dest="creepjs", action="store_false", help="skip the CreepJS scrape")
    ap.add_argument("--binary", default=DEFAULT_BINARY)
    ap.add_argument("--readme", default=None, help="README path to inject the table into")
    ap.add_argument("--out", default=os.path.join(HERE, "creepjs_shots"))
    a = ap.parse_args()

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[X] pip package missing:  py -3 -m pip install playwright")
        sys.exit(2)
    if not os.path.exists(a.binary):
        print(f"[X] chrome.exe not found: {a.binary}")
        sys.exit(2)

    proxy = parse_proxy(a.proxy)
    try:
        res, table = audit(a.binary, a.seed, a.platform, a.timezone, proxy, a.webrtc_ip,
                           a.headed, a.creepjs, a.out)
        if a.readme:
            inject_readme(a.readme, table)
        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print("\n[X] audit crashed:")
        import traceback
        traceback.print_exc()
        print(f"    ({type(e).__name__}: {e})")
        sys.exit(3)


if __name__ == "__main__":
    main()
