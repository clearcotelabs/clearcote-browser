// Widevine CDM fetch + persistent-profile seeding (Windows + Linux).
//
// clearcote is a 100%-open-source build: it ships the EME/Widevine *plumbing* (enable_widevine=true)
// but NOT Google's proprietary CDM binary (that blob can't live in a FOSS package). This module fetches
// the CDM from Google's component server at runtime — exactly how a real Chrome receives it via the
// component updater — so requestMediaKeySystemAccess('com.widevine.alpha') resolves (DRM plays, and the
// EME surface matches a real Chrome instead of a no-Widevine tell).
//
//   import { fetchWidevine, launchPersistentContext } from "clearcote";
//   await fetchWidevine();                                  // download + verify into ~/.clearcote
//   const ctx = await launchPersistentContext("profile", { widevine: true });  // seeds + enables it
//
// WHEN IT IS ON (since 0.48.0). A licensed launch (a licence key resolved: the launch that selects the
// PRO build, which claims the Google Chrome brand) on an engine with --widevine-cdm-path (r32+) loads the
// CDM by default; a free launch keeps it opt-in. `widevine: false` or CLEARCOTE_WIDEVINE=0 turns it off,
// `widevine: true` or CLEARCOTE_WIDEVINE=1 on for any launch; an explicit option wins over the
// environment (see widevineMode). The default is FAIL-SOFT and never waits on the network when a CDM is
// cached: a cached CDM is used at once (a stale one is refreshed in the background). Only the very first
// launch, with nothing cached, waits for the download, at most CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS (default
// 15, clamped to 1-600); when that fails the launch goes on without Widevine and says so once per process.
// The default path prints nothing else. A launch that ASKED for Widevine keeps its old behaviour (a note,
// unless quiet, and no DRM).
//
// The CDM is cached (CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm); Google is asked for a newer
// one at most once a day, and a failed check counts as one (the cached CDM keeps working). Installs take
// the SDK's install lock in the cache directory and land with download.ts's rename (retried on Windows
// while a scanner holds the fresh tree), so concurrent first launches never see a half-extracted CDM. The
// version Google names must look like one before it is used as a directory name. Same rules, names and
// warning as the Python and .NET SDKs.

import { createHash, randomBytes } from "node:crypto";
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { performance } from "node:perf_hooks";
import extract from "extract-zip";
import { acquireInstallLock, renameDir } from "./download.js";
import { engineSupportsSwitch } from "./launchopts.js";
import { hostArch } from "./release.js";
import { warnOnce } from "./warnings.js";

/** 1/true/yes/on forces Widevine on, 0/false/no/off off; an explicit `widevine` option wins over it. */
export const WIDEVINE_ENV = "CLEARCOTE_WIDEVINE";
/** How long a DEFAULT (not requested) launch waits for a first download, in seconds (clamped to 1-600). */
export const TIMEOUT_ENV = "CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS";
export const DEFAULT_TIMEOUT_SECONDS = 15;
export const MIN_TIMEOUT_SECONDS = 1;
export const MAX_TIMEOUT_SECONDS = 600;
/** Ask Google for a newer CDM at most once a day. */
export const CHECK_INTERVAL_MS = 24 * 3600_000;
/** A failed first download is not retried for 10 minutes in this process. */
export const RETRY_AFTER_MS = 600_000;
/** How long a background refresh waits for Google's update check (it may hold a finishing process that long). */
export const REFRESH_CHECK_TIMEOUT_MS = 10_000;
/** In the cache root: its mtime is the last update check (a failed one counts). */
export const STAMP_FILE = ".last-check";
/** What a CDM version looks like; anything else never becomes a path. */
export const VERSION_RE = /^[0-9]+(\.[0-9]+){1,3}$/;
/** How long a requested install waits for another process installing the CDM. */
export const LOCK_WAIT_MS = 300_000;
export const WARN_CODE = "widevine-default";
const TRUTHY = ["1", "true", "yes", "on"];
const FALSY = ["0", "false", "no", "off"];

/** What one launch does about Widevine: requested, the licensed default, or nothing. */
export type WidevineMode = "explicit" | "default" | "off";

/** Chrome's component-updater app id for the Widevine CDM + Google's Omaha JSON endpoint. */
export const WIDEVINE_APP_ID = "oimompecagnajdejgnnjijobebaeigek";
export const OMAHA_URL = "https://update.googleapis.com/service/update2/json";
export const HINT_FILE = "latest-component-updated-widevine-cdm";

/** One OS's CDM coordinates: what the Omaha request names, and where the library sits in the package. */
export interface CdmPlatform {
  atOs: string;
  osPlatform: string;
  osVersion: string;
  subdir: string;
  filename: string;
  /** Omaha's `arch`, `nacl_arch` and `os.arch` for the CPU asked for. */
  arch: string;
  naclArch: string;
  osArch: string;
}

/**
 * Per-OS CDM coordinates. Linux ships libwidevinecdm.so under linux_x64 and registers via the hint
 * file; Windows ships widevinecdm.dll under win_x64 and registers via the component-updater scan, and
 * so does macOS: libwidevinecdm.dylib under mac_arm64 on Apple Silicon (the native PRO build), mac_x64
 * on an Intel Mac. Asked as an arm64 Mac, Google's server answers with `…_mac_arm64_….crx3`, whose
 * library sits at _platform_specific/mac_arm64/libwidevinecdm.dylib (checked 2026-10-17, CDM
 * 4.10.3112.0). Before this every Mac was sent the Windows CDM, which no Mac can load.
 */
export function cdmPlatform(platform: string = process.platform, arch: string = hostArch.get()): CdmPlatform {
  const x64 = { arch: "x64", naclArch: "x86-64", osArch: "x86_64" };
  if (platform === "linux") {
    return { atOs: "Linux", osPlatform: "Linux", osVersion: "6.1.0", subdir: "linux_x64", filename: "libwidevinecdm.so", ...x64 };
  }
  if (platform === "darwin") {
    const arm = arch === "arm64";
    return {
      atOs: "mac", osPlatform: "Mac OS X", osVersion: "14.0.0",
      subdir: arm ? "mac_arm64" : "mac_x64", filename: "libwidevinecdm.dylib",
      ...(arm ? { arch: "arm64", naclArch: "arm", osArch: "arm64" } : x64),
    };
  }
  return { atOs: "win", osPlatform: "Windows", osVersion: "10.0.19045.0", subdir: "win_x64", filename: "widevinecdm.dll", ...x64 };
}

/**
 * update.googleapis.com sits behind Google's edge; a bare fetch UA can 403 — send a browser-ish one.
 * Match the request UA to the OS we ask the CDM for, so the Omaha call is coherent. Read per request,
 * not once at import, so it always follows the OS the request names. (Chrome on Apple Silicon still
 * says "Intel Mac OS X 10_15_7".)
 */
export function omahaUserAgent(platform: string = process.platform): string {
  if (platform === "linux") return "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
  if (platform === "darwin") return "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
  return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
}

export interface WidevineOptions {
  /** Destination root (default CLEARCOTE_WIDEVINE_DIR, else ~/.clearcote/WidevineCdm). */
  dest?: string;
  /** Suppress progress logging. */
  quiet?: boolean;
  /** Abort the update check and the download (the default launch path bounds its wait with it). */
  signal?: AbortSignal;
  /** Abort only the update check (a background refresh bounds it). */
  checkSignal?: AbortSignal;
  /** How long to wait for another process installing the CDM (default 5 minutes). */
  lockTimeoutMs?: number;
}

function log(quiet: boolean | undefined, msg: string): void {
  if (!quiet) process.stderr.write(`[clearcote] [widevine] ${msg}\n`);
}

function cacheRoot(): string {
  return process.env.CLEARCOTE_WIDEVINE_DIR || path.join(os.homedir(), ".clearcote", "WidevineCdm");
}

/** Minimal Omaha v3.1 update check for the current OS's Widevine CDM (version 0.0.0.0 -> latest). */
export function omahaRequestBody(): unknown {
  const { atOs, osPlatform, osVersion, arch, naclArch, osArch } = cdmPlatform();
  return {
    request: {
      "@os": atOs,
      "@updater": "clearcote",
      acceptformat: "crx3",
      protocol: "3.1",
      arch,
      nacl_arch: naclArch,
      prodversion: "149.0.0.0",
      updaterversion: "149.0.0.0",
      dedup: "cr",
      os: { arch: osArch, platform: osPlatform, version: osVersion },
      app: [{ appid: WIDEVINE_APP_ID, version: "0.0.0.0", updatecheck: {}, ping: { r: -2 } }],
    },
  };
}

/** `AbortSignal.timeout(ms)`, also aborted with `signal` (AbortSignal.any is Node 20.3+). */
export function withTimeout(ms: number, signal?: AbortSignal): AbortSignal {
  const timeout = AbortSignal.timeout(ms);
  if (!signal) return timeout;
  const both = new AbortController();
  const stop = (s: AbortSignal) => () => both.abort(s.reason);
  if (signal.aborted) both.abort(signal.reason);
  else {
    signal.addEventListener("abort", stop(signal), { once: true });
    timeout.addEventListener("abort", stop(timeout), { once: true });
  }
  return both.signal;
}

/** POST an Omaha JSON request and parse the answer (XSSI guard stripped). Also the CRLSet fetch's (./crlset.ts). */
export async function postJson(url: string, body: unknown, signal?: AbortSignal): Promise<any> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "User-Agent": omahaUserAgent() },
    body: JSON.stringify(body),
    redirect: "follow",
    signal: withTimeout(60_000, signal),
  });
  if (!res.ok) throw new Error(`Widevine update check HTTP ${res.status}`);
  let raw = await res.text();
  if (raw.startsWith(")]}'")) raw = raw.includes("\n") ? raw.slice(raw.indexOf("\n") + 1) : raw.slice(4); // strip XSSI guard
  return JSON.parse(raw);
}

/** Pull [downloadUrl, sha256, version] from an Omaha JSON response (pipelines or classic shape). */
export function parseUpdate(resp: any): [string, string, string] {
  const app = resp?.response?.app?.[0];
  const uc = app?.updatecheck;
  if (!uc || uc.status !== "ok") throw new Error(`Widevine update check status: ${uc?.status}`);
  for (const pl of uc.pipelines || []) {
    for (const op of pl.operations || []) {
      const urls = op.urls || [];
      const out = op.out || {};
      if (urls.length && out.sha256) return [urls[0].url, out.sha256, app.nextversion || uc.nextversion || ""];
    }
  }
  const base = uc.urls?.url?.[0]?.codebase;
  const pkg = uc.manifest?.packages?.package?.[0];
  if (base && pkg?.name) return [base.replace(/\/+$/, "") + "/" + pkg.name, pkg.hash_sha256 || "", uc.manifest?.version || ""];
  throw new Error("could not find a CDM download URL in the update response");
}

/** A CRX3 file is 'Cr24' + u32 version + u32 headerLen + header + zip. Return the zip bytes. */
export function crx3ToZip(buf: Buffer): Buffer {
  if (buf.subarray(0, 4).toString("latin1") !== "Cr24") return buf; // already a plain zip
  if (buf.length < 12) throw new Error("malformed CRX3 (truncated header)");
  const headerLen = buf.readUInt32LE(8);
  if (12 + headerLen > buf.length) throw new Error("malformed CRX3 (header overruns buffer)");
  return buf.subarray(12 + headerLen);
}

function cdmBinary(verDir: string): string {
  const { subdir, filename } = cdmPlatform();
  return path.join(verDir, "_platform_specific", subdir, filename);
}

/** A complete CDM directory: its manifest and this OS's CDM binary are both there. */
function validCdm(verDir: string | undefined): boolean {
  return !!verDir && existsSync(cdmBinary(verDir)) && existsSync(path.join(verDir, "manifest.json"));
}

function versionKey(name: string): number[] {
  // "current" (no version in the update response) sorts below any real version
  return /^\d+(\.\d+)*$/.test(name) ? [1, ...name.split(".").map(Number)] : [0];
}

function compareVersions(a: string, b: string): number {
  const ka = versionKey(a);
  const kb = versionKey(b);
  for (let i = 0; i < Math.max(ka.length, kb.length); i++) {
    const d = (ka[i] ?? -1) - (kb[i] ?? -1);
    if (d) return d;
  }
  return 0;
}

/** The newest complete CDM in the cache (`root`, default the cache root), or undefined. No network. */
export function cachedCdm(root: string = cacheRoot()): string | undefined {
  let names: string[];
  try {
    names = readdirSync(root).filter((n) => !n.startsWith("."));
  } catch {
    return undefined;
  }
  for (const name of names.sort(compareVersions).reverse()) {
    const dir = path.join(root, name);
    if (validCdm(dir)) return dir;
  }
  return undefined;
}

/** Whether Google was asked for a newer CDM less than CHECK_INTERVAL_MS ago. */
function stampFresh(root: string): boolean {
  try {
    const age = Date.now() - statSync(path.join(root, STAMP_FILE)).mtimeMs;
    return age > -3600_000 && age < CHECK_INTERVAL_MS; // a stamp from the future (clock moved back) is not fresh
  } catch {
    return false;
  }
}

function touchStamp(root: string): void {
  try {
    writeFileSync(path.join(root, STAMP_FILE), String(Math.floor(Date.now() / 1000)));
  } catch {
    /* the next launch asks again */
  }
}

/** Remove a tree, never throwing (a Windows scanner can hold a file in it for a moment; the next install sweeps it). */
function removeQuietly(p: string): void {
  try {
    rmSync(p, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  } catch {
    /* swept by the next install */
  }
}

/** Remove what an interrupted install left in the cache root (only under the install lock). */
function sweepLeftovers(root: string): void {
  let names: string[] = [];
  try {
    names = readdirSync(root);
  } catch {
    return;
  }
  for (const n of names) if (n.startsWith(".tmp-") || n.startsWith(".trash-")) removeQuietly(path.join(root, n));
}

/**
 * `root`/<version>, once `version` looks like a CDM version (`4.10.2830.0`). The version comes from Google's
 * update response: anything else (`../victim`, an absolute path) is refused before it can name, or delete, a
 * directory. No version at all means `current`.
 */
function versionDir(root: string, version: string): string {
  if (!version) return path.join(root, "current");
  if (!VERSION_RE.test(String(version))) {
    throw new Error(`Widevine update response named an unexpected version ${JSON.stringify(String(version).slice(0, 80))} — refusing to use it`);
  }
  return path.join(root, version);
}

/**
 * Download, verify and install one CDM version into `root`/<version>. Holds the SDK's install lock
 * (`root`/.install-lock, the protocol in download.ts) so concurrent first launches take turns, extracts
 * into `root`/.tmp-<id> and renames the finished tree into place: a reader never sees a half-extracted CDM.
 * The renames are download.ts's, which retry while a Windows scanner holds a file in the fresh tree.
 */
async function install(root: string, url: string, sha256: string, version: string, opts: WidevineOptions): Promise<string> {
  const verDir = versionDir(root, version);
  mkdirSync(root, { recursive: true });
  const lock = await acquireInstallLock(root, { timeoutMs: opts.lockTimeoutMs ?? LOCK_WAIT_MS, quiet: true });
  try {
    if (validCdm(verDir)) return verDir; // another process installed it while we waited
    sweepLeftovers(root);
    log(opts.quiet, `fetching CDM ${version || "(latest)"}`);
    const res = await fetch(url, { headers: { "User-Agent": omahaUserAgent() }, redirect: "follow", signal: withTimeout(120_000, opts.signal) });
    if (!res.ok) throw new Error(`Widevine CDM download HTTP ${res.status}`);
    const blob = Buffer.from(await res.arrayBuffer());
    if (createHash("sha256").update(blob).digest("hex") !== sha256.toLowerCase()) {
      throw new Error("Widevine CDM sha256 mismatch — refusing to install");
    }
    const tmp = path.join(root, `.tmp-${randomBytes(8).toString("hex")}`);
    try {
      mkdirSync(tmp);
      const zipPath = path.join(tmp, ".cdm.zip");
      writeFileSync(zipPath, crx3ToZip(blob));
      await extract(zipPath, { dir: tmp });
      rmSync(zipPath, { force: true });
      // some CRX layouts nest the platform dir differently; surface a clear error
      if (!validCdm(tmp)) throw new Error(`extracted CDM but ${cdmPlatform().filename} not at ${cdmBinary(verDir)}`);
      if (existsSync(verDir)) { // an incomplete tree (an older SDK extracted in place): move it aside
        const trash = path.join(root, `.trash-${randomBytes(8).toString("hex")}`);
        renameDir(verDir, trash);
        removeQuietly(trash);
      }
      renameDir(tmp, verDir);
    } finally {
      removeQuietly(tmp);
    }
  } finally {
    await lock.release();
  }
  log(opts.quiet, `installed: ${verDir}`);
  return verDir;
}

/**
 * Download + verify the current-OS x64 Widevine CDM into `dest` (default CLEARCOTE_WIDEVINE_DIR, else
 * ~/.clearcote/WidevineCdm) as <version>/. Returns the versioned CDM directory (manifest.json +
 * _platform_specific/<subdir>/<filename>).
 *
 * Always asks Google's update server for the latest version; the download is skipped when that version is
 * already installed. Installs take the install lock and land with a rename. A version that does not look
 * like one is refused.
 */
export async function fetchWidevine(opts: WidevineOptions = {}): Promise<string> {
  const [url, sha256, version] = parseUpdate(await postJson(OMAHA_URL, omahaRequestBody(), opts.checkSignal ?? opts.signal));
  const root = opts.dest || cacheRoot();
  const verDir = versionDir(root, version);
  if (validCdm(verDir)) {
    touchStamp(root);
    log(opts.quiet, `already present: ${verDir}`);
    return verDir;
  }
  // The CDM is a NATIVE library loaded into the browser process — never install it unverified. A
  // missing hash in the update response is a hard failure, not a skip (checked before downloading).
  if (!sha256) throw new Error("Widevine update response had no sha256 — refusing to install an unverified CDM");
  const dir = await install(root, url, sha256, version, opts);
  touchStamp(root);
  return dir;
}

/**
 * The CDM directory a launch should use, network only when needed: the cached CDM when Google was asked
 * within the last day (no network), otherwise {@link fetchWidevine}. When that fails (offline, a firewall,
 * Google refusing the request) the CDM already on disk still works and is used, and the failed check is
 * recorded (the stamp is touched), so no launch asks again for a day. Rejects only when there is no CDM.
 */
export async function resolveCdm(opts: WidevineOptions = {}): Promise<string> {
  const root = opts.dest || cacheRoot();
  const cached = cachedCdm(root);
  if (cached && stampFresh(root)) return cached;
  try {
    return await fetchWidevine({ ...opts, dest: root });
  } catch (e) {
    if (cached) {
      touchStamp(root);
      return cached;
    }
    throw e;
  }
}

/**
 * `"explicit"`, `"default"` or `"off"` for a launch. `option` (the `widevine` launch option) wins: true ->
 * explicit, false -> off. Unset follows CLEARCOTE_WIDEVINE (1/true/yes/on -> explicit, 0/false/no/off ->
 * off). Otherwise a licensed launch (a licence key resolved, which selects the PRO build) gets the default
 * and a free launch stays off. Same rules in the Python and .NET SDKs.
 */
export function widevineMode(option: boolean | undefined | null, licensed: boolean, env: NodeJS.ProcessEnv = process.env): WidevineMode {
  if (option !== undefined && option !== null) return option ? "explicit" : "off";
  const value = String(env[WIDEVINE_ENV] ?? "").trim().toLowerCase();
  if (TRUTHY.includes(value)) return "explicit";
  if (FALSY.includes(value)) return "off";
  return licensed ? "default" : "off";
}

/** How long a default launch waits for a first download: CLEARCOTE_WIDEVINE_TIMEOUT_SECONDS clamped to 1-600, else 15
 * (an unparsable or non-positive value too). */
export function timeoutSeconds(env: NodeJS.ProcessEnv = process.env): number {
  const raw = String(env[TIMEOUT_ENV] ?? "").trim();
  const value = raw === "" ? NaN : Number(raw);
  if (!(value > 0)) return DEFAULT_TIMEOUT_SECONDS; // also NaN
  return Math.min(Math.max(value, MIN_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS);
}

// The default path's process state: the first download in flight (one at a time), the background refresh of
// a stale cached CDM (one at a time), and when a first download last failed (a monotonic clock).
let defaultJob: { promise: Promise<{ ok: string } | { err: unknown }>; controller: AbortController; done: boolean } | undefined;
let refreshJob: { promise: Promise<void>; controller: AbortController } | undefined;
let defaultFailedAt: number | undefined;

/** Tests: forget the in-flight fetch, the refresh and the last failure (and stop what is running). */
export function resetWidevineDefaultState(): void {
  defaultJob?.controller.abort();
  refreshJob?.controller.abort();
  defaultJob = undefined;
  refreshJob = undefined;
  defaultFailedAt = undefined;
}

/** Tests: the background refresh in flight, if any. */
export function widevineRefreshInFlight(): Promise<void> | undefined {
  return refreshJob?.promise;
}

/** Tests: pretend the last failure was this long ago. */
export function ageLastWidevineFailure(byMs: number): void {
  if (defaultFailedAt !== undefined) defaultFailedAt -= byMs;
}

function shortReason(e: unknown): string {
  const err = e as { name?: string; message?: string };
  const text = String(err?.message ?? e ?? "").trim().split("\n")[0].slice(0, 160);
  const name = err?.name ?? "Error";
  return text ? `${name}: ${text}` : name;
}

function warnDefault(reason: string, quiet?: boolean): void {
  warnOnce(WARN_CODE,
    `Widevine is on by default for licensed launches, but the CDM could not be loaded (${reason}); ` +
      "launching without it. Set CLEARCOTE_WIDEVINE=0 (or pass widevine: false) to stop trying.",
    quiet);
}

/** Ask Google whether a newer CDM exists, with nobody waiting: one at a time, quiet. The update check is bounded
 * (REFRESH_CHECK_TIMEOUT_MS), so a blackholed network holds a finishing process that long at most, once a day:
 * {@link resolveCdm} records a failed check. */
function refreshInBackground(): void {
  if (refreshJob) return;
  const controller = new AbortController();
  const job: { promise: Promise<void>; controller: AbortController } = {
    controller,
    promise: resolveCdm({ quiet: true, signal: controller.signal, checkSignal: withTimeout(REFRESH_CHECK_TIMEOUT_MS, controller.signal) })
      .then(() => undefined, () => undefined)
      .finally(() => { if (refreshJob === job) refreshJob = undefined; }),
  };
  refreshJob = job;
}

/**
 * The CDM for a launch that did not ask for Widevine (the licensed default), or undefined.
 *
 * A CDM in the cache is used AT ONCE, with no network on the launch path; when its last update check is over a
 * day old, a background refresh asks Google for a newer one (used from the next launch on). Only with nothing
 * cached does the launch wait for a download, at most `timeoutMs` (default {@link timeoutSeconds}). FAIL-SOFT:
 * never rejects. When there is no CDM to use (the download failed or did not finish in time) it warns once per
 * process (unless quiet) and answers undefined, so the launch goes on without DRM; a download still running at
 * the deadline is cancelled (it cannot keep the process alive), and a failed one is not retried for
 * RETRY_AFTER_MS in this process. The fetches here are quiet: the default path prints nothing else.
 */
export async function defaultCdm(opts: { quiet?: boolean; timeoutMs?: number } = {}): Promise<string | undefined> {
  const root = cacheRoot();
  const cached = cachedCdm(root);
  if (cached) {
    if (!stampFresh(root)) refreshInBackground();
    return cached;
  }
  if (defaultFailedAt !== undefined && performance.now() - defaultFailedAt < RETRY_AFTER_MS) return undefined;
  const waitMs = opts.timeoutMs ?? timeoutSeconds() * 1000;
  if (!defaultJob || defaultJob.done) {
    const controller = new AbortController();
    const started: NonNullable<typeof defaultJob> = { controller, done: false, promise: Promise.resolve({ err: undefined }) };
    started.promise = resolveCdm({ quiet: true, signal: controller.signal, lockTimeoutMs: waitMs })
      .then((ok) => ({ ok }), (err: unknown) => ({ err }))
      .finally(() => { started.done = true; });
    defaultJob = started;
  }
  const job = defaultJob;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<"timeout">((resolve) => { timer = setTimeout(() => resolve("timeout"), waitMs); });
  try {
    const r = await Promise.race([job.promise, deadline]);
    if (r === "timeout") {
      defaultFailedAt = performance.now();
      job.controller.abort(); // never keep the process alive for a launch that went on without it
      warnDefault(`not ready within ${waitMs / 1000} s`, opts.quiet);
      return undefined;
    }
    if ("err" in r) {
      defaultFailedAt = performance.now();
      warnDefault(shortReason(r.err), opts.quiet);
      return undefined;
    }
    return r.ok;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Make a persistent profile load the fetched CDM: copy it under <userDataDir>/WidevineCdm/<version>/
 * and write the component hint file the engine reads. Fetches the CDM first if needed
 * ({@link resolveCdm}: the cached one when it is current).
 */
export async function seedWidevine(userDataDir: string, opts: WidevineOptions = {}): Promise<string> {
  const src = await resolveCdm(opts);
  const version = path.basename(src);
  const wvRoot = path.join(userDataDir, "WidevineCdm");
  const target = path.join(wvRoot, version);
  const { subdir, filename } = cdmPlatform();
  if (!existsSync(path.join(target, "_platform_specific", subdir, filename))) {
    mkdirSync(wvRoot, { recursive: true });
    cpSync(src, target, { recursive: true });
  }
  try {
    writeFileSync(path.join(wvRoot, HINT_FILE), JSON.stringify({ Path: target }));
  } catch {
    /* hint file is Linux-only; harmless if it can't be written */
  }
  log(opts.quiet, `seeded into ${wvRoot}`);
  return target;
}

/**
 * `ignoreDefaultArgs` with Playwright's `--disable-component-update` dropped from the launch, so the
 * component updater runs (it registers a seeded CDM or CRLSet). A list gets the flag added, `undefined`
 * becomes `["--disable-component-update"]`, and `true` (ignore ALL defaults) already un-suppresses it and
 * is returned as is. Pure. Also used by the CRLSet seeding (./crlset.ts).
 */
export function unsuppressComponentUpdate(ignoreDefaultArgs: string[] | boolean | undefined): string[] | boolean | undefined {
  if (Array.isArray(ignoreDefaultArgs)) {
    return ignoreDefaultArgs.includes("--disable-component-update")
      ? ignoreDefaultArgs
      : [...ignoreDefaultArgs, "--disable-component-update"];
  }
  return ignoreDefaultArgs === undefined ? ["--disable-component-update"] : ignoreDefaultArgs;
}

/**
 * Adjust launch args so the engine actually registers the seeded CDM. Pure — returns the new values.
 *
 * The un-suppress of the component updater (Playwright disables it by default via
 * `--disable-component-update`) applies on BOTH platforms: `ignoreDefaultArgs` may be a list,
 * `undefined`, or the boolean form Playwright accepts (`true` = ignore ALL defaults, so the updater
 * is already un-suppressed); only the list/undefined forms need `--disable-component-update` added.
 *
 * The `--component-updater=fast-update` startup scan is for Windows and macOS: on Linux the seeded CDM
 * hint file IS the registration mechanism (read at startup regardless), so no scan flag is added there.
 * Verified: with these, requestMediaKeySystemAccess + createMediaKeys succeed.
 */
export function widevineArgs(
  ignoreDefaultArgs: string[] | boolean | undefined,
  userArgs: string[],
): { ignoreDefaultArgs: string[] | boolean | undefined; args: string[] } {
  const ida = unsuppressComponentUpdate(ignoreDefaultArgs);
  // Force the pre-installed-component scan on Windows and macOS. On Linux the hint file registers the CDM.
  const args =
    process.platform === "linux" || userArgs.some((a) => a.includes("component-updater"))
      ? userArgs
      : [...userArgs, "--component-updater=fast-update"];
  return { ignoreDefaultArgs: ida, args };
}

/**
 * `["--widevine-cdm-path=<the fetched CDM>"]` when the engine implements that switch (patch 1022),
 * else `[]`. Such an engine registers the CDM at startup in every profile, Playwright's throwaway ones
 * included, so DRM no longer depends on the component updater; the hint file {@link seedWidevine}
 * writes keeps older engines working. Best-effort, like the rest of DRM: any failure answers `[]` and
 * the launch goes on.
 */
export async function widevineCdmArgs(exe: string | undefined, opts: { quiet?: boolean } = {}): Promise<string[]> {
  try {
    if (!engineSupportsSwitch(exe, "widevine-cdm-path")) return [];
    return [`--widevine-cdm-path=${await resolveCdm({ quiet: opts.quiet })}`];
  } catch {
    return [];
  }
}

function callerSetCdmPath(userArgs: readonly string[] | undefined): boolean {
  return (userArgs ?? []).some((a) => String(a).split("=", 1)[0] === "--widevine-cdm-path");
}

/**
 * The Widevine switch for one launch, by {@link widevineMode}. explicit: {@link widevineCdmArgs} (the
 * behaviour `widevine: true` always had). default: the same switch, only on an engine that implements it
 * (r32+; an older engine keeps the old default, off), with the CDM from {@link defaultCdm} (bounded,
 * fail-soft, warns once). off: nothing. A `--widevine-cdm-path` the caller put in `args` is theirs:
 * nothing is added then. Works for every launch path: such an engine loads the CDM without a profile,
 * incognito included.
 */
export async function widevineLaunchArgs(
  exe: string | undefined, option: boolean | undefined | null, licensed: boolean, userArgs: readonly string[] | undefined,
  opts: { quiet?: boolean; timeoutMs?: number } = {},
): Promise<string[]> {
  const mode = widevineMode(option, licensed);
  if (mode === "off" || callerSetCdmPath(userArgs)) return [];
  if (mode === "explicit") return widevineCdmArgs(exe, { quiet: opts.quiet });
  if (!engineSupportsSwitch(exe, "widevine-cdm-path")) return [];
  const cdm = await defaultCdm(opts);
  return cdm ? [`--widevine-cdm-path=${cdm}`] : [];
}
