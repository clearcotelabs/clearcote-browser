// fingerprint: "off" (pass-through) shows the host.
//
// From engine r51 a pass-through launch still carries a persona token, and the engine serves that token's GPU
// and voices unless --disable-gpu-fingerprint and --disable-fingerprint-voices say otherwise; the default geoip
// lookup filled a language, timezone and WebRTC IP from the IP address, which went out as if the caller had set
// them. Measured on r60 (2026-11-16): a Windows host with an RTX 3070 set to en-GB answered "GTX 1660 ... OpenGL
// 4.5.0", no voices and nl-NL from a Dutch address. So pass-through sends the two switches itself and skips the
// default lookup.
//
// Hermetic: Playwright and the geoip resolver are stand-ins and the "engine" is a small file. Mirrors
// sdk/python/tests/test_passthrough.py.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stub = vi.hoisted(() => ({
  // PROFILE_DIR is read once, at import: point it somewhere empty before the SDK loads, so no saved
  // profile on this machine can change what these tests see.
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  launches: [] as Array<Record<string, unknown>>,
  spawns: [] as string[][],
}));

vi.mock("playwright-core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("playwright-core")>();
  const { EventEmitter } = await import("node:events");
  const fakeContext = () => {
    const ctx = new EventEmitter() as EventEmitter & Record<string, unknown>;
    ctx.pages = () => [];
    ctx.browser = () => null;
    ctx.addInitScript = async () => {};
    ctx.close = async () => { ctx.emit("close", ctx); };
    return ctx;
  };
  const chromium = {
    launchPersistentContext: async (_userDataDir: string, opts: Record<string, unknown>) => { stub.launches.push(opts); return fakeContext(); },
    launch: async (opts: Record<string, unknown>) => {
      stub.launches.push(opts);
      const browser = new EventEmitter() as EventEmitter & Record<string, unknown>;
      browser.contexts = () => [];
      browser.newPage = async () => ({});
      browser.newContext = async () => fakeContext();
      browser.close = async () => {};
      return browser;
    },
  };
  return { ...actual, chromium };
});

vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  const { EventEmitter } = await import("node:events");
  return {
    ...actual,
    spawn: (_cmd: string, args: string[]) => {
      stub.spawns.push(args);
      const p = new EventEmitter() as EventEmitter & Record<string, unknown>;
      p.exitCode = null;
      p.killed = false;
      p.pid = 4242;
      p.kill = () => { p.killed = true; return true; };
      queueMicrotask(() => p.emit("spawn"));
      return p;
    },
  };
});

import { fingerprintArgs, PASSTHROUGH_HOST_SWITCHES, withoutCallerRepeats } from "../src/fingerprint.js";
import { geoipMode, geoResolver, resetDirectGeoCache, type GeoResult } from "../src/geoip.js";
import { gateEngineSwitches } from "../src/launchopts.js";
import { SELECTOR_SWITCHES, selectorFromArgs } from "../src/persona.js";
import { applyGeoipBeforeStart } from "../src/docker.js";
import { resetSeenWarnings } from "../src/warnings.js";
import { launch, launchPersistentContext } from "../src/index.js";

const PASSTHROUGH = "--fingerprint-passthrough";
const GPU = "--disable-gpu-fingerprint";
const VOICES = "--disable-fingerprint-voices";
const GEO = { ip: "203.0.113.7", country: "NL", timezone: "Europe/Amsterdam", acceptLanguage: "nl-NL", location: "52.37,4.89" };
// 152 r22 brought pass-through and the voices switch; the GPU switch is in every engine.
const ENGINE_R22 = ["fingerprint-passthrough", "disable-fingerprint-voices", "disable-gpu-fingerprint", "allow-third-party-cookies"];
const ENGINE_OLD = ["disable-gpu-fingerprint"];

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

// No licence (a key would take a lease), the geoip default ON (the suite's setup turns it off), and a TEMP of
// our own: everything a test or the code under test creates goes under `root`, removed after.
const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_GEOIP",
  "CLEARCOTE_PERSONA_ENV"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let asked: unknown[];
let answer: GeoResult;
const realResolve = geoResolver.resolve;

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-passthrough-"));
  for (const d of ["temp", "home"]) mkdirSync(join(root, d));
  process.env.HOME = process.env.USERPROFILE = join(root, "home");
  process.env.TEMP = process.env.TMP = process.env.TMPDIR = join(root, "temp");
  for (const k of ENV_KEYS.slice(5)) delete process.env[k];
  stub.launches.length = 0;
  stub.spawns.length = 0;
  asked = [];
  answer = { geo: GEO, elapsedMs: 5 };
  geoResolver.resolve = async (proxy) => { asked.push(proxy); return answer; };
  resetDirectGeoCache();
  resetSeenWarnings();
});

afterEach(() => {
  geoResolver.resolve = realResolve;
  resetDirectGeoCache();
  vi.restoreAllMocks();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

/** A stand-in engine: the probe reads the NUL-delimited switch names in it. */
function fakeEngine(switches: string[], name: string): string {
  const dir = join(root, `engine-${name}`);
  mkdirSync(dir, { recursive: true });
  const p = join(dir, "chrome");
  writeFileSync(p, Buffer.from(`\x7fELF\0${switches.join("\0")}\0--remote-debugging-port\0`, "latin1"));
  return p;
}

const count = (args: string[], name: string) => args.filter((a) => a === name).length;
const named = (args: string[], prefix: string) => args.filter((a) => a.startsWith(prefix));

/** Launch through `entry`; returns the arguments the browser was started with. */
async function launchArgs(entry: string, options: Record<string, unknown>): Promise<string[]> {
  if (entry === "launch") await (await launch(options as never)).close();
  else if (entry === "launch-incognito") await (await launch({ ...options, ephemeralProfile: false } as never)).close();
  else if (entry === "persistent") await (await launchPersistentContext(join(root, "udd"), options as never)).close();
  else throw new Error(entry);
  return stub.launches.at(-1)!.args as string[];
}

describe("the switches a pass-through launch sends", () => {
  it("sends the host GPU and host voices switches next to --fingerprint-passthrough", () => {
    expect(fingerprintArgs({ fingerprint: "off" })).toEqual([PASSTHROUGH, GPU, VOICES]);
    expect(PASSTHROUGH_HOST_SWITCHES).toEqual([GPU, VOICES]);
  });

  it("both are selector switches, so the persona token is issued for them", () => {
    for (const s of PASSTHROUGH_HOST_SWITCHES) expect(SELECTOR_SWITCHES.has(s.slice(2))).toBe(true);
    expect(selectorFromArgs(fingerprintArgs({ fingerprint: "off" })).sel).toEqual({ "disable-gpu-fingerprint": "", "disable-fingerprint-voices": "" });
  });

  it("the options that send the same switches under a persona add no second copy", () => {
    expect(fingerprintArgs({ fingerprint: "off", disableGpuFingerprint: true, fingerprintVoices: false })).toEqual([PASSTHROUGH, GPU, VOICES]);
  });

  it("a persona launch is unchanged: neither switch unless asked for", () => {
    const args = fingerprintArgs({ fingerprint: "seed-1" });
    expect(args).not.toContain(GPU);
    expect(args).not.toContain(VOICES);
  });

  it("withoutCallerRepeats drops only the host switches the caller passed", () => {
    const fp = [PASSTHROUGH, GPU, VOICES, "--timezone=Europe/Berlin"];
    expect(withoutCallerRepeats(fp, [])).toEqual(fp);
    expect(withoutCallerRepeats(fp, [GPU])).toEqual([PASSTHROUGH, VOICES, "--timezone=Europe/Berlin"]);
    expect(withoutCallerRepeats(fp, [VOICES, GPU, "--timezone=UTC", PASSTHROUGH])).toEqual([PASSTHROUGH, "--timezone=Europe/Berlin"]);
  });

  it.each(["launch", "launch-incognito", "persistent"])("%s: each switch is on the command line exactly once", async (entry) => {
    const exe = fakeEngine(ENGINE_R22, "r22");
    let args = await launchArgs(entry, { executablePath: exe, headless: false, quiet: true, fingerprint: "off" });
    for (const s of [PASSTHROUGH, GPU, VOICES]) expect(count(args, s)).toBe(1);
    expect(named(args, "--fingerprint=")).toEqual([]);
    expect(named(args, "--fingerprint-platform")).toEqual([]);
    // the caller passed them too: still once
    args = await launchArgs(entry, { executablePath: exe, headless: false, quiet: true, fingerprint: "off", args: [GPU, VOICES] });
    for (const s of [PASSTHROUGH, GPU, VOICES]) expect(count(args, s)).toBe(1);
  });
});

describe("older engines", () => {
  it("an engine without pass-through gets one warning, about pass-through, and keeps the GPU switch", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(fakeEngine(ENGINE_OLD, "old"), fingerprintArgs({ fingerprint: "off" }), false);
    expect(r.args).toEqual([GPU]);
    expect(r.warnings).toHaveLength(1);
    expect(r.warnings[0]).toMatch(/fingerprint: "off" \(pass-through debug mode\) needs engine 152 r22/);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("fingerprintVoices: false under a persona still warns there", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(fakeEngine(ENGINE_OLD, "old"), ["--fingerprint=seed-1", VOICES], false);
    expect(r.args).toEqual(["--fingerprint=seed-1"]);
    expect(r.warnings).toHaveLength(1);
    expect(r.warnings[0]).toMatch(/fingerprintVoices: false needs engine 152 r22/);
  });

  it("the voices switch pass-through brings along never gets a warning of its own", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const r = gateEngineSwitches(fakeEngine(["fingerprint-passthrough", "disable-gpu-fingerprint"], "half"), fingerprintArgs({ fingerprint: "off" }), false);
    expect(r.args).toEqual([PASSTHROUGH, GPU]);
    expect(r.warnings).toEqual([]);
    expect(warn).not.toHaveBeenCalled();
  });

  it("an engine with pass-through keeps all three, without a warning", () => {
    const r = gateEngineSwitches(fakeEngine(ENGINE_R22, "r22"), fingerprintArgs({ fingerprint: "off" }), false);
    expect(r.args).toEqual([PASSTHROUGH, GPU, VOICES]);
    expect(r.warnings).toEqual([]);
  });
});

describe("nothing taken from the IP address", () => {
  it("geoipMode: pass-through turns the default lookup off, and geoip: true still asks for it", () => {
    expect(geoipMode(undefined, {}, true)).toBe("off");
    expect(geoipMode(undefined, { CLEARCOTE_GEOIP: "1" }, true)).toBe("off");
    expect(geoipMode(false, {}, true)).toBe("off");
    expect(geoipMode(true, {}, true)).toBe("strict");
    expect(geoipMode(undefined, {}, false)).toBe("auto");
    expect(geoipMode(undefined, {})).toBe("auto");
  });

  it.each(["launch", "launch-incognito", "persistent"])("%s: a persona launch looks the address up, pass-through does not", async (entry) => {
    process.env.CLEARCOTE_GEOIP = "1";
    const exe = fakeEngine(ENGINE_R22, "r22");
    let args = await launchArgs(entry, { executablePath: exe, headless: false, quiet: true, fingerprint: "seed-1" });
    expect(asked).toHaveLength(1);
    expect(args).toContain("--accept-lang=nl-NL,nl,en-US,en");
    expect(args).toContain("--timezone=Europe/Amsterdam");
    expect(args).toContain("--webrtc-ip=203.0.113.7");

    asked = [];
    resetDirectGeoCache();
    args = await launchArgs(entry, { executablePath: exe, headless: false, quiet: true, fingerprint: "off" });
    expect(asked).toEqual([]);
    for (const p of ["--accept-lang=", "--lang=", "--timezone=", "--webrtc-ip=", "--fingerprint-location=", "--webrtc-direct-stun"]) expect(named(args, p)).toEqual([]);
  });

  it("the lookup a persona launch cached in this process is not applied either", async () => {
    process.env.CLEARCOTE_GEOIP = "1";
    const exe = fakeEngine(ENGINE_R22, "r22");
    await launchArgs("persistent", { executablePath: exe, headless: false, quiet: true, fingerprint: "seed-1" });
    const args = await launchArgs("persistent", { executablePath: exe, headless: false, quiet: true, fingerprint: "off" });
    expect(asked).toHaveLength(1);
    for (const p of ["--accept-lang=", "--timezone=", "--webrtc-ip="]) expect(named(args, p)).toEqual([]);
  });

  it("what the caller set is kept", async () => {
    process.env.CLEARCOTE_GEOIP = "1";
    const exe = fakeEngine(ENGINE_R22, "r22");
    const args = await launchArgs("persistent", { executablePath: exe, headless: false, quiet: true, fingerprint: "off",
      timezone: "Europe/Berlin", acceptLanguage: "de-DE", webrtcIp: "198.51.100.4" });
    expect(asked).toEqual([]);
    expect(args).toContain("--timezone=Europe/Berlin");
    expect(args).toContain("--accept-lang=de-DE,de,en-US,en");
    expect(args).toContain("--webrtc-ip=198.51.100.4");
  });

  it("geoip: true is the caller asking for the lookup: it runs and fills what is unset", async () => {
    const exe = fakeEngine(ENGINE_R22, "r22");
    const args = await launchArgs("persistent", { executablePath: exe, headless: false, quiet: true, fingerprint: "off", geoip: true, timezone: "Europe/Berlin" });
    expect(asked).toHaveLength(1);
    expect(args).toContain("--timezone=Europe/Berlin");
    expect(args).toContain("--accept-lang=nl-NL,nl,en-US,en");
    expect(args).toContain("--webrtc-ip=203.0.113.7");
  });

  it("the Docker launch does not look the address up for pass-through either", async () => {
    process.env.CLEARCOTE_GEOIP = "1";
    const off: Record<string, unknown> = { fingerprint: "off", quiet: true };
    await applyGeoipBeforeStart(off);
    expect(asked).toEqual([]);
    expect(off).toEqual({ fingerprint: "off", quiet: true });
    const seeded: Record<string, unknown> = { fingerprint: "seed-1", quiet: true };
    await applyGeoipBeforeStart(seeded);
    expect(asked).toHaveLength(1);
    expect(seeded.timezone).toBe("Europe/Amsterdam");
  });
});
