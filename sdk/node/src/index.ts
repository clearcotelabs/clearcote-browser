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
//
// Since 0.34.0 the same call can run the browser on Clearcote's servers instead: `launch({ cloud: true })`
// (or CLEARCOTE_CLOUD=1) resolves to the same Playwright `Browser`, connected to a hosted session.
// `Cloud` (./cloud.ts) is the rest of the hosted API: agent runs, profiles, recordings, events,
// hand-off and webhooks.

import { chromium } from "playwright-core";
import { cpSync, existsSync, mkdtempSync, readFileSync, readdirSync, readlinkSync, rmSync, statSync } from "node:fs";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import type {
  Browser,
  BrowserContext,
  BrowserContextOptions,
  LaunchOptions as PlaywrightLaunchOptions,
  Page,
} from "playwright-core";
import { checkInstall, ensureBinary, ensureVersion, proEnsureBinary, resolvedEngineVersion, resolveReleaseChannel, warmFiles, type DownloadOptions } from "./download.js";
import { fingerprintArgs, isFingerprintPassthrough, splitFingerprintOptions, type FingerprintOptions } from "./fingerprint.js";
import { resolveGeoDetailed, startEgressDriftCheck, GeoipError, type Geo } from "./geoip.js";
import { installHumanize, installHumanizeOnContext, type HumanizeOptions } from "./humanize.js";
import { agentArgs, splitAgentOptions, type AgentOptions } from "./agent.js";
import { resolveProfileOptions, Profile } from "./profile.js";
import { resolveAuto, resolveLocal, localSetupHint, engineSupportsProfiles, MIN_PROFILE_ENGINE_MAJOR, DEFAULT_LOCAL_DIR, profileWithWindows11Faces, type AutoOptions, type AutoResult } from "./profileauto.js";
import { measureHost, fetchIndex, fetchProfile, hostOsFamily, type ProfileSourceOptions } from "./profilesource.js";
import { importDirectory, loadImportedProfile, indexEntryFromProfile } from "./profileimport.js";
import {
  selectProfile, scoreProfile, eligible, gpuVendorClass, defaultStickyKey,
  type HostFacts, type ProfileIndexEntry, type SelectMode, type SelectOptions, type Selection,
} from "./profilelib.js";
import {
  extensionArgs,
  portableArgs,
  resolveProxy,
  engineSupportsSwitch,
  warnUnsupportedEngineOptions,
  mergeFeatureFlags,
  playwrightFeatureOverrideArgs,
  privacySandboxArgs,
  quicArgs,
  socks5UdpArgs,
  webBluetoothArgs,
  webrtcDefaultDenyArgs,
  DEFAULT_IGNORED_ARGS,
  gpuBlocklistArgs,
  gateEngineSwitches,
  engineExtrasArgs,
  type PwProxy,
} from "./launchopts.js";
import { RELEASE, platformRelease } from "./release.js";
import { fetchWidevine, seedWidevine, widevineArgs } from "./widevine.js";
import { emitCoherenceWarnings, emitWarnings, serveExposureWarnings } from "./warnings.js";
import { fontLaunchEnv } from "./fonts.js";
import { withShaderDialect, type ShaderDialect } from "./shaderdialect.js";
import {
  applyHeadlessGeometry, fitServedWindow, fitWindowToWorkArea, installWindowFixup, servedGeometry,
} from "./geometry.js";
import { acquireLease, resolveLicenseKey, withRunToken, STALE_TOKEN_REFUSAL, type LicenseOptions, type LeaseSession } from "./license.js";
import { Cloud, cloudRequested, launchCloud, verifyWebhook, type CloudLaunchOptions, type CloudProfile } from "./cloud.js";

export {
  Cloud,
  CloudError,
  CloudTimeoutError,
  CloudBrowsers,
  CloudRuns,
  CloudProfiles,
  CloudWebhooks,
  verifyWebhook,
  cloudRequested,
  cloudSessionOf,
  sessionBody,
  filterCookies,
  cookiesFromState,
  DEFAULT_API_URL,
  TERMINAL_RUN_STATUSES,
  SESSION_FIELDS,
  RUN_FIELDS,
  SDK_SIDE_OPTIONS,
  LOCAL_ONLY_OPTIONS,
  type CloudOptions,
  type CloudLaunchOptions,
  type CloudSessionOptions,
  type CloudProxy,
  type CloudProfile,
  type CloudChallengeService,
  type RunOptions,
  type SyncOptions,
} from "./cloud.js";

export type { FingerprintOptions } from "./fingerprint.js";
export type { DownloadOptions } from "./download.js";
export { proEnsureBinary, type ProDownloadOptions } from "./download.js";
export { checkInstall, verifyInstall } from "./download.js";
export { resolveGeo, resolveGeoDetailed, geoipTimeoutMs, GeoipError, type Geo, type GeoResult } from "./geoip.js";
export { proxiedRequest, toProxySpec, type ProxySpec } from "./net.js";
export { resolveReleaseChannel, type ReleaseChannel } from "./download.js";
export { isFingerprintPassthrough } from "./fingerprint.js";
export { DEFAULT_IGNORED_ARGS, gpuBlocklistArgs, gateEngineSwitches, engineSupportsSwitch } from "./launchopts.js";
export { serveMultiplex, type MultiplexOptions, type MultiplexServer } from "./multiplex.js";
export type { HumanizeOptions } from "./humanize.js";
export { Profile, listProfiles, loadProfile, PROFILE_DIR, type ProfileOptions } from "./profile.js";
// Profile library: real captured personas, selected for coherence with THIS host.
export {
  selectProfile, scoreProfile, eligible, gpuVendorClass, defaultStickyKey, DEFAULT_MAX_ENCODED,
  type HostFacts, type ProfileIndexEntry, type SelectMode, type SelectOptions, type Selection,
} from "./profilelib.js";
export {
  importDirectory, loadImportedProfile, indexEntryFromProfile, type ImportResult,
} from "./profileimport.js";
export {
  fetchIndex, fetchProfile, measureHost, hostOsFamily, resolveAutoProfile,
  type ProfileSourceOptions,
} from "./profilesource.js";
export {
  resolveAuto, resolveLocal, loadLocalIndex, localSetupHint, engineSupportsProfiles,
  MIN_PROFILE_ENGINE_MAJOR, DEFAULT_LOCAL_DIR,
  type AutoOptions, type AutoResult, type ProfileOrigin,
} from "./profileauto.js";
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
  getSessionSeats,
  saveLicenseKey,
  removeLicenseKey,
  licenseKeySource,
  licenseKeyPath,
  type SessionSeats,
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
 * here override them.
 *
 * `"auto"` is special: instead of a saved option-set, it resolves a REAL CAPTURED FINGERPRINT
 * for this machine — the licensed profile service first, a local imported directory as backup —
 * and applies it as `fingerprintProfile` with NO seed. That distinction matters: with no
 * `--fingerprint`, the farbling machinery never engages and canvas/WebGL/audio readbacks are
 * byte-identical to an unmodified browser, which is why the profile path survives strict
 * anti-bot scoring where a synthetic seed does not.
 *
 * Tune it with {@link AutoProfileOptions.profileSelect}. */
interface ProfileOption {
  profile?: string | Profile | "auto";
  /** Selection + source options for `profile: "auto"`. */
  profileSelect?: AutoOptions;
}

/** Load unpacked extensions (emits --load-extension + --disable-extensions-except). */
interface ExtensionsOption {
  /** Unpacked-extension directory paths. */
  extensions?: string[];
  /** Keep the cookie encryption key in the profile so the user data dir is portable between
   * machines. Opt-in: the cookie database is then effectively unencrypted at rest. */
  portableProfile?: boolean;
  /** Derive the profile encryption key from this secret instead, writing nothing to disk.
   * Preferred when the profile is synced to shared storage. */
  encryptionKey?: string;
  /** Disable Privacy Sandbox APIs (Topics/FLEDGE/Shared Storage/Fenced Frames).
   *
   * Default `false` since 0.23.0 — real Google Chrome ships all of them, and the default persona
   * (`brand: "chrome"`) claims to be Google Chrome, so disabling them was a coherence tell rather
   * than a privacy win. Set `true` when the persona genuinely is de-Googled Chromium. */
  disablePrivacySandbox?: boolean;
}

/** Profile-directory control for {@link launch}. */
interface EphemeralProfileOption {
  /** Launch on a throwaway persistent profile that is deleted on close. Default `true` since
   * 0.23.0 — incognito cannot load the Widevine CDM, which is itself a tell. Set `false` for the
   * pre-0.23 incognito launch. */
  ephemeralProfile?: boolean;
  /** Keep a profile at this path instead of a throwaway one (delegates to
   * {@link launchPersistentContext}; the directory is NOT deleted). */
  userDataDir?: string;
}

/** Shader-dialect reporting (see ./shaderdialect.ts). */
interface ShaderDialectOption {
  /** Report ANGLE's translated shader in this dialect for
   * `WEBGL_debug_shaders.getTranslatedShaderSource()`.
   *
   * `"hlsl"` makes a Windows persona on a Linux host report HLSL, matching the Direct3D renderer
   * string it already advertises — without it the Vulkan backend answers with SPIR-V and the two
   * values contradict each other. Rendering is unaffected.
   *
   * ON by default for a Windows claim on a non-Windows host (since 0.39.0); pass `false` to turn it
   * off. A shader the HLSL translator rejects falls back to the backend's own output, the state
   * every launch was in before. Needs a PRO engine 151 r15+; older engines ignore it. */
  shaderDialect?: ShaderDialect | false;
}

/** Engine behaviour switches that are not part of the persona (engine 152 r22+). */
interface EngineExtrasOption {
  /**
   * Allow third-party cookies, as stock Chrome does. The de-Googled base blocks them by default,
   * which breaks embedded flows that rely on them: reCAPTCHA, SSO sign-in, payment challenges.
   * Default off (unchanged behaviour).
   */
  allowThirdPartyCookies?: boolean;
  /**
   * Hide proxy use from origins and pages: send `Connection` instead of `Proxy-Connection` on
   * plain-HTTP requests through an HTTP proxy, and report proxied connection timing the way a reused
   * connection reports it (no proxy-shaped DNS/connect/TLS durations). Requires a proxy.
   */
  transparentProxy?: boolean;
}

/** Opt-in SOCKS5 UDP relaying (see {@link socks5UdpArgs}). */
interface Socks5UdpOption {
  /** Relay WebRTC's UDP through the SOCKS5 proxy using UDP ASSOCIATE, instead of letting it egress
   * on the host's own path.
   *
   * By default clearcote denies non-proxied UDP, which keeps UDP from leaking around the proxy but
   * also means peer connections that need UDP never establish — stock Chromium cannot proxy a
   * datagram. Turn this on to get working UDP that still leaves from the proxy's address.
   *
   * Applies only to a `socks5://` proxy; ignored otherwise. Needs a PRO engine 151 r17+, and a
   * proxy that actually permits the ASSOCIATE command. */
  socks5Udp?: boolean;
}

/** Local or cloud: the one switch, and the account a cloud launch uses (a local launch ignores the
 * account options, so code that always passes them switches with nothing but `cloud`). */
interface CloudSwitchOption {
  /** true (or a {@link Cloud} client): run the browser on Clearcote's servers and resolve to the same
   * Playwright Browser. Unset follows CLEARCOTE_CLOUD=1|true|yes. See {@link CloudLaunchOptions}. */
  cloud?: boolean | Cloud;
  /** Cloud API key; defaults to CLEARCOTE_API_KEY. */
  apiKey?: string;
  /** Cloud API base URL; defaults to CLEARCOTE_API_URL, then https://www.clearcotelabs.com. */
  apiUrl?: string;
}

/** Options for {@link launch}: Playwright launch options + Clearcote fingerprint + agent + download options. */
export interface LaunchOptions extends PlaywrightLaunchOptions, FingerprintOptions, AgentOptions, GeoipOption, ProfileOption, ExtensionsOption, EphemeralProfileOption, HumanizeOptions, DownloadOptions, LicenseOptions, ShaderDialectOption, Socks5UdpOption, EngineExtrasOption, CloudSwitchOption {}

/** The account options a local launch drops (see CloudSwitchOption). */
function withoutCloudOptions<T extends object>(options: T): T {
  const { cloud: _c, apiKey: _k, apiUrl: _u, ...rest } = options as T & CloudSwitchOption;
  return rest as T;
}

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
    LicenseOptions,
    ShaderDialectOption,
    Socks5UdpOption,
    EngineExtrasOption,
    CloudSwitchOption {
  /**
   * Seed + enable the opt-in Widevine CDM in this profile so DRM/EME works
   * (`requestMediaKeySystemAccess('com.widevine.alpha')` resolves) and the EME surface matches a
   * real Chrome instead of being a no-Widevine tell. The CDM is fetched once from Google's
   * component server (see {@link fetchWidevine}); clearcote never bundles Google's blob.
   */
  widevine?: boolean;
}

/**
 * Fill unset timezone/acceptLanguage/location/webrtcIp on `fp` from the proxy's exit-IP geo.
 *
 * FAILS CLOSED: if the region cannot be resolved, the launch throws {@link GeoipError} instead of
 * continuing on the host's clock and a default language (UTC + en-US on most servers) — the exact
 * mismatch geoip exists to prevent. A caller who set BOTH `timezone` and `acceptLanguage`
 * explicitly still launches (with a warning), since nothing geoip would fill is missing.
 */
export async function applyGeoip(fp: FingerprintOptions, proxy: unknown, quiet?: boolean): Promise<Geo | undefined> {
  const result = await resolveGeoDetailed(proxy as { server?: string; username?: string; password?: string } | undefined, { quiet });
  const geo: Geo | null = result.geo;
  if (!geo || !geo.timezone) {
    if (fp.timezone && fp.acceptLanguage) {
      if (!quiet) console.warn(`clearcote: geoip could not resolve the region (${result.reason}); using the explicit timezone and acceptLanguage.`);
      return undefined;
    }
    throw new GeoipError(
      `geoip: could not resolve the ${proxy ? "proxy's" : "connection's"} region (${result.reason}). ` +
        "Launching anyway would use this machine's clock and a default language. Fix the proxy, raise " +
        "CLEARCOTE_GEOIP_TIMEOUT_SECONDS, or pass timezone and acceptLanguage explicitly.",
    );
  }
  if (geo.timezone && fp.timezone == null) fp.timezone = geo.timezone;
  if (geo.acceptLanguage && fp.acceptLanguage == null) fp.acceptLanguage = geo.acceptLanguage;
  if (geo.location && fp.location == null) fp.location = geo.location;
  // make WebRTC report the proxy egress IP too, coherent with HTTP egress (engine fabricates
  // the srflx candidate at this IP; no real STUN leaves the host).
  if (geo.ip && fp.webrtcIp == null) fp.webrtcIp = geo.ip;
  return geo;
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
  options: { executablePath?: string; version?: string; releaseChannel?: string; pro?: { licenseKey: string; licenseApiBase?: string } } & DownloadOptions = {}
): Promise<string> {
  if (options.executablePath) {
    // Caller-supplied tree (often a browser bundled into a packaged app): we did not install it, so
    // validate it here — a half-copied tree otherwise CHECK-crashes during browser startup.
    requireBinary(options.executablePath);
    checkInstall(options.executablePath);
    return options.executablePath;
  }
  if (process.env.CLEARCOTE_BINARY) {
    requireBinary(process.env.CLEARCOTE_BINARY);
    checkInstall(process.env.CLEARCOTE_BINARY);
    return process.env.CLEARCOTE_BINARY;
  }
  const version = options.version || process.env.CLEARCOTE_BROWSER_VERSION;
  if (version) {
    // Explicit version selector: validate against the catalog FIRST (clear error if it doesn't
    // exist or needs a license), then route free (GitHub) vs pro (authenticated route).
    return ensureVersion(version, {
      licenseKey: options.pro?.licenseKey,
      apiBase: options.pro?.licenseApiBase,
      cacheDir: options.cacheDir,
      quiet: options.quiet,
      releaseChannel: options.releaseChannel,
    });
  }
  if (options.pro) {
    return proEnsureBinary(options.pro.licenseKey, {
      apiBase: options.pro.licenseApiBase,
      cacheDir: options.cacheDir,
      quiet: options.quiet,
      releaseChannel: resolveReleaseChannel(options.releaseChannel),
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
 * versions need `licenseKey` / `CLEARCOTE_LICENSE_KEY`). Pin a PRO rebuild with "150.0.7871.114-r7"
 * (or bare "r7"). */
export async function download(
  options: DownloadOptions & { version?: string; licenseKey?: string; licenseApiBase?: string; releaseChannel?: string } = {},
): Promise<string> {
  const { version, licenseKey, licenseApiBase, releaseChannel, ...dl } = options;
  return executablePath({ version, releaseChannel, pro: proSelector(licenseKey, licenseApiBase), ...dl });
}

/** Headless: `viewport: null` is a CONTEXT option and `chromium.launch()` takes none, so it rides on
 * newPage/newContext instead, with the window fit to the work area (the persona's, or the display
 * `--screen-info` set). See ./geometry.ts for why each is needed. */
function installHeadlessGeometry(browser: Browser, args?: readonly string[] | null): void {
  // Same shape as installHeadedViewport. Each new context is a new window, so each also gets the
  // window fit. Any per-call geometry option wins.
  const merge = (o: Record<string, unknown> = {}) =>
    "viewport" in o || "screen" in o ? o : { ...o, viewport: null };
  const origNewPage = browser.newPage.bind(browser);
  const origNewContext = browser.newContext.bind(browser);
  (browser as unknown as { newPage: (o?: Record<string, unknown>) => Promise<Page> }).newPage =
    async (o = {}) => {
      const page = await origNewPage(merge(o) as Parameters<typeof origNewPage>[0]);
      await fitWindowToWorkArea(page, args);
      return page;
    };
  (browser as unknown as { newContext: (o?: Record<string, unknown>) => Promise<BrowserContext> }).newContext =
    async (o = {}) => {
      const context = await origNewContext(merge(o) as Parameters<typeof origNewContext>[0]);
      await installWindowFixup(context, args);
      return context;
    };
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
  proxyForQuic?: PwProxy,
  socks5Udp?: boolean,
  extra?: {
    exe?: string; headed?: boolean; quiet?: boolean; allowThirdPartyCookies?: boolean; transparentProxy?: boolean;
    /** Set when Playwright starts this browser (launch / launchPersistentContext, not serve): the
     * caller's ignoreDefaultArgs, `null` when they passed none. See playwrightFeatureOverrideArgs. */
    playwrightIgnoreDefaultArgs?: string[] | boolean | null;
  },
): string[] {
  // webBluetoothArgs: navigator.bluetooth must follow the CLAIM, in both directions. A Linux
  // build hides it while exposing usb/serial/hid (an OS-origin tell on a Windows persona); a
  // Windows build shows it under a LINUX claim, which genuine Chrome 154 on Linux does not. The
  // host is never consulted; whichever switch agrees with this build's default is a no-op.
  const base = [...fpArgs, ...agArgs, ...extArgs, ...proxyArgs, ...quicArgs(proxyForQuic), ...socks5UdpArgs(socks5Udp, proxyForQuic),
    ...webBluetoothArgs(
      fpArgs.find((a) => a.startsWith("--fingerprint-platform="))?.slice("--fingerprint-platform=".length),
    ),
  ];
  // DEFAULT FLIPPED IN 0.23.0 — opt IN to disabling, rather than opt out.
  //
  // Disabling Topics/FLEDGE/Shared Storage/Fenced Frames is coherent for a de-Googled persona, and
  // incoherent for the default one: `brand: "chrome"` claims Google Chrome, which ships all of
  // them. Measured on the live audit against 150-r10, "a build claiming Chrome carries the Privacy
  // Sandbox surface Chrome ships" failed as an implausible value — the same defect class as the
  // WebUSB split fixed in r7. Pass disablePrivacySandbox: true when the persona really is
  // de-Googled Chromium.
  // Playwright disables third-party storage partitioning, which genuine Chrome never does; re-emit
  // its list without that entry as the last --disable-features. See playwrightFeatureOverrideArgs.
  if (extra && extra.playwrightIgnoreDefaultArgs !== undefined) {
    base.push(...playwrightFeatureOverrideArgs(extra.playwrightIgnoreDefaultArgs ?? undefined));
  }
  if (disablePrivacySandbox === true) base.push(...privacySandboxArgs());
  base.push(...webrtcDefaultDenyArgs([...base, ...userArgs], webrtcIp));
  if (extra) {
    base.push(...engineExtrasArgs(extra, proxyForQuic, extra.quiet));
    base.push(...gpuBlocklistArgs(!!extra.headed, process.platform, userArgs));
  }
  const merged = mergeFeatureFlags([...base, ...userArgs]);
  // Last: drop 152 r22+ switches this engine does not implement (with a warning), wherever they came from.
  return extra ? gateEngineSwitches(extra.exe, merged, extra.quiet).args : merged;
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
  sweepRecoverDirs();
  const recover = join(mkdtempSync(join(tmpdir(), "clearcote-recover-")), "browser");
  cpSync(dirname(exe), recover, { recursive: true });
  warmFiles(recover);
  return doLaunch(join(recover, basename(exe)));
}

/**
 * Delete stale `clearcote-recover-*` copies left by earlier runs of the fallback above.
 *
 * That fallback copies the WHOLE browser (~400 MB on Windows) to a fresh temp dir and never
 * removed it, so a machine where the SxS/AV race fires on every launch accumulates one ~400 MB
 * directory per launch indefinitely — 75 of them were found on one dev box. It is also why the
 * Windows Firewall re-prompts forever: each launch runs from a path Windows has never seen, and
 * the random name means no per-path firewall rule can ever match.
 *
 * Best-effort by design: the directory belonging to a browser that is still running is locked on
 * Windows, so removal throws and we skip it — it will be swept by a later run instead. Anything
 * newer than `keepMs` is left alone so we never delete a copy a concurrent launch is mid-way
 * through creating. Set `CLEARCOTE_KEEP_RECOVER=1` to retain them for debugging.
 */
function sweepRecoverDirs(keepMs = 60_000): void {
  if (process.env.CLEARCOTE_KEEP_RECOVER) return;
  try {
    const tmp = tmpdir();
    const now = Date.now();
    for (const name of readdirSync(tmp)) {
      if (!name.startsWith("clearcote-recover-")) continue;
      const dir = join(tmp, name);
      try {
        if (now - statSync(dir).mtimeMs < keepMs) continue;  // possibly an in-flight launch
        rmSync(dir, { recursive: true, force: true });
      } catch {
        /* locked by a live browser, or vanished under us — a later sweep gets it */
      }
    }
  } catch {
    /* never let cleanup break a launch */
  }
}

/**
 * Resolve `profile: "auto"` into a `fingerprintProfile`, in place.
 *
 * Host GPU/display can only be read by rendering, so this may launch the engine once with NO
 * persona and cache the result (keyed by binary, 30 days). The nested launch passes no `profile`,
 * so it cannot recurse.
 *
 * An explicit `fingerprintProfile` always wins — if the caller already named a profile, "auto"
 * has nothing to decide and must not silently replace it.
 */
async function applyAutoProfile(
  fingerprint: FingerprintOptions,
  exe: string,
  opts: AutoOptions,
): Promise<void> {
  if (fingerprint.fingerprintProfile !== undefined) return;
  // RELEASE.version is only the SDK's PINNED default; measureHost reads the real major from the
  // engine it launches, which is what matters when the caller pinned a version or brought their
  // own binary.
  // THE NESTED LAUNCH NEEDS THE LICENSE TOO, and used to be given it only by accident.
  //
  // measureHost launches the SAME binary the real session will use. On PRO that is the gated
  // build: with no run-token the engine gate kills it at startup and Playwright surfaces
  // `TargetClosedError: Target page, context or browser has been closed` — which names neither
  // licensing nor a call the caller wrote. It only worked when the key happened to be in
  // CLEARCOTE_LICENSE_KEY, because launch() resolves that itself; passing licenseKey as an
  // option — the documented way — failed. opts.licenseKey is already resolved by the caller.
  //
  // ephemeralProfile: false — the probe reads GPU/display off about:blank and needs no profile,
  // so it skips the create+delete a persistent launch would otherwise pay on every resolution.
  const host = await measureHost(
    (o) => launch({
      ...(o as LaunchOptions),
      licenseKey: opts.licenseKey,
      licenseApiBase: opts.apiBase,
      ephemeralProfile: false,
      cloud: false, // the probe measures THIS host, whatever CLEARCOTE_CLOUD says
    }) as unknown as Promise<{
      newContext: () => Promise<{ newPage: () => Promise<unknown> }>;
      version?: () => string;
      close: () => Promise<void>;
    }>,
    exe,
    Number(String(RELEASE.version).split(".")[0]),
  );

  // BACKWARDS COMPATIBILITY. The free 149 engine has no persona-profile patch, and Chromium
  // discards unknown switches silently — so sending it a profile would leave the user with NO
  // persona while believing they had one. Fall back to the seed path that engine does support,
  // and say so, rather than failing or silently doing nothing.
  if (!engineSupportsProfiles(host.browser_major)) {
    if (fingerprint.fingerprint === undefined) {
      // A stable per-machine seed, so this degrades to a CONSISTENT identity rather than a new
      // one per launch — same reasoning as keyless "rotate".
      fingerprint.fingerprint = defaultStickyKey();
    }
    if (!opts.quiet) {
      process.stderr.write(
        `[clearcote] [profile] engine ${host.browser_major} does not support imported profiles ` +
          `(added in ${MIN_PROFILE_ENGINE_MAJOR}) — using the seed persona instead. ` +
          `Upgrade with version: "150" (PRO) for the profile path.\n`,
      );
    }
    return;
  }

  const { profile } = await resolveAuto(host, opts);
  // The corpus never probed the Windows 11 system fonts; a Windows 11 donor gets them back.
  fingerprint.fingerprintProfile = profileWithWindows11Faces(profile);
  // A seed alongside a profile is the combination that fails strict scoring, and it also makes
  // profile fields apply only partially. "auto" therefore never sets one — and says so if the
  // caller supplied one, rather than silently doing something different from what was asked.
  if (fingerprint.fingerprint !== undefined && !opts.quiet) {
    process.stderr.write(
      "[clearcote] [profile] warning: profile:\"auto\" with an explicit fingerprint seed — the " +
        "seed engages farbling, which strict anti-bots score as tampering and which makes " +
        "profile fields apply only partially. Drop `fingerprint` for the coherent path.\n",
    );
  }
}

/**
 * Layer a saved persona's options under the caller's (`profile:` a name, path or Profile).
 *
 * `profile: "auto"` is NOT a saved option-set — it resolves a real captured fingerprint once the
 * binary is known ({@link applyProfileAuto}) — so it must never reach Profile.load. Every entry
 * point goes through here: launchPersistentContext and serve() used to load "auto" as a saved
 * name and throw ENOENT for ~/.clearcote/profiles/auto.json, which broke the default launch().
 */
function mergeSavedProfile<T extends { profile?: string | Profile }>(options: T): T {
  return options.profile && options.profile !== "auto"
    ? { ...resolveProfileOptions(options.profile), ...options }
    : options;
}

/**
 * `profile: "auto"` -> resolve a REAL captured fingerprint for this host and apply it as
 * fingerprintProfile. Deliberately does NOT set a seed: with no --fingerprint the farbling
 * machinery stays off, which is the whole reason this path survives strict scoring.
 * Called once `exe` is known, because both the engine's Chromium major and the host GPU
 * measurement depend on the binary that will actually run. Pass-through (fingerprint "off") runs
 * with NO persona, so "auto" has nothing to apply.
 */
async function applyProfileAuto(
  profile: unknown,
  fingerprint: FingerprintOptions,
  exe: string,
  o: { quiet?: boolean; licenseKey?: string; licenseApiBase?: string; profileSelect?: AutoOptions },
): Promise<void> {
  if (profile !== "auto" || isFingerprintPassthrough(fingerprint.fingerprint)) return;
  await applyAutoProfile(fingerprint, exe, {
    quiet: o.quiet,
    licenseKey: resolveLicenseKey(o.licenseKey),
    // The profile service lives on the same backend as licensing, so a caller who overrode
    // one has overridden both; profilesource still prefers CLEARCOTE_PROFILE_API when set.
    apiBase: o.licenseApiBase,
    ...(o.profileSelect ?? {}),
  });
}

/**
 * Delete a throwaway profile directory — but only once the browser that used it has EXITED.
 *
 * NOT ON THE `close` EVENT. Playwright emits it when the browser's pipe drops, which on Linux is
 * ~100 ms BEFORE the browser process exits, and Chrome keeps writing the profile until then. Deleting
 * it at that point either fails (ENOTEMPTY: 28 of 30 traced launches) or succeeds and Chrome writes
 * the directory back — Default/, Local State, first_party_sets.db — after the cleanup has marked
 * itself done, so nothing retries. That left a clearcote-run-* directory in /tmp on the 0.36.0 release
 * smoke run. It also deleted the SingletonSocket link Chrome uses to find its own socket directory,
 * which then leaked as an org.chromium.Chromium.* directory.
 *
 * So no trigger deletes a profile under a live browser:
 *  - close(): Playwright resolves it only after the browser process has exited, so the directory is
 *    removed right then — and is gone by the time the caller's `await close()` returns;
 *  - `close` with no close() call (the browser crashed, or its last window was closed): wait for the
 *    browser's process to exit first (its pid is in the profile, see browserProcessFiles);
 *  - process exit: Playwright's own exit handler, registered at launch and so run before this one, has
 *    killed the browser by then.
 *
 * THE RETRY IS NOT DEFENSIVE PADDING — on Windows the browser can still hold handles under the
 * profile for a moment, so the first removal throws EBUSY/EPERM (measured on the Python port: the
 * directory survived a close plus a 1.5 s wait).
 */
function installEphemeralProfileCleanup(context: BrowserContext, userDataDir: string): void {
  const browser = browserProcessFiles(userDataDir);
  let done = false;
  const onExit = () => { if (!done) removeProfileDirSync(userDataDir, browser.socketDir); };
  let removing: Promise<void> | null = null;
  const removeAfterExit = (): Promise<void> => (removing ??= (async () => {
    if (await waitForProcessExit(browser.pid)) done = await removeProfileDir(userDataDir, browser.socketDir);
    if (done) process.off("exit", onExit); // one hook per launch would otherwise pile up for the process's life
    else removing = null; // still running, or out of retries: a later trigger tries again
  })());
  let closing = false;
  const close = context.close.bind(context);
  context.close = async (...args: Parameters<BrowserContext["close"]>) => {
    closing = true;
    try {
      return await close(...args);
    } finally {
      await removeAfterExit();
    }
  };
  context.on("close", () => { if (!closing) void removeAfterExit(); });
  process.once("exit", onExit);
}

/**
 * The browser's pid and its singleton-socket directory, read from the symlinks Chrome keeps in its
 * profile on Linux and macOS while it runs: SingletonLock -> "<hostname>-<pid>" and SingletonSocket ->
 * "<tmp>/org.chromium.Chromium.XXXXXX/SingletonSocket". Read right after launch, while both exist.
 * Windows has neither: nothing to wait for, and there its file locks make an early delete fail rather
 * than succeed.
 */
function browserProcessFiles(userDataDir: string): { pid: number | null; socketDir: string | null } {
  let pid: number | null = null;
  let socketDir: string | null = null;
  try {
    const n = Number(readlinkSync(join(userDataDir, "SingletonLock")).split("-").pop());
    if (Number.isInteger(n) && n > 0) pid = n;
  } catch { /* no lock: nothing to wait for */ }
  try {
    const dir = dirname(readlinkSync(join(userDataDir, "SingletonSocket")));
    // Only ever Chrome's own temp directory: the path comes out of a file, so never delete just anything.
    if (/^(org\.chromium\.Chromium|com\.google\.Chrome)\./.test(basename(dir))) socketDir = dir;
  } catch { /* no socket */ }
  return { pid, socketDir };
}

/** Wait for a process to exit: true once it has (or when there is no pid), false if still running at the deadline. */
async function waitForProcessExit(pid: number | null, timeoutMs = 10_000): Promise<boolean> {
  if (pid === null) return true;
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      process.kill(pid, 0);
    } catch (e) {
      if ((e as NodeJS.ErrnoException).code === "ESRCH") return true; // EPERM: alive, someone else's
    }
    if (Date.now() >= deadline) return false;
    await new Promise((r) => setTimeout(r, 50));
  }
}

/**
 * Remove a profile directory (and the browser's socket dir), retrying while the browser still holds
 * handles (see above). A null profile: only the socket dir, for a profile that is the caller's.
 */
async function removeProfileDir(userDataDir: string | null, socketDir: string | null = null): Promise<boolean> {
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      if (socketDir) rmSync(socketDir, { recursive: true, force: true });
      if (userDataDir) rmSync(userDataDir, { recursive: true, force: true });
      return true;
    } catch {
      await new Promise((r) => setTimeout(r, 250 * (attempt + 1))); // 0.25→1.5s, ~5s total
    }
  }
  return false;
}

/**
 * removeProfileDir for process exit. Synchronous: an exit handler cannot await, and an unresolved
 * promise at exit removes nothing. It retries all the same: on Windows the killed browser's handles
 * outlive it for a moment, and a single attempt left the profile behind (1 of 3 exits with the browser
 * open on 0.36.0).
 */
function removeProfileDirSync(userDataDir: string | null, socketDir: string | null): void {
  const pause = new Int32Array(new SharedArrayBuffer(4));
  for (let attempt = 0; attempt < 8; attempt++) {
    try {
      if (socketDir) rmSync(socketDir, { recursive: true, force: true });
      if (userDataDir) rmSync(userDataDir, { recursive: true, force: true });
      return;
    } catch {
      Atomics.wait(pause, 0, 0, 50 * (attempt + 1)); // 50→400 ms, ~1.8 s at most
    }
  }
}

/**
 * Stop a browser serve() started, and wait for it to exit: SIGTERM (on Windows, TerminateProcess),
 * then SIGKILL once `graceMs` has passed. True once it has exited.
 */
async function stopServedBrowser(proc: ChildProcess, graceMs: number): Promise<boolean> {
  const exited = () => proc.exitCode !== null || proc.signalCode !== null;
  const waitForExit = (ms: number) => new Promise<boolean>((resolve) => {
    if (exited()) return resolve(true);
    const onExit = () => { clearTimeout(timer); resolve(true); };
    const timer = setTimeout(() => { proc.off("exit", onExit); resolve(exited()); }, ms);
    proc.once("exit", onExit);
  });
  if (exited()) return true;
  try { proc.kill("SIGTERM"); } catch { /* gone */ }
  if (await waitForExit(graceMs)) return true;
  try { proc.kill("SIGKILL"); } catch { /* gone */ }
  return waitForExit(5_000);
}

/**
 * stopServedBrowser for process exit, where nothing can be awaited. Nor can the child's exit be seen
 * the usual way: the event loop reaps exited children, and it no longer runs, so an exited browser
 * stays a zombie that signal 0 still finds. Linux reads its state from /proc, macOS asks ps.
 */
function stopServedBrowserSync(proc: ChildProcess, graceMs: number): boolean {
  const pid = proc.pid;
  if (pid === undefined || proc.exitCode !== null || proc.signalCode !== null) return true;
  const gone = (): boolean => {
    try {
      process.kill(pid, 0); // on Windows this asks whether the process is still active
    } catch (e) {
      return (e as NodeJS.ErrnoException).code === "ESRCH";
    }
    if (process.platform === "win32") return false;
    if (process.platform === "linux") {
      try {
        const stat = readFileSync(`/proc/${pid}/stat`, "utf8"); // "<pid> (<name>) <state> ..."
        return stat[stat.lastIndexOf(")") + 2] === "Z";
      } catch {
        return true;
      }
    }
    // ps prints the state, Z for a zombie, and nothing once the pid is gone.
    const ps = spawnSync("ps", ["-o", "stat=", "-p", String(pid)], { encoding: "utf8" });
    if (ps.error) return false;
    const state = (ps.stdout ?? "").trim();
    return state === "" || state.startsWith("Z");
  };
  const pause = new Int32Array(new SharedArrayBuffer(4));
  const waitForExit = (ms: number) => {
    for (const deadline = Date.now() + ms; ;) {
      if (gone()) return true;
      if (Date.now() >= deadline) return false;
      Atomics.wait(pause, 0, 0, 25);
    }
  };
  try { proc.kill("SIGTERM"); } catch { /* gone */ }
  if (waitForExit(graceMs)) return true;
  try { proc.kill("SIGKILL"); } catch { /* gone */ }
  return waitForExit(2_000);
}

/**
 * A persistent context on a fresh temp profile the caller never named, so never sees again: it is
 * removed when the context closes and at process exit — and at once when the launch fails, which
 * used to leak one directory per failed launch.
 */
async function launchOnThrowawayProfile(prefix: string, options: PersistentContextOptions): Promise<BrowserContext> {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  let context: BrowserContext;
  try {
    // cloud: false — the caller already chose a local launch; CLEARCOTE_CLOUD must not re-route it.
    context = await launchPersistentContext(dir, { ...options, cloud: false });
  } catch (e) {
    await removeProfileDir(dir);
    throw e;
  }
  installEphemeralProfileCleanup(context, dir);
  return context;
}

/**
 * Make a persistent BrowserContext satisfy code written against launch()'s Browser.
 *
 * `newContext()` returns THE PERSISTENT CONTEXT ITSELF rather than a fresh incognito one. That is
 * deliberate: a real incognito context would silently leave the profile behind — taking the
 * Widevine CDM and the component-updated state with it — handing back exactly the browser this
 * change exists to stop producing. Two calls returning the same context is a visible, documented
 * compromise; quietly returning a profile-less browser is not.
 */
function asBrowserLike(context: BrowserContext): Browser {
  const c = context as BrowserContext & { newContext?: unknown; contexts?: unknown };
  if (c.newContext === undefined) c.newContext = async () => context;
  if (c.contexts === undefined) c.contexts = () => [context];
  return context as unknown as Browser;
}

/**
 * Launch Clearcote and return a Playwright browser handle backed by a REAL Chrome profile.
 *
 * PROFILE-BACKED BY DEFAULT (changed in 0.23.0). This used to be `chromium.launch()` — incognito,
 * no profile directory. Incognito cannot load a component-updated CDM, so
 * `requestMediaKeySystemAccess('com.widevine.alpha')` rejected and the EME surface was a
 * no-Widevine tell on a build branded Google Chrome (measured against the live audit on 150-r10).
 * It now launches a persistent context on a throwaway directory, so `widevine: true` works here.
 *
 * The directory is deleted when the context closes AND on process exit, so nothing is left behind
 * and no state survives to the next launch — the incognito-like isolation callers relied on is
 * preserved. Pass `userDataDir` to keep a profile, or `ephemeralProfile: false` to opt back out.
 */
/**
 * Start a browser; if it fails to start, release the lease before re-throwing. On a per-browser plan
 * (the free tier) the slot would otherwise stay taken until the lease TTL. Exported for tests.
 *
 * `launchToken` (the launch's run-token file) goes too: a paid lease is shared, so its stop() only drops
 * a reference, and the file stayed in the temp directory until the process exited.
 */
export async function releaseLeaseOnFailure<T>(
  lease: LeaseSession | null, start: () => Promise<T>, launchToken?: { release(): void } | null,
): Promise<T> {
  try {
    return await start();
  } catch (e) {
    try { await lease?.stop(); } catch { /* ignore: the original failure is what matters */ }
    launchToken?.release();
    throw e;
  }
}

/**
 * A browser the caller named must exist. Handed a missing one, Playwright fails only after creating
 * temp directories it then leaves behind (playwright-artifacts-*, playwright_chromiumdev_profile-*),
 * and a licensed launch would have taken a lease first. Same message as the .NET SDK.
 */
function requireBinary(path: string): void {
  if (!existsSync(path)) {
    throw new Error(`Clearcote binary not found at '${path}'. Set executablePath / CLEARCOTE_BINARY, or let the SDK auto-download it.`);
  }
}

/**
 * Start a browser; if the PRO engine refuses the run-token as older than one it has already accepted on
 * this machine ("older than the last one accepted"), mint a fresh token and start once more.
 *
 * acquireLease() already replaces a token it can SEE is older than the engine's mark. This covers the race
 * it cannot see: another process (a parallel launch, the hosted-browser gateway, another key) moving the
 * mark between that check and this launch. `start` must build its env from `lease.token` each time it is
 * called; the bound token file follows the lease by itself. Playwright's launch error carries the engine's
 * stderr, which is where the refusal lands. Exported for tests.
 */
export async function retryOnStaleRunToken<T>(lease: LeaseSession | null, start: () => Promise<T>): Promise<T> {
  try {
    return await start();
  } catch (e) {
    const refused = String((e as Error)?.message ?? e).includes(STALE_TOKEN_REFUSAL);
    if (!lease?.refreshToken || !refused || !(await lease.refreshToken())) throw e;
    return await start();
  }
}

/**
 * LOCAL OR CLOUD. `cloud: true` runs the browser on Clearcote's servers instead and resolves to the
 * same Playwright `Browser`, connected over CDP, with `humanize` applied here exactly as for a local
 * browser. `cloud` unset follows CLEARCOTE_CLOUD=1|true|yes. The API key comes from `apiKey` or
 * CLEARCOTE_API_KEY; the cloud options are {@link CloudLaunchOptions}, and an option a cloud browser
 * cannot take (executablePath, args, userDataDir, ...) throws naming it. `close()` disconnects and
 * ends the session.
 */
export async function launch(options: LaunchOptions | CloudLaunchOptions = {}): Promise<Browser> {
  if (cloudRequested((options as CloudSwitchOption).cloud)) {
    return (await launchCloud(options as Record<string, unknown>)) as Browser;
  }
  // ephemeralProfile: false restores the pre-0.23 incognito launch. Kept because the persistent
  // path costs a directory create+delete per launch, which a caller spawning hundreds of
  // short-lived browsers may reasonably not want to pay for a CDM they never touch.
  const { ephemeralProfile, userDataDir, ...restOpts } = withoutCloudOptions(options as LaunchOptions) as LaunchOptions & {
    ephemeralProfile?: boolean;
    userDataDir?: string;
  };
  if (userDataDir !== undefined) {
    return asBrowserLike(await launchPersistentContext(userDataDir, { ...(restOpts as PersistentContextOptions), cloud: false }));
  }
  if (ephemeralProfile !== false) {
    return asBrowserLike(await launchOnThrowawayProfile("clearcote-run-", restOpts as PersistentContextOptions));
  }
  return launchIncognito(restOpts as LaunchOptions);
}

/** The pre-0.23 incognito launch, reached via `ephemeralProfile: false`. */
async function launchIncognito(options: LaunchOptions = {}): Promise<Browser> {
  // profile= a saved persona: its options are the base, explicit options override. ("auto" is
  // resolved later, once the executable is known.)
  const merged = mergeSavedProfile(options);
  const { profile, profileSelect, extensions, portableProfile, shaderDialect, socks5Udp, encryptionKey, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, version, licenseKey, licenseApiBase, licenseThroughProxy, releaseChannel, allowThirdPartyCookies, transparentProxy, ...rest } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy;  // captured before resolveProxy drops it
  const geo = geoip ? await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy, quiet) : undefined;
  // A rotating proxy changes the exit per connection; checked alongside the launch, awaited at the end.
  const egressDrift = startEgressDriftCheck((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined, geo?.ip ?? undefined, quiet);
  // The binary is resolved before the proxy route is chosen: http(s) credentials go to the
  // engine's --proxy-auth only when THIS engine implements it (r19+); older engines keep
  // Playwright's handling, which authenticates (routing blindly would leave every request at 407).
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, releaseChannel, pro: proSelector(licenseKey, licenseApiBase) });  // capability-gated proxy route
  ensureRunnableHere(exe);
  // Before the warnings below, so they judge the persona that will actually run (as in Python).
  await applyProfileAuto(profile, fingerprint, exe, { quiet, licenseKey, licenseApiBase, profileSelect });
  // SOCKS5-with-credentials must go through --proxy-server (Playwright rejects it); drop it from PW.
  const { args: proxyArgs, proxy } = resolveProxy((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined, engineSupportsSwitch(exe, "proxy-auth"));
  warnUnsupportedEngineOptions(exe, fingerprint as Record<string, unknown>, proxyOpt as PwProxy | undefined, quiet);
  // proxy unchanged unless it was rerouted to --proxy-server (drop it from Playwright) or its URL
  // carried the credentials (hand Playwright the fields it reads)
  if (proxy === undefined) delete (pwOptions as Record<string, unknown>).proxy;
  else (pwOptions as PlaywrightLaunchOptions).proxy = proxy as PlaywrightLaunchOptions["proxy"];
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless: (pwOptions as PlaywrightLaunchOptions).headless,
      devtools: (pwOptions as Record<string, unknown>).devtools, userAgent: (pwOptions as Record<string, unknown>).userAgent,
      _userArgs: args ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  const headed = (pwOptions as PlaywrightLaunchOptions).headless === false;
  // License (opt-in): check out a concurrency slot and inject CLEARCOTE_RUN_TOKEN so the PRO
  // engine gate lets the browser launch. Inert (null) in free mode / when no key is set.
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION, licenseThroughProxy, proxy: proxyOpt as PwProxy | undefined,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  const engineArgs = assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)], proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, args ?? [], proxyOpt as PwProxy | undefined, socks5Udp,
    { exe, headed, quiet, allowThirdPartyCookies, transparentProxy,
      playwrightIgnoreDefaultArgs: ((pwOptions as PlaywrightLaunchOptions).ignoreDefaultArgs as string[] | boolean | undefined) ?? null });
  // On Linux, point FONTCONFIG_FILE at the bundled metric-compatible clones (Segoe UI, Arial, …)
  // and LANGUAGE at the persona's UI locale (after engineArgs: it reads their --lang).
  const launchEnv = withShaderDialect(shaderDialect, fontLaunchEnv(exe, (pwOptions as PlaywrightLaunchOptions).env, engineArgs), engineArgs);
  const launchToken = lease?.bindLaunch();
  // A function: a launch retried after a stale-token refusal must carry the lease's fresh token.
  const runtimeEnv = () => (lease ? withRunToken(lease.token, launchEnv, launchToken?.file) : launchEnv);
  // Headless: screen.* has to be handled alongside the viewport or the window reports a geometry no
  // real browser can (see ./geometry.ts). Probe a copy — viewport is a context option, which
  // chromium.launch() does not take — and carry the result to newPage/newContext. The display
  // switches go on the command line; the fit keeps reading the caller's own engineArgs.
  const geom = headed
    ? null
    : applyHeadlessGeometry({ ...(pwOptions as Record<string, unknown>) }, fingerprint.fingerprint, engineArgs, fingerprint);
  const browser = await releaseLeaseOnFailure(lease, () => retryOnStaleRunToken(lease, () => winAvRetry((exePath) => {
    const env = runtimeEnv();
    return chromium.launch({
      // Drop Playwright's --enable-automation (keeps AutomationControlled off), --enable-unsafe-swiftshader
      // (see DEFAULT_IGNORED_ARGS; paired with --ignore-gpu-blocklist in assembleArgs) and the headless
      // --hide-scrollbars. Caller can override via ignoreDefaultArgs.
      ignoreDefaultArgs: [...DEFAULT_IGNORED_ARGS],
      ...(pwOptions as PlaywrightLaunchOptions),
      executablePath: exePath,
      ...(env ? { env } : {}),
      args: [...engineArgs, ...(geom?.args ?? [])],
    });
  }, exe)), launchToken);
  // Release the concurrency slot + remove the run-token file when the browser closes.
  if (lease) browser.on("disconnected", () => { void lease.stop(); launchToken?.release(); });
  if (headed) installHeadedViewport(browser); // launch() takes no viewport option -> wrap newPage/newContext
  else if (geom) installHeadlessGeometry(browser, engineArgs);
  installHumanize(browser, { humanize, showCursor, seed: fingerprint.fingerprint }); // seed => stable motor persona
  await egressDrift;
  return browser;
}

/** Options of a cloud {@link launchPersistentContext}: a cloud launch on a named cloud profile. */
export interface CloudPersistentOptions extends Omit<CloudLaunchOptions, "profile"> {
  /** The cloud profile whose cookies the session loads and saves back when the context closes. */
  profile: CloudProfile;
}

/**
 * Launch Clearcote with a persistent profile directory and return a Playwright
 * {@link BrowserContext} (cookies, storage, etc. persist in `userDataDir`).
 *
 * CLOUD: `launchPersistentContext({ cloud: true, profile: "name" })` resolves to the context of a
 * hosted session that loads the cloud profile `name` and saves it back when the context closes
 * (which also ends the session). A cloud browser has no local directory: a `userDataDir` with
 * `cloud: true` throws, pointing at `profile`.
 */
export async function launchPersistentContext(options: CloudPersistentOptions): Promise<BrowserContext>;
export async function launchPersistentContext(userDataDir: string, options?: PersistentContextOptions): Promise<BrowserContext>;
export async function launchPersistentContext(
  dirOrOptions: string | CloudPersistentOptions | null | undefined,
  maybeOptions: PersistentContextOptions = {},
): Promise<BrowserContext> {
  const optionsFirst = dirOrOptions !== null && typeof dirOrOptions === "object";
  const options = (optionsFirst ? dirOrOptions : maybeOptions) as PersistentContextOptions;
  const userDataDir = optionsFirst ? undefined : (dirOrOptions as string | null | undefined);
  if (cloudRequested(options.cloud)) {
    return (await launchCloud(options as Record<string, unknown>, true, userDataDir)) as BrowserContext;
  }
  if (typeof userDataDir !== "string") {
    throw new TypeError('launchPersistentContext() needs a userDataDir (or { cloud: true, profile: "name" } for a cloud profile)');
  }
  return launchLocalPersistentContext(userDataDir, withoutCloudOptions(options));
}

async function launchLocalPersistentContext(
  userDataDir: string,
  options: PersistentContextOptions = {}
): Promise<BrowserContext> {
  const merged = mergeSavedProfile(options);
  const { profile, profileSelect, extensions, portableProfile, shaderDialect, socks5Udp, encryptionKey, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, widevine, version, licenseKey, licenseApiBase, licenseThroughProxy, releaseChannel, allowThirdPartyCookies, transparentProxy, ...rest } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy;  // captured before resolveProxy drops it
  const geo = geoip ? await applyGeoip(fingerprint, (pwOptions as PlaywrightLaunchOptions).proxy, quiet) : undefined;
  // A rotating proxy changes the exit per connection; checked alongside the launch, awaited at the end.
  const egressDrift = startEgressDriftCheck((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined, geo?.ip ?? undefined, quiet);
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, releaseChannel, pro: proSelector(licenseKey, licenseApiBase) });  // capability-gated proxy route
  ensureRunnableHere(exe);
  await applyProfileAuto(profile, fingerprint, exe, { quiet, licenseKey, licenseApiBase, profileSelect });
  const { args: proxyArgs, proxy } = resolveProxy((pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined, engineSupportsSwitch(exe, "proxy-auth"));
  warnUnsupportedEngineOptions(exe, fingerprint as Record<string, unknown>, proxyOpt as PwProxy | undefined, quiet);
  if (proxy === undefined) delete (pwOptions as Record<string, unknown>).proxy;
  else (pwOptions as PlaywrightLaunchOptions).proxy = proxy as PlaywrightLaunchOptions["proxy"];
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless: (pwOptions as PlaywrightLaunchOptions).headless,
      devtools: (pwOptions as Record<string, unknown>).devtools, userAgent: (pwOptions as Record<string, unknown>).userAgent,
      _userArgs: args ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  const opts = pwOptions as PlaywrightLaunchOptions & BrowserContextOptions;
  // headed + no explicit viewport -> disable the emulated viewport (impossible-window tell)
  if (opts.headless === false && opts.viewport === undefined) opts.viewport = null;
  // widevine=true: seed the CDM into the profile + un-suppress the component updater (Playwright
  // disables it by default) so the engine registers it. Failure -> DRM gracefully off, launch proceeds.
  // Default the automation strip BEFORE the Widevine helper so it appends --disable-component-update
  // to ['--enable-automation'] rather than clobbering it (losing the strip). Caller's own wins.
  let ignoreDefaultArgs: string[] | boolean | undefined =
    (opts.ignoreDefaultArgs as string[] | boolean | undefined) ?? [...DEFAULT_IGNORED_ARGS];
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
  // License (opt-in): check out a concurrency slot + inject CLEARCOTE_RUN_TOKEN. Inert in free mode.
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION, licenseThroughProxy, proxy: proxyOpt as PwProxy | undefined,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  const engineArgs = assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)], proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs, proxyOpt as PwProxy | undefined, socks5Udp,
    { exe, headed: opts.headless === false, quiet, allowThirdPartyCookies, transparentProxy,
      playwrightIgnoreDefaultArgs: ignoreDefaultArgs ?? null });
  const ctxEnv = withShaderDialect(shaderDialect, fontLaunchEnv(exe, (opts as PlaywrightLaunchOptions).env, engineArgs), engineArgs);
  const launchToken = lease?.bindLaunch();
  // A function: a launch retried after a stale-token refusal must carry the lease's fresh token.
  const runtimeEnv = () => (lease ? withRunToken(lease.token, ctxEnv, launchToken?.file) : ctxEnv);
  // headless: the persona owns screen when it is running, so only the window needs fitting; with no
  // persona the SDK sets the headless display itself (see ./geometry.ts). Headed already set
  // viewport: null.
  const geom = opts.headless === false
    ? null
    : applyHeadlessGeometry(opts as unknown as Record<string, unknown>, fingerprint.fingerprint, engineArgs, fingerprint);
  const context = await releaseLeaseOnFailure(lease, () => retryOnStaleRunToken(lease, () => winAvRetry((exePath) => {
    const env = runtimeEnv();
    return chromium.launchPersistentContext(userDataDir, {
      ...opts,
      ignoreDefaultArgs,  // keep AutomationControlled off (+ component updater on when widevine)
      executablePath: exePath,
      ...(env ? { env } : {}),
      args: [...engineArgs, ...(geom?.args ?? [])],
    });
  }, exe)), launchToken);
  if (lease) context.on("close", () => { void lease.stop(); launchToken?.release(); });
  if (geom) await installWindowFixup(context, engineArgs);
  installHumanizeOnContext(context, { humanize, showCursor, seed: fingerprint.fingerprint }); // seed => stable motor persona
  await egressDrift;
  return context;
}

/** Options for {@link launchAgent}: persistent-context options + an optional `userDataDir`. */
export interface LaunchAgentOptions extends PersistentContextOptions {
  /** Profile directory to persist (cookies/storage/logins). Defaults to a fresh temp dir that is
   * deleted when the context closes. */
  userDataDir?: string;
}

/**
 * Launch Clearcote ready for the in-browser AI agent and return a Playwright {@link BrowserContext}.
 *
 * The agent drives Chrome's Actor framework, which only attaches to a **regular profile** — not
 * incognito — so this uses a *persistent* context: a fresh temp `userDataDir`, deleted when the
 * context closes, unless you pass one to keep.
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
  // the agent drives the LOCAL engine's Actor framework: never a cloud browser
  if (userDataDir !== undefined) return launchPersistentContext(userDataDir, { ...rest, cloud: false });
  return launchOnThrowawayProfile("clearcote-agent-", rest);
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
  /**
   * Headless: the outer window size in CSS px, clamped to the display's work area so the window can
   * never be larger than its screen. Default: the whole work area (a maximized window). Ignored when
   * headed, and when `args` carries a window or display switch (the caller then owns geometry).
   */
  windowSize?: { width: number; height: number };
}

/**
 * Handle for a standing clearcote CDP endpoint. Use `.cdpUrl` with any CDP client.
 *
 * close() WAITS FOR THE BROWSER TO EXIT, then removes what it leaves in the temp directory: the
 * profile serve() made, and on Linux and macOS the browser's singleton-socket directory
 * (org.chromium.Chromium.*), whichever profile it ran on. A browser stopped with a signal never
 * removes that directory itself, and close() used to signal it and return at once: every serve() +
 * close() left one behind, and the profile went while the browser was still writing it.
 */
export class Server {
  /** Where the browser's singleton socket lives, read while it runs (see browserProcessFiles). */
  private readonly socketDir: string | null;
  private closing: Promise<void> | null = null;
  private cleanedUp = false;
  // Process exit: Node runs no async work there, so close()'s steps run synchronously.
  private readonly onProcessExit = (): void => {
    if (this.cleanedUp) return;
    stopServedBrowserSync(this.proc, 5_000);
    this.lease?.stop().catch(() => { /* best-effort */ });
    this.launchToken?.release();
    removeProfileDirSync(this.ownUdd ? this.userDataDir : null, this.socketDir ?? browserProcessFiles(this.userDataDir).socketDir);
    this.cleanedUp = true;
  };

  constructor(
    private readonly proc: ChildProcess,
    readonly host: string,
    readonly port: number,
    private readonly userDataDir: string,
    private readonly ownUdd: boolean,
    private readonly lease?: LeaseSession | null,
    private readonly launchToken?: { file: string; release(): void } | null,
  ) {
    // Now, while the browser runs: one that shuts down cleanly unlinks it from the profile.
    this.socketDir = browserProcessFiles(userDataDir).socketDir;
    process.once("exit", this.onProcessExit);
  }
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
  /** OS process id of the browser, when running. */
  get pid(): number | undefined {
    return this.proc.pid;
  }
  isAlive(): boolean {
    return this.proc.exitCode === null && !this.proc.killed;
  }
  /**
   * Stop the browser and wait for it to exit (SIGTERM, then SIGKILL after 10 s), release the licence
   * slot, then remove the temp profile serve() made and the browser's socket directory.
   */
  close(): Promise<void> {
    return (this.closing ??= (async () => {
      if (this.cleanedUp) return;
      const socketDir = this.socketDir ?? browserProcessFiles(this.userDataDir).socketDir;
      await stopServedBrowser(this.proc, 10_000);
      // Release the concurrency slot (best-effort) + remove the run-token file.
      try { await this.lease?.stop(); } catch { /* ignore */ }
      this.launchToken?.release();
      await removeProfileDir(this.ownUdd ? this.userDataDir : null, socketDir);
      this.cleanedUp = true;
      process.off("exit", this.onProcessExit); // one per serve() would otherwise pile up
    })());
  }
}

/** serve() as root on Linux needs --no-sandbox (unless the caller already passed it). */
export function serveNeedsNoSandbox(platform: string, uid: number | undefined, args: string[]): boolean {
  return platform === "linux" && uid === 0 && !args.includes("--no-sandbox");
}

/**
 * The switch that keeps the engine's "unsupported command-line flag" warning bar off a served browser.
 * Any flag on Chromium's list raises it, --no-sandbox among them (which serve adds as root on Linux),
 * and it lands on the first tab: 56px off that tab's innerHeight, a frame (outer - inner) no other tab
 * and no real Chrome has. launch() never shows it: Playwright starts that browser without a startup
 * window, and passes --disable-infobars to a persistent context.
 * - Headless: --disable-infobars, the same switch. Chromium honours it only in headless, where it
 *   suppresses infobars and nothing else, so every headless serve gets it.
 * - Headed: Chromium ignores --disable-infobars. The one switch that drops the warning is --test-type,
 *   which also turns on test-harness behaviour (chrome.test in extension pages, no component
 *   extensions with background pages, no OS integration for installed web apps), none of it visible
 *   to a page. So only with --no-sandbox, the flag root on Linux cannot run without, and bare:
 *   --test-type=webdriver would also waive Payment Request's user-interaction check.
 */
export function serveInfobarArgs(headless: boolean, args: readonly string[]): string[] {
  const has = (sw: string) => args.some((a) => a === sw || a.startsWith(`${sw}=`));
  if (headless) return has("--disable-infobars") ? [] : ["--disable-infobars"];
  return has("--no-sandbox") && !has("--test-type") ? ["--test-type"] : [];
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
    windowSize,
    humanize: _humanize, // Playwright-only; not applicable to a direct launch
    showCursor: _showCursor,
    ...launchOpts
  } = options;
  if (windowSize !== undefined) {
    const ok = (v: unknown) => typeof v === "number" && Number.isInteger(v) && v >= 100 && v <= 10000;
    if (!windowSize || !ok(windowSize.width) || !ok(windowSize.height)) {
      throw new TypeError("clearcote serve: windowSize must be { width, height } in whole CSS px, 100-10000");
    }
  }

  // Build the same stealth arg set as launch(), then launch the binary ourselves.
  const merged = mergeSavedProfile(launchOpts);
  const {
    profile, profileSelect, extensions, portableProfile, shaderDialect, socks5Udp, encryptionKey, disablePrivacySandbox, executablePath: exeOption,
    args: userArgs, geoip, autoUpdate, cacheDir, quiet, version, licenseKey, licenseApiBase, licenseThroughProxy, releaseChannel, allowThirdPartyCookies, transparentProxy, ...rest
  } = merged;
  const { fingerprint, rest: afterFp } = splitFingerprintOptions(rest);
  const { agent, rest: pwOptions } = splitAgentOptions(afterFp);
  const proxyOpt = (pwOptions as PlaywrightLaunchOptions).proxy as PwProxy | undefined;
  const geo = geoip ? await applyGeoip(fingerprint, proxyOpt, quiet) : undefined;
  // A rotating proxy changes the exit per connection; checked alongside the launch, awaited at the end.
  const egressDrift = startEgressDriftCheck(proxyOpt, geo?.ip ?? undefined, quiet);
  const exe = await executablePath({ executablePath: exeOption, version, autoUpdate, cacheDir, quiet, releaseChannel, pro: proSelector(licenseKey, licenseApiBase) });  // capability-gated proxy route
  ensureRunnableHere(exe);
  await applyProfileAuto(profile, fingerprint, exe, { quiet, licenseKey, licenseApiBase, profileSelect });
  // passProxy: what is left for a plain --proxy-server once credentials went to the engine's own
  // switches (undefined then — proxyArgs already carries --proxy-server).
  const { args: proxyArgs, proxy: passProxy } = resolveProxy(proxyOpt, engineSupportsSwitch(exe, "proxy-auth"));
  warnUnsupportedEngineOptions(exe, fingerprint as Record<string, unknown>, proxyOpt, quiet);
  emitCoherenceWarnings(
    { ...fingerprint, proxy: proxyOpt, geoip, headless, _userArgs: userArgs ?? [] },
    quiet, process.platform, String(RELEASE.version).split(".")[0]);
  // A license key selects the PRO (gated) binary; no key -> the free binary (unchanged path).
  const engineArgs = assembleArgs(
    fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)],
    proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs ?? [], proxyOpt, socks5Udp,
    // serve launches the binary directly, so Playwright's SwiftShader default is never added; the
    // blocklist rule still applies to a headed endpoint and on Windows.
    { exe, headed: !headless, quiet, allowThirdPartyCookies, transparentProxy });

  const resolvedPort = port ?? (await freePort());
  const ownUdd = !uddOption;
  const userDataDir = uddOption ?? mkdtempSync(join(tmpdir(), "clearcote-serve-"));
  const origins = allowOrigins ?? `http://${host}:${resolvedPort},http://localhost:${resolvedPort}`;
  // A non-loopback bind or a "*" origin list hands the browser to whoever can reach the port.
  emitWarnings(serveExposureWarnings(host, origins), quiet);
  const cdpArgs = [
    `--remote-debugging-port=${resolvedPort}`,
    `--remote-debugging-address=${host}`,
    `--remote-allow-origins=${origins}`,
    `--user-data-dir=${userDataDir}`,
  ];
  if (headless) cdpArgs.push("--headless=new");
  // Never the caller's raw server: it comes after proxyArgs, so a second --proxy-server would win,
  // and one still carrying user:pass@ is rejected by Chromium's parser — every request then failed
  // with ERR_NO_SUPPORTED_PROXIES (measured on r27).
  if (passProxy?.server) cdpArgs.push(`--proxy-server=${passProxy.server}`);
  // Chromium refuses to start as root without --no-sandbox, and serve spawns the binary itself, so
  // Playwright's own --no-sandbox is missing: `clearcote serve` in a root container just timed out.
  if (serveNeedsNoSandbox(process.platform, process.getuid?.(), engineArgs)) cdpArgs.push("--no-sandbox");
  // ...and the warning bar that flag (or any of the caller's on Chromium's list) puts on the first tab.
  cdpArgs.push(...serveInfobarArgs(headless, [...engineArgs, ...cdpArgs]));
  // Headless geometry for a raw endpoint: the display and window are set browser-wide, since no
  // client's context options or CDP overrides would reach every page (see ./geometry.ts).
  const geometry = servedGeometry(engineArgs, fingerprint, headless);
  if (geometry) cdpArgs.push(...geometry.args);

  // License (opt-in): check out a concurrency slot + inject CLEARCOTE_RUN_TOKEN. Inert in free mode.
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION, licenseThroughProxy, proxy: proxyOpt,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  const launchToken = lease?.bindLaunch();
  const env = { ...process.env, ...(withShaderDialect(shaderDialect, fontLaunchEnv(exe, undefined, engineArgs), engineArgs) ?? {}), ...(lease ? { CLEARCOTE_RUN_TOKEN: lease.token, ...(launchToken ? { CLEARCOTE_RUN_TOKEN_FILE: launchToken.file } : {}) } : {}) };
  // Launched DIRECTLY (no Playwright) => no --enable-automation => navigator.webdriver stays false.
  // Wrap in winAvRetry so a just-extracted binary survives the Windows SxS/AV first-launch race
  // ("spawn UNKNOWN"), same as launch(): warm + back off + retry, then recover from a fresh copy.
  const proc = await releaseLeaseOnFailure(lease, () => winAvRetry(
    (exePath) => new Promise<ChildProcess>((resolve, reject) => {
      let settled = false;
      const p = spawn(exePath, [...engineArgs, ...cdpArgs], { env, stdio: "ignore" });
      p.once("error", (err) => { if (!settled) { settled = true; reject(err); } });
      p.once("spawn", () => { if (!settled) { settled = true; resolve(p); } });
    }),
    exe,
  ));

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
  const srv = new Server(proc, host, resolvedPort, userDataDir, ownUdd, lease, launchToken);
  if (!ready) {
    await srv.close();
    throw new Error(
      `clearcote serve: CDP endpoint at http://${host}:${resolvedPort} did not come up within ${readyTimeoutMs}ms`);
  }
  // Before any client attaches: the window onto the work area (and, under a persona, the headless
  // display onto the persona's). Its own connection, closed again; never fails the launch.
  if (geometry) await fitServedWindow(await srv.wsUrl().catch(() => undefined), { persona: geometry.persona, windowSize });
  if (!quiet) {
    process.stderr.write(
      `[clearcote] CDP endpoint ready: ${srv.cdpUrl}\n` +
      `            attach any client: connectOverCDP(${JSON.stringify(srv.cdpUrl)}) / puppeteer.connect({ browserURL })\n`);
  }
  await egressDrift;
  return srv;
}

import { runAgentTask } from "./agent.js";

// The SDK PACKAGE version (reported to the lease backend as sdk_version). Read from the packaged
// package.json (present in every npm install, one level above dist/). Falls back to the engine pin
// only if that read ever fails. Kept separate from the engine build (engine_version telemetry).
const SDK_VERSION: string = (() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return String(RELEASE.version);
  }
})();
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
  Cloud,
  verifyWebhook,
  RELEASE,
};
