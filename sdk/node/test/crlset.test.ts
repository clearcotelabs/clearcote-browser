// The CRLSet seeding (src/crlset.ts): a port of the Python SDK's tests/test_crlset.py.
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { request } from "node:http";
import { Agent } from "node:http";
import path from "node:path";
import { crc32 } from "node:zlib";
import {
  ALREADY_STARTED_GRACE_SECONDS,
  CRLSET_APP_ID,
  CrlsetReadiness,
  ProbeServer,
  ReadinessState,
  applyCrlsetLaunch,
  crlsetMode,
  crlsetRefreshInFlight,
  fetchCrlset,
  findCrlset,
  installCrlset,
  parseHeader,
  proxyBypassArgs,
  resetCrlsetWarnings,
  resolveCrlset,
  seedCrlset,
  unsuppressComponentUpdater,
} from "../src/crlset.js";
import { tempDir } from "./helpers/temp.js";

function crlsetBytes(sequence: number, body: Buffer = Buffer.alloc(0)): Buffer {
  const header = Buffer.from(JSON.stringify({ Version: 0, ContentType: "CRLSet", Sequence: sequence, BlockedSPKIs: [] }));
  const len = Buffer.alloc(2);
  len.writeUInt16LE(header.length);
  return Buffer.concat([len, header, body]);
}

function write(p: string, data: Buffer): string {
  mkdirSync(path.dirname(p), { recursive: true });
  writeFileSync(p, data);
  return p;
}

const ENV = ["CLEARCOTE_CRLSET", "CLEARCOTE_CRLSET_FILE", "CLEARCOTE_CRLSET_TIMEOUT", "CLEARCOTE_CRLSET_URL",
  "CLEARCOTE_CRLSET_FETCH_TIMEOUT", "CLEARCOTE_NO_WARN", "PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK"];
let saved: Record<string, string | undefined>;
let tmp: string;
let stderr: string[];

beforeEach(() => {
  saved = Object.fromEntries([...ENV, "CLEARCOTE_CRLSET_DIR"].map((k) => [k, process.env[k]]));
  for (const k of ENV) delete process.env[k];
  tmp = tempDir("cc-crlset-");
  process.env.CLEARCOTE_CRLSET_DIR = path.join(tmp, "cache");
  resetCrlsetWarnings();
  // no test reaches the network: a stale cache's background refresh meets this unless a test stubs its own
  vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network disabled in tests"); }));
  stderr = [];
  vi.spyOn(process.stderr, "write").mockImplementation((chunk: unknown) => { stderr.push(String(chunk)); return true; });
});

afterEach(async () => {
  await crlsetRefreshInFlight();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

const err = () => stderr.join("");

describe("crlset files and cache", () => {
  it("parseHeader reads the sequence and rejects other files", () => {
    expect(parseHeader(crlsetBytes(10849, Buffer.alloc(40))).Sequence).toBe(10849);
    const two = Buffer.alloc(2);
    two.writeUInt16LE(2);
    for (const bad of [Buffer.alloc(0), Buffer.from("\x05\x00hello", "latin1"), Buffer.concat([two, Buffer.from("{}")])]) {
      expect(() => parseHeader(bad)).toThrow(/not a CRLSet/);
    }
  });

  it("install takes a file or Chrome's component directory and find returns the newest", () => {
    const chrome = path.join(tmp, "CertificateRevocation");
    write(path.join(chrome, "10848", "crl-set"), crlsetBytes(10848));
    write(path.join(chrome, "10849", "crl-set"), crlsetBytes(10849));
    const cached = installCrlset(chrome);
    expect(cached.endsWith(path.join("10849", "crl-set"))).toBe(true);
    installCrlset(write(path.join(tmp, "old", "crl-set"), crlsetBytes(10700)));
    expect(findCrlset()).toBe(cached);
  });

  it("find prefers the explicit path, then the env file", () => {
    installCrlset(write(path.join(tmp, "a", "crl-set"), crlsetBytes(5)));
    const envFile = write(path.join(tmp, "env", "crl-set"), crlsetBytes(6));
    process.env.CLEARCOTE_CRLSET_FILE = envFile;
    const explicit = write(path.join(tmp, "x", "crl-set"), crlsetBytes(7));
    expect(findCrlset(explicit)).toBe(explicit);
    expect(findCrlset()).toBe(envFile);
  });

  it("find is undefined without a cache", () => {
    expect(findCrlset()).toBeUndefined();
  });

  it("seed writes the component layout and keeps a newer one", () => {
    const profile = path.join(tmp, "profile");
    const src = write(path.join(tmp, "s", "crl-set"), crlsetBytes(10849, Buffer.from("body")));
    expect(seedCrlset(profile, src)).toBe(10849);
    const versionDir = path.join(profile, "CertificateRevocation", "10849");
    expect(readFileSync(path.join(versionDir, "crl-set")).equals(readFileSync(src))).toBe(true);
    expect(JSON.parse(readFileSync(path.join(versionDir, "manifest.json"), "utf8")))
      .toEqual({ manifest_version: 2, name: "crl-set-clearcote.data", version: "10849" });
    write(path.join(profile, "CertificateRevocation", "10900", "crl-set"), crlsetBytes(10900));
    expect(seedCrlset(profile, src)).toBe(10900);
  });
});

describe("crlsetMode", () => {
  it.each([
    [undefined, undefined, ["off", undefined]],
    [undefined, "0", ["off", undefined]],
    [undefined, "1", ["wait", undefined]],
    [undefined, "nowait", ["nowait", undefined]],
    [true, "0", ["wait", undefined]],
    [false, "1", ["off", undefined]],
    ["nowait", undefined, ["nowait", undefined]],
    ["/x/crl-set", undefined, ["wait", "/x/crl-set"]],
  ] as const)("%s with CLEARCOTE_CRLSET=%s", (value, env, expected) => {
    if (env !== undefined) process.env.CLEARCOTE_CRLSET = env;
    expect(crlsetMode(value)).toEqual(expected);
  });

  it("rejects other values", () => {
    expect(() => crlsetMode(3)).toThrow(/crlset must be/);
  });
});

describe("unsuppressComponentUpdater", () => {
  it.each([
    [undefined, ["--disable-component-update"]],
    [["--enable-automation"], ["--enable-automation", "--disable-component-update"]],
    [["--disable-component-update"], ["--disable-component-update"]],
    [true, true],
  ] as const)("%j -> %j", (given, expected) => {
    const opts: { ignoreDefaultArgs?: string[] | boolean } = given === undefined ? {} : { ignoreDefaultArgs: given as string[] | boolean };
    unsuppressComponentUpdater(opts);
    expect(opts.ignoreDefaultArgs).toEqual(expected);
  });
});

describe("applyCrlsetLaunch", () => {
  it("seeds, un-suppresses and removes its options", async () => {
    installCrlset(write(path.join(tmp, "s", "crl-set"), crlsetBytes(42)));
    const opts: Record<string, unknown> = { crlset: true, crlsetTimeout: 4, ignoreDefaultArgs: ["--enable-automation"] };
    const plan = await applyCrlsetLaunch(path.join(tmp, "p"), opts, true);
    expect([plan?.timeout, plan?.sequence]).toEqual([4, 42]);
    expect("crlset" in opts || "crlsetTimeout" in opts).toBe(false);
    expect(opts.ignoreDefaultArgs).toContain("--disable-component-update");
    expect(existsSync(path.join(tmp, "p", "CertificateRevocation", "42", "crl-set"))).toBe(true);
  });

  it("the timeout defaults to CLEARCOTE_CRLSET_TIMEOUT, else 15", async () => {
    installCrlset(write(path.join(tmp, "s", "crl-set"), crlsetBytes(42)));
    expect((await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true }, true))?.timeout).toBe(15);
    process.env.CLEARCOTE_CRLSET_TIMEOUT = "2.5";
    expect((await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true }, true))?.timeout).toBe(2.5);
  });

  it("nowait seeds without a plan", async () => {
    installCrlset(write(path.join(tmp, "s", "crl-set"), crlsetBytes(42)));
    const opts: Record<string, unknown> = { crlset: "nowait" };
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), opts, true)).toBeUndefined();
    expect(opts.ignoreDefaultArgs).toEqual(["--disable-component-update"]);
    expect(existsSync(path.join(tmp, "p", "CertificateRevocation", "42"))).toBe(true);
  });

  it("is silent and untouched by default without a cache", async () => {
    const opts: Record<string, unknown> = {};
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), opts)).toBeUndefined();
    expect(opts).toEqual({});
    expect(err()).toBe("");
  });

  it("warns once when the first fetch fails", async () => {
    const fetchMock = vi.fn(async () => { throw new TypeError("fetch failed: firewall"); });
    vi.stubGlobal("fetch", fetchMock);
    for (let i = 0; i < 2; i++) expect(await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true })).toBeUndefined();
    expect(err().split("could not fetch a CRLSet").length - 1).toBe(1);
    expect(err()).toMatch(/^clearcote: crlset: /);
  });

  it("CLEARCOTE_NO_WARN and quiet silence it", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true }, true)).toBeUndefined();
    process.env.CLEARCOTE_NO_WARN = "1";
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true })).toBeUndefined();
    expect(err()).toBe("");
  });

  it("fetches into an empty cache, then seeds", async () => {
    process.env.CLEARCOTE_CRLSET_URL = "https://mirror.example/crl-set";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(crlsetBytes(10850))));
    const plan = await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true }, true);
    expect(plan?.sequence).toBe(10850);
    expect(findCrlset()?.endsWith(path.join("10850", "crl-set"))).toBe(true);
    const stamp = statSync(path.join(process.env.CLEARCOTE_CRLSET_DIR!, ".last-check"));
    expect(Date.now() - stamp.mtimeMs).toBeLessThan(60_000);
  });

  it("a slow first fetch is not waited for past CLEARCOTE_CRLSET_FETCH_TIMEOUT", async () => {
    process.env.CLEARCOTE_CRLSET_FETCH_TIMEOUT = "0.2";
    process.env.CLEARCOTE_CRLSET_URL = "https://mirror.example/crl-set";
    let release!: () => void;
    const gate = new Promise<void>((r) => { release = r; });
    vi.stubGlobal("fetch", vi.fn(async () => { await gate; return new Response(crlsetBytes(5)); }));
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: true })).toBeUndefined();
    expect(err()).toContain("first download took over 0.2 s");
    release();
    await vi.waitFor(() => expect(findCrlset()).toBeDefined()); // still fetched, for the next launch
  });

  it("a stale cache is used and refreshed in the background", async () => {
    const cached = installCrlset(write(path.join(tmp, "s", "crl-set"), crlsetBytes(10849)));
    process.env.CLEARCOTE_CRLSET_URL = "https://mirror.example/crl-set";
    const fetchMock = vi.fn(async () => new Response(crlsetBytes(10851)));
    vi.stubGlobal("fetch", fetchMock);
    expect(await resolveCrlset()).toBe(cached); // no stamp: stale, the launch uses the cache
    await crlsetRefreshInFlight();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(findCrlset()?.endsWith(path.join("10851", "crl-set"))).toBe(true);
    await resolveCrlset(); // fresh stamp: no refresh
    expect(crlsetRefreshInFlight()).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("is off with crlset: false", async () => {
    installCrlset(write(path.join(tmp, "s", "crl-set"), crlsetBytes(42)));
    process.env.CLEARCOTE_CRLSET = "1";
    const opts: Record<string, unknown> = { crlset: false };
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), opts)).toBeUndefined();
    expect("ignoreDefaultArgs" in opts).toBe(false);
    expect(existsSync(path.join(tmp, "p"))).toBe(false);
  });

  it("never rejects on a broken path", async () => {
    expect(await applyCrlsetLaunch(path.join(tmp, "p"), { crlset: path.join(tmp, "missing") })).toBeUndefined();
    expect(err()).toContain("crlset");
  });
});

describe("fetchCrlset", () => {
  it("from a mirror URL", async () => {
    const seen: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => { seen.push(String(url)); return new Response(crlsetBytes(77)); }));
    process.env.CLEARCOTE_CRLSET_URL = "https://mirror.example/crl-set";
    expect((await fetchCrlset()).endsWith(path.join("77", "crl-set"))).toBe(true);
    expect(seen).toEqual(["https://mirror.example/crl-set"]);
  });

  /** A stored (uncompressed) zip with `files`, inside a CRX3 envelope. */
  function crx(files: Record<string, Buffer>): Buffer {
    const locals: Buffer[] = [];
    const centrals: Buffer[] = [];
    let offset = 0;
    for (const [name, data] of Object.entries(files)) {
      const n = Buffer.from(name);
      const crc = crc32(data);
      const local = Buffer.alloc(30);
      local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt32LE(crc, 14);
      local.writeUInt32LE(data.length, 18); local.writeUInt32LE(data.length, 22); local.writeUInt16LE(n.length, 26);
      const central = Buffer.alloc(46);
      central.writeUInt32LE(0x02014b50, 0); central.writeUInt16LE(20, 4); central.writeUInt16LE(20, 6);
      central.writeUInt32LE(crc, 16); central.writeUInt32LE(data.length, 20); central.writeUInt32LE(data.length, 24);
      central.writeUInt16LE(n.length, 28); central.writeUInt32LE(offset, 42);
      locals.push(local, n, data);
      centrals.push(central, n);
      offset += 30 + n.length + data.length;
    }
    const cd = Buffer.concat(centrals);
    const end = Buffer.alloc(22);
    end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(Object.keys(files).length, 8);
    end.writeUInt16LE(Object.keys(files).length, 10); end.writeUInt32LE(cd.length, 12); end.writeUInt32LE(offset, 16);
    const head = Buffer.alloc(12);
    head.write("Cr24", 0, "latin1"); head.writeUInt32LE(3, 4); head.writeUInt32LE(8, 8);
    return Buffer.concat([head, Buffer.alloc(8), ...locals, cd, end]);
  }

  it.each([true, false])("from Google verifies the package hash (good hash: %s)", async (goodHash) => {
    const blob = crx({ "crl-set": crlsetBytes(10850), "manifest.json": Buffer.from("{}") });
    const digest = goodHash ? createHash("sha256").update(blob).digest("hex") : "00".repeat(32);
    const bodies: any[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)));
        return new Response(")]}'\n" + JSON.stringify({ response: { app: [{ appid: CRLSET_APP_ID, updatecheck: { status: "ok", pipelines: [
          { operations: [{ type: "download", urls: [{ url: "http://dl/c.crx3" }], out: { sha256: digest } }] }] } }] } }));
      }
      expect(url).toBe("http://dl/c.crx3");
      return new Response(blob);
    }));
    if (goodHash) {
      const got = await fetchCrlset();
      expect(parseHeader(readFileSync(got)).Sequence).toBe(10850);
    } else {
      await expect(fetchCrlset()).rejects.toThrow(/sha256/);
      expect(findCrlset()).toBeUndefined();
    }
    expect(bodies[0].request.app[0].appid).toBe("hfnkpimlhhgieaddgfemjhofmfblmnib");
  });
});

describe("readiness probe", () => {
  function get(url: string, agent: Agent): Promise<number> {
    return new Promise((resolve, reject) => {
      const req = request(url, { agent }, (res) => { res.resume(); res.on("end", () => resolve(res.statusCode ?? 0)); });
      req.on("error", reject);
      req.end();
    });
  }

  it("counts connections, not requests", async () => {
    const server = await ProbeServer.start();
    try {
      expect(server.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/$/);
      const agent = new Agent({ keepAlive: true, maxSockets: 1 });
      for (let i = 0; i < 3; i++) expect(await get(server.url, agent)).toBe(204); // keep-alive: one connection
      expect(server.count()).toBe(1);
      agent.destroy();
      const second = new Agent({ keepAlive: false });
      await get(server.url, second);
      second.destroy();
      expect(server.count()).toBe(2);
    } finally {
      server.close();
    }
  });

  it("answers 204 with CORS and an empty body", async () => {
    const server = await ProbeServer.start();
    try {
      const res = await new Promise<{ status?: number; headers: Record<string, unknown>; body: string }>((resolve, reject) => {
        request(server.url, (r) => {
          let body = "";
          r.on("data", (c) => { body += c; });
          r.on("end", () => resolve({ status: r.statusCode, headers: r.headers, body }));
        }).on("error", reject).end();
      });
      expect(res.status).toBe(204);
      expect(res.headers["access-control-allow-origin"]).toBe("*");
      expect(res.body).toBe("");
    } finally {
      server.close();
    }
  });

  it.each([
    [undefined, []],
    [{ server: "http://p:1" }, ["--proxy-bypass-list=<-loopback>;127.0.0.1:9"]],
    [{ server: "http://p:1", bypass: ".corp, intra" }, ["--proxy-bypass-list=*.corp;intra;<-loopback>;127.0.0.1:9"]],
    [{ server: "http://p:1", bypass: "<-loopback>" }, ["--proxy-bypass-list=<-loopback>;127.0.0.1:9"]],
  ] as const)("the probe port bypasses a Playwright proxy last (%j)", (proxy, expected) => {
    const opts = proxy === undefined ? {} : { proxy: { ...proxy } };
    expect(proxyBypassArgs(opts, "127.0.0.1:9")).toEqual(expected);
    expect(opts).toEqual(proxy === undefined ? {} : { proxy }); // the proxy itself is left alone
  });

  it("PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK drops <-loopback>", () => {
    process.env.PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK = "1";
    expect(proxyBypassArgs({ proxy: { server: "http://p:1" } } as never, "127.0.0.1:9")).toEqual(["--proxy-bypass-list=127.0.0.1:9"]);
  });

  it("starts the probe before launch and closes it", async () => {
    const plan = new CrlsetReadiness(1, 42, true);
    const args = await plan.beforeLaunch({ proxy: { server: "http://p:1" } } as never);
    expect(args).toEqual(["--proxy-bypass-list=<-loopback>;" + plan.server!.url.slice("http://".length).replace(/\/$/, "")]);
    plan.close();
    expect(plan.server).toBeUndefined();
    expect(await plan.wait(null)).toBe(false); // nothing to wait with once closed
  });

  it("wait resolves true once the pooled connection is replaced, and closes the server", async () => {
    const plan = new CrlsetReadiness(5, 42, true);
    await plan.beforeLaunch({});
    const server = plan.server!;
    let calls = 0;
    // startup completes before the 2nd probe; the pooled connection is replaced at the 3rd
    const cdp = { send: async () => { if (calls < 1) throw new Error("Cannot find histogram: x"); return {}; }, detach: async () => undefined };
    const page = { evaluate: async () => { calls += 1; if (calls === 3) server.opened.push(performance.now()); else if (calls === 1) server.opened.push(performance.now()); return 204; } };
    const context = { pages: () => [page], newPage: async () => page, newCDPSession: async () => cdp };
    expect(await plan.wait(context as never)).toBe(true);
    expect(calls).toBe(3);
    expect(plan.server).toBeUndefined();
  });

  it("wait times out with one warning and never throws", async () => {
    const plan = new CrlsetReadiness(0.2, 42);
    await plan.beforeLaunch({});
    const page = { evaluate: async () => 204 };
    const cdp = { send: async () => { throw new Error("Cannot find histogram: x"); }, detach: async () => undefined };
    expect(await plan.wait({ pages: () => [page], newCDPSession: async () => cdp } as never)).toBe(false);
    expect(err()).toContain("CRLSet not confirmed live within 0.2 s");
    const broken = new CrlsetReadiness(1, 42);
    await broken.beforeLaunch({});
    expect(await broken.wait({ pages: () => { throw new Error("gone"); } } as never)).toBe(false);
    expect(err()).toContain("readiness check failed (Error: gone)");
  });
});

describe("readiness gate: only a drop after startup counts", () => {
  const opened = (...times: number[]) => ({
    times,
    count() { return this.times.length; },
    reopenedAfter(m: number) { return this.times.slice(1).some((t) => t > m); },
  });

  it("ignores a drop before startup completes (network change at launch)", () => {
    const server = opened(1000, 2000);
    const state = new ReadinessState(server);
    expect(state.ready(3000)).toBe(false);
    state.noteStartup(true, false, 5000);
    expect(state.ready(5100)).toBe(false);
    server.times.push(5600); // the CRLSet's drop
    expect(state.ready(5700)).toBe(true);
  });

  it("counts after a grace when startup was complete at the first check", () => {
    const state = new ReadinessState(opened(10_000));
    state.noteStartup(true, true, 10_000);
    expect(state.ready(10_500)).toBe(false);
    expect(state.ready(10_000 + ALREADY_STARTED_GRACE_SECONDS * 1000)).toBe(true);
  });

  it("falls back to the first drop without the histogram", () => {
    const server = opened(1000);
    const state = new ReadinessState(server);
    state.histogram = false;
    expect(state.ready(2000)).toBe(false);
    server.times.push(2500);
    expect(state.ready(2600)).toBe(true);
  });
});
