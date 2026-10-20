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

import { offlineLease } from "./offline.js";
import { existsSync, readFileSync, mkdirSync, writeFileSync, renameSync, rmSync, chmodSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { createHash, randomBytes, randomUUID } from "node:crypto";
import { setTimeout as sleep } from "node:timers/promises";
import { proxiedRequest, toProxySpec, type ProxySpec, type SimpleResponse } from "./net.js";

/** Default backend. Override with `licenseApiBase` or CLEARCOTE_LICENSE_API. */
const DEFAULT_API_BASE = "https://www.clearcotelabs.com";
/** The licence lease server: lease checkout, heartbeat, check-in and seats go there first. */
const DEFAULT_LEASE_BASE = "https://lease.clearcotelabs.com";
const RUN_TOKEN_ENV = "CLEARCOTE_RUN_TOKEN";

/**
 * Where the lease calls go when no base is set (`licenseApiBase`, CLEARCOTE_LICENSE_API, CLEARCOTE_LEASE_API),
 * and the timings of the fallback and of the rate-limit retries. Tests point the bases at local servers and
 * shorten the timings; nothing else changes them.
 */
export const leaseDefaults = {
  primary: DEFAULT_LEASE_BASE,
  /** Gets a lease call the primary could not take (see leaseCall()). */
  fallback: DEFAULT_API_BASE,
  /** After the primary failed, lease calls skip it for this long (ms) and go straight to the fallback. */
  breakerMs: 60_000,
  /** Waits (ms) before each retry of a checkout the server answered with RATE_LIMITED. */
  rateLimitRetryMs: [1_000, 2_000, 4_000],
};

/** The 429 code for "this address's request budget is spent", which says nothing about the licence. */
const RATE_LIMITED = "RATE_LIMITED";

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
  /**
   * Backend base URL. Default CLEARCOTE_LICENSE_API env or clearcotelabs.com. When set (or with
   * CLEARCOTE_LICENSE_API / CLEARCOTE_LEASE_API), the lease calls use that base only; by default they go to
   * the licence lease server, falling back to clearcotelabs.com when it cannot be reached.
   */
  licenseApiBase?: string;
  /**
   * Send the licence calls (lease checkout / heartbeat / check-in) through the launch's own proxy
   * instead of directly from this machine. Off by default: direct calls never spend proxy bandwidth.
   * Turn it on when the host's own address must not reach the licence server, or when the host has
   * no direct egress. Also enabled by CLEARCOTE_LICENSE_THROUGH_PROXY=1. No effect without a proxy.
   */
  licenseThroughProxy?: boolean;
  /**
   * Offline mode: path to an Offline Key file (Platform Licence). Also CLEARCOTE_OFFLINE_KEY. No licence
   * calls and no download are made; pass `executablePath` (or CLEARCOTE_BINARY) to the offline bundle's
   * PRO binary. See ./offline.ts.
   */
  offlineKey?: string;
}

/** Whether licence calls should use the launch proxy: explicit option, else the env switch. */
export function licenseThroughProxyRequested(opt: boolean | undefined, env: Record<string, string | undefined> = process.env): boolean {
  if (opt !== undefined) return opt;
  return /^(1|true|yes|on)$/i.test((env.CLEARCOTE_LICENSE_THROUGH_PROXY ?? "").trim());
}

export class LicenseError extends Error {
  code: string;
  /** HTTP status of the answer this error reports; undefined when the licence server never answered. */
  status?: number;
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
/**
 * The licence server never answered: something in front of it (a CDN firewall or bot check, e.g. the
 * "Vercel Security Checkpoint") returned a challenge page instead of a licence verdict. Says nothing about
 * the licence itself, which is NOT revoked or over its limit. Retryable: such challenges are usually
 * temporary and tied to this machine's network; `licenseThroughProxy` sends the calls another way.
 */
export class LicenseServerChallengeError extends LicenseError {
  readonly retryable = true;
  constructor(message: string) {
    super(message, "LICENSE_SERVER_CHALLENGE");
    this.name = "LicenseServerChallengeError";
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

/** Where the lease calls go: one base, or the lease server with a fallback for when it cannot be reached. */
interface LeaseRoute {
  primary: string;
  fallback: string | null;
}

/**
 * The lease call bases (checkout, heartbeat, check-in, seats). A base that is set is the only one used:
 * `licenseApiBase` > CLEARCOTE_LICENSE_API > CLEARCOTE_LEASE_API. Otherwise the licence lease server, with
 * clearcotelabs.com as its fallback. Every other licence route (the PRO download, device login) keeps
 * {@link licenseApiBase}.
 */
function leaseRoute(opts: LicenseOptions, env: Record<string, string | undefined> = process.env): LeaseRoute {
  const set = opts.licenseApiBase || env.CLEARCOTE_LICENSE_API || env.CLEARCOTE_LEASE_API;
  if (set) return { primary: set.replace(/\/$/, ""), fallback: null };
  return { primary: leaseDefaults.primary.replace(/\/$/, ""), fallback: leaseDefaults.fallback.replace(/\/$/, "") };
}

type LeaseAnswer = Response | SimpleResponse;

/** Per process: until when (epoch ms) a route skips its primary, after the primary could not be reached. */
const primarySkippedUntil = new Map<string, number>();

/** Gateway and CDN errors: the request never reached the lease server. */
function gatewayStatus(status: number): boolean {
  return status === 502 || status === 503 || status === 504 || (status >= 520 && status <= 527) || status === 530;
}

/** Whether the primary's answer is not the lease server's: a gateway error, or a challenge page. */
async function notTheServer(res: LeaseAnswer): Promise<boolean> {
  if (gatewayStatus(res.status)) return true;
  if (res.status < 400) return false;
  // Read a copy: a JSON answer is final and goes to the caller unread.
  const copy = typeof (res as Response).clone === "function" ? (res as Response).clone() : res;
  const text = await copy.text().catch(() => "");
  return challengeReason(res.status, headerOf(res, "content-type"), text, headerOf(res, "x-vercel-mitigated")) !== null;
}

/**
 * Send one lease call over `route`. With the default bases, a call the primary cannot take (a network error
 * or timeout, a gateway error, a challenge page) is sent once more to the fallback, and the primary is
 * skipped for the next `leaseDefaults.breakerMs`. Any answer from the lease server itself (a 200 or a JSON
 * error: 401, 403, 409, 426, 429, ...) is final. `via` keeps a proxied route's skip apart from the direct one.
 */
async function leaseCall(route: LeaseRoute, via: ProxySpec | null, send: (base: string) => Promise<LeaseAnswer>): Promise<LeaseAnswer> {
  if (!route.fallback) return send(route.primary);
  const key = `${route.primary}|${via?.server ?? ""}`;
  if ((primarySkippedUntil.get(key) ?? 0) > Date.now()) return send(route.fallback);
  try {
    const res = await send(route.primary);
    if (!(await notTheServer(res))) return res;
    (res as Response).body?.cancel().catch(() => {}); // not read: let its connection go
  } catch {
    /* not reached: the fallback below */
  }
  primarySkippedUntil.set(key, Date.now() + leaseDefaults.breakerMs);
  return send(route.fallback);
}

/** The User-Agent every licence call sends ("clearcote-sdk-node/<version>"). */
export function licenseUserAgent(): string {
  return LICENSE_USER_AGENT;
}

const osTag = (): string =>
  ({ win32: "windows", linux: "linux", darwin: "macos" } as Record<string, string>)[process.platform] ?? "unknown";

// ── shared token cache (cross-process reuse + offline grace) ───────────────
// {token, exp, lease_id, scope}. A second process on the machine reuses a still-valid
// token instead of checking out again, but only from a file marked `scope: "machine"`: this
// SDK writes the mark when a checkout that offered the browser scope got the machine lease.
// An unmarked file (an older SDK's, which never offered it) says nothing about the licence's
// scope, so the first launch checks out to learn it; it still serves as offline grace.
// Older caches without lease_id are honored.
const keyHash = (licenseKey: string): string => createHash("sha256").update(licenseKey).digest("hex").slice(0, 16);
function cachePath(licenseKey: string): string {
  return join(homedir(), ".clearcote", `lease-${keyHash(licenseKey)}.json`);
}
function readCache(licenseKey: string): { token: string; exp: number; lease_id?: string; scope?: unknown } | null {
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

/** `machine`: mark the file as a confirmed machine lease (see the cache comment above). */
function writeCache(licenseKey: string, token: string, exp: number, leaseId?: string, machine = false): void {
  try {
    const dir = join(homedir(), ".clearcote");
    mkdirSync(dir, { recursive: true });
    // token / exp / lease_id stay exactly as older SDKs read them; `scope` is an addition they ignore.
    writeFileSync(cachePath(licenseKey), JSON.stringify({ token, exp, lease_id: leaseId ?? null, ...(machine ? { scope: "machine" } : {}) }));
  } catch {
    /* ignore */
  }
}

// ── grace token (paid per-browser leases) ──────────────────────────────────
// {token, exp}: the newest paid run-token of a per-browser lease, kept so a paid browser can still start
// while the licence server cannot be reached (see MachineLease.uncountedLaunch). A file of its own: older
// SDKs read lease-<hash>.json and would hand a per-browser token to every launch.
function gracePath(licenseKey: string): string {
  return join(homedir(), ".clearcote", `grace-${keyHash(licenseKey)}.json`);
}

function readGrace(licenseKey: string): { token: string; exp: number } | null {
  try {
    const d = JSON.parse(readFileSync(gracePath(licenseKey), "utf8"));
    if (d && typeof d.token === "string" && typeof d.exp === "number" && tokenPlan(d.token) !== PER_BROWSER_PLAN) return d;
  } catch {
    /* ignore */
  }
  return null;
}

/** Keep a paid per-browser token as the grace token, unless the one kept was minted later. Best-effort. */
function writeGrace(licenseKey: string, token: string, exp: number): void {
  if (tokenPlan(token) === PER_BROWSER_PLAN) return; // a free token is never a fallback
  const final = gracePath(licenseKey);
  let kept = 0;
  try {
    const d = JSON.parse(readFileSync(final, "utf8"));
    if (d && typeof d.token === "string") kept = tokenIat(d.token) ?? 0;
  } catch {
    /* none kept, or unreadable: replace it */
  }
  if ((tokenIat(token) ?? 0) < kept) return;
  // Written to a temporary file and renamed into place, so another process never reads half a file.
  const tmp = `${final}.tmp-${randomBytes(4).toString("hex")}`;
  try {
    mkdirSync(join(homedir(), ".clearcote"), { recursive: true });
    writeFileSync(tmp, JSON.stringify({ token, exp }));
    renameSync(tmp, final);
  } catch {
    try { rmSync(tmp, { force: true }); } catch { /* ignore */ }
  }
}

/** Per process and licence key: browsers started on a cached token, not yet reported by a checkout. */
const uncountedLaunches = new Map<string, number>();

/** Notices the licence server sent, printed once per process. */
const noticesShown = new Set<string>();

function showNotice(notice: unknown, quiet: boolean): void {
  if (typeof notice !== "string" || !notice.trim() || quiet || noticesShown.has(notice)) return;
  noticesShown.add(notice);
  process.stderr.write(`[clearcote] [license] ${notice.trim()}\n`);
}

interface CheckoutResponse {
  lease_id: string;
  token: string;
  exp: number;
  lease_ttl_sec: number;
  heartbeat_interval_sec: number;
  /** `used` can be absent (per-browser paid leases); nothing here reads it. */
  concurrency?: { used?: number; limit: number };
  /** "browser" when every browser holds its own lease (the free tier, uncapped paid plans); absent = the machine-shared lease. */
  lease_scope?: "browser";
  /** A message for the user (e.g. about the plan), printed once per process. */
  notice?: string;
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
  // Through the launch proxy when licenseThroughProxy is on; otherwise the plain fetch path. Both give
  // up after 30 s, so a server that never answers counts as unreachable (see leaseCall()).
  if (proxy) return proxiedRequest(url, { ...init, proxy, timeoutMs: 30_000 });
  return fetch(url, { ...init, signal: AbortSignal.timeout(30_000) });
}

function headerOf(res: Response | SimpleResponse, name: string): string {
  const h = res.headers as Headers | Record<string, string> | undefined;
  if (!h) return "";
  if (typeof (h as Headers).get === "function") return (h as Headers).get(name) ?? "";
  return (h as Record<string, string>)[name] ?? "";
}

/**
 * Why an error answer is a firewall/bot challenge rather than the licence server's verdict, or null.
 * The licence server answers every error with JSON. A Vercel challenge sets `x-vercel-mitigated` and serves
 * an HTML page titled "Vercel Security Checkpoint"; any other non-JSON 403/429 comes from something in
 * front of the server too, so it is not a revoke or a concurrency verdict either.
 */
export function challengeReason(status: number, contentType: string, text: string, mitigated = ""): string | null {
  if (status < 400) return null;
  if (mitigated) return `x-vercel-mitigated: ${mitigated}`;
  if (text.includes("Vercel Security Checkpoint")) return "Vercel Security Checkpoint";
  if ((status === 403 || status === 429) && text.trim()) {
    try {
      JSON.parse(text);
      return null;
    } catch {
      return `non-JSON ${contentType.split(";")[0].trim() || "body"}`;
    }
  }
  return null;
}

/** Read an error response once: a challenge page's reason, or the licence server's JSON body. */
async function readError(res: Response | SimpleResponse): Promise<{ challenge: string | null; body: { error?: string; code?: string } }> {
  const text = await res.text().catch(() => "");
  const challenge = challengeReason(res.status, headerOf(res, "content-type"), text, headerOf(res, "x-vercel-mitigated"));
  if (challenge) return { challenge, body: {} };
  try {
    const body = JSON.parse(text || "{}");
    return { challenge: null, body: body && typeof body === "object" ? body : {} };
  } catch {
    return { challenge: null, body: {} };
  }
}

function challengeError(status: number, why: string): LicenseServerChallengeError {
  const err = new LicenseServerChallengeError(
    `The licence server could not be reached: a firewall in front of it answered with a security challenge ` +
      `(HTTP ${status}, ${why}) instead of a licence verdict, so your licence was not checked (it is not revoked). ` +
      `This is usually temporary and tied to this machine's network: retry later, or send the licence calls ` +
      `through your proxy (licenseThroughProxy: true).`,
  );
  err.status = status;
  return err;
}

function throwForStatus(status: number, body: { error?: string; code?: string }): never {
  const msg = body?.error || `License request failed (${status}).`;
  let err: LicenseError;
  if (status === 429 || body?.code === "CONCURRENCY_LIMIT_EXCEEDED") err = new ConcurrencyLimitError(msg);
  else if (status === 403 || body?.code === "LICENSE_REVOKED" || body?.code === "LICENSE_EXPIRED") err = new LicenseRevokedError(msg);
  else err = new LicenseError(msg, body?.code || `HTTP_${status}`);
  err.status = status;
  throw err;
}

/** A checkout still rate-limited after its retries: this address's request budget, not the licence's limit. */
function rateLimitedError(body: { error?: string }): LicenseError {
  const err = new LicenseError(
    `${body.error || "Rate limit exceeded."} The licence server is limiting requests from this network ` +
      `(${RATE_LIMITED}); this is not the licence's concurrency limit. Retry in a minute.`,
    RATE_LIMITED,
  );
  err.status = 429;
  return err;
}

/**
 * A checkout failure that is no verdict on the licence: the server was not reached (a network error, a
 * timeout, a challenge page), could not answer (HTTP 5xx), or rate-limited this address. A refusal (401,
 * 403, 409, 426, a concurrency limit) is a verdict.
 */
function noVerdict(e: unknown): boolean {
  if (!(e instanceof LicenseError)) return true;
  return e instanceof LicenseServerChallengeError || e.code === RATE_LIMITED || (e.status ?? 0) >= 500;
}

/** A few words on why a checkout got no verdict, for the one line an uncounted launch prints. */
function failureReason(e: unknown): string {
  if (e instanceof LicenseServerChallengeError) return `security challenge, HTTP ${e.status ?? "?"}`;
  if (e instanceof LicenseError) return e.code === RATE_LIMITED ? "rate limited" : `HTTP ${e.status ?? e.code}`;
  const cause = (e as { cause?: { code?: unknown } } | null)?.cause?.code;
  const msg = e instanceof Error ? e.message : String(e);
  return typeof cause === "string" ? `${msg}: ${cause}` : msg;
}

/** The handle of a browser started on a cached token: no lease, so nothing to beat or check in. */
function uncountedSession(token: string): LeaseSession {
  const files = new TokenFileSet();
  return {
    token,
    leaseId: "uncounted",
    stop: async () => files.closeAll(),
    bindLaunch: () => files.bind(token),
    refreshToken: async () => false,
  };
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
  /**
   * The machine lease held was confirmed as one: by a checkout that offered the browser scope, or by a
   * cache file marked so. Only then are the cache writes marked (a lease taken from an older SDK's file
   * during an outage, and its re-checkouts without a launch_id, say nothing about the licence's scope).
   */
  private machineConfirmed = false;
  /**
   * What ensure() learned for the one launch that asked for it: a per-browser checkout, or a checkout
   * that got no verdict while a paid grace token was at hand (that launch starts on it, uncounted).
   */
  private pendingBrowser: { co: CheckoutResponse; launchId: string } | { error: unknown } | null = null;
  /** The plan of the last per-browser lease this process got ("free" never falls back to a cached token). */
  private browserPlan: string | undefined;
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
    private readonly route: LeaseRoute,
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
    if (cached && cached.scope === "machine" && cached.exp > now + SKEW_SEC) {
      // cross-process reuse: another process's owner keeps the slot alive. Only from a file this SDK
      // marked as a machine lease: an older SDK's file is checked out past, to learn the licence's scope.
      this.token = cached.token;
      this.exp = cached.exp;
      this.leaseId = cached.lease_id ?? null;
      this.owner = false;
      this.machineConfirmed = true;
      return; // NO checkout, NO heartbeat
    }
    await this.checkout();
    if (this.scope === "browser") return; // the lease belongs to one launch; see acquire()
    this.owner = true;
    this.startHeartbeat();
  }

  /**
   * POST a checkout: for one launch (`launchId`), or without one for the machine heartbeat's re-checkout.
   * A RATE_LIMITED answer is retried after `leaseDefaults.rateLimitRetryMs`. Throws the backend's verdict;
   * network errors propagate.
   */
  async checkoutFor(launchId?: string): Promise<CheckoutResponse> {
    const body: Record<string, unknown> = {
      instance_id: this.instanceId,
      // Per-browser plans count each launch_id as its own slot; machine plans ignore it.
      ...(launchId ? { launch_id: launchId } : {}),
      os: osTag(),
      sdk_version: this.sdkVersion,
      engine_version: await this.engineVer(),
      // This SDK runs every browser on its own lease when the backend says so (lease_scope "browser").
      lease_scope_supported: ["browser"],
    };
    const waits = leaseDefaults.rateLimitRetryMs;
    for (let attempt = 0; ; attempt++) {
      // Browsers started on a cached token while the server was unreachable, reported by the next checkout.
      const uncounted = uncountedLaunches.get(this.key) ?? 0;
      const res = await this.post("/api/v1/lease/checkout", uncounted ? { ...body, uncounted_launches: uncounted } : body);
      if (res.ok) {
        const co = (await res.json()) as CheckoutResponse;
        if (uncounted) uncountedLaunches.set(this.key, Math.max(0, (uncountedLaunches.get(this.key) ?? 0) - uncounted));
        showNotice(co.notice, this.quiet);
        return co;
      }
      const { challenge, body: err } = await readError(res);
      if (challenge) throw challengeError(res.status, challenge);
      if (res.status === 429 && err.code === RATE_LIMITED) {
        // This address's request budget is spent: wait and ask again. Never a concurrency verdict.
        if (attempt < waits.length) {
          await sleep(waits[attempt]);
          continue;
        }
        throw rateLimitedError(err);
      }
      throwForStatus(res.status, err);
    }
  }

  /** One lease call (checkout / heartbeat / check-in) over this lease's route (direct or proxy). */
  post(path: string, body: unknown): Promise<Response | SimpleResponse> {
    return leaseCall(this.route, this.proxy, (base) => postJson(`${base}${path}`, this.key, body, this.proxy));
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
      // Offered the browser scope (with a launch_id) and got the machine lease: a confirmed machine lease.
      this.machineConfirmed = true;
      writeCache(this.key, co.token, co.exp, co.lease_id, true);
    } catch (e) {
      // A paid grace token means this licence runs every browser on its own lease: without a verdict, the
      // launch starts on it like any per-browser launch would (see uncountedLaunch()).
      if (noVerdict(e) && this.usableGrace()) {
        this.scope = "browser";
        this.pendingBrowser = { error: e };
        return;
      }
      // A challenge or a rate limit is not a verdict (the server was never reached, or did not decide):
      // offline grace like a network error, and the error itself surfaces when there is no cached token.
      const passing = e instanceof LicenseServerChallengeError || (e instanceof LicenseError && e.code === RATE_LIMITED);
      if (e instanceof LicenseError && !passing) throw e; // definitive verdict must surface
      const cached = readCache(this.key);
      const now = Math.floor(Date.now() / 1000);
      if (cached && cached.exp > now + SKEW_SEC) {
        if (!this.quiet)
          process.stderr.write(`[clearcote] [license] backend unreachable (${String(e)}); using cached run-token.\n`);
        this.token = cached.token;
        this.exp = cached.exp;
        this.leaseId = cached.lease_id ?? null;
        this.machineConfirmed = cached.scope === "machine"; // an older SDK's file is offline grace only
        return;
      }
      if (passing) throw e;
      throw new LicenseError(`Could not reach the license server and no valid cached token: ${String(e)}`);
    }
  }

  /** The grace token, while still valid with SKEW_SEC to spare. */
  private usableGrace(): { token: string; exp: number } | null {
    const grace = readGrace(this.key);
    return grace && grace.exp > Math.floor(Date.now() / 1000) + SKEW_SEC ? grace : null;
  }

  /**
   * A paid browser starts without a lease when its checkout got no verdict (see noVerdict()): on the grace
   * token, else on a still-valid machine cache token. It is not counted, nothing beats or checks it in, and
   * the next checkout reports it (`uncounted_launches`). Null on a free licence or with no usable token.
   */
  private uncountedLaunch(e: unknown): LeaseSession | null {
    if (this.browserPlan === PER_BROWSER_PLAN) return null; // free stays fail-closed
    const now = Math.floor(Date.now() / 1000);
    const cached = readCache(this.key);
    const token = this.usableGrace()?.token ?? (cached && cached.exp > now + SKEW_SEC ? cached.token : null);
    if (!token) return null;
    if (!this.quiet)
      process.stderr.write(
        `[clearcote] [license] licence server unreachable (${failureReason(e)}); this browser starts on a cached run-token and is not counted.\n`,
      );
    uncountedLaunches.set(this.key, (uncountedLaunches.get(this.key) ?? 0) + 1);
    return uncountedSession(token);
  }

  /** The error a per-browser launch raises when its checkout failed and it cannot start uncounted. */
  private browserFailure(e: unknown): LicenseError {
    if (e instanceof LicenseError) return e;
    // No offline grace here: without the backend there is no slot for this browser.
    return new LicenseError(`Could not reach the license server to start this browser: ${String(e)}`);
  }

  private startHeartbeat(): void {
    if (this.timer) return;
    this.timer = setInterval(async () => {
      try {
        const res = await this.post("/api/v1/lease/heartbeat", {
          lease_id: this.leaseId,
          nonce: randomUUID(),
        });
        if (res.status === 409) {
          // Re-checkout without a launch_id: the backend keeps a machine lease a machine lease. A refusal
          // or a network error changes nothing; the next beat tries again.
          const d = await this.checkoutFor();
          this.leaseId = d.lease_id;
          this.token = d.token;
          this.exp = d.exp;
          writeCache(this.key, d.token, d.exp, d.lease_id, this.machineConfirmed);
          return;
        }
        if (res.ok) {
          const d = (await res.json()) as { token: string; exp: number };
          this.token = d.token;
          this.exp = d.exp;
          writeCache(this.key, d.token, d.exp, this.leaseId ?? undefined, this.machineConfirmed);
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
        const res = await this.post("/api/v1/lease/heartbeat", {
          lease_id: this.leaseId,
          nonce: randomUUID(),
        });
        if (res.ok) {
          const d = (await res.json()) as { token?: string; exp?: number };
          if (typeof d.token === "string") {
            this.token = d.token;
            if (typeof d.exp === "number") this.exp = d.exp;
            writeCache(this.key, d.token, this.exp, this.leaseId, this.machineConfirmed);
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
      if (!pending) return this.acquireBrowser();
      if ("error" in pending) {
        const uncounted = this.uncountedLaunch(pending.error);
        if (uncounted) return uncounted;
        throw this.browserFailure(pending.error);
      }
      return this.startBrowser(pending.co, pending.launchId);
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

  /**
   * A new launch on a per-browser plan: its own checkout. A refusal (one browser already running) throws.
   * A paid licence's checkout that gets no verdict starts the browser uncounted when it can.
   */
  private async acquireBrowser(): Promise<LeaseSession> {
    const launchId = newLaunchId();
    let co: CheckoutResponse;
    try {
      co = await this.checkoutFor(launchId);
    } catch (e) {
      const uncounted = noVerdict(e) ? this.uncountedLaunch(e) : null;
      if (uncounted) return uncounted;
      throw this.browserFailure(e);
    }
    return this.startBrowser(co, launchId);
  }

  private startBrowser(co: CheckoutResponse, launchId: string): LeaseSession {
    this.browserPlan = tokenPlan(co.token);
    const lease = new BrowserLease(this, co, launchId, () => this.browsers.delete(lease));
    this.browsers.add(lease);
    return lease.session();
  }

  /** Keep a per-browser lease's newest paid token as the grace token (free tokens are skipped). */
  keepGrace(token: string, exp: unknown): void {
    if (typeof exp === "number") writeGrace(this.key, token, exp);
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
        await this.post("/api/v1/lease/checkin", { lease_id: this.leaseId });
      } catch {
        /* best-effort; the lease TTL reclaims it anyway */
      }
    }
  }
}

/**
 * One browser's own lease on a per-browser plan (the free tier: "1 browser at a time"; uncapped paid plans).
 *
 * Checked out for exactly one launch, heartbeated while that browser runs, checked in when it closes.
 * Its token is never written to the shared cache and never handed to another launch. A paid token is kept
 * as the grace token (see writeGrace()).
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
    this.take(co.token, co.exp);
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
        this.take(co.token, co.exp);
        return;
      }
      if (res.ok) {
        const d = (await res.json()) as { token?: string; exp?: number };
        if (typeof d.token === "string") this.take(d.token, d.exp);
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
      const d = (await res.json()) as { token?: string; exp?: number };
      if (typeof d.token === "string") {
        this.take(d.token, d.exp);
        return true;
      }
      return false;
    }
    if (res.status !== 409) return false;
    const co = await this.owner.checkoutFor(this.launchId); // a refusal (another browser holds it) throws
    this.leaseId = co.lease_id;
    this.take(co.token, co.exp);
    return true;
  }

  /** A new token for this browser (checkout or heartbeat). */
  private take(token: string, exp: unknown): void {
    this.token = token;
    this.owner.keepGrace(token, exp);
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
 * Machine plans (capped paid plans): a per-MACHINE lease shared across every launch in this process
 * (checks out ~once per token-TTL, not once per launch); `stop()` only drops a reference.
 * Per-browser plans (the GitHub free tier and uncapped paid plans, told by the backend's
 * `lease_scope: "browser"`): every launch checks out its own slot, and `stop()` releases it, so a second
 * browser is refused while one runs on a plan that allows one.
 * Throws {@link ConcurrencyLimitError} / {@link LicenseRevokedError} /
 * {@link LicenseError} only on a cold checkout the backend definitively refuses;
 * falls back to a cached, still-valid token on a transient network failure (a paid per-browser launch
 * then starts uncounted; a free one never falls back).
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
  // Offline mode: the key file's token is the run token; nothing is checked out (./offline.ts).
  const off = offlineLease(opts.offlineKey, !!opts.quiet);
  if (off) return off;
  const licenseKey = resolveLicenseKey(opts.licenseKey);
  if (!licenseKey) return null; // free mode — inert

  const route = leaseRoute(opts);
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
    ml = new MachineLease(licenseKey, route, resolveInstanceId(), opts.sdkVersion, !!opts.quiet,
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
    const res = await leaseCall(leaseRoute(opts), via, (base) => proxiedRequest(`${base}/api/v1/lease/seats`, {
      method: "GET",
      headers: { authorization: `Bearer ${key}`, "User-Agent": LICENSE_USER_AGENT },
      proxy: via,
      timeoutMs: 15_000,
    }));
    const text = await res.text().catch(() => "");
    const why = challengeReason(res.status, headerOf(res, "content-type"), text, headerOf(res, "x-vercel-mitigated"));
    if (why) return { state: "unavailable", reason: `licence server behind a security challenge (${why})` };
    let body: { used?: number; limit?: number | null; plan?: string; error?: string; code?: string } = {};
    try {
      const parsed = JSON.parse(text || "{}");
      if (parsed && typeof parsed === "object") body = parsed;
    } catch {
      /* non-JSON: reported by status below */
    }
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
