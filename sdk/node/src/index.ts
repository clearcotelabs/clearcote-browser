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
import { cpSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { spawn, type ChildProcess } from "node:child_process";
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
import { ensureBinary, ensureVersion, proEnsureBinary, resolvedEngineVersion, warmFiles, type DownloadOptions } from "./download.js";
import { fingerprintArgs, splitFingerprintOptions, type FingerprintOptions } from "./fingerprint.js";
import { resolveGeo, type Geo } from "./geoip.js";
import { installHumanize, installHumanizeOnContext, type HumanizeOptions } from "./humanize.js";
import { agentArgs, splitAgentOptions, type AgentOptions } from "./agent.js";
import { resolveProfileOptions, Profile } from "./profile.js";
import { resolveAuto, resolveLocal, localSetupHint, engineSupportsProfiles, MIN_PROFILE_ENGINE_MAJOR, DEFAULT_LOCAL_DIR, type AutoOptions, type AutoResult } from "./profileauto.js";
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
  mergeFeatureFlags,
  privacySandboxArgs,
  quicArgs,
  webBluetoothArgs,
  webrtcDefaultDenyArgs,
  type PwProxy,
} from "./launchopts.js";
import { RELEASE, platformRelease } from "./release.js";
import { fetchWidevine, seedWidevine, widevineArgs } from "./widevine.js";
import { emitCoherenceWarnings } from "./warnings.js";
import { fontLaunchEnv } from "./fonts.js";
import { withShaderDialect, type ShaderDialect } from "./shaderdialect.js";
import {
  applyHeadlessGeometry, fitWindowToPersona, installWindowFixup, moveWindowToOrigin,
  type AppliedGeometry,
} from "./geometry.js";
import { acquireLease, resolveLicenseKey, withRunToken, type LicenseOptions, type LeaseSession } from "./license.js";

export type { FingerprintOptions } from "./fingerprint.js";
export type { DownloadOptions } from "./download.js";
export { proEnsureBinary, type ProDownloadOptions } from "./download.js";
export { resolveGeo, type Geo } from "./geoip.js";
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

/** Opt-in shader-dialect reporting (see ./shaderdialect.ts). */
interface ShaderDialectOption {
  /** Report ANGLE's translated shader in this dialect for
   * `WEBGL_debug_shaders.getTranslatedShaderSource()`.
   *
   * `"hlsl"` makes a Windows persona on a Linux host report HLSL, matching the Direct3D renderer
   * string it already advertises — without it the Vulkan backend answers with SPIR-V and the two
   * values contradict each other. Rendering is unaffected.
   *
   * OFF by default: the re-translation is a different code path from the one that rendered, so a
   * shader the real backend accepts but the HLSL translator rejects falls back to the honest
   * dialect. Turn it on if you hit this specific check. Needs a PRO engine 151 r15+. */
  shaderDialect?: ShaderDialect;
}

/** Options for {@link launch}: Playwright launch options + Clearcote fingerprint + agent + download options. */
export interface LaunchOptions extends PlaywrightLaunchOptions, FingerprintOptions, AgentOptions, GeoipOption, ProfileOption, ExtensionsOption, EphemeralProfileOption, HumanizeOptions, DownloadOptions, LicenseOptions, ShaderDialectOption {}

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
    ShaderDialectOption {
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
 * versions need `licenseKey` / `CLEARCOTE_LICENSE_KEY`). Pin a PRO rebuild with "150.0.7871.114-r7"
 * (or bare "r7"). */
export async function download(
  options: DownloadOptions & { version?: string; licenseKey?: string; licenseApiBase?: string } = {},
): Promise<string> {
  const { version, licenseKey, licenseApiBase, ...dl } = options;
  return executablePath({ version, pro: proSelector(licenseKey, licenseApiBase), ...dl });
}

/** Headless: the geometry defaults are CONTEXT options and `chromium.launch()` takes none, so they
 * ride on newPage/newContext instead — persona regime gets `viewport: null` plus a window fit, no-
 * persona regime gets the screen + viewport override. See ./geometry.ts for why each is needed. */
function installHeadlessGeometry(
  browser: Browser,
  geom: AppliedGeometry,
  args?: readonly string[] | null,
): void {
  // chromium.launch() accepts no context options, so the headless geometry default has to ride on
  // newPage/newContext — the same shape as installHeadedViewport. In persona mode each new context
  // is a new window, so each also gets the window fit. Any per-call geometry option wins.
  const persona = geom.mode === "persona";
  const defaults: Record<string, unknown> = persona
    ? { viewport: null }
    : { screen: geom.screen, viewport: geom.viewport };
  const merge = (o: Record<string, unknown> = {}) =>
    "viewport" in o || "screen" in o ? o : { ...o, ...defaults };
  const origNewPage = browser.newPage.bind(browser);
  const origNewContext = browser.newContext.bind(browser);
  (browser as unknown as { newPage: (o?: Record<string, unknown>) => Promise<Page> }).newPage =
    async (o = {}) => {
      const page = await origNewPage(merge(o) as Parameters<typeof origNewPage>[0]);
      if (persona) await fitWindowToPersona(page, args);
      else await moveWindowToOrigin(page, args);
      return page;
    };
  (browser as unknown as { newContext: (o?: Record<string, unknown>) => Promise<BrowserContext> }).newContext =
    async (o = {}) => {
      const context = await origNewContext(merge(o) as Parameters<typeof origNewContext>[0]);
      await installWindowFixup(context, args, persona);
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
  proxyForQuic?: PwProxy
): string[] {
  // webBluetoothArgs: Linux hosts hide navigator.bluetooth while exposing usb/serial/hid, an
  // OS-origin tell on a Windows persona. No-op off Linux.
  const base = [...fpArgs, ...agArgs, ...extArgs, ...proxyArgs, ...quicArgs(proxyForQuic), ...webBluetoothArgs()];
  // DEFAULT FLIPPED IN 0.23.0 — opt IN to disabling, rather than opt out.
  //
  // Disabling Topics/FLEDGE/Shared Storage/Fenced Frames is coherent for a de-Googled persona, and
  // incoherent for the default one: `brand: "chrome"` claims Google Chrome, which ships all of
  // them. Measured on the live audit against 150-r10, "a build claiming Chrome carries the Privacy
  // Sandbox surface Chrome ships" failed as an implausible value — the same defect class as the
  // WebUSB split fixed in r7. Pass disablePrivacySandbox: true when the persona really is
  // de-Googled Chromium.
  if (disablePrivacySandbox === true) base.push(...privacySandboxArgs());
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
  fingerprint.fingerprintProfile = profile;
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
 * Delete a throwaway profile directory once its context closes.
 *
 * THE RETRY IS NOT DEFENSIVE PADDING — a single attempt measurably does not work. On Windows the
 * browser still holds handles under the profile directory for a short window after `close()`
 * resolves, so the first removal throws EBUSY/EPERM. Measured on the first build of this change
 * (Python port): the directory survived a close plus a 1.5s wait and leaked silently.
 *
 * Two triggers, because neither alone is enough: `close` covers an orderly shutdown, and the
 * process-exit hook covers a script that throws or is interrupted with the browser still open.
 * Both funnel through one idempotent remove.
 */
function installEphemeralProfileCleanup(context: BrowserContext, userDataDir: string): void {
  let done = false;
  const remove = async () => {
    if (done) return;
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        rmSync(userDataDir, { recursive: true, force: true });
        done = true;
        return;
      } catch {
        await new Promise((r) => setTimeout(r, 250 * (attempt + 1))); // 0.25→1.5s, ~5s total
      }
    }
  };
  context.on("close", () => { void remove(); });
  // Synchronous: an exit handler cannot await, and an unresolved promise at exit removes nothing.
  process.once("exit", () => {
    if (done) return;
    try { rmSync(userDataDir, { recursive: true, force: true }); } catch { /* temp sweeper gets it */ }
  });
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
export async function launch(options: LaunchOptions = {}): Promise<Browser> {
  // ephemeralProfile: false restores the pre-0.23 incognito launch. Kept because the persistent
  // path costs a directory create+delete per launch, which a caller spawning hundreds of
  // short-lived browsers may reasonably not want to pay for a CDM they never touch.
  const { ephemeralProfile, userDataDir, ...restOpts } = options as LaunchOptions & {
    ephemeralProfile?: boolean;
    userDataDir?: string;
  };
  if (userDataDir !== undefined) {
    return asBrowserLike(await launchPersistentContext(userDataDir, restOpts as PersistentContextOptions));
  }
  if (ephemeralProfile !== false) {
    const dir = mkdtempSync(join(tmpdir(), "clearcote-run-"));
    const context = await launchPersistentContext(dir, restOpts as PersistentContextOptions);
    installEphemeralProfileCleanup(context, dir);
    return asBrowserLike(context);
  }
  return launchIncognito(restOpts as LaunchOptions);
}

/** The pre-0.23 incognito launch, reached via `ephemeralProfile: false`. */
async function launchIncognito(options: LaunchOptions = {}): Promise<Browser> {
  // profile="auto" is NOT a saved option-set — it resolves a real captured fingerprint later,
  // once the executable (and therefore the engine's Chromium major) is known.
  const isAutoProfile = options.profile === "auto";
  // profile= a saved persona: its options are the base, explicit options override.
  const merged =
    options.profile && !isAutoProfile
      ? { ...resolveProfileOptions(options.profile as string | Profile), ...options }
      : options;
  const { profile: _profile, profileSelect: _profileSelect, extensions, portableProfile, shaderDialect, encryptionKey, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, version, licenseKey, licenseApiBase, ...rest } = merged;
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
  // profile:"auto" -> resolve a REAL captured fingerprint for this host and apply it as
  // fingerprintProfile. Deliberately does NOT set a seed: with no --fingerprint the farbling
  // machinery stays off, which is the whole reason this path survives strict scoring.
  // Resolved here, after `exe` is known, because both the engine's Chromium major and the host
  // GPU measurement depend on the binary that will actually run.
  if (isAutoProfile) {
    await applyAutoProfile(fingerprint, exe, {
      quiet,
      licenseKey: resolveLicenseKey(licenseKey),
      // The profile service lives on the same backend as licensing, so a caller who overrode
      // one has overridden both; profilesource still prefers CLEARCOTE_PROFILE_API when set.
      apiBase: licenseApiBase,
      ...(options.profileSelect ?? {}),
    });
  }
  const headed = (pwOptions as PlaywrightLaunchOptions).headless === false;
  // License (opt-in): check out a concurrency slot and inject CLEARCOTE_RUN_TOKEN so the PRO
  // engine gate lets the browser launch. Inert (null) in free mode / when no key is set.
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  // On Linux, point FONTCONFIG_FILE at the bundled metric-compatible clones (Segoe UI, Arial, …).
  const launchEnv = withShaderDialect(shaderDialect, fontLaunchEnv(exe, (pwOptions as PlaywrightLaunchOptions).env));
  const runtimeEnv = lease ? withRunToken(lease.token, launchEnv) : launchEnv;
  const engineArgs = assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)], proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, args ?? [], proxyOpt as PwProxy | undefined);
  // Headless: screen.* has to be handled alongside the viewport or the window reports a geometry no
  // real browser can (see ./geometry.ts). Probe a copy — viewport/screen are context options, which
  // chromium.launch() does not take — and carry the result to newPage/newContext.
  const geom = headed
    ? null
    : applyHeadlessGeometry({ ...(pwOptions as Record<string, unknown>) }, fingerprint.fingerprint, engineArgs);
  const browser = await winAvRetry((exePath) => chromium.launch({
    // Drop Playwright's default --enable-automation so the engine's AutomationControlled feature
    // stays off (it flips webdriver-adjacent tells). Caller can override via ignoreDefaultArgs.
    ignoreDefaultArgs: ["--enable-automation"],
    ...(pwOptions as PlaywrightLaunchOptions),
    executablePath: exePath,
    ...(runtimeEnv ? { env: runtimeEnv } : {}),
    args: engineArgs,
  }), exe);
  // Release the concurrency slot when the browser closes.
  if (lease) browser.on("disconnected", () => { void lease.stop(); });
  if (headed) installHeadedViewport(browser); // launch() takes no viewport option -> wrap newPage/newContext
  else if (geom) installHeadlessGeometry(browser, geom, engineArgs);
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
  const { profile: _profile, extensions, portableProfile, shaderDialect, encryptionKey, disablePrivacySandbox, executablePath: exeOption, args, geoip, humanize, showCursor, autoUpdate, cacheDir, quiet, widevine, version, licenseKey, licenseApiBase, ...rest } = merged;
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
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  const ctxEnv = withShaderDialect(shaderDialect, fontLaunchEnv(exe, (opts as PlaywrightLaunchOptions).env));
  const runtimeEnv = lease ? withRunToken(lease.token, ctxEnv) : ctxEnv;
  const engineArgs = assembleArgs(fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)], proxyArgs, disablePrivacySandbox, fingerprint.webrtcIp, userArgs, proxyOpt as PwProxy | undefined);
  // headless: the persona owns screen when it is running, so only the window needs fitting; with no
  // persona the SDK overrides screen itself (see ./geometry.ts). Headed already set viewport: null.
  const geom = opts.headless === false
    ? null
    : applyHeadlessGeometry(opts as unknown as Record<string, unknown>, fingerprint.fingerprint, engineArgs);
  const context = await winAvRetry((exePath) => chromium.launchPersistentContext(userDataDir, {
    ...opts,
    ignoreDefaultArgs,  // keep AutomationControlled off (+ component updater on when widevine)
    executablePath: exePath,
    ...(runtimeEnv ? { env: runtimeEnv } : {}),
    args: engineArgs,
  }), exe);
  if (lease) context.on("close", () => { void lease.stop(); });
  if (geom) await installWindowFixup(context, engineArgs, geom.mode === "persona");
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
    profile: _profile, extensions, portableProfile, shaderDialect, encryptionKey, disablePrivacySandbox, executablePath: exeOption,
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
    fingerprintArgs(fingerprint), agentArgs(agent), [...extensionArgs(extensions), ...portableArgs(portableProfile, encryptionKey)],
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
  const lease = await acquireLease({
    licenseKey, licenseApiBase, quiet, sdkVersion: SDK_VERSION,
    // resolved lazily on cold checkout only (never per launch); telemetry, never gates the lease
    engineVersion: () => resolvedEngineVersion(version, !!resolveLicenseKey(licenseKey)),
  });
  const env = { ...process.env, ...(withShaderDialect(shaderDialect, fontLaunchEnv(exe, undefined)) ?? {}), ...(lease ? { CLEARCOTE_RUN_TOKEN: lease.token } : {}) };
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
  RELEASE,
};
