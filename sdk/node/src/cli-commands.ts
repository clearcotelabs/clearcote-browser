// clearcote — manage the browser binary, diagnose a setup, save a licence key, run a CDP endpoint.
//
//   clearcote install [--version 152] [--channel preview]   download + verify the binary
//   clearcote info    [--quick] [--json] [--proxy URL]      diagnostics (alias: doctor)
//   clearcote update  [--channel preview]                   fetch a newer build if one exists
//   clearcote clear-cache                                   delete every cached binary
//   clearcote login   [key] | --device                      save a licence key (validated first), or sign in
//                                                           from a browser and save the key it issues
//   clearcote logout                                        remove the saved key
//   clearcote serve   [--port 9222] [--idle-timeout 300] …  multi-identity CDP endpoint
//   clearcote cloud   run|sessions|stop|events|recording|profile sync|webhooks …  the hosted API
//
// `info` never downloads: it reports what is already cached and what a launch would resolve to.

import { parseArgs } from "node:util";
import { existsSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import {
  download,
  launch,
  getSessionSeats,
  saveLicenseKey,
  removeLicenseKey,
  licenseKeySource,
  licenseKeyPath,
  resolveLicenseKey,
  resolveGeoDetailed,
  resolveReleaseChannel,
  engineSupportsSwitch,
  serveMultiplex,
  RELEASE,
  type SessionSeats,
} from "./index.js";
import { autoUpdateRequested, defaultCacheRoot, isProRevisionSelector, listCachedBuilds, resolveVersion } from "./download.js";
import { CATALOG_FALLBACK } from "./release.js";
import { DeviceLoginError, pollForKey, requestDeviceCode } from "./devicelogin.js";
import { licenseExpiry, removeLicenseMeta, saveLicenseMeta, type LicenseExpiry } from "./license.js";
import { geoCacheRoot } from "./geoip.js";
import { GATED_ENGINE_SWITCHES, QUIET_GATED_ENGINE_SWITCHES } from "./launchopts.js";
import { fontLines, linuxFontReport, type FontReport } from "./fonts.js";
import { toProxySpec } from "./net.js";
import { clearRecovered, recoverRoot } from "./winlaunch.js";
import { Cloud, CloudError, announceHandoff, type Json, type RunOptions } from "./cloud.js";

const SDK_VERSION: string = (() => {
  try {
    return JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version as string;
  } catch {
    return "unknown";
  }
})();

export const USAGE = `clearcote ${SDK_VERSION} — manage and diagnose the Clearcote browser.

USAGE
  clearcote install [--version <v>] [--channel stable|preview]
  clearcote info [--quick] [--json] [--proxy <url>]      (alias: doctor)
  clearcote update [--channel stable|preview]
  clearcote clear-cache
  clearcote login [key]
  clearcote login --device                               (sign in from a browser; no key to paste)
  clearcote logout
  clearcote serve [--port 9222] [--host 127.0.0.1] [--idle-timeout <s>] [--data-dir <dir>]
                  [--max-browsers 16] [--allow-origin <origin>]... [--allow-host <name>]... [--headed]
                  [--fingerprint <seed>] [--platform <os>] [--proxy <url>] [--timezone <tz>]
                  [--accept-language <l>] [--geoip]
  clearcote cloud run <task> [--url <url>] [--schema <file.json>] [--secret <name>=<value>]... [--json]
  clearcote cloud sessions | stop <id> | events <id> | recording <id> [-o <file.mp4>]
  clearcote cloud profile sync <name> (--from-profile <dir> | --from-cdp <url> | --from-file <file> | --login <url>)
  clearcote cloud webhooks add <url> | list | rm <id> | test <id>      (all flags: clearcote cloud --help)

INFO FLAGS
  --quick          skip everything that needs the network or a launch (seat count, launch test)
  --json           machine-readable output
  --proxy <url>    resolve the exit IP, timezone and language a launch through this proxy would use

ENVIRONMENT
  CLEARCOTE_LICENSE_KEY, CLEARCOTE_RELEASE_CHANNEL, CLEARCOTE_GEOIP_TIMEOUT_SECONDS,
  CLEARCOTE_LICENSE_THROUGH_PROXY, CLEARCOTE_BINARY, CLEARCOTE_CACHE, CLEARCOTE_SERVE_IDLE_TIMEOUT,
  CLEARCOTE_API_KEY, CLEARCOTE_API_URL, CLEARCOTE_CLOUD,
  CLEARCOTE_FONT_DIRS (Linux: your own fonts, e.g. a copy of Windows' Fonts folder, used instead of the lookalikes),
  CLEARCOTE_FALLBACK_FONT_DIRS (Linux: fonts only for characters nothing else covers)`;

/** `clearcote cloud --help`. Kept byte-identical to CLOUD_USAGE in the Python SDK's _commands.py. */
export const CLOUD_USAGE = `clearcote cloud -- the hosted Clearcote API from the command line.

USAGE
  clearcote cloud run <task> [--url <url>] [--schema <file.json>] [--secret <name>=<value>]...
                      [--secret-domain <name>=<host>]... [--handoff] [--record] [--json]
                      [--country <cc>] [--state <s>] [--city <c>] [--proxy managed|<url>]
                      [--profile <name> [--persist-profile]] [--fingerprint <seed>]
                      [--timeout-sec <n>] [--max-steps <n>] [--note <text>]
  clearcote cloud sessions [--json]
  clearcote cloud stop <id> [--json]
  clearcote cloud events <id> [--json]
  clearcote cloud recording <id> [-o <file.mp4>] [--json]
  clearcote cloud profile sync <name> (--from-profile <dir> | --from-cdp <url> | --from-file <state.json>
                      | --login <url>) (--domain <domain>... | --all-domains) [--replace] [--json]
  clearcote cloud webhooks add <url> [--event <type>]... [--json]
  clearcote cloud webhooks list [--json]
  clearcote cloud webhooks rm <id> [--json]
  clearcote cloud webhooks test <id> [--json]

  run waits for the result and exits 0 only when the run succeeded. profile sync uploads only the
  cookies of the --domain names you list (subdomains included); --all-domains uploads every cookie.

ENVIRONMENT
  CLEARCOTE_API_KEY (required), CLEARCOTE_API_URL (default https://www.clearcotelabs.com)`;

function out(line = ""): void {
  process.stdout.write(line + "\n");
}

function fail(msg: string, code = 1): never {
  process.stderr.write(`clearcote: ${msg}\n`);
  process.exit(code);
}

/** What `info` reports. Also the `--json` shape. */
export interface InfoReport {
  sdk: { version: string; node: string; platform: string };
  license: { source: string; key?: string; expiry?: LicenseExpiry; seats?: SessionSeats };
  binary: {
    source: "CLEARCOTE_BINARY" | "cache" | "none";
    path?: string;
    tag?: string;
    cached: Array<{ tag: string; path: string }>;
    pinnedFree: string;
    releaseChannel: string;
    /** Why `path` is the build launch() would run for the current licence state (or why there is none). */
    selectedBy?: string;
  };
  engineFeatures?: Record<string, boolean>;
  launch?: { tested: boolean; ok?: boolean; version?: string; build?: string; error?: string; missingLibs?: string[]; reason?: string };
  fonts?: FontReport;
  geoip: { databaseCached: boolean; path: string; proxy?: { exitIp?: string; country?: string; timezone?: string; acceptLanguage?: string; error?: string } };
}

function geoDbPath(): string {
  return join(geoCacheRoot(), "geoip-aio-all.mmdb");
}

function missingSharedLibs(exe: string): string[] {
  if (process.platform !== "linux") return [];
  const r = spawnSync("ldd", ["--", exe], { encoding: "utf8" });
  return (r.stdout || "").split("\n").filter((l) => l.includes("not found")).map((l) => l.trim().split(" ")[0]);
}

/** Whether a cached licensed build's tag (pro-<version>-r<N>) is what `version` selects: a revision ("r30",
 * "154.0.8037.57-r30"), an exact version (its newest revision), a major ("154"), or "latest". */
export function proTagMatches(tag: string, version: string): boolean {
  if (!tag.startsWith("pro-")) return false;
  const t = tag.slice(4).toLowerCase();
  const v = String(version).trim().toLowerCase();
  if (v === "latest" || v === "newest") return true;
  if (/^r\d+$/.test(v)) return t.endsWith(`-${v}`);
  if (/^\d+$/.test(v)) return t.startsWith(`${v}.`);
  return t === v || t.startsWith(`${v}-`);
}

/**
 * The build launch() would run for the current licence state, from what is already cached (`info` never
 * downloads). CLEARCOTE_BINARY wins. A version selector (CLEARCOTE_BROWSER_VERSION) is resolved the way
 * launch() resolves it -- through the version catalog (the bundled copy when `offline`), a licensed revision
 * straight to the licensed builds -- and picks the cached build it names. Otherwise, with a licence key,
 * launch() runs a licensed build: the newest one cached (launch() itself asks the licence server, which may
 * name a newer one). Without a key it runs the open build this SDK pins (the newest cached open build with
 * CLEARCOTE_AUTO_UPDATE). A cached licensed build is never picked for a keyless setup: it cannot run without
 * a key, which is what made the old "newest cached build" launch test fail for open-build users.
 */
export async function launchBuild(
  cached: Array<{ tag: string; path: string }>, envBinary?: string, licenseKey?: string, autoUpdate = false,
  version?: string, offline = false,
): Promise<{ pick?: { path: string; tag?: string }; selectedBy: string }> {
  if (envBinary) return { pick: { path: envBinary }, selectedBy: "CLEARCOTE_BINARY" };
  const sel = String(version ?? "").trim();
  if (sel) {
    const named = `CLEARCOTE_BROWSER_VERSION=${sel}`;
    let plan;
    try {
      plan = licenseKey && isProRevisionSelector(sel)
        ? { kind: "pro" as const, version: sel }
        : await resolveVersion(sel, !!licenseKey, true, offline ? CATALOG_FALLBACK : undefined);
    } catch (e) {
      return { selectedBy: `${named}: ${(e as Error).message.split("\n")[0]}` };
    }
    if (plan.kind === "pro") {
      const m = cached.find((c) => proTagMatches(c.tag, plan.version));
      return m ? { pick: m, selectedBy: `licensed: the newest cached build ${named} selects` }
        : { selectedBy: `no licensed build for ${named} is cached — run: clearcote install --version ${sel}` };
    }
    const hit = cached.find((c) => c.tag === plan.rel.tag);
    return hit ? { pick: hit, selectedBy: `open build ${plan.rel.tag} (${named})` }
      : { selectedBy: `the open build ${plan.rel.tag} (${named}) is not installed — run: clearcote install --version ${sel}` };
  }
  if (licenseKey) {
    const licensed = cached.filter((c) => c.tag.startsWith("pro-"));
    return licensed.length
      ? { pick: licensed[0], selectedBy: "licensed: the newest cached licensed build (launch() asks the licence server, which may name a newer one)" }
      : { selectedBy: "no licensed build is cached — run: clearcote install" };
  }
  if (autoUpdate) {
    const open = cached.filter((c) => !c.tag.startsWith("pro-"));
    if (open.length) return { pick: open[0], selectedBy: "open build: the newest cached (CLEARCOTE_AUTO_UPDATE)" };
  }
  const pinned = cached.find((c) => c.tag === RELEASE.tag);
  return pinned
    ? { pick: pinned, selectedBy: `open build pinned by this SDK (${RELEASE.tag})` }
    : { selectedBy: `the open build this SDK pins (${RELEASE.tag}) is not installed — run: clearcote install` };
}

export async function buildInfo(
  flags: { quick?: boolean; proxy?: string },
  launchFn: (o: Record<string, unknown>) => Promise<{ version(): string; close(): Promise<void> }> = launch as never,
): Promise<InfoReport> {
  const src = licenseKeySource();
  let channel: string;
  try {
    channel = resolveReleaseChannel();
  } catch (e) {
    channel = `invalid (${(e as Error).message})`;
  }
  const cached = listCachedBuilds();
  const envBinary = process.env.CLEARCOTE_BINARY;
  const { pick, selectedBy } = await launchBuild(cached, envBinary, resolveLicenseKey(), autoUpdateRequested(undefined),
    process.env.CLEARCOTE_BROWSER_VERSION, !!flags.quick);
  const report: InfoReport = {
    sdk: { version: SDK_VERSION, node: process.version, platform: `${process.platform}-${process.arch}` },
    license: { source: src.source, ...(src.masked ? { key: src.masked } : {}) },
    binary: {
      source: envBinary ? "CLEARCOTE_BINARY" : pick ? "cache" : "none",
      ...(pick ? { path: pick.path } : {}),
      ...(!envBinary && pick?.tag ? { tag: pick.tag } : {}),
      cached,
      pinnedFree: `${RELEASE.version} (${RELEASE.tag})`,
      releaseChannel: channel,
      selectedBy,
    },
    geoip: { databaseCached: existsSync(geoDbPath()), path: geoDbPath() },
  };

  const expiry = licenseExpiry(); // a local record only: no network, so --quick reports it too
  if (expiry) report.license.expiry = expiry;

  if (pick && existsSync(pick.path)) {
    report.engineFeatures = Object.fromEntries(
      ["proxy-auth", "socks5-credentials", "socks5-udp",
        ...[...Object.keys(GATED_ENGINE_SWITCHES), ...QUIET_GATED_ENGINE_SWITCHES].map((s) => s.slice(2))]
        .map((name) => [name, engineSupportsSwitch(pick.path, name)]),
    );
  }

  if (process.platform === "linux" && pick) {
    // The fonts a launch would see (bundle + CLEARCOTE_FONT_DIRS + CLEARCOTE_FALLBACK_FONT_DIRS): which
    // scripts they draw, and which Windows families are genuine rather than lookalikes.
    report.fonts = linuxFontReport(pick.path)
      ?? { bundled: false, note: "this build ships no font bundle; a Windows persona on this host may render with Linux fonts" };
  }

  if (!flags.quick && src.source !== "none") {
    report.license.seats = await getSessionSeats();
  }

  if (flags.quick) {
    report.launch = { tested: false, reason: "skipped (--quick)" };
  } else if (!pick) {
    report.launch = { tested: false, reason: cached.length ? selectedBy : "no binary installed — run: clearcote install" };
  } else {
    try {
      // cloud: false — this tests the LOCAL install, whatever CLEARCOTE_CLOUD says.
      const b = await launchFn({ executablePath: pick.path, headless: true, quiet: true, ephemeralProfile: false, cloud: false });
      const version = b.version();
      await b.close();
      report.launch = { tested: true, ok: true, version, build: pick.tag ?? pick.path };
    } catch (e) {
      const libs = missingSharedLibs(pick.path);
      report.launch = { tested: true, ok: false, error: (e as Error).message.split("\n")[0], build: pick.tag ?? pick.path, ...(libs.length ? { missingLibs: libs } : {}) };
    }
  }

  if (flags.proxy) {
    const r = await resolveGeoDetailed(flags.proxy, { quiet: true });
    report.geoip.proxy = r.geo && r.geo.timezone
      ? { exitIp: r.geo.ip, country: r.geo.country, timezone: r.geo.timezone, acceptLanguage: r.geo.acceptLanguage }
      : { error: r.reason };
  }
  return report;
}

function printInfo(r: InfoReport): void {
  const ok = (b: boolean | undefined) => (b ? "yes" : "no");
  out(`clearcote SDK   ${r.sdk.version}  (node ${r.sdk.node}, ${r.sdk.platform})`);
  out(`Licence         ${r.license.source === "none" ? "none (free build)" : `${r.license.key} from ${r.license.source}`}`);
  const exp = r.license.expiry;
  if (exp && exp.source === "device-login") out(`Expires         ${exp.expiresAt || "never"}  (as reported at device login)`);
  const seats = r.license.seats;
  if (seats) {
    if (seats.state === "ok") out(`Seats           ${seats.used} of ${seats.limit ?? "unlimited"} in use${seats.plan ? `  (plan: ${seats.plan})` : ""}`);
    else out(`Seats           unavailable: ${seats.reason ?? seats.state}`);
  }
  if (r.binary.path) {
    out(`Binary          ${r.binary.path}${r.binary.tag ? `  [${r.binary.tag}]` : ""} (${r.binary.source})`);
    out(`                launch() runs this one: ${r.binary.selectedBy}`);
  } else if (r.binary.cached.length) {
    out(`Binary          none for this setup: ${r.binary.selectedBy}`);
  } else {
    out("Binary          not installed — run: clearcote install");
  }
  out(`Release channel ${r.binary.releaseChannel}`);
  out(`Free pin        ${r.binary.pinnedFree}`);
  if (r.binary.cached.length > 1) out(`Also cached     ${r.binary.cached.slice(1).map((c) => c.tag).join(", ")}`);
  if (r.engineFeatures) {
    out(`Engine support  ${Object.entries(r.engineFeatures).map(([k, v]) => `${k}=${ok(v)}`).join("  ")}`);
  }
  if (r.launch) {
    if (!r.launch.tested) out(`Launch test     ${r.launch.reason}`);
    else if (r.launch.ok) out(`Launch test     ok (${r.launch.version}) with ${r.launch.build}`);
    else {
      out(`Launch test     FAILED with ${r.launch.build}: ${r.launch.error}`);
      for (const lib of r.launch.missingLibs ?? []) out(`                missing library: ${lib}`);
    }
  }
  if (r.fonts) for (const line of fontLines(r.fonts)) out(line);
  out(`GeoIP database  ${r.geoip.databaseCached ? "cached" : "not cached (downloaded on first geoip launch)"}`);
  if (r.geoip.proxy) {
    const p = r.geoip.proxy;
    out(p.error ? `Proxy geo       FAILED: ${p.error}` : `Proxy geo       exit ${p.exitIp} (${p.country})  timezone ${p.timezone}  language ${p.acceptLanguage}`);
  }
}

function err(line = ""): void {
  process.stderr.write(line + "\n");
}

/**
 * `clearcote login --device`: show a link and a short code, wait for the approval in the browser, then
 * save the key it issues exactly as `clearcote login <key>` does. The key is never printed.
 * Instructions go to stderr and the result to stdout, like the paste login's prompt. Output and exit
 * codes match the Python CLI (test/device-login.test.ts).
 *
 * Ctrl-C is deterministic: it never tears down a request already on its way (its answer may be the key,
 * which the server hands out once). Ctrl-C before the key arrives saves nothing (exit 130); one that lands
 * while the request that brings the key is in flight still saves it, and says so.
 */
export async function deviceLogin(opts: { sleep?: (s: number, signal?: AbortSignal) => Promise<void>; clock?: () => number } = {}): Promise<void> {
  const ctrl = new AbortController(); // Ctrl-C: stop between requests; one already sent is answered first
  const hard = new AbortController(); // a second Ctrl-C: give up on the request in flight too
  const onSigint = () => {
    if (ctrl.signal.aborted) {
      hard.abort();
      return;
    }
    ctrl.abort();
    err("");
    err("stopping (a request already sent is answered first; press Ctrl-C again to stop at once)");
  };
  process.on("SIGINT", onSigint);
  let grant;
  try {
    const code = await requestDeviceCode();
    if (ctrl.signal.aborted) throw new DeviceLoginError("cancelled", "cancelled");
    err("To sign in, open this link in a browser and approve the code shown there:");
    err("");
    err(`  ${code.verification_uri_complete}`);
    err("");
    err(`Code: ${code.user_code}  (or enter it at ${code.verification_uri})`);
    err("Waiting for the approval (Ctrl-C to cancel)...");
    let warned = false;
    grant = await pollForKey(code, {
      ...opts,
      signal: ctrl.signal,
      abort: hard.signal,
      onRetry: (reason) => {
        if (!warned) { // once: a flaky network must not flood the terminal
          warned = true;
          err(`note: ${reason}; still waiting`);
        }
      },
    });
  } catch (e) {
    if (e instanceof DeviceLoginError) {
      if (e.code === "cancelled") fail("cancelled. Nothing was saved.", 130);
      if (e.code === "aborted") fail("stopped at once (second Ctrl-C). Nothing was saved.", 130);
      const again = e.code === "expired_token" || e.code === "outcome_unknown" ? " Run `clearcote login --device` again." : "";
      fail(`${e.message}. Nothing was saved.${again}`);
    }
    throw e;
  } finally {
    process.off("SIGINT", onSigint);
  }
  const where = saveLicenseKey(grant.licenseKey);
  saveLicenseMeta(grant.licenseKey, grant.plan, grant.expiresAt);
  if (grant.accountEmail) out(`Signed in as ${grant.accountEmail}`);
  out(`saved to ${where}`);
  out(`plan ${grant.plan || "unknown"}, ${grant.expiresAt ? `expires ${grant.expiresAt}` : "no expiry"}`);
  if (ctrl.signal.aborted) out("note: Ctrl-C came after the licence server had sent the key, so it was saved anyway");
  if (process.env.CLEARCOTE_LICENSE_KEY) out("note: CLEARCOTE_LICENSE_KEY is set in this environment and takes precedence over the saved key");
}

/** The parts of a TTY input stream readHidden uses. */
export interface HiddenInput {
  isTTY?: boolean;
  setRawMode?(mode: boolean): unknown;
  setEncoding(enc: BufferEncoding): unknown;
  on(ev: "data", fn: (chunk: string) => void): unknown;
  removeListener(ev: "data", fn: (chunk: string) => void): unknown;
  resume(): unknown;
  pause(): unknown;
}

/**
 * Read one line from a terminal without echoing it: raw mode, so the terminal shows nothing of what is typed
 * or pasted (a screen share, a recording or the scrollback must not show a key). Enter ends it, Backspace
 * edits, Ctrl-C rejects with "cancelled". Terminal escape sequences (bracketed paste) are dropped.
 */
export function readHidden(input: HiddenInput, output: { write(s: string): unknown }, prompt: string): Promise<string> {
  return new Promise((resolve, reject) => {
    output.write(prompt);
    let value = "";
    const finish = (err?: Error) => {
      input.removeListener("data", onData);
      input.setRawMode?.(false);
      input.pause();
      output.write("\n");
      if (err) reject(err);
      else resolve(value.replace(/\x1b\[[0-9;]*[~A-Za-z]/g, "").trim());
    };
    const onData = (chunk: string) => {
      for (const ch of chunk) {
        if (ch === "\r" || ch === "\n" || ch === "\u0004") return finish();
        if (ch === "\u0003") return finish(new Error("cancelled"));
        if (ch === "\u007f" || ch === "\b") value = value.slice(0, -1);
        else value += ch;
      }
    };
    input.setRawMode?.(true);
    input.setEncoding("utf8");
    input.on("data", onData);
    input.resume();
  });
}

async function promptKey(): Promise<string> {
  if (!process.stdin.isTTY) {
    fail("no key given. Run `clearcote login <key>`, or copy a key from https://www.clearcotelabs.com/dashboard/licenses");
  }
  try {
    return await readHidden(process.stdin, process.stderr, "Paste your licence key (https://www.clearcotelabs.com/dashboard/licenses; it is not shown): ");
  } catch {
    return fail("cancelled. Nothing was saved.", 130);
  }
}

/** What `clearcote <command> --help` adds to that command's USAGE lines. */
const COMMAND_NOTES: Record<string, string> = {
  login: "Saves a licence key to ~/.clearcote/license.key, where every launch finds it.\n"
    + "  <key>      the key, checked with the licence server first\n"
    + "  (no key)   asks for it on the terminal; what you paste is not shown\n"
    + "  --device   sign in from a browser instead: shows a link and a code, and saves the key the\n"
    + "             site issues once you approve it there. Nothing to copy or paste.",
  logout: "Removes the saved key (and what device login recorded about it).",
  info: "Reports the SDK, the licence, the cached builds and what a launch would use. Never downloads.",
  install: "Downloads and verifies the build a launch would use.",
  update: "Fetches a newer build if one exists.",
  "clear-cache": "Deletes every cached browser build (nothing else in the cache directory), and on Windows the " +
                 "recovered copies in ~/.clearcote/recovered.",
  serve: "A CDP endpoint that gives every connection its own browser and identity.",
};

/** `clearcote <cmd> --help`: that command's lines from USAGE (with their flag sections) and a note. */
export function commandUsage(cmd: string): string {
  const name = cmd === "doctor" ? "info" : cmd;
  const lines = USAGE.split("\n");
  const picked: string[] = [];
  let keep = false;
  for (const line of lines.slice(lines.indexOf("USAGE") + 1)) {
    if (!line.trim()) break;
    if (line.startsWith("  clearcote ")) keep = line.trim().split(/\s+/)[1] === name;
    if (keep) picked.push(line);
  }
  let text = `USAGE\n${picked.join("\n")}`;
  if (name === "info") {
    const flags = lines.slice(lines.indexOf("INFO FLAGS"));
    const end = flags.indexOf("");
    text += `\n\n${flags.slice(0, end < 0 ? flags.length : end).join("\n")}`;
  }
  const note = COMMAND_NOTES[name];
  return note ? `clearcote ${name} -- ${note}\n\n${text}` : text;
}

function dirSize(p: string): number {
  let total = 0;
  try {
    for (const e of readdirSync(p, { withFileTypes: true })) {
      const full = join(p, e.name);
      total += e.isDirectory() ? dirSize(full) : statSync(full).size;
    }
  } catch { /* unreadable — skip */ }
  return total;
}

export async function main(argv = process.argv.slice(2)): Promise<void> {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === "-h" || cmd === "--help" || cmd === "help") {
    out(USAGE);
    return;
  }
  if (cmd === "version" || cmd === "--version") {
    out(SDK_VERSION);
    return;
  }

  if (cmd === "cloud") {
    const code = await cloudMain(rest);
    if (code) process.exitCode = code;
    return;
  }

  const known = ["info", "doctor", "install", "update", "clear-cache", "login", "logout", "serve"];
  if (known.includes(cmd) && rest.some((a) => a === "-h" || a === "--help")) {
    out(commandUsage(cmd));
    return;
  }

  if (cmd === "info" || cmd === "doctor") {
    const { values } = parseArgs({ args: rest, options: { quick: { type: "boolean" }, "no-launch": { type: "boolean" }, json: { type: "boolean" }, proxy: { type: "string" } } });
    const report = await buildInfo({ quick: !!(values.quick || values["no-launch"]), proxy: values.proxy });
    if (values.json) out(JSON.stringify(report, null, 2));
    else printInfo(report);
    return;
  }

  if (cmd === "install" || cmd === "update") {
    const { values } = parseArgs({ args: rest, options: { version: { type: "string" }, channel: { type: "string" } } });
    const releaseChannel = resolveReleaseChannel(values.channel);
    const licenseKey = resolveLicenseKey();
    const path = await download({
      version: values.version,
      releaseChannel,
      licenseKey,
      // update: re-resolve the newest build instead of reusing the SDK's pin (free) — PRO always asks the server
      ...(cmd === "update" ? { autoUpdate: true } : {}),
    });
    out(path);
    return;
  }

  if (cmd === "clear-cache") {
    const root = defaultCacheRoot();
    if (!existsSync(root)) {
      out(`nothing to clear (${root} does not exist)`);
    } else {
      // Only build directories: a CLEARCOTE_CACHE pointing at $HOME or a shared directory must not
      // become `rm -rf` of that directory. A build dir carries a .verified marker once complete; a
      // half-finished download has the build-tag name but no marker yet.
      let bytes = 0;
      const removed: string[] = [];
      for (const name of readdirSync(root)) {
        const dir = join(root, name);
        let isDir = false;
        try { isDir = statSync(dir).isDirectory(); } catch { /* vanished */ }
        if (!isDir) continue;
        if (!existsSync(join(dir, ".verified")) && !/^(pro-\d|v\d)/.test(name)) continue;
        bytes += dirSize(dir);
        rmSync(dir, { recursive: true, force: true });
        removed.push(name);
      }
      out(removed.length
        ? `removed ${removed.length} cached build${removed.length === 1 ? "" : "s"} from ${root} (${(bytes / 1e6).toFixed(0)} MB)`
        : `no cached builds in ${root}`);
    }
    // Windows: the copies launches fall back to when a cached build cannot start in place.
    const rec = clearRecovered();
    if (rec.removed) {
      out(`removed ${rec.removed} recovered build cop${rec.removed === 1 ? "y" : "ies"} from ${recoverRoot()} (${(rec.bytes / 1e6).toFixed(0)} MB)`);
    }
    if (rec.inUse) {
      out(`kept ${rec.inUse} recovered build cop${rec.inUse === 1 ? "y" : "ies"} a running browser is using (in ${recoverRoot()})`);
    }
    return;
  }

  if (cmd === "login") {
    if (rest.includes("--device")) {
      if (rest.some((a) => a !== "--device")) fail("pass a key or --device, not both", 2);
      await deviceLogin();
      return;
    }
    const key = rest[0] ?? (await promptKey());
    if (!key) fail("empty key");
    const seats = await getSessionSeats({ licenseKey: key });
    if (seats.state === "invalid") fail(`the licence server rejected this key (${seats.reason}). Nothing was saved.`);
    const where = saveLicenseKey(key);
    removeLicenseMeta(key.trim()); // a device login's record of another key no longer applies
    out(`saved to ${where}`);
    if (seats.state === "ok") out(`valid: ${seats.used} of ${seats.limit ?? "unlimited"} seats in use${seats.plan ? `, plan ${seats.plan}` : ""}`);
    else out(`note: could not confirm the key right now (${seats.reason}); it was saved anyway`);
    return;
  }

  if (cmd === "logout") {
    const removed = removeLicenseKey();
    out(removed ? `removed ${licenseKeyPath()}` : "no saved key");
    if (process.env.CLEARCOTE_LICENSE_KEY) out("note: CLEARCOTE_LICENSE_KEY is still set in this environment and will keep being used");
    return;
  }

  if (cmd === "serve") {
    const { values } = parseArgs({
      args: rest,
      options: {
        port: { type: "string" }, host: { type: "string" }, "idle-timeout": { type: "string" }, "data-dir": { type: "string" },
        "max-browsers": { type: "string" }, "allow-origin": { type: "string", multiple: true }, "allow-host": { type: "string", multiple: true }, headed: { type: "boolean" },
        fingerprint: { type: "string" }, platform: { type: "string" }, proxy: { type: "string" }, timezone: { type: "string" },
        "accept-language": { type: "string" }, geoip: { type: "boolean" }, quiet: { type: "boolean" },
      },
    });
    const num = (name: string, v: string | undefined): number | undefined => {
      if (v === undefined) return undefined;
      const n = Number(v);
      if (!Number.isFinite(n) || n < 0) fail(`--${name} must be a non-negative number`);
      return n;
    };
    const srv = await serveMultiplex({
      port: num("port", values.port) ?? 9222,
      host: values.host,
      idleTimeoutSec: num("idle-timeout", values["idle-timeout"]),
      dataDir: values["data-dir"],
      maxBrowsers: num("max-browsers", values["max-browsers"]),
      allowOrigins: values["allow-origin"],
      allowHosts: values["allow-host"],
      headless: !values.headed,
      quiet: values.quiet,
      ...(values.fingerprint ? { fingerprint: values.fingerprint } : {}),
      ...(values.platform ? { platform: values.platform as "windows" | "linux" | "macos" | "android" } : {}),
      // Split user:pass out of the URL: Chromium rejects a --proxy-server that carries credentials
      // and goes DIRECT, so passing the URL through whole leaked the host's real IP.
      ...(values.proxy ? { proxy: proxyOption(values.proxy) } : {}),
      ...(values.timezone ? { timezone: values.timezone } : {}),
      ...(values["accept-language"] ? { acceptLanguage: values["accept-language"] } : {}),
      ...(values.geoip ? { geoip: true } : {}),
    });
    const stop = () => { void srv.close().then(() => process.exit(0)); };
    process.once("SIGINT", stop);
    process.once("SIGTERM", stop);
    return; // keep running
  }

  fail(`unknown command '${cmd}'. Run \`clearcote --help\`.`, 2);
}

// ── clearcote cloud ────────────────────────────────────────────────────────────────────────────────
// Output is part of the contract: the Python SDK's tests/test_parity_cloud_cli.py runs this CLI and
// the Python one against the same fake API and compares stdout byte for byte. Change a line here,
// change it in _commands.py too.

/** A usage error inside `clearcote cloud`: message already on stderr, exit with `code`. */
class CloudCliExit extends Error {
  constructor(readonly code: number) {
    super(`exit ${code}`);
  }
}

function cloudFail(msg: string, code = 1): never {
  process.stderr.write(`clearcote: ${msg}\n`);
  throw new CloudCliExit(code);
}

const dump = (obj: unknown) => out(JSON.stringify(obj, null, 2));
const str = (v: unknown) => (v === null || v === undefined ? "-" : String(v));
const eur = (v: unknown, places: number) => (typeof v === "number" ? `€${v.toFixed(places)}` : "-");

/** The human summary `clearcote cloud run` prints. */
export function formatRun(run: Json): string[] {
  const lines = [`run ${str(run?.id)} ${str(run?.status)}`];
  const r = run?.result ?? {};
  if (r.status) lines.push(`result  ${r.status}${r.detail ? `: ${r.detail}` : ""}`);
  if (r.url) lines.push(`url     ${r.url}`);
  if (r.title) lines.push(`title   ${r.title}`);
  if (r.outputError) lines.push(`output  error: ${r.outputError}`);
  else if (r.output !== undefined && r.output !== null) lines.push(`output  ${JSON.stringify(r.output, null, 2)}`);
  const cost = run?.costEur;
  if (cost && typeof cost === "object" && cost.total !== undefined && cost.total !== null) lines.push(`cost    ${eur(cost.total, 4)}`);
  return lines;
}

function sessionLine(s: Json): string {
  const line = `${str(s?.id)}  ${str(s?.status)}  ${str(s?.createdAt)}  ${eur(s?.costEur, 4)}`;
  return line + (s?.note ? `  ${s.note}` : "");
}

// A --secret argument is never echoed back, not even a malformed one: it may be the bare secret.
const SECRET_SHAPE = "--secret wants <name>=<value> (the argument given is not shown: it may hold the secret)";

function parsePairs(values: string[] | undefined, flag: string): Array<[string, string]> {
  return (values ?? []).map((raw) => {
    const i = raw.indexOf("=");
    const name = i < 0 ? "" : raw.slice(0, i).trim();
    const value = i < 0 ? "" : raw.slice(i + 1);
    if (!name || !value) cloudFail(flag === "--secret" ? SECRET_SHAPE : `${flag} wants <name>=<value>, got '${raw}'`, 2);
    return [name, value];
  });
}

/** --secret name=value and --secret-domain name=host as the API's `secrets` object: a plain string,
 * or { value, domains } once a domain is given for that name. */
export function cloudSecrets(secretArgs?: string[], domainArgs?: string[]): Record<string, unknown> {
  const secrets = new Map(parsePairs(secretArgs, "--secret"));
  const domains = new Map<string, string[]>();
  for (const [name, host] of parsePairs(domainArgs, "--secret-domain")) {
    if (!secrets.has(name)) cloudFail(`--secret-domain ${name}: there is no --secret ${name}=...`, 2);
    domains.set(name, [...(domains.get(name) ?? []), host.trim().toLowerCase()]);
  }
  return Object.fromEntries([...secrets].map(([n, v]) => [n, domains.has(n) ? { value: v, domains: domains.get(n) } : v]));
}

// `clearcote cloud run` browser/run options that take a value, mapped to the Runs.create option of the
// same meaning (the Python launch kwargs): --timeout-sec -> timeoutSec, --max-steps -> maxSteps, ...
const RUN_OPTION_FLAGS = ["country", "state", "city", "proxy", "profile", "fingerprint", "timeout-sec", "max-steps", "note"];

function whole(value: string | undefined, flag: string): number | undefined {
  if (value === undefined) return undefined;
  if (!/^[0-9]+$/.test(value)) cloudFail(`${flag} wants a whole number, got '${value}'`, 2);
  return Number(value);
}

/** The browser and run options of `clearcote cloud run` (unset ones left out). --profile NAME loads a
 * cloud profile; with --persist-profile the run also saves it back. */
export function cloudRunOptions(v: Record<string, Json>): Record<string, unknown> {
  if (v["persist-profile"] && !v.profile) cloudFail("--persist-profile needs --profile <name>", 2);
  const opts: Record<string, unknown> = {
    country: v.country, state: v.state, city: v.city, proxy: v.proxy,
    profile: v.profile ? (v["persist-profile"] ? { name: v.profile, persist: true } : v.profile) : undefined,
    fingerprint: v.fingerprint, timeoutSec: whole(v["timeout-sec"], "--timeout-sec"),
    maxSteps: whole(v["max-steps"], "--max-steps"), note: v.note,
    handoff: v.handoff ? true : undefined, record: v.record ? true : undefined,
  };
  return Object.fromEntries(Object.entries(opts).filter(([, x]) => x !== undefined));
}

function runProgress(view: Json): void {
  if (view?.status === "waiting_for_human") announceHandoff(view);
  else process.stderr.write(`[clearcote] run ${view?.id}: ${view?.status}\n`);
}

type ParseOpts = Record<string, { type: "string" | "boolean"; multiple?: boolean; short?: string }>;

function parse(args: string[], options: ParseOpts, positionals: number, what: string): { values: Record<string, Json>; pos: string[] } {
  let r;
  try {
    r = parseArgs({ args, options: { ...options, json: { type: "boolean" } }, allowPositionals: true, strict: true });
  } catch (e) {
    cloudFail((e as Error).message, 2);
  }
  if (positionals >= 0 ? r.positionals.length !== positionals : r.positionals.length < 1) {
    cloudFail(`\`clearcote cloud ${what}\`: wrong arguments. Run \`clearcote cloud --help\`.`, 2);
  }
  return { values: r.values as Record<string, Json>, pos: r.positionals };
}

/** `clearcote cloud …`: resolves to the exit code (0 ok, 1 failed, 2 usage). Never exits itself. */
export async function cloudMain(argv: string[]): Promise<number> {
  try {
    return await cloudCommand(argv);
  } catch (e) {
    if (e instanceof CloudCliExit) return e.code;
    if (e instanceof CloudError) {
      process.stderr.write(`clearcote: ${e.message}${e.status ? ` (HTTP ${e.status}${e.code ? `, ${e.code}` : ""})` : ""}\n`);
      return 1;
    }
    process.stderr.write(`clearcote: ${(e as Error)?.message ?? String(e)}\n`);
    return 1;
  }
}

async function cloudCommand(argv: string[]): Promise<number> {
  const [cmd, ...rest] = argv;
  if (!cmd || cmd === "-h" || cmd === "--help" || cmd === "help") {
    out(CLOUD_USAGE);
    return 0;
  }
  if (!["run", "sessions", "stop", "events", "recording", "profile", "webhooks"].includes(cmd)) {
    cloudFail(`unknown cloud command '${cmd}'. Run \`clearcote cloud --help\`.`, 2);
  }
  if ((cmd === "profile" || cmd === "webhooks") && !rest[0]) {
    cloudFail(`\`clearcote cloud ${cmd}\` needs a subcommand. Run \`clearcote cloud --help\`.`, 2);
  }

  if (cmd === "run") {
    const { values: v, pos } = parse(rest, {
      url: { type: "string" }, schema: { type: "string" }, secret: { type: "string", multiple: true },
      "secret-domain": { type: "string", multiple: true }, handoff: { type: "boolean" }, record: { type: "boolean" },
      ...Object.fromEntries(RUN_OPTION_FLAGS.map((f) => [f, { type: "string" as const }])),
      "persist-profile": { type: "boolean" },
    }, -1, "run");
    const secrets = cloudSecrets(v.secret, v["secret-domain"]);
    const runOptions = cloudRunOptions(v);
    let schema: Json;
    if (v.schema) {
      try {
        schema = JSON.parse(readFileSync(v.schema, "utf8"));
      } catch (e) {
        cloudFail(`--schema ${v.schema}: ${(e as Error).message}`, 2);
      }
    }
    const cloud = new Cloud();
    const run = await cloud.runs.create(pos.join(" "), {
      url: v.url, schema, secrets: Object.keys(secrets).length ? (secrets as RunOptions["secrets"]) : undefined,
      ...(runOptions as RunOptions), onUpdate: runProgress,
    });
    if (v.json) dump(run);
    else formatRun(run).forEach((l) => out(l));
    return run?.status === "succeeded" ? 0 : 1;
  }

  if (cmd === "sessions") {
    const { values: v } = parse(rest, {}, 0, "sessions");
    const data = (await new Cloud().browsers.list()) ?? {};
    if (v.json) {
      dump(data);
      return 0;
    }
    const sessions: Json[] = data.sessions ?? [];
    for (const s of sessions) out(sessionLine(s));
    if (!sessions.length) out("no sessions");
    if (data.balanceEur !== undefined && data.balanceEur !== null) out(`balance ${eur(data.balanceEur, 2)}`);
    return 0;
  }

  if (cmd === "stop") {
    const { values: v, pos } = parse(rest, {}, 1, "stop");
    const view = await new Cloud().browsers.stop(pos[0]);
    if (v.json) dump(view);
    else out(`stopped ${pos[0]} (${str(view?.status)})`);
    return 0;
  }

  if (cmd === "events") {
    const { values: v, pos } = parse(rest, {}, 1, "events");
    const cloud = new Cloud();
    const events: Json[] = [];
    let after = 0;
    for (;;) {
      const page = (await cloud.browsers.events(pos[0], { after })) ?? {};
      const batch: Json[] = page.events ?? [];
      events.push(...batch);
      const next = page.next;
      if (!batch.length || next === null || next === undefined || next === after) break;
      after = next;
    }
    if (v.json) {
      dump({ events });
      return 0;
    }
    for (const e of events) {
      const data = e?.data;
      const extra = data && Object.keys(data).length ? `  ${JSON.stringify(data)}` : "";
      out(`${str(e?.seq)}  ${str(e?.at)}  ${str(e?.type)}${extra}`);
    }
    if (!events.length) out("no events");
    return 0;
  }

  if (cmd === "recording") {
    const { values: v, pos } = parse(rest, { output: { type: "string", short: "o" } }, 1, "recording");
    const path = v.output || `${pos[0]}.mp4`;
    await new Cloud().browsers.downloadRecording(pos[0], path);
    const size = statSync(path).size;
    if (v.json) dump({ path, bytes: size });
    else out(`saved ${path} (${size} bytes)`);
    return 0;
  }

  if (cmd === "profile") {
    if (rest[0] !== "sync") cloudFail(`unknown profile command '${rest[0]}'. Run \`clearcote cloud --help\`.`, 2);
    const { values: v, pos } = parse(rest.slice(1), {
      "from-profile": { type: "string" }, "from-cdp": { type: "string" }, "from-file": { type: "string" },
      login: { type: "string" }, domain: { type: "string", multiple: true }, "all-domains": { type: "boolean" },
      replace: { type: "boolean" },
    }, 1, "profile sync");
    const sources = [v["from-profile"], v["from-cdp"], v["from-file"], v.login].filter(Boolean);
    if (sources.length !== 1) cloudFail("profile sync needs exactly one of --from-profile, --from-cdp, --from-file or --login", 2);
    if (v.domain?.length && v["all-domains"]) cloudFail("pass --domain or --all-domains, not both", 2);
    if (!v.domain?.length && !v["all-domains"]) {
      cloudFail("refusing to upload every cookie: pass --domain <domain> (repeatable, subdomains included) or --all-domains", 2);
    }
    const res = (await new Cloud().profiles.sync(pos[0], {
      fromProfile: v["from-profile"], fromCdp: v["from-cdp"], fromFile: v["from-file"], loginUrl: v.login,
      domains: v.domain, allDomains: !!v["all-domains"], replace: !!v.replace,
    })) ?? {};
    if (v.json) {
      dump(res);
      return 0;
    }
    out(`imported ${str(res.imported)} cookies into profile ${res.name || pos[0]} (${str(res.cookies)} in total)`);
    if (res.domains?.length) out(`domains  ${res.domains.join(", ")}`);
    return 0;
  }

  // webhooks
  const action = rest[0];
  if (action === "add") {
    const { values: v, pos } = parse(rest.slice(1), { event: { type: "string", multiple: true } }, 1, "webhooks add");
    const res = (await new Cloud().webhooks.create(pos[0], { events: v.event })) ?? {};
    if (v.json) {
      dump(res);
      return 0;
    }
    out(`webhook ${str(res.id)} -> ${str(res.url)}`);
    out(`events  ${(res.events ?? []).join(", ") || "all except ping"}`);
    out(`secret  ${str(res.secret)}`);
    out("store the secret now: it is not shown again");
    return 0;
  }
  if (action === "list") {
    const { values: v } = parse(rest.slice(1), {}, 0, "webhooks list");
    const data = (await new Cloud().webhooks.list()) ?? {};
    if (v.json) {
      dump(data);
      return 0;
    }
    const hooks: Json[] = data.webhooks ?? [];
    for (const h of hooks) {
      const ld = h?.lastDelivery;
      const last = ld ? `  last ${str(ld.status)} ${str(ld.at)}` : "";
      out(`${str(h?.id)}  ${str(h?.url)}  ${(h?.events ?? []).join(",") || "all"}${last}`);
    }
    if (!hooks.length) out("no webhooks");
    return 0;
  }
  if (action === "rm" || action === "test") {
    const { values: v, pos } = parse(rest.slice(1), {}, 1, `webhooks ${action}`);
    const cloud = new Cloud();
    const res = action === "rm" ? await cloud.webhooks.delete(pos[0]) : await cloud.webhooks.test(pos[0]);
    if (v.json) dump(res);
    else out(action === "rm" ? `removed ${pos[0]}` : `sent a ping to ${pos[0]}`);
    return 0;
  }
  return cloudFail(`unknown webhooks command '${action}'. Run \`clearcote cloud --help\`.`, 2);
}

/** `--proxy scheme://user:pass@host:port` as a launch proxy option with the credentials split out. */
export function proxyOption(url: string): { server: string; username?: string; password?: string } {
  let spec;
  try { spec = toProxySpec(url); } catch { spec = null; }
  if (!spec) fail(`--proxy must be a URL such as http://user:pass@host:8080 or socks5://host:1080`);
  return { server: spec.server, ...(spec.username ? { username: spec.username } : {}), ...(spec.password ? { password: spec.password } : {}) };
}
