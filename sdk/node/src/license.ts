// Floating-concurrency licensing client (opt-in).
//
// When a license key is present, the SDK checks out one of the license's N
// concurrency slots from the backend, receives a short-lived Ed25519 "run-token",
// and injects it into the engine as CLEARCOTE_RUN_TOKEN. A background heartbeat
// keeps the slot alive + rotates the token; on close the slot is released. The
// PRO engine's gate refuses to launch without a valid token.
//
// With NO license key this is entirely inert — the free build never calls the
// backend and never gates. See clearcoat/PRIVATE-SDK-LICENSING-PLAN.md.

import { existsSync, readFileSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";

/** Default backend. Override with `licenseApiBase` or CLEARCOTE_LICENSE_API. */
const DEFAULT_API_BASE = "https://www.clearcotelabs.com";
const RUN_TOKEN_ENV = "CLEARCOTE_RUN_TOKEN";

export interface LicenseOptions {
  /** License key (`cc_lic_...`). Resolved from this > CLEARCOTE_LICENSE_KEY env >
   * ~/.clearcote/license.key. When absent, licensing is fully inert (free mode). */
  licenseKey?: string;
  /** Backend base URL. Default CLEARCOTE_LICENSE_API env or clearcotelabs.com. */
  licenseApiBase?: string;
}

export class LicenseError extends Error {
  code: string;
  constructor(message: string, code = "LICENSE_ERROR") {
    super(message);
    this.name = "LicenseError";
    this.code = code;
  }
}
export class ConcurrencyLimitError extends LicenseError {
  constructor(message: string) {
    super(message, "CONCURRENCY_LIMIT_EXCEEDED");
    this.name = "ConcurrencyLimitError";
  }
}
export class LicenseRevokedError extends LicenseError {
  constructor(message: string) {
    super(message, "LICENSE_REVOKED");
    this.name = "LicenseRevokedError";
  }
}

/** Resolve a license key: explicit > CLEARCOTE_LICENSE_KEY env > ~/.clearcote/license.key. */
export function resolveLicenseKey(explicit?: string): string | undefined {
  if (explicit && explicit.trim()) return explicit.trim();
  const env = process.env.CLEARCOTE_LICENSE_KEY;
  if (env && env.trim()) return env.trim();
  try {
    const p = join(homedir(), ".clearcote", "license.key");
    if (existsSync(p)) {
      const v = readFileSync(p, "utf8").trim();
      if (v) return v;
    }
  } catch {
    /* ignore */
  }
  return undefined;
}

/** A STABLE per-machine id so a restart REUSES its concurrency slot instead of spawning a second
 * lease (the backend dedupes a machine's own prior live lease on re-checkout). Order:
 * CLEARCOTE_INSTANCE_ID env > ~/.clearcote/instance_id file > a freshly generated id (persisted).
 * Falls back to an ephemeral id if the file can't be written — in containers with an ephemeral
 * filesystem, set CLEARCOTE_INSTANCE_ID per replica to keep it stable. */
export function resolveInstanceId(): string {
  const env = process.env.CLEARCOTE_INSTANCE_ID;
  if (env && env.trim()) return env.trim();
  const dir = join(homedir(), ".clearcote");
  const p = join(dir, "instance_id");
  try {
    if (existsSync(p)) {
      const v = readFileSync(p, "utf8").trim();
      if (v) return v;
    }
  } catch {
    /* ignore */
  }
  const id = randomUUID();
  try {
    mkdirSync(dir, { recursive: true });
    writeFileSync(p, id + "\n");
  } catch {
    /* ephemeral fallback — set CLEARCOTE_INSTANCE_ID to persist across restarts */
  }
  return id;
}

function apiBase(opts: LicenseOptions): string {
  return (opts.licenseApiBase || process.env.CLEARCOTE_LICENSE_API || DEFAULT_API_BASE).replace(/\/$/, "");
}

const osTag = (): string =>
  ({ win32: "windows", linux: "linux", darwin: "macos" } as Record<string, string>)[process.platform] ?? "unknown";

// ── offline token cache (best-effort grace) ───────────────────────────────
function cachePath(licenseKey: string): string {
  const id = createHash("sha256").update(licenseKey).digest("hex").slice(0, 16);
  return join(homedir(), ".clearcote", `lease-${id}.json`);
}
function readCache(licenseKey: string): { token: string; exp: number } | null {
  try {
    const d = JSON.parse(readFileSync(cachePath(licenseKey), "utf8"));
    if (d && typeof d.token === "string" && typeof d.exp === "number") return d;
  } catch {
    /* ignore */
  }
  return null;
}
function writeCache(licenseKey: string, token: string, exp: number): void {
  try {
    const dir = join(homedir(), ".clearcote");
    mkdirSync(dir, { recursive: true });
    writeFileSync(cachePath(licenseKey), JSON.stringify({ token, exp }));
  } catch {
    /* ignore */
  }
}

interface CheckoutResponse {
  lease_id: string;
  token: string;
  exp: number;
  lease_ttl_sec: number;
  heartbeat_interval_sec: number;
  concurrency: { used: number; limit: number };
}

async function postJson(url: string, licenseKey: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers: { authorization: `Bearer ${licenseKey}`, "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

function throwForStatus(status: number, body: { error?: string; code?: string }): never {
  const msg = body?.error || `License request failed (${status}).`;
  if (status === 429 || body?.code === "CONCURRENCY_LIMIT_EXCEEDED") throw new ConcurrencyLimitError(msg);
  if (status === 403 || body?.code === "LICENSE_REVOKED" || body?.code === "LICENSE_EXPIRED")
    throw new LicenseRevokedError(msg);
  throw new LicenseError(msg, body?.code || `HTTP_${status}`);
}

/** A live lease. Keep it until the browser closes, then call `stop()`. */
export interface LeaseSession {
  token: string;
  leaseId: string;
  /** Release the slot + stop the heartbeat (best-effort; safe to call twice). */
  stop(): Promise<void>;
}

/**
 * Acquire a concurrency lease if a license key is configured.
 *
 * Returns `null` in free mode (no key) — the caller launches normally with no
 * token. Throws {@link ConcurrencyLimitError} / {@link LicenseRevokedError} /
 * {@link LicenseError} when a key IS present but the backend refuses. On a
 * network failure with a still-valid cached token, resumes offline (degraded).
 */
export async function acquireLease(
  opts: LicenseOptions & { sdkVersion?: string; quiet?: boolean } = {},
): Promise<LeaseSession | null> {
  const licenseKey = resolveLicenseKey(opts.licenseKey);
  if (!licenseKey) return null; // free mode — inert

  const base = apiBase(opts);
  const instanceId = resolveInstanceId();
  const warn = (m: string) => {
    if (!opts.quiet) process.stderr.write(`[clearcote] [license] ${m}\n`);
  };

  let checkout: CheckoutResponse;
  try {
    const res = await postJson(`${base}/api/v1/lease/checkout`, licenseKey, {
      instance_id: instanceId,
      os: osTag(),
      sdk_version: opts.sdkVersion,
    });
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string; code?: string };
      throwForStatus(res.status, body);
    }
    checkout = (await res.json()) as CheckoutResponse;
    writeCache(licenseKey, checkout.token, checkout.exp);
  } catch (e) {
    // A definitive licensing verdict must surface (never silently downgrade).
    if (e instanceof LicenseError) throw e;
    // Network/other failure: fall back to a cached, still-valid token if we have one.
    const cached = readCache(licenseKey);
    const now = Math.floor(Date.now() / 1000);
    if (cached && cached.exp > now + 60) {
      warn(`backend unreachable (${String(e)}); using cached run-token (offline grace).`);
      return { token: cached.token, leaseId: "cached", stop: async () => {} };
    }
    throw new LicenseError(`Could not reach the license server and no valid cached token: ${String(e)}`);
  }

  let leaseId = checkout.lease_id;
  let currentToken = checkout.token;
  const hbMs = Math.max(5, checkout.heartbeat_interval_sec || 30) * 1000;

  const timer: NodeJS.Timeout = setInterval(async () => {
    try {
      const res = await postJson(`${base}/api/v1/lease/heartbeat`, licenseKey, { lease_id: leaseId, nonce: randomUUID() });
      if (res.status === 409) {
        // Lease reclaimed/expired server-side — re-check out to keep the slot.
        const co = await postJson(`${base}/api/v1/lease/checkout`, licenseKey, {
          instance_id: instanceId, os: osTag(), sdk_version: opts.sdkVersion,
        });
        if (co.ok) {
          const data = (await co.json()) as CheckoutResponse;
          leaseId = data.lease_id;
          currentToken = data.token;
          writeCache(licenseKey, data.token, data.exp);
        }
        return;
      }
      if (res.ok) {
        const data = (await res.json()) as { token: string; exp: number };
        currentToken = data.token;
        writeCache(licenseKey, data.token, data.exp);
      }
    } catch {
      /* transient — offline grace until token exp */
    }
  }, hbMs);
  // Don't keep the process alive just for the heartbeat.
  (timer as unknown as { unref?: () => void }).unref?.();

  let stopped = false;
  const stop = async () => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    try {
      await postJson(`${base}/api/v1/lease/checkin`, licenseKey, { lease_id: leaseId });
    } catch {
      /* best-effort; the lease TTL will reclaim it anyway */
    }
  };

  return {
    get token() {
      return currentToken;
    },
    leaseId,
    stop,
  } as LeaseSession;
}

/** Merge the run-token into a child-process env (base defaults to the parent env). */
export function withRunToken(
  token: string,
  baseEnv: Record<string, string | undefined> | undefined,
): Record<string, string> {
  const src = baseEnv ?? process.env;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) if (v !== undefined) out[k] = v;
  out[RUN_TOKEN_ENV] = token;
  return out;
}
