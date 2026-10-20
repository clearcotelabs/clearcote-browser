// Per-machine token-reuse tests: acquireLease shares ONE checkout across many
// launches in a process. Hermetic — only a mocked global fetch; HOME is a temp dir
// so the on-disk cache/instance_id are isolated. Uses a unique license key per test
// so the module-level machine-lease registry never leaks between cases.
import { describe, it, expect, afterEach, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { acquireLease, getSessionSeats } from "../src/license.js";

const realFetch = globalThis.fetch;

function mockBackend() {
  const calls: string[] = [];
  globalThis.fetch = (async (url: unknown) => {
    const ep = String(url).split("/").pop()!;
    calls.push(ep);
    const now = Math.floor(Date.now() / 1000);
    if (ep === "checkout")
      return new Response(
        JSON.stringify({ lease_id: "L" + calls.length, token: "TOK-" + calls.length, exp: now + 800,
          lease_ttl_sec: 810, heartbeat_interval_sec: 270, concurrency: { used: 1, limit: 5 } }),
        { status: 200 });
    if (ep === "heartbeat") return new Response(JSON.stringify({ token: "TOK-hb", exp: now + 800 }), { status: 200 });
    return new Response("{}", { status: 200 });
  }) as typeof fetch;
  return calls;
}

/** A temp HOME for one test, removed after it (tempHome below; each run used to leave one per test in TEMP). */
function isolateHome(): void {
  tempHome();
}

function writeCacheFile(key: string, obj: unknown): void {
  const id = createHash("sha256").update(key).digest("hex").slice(0, 16);
  const dir = join(process.env.HOME!, ".clearcote");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `lease-${id}.json`), JSON.stringify(obj));
}

describe("acquireLease — per-machine token reuse", () => {
  const OLD = {
    key: process.env.CLEARCOTE_LICENSE_KEY, api: process.env.CLEARCOTE_LICENSE_API,
    home: process.env.HOME, prof: process.env.USERPROFILE, iid: process.env.CLEARCOTE_INSTANCE_ID,
  };
  afterEach(() => {
    globalThis.fetch = realFetch;
    const restore: Record<string, string | undefined> = {
      CLEARCOTE_LICENSE_KEY: OLD.key, CLEARCOTE_LICENSE_API: OLD.api,
      HOME: OLD.home, USERPROFILE: OLD.prof, CLEARCOTE_INSTANCE_ID: OLD.iid,
    };
    for (const [k, v] of Object.entries(restore)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
    for (const h of homes.splice(0)) rmSync(h, { recursive: true, force: true });
  });

  it("shares ONE checkout across N launches in a process; stop() does not checkin", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_reuse_" + Date.now();
    const calls = mockBackend();
    const h1 = await acquireLease({ quiet: true });
    const h2 = await acquireLease({ quiet: true });
    const h3 = await acquireLease({ quiet: true });
    expect(calls.filter((c) => c === "checkout").length).toBe(1);
    expect(h1?.token).toBeTruthy();
    expect(h2?.token).toBe(h1?.token);
    await h1?.stop(); await h2?.stop(); await h3?.stop();
    expect(calls.filter((c) => c === "checkin").length).toBe(0);
  });

  it("free mode (no key) returns null and makes no calls", async () => {
    isolateHome();
    delete process.env.CLEARCOTE_LICENSE_KEY;
    const calls = mockBackend();
    const r = await acquireLease({ quiet: true });
    expect(r).toBeNull();
    expect(calls.length).toBe(0);
  });

  it("throws on a definitive concurrency-limit verdict (cold checkout)", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_limit_" + Date.now();
    globalThis.fetch = (async (url: unknown) => {
      const ep = String(url).split("/").pop();
      if (ep === "checkout")
        return new Response(JSON.stringify({ error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }), { status: 429 });
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    await expect(acquireLease({ quiet: true })).rejects.toMatchObject({ code: "CONCURRENCY_LIMIT_EXCEEDED" });
  });

  it("reuses a valid on-disk token from another process (0 checkout)", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    const key = "cc_lic_disk_" + Date.now();
    process.env.CLEARCOTE_LICENSE_KEY = key;
    writeCacheFile(key, { token: "DISK-TOK", exp: Math.floor(Date.now() / 1000) + 800, lease_id: "Ld", scope: "machine" });
    const calls = mockBackend();
    const h = await acquireLease({ quiet: true });
    expect(calls.filter((c) => c === "checkout").length).toBe(0);
    expect(h?.token).toBe("DISK-TOK");
  });

  it("checkout body carries sdk_version + resolved engine_version (resolver runs once)", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_tel_" + Date.now();
    const bodies: Record<string, unknown>[] = [];
    globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
      const ep = String(url).split("/").pop();
      if (ep === "checkout") {
        bodies.push(JSON.parse(String(init?.body ?? "{}")));
        const now = Math.floor(Date.now() / 1000);
        return new Response(JSON.stringify({ lease_id: "L1", token: "T1", exp: now + 800,
          lease_ttl_sec: 810, heartbeat_interval_sec: 270, concurrency: { used: 1, limit: 5 } }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    let resolved = 0;
    const engineVersion = () => { resolved++; return "150.0.7871.114"; };
    await acquireLease({ quiet: true, sdkVersion: "0.17.1", engineVersion });
    await acquireLease({ quiet: true, sdkVersion: "0.17.1", engineVersion }); // reuse -> no 2nd checkout
    expect(bodies.length).toBe(1);
    expect(bodies[0].sdk_version).toBe("0.17.1");
    expect(bodies[0].engine_version).toBe("150.0.7871.114");
    expect(resolved).toBe(1); // memoized, resolved once on the cold checkout
  });

  it("a throwing engine resolver is soft — checkout still succeeds, field omitted", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_soft_" + Date.now();
    let body: Record<string, unknown> = {};
    globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
      const ep = String(url).split("/").pop();
      if (ep === "checkout") {
        body = JSON.parse(String(init?.body ?? "{}"));
        const now = Math.floor(Date.now() / 1000);
        return new Response(JSON.stringify({ lease_id: "L1", token: "T1", exp: now + 800 }), { status: 200 });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    const h = await acquireLease({ quiet: true, sdkVersion: "0.17.1",
      engineVersion: () => { throw new Error("catalog down"); } });
    expect(h?.token).toBe("T1");            // launch still works
    expect(body.engine_version).toBeUndefined(); // omitted, not fatal
  });

  it("reads a cache without lease_id (backwards compat, 0 checkout)", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    const key = "cc_lic_legacy_" + Date.now();
    process.env.CLEARCOTE_LICENSE_KEY = key;
    writeCacheFile(key, { token: "LEGACY", exp: Math.floor(Date.now() / 1000) + 800, scope: "machine" }); // no lease_id
    const calls = mockBackend();
    const h = await acquireLease({ quiet: true });
    expect(calls.filter((c) => c === "checkout").length).toBe(0);
    expect(h?.token).toBe("LEGACY");
  });
});

// ── shared by the two suites below ───────────────────────────────────────────────────────────────────
const ENV_KEYS = ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "HOME", "USERPROFILE", "CLEARCOTE_INSTANCE_ID"] as const;
function snapshotEnv() {
  const saved = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  return () => {
    for (const [k, v] of Object.entries(saved)) {
      if (v === undefined) delete process.env[k];
      else process.env[k] = v;
    }
  };
}
const homes: string[] = [];
function tempHome(): void {
  const home = mkdtempSync(join(tmpdir(), "cc-lease-"));
  homes.push(home);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  delete process.env.CLEARCOTE_INSTANCE_ID;
}

// ── heartbeat 409: reclaimed/expired -> re-checkout ─────────────────────────────────────────────────
// The backend answers a heartbeat for a lease it no longer holds with 409 (LEASE_NOT_FOUND / LEASE_EXPIRED)
// and the SDK must re-checkout to keep its slot. The per-browser (free) loop is covered in
// license-per-browser.test.ts; these drive the MACHINE (paid) loop with fake timers.
describe("acquireLease — machine lease heartbeat 409 recovery", () => {
  let restoreEnv = () => {};
  afterEach(() => {
    globalThis.fetch = realFetch;
    vi.useRealTimers();
    restoreEnv();
    for (const h of homes.splice(0)) rmSync(h, { recursive: true, force: true });
  });

  type Step = [endpoint: string, status: number, answer: unknown];
  /** The cold checkout gets L1; after that, heartbeat/checkout answers come from `script` in order. */
  function scripted(script: Step[]) {
    const calls: { ep: string; body: Record<string, unknown> }[] = [];
    globalThis.fetch = (async (url: unknown, init?: { body?: string }) => {
      const ep = String(url).split("/").pop()!;
      calls.push({ ep, body: init?.body ? JSON.parse(init.body) : {} });
      const now = Math.floor(Date.now() / 1000);
      if (ep === "checkout" && calls.filter((c) => c.ep === "checkout").length === 1)
        return new Response(JSON.stringify({ lease_id: "L1", token: "TOK-1", exp: now + 800, lease_ttl_sec: 810, heartbeat_interval_sec: 270, concurrency: { used: 1, limit: 5 } }), { status: 200 });
      if (script.length && script[0][0] === ep) {
        const [, status, answer] = script.shift()!;
        if (answer instanceof Error) throw answer;
        return new Response(JSON.stringify(answer), { status });
      }
      return new Response("{}", { status: 200 });
    }) as typeof fetch;
    return calls;
  }
  const beat = () => vi.advanceTimersByTimeAsync(270_000);
  function setup() {
    restoreEnv = snapshotEnv();
    tempHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_hb409_" + Date.now() + "_" + Math.random().toString(36).slice(2);
    vi.useFakeTimers();
  }

  it("re-checks out and heartbeats the NEW lease", async () => {
    setup();
    const now = Math.floor(Date.now() / 1000);
    const calls = scripted([
      ["heartbeat", 409, { code: "LEASE_EXPIRED" }],
      ["checkout", 200, { lease_id: "L2", token: "TOK-2", exp: now + 900 }],
      ["heartbeat", 200, { token: "TOK-3", exp: now + 1000 }],
    ]);
    const h = await acquireLease({ quiet: true });
    await beat();
    await beat();
    expect(calls.map((c) => c.ep)).toEqual(["checkout", "heartbeat", "checkout", "heartbeat"]);
    expect(calls[1].body.lease_id).toBe("L1"); // the refused beat
    expect(calls[3].body.lease_id).toBe("L2"); // the next beat holds the re-checked-out lease
    expect(h!.token).toBe("TOK-3");
  });

  it("a refused re-checkout changes nothing, and the next beat tries again", async () => {
    setup();
    const now = Math.floor(Date.now() / 1000);
    const calls = scripted([
      ["heartbeat", 409, { code: "LEASE_EXPIRED" }],
      ["checkout", 429, { code: "CONCURRENCY_LIMIT_EXCEEDED" }],
      ["heartbeat", 409, { code: "LEASE_EXPIRED" }],
      ["checkout", 200, { lease_id: "L9", token: "TOK-9", exp: now + 900 }],
      ["heartbeat", 200, { token: "TOK-10", exp: now + 1000 }],
    ]);
    const h = await acquireLease({ quiet: true });
    await beat();
    expect(h!.token).toBe("TOK-1");
    await beat();
    await beat();
    expect(calls.map((c) => c.ep)).toEqual(["checkout", "heartbeat", "checkout", "heartbeat", "checkout", "heartbeat"]);
    expect(calls[5].body.lease_id).toBe("L9");
    expect(h!.token).toBe("TOK-10");
  });

  it("a network error on the re-checkout is retried on the next beat", async () => {
    setup();
    const now = Math.floor(Date.now() / 1000);
    const calls = scripted([
      ["heartbeat", 409, { code: "LEASE_NOT_FOUND" }],
      ["checkout", 0, new Error("ECONNRESET")],
      ["heartbeat", 409, { code: "LEASE_NOT_FOUND" }],
      ["checkout", 200, { lease_id: "L5", token: "TOK-5", exp: now + 900 }],
    ]);
    const h = await acquireLease({ quiet: true });
    await beat();
    await beat();
    expect(calls.map((c) => c.ep)).toEqual(["checkout", "heartbeat", "checkout", "heartbeat", "checkout"]);
    expect(h!.token).toBe("TOK-5");
  });
});

// ── User-Agent: every licence call names the SDK and its version ────────────────────────────────────
// So the licence server's logs can tell SDK builds apart (2026-09-29: the only clients stuck on 409s sent
// no User-Agent at all, and nothing in the logs said who they were). Checked on the wire, with a real
// local server, so a duplicate header or fetch's own "node" default would show.
describe("licence calls — User-Agent on the wire", () => {
  const WANT = `clearcote-sdk-node/${JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version}`;
  let restoreEnv = () => {};
  let server: Server | null = null;
  afterEach(async () => {
    restoreEnv();
    for (const h of homes.splice(0)) rmSync(h, { recursive: true, force: true });
    await new Promise<void>((r) => (server ? server.close(() => r()) : r()));
    server = null;
  });

  /** A local server answering checkout/seats, recording each request's target and ALL its User-Agent headers. */
  async function listen() {
    const seen: { target: string; uas: string[] }[] = [];
    server = createServer((req: IncomingMessage, res) => {
      const uas: string[] = [];
      for (let i = 0; i < req.rawHeaders.length; i += 2) if (req.rawHeaders[i].toLowerCase() === "user-agent") uas.push(req.rawHeaders[i + 1]);
      seen.push({ target: req.url ?? "", uas });
      req.resume();
      req.on("end", () => {
        const now = Math.floor(Date.now() / 1000);
        const body = req.url!.endsWith("/checkout")
          ? { lease_id: "L1", token: "TOK", exp: now + 800, lease_ttl_sec: 810, heartbeat_interval_sec: 3600, concurrency: { used: 1, limit: 5 } }
          : { used: 1, limit: 5 };
        res.writeHead(200, { "content-type": "application/json", connection: "close" }).end(JSON.stringify(body));
      });
    });
    await new Promise<void>((r) => server!.listen(0, "127.0.0.1", () => r()));
    return { base: `http://127.0.0.1:${(server!.address() as AddressInfo).port}`, seen };
  }
  function setup(key: string) {
    restoreEnv = snapshotEnv();
    tempHome();
    process.env.CLEARCOTE_LICENSE_KEY = key + Date.now() + "_" + Math.random().toString(36).slice(2);
  }

  it("direct: checkout and seats each carry exactly one User-Agent naming the SDK version", async () => {
    setup("cc_lic_ua_direct_");
    const { base, seen } = await listen();
    process.env.CLEARCOTE_LICENSE_API = base;
    await acquireLease({ quiet: true });
    expect((await getSessionSeats({})).state).toBe("ok");
    expect(seen).toEqual([
      { target: "/api/v1/lease/checkout", uas: [WANT] },
      { target: "/api/v1/lease/seats", uas: [WANT] },
    ]);
  });

  it("through the launch proxy: replaces the proxy path's own default instead of adding a second header", async () => {
    setup("cc_lic_ua_proxy_");
    const { base, seen } = await listen();
    // An http:// API through an http:// proxy goes absolute-form, straight to the proxy: the local server
    // plays the proxy and sees the exact request the SDK wrote.
    process.env.CLEARCOTE_LICENSE_API = "http://licence.test";
    await acquireLease({ quiet: true, licenseThroughProxy: true, proxy: base });
    expect(seen).toEqual([{ target: "http://licence.test/api/v1/lease/checkout", uas: [WANT] }]);
  });
});
