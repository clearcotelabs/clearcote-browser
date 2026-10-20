// Paid licences counted per browser (SDK 0.55). Every checkout offers `lease_scope_supported: ["browser"]`;
// an uncapped paid licence then gets one lease per launch, like the free tier, and a capped one keeps the
// machine-shared lease. A paid browser whose checkout gets no verdict (unreachable, 5xx, challenge,
// rate-limited) starts on a cached token, uncounted, and the next checkout reports it; a free one never does.
// Hermetic: a mocked fetch that behaves like the backend, one explicit base (no fallback: that is
// license-lease-base.test.ts), and HOME / USERPROFILE / LOCALAPPDATA in a temp dir.
import { describe, it, expect, afterEach, vi } from "vitest";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { createHash } from "node:crypto";
import {
  acquireLease,
  leaseDefaults,
  tokenIat,
  ConcurrencyLimitError,
  LicenseError,
  LicenseRevokedError,
  LicenseServerChallengeError,
  type LeaseSession,
} from "../src/license.js";
import { tempDir } from "./helpers/temp.js";

const realFetch = globalThis.fetch;
const savedDefaults = { ...leaseDefaults, rateLimitRetryMs: [...leaseDefaults.rateLimitRetryMs] };
const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "CLEARCOTE_LEASE_API",
  "CLEARCOTE_INSTANCE_ID", "CLEARCOTE_LICENSE_THROUGH_PROXY"] as const;
const savedEnv = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
const open: LeaseSession[] = [];

afterEach(async () => {
  for (const l of open.splice(0)) await l.stop();
  vi.useRealTimers();
  vi.restoreAllMocks();
  globalThis.fetch = realFetch;
  Object.assign(leaseDefaults, savedDefaults, { rateLimitRetryMs: [...savedDefaults.rateLimitRetryMs] });
  for (const [k, v] of Object.entries(savedEnv)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
});

/** A run-token as the server mints it; only the payload matters here (plan, iat). */
const tokenFor = (plan: string, n: number, iat = 1_000 + n) =>
  Buffer.from(JSON.stringify({ v: 1, plan, n, iat })).toString("base64url") + ".sig";
const now = () => Math.floor(Date.now() / 1000);

let seq = 0;
/** A temp HOME (also LOCALAPPDATA) and a licence key of its own (the in-process lease registry is keyed by it). */
function isolate(plan: string): string {
  const home = tempDir("cc-paidb-");
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  process.env.CLEARCOTE_LICENSE_API = "http://licence.test";
  delete process.env.CLEARCOTE_LEASE_API;
  delete process.env.CLEARCOTE_INSTANCE_ID;
  delete process.env.CLEARCOTE_LICENSE_THROUGH_PROXY;
  leaseDefaults.rateLimitRetryMs = [1, 1, 1];
  const key = `cc_lic_${plan}_${Date.now()}_${++seq}`;
  process.env.CLEARCOTE_LICENSE_KEY = key;
  return key;
}

const keyId = (key: string) => createHash("sha256").update(key).digest("hex").slice(0, 16);
const leaseFile = (key: string) => join(process.env.HOME!, ".clearcote", `lease-${keyId(key)}.json`);
const graceFile = (key: string) => join(process.env.HOME!, ".clearcote", `grace-${keyId(key)}.json`);
function writeJson(file: string, obj: unknown): void {
  mkdirSync(join(process.env.HOME!, ".clearcote"), { recursive: true });
  writeFileSync(file, JSON.stringify(obj));
}

interface Call {
  ep: string;
  body: Record<string, unknown>;
}
type Answer = () => Response;
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/**
 * A fake licence backend. "free" = per browser, one at a time. A paid plan: per browser when the checkout
 * offers lease_scope_supported with a launch_id and the licence has no cap; a machine lease otherwise.
 */
function backend(plan: string, opts: { capped?: boolean } = {}) {
  const calls: Call[] = [];
  const live = new Map<string, string | null>();
  const state = {
    /** Every request fails as an unreachable host does. */
    down: false,
    /** The next N checkouts are answered 429 RATE_LIMITED. */
    rateLimited: 0,
    /** Answers for the next checkouts, in order (then the normal answer). */
    checkoutAnswers: [] as Answer[],
    /** Answers for the next heartbeats, in order. */
    heartbeatAnswers: [] as Answer[],
    notice: undefined as string | undefined,
  };
  let n = 0;
  globalThis.fetch = (async (url: unknown, init?: { body?: string }) => {
    const u = String(url);
    if (!u.startsWith("http://licence.test/")) throw new Error(`unexpected request to ${u}`);
    const ep = u.split("/").pop()!;
    const body = init?.body ? (JSON.parse(init.body) as Record<string, unknown>) : {};
    calls.push({ ep, body });
    if (state.down) throw new TypeError("fetch failed", { cause: Object.assign(new Error("connect ECONNREFUSED"), { code: "ECONNREFUSED" }) });
    if (ep === "checkout") {
      if (state.rateLimited > 0) {
        state.rateLimited--;
        return json(429, { error: "Rate limit exceeded.", code: "RATE_LIMITED" });
      }
      const scripted = state.checkoutAnswers.shift();
      if (scripted) return scripted();
      const free = plan === "free";
      const offered = Array.isArray(body.lease_scope_supported) && body.lease_scope_supported.includes("browser");
      const browser = free || (!opts.capped && offered && typeof body.launch_id === "string");
      if (free && live.size >= 1) return json(429, { error: "The free tier runs one browser at a time.", code: "CONCURRENCY_LIMIT_EXCEEDED" });
      const id = `L${++n}`;
      live.set(id, typeof body.launch_id === "string" ? body.launch_id : null);
      return json(200, {
        lease_id: id,
        token: tokenFor(plan, n),
        exp: now() + (free ? 180 : 86_400),
        lease_ttl_sec: free ? 360 : 810,
        heartbeat_interval_sec: free ? 60 : 270,
        // As the server answers a paid per-browser lease: no `used`.
        concurrency: browser && !free ? { limit: 0 } : { used: live.size, limit: free ? 1 : 5 },
        ...(browser ? { lease_scope: "browser" } : {}),
        ...(state.notice !== undefined ? { notice: state.notice } : {}),
      });
    }
    if (ep === "heartbeat") {
      const scripted = state.heartbeatAnswers.shift();
      if (scripted) return scripted();
      n++;
      return json(200, { token: tokenFor(plan, n), exp: now() + 86_400 });
    }
    if (ep === "checkin") {
      live.delete(String(body.lease_id));
      return json(200, {});
    }
    return json(404, {});
  }) as typeof fetch;
  return {
    calls,
    live,
    state,
    checkouts: () => calls.filter((c) => c.ep === "checkout"),
    count: (ep: string) => calls.filter((c) => c.ep === ep).length,
  };
}

/** stderr lines written while `fn` runs. */
async function stderrOf<T>(fn: () => Promise<T>): Promise<{ out: T; lines: string[] }> {
  const lines: string[] = [];
  const spy = vi.spyOn(process.stderr, "write").mockImplementation(((s: unknown) => {
    lines.push(String(s));
    return true;
  }) as typeof process.stderr.write);
  try {
    return { out: await fn(), lines };
  } finally {
    spy.mockRestore();
  }
}

async function launch(quiet = true): Promise<LeaseSession> {
  const l = (await acquireLease({ quiet }))!;
  open.push(l);
  return l;
}

const UNCOUNTED = /^\[clearcote\] \[license\] licence server unreachable \(.+\); this browser starts on a cached run-token and is not counted\.\n$/;

// ── 1-2: who gets which lease ───────────────────────────────────────────────────────────────────────
describe("paid per-browser leases (lease_scope browser)", () => {
  it("5 launches in one process: 5 checkouts offering the browser scope with distinct launch ids, 5 check-ins, no lease cache, a grace file", async () => {
    const key = isolate("pro");
    const be = backend("pro");
    const hs: LeaseSession[] = [];
    for (let i = 0; i < 5; i++) hs.push((await acquireLease({ quiet: true }))!);
    const cos = be.checkouts();
    expect(cos).toHaveLength(5);
    for (const c of cos) {
      expect(c.body.lease_scope_supported).toEqual(["browser"]);
      expect(c.body.launch_id).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    }
    expect(new Set(cos.map((c) => c.body.launch_id)).size).toBe(5);
    expect(new Set(hs.map((h) => h.leaseId)).size).toBe(5);
    expect(new Set(hs.map((h) => h.token)).size).toBe(5);
    for (const h of hs) await h.stop();
    const checkins = be.calls.filter((c) => c.ep === "checkin").map((c) => c.body.lease_id);
    expect(checkins.sort()).toEqual(hs.map((h) => h.leaseId).sort());
    expect(be.live.size).toBe(0);
    expect(existsSync(leaseFile(key))).toBe(false); // older SDKs read that file: never a per-browser token
    const grace = JSON.parse(readFileSync(graceFile(key), "utf8"));
    expect(grace.token).toBe(hs[4].token); // the newest
    expect(grace.exp).toBeGreaterThan(now() + 80_000);
  });

  it("5 concurrent launches each hold their own lease", async () => {
    isolate("pro");
    const be = backend("pro");
    const hs = await Promise.all(Array.from({ length: 5 }, () => launch()));
    expect(be.checkouts()).toHaveLength(5);
    expect(new Set(hs.map((h) => h.leaseId)).size).toBe(5);
    expect(be.live.size).toBe(5);
  });

  it("each paid browser heartbeats its own lease at the server's interval; a new token becomes the grace token", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const key = isolate("pro");
    const be = backend("pro");
    const a = await launch();
    const b = await launch();
    await vi.advanceTimersByTimeAsync(269_000);
    expect(be.count("heartbeat")).toBe(0);
    await vi.advanceTimersByTimeAsync(1_000);
    const beaten = be.calls.filter((c) => c.ep === "heartbeat").map((c) => c.body.lease_id).sort();
    expect(beaten).toEqual([a.leaseId, b.leaseId].sort());
    const grace = JSON.parse(readFileSync(graceFile(key), "utf8"));
    expect([a.token, b.token]).toContain(grace.token);
    expect(tokenIat(grace.token)).toBe(Math.max(tokenIat(a.token)!, tokenIat(b.token)!));
    expect(existsSync(leaseFile(key))).toBe(false);
  });

  it("a machine-scope answer (capped paid licence) is unchanged: one checkout, the lease cache, reuse with no call", async () => {
    const key = isolate("pro");
    const be = backend("pro", { capped: true });
    const h1 = await launch();
    const h2 = await launch();
    expect(be.checkouts()).toHaveLength(1);
    expect(be.checkouts()[0].body.lease_scope_supported).toEqual(["browser"]); // offered; the server said machine
    expect(h2.token).toBe(h1.token);
    expect(JSON.parse(readFileSync(leaseFile(key), "utf8")).token).toBe(h1.token);
    expect(existsSync(graceFile(key))).toBe(false);
    await h1.stop();
    await h2.stop();
    expect(be.count("checkin")).toBe(0); // the machine slot is held for the process
    expect(be.calls).toHaveLength(1);
  });

  it("checkout bodies: lease_scope_supported added, nothing else changed", async () => {
    isolate("pro");
    const be = backend("pro");
    open.push((await acquireLease({ quiet: true, sdkVersion: "9.9.9", engineVersion: "150.0.7871.114" }))!);
    expect(Object.keys(be.checkouts()[0].body).sort()).toEqual(
      ["engine_version", "instance_id", "launch_id", "lease_scope_supported", "os", "sdk_version"],
    );
  });

  it("answers without concurrency.used are accepted, machine and browser scope", async () => {
    for (const capped of [false, true]) {
      isolate("pro");
      const be = backend("pro", { capped });
      be.state.checkoutAnswers.push(() =>
        json(200, { lease_id: "L-x", token: tokenFor("pro", 7), exp: now() + 86_400, lease_ttl_sec: 810, heartbeat_interval_sec: 270,
          concurrency: { limit: 0 }, ...(capped ? {} : { lease_scope: "browser" }) }));
      const h = await launch();
      expect(h.token).toBe(tokenFor("pro", 7));
    }
  });
});

// ── 5-7: no verdict -> a paid browser starts uncounted; free never does ─────────────────────────────
describe("paid browser, no verdict: starts uncounted on a cached token", () => {
  it("unreachable + a valid paid grace token: starts on it, says so, no heartbeat; the next checkout reports it", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const key = isolate("pro");
    const be = backend("pro");
    const first = await launch(false); // quiet is the process's lease's: set by its first launch
    await first.stop();
    const grace = JSON.parse(readFileSync(graceFile(key), "utf8"));

    be.state.down = true;
    const { out: h, lines } = await stderrOf(() => launch(false));
    expect(h.token).toBe(grace.token);
    expect(lines.filter((l) => UNCOUNTED.test(l))).toHaveLength(1);
    expect(lines.join("")).toContain("ECONNREFUSED");
    const before = be.calls.length;
    await vi.advanceTimersByTimeAsync(600_000);
    expect(be.calls.length).toBe(before); // nothing beats an uncounted browser
    await h.stop();
    expect(be.calls.length).toBe(before); // nor checks it in

    be.state.down = false;
    await launch();
    expect(be.checkouts().at(-1)!.body.uncounted_launches).toBe(1);
    await launch();
    expect(be.checkouts().at(-1)!.body).not.toHaveProperty("uncounted_launches"); // reset after the 200
  });

  it("the count is kept until a checkout that reports it gets a 200", async () => {
    isolate("pro");
    const be = backend("pro");
    await launch();
    be.state.down = true;
    await launch();
    await launch();
    be.state.down = false;
    be.state.checkoutAnswers.push(() => json(500, { error: "boom" }));
    const mark = be.checkouts().length;
    await launch(); // the 500 is no verdict either: a third uncounted browser
    expect(be.checkouts().slice(mark).map((c) => c.body.uncounted_launches)).toEqual([2]);
    await launch();
    expect(be.checkouts().at(-1)!.body.uncounted_launches).toBe(3);
    await launch();
    expect(be.checkouts().at(-1)!.body).not.toHaveProperty("uncounted_launches");
  });

  it("a fresh process with only a paid grace token starts on it when its first checkout gets no verdict", async () => {
    const key = isolate("pro");
    writeJson(graceFile(key), { token: tokenFor("pro", 77), exp: now() + 3_600 });
    const be = backend("pro");
    be.state.down = true;
    const h = await launch();
    expect(h.token).toBe(tokenFor("pro", 77));
    be.state.down = false;
    const next = await launch(); // the scope is per browser now: its own checkout, reporting the first
    expect(be.checkouts().at(-1)!.body.uncounted_launches).toBe(1);
    expect(next.token).not.toBe(tokenFor("pro", 77));
  });

  it("the grace token goes first; a valid machine cache token is next when the grace token is about to expire", async () => {
    const key = isolate("pro");
    const be = backend("pro");
    await launch(); // known paid, per browser
    writeJson(leaseFile(key), { token: tokenFor("pro", 90), exp: now() + 3_600, lease_id: "Lm" });
    writeJson(graceFile(key), { token: tokenFor("pro", 91), exp: now() + 3_600 });
    be.state.down = true;
    expect((await launch()).token).toBe(tokenFor("pro", 91));
    writeJson(graceFile(key), { token: tokenFor("pro", 92), exp: now() + 30 }); // inside the 60 s margin
    expect((await launch()).token).toBe(tokenFor("pro", 90));
  });

  it.each([
    ["HTTP 503", () => json(503, { error: "unavailable" })],
    ["HTTP 500", () => json(500, { error: "boom" })],
    ["a challenge page", () => new Response("<html><title>Vercel Security Checkpoint</title></html>", { status: 403, headers: { "content-type": "text/html" } })],
  ] as const)("%s is no verdict either", async (_name, answer) => {
    const key = isolate("pro");
    const be = backend("pro");
    await launch();
    const grace = JSON.parse(readFileSync(graceFile(key), "utf8"));
    be.state.checkoutAnswers.push(answer);
    expect((await launch()).token).toBe(grace.token);
  });

  it("no usable token: the same LicenseError as before", async () => {
    isolate("pro");
    const be = backend("pro");
    be.state.down = true;
    await expect(acquireLease({ quiet: true })).rejects.toThrow(/Could not reach the license server and no valid cached token/);

    const key = isolate("pro");
    const be2 = backend("pro");
    await launch();
    writeJson(graceFile(key), { token: tokenFor("pro", 5), exp: now() + 30 }); // expiring
    be2.state.down = true;
    const err = await acquireLease({ quiet: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(LicenseError);
    expect((err as Error).message).toMatch(/Could not reach the license server to start this browser/);
    void be;
  });

  it("a challenge with no usable token is still a LicenseServerChallengeError", async () => {
    isolate("pro");
    const be = backend("pro");
    await launch();
    be.state.checkoutAnswers.push(() => new Response("<html>Access denied</html>", { status: 403, headers: { "content-type": "text/html" } }));
    // the grace file holds the first launch's token: remove it to leave nothing usable
    writeJson(graceFile(process.env.CLEARCOTE_LICENSE_KEY!), { token: "x", exp: 0 });
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(LicenseServerChallengeError);
  });

  it("a free licence never falls back, whatever grace file there is", async () => {
    // (a) this process runs free leases: a paid grace token left from before is not used
    let key = isolate("free");
    let be = backend("free");
    const b = await launch();
    expect(existsSync(graceFile(key))).toBe(false); // free tokens are never kept
    await b.stop();
    writeJson(graceFile(key), { token: tokenFor("pro", 3), exp: now() + 3_600 });
    be.state.down = true;
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(LicenseError);
    // (b) a grace file holding a free token is no fallback for a fresh process
    key = isolate("free");
    writeJson(graceFile(key), { token: tokenFor("free", 4), exp: now() + 3_600 });
    be = backend("free");
    be.state.down = true;
    await expect(acquireLease({ quiet: true })).rejects.toThrow(/no valid cached token/);
  });

  it.each([
    ["401 INVALID_LICENSE", 401, { error: "Invalid licence key.", code: "INVALID_LICENSE" }, LicenseError],
    ["403 revoked", 403, { error: "License revoked.", code: "LICENSE_REVOKED" }, LicenseRevokedError],
    ["426 SDK_UPGRADE_REQUIRED", 426, { error: "Update the Clearcote SDK to 0.55 or newer.", code: "SDK_UPGRADE_REQUIRED" }, LicenseError],
    ["429 CONCURRENCY_LIMIT_EXCEEDED", 429, { error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }, ConcurrencyLimitError],
  ] as const)("a definitive verdict (%s) never falls back to a cached token", async (_name, status, body, cls) => {
    const key = isolate("pro");
    const be = backend("pro");
    await launch(false);
    writeJson(leaseFile(key), { token: tokenFor("pro", 90), exp: now() + 3_600 });
    expect(existsSync(graceFile(key))).toBe(true);
    be.state.checkoutAnswers.push(() => json(status, body));
    const { out: err, lines } = await stderrOf(() => acquireLease({ quiet: false }).catch((e: unknown) => e));
    expect(err).toBeInstanceOf(cls);
    expect((err as Error).message).toBe(body.error);
    expect((err as LicenseError).code).toBe(body.code);
    expect(lines.filter((l) => UNCOUNTED.test(l))).toHaveLength(0);
  });

  it("426 on the first checkout of a process: the server's message, no cached token used", async () => {
    const key = isolate("pro");
    writeJson(graceFile(key), { token: tokenFor("pro", 8), exp: now() + 3_600 });
    const be = backend("pro");
    be.state.checkoutAnswers.push(() => json(426, { error: "Update the Clearcote SDK.", code: "SDK_UPGRADE_REQUIRED" }));
    await expect(acquireLease({ quiet: true })).rejects.toMatchObject({ code: "SDK_UPGRADE_REQUIRED", message: "Update the Clearcote SDK." });
  });
});

// ── 8: rate limiting ────────────────────────────────────────────────────────────────────────────────
describe("RATE_LIMITED (this address's request budget) is retried, never a concurrency verdict", () => {
  it("waits 1 s, 2 s, 4 s by default", () => {
    expect(savedDefaults.rateLimitRetryMs).toEqual([1_000, 2_000, 4_000]);
  });

  it("is retried, then the checkout succeeds", async () => {
    isolate("pro");
    const be = backend("pro");
    be.state.rateLimited = 2;
    const h = await launch();
    expect(h.token).toBeTruthy();
    expect(be.checkouts()).toHaveLength(3);
    expect(new Set(be.checkouts().map((c) => c.body.launch_id)).size).toBe(1); // the same launch asks again
  });

  it("waits between the retries", async () => {
    isolate("pro");
    const be = backend("pro");
    leaseDefaults.rateLimitRetryMs = [40, 80, 160];
    be.state.rateLimited = 3;
    const t = Date.now();
    await launch();
    expect(Date.now() - t).toBeGreaterThanOrEqual(270);
    expect(be.checkouts()).toHaveLength(4);
  });

  it("still rate-limited after 3 retries: a paid browser starts on its grace token", async () => {
    const key = isolate("pro");
    const be = backend("pro");
    await launch(false);
    const grace = JSON.parse(readFileSync(graceFile(key), "utf8"));
    be.state.rateLimited = 99;
    const { out: h, lines } = await stderrOf(() => launch(false));
    expect(h.token).toBe(grace.token);
    expect(be.checkouts()).toHaveLength(1 + 4);
    expect(lines.filter((l) => UNCOUNTED.test(l)).join("")).toContain("rate limited");
  });

  it("still rate-limited with no usable token: a LicenseError (RATE_LIMITED), not a ConcurrencyLimitError", async () => {
    isolate("pro");
    const be = backend("pro");
    be.state.rateLimited = 99;
    const err = await acquireLease({ quiet: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(LicenseError);
    expect(err).not.toBeInstanceOf(ConcurrencyLimitError);
    expect((err as LicenseError).code).toBe("RATE_LIMITED");
    expect(be.checkouts()).toHaveLength(4);
  });

  it.each([
    ["CONCURRENCY_LIMIT_EXCEEDED", { error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }],
    ["no code", { error: "Too many browsers." }],
  ] as const)("429 with %s is a ConcurrencyLimitError at once (no retry)", async (_name, body) => {
    isolate("pro");
    const be = backend("pro");
    be.state.checkoutAnswers.push(() => json(429, body));
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(ConcurrencyLimitError);
    expect(be.checkouts()).toHaveLength(1);
  });

  it("a rate-limited heartbeat just waits for the next beat", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    isolate("pro");
    const be = backend("pro");
    const h = await launch();
    const t0 = h.token;
    be.state.heartbeatAnswers.push(() => json(429, { error: "Rate limit exceeded.", code: "RATE_LIMITED" }));
    await vi.advanceTimersByTimeAsync(270_000);
    expect(h.token).toBe(t0);
    expect(be.checkouts()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(270_000);
    expect(h.token).not.toBe(t0);
    expect(be.count("heartbeat")).toBe(2);
  });
});

// ── 10: notice ──────────────────────────────────────────────────────────────────────────────────────
describe("notice", () => {
  it("is printed once per process across launches; no notice prints nothing", async () => {
    isolate("pro");
    const be = backend("pro");
    const quietRun = await stderrOf(async () => { await launch(false); await launch(false); });
    expect(quietRun.lines.filter((l) => l.includes("[license]"))).toEqual([]);
    be.state.notice = "Your plan renews on 1 November.";
    const run = await stderrOf(async () => { await launch(false); await launch(false); });
    expect(run.lines.filter((l) => l.includes("[license]"))).toEqual(["[clearcote] [license] Your plan renews on 1 November.\n"]);
  });

  it("is not printed when quiet", async () => {
    isolate("pro");
    const be = backend("pro");
    be.state.notice = "Something to know.";
    const run = await stderrOf(() => launch(true));
    expect(run.lines).toEqual([]);
  });
});

// ── 13: the grace file only moves forward ────────────────────────────────────────────────────────────
describe("grace file", () => {
  it("is not replaced by a token minted earlier than the one it holds", async () => {
    const key = isolate("pro");
    const newer = tokenFor("pro", 1, 9_999_999_999);
    writeJson(graceFile(key), { token: newer, exp: now() + 3_600 });
    backend("pro");
    await launch(); // mints iat 1001
    expect(JSON.parse(readFileSync(graceFile(key), "utf8")).token).toBe(newer);
  });

  it("is replaced by a token minted at or after the one it holds", async () => {
    const key = isolate("pro");
    writeJson(graceFile(key), { token: tokenFor("pro", 0, 1_001), exp: now() + 3_600 });
    backend("pro");
    const h = await launch(); // iat 1001: same iat replaces it
    expect(JSON.parse(readFileSync(graceFile(key), "utf8")).token).toBe(h.token);
  });
});

// ── the lease cache: only a file this SDK marked as a machine lease skips the checkout ────────────────
// An older SDK's lease-<hash>.json (no `scope`) was written without ever offering the browser scope, so it
// says nothing about how this licence is counted: the first launch checks out to learn it. It is still the
// offline-grace token when the server cannot be reached.
describe("lease cache: zero-call reuse only from a file marked scope machine", () => {
  const olderSdkFile = () => ({ token: tokenFor("pro", 40), exp: now() + 3_600, lease_id: "L-old" });

  it("(a) an older SDK's valid file does not skip the checkout; on a browser-scope answer every launch checks out its own lease", async () => {
    const key = isolate("pro");
    const old = olderSdkFile();
    writeJson(leaseFile(key), old);
    const be = backend("pro");
    const hs = [await launch(), await launch(), await launch()];
    expect(be.checkouts()).toHaveLength(3);
    for (const c of be.checkouts()) expect(c.body.lease_scope_supported).toEqual(["browser"]);
    expect(hs.map((h) => h.token)).not.toContain(old.token);
    expect(new Set(hs.map((h) => h.leaseId)).size).toBe(3);
    expect(JSON.parse(readFileSync(leaseFile(key), "utf8"))).toEqual(old); // left as the older SDK wrote it
  });

  it("(a) an older SDK's file + a machine-scope answer: one checkout marks the file, and the next process reuses it with no call", async () => {
    const key = isolate("pro");
    writeJson(leaseFile(key), olderSdkFile());
    const be = backend("pro", { capped: true });
    const h = await launch();
    expect(be.checkouts()).toHaveLength(1);
    expect(be.checkouts()[0].body.lease_scope_supported).toEqual(["browser"]);
    expect(JSON.parse(readFileSync(leaseFile(key), "utf8")).scope).toBe("machine");
    vi.resetModules(); // another process: a fresh module, nothing in memory
    const other = await import("../src/license.js");
    const h2 = (await other.acquireLease({ quiet: true }))!;
    expect(h2.token).toBe(h.token);
    expect(be.calls).toHaveLength(1);
  });

  it("(b) a file marked scope machine is reused with zero calls", async () => {
    const key = isolate("pro");
    writeJson(leaseFile(key), { ...olderSdkFile(), scope: "machine" });
    const be = backend("pro");
    expect((await launch()).token).toBe(tokenFor("pro", 40));
    expect(be.calls).toHaveLength(0);
  });

  it("(c) an older SDK's file is still the token when the server cannot be reached", async () => {
    const key = isolate("pro");
    writeJson(leaseFile(key), olderSdkFile());
    const be = backend("pro");
    be.state.down = true;
    expect((await launch()).token).toBe(tokenFor("pro", 40));
    expect(be.checkouts()).toHaveLength(1); // tried first
  });

  it("(d) a machine-scope answer writes scope machine next to token, exp and lease_id; heartbeats keep the mark", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const key = isolate("pro");
    backend("pro", { capped: true });
    const h = await launch();
    const file = JSON.parse(readFileSync(leaseFile(key), "utf8"));
    expect(Object.keys(file).sort()).toEqual(["exp", "lease_id", "scope", "token"]);
    expect(file).toMatchObject({ token: h.token, lease_id: h.leaseId, scope: "machine" });
    expect(file.exp).toBeGreaterThan(now() + 80_000);
    const first = h.token;
    await vi.advanceTimersByTimeAsync(270_000); // the owner's heartbeat rotates the token
    expect(h.token).not.toBe(first);
    expect(JSON.parse(readFileSync(leaseFile(key), "utf8"))).toMatchObject({ token: h.token, scope: "machine" });
  });

  it("a lease taken from an older SDK's file during an outage leaves the file unmarked", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const key = isolate("pro");
    writeJson(leaseFile(key), olderSdkFile());
    const be = backend("pro", { capped: true });
    be.state.down = true;
    const h = await launch();
    be.state.down = false;
    await vi.advanceTimersByTimeAsync(270_000); // its heartbeat gets a new token for that lease
    expect(be.count("heartbeat")).toBe(1);
    const file = JSON.parse(readFileSync(leaseFile(key), "utf8"));
    expect(file.token).toBe(h.token);
    expect(file).not.toHaveProperty("scope"); // nothing confirmed how the licence is counted
  });
});
