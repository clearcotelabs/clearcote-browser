// r32: leave prefers-color-scheme to the engine's persona (Playwright emulates light by default).
// The default is gated on the engine binary having --fingerprint-color-scheme, so an older engine
// launches exactly as before, and a caller's own colorScheme always wins.
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const stub = vi.hoisted(() => ({
  launches: [] as Array<{ kind: "persistent" | "incognito"; opts: Record<string, unknown> }>,
  pageOpts: [] as Array<Record<string, unknown>>,
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
    launchPersistentContext: async (_dir: string, opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "persistent", opts });
      return fakeContext();
    },
    launch: async (opts: Record<string, unknown>) => {
      stub.launches.push({ kind: "incognito", opts });
      const browser = new EventEmitter() as EventEmitter & Record<string, unknown>;
      browser.newPage = async (o: Record<string, unknown> = {}) => { stub.pageOpts.push(o); return {}; };
      browser.newContext = async (o: Record<string, unknown> = {}) => { stub.pageOpts.push(o); return fakeContext(); };
      browser.close = async () => {};
      return browser;
    },
  };
  return { ...actual, chromium };
});

import { launch, launchPersistentContext } from "../src/index.js";
import { COLOR_SCHEME_SWITCH, defaultColorScheme, engineDecidesColorScheme, installColorSchemeDefault }
  from "../src/colorscheme.js";

let dir: string;
const saved: Record<string, string | undefined> = {};
const ENV = ["HOME", "USERPROFILE", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_BINARY", "CLEARCOTE_BROWSER_VERSION", "CLEARCOTE_CLOUD"];

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "cc-colorscheme-"));
  for (const k of ENV) saved[k] = process.env[k];
  // a clean home: no saved licence key or profiles of this machine reach these launches
  process.env.HOME = dir;
  process.env.USERPROFILE = dir;
  for (const k of ENV.slice(2)) delete process.env[k];
  stub.launches.length = 0;
  stub.pageOpts.length = 0;
});

afterEach(() => {
  for (const k of ENV) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  rmSync(dir, { recursive: true, force: true });
});

function engine(r32: boolean): string {
  const exe = join(dir, r32 ? "chrome-r32" : "chrome-r31");
  writeFileSync(exe, Buffer.from(`\0fingerprint-platform\0${r32 ? "\0fingerprint-color-scheme\0" : ""}`, "latin1"));
  return exe;
}

describe("colour-scheme helpers", () => {
  it("turns the emulation off only when the caller chose nothing", () => {
    expect(defaultColorScheme({})).toEqual({ colorScheme: null });
    expect(defaultColorScheme({ colorScheme: "dark" })).toEqual({ colorScheme: "dark" });
    expect(defaultColorScheme({ colorScheme: undefined })).toEqual({ colorScheme: undefined });
    expect(defaultColorScheme({ viewport: null })).toEqual({ viewport: null, colorScheme: null });
  });

  it("probes the engine binary for the switch literal", () => {
    expect(COLOR_SCHEME_SWITCH).toBe("fingerprint-color-scheme");
    expect(engineDecidesColorScheme(engine(true))).toBe(true);
    expect(engineDecidesColorScheme(engine(false))).toBe(false);
    expect(engineDecidesColorScheme(undefined)).toBe(false);
  });

  it("wraps a browser's newPage and newContext", async () => {
    const seen: Array<[string, Record<string, unknown>]> = [];
    const b = {
      newPage: async (o: Record<string, unknown> = {}) => { seen.push(["page", o]); },
      newContext: async (o: Record<string, unknown> = {}) => { seen.push(["context", o]); },
    };
    installColorSchemeDefault(b as never);
    await (b.newPage as (o?: object) => Promise<void>)();
    await (b.newContext as (o?: object) => Promise<void>)({ colorScheme: "light" });
    expect(seen).toEqual([["page", { colorScheme: null }], ["context", { colorScheme: "light" }]]);
  });
});

describe("launch entry points", () => {
  const base = (exe: string) => ({ executablePath: exe, headless: false, quiet: true });

  it("persistent context on an r32 engine: emulation off", async () => {
    await launchPersistentContext(join(dir, "prof"), base(engine(true)));
    expect(stub.launches[0].opts.colorScheme).toBeNull();
    expect("colorScheme" in stub.launches[0].opts).toBe(true);
  });

  it("persistent context on an older engine: unchanged", async () => {
    await launchPersistentContext(join(dir, "prof"), base(engine(false)));
    expect("colorScheme" in stub.launches[0].opts).toBe(false);
  });

  it("persistent context keeps the caller's colorScheme", async () => {
    await launchPersistentContext(join(dir, "prof"), { ...base(engine(true)), colorScheme: "dark" });
    expect(stub.launches[0].opts.colorScheme).toBe("dark");
  });

  it("default launch() goes through the throwaway profile with the default", async () => {
    const b = await launch(base(engine(true)));
    expect(stub.launches[0].kind).toBe("persistent");
    expect(stub.launches[0].opts.colorScheme).toBeNull();
    await b.close();
  });

  it("incognito launch on an r32 engine: new pages default to no emulation", async () => {
    const b = await launch({ ...base(engine(true)), ephemeralProfile: false });
    expect(stub.launches[0].kind).toBe("incognito");
    expect("colorScheme" in stub.launches[0].opts).toBe(false);
    await b.newPage();
    await b.newContext({ colorScheme: "light" });
    expect(stub.pageOpts).toEqual([{ viewport: null, colorScheme: null }, { viewport: null, colorScheme: "light" }]);
  });

  it("incognito launch on an older engine: unchanged", async () => {
    const b = await launch({ ...base(engine(false)), ephemeralProfile: false });
    await b.newPage();
    expect(stub.pageOpts).toEqual([{ viewport: null }]);
  });
});
