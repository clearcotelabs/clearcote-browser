// A firewall/bot challenge page in front of the licence server (e.g. the "Vercel Security Checkpoint", a
// 403 HTML page) is not a licence verdict: it throws LicenseServerChallengeError (retryable), never
// LicenseRevokedError, and a cached token still rides it out. The licence server's own JSON 403 still means
// revoked. Served by a real local HTTP server so the fetch path is what is tested.
import { describe, it, expect, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import {
  acquireLease,
  challengeReason,
  getSessionSeats,
  ConcurrencyLimitError,
  LicenseError,
  LicenseRevokedError,
  LicenseServerChallengeError,
} from "../src/license.js";
import * as pkg from "../src/index.js";

// The in-process lease registry is keyed by licence key, so each case uses its own (it would otherwise keep
// the first case's server).
let KEY = "cc_lic_TESTKEY_CHALLENGE";
const CHECKPOINT =
  "<!DOCTYPE html><html><head><title>Vercel Security Checkpoint</title></head>" +
  "<body><p>We're verifying your browser</p></body></html>";

const ENV_KEYS = ["CLEARCOTE_LICENSE_KEY", "CLEARCOTE_LICENSE_API", "HOME", "USERPROFILE", "CLEARCOTE_INSTANCE_ID",
  "CLEARCOTE_LICENSE_THROUGH_PROXY"] as const;
const OLD = Object.fromEntries(ENV_KEYS.map((k) => [k, process.env[k]]));
let servers: Server[] = [];
let homes: string[] = [];
let leases: { stop(): Promise<void> | void }[] = [];

afterEach(async () => {
  for (const l of leases.splice(0)) await l.stop();
  for (const s of servers.splice(0)) await new Promise((r) => s.close(r));
  for (const [k, v] of Object.entries(OLD)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
  for (const h of homes.splice(0)) rmSync(h, { recursive: true, force: true });
});

/** Fresh HOME, a unique instance id (so the process-wide lease registry never reuses another test's), and a
 * local server that answers every request with one canned response. Returns its base URL. */
async function serve(status: number, body: string, contentType: string, extra: Record<string, string> = {}): Promise<string> {
  const home = mkdtempSync(join(tmpdir(), "cc-chal-"));
  homes.push(home);
  process.env.HOME = home;
  process.env.USERPROFILE = home;
  KEY = "cc_lic_TESTKEY_CHALLENGE_" + Math.random().toString(36).slice(2);
  process.env.CLEARCOTE_LICENSE_KEY = KEY;
  process.env.CLEARCOTE_INSTANCE_ID = "chal-" + Math.random().toString(36).slice(2);
  delete process.env.CLEARCOTE_LICENSE_THROUGH_PROXY;
  const srv = createServer((req, res) => {
    req.resume();
    req.on("end", () => {
      res.writeHead(status, { "content-type": contentType, ...extra });
      res.end(body);
    });
  });
  servers.push(srv);
  await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
  const base = `http://127.0.0.1:${(srv.address() as AddressInfo).port}`;
  process.env.CLEARCOTE_LICENSE_API = base;
  return base;
}

function writeCacheFile(obj: unknown): void {
  const id = createHash("sha256").update(KEY).digest("hex").slice(0, 16);
  const dir = join(process.env.HOME!, ".clearcote");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, `lease-${id}.json`), JSON.stringify(obj));
}

describe("licence calls answered by a firewall challenge", () => {
  it.each([
    [403, CHECKPOINT, "text/html; charset=utf-8", { "x-vercel-mitigated": "challenge" }],
    [403, CHECKPOINT, "text/html; charset=utf-8", {}], // the page alone is enough
    [403, "<html>Access denied</html>", "text/html", {}], // any non-JSON 403
    [429, "<html>Too many requests</html>", "text/html", {}], // not a concurrency verdict either
  ] as const)("HTTP %i challenge page is a LicenseServerChallengeError, not a revoke", async (status, body, ct, extra) => {
    await serve(status, body, ct, extra);
    const err = await acquireLease({ quiet: true }).then(
      () => null,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(LicenseServerChallengeError);
    expect(err).toBeInstanceOf(LicenseError); // existing `instanceof LicenseError` handlers still catch it
    expect(err).not.toBeInstanceOf(LicenseRevokedError);
    expect(err).not.toBeInstanceOf(ConcurrencyLimitError);
    const e = err as LicenseServerChallengeError;
    expect(e.code).toBe("LICENSE_SERVER_CHALLENGE");
    expect(e.retryable).toBe(true);
    expect(e.message).toContain("not revoked");
    expect(e.message).toContain(`HTTP ${status}`);
  });

  it("the licence server's own JSON 403 is still a revoke", async () => {
    await serve(403, JSON.stringify({ error: "License revoked.", code: "LICENSE_REVOKED" }), "application/json");
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(LicenseRevokedError);
  });

  it("the licence server's own JSON 429 is still the concurrency limit", async () => {
    await serve(429, JSON.stringify({ error: "limit", code: "CONCURRENCY_LIMIT_EXCEEDED" }), "application/json");
    await expect(acquireLease({ quiet: true })).rejects.toBeInstanceOf(ConcurrencyLimitError);
  });

  it("a challenge on a forced refresh rides on the still-valid cached token", async () => {
    await serve(403, CHECKPOINT, "text/html", { "x-vercel-mitigated": "challenge" });
    writeCacheFile({ token: "CACHED-TOK", exp: Math.floor(Date.now() / 1000) + 800, lease_id: "Lcache", scope: "machine" });
    const lease = (await acquireLease({ quiet: true }))!; // reuses the cache: no call yet
    leases.push(lease);
    expect(lease.token).toBe("CACHED-TOK");
    // The engine refused the held token: heartbeat, then check out. Both get the challenge; the checkout
    // falls back to the cache like a network error instead of throwing a revoke.
    await expect(lease.refreshToken!()).resolves.toBe(true);
    expect(lease.token).toBe("CACHED-TOK");
  });

  it("seats reports a challenge as unavailable, not as an invalid key", async () => {
    const base = await serve(403, CHECKPOINT, "text/html");
    const got = await getSessionSeats({ licenseKey: KEY, apiBase: base });
    expect(got.state).toBe("unavailable");
    expect((got as { reason: string }).reason).toContain("challenge");
  });

  it("challengeReason: JSON error bodies and successes are never a challenge", () => {
    expect(challengeReason(403, "application/json", '{"error":"x"}')).toBeNull();
    expect(challengeReason(200, "text/html", CHECKPOINT)).toBeNull();
    expect(challengeReason(500, "text/html", "<html>oops</html>")).toBeNull();
    expect(challengeReason(403, "text/html", "", "challenge")).toBe("x-vercel-mitigated: challenge");
  });

  it("is exported from the package", () => {
    expect(pkg.LicenseServerChallengeError).toBe(LicenseServerChallengeError);
  });
});
