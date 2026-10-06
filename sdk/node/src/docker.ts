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
// docker CLI's environment (`-e NAME` without a value), never on its command line, so a licence key or
// proxy password is not visible in the process list. The CDP port is published on 127.0.0.1 only.
// Mirrors _docker.py in the Python SDK.

import { execFile, spawnSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import http from "node:http";
import { delimiter, join } from "node:path";
import { chromium } from "playwright-core";
import type { Browser } from "playwright-core";
import { installHumanize, installHumanizeOnContext } from "./humanize.js";
import { resolveLicenseKey } from "./license.js";

export const DEFAULT_REPOSITORY = "teamflatearth/clearcote";
export const TEST_ONLY_ASSUME_MACOS = "CLEARCOTE_TEST_ONLY_ASSUME_MACOS";
const LABEL = "com.clearcotelabs.sdk-launch=1";
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

function proxyUrl(proxy: unknown): string {
  if (typeof proxy === "string") return proxy;
  const p = proxy as { server?: string; username?: string; password?: string } | null;
  if (!p || !p.server) throw new TypeError("proxy must be a URL or { server, username?, password? }");
  const i = p.server.indexOf("://");
  const scheme = i < 0 ? "http" : p.server.slice(0, i);
  let rest = i < 0 ? p.server : p.server.slice(i + 3);
  if (p.username || p.password) rest = `${encodeURIComponent(p.username ?? "")}:${encodeURIComponent(p.password ?? "")}@${rest}`;
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

/** The docker CLI. Seams: tests replace `which` and `run`. */
export const dockerCli = {
  which: (): string | null => findOnPath("docker"),
  run: (argv: string[], env?: Record<string, string>, timeoutMs = 120_000): Promise<CliResult> =>
    new Promise((resolve) => {
      execFile(argv[0], argv.slice(1), { env: env ? { ...process.env, ...env } : process.env, timeout: timeoutMs, maxBuffer: 16 << 20, windowsHide: true },
        (error, stdout, stderr) => {
          const e = error as (NodeJS.ErrnoException & { code?: number | string; killed?: boolean }) | null;
          const code = !e ? 0 : typeof e.code === "number" ? e.code : e.killed ? 124 : 127;
          resolve({ code, stdout: String(stdout ?? ""), stderr: String(stderr ?? "") || (e && !stderr ? e.message : "") });
        });
    }),
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

const live = new Map<string, string>(); // container id -> docker executable, for the exit-time sweep
let sweepInstalled = false;

async function removeContainer(exe: string, id: string): Promise<void> {
  // stop first: SIGTERM lets the image release a licence seat; rm then deletes what is left. -v takes the
  // container's anonymous volume with it: the image declares VOLUME /opt/xdg-cache, so every container gets
  // one holding a copy of the engine (~0.5 GB) that `rm` alone leaves behind. A named volume
  // (clearcote-cache, mounted when licensed) is never removed by -v.
  await dockerCli.run([exe, "stop", "--time", "10", id], undefined, 60_000);
  await dockerCli.run([exe, "rm", "-f", "-v", id], undefined, 60_000);
  live.delete(id);
}

function installSweep(): void {
  if (sweepInstalled) return;
  sweepInstalled = true;
  // "exit" handlers must be synchronous: a container a caller never closed is removed outright.
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

async function waitReady(exe: string, id: string, port: number, budgetMs: number): Promise<void> {
  const deadline = Date.now() + budgetMs;
  for (;;) {
    if (await cdpReady(port)) return;
    const st = await dockerCli.run([exe, "inspect", "-f", "{{.State.Running}}", id], undefined, 30_000);
    if (st.code !== 0 || st.stdout.trim() !== "true") {
      const logs = await dockerCli.run([exe, "logs", "--tail", "25", id], undefined, 30_000);
      throw new Error(`the Clearcote container stopped before its browser came up:\n${(logs.stdout + logs.stderr).trim()}`);
    }
    if (Date.now() >= deadline) throw new Error(`the Clearcote container's CDP endpoint did not answer within ${Math.round(budgetMs / 1000)} s`);
    await new Promise((r) => setTimeout(r, 250));
  }
}

export interface DockerContainer { id: string; image: string; endpoint: string }

/** Start the image and wait for its CDP endpoint. Nothing is left running when this rejects. */
export async function startContainer(options: Record<string, unknown>): Promise<DockerContainer & { exe: string }> {
  const env = containerEnv(options);
  const exe = await checkDocker();
  const image = (options.dockerImage as string | undefined) || defaultImage();
  const argv = [exe, "run", "-d", "--platform", "linux/amd64", "--shm-size", "1g", "-p", "127.0.0.1::9222", "--label", LABEL];
  for (const name of Object.keys(env).sort()) argv.push("-e", name); // the value comes from the CLI's environment
  if (env.CLEARCOTE_LICENSE_KEY) argv.push("-v", "clearcote-cache:/opt/xdg-cache"); // the licensed engine downloads once
  argv.push(image);
  if (!options.quiet) process.stderr.write(`[clearcote] no native macOS build: starting the Clearcote Docker image ${image} (the first run downloads it)\n`);
  const r = await dockerCli.run(argv, env, 1_800_000);
  const id = r.stdout.trim().split(/\r?\n/).pop()?.trim() ?? "";
  if (r.code !== 0 || !id) {
    throw new Error(`\`docker run ${image}\` failed: ${r.stderr.trim() || `exit ${r.code}`}\n(set CLEARCOTE_DOCKER_IMAGE or dockerImage to use another image)`);
  }
  live.set(id, exe);
  installSweep();
  try {
    const port = await publishedPort(exe, id);
    const timeout = options.timeout as number | undefined;
    await waitReady(exe, id, port, timeout ? Math.max(timeout, 1000) : READY_TIMEOUT_MS);
    return { id, image, endpoint: `http://127.0.0.1:${port}`, exe };
  } catch (e) {
    await removeContainer(exe, id);
    throw e;
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
    const info: DockerContainer = { id: container.id, image: container.image, endpoint: container.endpoint };
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
