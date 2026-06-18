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
import type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  LaunchOptions as PlaywrightLaunchOptions,
} from "playwright-core";
import { ensureBinary, type DownloadOptions } from "./download.js";
import { fingerprintArgs, splitFingerprintOptions, type FingerprintOptions } from "./fingerprint.js";
import { resolveGeo, type Geo } from "./geoip.js";
import { installHumanize, installHumanizeOnContext, type HumanizeOptions } from "./humanize.js";
import { RELEASE } from "./release.js";

export type { FingerprintOptions } from "./fingerprint.js";
export type { DownloadOptions } from "./download.js";
export { resolveGeo, type Geo } from "./geoip.js";
export type { HumanizeOptions } from "./humanize.js";
export { RELEASE } from "./release.js";

/** When true (and a proxy is set), resolve the proxy's exit-IP geo and auto-fill any unset
 * `timezone` + `acceptLanguage` (+ `location`) so they match the proxy region. */
interface GeoipOption {
  geoip?: boolean;
}

/** Options for {@link launch}: Playwright launch options + Clearcote fingerprint + download options. */
export interface LaunchOptions extends PlaywrightLaunchOptions, FingerprintOptions, GeoipOption, HumanizeOptions, DownloadOptions {}

/** Options for {@link launchPersistentContext}. */
export interface PersistentContextOptions
  extends PlaywrightLaunchOptions,
    BrowserContextOptions,
    FingerprintOptions,
    GeoipOption,
    HumanizeOptions,
    DownloadOptions {}

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
  if (process.platform !== "win32") {
    throw new Error(
      `Clearcote ${RELEASE.version} ships a Windows x64 binary only — it cannot launch on '${process.platform}'.\n` +
        `Run on Windows, or pass executablePath to a compatible binary.\n` +
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

/** Launch Clearcote and return a standard Playwright {@link Browser}. */
export async function launch(options: LaunchOptions = {}): Promise<Browser> {
  const { executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, ...rest } = options;
  const { fingerprint, rest: pwOptions } = splitFingerprintOptions(rest);
  if (geoip) await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy);
  const exe = await executablePath({ executablePath: exeOption, autoUpdate, cacheDir, quiet });
  ensureRunnableHere(exe);
  const browser = await chromium.launch({
    ...(pwOptions as PlaywrightLaunchOptions),
    executablePath: exe,
    args: [...fingerprintArgs(fingerprint), ...(args ?? [])],
  });
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
  const { executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, ...rest } = options;
  const { fingerprint, rest: pwOptions } = splitFingerprintOptions(rest);
  if (geoip) await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy);
  const exe = await executablePath({ executablePath: exeOption, autoUpdate, cacheDir, quiet });
  ensureRunnableHere(exe);
  const context = await chromium.launchPersistentContext(userDataDir, {
    ...(pwOptions as PlaywrightLaunchOptions & BrowserContextOptions),
    executablePath: exe,
    args: [...fingerprintArgs(fingerprint), ...(args ?? [])],
  });
  installHumanizeOnContext(context, { humanize, showCursor });
  return context;
}

export default { launch, launchPersistentContext, executablePath, download, RELEASE };
