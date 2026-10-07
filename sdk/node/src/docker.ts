// launch() on macOS: run the Clearcote Docker image and connect to it.
//
// There is no native Clearcote build for macOS. Rather than fail there, launch() starts the published
// Clearcote image (teamflatearth/clearcote:sdk-<this SDK's version>, a linux/amd64 image Docker Desktop
// runs on Intel and Apple silicon alike), waits for its CDP endpoint, connects with Playwright and
// resolves to the same Playwright Browser a local launch does. close() disconnects and stops the
// container; nothing is left running.
//
// When it applies:
//   - `docker: false` (or CLEARCOTE_DOCKER=0) turns it off: launch() behaves as before, which on macOS
//     means "pass executablePath to a compatible binary".
//   - `docker: true` (or CLEARCOTE_DOCKER=1) turns it on on any OS that has Docker.
//   - Unset: on for macOS, unless the caller named a binary (executablePath / CLEARCOTE_BINARY).
//   - CLEARCOTE_TEST_ONLY_ASSUME_MACOS=1 makes this module treat the host as macOS, so the real Docker
//     path can be exercised end to end on a Linux or Windows machine. It is not an option.
//
// The container is configured through the image's own CC_* variables (docker/serve.py), so only the
// options the image understands are accepted; anything else throws naming it. Values travel in the
// docker CLI's environment (`-e NAME` without a value), never on its command line. The licence key and
// the proxy URL are not passed as variables at all: they are copied in as a file serve.py reads once and
// deletes, so `docker inspect` does not show them (anyone who can reach the Docker daemon can still read a
// running container's files and memory). The CDP port is published on 127.0.0.1 only.
//
// What an image understands is read from its serve-protocol label before a container is created. An image
// older than sdk-0.40.0 has none: it gets the key and proxy as plain variables, with a warning. Whatever the
// image, launch() checks the container's log once the browser answers and refuses -- stopping and removing the
// container -- unless it runs the licensed engine when a key was given and the proxy when one was given.
//
// Nothing outlives its owner: the container is created with --rm, stops itself once no CDP client has been
// connected for 30 s after its CDP first answers (CLEARCOTE_DOCKER_IDLE_EXIT), and the next launch removes
// any whose owner process is certainly gone (sweepStale). close() stops it at once.
// Mirrors _docker.py in the Python SDK.

import { execFile, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, readlinkSync, rmSync, statSync, writeFileSync } from "node:fs";
import http from "node:http";
import { randomBytes } from "node:crypto";
import { homedir, hostname } from "node:os";
import { delimiter, join } from "node:path";
import { chromium } from "playwright-core";
import type { Browser } from "playwright-core";
import { installHumanize, installHumanizeOnContext } from "./humanize.js";
import { resolveLicenseKey } from "./license.js";

export const DEFAULT_REPOSITORY = "teamflatearth/clearcote";
export const TEST_ONLY_ASSUME_MACOS = "CLEARCOTE_TEST_ONLY_ASSUME_MACOS";
const LABEL = "com.clearcotelabs.sdk-launch=1";
// Who started a container: sweepStale() removes the ones whose owner process is gone.
const OWNER_HOST_LABEL = "com.clearcotelabs.owner-host";
const OWNER_TOKEN_LABEL = "com.clearcotelabs.owner-token";
// The largest pid an owner record can name: no system has larger ones, and process.kill() throws
// ERR_INVALID_ARG_TYPE beyond it (which pidAlive() would read as "gone").
const PID_MAX = 2 ** 31 - 1;
// The image's serve protocol (docker/serve.py's SERVE_PROTOCOL, carried as this image label). 2: takes
// CC_SECRETS_FILE and CC_IDLE_EXIT_SECONDS and logs a serve-state line. An image without the label is older.
const PROTOCOL_LABEL = "com.clearcotelabs.serve-protocol";
export const SERVE_PROTOCOL = 2;
const FIRST_PROTOCOL_TAG = "sdk-0.40.0"; // the first published image that speaks it
// A container stops itself once no CDP client has been connected for this long (serve.py's
// CC_IDLE_EXIT_SECONDS). CLEARCOTE_DOCKER_IDLE_EXIT overrides it; 0 turns it off.
const DEFAULT_IDLE_EXIT_S = 30;
const DEFAULT_CACHE_VOLUME = "clearcote-cache";
// The licence key and the proxy URL (it may carry a password) reach the container as this file, copied in
// with `docker cp` and deleted by serve.py once read, not as -e variables `docker inspect` would show.
const SECRET_ENV = ["CLEARCOTE_LICENSE_KEY", "CC_PROXY"];
const SECRETS_FILE = "/tmp/clearcote-secrets.json";
const IMAGE_UID = 10001; // the image's user (cc)
const TRUTHY = ["1", "true", "yes", "on"];
const FALSY = ["0", "false", "no", "off"];
// The image starts a virtual display, Chromium and (with a licence) fetches the licensed engine on its
// first run: well past Playwright's 30 s connect default, so the wait for CDP gets its own budget.
const READY_TIMEOUT_MS = 180_000;
const CONNECT_TIMEOUT_MS = 120_000;
const INSTALL_URL = "https://docs.docker.com/desktop/setup/install/mac-install/";
const OFF_HINT = "To launch without Docker, pass docker: false (or set CLEARCOTE_DOCKER=0) and executablePath to a compatible Clearcote binary.";

const SDK_VERSION: string = (() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return "unknown";
  }
})();

/** launch() needs Docker on this host (macOS has no native Clearcote build) and it is missing or not running. */
export class DockerUnavailableError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DockerUnavailableError";
  }
}

/** The Docker options of launch(). */
export interface DockerOption {
  /** Run Clearcote in its Docker image. Unset: on macOS only (there is no native macOS build), unless
   * executablePath / CLEARCOTE_BINARY names a binary. false (or CLEARCOTE_DOCKER=0) turns it off;
   * true uses the image on any OS. */
  docker?: boolean;
  /** The image to run. Default CLEARCOTE_DOCKER_IMAGE, else teamflatearth/clearcote:sdk-<SDK version>. */
  dockerImage?: string;
}

// launch() option -> the image's CC_* variable.
const STR_ENV: Record<string, string> = {
  fingerprint: "CC_FINGERPRINT", platform: "CC_PLATFORM", platformVersion: "CC_PLATFORM_VERSION",
  brand: "CC_BRAND", brandVersion: "CC_BRAND_VERSION", gpuVendor: "CC_GPU_VENDOR", gpuRenderer: "CC_GPU_RENDERER",
  timezone: "CC_TIMEZONE", acceptLanguage: "CC_ACCEPT_LANGUAGE", webrtcIp: "CC_WEBRTC_IP", webrtcMdns: "CC_WEBRTC_MDNS",
  tlsProfile: "CC_TLS_PROFILE", version: "CC_VERSION",
};
const INT_ENV: Record<string, string> = {
  hardwareConcurrency: "CC_HARDWARE_CONCURRENCY", deviceMemory: "CC_DEVICE_MEMORY", colorDepth: "CC_COLOR_DEPTH",
  maxTouchPoints: "CC_MAX_TOUCH_POINTS", storageQuota: "CC_STORAGE_QUOTA",
};
const FLOAT_ENV: Record<string, string> = { devicePixelRatio: "CC_DEVICE_PIXEL_RATIO" };
const BOOL_ENV: Record<string, string> = {
  lightStealth: "CC_LIGHT_STEALTH", disableGpuFingerprint: "CC_DISABLE_GPU_FINGERPRINT",
  fingerprintNoise: "CC_FINGERPRINT_NOISE", canvasNoise: "CC_CANVAS_NOISE", gpuStringSpoof: "CC_GPU_STRING_SPOOF",
};
const SPECIAL = ["location", "fingerprintProfile", "headless", "proxy", "args", "licenseKey", "licenseApiBase"];
const SDK_SIDE = ["timeout", "slowMo", "humanize", "showCursor", "quiet", "docker", "dockerImage", "ephemeralProfile"];
export const DOCKER_ACCEPTED: readonly string[] = [
  ...Object.keys(STR_ENV), ...Object.keys(INT_ENV), ...Object.keys(FLOAT_ENV), ...Object.keys(BOOL_ENV), ...SPECIAL, ...SDK_SIDE,
];

export function hostIsMacos(): boolean {
  return process.platform === "darwin" || (process.env[TEST_ONLY_ASSUME_MACOS] ?? "").trim() === "1";
}

/** Whether this launch runs in the Clearcote Docker image (see the header). */
export function dockerRequested(docker: unknown, options: Record<string, unknown> = {}): boolean {
  if (docker !== undefined && docker !== null) {
    if (typeof docker === "string") return TRUTHY.includes(docker.trim().toLowerCase()); // "0" from a config file is off
    return !!docker;
  }
  if (options.executablePath) return false; // the caller named a binary: theirs to run
  const env = (process.env.CLEARCOTE_DOCKER ?? "").trim().toLowerCase();
  if (FALSY.includes(env)) return false;
  if (TRUTHY.includes(env)) return true;
  if (process.env.CLEARCOTE_BINARY) return false;
  return hostIsMacos();
}

export function defaultImage(): string {
  return process.env.CLEARCOTE_DOCKER_IMAGE || `${DEFAULT_REPOSITORY}:sdk-${SDK_VERSION}`;
}

/** A proxy's username or password escaped for its URL as the Python and .NET SDKs do it (quote(safe="") and
 * Uri.EscapeDataString): everything but A-Z a-z 0-9 - . _ ~. encodeURIComponent alone leaves ! ' ( ) * as they are. */
const escapeLogin = (s: string) => encodeURIComponent(s).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`);

function proxyUrl(proxy: unknown): string {
  if (typeof proxy === "string") return proxy;
  const p = proxy as { server?: string; username?: string; password?: string } | null;
  if (!p || !p.server) throw new TypeError("proxy must be a URL or { server, username?, password? }");
  const i = p.server.indexOf("://");
  const scheme = i < 0 ? "http" : p.server.slice(0, i);
  let rest = i < 0 ? p.server : p.server.slice(i + 3);
  if (p.username || p.password) rest = `${escapeLogin(p.username ?? "")}:${escapeLogin(p.password ?? "")}@${rest}`;
  return `${scheme}://${rest}`;
}

/** The CC_* (and licence) variables the image is started with. Throws naming the first option the
 * image cannot take. */
export function containerEnv(options: Record<string, unknown>): Record<string, string> {
  const unknown = Object.keys(options).filter((k) => !DOCKER_ACCEPTED.includes(k) && options[k] !== undefined && options[k] !== null).sort();
  if (unknown.length) {
    const takes = DOCKER_ACCEPTED.filter((k) => !SDK_SIDE.includes(k)).sort().join(", ");
    throw new TypeError(`${unknown[0]} is not available when launch() runs Clearcote in Docker (there is no native macOS build). The container takes: ${takes}. ${OFF_HINT}`);
  }
  const env: Record<string, string> = {};
  const has = (k: string) => options[k] !== undefined && options[k] !== null;
  for (const [k, name] of Object.entries(STR_ENV)) {
    if (!has(k)) continue;
    if (typeof options[k] === "boolean") throw new TypeError(`${k}: ${options[k]} is not available when launch() runs Clearcote in Docker`);
    env[name] = String(options[k]);
  }
  for (const [k, name] of Object.entries(INT_ENV)) if (has(k)) env[name] = String(Math.trunc(Number(options[k])));
  for (const [k, name] of Object.entries(FLOAT_ENV)) if (has(k)) env[name] = String(Number(options[k]));
  for (const [k, name] of Object.entries(BOOL_ENV)) if (has(k)) env[name] = options[k] ? "1" : "0"; // false is a value
  if (has("location")) env.CC_LOCATION = Array.isArray(options.location) ? options.location.join(",") : String(options.location);
  if (has("fingerprintProfile")) {
    const prof = options.fingerprintProfile;
    if (typeof prof === "object") env.CC_FINGERPRINT_PROFILE = JSON.stringify(prof);
    else if (typeof prof === "string" && existsSync(prof) && statSync(prof).isFile()) env.CC_FINGERPRINT_PROFILE = readFileSync(prof, "utf8"); // a path on THIS machine
    else env.CC_FINGERPRINT_PROFILE = String(prof);
  }
  if (options.headless === true) env.CC_HEADLESS = "1"; // unset or false: the image's default, headed on its own display
  if (options.proxy) env.CC_PROXY = proxyUrl(options.proxy);
  const args = options.args as string[] | undefined;
  if (args?.length) {
    const bad = args.find((a) => !String(a) || /\s/.test(String(a)));
    if (bad !== undefined) throw new TypeError(`args ${JSON.stringify(bad)}: an argument with whitespace cannot be passed to the Docker image`);
    env.CC_EXTRA_ARGS = args.join(" ");
  }
  const key = resolveLicenseKey(options.licenseKey as string | undefined); // as a local launch: option > env > saved key
  if (key) env.CLEARCOTE_LICENSE_KEY = key;
  if (options.licenseApiBase) env.CLEARCOTE_LICENSE_API = String(options.licenseApiBase);
  return env;
}

// ── the docker CLI ─────────────────────────────────────────────────────────────────────────────────

export interface CliResult { code: number; stdout: string; stderr: string }

/** A starting container's log, kept in memory (it is created with --rm: once stopped, its log is gone). The
 * lines saying what the entrypoint applied (serve-state, engine, proxy) are kept apart in `marks`, whatever
 * Chrome logs after them. */
export interface LogTail { text(waitMs?: number): Promise<string>; waitMarks(pattern: RegExp, waitMs?: number): Promise<string[]>; stop(): void }

const MARK = /\[clearcote\] (serve-state |engine: |proxy: )/;

function findOnPath(name: string): string | null {
  const exts = process.platform === "win32" ? (process.env.PATHEXT ?? ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH ?? "").split(delimiter)) {
    if (!dir) continue;
    for (const ext of exts) {
      const p = join(dir, name + ext.toLowerCase());
      try { if (statSync(p).isFile()) return p; } catch { /* not here */ }
    }
  }
  return null;
}

function followLogs(exe: string, id: string): LogTail {
  const lines: string[] = [];
  const marks: string[] = [];
  let buf = "";
  let child: ReturnType<typeof spawn> | null = null;
  let done: Promise<void> = Promise.resolve();
  try {
    child = spawn(exe, ["logs", "--follow", id], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
    const onData = (c: Buffer) => {
      buf += c.toString("utf8");
      const parts = buf.split(/\r?\n/);
      buf = parts.pop() ?? "";
      lines.push(...parts);
      marks.push(...parts.filter((l) => MARK.test(l)));
      if (lines.length > 40) lines.splice(0, lines.length - 40);
    };
    child.stdout!.on("data", onData);
    child.stderr!.on("data", onData);
    done = new Promise((r) => { child!.once("close", () => r()); child!.once("error", () => r()); });
  } catch { /* no log, then */ }
  return {
    async text(waitMs = 3000) {
      // the container is gone: the follower ends with its last line
      await Promise.race([done, new Promise((r) => setTimeout(r, waitMs))]);
      return [...lines, buf].filter(Boolean).join("\n");
    },
    async waitMarks(pattern, waitMs = 10_000) {
      const deadline = Date.now() + waitMs;
      while (Date.now() < deadline && !marks.some((m) => pattern.test(m))) await new Promise((r) => setTimeout(r, 50));
      return [...marks];
    },
    stop() { if (child && child.exitCode === null) child.kill(); },
  };
}

/** The docker CLI. Seams: tests replace `which`, `run` and `followLogs`. */
export const dockerCli = {
  which: (): string | null => findOnPath("docker"),
  run: (argv: string[], env?: Record<string, string>, timeoutMs = 120_000, input?: Buffer): Promise<CliResult> =>
    new Promise((resolve) => {
      const child = execFile(argv[0], argv.slice(1), { env: env ? { ...process.env, ...env } : process.env, timeout: timeoutMs, maxBuffer: 16 << 20, windowsHide: true },
        (error, stdout, stderr) => {
          const e = error as (NodeJS.ErrnoException & { code?: number | string; killed?: boolean }) | null;
          const code = !e ? 0 : typeof e.code === "number" ? e.code : e.killed ? 124 : 127;
          resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") || (e && !stderr ? e.message : "") });
        });
      if (input !== undefined) child.stdin?.end(input);
    }),
  followLogs,
};

const firstLine = (t: string): string => (t ?? "").split(/\r?\n/).map((l) => l.trim()).find(Boolean) ?? "";

/** The docker executable, or DockerUnavailableError saying what to do. */
export async function checkDocker(): Promise<string> {
  const exe = dockerCli.which();
  if (!exe) {
    throw new DockerUnavailableError(
      `Clearcote has no native macOS build, so on macOS launch() runs it in Docker, but the \`docker\` command was not found. Install Docker Desktop (${INSTALL_URL}), start it, and try again. ${OFF_HINT}`);
  }
  const r = await dockerCli.run([exe, "info", "--format", "{{.ServerVersion}}"], undefined, 30_000);
  if (r.code !== 0) {
    throw new DockerUnavailableError(
      `Clearcote has no native macOS build, so on macOS launch() runs it in Docker, but Docker is not running (\`docker info\`: ${firstLine(r.stderr) || `exit ${r.code}`}). Start Docker Desktop and try again. ${OFF_HINT}`);
  }
  return exe;
}

/** How long a launched container waits with no CDP client before it stops itself: CLEARCOTE_DOCKER_IDLE_EXIT
 * (seconds, 0 = never), default 30. */
export function idleExitSeconds(): number {
  const raw = (process.env.CLEARCOTE_DOCKER_IDLE_EXIT ?? "").trim();
  if (!raw) return DEFAULT_IDLE_EXIT_S;
  if (!/^\d+$/.test(raw)) throw new TypeError(`CLEARCOTE_DOCKER_IDLE_EXIT=${JSON.stringify(raw)} is not a whole number of seconds`);
  return Number(raw);
}

/** The named volume a licensed container keeps its engine in (shared, so it downloads once):
 * CLEARCOTE_DOCKER_CACHE_VOLUME, default clearcote-cache. */
export function cacheVolume(): string {
  return (process.env.CLEARCOTE_DOCKER_CACHE_VOLUME ?? "").trim() || DEFAULT_CACHE_VOLUME;
}

export function pidAlive(pid: number): boolean {
  if (!(pid > 0)) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return (e as NodeJS.ErrnoException).code === "EPERM"; // exists, but not ours to signal
  }
}

// ── who owns a container ───────────────────────────────────────────────────────────────────────────
// A container carries a random per-process token (OWNER_TOKEN_LABEL); the owner writes a record under
// ~/.clearcote/docker-owners/<token>.json saying which process it is: pid, a start marker that changes when
// the pid is reused, its host name and (Linux) its PID namespace and boot id. A sweeper only judges a record it
// can verify completely: one in its own home, written on this host, from this boot and PID namespace -- every
// one of those fields as this process would write it, so a record written on another OS into a shared home (no
// namespace or boot id), in a container given the Docker socket (another namespace), next to Windows in WSL2,
// or on another machine is left alone, and so is a pid that is not a positive 32-bit number. When in doubt,
// nothing is removed: the container's idle exit still stops it. The record format is shared by the Python, Node
// and .NET SDKs, so any of them can sweep any other's containers.

const readText = (path: string): string | null => {
  try { return readFileSync(path, "utf8").trim(); } catch { return null; }
};
/** What this host says about processes: /proc where there is one, `ps` elsewhere. Seams: tests replace members. */
export const hostProbe = {
  platform: (): string => process.platform,
  readText,
  bootId: (): string | null => readText("/proc/sys/kernel/random/boot_id"),
  pidNamespace: (): string | null => {
    try { return readlinkSync("/proc/self/ns/pid"); } catch { return null; }
  },
  spawnSync: spawnSync as typeof spawnSync,
};
const bootId = () => hostProbe.bootId();
const pidNamespace = () => hostProbe.pidNamespace();

/** `ps -o lstart=` prints local time, in the locale's words. Pinned, so an owner and a sweeper that run with
 * different TZ / LANG settings (or one script that changes TZ between launches) read the same string for the
 * same process. Without it a sweeper in another time zone took a live owner for a reused pid. */
export const PS_ENV = { TZ: "UTC0", LC_ALL: "C" } as const;

/** A marker that differs once `pid` belongs to another process (it was reused), or null when it cannot be
 * read here. Linux: the start time from /proc; macOS: `ps -o lstart=` in UTC and the C locale; Windows: not
 * readable from Node (null: the pid alone is then trusted, which only ever keeps a container). The same
 * strings in all three SDKs. */
export function processStart(pid: number): string | null {
  const stat = hostProbe.readText(`/proc/${pid}/stat`);
  if (stat) {
    const f = stat.slice(stat.lastIndexOf(")") + 2).split(/\s+/);
    return f[19] ? `linux:${f[19]}` : null;
  }
  if (hostProbe.platform() === "win32") return null;
  try {
    const r = hostProbe.spawnSync("ps", ["-o", "lstart=", "-p", String(pid)],
      { encoding: "utf8", timeout: 10_000, env: { ...process.env, ...PS_ENV } });
    const out = String(r.stdout ?? "").split(/\s+/).filter(Boolean).join(" ");
    return r.status === 0 && out ? `ps-utc:${out}` : null;
  } catch {
    return null;
  }
}

const ownersDir = () => join(homedir(), ".clearcote", "docker-owners");
let ownerTokenValue: string | null = null;

/** This process's owner token, recording it (once) under ~/.clearcote/docker-owners/. */
export function ownerToken(): string {
  if (ownerTokenValue) return ownerTokenValue;
  const token = randomBytes(16).toString("hex");
  const record = { token, pid: process.pid, start: processStart(process.pid), pidns: pidNamespace(), boot: bootId(), host: hostname(), sdk: "node" };
  const path = join(ownersDir(), `${token}.json`);
  try {
    mkdirSync(ownersDir(), { recursive: true });
    writeFileSync(path, JSON.stringify(record));
    process.once("exit", () => { try { rmSync(path, { force: true }); } catch { /* best-effort */ } });
  } catch { /* no record: other launches leave its containers alone, and idle exit still ends them */ }
  ownerTokenValue = token;
  return token;
}

/** Forget this process's token (tests: a new HOME gets a new record). */
export function resetOwnerToken(): void { ownerTokenValue = null; }

type OwnerRecord = { pid?: unknown; start?: string | null; pidns?: string | null; boot?: string | null; host?: unknown };

/** The fields an owner record from this process would carry besides its pid and start marker. */
export const ownerHere = () => ({ host: hostname(), boot: bootId(), pidns: pidNamespace() });

const markerKind = (m: unknown) => (typeof m === "string" && m.includes(":") ? m.slice(0, m.indexOf(":")) : null);

/** true or false for the owner a record describes, or null when that cannot be told for certain here: a record
 * whose host, boot id or PID namespace is not exactly what this process would record (missing ones included),
 * or whose pid is not a positive 32-bit integer (PID_MAX at most). A pid that is alive with a start marker of the
 * same kind but another value was reused (false); one whose marker cannot be compared counts as alive. */
export function ownerAlive(rec: OwnerRecord): boolean | null {
  const here = ownerHere();
  if (!here.host || typeof rec.host !== "string" || rec.host.toLowerCase() !== here.host.toLowerCase()) return null;
  if ((rec.boot ?? null) !== here.boot || (rec.pidns ?? null) !== here.pidns) return null;
  if (typeof rec.pid !== "number" || !Number.isInteger(rec.pid) || rec.pid <= 0 || rec.pid > PID_MAX) return null;
  if (!pidAlive(rec.pid)) return false;
  if (rec.start) {
    const now = processStart(rec.pid);
    if (now && markerKind(now) === markerKind(rec.start) && now !== rec.start) return false; // the pid was reused
  }
  return true;
}

function loadRecord(path: string): OwnerRecord | null {
  try {
    const rec = JSON.parse(readFileSync(path, "utf8"));
    return rec && typeof rec === "object" ? (rec as OwnerRecord) : null;
  } catch {
    return null;
  }
}

/**
 * Stop and remove containers an earlier launch left behind whose owner process is certainly gone (killed,
 * crashed): their token's record is in this home, from this boot and PID namespace, and its process no longer
 * exists. They would stop on their own after the idle period; this frees their licence seat now. Anything else
 * -- live owners, owners in another namespace or on another machine, containers without a token -- is left
 * alone. Resolves to the ids removed.
 */
export async function sweepStale(exe: string): Promise<string[]> {
  const fmt = `{{.ID}}\t{{.Label "${OWNER_TOKEN_LABEL}"}}`;
  const r = await dockerCli.run([exe, "ps", "-a", "--filter", `label=${LABEL}`, "--format", fmt], undefined, 30_000);
  const stale: string[] = [];
  const dead = new Set<string>();
  for (const line of r.code === 0 ? r.stdout.split(/\r?\n/) : []) {
    const parts = line.trim().split("\t");
    if (parts.length !== 2 || !/^[0-9a-f]{32}$/.test(parts[1])) continue; // no owner token (an older SDK): not ours to judge
    const path = join(ownersDir(), `${parts[1]}.json`);
    const rec = loadRecord(path);
    if (rec && ownerAlive(rec) === false) {
      stale.push(parts[0]);
      dead.add(path);
    }
  }
  if (stale.length) {
    await dockerCli.run([exe, "stop", "--time", "10", ...stale], undefined, 120_000);
    await dockerCli.run([exe, "rm", "-f", "-v", ...stale], undefined, 60_000);
  }
  try { // the records of owners that are certainly gone
    for (const name of readdirSync(ownersDir())) {
      const path = join(ownersDir(), name);
      const rec = name.endsWith(".json") ? loadRecord(path) : null;
      if (dead.has(path) || (rec && ownerAlive(rec) === false)) rmSync(path, { force: true });
    }
  } catch { /* no records */ }
  return stale;
}

const live = new Map<string, string>(); // container id -> docker executable, for the exit-time sweep
let sweepInstalled = false;

async function removeContainer(exe: string, id: string): Promise<void> {
  // stop first: SIGTERM lets the image release a licence seat. The container was created with --rm, so
  // stopping it removes it and its anonymous volume (the image declares VOLUME /opt/xdg-cache: ~0.5 GB);
  // rm -f -v is for a container that did not go away. A named volume is never removed.
  await dockerCli.run([exe, "stop", "--time", "10", id], undefined, 60_000);
  await dockerCli.run([exe, "rm", "-f", "-v", id], undefined, 60_000);
  live.delete(id);
}

function installSweep(): void {
  if (sweepInstalled) return;
  sweepInstalled = true;
  // A normal exit removes what a caller never closed ("exit" handlers must be synchronous). Ctrl-C, a kill
  // or a crash never runs this: the container's idle exit and the next launch's sweepStale cover those.
  process.once("exit", () => {
    for (const [id, exe] of live) {
      try { spawnSync(exe, ["rm", "-f", "-v", id], { timeout: 30_000, windowsHide: true }); } catch { /* best-effort */ }
    }
  });
}

function cdpReady(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const req = http.get({ host: "127.0.0.1", port, path: "/json/version", timeout: 2000 }, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => resolve(res.statusCode === 200 && body.includes("webSocketDebuggerUrl")));
    });
    req.on("timeout", () => req.destroy());
    req.on("error", () => resolve(false));
  });
}

async function publishedPort(exe: string, id: string): Promise<number> {
  const r = await dockerCli.run([exe, "port", id, "9222/tcp"], undefined, 30_000);
  for (const line of r.stdout.split(/\r?\n/)) {
    const m = /:(\d+)\s*$/.exec(line.trim());
    if (m) return Number(m[1]);
  }
  throw new Error(`could not read the container's published CDP port (${firstLine(r.stderr) || JSON.stringify(r.stdout)})`);
}

async function waitReady(exe: string, id: string, port: number, budgetMs: number, logs: LogTail): Promise<void> {
  const deadline = Date.now() + budgetMs;
  for (;;) {
    if (await cdpReady(port)) return;
    const st = await dockerCli.run([exe, "inspect", "-f", "{{.State.Running}}", id], undefined, 30_000);
    if (st.code !== 0 || st.stdout.trim() !== "true") { // stopped (and, with --rm, already gone)
      throw new Error(`the Clearcote container stopped before its browser came up:\n${(await logs.text()).trim() || "(it left no log)"}`);
    }
    if (Date.now() >= deadline) throw new Error(`the Clearcote container's CDP endpoint did not answer within ${Math.round(budgetMs / 1000)} s`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

const VOLUME_INIT_RACE = /volumes\/[^/\s]+\/_data\S*: (?:file exists|no such file)/i;
// A tag that is not there: Docker 29 says `failed to resolve reference "docker.io/...:tag": ...: not found`;
// older ones `manifest unknown` / `manifest for ... not found`. Docker 29 reports a registry it cannot reach with
// the same "failed to resolve reference" opening, so that phrase alone proves nothing: the network wordings are
// looked for first.
const NOT_PUBLISHED = /manifest unknown|manifest for .* not found|not found: manifest|pull access denied|repository does not exist|: not found\s*$/im;
const REGISTRY_UNREACHABLE = /dial tcp|no such host|i\/o timeout|connection refused|connection reset|network is unreachable|no route to host|TLS handshake timeout|context deadline exceeded|failed to do request|Client\.Timeout|temporary failure in name resolution|server misbehaving/i;

function imageFailed(image: string, code: number, stderr: string, what = "docker pull"): Error {
  const detail = stderr.trim() || `exit ${code}`;
  if (REGISTRY_UNREACHABLE.test(detail)) {
    return new Error(`could not download the Clearcote image ${image}: the registry did not answer (${firstLine(detail)}). Check this machine's network (and any proxy Docker itself needs), or set CLEARCOTE_DOCKER_IMAGE / dockerImage to an image that is already here (\`docker images ${DEFAULT_REPOSITORY}\`).`);
  }
  if (NOT_PUBLISHED.test(detail)) {
    const why = image === `${DEFAULT_REPOSITORY}:sdk-${SDK_VERSION}`
      ? `Each image is published a few minutes after its SDK release (it is built from the PyPI package of the same version), so a brand-new SDK can be ahead of its image: try again in a few minutes. To launch meanwhile, set CLEARCOTE_DOCKER_IMAGE to an earlier tag of ${DEFAULT_REPOSITORY} (${DEFAULT_REPOSITORY}:latest is the newest published one). An image older than ${FIRST_PROTOCOL_TAG} still works, but the licence key and the proxy reach it as container variables that \`docker inspect\` shows, and it does not stop on its own if this program dies; launch() says so when that happens.`
      : "Check the name, or unset CLEARCOTE_DOCKER_IMAGE / dockerImage to use the default.";
    return new Error(`the Clearcote image ${image} is not available (${firstLine(detail)}). ${why}`);
  }
  return new Error(`\`${what} ${image}\` failed: ${detail}\n(set CLEARCOTE_DOCKER_IMAGE or dockerImage to use another image)`);
}

/** The serve protocol `image` speaks (its PROTOCOL_LABEL; 0 for an image without it), pulling the image first
 * when it is not here. Known before a container exists, so the settings a container gets are always ones its
 * entrypoint understands. */
export async function imageProtocol(exe: string, image: string, quiet = false): Promise<number> {
  const fmt = "{{json .Config.Labels}}";
  let r = await dockerCli.run([exe, "image", "inspect", "--format", fmt, image], undefined, 60_000);
  if (r.code !== 0) {
    if (!quiet) process.stderr.write(`[clearcote] downloading the Clearcote Docker image ${image}\n`);
    const pull = await dockerCli.run([exe, "pull", "--platform", "linux/amd64", image], undefined, 1_800_000);
    if (pull.code !== 0) throw imageFailed(image, pull.code, pull.stderr);
    r = await dockerCli.run([exe, "image", "inspect", "--format", fmt, image], undefined, 60_000);
    if (r.code !== 0) throw imageFailed(image, r.code, r.stderr, "docker image inspect");
  }
  try {
    const labels = JSON.parse(r.stdout.trim() || "null") ?? {};
    const n = parseInt(String(labels[PROTOCOL_LABEL] ?? "0"), 10);
    return Number.isFinite(n) ? n : 0;
  } catch {
    return 0;
  }
}

function proxyHostPort(url: string | null | undefined): [string | null, number | null] {
  if (!url) return [null, null];
  try {
    const u = new URL(url.includes("://") ? url : `http://${url}`);
    return [u.hostname.toLowerCase().replace(/^\[|\]$/g, ""), u.port ? Number(u.port) : null];
  } catch {
    return [null, null];
  }
}

/** [scheme, whether the proxy URL carries a username or password, whether they are percent-escaped], read from the
 * URL as written, as the Python SDK reads it: the userinfo is what comes before the last '@' of the authority. (The
 * URL parser escapes a space, '^' or an inner '@' itself, which made a login written without escapes look escaped.) */
function proxyLogin(url: string): [string, boolean, boolean] {
  const i = url.indexOf("://");
  const rest = i < 0 ? url : url.slice(i + 3);
  const authority = rest.split(/[/?#]/, 1)[0];
  const at = authority.lastIndexOf("@");
  const info = at < 0 ? "" : authority.slice(0, at);
  return [i < 0 ? "http" : url.slice(0, i).toLowerCase(), info !== "" && info !== ":", info.includes("%")];
}

/** Why an image older than FIRST_PROTOCOL_TAG cannot take this proxy, or null. Its entrypoint drops the password
 * of an http(s) proxy (Chrome is then challenged and nothing answers: every request fails), and hands a SOCKS5
 * password to an engine switch only the licensed engine has -- as written in the URL, without decoding it, so a
 * username or password with characters that have to be escaped there arrives wrong. */
export function legacyProxyRefusal(image: string, proxyUrlValue: string | null | undefined, licensed: boolean): string | null {
  if (!proxyUrlValue) return null;
  const [scheme, creds, escaped] = proxyLogin(proxyUrlValue);
  if (!creds || (scheme.startsWith("socks5") && licensed && !escaped)) return null;
  const what = scheme.startsWith("http") ? "an HTTP proxy that needs a password"
    : !licensed ? "a SOCKS5 proxy that needs a password without a licence key (its open engine cannot log in)"
      : "a SOCKS5 proxy that needs a password with characters a URL has to escape (it would send them escaped)";
  return `the Clearcote image ${image} predates ${FIRST_PROTOCOL_TAG} and cannot use ${what}: every request through it would fail. Use ${DEFAULT_REPOSITORY}:${FIRST_PROTOCOL_TAG} or newer (the default image is), or a proxy that does not need a password.`;
}

/** What the container did not apply that was asked for (empty when all is well), from the lines its
 * entrypoint logged: serve-state (protocol 2) or, for an older image, its engine and proxy lines. */
export function verifyApplied(marks: string[], protocol: number, expect: { licensed: boolean; proxy?: string | null }): string[] {
  const problems: string[] = [];
  let engine: string | null;
  let proxy: string | null;
  let proxyAuth: unknown = null;
  if (protocol >= 2) {
    let state: { engine?: string; proxy?: string | null; proxy_auth?: unknown } | null = null;
    for (const line of marks) {
      const i = line.indexOf("serve-state ");
      if (i >= 0) { try { state = JSON.parse(line.slice(i + "serve-state ".length)); } catch { state = null; } }
    }
    if (!state || typeof state !== "object") return ["it did not report what it applied (no serve-state line)"];
    engine = state.engine ?? null;
    proxy = state.proxy ?? null;
    proxyAuth = state.proxy_auth ?? null;
  } else {
    const engineLine = marks.find((m) => m.includes("] engine: ")) ?? "";
    engine = engineLine.includes("(licensed)") && engineLine.includes("/pro-") ? "licensed" : engineLine ? "open" : null;
    const proxyLine = marks.find((m) => m.includes("] proxy: "));
    proxy = proxyLine ? proxyLine.slice(proxyLine.indexOf("] proxy: ") + "] proxy: ".length).trim() : null;
  }
  if (expect.licensed && engine !== "licensed") {
    problems.push(engine ? "a licence key was given, but it runs the open engine" : "a licence key was given, but it did not say which engine it runs");
  }
  if (expect.proxy) {
    const [wh, wp] = proxyHostPort(expect.proxy);
    const [gh, gp] = proxyHostPort(proxy);
    if (!proxy || gh !== wh || (wp && gp && wp !== gp)) {
      problems.push(proxy ? `it applied a different proxy (${proxy})` : "a proxy was given, but it did not apply it (its traffic would leave from the container's own address)");
    } else if (protocol >= 2 && proxyLogin(expect.proxy)[1] && proxyAuth !== "engine" && proxyAuth !== "relay") {
      problems.push("the proxy needs a password, but it did not say it can log in to it (every request through it would fail)");
    }
  }
  return problems;
}

/** A ustar archive of one file, as `docker cp -` reads it. */
export function tarOneFile(name: string, data: Buffer, opts: { uid: number; gid: number; mode: number }): Buffer {
  const header = Buffer.alloc(512, 0);
  const put = (s: string, off: number, len: number) => header.write(s, off, len, "ascii");
  const oct = (n: number, len: number) => n.toString(8).padStart(len - 1, "0") + "\0";
  put(name, 0, 100);
  put(oct(opts.mode, 8), 100, 8);
  put(oct(opts.uid, 8), 108, 8);
  put(oct(opts.gid, 8), 116, 8);
  put(oct(data.length, 12), 124, 12);
  put(oct(Math.floor(Date.now() / 1000), 12), 136, 12);
  put("        ", 148, 8); // checksum is computed with this field as spaces
  put("0", 156, 1); // a regular file
  put("ustar\0", 257, 6);
  put("00", 263, 2);
  let sum = 0;
  for (const b of header) sum += b;
  put(sum.toString(8).padStart(6, "0") + "\0 ", 148, 8);
  const pad = Buffer.alloc((512 - (data.length % 512)) % 512, 0);
  return Buffer.concat([header, data, pad, Buffer.alloc(1024, 0)]);
}

export interface DockerContainer { id: string; image: string; endpoint: string; serveProtocol: number }

/**
 * Start the image and wait for its CDP endpoint. Nothing is left running when this rejects.
 *
 * What the image does with its settings is known first (imageProtocol). An image of this SDK's protocol gets
 * --rm, an idle exit (it stops once no CDP client has been connected for idleExitSeconds(), counted from when
 * its CDP answers) and the licence key and proxy URL as a file copied in, not as -e variables `docker inspect`
 * would show. An older image gets plain variables -- they still work -- and a warning that they are visible
 * and that it will not stop on its own -- and is refused, before it starts, a proxy password it cannot answer
 * (legacyProxyRefusal). Either way, once the browser answers, the container's own log must show the licensed
 * engine when a key was given and the proxy when one was given, logged in to when it needs a password
 * (verifyApplied); otherwise it is stopped and removed and launch() rejects: a browser on the open engine, one
 * sending traffic direct, or one whose every request the proxy turns away is never handed back in their place. The container carries this process's owner token for sweepStale().
 */
export async function startContainer(options: Record<string, unknown>): Promise<DockerContainer & { exe: string }> {
  const env = containerEnv(options);
  const idle = idleExitSeconds();
  const exe = await checkDocker();
  await sweepStale(exe);
  const image = (options.dockerImage as string | undefined) || defaultImage();
  const protocol = await imageProtocol(exe, image, !!options.quiet);
  const secrets: Record<string, string> = {};
  for (const k of SECRET_ENV) if (env[k] !== undefined) { secrets[k] = env[k]; delete env[k]; }
  const expect = { licensed: secrets.CLEARCOTE_LICENSE_KEY !== undefined, proxy: secrets.CC_PROXY ?? null };
  if (protocol >= SERVE_PROTOCOL) {
    if (idle) env.CC_IDLE_EXIT_SECONDS = String(idle);
    if (Object.keys(secrets).length) env.CC_SECRETS_FILE = SECRETS_FILE;
  } else {
    const refusal = legacyProxyRefusal(image, expect.proxy, expect.licensed);
    if (refusal) throw new Error(refusal);
    if (!options.quiet) {
      process.stderr.write(`[clearcote] warning: the image ${image} predates ${FIRST_PROTOCOL_TAG}: `
        + (Object.keys(secrets).length ? "its licence key and proxy are passed as container variables, which `docker inspect` shows, and " : "")
        + `it will not stop on its own if this program dies (close() still stops it). Use ${DEFAULT_REPOSITORY}:${FIRST_PROTOCOL_TAG} or newer.\n`);
    }
    Object.assign(env, secrets); // plain variables: names on the command line, values in the CLI's environment
    for (const k of Object.keys(secrets)) delete secrets[k];
  }
  const argv = [exe, "create", "--rm", "--platform", "linux/amd64", "--shm-size", "1g", "-p", "127.0.0.1::9222",
    "--label", LABEL, "--label", `${OWNER_HOST_LABEL}=${hostname()}`, "--label", `${OWNER_TOKEN_LABEL}=${ownerToken()}`];
  for (const name of Object.keys(env).sort()) argv.push("-e", name); // the value comes from the CLI's environment
  if (expect.licensed) argv.push("-v", `${cacheVolume()}:/opt/xdg-cache`); // the licensed engine downloads once
  argv.push(image);
  if (!options.quiet) process.stderr.write(`[clearcote] no native macOS build: starting the Clearcote Docker image ${image}\n`);
  let r = await dockerCli.run(argv, env, 1_800_000);
  // Two launches creating their first container on a NEW shared volume at once collide in Docker's own volume
  // initialisation ("failed to mkdir .../volumes/<name>/_data/...: file exists"); the loser succeeds a moment
  // later, once the volume has been filled.
  for (let attempt = 1; attempt < 4 && r.code !== 0 && VOLUME_INIT_RACE.test(r.stderr); attempt++) {
    await new Promise((res) => setTimeout(res, 1000 * attempt));
    r = await dockerCli.run(argv, env, 1_800_000);
  }
  const id = r.stdout.trim().split(/\r?\n/).pop()?.trim() ?? "";
  if (r.code !== 0 || !id) throw imageFailed(image, r.code, r.stderr, "docker create");
  live.set(id, exe);
  installSweep();
  let logs: LogTail | null = null;
  try {
    if (Object.keys(secrets).length) {
      const tar = tarOneFile(SECRETS_FILE.split("/").pop()!, Buffer.from(JSON.stringify(secrets), "utf8"), { uid: IMAGE_UID, gid: IMAGE_UID, mode: 0o600 });
      const cp = await dockerCli.run([exe, "cp", "-", `${id}:${SECRETS_FILE.slice(0, SECRETS_FILE.lastIndexOf("/"))}`], undefined, 60_000, tar);
      if (cp.code !== 0) throw new Error(`could not copy the licence/proxy settings into the container: ${firstLine(cp.stderr)}`);
    }
    const st = await dockerCli.run([exe, "start", id], undefined, 120_000);
    if (st.code !== 0) throw new Error(`\`docker start\` failed: ${st.stderr.trim() || `exit ${st.code}`}`);
    logs = dockerCli.followLogs(exe, id);
    const port = await publishedPort(exe, id);
    const timeout = options.timeout as number | undefined;
    await waitReady(exe, id, port, timeout ? Math.max(timeout, 1000) : READY_TIMEOUT_MS, logs);
    if (protocol >= SERVE_PROTOCOL || expect.licensed || expect.proxy) {
      const wanted = protocol >= SERVE_PROTOCOL ? /serve-state / : expect.proxy ? /\] proxy: / : /\] engine: /;
      const problems = verifyApplied(await logs.waitMarks(wanted), protocol, expect);
      if (problems.length) {
        throw new Error(`the Clearcote container (${image}) did not apply what launch() asked for: ${problems.join("; ")}. It was stopped and removed.`);
      }
    }
    return { id, image, endpoint: `http://127.0.0.1:${port}`, serveProtocol: protocol, exe };
  } catch (e) {
    await removeContainer(exe, id);
    throw e;
  } finally {
    logs?.stop();
  }
}

function defaultNoViewport(browser: Browser): void {
  const origNewPage = browser.newPage.bind(browser);
  (browser as { newPage: unknown }).newPage = (o: Record<string, unknown> = {}) =>
    origNewPage(("viewport" in o ? o : { ...o, viewport: null }) as Parameters<typeof origNewPage>[0]);
  const origNewContext = browser.newContext.bind(browser);
  (browser as { newContext: unknown }).newContext = (o: Record<string, unknown> = {}) =>
    origNewContext(("viewport" in o ? o : { ...o, viewport: null }) as Parameters<typeof origNewContext>[0]);
}

/** `launch()` on macOS (see the header): the Playwright Browser of a Clearcote container, which also
 * carries `dockerContainer` ({ id, image, endpoint }). close() disconnects and stops the container. */
export async function launchDocker(options: Record<string, unknown>): Promise<Browser> {
  const container = await startContainer(options);
  let disconnect: (() => Promise<void>) | null = null;
  try {
    const connect: Record<string, unknown> = { timeout: CONNECT_TIMEOUT_MS };
    for (const k of ["timeout", "slowMo"]) if (options[k] != null) connect[k] = options[k];
    const browser = await chromium.connectOverCDP(container.endpoint, connect as { timeout?: number; slowMo?: number });
    const origClose = browser.close.bind(browser);
    disconnect = () => origClose();
    const info: DockerContainer = { id: container.id, image: container.image, endpoint: container.endpoint, serveProtocol: container.serveProtocol };
    Object.defineProperty(browser, "dockerContainer", { value: info, enumerable: false, configurable: true });
    (browser as { close: unknown }).close = async (...args: Parameters<Browser["close"]>) => {
      try {
        return await origClose(...args);
      } finally {
        await removeContainer(container.exe, container.id);
      }
    };
    // The window is real (on the image's virtual display, sized to the persona's screen): an emulated
    // viewport on top of it is the impossible-window tell a local launch avoids.
    defaultNoViewport(browser);
    const humanize = { humanize: options.humanize as boolean | undefined, showCursor: options.showCursor as boolean | undefined, seed: options.fingerprint as string | number | undefined };
    for (const ctx of browser.contexts()) installHumanizeOnContext(ctx, humanize, browser);
    installHumanize(browser, humanize);
    return browser;
  } catch (e) {
    if (disconnect) await disconnect().catch(() => {});
    await removeContainer(container.exe, container.id);
    throw e;
  }
}
