// Server-side personas (engine r51+): fetch the signed persona token for a launch and hand it to the engine.
//
// From r51 the engine derives no persona of its own. The launcher asks the persona service for one -- with the run
// token it just leased, the persona switches it is about to launch with (the *selector*) and the one fact of this
// machine the generator needs (the host GPU vendor) -- and passes the signed answer to the browser in
// CLEARCOTE_PERSONA_TOKEN, next to CLEARCOTE_RUN_TOKEN. Every launch gets one, persona or not: a selector without a
// seed yields the persona the engine computed for seed 0 up to r50, so a launch with no fingerprint behaves exactly
// as before. The engine checks the token's signature and that it was issued to this licence and lease for exactly
// these switches on this machine; it refuses to start without one.
//
// Engines before r51 ignore the variable and keep deriving personas themselves, so this module does nothing for
// them (engineWantsPersonaToken), and the SDK keeps working with either.
//
// A captured profile (fingerprintProfile) no longer rides on the command line: its exact JSON text goes to the
// service, which hashes it, generates from it and names the hash in the token's selector; the launch carries
// --fingerprint-profile-sha256=<hash> so the engine can bind the two.
//
// The selector list mirrors the Python SDK's (_persona.py), the engine's (components/ungoogled/persona_token.cc)
// and the service's (services/cc-persona/src/selector.mjs): a switch added to one must be added to the others.
import { spawnSync, type SpawnSyncOptionsWithStringEncoding, type SpawnSyncReturns } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { resolve as resolvePath } from "node:path";
import { gunzipSync, gzipSync } from "node:zlib";
import { assertClockFor } from "./clockcheck.js";
import { warmFiles } from "./download.js";
import { gpuMaker } from "./fingerprint.js";
import { engineSupportsSwitch } from "./launchopts.js";
import { LicenseError, licenseUserAgent } from "./license.js";
import { proxiedRequest } from "./net.js";
import { warnOnce } from "./warnings.js";
import { makeRecoveredCopy, recoveredExe } from "./winlaunch.js";

export const TOKEN_ENV = "CLEARCOTE_PERSONA_TOKEN";
export const TOKEN_GZ_ENV = "CLEARCOTE_PERSONA_TOKEN_GZ";
export const API_ENV = "CLEARCOTE_PERSONA_API"; // comma-separated base URLs; tests and self-hosting
export const MARKER_SWITCH = "cc-persona-host-facts"; // only an r51+ engine implements it
export const DEFAULT_HOSTS = ["https://persona.clearcotelabs.com", "https://persona-us.clearcotelabs.com"];
// Over this many bytes the token goes gzip + base64 in TOKEN_GZ_ENV (a Windows environment variable holds at most
// 32,767 characters; a macOS persona lists 180 voices).
export const ENV_INLINE_LIMIT = 30000;
const REQUEST_TIMEOUT_MS = 8000;
const SKEW_SEC = 60;

export const SELECTOR_SWITCHES: ReadonlySet<string> = new Set([
  "fingerprint", "fingerprint-platform", "fingerprint-brand", "fingerprint-brand-version",
  "fingerprint-schema", "fingerprint-gpu-vendor", "fingerprint-gpu-renderer", "fingerprint-gpu-draw",
  "fingerprint-gpu-backend-real", "fingerprint-hardware-concurrency", "fingerprint-device-pixel-ratio",
  "fingerprint-color-scheme", "fingerprint-reduced-motion", "fingerprint-webauthn", "fingerprint-bluetooth",
  "fingerprint-webgpu-vendor", "fingerprint-webgpu-architecture", "disable-gpu-string-spoof",
  "disable-gpu-fingerprint", "disable-fingerprint-voices", "canvas-bridge-url", "fingerprint-profile-id",
  "fingerprint-profile-sha256",
]);
// The switches the engine's host-GPU-vendor detection reads; the host-facts run carries exactly these.
const HOST_FACT_SWITCHES: ReadonlySet<string> = new Set([
  "headless", "disable-gpu", "enable-gpu", "use-gl", "use-angle", "use-adapter-luid", "canvas-bridge-url",
  "fingerprint-gpu-host-vendor",
]);
const PROFILE_SWITCH = "fingerprint-profile";
const PROFILE_SHA_SWITCH = "fingerprint-profile-sha256";

export type Env = Record<string, string | undefined>;
export type Selector = Record<string, string>;
export type HostFacts = { gpu_vendor: string };
export type Probe = (exe: string | undefined, name: string) => boolean;
export type FactsRunner = (cmd: string[], env: Env) => string;
export type Fetcher = (runToken: string, sel: Selector, host: HostFacts, profileJson: string | null, proxy: unknown) => Promise<{ token: string; exp: number }>;
/** What a lease must offer here; the SDK's LeaseSession does. */
export interface LeaseLike { readonly token: string | null; readonly leaseId?: string | null }

export class PersonaServiceError extends LicenseError {
  constructor(message: string) { super(message); this.name = "PersonaServiceError"; }
}

const tokens = new Map<string, { token: string; exp: number }>();
const facts = new Map<string, HostFacts>();

function parseSwitch(arg: string): [string, string] | null {
  if (!arg.startsWith("--") || arg.length === 2) return null;
  const eq = arg.indexOf("=");
  const name = eq < 0 ? arg.slice(2) : arg.slice(2, eq);
  return name ? [name, eq < 0 ? "" : arg.slice(eq + 1)] : null;
}

/** Whether `exe` is an r51+ engine, which starts only with a persona token. */
export function engineWantsPersonaToken(exe: string | undefined, supports: Probe = engineSupportsSwitch): boolean {
  return !!exe && supports(exe, MARKER_SWITCH);
}

/**
 * The selector the generator reads from `args` (last value wins, as Chromium keeps the last copy), the captured
 * profile's exact JSON text when --fingerprint-profile is present, and `args` with that switch removed and
 * --fingerprint-profile-sha256 added in its place.
 */
export function selectorFromArgs(args: readonly string[]): { sel: Selector; profileJson: string | null; args: string[] } {
  const sel: Selector = {};
  let profileJson: string | null = null;
  let out: string[] = [];
  for (const arg of args) {
    const s = parseSwitch(arg);
    if (s && s[0] === PROFILE_SWITCH) {
      let raw = Buffer.from(s[1], "base64");
      try { raw = gunzipSync(raw); } catch { /* an uncompressed profile */ }
      profileJson = raw.toString("utf8");
      continue;
    }
    if (s && SELECTOR_SWITCHES.has(s[0])) sel[s[0]] = s[0] === "canvas-bridge-url" ? "1" : s[1];
    out.push(arg);
  }
  if (profileJson !== null) {
    delete sel[PROFILE_SHA_SWITCH];
    out = out.filter((a) => parseSwitch(a)?.[0] !== PROFILE_SHA_SWITCH);
    const sha = createHash("sha256").update(profileJson, "utf8").digest("hex");
    sel[PROFILE_SHA_SWITCH] = sha;
    out.push(`--${PROFILE_SHA_SWITCH}=${sha}`);
  }
  return { sel, profileJson, args: out };
}

/**
 * The probe's stdout, or an error saying why there is none. The engine prints a refusal (no lease, a FREE token
 * without its file, the clock set back) on stderr only, and a binary that could not start has no output at all:
 * reading stdout alone turned every one of those into "Unexpected end of JSON input".
 */
export function describeProbe(r: { stdout?: string | null; stderr?: string | null; status: number | null; signal?: string | null; error?: Error }): string {
  const stdout = r.stdout ?? "";
  if (!r.error && r.status === 0 && stdout.trim()) return stdout;
  const said = (r.stderr ?? "").trim().split(/\r?\n/).filter(Boolean).pop()?.replace(/^\[clearcote\]\s*/, "");
  let why: string;
  if ((r.error as NodeJS.ErrnoException | undefined)?.code === "ETIMEDOUT") why = "it did not answer within 30 s";
  else if (r.error) why = `it could not be started: ${r.error.message}`;
  else if (r.signal) why = `it was stopped by ${r.signal}`;
  else if (r.status !== 0) why = `it exited with code ${r.status}`;
  else why = "it exited without output";
  throw new Error(said ? `${why}: ${said.slice(0, 300)}` : why);
}

/** Windows could not start the process ("spawnSync <path> UNKNOWN" / "side-by-side configuration is incorrect"). */
function winSpawnRace(r: SpawnSyncReturns<string>): boolean {
  if (!r.error) return false;
  return (r.error as NodeJS.ErrnoException).code === "UNKNOWN" || /side.by.side/i.test(r.error.message);
}

/**
 * spawnSync with winAvRetry's recovery: the probe runs before the launch on the same binary, so without it a build
 * Windows will not start in place failed the probe on every launch and the launch's own recovery was never reached.
 * From this build's recovered copy when one exists; else in place, then once from a new recovered copy.
 */
function spawnProbe(exe: string, args: string[], opts: SpawnSyncOptionsWithStringEncoding): SpawnSyncReturns<string> {
  if (process.platform !== "win32") return spawnSync(exe, args, opts);
  const ready = recoveredExe(exe);
  if (ready) {
    const r = spawnSync(ready, args, opts);
    if (!winSpawnRace(r)) return r;
  }
  const r = spawnSync(exe, args, opts);
  if (!winSpawnRace(r)) return r;
  try {
    return spawnSync(makeRecoveredCopy(exe, warmFiles), args, opts);
  } catch {
    return r; // the copy failed (disk full, locked files): report the first failure
  }
}

function defaultRunner(cmd: string[], env: Env): string {
  return describeProbe(spawnProbe(cmd[0], cmd.slice(1), { env: env as NodeJS.ProcessEnv, encoding: "utf8", timeout: 30000, windowsHide: true }));
}

function factsKey(exe: string, relevant: readonly string[]): string {
  try {
    const st = statSync(exe);
    return JSON.stringify([resolvePath(exe), st.size, Math.floor(st.mtimeMs / 1000), relevant]);
  } catch {
    return JSON.stringify([exe, 0, 0, relevant]);
  }
}

function relevantSwitches(args: readonly string[]): string[] {
  return args.filter((a) => { const s = parseSwitch(a); return !!s && HOST_FACT_SWITCHES.has(s[0]); }).sort();
}

/**
 * The facts of this machine the generator needs, as the engine itself reports them for a launch with these
 * switches: `chrome --cc-persona-host-facts` prints {"gpu_vendor": ...} and exits. Cached per (binary, the switches
 * that influence the answer) for the life of this process only.
 *
 * Up to 0.70.0 the answer was also kept on disk (persona-host-facts.json) and never checked again. The vendor is
 * that of the adapter Windows lists first, which changes with a driver install or a Remote Desktop session; a saved
 * "none" on a machine that now renders on NVIDIA then failed every launch with "generated for another host GPU
 * vendor". The probe takes about 0.1 s, so each process asks once.
 */
export function hostFacts(exe: string, args: readonly string[], runToken: string, runner: FactsRunner = defaultRunner, env: Env = process.env): HostFacts {
  const relevant = relevantSwitches(args);
  const key = factsKey(exe, relevant);
  const hit = facts.get(key);
  if (hit) return { ...hit };
  const probeEnv: Env = { ...env, CLEARCOTE_RUN_TOKEN: runToken }; // the probe runs after the licence gate, like every launch
  delete probeEnv[TOKEN_ENV];
  delete probeEnv[TOKEN_GZ_ENV];
  let out: HostFacts;
  try {
    const text = runner([exe, `--${MARKER_SWITCH}`, ...relevant], probeEnv);
    const lines = text.trim().split(/\r?\n/);
    const parsed = JSON.parse(lines[lines.length - 1]) as { gpu_vendor?: unknown };
    if (typeof parsed.gpu_vendor !== "string") throw new Error("no gpu_vendor");
    out = { gpu_vendor: parsed.gpu_vendor };
  } catch (e) {
    throw new PersonaServiceError(`the engine did not report its host facts (${(e as Error).message})`);
  }
  facts.set(key, out);
  return { ...out };
}

// The engine's refusal when the persona was generated for another GPU vendor than the one it renders on
// (pro-r51-02 Bind): "generated for another host GPU vendor (<token's>, this machine: <engine's>)". The second value
// is exactly what the probe reports ("none" for no hardware GPU), so it can stand in for a new probe.
const STALE_VENDOR = /generated for another host GPU vendor \(([^,()]*), this machine: ([A-Za-z0-9_.-]+)\)/;

/** [token's vendor, this machine's vendor] when `err` is the engine refusing a persona for another GPU vendor. */
export function staleHostVendor(err: unknown): [string, string] | null {
  const m = STALE_VENDOR.exec(String((err as Error)?.message ?? err));
  return m ? [m[1], m[2]] : null;
}

export function personaHosts(env: Env = process.env): string[] {
  const raw = (env[API_ENV] ?? "").trim();
  if (raw) return raw.split(",").map((h) => h.trim().replace(/\/+$/, "")).filter(Boolean);
  return DEFAULT_HOSTS;
}

/** POST the request to the first persona host that answers. */
export async function fetchPersonaToken(runToken: string, sel: Selector, host: HostFacts, profileJson: string | null = null, proxy: unknown = undefined, hosts: string[] = personaHosts(), timeoutMs = REQUEST_TIMEOUT_MS): Promise<{ token: string; exp: number }> {
  const body = JSON.stringify(profileJson === null ? { token: runToken, sel, host } : { token: runToken, sel, host, profile_json: profileJson });
  const failures: string[] = [];
  for (const base of hosts) {
    let res;
    try {
      res = await proxiedRequest(`${base}/v1/persona`, {
        method: "POST", body, timeoutMs, proxy: proxy as never,
        headers: { "content-type": "application/json", "User-Agent": licenseUserAgent() },
      });
    } catch (e) {
      failures.push(`${base}: ${(e as Error).message}`);
      continue;
    }
    let answer: { persona_token?: unknown; exp?: unknown; error?: unknown } = {};
    try { answer = (await res.json()) as typeof answer; } catch { /* not JSON */ }
    if (res.ok && typeof answer.persona_token === "string") return { token: answer.persona_token, exp: Number(answer.exp ?? 0) };
    const reason = typeof answer.error === "string" ? answer.error : `HTTP ${res.status}`;
    if (res.status >= 400 && res.status < 500) throw new PersonaServiceError(`the persona service refused this launch: ${reason}`);
    failures.push(`${base}: ${reason}`);
  }
  throw new PersonaServiceError(`no persona service answered (${failures.join("; ")}). The engine cannot start without a persona; check outbound HTTPS to persona.clearcotelabs.com.`);
}

/** A valid persona token for this lease + selector + host, from the cache or the service. */
export async function personaTokenFor(lease: LeaseLike, sel: Selector, host: HostFacts, profileJson: string | null, proxy: unknown, fetch: Fetcher = fetchPersonaToken): Promise<string> {
  const key = createHash("sha256").update(JSON.stringify([lease.leaseId ?? null, Object.fromEntries(Object.entries(sel).sort()), host])).digest("hex");
  const now = Date.now() / 1000;
  const hit = tokens.get(key);
  if (hit && hit.exp > now + SKEW_SEC) return hit.token;
  const got = await fetch(lease.token ?? "", sel, host, profileJson, proxy);
  tokens.set(key, got);
  return got.token;
}

/** `env` with the token the way the engine reads it (gzip + base64 when long). */
export function injectPersonaToken(env: Env, token: string): Env {
  const out = { ...env };
  delete out[TOKEN_ENV];
  delete out[TOKEN_GZ_ENV];
  if (token.length > ENV_INLINE_LIMIT) out[TOKEN_GZ_ENV] = gzipSync(Buffer.from(token, "ascii"), { level: 9 }).toString("base64");
  else out[TOKEN_ENV] = token;
  return out;
}

// ── GPU vendor guard ────────────────────────────────────────────────────────────────────────────────────────────
//
// Sites can send a seed, run seeded arithmetic on the GPU and compare what the computing GPU's class (its vendor and
// graphics backend, e.g. NVIDIA on Direct3D 11) returns with the WebGL renderer the page declares. A persona that names
// another vendor than the GPU that really computes ("the card that computes is not the declared graphics card"), or a
// graphics card's name over a software renderer (SwiftShader / WARP on Remote Desktop, a VM or a VPS), is caught by
// that check whatever the strings say. The engine matches the persona to this machine's vendor by itself on NVIDIA and
// Intel hosts; explicit gpuVendor / gpuRenderer, an imported profile, an AMD host or a software-rendering host can
// still end up with another vendor. After the persona token is fetched, the GPU it declares (the token's payload is
// the generator's own output) is compared with the vendor the engine reports for this machine at this launch (read
// fresh every process, see hostFacts), and a mismatch is said once per process. CLEARCOTE_NO_GPU_HOST_CHECK=1
// silences it (as do quiet and CLEARCOTE_NO_WARN).

export const GPU_CHECK_ENV = "CLEARCOTE_NO_GPU_HOST_CHECK";
// Launches whose page does not show the persona's GPU strings, or whose WebGL does not render on this machine's
// default adapter: the check has nothing to compare.
const GPU_CHECK_SKIP_SWITCHES: ReadonlySet<string> = new Set([
  "canvas-bridge-url", "use-adapter-luid", "disable-gpu-string-spoof", "disable-gpu-fingerprint", "fingerprint-passthrough",
]);
const HARDWARE_MAKERS: ReadonlySet<string> = new Set(["nvidia", "intel", "amd", "apple", "qualcomm", "arm"]);
const HOST_VENDOR_NAMES: Record<string, string> = { nvidia: "NVIDIA", intel: "Intel", amd: "AMD" };
const MAKER_NAMES: Record<string, string> = {
  nvidia: "NVIDIA", intel: "Intel", amd: "AMD", apple: "Apple", qualcomm: "Qualcomm", arm: "ARM",
};

export type PersonaGpu = { vendor: string; renderer: string };
/** Where the persona's GPU came from: the caller's gpuVendor / gpuRenderer, an imported profile, or the engine. */
export type GpuSource = "options" | "profile" | "engine";
export type GpuHostMismatch = { code: string; message: string };

/** The WebGL vendor and renderer a persona token declares (its payload `p` is the generator's codec JSON), or null. */
export function personaGpuFromToken(token: string): PersonaGpu | null {
  try {
    const payload = JSON.parse(Buffer.from(token.split(".")[0], "base64url").toString("utf8")) as
      { p?: { persona?: { webgl_vendor?: unknown; webgl_renderer?: unknown } } };
    const p = payload?.p?.persona;
    if (!p || typeof p.webgl_renderer !== "string" || !p.webgl_renderer) return null;
    return { vendor: typeof p.webgl_vendor === "string" ? p.webgl_vendor : "", renderer: p.webgl_renderer };
  } catch {
    return null;
  }
}

/** Where the GPU of a launch with this selector and profile comes from (the engine's own rule, persona_profile.cc). */
export function gpuSource(sel: Selector, profileJson: string | null): GpuSource {
  if (sel["fingerprint-gpu-vendor"] !== undefined || sel["fingerprint-gpu-renderer"] !== undefined) return "options";
  if (sel["fingerprint-profile-id"] !== undefined) return "profile";
  if (profileJson !== null) {
    try {
      const dbg = (JSON.parse(profileJson) as { webgl?: { webgl1?: { debug?: Record<string, unknown> } } })?.webgl?.webgl1?.debug;
      if (dbg && (typeof dbg.UNMASKED_VENDOR_WEBGL === "string" || typeof dbg.UNMASKED_RENDERER_WEBGL === "string")) return "profile";
    } catch { /* not JSON: the engine refuses it anyway */ }
  }
  return "engine";
}

/**
 * The mismatch between the GPU a persona declares and the one this machine renders on, or null when they agree or
 * the launch has nothing to compare. `host.gpu_vendor` is the engine's answer for this launch: "nvidia", "intel",
 * "amd", or "none" (no hardware adapter WebGL renders on: software rendering, a virtual adapter, Linux headless).
 * `args` are the launch's switches (a canvas bridge, an adapter chosen by LUID or the real GPU strings skip the check).
 */
export function gpuHostMismatch(persona: PersonaGpu | null, host: HostFacts, args: readonly string[], source: GpuSource,
  names: { vendor: string; renderer: string; spoof: string } = { vendor: "gpuVendor", renderer: "gpuRenderer", spoof: "gpuStringSpoof: false" }): GpuHostMismatch | null {
  if (!persona) return null;
  if (args.some((a) => { const s = parseSwitch(a); return !!s && GPU_CHECK_SKIP_SWITCHES.has(s[0]); })) return null;
  const maker = gpuMaker(persona.renderer) ?? gpuMaker(persona.vendor);
  if (!maker || !HARDWARE_MAKERS.has(maker)) return null;   // a software or unknown renderer claims no card
  const hv = String(host.gpu_vendor ?? "").toLowerCase();
  const claim = `${MAKER_NAMES[maker]} GPU ("${persona.renderer}")`;
  const why = "Sites that send a seed and run GPU arithmetic compare the card that computes with the declared " +
    "graphics card, and report the graphics as spoofed whatever the strings say.";
  const off = ` Set ${GPU_CHECK_ENV}=1 to silence this.`;
  const fix = source === "options"
    ? `Remove ${names.vendor} / ${names.renderer} (the persona then claims this machine's vendor), or set them to this machine's GPU.`
    : source === "profile"
      ? "Use a profile captured on a machine with the same GPU vendor as this one."
      : `This engine's persona pool has no matching GPU for this vendor yet: set ${names.vendor} and ${names.renderer} to this machine's GPU ` +
        `(what WebGL reports with ${names.spoof}).`;
  const from = source === "options" ? `${names.vendor} / ${names.renderer} declare` : source === "profile" ? "the imported profile declares" : "the persona declares";
  if (hv === "none") {
    return {
      code: `gpu-software-host:${maker}`,
      message: `this machine renders WebGL in software (no NVIDIA, Intel or AMD adapter: Remote Desktop, a VM or a VPS, ` +
        `or Linux headless without a GPU), but ${from} a ${claim}. Sites that run GPU arithmetic see a software renderer ` +
        `under a graphics card's name. Run on a machine with a GPU, use the canvas bridge, or pass ${names.spoof} to show ` +
        `the real renderer.${off}`,
    };
  }
  if (!HOST_VENDOR_NAMES[hv] || hv === maker) return null;
  return {
    code: `gpu-vendor-host:${maker}:${hv}`,
    message: `${from} a ${claim}, but this machine renders on ${HOST_VENDOR_NAMES[hv]}. ${why} ${fix}${off}`,
  };
}

/** Say a {@link gpuHostMismatch} once per process, unless quiet, CLEARCOTE_NO_WARN or CLEARCOTE_NO_GPU_HOST_CHECK. */
export function warnGpuHostMismatch(token: string, host: HostFacts, args: readonly string[], sel: Selector, profileJson: string | null,
  quiet?: boolean, env: Env = process.env): void {
  if (quiet || env.CLEARCOTE_NO_WARN || /^(1|true|yes|on)$/i.test((env[GPU_CHECK_ENV] ?? "").trim())) return;
  const m = gpuHostMismatch(personaGpuFromToken(token), host, args, gpuSource(sel, profileJson));
  if (m) warnOnce(m.code, m.message, quiet);
}

export interface PersonaLaunch {
  /** The launch args, with a captured profile replaced by its hash. */
  args: string[];
  /** `base` with the persona token added (or `base` itself when this engine needs none). */
  env(base: Env | undefined): Env | undefined;
  /**
   * After a launch the engine refused as "generated for another host GPU vendor": fetch a persona for the vendor the
   * engine named as this machine's, so the next `env()` carries it, and resolve true. False for any other error.
   */
  heal(err: unknown): Promise<boolean>;
}

/**
 * Prepare a launch of `exe`: on an r51+ engine with a lease, fetch the persona token; `env()` then adds it to each
 * attempt's environment (a retried launch rebuilds its env). Otherwise the args come back unchanged and `env()` is
 * the identity.
 */
export async function preparePersonaLaunch(exe: string, args: readonly string[], lease: LeaseLike | null | undefined, proxy: unknown = undefined,
  deps: { supports?: Probe; runner?: FactsRunner; fetch?: Fetcher; env?: Env; now?: () => number; quiet?: boolean } = {}): Promise<PersonaLaunch> {
  const l = lease?.token ? lease : leaseFromEnv(deps.env);
  if (!l?.token || !engineWantsPersonaToken(exe, deps.supports)) return { args: [...args], env: (b) => b, heal: async () => false };
  // A clock far off makes the engine refuse the token whatever the key: say so before the probe fails on it.
  assertClockFor(l.token, "licence token", deps.now?.());
  const { sel, profileJson, args: out } = selectorFromArgs(args);
  const host = hostFacts(exe, out, l.token, deps.runner, deps.env);
  let token = await personaTokenFor(l, sel, host, profileJson, proxy, deps.fetch);
  assertClockFor(token, "persona token", deps.now?.());
  warnGpuHostMismatch(token, host, out, sel, profileJson, deps.quiet);
  const heal = async (err: unknown): Promise<boolean> => {
    const stale = staleHostVendor(err);
    if (!stale) return false;
    const fixed = { gpu_vendor: stale[1] };
    facts.set(factsKey(exe, relevantSwitches(out)), fixed); // the rest of this process launches with it too
    process.stderr.write(`clearcote: the GPU this machine renders on changed (${stale[0]} -> ${stale[1]}); `
      + "fetched a persona for it and launching once more\n");
    token = await personaTokenFor(l, sel, fixed, profileJson, proxy, deps.fetch);
    warnGpuHostMismatch(token, fixed, out, sel, profileJson, deps.quiet);
    return true;
  };
  return { args: out, env: (b) => injectPersonaToken(b ?? { ...process.env }, token), heal };
}

/**
 * A lease-like view of a run token the CALLER put in the environment (CLEARCOTE_RUN_TOKEN, or _FILE): the hosted
 * gateway mints its own run tokens and hands them to serve() this way with no SDK lease. The lease id is read from
 * the token's payload without verifying it (the engine and the persona service verify; here it only keys the cache).
 */
export function leaseFromEnv(env: Env | undefined): LeaseLike | null {
  const e = env ?? process.env;
  let token = (e.CLEARCOTE_RUN_TOKEN ?? "").trim();
  if (!token && e.CLEARCOTE_RUN_TOKEN_FILE) {
    try { token = readFileSync(e.CLEARCOTE_RUN_TOKEN_FILE, "utf8").trim(); } catch { /* no file: no lease */ }
  }
  if (!token) return null;
  let leaseId: string | null = null;
  try {
    const body = token.split(".")[0];
    leaseId = (JSON.parse(Buffer.from(body, "base64url").toString("utf8")) as { lease?: unknown }).lease as string ?? null;
  } catch { /* junk token: still fetch, the service will refuse it */ }
  return { token, leaseId };
}

export function clearPersonaCaches(): void {
  tokens.clear();
  facts.clear();
}
