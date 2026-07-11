// Clearcote — Playwright drop-in.
//
//   import { launch } from "clearcote";
//   const browser = await launch({ fingerprint: "seed-123", platform: "windows" });
//   const page = await browser.newPage();
//   await page.goto("https://abrahamjuliot.github.io/creepjs/");
//
// launch() returns a standard Playwright `Browser`, backed by the verified Clearcote binary
// (auto-downloaded + SHA-256 checked on first use, then cached). Every Playwright launch option
// (headless, proxy, args, timeout, ...) passes through; the fingerprint options below are added
// as engine switches.

import { chromium } from "playwright-core";
import { cpSync, mkdtempSync, rmSync } from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  LaunchOptions as PlaywrightLaunchOptions,
} from "playwright-core";
import { ensureBinary, ensureVersion, proEnsureBinary, warmFiles, type DownloadOptions } from "./download.js";
import { fingerprintArgs, splitFingerprintOptions, type FingerprintOptions } from "./fingerprint.js";
import { resolveGeo, type Geo } from "./geoip.js";
import { installHumanize, installHumanizeOnContext, type HumanizeOptions } from "./humanize.js";
import { agentArgs, splitAgentOptions, type AgentOptions } from "./agent.js";
import { resolveProfileOptions, Profile } from "./profile.js";
import {
  extensionArgs,
  resolveProxy,
  mergeFeatureFlags,
  privacySandboxArgs,
  quicArgs,
  webrtcDefaultDenyArgs,
  type PwProxy,
} from "./launchopts.js";
import { RELEASE, platformRelease } from "./release.js";
import { fetchWidevine, seedWidevine, widevineArgs } from "./widevine.js";
import { emitCoherenceWarnings } from "./warnings.js";
import { fontLaunchEnv } from "./fonts.js";
import { acquireLease, resolveLicenseKey, withRunToken, type LicenseOptions, type LeaseSession } from "./license.js";

export type { FingerprintOptions } from "./fingerprint.js";
export type { DownloadOptions } from "./download.js";
export { proEnsureBinary, type ProDownloadOptions } from "./download.js";
export { resolveGeo, type Geo } from "./geoip.js";
export type { HumanizeOptions } from "./humanize.js";
export { Profile, listProfiles, loadProfile, PROFILE_DIR, type ProfileOptions } from "./profile.js";
export {
  runAgentTask,
  agentArgs,
  OPENROUTER_BASE_URL,
  type AgentOptions,
  type AgentTaskOptions,
  type AgentTaskResult,
  type AgentStep,
} from "./agent.js";
export { RELEASE } from "./release.js";
export { fetchWidevine, seedWidevine } from "./widevine.js";
export { checkRenderCoherence, type RenderVerdict } from "./render.js";
export {
  resolveLicenseKey,
  acquireLease,
  LicenseError,
  ConcurrencyLimitError,
  LicenseRevokedError,
  type LicenseOptions,
  type LeaseSession,
} from "./license.js";

/** When true (and a proxy is set), resolve the proxy's exit-IP geo and auto-fill any unset
 * `timezone` + `acceptLanguage` (+ `location`) so they match the proxy region. */
interface GeoipOption {
  geoip?: boolean;
}

/** When set, launch a saved persona ({@link Profile}) — by name (under `CLEARCOTE_PROFILE_DIR`),
 * by path, or a `Profile` instance. Its saved options form the base; any options passed alongside
 * here override them. */
interface ProfileOption {
  profile?: string | Profile;
}

/** Load unpacked extensions (emits --load-extension + --disable-extensions-except). */
interface ExtensionsOption {
  /** Unpacked-extension directory paths. */
  extensions?: string[];
  /** Disable Privacy Sandbox + intrusive APIs (Topics/FLEDGE/WebUSB/…). Default `true` — a
   * de-Googled build shouldn't expose them. Set `false` to keep them. */
  disablePrivacySandbox?: boolean;
}

/** Options for {@link launch}: Playwright launch options + Clearcote fingerprint + agent + download options. */
export interface LaunchOptions extends PlaywrightLaunchOptions, FingerprintOptions, AgentOptions, GeoipOption, ProfileOption, ExtensionsOption, HumanizeOptions, DownloadOptions, LicenseOptions {}

/** Options for {@link launchPersistentContext}. */
export interface PersistentContextOptions
  extends PlaywrightLaunchOptions,
    BrowserContextOptions,
    FingerprintOptions,
    AgentOptions,
    GeoipOption,
    ProfileOption,
    ExtensionsOption,
    HumanizeOptions,
    DownloadOptions,
    LicenseOptions {
  /**
   * Seed + enable the opt-in Widevine CDM in this profile so DRM/EME works
   * (`requestMediaKeySystemAccess('com.widevine.alpha')` resolves) and the EME surface matches a
   * real Chrome instead of being a no-Widevine tell. The CDM is fetched once from Google's
   * component server (see {@link fetchWidevine}); clearcote never bundles Google's blob.
   */
  widevine?: boolean;
}

/** Fill unset timezone/acceptLanguage/location/webrtcIp on `fp` from the proxy's exit-IP geo. */
async function applyGeoip(fp: FingerprintOptions, proxy: unknown): Promise<void> {
  const geo: Geo | null = await resolveGeo(proxy as { server?: string; username?: string; password?: string } | undefined);
  if (!geo) return;
  if (geo.timezone && fp.timezone == null) fp.timezone = geo.timezone;
  if (geo.acceptLanguage && fp.acceptLanguage == null) fp.acceptLanguage = geo.acceptLanguage;
  if (geo.location && fp.location == null) fp.location = geo.location;
  // make WebRTC report the proxy egress IP too, coherent with HTTP egress (engine fabricates
  // the srflx candidate at this IP; no real STUN leaves the host).
  if (geo.ip && fp.webrtcIp == null) fp.webrtcIp = geo.ip;
}

function ensureRunnableHere(exe: string): void {
  if (platformRelease() === undefined) {
    throw new Error(
      `Clearcote ${RELEASE.version} ships Windows x64 and Linux x64 binaries — there is no build for '${process.platform}'.\n` +
        `Run on Windows or Linux, or pass executablePath to a compatible binary.\n` +
        `(The binary downloaded and verified fine; it is cached at: ${exe})`
    );
  }
}

/**
 * Resolve the Clearcote chrome.exe path, downloading + verifying it if needed.
 * Order: explicit `executablePath` > `CLEARCOTE_BINARY` env > PRO (when licensed) > free auto-download.
 *
 * `pro` (a resolved license key + optional API base) selects the license-gated PRO binary via the
 * site's authenticated download route. When it's absent — the free path — behaviour is unchanged.
 */
export async function executablePath(
  options: { executablePath?: string; version?: string; pro?: { licenseKey: string; licenseApiBase?: string } } & DownloadOptions = {}
): Promise<string> {
  if (options.executablePath) return options.executablePath;
  if (process.env.CLEARCOTE_BINARY) return process.env.CLEARCOTE_BINARY;
  const version = options.version || process.env.CLEARCOTE_BROWSER_VERSION;
  if (version) {
    // Explicit version selector: validate against the catalog FIRST (clear error if it doesn't
    // exist or needs a license), then route free (GitHub) vs pro (authenticated route).
    return ensureVersion(version, {
      licenseKey: options.pro?.licenseKey,
      apiBase: options.pro?.licenseApiBase,
      cacheDir: options.cacheDir,
      quiet: options.quiet,
    });
  }
  if (options.pro) {
    return proEnsureBinary(options.pro.licenseKey, {
      apiBase: options.pro.licenseApiBase,
      cacheDir: options.cacheDir,
      quiet: options.quiet,
    });
  }
  return ensureBinary({ cacheDir: options.cacheDir, quiet: options.quiet, autoUpdate: options.autoUpdate });
}

/** A resolved license key + API base for PRO-binary selection, or undefined in free mode. */
function proSelector(
  licenseKey: string | undefined,
  licenseApiBase: string | undefined,
): { licenseKey: string; licenseApiBase?: string } | undefined {
  const key = resolveLicenseKey(licenseKey);
  return key ? { licenseKey: key, licenseApiBase } : undefined;
}

/** Pre-fetch + verify the Clearcote binary without launching it. Returns the chrome.exe path.
 * Pass `version` ("150" / "150.0.7871.115" / "latest") to fetch a specific catalog build (PRO-tier
 * versions need `licenseKey` / `CLEARCOTE_LICENSE_KEY`). */
export async function download(
  options: DownloadOptions & { version?: string; licenseKey?: string; licenseApiBase?: string } = {},
): Promise<string> {
  const { version, licenseKey, licenseApiBase, ...dl } = options;
  return executablePath({ version, pro: proSelector(licenseKey, licenseApiBase), ...dl });
}

/** A headed launch with Playwright's default emulated viewport (1280x720) on the real OS window
 * makes window.innerWidth/Height disagree with the actual window — an impossible-window tell. For a
 * headed browser, default new pages/contexts to `viewport: null` (innerWidth tracks the real window)
 * unless the caller asked for a viewport. */
function installHeadedViewport(browser: Browser): void {
  const origNewPage = browser.newPage.bind(browser);
  (browser as { newPage: unknown }).newPage = (o: Record<string, unknown> = {}) =>
    origNewPage("viewport" in o ? o : { ...o, viewport: null });
  const origNewContext = browser.newContext.bind(browser);
  (browser as { newContext: unknown }).newContext = (o: Record<string, unknown> = {}) =>
    origNewContext("viewport" in o ? o : { ...o, viewport: null });
}

/** Assemble the final engine args from all layers: persona + agent + extensions + proxy, the
 * Privacy-Sandbox-disable default, the WebRTC leak-proof default, and the user's own args — then
 * collapse all --enable-features/--disable-features into one each (Chromium keeps only the last). */
function assembleArgs(
  fpArgs: string[],
  agArgs: string[],
  extArgs: string[],
  proxyArgs: string[],
  disablePrivacySandbox: boolean | undefined,
  webrtcIp: unknown,
  userArgs: string[],
  proxyForQuic?: PwProxy
): string[] {
  const base = [...fpArgs, ...agArgs, ...extArgs, ...proxyArgs, ...quicArgs(proxyForQuic)];
  if (disablePrivacySandbox !== false) base.push(...privacySandboxArgs());
  base.push(...webrtcDefaultDenyArgs([...base, ...userArgs], webrtcIp));
  return mergeFeatureFlags([...base, ...userArgs]);
}

export function isWinLaunchRace(err: unknown): boolean {
  const m = String((err as Error)?.message ?? err).toLowerCase();
  return m.includes("spawn unknown") || m.includes("side-by-side") || m.includes("side by side");
}

/**
 * Launch via `doLaunch(exePath)`, working around the Windows first-launch antivirus-scan race.
 *
 * A just-extracted, unsigned chrome.exe can fail with "spawn UNKNOWN" / "side-by-side configuration
 * is incorrect" while real-time AV scans chrome_elf.dll (the SxS assembly member), and Windows
 * caches that negative activation context against the *path* — so retrying the same path keeps
 * failing. `warmFiles` (in ensureBinary) pre-scans to prevent it; here we (1) re-scan + back off +
 * retry a couple times, then (2) as a last resort relaunch from a pristine copy on a fresh temp
 * path, which always gets a clean SxS evaluation. Pass-through on non-Windows.
 */
export async function winAvRetry<T>(doLaunch: (exe: string) => Promise<T>, exe: string): Promise<T> {
  if (process.platform !== "win32") return doLaunch(exe);
  for (let i = 0; i < 3; i++) {
    try {
      return await doLaunch(exe);
    } catch (err) {
      if (!isWinLaunchRace(err)) throw err;
      warmFiles(dirname(exe));
      await new Promise((resolve) => setTimeout(resolve, 800 * (i + 1)));
    }
  }
  // The in-place SxS activation-context poison never clears; relaunch from a fresh copy.
  const recover = join(mkdtempSync(join(tmpdir(), "clearcote-recover-")), "browser");
  cpSync(dirname(exe), recover, { recursive: true });
  warmFiles(recover);
  return doLaunch(join(recover, basename(exe)));
}

/** Launch Clearcote and return a standard Playwright {@link Browser}. */
export async function launch(options: LaunchOptions = {}): Promise<Browser> {
  // profile= a saved persona: its options are the base, explicit options override.
  const merged = options.profile ? { ...resolveProfileOptions(options.profile), ...options } : options;
  const { profile: _profile, extensions, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, version, licenseKey, licenseApiBase, ...rest } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy;  // captured before resolveProxy drops it
  if (geoip) await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy);
  // SOCKS5-with-credentials must go through --proxy-server (Playwright rejects it); drop it from PW.
  const { args: proxyArgs, proxy } = resolveProxy((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined);
  // proxy unchanged unless it was rerouted to --proxy-server, in which case drop it from Playwright
  if (proxy === undefined) delete (pwOptions as Record<string, unknown>).proxy;
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless: (pwOptions as PlaywrightLaunchOptions).headless, _userArgs: args ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  // A license key selects the PRO (gated) binary; no key -> the free binary (unchanged path).
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, pro: proSelector(licenseKey, licenseApiBase) });
  ensureRunnableHere(exe);
  const headed = (pwOptions as PlaywrightLaunchOptions).headless === false;
  // License (opt-in): check out a concurrency slot and inject CLEARCOTE_RUN_TOKEN so the PRO
  // engine gate lets the browser launch. Inert (null) in free mode / when no key is set.
  const lease = await acquireLease({ licenseKey, licenseApiBase, sdkVersion: String(RELEASE.version), quiet });
  // On Linux, point FONTCONFIG_FILE at the bundled metric-compatible clones (Segoe UI, Arial, …).
  const launchEnv = fontLaunchEnv(exe, (pwOptions as PlaywrightLaunchOptions).env);
  const runtimeEnv = lease ? withRunToken(lease.token, launchEnv) : launchEnv;
  const browser = await winAvRetry((exePath) => chromium.launch({
    // Drop Playwright's default --enable-automation so the engine's AutomationControlled feature
    // stays off (it flips webdriver-adjacent tells). Caller can override via ignoreDefaultArgs.
    ignoreDefaultArgs: ["--enable-automation"],
    ...(pwOptions as PlaywrightLaunchOptions),
    executablePath: exePath,
    ...(runtimeEnv ? { env: runtimeEnv } : {}),
    args: assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), extensionArgs(extensions), proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, args ?? [], proxyOpt as PwProxy | undefined),
  }), exe);
  // Release the concurrency slot when the browser closes.
  if (lease) browser.on("disconnected", () => { void lease.stop(); });
  if (headed) installHeadedViewport(browser); // launch() takes no viewport option -> wrap newPage/newContext
  installHumanize(browser, { humanize, showCursor, seed: fingerprint.fingerprint }); // seed => stable motor persona
  return browser;
}

/**
 * Launch Clearcote with a persistent profile directory and return a Playwright
 * {@link BrowserContext} (cookies, storage, etc. persist in `userDataDir`).
 */
export async function launchPersistentContext(
  userDataDir: string,
  options: PersistentContextOptions = {}
): Promise<BrowserContext> {
  const merged = options.profile ? { ...resolveProfileOptions(options.profile), ...options } : options;
  const { profile: _profile, extensions, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, widevine, version, licenseKey, licenseApiBase, ...rest } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy;  // captured before resolveProxy drops it
  if (geoip) await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy);
  const { args: proxyArgs, proxy } = resolveProxy((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined);
  if (proxy === undefined) delete (pwOptions as Record<string, unknown>).proxy;
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless: (pwOptions as PlaywrightLaunchOptions).headless, _userArgs: args ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  const opts = pwOptions as PlaywrightLaunchOptions & BrowserContextOptions;
  // headed + no explicit viewport -> disable the emulated viewport (impossible-window tell)
  if (opts.headless === false && opts.viewport === undefined) opts.viewport = null;
  // widevine=true: seed the CDM into the profile + un-suppress the component updater (Playwright
  // disables it by default) so the engine registers it. Failure -> DRM gracefully off, launch proceeds.
  // Default the automation strip BEFORE the Widevine helper so it appends --disable-component-update
  // to ['--enable-automation'] rather than clobbering it (losing the strip). Caller's own wins.
  let ignoreDefaultArgs: string[] | boolean | undefined =
    (opts.ignoreDefaultArgs as string[] | boolean | undefined) ?? ["--enable-automation"];
  let userArgs = args ?? [];
  if (widevine) {
    try {
      await seedWidevine(userDataDir, { quiet });
      // The --component-updater=fast-update scan is Windows-only (on Linux the hint file registers
      // the CDM). Only warn about a user-supplied non-fast-update mode where fast-update matters.
      const cu = userArgs.filter((a) => a.includes("component-updater"));
      if (process.platform !== "linux" && cu.length && !cu.some((a) => a.includes("fast-update")) && !quiet) {
        process.stderr.write("[clearcote] [widevine] note: your --component-updater mode may not register the CDM; --component-updater=fast-update is needed to scan the pre-installed component\n");
      }
      const tweak = widevineArgs(ignoreDefaultArgs, userArgs);
      ignoreDefaultArgs = tweak.ignoreDefaultArgs;
      userArgs = tweak.args;
    } catch (e) {
      if (!quiet) process.stderr.write(`[clearcote] [widevine] setup failed (continuing without DRM): ${String(e)}\n`);
    }
  }
  delete (opts as Record<string, unknown>).ignoreDefaultArgs;  // passed explicitly below
  // A license key selects the PRO (gated) binary; no key -> the free binary (unchanged path).
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, pro: proSelector(licenseKey, licenseApiBase) });
  ensureRunnableHere(exe);
  // License (opt-in): check out a concurrency slot + inject CLEARCOTE_RUN_TOKEN. Inert in free mode.
  const lease = await acquireLease({ licenseKey, licenseApiBase, sdkVersion: String(RELEASE.version), quiet });
  const ctxEnv = fontLaunchEnv(exe, (opts as PlaywrightLaunchOptions).env);
  const runtimeEnv = lease ? withRunToken(lease.token, ctxEnv) : ctxEnv;
  const context = await winAvRetry((exePath) => chromium.launchPersistentContext(userDataDir, {
    ...opts,
    ignoreDefaultArgs,  // keep AutomationControlled off (+ component updater on when widevine)
    executablePath: exePath,
    ...(runtimeEnv ? { env: runtimeEnv } : {}),
    args: assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), extensionArgs(extensions), proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs, proxyOpt as PwProxy | undefined),
  }), exe);
  if (lease) context.on("close", () => { void lease.stop(); });
  installHumanizeOnContext(context, { humanize, showCursor, seed: fingerprint.fingerprint }); // seed => stable motor persona
  return context;
}

/** Options for {@link launchAgent}: persistent-context options + an optional `userDataDir`. */
export interface LaunchAgentOptions extends PersistentContextOptions {
  /** Profile directory to persist (cookies/storage/logins). Defaults to a fresh temp dir. */
  userDataDir?: string;
}

/**
 * Launch Clearcote ready for the in-browser AI agent and return a Playwright {@link BrowserContext}.
 *
 * The agent drives Chrome's Actor framework, which only attaches to a **regular profile** — not
 * incognito — so this uses a *persistent* context (a fresh temp `userDataDir` unless you pass one).
 * Set `agentLlmKey` (+ optional `agentModel`), then drive a page with {@link runAgentTask}:
 *
 * ```ts
 * const ctx = await launchAgent({ agentLlmKey: process.env.OPENROUTER_API_KEY, agentModel: "openai/gpt-4o-mini" });
 * const page = ctx.pages()[0] ?? (await ctx.newPage());
 * await page.goto("https://example.com");
 * const result = await runAgentTask(page, "Click the 'More information...' link.");
 * ```
 *
 * Use this (or {@link launchPersistentContext}) for the agent — plain {@link launch} is incognito,
 * where the Actor framework can't attach the tab.
 */
export async function launchAgent(options: LaunchAgentOptions = {}): Promise<BrowserContext> {
  const { userDataDir, ...rest } = options;
  const dir = userDataDir ?? mkdtempSync(join(tmpdir(), "clearcote-agent-"));
  return launchPersistentContext(dir, rest);
}

/** A free ephemeral TCP port on loopback. */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const s = createServer();
    s.once("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const addr = s.address();
      const port = typeof addr === "object" && addr ? addr.port : 0;
      s.close(() => resolve(port));
    });
  });
}

export interface ServeOptions extends LaunchOptions {
  /** CDP port (default: a free ephemeral port; pass 9222 for the conventional one). */
  port?: number;
  /** Bind address — keep it loopback (default 127.0.0.1) for stealth + safety. */
  host?: string;
  /** `--remote-allow-origins` value (default: the loopback origins only; "*" for trusted local use). */
  allowOrigins?: string;
  /** Persistent profile dir (default: a fresh temp dir, removed on close). */
  userDataDir?: string;
  /** Run headless (default true; false for a visible window). */
  headless?: boolean;
  /** How long to wait for the CDP endpoint to come up (ms; default 30000). */
  readyTimeoutMs?: number;
}

/** Handle for a standing clearcote CDP endpoint. Use `.cdpUrl` with any CDP client. */
export class Server {
  constructor(
    private readonly proc: ChildProcess,
    readonly host: string,
    readonly port: number,
    private readonly userDataDir: string,
    private readonly ownUdd: boolean,
    private readonly lease?: LeaseSession | null,
  ) {}
  /** HTTP CDP base — pass to `connectOverCDP` / `puppeteer.connect({ browserURL })`. */
  get cdpUrl(): string {
    return `http://${this.host}:${this.port}`;
  }
  /** The browser-level WebSocket URL (for clients that want `connect({ browserWSEndpoint })`). */
  async wsUrl(): Promise<string | undefined> {
    try {
      const r = await fetch(`${this.cdpUrl}/json/version`);
      return ((await r.json()) as { webSocketDebuggerUrl?: string }).webSocketDebuggerUrl;
    } catch {
      return undefined;
    }
  }
  isAlive(): boolean {
    return this.proc.exitCode === null && !this.proc.killed;
  }
  async close(): Promise<void> {
    try {
      this.proc.kill("SIGTERM");
    } catch {
      /* ignore */
    }
    // Release the concurrency slot (best-effort).
    try { await this.lease?.stop(); } catch { /* ignore */ }
    if (this.ownUdd) {
      try {
        rmSync(this.userDataDir, { recursive: true, force: true });
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * Launch Clearcote with a RAW CDP endpoint and return a {@link Server} — the drop-in-for-the-whole-
 * ecosystem mode. Unlike {@link launch} (which spawns and *owns* a Playwright browser), `serve`
 * leaves a standing browser any client attaches to with no code change:
 * ```ts
 * const srv = await serve({ fingerprint: "seed-1", platform: "windows" });
 * const browser = await chromium.connectOverCDP(srv.cdpUrl);        // Playwright
 * // or: await puppeteer.connect({ browserURL: srv.cdpUrl });        // Puppeteer
 * // or: point browser-use / Crawl4AI / Stagehand at srv.cdpUrl
 * await srv.close();
 * ```
 * Stays stealthy: the binary is launched **directly** (not through Playwright/Puppeteer), so the
 * `--enable-automation` flag those frameworks add is never present and `navigator.webdriver` stays
 * `false`; the engine's `Runtime.enable` neutralization keeps the attached CDP client undetectable
 * to the page; the port binds to loopback with an origin allowlist; attaching over CDP adds no
 * launch flags, so the served persona is preserved end to end.
 */
export async function serve(options: ServeOptions = {}): Promise<Server> {
  const {
    port,
    host = "127.0.0.1",
    allowOrigins,
    userDataDir: uddOption,
    headless = true,
    readyTimeoutMs = 30000,
    humanize: _humanize, // Playwright-only; not applicable to a direct launch
    showCursor: _showCursor,
    ...launchOpts
  } = options;

  // Build the same stealth arg set as launch(), then launch the binary ourselves.
  const merged = launchOpts.profile
    ? { ...resolveProfileOptions(launchOpts.profile), ...launchOpts }
    : launchOpts;
  const {
    profile: _profile, extensions, disablePrivacySandbox, executablePath: exeOption,
    args: userArgs, geoip, autoUpdate, cacheDir, quiet, version, licenseKey, licenseApiBase, ...rest
  } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined;
  if (geoip) await applyGeoip(fingerprint, proxyOpt);
  const { args: proxyArgs } = resolveProxy(proxyOpt);
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless, _userArgs: userArgs ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  // A license key selects the PRO (gated) binary; no key -> the free binary (unchanged path).
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, pro: proSelector(licenseKey, licenseApiBase) });
  ensureRunnableHere(exe);
  const engineArgs = assembleArgs(
    fingerprintArgs(fingerprint), agentArgs(agent), extensionArgs(extensions),
    proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs ?? [], proxyOpt);

  const resolvedPort = port ?? (await freePort());
  const ownUdd = !uddOption;
  const userDataDir = uddOption ?? mkdtempSync(join(tmpdir(), "clearcote-serve-"));
  const origins = allowOrigins ?? `http://${host}:${resolvedPort},http://localhost:${resolvedPort}`;
  const cdpArgs = [
    `--remote-debugging-port=${resolvedPort}`,
    `--remote-debugging-address=${host}`,
    `--remote-allow-origins=${origins}`,
    `--user-data-dir=${userDataDir}`,
  ];
  if (headless) cdpArgs.push("--headless=new");
  if (proxyOpt?.server) cdpArgs.push(`--proxy-server=${proxyOpt.server}`);

  // License (opt-in): check out a concurrency slot + inject CLEARCOTE_RUN_TOKEN. Inert in free mode.
  const lease = await acquireLease({ licenseKey, licenseApiBase, sdkVersion: String(RELEASE.version), quiet });
  const env = { ...process.env, ...(fontLaunchEnv(exe, undefined) ?? {}), ...(lease ? { CLEARCOTE_RUN_TOKEN: lease.token } : {}) };
  // Launched DIRECTLY (no Playwright) => no --enable-automation => navigator.webdriver stays false.
  // Wrap in winAvRetry so a just-extracted binary survives the Windows SxS/AV first-launch race
  // ("spawn UNKNOWN"), same as launch(): warm + back off + retry, then recover from a fresh copy.
  const proc = await winAvRetry(
    (exePath) => new Promise<ChildProcess>((resolve, reject) => {
      let settled = false;
      const p = spawn(exePath, [...engineArgs, ...cdpArgs], { env, stdio: "ignore" });
      p.once("error", (err) => { if (!settled) { settled = true; reject(err); } });
      p.once("spawn", () => { if (!settled) { settled = true; resolve(p); } });
    }),
    exe,
  );

  const deadline = Date.now() + readyTimeoutMs;
  let ready = false;
  while (Date.now() < deadline) {
    if (proc.exitCode !== null) break;
    try {
      await fetch(`http://${host}:${resolvedPort}/json/version`);
      ready = true;
      break;
    } catch {
      /* not up yet */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  if (!ready) {
    try { proc.kill(); } catch { /* ignore */ }
    try { await lease?.stop(); } catch { /* ignore */ }
    if (ownUdd) { try { rmSync(userDataDir, { recursive: true, force: true }); } catch { /* ignore */ } }
    throw new Error(
      `clearcote serve: CDP endpoint at http://${host}:${resolvedPort} did not come up within ${readyTimeoutMs}ms`);
  }
  const srv = new Server(proc, host, resolvedPort, userDataDir, ownUdd, lease);
  process.once("exit", () => { void srv.close(); });
  if (!quiet) {
    process.stderr.write(
      `[clearcote] CDP endpoint ready: ${srv.cdpUrl}\n` +
      `            attach any client: connectOverCDP(${JSON.stringify(srv.cdpUrl)}) / puppeteer.connect({ browserURL })\n`);
  }
  return srv;
}

import { runAgentTask } from "./agent.js";
import { listProfiles, loadProfile } from "./profile.js";
export default {
  launch,
  launchPersistentContext,
  launchAgent,
  serve,
  Server,
  executablePath,
  download,
  runAgentTask,
  Profile,
  listProfiles,
  loadProfile,
  fetchWidevine,
  seedWidevine,
  RELEASE,
};
