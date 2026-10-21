"""clearcote open build — macOS persona font coherence check.

The open build needs no licence key. Pass CC_APP to point at another binary.

A family is "present" only if its whole TextMetrics signature differs from a
guaranteed-absent family's, measured against BOTH generic fallbacks: macOS's
monospace default is Menlo, so a monospace-metric family compared against a
single monospace baseline reports absent; and Tinos (the Times New Roman clone)
matches macOS's Times, which is what the serif fallback resolves to under a
Windows persona. document.fonts.check() answers true for almost any name.

A `fingerprint=` seed is required: the font block in
FontCache::GetFontPlatformData is gated on --fingerprint / --fingerprint-profile,
so a platform=-only launch spoofs the strings and measures no font work at all.
"""
import os, sys
APP = os.environ.get("CC_APP") or os.path.expanduser(
    "~/clearcote-mac-build/open150/build/src/out/Default/Chromium.app/Contents/MacOS/Chromium")
import clearcote
SEED = os.environ.get("CC_SEED", "open150-macport")

WIN_ONLY = ['Segoe UI', 'Calibri', 'Consolas', 'Cambria', 'Candara', 'Corbel',
            'Tahoma', 'Impact', 'Trebuchet MS']
MAC_ONLY = ['Helvetica Neue', 'Lucida Grande', 'Apple Chancery', 'Geneva',
            'Optima', 'Hiragino Sans', 'Menlo']
BOTH = ['Arial', 'Georgia', 'Times New Roman', 'Courier New', 'Verdana']

PROBE = """(names) => {
  const c = document.createElement('canvas').getContext('2d');
  const sig = (f, gen) => { c.font = '32px "'+f+'", '+gen;
    const m = c.measureText('WgQmil');
    return [m.width, m.fontBoundingBoxAscent, m.fontBoundingBoxDescent,
            m.actualBoundingBoxAscent, m.actualBoundingBoxDescent,
            m.alphabeticBaseline].map(v => +(+v).toFixed(3)).join('/'); };
  const w = (f, gen) => { c.font = '32px "'+f+'", '+gen;
                          return +c.measureText('WgQmil').width.toFixed(2); };
  // Three baselines, not two. Under a Windows persona on macOS the serif AND monospace
  // generics both land on Times-metric fonts, and Tinos (the Times New Roman clone) is
  // metrically identical to them -- so with only those two contexts a correctly
  // substituted Times New Roman is indistinguishable from an absent family.
  const GENERICS = ['monospace','serif','sans-serif'];
  const base = Object.fromEntries(GENERICS.map(g => [g, sig('ZZZnotarealfamilyZZZ', g)]));
  const present = f => GENERICS.some(g => sig(f, g) !== base[g]);
  c.font = '16px system-ui';
  const sysui = +c.measureText('WgQmil').width.toFixed(2);
  c.font = '32px serif';
  const emoji = +c.measureText('\\u{1F600}').width.toFixed(2);
  // On real Windows the generics bind to different families (serif Times New Roman,
  // monospace Consolas, sans-serif Arial), so equal widths here are a reference-free tell.
  const generics = Object.fromEntries(
    ['serif','sans-serif','monospace','cursive','fantasy'].map(
      g => [g, (c.font = '32px '+g, +c.measureText('WgQmil').width.toFixed(2))]));
  return { sysui, emoji, generics, platform: navigator.platform,
           width: Object.fromEntries(names.map(f => [f, w(f,'serif')])),
           present: Object.fromEntries(names.map(f => [f, present(f)])) };
}"""

def run(persona):
    b = clearcote.launch(executable_path=APP, docker=False, headless=True,
                         platform=persona, fingerprint=SEED,
                         args=["--use-mock-keychain"])
    pg = b.new_page(); pg.goto("about:blank")
    r = pg.evaluate(PROBE, WIN_ONLY + MAC_ONLY + BOTH)
    b.close()
    return r

res = {p: run(p) for p in ("macos", "windows")}
fails = 0
print(f"{'font':22s} {'macos persona':>14s} {'windows persona':>16s}   want (win)")
for label, fonts, want in [("Windows-only", WIN_ONLY, True),
                           ("macOS-only", MAC_ONLY, False),
                           ("on both OSes", BOTH, True)]:
    print(f"-- {label}")
    for f in fonts:
        a, b_ = res['macos']['present'][f], res['windows']['present'][f]
        ok = (b_ == want)
        fails += (not ok)
        print(f"   {f:22s} {str(a):>11s} {str(b_):>13s}   {str(want):5s} "
              f"{'ok ' if ok else 'FAIL'}   w(mac)={res['macos']['width'][f]:7} "
              f"w(win)={res['windows']['width'][f]:7}")
for p in ("macos", "windows"):
    r = res[p]
    print(f"persona={p:8s} platform={r['platform']:9s} "
          f"16px-system-ui={r['sysui']:7} 32px-emoji={r['emoji']:7}")
print("generic widths:")
for p in ("macos", "windows"):
    print(f"   {p:8s} {res[p]['generics']}")
sysui_ok = res['macos']['sysui'] != res['windows']['sysui']
mono_ok = res['windows']['generics']['monospace'] != res['windows']['generics']['serif']
print("system-ui differs between personas:", sysui_ok)
print("windows persona: monospace width != serif width:", mono_ok)
fails += (not sysui_ok) + (not mono_ok)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
