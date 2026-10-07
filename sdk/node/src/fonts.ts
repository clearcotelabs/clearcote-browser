// Linux font wiring.
//
// The Linux release bundles metric-compatible font clones (Segoe UI->Selawik,
// Arial->Arimo, Times New Roman->Tinos, …) under `<binDir>/fonts/`, together with a
// self-contained `fonts.conf.template`. On a bare server/container the Windows families
// (and even the standard fontconfig metric-alias rules) are absent, so a page asking for
// "Segoe UI" collapses to a single default — a detectable render + an absent-font tell.
//
// At launch we materialize the template (substituting the real fonts dir + a writable
// cache dir) and point FONTCONFIG_FILE at it, so the clones resolve without depending on
// the host's /etc/fonts. No-op on non-Linux and on older binaries that ship no `fonts/`.
//
// Because the template is self-contained, fonts installed on the host are invisible to the
// browser. Two settings add directories to it (the same as the Python SDK's _fonts.py):
//
// * `fontDirs` / CLEARCOTE_FONT_DIRS — your own fonts, typically a copy of a Windows machine's
//   Fonts folder. Listed ahead of the bundle. On an engine that lets a genuine face win over its
//   substitute (it carries GENUINE_FACES_SWITCH; PRO r32+), every family they provide is also used as
//   itself: its lookalike rule (Arial->Arimo, Segoe UI->Selawik, …) is dropped, and the names and CSS
//   generics that resolve to it on Windows (Helvetica->Arial, sans-serif->Arial, system-ui->Segoe UI,
//   …) point at it. An older engine still draws every family a persona lists through its own
//   substitute, so the rules stay there: changing them would split Helvetica from Arial. The
//   directories still serve the characters the bundle cannot draw.
// * CLEARCOTE_FALLBACK_FONT_DIRS — fonts used only for characters nothing else covers (CJK, Indic,
//   emoji, …). Listed after the bundle and change no rules. The Docker image sets it to the script
//   fonts it installs.
//
// Both are lists separated by the path delimiter. Never point either at all of /usr/share/fonts:
// the host's Latin families then compete with the clones and change the Windows widths.

import { closeSync, existsSync, mkdirSync, openSync, readFileSync, readSync, readdirSync, renameSync, statSync, writeFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { delimiter, dirname, join, resolve } from "node:path";
import { homedir, tmpdir } from "node:os";
import { engineSupportsSwitch } from "./launchopts.js";

export const FONT_DIRS_ENV = "CLEARCOTE_FONT_DIRS";
export const FALLBACK_FONT_DIRS_ENV = "CLEARCOTE_FALLBACK_FONT_DIRS";

const FONT_EXTS = [".ttf", ".otf", ".ttc", ".otc"];

/** Carried by an engine that lets a genuine Windows face installed here win over its substitute (PRO
 * r32+). The SDK never passes it: its presence in the binary is the marker. Measured on an engine without
 * it: with a genuine Arial in CLEARCOTE_FONT_DIRS and the rules changed, "Helvetica" reached the genuine
 * file while "Arial" stayed on the engine's Arimo -- two widths no Windows machine has. */
export const GENUINE_FACES_SWITCH = "disable-genuine-font-faces";

/** Whether the engine at `exePath` lets a genuine face from your own fonts win (see GENUINE_FACES_SWITCH). */
export function engineHonoursGenuineFaces(exePath: string): boolean {
  return engineSupportsSwitch(exePath, GENUINE_FACES_SWITCH);
}

/** The Windows family each non-Windows name in the template stands for (the Windows registry's
 * FontSubstitutes): when the genuine family is present, the rule points at it instead of a clone. */
const WINDOWS_ALIASES: Record<string, string> = { helvetica: "Arial", times: "Times New Roman", courier: "Courier New" };

/** What each CSS generic resolves to in Chrome on Windows (measured: see fonts.conf.template). */
const GENERIC_WINDOWS: Record<string, string> = {
  "sans-serif": "Arial", serif: "Times New Roman", monospace: "Consolas", "system-ui": "Segoe UI",
};

/** The Windows families the bundle stands in for with a lookalike; `clearcote info` reports which of
 * them your own fonts provide for real. */
export const KEY_WINDOWS_FAMILIES = ["Arial", "Segoe UI", "Calibri", "Cambria", "Consolas", "Courier New", "Georgia",
  "Times New Roman", "Verdana", "Tahoma", "Trebuchet MS", "Comic Sans MS", "Impact"] as const;

/** One character per script that Windows 10/11 renders out of the box. A script with no font for its
 * character draws empty boxes, which a page tells apart from real glyphs with one fillText. */
export const SCRIPT_SAMPLES: ReadonlyArray<readonly [string, number]> = [
  ["Latin", 0x0041], ["Greek", 0x03a9], ["Cyrillic", 0x0416], ["Hebrew", 0x05d0], ["Arabic", 0x0628],
  ["Armenian", 0x0531], ["Georgian", 0x10d0], ["Devanagari", 0x0915], ["Bengali", 0x0995],
  ["Gurmukhi", 0x0a15], ["Gujarati", 0x0a95], ["Oriya", 0x0b15], ["Tamil", 0x0b95], ["Telugu", 0x0c15],
  ["Kannada", 0x0c95], ["Malayalam", 0x0d15], ["Sinhala", 0x0d9a], ["Thai", 0x0e01], ["Lao", 0x0e81],
  ["Tibetan", 0x0f40], ["Myanmar", 0x1000], ["Ethiopic", 0x1200], ["Cherokee", 0x13a0],
  ["Khmer", 0x1780], ["Mongolian", 0x1820], ["Hangul", 0xac00], ["Kana", 0x3042],
  ["CJK ideographs", 0x4e2d], ["Emoji", 0x1f600],
];
/** Emoji count only from a colour font: a monochrome glyph (DejaVu has one) is not what Windows draws. */
const COLOR_TABLES = ["COLR", "CBDT", "sbix", "SVG "];

// --- reading font files (just the name and cmap tables; no fontconfig needed) --------------------

function readAt(fd: number, offset: number, length: number): Buffer {
  const buf = Buffer.alloc(Math.max(0, length));
  const n = length > 0 ? readSync(fd, buf, 0, length, offset) : 0;
  return buf.subarray(0, n);
}

/** Offsets of the sfnt faces in a font file: one for .ttf/.otf, several for a collection. */
function faces(fd: number): number[] {
  const head = readAt(fd, 0, 12);
  if (head.length < 12) return [];
  if (head.toString("latin1", 0, 4) === "ttcf") {
    const count = Math.min(head.readUInt32BE(8), 1024);
    const data = readAt(fd, 12, 4 * count);
    const out: number[] = [];
    for (let i = 0; i + 4 <= data.length; i += 4) out.push(data.readUInt32BE(i));
    return out;
  }
  return [0];
}

function tables(fd: number, offset: number): Map<string, [number, number]> {
  const head = readAt(fd, offset, 12);
  const out = new Map<string, [number, number]>();
  if (head.length < 12) return out;
  const num = head.readUInt16BE(4);
  const data = readAt(fd, offset + 12, 16 * num);
  for (let i = 0; i + 16 <= data.length; i += 16) {
    out.set(data.toString("latin1", i, i + 4), [data.readUInt32BE(i + 8), data.readUInt32BE(i + 12)]);
  }
  return out;
}

function macRoman(raw: Buffer): string {
  try {
    return new TextDecoder("macintosh").decode(raw);
  } catch {
    return raw.toString("latin1"); // a Node without the legacy encodings: right for ASCII names
  }
}

/** Family names (name IDs 1 and 16, every language) of one face, lower-cased. */
function families(fd: number, [off, length]: [number, number]): Set<string> {
  const data = readAt(fd, off, length);
  const names = new Set<string>();
  if (data.length < 6) return names;
  const count = data.readUInt16BE(2);
  const strings = data.readUInt16BE(4);
  for (let i = 0; i < count; i++) {
    const at = 6 + 12 * i;
    if (at + 12 > data.length) break;
    const platform = data.readUInt16BE(at), encoding = data.readUInt16BE(at + 2), nameId = data.readUInt16BE(at + 6);
    const n = data.readUInt16BE(at + 8), o = data.readUInt16BE(at + 10);
    if (nameId !== 1 && nameId !== 16) continue;
    const raw = data.subarray(strings + o, strings + o + n);
    let text: string;
    if (platform === 0 || platform === 3) {
      if (raw.length % 2) continue;
      text = Buffer.from(raw).swap16().toString("utf16le");
    } else if (platform === 1 && encoding === 0) {
      text = macRoman(raw);
    } else {
      continue;
    }
    if (text.trim()) names.add(text.trim().toLowerCase());
  }
  return names;
}

/** Which of `codepoints` the face maps to a real glyph (format 4 and 12 subtables). */
function cmapCovers(fd: number, [off, length]: [number, number], codepoints: readonly number[]): Set<number> {
  const data = readAt(fd, off, length);
  if (data.length < 4) return new Set();
  const count = data.readUInt16BE(2);
  const subtables = new Map<string, number>();
  for (let i = 0; i < count; i++) {
    const at = 4 + 8 * i;
    if (at + 8 > data.length) break;
    const sub = data.readUInt32BE(at + 4);
    if (sub + 2 <= data.length) {
      const key = `${data.readUInt16BE(at)},${data.readUInt16BE(at + 2)},${data.readUInt16BE(sub)}`;
      if (!subtables.has(key)) subtables.set(key, sub);
    }
  }
  for (const key of ["3,10,12", "0,6,12", "0,4,12", "3,1,4", "0,3,4", "0,2,4", "0,1,4", "0,0,4"]) {
    const sub = subtables.get(key);
    if (sub !== undefined) return key.endsWith(",12") ? fmt12(data, sub, codepoints) : fmt4(data, sub, codepoints);
  }
  return new Set();
}

function fmt12(data: Buffer, sub: number, codepoints: readonly number[]): Set<number> {
  const out = new Set<number>();
  if (sub + 16 > data.length) return out;
  const groups = Math.min(data.readUInt32BE(sub + 12), Math.floor((data.length - sub - 16) / 12));
  for (let i = 0; i < groups; i++) {
    const at = sub + 16 + 12 * i;
    const start = data.readUInt32BE(at), end = data.readUInt32BE(at + 4), glyph = data.readUInt32BE(at + 8);
    for (const c of codepoints) if (c >= start && c <= end && glyph + (c - start) !== 0) out.add(c);
  }
  return out;
}

function fmt4(data: Buffer, sub: number, codepoints: readonly number[]): Set<number> {
  const out = new Set<number>();
  if (sub + 14 > data.length) return out;
  const seg2 = data.readUInt16BE(sub + 6);
  const endsAt = sub + 14, startsAt = endsAt + seg2 + 2, deltasAt = startsAt + seg2, rangesAt = deltasAt + seg2;
  if (rangesAt + seg2 > data.length) return out;
  for (const c of codepoints) {
    if (c > 0xffff) continue;
    for (let i = 0; i < seg2 / 2; i++) {
      if (c > data.readUInt16BE(endsAt + 2 * i)) continue;
      const start = data.readUInt16BE(startsAt + 2 * i);
      if (c < start) break;
      const delta = data.readInt16BE(deltasAt + 2 * i);
      const rangeOff = data.readUInt16BE(rangesAt + 2 * i);
      let glyph: number;
      if (rangeOff === 0) {
        glyph = (c + delta) & 0xffff;
      } else {
        const at = rangesAt + 2 * i + rangeOff + 2 * (c - start);
        if (at + 2 > data.length) break;
        glyph = data.readUInt16BE(at);
        if (glyph) glyph = (glyph + delta) & 0xffff;
      }
      if (glyph) out.add(c);
      break;
    }
  }
  return out;
}

export interface FontInfo { families: Set<string>; covered: Set<number>; color: boolean }

/** A font file's family names (lower-cased, every face of a collection), which of `codepoints` it
 * covers, and whether a face with a colour table covers any of them. Empty for a file that is not a
 * readable font. */
export function readFont(path: string, codepoints: readonly number[] = []): FontInfo {
  const info: FontInfo = { families: new Set(), covered: new Set(), color: false };
  let fd: number | undefined;
  try {
    fd = openSync(path, "r");
    for (const face of faces(fd)) {
      const t = tables(fd, face);
      const name = t.get("name");
      if (name) for (const f of families(fd, name)) info.families.add(f);
      const cmap = t.get("cmap");
      if (codepoints.length && cmap) {
        const got = cmapCovers(fd, cmap, codepoints);
        for (const c of got) info.covered.add(c);
        if (got.size && COLOR_TABLES.some((tag) => t.has(tag))) info.color = true;
      }
    }
  } catch {
    return { families: new Set(), covered: new Set(), color: false };
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
  return info;
}

/** Every font file under `dirs` (recursively, as fontconfig reads a `<dir>`), sorted. */
export function fontFiles(dirs: readonly string[]): string[] {
  const found: string[] = [];
  const walk = (d: string) => {
    let entries;
    try {
      entries = readdirSync(d, { withFileTypes: true });
    } catch {
      return;
    }
    for (const e of entries) {
      const p = join(d, e.name);
      if (e.isDirectory()) walk(p);
      else if (FONT_EXTS.some((x) => e.name.toLowerCase().endsWith(x))) found.push(p);
    }
  };
  for (const d of dirs) walk(d);
  return found.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

const familyCache = new Map<string, ReadonlySet<string>>();

/** Lower-cased family names of every font under `dirs` (cached per file size and mtime). */
export function dirFamilies(dirs: readonly string[]): Set<string> {
  const fams = new Set<string>();
  for (const path of fontFiles(dirs)) {
    let key: string;
    try {
      const st = statSync(path);
      key = `${path}\0${st.size}\0${st.mtimeMs}`;
    } catch {
      continue;
    }
    if (!familyCache.has(key)) familyCache.set(key, readFont(path).families);
    for (const f of familyCache.get(key)!) fams.add(f);
  }
  return fams;
}

// --- which directories -----------------------------------------------------------------------------

function expand(p: string): string {
  return resolve(p === "~" || p.startsWith("~/") ? join(homedir(), p.slice(1)) : p);
}

function split(value: string | undefined): string[] {
  if (!value) return [];
  return value.split(delimiter).map((p) => p.trim()).filter(Boolean).map(expand);
}

function isDir(p: string): boolean {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** The `fontDirs` launch option as a list of absolute paths. Throws TypeError for a wrong type, and on
 * Linux for a path that is not a directory: a typo must not silently fall back to the lookalikes.
 * (Elsewhere the option does nothing: Windows and macOS have their own fonts.) */
export function checkFontDirs(fontDirs: unknown, platform: string = process.platform): string[] {
  if (fontDirs === undefined || fontDirs === null) return [];
  const list = typeof fontDirs === "string" ? [fontDirs] : fontDirs;
  if (!Array.isArray(list) || !list.every((d) => typeof d === "string")) {
    throw new TypeError("fontDirs must be a directory path or an array of them");
  }
  const dirs = (list as string[]).map(expand);
  if (platform === "linux") {
    for (const d of dirs) if (!isDir(d)) throw new TypeError(`fontDirs: ${d} is not a directory`);
  }
  return dirs;
}

export interface FontDirs { user: string[]; fallback: string[]; ignored: string[] }

/** The option's directories then CLEARCOTE_FONT_DIRS (`user`), and CLEARCOTE_FALLBACK_FONT_DIRS
 * (`fallback`), each without duplicates; `ignored` lists entries from the environment that are not
 * directories (the option throws for those instead). */
export function resolveFontDirs(fontDirs?: unknown, env: Record<string, string | undefined> = process.env): FontDirs {
  const user: string[] = [], fallback: string[] = [], ignored: string[] = [];
  for (const d of checkFontDirs(fontDirs)) if (!user.includes(d)) user.push(d);
  for (const d of split(env[FONT_DIRS_ENV])) {
    if (!isDir(d)) ignored.push(d);
    else if (!user.includes(d)) user.push(d);
  }
  for (const d of split(env[FALLBACK_FONT_DIRS_ENV])) {
    if (!isDir(d)) ignored.push(d);
    else if (!user.includes(d) && !fallback.includes(d)) fallback.push(d);
  }
  return { user, fallback, ignored };
}

// --- the generated fontconfig file -----------------------------------------------------------------

const xml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const unxml = (s: string) =>
  s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, "\"").replace(/&amp;/g, "&").trim();

const RULE = /[ \t]*<match\b[^>]*>\s*<test\b[^>]*name="family"[^>]*>\s*<string>([^<]*)<\/string>\s*<\/test>\s*<edit\b[^>]*name="family"[^>]*>\s*<string>([^<]*)<\/string>(\s*<\/edit>\s*<\/match>)[ \t]*\n?/g;
const ALIAS = /(<alias\b[^>]*>\s*<family>([^<]*)<\/family>\s*<prefer>\s*<family>)([^<]*)(<\/family>)/g;
const BUNDLE_DIR = /<dir>\s*@FONTS_DIR@\s*<\/dir>/;

/** `template` with the lookalike rules and generics adjusted to the `genuine` families (lower-cased
 * names your own fonts provide). A family that is genuinely present loses its rule; a non-Windows name
 * whose Windows family is present (Helvetica -> Arial) and a generic that resolves to it on Windows
 * (sans-serif -> Arial) point at the genuine family. */
export function genuineRules(template: string, genuine: ReadonlySet<string>): string {
  if (!genuine.size) return template;
  return template
    .replace(RULE, (whole, fam: string, clone: string, tail: string) => {
      const family = unxml(fam);
      if (genuine.has(family.toLowerCase())) return "";
      const target = WINDOWS_ALIASES[family.toLowerCase()];
      if (target && genuine.has(target.toLowerCase()) && unxml(clone) !== target) {
        return whole.replace(`<string>${clone}</string>${tail}`, `<string>${xml(target)}</string>${tail}`);
      }
      return whole;
    })
    .replace(ALIAS, (whole, head: string, generic: string, _prefer: string, close: string) => {
      const target = GENERIC_WINDOWS[unxml(generic).toLowerCase()];
      return target && genuine.has(target.toLowerCase()) ? head + xml(target) + close : whole;
    });
}

/** The fontconfig file for one launch: the template with its directories filled in, your own
 * directories ahead of the bundle, the fallback ones after it, and the rules adjusted to the families
 * your own fonts genuinely provide. */
export function buildConf(template: string, fontsDir: string, cacheDir: string, userDirs: readonly string[] = [],
  fallbackDirs: readonly string[] = [], genuine: ReadonlySet<string> = new Set()): string {
  let conf = genuineRules(template, genuine);
  if (userDirs.length || fallbackDirs.length) {
    const mine = userDirs.map((d) => `<dir>${xml(d)}</dir>`);
    const after = fallbackDirs.map((d) => `<dir>${xml(d)}</dir>`);
    if (BUNDLE_DIR.test(conf)) {
      conf = conf.replace(BUNDLE_DIR, () => [...mine, "<dir>@FONTS_DIR@</dir>", ...after].join("\n  "));
    } else { // a template without the usual line: still list everything, the bundle's place unknown
      conf = conf.replace("</fontconfig>", `  ${[...mine, ...after].join("\n  ")}\n</fontconfig>`);
    }
  }
  return conf.split("@FONTS_DIR@").join(fontsDir).split("@CACHE_DIR@").join(cacheDir);
}

function writeAtomic(path: string, text: string): void {
  const tmp = `${path}.${process.pid}.tmp`;
  writeFileSync(tmp, text);
  renameSync(tmp, path);
}

/**
 * Returns `{ FONTCONFIG_FILE }` on Linux when the font bundle is present, else `{}`.
 *
 * `fontDirs` (and CLEARCOTE_FONT_DIRS / CLEARCOTE_FALLBACK_FONT_DIRS) add directories: see the top of
 * this file. Without any, the file is `fonts.generated.conf` exactly as before; with some, each
 * distinct result gets its own `fonts.generated-<hash>.conf`, so launches with different directories
 * never rewrite each other's file. `args` is the engine's command line (see genuineFacesDisabled).
 */
/** Whether the launch turns genuine faces off itself (`--disable-genuine-font-faces` in `args`). The engine
 * then keeps every substitute, so the rules must stay as the template has them: measured on an engine with
 * the switch, retargeting anyway sent Helvetica to the genuine file while Arial stayed on its substitute. */
export function genuineFacesDisabled(args: readonly string[] | undefined): boolean {
  const flag = `--${GENUINE_FACES_SWITCH}`;
  return (args ?? []).some((a) => typeof a === "string" && (a === flag || a.startsWith(`${flag}=`)));
}

export function linuxFontEnv(exePath: string, fontDirs?: unknown, args?: readonly string[]): Record<string, string> {
  if (process.platform !== "linux") return {};
  const { user, fallback } = resolveFontDirs(fontDirs);
  const fontsDir = join(dirname(exePath), "fonts");
  const template = join(fontsDir, "fonts.conf.template");
  if (!existsSync(template)) return {};
  try {
    const cacheDir = join(tmpdir(), "cc-fc-cache");
    mkdirSync(cacheDir, { recursive: true });
    // The rules follow your fonts only on an engine that draws them (GENUINE_FACES_SWITCH).
    const genuine = user.length && engineHonoursGenuineFaces(exePath) && !genuineFacesDisabled(args)
      ? dirFamilies(user) : new Set<string>();
    const conf = buildConf(readFileSync(template, "utf8"), fontsDir, cacheDir, user, fallback, genuine);
    const name = user.length || fallback.length
      ? `fonts.generated-${createHash("sha256").update(conf, "utf8").digest("hex").slice(0, 12)}.conf`
      : "fonts.generated.conf";
    const confPath = join(fontsDir, name);
    writeAtomic(confPath, conf);
    return { FONTCONFIG_FILE: confPath };
  } catch {
    return {}; // never block a launch on font wiring
  }
}

export interface FontReport {
  bundled: boolean;
  note: string;
  scripts?: { covered: string[]; missing: string[] };
  fontDirs?: string[];
  fallbackFontDirs?: string[];
  ignoredFontDirs?: string[];
  genuineWindowsFamilies?: string[];
  lookalikeWindowsFamilies?: string[];
  genuineFacesSupported?: boolean;
}

/** What `clearcote info` says about fonts on Linux (the `fonts` key of its JSON): whether the build
 * bundles fonts, which scripts the fonts a launch would see can draw, and which of the key Windows
 * families your own fonts provide for real. `undefined` when the build ships no bundle. */
export function linuxFontReport(exePath: string, fontDirs?: unknown,
  env: Record<string, string | undefined> = process.env): FontReport | undefined {
  const fontsDir = join(dirname(exePath), "fonts");
  if (!existsSync(join(fontsDir, "fonts.conf.template"))) return undefined;
  const { user, fallback, ignored } = resolveFontDirs(fontDirs, env);
  const wanted = SCRIPT_SAMPLES.map(([, cp]) => cp);
  const covered = new Set<number>(), color = new Set<number>();
  for (const path of fontFiles([fontsDir, ...user, ...fallback])) {
    const f = readFont(path, wanted);
    for (const c of f.covered) {
      covered.add(c);
      if (f.color) color.add(c);
    }
  }
  const emoji = SCRIPT_SAMPLES.find(([name]) => name === "Emoji")![1];
  const ok = SCRIPT_SAMPLES.filter(([, cp]) => (cp === emoji ? color : covered).has(cp)).map(([name]) => name);
  const report: FontReport = {
    bundled: true,
    note: "metric-compatible Windows font clones are bundled with this build",
    scripts: { covered: ok, missing: SCRIPT_SAMPLES.map(([name]) => name).filter((n) => !ok.includes(n)) },
    fontDirs: user,
    fallbackFontDirs: fallback,
  };
  if (ignored.length) report.ignoredFontDirs = ignored;
  if (user.length) {
    const genuine = dirFamilies(user);
    report.genuineWindowsFamilies = KEY_WINDOWS_FAMILIES.filter((f) => genuine.has(f.toLowerCase()));
    report.lookalikeWindowsFamilies = KEY_WINDOWS_FAMILIES.filter((f) => !genuine.has(f.toLowerCase()));
    report.genuineFacesSupported = engineHonoursGenuineFaces(exePath);
  }
  return report;
}

/** The Linux font lines of `clearcote info` (same text as the Python CLI). */
export function fontLines(f: FontReport): string[] {
  const lines = [`Fonts           ${f.note}`];
  if (f.scripts) {
    const total = f.scripts.covered.length + f.scripts.missing.length;
    lines.push(f.scripts.missing.length
      ? `Scripts         ${f.scripts.covered.length} of ${total} render; no font for ${f.scripts.missing.join(", ")} (they draw as empty boxes)`
      : `Scripts         all ${total} render`);
  }
  for (const d of f.fontDirs ?? []) lines.push(`Your fonts      ${d}`);
  if (f.fontDirs?.length) {
    const genuine = f.genuineWindowsFamilies ?? [], lookalike = f.lookalikeWindowsFamilies ?? [];
    lines.push(`                real: ${genuine.length ? genuine.join(", ") : "none of the key Windows families"}`);
    if (lookalike.length) lines.push(`                lookalike: ${lookalike.join(", ")}`);
    if (genuine.length && f.genuineFacesSupported === false) {
      lines.push("                this engine still draws its lookalikes for them (real faces need PRO r32 or newer)");
    }
  }
  for (const d of f.fallbackFontDirs ?? []) lines.push(`Fallback fonts  ${d}`);
  for (const d of f.ignoredFontDirs ?? []) lines.push(`Ignored         ${d} (not a directory)`);
  return lines;
}

type EnvMap = { [key: string]: string | undefined };

/**
 * `{ LANGUAGE: <ui locale> }` on Linux when `args` pin a UI locale with `--lang`, else `{}`.
 *
 * Chrome on Linux takes its UI locale (browser strings such as a form's validationMessage, and
 * before r29 also Intl) from the environment -- LANGUAGE, LC_ALL, LC_MESSAGES, LANG, in GLib's
 * order -- and engines before 153 r29 ignore `--lang` there, so a German persona on a Linux host
 * still spoke English. LANGUAGE is read first and only steers message catalogues, so it fixes that
 * without touching the C library locale (LANG/LC_* would also change number parsing and
 * fontconfig's default language). `de` -> `de`, `en-GB` -> `en_GB`.
 */
export function linuxLocaleEnv(args: readonly string[] | undefined, platform: string = process.platform): Record<string, string> {
  if (platform !== "linux") return {};
  let lang: string | undefined;
  for (const arg of args ?? []) {
    if (typeof arg === "string" && arg.startsWith("--lang=")) lang = arg.slice("--lang=".length).trim(); // last wins, as in Chromium
  }
  return lang ? { LANGUAGE: lang.replace(/-/g, "_") } : {};
}

/**
 * Build the `env` to pass to Playwright's launch so the bundled fonts resolve (and, from `args`,
 * the Linux UI-locale LANGUAGE -- see linuxLocaleEnv).
 * Merges process.env (Playwright replaces the env when `env` is set, so we must include it),
 * the bundled-font FONTCONFIG_FILE + locale, then any caller-supplied `env` (caller wins).
 * Returns `undefined` when there is nothing to add (preserve Playwright's default env).
 */
export function fontLaunchEnv(exePath: string, userEnv?: EnvMap, args?: readonly string[], fontDirs?: readonly string[]): EnvMap | undefined {
  const fontEnv = { ...linuxFontEnv(exePath, fontDirs, args), ...linuxLocaleEnv(args) };
  if (Object.keys(fontEnv).length === 0 && !userEnv) return undefined;
  return { ...process.env, ...fontEnv, ...(userEnv ?? {}) };
}
