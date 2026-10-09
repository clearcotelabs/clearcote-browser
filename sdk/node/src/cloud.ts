// Clearcote Cloud: hosted browsers, agent runs, profiles, recordings, events, hand-off and webhooks.
//
// The same SDK runs a browser on this machine or on Clearcote's servers, and one flag picks which:
//
//   import { launch } from "clearcote";
//   const browser = await launch({ cloud: true, country: "us" }); // or CLEARCOTE_CLOUD=1; humanized by default
//   const page = await browser.newPage();                         // the same Playwright Browser
//   await browser.close();                                        // disconnects, ends the session
//
// Everything else the hosted API does is on `Cloud`:
//
//   import { Cloud } from "clearcote";
//   const cloud = new Cloud();   // CLEARCOTE_API_KEY; CLEARCOTE_API_URL points it at another server
//   const run = await cloud.runs.create("Find the price of the cheapest plan", { url: "https://example.com" });
//   console.log(run.status, run.result?.output);
//
// Conventions:
//   * No dependencies: global fetch for HTTP, node:crypto for webhook signatures.
//   * Every method resolves to the parsed JSON body of the endpoint it calls (null for an empty body).
//   * Every non-2xx answer rejects with a CloudError carrying the server's own message and code.
//   * Options use the API's own names (proxySession, timeoutSec), passed through unchanged; the API
//     validates the values. Durations of the SDK's own waits (timeout, poll) are in SECONDS, as in Python.
//
// Mirrors the Python SDK's clearcote/cloud.py: same resources, same defaults, same errors, same CLI output.

import { createHmac, timingSafeEqual } from "node:crypto";
import { createWriteStream, existsSync, readFileSync, renameSync, rmSync, statSync } from "node:fs";
import * as readline from "node:readline";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import type { ReadableStream as WebReadableStream } from "node:stream/web";
import { chromium } from "playwright-core";
import type { Browser, BrowserContext } from "playwright-core";
import { AGENT_KEYS } from "./agent.js";
import { FINGERPRINT_KEYS } from "./fingerprint.js";
import { installHumanize, installHumanizeOnContext } from "./humanize.js";
import { warnStockRuntimeCloud } from "./launchopts.js";
import { toProxySpec } from "./net.js";

export const DEFAULT_API_URL = "https://www.clearcotelabs.com";

/** A run is finished in exactly these states. "waiting_for_human" is NOT one of them: the run is
 * paused on a hand-off and resumes once the person marks it done (or the hand-off times out). */
export const TERMINAL_RUN_STATUSES: readonly string[] = ["succeeded", "failed", "cancelled", "expired"];

const TRUTHY = ["1", "true", "yes"];
// Errors worth another poll rather than giving up on a long wait: no connection, rate limited, or a
// gateway in front of the API restarting. Anything else (401, 404, ...) will not fix itself.
const TRANSIENT = [0, 429, 502, 503, 504];
const MAX_TRANSIENT = 4;

export const NO_API_KEY =
  "no Clearcote API key: pass apiKey or set CLEARCOTE_API_KEY (create a key in the Clearcote dashboard)";

// Plain http:// is accepted only for these hosts (a local dev control plane): anywhere else the API key
// would cross the network unencrypted.
const LOOPBACK_HOSTS = ["127.0.0.1", "::1", "localhost"];

/** `base` (already stripped of trailing slashes) if it is an https:// URL, or an http:// URL of this
 * machine; throws naming the reason otherwise. */
function checkBaseUrl(base: string): string {
  let url: URL | null = null;
  try {
    url = new URL(base);
  } catch { /* reported below */ }
  const scheme = url?.protocol.replace(/:$/, "").toLowerCase() ?? "";
  const host = (url?.hostname ?? "").toLowerCase().replace(/^\[(.*)\]$/, "$1");
  if (!url || !/^https?:\/\//i.test(base) || !["https", "http"].includes(scheme) || !host) {
    throw new Error(`the API URL must start with https:// (got ${JSON.stringify(base)})`);
  }
  if (scheme === "http" && !LOOPBACK_HOSTS.includes(host)) {
    throw new Error(
      `the API URL must use https:// (got http://${host}): over plain http the API key would travel ` +
        "unencrypted. http:// is only accepted for this machine (127.0.0.1, ::1, localhost)",
    );
  }
  return base;
}

const SDK_VERSION: string = (() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return "unknown";
  }
})();
const USER_AGENT = `clearcote-sdk-node/${SDK_VERSION}`;

// ── errors ─────────────────────────────────────────────────────────────────────────────────────────

/**
 * A request the hosted API refused or could not answer. `status` is the HTTP status (0 when the
 * server could not be reached), `code` the API's machine-readable code when it sent one
 * ("PROFILE_IN_USE", "NOT_READY", ...; otherwise null) and `message` the API's own explanation.
 */
export class CloudError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(status: number, code: string | null, message: string) {
    super(message);
    this.name = "CloudError";
    this.status = status;
    this.code = code;
  }
}

/** A wait ran out of time. The run (or hand-off) carries on on the server; `last` holds the last
 * view the SDK read, so its id and status are at hand. */
export class CloudTimeoutError extends Error {
  readonly last: Json | null;
  constructor(message: string, last: Json | null) {
    super(message);
    this.name = "CloudTimeoutError";
    this.last = last;
  }
}

/** A parsed JSON answer of the API (the shapes are documented per endpoint). */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Json = any;

// ── local or cloud ─────────────────────────────────────────────────────────────────────────────────

/**
 * Whether a launch should go to the cloud. `cloud` unset defers to the environment:
 * `CLEARCOTE_CLOUD` set to 1, true or yes means cloud, anything else local. An explicit true/false
 * (or a {@link Cloud} client, which means cloud) always wins over the environment.
 */
export function cloudRequested(cloud?: boolean | Cloud | null): boolean {
  if (cloud === undefined || cloud === null) {
    return TRUTHY.includes((process.env.CLEARCOTE_CLOUD ?? "").trim().toLowerCase());
  }
  // "0"/"false" from a config file must not mean cloud
  if (typeof cloud === "string") return TRUTHY.includes((cloud as string).trim().toLowerCase());
  return Boolean(cloud);
}

/** Option name -> POST /api/v1/browsers field. The Node names ARE the API names (plus the local
 * launch's `acceptLanguage` as an alias of `locale`). Values pass through as given; the API validates
 * them, so the SDK keeps no second copy of the server's rules that could drift. */
export const SESSION_FIELDS: Readonly<Record<string, string>> = {
  fingerprint: "fingerprint",
  identity: "identity",
  platform: "platform",
  brand: "brand",
  timezone: "timezone",
  locale: "locale",
  acceptLanguage: "locale",
  geoip: "geoip",
  headless: "headless",
  lightStealth: "lightStealth",
  proxy: "proxy",
  country: "country",
  state: "state",
  city: "city",
  proxySession: "proxySession",
  timeoutSec: "timeoutSec",
  idleTimeoutSec: "idleTimeoutSec",
  maxGb: "maxGb",
  version: "version",
  profile: "profile",
  url: "url",
  adblock: "adblock",
  solveSliders: "solveSliders",
  solveCheckboxes: "solveCheckboxes",
  humanize: "humanize",
  challengeService: "challengeService",
  keepAlive: "keepAlive",
  record: "record",
  note: "note",
  worker: "worker",
};

/** Run-only fields of POST /api/v1/runs (task, url, schema and secrets are named options). */
export const RUN_FIELDS: Readonly<Record<string, string>> = {
  maxSteps: "maxSteps",
  handoff: "handoff",
  handoffTimeoutSec: "handoffTimeoutSec",
};

// Options a cloud launch() handles on THIS side and never sends: input humanization runs in the SDK
// exactly as for a local browser, timeout/slowMo are Playwright's connectOverCDP options (in its
// units, milliseconds), and apiKey/apiUrl pick the account and server.
const CONNECT_OPTIONS = ["timeout", "slowMo"] as const;
// How long a cloud launch waits for the connect when the caller gives no timeout (Playwright's own
// default is 30 s). The browser starts as the client connects, and a launch with a country can take
// over 30 s; a launch that fails is answered at once, so the longer wait only covers one in progress.
const CLOUD_CONNECT_TIMEOUT_MS = 120_000;
// stockRuntime (engine r32+) is a local and Docker option: a cloud launch only says once that it was not applied.
export const SDK_SIDE_OPTIONS: readonly string[] = ["humanize", "showCursor", "quiet", "apiKey", "apiUrl", "stockRuntime", ...CONNECT_OPTIONS];

/** Everything launch() accepts that only makes sense for a browser on this machine: derived from
 * the local launch surface (persona switches, agent switches, binary, licence, profile directory and
 * Playwright launch options), so an option added there is classified here too. */
export const LOCAL_ONLY_OPTIONS: readonly string[] = [
  ...(FINGERPRINT_KEYS as string[]).filter((k) => !Object.prototype.hasOwnProperty.call(SESSION_FIELDS, k)),
  ...(AGENT_KEYS as string[]),
  "executablePath", "args", "ignoreDefaultArgs", "userDataDir", "ephemeralProfile", "extensions",
  "portableProfile", "encryptionKey", "disablePrivacySandbox", "socks5Udp", "shaderDialect", "fontDirs", "widevine",
  "profileSelect", "cacheDir", "autoUpdate", "releaseChannel", "allowThirdPartyCookies", "transparentProxy",
  "licenseKey", "licenseApiBase", "licenseThroughProxy", "env", "devtools", "downloadsPath", "tracesDir",
  "chromiumSandbox", "channel", "handleSIGINT", "handleSIGTERM", "handleSIGHUP", "firefoxUserPrefs",
  "artifactsDir", "logger",
];

const USER_DATA_DIR_MSG =
  "userDataDir is not available for cloud browsers: a cloud browser keeps its cookies in a cloud " +
  "profile, so pass profile: \"name\" instead (cloud.profiles.sync can fill one from a local profile directory)";

function notAvailable(name: string, run: boolean): Error {
  if (name === "userDataDir") return new Error(USER_DATA_DIR_MSG);
  return new Error(`${name} is not available for cloud ${run ? "runs" : "browsers"}`);
}

/** A cloud proxy: "managed" (the residential pool), your own as a URL with the credentials inline, or
 * { server, username, password }. The API wants the credentials as separate fields, so a URL's
 * user:pass@ is split out (it rejects credentials inside `server`). */
export type CloudProxy = "managed" | string | { server: string; username?: string; password?: string };

const PROXY_SHAPE = 'proxy must be "managed", a proxy URL, or {server, username, password}';

function cloudProxy(value: unknown): unknown {
  if (typeof value === "string" && ["managed", "direct"].includes(value.trim())) return value.trim();
  if (value && typeof value === "object") {
    const extra = Object.keys(value).filter((k) => (value as Record<string, unknown>)[k] != null && !["server", "username", "password"].includes(k)).sort();
    if (extra.length) throw new Error(`proxy.${extra[0]} is not available for cloud browsers`);
  } else if (typeof value !== "string") {
    throw new Error(PROXY_SHAPE);
  }
  let spec;
  try {
    spec = toProxySpec(value as string | { server?: string; username?: string; password?: string });
  } catch {
    throw new Error(PROXY_SHAPE);
  }
  if (!spec) throw new Error(PROXY_SHAPE);
  return spec;
}

/** A cloud profile is a NAMED COOKIE STORE on the server: "name" loads it, and { name, persist: true }
 * also saves it back when the session ends. The local launch's `profile` (a saved persona, or "auto")
 * means something else, so those are refused rather than silently creating a cloud profile. */
export type CloudProfile = string | { name: string; persist?: boolean };

function cloudProfile(value: unknown): unknown {
  if (value && typeof value === "object") {
    if (Object.getPrototypeOf(value) !== Object.prototype) {
      throw new Error("a saved local Profile cannot be used for a cloud browser; pass the name of a cloud profile (see cloud.profiles)");
    }
    const extra = Object.keys(value).filter((k) => !["name", "persist"].includes(k)).sort();
    if (extra.length) throw new Error(`profile.${extra[0]} is not a cloud profile field (use name and persist)`);
  } else if (typeof value !== "string") {
    throw new Error("a saved local Profile cannot be used for a cloud browser; pass the name of a cloud profile (see cloud.profiles)");
  }
  const name = typeof value === "string" ? value : (value as { name?: unknown }).name;
  if (name === "auto") {
    throw new Error('profile "auto" picks a local persona; for a stable cloud device pass identity, and for cookies a cloud profile name');
  }
  return typeof value === "string" ? value : { ...(value as object) };
}

/** The challenge service (off unless asked for): true, or what it may do. Field values are the API's to
 * check; the SDK only refuses a field the API does not have, so a typo fails here and not silently. */
export type CloudChallengeService =
  | boolean
  | {
      /** "auto" (the default): token, clearance, block-page and image; "score" only when listed. */
      categories?: "auto" | ("token" | "score" | "clearance" | "block-page" | "image")[];
      /** Only on these hosts (and their subdomains). */
      sites?: string[];
      /** "own": your solving-service key (stored in the dashboard, or apiKey); "managed": ours, billed per solve. */
      key?: "own" | "managed";
      /** Your solving-service key for this session (sent sealed; never shown again). */
      apiKey?: string;
      /** "report": only report what the pages show; nothing is asked or paid for. */
      mode?: "solve" | "report";
      maxSolves?: number;
      maxSpendEur?: number;
    };

const CHALLENGE_FIELDS = ["categories", "sites", "key", "apiKey", "mode", "maxSolves", "maxSpendEur"];

function cloudChallengeService(value: unknown): unknown {
  if (typeof value === "boolean") return value;
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) {
    throw new Error("challengeService must be true or an object like { categories, key }");
  }
  const extra = Object.keys(value).filter((k) => !CHALLENGE_FIELDS.includes(k)).sort();
  if (extra.length) throw new Error(`challengeService.${extra[0]} is not a field (use ${CHALLENGE_FIELDS.join(", ")})`);
  return { ...(value as object) };
}

/** Map launch()/create() options to the API's JSON body. Throws naming the first option a cloud
 * browser (or run) cannot take. undefined/null values are left out, so an optional setting can be
 * passed through unconditionally. */
export function sessionBody(options: Record<string, unknown>, run = false): Record<string, unknown> {
  const fields: Record<string, string> = run ? { ...SESSION_FIELDS, ...RUN_FIELDS } : { ...SESSION_FIELDS };
  if (options.locale != null && options.acceptLanguage != null) throw new Error("pass locale or acceptLanguage, not both");
  const body: Record<string, unknown> = {};
  for (const [key, raw] of Object.entries(options)) {
    if (!Object.prototype.hasOwnProperty.call(fields, key)) throw notAvailable(key, run);
    if (raw === undefined || raw === null) continue;
    let value: unknown = raw;
    if (key === "proxy") value = cloudProxy(raw);
    else if (key === "profile") value = cloudProfile(raw);
    else if (key === "challengeService") value = cloudChallengeService(raw);
    body[fields[key]] = value;
  }
  return body;
}

/** The POST /api/v1/runs body: the task, where it starts, what to return, and the browser. */
export function runBody(task: string, options: RunOptions = {}): Record<string, unknown> {
  const { url, schema, secrets, wait: _w, timeout: _t, poll: _p, onUpdate: _o, ...browser } = options;
  return {
    task,
    ...(url != null ? { url } : {}),
    ...(schema != null ? { schema } : {}),
    ...(secrets != null ? { secrets } : {}),
    ...sessionBody(browser as Record<string, unknown>, true),
  };
}

// ── option types ───────────────────────────────────────────────────────────────────────────────────

/** The browser options of a cloud session (POST /api/v1/browsers), by their API names. */
export interface CloudSessionOptions {
  fingerprint?: string | number;
  identity?: string;
  platform?: "windows" | "linux" | "macos" | "android";
  brand?: string;
  timezone?: string;
  locale?: string;
  /** Alias of `locale` (the local launch's name for it). */
  acceptLanguage?: string;
  geoip?: boolean;
  headless?: boolean;
  lightStealth?: boolean;
  proxy?: CloudProxy;
  country?: string;
  state?: string;
  city?: string;
  proxySession?: string;
  timeoutSec?: number;
  idleTimeoutSec?: number;
  maxGb?: number;
  version?: string;
  profile?: CloudProfile;
  url?: string;
  adblock?: boolean;
  /** Default true: the server drags slide-to-verify challenges for you. false leaves them to your script. */
  solveSliders?: boolean;
  /** Default true: the server clicks "verify you are human" checkboxes for you. false leaves them to your script. */
  solveCheckboxes?: boolean;
  /** Default true: the server moves your mouse along human paths and holds clicks for a human press time
   *  (any client). false turns it off. A `launch({ cloud })` humanizes in the SDK instead and turns this off for you. */
  humanize?: boolean;
  /** Default off: challenges the free actions cannot clear go to a solving service, with your key or ours. */
  challengeService?: CloudChallengeService;
  keepAlive?: boolean;
  record?: boolean;
  note?: string;
  worker?: string;
}

/** Options of a cloud `launch()`: the session options plus what the SDK handles itself. */
export interface CloudLaunchOptions extends CloudSessionOptions {
  /** true (or a Cloud client) for a cloud browser; unset follows CLEARCOTE_CLOUD. */
  cloud?: boolean | Cloud;
  /** API key; defaults to CLEARCOTE_API_KEY. */
  apiKey?: string;
  /** API base URL; defaults to CLEARCOTE_API_URL, then https://www.clearcotelabs.com. */
  apiUrl?: string;
  /** Default true on a cloud launch: humanize all input in the SDK exactly as for a local browser (typing at
   *  the persona's cadence with key rollover, clicks, the wheel in whole notches; a coordinate mouse.click goes
   *  to the engine's own pointer path), and switch the hosted browser's own mouse humanizer off. false: no
   *  humanizing at all. (Only the server's: create the session with `cloud.browsers.create()`.) */
  humanize?: boolean;
  showCursor?: boolean;
  quiet?: boolean;
  /** Playwright connectOverCDP options (milliseconds). `timeout` defaults to 120 000 here (the browser
   * starts as you connect); 0 means no limit. */
  timeout?: number;
  slowMo?: number;
}

/** A run's own options plus the browser options of its session. */
export interface RunOptions extends CloudSessionOptions {
  url?: string;
  /** JSON Schema (draft-07 subset) of the output; top-level type "object" or "array". */
  schema?: Json;
  /** Run-scoped secrets, referred to in the task as {{name}}; the model never sees the values. */
  secrets?: Record<string, string | { value: string; domains?: string[] } | { totp: string; domains?: string[]; digits?: 6 | 8; period?: 30 | 60 }>;
  maxSteps?: number;
  /** On needs_input / blocked, pause and hand the browser to a person. */
  handoff?: boolean;
  handoffTimeoutSec?: number;
  /** Wait for the run to finish (default true). */
  wait?: boolean;
  /** Seconds to wait before rejecting with CloudTimeoutError (default: no limit). */
  timeout?: number;
  /** Seconds between polls (default 1.5). */
  poll?: number;
  /** Called whenever the status or the hand-off changes. Without it, a run that pauses for a person
   * is reported on stderr with its live link. */
  onUpdate?: (run: Json) => unknown;
}

// ── HTTP ───────────────────────────────────────────────────────────────────────────────────────────

function apiError(status: number, text: string, reason = ""): CloudError {
  let data: Json = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch { /* not JSON */ }
  if (data && typeof data === "object" && data.error && typeof data.error === "object") {
    data = { ...data.error, code: data.error.code ?? data.code }; // { error: { message, code } }
  }
  if (data && typeof data === "object" && (data.error || data.message)) {
    return new CloudError(status, typeof data.code === "string" ? data.code : null, String(data.error || data.message));
  }
  const t = (text || "").trim();
  return new CloudError(status, null, t ? t.slice(0, 300) : `HTTP ${status} ${reason}`.trim());
}

function parseJson(status: number, text: string): Json {
  if (!text || !text.trim()) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new CloudError(status, null, "the API answered with something that is not JSON");
  }
}

/** One URL path segment (ids and profile names are user input). */
const q = (segment: string) => encodeURIComponent(String(segment));

function compact(o: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined && v !== null));
}

function causeOf(e: unknown): string {
  const c = (e as { cause?: { message?: string; code?: string } })?.cause;
  return c?.message || c?.code || (e as Error)?.message || String(e);
}

class Http {
  readonly #key: string; // a true private field: never printed by console.log / util.inspect

  constructor(key: string, readonly baseUrl: string, readonly timeoutMs: number) {
    this.#key = key;
  }

  url(path: string, query?: Record<string, unknown>): string {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query ?? {})) {
      if (v === undefined || v === null) continue;
      qs.append(k, v === true ? "1" : v === false ? "0" : String(v));
    }
    const s = qs.toString();
    return this.baseUrl + path + (s ? `?${s}` : "");
  }

  /** { status, location, text }. 3xx comes back as is (never followed: the recording endpoint
   * redirects to presigned storage, which must not receive the API key); 4xx/5xx reject. */
  async raw(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<{ status: number; location: string | null; text: string }> {
    const headers: Record<string, string> = { authorization: `Bearer ${this.#key}`, accept: "application/json", "User-Agent": USER_AGENT };
    if (body !== undefined) headers["content-type"] = "application/json";
    let res: Response;
    try {
      res = await fetch(this.url(path, query), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: "manual",
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (e) {
      throw new CloudError(0, "NETWORK", `could not reach ${this.baseUrl}: ${causeOf(e)}`);
    }
    let text: string;
    try {
      text = await res.text();
    } catch (e) {
      // the connection was cut (or the timeout hit) while the answer was being read
      throw new CloudError(0, "NETWORK", `could not reach ${this.baseUrl}: ${causeOf(e)}`);
    }
    if (res.status >= 300 && res.status < 400) return { status: res.status, location: res.headers.get("location"), text };
    if (!res.ok) throw apiError(res.status, text, res.statusText);
    return { status: res.status, location: null, text };
  }

  async call(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<Json> {
    const r = await this.raw(method, path, body, query);
    if (r.status >= 300 && r.status < 400) throw new CloudError(r.status, null, `unexpected redirect to ${r.location}`);
    return parseJson(r.status, r.text);
  }
}

// ── polling ────────────────────────────────────────────────────────────────────────────────────────

/** Default report when a run pauses for a person: without it a waiting run just looks stuck. */
export function announceHandoff(view: Json): void {
  const h = view?.handoff ?? {};
  process.stderr.write(
    `[clearcote] run ${view?.id} is waiting for a human${h.reason ? ` (${h.reason})` : ""}: ` +
      `${h.liveUrl || "open it in the Clearcote dashboard"}\n`,
  );
}

const sleep = (s: number) => new Promise<void>((r) => setTimeout(r, Math.max(0, s * 1000)));

/** The decisions a wait makes on every poll (shared shape with the Python SDK): has it finished,
 * should the caller hear about it, is an error worth another try, how long to sleep. */
class Watch {
  private readonly deadline: number | null;
  private lastKey = "\u0000";
  failures = 0;
  last: Json = null;
  constructor(
    private readonly what: string,
    private readonly timeout: number | undefined,
    private readonly done: (v: Json) => boolean,
    private readonly key: (v: Json) => string,
  ) {
    this.deadline = timeout == null ? null : Date.now() + timeout * 1000;
  }

  seen(view: Json): { done: boolean; changed: boolean } {
    this.failures = 0;
    this.last = view;
    const k = this.key(view);
    const changed = k !== this.lastKey;
    this.lastKey = k;
    return { done: this.done(view), changed };
  }

  retry(err: unknown): boolean {
    if (err instanceof CloudError && TRANSIENT.includes(err.status) && this.failures < MAX_TRANSIENT) {
      this.failures += 1;
      return true;
    }
    return false;
  }

  pause(poll: number): number {
    if (this.deadline === null) return poll;
    const left = (this.deadline - Date.now()) / 1000;
    if (left <= 0) {
      const status = this.last?.status;
      throw new CloudTimeoutError(`${this.what} is still ${status || "not finished"} after ${this.timeout}s; it carries on on the server`, this.last);
    }
    return Math.min(poll, left);
  }
}

const runWatch = (id: string, timeout?: number) =>
  new Watch(`run ${id}`, timeout, (v) => TERMINAL_RUN_STATUSES.includes(v?.status), (v) =>
    JSON.stringify([v?.status ?? null, v?.handoff?.state ?? null, v?.handoff?.since ?? null]));

/** A session with no hand-off at all reads as "not waiting", so waitHandoff would resolve at once as if
 * a person had finished. On the first read that is a mistake (no handoff() was requested): say so
 * instead. A done or timed-out hand-off still resolves. */
function requireHandoff(id: string, view: Json, first: boolean): void {
  if (first && !view?.handoff) {
    throw new CloudError(200, "NO_HANDOFF", `no hand-off was requested for session ${id} (request one with browsers.handoff first)`);
  }
}

const handoffWatch = (id: string, timeout?: number) =>
  new Watch(`the hand-off of ${id}`, timeout, (v) => v?.handoff?.state !== "waiting", (v) => String(v?.handoff?.state ?? null));

// ── resources ──────────────────────────────────────────────────────────────────────────────────────

/** Hosted browser sessions: /api/v1/browsers. */
export class CloudBrowsers {
  constructor(private readonly http: Http) {}

  /** Start a session; resolves to { id, connectUrl, expiresAt, ... }. For a connected Playwright
   * browser in one step use `launch({ cloud: true, ... })` instead. */
  create(options: CloudSessionOptions = {}): Promise<Json> {
    return this._create(sessionBody(options as Record<string, unknown>));
  }

  /** @internal */
  _create(body: Record<string, unknown>): Promise<Json> {
    return this.http.call("POST", "/api/v1/browsers", body);
  }

  get(id: string): Promise<Json> {
    return this.http.call("GET", `/api/v1/browsers/${q(id)}`);
  }

  /** { balanceEur, sessions: [...] }, newest first. `status` may be an array. */
  list(opts: { status?: string | string[]; note?: string; limit?: number; before?: string } = {}): Promise<Json> {
    const status = Array.isArray(opts.status) ? opts.status.join(",") : opts.status;
    return this.http.call("GET", "/api/v1/browsers", undefined, { status, note: opts.note, limit: opts.limit, before: opts.before });
  }

  stop(id: string): Promise<Json> {
    return this.http.call("DELETE", `/api/v1/browsers/${q(id)}`);
  }

  /** A live-view WebSocket for a running session (`control: true` may also drive it). */
  live(id: string, opts: { control?: boolean } = {}): Promise<Json> {
    return this.http.call("GET", `/api/v1/browsers/${q(id)}/live`, undefined, { control: opts.control ? "1" : undefined });
  }

  /** A link anyone can open: the live view (`control: true` to let them drive) or, with
   * `recording: true`, the session's recording. */
  share(id: string, opts: { control?: boolean; minutes?: number; recording?: boolean } = {}): Promise<Json> {
    return this.http.call("POST", `/api/v1/browsers/${q(id)}/share`, compact({ control: opts.control, minutes: opts.minutes, recording: opts.recording }));
  }

  /** Hand a running session to a person: { state: "waiting", liveUrl, expiresAt, ... }. */
  handoff(id: string, opts: { reason?: string; timeoutSec?: number } = {}): Promise<Json> {
    return this.http.call("POST", `/api/v1/browsers/${q(id)}/handoff`, compact({ reason: opts.reason, timeoutSec: opts.timeoutSec }));
  }

  /** Mark a waiting hand-off done (what the live page's "I'm done" button does). */
  handoffDone(id: string): Promise<Json> {
    return this.http.call("POST", `/api/v1/browsers/${q(id)}/handoff/done`, {});
  }

  /** Poll until the session's hand-off is no longer waiting (done, or timed out on the server);
   * resolves to the session view. `timeout` (seconds) rejects with CloudTimeoutError. A session with no
   * hand-off at all rejects with CloudError NO_HANDOFF rather than resolving at once. */
  async waitHandoff(id: string, opts: { timeout?: number; poll?: number } = {}): Promise<Json> {
    const poll = opts.poll ?? 2;
    const watch = handoffWatch(id, opts.timeout);
    for (;;) {
      let view: Json;
      try {
        view = await this.get(id);
      } catch (e) {
        if (!watch.retry(e)) throw e;
        await sleep(watch.pause(poll));
        continue;
      }
      requireHandoff(id, view, watch.last === null);
      if (watch.seen(view).done) return view;
      await sleep(watch.pause(poll));
    }
  }

  /** One page of the session's event timeline: { events: [{ seq, at, type, data }], next }. Pass
   * `next` back as `after` for the following page; it is null at the end. */
  events(id: string, opts: { after?: number; limit?: number } = {}): Promise<Json> {
    return this.http.call("GET", `/api/v1/browsers/${q(id)}/events`, undefined, { after: opts.after ?? 0, limit: opts.limit });
  }

  /** A short-lived URL of the session's MP4. Rejects with CloudError 409 NOT_READY while it is still
   * being processed, and 404 when the session was not recorded. */
  async recordingUrl(id: string): Promise<string> {
    const r = await this.http.raw("GET", `/api/v1/browsers/${q(id)}/recording`);
    let url: string | null = null;
    if (r.status >= 300 && r.status < 400 && r.location) url = new URL(r.location, this.http.baseUrl + "/").href;
    else {
      const data = parseJson(r.status, r.text);
      if (data && typeof data === "object" && data.url) url = String(data.url);
    }
    // only a web URL (as the Python SDK, whose urllib would also open file://)
    if (!url || !/^https?:/i.test(url)) throw new CloudError(r.status, null, "the API did not answer with a recording URL");
    return url;
  }

  /** Save the session's recording to `path`; resolves to `path`. The storage URL is presigned, so
   * the API key is NOT sent to it. */
  async downloadRecording(id: string, path: string): Promise<string> {
    const url = await this.recordingUrl(id);
    const part = `${path}.part`;
    let res: Response;
    try {
      res = await fetch(url, { headers: { "User-Agent": USER_AGENT }, signal: AbortSignal.timeout(Math.max(this.http.timeoutMs, 600_000)) });
    } catch (e) {
      throw new CloudError(0, "NETWORK", `downloading the recording failed: ${causeOf(e)}`);
    }
    if (!res.ok || !res.body) throw new CloudError(res.status, null, `downloading the recording failed: HTTP ${res.status}`);
    try {
      await pipeline(Readable.fromWeb(res.body as unknown as WebReadableStream), createWriteStream(part));
      renameSync(part, path);
    } catch (e) {
      rmSync(part, { force: true });
      throw new CloudError(0, "NETWORK", `downloading the recording failed: ${causeOf(e)}`);
    }
    return path;
  }
}

/** Agent runs: a task in, JSON out. /api/v1/runs. */
export class CloudRuns {
  constructor(private readonly http: Http) {}

  /**
   * Start a run. With `wait` (the default) poll until it finishes and resolve to the run (status,
   * result with output, costEur, ...); otherwise resolve to the create answer at once. The browser
   * options of a cloud launch apply (country, identity, profile, record, ...) plus maxSteps, handoff
   * and handoffTimeoutSec.
   */
  async create(task: string, options: RunOptions = {}): Promise<Json> {
    const created = await this.http.call("POST", "/api/v1/runs", runBody(task, options));
    if (options.wait === false) return created;
    return this.wait(created.id, { timeout: options.timeout, poll: options.poll, onUpdate: options.onUpdate });
  }

  get(id: string): Promise<Json> {
    return this.http.call("GET", `/api/v1/runs/${q(id)}`);
  }

  /** { runs: [...] }, newest first. */
  list(opts: { limit?: number; before?: string } = {}): Promise<Json> {
    return this.http.call("GET", "/api/v1/runs", undefined, { limit: opts.limit, before: opts.before });
  }

  cancel(id: string): Promise<Json> {
    return this.http.call("DELETE", `/api/v1/runs/${q(id)}`);
  }

  /** Poll GET /api/v1/runs/{id} until the run is succeeded, failed, cancelled or expired. */
  async wait(id: string, opts: { timeout?: number; poll?: number; onUpdate?: (run: Json) => unknown } = {}): Promise<Json> {
    const poll = opts.poll ?? 1.5;
    const watch = runWatch(id, opts.timeout);
    const notify = opts.onUpdate ?? ((v: Json) => { if (v?.status === "waiting_for_human") announceHandoff(v); });
    for (;;) {
      let view: Json;
      try {
        view = await this.get(id);
      } catch (e) {
        if (!watch.retry(e)) throw e;
        await sleep(watch.pause(poll));
        continue;
      }
      const { done, changed } = watch.seen(view);
      if (changed) await notify(view);
      if (done) return view;
      await sleep(watch.pause(poll));
    }
  }
}

/** Where profiles.sync reads the cookies from: exactly one source. */
export interface SyncOptions {
  /** A local Chrome/Clearcote profile directory, read by a headless Clearcote started on it. */
  fromProfile?: string;
  /** A browser you already run with remote debugging, e.g. http://127.0.0.1:9222. */
  fromCdp?: string;
  /** A Playwright storage-state file, or a JSON array of cookies. */
  fromFile?: string;
  /** A visible Clearcote on a throwaway profile opens this page; you sign in, then `confirm` resolves
   * (default: press Enter in this terminal). */
  loginUrl?: string;
  /** Only the cookies a browser would use on these domains are uploaded: those of each domain, of its
   * subdomains, and of its parent domains (.example.com for www.example.com). */
  domains?: string[];
  /** Upload every cookie. Needed explicitly: with neither this nor `domains`, nothing is read. */
  allDomains?: boolean;
  /** Replace what the profile had instead of merging into it. */
  replace?: boolean;
  confirm?: () => unknown;
  /** @internal test seam: stands in for serve(). */
  serve?: ServeFn;
}

export const NEED_DOMAINS =
  "choose the cookies to upload: pass domains: [...] (each also covers its subdomains and the parent-domain cookies a browser sends it), or allDomains: true to upload every cookie";

/** Cloud profiles (named cookie stores): /api/v1/browsers/profiles. */
export class CloudProfiles {
  constructor(private readonly http: Http) {}

  list(): Promise<Json> {
    return this.http.call("GET", "/api/v1/browsers/profiles");
  }

  /** { name, cookies, domains, bytes, storage, updatedAt }; never the cookie values. */
  get(name: string): Promise<Json> {
    return this.http.call("GET", `/api/v1/browsers/profiles/${q(name)}`);
  }

  delete(name: string): Promise<Json> {
    return this.http.call("DELETE", `/api/v1/browsers/profiles/${q(name)}`);
  }

  /** Upload cookies (CDP or Playwright shape) into a profile, creating it if needed. mode "replace"
   * drops what the profile had first. 409 PROFILE_IN_USE while a live session saves to it. */
  importCookies(name: string, cookies: Json[], opts: { mode?: "merge" | "replace" } = {}): Promise<Json> {
    return this.http.call("PUT", `/api/v1/browsers/profiles/${q(name)}/cookies`, { cookies: [...cookies], mode: opts.mode ?? "merge" });
  }

  /** Copy a logged-in state into a cloud profile; see {@link SyncOptions}. */
  async sync(name: string, opts: SyncOptions = {}): Promise<Json> {
    const sources = [opts.fromProfile, opts.fromCdp, opts.fromFile, opts.loginUrl].filter(Boolean);
    if (sources.length !== 1) throw new Error("pass exactly one of fromProfile, fromCdp, fromFile or loginUrl");
    const domains = (typeof opts.domains === "string" ? [opts.domains] : opts.domains ?? []).filter((d) => d && String(d).trim());
    if (domains.length && opts.allDomains) throw new Error("pass domains or allDomains, not both");
    if (!domains.length && !opts.allDomains) throw new Error(NEED_DOMAINS);
    let cookies: Json[];
    if (opts.fromFile) cookies = readCookiesFromFile(opts.fromFile);
    else if (opts.fromCdp) cookies = await readCookiesFromCdp(opts.fromCdp);
    else if (opts.fromProfile) cookies = await readCookiesFromProfile(opts.fromProfile, { serve: opts.serve });
    else cookies = await readCookiesByLogin(opts.loginUrl as string, { confirm: opts.confirm, serve: opts.serve });
    const picked = opts.allDomains ? cookies : filterCookies(cookies, domains);
    if (!picked.length) {
      throw new Error(`no cookies found ${opts.allDomains ? "anywhere" : `for ${domains.join(", ")}`}; nothing was uploaded`);
    }
    return this.importCookies(name, picked.map(normalizeCookie), { mode: opts.replace ? "replace" : "merge" });
  }
}

/** Signed event deliveries to your HTTPS endpoint: /api/v1/webhooks. */
export class CloudWebhooks {
  constructor(private readonly http: Http) {}

  /** Register an endpoint; the answer carries `secret` (whsec_...), shown only here. */
  create(url: string, opts: { events?: string[]; description?: string } = {}): Promise<Json> {
    return this.http.call("POST", "/api/v1/webhooks", compact({ url, events: opts.events ? [...opts.events] : undefined, description: opts.description }));
  }

  list(): Promise<Json> {
    return this.http.call("GET", "/api/v1/webhooks");
  }

  delete(id: string): Promise<Json> {
    return this.http.call("DELETE", `/api/v1/webhooks/${q(id)}`);
  }

  /** Send a `ping` event to the endpoint. */
  test(id: string): Promise<Json> {
    return this.http.call("POST", `/api/v1/webhooks/${q(id)}/test`, {});
  }
}

/** Options of {@link Cloud}. */
export interface CloudOptions {
  /** Defaults to CLEARCOTE_API_KEY. */
  apiKey?: string;
  /** Defaults to CLEARCOTE_API_URL, then https://www.clearcotelabs.com. */
  baseUrl?: string;
  /** Per HTTP request, in seconds (default 30). */
  timeout?: number;
}

/** The hosted API, one client: `browsers`, `runs`, `profiles` and `webhooks`. */
export class Cloud {
  readonly baseUrl: string;
  readonly browsers: CloudBrowsers;
  readonly runs: CloudRuns;
  readonly profiles: CloudProfiles;
  readonly webhooks: CloudWebhooks;

  constructor(opts: CloudOptions = {}) {
    const key = String(opts.apiKey || process.env.CLEARCOTE_API_KEY || "").trim();
    if (!key) throw new Error(NO_API_KEY);
    const base = String(opts.baseUrl || process.env.CLEARCOTE_API_URL || DEFAULT_API_URL).trim().replace(/\/+$/, "");
    this.baseUrl = checkBaseUrl(base);
    const http = new Http(key, base, (opts.timeout ?? 30) * 1000);
    this.browsers = new CloudBrowsers(http);
    this.runs = new CloudRuns(http);
    this.profiles = new CloudProfiles(http);
    this.webhooks = new CloudWebhooks(http);
  }

  /** Never the key. */
  toJSON(): Record<string, string> {
    return { baseUrl: this.baseUrl };
  }
}

// ── webhooks: signature check ──────────────────────────────────────────────────────────────────────

/**
 * Check a webhook delivery and return its parsed JSON event.
 *
 * `rawBody` must be the request body EXACTLY as received (a string or Buffer): re-serialised JSON does
 * not match the signature. `signatureHeader` is the Clearcote-Signature header,
 * `t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`; more than one v1 may be present
 * (while a secret rotates) and any one matching is enough. Comparisons are constant-time. A timestamp
 * more than `toleranceSec` away from now is refused, so a captured delivery cannot be replayed later
 * (`toleranceSec: null` turns that off). Throws on any failure.
 */
// A real header is ~80 bytes (one t, one or two v1). Anything near this is not a Clearcote signature.
const MAX_SIGNATURE_HEADER = 4096;

export function verifyWebhook(
  rawBody: string | Buffer | Uint8Array,
  signatureHeader: string | null | undefined,
  secret: string,
  toleranceSec: number | null = 300,
  nowSec?: number,
): Json {
  if (secret === undefined || secret === null || !String(secret).trim()) {
    // String(undefined) is "undefined": an unset secret (process.env.X) would otherwise verify anything
    // signed with the key "undefined", which anyone can compute.
    throw new Error("verifyWebhook needs the signing secret of the endpoint (whsec_...), got none");
  }
  const body = typeof rawBody === "string" ? Buffer.from(rawBody, "utf8") : Buffer.from(rawBody);
  const header = String(signatureHeader ?? "");
  if (header.length > MAX_SIGNATURE_HEADER) {
    throw new Error("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)");
  }
  let stamp: string | null = null;
  const signatures: string[] = [];
  for (const part of header.split(",")) {
    const i = part.indexOf("=");
    if (i < 0) continue;
    const key = part.slice(0, i).trim();
    const value = part.slice(i + 1).trim();
    if (key === "t") stamp = value;
    else if (key === "v1" && value) signatures.push(value.toLowerCase());
  }
  if (!stamp || !/^[0-9]+$/.test(stamp) || !signatures.length) {
    throw new Error("invalid Clearcote-Signature header (expected t=<unix seconds>,v1=<hex>)");
  }
  const expected = Buffer.from(
    createHmac("sha256", String(secret)).update(Buffer.concat([Buffer.from(`${stamp}.`, "ascii"), body])).digest("hex"),
    "ascii",
  );
  const ok = signatures.some((s) => {
    const got = Buffer.from(s, "utf8");
    return got.length === expected.length && timingSafeEqual(got, expected);
  });
  if (!ok) throw new Error("webhook signature does not match");
  if (toleranceSec !== null && toleranceSec !== undefined) {
    const now = nowSec ?? Date.now() / 1000;
    if (Math.abs(now - Number(stamp)) > toleranceSec) {
      throw new Error("webhook timestamp is outside the tolerance window (a replay, or a clock that is off)");
    }
  }
  return JSON.parse(body.toString("utf8"));
}

// ── cookies: profile sync ──────────────────────────────────────────────────────────────────────────

// What PUT .../cookies keeps of a cookie (CookieParam). The CDP shape carries more (size, priority,
// sourceScheme, ...); the API drops those anyway, so they are not uploaded at all.
const COOKIE_FIELDS = ["name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"];

export function normalizeCookie(cookie: Json): Json {
  return Object.fromEntries(COOKIE_FIELDS.filter((k) => cookie[k] !== undefined && cookie[k] !== null).map((k) => [k, cookie[k]]));
}

const bareDomain = (d: unknown) => String(d ?? "").trim().toLowerCase().replace(/^\.+/, "");

/** `cookieDomain` is `allowed`, a subdomain of it, or a PARENT domain of it (a .example.com cookie is
 * sent to www.example.com too, so signing in there needs it). A parent must still have a dot of its own:
 * a cookie on a bare suffix (com) never matches. */
function domainMatches(cookieDomain: string, allowed: string): boolean {
  if (cookieDomain === allowed || cookieDomain.endsWith(`.${allowed}`)) return true;
  return cookieDomain.includes(".") && allowed.endsWith(`.${cookieDomain}`);
}

/** The cookies a browser would use on `domains`: those whose domain is one of them, a subdomain of one,
 * or a parent domain of one (.example.com for www.example.com; never a bare suffix such as com). A
 * leading dot on either side is ignored, and example.com does not match badexample.com. */
export function filterCookies(cookies: Json[], domains: string[]): Json[] {
  const allowed = domains.map(bareDomain).filter(Boolean);
  return cookies.filter((c) => {
    const d = bareDomain(c?.domain);
    return !!d && allowed.some((a) => domainMatches(d, a));
  });
}

/** The cookies in a Playwright storage state ({ cookies, origins }), a CDP { cookies } answer, or a
 * plain JSON array of cookies. */
export function cookiesFromState(data: Json): Json[] {
  const cookies = data && !Array.isArray(data) && typeof data === "object" ? data.cookies : data;
  if (!Array.isArray(cookies)) {
    throw new Error('expected a Playwright storage state ({"cookies": [...]}) or a JSON array of cookies');
  }
  for (const c of cookies) {
    if (!c || typeof c !== "object" || !c.name || !c.domain) throw new Error("every cookie needs at least a name and a domain");
  }
  return cookies;
}

export function readCookiesFromFile(path: string): Json[] {
  let data: Json;
  try {
    data = JSON.parse(readFileSync(path, "utf8"));
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code) throw e;
    throw new Error(`${path} is not JSON: ${(e as Error).message}`);
  }
  return cookiesFromState(data);
}

/** Every cookie of a running browser's default context, over CDP (Storage.getCookies on the browser
 * target). Closing a CDP connection only disconnects: the browser keeps running. */
export async function readCookiesFromCdp(endpoint: string): Promise<Json[]> {
  if (!/^(wss?|https?):\/\//.test(String(endpoint).trim())) {
    throw new Error("fromCdp must be an http(s):// or ws(s):// CDP endpoint, e.g. http://127.0.0.1:9222");
  }
  const browser = await chromium.connectOverCDP(String(endpoint).trim());
  try {
    const session = await browser.newBrowserCDPSession();
    const { cookies } = (await session.send("Storage.getCookies")) as { cookies?: Json[] };
    return [...(cookies ?? [])];
  } finally {
    await browser.close();
  }
}

/** What profile sync needs of serve(): a CDP endpoint on a local Clearcote, and a way to stop it. */
export type ServeFn = (opts: { userDataDir?: string; headless: boolean; quiet: boolean }) =>
  Promise<{ cdpUrl: string; close(): Promise<void>; isAlive(): boolean }>;

const defaultServe: ServeFn = async (opts) => (await import("./index.js")).serve(opts);

/** Start a headless Clearcote on `userDataDir` (a raw CDP endpoint via serve(): no Playwright launch,
 * nothing written but what Chrome itself writes), read its cookies, stop it. */
export async function readCookiesFromProfile(userDataDir: string, opts: { serve?: ServeFn } = {}): Promise<Json[]> {
  if (!existsSync(userDataDir) || !statSync(userDataDir).isDirectory()) throw new Error(`${userDataDir} is not a directory`);
  const srv = await (opts.serve ?? defaultServe)({ userDataDir, headless: true, quiet: true });
  try {
    return await readCookiesFromCdp(srv.cdpUrl);
  } finally {
    await srv.close();
  }
}

export const LOGIN_PROMPT = "Sign in in the browser window that just opened, then press Enter here to upload the cookies.";

async function waitForEnter(): Promise<void> {
  if (!process.stdin.isTTY) {
    throw new Error("login needs an interactive terminal to confirm the sign-in (pass confirm to wait another way)");
  }
  const rl = readline.createInterface({ input: process.stdin, output: process.stderr });
  await new Promise<void>((resolve) => rl.question(`${LOGIN_PROMPT} `, () => resolve()));
  rl.close();
}

/** Open `url` in a VISIBLE Clearcote on a throwaway profile, wait for `confirm()` (default: Enter in
 * this terminal), then read the cookies. The profile directory is deleted afterwards. */
export async function readCookiesByLogin(url: string, opts: { confirm?: () => unknown; serve?: ServeFn } = {}): Promise<Json[]> {
  const srv = await (opts.serve ?? defaultServe)({ headless: false, quiet: true });
  try {
    const browser = await chromium.connectOverCDP(srv.cdpUrl);
    try {
      await (await browser.newBrowserCDPSession()).send("Target.createTarget", { url });
    } finally {
      await browser.close();
    }
    await (opts.confirm ?? waitForEnter)();
    if (!srv.isAlive()) throw new Error("the browser was closed before the cookies were read; nothing was uploaded");
    return await readCookiesFromCdp(srv.cdpUrl);
  } finally {
    await srv.close();
  }
}

// ── launch({ cloud: true }) ────────────────────────────────────────────────────────────────────────

/** The hosted session a cloud launch() is connected to: the create answer minus the single-use
 * connect URL (it holds a token and is spent once connected). Also on `browser.cloudSession`. */
export function cloudSessionOf(target: Browser | BrowserContext): Json | undefined {
  return (target as unknown as { cloudSession?: Json }).cloudSession;
}

function prepareLaunch(options: Record<string, unknown>, persistent: boolean, userDataDir?: string | null) {
  const opts: Record<string, unknown> = { ...options };
  delete opts.cloud;
  const sdk: Record<string, unknown> = {};
  for (const k of SDK_SIDE_OPTIONS) {
    if (k in opts) {
      sdk[k] = opts[k];
      delete opts[k];
    }
  }
  if (persistent) {
    if (userDataDir !== undefined && userDataDir !== null) throw new Error(USER_DATA_DIR_MSG);
    const profile = opts.profile;
    if (!profile) {
      throw new Error('launchPersistentContext({ cloud: true }) needs profile: "name": the cloud profile whose cookies it loads and saves back when it closes');
    }
    opts.profile = typeof profile === "string" ? { name: profile, persist: true }
      : profile && typeof profile === "object" && Object.getPrototypeOf(profile) === Object.prototype ? { persist: true, ...(profile as object) }
        : profile;
  }
  const body = sessionBody(opts);
  // A cloud launch humanizes in the SDK by default, exactly as a local launch with humanize: typing at the
  // persona's cadence with key rollover, clicks and the wheel. The hosted browser would also move the
  // client's mouse along human paths by itself (cc-gateway 0.9.0), so the session says humanize: false: two
  // humanizers would fight over the cursor. humanize: false turns the SDK's off as well (none at all).
  if (sdk.humanize === undefined || sdk.humanize === null) sdk.humanize = true;
  body.humanize = false;
  return { sdk, body };
}

function connectUrlOf(created: Json): string {
  if (!created?.connectUrl) throw new CloudError(200, null, `the API created session ${created?.id} but sent no connectUrl`);
  return created.connectUrl as string;
}

async function stopQuietly(client: Cloud, created: Json): Promise<void> {
  try {
    if (created?.id) await client.browsers.stop(created.id);
  } catch { /* best-effort: the gateway ends it when the client goes anyway */ }
}

/** A new context would otherwise get Playwright's emulated 1280x720 viewport on top of the real
 * window: the impossible-window tell a local launch avoids the same way. */
function defaultNoViewport(browser: Browser): void {
  const origNewPage = browser.newPage.bind(browser);
  (browser as { newPage: unknown }).newPage = (o: Record<string, unknown> = {}) =>
    origNewPage(("viewport" in o ? o : { ...o, viewport: null }) as Parameters<typeof origNewPage>[0]);
  const origNewContext = browser.newContext.bind(browser);
  (browser as { newContext: unknown }).newContext = (o: Record<string, unknown> = {}) =>
    origNewContext(("viewport" in o ? o : { ...o, viewport: null }) as Parameters<typeof origNewContext>[0]);
}

/**
 * `launch({ cloud })`: create the session, connect over CDP, humanize as a local launch does, and
 * resolve to the Playwright Browser (or, `persistent`, its profile context). Exported for index.ts.
 * Nothing is left behind when a step after the create fails: the connection is closed and the session
 * stopped (DELETE), so a failed launch never keeps a billed browser running.
 */
export async function launchCloud(options: Record<string, unknown>, persistent = false, userDataDir?: string | null): Promise<Browser | BrowserContext> {
  const cloud = options.cloud;
  const { sdk, body } = prepareLaunch(options, persistent, userDataDir);
  warnStockRuntimeCloud(sdk.stockRuntime, sdk.quiet as boolean | undefined);
  const client = cloud instanceof Cloud ? cloud : new Cloud({ apiKey: sdk.apiKey as string | undefined, baseUrl: sdk.apiUrl as string | undefined });
  const created = await client.browsers._create(body);
  const connect: Record<string, unknown> = { timeout: CLOUD_CONNECT_TIMEOUT_MS };
  for (const k of CONNECT_OPTIONS) if (sdk[k] != null) connect[k] = sdk[k];
  let disconnect: (() => Promise<void>) | null = null;
  try {
    const browser = await chromium.connectOverCDP(connectUrlOf(created), connect as { timeout?: number; slowMo?: number });
    const origClose = browser.close.bind(browser);
    disconnect = () => origClose();
    const info = Object.fromEntries(Object.entries(created).filter(([k]) => k !== "connectUrl"));
    Object.defineProperty(browser, "cloudSession", { value: info, enumerable: false, configurable: true });
    const keepAlive = body.keepAlive === true;
    (browser as { close: unknown }).close = async (...args: Parameters<Browser["close"]>) => {
      // Disconnect first (the gateway ends a session whose client goes), then say so explicitly.
      // A keep-alive session is left running on purpose: stop it with cloud.browsers.stop(id).
      try {
        return await origClose(...args);
      } finally {
        if (!keepAlive) await stopQuietly(client, created);
      }
    };
    defaultNoViewport(browser);
    // The humanizer's persona seed: the fingerprint, else the identity label, so one identity keeps one
    // hand across sessions (a local launch seeds from the fingerprint the same way).
    const seed = (body.fingerprint ?? body.identity) as string | number | undefined;
    const humanize = { humanize: sdk.humanize as boolean | undefined, showCursor: sdk.showCursor as boolean | undefined, seed };
    for (const ctx of browser.contexts()) installHumanizeOnContext(ctx, humanize, browser); // the session's own context and its tabs
    installHumanize(browser, humanize);
    if (!persistent) return browser;
    const context = browser.contexts()[0] ?? (await browser.newContext());
    Object.defineProperty(context, "cloudSession", { value: info, enumerable: false, configurable: true });
    // Closing the profile context closes (and saves) the session.
    (context as { close: unknown }).close = async () => { await browser.close(); };
    return context;
  } catch (e) {
    if (disconnect) await disconnect().catch(() => {});
    await stopQuietly(client, created);
    throw e;
  }
}
