// The PRO engine refuses a run-token older than the newest it has accepted for this OS user.
//
// Its clock-rollback guard (patch 990) keeps that newest `iat` in `$LOCALAPPDATA/.clearcote/.cc_hwm` (else
// `$HOME`) and refuses any lower one: "this run-token is older than the last one accepted here (system clock
// set back?); refusing." The SDK reuses a cached token for its 24 h life, and anything else under the same OS
// user that launches with a newer token (another process, the hosted-browser gateway, a run with another key)
// moves the mark past it. On a production worker that failed every job until the cache was deleted by hand.
//
// So (1) acquireLease() replaces a token it can see is behind the mark, preferring a heartbeat of the lease it
// knows (same lease, nothing revoked) over a checkout; and (2) a launch the engine refuses anyway (a race)
// refreshes the token and launches once more. Mirrors sdk/python/tests/test_license_stale_token.py; the same
// scenario ran end to end against the real r29 engine. Hermetic: fetch is mocked, HOME/LOCALAPPDATA are temp.
import { describe, it, expect, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { acquireLease, engineHwm, tokenIat, STALE_TOKEN_REFUSAL, type LeaseSession } from "../src/license.js";

const realFetch = globalThis.fetch;
const REFUSAL =
  "browserType.launchPersistentContext: Target page, context or browser has been closed\nBrowser logs:\n" +
  "[pid=1][err] [clearcote] licence: this run-token is older than the last one accepted here (system clock set back?); refusing.";

const tok = (iat: number, plan = "pro") => Buffer.from(JSON.stringify({ v: 1, plan, iat })).toString("base64url") + ".sig";

const ENV_KEYS = ["HOME", "USERPROFILE", "LOCALAPPDATA", "CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "CLEARCOTE_INSTANCE_ID"] as const;
let saved: Record<string, string | undefined> = {};
const homes: string[] = [];

/** A temp HOME that is also LOCALAPPDATA (the engine mark lives there), and a fresh licence key. */
function isolate(): string {
  saved = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
  const home = mkdtempSync(join(tmpdir(), "cc-stale-"));
  homes.push(home);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  process.env.LOCALAPPDATA = home;
  process.env.CLEARCOTE_LICENSE_API = "http://test.local";
  process.env.CLEARCOTE_LICENSE_KEY = "cc_lic_stale_" + Date.now() + "_" + Math.random().toString(36).slice(2);
  delete process.env.CLEARCOTE_INSTANCE_ID;
  return home;
}

afterEach(() => {
  globalThis.fetch = realFetch;
  for (const [k, v] of Object.entries(saved)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  for (const h of homes.splice(0)) rmSync(h, { recursive: true, force: true });
});

function setMark(home: string, iat: number): void {
  mkdirSync(join(home, ".clearcote"), { recursive: true });
  writeFileSync(join(home, ".clearcote", ".cc_hwm"), String(iat));
}

function cacheToken(home: string, iat: number, leaseId: string | null = "L1"): void {
  const id = createHash("sha256").update(process.env.CLEARCOTE_LICENSE_KEY!).digest("hex").slice(0, 16);
  mkdirSync(join(home, ".clearcote"), { recursive: true });
  writeFileSync(join(home, ".clearcote", `lease-${id}.json`), JSON.stringify({ token: tok(iat), exp: Math.floor(Date.now() / 1000) + 3600, lease_id: leaseId, scope: "machine" }));
}

type Answer = [status: number, body: unknown];
/** fetch: records (endpoint, body); answers[endpoint] are served in order, the last one repeating. */
function backend(answers: Record<string, Answer[]>) {
  const calls: { ep: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = (async (url: unknown, init?: { body?: string }) => {
    const ep = String(url).split("/").pop()!;
    calls.push({ ep, body: init?.body ? JSON.parse(init.body) : {} });
    const q = answers[ep] ?? [[200, {}]];
    const [status, body] = q.length > 1 ? q.shift()! : q[0];
    return new Response(JSON.stringify(body), { status });
  }) as typeof fetch;
  return calls;
}
const exp = () => Math.floor(Date.now() / 1000) + 3600;

// ── reading the token and the engine's mark ──────────────────────────────────────────────────────────
describe("token iat and the engine mark", () => {
  it("reads the token's iat from its payload", () => {
    expect(tokenIat(tok(1790685547))).toBe(1790685547);
    expect(tokenIat("not-a-token")).toBeUndefined();
    expect(tokenIat(Buffer.from(JSON.stringify({ iat: "soon" })).toString("base64url") + ".s")).toBeUndefined();
  });

  it("finds the mark where the engine keeps it", () => {
    const root = mkdtempSync(join(tmpdir(), "cc-stale-"));
    homes.push(root);
    const local = join(root, "local");
    const home = join(root, "home");
    for (const [d, v] of [[local, "200"], [home, "100"]] as const) {
      mkdirSync(join(d, ".clearcote"), { recursive: true });
      writeFileSync(join(d, ".clearcote", ".cc_hwm"), v + "\n");
    }
    expect(engineHwm({ LOCALAPPDATA: local, HOME: home })).toBe(200); // Windows: LOCALAPPDATA first
    expect(engineHwm({ HOME: home })).toBe(100); // elsewhere: HOME
    expect(engineHwm({ LOCALAPPDATA: "", HOME: home })).toBe(100); // empty counts as unset, as there
    expect(engineHwm({ HOME: join(root, "none") })).toBe(0);
    expect(engineHwm({})).toBe(0);
    writeFileSync(join(home, ".clearcote", ".cc_hwm"), "garbage");
    expect(engineHwm({ HOME: home })).toBe(0);
  });
});

// ── (1) acquire: a token the engine would refuse is replaced before the launch ──────────────────────────
describe("acquireLease — a token behind the engine mark is refreshed first", () => {
  it("heartbeats the lease it knows (no checkout: same lease, nothing revoked)", async () => {
    const home = isolate();
    cacheToken(home, 1000, "L-owner");
    setMark(home, 1500); // another process launched with a newer token
    const calls = backend({ heartbeat: [[200, { token: tok(2000), exp: exp() }]] });
    const h = await acquireLease({ quiet: true });
    expect(calls.map((c) => c.ep)).toEqual(["heartbeat"]);
    expect(calls[0].body.lease_id).toBe("L-owner");
    expect(tokenIat(h!.token)).toBe(2000);
  });

  it("reuses a token at or past the mark with no backend call (the engine only refuses iat < mark)", async () => {
    for (const mark of [1000, 900]) {
      const home = isolate();
      cacheToken(home, 1000);
      setMark(home, mark);
      const calls = backend({});
      expect(tokenIat((await acquireLease({ quiet: true }))!.token)).toBe(1000);
      expect(calls).toEqual([]);
    }
  });

  it("has nothing to compare while there is no mark yet", async () => {
    const home = isolate();
    cacheToken(home, 1000);
    const calls = backend({});
    expect(tokenIat((await acquireLease({ quiet: true }))!.token)).toBe(1000);
    expect(calls).toEqual([]);
  });

  it("falls back to a checkout when the lease is gone", async () => {
    const home = isolate();
    cacheToken(home, 1000, "L-dead");
    setMark(home, 1500);
    const calls = backend({
      heartbeat: [[409, { code: "LEASE_NOT_FOUND" }]],
      checkout: [[200, { lease_id: "L-new", token: tok(2000), exp: exp(), lease_ttl_sec: 810, heartbeat_interval_sec: 3600, concurrency: { used: 1, limit: 5 } }]],
    });
    const h = await acquireLease({ quiet: true });
    expect(calls.map((c) => c.ep)).toEqual(["heartbeat", "checkout"]);
    expect(tokenIat(h!.token)).toBe(2000);
    expect(h!.leaseId).toBe("L-new");
  });

  it("refreshes a legacy cache without a lease id by checkout", async () => {
    const home = isolate();
    cacheToken(home, 1000, null);
    setMark(home, 1500);
    const calls = backend({ checkout: [[200, { lease_id: "L2", token: tok(2000), exp: exp(), heartbeat_interval_sec: 3600 }]] });
    expect(tokenIat((await acquireLease({ quiet: true }))!.token)).toBe(2000);
    expect(calls.map((c) => c.ep)).toEqual(["checkout"]);
  });

  it("surfaces a definitive refusal instead of a doomed launch", async () => {
    const home = isolate();
    cacheToken(home, 1000, "L-dead");
    setMark(home, 1500);
    backend({
      heartbeat: [[409, { code: "LEASE_EXPIRED" }]],
      checkout: [[429, { error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }]],
    });
    await expect(acquireLease({ quiet: true })).rejects.toThrow(/limit/);
  });

  it("refreshes an in-memory token overtaken since the last launch", async () => {
    const home = isolate();
    const calls = backend({
      checkout: [[200, { lease_id: "L1", token: tok(1000), exp: exp(), heartbeat_interval_sec: 3600 }]],
      heartbeat: [[200, { token: tok(3000), exp: exp() }]],
    });
    expect(tokenIat((await acquireLease({ quiet: true }))!.token)).toBe(1000);
    setMark(home, 2000); // e.g. the hosted-browser gateway launched meanwhile
    expect(tokenIat((await acquireLease({ quiet: true }))!.token)).toBe(3000);
    expect(calls.map((c) => c.ep)).toEqual(["checkout", "heartbeat"]);
  });

  it("the machine handle refreshes on demand even when it cannot see the mark", async () => {
    const home = isolate();
    cacheToken(home, 1000, "L1");
    const calls = backend({ heartbeat: [[200, { token: tok(2000), exp: exp() }]] });
    const h = await acquireLease({ quiet: true });
    expect(calls).toEqual([]);
    expect(await h!.refreshToken!()).toBe(true);
    expect(tokenIat(h!.token)).toBe(2000);
  });
});

// ── (2) launch: the engine refused anyway (a race) -> fresh token, one more launch ─────────────────────
describe("retryOnStaleRunToken", () => {
  function fakeLease(ok = true) {
    const lease = {
      token: "TOK-OLD",
      refreshes: 0,
      async refreshToken() {
        lease.refreshes++;
        if (ok) lease.token = "TOK-FRESH";
        return ok;
      },
    };
    return lease;
  }

  it("retries once with the fresh token when the engine refuses the old one", async () => {
    const { retryOnStaleRunToken } = await import("../src/index.js");
    const lease = fakeLease();
    const seen: string[] = [];
    const out = await retryOnStaleRunToken(lease as unknown as LeaseSession, async () => {
      seen.push(lease.token);
      if (seen.length === 1) throw new Error(REFUSAL);
      return "context";
    });
    expect(out).toBe("context");
    expect(seen).toEqual(["TOK-OLD", "TOK-FRESH"]);
    expect(lease.refreshes).toBe(1);
  });

  it.each(["other error", "refresh failed", "no lease", "still refused"] as const)("%s: raised as it was, at most one retry", async (c) => {
    const { retryOnStaleRunToken } = await import("../src/index.js");
    const lease = c === "no lease" ? null : fakeLease(c !== "refresh failed");
    let attempts = 0;
    await expect(retryOnStaleRunToken(lease as unknown as LeaseSession | null, async () => {
      attempts++;
      throw new Error(c === "other error" ? "spawn UNKNOWN" : REFUSAL);
    })).rejects.toThrow();
    expect(attempts).toBe(c === "still refused" ? 2 : 1);
    if (c === "other error") expect(lease!.refreshes).toBe(0);
  });

  it("matches the engine's own refusal line", () => {
    expect(REFUSAL.includes(STALE_TOKEN_REFUSAL)).toBe(true);
  });
});

// ── the per-browser (free) lease refreshes on demand too ─────────────────────────────────────────────
describe("per-browser lease refreshToken", () => {
  it("heartbeats its own lease, or re-takes its slot as the SAME launch", async () => {
    isolate();
    const calls = backend({
      checkout: [
        [200, { lease_id: "L-b1", token: tok(1000, "free"), exp: exp(), heartbeat_interval_sec: 3600, lease_scope: "browser" }],
        [200, { lease_id: "L-b2", token: tok(3000, "free"), exp: exp(), heartbeat_interval_sec: 3600, lease_scope: "browser" }],
      ],
      heartbeat: [[200, { token: tok(2000, "free") }], [409, { code: "LEASE_EXPIRED" }]],
    });
    const b = await acquireLease({ quiet: true });
    try {
      expect(await b!.refreshToken!()).toBe(true);
      expect(tokenIat(b!.token)).toBe(2000);
      expect(await b!.refreshToken!()).toBe(true);
      expect(tokenIat(b!.token)).toBe(3000);
      expect(b!.leaseId).toBe("L-b2");
      const checkouts = calls.filter((c) => c.ep === "checkout");
      expect(checkouts[1].body.launch_id).toBe(checkouts[0].body.launch_id); // the same launch retakes it
    } finally {
      await b!.stop();
    }
  });
});
