// realComputer: this computer's screen / touch / display language instead of the persona's generated values.
// Parsers for each operating system's answer, the option rules, and the cloud / Docker handling.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  parseWindows, parseMac, parseXrandr, plausibleDisplay, realComputerParts, withRealComputer, readHostFacts,
  resetHostFacts, REAL_COMPUTER_WARN, type HostFacts,
} from "../src/realcomputer.js";
import { resetSeenWarnings } from "../src/warnings.js";
import { fingerprintArgs, FINGERPRINT_KEYS } from "../src/fingerprint.js";
import { containerEnv } from "../src/docker.js";
import { LOCAL_ONLY_OPTIONS } from "../src/cloud.js";

const FACTS: HostFacts = {
  display: { width: 3440, height: 1440, availWidth: 3440, availHeight: 1392, devicePixelRatio: 1 },
  touch: 2,
  language: "en-GB",
};
const read = (f: HostFacts = FACTS) => vi.fn(async () => f);

let stderr: string[];
beforeEach(() => {
  resetSeenWarnings();
  resetHostFacts();
  stderr = [];
  vi.spyOn(process.stderr, "write").mockImplementation(((c: unknown) => { stderr.push(String(c)); return true; }) as typeof process.stderr.write);
});
afterEach(() => vi.restoreAllMocks());

describe("parsers", () => {
  it("Windows: physical px divided by the system scaling, touch points and the display language", () => {
    expect(parseWindows("3440 1440 3440 1392 96 2 en-GB")).toEqual(FACTS);
    // a 1080p laptop at 125%: 1920x1080 physical, taskbar 48 physical px -> 1536x864, work area 1536x826 (rounded)
    expect(parseWindows("1920 1080 1920 1032 120 0 de-DE")).toEqual({
      display: { width: 1536, height: 864, availWidth: 1536, availHeight: 826, devicePixelRatio: 1.25 }, touch: 0, language: "de-DE",
    });
  });
  it("Windows: a broken answer gives nothing rather than a guess", () => {
    expect(parseWindows("")).toEqual({ display: null, touch: null, language: null });
    expect(parseWindows("abc def")).toEqual({ display: null, touch: null, language: null });
    expect(parseWindows("640 300 640 300 96 0 en-US").display).toBeNull(); // implausibly small
  });
  it("macOS: points are CSS px, the Retina factor is the ratio, touch is 0 as Chrome reports", () => {
    expect(parseMac("1512 982 1512 944 2 en-GB")).toEqual({
      display: { width: 1512, height: 982, availWidth: 1512, availHeight: 944, devicePixelRatio: 2 }, touch: 0, language: "en-GB",
    });
  });
  it("Linux: the primary xrandr output, else the first connected one", () => {
    const out = [
      "Screen 0: minimum 8 x 8, current 4480 x 1440, maximum 32767 x 32767",
      "HDMI-1 connected 1920x1080+2560+0 (normal left inverted right x axis y axis) 527mm x 296mm",
      "DP-1 connected primary 2560x1440+0+0 (normal left inverted right x axis y axis) 597mm x 336mm",
    ].join("\n");
    expect(parseXrandr(out)).toEqual({ width: 2560, height: 1440, availWidth: 2560, availHeight: 1440, devicePixelRatio: 1 });
    expect(parseXrandr("eDP-1 connected 1366x768+0+0 (normal)")).toMatchObject({ width: 1366, height: 768 });
    expect(parseXrandr("Screen 0: current 1024 x 768\nVGA-1 disconnected")).toBeNull();
  });
  it("plausibility: work area inside the screen, ratio from 1 to 4", () => {
    expect(plausibleDisplay({ width: 1920, height: 1080, availWidth: 2000, availHeight: 1040, devicePixelRatio: 1 })).toBeNull();
    expect(plausibleDisplay({ width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, devicePixelRatio: 5 })).toBeNull();
  });
  it("Linux without a display server: no screen, the language from the environment, no touch value", async () => {
    const f = await readHostFacts("linux", { LANG: "fr_FR.UTF-8" });
    expect(f).toEqual({ display: null, touch: null, language: "fr-FR" });
  });
});

describe("realComputerParts", () => {
  it("true is every part; an object picks; anything else is off", () => {
    expect(realComputerParts(true)).toEqual({ screen: true, touch: true, languages: true });
    expect(realComputerParts({ screen: true })).toEqual({ screen: true, touch: false, languages: false });
    expect(realComputerParts(false)).toEqual({ screen: false, touch: false, languages: false });
    expect(realComputerParts(undefined)).toEqual({ screen: false, touch: false, languages: false });
  });
});

describe("withRealComputer", () => {
  it("off: unchanged, nothing read", async () => {
    const r = read();
    const o = { fingerprint: "a1" };
    expect(await withRealComputer(o, { read: r })).toBe(o);
    expect(r).not.toHaveBeenCalled();
  });
  it("true: screen, touch and language from this computer; the input is not mutated", async () => {
    const o = { fingerprint: "a1", realComputer: true };
    const out = await withRealComputer(o, { read: read() });
    expect(out).toMatchObject({ screenWidth: 3440, screenHeight: 1440, availWidth: 3440, availHeight: 1392, devicePixelRatio: 1, maxTouchPoints: 2, acceptLanguage: "en-GB" });
    expect(o).toEqual({ fingerprint: "a1", realComputer: true });
  });
  it("picks only the parts asked for", async () => {
    const out = await withRealComputer({ fingerprint: "a1", realComputer: { screen: true } }, { read: read() });
    expect(out).toMatchObject({ screenWidth: 3440 });
    expect(out).not.toHaveProperty("maxTouchPoints");
    expect(out).not.toHaveProperty("acceptLanguage");
  });
  it("a value the caller set wins", async () => {
    const out = await withRealComputer({ fingerprint: "a1", realComputer: true, screenWidth: 1920, screenHeight: 1080, maxTouchPoints: 0, acceptLanguage: "de-DE" }, { read: read() });
    expect(out).toMatchObject({ screenWidth: 1920, screenHeight: 1080, maxTouchPoints: 0, acceptLanguage: "de-DE" });
    expect(out).not.toHaveProperty("availWidth");
  });
  it("behind a proxy with geoip on, the exit keeps the languages", async () => {
    const out = await withRealComputer({ fingerprint: "a1", realComputer: true }, { read: read(), proxied: true });
    expect(out).toMatchObject({ screenWidth: 3440, maxTouchPoints: 2 });
    expect(out).not.toHaveProperty("acceptLanguage");
  });
  it("pass-through already shows this computer: nothing added, nothing read", async () => {
    const r = read();
    const o = { fingerprint: "off", realComputer: true };
    expect(await withRealComputer(o, { read: r })).toBe(o);
    expect(r).not.toHaveBeenCalled();
  });
  it("what cannot be read stays generated, and the launch says so once", async () => {
    const none = read({ display: null, touch: null, language: "en-US" });
    const out = await withRealComputer({ fingerprint: "a1", realComputer: true }, { read: none });
    expect(out).not.toHaveProperty("screenWidth");
    expect(out).not.toHaveProperty("maxTouchPoints");
    expect(out).toMatchObject({ acceptLanguage: "en-US" });
    await withRealComputer({ fingerprint: "a1", realComputer: true }, { read: none });
    const notes = stderr.filter((l) => l.includes("realComputer:"));
    expect(notes).toHaveLength(1);
    expect(notes[0]).toContain("screen and touch");
    expect(REAL_COMPUTER_WARN).toBe("real-computer-partial");
  });
  it("quiet: no note", async () => {
    await withRealComputer({ fingerprint: "a1", realComputer: true }, { read: read({ display: null, touch: null, language: null }), quiet: true });
    expect(stderr.filter((l) => l.includes("realComputer:"))).toHaveLength(0);
  });
});

describe("the filled values reach the engine through the existing switches", () => {
  it("screen, ratio and touch switches", async () => {
    const o = await withRealComputer({ fingerprint: "a1", realComputer: true }, { read: read() });
    const args = fingerprintArgs(o as Record<string, unknown>);
    for (const a of ["--fingerprint-screen-width=3440", "--fingerprint-screen-height=1440", "--fingerprint-avail-width=3440",
      "--fingerprint-avail-height=1392", "--fingerprint-device-pixel-ratio=1"]) expect(args).toContain(a);
    expect(args).toContain("--fingerprint-max-touch-points=2");
    expect(args.some((a) => a.startsWith("--accept-lang=en-GB"))).toBe(true);
  });
});

describe("cloud and Docker", () => {
  it("is a local-only option", () => {
    expect(FINGERPRINT_KEYS).toContain("realComputer");
    expect(LOCAL_ONLY_OPTIONS).toContain("realComputer");
  });
  it("Docker drops realComputer: false and refuses true", () => {
    expect(() => containerEnv({ realComputer: false })).not.toThrow();
    expect(() => containerEnv({ realComputer: true })).toThrow(/realComputer is not available/);
  });
});
