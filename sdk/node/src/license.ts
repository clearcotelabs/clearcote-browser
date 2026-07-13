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

// ── shared token cache (cross-process reuse + offline grace) ───────────────
// {token, exp, lease_id}. A second process on the machine reuses a still-valid
// token instead of checking out again. Older caches without lease_id are honored.
function cachePath(licenseKey: string): string {
  const id = createHash("sha256").update(licenseKey).digest("hex").slice(0, 16);
  return join(homedir(), ".clearcote", `lease-${id}.json`);
}
function readCache(licenseKey: string): { token: string; exp: number; lease_id?: string } | null {
  try {
    const d = JSON.parse(readFileSync(cachePath(licenseKey), "utf8"));
    if (d && typeof d.token === "string" && typeof d.exp === "number") return d;
  } catch {
    /* ignore */
  }
  return null;
}
function writeCache(licenseKey: string, token: string, exp: number, leaseId?: string): void {
  try {
    const dir = join(homedir(), ".clearcote");
    mkdirSync(dir, { recursive: true });
    writeFileSync(cachePath(licenseKey), JSON.stringify({ token, exp, lease_id: leaseId ?? null }));
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

// Seconds of headroom kept before a token's exp: reuse it only while still valid
// with this much slack, so an in-flight launch never ships an expiring token.
const SKEW_SEC = 60;

/**
 * One shared lease per (process, license key).
 *
 * Concurrency is per-MACHINE (the backend dedups by instance_id), so re-checking
 * out on every launch is redundant — the machine already holds its one slot. This
 * checks out at most once per token-TTL and lets every launch in the process share
 * the same run-token, cutting backend calls from O(launches) to O(TTL windows).
 * Only the cold-checkout owner heartbeats + checks in at exit; a process that
 * reuses a still-valid on-disk token makes no backend calls at all.
 */
class MachineLease {
  token: string | null = null;
  exp = 0;
  leaseId: string | null = null;
  private hbSec = 270;
  private owner = false;
  private timer: NodeJS.Timeout | null = null;
  private refs = 0;
  private ensuring: Promise<void> | null = null;
  private engineResolved: string | null = null;

  constructor(
    private readonly key: string,
    private readonly base: string,
    private readonly instanceId: string,
    private readonly sdkVersion: string | undefined,
    private readonly quiet: boolean,
    // Resolved browser build (e.g. "150.0.7871.114"). A string, or a thunk that resolves it lazily
    // so the catalog is only consulted on a cold checkout — never per launch. Telemetry only.
    private readonly engineVersion?: string | (() => string | Promise<string>),
  ) {}

  private valid(): boolean {
    return !!this.token && this.exp > Math.floor(Date.now() / 1000) + SKEW_SEC;
  }

  /** Resolved engine version for telemetry — memoized, resolved at most once (on cold checkout).
   * Any failure yields undefined (the field is simply omitted from the body). */
  private async engineVer(): Promise<string | undefined> {
    if (this.engineResolved === null) {
      try {
        const ev = this.engineVersion;
        this.engineResolved = (typeof ev === "function" ? await ev() : ev) || "";
      } catch {
        this.engineResolved = "";
      }
    }
    return this.engineResolved || undefined;
  }

  /** Serialize concurrent ensures so only one cold checkout happens. */
  ensure(): Promise<void> {
    if (this.valid()) return Promise.resolve();
    if (!this.ensuring) this.ensuring = this._ensure().finally(() => { this.ensuring = null; });
    return this.ensuring;
  }

  private async _ensure(): Promise<void> {
    if (this.valid()) return;
    const now = Math.floor(Date.now() / 1000);
    const cached = readCache(this.key);
    if (cached && cached.exp > now + SKEW_SEC) {
      // cross-process reuse: another process's owner keeps the slot alive.
      this.token = cached.token;
      this.exp = cached.exp;
      this.leaseId = cached.lease_id ?? null;
      this.owner = false;
      return; // NO checkout, NO heartbeat
    }
    await this.checkout();
    this.owner = true;
    this.startHeartbeat();
  }

  private async checkout(): Promise<void> {
    try {
      const res = await postJson(`${this.base}/api/v1/lease/checkout`, this.key, {
        instance_id: this.instanceId,
        os: osTag(),
        sdk_version: this.sdkVersion,
        engine_version: await this.engineVer(),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string; code?: string };
        throwForStatus(res.status, body);
      }
      const co = (await res.json()) as CheckoutResponse;
      this.token = co.token;
      this.exp = co.exp;
      this.leaseId = co.lease_id;
      this.hbSec = Math.max(5, co.heartbeat_interval_sec || 270);
      writeCache(this.key, co.token, co.exp, co.lease_id);
    } catch (e) {
      if (e instanceof LicenseError) throw e; // definitive verdict must surface
      const cached = readCache(this.key);
      const now = Math.floor(Date.now() / 1000);
      if (cached && cached.exp > now + SKEW_SEC) {
        if (!this.quiet)
          process.stderr.write(`[clearcote] [license] backend unreachable (${String(e)}); using cached run-token.\n`);
        this.token = cached.token;
        this.exp = cached.exp;
        this.leaseId = cached.lease_id ?? null;
        return;
      }
      throw new LicenseError(`Could not reach the license server and no valid cached token: ${String(e)}`);
    }
  }

  private startHeartbeat(): void {
    if (this.timer) return;
    this.timer = setInterval(async () => {
      try {
        const res = await postJson(`${this.base}/api/v1/lease/heartbeat`, this.key, {
          lease_id: this.leaseId,
          nonce: randomUUID(),
        });
        if (res.status === 409) {
          const co = await postJson(`${this.base}/api/v1/lease/checkout`, this.key, {
            instance_id: this.instanceId,
            os: osTag(),
            sdk_version: this.sdkVersion,
            engine_version: await this.engineVer(),
          });
          if (co.ok) {
            const d = (await co.json()) as CheckoutResponse;
            this.leaseId = d.lease_id;
            this.token = d.token;
            this.exp = d.exp;
            writeCache(this.key, d.token, d.exp, d.lease_id);
          }
          return;
        }
        if (res.ok) {
          const d = (await res.json()) as { token: string; exp: number };
          this.token = d.token;
          this.exp = d.exp;
          writeCache(this.key, d.token, d.exp, this.leaseId ?? undefined);
        }
      } catch {
        /* transient — offline grace until token exp */
      }
    }, this.hbSec * 1000);
    (this.timer as unknown as { unref?: () => void }).unref?.();
  }

  async acquire(): Promise<LeaseSession> {
    await this.ensure();
    this.refs++;
    const self = this;
    return {
      get token() {
        return self.token as string;
      },
      get leaseId() {
        return (self.leaseId ?? "cached") as string;
      },
      // Per-launch close: refcount only. The machine slot is held for the process
      // lifetime and reclaimed by TTL after the heartbeat stops (see shutdown()).
      stop: async () => {
        self.release();
      },
    } as LeaseSession;
  }

  release(): void {
    if (this.refs > 0) this.refs--;
  }

  async shutdown(): Promise<void> {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.owner && this.leaseId) {
      try {
        await postJson(`${this.base}/api/v1/lease/checkin`, this.key, { lease_id: this.leaseId });
      } catch {
        /* best-effort; the lease TTL reclaims it anyway */
      }
    }
  }
}

const machineLeases = new Map<string, MachineLease>();
let exitHooked = false;

/**
 * Acquire a per-MACHINE concurrency lease, shared across every launch in this
 * process (checks out ~once per token-TTL, not once per launch). Returns `null` in
 * free mode. Throws {@link ConcurrencyLimitError} / {@link LicenseRevokedError} /
 * {@link LicenseError} only on a cold checkout the backend definitively refuses;
 * falls back to a cached, still-valid token on a transient network failure.
 */
export async function acquireLease(
  opts: LicenseOptions & {
    sdkVersion?: string;
    quiet?: boolean;
    engineVersion?: string | (() => string | Promise<string>);
  } = {},
): Promise<LeaseSession | null> {
  const licenseKey = resolveLicenseKey(opts.licenseKey);
  if (!licenseKey) return null; // free mode — inert

  const base = apiBase(opts);
  let ml = machineLeases.get(licenseKey);
  if (!ml) {
    ml = new MachineLease(licenseKey, base, resolveInstanceId(), opts.sdkVersion, !!opts.quiet,
      opts.engineVersion);
    machineLeases.set(licenseKey, ml);
  }
  if (!exitHooked) {
    exitHooked = true;
    process.once("beforeExit", () => {
      for (const m of machineLeases.values()) void m.shutdown();
    });
  }
  return ml.acquire();
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
