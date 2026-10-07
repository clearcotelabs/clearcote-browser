// Your own fonts (fontDirs / CLEARCOTE_FONT_DIRS), fallback fonts (CLEARCOTE_FALLBACK_FONT_DIRS) and the
// font lines of `clearcote info` on Linux. Mirrors sdk/python/tests/test_font_dirs.py.
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { basename, delimiter, dirname, join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import {
  FALLBACK_FONT_DIRS_ENV, FONT_DIRS_ENV, SCRIPT_SAMPLES, buildConf, checkFontDirs, fontFiles, fontLaunchEnv, fontLines,
  genuineRules, linuxFontEnv, linuxFontReport, readFont, resolveFontDirs,
} from "../src/fonts.js";
import { makeCollection, makeFont } from "./helpers/fontfiles.js";
import { removeAfterFile, tempDir } from "./helpers/temp.js";

const FC_CACHE = join(tmpdir(), "cc-fc-cache");
if (!existsSync(FC_CACHE)) removeAfterFile(FC_CACHE);

const ASSETS = fileURLToPath(new URL("../../../assets/fonts/", import.meta.url));
const TEMPLATE = readFileSync(join(ASSETS, "fonts.conf.template"), "utf8");

const withPlatform = <T>(plat: string, fn: () => T): T => {
  const orig = Object.getOwnPropertyDescriptor(process, "platform");
  Object.defineProperty(process, "platform", { value: plat, configurable: true });
  try {
    return fn();
  } finally {
    if (orig) Object.defineProperty(process, "platform", orig);
  }
};

let saved: Record<string, string | undefined>;
beforeEach(() => {
  saved = { [FONT_DIRS_ENV]: process.env[FONT_DIRS_ENV], [FALLBACK_FONT_DIRS_ENV]: process.env[FALLBACK_FONT_DIRS_ENV] };
  delete process.env[FONT_DIRS_ENV];
  delete process.env[FALLBACK_FONT_DIRS_ENV];
});
afterEach(() => {
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

function bundle() {
  const dir = tempDir("ccfontdirs-");
  const fonts = join(dir, "bin", "fonts");
  mkdirSync(fonts, { recursive: true });
  writeFileSync(join(fonts, "fonts.conf.template"), TEMPLATE);
  return { dir, exe: join(dir, "bin", "chrome"), fonts };
}

const sub = (root: string, name: string) => { const d = join(root, name); mkdirSync(d, { recursive: true }); return d; };

describe("reading font files", () => {
  it("family names (IDs 1 and 16) and coverage through format 4 and 12", () => {
    const dir = tempDir("ccfont-");
    const f = makeFont(join(dir, "a.ttf"), ["Segoe UI Semibold", "Segoe UI"], [0x41, 0x995, 0x1f600]);
    const info = readFont(f, [0x41, 0x995, 0x1780, 0x1f600]);
    expect([...info.families].sort()).toEqual(["segoe ui", "segoe ui semibold"]);
    expect([...info.covered].sort((a, b) => a - b)).toEqual([0x41, 0x995, 0x1f600]);
    expect(info.color).toBe(false);
  });

  it("glyphIdArray segments, and glyph 0 is not coverage", () => {
    const f = makeFont(join(tempDir("ccfont-"), "b.ttf"), ["X"], [0x41, 0x42, 0x995], { viaGlyphArray: [0x42, 0x995] });
    expect([...readFont(f, [0x41, 0x42, 0x995, 0x43, 0xffff]).covered].sort((a, b) => a - b)).toEqual([0x41, 0x42, 0x995]);
  });

  it("colour tables and collections", () => {
    const dir = tempDir("ccfont-");
    const emoji = readFont(makeFont(join(dir, "e.ttf"), ["Noto Color Emoji"], [0x1f600], { color: true }), [0x1f600]);
    expect(emoji.color && emoji.covered.has(0x1f600) && emoji.families.has("noto color emoji")).toBe(true);
    const ttc = readFont(makeCollection(join(dir, "c.ttc"), [[["WenQuanYi Zen Hei"], [0x4e2d]], [["WenQuanYi Zen Hei Mono"], [0xac00]]]), [0x4e2d, 0xac00]);
    expect([...ttc.families].sort()).toEqual(["wenquanyi zen hei", "wenquanyi zen hei mono"]);
    expect(ttc.covered.size).toBe(2);
  });

  it.each([
    ["Arimo-Regular.ttf", "arimo", [0x41, 0x05d0], [0x0995]],
    ["DejaVuSans.ttf", "dejavu sans", [0x0628, 0x0531, 0x0e81], [0x0995, 0x4e2d]],
    ["Inconsolata.otf", "inconsolata", [0x41], [0x0628]],
  ])("reads the bundled %s like any font", (name, family, has, lacks) => {
    const info = readFont(join(ASSETS, name as string), [...(has as number[]), ...(lacks as number[])]);
    expect(info.families.has(family as string)).toBe(true);
    expect([...info.covered].sort((a, b) => a - b)).toEqual([...(has as number[])].sort((a, b) => a - b));
    expect(info.color).toBe(false);
  });

  it("ignores what is not a font", () => {
    const dir = tempDir("ccfont-");
    writeFileSync(join(dir, "junk.ttf"), "not a font at all");
    writeFileSync(join(dir, "empty.otf"), "");
    writeFileSync(join(dir, "short.ttc"), Buffer.from("ttcf\0\x01\0\0\0\0\0\x05\0\0\0\x40", "latin1"));
    for (const n of ["junk.ttf", "empty.otf", "short.ttc"]) {
      const info = readFont(join(dir, n), [0x41]);
      expect([info.families.size, info.covered.size, info.color]).toEqual([0, 0, false]);
    }
  });

  it("walks subdirectories", () => {
    const dir = tempDir("ccfont-");
    makeFont(join(sub(dir, "sub"), "x.TTF"), ["X"]);
    makeFont(join(dir, "y.otf"), ["Y"]);
    writeFileSync(join(dir, "readme.txt"), "no");
    expect(fontFiles([dir]).map((p) => basename(p))).toEqual(["x.TTF", "y.otf"]);
  });
});

describe("which directories", () => {
  it("the option first, then the variable, no duplicates; fallback keeps out your own", () => {
    const root = tempDir("ccdirs-");
    const [a, b, c] = ["a", "b", "c"].map((n) => sub(root, n));
    const env = {
      [FONT_DIRS_ENV]: [b, a, join(root, "gone")].join(delimiter),
      [FALLBACK_FONT_DIRS_ENV]: [c, a, ""].join(delimiter),
    };
    expect(withPlatform("linux", () => resolveFontDirs([a], env))).toEqual({ user: [a, b], fallback: [c], ignored: [join(root, "gone")] });
  });

  it("checks the option", () => {
    const root = tempDir("ccdirs-");
    expect(() => checkFontDirs([join(root, "typo")], "linux")).toThrow(/not a directory/);
    expect(() => checkFontDirs(42, "linux")).toThrow(TypeError);
    expect(() => checkFontDirs([root, 7], "linux")).toThrow(TypeError);
    expect(checkFontDirs(root, "linux")).toEqual([root]);
    expect(checkFontDirs([join(root, "typo")], "win32")).toEqual([join(root, "typo")]); // Windows has its own fonts
  });
});

describe("the generated fontconfig file", () => {
  it("is the template when no genuine family is involved", () => {
    expect(genuineRules(TEMPLATE, new Set())).toBe(TEMPLATE);
    expect(genuineRules(TEMPLATE, new Set(["some other family"]))).toBe(TEMPLATE);
  });

  it("drops the lookalike, retargets aliases and generics", () => {
    const out = genuineRules(TEMPLATE, new Set(["arial", "segoe ui", "consolas"]));
    for (const f of ["Arial", "Segoe UI", "Consolas"]) expect(out).not.toContain(`<string>${f}</string></test>`);
    expect(out).toContain('<test name="family"><string>Helvetica</string></test><edit name="family" mode="assign" binding="strong"><string>Arial</string></edit>');
    expect(out).toContain("<alias><family>sans-serif</family><prefer><family>Arial</family></prefer></alias>");
    expect(out).toContain("<alias><family>system-ui</family><prefer><family>Segoe UI</family></prefer></alias>");
    expect(out).toContain("<alias><family>monospace</family><prefer><family>Consolas</family></prefer></alias>");
    expect(out).toContain("<alias><family>serif</family><prefer><family>Tinos</family></prefer></alias>");
    expect(out).toContain('<test name="family"><string>Calibri</string></test><edit name="family" mode="assign" binding="strong"><string>Carlito</string></edit>');
    expect(out).toContain('<test name="family"><string>Times</string></test><edit name="family" mode="assign" binding="strong"><string>Tinos</string></edit>');
    const before = new Set(TEMPLATE.split("\n"));
    for (const line of out.split("\n")) before.delete(line);
    expect(before.size).toBeGreaterThan(0);
    for (const line of before) expect(line.includes('<test name="family">') || line.includes("<alias>")).toBe(true);
    expect(out.split('<match target="font">').length).toBe(TEMPLATE.split('<match target="font">').length);
  });

  it("follows the template it is given", () => {
    const t = TEMPLATE.replace("<string>Inconsolata</string></edit>", "<string>Cousine</string></edit>");
    const out = genuineRules(t, new Set(["trebuchet ms"]));
    expect(out).not.toContain("<string>Trebuchet MS</string></test>");
    expect(out).toContain('<test name="family"><string>Consolas</string></test><edit name="family" mode="assign" binding="strong"><string>Cousine</string></edit>');
  });

  it("lists your fonts first and fallback last; plain substitution without them", () => {
    const conf = buildConf(TEMPLATE, "/opt/cc/fonts", "/tmp/c", ["/home/me/win & fonts"], ["/usr/local/fb"]);
    const dirs = conf.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("<dir>"));
    expect(dirs).toEqual(["<dir>/home/me/win &amp; fonts</dir>", "<dir>/opt/cc/fonts</dir>", "<dir>/usr/local/fb</dir>"]);
    expect(conf).not.toContain("@FONTS_DIR@");
    expect(conf).toContain("<cachedir>/tmp/c</cachedir>");
    expect(buildConf(TEMPLATE, "/f", "/c")).toBe(TEMPLATE.split("@FONTS_DIR@").join("/f").split("@CACHE_DIR@").join("/c"));
  });

  it("without extra directories the file is what it always was", () => {
    withPlatform("linux", () => {
      const { exe, fonts } = bundle();
      const path = linuxFontEnv(exe).FONTCONFIG_FILE;
      expect(path).toBe(join(fonts, "fonts.generated.conf"));
      expect(readFileSync(path, "utf8")).toBe(TEMPLATE.split("@FONTS_DIR@").join(fonts).split("@CACHE_DIR@").join(FC_CACHE));
      expect(readdirSync(fonts).sort()).toEqual(["fonts.conf.template", "fonts.generated.conf"]);
    });
  });

  it("with your Windows fonts: listed first, their lookalikes dropped, one file per set of directories", () => {
    withPlatform("linux", () => {
      const { dir, exe, fonts } = bundle();
      const win = sub(dir, "winfonts");
      makeFont(join(win, "arial.ttf"), ["Arial"], [0x41]);
      process.env[FONT_DIRS_ENV] = win;
      const path = linuxFontEnv(exe).FONTCONFIG_FILE;
      expect(basename(path).startsWith("fonts.generated-")).toBe(true);
      expect(dirname(path)).toBe(fonts);
      const conf = readFileSync(path, "utf8");
      expect(conf.indexOf(`<dir>${win}</dir>`)).toBeLessThan(conf.indexOf(`<dir>${fonts}</dir>`));
      expect(conf).not.toContain("<string>Arial</string></test>");
      expect(conf).toContain("<string>Segoe UI</string></test>");
      const other = sub(dir, "other");
      const path2 = linuxFontEnv(exe, [other]).FONTCONFIG_FILE;
      expect(path2).not.toBe(path);
      expect(existsSync(path)).toBe(true);
      const conf2 = readFileSync(path2, "utf8");
      expect(conf2.indexOf(`<dir>${other}</dir>`)).toBeLessThan(conf2.indexOf(`<dir>${win}</dir>`));
      expect(conf2.indexOf(`<dir>${win}</dir>`)).toBeLessThan(conf2.indexOf(`<dir>${fonts}</dir>`));
      expect(linuxFontEnv(exe).FONTCONFIG_FILE).toBe(path);
    });
  });

  it("with fallback fonts: listed after the bundle, no rule changes", () => {
    withPlatform("linux", () => {
      const { dir, exe, fonts } = bundle();
      const fb = sub(dir, "fallback");
      makeFont(join(fb, "lookalike.ttf"), ["Arial"], [0x41]);
      process.env[FALLBACK_FONT_DIRS_ENV] = fb;
      const conf = readFileSync(linuxFontEnv(exe).FONTCONFIG_FILE, "utf8");
      expect(conf.indexOf(`<dir>${fonts}</dir>`)).toBeLessThan(conf.indexOf(`<dir>${fb}</dir>`));
      expect(conf).toContain("<string>Arial</string></test>");
    });
  });

  it("a missing directory from the environment is ignored", () => {
    withPlatform("linux", () => {
      const { dir, exe, fonts } = bundle();
      process.env[FONT_DIRS_ENV] = join(dir, "not-there");
      expect(linuxFontEnv(exe).FONTCONFIG_FILE).toBe(join(fonts, "fonts.generated.conf"));
    });
  });

  it("fontLaunchEnv passes fontDirs", () => {
    withPlatform("linux", () => {
      const { dir, exe } = bundle();
      const win = sub(dir, "w");
      const env = fontLaunchEnv(exe, undefined, [], [win])!;
      expect(readFileSync(env.FONTCONFIG_FILE!, "utf8")).toContain(`<dir>${win}</dir>`);
    });
  });
});

describe("clearcote info", () => {
  function reportBundle() {
    const b = bundle();
    makeFont(join(b.fonts, "Arimo-Regular.ttf"), ["Arimo"], [0x41, 0x3a9, 0x416, 0x5d0]);
    makeFont(join(b.fonts, "DejaVuSans.ttf"), ["DejaVu Sans"], [0x41, 0x628, 0x531, 0x10d0, 0xe81, 0x1f600]);
    return b;
  }

  it("lists scripts without a font; a monochrome emoji does not count", () => {
    const { exe } = reportBundle();
    const r = withPlatform("linux", () => linuxFontReport(exe))!;
    expect(r.bundled).toBe(true);
    expect([r.fontDirs, r.fallbackFontDirs]).toEqual([[], []]);
    expect(r.scripts!.covered).toEqual(["Latin", "Greek", "Cyrillic", "Hebrew", "Arabic", "Armenian", "Georgian", "Lao"]);
    expect(r.scripts!.missing).toContain("Emoji");
    expect(r.scripts!.covered.length + r.scripts!.missing.length).toBe(SCRIPT_SAMPLES.length);
    expect(r.genuineWindowsFamilies).toBeUndefined();
    expect(r.ignoredFontDirs).toBeUndefined();
  });

  it("with your fonts and fallback fonts", () => {
    const { dir, exe } = reportBundle();
    const win = sub(dir, "win"), fb = sub(dir, "fb");
    makeFont(join(win, "arial.ttf"), ["Arial"], [0x41]);
    makeCollection(join(win, "fonts.ttc"), [[["Segoe UI"], [0x41]], [["Nirmala UI"], [0x995, 0xb95]]]);
    makeFont(join(fb, "emoji.ttf"), ["Noto Color Emoji"], [0x1f600], { color: true });
    const env = { [FALLBACK_FONT_DIRS_ENV]: [fb, join(dir, "nope")].join(delimiter) };
    const r = withPlatform("linux", () => linuxFontReport(exe, [win], env))!;
    expect([r.fontDirs, r.fallbackFontDirs, r.ignoredFontDirs]).toEqual([[win], [fb], [join(dir, "nope")]]);
    for (const s of ["Bengali", "Tamil", "Emoji"]) expect(r.scripts!.covered).toContain(s);
    expect(r.genuineWindowsFamilies).toEqual(["Arial", "Segoe UI"]);
    expect(r.lookalikeWindowsFamilies).toContain("Calibri");
    expect(r.lookalikeWindowsFamilies).not.toContain("Arial");
  });

  it("no report without a bundle", () => {
    expect(linuxFontReport(join(tempDir("ccnob-"), "chrome"))).toBeUndefined();
  });

  it("prints the same lines as the Python CLI", () => {
    expect(fontLines({
      bundled: true, note: "metric-compatible Windows font clones are bundled with this build",
      scripts: { covered: ["Latin", "Greek"], missing: ["Khmer", "Ethiopic"] },
      fontDirs: ["/srv/winfonts"], fallbackFontDirs: ["/usr/local/share/clearcote/fonts"], ignoredFontDirs: ["/nope"],
      genuineWindowsFamilies: ["Arial", "Segoe UI"], lookalikeWindowsFamilies: ["Calibri"],
    })).toEqual([
      "Fonts           metric-compatible Windows font clones are bundled with this build",
      "Scripts         2 of 4 render; no font for Khmer, Ethiopic (they draw as empty boxes)",
      "Your fonts      /srv/winfonts",
      "                real: Arial, Segoe UI",
      "                lookalike: Calibri",
      "Fallback fonts  /usr/local/share/clearcote/fonts",
      "Ignored         /nope (not a directory)",
    ]);
    expect(fontLines({ bundled: true, note: "n", scripts: { covered: ["Latin"], missing: [] }, fontDirs: [] }))
      .toEqual(["Fonts           n", "Scripts         all 1 render"]);
    expect(fontLines({ bundled: false, note: "no bundle" })).toEqual(["Fonts           no bundle"]);
  });
});
