// Engine patches 1021 (persona in the environment, "env mode") and 1022 (--widevine-cdm-path), SDK side.
//
// Both are gated on the engine: they change a launch only when the binary implements the switch, so an
// r30/r31 engine launches exactly as before. Hermetic: Playwright and the child-process spawn are
// replaced by stand-ins that record what they were asked to launch, the "engine" is a small file
// carrying (or not) the switch names the probe looks for, and the Widevine update check answers from
// a stubbed fetch with a CDM already in the cache. Mirrors sdk/python/tests/test_personaenv.py.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { createServer, type Server as HttpServer } from "node:http";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stub = vi.hoisted(() => ({
  // PROFILE_DIR is read once, at import: point it somewhere empty before the SDK loads, so no saved
  // profile on this machine can change what these tests see.
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  launches: [] as Array<{ kind: "persistent" | "incognito"; userDataDir?: string; opts: Record<string, unknown> }>,
  spawns: [] as Array<{ args: string[]; env: Record<string, string | undefined> | undefined }>,
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
    spawn: (_cmd: string, args: string[], options?: { env?: Record<string, string | undefined> }) => {
      stub.spawns.push({ args, env: options?.env });
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

import * as P from "../src/personaenv.js";
import { OMAHA_URL, widevineCdmArgs } from "../src/widevine.js";
import { containerEnv } from "../src/docker.js";
import { resolveLicenseKey } from "../src/license.js";
import { launch, launchPersistentContext, serve } from "../src/index.js";

const ENGINE_1021 = "\0persona-from-env\0";
const ENGINE_1022 = "\0widevine-cdm-path\0";
const ON = () => true; // probe stubs
const OFF = () => false;

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

// No licence (a key would select a lease), no opt-out or leftover payload from the environment, and a
// TEMP of our own: everything a test or the code under test creates goes under `root`, removed after.
const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_WIDEVINE_DIR",
  P.OPT_OUT_ENV, P.ENV_VAR, "CC_TEST_MARKER"] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-personaenv-"));
  const temp = join(root, "temp");
  const home = join(root, "home");
  for (const d of [temp, home]) mkdirSync(d);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.TEMP = temp;
  process.env.TMP = temp;
  process.env.TMPDIR = temp;
  process.env.CLEARCOTE_NO_WARN = "1";
  for (const k of ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD",
    "CLEARCOTE_DOCKER", "CLEARCOTE_WIDEVINE_DIR", P.OPT_OUT_ENV, P.ENV_VAR, "CC_TEST_MARKER"]) delete process.env[k];
  stub.launches.length = 0;
  stub.spawns.length = 0;
});

afterEach(() => {
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
  rmSync(root, { recursive: true, force: true, maxRetries: 5 });
});

/** A stand-in engine: the probe reads the NUL-delimited switch names in it. */
function fakeEngine(markers: string[] = [], name = "chrome"): string {
  const dir = join(root, `engine-${name}`);
  mkdirSync(dir, { recursive: true });
  const p = join(dir, name);
  writeFileSync(p, Buffer.from(`\x7fELF${markers.join("")}\0--remote-debugging-port\0`, "latin1"));
  return p;
}

it("harness: no licence is visible, so no launch here can take a real lease", () => {
  expect(resolveLicenseKey()).toBeUndefined();
});

// -- the payload and the switch list ------------------------------------------------------------

describe("the payload and the switch list", () => {
  it("the payload is the engine's format", () => {
    const entries: Array<[string, string]> = [["fingerprint", "17"], ["disable-canvas-noise", ""], ["fingerprint-timezone", "Europe/Zürich"]];
    const raw = Buffer.from("fingerprint\x0017\x00disable-canvas-noise\x00\x00fingerprint-timezone\x00Europe/Z\xc3\xbcrich\x00", "latin1");
    expect(P.encode(entries)).toBe(raw.toString("base64"));
    expect(P.decode(P.encode(entries))).toEqual(entries);
  });

  it("decode refuses a payload that is not name\\0value\\0 pairs", () => {
    expect(() => P.decode(Buffer.from("fingerprint\x0017", "latin1").toString("base64"))).toThrow(/malformed/);
    expect(() => P.decode(Buffer.from("fingerprint\x00", "latin1").toString("base64"))).toThrow(/malformed/);
  });

  it.each(["fingerprint", "fingerprint-platform", "fingerprint-profile", "canvas-bridge-url", "canvas-bridge-auth",
    "disable-canvas-noise", "disable-fingerprint-noise", "disable-fingerprint-voices", "disable-gpu-fingerprint",
    "disable-gpu-string-spoof", "proxy-auth", "socks5-credentials", "webrtc-ip"])("the engine transports %s", (name) => {
    expect(P.isTransported(name)).toBe(true);
  });

  // The engine stops the launch on a name outside its list: the SDK's list must never be wider.
  it.each(["proxy-server", "lang", "user-data-dir", "fingerprinting", "disable-features", "persona-from-env",
    "disable-persona-env-transport", "headless", "canvas-bridge"])("%s stays on the command line", (name) => {
    expect(P.isTransported(name)).toBe(false);
  });
});

// -- apply() ------------------------------------------------------------------------------------

describe("apply()", () => {
  it("moves the persona switches into the environment", () => {
    const args = ["--fingerprint=17", "--lang=en-US", "--fingerprint-platform=windows", "--socks5-credentials=u:p",
      "--no-first-run", "https://example.com/"];
    const base = { KEEP: "1" };
    const { args: newArgs, env } = P.apply("chrome", args, base, undefined, ON);
    expect(newArgs).toEqual(["--lang=en-US", "--no-first-run", "https://example.com/", "--persona-from-env"]);
    expect(P.decode(env![P.ENV_VAR]!)).toEqual([["fingerprint", "17"], ["fingerprint-platform", "windows"],
      ["socks5-credentials", "u:p"]]);
    expect(env!.KEEP).toBe("1");
    expect(base).toEqual({ KEEP: "1" }); // the caller's object is not touched
  });

  it("the process environment is the base", () => {
    process.env.CC_TEST_MARKER = "1";
    const { env } = P.apply("chrome", ["--fingerprint=1"], undefined, undefined, ON);
    expect(env!.CC_TEST_MARKER).toBe("1");
    expect(env![P.ENV_VAR]).toBeDefined();
    expect(process.env[P.ENV_VAR]).toBeUndefined(); // a copy: process.env is not touched either
  });

  it("the last copy of a repeated switch wins", () => {
    // On a command line Chromium keeps the LAST copy; the engine adopts the FIRST payload entry.
    const args = ["--fingerprint-hardware-concurrency=8", "--fingerprint=1", "--fingerprint-hardware-concurrency=4"];
    const { env } = P.apply("chrome", args, {}, undefined, ON);
    expect(P.decode(env![P.ENV_VAR]!)).toEqual([["fingerprint", "1"], ["fingerprint-hardware-concurrency", "4"]]);
  });

  it("is inert on an engine without the switch", () => {
    const args = ["--fingerprint=17", "--lang=en-US"];
    const base = { KEEP: "1" };
    const out = P.apply("chrome", args, base, undefined, OFF);
    expect(out.args).toEqual(args);
    expect(out.env).toBe(base);
  });

  it.each([
    [false, undefined],
    [undefined, "0"],
    [undefined, "false"],
    [undefined, "off"],
    [undefined, "no"],
    [undefined, " OFF "],
  ] as Array<[boolean | undefined, string | undefined]>)("turned off (personaEnv %s, CLEARCOTE_PERSONA_ENV %j)", (enabled, envValue) => {
    if (envValue !== undefined) process.env[P.OPT_OUT_ENV] = envValue;
    const args = ["--fingerprint=17"];
    expect(P.apply("chrome", args, undefined, enabled, ON)).toEqual({ args, env: undefined });
  });

  it("the option beats the environment variable", () => {
    process.env[P.OPT_OUT_ENV] = "0";
    const { args, env } = P.apply("chrome", ["--fingerprint=17"], {}, true, ON);
    expect(args).toEqual(["--persona-from-env"]);
    expect(env![P.ENV_VAR]).toBeDefined();
  });

  it.each(["--disable-persona-env-transport", "--persona-from-env", "--disable-persona-env-transport=1"])(
    "a transport the caller chose is left alone (%s)", (chosen) => {
      const args = ["--fingerprint=17", chosen];
      expect(P.apply("chrome", args, undefined, undefined, ON)).toEqual({ args, env: undefined });
    });

  it("nothing to move does not even probe", () => {
    const probe = () => { throw new Error("probed without a persona switch to move"); };
    const args = ["--lang=en-US", "--no-first-run"];
    expect(P.apply("chrome", args, undefined, undefined, probe)).toEqual({ args, env: undefined });
  });

  it("an oversized persona stays on the command line", () => {
    const args = ["--fingerprint=17", "--fingerprint-profile=" + "A".repeat(40000)];
    expect(P.apply("chrome", args, undefined, undefined, ON)).toEqual({ args, env: undefined });
  });

  it("the real probe reads the engine", () => {
    const with1021 = fakeEngine([ENGINE_1021], "new");
    const without = fakeEngine([], "old");
    expect(P.apply(with1021, ["--fingerprint=17"], {}).args).toEqual(["--persona-from-env"]);
    expect(P.apply(without, ["--fingerprint=17"], {}).args).toEqual(["--fingerprint=17"]);
  });
});

// -- the launch entry points ------------------------------------------------------------------

/** The persona a launch's env carries, or null. */
function persona(env: unknown): Array<[string, string]> | null {
  const value = (env as Record<string, string | undefined> | undefined)?.[P.ENV_VAR];
  return value === undefined ? null : P.decode(value);
}

const base = (exe: string) => ({ executablePath: exe, headless: false, quiet: true });

async function fakeCdpEndpoint(): Promise<{ port: number; close: () => Promise<void> }> {
  const server: HttpServer = createServer((_req, res) => {
    res.setHeader("content-type", "application/json");
    res.end("{}");
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const addr = server.address();
  const port = typeof addr === "object" && addr ? addr.port : 0;
  return { port, close: () => new Promise<void>((r) => server.close(() => r())) };
}

describe("the launch entry points", () => {
  it("launchPersistentContext() on a 1021 engine", async () => {
    const exe = fakeEngine([ENGINE_1021]);
    const context = await launchPersistentContext(join(root, "udd"), { ...base(exe), fingerprint: "17", platform: "windows" });
    const call = stub.launches.at(-1)!;
    const args = call.opts.args as string[];
    expect(args).toContain("--persona-from-env");
    expect(args.filter((a) => a.startsWith("--fingerprint"))).toEqual([]);
    expect(persona(call.opts.env)).toContainEqual(["fingerprint", "17"]);
    expect(call.opts).not.toHaveProperty("personaEnv"); // an SDK option, never a Playwright one
    await context.close();
  });

  it("launchPersistentContext() on an older engine is unchanged", async () => {
    const exe = fakeEngine();
    const context = await launchPersistentContext(join(root, "udd"), { ...base(exe), fingerprint: "17", platform: "windows" });
    const call = stub.launches.at(-1)!;
    expect(call.opts.args).toContain("--fingerprint=17");
    expect(call.opts.args).not.toContain("--persona-from-env");
    expect(persona(call.opts.env)).toBeNull();
    await context.close();
  });

  it("launch({ ephemeralProfile: false }) on a 1021 engine", async () => {
    const exe = fakeEngine([ENGINE_1021]);
    const browser = await launch({ ...base(exe), fingerprint: "17", ephemeralProfile: false });
    const call = stub.launches.at(-1)!;
    expect(call.kind).toBe("incognito");
    expect(call.opts.args).toContain("--persona-from-env");
    expect(persona(call.opts.env)).toContainEqual(["fingerprint", "17"]);
    expect(call.opts).not.toHaveProperty("personaEnv");
    await browser.close();
  });

  it("launch() on its throwaway profile, on a 1021 engine", async () => {
    const exe = fakeEngine([ENGINE_1021]);
    const browser = await launch({ ...base(exe), fingerprint: "17" });
    const call = stub.launches.at(-1)!;
    expect(call.kind).toBe("persistent");
    expect(call.opts.args).toContain("--persona-from-env");
    expect(persona(call.opts.env)).toContainEqual(["fingerprint", "17"]);
    await browser.close();
  });

  it("personaEnv: false keeps the command line", async () => {
    const exe = fakeEngine([ENGINE_1021]);
    const context = await launchPersistentContext(join(root, "udd"), { ...base(exe), fingerprint: "17", personaEnv: false });
    const call = stub.launches.at(-1)!;
    expect(call.opts.args).toContain("--fingerprint=17");
    expect(persona(call.opts.env)).toBeNull();
    expect(call.opts).not.toHaveProperty("personaEnv");
    await context.close();
  });

  it("the caller's own env is the base, and is left as it was", async () => {
    const exe = fakeEngine([ENGINE_1021]);
    const own = { KEEP: "1" };
    const context = await launchPersistentContext(join(root, "udd"), { ...base(exe), fingerprint: "17", env: own });
    const env = stub.launches.at(-1)!.opts.env as Record<string, string>;
    expect(env.KEEP).toBe("1");
    expect(persona(env)).toContainEqual(["fingerprint", "17"]);
    expect(own).toEqual({ KEEP: "1" });
    await context.close();
  });

  // The persona step comes after the run-token step: a licensed launch's env carries both.
  it("a licensed launch carries the run token and the persona", async () => {
    const realFetch = globalThis.fetch;
    const token = Buffer.from(JSON.stringify({ v: 1, plan: "pro", n: 1 })).toString("base64url") + ".sig";
    globalThis.fetch = (async (url: unknown) => {
      if (String(url).endsWith("/checkout")) {
        return new Response(JSON.stringify({
          lease_id: "L1", token, exp: Math.floor(Date.now() / 1000) + 900, lease_ttl_sec: 810,
          heartbeat_interval_sec: 270, concurrency: { used: 1, limit: 5 },
        }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    try {
      const exe = fakeEngine([ENGINE_1021]);
      const key = `cc_lic_pro_${Date.now()}_${Math.random().toString(36).slice(2)}`; // its own lease
      const context = await launchPersistentContext(join(root, "udd"), {
        ...base(exe), fingerprint: "17", licenseKey: key, licenseApiBase: "http://test.local",
      });
      const call = stub.launches.at(-1)!;
      const env = call.opts.env as Record<string, string>;
      expect(env.CLEARCOTE_RUN_TOKEN).toBe(token);
      expect(persona(env)).toContainEqual(["fingerprint", "17"]);
      expect(call.opts.args).toContain("--persona-from-env");
      await context.close();
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("serve() on a 1021 engine", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const srv = await serve({ ...base(fakeEngine([ENGINE_1021])), port, fingerprint: "17" });
      const { args, env } = stub.spawns.at(-1)!;
      expect(args).toContain("--persona-from-env");
      expect(args.filter((a) => a.startsWith("--fingerprint"))).toEqual([]);
      expect(args).toContain(`--remote-debugging-port=${port}`); // the CDP switches stay
      expect(persona(env)).toContainEqual(["fingerprint", "17"]);
      await srv.close();
    } finally {
      await close();
    }
  });

  it("serve() on an older engine, and with personaEnv: false, is unchanged", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      for (const [exe, extra] of [[fakeEngine([], "old"), {}], [fakeEngine([ENGINE_1021], "new"), { personaEnv: false }]] as const) {
        const srv = await serve({ ...base(exe), port, fingerprint: "17", ...extra });
        const { args, env } = stub.spawns.at(-1)!;
        expect(args).toContain("--fingerprint=17");
        expect(args).not.toContain("--persona-from-env");
        expect(persona(env)).toBeNull();
        await srv.close();
      }
    } finally {
      await close();
    }
  });

  it("the Docker path hands personaEnv to the image's entrypoint, which reads CLEARCOTE_PERSONA_ENV", () => {
    // The container's chrome is started by docker/serve.py, which takes the same last step a launch here does.
    expect(containerEnv({ fingerprint: "17" })).toEqual({ CC_FINGERPRINT: "17" });
    expect(containerEnv({ fingerprint: "17", personaEnv: false })).toEqual({ CC_FINGERPRINT: "17", CLEARCOTE_PERSONA_ENV: "0" });
    expect(containerEnv({ fingerprint: "17", personaEnv: true })).toEqual({ CC_FINGERPRINT: "17", CLEARCOTE_PERSONA_ENV: "1" });
  });
});

// -- 1022: --widevine-cdm-path ----------------------------------------------------------------

describe("1022: --widevine-cdm-path", () => {
  const VERSION = "4.10.9999.0";
  let realFetch: typeof fetch;
  let fetched: string[];

  /** A CDM already in the cache (CLEARCOTE_WIDEVINE_DIR), so the fetch needs only the update check. */
  function cachedCdm(): string {
    const cache = join(root, "wv-cache");
    const linux = process.platform === "linux";
    const dir = join(cache, VERSION);
    mkdirSync(join(dir, "_platform_specific", linux ? "linux_x64" : "win_x64"), { recursive: true });
    writeFileSync(join(dir, "manifest.json"), "{}");
    writeFileSync(join(dir, "_platform_specific", linux ? "linux_x64" : "win_x64", linux ? "libwidevinecdm.so" : "widevinecdm.dll"), "cdm");
    process.env.CLEARCOTE_WIDEVINE_DIR = cache;
    return dir;
  }

  /** The update check answers with VERSION; anything else is a network call this test did not expect. */
  function updateCheck(fail = false): void {
    globalThis.fetch = (async (url: unknown) => {
      fetched.push(String(url));
      if (fail) throw new Error("no network");
      if (String(url) !== OMAHA_URL) throw new Error(`unexpected fetch ${String(url)}`);
      return new Response(JSON.stringify({ response: { app: [{ nextversion: VERSION, updatecheck: { status: "ok",
        pipelines: [{ operations: [{ urls: [{ url: "https://example.invalid/cdm.crx3" }], out: { sha256: "ab" } }] }] } }] } }),
      { status: 200 });
    }) as typeof fetch;
  }

  beforeEach(() => {
    realFetch = globalThis.fetch;
    fetched = [];
  });
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  it("only on an engine that has it", async () => {
    const cdm = cachedCdm();
    updateCheck();
    expect(await widevineCdmArgs(fakeEngine([ENGINE_1022], "new"), { quiet: true })).toEqual([`--widevine-cdm-path=${cdm}`]);
    fetched.length = 0;
    expect(await widevineCdmArgs(fakeEngine([], "old"), { quiet: true })).toEqual([]);
    expect(fetched).toEqual([]); // the probe comes first: an older engine costs no network call
  });

  it("is best-effort", async () => {
    cachedCdm();
    updateCheck(true);
    expect(await widevineCdmArgs(fakeEngine([ENGINE_1022]), { quiet: true })).toEqual([]);
    expect(fetched).toEqual([OMAHA_URL]);
  });

  it("a persistent widevine launch passes the CDM path, and still seeds the profile", async () => {
    const cdm = cachedCdm();
    updateCheck();
    const udd = join(root, "udd");
    const context = await launchPersistentContext(udd, { ...base(fakeEngine([ENGINE_1022])), widevine: true });
    const args = stub.launches.at(-1)!.opts.args as string[];
    expect(args.filter((a) => a.startsWith("--widevine-cdm-path="))).toEqual([`--widevine-cdm-path=${cdm}`]);
    expect(existsSync(join(udd, "WidevineCdm", "latest-component-updated-widevine-cdm"))).toBe(true);
    await context.close();
  });

  it("an older engine gets the seeded profile and no CDM path", async () => {
    cachedCdm();
    updateCheck();
    const udd = join(root, "udd");
    const context = await launchPersistentContext(udd, { ...base(fakeEngine()), widevine: true });
    const args = stub.launches.at(-1)!.opts.args as string[];
    expect(args.some((a) => a.startsWith("--widevine-cdm-path"))).toBe(false);
    expect(existsSync(join(udd, "WidevineCdm", "latest-component-updated-widevine-cdm"))).toBe(true);
    await context.close();
  });

  it("a failed CDM fetch leaves DRM off and the launch going", async () => {
    updateCheck(true);
    const context = await launchPersistentContext(join(root, "udd"), { ...base(fakeEngine([ENGINE_1022])), widevine: true });
    expect((stub.launches.at(-1)!.opts.args as string[]).some((a) => a.startsWith("--widevine-cdm-path"))).toBe(false);
    await context.close();
  });
});
