// A Linux persona on a Windows host. There the GPU runs through Direct3D 11, which clamps the WebGL/WebGPU
// limits, so a Linux claim reports limits no real Linux machine has. launch() says so once per process, for a
// local launch only: a Docker launch runs on Linux in its container and a cloud launch runs remotely, and
// pass-through (no persona) claims nothing.
//
// Hermetic: Playwright, the Docker launch and the cloud launch are stand-ins, the "engine" is a small file,
// and a local launch's warnings are judged as on a Windows host wherever the suite runs. Mirrors the
// Linux-persona tests in sdk/python/tests/test_warnings.py.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stub = vi.hoisted(() => ({
  // PROFILE_DIR is read once, at import: point it somewhere empty before the SDK loads, so no saved
  // profile on this machine can change what these tests see.
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  host: undefined as string | undefined, // the host a local launch's warnings are judged against
  routed: [] as string[],
}));

vi.mock("playwright-core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("playwright-core")>();
  const { EventEmitter } = await import("node:events");
  const chromium = {
    launchPersistentContext: async () => {
      const ctx = new EventEmitter() as EventEmitter & Record<string, unknown>;
      ctx.pages = () => [];
      ctx.newPage = async () => ({});
      ctx.browser = () => null;
      ctx.close = async () => { ctx.emit("close", ctx); };
      return ctx;
    },
  };
  return { ...actual, chromium };
});
vi.mock("../src/warnings.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/warnings.js")>();
  return {
    ...real,
    emitCoherenceWarnings: (o: Record<string, unknown>, quiet?: boolean, host?: string, build?: string) =>
      real.emitCoherenceWarnings(o, quiet, stub.host ?? host, build),
  };
});
vi.mock("../src/docker.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/docker.js")>();
  return { ...real, launchDocker: async () => { stub.routed.push("docker"); return {} as never; } };
});
vi.mock("../src/cloud.js", async (importOriginal) => {
  const real = await importOriginal<typeof import("../src/cloud.js")>();
  return { ...real, launchCloud: async () => { stub.routed.push("cloud"); return {} as never; } };
});

import { coherenceWarnings, emitCoherenceWarnings, resetSeenWarnings } from "../src/warnings.js";
import { launch, launchPersistentContext } from "../src/index.js";

const CODE = "linux-persona-windows-host";
const LINE = "a Linux persona on a Windows host";
const codes = (o: Record<string, unknown>, host = "win32") => new Set(coherenceWarnings(o, host, "149").map((w) => w.code));

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

// No licence (a key would take a lease), warnings on, and a TEMP of our own: everything a test or the code
// under test creates goes under `root`, removed after.
const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_PERSONA_ENV"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let written: string[];

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-linuxhost-"));
  for (const d of ["temp", "home"]) mkdirSync(join(root, d));
  process.env.HOME = process.env.USERPROFILE = join(root, "home");
  process.env.TEMP = process.env.TMP = process.env.TMPDIR = join(root, "temp");
  for (const k of ["CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION",
    "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_PERSONA_ENV"]) delete process.env[k];
  stub.host = undefined;
  stub.routed.length = 0;
  resetSeenWarnings();
  written = [];
  vi.spyOn(process.stderr, "write").mockImplementation(((chunk: unknown) => { written.push(String(chunk)); return true; }) as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

const said = () => written.filter((w) => w.includes(LINE)).length;

/** A stand-in engine (with pass-through, so fingerprint: "off" is applied rather than gated away). */
function fakeEngine(): string {
  const dir = join(root, "engine");
  mkdirSync(dir, { recursive: true });
  const p = join(dir, "chrome");
  writeFileSync(p, Buffer.from("\x7fELF\0fingerprint-passthrough\0--remote-debugging-port\0", "latin1"));
  return p;
}

/** Run `fn` with process.platform reading `platform`. */
async function onPlatform<T>(platform: string, fn: () => Promise<T>): Promise<T> {
  const desc = Object.getOwnPropertyDescriptor(process, "platform")!;
  Object.defineProperty(process, "platform", { ...desc, value: platform });
  try {
    return await fn();
  } finally {
    Object.defineProperty(process, "platform", desc);
  }
}

describe("coherenceWarnings: a Linux persona on a Windows host", () => {
  it("flags a Linux claim on a Windows host", () => {
    expect(codes({ platform: "linux", fingerprint: "17" }).has(CODE)).toBe(true);
    expect(codes({ platform: " Linux ", fingerprintProfile: "p.json" }).has(CODE)).toBe(true);
    expect(codes({ platform: "linux" }).has(CODE)).toBe(true); // no seed: the engine still claims Linux
  });

  it("not for a Windows claim, nor off Windows", () => {
    expect(codes({ platform: "windows", fingerprint: "17" }).has(CODE)).toBe(false);
    expect(codes({ fingerprint: "17" }).has(CODE)).toBe(false); // no platform: the host's own
    expect(codes({ platform: "linux", fingerprint: "17" }, "linux").has(CODE)).toBe(false);
    expect(codes({ platform: "linux", fingerprint: "17" }, "darwin").has(CODE)).toBe(false);
  });

  it("not without a persona", () => {
    // Pass-through runs with no persona at all (the engine presents the real host); nothing set claims the host.
    expect(codes({ platform: "linux", fingerprint: "off" }).has(CODE)).toBe(false);
    expect(codes({}).has(CODE)).toBe(false);
  });

  it("is said once per process, and quiet neither says it nor uses it up", () => {
    const opts = { platform: "linux", fingerprint: "17", headless: false };
    emitCoherenceWarnings(opts, true, "win32", "149");
    expect(said()).toBe(0);
    for (let i = 0; i < 3; i++) emitCoherenceWarnings(opts, false, "win32", "149");
    expect(said()).toBe(1);
    const line = written.find((w) => w.includes(LINE))!;
    expect(line).toContain("clearcote: warning: a Linux persona on a Windows host reports Windows GPU limits (Direct3D caps WebGL)");
    expect(line).toContain('platform: "windows"');
    expect(line).toContain("docker: true");
  });
});

describe("launch(): a Linux persona on a Windows host", () => {
  it("a local launch says it, once", async () => {
    stub.host = "win32";
    const exe = fakeEngine();
    for (const d of ["a", "b"]) {
      const ctx = await launchPersistentContext(join(root, d), { executablePath: exe, platform: "linux", fingerprint: "17", headless: true });
      await ctx.close();
    }
    expect(said()).toBe(1);
  });

  it("a local launch with a Windows persona, or none, does not", async () => {
    stub.host = "win32";
    const exe = fakeEngine();
    const cases: Array<[string, Record<string, unknown>]> = [
      ["a", { platform: "windows", fingerprint: "17" }], ["b", { platform: "linux", fingerprint: "off" }], ["c", {}],
    ];
    for (const [d, o] of cases) {
      const ctx = await launchPersistentContext(join(root, d), { executablePath: exe, headless: true, ...o });
      await ctx.close();
    }
    expect(said()).toBe(0);
  });

  it("a Docker or cloud launch does not", async () => {
    await onPlatform("win32", async () => {
      await launch({ docker: true, platform: "linux", fingerprint: "17" });
      await launch({ cloud: true, platform: "linux", fingerprint: "17" } as never);
    });
    expect(stub.routed).toEqual(["docker", "cloud"]);
    expect(said()).toBe(0);
  });
});
