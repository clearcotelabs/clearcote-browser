// Where the lease calls go (SDK 0.55). By default: the licence lease server, and clearcotelabs.com when it
// cannot be reached (network error, gateway error, challenge page), once per call, with the primary skipped
// for a while after it failed. A base that is set (`licenseApiBase`, CLEARCOTE_LICENSE_API, CLEARCOTE_LEASE_API)
// is the only one used. Real local HTTP servers stand in for both bases; fetch is wrapped to record every URL
// and to refuse any host but this machine, so no test can reach a real licence server.
import { describe, it, expect, afterEach } from "vitest";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { createServer, type IncomingMessage, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { acquireLease, getSessionSeats, leaseDefaults, LicenseError, LicenseRevokedError, ConcurrencyLimitError, type LeaseSession } from "../src/license.js";
import { tempDir } from "./helpers/temp.js";

const realFetch = globalThis.fetch;
const savedDefaults = { ...leaseDefaults, rateLimitRetryMs: [...leaseDefaults.rateLimitRetryMs] };
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API",
  "CLEARCOTE_INSTANCE_ID", "CLEARCOTE_LICENSE_THROUGH_PROXY"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const servers: Server[] = [];
const open: LeaseSession[] = [];

afterEach(async () => {
  for (const l of open.splice(0)) await l.stop();
  for (const s of servers.splice(0)) {
    s.closeAllConnections();
    await new Promise((r) => s.close(r));
  }
  globalThis.fetch = realFetch;
  Object.assign(leaseDefaults, savedDefaults, { rateLimitRetryMs: [...savedDefaults.rateLimitRetryMs] });
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

const tokenFor = (plan: string, n: number) => Buffer.from(JSON.stringify({ v: 1, plan, n, iat: 1_000 + n })).toString("base64url") + ".sig";
const now = () => Math.floor(Date.now() / 1000);

let seq = 0;
function isolate(): string {
  const home = tempDir("cc-leasebase-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  for (const k of ["CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API", "CLEARCOTE_INSTANCE_ID", "CLEARCOTE_LICENSE_THROUGH_PROXY"]) delete process.env[k];
  leaseDefaults.rateLimitRetryMs = [1, 1, 1];
  const key = `cc_lic_base_${Date.now()}_${++seq}`;
  process.env.CLEARCOTE_LICENSE_KEY = key;
  return key;
}

/** Every URL fetched, in order; anything but this machine is refused (recorded, then a network error). */
function guardFetch(): { urls: string[]; bodies: Record<string, unknown>[] } {
  const urls: string[] = [];
  const bodies: Record<string, unknown>[] = [];
  globalThis.fetch = (async (url: unknown, init?: RequestInit) => {
    const u = String(url);
    urls.push(u);
    bodies.push(typeof init?.body === "string" ? JSON.parse(init.body) : {});
    if (new URL(u).hostname !== "127.0.0.1") throw new TypeError(`refused in tests: ${u}`);
    return realFetch(url as string, init);
  }) as typeof fetch;
  return { urls, bodies };
}

type Handler = (req: IncomingMessage, body: Record<string, unknown>) => [number, string, Record<string, string>?] | "reset";
interface Fake {
  base: string;
  hits: { path: string; body: Record<string, unknown> }[];
  handler: Handler;
}

/** A licence backend on a local port: a paid per-browser lease per checkout unless `handler` says otherwise. */
async function fakeServer(handler?: Handler): Promise<Fake> {
  let n = 0;
  const fake: Fake = { base: "", hits: [], handler: handler ?? healthy };
  function healthy(req: IncomingMessage, body: Record<string, unknown>): [number, string] {
    const path = req.url ?? "";
    if (path.endsWith("/checkout")) {
      n++;
      return [200, JSON.stringify({ lease_id: `${fake.base}#L${n}`, token: tokenFor("pro", n), exp: now() + 86_400, lease_ttl_sec: 810,
        heartbeat_interval_sec: 270, concurrency: { limit: 0 },
        ...(typeof body.launch_id === "string" ? { lease_scope: "browser" } : {}) })];
    }
    if (path.endsWith("/heartbeat")) return [200, JSON.stringify({ token: tokenFor("pro", 100 + ++n), exp: now() + 86_400 })];
    if (path.endsWith("/seats")) return [200, JSON.stringify({ used: 3, limit: null, plan: "pro" })];
    return [200, "{}"];
  }
  const srv = createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      const body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      fake.hits.push({ path: req.url ?? "", body });
      const out = fake.handler(req, body);
      if (out === "reset") {
        req.socket.destroy();
        return;
      }
      const [status, text, headers] = out;
      res.writeHead(status, { "content-type": "application/json", connection: "close", ...(headers ?? {}) }).end(text);
    });
  });
  servers.push(srv);
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
  fake.base = `http://127.0.0.1:${(srv.address() as AddressInfo).port}`;
  return fake;
}

/** A base on a local port nothing listens on: connection refused. */
async function closedBase(): Promise<string> {
  const srv = createServer();
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
  const port = (srv.address() as AddressInfo).port;
  await new Promise((r) => srv.close(r));
  return `http://127.0.0.1:${port}`;
}

async function launch(opts: Parameters<typeof acquireLease>[0] = {}): Promise<LeaseSession> {
  const l = (await acquireLease({ quiet: true, ...opts }))!;
  open.push(l);
  return l;
}

const CHECKPOINT = "<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head><body></body></html>";

describe("default lease bases: the lease server, then clearcotelabs.com", () => {
  it("defaults to lease.clearcotelabs.com with www.clearcotelabs.com as the fallback; the breaker is 60 s", () => {
    expect(savedDefaults.primary).toBe("https://lease.clearcotelabs.com");
    expect(savedDefaults.fallback).toBe("https://www.clearcotelabs.com");
    expect(savedDefaults.breakerMs).toBe(60_000);
  });

  it("an unreachable primary: the same call goes to the fallback, and the primary is skipped until the breaker closes", async () => {
    isolate();
    const primary = await closedBase();
    const fb = await fakeServer();
    Object.assign(leaseDefaults, { primary, fallback: fb.base, breakerMs: 1_500 });
    const { urls, bodies } = guardFetch();

    const a = await launch();
    expect(urls).toEqual([`${primary}/api/v1/lease/checkout`, `${fb.base}/api/v1/lease/checkout`]);
    expect(bodies[1]).toEqual(bodies[0]); // the same call, launch_id and all
    expect(fb.hits[0].body.launch_id).toBe(bodies[0].launch_id);
    expect(a.leaseId.startsWith(fb.base)).toBe(true);

    const b = await launch(); // within the breaker window: straight to the fallback
    expect(urls.slice(2)).toEqual([`${fb.base}/api/v1/lease/checkout`]);
    await a.stop();
    await b.stop();
    expect(urls.slice(3)).toEqual([`${fb.base}/api/v1/lease/checkin`, `${fb.base}/api/v1/lease/checkin`]);

    await new Promise((r) => setTimeout(r, 1_600)); // the breaker closes: the primary is tried again
    await launch();
    expect(urls.slice(5)).toEqual([`${primary}/api/v1/lease/checkout`, `${fb.base}/api/v1/lease/checkout`]);
  });

  it.each([
    ["502", 502, "{}", {}],
    ["503", 503, '{"error":"unavailable"}', {}],
    ["504", 504, "<html>timeout</html>", { "content-type": "text/html" }],
    ["520", 520, "", {}],
    ["527", 527, "", {}],
    ["530", 530, "", {}],
    ["a challenge page (403)", 403, CHECKPOINT, { "content-type": "text/html" }],
    ["a challenge header", 429, "<html></html>", { "content-type": "text/html", "x-vercel-mitigated": "challenge" }],
  ] as const)("the primary answering %s: the call goes to the fallback", async (_name, status, text, headers) => {
    isolate();
    const primary = await fakeServer(() => [status, text, headers as Record<string, string>]);
    const fb = await fakeServer();
    Object.assign(leaseDefaults, { primary: primary.base, fallback: fb.base });
    guardFetch();
    const l = await launch();
    expect(primary.hits).toHaveLength(1);
    expect(fb.hits.map((h) => h.path)).toEqual(["/api/v1/lease/checkout"]);
    expect(l.leaseId.startsWith(fb.base)).toBe(true);
  });

  it.each([
    ["401", 401, { error: "Invalid licence key.", code: "INVALID_LICENSE" }, LicenseError, 1],
    ["403 revoked", 403, { error: "License revoked.", code: "LICENSE_REVOKED" }, LicenseRevokedError, 1],
    ["409", 409, { error: "conflict", code: "LEASE_CONFLICT" }, LicenseError, 1],
    ["426", 426, { error: "Update the SDK.", code: "SDK_UPGRADE_REQUIRED" }, LicenseError, 1],
    ["429 concurrency", 429, { error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }, ConcurrencyLimitError, 1],
    ["429 RATE_LIMITED (retried on the primary)", 429, { error: "Rate limit exceeded.", code: "RATE_LIMITED" }, LicenseError, 4],
    ["500 JSON", 500, { error: "boom" }, LicenseError, 1],
  ] as const)("a JSON answer from the primary (%s) is final: never sent to the fallback", async (_name, status, body, cls, hits) => {
    isolate();
    const primary = await fakeServer(() => [status, JSON.stringify(body)]);
    const fb = await fakeServer();
    Object.assign(leaseDefaults, { primary: primary.base, fallback: fb.base });
    guardFetch();
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(cls);
    expect(primary.hits).toHaveLength(hits);
    expect(fb.hits).toHaveLength(0);
  });

  it("heartbeats fall back too", async () => {
    isolate();
    let primaryUp = true;
    const fb = await fakeServer();
    const primary = await fakeServer((req, body) => (primaryUp ? fb.handler(req, body) : [503, "{}"]));
    Object.assign(leaseDefaults, { primary: primary.base, fallback: fb.base });
    guardFetch();
    const l = await launch();
    primaryUp = false;
    expect(await l.refreshToken!()).toBe(true); // a heartbeat of this browser's lease
    expect(primary.hits.map((h) => h.path)).toEqual(["/api/v1/lease/checkout", "/api/v1/lease/heartbeat"]);
    expect(fb.hits.map((h) => h.path)).toEqual(["/api/v1/lease/heartbeat"]);
  });

  it("seats use the lease bases and the fallback", async () => {
    isolate();
    const fb = await fakeServer();
    Object.assign(leaseDefaults, { primary: await closedBase(), fallback: fb.base });
    guardFetch();
    expect(await getSessionSeats({})).toEqual({ state: "ok", used: 3, limit: null, plan: "pro" });
    expect(fb.hits.map((h) => h.path)).toEqual(["/api/v1/lease/seats"]);
  });

  it("through the launch proxy, the fallback works the same", async () => {
    isolate();
    // The local server plays an HTTP proxy: an http:// lease base goes to it in absolute form.
    let n = 0;
    const proxy = await fakeServer((req) => {
      if (req.url!.startsWith("http://lease.test/")) return [503, "{}"];
      n++;
      return [200, JSON.stringify({ lease_id: `fb#L${n}`, token: tokenFor("pro", n), exp: now() + 86_400, heartbeat_interval_sec: 270, lease_scope: "browser" })];
    });
    Object.assign(leaseDefaults, { primary: "http://lease.test", fallback: "http://www.test" });
    guardFetch();
    const l = await launch({ licenseThroughProxy: true, proxy: proxy.base });
    expect(l.leaseId).toBe("fb#L1");
    expect(proxy.hits.map((h) => h.path)).toEqual(["http://lease.test/api/v1/lease/checkout", "http://www.test/api/v1/lease/checkout"]);
    const l2 = await launch({ licenseThroughProxy: true, proxy: proxy.base });
    expect(l2.leaseId).toBe("fb#L2");
    expect(proxy.hits.slice(2).map((h) => h.path)).toEqual(["http://www.test/api/v1/lease/checkout"]); // the primary is skipped
  });

  it("both bases unreachable + a valid paid grace token: the browser starts uncounted; the next checkout that gets through reports it", async () => {
    const key = isolate();
    const id = createHash("sha256").update(key).digest("hex").slice(0, 16);
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    writeFileSync(join(process.env.HOME!, ".clearcote", `grace-${id}.json`), JSON.stringify({ token: tokenFor("pro", 50), exp: now() + 3_600 }));
    let fbUp = false;
    const fb = await fakeServer();
    const healthy = fb.handler;
    fb.handler = (req, body) => (fbUp ? healthy(req, body) : "reset");
    Object.assign(leaseDefaults, { primary: await closedBase(), fallback: fb.base });
    guardFetch();
    const l = await launch();
    expect(l.token).toBe(tokenFor("pro", 50));
    fbUp = true;
    await launch();
    const checkouts = fb.hits.filter((h) => h.path.endsWith("/checkout"));
    expect(checkouts.at(-1)!.body.uncounted_launches).toBe(1);
    await launch();
    expect(fb.hits.filter((h) => h.path.endsWith("/checkout")).at(-1)!.body).not.toHaveProperty("uncounted_launches");
  });
});

describe("an older SDK's lease cache with both bases unreachable", () => {
  it("is the token, as before (it no longer skips the checkout, but it still rides out an outage)", async () => {
    const key = isolate();
    const id = createHash("sha256").update(key).digest("hex").slice(0, 16);
    mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
    writeFileSync(join(process.env.HOME!, ".clearcote", `lease-${id}.json`),
      JSON.stringify({ token: tokenFor("pro", 60), exp: now() + 3_600, lease_id: "L-old" })); // no scope mark
    const fb = await fakeServer(() => "reset");
    const primary = await closedBase();
    Object.assign(leaseDefaults, { primary, fallback: fb.base });
    const { urls } = guardFetch();
    expect((await launch()).token).toBe(tokenFor("pro", 60));
    expect(urls).toEqual([`${primary}/api/v1/lease/checkout`, `${fb.base}/api/v1/lease/checkout`]); // both tried first
  });
});

describe("a base that is set is the only one used", () => {
  it.each(["licenseApiBase", "CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API"] as const)("%s: no fallback, never the lease server", async (how) => {
    isolate();
    const set = await closedBase();
    const fb = await fakeServer();
    leaseDefaults.fallback = fb.base; // would answer, if it were asked; the primary stays the real default
    const { urls } = guardFetch();
    if (how !== "licenseApiBase") process.env[how] = set;
    const err = await acquireLease({ quiet: true, ...(how === "licenseApiBase" ? { licenseApiBase: set } : {}) }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(LicenseError);
    expect(urls).toEqual([`${set}/api/v1/lease/checkout`]);
    expect(fb.hits).toHaveLength(0);
    expect(urls.join(" ")).not.toContain("lease.clearcotelabs.com");
  });

  it("precedence: licenseApiBase > CLEARCOTE_LICENSE_API > CLEARCOTE_LEASE_API", async () => {
    const [opt, licApi, leaseApi] = await Promise.all([fakeServer(), fakeServer(), fakeServer()]);
    guardFetch();
    const hitsOf = () => [opt, licApi, leaseApi].map((s) => s.hits.length);
    isolate();
    process.env.CLEARCOTE_LICENSE_API = licApi.base;
    process.env.CLEARCOTE_LEASE_API = leaseApi.base;
    await launch({ licenseApiBase: opt.base });
    expect(hitsOf()).toEqual([1, 0, 0]);
    isolate();
    process.env.CLEARCOTE_LICENSE_API = licApi.base;
    process.env.CLEARCOTE_LEASE_API = leaseApi.base;
    await launch();
    expect(hitsOf()).toEqual([1, 1, 0]);
    isolate();
    process.env.CLEARCOTE_LEASE_API = leaseApi.base;
    await launch();
    expect(hitsOf()).toEqual([1, 1, 1]);
  });

  it("seats with a set base: that base only", async () => {
    isolate();
    const set = await closedBase();
    const fb = await fakeServer();
    leaseDefaults.fallback = fb.base;
    guardFetch();
    process.env.CLEARCOTE_LEASE_API = set;
    expect((await getSessionSeats({})).state).toBe("unavailable");
    expect(fb.hits).toHaveLength(0);
  });
});
