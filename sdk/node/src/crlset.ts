// Chrome's revocation list (CRLSet) for an engine that never receives one.
//
// Real Chrome downloads a CRLSet through its component updater: Google's list of revoked and blocked
// certificate keys, which is how a CA distrust reaches the browser within hours (the eMudhra key compromise of
// 2026-10-17 shipped as CRLSet 10849). A Clearcote engine never gets it: the updater URL is
// domain-substituted to a dead host, and Playwright launches with --disable-component-update anyway. So every
// engine since 149 kept trusting those intermediates (tested on 12 builds, 149-155, free and PRO).
//
// The engine still knows how to use one. This module:
//
// 1. SEEDS a CRLSet into the profile (`<userDataDir>/CertificateRevocation/<sequence>/crl-set` plus a
//    manifest). Chrome checks only that the file exists (CRLSetPolicy::VerifyInstallation), so a cached copy
//    loads as is. An expired one still blocks everything it lists: expiry only turns off the "known good"
//    shortcut (revocation_checker.cc).
// 2. UN-SUPPRESSES the component updater. Without it RegisterComponentsForUpdate never runs and the seeded
//    file is never read. The updater makes no network request on this engine (NetLog, 90 s idle with
//    fast-update: no update check, no DNS, nothing through the proxy).
// 3. WAITS until the CRLSet is live before the caller gets the browser. Chrome loads it with BEST_EFFORT tasks
//    that only run once startup completes, and startup completes when the first tab has been idle for a
//    moment (AfterStartupTaskUtils, 3 min failsafe). A caller who navigates straight away would keep the tab
//    busy and stay unprotected for minutes. So the first tab stays on about:blank until the CRLSet applies.
//
// Readiness signal: applying a CRLSet changes the cert verifier, and the network service then closes its
// idle pooled connections, plain HTTP ones included. The probe keeps one keep-alive HTTP connection to a
// local server on 127.0.0.1 and waits for that connection to be dropped and re-opened (no certificate, no
// CDP). Measured 5/5 live: an eMudhra host navigated right after is ERR_CERT_REVOKED. It adds a few seconds
// to a launch.
//
// OPT-IN: `crlset: true` (or CLEARCOTE_CRLSET=1). Where the CRLSet comes from, first match wins:
// `crlset: "<path>"` (a crl-set file or a component directory), CLEARCOTE_CRLSET_FILE, then the newest
// copy in the cache (CLEARCOTE_CRLSET_DIR, else ~/.clearcote/crlset). An empty cache is filled at launch
// from Google's component updater, the same Omaha service the Widevine fetch uses (sha256 from the HTTPS
// update response); a cache older than 6 h is refreshed in the background while the launch uses it.
// CLEARCOTE_CRLSET_URL points the fetch at a mirror instead. The cache layout (`<sequence>/crl-set`) is
// shared by the Python, Node and .NET SDKs. Same rules, names and warnings as the Python SDK (_crlset.py).

import { createHash, randomBytes } from "node:crypto";
import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import extract from "extract-zip";
import type { BrowserContext } from "playwright-core";
import { OMAHA_URL, crx3ToZip, omahaRequestBody, omahaUserAgent, parseUpdate, postJson, unsuppressComponentUpdate, withTimeout } from "./widevine.js";

export const COMPONENT_DIR = "CertificateRevocation";
export const CRLSET_FILE = "crl-set";
export const CRLSET_APP_ID = "hfnkpimlhhgieaddgfemjhofmfblmnib"; // Chrome's CRLSet component
export const DEFAULT_TIMEOUT_SECONDS = 15; // the readiness wait (CLEARCOTE_CRLSET_TIMEOUT, crlsetTimeout)
export const FETCH_TIMEOUT_SECONDS = 15; // how long a launch waits for a first download (CLEARCOTE_CRLSET_FETCH_TIMEOUT)
export const REFRESH_SECONDS = 6 * 3600; // a cache older than this is refreshed in the background
export const STAMP_FILE = ".last-check"; // in the cache root: mtime = the last fetch attempt

/** `crlset` launch option: true (wait), false (off), "nowait", or a path to a crl-set file / component directory. */
export type CrlsetOption = boolean | "nowait" | string;
export type CrlsetMode = "off" | "wait" | "nowait";

const WARNED = new Set<string>();
let refreshJob: Promise<void> | undefined;

function warnOnce(key: string, message: string, quiet?: boolean): void {
  if (quiet || process.env.CLEARCOTE_NO_WARN || WARNED.has(key)) return;
  WARNED.add(key);
  process.stderr.write(`clearcote: crlset: ${message}\n`);
}

/** Tests: forget the warnings already said (each warns once per process). */
export function resetCrlsetWarnings(): void {
  WARNED.clear();
}

/** Tests: the background refresh in flight, if any. */
export function crlsetRefreshInFlight(): Promise<void> | undefined {
  return refreshJob;
}

function errName(e: unknown): string {
  return (e as { name?: string })?.name ?? "Error";
}

function errText(e: unknown): string {
  return String((e as { message?: string })?.message ?? e);
}

export function cacheRoot(): string {
  return process.env.CLEARCOTE_CRLSET_DIR ?? path.join(os.homedir(), ".clearcote", "crlset");
}

/** The JSON header of a CRLSet file (little-endian uint16 length, then JSON). Throws on anything else. */
export function parseHeader(data: Buffer): { Sequence: number; [k: string]: unknown } {
  if (data.length < 2) throw new Error("not a CRLSet: too short");
  const length = data.readUInt16LE(0);
  let header: any;
  try {
    header = JSON.parse(data.subarray(2, 2 + length).toString("utf8"));
  } catch (e) {
    throw new Error(`not a CRLSet: bad header (${errText(e)})`);
  }
  if (!header || typeof header !== "object" || Array.isArray(header) || header.ContentType !== "CRLSet") {
    throw new Error("not a CRLSet: ContentType is not CRLSet");
  }
  if (!Number.isInteger(header.Sequence)) throw new Error("not a CRLSet: no Sequence");
  return header;
}

function isFile(p: string): boolean {
  try {
    return statSync(p).isFile();
  } catch {
    return false;
  }
}

function isDir(p: string): boolean {
  try {
    return statSync(p).isDirectory();
  } catch {
    return false;
  }
}

/** A crl-set file path from a file or a component directory (`<dir>/crl-set` or `<dir>/<ver>/crl-set`). */
function asCrlsetFile(p: string): string {
  if (isFile(p)) return p;
  const direct = path.join(p, CRLSET_FILE);
  if (isFile(direct)) return direct;
  const versions: [number, string][] = [];
  for (const name of isDir(p) ? readdirSync(p) : []) {
    const candidate = path.join(p, name, CRLSET_FILE);
    if (!isFile(candidate)) continue;
    try {
      versions.push([parseHeader(readFileSync(candidate)).Sequence, candidate]);
    } catch {
      continue;
    }
  }
  if (!versions.length) throw new Error(`no crl-set file under ${p}`);
  versions.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0));
  return versions[versions.length - 1][1];
}

/**
 * Copy a CRLSet (a crl-set file, or Chrome's `CertificateRevocation` directory) into the cache as
 * `<cache>/<sequence>/crl-set`. Returns that path.
 */
export function installCrlset(source: string): string {
  const src = asCrlsetFile(source);
  const data = readFileSync(src);
  const sequence = parseHeader(data).Sequence;
  const destDir = path.join(cacheRoot(), String(sequence));
  mkdirSync(destDir, { recursive: true });
  const dest = path.join(destDir, CRLSET_FILE);
  const tmp = dest + ".tmp";
  writeFileSync(tmp, data);
  renameSync(tmp, dest);
  return dest;
}

/** The current CRLSet from Google's component updater (the crl-set bytes). Verified against the sha256
 * of the HTTPS update response (the package itself may come over plain HTTP from edgedl). `work` is a
 * scratch directory for the zip. */
async function downloadFromGoogle(work: string, signal: AbortSignal): Promise<Buffer> {
  const body = omahaRequestBody() as { request: { app: { appid: string }[] } }; // the Omaha client the Widevine fetch uses
  body.request.app[0].appid = CRLSET_APP_ID;
  const [url, sha256] = parseUpdate(await postJson(OMAHA_URL, body, signal));
  const res = await fetch(url, { headers: { "User-Agent": omahaUserAgent() }, redirect: "follow", signal });
  if (!res.ok) throw new Error(`CRLSet download HTTP ${res.status}`);
  const blob = Buffer.from(await res.arrayBuffer());
  if (!sha256 || createHash("sha256").update(blob).digest("hex") !== sha256.toLowerCase()) {
    throw new Error("CRLSet sha256 mismatch - refusing to install");
  }
  const zipPath = path.join(work, "crlset.zip");
  writeFileSync(zipPath, crx3ToZip(blob));
  const unpacked = path.join(work, "unpacked");
  await extract(zipPath, { dir: unpacked });
  return readFileSync(path.join(unpacked, CRLSET_FILE));
}

function touchStamp(): void {
  const root = cacheRoot();
  mkdirSync(root, { recursive: true });
  writeFileSync(path.join(root, STAMP_FILE), "");
}

function stampFresh(): boolean {
  try {
    return Date.now() - statSync(path.join(cacheRoot(), STAMP_FILE)).mtimeMs < REFRESH_SECONDS * 1000;
  } catch {
    return false;
  }
}

/**
 * Download the current CRLSet into the cache and return its path: from Google's component updater, or
 * from `url` / CLEARCOTE_CRLSET_URL (a mirror serving the raw crl-set file).
 */
export async function fetchCrlset(url?: string, signal?: AbortSignal): Promise<string> {
  url = url || process.env.CLEARCOTE_CRLSET_URL;
  const limit = withTimeout(60_000, signal);
  touchStamp(); // a failed attempt counts too: no retry storm against Google
  const tmpDir = path.join(cacheRoot(), `.download-${process.pid}-${randomBytes(6).toString("hex")}`);
  mkdirSync(tmpDir, { recursive: true });
  try {
    let data: Buffer;
    if (url) {
      const res = await fetch(url, { redirect: "follow", signal: limit });
      if (!res.ok) throw new Error(`CRLSet download HTTP ${res.status}`);
      data = Buffer.from(await res.arrayBuffer());
    } else {
      data = await downloadFromGoogle(tmpDir, limit);
    }
    parseHeader(data);
    const tmp = path.join(tmpDir, CRLSET_FILE);
    writeFileSync(tmp, data);
    return installCrlset(tmp);
  } finally {
    rmSync(tmpDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  }
}

/** At most one refresh per process at a time; the launch goes on with the cached copy. */
function refreshInBackground(): void {
  if (refreshJob) return;
  const job: Promise<void> = fetchCrlset()
    .then(() => undefined, () => undefined) // the cached copy still serves; the next stale launch retries
    .finally(() => { if (refreshJob === job) refreshJob = undefined; });
  refreshJob = job;
}

function secondsFromEnv(name: string, fallback: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value)) throw new Error(`could not convert ${name}=${JSON.stringify(raw)} to seconds`);
  return value;
}

/** An empty cache: fetch now, waiting at most CLEARCOTE_CRLSET_FETCH_TIMEOUT. The path, or undefined (warned). */
async function fetchFirst(quiet?: boolean): Promise<string | undefined> {
  const wait = secondsFromEnv("CLEARCOTE_CRLSET_FETCH_TIMEOUT", FETCH_TIMEOUT_SECONDS);
  // Aborted at the deadline (Python lets its daemon thread finish): a pending fetch would keep the Node
  // process alive after the caller's script ends. The next launch, still with an empty cache, retries.
  const abort = new AbortController();
  const job = fetchCrlset(undefined, abort.signal).then((ok) => ({ ok }), (err: unknown) => ({ err }));
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<"timeout">((resolve) => { timer = setTimeout(() => resolve("timeout"), wait * 1000); });
  try {
    const r = await Promise.race([job, deadline]);
    if (r === "timeout") {
      abort.abort();
      warnOnce("fetch", `no CRLSet cached and the first download took over ${wait} s; launching without ` +
        "one (the next launch tries again)", quiet);
      return undefined;
    }
    if ("err" in r) {
      warnOnce("fetch", `could not fetch a CRLSet (${errName(r.err)}: ${errText(r.err).slice(0, 160)}); launching without one`, quiet);
      return undefined;
    }
    return r.ok;
  } finally {
    clearTimeout(timer);
  }
}

/** The crl-set file to seed, or undefined. `explicit` (a path) > CLEARCOTE_CRLSET_FILE > newest cached. */
export function findCrlset(explicit?: string): string | undefined {
  for (const p of [explicit, process.env.CLEARCOTE_CRLSET_FILE]) {
    if (p) return asCrlsetFile(p);
  }
  const root = cacheRoot();
  if (!isDir(root)) return undefined;
  try {
    return asCrlsetFile(root);
  } catch {
    return undefined;
  }
}

/** {@link findCrlset}, filling an empty cache from Google first and refreshing a stale one in the
 * background. undefined when there is none to be had (warned once). */
export async function resolveCrlset(explicit?: string, quiet?: boolean): Promise<string | undefined> {
  const found = findCrlset(explicit);
  if (explicit || process.env.CLEARCOTE_CRLSET_FILE) return found;
  if (found === undefined) return fetchFirst(quiet);
  if (!stampFresh()) refreshInBackground();
  return found;
}

/** `crlset` (undefined/null reads CLEARCOTE_CRLSET) -> [mode, path]: mode is "off" (the default), "wait" or "nowait". */
export function crlsetMode(value: unknown): [CrlsetMode, string | undefined] {
  if (value === undefined || value === null) {
    const env = String(process.env.CLEARCOTE_CRLSET ?? "").trim().toLowerCase();
    if (env === "nowait") return ["nowait", undefined];
    return [["1", "true", "on", "yes"].includes(env) ? "wait" : "off", undefined];
  }
  if (value === false) return ["off", undefined];
  if (value === true) return ["wait", undefined];
  if (value === "nowait") return ["nowait", undefined];
  if (typeof value === "string") return ["wait", value];
  throw new Error(`crlset must be true, false, "nowait" or a path to a CRLSet, not ${JSON.stringify(value)}`);
}

/** Place the CRLSet in the profile where the component installer finds it. Keeps a newer one that is
 * already there (a reused profile). Returns the sequence now in the profile. */
export function seedCrlset(userDataDir: string, crlsetFile: string): number {
  const data = readFileSync(crlsetFile);
  const sequence = parseHeader(data).Sequence;
  const base = path.join(userDataDir, COMPONENT_DIR);
  const present: number[] = [];
  if (isDir(base)) {
    for (const name of readdirSync(base)) {
      if (/^\d+$/.test(name) && isFile(path.join(base, name, CRLSET_FILE))) present.push(Number(name));
    }
  }
  if (present.length && Math.max(...present) >= sequence) return Math.max(...present);
  const versionDir = path.join(base, String(sequence));
  mkdirSync(versionDir, { recursive: true });
  writeFileSync(path.join(versionDir, CRLSET_FILE), data);
  writeFileSync(path.join(versionDir, "manifest.json"),
    JSON.stringify({ manifest_version: 2, name: "crl-set-clearcote.data", version: String(sequence) }));
  return sequence;
}

/** Drop Playwright's --disable-component-update (as the Widevine seeding does). `true` already ignores every
 * default argument; list/unset forms get the flag added. Mutates `options.ignoreDefaultArgs`. */
export function unsuppressComponentUpdater(options: { ignoreDefaultArgs?: string[] | boolean }): void {
  options.ignoreDefaultArgs = unsuppressComponentUpdate(options.ignoreDefaultArgs);
}

/** What {@link applyCrlsetLaunch} reads and changes: the two options it removes and Playwright's ignoreDefaultArgs. */
export interface CrlsetLaunchOptions {
  crlset?: CrlsetOption | null;
  crlsetTimeout?: number | null;
  ignoreDefaultArgs?: string[] | boolean;
  [k: string]: unknown;
}

/**
 * Remove `crlset` / `crlsetTimeout` from `options`, seed the profile and un-suppress the updater.
 * Resolves to a {@link CrlsetReadiness} when the launch must wait, else undefined.
 * Never rejects: revocation data is best-effort and must not stop a launch.
 */
export async function applyCrlsetLaunch(userDataDir: string, options: CrlsetLaunchOptions, quiet?: boolean): Promise<CrlsetReadiness | undefined> {
  const value = options.crlset;
  let timeout = options.crlsetTimeout;
  delete options.crlset;
  delete options.crlsetTimeout;
  try {
    const [mode, explicit] = crlsetMode(value);
    if (mode === "off") return undefined;
    let crlsetFile: string | undefined;
    try {
      crlsetFile = await resolveCrlset(explicit, quiet);
    } catch (e) {
      warnOnce("find", `no usable CRLSet (${errText(e)}); launching without one`, quiet);
      return undefined;
    }
    if (crlsetFile === undefined) return undefined; // resolveCrlset warned
    const sequence = seedCrlset(userDataDir, crlsetFile);
    unsuppressComponentUpdater(options);
    if (mode === "nowait") return undefined;
    if (timeout === undefined || timeout === null) timeout = secondsFromEnv("CLEARCOTE_CRLSET_TIMEOUT", DEFAULT_TIMEOUT_SECONDS);
    return new CrlsetReadiness(Number(timeout), sequence, quiet);
  } catch (e) {
    warnOnce("seed", `seeding failed (${errName(e)}: ${errText(e)}); launching without one`, quiet);
    return undefined;
  }
}

/** HTTP/1.1 on 127.0.0.1 that counts the TCP connections the browser opens to it. */
export class ProbeServer {
  connections = 0;
  url = "";
  private constructor(private readonly server: Server) {}

  static async start(): Promise<ProbeServer> {
    const server = createServer((_req, res) => {
      res.writeHead(204, { "Access-Control-Allow-Origin": "*", "Content-Length": "0" });
      res.end();
    });
    // keep-alive (HTTP/1.1): the browser pools the connection. No idle timeout: a connection the server closed
    // would be re-opened and read as the CRLSet's drop.
    server.keepAliveTimeout = 0;
    const probe = new ProbeServer(server);
    server.on("connection", () => { probe.connections += 1; });
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => { server.off("error", reject); resolve(); });
    });
    server.unref(); // never keeps the process alive
    probe.url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
    return probe;
  }

  count(): number {
    return this.connections;
  }

  close(): void {
    this.server.closeAllConnections();
    this.server.close();
  }
}

// runs in the page (the browser's fetch takes `cache`; Node's RequestInit type does not list it)
const probeJs = (u: string) => fetch(u, { cache: "no-store" } as RequestInit).then((r) => r.status, () => 0);

/**
 * Launch args that send `rule` around a Playwright-handled proxy, else []. Playwright adds `<-loopback>`
 * after the caller's bypass rules, and Chrome evaluates bypass rules last-first, so a rule added to
 * `proxy.bypass` loses and the local probe went to the user's proxy as `CONNECT 127.0.0.1:<port>` (seen
 * live). Chrome keeps the last of a repeated switch and Playwright puts the caller's args after its own, so
 * this repeats Playwright's list with `rule` LAST: only that host:port goes direct; other loopback traffic
 * still goes through the proxy. A proxy the SDK hands to the engine itself (credentials, see resolveProxy)
 * has no `<-loopback>` and needs nothing.
 */
export function proxyBypassArgs(pwOptions: { proxy?: { bypass?: string } | null }, rule: string): string[] {
  const proxy = pwOptions.proxy;
  if (!proxy) return [];
  let rules = String(proxy.bypass ?? "").split(",").map((r) => r.trim()).filter(Boolean);
  rules = rules.map((r) => (r.startsWith(".") ? "*" + r : r)); // as Playwright does
  if (!process.env.PLAYWRIGHT_DISABLE_FORCED_CHROMIUM_PROXIED_LOOPBACK && !rules.includes("<-loopback>")) rules.push("<-loopback>");
  return ["--proxy-bypass-list=" + [...rules, rule].join(";")];
}

/** The wait for a seeded CRLSet: {@link beforeLaunch} (the probe server, so its port can bypass the proxy),
 * then {@link wait} once the context is up, or {@link close} if the launch failed. */
export class CrlsetReadiness {
  server: ProbeServer | undefined;

  constructor(readonly timeout: number, readonly sequence: number, readonly quiet?: boolean) {}

  /** Start the probe server; resolves to the launch args to append (the proxy bypass for its port). */
  async beforeLaunch(pwOptions: { proxy?: { bypass?: string } | null }): Promise<string[]> {
    try {
      this.server = await ProbeServer.start();
      return proxyBypassArgs(pwOptions, this.server.url.slice("http://".length).replace(/\/+$/, ""));
    } catch (e) {
      warnOnce("probe", `readiness probe unavailable (${errName(e)}: ${errText(e)})`, this.quiet);
      this.close();
      return [];
    }
  }

  /** Hold the first tab on about:blank until the CRLSet is live. true when confirmed; false on timeout or
   * error (warned once). See the comment at the top of this file for the signal. */
  async wait(context: BrowserContext | null | undefined): Promise<boolean> {
    const server = this.server;
    if (!server || !context) {
      this.close();
      return false;
    }
    const deadline = performance.now() + this.timeout * 1000;
    try {
      const page = context.pages()[0] ?? (await context.newPage());
      while (performance.now() < deadline) {
        await page.evaluate(probeJs, server.url);
        if (server.count() >= 2) return true; // the pooled connection was dropped: the cert verifier changed
        await new Promise((r) => setTimeout(r, 50));
      }
      warnOnce("timeout", `CRLSet not confirmed live within ${this.timeout} s; the browser may still trust revoked ` +
        "certificates until it loads", this.quiet);
      return false;
    } catch (e) {
      warnOnce("wait", `readiness check failed (${errName(e)}: ${errText(e)})`, this.quiet);
      return false;
    } finally {
      this.close();
    }
  }

  close(): void {
    if (this.server) {
      try {
        this.server.close();
      } catch {
        /* already closed */
      }
      this.server = undefined;
    }
  }
}
