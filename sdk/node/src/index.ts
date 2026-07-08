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
import { cpSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  LaunchOptions as PlaywrightLaunchOptions,
} from "playwright-core";
import { ensureBinary, warmFiles, type DownloadOptions } from "./download.js";
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

export type { FingerprintOptions } from "./fingerprint.js";
export type { DownloadOptions } from "./download.js";
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
export interface LaunchOptions extends PlaywrightLaunchOptions, FingerprintOptions, AgentOptions, GeoipOption, ProfileOption, ExtensionsOption, HumanizeOptions, DownloadOptions {}

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
    DownloadOptions {
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
 * Order: explicit `executablePath` > `CLEARCOTE_BINARY` env > auto-download.
 */
export async function executablePath(
  options: { executablePath?: string } & DownloadOptions = {}
): Promise<string> {
  if (options.executablePath) return options.executablePath;
  if (process.env.CLEARCOTE_BINARY) return process.env.CLEARCOTE_BINARY;
  return ensureBinary({ cacheDir: options.cacheDir, quiet: options.quiet, autoUpdate: options.autoUpdate });
}

/** Pre-fetch + verify the Clearcote binary without launching it. Returns the chrome.exe path. */
export async function download(options: DownloadOptions = {}): Promise<string> {
  return ensureBinary(options);
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
  const { profile: _profile, extensions, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, ...rest } = merged;
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
  const exe = await executablePath({ executablePath: exeOption, autoUpdate, cacheDir, quiet });
  ensureRunnableHere(exe);
  const headed = (pwOptions as PlaywrightLaunchOptions).headless === false;
  const browser = await winAvRetry((exePath) => chromium.launch({
    // Drop Playwright's default --enable-automation so the engine's AutomationControlled feature
    // stays off (it flips webdriver-adjacent tells). Caller can override via ignoreDefaultArgs.
    ignoreDefaultArgs: ["--enable-automation"],
    ...(pwOptions as PlaywrightLaunchOptions),
    executablePath: exePath,
    args: assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), extensionArgs(extensions), proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, args ?? [], proxyOpt as PwProxy | undefined),
  }), exe);
  if (headed) installHeadedViewport(browser); // launch() takes no viewport option -> wrap newPage/newContext
  installHumanize(browser, { humanize, showCursor });
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
  const { profile: _profile, extensions, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, widevine, ...rest } = merged;
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
  const exe = await executablePath({ executablePath: exeOption, autoUpdate, cacheDir, quiet });
  ensureRunnableHere(exe);
  const context = await winAvRetry((exePath) => chromium.launchPersistentContext(userDataDir, {
    ...opts,
    ignoreDefaultArgs,  // keep AutomationControlled off (+ component updater on when widevine)
    executablePath: exePath,
    args: assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), extensionArgs(extensions), proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs, proxyOpt as PwProxy | undefined),
  }), exe);
  installHumanizeOnContext(context, { humanize, showCursor });
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

import { runAgentTask } from "./agent.js";
import { listProfiles, loadProfile } from "./profile.js";
export default {
  launch,
  launchPersistentContext,
  launchAgent,
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
