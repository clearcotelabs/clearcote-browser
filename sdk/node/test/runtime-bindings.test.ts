// runtimeBindings / CLEARCOTE_RUNTIME_BINDINGS: Runtime.addBinding bindings across navigations, on request.
//
// Patch 110 keeps CDP bindings out of documents created after the binding was added, so Playwright's exposeFunction()
// and exposeBinding() fail after page.goto() (clearcote-browser#43). Engine r54 (pro-r54-04) adds
// --enable-runtime-bindings, which restores only that. Off by default, gated on the binary like stockRuntime, never
// part of the persona payload; redundant next to stockRuntime, which already restores it.
//
// Hermetic, on the stand-ins of stock-runtime.test.ts. Mirrors sdk/python/tests/test_runtime_bindings.py.
import { describe, it, expect, vi, beforeEach, afterEach, afterAll } from "vitest";
import { createServer, type Server as HttpServer } from "node:http";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stub = vi.hoisted(() => ({
  // PROFILE_DIR is read once, at import: point it somewhere empty before the SDK loads, so no saved
  // profile on this machine can change what these tests see.
  savedProfileDir: ((prev) => { process.env.CLEARCOTE_PROFILE_DIR = `${process.cwd()}/.no-saved-profiles`; return prev; })(process.env.CLEARCOTE_PROFILE_DIR),
  launches: [] as Array<{ kind: "persistent" | "incognito"; opts: Record<string, unknown> }>,
  spawns: [] as Array<{ args: string[]; env: Record<string, string | undefined> | undefined }>,
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
  const fakeBrowser = () => {
    const browser = new EventEmitter() as EventEmitter & Record<string, unknown>;
    const ctx = fakeContext();
    browser.contexts = () => [ctx];
    browser.newPage = async () => ({});
    browser.newContext = async () => fakeContext();
    browser.close = async () => {};
    return browser;
  };
  const chromium = {
    launchPersistentContext: async (_userDataDir: string, opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "persistent", opts });
      return fakeContext();
    },
    launch: async (opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "incognito", opts });
      return fakeBrowser();
    },
    connectOverCDP: async () => fakeBrowser(),
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
import { containerEnv, dockerCli, resetOwnerToken, type CliResult } from "../src/docker.js";
import { resetSeenWarnings } from "../src/warnings.js";
import { launch, launchPersistentContext, serve } from "../src/index.js";
import { API_KEY, startFakeCloud, type FakeCloud } from "./helpers/fake-cloud.js";


const SWITCH = "--enable-runtime-bindings";
const STOCK = "--disable-runtime-suppression";
const ENV = "CLEARCOTE_RUNTIME_BINDINGS";
const STOCK_ENV = "CLEARCOTE_STOCK_RUNTIME";
const ENGINE_R32 = "\0disable-runtime-suppression\0";
const ENGINE_R54 = "\0enable-runtime-bindings\0";
const ENGINE_1021 = "\0persona-from-env\0";
const UNSUPPORTED = "runtimeBindings (CLEARCOTE_RUNTIME_BINDINGS) is on, but this engine does not support it";
const CLOUD_ONLY_LOCAL = "runtimeBindings (CLEARCOTE_RUNTIME_BINDINGS) only applies to local and Docker launches";
const BINDINGS_NOTE = "page.exposeFunction()"; // in the engine note about bindings
const CONSOLE_NOTE = "page.on('console')"; // in the engine note about console events

afterAll(() => {
  if (stub.savedProfileDir === undefined) delete process.env.CLEARCOTE_PROFILE_DIR;
  else process.env.CLEARCOTE_PROFILE_DIR = stub.savedProfileDir;
});

// No licence (a key would select a lease), nothing inherited from the shell, warnings on, and a TEMP of our own:
// everything a test or the code under test creates goes under `root`, removed after.
const ENV_KEYS = ["HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY",
  "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_API_KEY",
  "CLEARCOTE_API_URL", ENV, STOCK_ENV, P.OPT_OUT_ENV, P.ENV_VAR] as const;
let savedEnv: Record<string, string | undefined>;
let root: string;
let stderr: string[];

beforeEach(() => {
  savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  root = mkdtempSync(join(tmpdir(), "cc-rtbind-"));
  const temp = join(root, "temp");
  const home = join(root, "home");
  for (const d of [temp, home]) mkdirSync(d);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.TEMP = temp;
  process.env.TMP = temp;
  process.env.TMPDIR = temp;
  for (const k of ["CLEARCOTE_NO_WARN", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION",
    "CLEARCOTE_CLOUD", "CLEARCOTE_DOCKER", "CLEARCOTE_API_KEY", "CLEARCOTE_API_URL", ENV, STOCK_ENV, P.OPT_OUT_ENV, P.ENV_VAR]) delete process.env[k];
  stub.launches.length = 0;
  stub.spawns.length = 0;
  resetSeenWarnings();
  stderr = [];
  vi.spyOn(process.stderr, "write").mockImplementation(((chunk: unknown) => { stderr.push(String(chunk)); return true; }) as never);
});

afterEach(() => {
  vi.restoreAllMocks();
  resetSeenWarnings();
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
  const p = join(dir, "chrome");
  writeFileSync(p, Buffer.from(`\x7fELF${markers.join("")}\0--remote-debugging-port\0`, "latin1"));
  return p;
}

const said = (text: string) => stderr.join("").split(text).length - 1;
const base = (exe: string, quiet = true) => ({ executablePath: exe, headless: false, quiet });
const lastLaunch = () => stub.launches.at(-1)!;
const lastArgs = () => lastLaunch().opts.args as string[];
const count = (args: string[]) => args.filter((a) => a === SWITCH).length;

/** Launch through `entry`; returns the arguments Playwright was asked to launch with. */
async function local(entry: string, options: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (entry === "launch") await (await launch(options as never)).close();
  else if (entry === "launch-incognito") await (await launch({ ...options, ephemeralProfile: false } as never)).close();
  else if (entry === "persistent") await (await launchPersistentContext(join(root, "udd"), options as never)).close();
  else throw new Error(entry);
  return lastLaunch().opts;
}


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


// -- on, off, and the environment variable --------------------------------------------------------

describe("on, off, and the environment variable", () => {
  it.each(["launch", "launch-incognito", "persistent"])("%s: on with an r54 engine, the switch is there exactly once", async (entry) => {
    const exe = fakeEngine([ENGINE_R32, ENGINE_R54]);
    let opts = await local(entry, { ...base(exe), runtimeBindings: true });
    expect(count(opts.args as string[])).toBe(1);
    expect(opts.args).not.toContain(STOCK);
    expect(opts).not.toHaveProperty("runtimeBindings"); // an SDK option, never a Playwright one
    // the caller passed it too: still once
    opts = await local(entry, { ...base(exe), runtimeBindings: true, args: [SWITCH, "--lang=de-DE"] });
    expect(count(opts.args as string[])).toBe(1);
    expect(opts.args).toContain("--lang=de-DE");
  });

  it("off by default, the environment variable, and an explicit option wins over it", async () => {
    const exe = fakeEngine([ENGINE_R54]);
    const udd = join(root, "udd");
    for (const off of [undefined, false]) {
      await (await launchPersistentContext(udd, { ...base(exe), runtimeBindings: off })).close();
      expect(lastArgs()).not.toContain(SWITCH);
      expect(lastLaunch().opts).not.toHaveProperty("runtimeBindings");
    }
    for (const [value, on] of [["1", true], ["true", true], ["YES", true], [" on ", true],
      ["0", false], ["", false], ["no", false], ["off", false], ["2", false]] as Array<[string, boolean]>) {
      process.env[ENV] = value;
      await (await launchPersistentContext(udd, base(exe))).close();
      expect(count(lastArgs()), value).toBe(on ? 1 : 0);
    }
    process.env[ENV] = "1";
    await (await launchPersistentContext(udd, { ...base(exe), runtimeBindings: false })).close();
    expect(lastArgs()).not.toContain(SWITCH); // the option wins
    process.env[ENV] = "0";
    await (await launchPersistentContext(udd, { ...base(exe), runtimeBindings: true })).close();
    expect(count(lastArgs())).toBe(1);
  });

  it("is not added next to stockRuntime, which already restores it", async () => {
    const exe = fakeEngine([ENGINE_R32, ENGINE_R54]);
    await (await launchPersistentContext(join(root, "udd"), { ...base(exe), runtimeBindings: true, stockRuntime: true })).close();
    expect(lastArgs().filter((a) => a === STOCK)).toHaveLength(1);
    expect(lastArgs()).not.toContain(SWITCH);
    // the caller's own stock switch counts too
    await (await launchPersistentContext(join(root, "udd"), { ...base(exe), runtimeBindings: true, args: [STOCK] })).close();
    expect(lastArgs()).not.toContain(SWITCH);
  });
});

// -- an engine without the switch -------------------------------------------------------------------

describe("an engine without the switch", () => {
  it("launches without it and says so once", async () => {
    const exe = fakeEngine([ENGINE_R32]); // r32..r53, or an open build without the switch
    for (let i = 0; i < 2; i++) {
      await (await launchPersistentContext(join(root, "udd"), { ...base(exe, false), runtimeBindings: true })).close();
      expect(lastArgs()).not.toContain(SWITCH);
      expect(lastLaunch().opts).not.toHaveProperty("runtimeBindings");
    }
    expect(said(UNSUPPORTED)).toBe(1);
    const line = stderr.join("").split("\n").find((l) => l.includes(UNSUPPORTED))!;
    expect(line.startsWith("clearcote: warning: ")).toBe(true);
    expect(line).toContain("r54");
    expect(line).toContain("stockRuntime: true");
  });

  it("quiet and CLEARCOTE_NO_WARN silence it without using it up", async () => {
    const exe = fakeEngine();
    const udd = join(root, "udd");
    await (await launchPersistentContext(udd, { ...base(exe, true), runtimeBindings: true })).close();
    process.env.CLEARCOTE_NO_WARN = "1";
    await (await launchPersistentContext(udd, { ...base(exe, false), runtimeBindings: true })).close();
    expect(said(UNSUPPORTED)).toBe(0);
    delete process.env.CLEARCOTE_NO_WARN;
    await (await launchPersistentContext(udd, { ...base(exe, false), runtimeBindings: true })).close();
    expect(said(UNSUPPORTED)).toBe(1);
  });
});

// -- serve() ----------------------------------------------------------------------------------------

describe("serve()", () => {
  it("on, off, and on an engine without the switch", async () => {
    const { port, close } = await fakeCdpEndpoint();
    try {
      const served = async (options: Record<string, unknown>) => {
        await (await serve({ port, ...options } as never)).close();
        return stub.spawns.at(-1)!.args;
      };
      expect(count(await served({ ...base(fakeEngine([ENGINE_R54], "new")), runtimeBindings: true }))).toBe(1);
      expect(count(await served(base(fakeEngine([ENGINE_R54], "new"))))).toBe(0); // off by default
      expect(count(await served({ ...base(fakeEngine([], "old"), false), runtimeBindings: true }))).toBe(0);
      expect(said(UNSUPPORTED)).toBe(1);
    } finally {
      await close();
    }
  });
});

// -- not a persona switch ---------------------------------------------------------------------------

describe("not a persona switch", () => {
  it("stays on the command line when the persona moves to the environment", async () => {
    const exe = fakeEngine([ENGINE_1021, ENGINE_R54]);
    await (await launchPersistentContext(join(root, "udd"), { ...base(exe), fingerprint: "17", platform: "windows", runtimeBindings: true })).close();
    const args = lastArgs();
    expect(args).toContain("--persona-from-env"); // the persona did move...
    expect(count(args)).toBe(1); // ...and this switch did not
    const entries = P.decode((lastLaunch().opts.env as Record<string, string>)[P.ENV_VAR]);
    expect(entries.map(([name]) => name)).not.toContain("enable-runtime-bindings");
    expect(P.isTransported("enable-runtime-bindings")).toBe(false);
  });
});

// -- Docker -----------------------------------------------------------------------------------------

describe("Docker", () => {
  it("hands the switch to the image's entrypoint with the other engine switches", () => {
    expect(containerEnv({ runtimeBindings: false })).toEqual({});
    expect(containerEnv({ runtimeBindings: true })).toEqual({ CC_EXTRA_ARGS: SWITCH });
    expect(containerEnv({ runtimeBindings: true, args: [SWITCH] }).CC_EXTRA_ARGS).toBe(SWITCH); // once
    // the entrypoint drops it next to stockRuntime (docker/serve.py), as a local launch does
    expect(containerEnv({ runtimeBindings: true, stockRuntime: true }).CC_EXTRA_ARGS).toBe(`${STOCK} ${SWITCH}`);
    process.env[ENV] = "1";
    expect(containerEnv({}).CC_EXTRA_ARGS).toBe(SWITCH);
    expect(containerEnv({ runtimeBindings: false })).toEqual({}); // the option wins
  });

  it("a Docker launch takes the option", async () => {
    // A docker CLI that refuses the create: what launch() hands the image is in that call's environment.
    const calls: Array<{ argv: string[]; env: Record<string, string> }> = [];
    vi.spyOn(dockerCli, "which").mockReturnValue("docker");
    vi.spyOn(dockerCli, "run").mockImplementation(async (argv: string[], env?: Record<string, string>): Promise<CliResult> => {
      calls.push({ argv: [...argv], env: { ...(env ?? {}) } });
      if (argv[1] === "info") return { code: 0, stdout: "29.1.3 x86_64\n", stderr: "" };
      if (argv[1] === "image") return { code: 0, stdout: JSON.stringify({ "com.clearcotelabs.serve-protocol": "3" }) + "\n", stderr: "" };
      if (argv[1] === "create") return { code: 125, stdout: "", stderr: "refused by the test" };
      return { code: 0, stdout: "", stderr: "" };
    });
    resetOwnerToken();
    try {
      await expect(launch({ docker: true, runtimeBindings: true, headless: true, quiet: true } as never)).rejects.toThrow(/refused by the test/);
    } finally {
      resetOwnerToken();
    }
    expect(calls.find((c) => c.argv[1] === "create")?.env.CC_EXTRA_ARGS).toBe(SWITCH);
  });
});

// -- cloud ------------------------------------------------------------------------------------------

describe("cloud", () => {
  let api: FakeCloud;
  beforeEach(async () => {
    api = await startFakeCloud();
    process.env.CLEARCOTE_API_KEY = API_KEY;
    process.env.CLEARCOTE_API_URL = api.url;
  });
  afterEach(async () => {
    await api.close();
  });

  it("a cloud launch does not take it, and says so once", async () => {
    await (await launch({ cloud: true, runtimeBindings: true, country: "us" } as never)).close();
    await (await launch({ cloud: true, runtimeBindings: true } as never)).close();
    const bodies = api.requests("POST", "/api/v1/browsers").map((r) => r.body as Record<string, unknown>);
    expect(bodies[0]).toEqual({ country: "us", humanize: false }); // nothing about it reaches the API
    expect(bodies.flatMap((b) => Object.keys(b)).filter((k) => /runtime|binding/i.test(k))).toEqual([]);
    expect(said(CLOUD_ONLY_LOCAL)).toBe(1);
    expect(stderr.join("").split("\n").find((l) => l.includes(CLOUD_ONLY_LOCAL))!.startsWith("clearcote: warning: runtimeBindings")).toBe(true);
    // the environment variable too
    resetSeenWarnings();
    process.env[ENV] = "1";
    await (await launch({ cloud: true } as never)).close();
    expect(said(CLOUD_ONLY_LOCAL)).toBe(2);
    resetSeenWarnings();
    await (await launch({ cloud: true, quiet: true, runtimeBindings: true } as never)).close(); // quiet: not said
    expect(said(CLOUD_ONLY_LOCAL)).toBe(2);
  });
});

// -- the bindings note ------------------------------------------------------------------------------

describe("the bindings note", () => {
  it("names the option, and is not said when bindings are restored", async () => {
    const exe = fakeEngine([ENGINE_R32, ENGINE_R54]);
    const udd = join(root, "udd");
    await (await launchPersistentContext(udd, { ...base(exe, false), runtimeBindings: true })).close();
    expect(said(BINDINGS_NOTE)).toBe(0); // this browser keeps them
    expect(said(CONSOLE_NOTE)).toBe(1); // console events stay suppressed
    await (await launchPersistentContext(udd, { ...base(exe, false), stockRuntime: true })).close();
    expect(said(BINDINGS_NOTE)).toBe(0);
    await (await launchPersistentContext(udd, base(exe, false))).close();
    const note = stderr.join("").split("\n").filter((l) => l.includes(BINDINGS_NOTE));
    expect(note).toHaveLength(1);
    expect(note[0].startsWith("clearcote: note: ")).toBe(true);
    expect(note[0]).toContain("runtimeBindings: true");
    expect(note[0]).toContain("r54");
  });
});
