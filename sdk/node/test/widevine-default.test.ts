// Widevine ON by default for licensed (PRO) launches, opt-in for free ones (0.48.0).
//
// Hermetic: no network. Google's update check and the CDM download answer from a stubbed global fetch,
// the CDM cache is a temp directory (CLEARCOTE_WIDEVINE_DIR), Playwright and the child-process spawn are
// stand-ins that record what they were asked to launch, the licence backend is replaced (acquireLease
// answers null, so a licensed launch takes no lease), and the "engine" is a small file carrying (or not)
// the `widevine-cdm-path` switch name the probe looks for. Mirrors sdk/python/tests/test_widevine_default.py.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { createHash } from "node:crypto";
import { createServer, type Server as HttpServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { storedZip } from "./helpers/fake-build.js";

const stub = vi.hoisted(() => ({
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  launches: [] as Array<{ kind: "persistent" | "incognito"; userDataDir?: string; opts: Record<string, unknown> }>,
  spawns: [] as Array<{ args: string[] }>,
  cloudCalls: [] as Array<Record<string, unknown>>,
  measured: [] as Array<Record<string, unknown>>,
}));

vi.mock("playwright-core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("playwright-core")>();
  const { EventEmitter } = await import("node:events");
  const fakeContext = () => {
    const ctx = new EventEmitter() as EventEmitter & Record<string, unknown>;
    ctx.pages = () => [];
    ctx.browser = () => null;
    ctx.close = async () => { ctx.emit("close", ctx); };
    return ctx;
  };
  const chromium = {
    launchPersistentContext: async (userDataDir: string, opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "persistent", userDataDir, opts });
      return fakeContext();
    },
    launch: async (opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "incognito", opts });
      const browser = new EventEmitter() as EventEmitter & Record<string, unknown>;
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
      stub.spawns.push({ args });
      const p = new EventEmitter() as EventEmitter & Record<string, unknown>;
      p.exitCode = null;
      p.killed = false;
      p.pid = 3999999; // no such process: the close's exit wait ends at once
      p.kill = () => { p.killed = true; p.exitCode = 0; return true; };
      queueMicrotask(() => p.emit("spawn"));
      return p;
    },
  };
});

// A licensed launch resolves the key (the PRO selection) but takes no lease: the backend is a stand-in.
vi.mock("../src/license.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/license.js")>();
  return { ...actual, acquireLease: async () => null };
});

vi.mock("../src/cloud.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/cloud.js")>();
  return { ...actual, launchCloud: async (o: Record<string, unknown>) => { stub.cloudCalls.push(o); return { close: async () => {} }; } };
});

vi.mock("../src/profilesource.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/profilesource.js")>();
  return {
    ...actual,
    // profile: "auto" measures the host with a nested launch: run it, then stop the outer launch there
    measureHost: async (launcher: (o: Record<string, unknown>) => Promise<{ close: () => Promise<void> }>, exe: string) => {
      const b = await launcher({ executablePath: exe, headless: true, quiet: true }); // as the real one calls it
      await b.close();
      throw new Error("stop after the host probe");
    },
  };
});

import * as W from "../src/widevine.js";
import { resetSeenWarnings } from "../src/warnings.js";
import { LOCAL_ONLY_OPTIONS } from "../src/cloud.js";
import { launch, launchPersistentContext, serve } from "../src/index.js";

// An engine with the --widevine-cdm-path switch (1022) AND CDM host verification (r63+): the licensed default turns
// Widevine on on every host. ENGINE_1022_UNVERIFIED (r32..r62) keeps the default off on Windows and macOS hosts.
const ENGINE_1022 = "\0widevine-cdm-path\0VerifyCdmHost_0\0";
const ENGINE_1022_UNVERIFIED = "\0widevine-cdm-path\0";
const VERSION = "4.10.9999.0";
const KEY = "cc_lic_test_widevine_default";
const LINUX = process.platform === "linux";
const WIN_OR_MAC = process.platform === "win32" || process.platform === "darwin";
const SUB = LINUX ? "linux_x64" : "win_x64";
const CDM_FILE = LINUX ? "libwidevinecdm.so" : "widevinecdm.dll";
const DOWNLOAD_URL = "https://example.invalid/cdm.crx3";

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_API_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_WIDEVINE_DIR",
  "CLEARCOTE_WIDEVINE", "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "CLEARCOTE_PERSONA_ENV"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let wv: string;
let realFetch: typeof fetch;
let stderr: string[];

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-wv-default-"));
  for (const d of ["temp", "home"]) mkdirSync(join(root, d));
  process.env.HOME = join(root, "home");
  process.env.USERPROFILE = join(root, "home");
  process.env.TEMP = process.env.TMP = process.env.TMPDIR = join(root, "temp");
  wv = join(root, "wv");
  process.env.CLEARCOTE_WIDEVINE_DIR = wv;
  for (const k of ["CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD",
    "CLEARCOTE_DOCKER", "CLEARCOTE_WIDEVINE", "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", "CLEARCOTE_PERSONA_ENV"]) delete process.env[k];
  stub.launches.length = 0;
  stub.spawns.length = 0;
  stub.cloudCalls.length = 0;
  W.resetWidevineDefaultState();
  resetSeenWarnings();
  realFetch = globalThis.fetch;
  // Any network call a test did not set up fails loudly.
  globalThis.fetch = (async (url: unknown) => { throw new Error(`unexpected fetch ${String(url)}`); }) as typeof fetch;
  stderr = [];
  vi.spyOn(process.stderr, "write").mockImplementation(((chunk: unknown) => { stderr.push(String(chunk)); return true; }) as typeof process.stderr.write);
});

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = realFetch;
  W.resetWidevineDefaultState();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

function fakeEngine(markers: string[] = [], name = "chrome"): string {
  const dir = join(root, `engine-${name}`);
  mkdirSync(dir, { recursive: true });
  const p = join(dir, name);
  writeFileSync(p, Buffer.from(`\x7fELF${markers.join("")}\0--remote-debugging-port\0`, "latin1"));
  if (process.platform === "win32") writeFileSync(join(dir, "chrome.dll"), Buffer.from(`MZ${markers.join("")}\0`, "latin1"));
  return p;
}

/** A CDM already in the cache; `stamp`: Google was asked just now. */
function cachedCdm(version = VERSION, { stamp = true, complete = true } = {}): string {
  const dir = join(wv, version);
  mkdirSync(join(dir, "_platform_specific", SUB), { recursive: true });
  writeFileSync(join(dir, "manifest.json"), "{}");
  if (complete) writeFileSync(join(dir, "_platform_specific", SUB, CDM_FILE), "cdm");
  if (stamp) writeFileSync(join(wv, W.STAMP_FILE), "now");
  return dir;
}

function crx(files: Record<string, Buffer>): Buffer {
  const header = Buffer.from([1, 2, 3, 4]);
  const fixed = Buffer.alloc(12);
  fixed.write("Cr24", 0, "latin1");
  fixed.writeUInt32LE(3, 4);
  fixed.writeUInt32LE(header.length, 8);
  return Buffer.concat([fixed, header, storedZip(files)]);
}

const cdmCrx = () => crx({
  "manifest.json": Buffer.from(`{"version":"${VERSION}"}`),
  [`_platform_specific/${SUB}/${CDM_FILE}`]: Buffer.from("\x7fELF fake cdm"),
});

/** Google's update server + download host: counts calls, can fail, hang (until aborted) or serve a bad hash. */
function fakeGoogle(o: { blob?: Buffer; fail?: boolean; sha?: string; hang?: boolean; downloadGate?: Promise<void>; version?: string } = {}) {
  const blob = o.blob ?? cdmCrx();
  const sha = o.sha ?? createHash("sha256").update(blob).digest("hex");
  const g = { checks: 0, downloads: 0, aborted: 0 };
  globalThis.fetch = (async (url: unknown, init?: { signal?: AbortSignal }) => {
    const u = String(url);
    if (o.hang) {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => { g.aborted++; reject(new Error("aborted")); }, { once: true });
      });
    }
    if (u === W.OMAHA_URL) {
      g.checks++;
      if (o.fail) throw new TypeError("fetch failed: Google said 403");
      return new Response(JSON.stringify({ response: { app: [{ nextversion: o.version ?? VERSION, updatecheck: { status: "ok",
        pipelines: [{ operations: [{ urls: [{ url: DOWNLOAD_URL }], out: { sha256: sha } }] }] } }] } }), { status: 200 });
    }
    if (u === DOWNLOAD_URL) {
      g.downloads++;
      if (o.downloadGate) await o.downloadGate; // held until the test has looked at the cache mid-install
      return new Response(blob, { status: 200 });
    }
    throw new Error(`unexpected fetch ${u}`);
  }) as typeof fetch;
  return g;
}

const warnings = () => stderr.filter((l) => l.includes("Widevine is on by default for licensed launches"));
const cdmArgs = (args: unknown) => (args as string[]).filter((a) => a.startsWith("--widevine-cdm-path"));
const lastArgs = () => stub.launches.at(-1)!.opts.args as string[];
const base = (exe: string, extra: Record<string, unknown> = {}) => ({ executablePath: exe, headless: true, quiet: true, ...extra });
const licensed = (exe: string, extra: Record<string, unknown> = {}) => base(exe, { licenseKey: KEY, licenseApiBase: "http://test.local", ...extra });

// -- the rules ------------------------------------------------------------------------------------

describe("widevineMode", () => {
  it.each([
    [undefined, true, undefined, "default"],
    [undefined, false, undefined, "off"],
    [true, true, undefined, "explicit"],
    [true, false, undefined, "explicit"],
    [false, true, undefined, "off"],
    [false, false, undefined, "off"],
    [undefined, true, "0", "off"],
    [undefined, true, "false", "off"],
    [undefined, true, "OFF", "off"],
    [undefined, true, "no", "off"],
    [undefined, false, "1", "explicit"],
    [undefined, false, "true", "explicit"],
    [undefined, true, "on", "explicit"],
    [undefined, false, "yes", "explicit"],
    [true, true, "0", "explicit"], // an explicit option wins over the environment
    [false, false, "1", "off"],
    [undefined, true, "maybe", "default"], // an unknown value is ignored
    [undefined, false, "maybe", "off"],
    [undefined, true, "", "default"],
  ] as const)("option %s, licensed %s, CLEARCOTE_WIDEVINE=%s -> %s", (option, isLicensed, env, expected) => {
    expect(W.widevineMode(option, isLicensed, env === undefined ? {} : { CLEARCOTE_WIDEVINE: env })).toBe(expected);
  });

  it("reads the process environment", () => {
    process.env.CLEARCOTE_WIDEVINE = "0";
    expect(W.widevineMode(undefined, true)).toBe("off");
    process.env.CLEARCOTE_WIDEVINE = "1";
    expect(W.widevineMode(undefined, false)).toBe("explicit");
  });

  // clamped to 1-600 s: a huge value used to overflow setTimeout (to 1 ms) in Node and break the launch elsewhere
  it.each([[undefined, 15], ["3", 3], ["0.5", 1], ["0", 15], ["-2", 15], ["soon", 15], ["", 15], ["600", 600], ["601", 600],
    ["1e10", 600], ["Infinity", 600], ["NaN", 15], ["-Infinity", 15]] as const)(
    "timeout CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS=%s -> %s", (value, expected) => {
      expect(W.timeoutSeconds(value === undefined ? {} : { CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS: value })).toBe(expected);
    });

  it("uses the same names as the other SDKs", () => {
    expect(W.WIDEVINE_ENV).toBe("CLEARCOTE_WIDEVINE");
    expect(W.TIMEOUT_ENV).toBe("CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS");
    expect(W.DEFAULT_TIMEOUT_SECONDS).toBe(15);
  });
});

// -- the cache ------------------------------------------------------------------------------------

describe("the CDM cache", () => {
  it("picks the newest complete version", () => {
    expect(W.cachedCdm(wv)).toBeUndefined();
    cachedCdm("4.10.100.0");
    const newest = cachedCdm("4.10.2830.0");
    cachedCdm("4.10.9000.0", { complete: false }); // half-extracted: never used
    cachedCdm("current");
    mkdirSync(join(wv, ".tmp-abc"));
    expect(W.cachedCdm(wv)).toBe(newest);
  });

  it("installs, verifies and stamps; nothing else is left", async () => {
    const g = fakeGoogle();
    const dir = await W.fetchWidevine({ quiet: true });
    expect(dir).toBe(join(wv, VERSION));
    expect(existsSync(join(dir, "_platform_specific", SUB, CDM_FILE))).toBe(true);
    expect(readdirSync(wv).sort()).toEqual([W.STAMP_FILE, VERSION]);
    expect(g).toMatchObject({ checks: 1, downloads: 1 });
  });

  it("does not download a CDM it already has", async () => {
    const g = fakeGoogle();
    await W.fetchWidevine({ quiet: true });
    await W.fetchWidevine({ quiet: true });
    expect(g).toMatchObject({ checks: 2, downloads: 1 }); // fetchWidevine always asks; it downloads once
  });

  it("refuses a bad hash and leaves nothing", async () => {
    fakeGoogle({ sha: "00".repeat(32) });
    await expect(W.fetchWidevine({ quiet: true })).rejects.toThrow("sha256 mismatch");
    expect(W.cachedCdm(wv)).toBeUndefined();
    expect(readdirSync(wv).filter((n) => /^\.(tmp-|trash-|install-lock)/.test(n))).toEqual([]);
  });

  it("rejects a CRX without the CDM", async () => {
    fakeGoogle({ blob: crx({ "manifest.json": Buffer.from("{}") }) });
    await expect(W.fetchWidevine({ quiet: true })).rejects.toThrow(`${CDM_FILE} not at`);
    expect(W.cachedCdm(wv)).toBeUndefined();
  });

  it("replaces a half-extracted tree and sweeps an interrupted install", async () => {
    cachedCdm(VERSION, { stamp: false, complete: false }); // an older SDK extracted in place and died
    writeFileSync(join(wv, VERSION, "stray"), "x");
    mkdirSync(join(wv, ".tmp-dead"));
    fakeGoogle();
    const dir = await W.fetchWidevine({ quiet: true });
    expect(existsSync(join(dir, "_platform_specific", SUB, CDM_FILE))).toBe(true);
    expect(existsSync(join(dir, "stray"))).toBe(false);
    expect(readdirSync(wv).filter((n) => n.startsWith(".tmp-") || n.startsWith(".trash-"))).toEqual([]);
  });

  it("concurrent first fetches download once and never expose a partial CDM", async () => {
    let release!: () => void;
    const g = fakeGoogle({ downloadGate: new Promise<void>((r) => { release = r; }) });
    const all = Promise.all(Array.from({ length: 6 }, () => W.fetchWidevine({ quiet: true })));
    // held mid-download (no timing race): wait until the one installer is downloading
    for (let i = 0; i < 500 && g.downloads === 0; i++) await new Promise((r) => setTimeout(r, 10));
    expect(g.downloads).toBe(1);
    expect(W.cachedCdm(wv)).toBeUndefined(); // mid-install: nothing a reader could load yet
    expect(existsSync(join(wv, ".install-lock"))).toBe(true);
    release();
    const dirs = await all;
    expect(new Set(dirs)).toEqual(new Set([join(wv, VERSION)]));
    expect(g.downloads).toBe(1); // the others waited at the install lock, then found it installed
    expect(existsSync(join(wv, ".install-lock"))).toBe(false);
  });

  it("resolveCdm uses a fresh cache without any network", async () => {
    const dir = cachedCdm();
    expect(await W.resolveCdm({ quiet: true })).toBe(dir); // the default fetch stub throws on any call
  });

  it("resolveCdm re-checks a stale cache and keeps it when offline", async () => {
    const dir = cachedCdm(VERSION, { stamp: false });
    const g = fakeGoogle({ fail: true });
    expect(await W.resolveCdm({ quiet: true })).toBe(dir);
    expect(g.checks).toBe(1);
    // the failed check is recorded: no launch (in any process) asks again for a day
    expect(await W.resolveCdm({ quiet: true })).toBe(dir);
    expect(g.checks).toBe(1);
  });

  it("resolveCdm without a cache rejects when offline", async () => {
    fakeGoogle({ fail: true });
    await expect(W.resolveCdm({ quiet: true })).rejects.toThrow("403");
  });

  it("an old stamp is not fresh", async () => {
    cachedCdm();
    const old = new Date(Date.now() - W.CHECK_INTERVAL_MS - 60_000);
    utimesSync(join(wv, W.STAMP_FILE), old, old);
    const g = fakeGoogle();
    await W.resolveCdm({ quiet: true });
    expect(g.checks).toBe(1);
  });
});

// -- the default: bounded, fail-soft, warns once ---------------------------------------------------

describe("defaultCdm", () => {
  it("is fail-soft and warns once", async () => {
    const g = fakeGoogle({ fail: true });
    expect(await W.defaultCdm()).toBeUndefined();
    expect(await W.defaultCdm()).toBeUndefined(); // within the retry window: no second attempt
    expect(warnings()).toHaveLength(1);
    expect(warnings()[0]).toContain("Google said 403");
    // the advice leads with the variable, which works for every launch (cloud and Docker included)
    expect(warnings()[0].indexOf("CLEARCOTE_WIDEVINE=0")).toBeLessThan(warnings()[0].indexOf("widevine: false"));
    expect(g.checks).toBe(1);
  });

  it("quiet says nothing, and does not use the warning up", async () => {
    fakeGoogle({ fail: true });
    expect(await W.defaultCdm({ quiet: true })).toBeUndefined();
    expect(stderr).toEqual([]);
    W.resetWidevineDefaultState();
    expect(await W.defaultCdm()).toBeUndefined();
    expect(warnings()).toHaveLength(1);
  });

  it("CLEARCOTE_NO_WARN says nothing", async () => {
    process.env.CLEARCOTE_NO_WARN = "1";
    fakeGoogle({ fail: true });
    expect(await W.defaultCdm()).toBeUndefined();
    expect(stderr).toEqual([]);
  });

  it("is bounded, and cancels the fetch it stopped waiting for", async () => {
    const g = fakeGoogle({ hang: true });
    const t0 = Date.now();
    expect(await W.defaultCdm({ timeoutMs: 200 })).toBeUndefined();
    expect(Date.now() - t0).toBeLessThan(3000);
    expect(warnings()[0]).toContain("not ready within 0.2 s");
    await new Promise((r) => setTimeout(r, 20));
    expect(g.aborted).toBe(1); // nothing left running to keep the process alive
    const t1 = Date.now();
    expect(await W.defaultCdm({ timeoutMs: 5000 })).toBeUndefined(); // a launch meanwhile does not wait again
    expect(Date.now() - t1).toBeLessThan(1000);
  });

  it("takes its timeout from CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS", async () => {
    process.env.CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS = "0.2";
    fakeGoogle({ hang: true });
    const t0 = Date.now();
    expect(await W.defaultCdm({ quiet: true })).toBeUndefined();
    expect(Date.now() - t0).toBeLessThan(3000);
  });

  it("retries after the window", async () => {
    const g = fakeGoogle({ fail: true });
    await W.defaultCdm({ quiet: true });
    W.ageLastWidevineFailure(W.RETRY_AFTER_MS + 1000);
    await W.defaultCdm({ quiet: true });
    expect(g.checks).toBe(2);
  });

  it("uses a stale cache at once, refreshes it in the background, and records a failed check", async () => {
    // S12: a stamp over a day old used to make the launch wait for Google, then warn "launching without it"
    const dir = cachedCdm(VERSION, { stamp: false });
    const g = fakeGoogle({ fail: true });
    const t0 = performance.now();
    expect(await W.defaultCdm()).toBe(dir);
    expect(await W.defaultCdm()).toBe(dir); // a second launch meanwhile: no second refresh
    expect(performance.now() - t0).toBeLessThan(1000);
    await W.widevineRefreshInFlight();
    expect(g.checks).toBe(1);
    expect(stderr).toEqual([]); // Widevine works: nothing to warn about, no progress lines
    expect(await W.defaultCdm()).toBe(dir); // the failed check counts: no new check for a day
    expect(W.widevineRefreshInFlight()).toBeUndefined();
    expect(g.checks).toBe(1);
  });

  it("a stale cache never waits on a blackholed network", async () => {
    const dir = cachedCdm(VERSION, { stamp: false });
    fakeGoogle({ hang: true });
    const t0 = performance.now();
    expect(await W.defaultCdm({ timeoutMs: 5000 })).toBe(dir);
    expect(performance.now() - t0).toBeLessThan(1000);
    expect(warnings()).toEqual([]);
    expect(W.widevineRefreshInFlight()).toBeDefined(); // the background check, bounded and cancelled at reset
  });

  it("a background refresh installs a newer CDM for the next launch", async () => {
    const old = cachedCdm("4.10.1.0", { stamp: false });
    const g = fakeGoogle(); // offers VERSION, newer than 4.10.1.0
    expect(await W.defaultCdm()).toBe(old); // this launch: the cached one, at once
    await W.widevineRefreshInFlight();
    expect(g).toMatchObject({ checks: 1, downloads: 1 });
    expect(await W.defaultCdm()).toBe(join(wv, VERSION)); // the next launch: the new one, no network
    expect(g.checks).toBe(1);
  });

  it("a fresh cache costs no network", async () => {
    const dir = cachedCdm();
    expect(await W.defaultCdm()).toBe(dir);
    expect(W.widevineRefreshInFlight()).toBeUndefined();
  });

  it("prints nothing but the warning (S17)", async () => {
    fakeGoogle();
    expect(await W.defaultCdm({ quiet: false })).toBe(join(wv, VERSION));
    expect(stderr).toEqual([]); // no "[clearcote] [widevine] fetching/installed" lines on the default path
  });
});

// -- an untrusted version, the Windows rename -----------------------------------------------------

describe("hardening", () => {
  it.each(["../victim", "4.10.1.0/../../victim", "/tmp/victim", "4", "4.10.1.0.5", "4.10.x.0", " 4.10.1.0", "4.10.1.0\n", "٤.10.1.0"])(
    "an unexpected version (%j) never becomes a path (S15)", async (version) => {
      const victim = join(root, "victim");
      mkdirSync(victim);
      writeFileSync(join(victim, "precious.txt"), "keep me");
      mkdirSync(wv, { recursive: true });
      fakeGoogle({ version });
      await expect(W.fetchWidevine({ quiet: true })).rejects.toThrow("unexpected version");
      expect(readFileSync(join(victim, "precious.txt"), "utf8")).toBe("keep me");
      expect(await W.defaultCdm({ quiet: true })).toBeUndefined(); // the default path: fail-soft, nothing deleted
      expect(readFileSync(join(victim, "precious.txt"), "utf8")).toBe("keep me");
    });

  it.each(["4.10", "4.10.2830", "4.10.2830.0"])("a version that looks like one (%s) is used", async (version) => {
    fakeGoogle({ version });
    expect(await W.fetchWidevine({ quiet: true })).toBe(join(wv, version));
  });

  it("no version still installs as current", async () => {
    fakeGoogle({ version: "" });
    expect(await W.fetchWidevine({ quiet: true })).toBe(join(wv, "current"));
  });

  it("the install's renames go through download.ts's Windows retry (S14)", async () => {
    const { installTestHooks } = await import("../src/download.js");
    const seen: Array<[string, string]> = [];
    installTestHooks.beforeRename = (src: string, dst: string) => { seen.push([basename(src).slice(0, 5), basename(dst)]); };
    try {
      cachedCdm(VERSION, { stamp: false, complete: false }); // an incomplete tree is moved aside first
      fakeGoogle();
      await W.fetchWidevine({ quiet: true });
    } finally {
      installTestHooks.beforeRename = undefined;
    }
    expect(seen.map(([s]) => s)).toEqual([VERSION.slice(0, 5), ".tmp-"]);
    expect(seen[0][1].startsWith(".trash-")).toBe(true);
    expect(seen[1][1]).toBe(VERSION);
  });
});

// -- the opt-out on cloud and Docker launches (S13) -----------------------------------------------

describe("the opt-out elsewhere", () => {
  it.each([false, undefined, null])("a cloud launch accepts and drops widevine: %s", async (value) => {
    delete process.env.CLEARCOTE_API_KEY;
    const actual = await vi.importActual<typeof import("../src/cloud.js")>("../src/cloud.js");
    // stops at the client (no API key): past the option check, which used to refuse it
    await expect(actual.launchCloud({ cloud: true, widevine: value })).rejects.toThrow("no Clearcote API key");
    await expect(actual.launchCloud({ cloud: true, widevine: true })).rejects.toThrow("widevine is not available for cloud browsers");
  });

  it("a Docker launch accepts and drops widevine: false", async () => {
    const { containerEnv } = await import("../src/docker.js");
    expect(containerEnv({ fingerprint: "17", widevine: false })).toEqual(containerEnv({ fingerprint: "17" }));
    expect(() => containerEnv({ fingerprint: "17", widevine: true })).toThrow("widevine is not available when launch() runs Clearcote in Docker");
  });
});

// -- one launch's switch ---------------------------------------------------------------------------

describe("widevineLaunchArgs", () => {
  it("per mode", async () => {
    const cdm = cachedCdm();
    const fresh = fakeEngine([ENGINE_1022], "new");
    const old = fakeEngine([], "old");
    const arg = `--widevine-cdm-path=${cdm}`;
    expect(await W.widevineLaunchArgs(fresh, undefined, true, [], { quiet: true })).toEqual([arg]); // licensed default
    expect(await W.widevineLaunchArgs(fresh, undefined, false, [], { quiet: true })).toEqual([]); // free default: off
    expect(await W.widevineLaunchArgs(fresh, true, false, [], { quiet: true })).toEqual([arg]); // asked for
    expect(await W.widevineLaunchArgs(fresh, false, true, [], { quiet: true })).toEqual([]); // turned off
    expect(await W.widevineLaunchArgs(old, undefined, true, [], { quiet: true })).toEqual([]); // no 1022: old default
    expect(await W.widevineLaunchArgs(old, true, true, [], { quiet: true })).toEqual([]); // (explicit seeds instead)
    const mine = ["--widevine-cdm-path=/my/cdm"];
    expect(await W.widevineLaunchArgs(fresh, undefined, true, mine, { quiet: true })).toEqual([]); // the caller's own wins
    expect(await W.widevineLaunchArgs(fresh, true, true, mine, { quiet: true })).toEqual([]);
  });

  it("an older engine costs the default no network", async () => {
    expect(await W.widevineLaunchArgs(fakeEngine(), undefined, true, [])).toEqual([]); // the fetch stub throws on any call
  });

  it("cdmHostVerified: Linux always; Windows/macOS only with VerifyCdmHost_0 in the engine", () => {
    const verified = fakeEngine([ENGINE_1022], "hv");
    const unverified = fakeEngine([ENGINE_1022_UNVERIFIED], "nohv");
    expect(W.cdmHostVerified(unverified, "linux")).toBe(true);
    expect(W.cdmHostVerified(verified, process.platform)).toBe(true);
    expect(W.cdmHostVerified(unverified, process.platform)).toBe(!WIN_OR_MAC);
    expect(W.cdmHostVerified(undefined, "win32")).toBe(false);
  });

  it("licensed default, engine without CDM host verification: off on Windows/macOS (one note), on elsewhere", async () => {
    const cdm = cachedCdm();
    const unverified = fakeEngine([ENGINE_1022_UNVERIFIED], "nohv2");
    const arg = `--widevine-cdm-path=${cdm}`;
    expect(await W.widevineLaunchArgs(unverified, undefined, true, [])).toEqual(WIN_OR_MAC ? [] : [arg]);
    expect(await W.widevineLaunchArgs(unverified, undefined, true, [])).toEqual(WIN_OR_MAC ? [] : [arg]);
    expect(stderr.filter((l) => l.includes("clear client id")).length).toBe(WIN_OR_MAC ? 1 : 0); // once per process
    expect(await W.widevineLaunchArgs(unverified, true, true, [], { quiet: true })).toEqual([arg]); // asked for: on
    process.env.CLEARCOTE_WIDEVINE = "1";
    expect(await W.widevineLaunchArgs(unverified, undefined, true, [], { quiet: true })).toEqual([arg]); // env opt-in: on
  });

  it("an explicit failure stays best-effort, without the default's warning", async () => {
    fakeGoogle({ fail: true });
    expect(await W.widevineLaunchArgs(fakeEngine([ENGINE_1022]), true, false, [])).toEqual([]);
    expect(warnings()).toEqual([]);
  });
});

// -- the launch entry points -----------------------------------------------------------------------

describe("launches", () => {
  it("a licensed persistent launch gets Widevine by default, without profile seeding", async () => {
    const cdm = cachedCdm();
    const udd = join(root, "udd");
    const ctx = await launchPersistentContext(udd, licensed(fakeEngine([ENGINE_1022])));
    const opts = stub.launches.at(-1)!.opts;
    expect(cdmArgs(opts.args)).toEqual([`--widevine-cdm-path=${cdm}`]);
    expect(existsSync(join(udd, "WidevineCdm"))).toBe(false);
    expect(opts.ignoreDefaultArgs).not.toContain("--disable-component-update");
    expect(opts).not.toHaveProperty("widevine");
    await ctx.close();
  });

  it("a licensed incognito launch gets it too", async () => {
    const cdm = cachedCdm();
    const browser = await launch(licensed(fakeEngine([ENGINE_1022]), { ephemeralProfile: false }));
    expect(stub.launches.at(-1)!.kind).toBe("incognito");
    expect(cdmArgs(lastArgs())).toEqual([`--widevine-cdm-path=${cdm}`]);
    expect(stub.launches.at(-1)!.opts).not.toHaveProperty("widevine");
    await browser.close();
  });

  it("a licensed throwaway-profile launch gets it too", async () => {
    const cdm = cachedCdm();
    const browser = await launch(licensed(fakeEngine([ENGINE_1022])));
    expect(stub.launches.at(-1)!.kind).toBe("persistent");
    expect(cdmArgs(lastArgs())).toEqual([`--widevine-cdm-path=${cdm}`]);
    await browser.close();
  });

  it.each(["option", "env"])("a licensed launch opts out (%s)", async (how) => {
    cachedCdm();
    if (how === "env") process.env.CLEARCOTE_WIDEVINE = "0";
    const ctx = await launchPersistentContext(join(root, "udd"), licensed(fakeEngine([ENGINE_1022]), how === "option" ? { widevine: false } : {}));
    expect(cdmArgs(lastArgs())).toEqual([]);
    await ctx.close();
  });

  it("a free launch stays off", async () => {
    cachedCdm();
    const ctx = await launchPersistentContext(join(root, "udd"), base(fakeEngine([ENGINE_1022])));
    const b = await launch(base(fakeEngine([ENGINE_1022], "c2"), { ephemeralProfile: false }));
    expect(stub.launches.map((l) => cdmArgs(l.opts.args))).toEqual([[], []]);
    await ctx.close();
    await b.close();
  });

  it.each(["option", "env"])("a free launch opts in (%s): seeded and enabled as before", async (how) => {
    const cdm = cachedCdm();
    if (how === "env") process.env.CLEARCOTE_WIDEVINE = "1";
    const udd = join(root, "udd");
    const ctx = await launchPersistentContext(udd, base(fakeEngine([ENGINE_1022]), how === "option" ? { widevine: true } : {}));
    const opts = stub.launches.at(-1)!.opts;
    expect(cdmArgs(opts.args)).toEqual([`--widevine-cdm-path=${cdm}`]);
    expect(JSON.parse(readFileSync(join(udd, "WidevineCdm", W.HINT_FILE), "utf8")).Path.endsWith(VERSION)).toBe(true);
    expect(opts.ignoreDefaultArgs).toContain("--disable-component-update");
    expect(opts.ignoreDefaultArgs).toContain("--enable-automation"); // the automation strip survives
    await ctx.close();
  });

  it("a licensed launch with widevine: true seeds the profile too", async () => {
    cachedCdm();
    const udd = join(root, "udd");
    const ctx = await launchPersistentContext(udd, licensed(fakeEngine([ENGINE_1022]), { widevine: true }));
    expect(cdmArgs(lastArgs())).toHaveLength(1);
    expect(existsSync(join(udd, "WidevineCdm", W.HINT_FILE))).toBe(true);
    await ctx.close();
  });

  it("offline, a licensed launch still starts, and says so once", async () => {
    const g = fakeGoogle({ fail: true });
    const exe = fakeEngine([ENGINE_1022]);
    for (let i = 0; i < 2; i++) {
      const ctx = await launchPersistentContext(join(root, `udd${i}`), licensed(exe, { quiet: false }));
      await ctx.close();
    }
    expect(stub.launches).toHaveLength(2);
    expect(stub.launches.map((l) => cdmArgs(l.opts.args))).toEqual([[], []]);
    expect(warnings()).toHaveLength(1);
    expect(g.checks).toBe(1); // the second launch did not try again (10-minute back-off)
  });

  it("offline and quiet, it says nothing", async () => {
    fakeGoogle({ fail: true });
    const ctx = await launchPersistentContext(join(root, "udd"), licensed(fakeEngine([ENGINE_1022])));
    expect(warnings()).toEqual([]);
    await ctx.close();
  });

  it("a licensed launch does not wait past the timeout", async () => {
    process.env.CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS = "0.3";
    fakeGoogle({ hang: true });
    const t0 = Date.now();
    const ctx = await launchPersistentContext(join(root, "udd"), licensed(fakeEngine([ENGINE_1022])));
    expect(Date.now() - t0).toBeLessThan(5000);
    expect(cdmArgs(lastArgs())).toEqual([]);
    await ctx.close();
  });

  it("widevine: true that cannot get the CDM keeps its old behaviour", async () => {
    fakeGoogle({ fail: true });
    const ctx = await launchPersistentContext(join(root, "udd"), base(fakeEngine([ENGINE_1022]), { widevine: true, quiet: false }));
    expect(cdmArgs(lastArgs())).toEqual([]);
    expect(stderr.some((l) => l.includes("[widevine] setup failed (continuing without DRM)"))).toBe(true);
    expect(warnings()).toEqual([]);
    await ctx.close();
  });

  it("a second launch does not download again", async () => {
    const g = fakeGoogle();
    const exe = fakeEngine([ENGINE_1022]);
    const a = await launchPersistentContext(join(root, "udd1"), licensed(exe));
    const b = await launchPersistentContext(join(root, "udd2"), licensed(exe));
    const c = await launch(licensed(exe, { ephemeralProfile: false }));
    const want = [`--widevine-cdm-path=${join(wv, VERSION)}`];
    expect(stub.launches.map((l) => cdmArgs(l.opts.args))).toEqual([want, want, want]);
    expect(g).toMatchObject({ checks: 1, downloads: 1 }); // one update check, one download, three launches
    await a.close();
    await b.close();
    await c.close();
  });

  it("the caller's own --widevine-cdm-path wins", async () => {
    cachedCdm();
    const ctx = await launchPersistentContext(join(root, "udd"), licensed(fakeEngine([ENGINE_1022]), { args: ["--widevine-cdm-path=/mine"] }));
    expect(cdmArgs(lastArgs())).toEqual(["--widevine-cdm-path=/mine"]);
    await ctx.close();
  });

  it("serve() follows the same rules", async () => {
    const cdm = cachedCdm();
    const server: HttpServer = createServer((_req, res) => { res.setHeader("content-type", "application/json"); res.end("{}"); });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
    const addr = server.address();
    const port = typeof addr === "object" && addr ? addr.port : 0;
    // serve() reaches its own CDP endpoint with fetch: let that one through to the real fetch
    const stubbed = globalThis.fetch;
    globalThis.fetch = ((url: unknown, init?: RequestInit) => String(url).startsWith(`http://127.0.0.1:${port}/`)
      ? realFetch(url as string, init) : stubbed(url as string, init)) as typeof fetch;
    try {
      const s1 = await serve({ ...licensed(fakeEngine([ENGINE_1022])), port });
      expect(cdmArgs(stub.spawns.at(-1)!.args)).toEqual([`--widevine-cdm-path=${cdm}`]);
      await s1.close();
      const s2 = await serve({ ...licensed(fakeEngine([ENGINE_1022], "c2")), port, widevine: false });
      expect(cdmArgs(stub.spawns.at(-1)!.args)).toEqual([]);
      await s2.close();
      const s3 = await serve({ ...base(fakeEngine([ENGINE_1022], "c3")), port });
      expect(cdmArgs(stub.spawns.at(-1)!.args)).toEqual([]);
      await s3.close();
    } finally {
      await new Promise<void>((r) => server.close(() => r()));
    }
  });

  it("a cloud launch is untouched", async () => {
    await launch({ cloud: true, licenseKey: KEY } as never).catch(() => undefined);
    // nothing fetched (the fetch stub throws on any call), nothing added for the hosted browser, which loads its own
    expect(stub.cloudCalls).toHaveLength(1);
    expect(stub.cloudCalls[0]).not.toHaveProperty("widevine");
    expect(LOCAL_ONLY_OPTIONS).toContain("widevine"); // an explicit widevine is still refused there, as before
  });

  it('the profile: "auto" host probe runs without Widevine', async () => {
    cachedCdm();
    await expect(launch(licensed(fakeEngine([ENGINE_1022]), { profile: "auto", ephemeralProfile: false }))).rejects.toThrow("stop after the host probe");
    expect(stub.launches).toHaveLength(1); // the probe's own launch
    expect(cdmArgs(lastArgs())).toEqual([]);
  });
});
