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

import { existsSync, readFileSync, mkdirSync, writeFileSync, rmSync, chmodSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { proxiedRequest, toProxySpec, type ProxySpec, type SimpleResponse } from "./net.js";

/** Default backend. Override with `licenseApiBase` or CLEARCOTE_LICENSE_API. */
const DEFAULT_API_BASE = "https://www.clearcotelabs.com";
const RUN_TOKEN_ENV = "CLEARCOTE_RUN_TOKEN";

/**
 * Names this SDK and its version on every licence call (fetch alone says just "node"), so the licence
 * server's logs can tell SDK builds apart, and tell clients that are not an SDK from ours. Spelled
 * "User-Agent": the proxied path then replaces its own default instead of sending a second header.
 */
const LICENSE_USER_AGENT = `clearcote-sdk-node/${(() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return "unknown";
  }
})()}`;

export interface LicenseOptions {
  /** License key (`cc_lic_...`). Resolved from this > CLEARCOTE_LICENSE_KEY env >
   * ~/.clearcote/license.key. When absent, licensing is fully inert (free mode). */
  licenseKey?: string;
  /** Backend base URL. Default CLEARCOTE_LICENSE_API env or clearcotelabs.com. */
  licenseApiBase?: string;
  /**
   * Send the licence calls (lease checkout / heartbeat / check-in) through the launch's own proxy
   * instead of directly from this machine. Off by default: direct calls never spend proxy bandwidth.
   * Turn it on when the host's own address must not reach the licence server, or when the host has
   * no direct egress. Also enabled by CLEARCOTE_LICENSE_THROUGH_PROXY=1. No effect without a proxy.
   */
  licenseThroughProxy?: boolean;
}

/** Whether licence calls should use the launch proxy: explicit option, else the env switch. */
export function licenseThroughProxyRequested(opt: boolean | undefined, env: Record<string, string | undefined> = process.env): boolean {
  if (opt !== undefined) return opt;
  return /^(1|true|yes|on)$/i.test((env.CLEARCOTE_LICENSE_THROUGH_PROXY ?? "").trim());
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

/** The licence server base URL (`licenseApiBase` > CLEARCOTE_LICENSE_API > clearcotelabs.com). */
export function licenseApiBase(explicit?: string): string {
  return apiBase({ licenseApiBase: explicit });
}

/** The User-Agent every licence call sends ("clearcote-sdk-node/<version>"). */
export function licenseUserAgent(): string {
  return LICENSE_USER_AGENT;
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
    if (d && typeof d.token === "string" && typeof d.exp === "number") {
      // A per-browser (free-tier) token belongs to the one browser it was checked out for. Reusing it
      // would start another browser without a slot, so it is never taken from the cache, even one an
      // older SDK wrote. Paid tokens are reused exactly as before.
      if (tokenPlan(d.token) === PER_BROWSER_PLAN) return null;
      return d;
    }
  } catch {
    /* ignore */
  }
  return null;
}

/** The plan a run-token was minted for, read from its payload WITHOUT verifying it (routing only). */
export function tokenPlan(token: string): string | undefined {
  try {
    const body = token.split(".")[0] ?? "";
    const json = Buffer.from(body.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
    const plan = (JSON.parse(json) as { plan?: unknown }).plan;
    return typeof plan === "string" ? plan : undefined;
  } catch {
    return undefined;
  }
}

/** When a run-token was minted (its `iat`, epoch seconds), read from its payload WITHOUT verifying it. */
export function tokenIat(token: string): number | undefined {
  try {
    const body = token.split(".")[0] ?? "";
    const json = Buffer.from(body.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
    const iat = (JSON.parse(json) as { iat?: unknown }).iat;
    return typeof iat === "number" && Number.isFinite(iat) ? Math.trunc(iat) : undefined;
  } catch {
    return undefined;
  }
}

/** What the PRO engine refuses a launch with when its run-token is older than the newest one it has accepted. */
export const STALE_TOKEN_REFUSAL = "older than the last one accepted";

/**
 * The newest run-token `iat` the PRO engine has accepted for this OS user (0 if none/unreadable).
 *
 * The engine (patch 990, clock-rollback guard) keeps it in `$LOCALAPPDATA/.clearcote/.cc_hwm`, else
 * `$HOME/.clearcote/.cc_hwm`, as a decimal number, and refuses any token with a lower `iat`. It is per OS
 * user, not per licence or process: another SDK process, the hosted-browser gateway or a run with a
 * different key can all move it past a token this process still holds. Looked up exactly as the engine
 * does it, env var by env var.
 */
export function engineHwm(env: Record<string, string | undefined> = process.env): number {
  const baseDir = env.LOCALAPPDATA || env.HOME;
  if (!baseDir) return 0;
  try {
    const v = Number.parseInt(readFileSync(join(baseDir, ".clearcote", ".cc_hwm"), "ascii").trim(), 10);
    return Number.isFinite(v) ? v : 0;
  } catch {
    return 0;
  }
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
  /** "browser" on per-browser plans (the free tier); absent = the machine-shared lease. */
  lease_scope?: "browser";
}

/** The plan whose tokens are per browser. Only used to keep such tokens out of the shared cache. */
const PER_BROWSER_PLAN = "free";

/** One id per browser launch: the backend counts every launch_id as its own slot on per-browser plans. */
export function newLaunchId(): string {
  return randomUUID().replace(/-/g, "");
}

async function postJson(url: string, licenseKey: string, body: unknown, proxy?: ProxySpec | null): Promise<Response | SimpleResponse> {
  const init = {
    method: "POST",
    headers: { authorization: `Bearer ${licenseKey}`, "content-type": "application/json", "User-Agent": LICENSE_USER_AGENT },
    body: JSON.stringify(body),
  };
  // Through the launch proxy when licenseThroughProxy is on; otherwise the plain fetch path, which
  // is unchanged from before the option existed.
  if (proxy) return proxiedRequest(url, { ...init, proxy, timeoutMs: 30_000 });
  return fetch(url, init);
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
  /**
   * Bind a per-launch run-token file (CLEARCOTE_RUN_TOKEN_FILE). It follows this lease's rotating
   * token so a supporting engine (r23+) can re-read it and stop a running free browser once the token
   * stops advancing. Call release() when the browser closes. Older engines ignore the file.
   */
  bindLaunch(): { file: string; release(): void };
  /**
   * The engine refused this lease's token as older than one it has already accepted on this machine:
   * mint a fresh one. Resolves true when `token` now holds it.
   */
  refreshToken?(): Promise<boolean>;
}

// Seconds of headroom kept before a token's exp: reuse it only while still valid
// with this much slack, so an in-flight launch never ships an expiring token.
const SKEW_SEC = 60;

// ── run-token files (engine online-enforcement opt-in) ───────────────────────
// A supporting engine (152 r23+) re-reads the run-token from CLEARCOTE_RUN_TOKEN_FILE and stops a
// running FREE browser once the token stops advancing (the SDK can only advance it by heartbeating,
// which the backend gates). This class mirrors a lease's rotating token into one file per launch and
// removes it on close. Older engines ignore the file (they read CLEARCOTE_RUN_TOKEN once at launch),
// so it is purely additive — nothing breaks if the engine does not support it.
class TokenFileSet {
  private readonly files = new Set<{ path: string }>();

  /** Create a token file seeded with `current`, kept updated until release() removes it. */
  bind(current: string): { file: string; release: () => void } {
    const path = join(tmpdir(), `clearcote-rt-${randomUUID()}.tok`);
    this.writeOne(path, current);
    const entry = { path };
    this.files.add(entry);
    return {
      file: path,
      release: () => {
        this.files.delete(entry);
        try { rmSync(path, { force: true }); } catch { /* already gone */ }
      },
    };
  }

  /** Rewrite every live file with the freshly-rotated token. */
  update(token: string): void {
    for (const e of this.files) this.writeOne(e.path, token);
  }

  /** Remove every file (lease shutdown). */
  closeAll(): void {
    for (const e of this.files) { try { rmSync(e.path, { force: true }); } catch { /* ignore */ } }
    this.files.clear();
  }

  private writeOne(path: string, token: string): void {
    try { writeFileSync(path, token, { mode: 0o600 }); } catch { /* the launch still has CLEARCOTE_RUN_TOKEN */ }
  }
}

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
  private _token: string | null = null;
  private readonly tokenFiles = new TokenFileSet();
  get token(): string | null { return this._token; }
  set token(v: string | null) { this._token = v; if (v) this.tokenFiles.update(v); }
  /** Bind a per-launch token file that follows this lease's rotating token. */
  bindLaunch(): { file: string; release: () => void } { return this.tokenFiles.bind(this._token ?? ""); }
  exp = 0;
  leaseId: string | null = null;
  /** Learned from the first checkout: "browser" means every launch holds its own lease. */
  scope: "machine" | "browser" = "machine";
  /** A per-browser checkout made by ensure(), waiting for the one launch that asked for it. */
  private pendingBrowser: { co: CheckoutResponse; launchId: string } | null = null;
  private readonly browsers = new Set<BrowserLease>();
  private hbSec = 270;
  private owner = false;
  private timer: NodeJS.Timeout | null = null;
  private refs = 0;
  private ensuring: Promise<void> | null = null;
  private refreshing: Promise<boolean> | null = null;
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
    private readonly proxy: ProxySpec | null = null,
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
    if (this.scope === "browser") return; // the lease belongs to one launch; see acquire()
    this.owner = true;
    this.startHeartbeat();
  }

  /** POST a checkout for one launch. Throws the backend's verdict; network errors propagate. */
  async checkoutFor(launchId: string): Promise<CheckoutResponse> {
    const res = await postJson(`${this.base}/api/v1/lease/checkout`, this.key, {
      instance_id: this.instanceId,
      // Per-browser plans count each launch_id as its own slot; machine plans ignore it.
      launch_id: launchId,
      os: osTag(),
      sdk_version: this.sdkVersion,
      engine_version: await this.engineVer(),
    }, this.proxy);
    if (!res.ok) {
      const body = (await res.json().catch(() => ({}))) as { error?: string; code?: string };
      throwForStatus(res.status, body);
    }
    return (await res.json()) as CheckoutResponse;
  }

  /** Heartbeat / re-checkout / check-in for a BrowserLease, over this lease's route (direct or proxy). */
  post(path: string, body: unknown): Promise<Response | SimpleResponse> {
    return postJson(`${this.base}${path}`, this.key, body, this.proxy);
  }

  private async checkout(): Promise<void> {
    try {
      const launchId = newLaunchId();
      const co = await this.checkoutFor(launchId);
      if (co.lease_scope === "browser") {
        // Per-browser plan: this checkout is the calling launch's own slot. Never shared, never cached.
        this.scope = "browser";
        this.pendingBrowser = { co, launchId };
        return;
      }
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
        }, this.proxy);
        if (res.status === 409) {
          const co = await postJson(`${this.base}/api/v1/lease/checkout`, this.key, {
            instance_id: this.instanceId,
            os: osTag(),
            sdk_version: this.sdkVersion,
            engine_version: await this.engineVer(),
          }, this.proxy);
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

  /**
   * Mint a fresh token when the one held is older than the newest the PRO engine has accepted here.
   *
   * The engine refuses such a token outright ("older than the last one accepted"), and a token reused
   * from the cache or from memory for its 24 h life can be: the mark is per OS user, so anything else
   * launching with a newer token moves it (another process, the hosted-browser gateway, another key).
   * Resolves true when the token was replaced. `force` skips the comparison: the engine has just refused
   * this token (a race this check could not see coming). Concurrent callers share one refresh.
   */
  refreshIfBehindEngine(force = false): Promise<boolean> {
    if (this.perBrowser() || !this.token) return Promise.resolve(false);
    if (!force) {
      const iat = tokenIat(this.token);
      const hwm = engineHwm();
      if (!iat || !hwm || iat >= hwm) return Promise.resolve(false);
    }
    if (!this.refreshing) this.refreshing = this.freshToken().finally(() => { this.refreshing = null; });
    return this.refreshing;
  }

  /**
   * Replace the token with a freshly minted one. First by heartbeating the lease this process knows (its
   * own, or the one another process's owner keeps alive): same lease, nothing revoked, so two processes
   * never knock each other's lease out. With no lease id, or once the lease is gone, check out again like
   * a cold ensure(). A definitive refusal (limit, revoked) throws.
   */
  private async freshToken(): Promise<boolean> {
    if (this.leaseId) {
      try {
        const res = await postJson(`${this.base}/api/v1/lease/heartbeat`, this.key, {
          lease_id: this.leaseId,
          nonce: randomUUID(),
        }, this.proxy);
        if (res.ok) {
          const d = (await res.json()) as { token?: string; exp?: number };
          if (typeof d.token === "string") {
            this.token = d.token;
            if (typeof d.exp === "number") this.exp = d.exp;
            writeCache(this.key, d.token, this.exp, this.leaseId);
            return true;
          }
        }
      } catch {
        /* unreachable: the checkout below says so properly */
      }
    }
    await this.checkout();
    if (this.perBrowser()) return false;
    if (!this.owner) {
      // The slot is this process's now: keep it alive, check it in at exit.
      this.owner = true;
      this.startHeartbeat();
    }
    return true;
  }

  async acquire(): Promise<LeaseSession> {
    if (this.perBrowser()) return this.acquireBrowser();
    await this.ensure();
    if (this.perBrowser()) {
      // ensure() discovered the per-browser scope. Exactly one waiting launch takes the checkout it
      // made; every other launch (including ones that were waiting on the same ensure) checks out its own.
      const pending = this.pendingBrowser;
      this.pendingBrowser = null;
      return pending ? this.startBrowser(pending.co, pending.launchId) : this.acquireBrowser();
    }
    await this.refreshIfBehindEngine();
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
      bindLaunch: () => self.bindLaunch(),
      refreshToken: () => self.refreshIfBehindEngine(true),
    } as LeaseSession;
  }

  /** Read through a method: ensure() can change the scope while acquire() awaits it. */
  private perBrowser(): boolean {
    return this.scope === "browser";
  }

  release(): void {
    if (this.refs > 0) this.refs--;
  }

  /** A new launch on a per-browser plan: its own checkout. A refusal (one browser already running) throws. */
  private async acquireBrowser(): Promise<LeaseSession> {
    const launchId = newLaunchId();
    let co: CheckoutResponse;
    try {
      co = await this.checkoutFor(launchId);
    } catch (e) {
      if (e instanceof LicenseError) throw e;
      // No offline grace here: without the backend there is no slot for this browser.
      throw new LicenseError(`Could not reach the license server to start this browser: ${String(e)}`);
    }
    return this.startBrowser(co, launchId);
  }

  private startBrowser(co: CheckoutResponse, launchId: string): LeaseSession {
    const lease = new BrowserLease(this, co, launchId, () => this.browsers.delete(lease));
    this.browsers.add(lease);
    return lease.session();
  }

  async shutdown(): Promise<void> {
    this.tokenFiles.closeAll();
    // Per-browser leases still open at exit (a browser nobody closed): release their slots too.
    await Promise.all([...this.browsers].map((b) => b.stop()));
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    if (this.owner && this.leaseId) {
      try {
        await postJson(`${this.base}/api/v1/lease/checkin`, this.key, { lease_id: this.leaseId }, this.proxy);
      } catch {
        /* best-effort; the lease TTL reclaims it anyway */
      }
    }
  }
}

/**
 * One browser's own lease on a per-browser plan (the free tier: "1 browser at a time").
 *
 * Checked out for exactly one launch, heartbeated while that browser runs, checked in when it closes.
 * Its token is never written to the shared cache and never handed to another launch.
 */
class BrowserLease {
  private _token!: string;
  private readonly tokenFiles = new TokenFileSet();
  get token(): string { return this._token; }
  set token(v: string) { this._token = v; this.tokenFiles.update(v); }
  /** Bind a per-launch token file that follows this browser's rotating token. */
  bindLaunch(): { file: string; release: () => void } { return this.tokenFiles.bind(this._token); }
  leaseId: string;
  private timer: NodeJS.Timeout | null = null;
  private stopped = false;

  constructor(
    private readonly owner: MachineLease,
    co: CheckoutResponse,
    private readonly launchId: string,
    private readonly onStop: () => void,
  ) {
    this.token = co.token;
    this.leaseId = co.lease_id;
    const hbMs = Math.max(5, co.heartbeat_interval_sec || 120) * 1000;
    this.timer = setInterval(() => void this.beat(), hbMs);
    (this.timer as unknown as { unref?: () => void }).unref?.();
  }

  private async beat(): Promise<void> {
    if (this.stopped) return;
    try {
      const res = await this.owner.post("/api/v1/lease/heartbeat", { lease_id: this.leaseId, nonce: randomUUID() });
      if (res.status === 409) {
        // Reclaimed (e.g. missed beats): re-take the slot as the SAME launch, which the backend treats
        // as this browser's own lease. If another browser took the slot meanwhile, this is refused.
        const co = await this.owner.checkoutFor(this.launchId);
        this.leaseId = co.lease_id;
        this.token = co.token;
        return;
      }
      if (res.ok) {
        const d = (await res.json()) as { token?: string };
        if (typeof d.token === "string") this.token = d.token;
      }
    } catch {
      /* transient; the next beat retries, and the lease TTL is the backstop */
    }
  }

  /**
   * The engine refused this token as older than one it accepted: mint a fresh one by heartbeating this
   * browser's lease or, if the lease is gone, by re-taking the slot as the SAME launch.
   */
  async refreshToken(): Promise<boolean> {
    const res = await this.owner.post("/api/v1/lease/heartbeat", { lease_id: this.leaseId, nonce: randomUUID() });
    if (res.ok) {
      const d = (await res.json()) as { token?: string };
      if (typeof d.token === "string") {
        this.token = d.token;
        return true;
      }
      return false;
    }
    if (res.status !== 409) return false;
    const co = await this.owner.checkoutFor(this.launchId); // a refusal (another browser holds it) throws
    this.leaseId = co.lease_id;
    this.token = co.token;
    return true;
  }

  async stop(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.tokenFiles.closeAll();
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = null;
    }
    this.onStop();
    try {
      await this.owner.post("/api/v1/lease/checkin", { lease_id: this.leaseId });
    } catch {
      /* best-effort; the lease TTL reclaims it */
    }
  }

  session(): LeaseSession {
    const self = this;
    return {
      get token() {
        return self.token;
      },
      get leaseId() {
        return self.leaseId;
      },
      stop: () => self.stop(),
      bindLaunch: () => self.bindLaunch(),
      refreshToken: () => self.refreshToken(),
    } as LeaseSession;
  }
}

const machineLeases = new Map<string, MachineLease>();
let exitHooked = false;

/**
 * Acquire a concurrency lease for one launch. Returns `null` in free mode (no key).
 *
 * Machine plans (every paid plan): a per-MACHINE lease shared across every launch in this process
 * (checks out ~once per token-TTL, not once per launch); `stop()` only drops a reference.
 * Per-browser plans (the GitHub free tier, told by the backend's `lease_scope: "browser"`): every
 * launch checks out its own slot, and `stop()` releases it, so a second browser is refused while one runs.
 * Throws {@link ConcurrencyLimitError} / {@link LicenseRevokedError} /
 * {@link LicenseError} only on a cold checkout the backend definitively refuses;
 * falls back to a cached, still-valid token on a transient network failure.
 */
export async function acquireLease(
  opts: LicenseOptions & {
    sdkVersion?: string;
    quiet?: boolean;
    engineVersion?: string | (() => string | Promise<string>);
    /** The launch's proxy; used for the licence calls only when licenseThroughProxy is on. */
    proxy?: string | { server?: string; username?: string; password?: string } | null;
  } = {},
): Promise<LeaseSession | null> {
  const licenseKey = resolveLicenseKey(opts.licenseKey);
  if (!licenseKey) return null; // free mode — inert

  const base = apiBase(opts);
  const viaProxy = licenseThroughProxyRequested(opts.licenseThroughProxy) ? toProxySpec(opts.proxy ?? null) : null;
  if (licenseThroughProxyRequested(opts.licenseThroughProxy) && !viaProxy && !opts.quiet) {
    process.stderr.write("[clearcote] [license] licenseThroughProxy is on but this launch has no proxy; licence calls go direct.\n");
  }
  // One lease per (key, route): a direct lease and a proxied lease are different network paths, so
  // they must not share the in-process heartbeat owner.
  // The username is part of the route: sticky-session gateways encode the session in it.
  const mapKey = viaProxy ? `${licenseKey}|${viaProxy.server}|${viaProxy.username ?? ""}` : licenseKey;
  let ml = machineLeases.get(mapKey);
  if (!ml) {
    ml = new MachineLease(licenseKey, base, resolveInstanceId(), opts.sdkVersion, !!opts.quiet,
      opts.engineVersion, viaProxy);
    machineLeases.set(mapKey, ml);
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
  tokenFile?: string,
): Record<string, string> {
  const src = baseEnv ?? process.env;
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) if (v !== undefined) out[k] = v;
  out[RUN_TOKEN_ENV] = token;
  // A supporting engine (r23+) re-reads this file so revoke/check-in/over-limit stops a running free
  // browser. Older engines ignore it. Additive: the launch-time token above is unchanged.
  if (tokenFile) out[`${RUN_TOKEN_ENV}_FILE`] = tokenFile;
  return out;
}

// ── seats, key storage (used by the `clearcote` CLI) ─────────────────────────

/** Concurrency seats on a licence, as reported by the backend. */
export interface SessionSeats {
  /** "ok" with counts; otherwise why they are unavailable. */
  state: "ok" | "no-key" | "invalid" | "unavailable";
  used?: number;
  /** null = unlimited. */
  limit?: number | null;
  plan?: string;
  reason?: string;
}

/**
 * Seats in use on a licence right now (live leases), without checking one out. Never cached and
 * never throws: an unreachable backend or an older backend without the endpoint reports
 * `state: "unavailable"` with the reason, rather than a guessed number.
 */
export async function getSessionSeats(opts: LicenseOptions & { proxy?: string | { server?: string; username?: string; password?: string } | null } = {}): Promise<SessionSeats> {
  const key = resolveLicenseKey(opts.licenseKey);
  if (!key) return { state: "no-key" };
  const via = licenseThroughProxyRequested(opts.licenseThroughProxy) ? toProxySpec(opts.proxy ?? null) : null;
  try {
    const res = await proxiedRequest(`${apiBase(opts)}/api/v1/lease/seats`, {
      method: "GET",
      headers: { authorization: `Bearer ${key}`, "User-Agent": LICENSE_USER_AGENT },
      proxy: via,
      timeoutMs: 15_000,
    });
    const body = (await res.json().catch(() => ({}))) as { used?: number; limit?: number | null; plan?: string; error?: string; code?: string };
    if (res.ok && typeof body.used === "number") {
      return { state: "ok", used: body.used, limit: body.limit ?? null, plan: body.plan };
    }
    if (res.status === 401 || res.status === 403) return { state: "invalid", reason: body.error || `HTTP ${res.status}` };
    if (res.status === 404) return { state: "unavailable", reason: "this licence server does not report seats yet" };
    return { state: "unavailable", reason: body.error || `HTTP ${res.status}` };
  } catch (e) {
    return { state: "unavailable", reason: `licence server unreachable (${(e as Error).message})` };
  }
}

/** Where `clearcote login` stores the key: ~/.clearcote/license.key. */
export function licenseKeyPath(): string {
  return join(homedir(), ".clearcote", "license.key");
}

/** Save a licence key for every later launch (owner-only permissions where the OS supports it). */
export function saveLicenseKey(key: string): string {
  const k = key.trim();
  if (!k) throw new LicenseError("Empty licence key.", "LICENSE_EMPTY");
  const p = licenseKeyPath();
  mkdirSync(join(homedir(), ".clearcote"), { recursive: true });
  writeFileSync(p, k + "\n", { mode: 0o600 });
  try { chmodSync(p, 0o600); } catch { /* not supported on this filesystem */ }
  return p;
}

/** Remove the saved key (and what device login recorded about it). Returns true when the key file
 * was removed. */
export function removeLicenseKey(): boolean {
  removeLicenseMeta();
  const p = licenseKeyPath();
  if (!existsSync(p)) return false;
  rmSync(p, { force: true });
  return true;
}

// What `clearcote login --device` learned about the key it saved. No licence endpoint reports a key's
// expiry (GET /api/v1/lease/seats answers used/limit/plan only), so the device-token answer is the one
// source; `clearcote info` reports it as such. Bound to the key by its SHA-256, so a key saved later
// (pasted, or set in CLEARCOTE_LICENSE_KEY) is never described by another key's record.
const EXPIRY_NOTE = "as the site reported it at `clearcote login --device`; no licence endpoint reports a key's expiry";
const EXPIRY_UNKNOWN_NOTE = "not known: only `clearcote login --device` records it, and no licence endpoint reports a key's expiry";

/** What `clearcote info` reports about the expiry of the key a launch would use. */
export type LicenseExpiry =
  | { expiresAt: string | null; source: "device-login"; recordedAt?: string; note: string }
  | { source: "unknown"; note: string };

export function licenseMetaPath(): string {
  return join(homedir(), ".clearcote", "license.meta.json");
}

const keyDigest = (key: string): string => createHash("sha256").update(key.trim()).digest("hex");

/** Record the plan and expiry the device login returned for `key` (never the key itself). */
export function saveLicenseMeta(key: string, plan?: string | null, expiresAt?: string | null): string {
  const p = licenseMetaPath();
  mkdirSync(join(homedir(), ".clearcote"), { recursive: true });
  const record = {
    key_sha256: keyDigest(key), plan: plan ?? null, expires_at: expiresAt ?? null, source: "device-login",
    recorded_at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
  writeFileSync(p, JSON.stringify(record) + "\n", { mode: 0o600 });
  try { chmodSync(p, 0o600); } catch { /* not supported on this filesystem */ }
  return p;
}

/** Remove the device-login record; with `unlessKey`, only when it describes a different key. */
export function removeLicenseMeta(unlessKey?: string): boolean {
  const p = licenseMetaPath();
  if (!existsSync(p)) return false;
  if (unlessKey !== undefined) {
    try {
      if (JSON.parse(readFileSync(p, "utf8"))?.key_sha256 === keyDigest(unlessKey)) return false;
    } catch { /* unreadable: replace it */ }
  }
  try { rmSync(p, { force: true }); } catch { return false; }
  return true;
}

/** `{ expiresAt, source: "device-login", ... }` when device login recorded it for this very key
 * (`expiresAt` null = no expiry); `{ source: "unknown" }` for any other key; undefined without one. */
export function licenseExpiry(explicit?: string): LicenseExpiry | undefined {
  const key = resolveLicenseKey(explicit);
  if (!key) return undefined;
  try {
    const rec = JSON.parse(readFileSync(licenseMetaPath(), "utf8"));
    if (rec && typeof rec === "object" && rec.key_sha256 === keyDigest(key)) {
      return { expiresAt: rec.expires_at ?? null, source: "device-login", recordedAt: rec.recorded_at, note: EXPIRY_NOTE };
    }
  } catch { /* no record */ }
  return { source: "unknown", note: EXPIRY_UNKNOWN_NOTE };
}

/** Where the key a launch would use comes from, without revealing it. */
export function licenseKeySource(explicit?: string): { source: "option" | "env" | "file" | "none"; masked?: string } {
  const mask = (k: string) => (k.length > 12 ? `${k.slice(0, 7)}…${k.slice(-4)}` : "…");
  if (explicit && explicit.trim()) return { source: "option", masked: mask(explicit.trim()) };
  const env = process.env.CLEARCOTE_LICENSE_KEY;
  if (env && env.trim()) return { source: "env", masked: mask(env.trim()) };
  try {
    const p = licenseKeyPath();
    if (existsSync(p)) {
      const v = readFileSync(p, "utf8").trim();
      if (v) return { source: "file", masked: mask(v) };
    }
  } catch { /* ignore */ }
  return { source: "none" };
}
