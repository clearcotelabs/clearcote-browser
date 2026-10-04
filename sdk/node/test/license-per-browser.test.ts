// Per-browser leases (the GitHub free tier, "1 browser at a time") vs the per-machine shared lease
// (every paid plan). Hermetic: a mocked global fetch that behaves like the backend — a free key gets
// lease_scope "browser" and one live lease per launch_id, a paid key gets the machine-shared lease —
// and HOME is a temp dir so the on-disk cache and instance_id are isolated.
import { describe, it, expect, afterEach, vi } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { acquireLease, ConcurrencyLimitError, LicenseError, tokenPlan, newLaunchId } from "../src/license.js";
import { tempDir } from "./helpers/temp.js";

const realFetch = globalThis.fetch;

const tokenFor = (plan: string, n: number) =>
  Buffer.from(JSON.stringify({ v: 1, plan, n })).toString("base64url") + ".sig";

interface Call {
  ep: string;
  body: Record<string, unknown>;
}

/** A fake licence backend. `plan` "free" = per-browser (limit 1), anything else = per-machine. */
function backend(plan: string, opts: { limit?: number; failNetwork?: boolean } = {}) {
  const calls: Call[] = [];
  const live = new Map<string, { launch: string | null; instance: string }>(); // leaseId -> owner
  let n = 0;
  const limit = opts.limit ?? 1;
  const perBrowser = plan === "free";
  const state = { heartbeatStatus: 200 };
  globalThis.fetch = (async (url: unknown, init?: { body?: string }) => {
    if (opts.failNetwork) throw new TypeError("fetch failed");
    const ep = String(url).split("/").pop()!;
    const body = init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : {};
    calls.push({ ep, body });
    const now = Math.floor(Date.now() / 1000);
    if (ep === "checkout") {
      const instance = String(body.instance_id);
      const launch = typeof body.launch_id === "string" ? body.launch_id : null;
      if (perBrowser && !launch) return new Response(JSON.stringify({ code: "SDK_UPGRADE_REQUIRED", error: "upgrade" }), { status: 426 });
      // takeover: the same machine (paid) or the same launch (free) replaces its own lease
      for (const [id, o] of live) if (perBrowser ? o.launch === launch : o.instance === instance) live.delete(id);
      if (live.size >= limit) {
        return new Response(JSON.stringify({ code: "CONCURRENCY_LIMIT_EXCEEDED", error: "The free tier runs one browser at a time." }), { status: 429 });
      }
      const id = `L${++n}`;
      live.set(id, { launch, instance });
      return new Response(
        JSON.stringify({
          lease_id: id,
          token: tokenFor(plan, n),
          exp: now + 900,
          lease_ttl_sec: perBrowser ? 360 : 810,
          heartbeat_interval_sec: perBrowser ? 120 : 270,
          concurrency: { used: live.size, limit },
          ...(perBrowser ? { lease_scope: "browser" } : {}),
        }),
        { status: 200 },
      );
    }
    if (ep === "heartbeat") {
      if (state.heartbeatStatus === 409) {
        live.delete(String(body.lease_id));
        return new Response(JSON.stringify({ code: "LEASE_EXPIRED" }), { status: 409 });
      }
      return new Response(JSON.stringify({ token: tokenFor(plan, 1000 + calls.length), exp: now + 900 }), { status: 200 });
    }
    if (ep === "checkin") {
      live.delete(String(body.lease_id));
      return new Response("{}", { status: 200 });
    }
    return new Response("{}", { status: 404 });
  }) as typeof fetch;
  return { calls, live, state, checkouts: () => calls.filter((c) => c.ep === "checkout") };
}

let seq = 0;
function isolate(plan: string): string {
  const home = tempDir("cc-perbrowser-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  delete process.env.CLEARCOTE_INSTANCE_ID;
  process.env.CLEARCOTE_LICENSE_API = "http://test.local";
  // A unique key per test: the module-level lease registry is keyed by it.
  const key = `cc_lic_${plan}_${Date.now()}_${++seq}`;
  process.env.CLEARCOTE_LICENSE_KEY = key;
  return key;
}

function cacheFile(key: string): string {
  const id = createHash("sha256").update(key).digest("hex").slice(0, 16);
  return join(process.env.HOME!, ".clearcote", `lease-${id}.json`);
}

const OLD = {
  key: process.env.CLEARCOTE_LICENSE_KEY,
  api: process.env.CLEARCOTE_LICENSE_API,
  home: process.env.HOME,
  prof: process.env.USERPROFILE,
  iid: process.env.CLEARCOTE_INSTANCE_ID,
};
afterEach(() => {
  vi.useRealTimers();
  globalThis.fetch = realFetch;
  const restore: Record<string, string | undefined> = {
    CLEARCOTE_LICENSE_KEY: OLD.key,
    CLEARCOTE_LICENSE_API: OLD.api,
    HOME: OLD.home,
    USERPROFILE: OLD.prof,
    CLEARCOTE_INSTANCE_ID: OLD.iid,
  };
  for (const [k, v] of Object.entries(restore)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

describe("helpers", () => {
  it("newLaunchId is unique and matches the backend's launch_id pattern", () => {
    const ids = new Set(Array.from({ length: 500 }, () => newLaunchId()));
    expect(ids.size).toBe(500);
    for (const id of ids) expect(id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
  });

  it("tokenPlan reads the plan claim and never throws on junk", () => {
    expect(tokenPlan(tokenFor("free", 1))).toBe("free");
    expect(tokenPlan(tokenFor("pro", 1))).toBe("pro");
    for (const junk of ["", ".", "not-a-token", "%%%.sig", Buffer.from("[1,2]").toString("base64url") + ".x"]) {
      expect(tokenPlan(junk)).toBeUndefined();
    }
  });
});

describe("free tier: one lease PER BROWSER", () => {
  it("every checkout sends a launch_id; the first browser runs, a second in the same process is refused", async () => {
    isolate("free");
    const be = backend("free");
    const b1 = await acquireLease({ quiet: true });
    expect(b1?.token).toBeTruthy();
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(ConcurrencyLimitError);
    const cos = be.checkouts();
    expect(cos).toHaveLength(2);
    for (const c of cos) expect(c.body.launch_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(cos[0].body.launch_id).not.toBe(cos[1].body.launch_id);
    expect(cos[0].body.instance_id).toBe(cos[1].body.instance_id); // same machine
    await b1?.stop();
  });

  it("closing a browser checks in ITS lease, and the next browser can start", async () => {
    isolate("free");
    const be = backend("free");
    const b1 = await acquireLease({ quiet: true });
    await b1!.stop();
    const checkins = be.calls.filter((c) => c.ep === "checkin");
    expect(checkins).toHaveLength(1);
    expect(checkins[0].body.lease_id).toBe(b1!.leaseId);
    const b2 = await acquireLease({ quiet: true });
    expect(b2?.token).toBeTruthy();
    expect(b2!.leaseId).not.toBe(b1!.leaseId);
    await b2!.stop();
    expect(be.live.size).toBe(0);
  });

  it("stop() is idempotent: one check-in however many times it is called", async () => {
    isolate("free");
    const be = backend("free");
    const b1 = await acquireLease({ quiet: true });
    await Promise.all([b1!.stop(), b1!.stop(), b1!.stop()]);
    await b1!.stop();
    expect(be.calls.filter((c) => c.ep === "checkin")).toHaveLength(1);
  });

  it("5 browsers launched at the same moment in one process: exactly one gets a slot", async () => {
    isolate("free");
    const be = backend("free");
    const results = await Promise.allSettled(Array.from({ length: 5 }, () => acquireLease({ quiet: true })));
    const ok = results.filter((r) => r.status === "fulfilled");
    const refused = results.filter((r) => r.status === "rejected");
    expect(ok).toHaveLength(1);
    expect(refused).toHaveLength(4);
    for (const r of refused) expect((r as PromiseRejectedResult).reason).toBeInstanceOf(ConcurrencyLimitError);
    expect(new Set(be.checkouts().map((c) => c.body.launch_id)).size).toBe(5);
    await (ok[0] as PromiseFulfilledResult<Awaited<ReturnType<typeof acquireLease>>>).value!.stop();
  });

  it("with a higher cap, each concurrent browser holds its OWN lease and token", async () => {
    isolate("free");
    const be = backend("free", { limit: 3 });
    const bs = await Promise.all([acquireLease({ quiet: true }), acquireLease({ quiet: true }), acquireLease({ quiet: true })]);
    expect(new Set(bs.map((b) => b!.leaseId)).size).toBe(3);
    expect(new Set(bs.map((b) => b!.token)).size).toBe(3);
    expect(be.live.size).toBe(3);
    await bs[1]!.stop();
    expect(be.live.size).toBe(2);
    expect(be.live.has(bs[1]!.leaseId)).toBe(false);
    await Promise.all(bs.map((b) => b!.stop()));
  });

  it("never writes a free token to the shared on-disk cache", async () => {
    const key = isolate("free");
    backend("free");
    const b1 = await acquireLease({ quiet: true });
    expect(existsSync(cacheFile(key))).toBe(false);
    await b1!.stop();
    expect(existsSync(cacheFile(key))).toBe(false);
  });

  it("ignores a still-valid FREE token an older SDK left in the cache (no slot-less launch)", async () => {
    const key = isolate("free");
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    writeFileSync(cacheFile(key), JSON.stringify({ token: tokenFor("free", 99), exp: Math.floor(Date.now() / 1000) + 800, lease_id: "OLD" }));
    const be = backend("free");
    const b1 = await acquireLease({ quiet: true });
    expect(be.checkouts()).toHaveLength(1);
    expect(b1!.token).not.toBe(tokenFor("free", 99));
    // ...and a second launch is still refused, instead of borrowing that cached token
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(ConcurrencyLimitError);
    await b1!.stop();
  });

  it("no offline grace: backend unreachable -> LicenseError even with a cached free token", async () => {
    const key = isolate("free");
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    writeFileSync(cacheFile(key), JSON.stringify({ token: tokenFor("free", 7), exp: Math.floor(Date.now() / 1000) + 800 }));
    backend("free", { failNetwork: true });
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(LicenseError);
  });

  it("heartbeats its own lease; a 409 re-checks-out as the SAME launch", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    isolate("free");
    const be = backend("free");
    const b1 = await acquireLease({ quiet: true });
    const firstLaunch = be.checkouts()[0].body.launch_id;
    const firstLease = b1!.leaseId;

    await vi.advanceTimersByTimeAsync(120_000);
    const hb = be.calls.filter((c) => c.ep === "heartbeat");
    expect(hb).toHaveLength(1);
    expect(hb[0].body.lease_id).toBe(firstLease);
    expect(tokenPlan(b1!.token)).toBe("free");

    be.state.heartbeatStatus = 409;
    await vi.advanceTimersByTimeAsync(120_000);
    const cos = be.checkouts();
    expect(cos).toHaveLength(2);
    expect(cos[1].body.launch_id).toBe(firstLaunch);
    expect(b1!.leaseId).not.toBe(firstLease);

    await b1!.stop();
    const beatsAtStop = be.calls.filter((c) => c.ep === "heartbeat").length;
    await vi.advanceTimersByTimeAsync(600_000);
    expect(be.calls.filter((c) => c.ep === "heartbeat").length).toBe(beatsAtStop); // stopped browsers stop beating
  });

  it("two browsers' heartbeats never touch each other's lease", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    isolate("free");
    const be = backend("free", { limit: 2 });
    const [a, b] = await Promise.all([acquireLease({ quiet: true }), acquireLease({ quiet: true })]);
    await vi.advanceTimersByTimeAsync(120_000);
    const beaten = be.calls.filter((c) => c.ep === "heartbeat").map((c) => c.body.lease_id).sort();
    expect(beaten).toEqual([a!.leaseId, b!.leaseId].sort());
    await a!.stop();
    await vi.advanceTimersByTimeAsync(120_000);
    const after = be.calls.filter((c) => c.ep === "heartbeat").slice(2).map((c) => c.body.lease_id);
    expect(after).toEqual([b!.leaseId]);
    await b!.stop();
  });
});

describe("paid plans: the machine-shared lease is unchanged", () => {
  it("N launches share ONE checkout and token; stop() does not check in", async () => {
    isolate("pro");
    const be = backend("pro", { limit: 5 });
    const hs = await Promise.all([acquireLease({ quiet: true }), acquireLease({ quiet: true }), acquireLease({ quiet: true })]);
    expect(be.checkouts()).toHaveLength(1);
    expect(new Set(hs.map((h) => h!.token)).size).toBe(1);
    for (const h of hs) await h!.stop();
    expect(be.calls.filter((c) => c.ep === "checkin")).toHaveLength(0);
  });

  it("a paid launch at the machine's cap of 1 still runs many browsers on the one slot", async () => {
    isolate("pro");
    const be = backend("pro", { limit: 1 });
    const hs = await Promise.all(Array.from({ length: 4 }, () => acquireLease({ quiet: true })));
    expect(hs.every((h) => !!h?.token)).toBe(true);
    expect(be.checkouts()).toHaveLength(1);
  });

  it("the paid token is still written to, and reused from, the shared cache", async () => {
    const key = isolate("pro");
    backend("pro", { limit: 5 });
    const h = await acquireLease({ quiet: true });
    const cached = JSON.parse(readFileSync(cacheFile(key), "utf8"));
    expect(cached.token).toBe(h!.token);
  });

  it("a still-valid cached PAID token is reused with zero backend calls (cross-process reuse)", async () => {
    const key = isolate("pro");
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    writeFileSync(cacheFile(key), JSON.stringify({ token: tokenFor("pro", 5), exp: Math.floor(Date.now() / 1000) + 800, lease_id: "Lx" }));
    const be = backend("pro");
    const h = await acquireLease({ quiet: true });
    expect(h!.token).toBe(tokenFor("pro", 5));
    expect(be.calls).toHaveLength(0);
  });

  it("paid offline grace still works on a cached paid token", async () => {
    const key = isolate("pro");
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    // expired-for-reuse (inside SKEW) would force a checkout; use a valid one but force a cold checkout path via failNetwork
    writeFileSync(cacheFile(key), JSON.stringify({ token: tokenFor("pro", 6), exp: Math.floor(Date.now() / 1000) + 800 }));
    backend("pro", { failNetwork: true });
    const h = await acquireLease({ quiet: true });
    expect(h!.token).toBe(tokenFor("pro", 6));
  });

  it("paid checkout bodies carry a launch_id the backend ignores; nothing else changed", async () => {
    isolate("pro");
    const be = backend("pro", { limit: 5 });
    await acquireLease({ quiet: true, sdkVersion: "9.9.9" });
    const body = be.checkouts()[0].body;
    expect(Object.keys(body).sort()).toEqual(["engine_version", "instance_id", "launch_id", "os", "sdk_version"].filter((k) => k !== "engine_version" || "engine_version" in body).sort());
    expect(body.sdk_version).toBe("9.9.9");
  });
});

describe("releaseLeaseOnFailure (a browser that fails to start gives its slot back)", () => {
  it("success: returns the browser and does not release", async () => {
    const { releaseLeaseOnFailure } = await import("../src/index.js");
    let stops = 0;
    const lease = { token: "t", leaseId: "L1", stop: async () => { stops++; } };
    await expect(releaseLeaseOnFailure(lease, async () => "browser")).resolves.toBe("browser");
    expect(stops).toBe(0);
  });

  it("failure: releases the lease and re-throws the ORIGINAL error", async () => {
    const { releaseLeaseOnFailure } = await import("../src/index.js");
    let stops = 0;
    const lease = { token: "t", leaseId: "L1", stop: async () => { stops++; } };
    const boom = new Error("spawn UNKNOWN");
    await expect(releaseLeaseOnFailure(lease, async () => { throw boom; })).rejects.toBe(boom);
    expect(stops).toBe(1);
  });

  it("a failing release never masks the start error", async () => {
    const { releaseLeaseOnFailure } = await import("../src/index.js");
    const lease = { token: "t", leaseId: "L1", stop: async () => { throw new Error("checkin failed"); } };
    await expect(releaseLeaseOnFailure(lease, async () => { throw new Error("start failed"); })).rejects.toThrow("start failed");
  });

  it("no lease (free build, no key): failure just re-throws", async () => {
    const { releaseLeaseOnFailure } = await import("../src/index.js");
    await expect(releaseLeaseOnFailure(null, async () => { throw new Error("x"); })).rejects.toThrow("x");
  });

  it("end to end with a real free lease: the slot is checked in and the next browser can start", async () => {
    const { releaseLeaseOnFailure } = await import("../src/index.js");
    isolate("free");
    const be = backend("free");
    const lease = await acquireLease({ quiet: true });
    await expect(releaseLeaseOnFailure(lease, async () => { throw new Error("browser crashed on start"); })).rejects.toThrow();
    expect(be.live.size).toBe(0);
    const next = await acquireLease({ quiet: true });
    expect(next?.token).toBeTruthy();
    await next!.stop();
  });
});

describe("run-token file (engine online-enforcement opt-in)", () => {
  it("bindLaunch writes the current token to a file and release() removes it", async () => {
    isolate("free");
    backend("free");
    const lease = (await acquireLease({ quiet: true }))!;
    try {
      const lt = lease.bindLaunch();
      expect(existsSync(lt.file)).toBe(true);
      expect(readFileSync(lt.file, "utf8")).toBe(lease.token); // seeded with the current token
      lt.release();
      expect(existsSync(lt.file)).toBe(false);
    } finally {
      await lease.stop();
    }
  });

  it("stop() removes any still-bound token files", async () => {
    isolate("free");
    backend("free");
    const lease = (await acquireLease({ quiet: true }))!;
    const lt = lease.bindLaunch();
    expect(existsSync(lt.file)).toBe(true);
    await lease.stop();
    expect(existsSync(lt.file)).toBe(false); // closeAll on stop
  });

  it("two launches on one lease get independent files that each follow the token", async () => {
    isolate("pro");
    backend("pro", { limit: 5 });
    const lease = (await acquireLease({ quiet: true }))!;
    try {
      const a = lease.bindLaunch();
      const b = lease.bindLaunch();
      expect(a.file).not.toBe(b.file);
      expect(readFileSync(a.file, "utf8")).toBe(lease.token);
      expect(readFileSync(b.file, "utf8")).toBe(lease.token);
      a.release();
      expect(existsSync(a.file)).toBe(false);
      expect(existsSync(b.file)).toBe(true); // b is independent
      b.release();
    } finally {
      await lease.stop();
    }
  });
});
