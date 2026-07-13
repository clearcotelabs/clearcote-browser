// Per-machine token-reuse tests: acquireLease shares ONE checkout across many
// launches in a process. Hermetic — only a mocked global fetch; HOME is a temp dir
// so the on-disk cache/instance_id are isolated. Uses a unique license key per test
// so the module-level machine-lease registry never leaks between cases.
import { describe, it, expect, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir, homedir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { acquireLease } from "../src/license.js";

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

function isolateHome(): void {
  const home = mkdtempSync(join(tmpdir(), "cc-lease-"));
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  delete process.env.CLEARCOTE_INSTANCE_ID;
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
    writeCacheFile(key, { token: "DISK-TOK", exp: Math.floor(Date.now() / 1000) + 800, lease_id: "Ld" });
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

  it("reads a LEGACY cache without lease_id (backwards compat, 0 checkout)", async () => {
    isolateHome();
    process.env.CLEARCOTE_LICENSE_API = "http://test.local";
    const key = "cc_lic_legacy_" + Date.now();
    process.env.CLEARCOTE_LICENSE_KEY = key;
    writeCacheFile(key, { token: "LEGACY", exp: Math.floor(Date.now() / 1000) + 800 }); // no lease_id
    const calls = mockBackend();
    const h = await acquireLease({ quiet: true });
    expect(calls.filter((c) => c === "checkout").length).toBe(0);
    expect(h?.token).toBe("LEGACY");
  });
});
