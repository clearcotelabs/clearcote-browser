// Fetching personas from the profile service, and caching them locally.
//
// Selection runs CLIENT-SIDE (see profilelib.ts) because it needs facts only this machine has:
// the real GPU, the real display, the engine's actual Chromium major. The server cannot see any
// of those, so it returns candidates for a described host and the SDK picks among them.
//
// The service caps DISTINCT profiles per license per day, and re-fetching one already held is
// free. That is why this module caches aggressively on disk: a sticky identity must resolve to
// the same profile every launch without spending budget or requiring the network.

import { existsSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { PROFILE_DIR } from "./profile.js";
import {
  selectProfile,
  gpuVendorClass,
  type HostFacts,
  type ProfileIndexEntry,
  type SelectOptions,
  type Selection,
} from "./profilelib.js";

const DEFAULT_API_BASE = "https://www.clearcotelabs.com";

function apiBase(base?: string): string {
  return (base || process.env.CLEARCOTE_PROFILE_API || process.env.CLEARCOTE_LICENSE_API || DEFAULT_API_BASE)
    .replace(/\/$/, "");
}

function cacheDir(): string {
  const d = join(PROFILE_DIR, "library");
  if (!existsSync(d)) mkdirSync(d, { recursive: true });
  return d;
}

export interface ProfileSourceOptions extends SelectOptions {
  licenseKey?: string;
  apiBase?: string;
  /** Skip the network entirely and use only what is already cached. */
  offline?: boolean;
}

/** Map a Node platform string onto the os_family values the corpus stores. */
export function hostOsFamily(): string {
  switch (process.platform) {
    case "win32": return "windows";
    case "darwin": return "macos";
    case "linux": return "linux";
    default: return process.platform;
  }
}

// ---------------------------------------------------------------------------
// Host facts
// ---------------------------------------------------------------------------
//
// The GPU and display can only be read by actually rendering something, so they come from a
// one-off no-persona launch. That is expensive (a browser start), so the result is cached on
// disk and keyed by the engine binary — a different build may report a different GL string.

// `_at` is an ISO time when this SDK wrote it and epoch seconds when the Python or .NET SDK did.
interface CachedHost extends HostFacts { _exe: string; _at: string | number; }

function hostCachePath(): string {
  return join(cacheDir(), "host.json");
}

/**
 * One cache key per BINARY, not per spelling of its path, computed as the Python SDK does it
 * (`os.path.normcase(os.path.realpath(exe))`) so both SDKs hit the same host.json entry: symlinks
 * resolved, and on Windows lower-cased with backslashes. A path that does not resolve keeps its
 * absolute form, as Python's non-strict realpath does.
 */
export function hostExeKey(exe: string): string {
  let p: string;
  try {
    p = realpathSync.native(exe);
  } catch {
    p = resolve(exe);
  }
  return process.platform === "win32" ? p.replace(/\//g, "\\").toLowerCase() : p;
}

/** `_at` as epoch milliseconds, from either format; NaN when unreadable (treated as expired). */
function cachedAtMs(v: unknown): number {
  if (typeof v === "number" && Number.isFinite(v)) return v * 1000;
  if (typeof v === "string" && v) return Date.parse(v);
  return Number.NaN;
}

export function readCachedHost(exe: string): HostFacts | null {
  try {
    const c = JSON.parse(readFileSync(hostCachePath(), "utf8")) as CachedHost;
    // Different binary: re-measure. The literal path, as older Node SDKs wrote it, is still honoured.
    if (c._exe !== hostExeKey(exe) && c._exe !== exe) return null;
    // Host hardware does not change often, but a monitor or driver swap does; a month is a
    // reasonable compromise between staleness and paying for a launch every run.
    const at = cachedAtMs(c._at);
    if (!(Date.now() - at <= 30 * 86_400_000)) return null;
    return c;
  } catch {
    return null;
  }
}

export function writeCachedHost(exe: string, facts: HostFacts): void {
  try {
    writeFileSync(hostCachePath(), JSON.stringify({ ...facts, _exe: hostExeKey(exe), _at: new Date().toISOString() }, null, 1));
  } catch {
    /* a cache write failure must never fail a launch */
  }
}

/** The in-page probe, as an expression string. Reads the REAL GPU and display — this launch
 *  applies no persona, so what it sees is the host itself. */
const PROBE_EXPR = `(() => {
  let renderer = "";
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    const dbg = gl && gl.getExtension("WEBGL_debug_renderer_info");
    if (gl && dbg) renderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL));
  } catch (e) { /* headless with no GPU: leave blank, scored as unknown rather than guessed */ }
  return {
    renderer: renderer,
    screen_width: screen.width,
    screen_height: screen.height,
    device_pixel_ratio: devicePixelRatio,
    hardware_concurrency: navigator.hardwareConcurrency,
    device_memory: navigator.deviceMemory,
  };
})()`;

interface ProbeResult {
  renderer: string;
  screen_width: number;
  screen_height: number;
  device_pixel_ratio: number;
  hardware_concurrency: number;
  device_memory?: number;
}

/**
 * Read the host's real GPU/display by rendering in the engine itself, with no persona applied.
 *
 * `browserMajor` is a FALLBACK only. The major is read from the running browser when possible,
 * because that is the only authoritative source: the caller may have pinned `version: "149"`,
 * set CLEARCOTE_BROWSER_VERSION, or supplied their own `executablePath`, and in all of those the
 * SDK's pinned RELEASE version describes a different binary than the one about to run. Filtering
 * profiles against the wrong major would silently produce a persona that contradicts the engine.
 */
export async function measureHost(
  launchFn: (opts: Record<string, unknown>) => Promise<{
    newContext: () => Promise<{ newPage: () => Promise<unknown> }>;
    version?: () => string;
    close: () => Promise<void>;
  }>,
  exe: string,
  browserMajor: number,
): Promise<HostFacts> {
  const cached = readCachedHost(exe);
  if (cached) return cached;

  const browser = await launchFn({ executablePath: exe, headless: true, quiet: true });
  try {
    const ctx = await browser.newContext();
    const page = (await ctx.newPage()) as {
      goto: (u: string, o?: unknown) => Promise<unknown>;
      evaluate: (expr: string) => Promise<ProbeResult>;
      close: () => Promise<void>;
    };
    // about:blank is enough: WebGL and screen do not need an origin.
    await page.goto("about:blank");
    // Passed as an EXPRESSION STRING, not a closure: this body runs in the browser, and typing
    // it here would mean adding the DOM lib to a Node-only SDK's tsconfig just to describe code
    // that never executes in Node.
    const facts = await page.evaluate(PROBE_EXPR);
    // The engine's OWN version wins over the caller's guess. Playwright reports it as
    // "150.0.7871.114"; fall back to the passed-in major if the shape is unexpected.
    let major = browserMajor;
    try {
      const v = typeof browser.version === "function" ? browser.version() : "";
      const m = String(v).match(/(\d+)\./);
      if (m) major = Number(m[1]);
    } catch {
      /* keep the fallback */
    }
    const out: HostFacts = {
      os_family: hostOsFamily(),
      browser_major: major,
      gpu_vendor: gpuVendorClass(facts.renderer),
      screen_width: facts.screen_width,
      screen_height: facts.screen_height,
      device_pixel_ratio: facts.device_pixel_ratio,
      hardware_concurrency: facts.hardware_concurrency,
      device_memory: facts.device_memory,
    };
    writeCachedHost(exe, out);
    return out;
  } finally {
    await browser.close().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Service client
// ---------------------------------------------------------------------------

function authHeaders(licenseKey: string | undefined): Record<string, string> {
  const key = licenseKey || process.env.CLEARCOTE_LICENSE_KEY || "";
  if (!key) {
    throw new Error(
      "the profile library needs a license key — pass licenseKey or set CLEARCOTE_LICENSE_KEY " +
        "(profiles are served to PRO licenses with an active subscription)",
    );
  }
  return { authorization: `Bearer ${key}` };
}

/** Candidate index for a described host. Cached briefly so repeated launches do not re-ask. */
export async function fetchIndex(host: HostFacts, opts: ProfileSourceOptions = {}): Promise<ProfileIndexEntry[]> {
  const os = (opts.os ?? host.os_family).toLowerCase();
  const cachePath = join(cacheDir(), `index-${os}-${host.browser_major}.json`);

  if (opts.offline) {
    if (!existsSync(cachePath)) {
      throw new Error(`offline: no cached profile index for ${os}/${host.browser_major}`);
    }
    return JSON.parse(readFileSync(cachePath, "utf8")) as ProfileIndexEntry[];
  }

  const url = `${apiBase(opts.apiBase)}/api/v1/profiles?os=${encodeURIComponent(os)}&major=${host.browser_major}`;
  const res = await fetch(url, { headers: authHeaders(opts.licenseKey) });
  if (!res.ok) {
    // Serve stale rather than fail the launch — a cached index is far better than no persona.
    if (existsSync(cachePath)) return JSON.parse(readFileSync(cachePath, "utf8")) as ProfileIndexEntry[];
    const body = await res.text().catch(() => "");
    throw new Error(`profile index request failed (${res.status}): ${body.slice(0, 200)}`);
  }
  const json = (await res.json()) as { candidates: ProfileIndexEntry[] };
  try { writeFileSync(cachePath, JSON.stringify(json.candidates)); } catch { /* non-fatal */ }
  return json.candidates ?? [];
}

/** Fetch one profile by id, preferring the on-disk copy — a repeat costs no distinct budget,
 *  but not spending a request at all is better still. */
export async function fetchProfile(
  id: string,
  opts: ProfileSourceOptions = {},
): Promise<Record<string, unknown>> {
  const cachePath = join(cacheDir(), `profile-${id}.json`);
  if (existsSync(cachePath)) {
    try { return JSON.parse(readFileSync(cachePath, "utf8")) as Record<string, unknown>; } catch { /* refetch */ }
  }
  if (opts.offline) throw new Error(`offline: profile ${id} is not cached`);

  const res = await fetch(`${apiBase(opts.apiBase)}/api/v1/profiles?id=${encodeURIComponent(id)}`, {
    headers: authHeaders(opts.licenseKey),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`profile fetch failed (${res.status}): ${body.slice(0, 200)}`);
  }
  const json = (await res.json()) as { profile: Record<string, unknown> };
  try { writeFileSync(cachePath, JSON.stringify(json.profile)); } catch { /* non-fatal */ }
  return json.profile;
}

/** Resolve a persona end to end: index -> score against this host -> fetch the winner. */
export async function resolveAutoProfile(
  host: HostFacts,
  opts: ProfileSourceOptions = {},
): Promise<{ profile: Record<string, unknown>; selection: Selection }> {
  const index = await fetchIndex(host, opts);
  const selection = selectProfile(index, host, opts);
  const profile = await fetchProfile(selection.entry.id, opts);
  return { profile, selection };
}
