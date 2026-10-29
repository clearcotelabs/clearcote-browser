// geoip: resolve the egress IP's geo (timezone + lat/lon + language) so the browser matches the
// proxy actually in use — the way Camoufox does it. Source: daijro's offline "geoip-all-in-one"
// MaxMind DB (more accurate than a single online API; merges IP2Location + GeoLite2 + DB-IP,
// timezone computed from coordinates). Flow: discover the exit IP via a small IP-echo *through the
// proxy*, then look that IP up in the cached .mmdb.
//
// The .mmdb (GPL-3.0 data) is downloaded + cached on first use (≈52 MB zip → ≈120 MB), NOT bundled.
// The exit-IP lookup goes THROUGH the proxy (HTTP or SOCKS5, see ./net.ts); we never fall back to the
// local IP under a proxy, which would give the wrong region.
//
// Settings:
// - CLEARCOTE_GEOIP_ECHO_URL — your own IP-echo endpoint(s), comma-separated, used instead of the public
//   ones (api.ipify.org, icanhazip.com). The answer is the IP as plain text, or JSON with an `ip` (or
//   `query`) field.
// - CLEARCOTE_GEOIP_IPAPI=1 — also ask ip-api.com (exit IP + geo in one call) when the database has no
//   answer, e.g. while it is still downloading on first use. Off by default: ip-api.com's free endpoint is
//   for non-commercial use only and allows 45 requests a minute per IP.
// - CLEARCOTE_GEOIP_TIMEOUT_SECONDS (default 20) bounds the whole resolution. A lookup that runs out of
//   budget fails rather than hanging a launch.

import { createWriteStream, existsSync, mkdirSync, readdirSync, rmSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { Readable } from "node:stream";
import { pipeline } from "node:stream/promises";
import extract from "extract-zip";
import maxmind, { type Reader } from "maxmind";
import { PROXIED_REQUEST_SCHEMES, proxiedRequest, toProxySpec, type ProxySpec } from "./net.js";

const MMDB_URL = "https://github.com/daijro/geoip-all-in-one/releases/latest/download/geoip-aio-all.mmdb.zip";
const MMDB_MAX_AGE_DAYS = 30;
/** @internal Public IP-echo services with no usage limit, tried in order; CLEARCOTE_GEOIP_ECHO_URL replaces
 * them. Mutable so tests can point the exit-IP echo at a local server. https: some proxies answer every
 * plain-http request with a 301 to https, so an http echo failed on a proxy that browses fine. */
export const IPECHO_URLS: string[] = ["https://api.ipify.org", "https://icanhazip.com"];
export const ECHO_URL_ENV = "CLEARCOTE_GEOIP_ECHO_URL";
export const IPAPI_ENV = "CLEARCOTE_GEOIP_IPAPI";
/** @internal mutable so tests can point the ip-api fallback at a local server */
export const geoUrls = { ipApi: "http://ip-api.com/json/?fields=status,message,countryCode,timezone,lat,lon,query" };

/** The IP-echo endpoints, in order: CLEARCOTE_GEOIP_ECHO_URL (comma-separated) when set, else the public defaults. */
export function echoUrls(env: Record<string, string | undefined> = process.env): string[] {
  const custom = (env[ECHO_URL_ENV] ?? "").split(",").map((u) => u.trim()).filter(Boolean);
  return custom.length ? custom : [...IPECHO_URLS];
}

/** Whether CLEARCOTE_GEOIP_IPAPI opts in to the ip-api.com fallback (1/true/yes/on). */
export function ipapiEnabled(env: Record<string, string | undefined> = process.env): boolean {
  return ["1", "true", "yes", "on"].includes((env[IPAPI_ENV] ?? "").trim().toLowerCase());
}

/** @internal The IP an echo answered: plain text (first word), or JSON with an `ip` or `query` field. */
export function ipFromEcho(text: string): string {
  let t = (text ?? "").trim();
  if (t.startsWith("{")) {
    try {
      const j = JSON.parse(t);
      t = String(j?.ip ?? j?.query ?? "");
    } catch {
      return "";
    }
  }
  return t ? t.split(/\s+/)[0] : "";
}

export interface Geo {
  ip?: string;
  country?: string;
  timezone?: string;
  acceptLanguage?: string;
  location?: string; // "lat,lon"
}

function log(quiet: boolean | undefined, m: string): void {
  if (!quiet) process.stderr.write(`[clearcote] ${m}\n`);
}

/** Where the geoip database is cached (CLEARCOTE_CACHE/geoip when set). */
export function geoCacheRoot(): string {
  if (process.env.CLEARCOTE_CACHE) return path.join(process.env.CLEARCOTE_CACHE, "geoip");
  if (process.platform === "win32")
    return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "clearcote", "geoip");
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Caches", "clearcote", "geoip");
  return path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "clearcote", "geoip");
}

/** Whole-resolution budget in ms: CLEARCOTE_GEOIP_TIMEOUT_SECONDS (seconds, > 0), default 20s. */
export function geoipTimeoutMs(env: Record<string, string | undefined> = process.env): number {
  const raw = (env.CLEARCOTE_GEOIP_TIMEOUT_SECONDS ?? "").trim();
  const n = Number(raw);
  return raw && Number.isFinite(n) && n > 0 ? Math.round(n * 1000) : 20_000;
}

async function getText(url: string, proxy: ProxySpec | null, timeoutMs: number): Promise<string> {
  const res = await proxiedRequest(url, { proxy, timeoutMs: Math.max(1, timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return (await res.text()).trim();
}

const IPV4 = /^(?:\d{1,3}\.){3}\d{1,3}$/;
const IPV6 = /^[0-9a-f:]+$/i;
function looksLikeIp(s: string): boolean {
  return IPV4.test(s) || (s.includes(":") && IPV6.test(s));
}

/** Discover the egress IP via an IP-echo through the proxy (or direct), within `deadline`. */
async function exitIp(proxy: ProxySpec | null, deadline: number, quiet?: boolean): Promise<string | null> {
  for (const url of echoUrls()) {
    const left = deadline - Date.now();
    if (left <= 0) break;
    try {
      const ip = ipFromEcho(await getText(url, proxy, Math.min(left, 8000)));
      if (looksLikeIp(ip)) return ip;
    } catch {
      /* try next */
    }
  }
  log(quiet, "geoip: could not determine the exit IP");
  return null;
}

let _mmdbInflight: Promise<string | null> | null = null;
async function ensureMmdb(quiet?: boolean): Promise<string | null> {
  const dir = geoCacheRoot();
  const file = path.join(dir, "geoip-aio-all.mmdb");
  if (existsSync(file)) {
    const ageDays = (Date.now() - statSync(file).mtimeMs) / 86_400_000;
    if (ageDays < MMDB_MAX_AGE_DAYS) return file;
  }
  if (_mmdbInflight) return _mmdbInflight;
  _mmdbInflight = (async () => {
    try {
      mkdirSync(dir, { recursive: true });
      const zip = path.join(dir, "geoip-aio-all.mmdb.zip");
      log(quiet, "geoip: downloading the geoip-all-in-one database (~52 MB, first run only)");
      const res = await fetch(MMDB_URL, { redirect: "follow" });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      await pipeline(Readable.fromWeb(res.body as any), createWriteStream(zip));
      const tmp = path.join(dir, ".extract");
      rmSync(tmp, { recursive: true, force: true });
      await extract(zip, { dir: tmp });
      const found = findMmdb(tmp);
      if (!found) throw new Error("no .mmdb in archive");
      rmSync(file, { force: true });
      // move (rename across same dir)
      const fs = await import("node:fs");
      fs.renameSync(found, file);
      rmSync(zip, { force: true });
      rmSync(tmp, { recursive: true, force: true });
      log(quiet, "geoip: database ready");
      return file;
    } catch (e) {
      log(quiet, `geoip: database fetch failed (${(e as Error).message})`);
      return existsSync(file) ? file : null; // a stale database still beats none
    } finally {
      _mmdbInflight = null;
    }
  })();
  return _mmdbInflight;
}

function findMmdb(dir: string): string | null {
  const stack = [dir];
  while (stack.length) {
    const cur = stack.pop() as string;
    for (const e of readdirSync(cur, { withFileTypes: true })) {
      const full = path.join(cur, e.name);
      if (e.isDirectory()) stack.push(full);
      else if (e.name.toLowerCase().endsWith(".mmdb")) return full;
    }
  }
  return null;
}

/** @internal Why a lookup had no database: still downloading (first use), or it could not be fetched. */
export interface DbInfo { db?: "downloading" | "unavailable" }

let _reader: Reader<any> | null = null;
async function mmdbLookup(ip: string, deadline: number, quiet?: boolean, info: DbInfo = {}): Promise<Geo | null> {
  // The first-run database download (~52 MB) may outlast the budget. It keeps going in the
  // background and caches for the next launch.
  const left = deadline - Date.now();
  if (left <= 0) return null;
  let timer: NodeJS.Timeout | undefined;
  let timedOut = false;
  const file = await Promise.race([
    ensureMmdb(quiet),
    new Promise<null>((resolve) => { timer = setTimeout(() => { timedOut = true; resolve(null); }, left); }),
  ]);
  if (timer) clearTimeout(timer);
  if (!file) {
    info.db = timedOut ? "downloading" : "unavailable";
    return null;
  }
  try {
    if (!_reader) _reader = await maxmind.open(file);
    const rec: any = _reader.get(ip);
    if (!rec) return null;
    const country: string | undefined = rec?.country?.iso_code;
    const lat = rec?.location?.latitude;
    const lon = rec?.location?.longitude;
    const tz: string | undefined = rec?.location?.time_zone;
    if (!tz && lat == null) return null;
    return {
      ip,
      country,
      timezone: tz,
      acceptLanguage: acceptLanguageForCountry(country),
      location: lat != null && lon != null ? `${lat},${lon}` : undefined,
    };
  } catch (e) {
    log(quiet, `geoip: mmdb read failed (${(e as Error).message})`);
    return null;
  }
}

/** @internal the database lookup, replaceable in tests */
export const geoDb = { lookup: mmdbLookup };

// Opt-in fallback (CLEARCOTE_GEOIP_IPAPI=1): ip-api.com returns geo directly (through the proxy), no DB
// needed. Its free endpoint is for non-commercial use only.
async function ipApiFallback(proxy: ProxySpec | null, deadline: number): Promise<Geo | null> {
  const left = deadline - Date.now();
  if (left <= 0) return null;
  try {
    const txt = await getText(geoUrls.ipApi, proxy, Math.min(left, 8000));
    const j = JSON.parse(txt);
    if (j?.status !== "success") return null;
    return {
      ip: j.query,
      country: j.countryCode,
      timezone: j.timezone,
      acceptLanguage: acceptLanguageForCountry(j.countryCode),
      location: j.lat != null && j.lon != null ? `${j.lat},${j.lon}` : undefined,
    };
  } catch {
    return null;
  }
}

/** Outcome of a geo resolution: the geo, or why there is none. */
export interface GeoResult {
  geo: Geo | null;
  /** Human-readable failure reason when `geo` is null or has no timezone. */
  reason?: string;
  /** How long the resolution took, in ms. */
  elapsedMs: number;
}

/**
 * Resolve geo for the egress (through `proxy` if given — HTTP or SOCKS5 — else direct), reporting
 * why it failed. Never throws. Bounded by `timeoutMs` (default {@link geoipTimeoutMs}).
 */
/**
 * Look the proxy's exit IP up once more. Resolves to the new IP when it differs from `firstIp` (same
 * address family: an IPv4 answer after an IPv6 one is a different echo, not a rotation), else null.
 * Never rejects.
 */
export async function checkEgressDrift(
  proxy: string | { server?: string; username?: string; password?: string } | undefined,
  firstIp: string | undefined,
  timeoutMs = 8000
): Promise<string | null> {
  if (!proxy || !firstIp) return null;
  try {
    const ip = await exitIp(toProxySpec(proxy ?? null), Date.now() + timeoutMs, true);
    if (ip && ip !== firstIp && ip.includes(":") === firstIp.includes(":")) return ip;
  } catch { /* never fails the launch */ }
  return null;
}

export function egressDriftMessage(firstIp: string, newIp: string): string {
  return `the proxy's exit IP changed between two lookups at launch (${firstIp} -> ${newIp}): it rotates, so the ` +
    `timezone, language and WebRTC IP taken from ${firstIp} will not match the exit sites see. Use a ` +
    "sticky session (most providers take a session id in the proxy username).";
}

/**
 * Start the egress-drift check; the returned promise prints one warning if the exit moved and never
 * rejects. The launch awaits it only once the browser is up, so the lookup overlaps the launch
 * instead of adding to it (a pending socket would otherwise hold a short script open).
 */
export function startEgressDriftCheck(
  proxy: string | { server?: string; username?: string; password?: string } | undefined,
  firstIp: string | undefined,
  quiet?: boolean
): Promise<void> {
  if (quiet || process.env.CLEARCOTE_NO_WARN || !proxy || !firstIp) return Promise.resolve();
  return checkEgressDrift(proxy, firstIp).then((newIp) => {
    if (newIp) process.stderr.write(`clearcote: warning: ${egressDriftMessage(firstIp, newIp)}\n`);
  }, () => undefined);
}

export async function resolveGeoDetailed(
  proxy?: string | { server?: string; username?: string; password?: string },
  opts: { quiet?: boolean; timeoutMs?: number } = {},
): Promise<GeoResult> {
  const started = Date.now();
  const budget = opts.timeoutMs ?? geoipTimeoutMs();
  const deadline = started + budget;
  let spec: ProxySpec | null;
  try {
    spec = toProxySpec(proxy ?? null);
  } catch (e) {
    return { geo: null, reason: `invalid proxy (${(e as Error).message})`, elapsedMs: Date.now() - started };
  }
  // Say which scheme, rather than the "could not determine the exit IP" every lookup would end in.
  const scheme = spec ? spec.server.slice(0, spec.server.indexOf(":")).toLowerCase() : "";
  if (spec && !PROXIED_REQUEST_SCHEMES.has(scheme)) {
    return { geo: null, reason: `${scheme}:// proxies are not supported; use http, https or socks5`, elapsedMs: Date.now() - started };
  }
  const ip = await exitIp(spec, deadline, opts.quiet);
  const info: DbInfo = {};
  let geo: Geo | null = ip ? await geoDb.lookup(ip, deadline, opts.quiet, info) : null;
  const ipapi = ipapiEnabled();
  if ((!geo || !geo.timezone) && ipapi) geo = (await ipApiFallback(spec, deadline)) ?? geo;
  const elapsedMs = Date.now() - started;
  if (geo && geo.timezone) {
    log(opts.quiet, `geoip: ${geo.ip} -> ${geo.country} tz=${geo.timezone} lang=${geo.acceptLanguage}`);
    return { geo, elapsedMs };
  }
  const dbReason = info.db === "downloading"
    ? "the geoip database is still downloading (~52 MB, first use only); launch again in a moment or raise CLEARCOTE_GEOIP_TIMEOUT_SECONDS"
    : "the geoip database could not be downloaded";
  const reason = ip && info.db && !ipapi
    ? `${dbReason} -- or set ${IPAPI_ENV}=1 to ask ip-api.com (non-commercial use only)`
    : Date.now() >= deadline
      ? `timed out after ${Math.round(budget / 1000)}s (CLEARCOTE_GEOIP_TIMEOUT_SECONDS)`
      : !ip
        ? `could not determine the exit IP${spec ? " through the proxy" : ""}`
        : geo
          ? `no timezone for exit IP ${ip}`
          : `no geo data for exit IP ${ip}`;
  return { geo, reason, elapsedMs };
}

/** Test seam: the resolver {@link applyGeoip} asks (resolveGeoDetailed). */
export const geoResolver: { resolve: typeof resolveGeoDetailed } = { resolve: resolveGeoDetailed };

/** The persona fields geoip fills (a FingerprintOptions, or the Docker launch's option bag). */
export interface GeoFillable {
  timezone?: string;
  acceptLanguage?: string;
  location?: string;
  webrtcIp?: string;
}

/**
 * Fill unset timezone/acceptLanguage/location/webrtcIp on `fp` from the proxy's exit-IP geo.
 *
 * FAILS CLOSED: if the region cannot be resolved, the launch throws {@link GeoipError} instead of
 * continuing on the host's clock and a default language (UTC + en-US on most servers) — the exact
 * mismatch geoip exists to prevent. A caller who set BOTH `timezone` and `acceptLanguage`
 * explicitly still launches (with a warning), since nothing geoip would fill is missing.
 */
export async function applyGeoip(fp: GeoFillable, proxy: unknown, quiet?: boolean, timeoutMs?: number): Promise<Geo | undefined> {
  const result = await geoResolver.resolve(proxy as { server?: string; username?: string; password?: string } | undefined,
    timeoutMs === undefined ? { quiet } : { quiet, timeoutMs });
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

/**
 * geoip is ON by default (since 0.66.0): with no `geoip` option the launch looks the egress up (the
 * proxy's exit, or this machine's own public IP when no proxy is set) so timezone, language, location
 * and the WebRTC IP describe the place sites see the connection come from.
 *   - "strict": `geoip: true`. Fails closed ({@link GeoipError}), as before.
 *   - "auto":   the default. A lookup that fails warns and launches with the persona's defaults.
 *   - "off":    `geoip: false`, or CLEARCOTE_GEOIP=0/false/no/off (an explicit `geoip: true` still wins).
 */
export type GeoipMode = "off" | "auto" | "strict";
export const GEOIP_ENV = "CLEARCOTE_GEOIP";

export function geoipMode(geoip: boolean | undefined | null, env: Record<string, string | undefined> = process.env): GeoipMode {
  if (geoip === true) return "strict";
  if (geoip === false) return "off";
  return /^(0|false|no|off)$/i.test((env[GEOIP_ENV] ?? "").trim()) ? "off" : "auto";
}

/** The default lookup's budget: CLEARCOTE_GEOIP_TIMEOUT_SECONDS when set, else 10 s (strict keeps 20 s). */
const AUTO_TIMEOUT_MS = 10_000;
/** This machine's own egress rarely moves; one lookup serves the process for 10 minutes. */
const DIRECT_CACHE_MS = 10 * 60_000;
let _direct: { geo: GeoFillable; at: number } | null = null;
/** @internal tests */
export function resetDirectGeoCache(): void { _direct = null; }

/** What {@link applyGeoipMode} did: the geo, and whether the WebRTC IP came from the default lookup. */
export interface GeoipApplied {
  geo?: Geo;
  autoWebrtcIp: boolean;
  /** The egress IP the lookup found (also from the no-proxy cache): with no proxy, this machine's own. */
  ip?: string;
}

/**
 * Apply geoip in `mode` (see {@link geoipMode}). "auto" skips the lookup when every field it fills is
 * already set, reuses a no-proxy lookup for 10 minutes, and on failure warns once and launches.
 */
export async function applyGeoipMode(fp: GeoFillable, proxy: unknown, mode: GeoipMode, quiet?: boolean): Promise<GeoipApplied> {
  if (mode === "off") return { autoWebrtcIp: false };
  if (mode === "strict") {
    const geo = await applyGeoip(fp, proxy, quiet);
    return { geo, autoWebrtcIp: false, ip: geo?.ip };
  }
  if (fp.timezone != null && fp.acceptLanguage != null && fp.location != null && fp.webrtcIp != null) return { autoWebrtcIp: false };
  const hadWebrtcIp = fp.webrtcIp != null;
  const direct = !proxy;
  if (direct && _direct && Date.now() - _direct.at < DIRECT_CACHE_MS) {
    for (const k of ["timezone", "acceptLanguage", "location", "webrtcIp"] as const)
      if (fp[k] == null && _direct.geo[k] != null) fp[k] = _direct.geo[k];
    return { autoWebrtcIp: !hadWebrtcIp && fp.webrtcIp != null, ip: _direct.geo.webrtcIp };
  }
  const env = (process.env.CLEARCOTE_GEOIP_TIMEOUT_SECONDS ?? "").trim();
  try {
    const before: GeoFillable = { ...fp };
    const geo = await applyGeoip(fp, proxy, quiet, env ? geoipTimeoutMs() : AUTO_TIMEOUT_MS);
    if (geo && direct) _direct = { geo: { timezone: geo.timezone, acceptLanguage: geo.acceptLanguage, location: geo.location, webrtcIp: geo.ip }, at: Date.now() };
    return { geo, autoWebrtcIp: before.webrtcIp == null && fp.webrtcIp != null, ip: geo?.ip };
  } catch (e) {
    if (!(e instanceof GeoipError)) throw e;
    if (!quiet && !process.env.CLEARCOTE_NO_WARN)
      process.stderr.write(`clearcote: warning: ${e.message.replace(/^geoip: /, "geoip (on by default): ")
        .replace(/ Launching anyway[\s\S]*$/, "")} Launching with the persona's default timezone and language; ` +
        "pass geoip: true to fail instead, or geoip: false to skip the lookup.\n");
    return { autoWebrtcIp: false };
  }
}

/**
 * The open build (no licence) has no WebRTC UDP egress block, and a WebRTC IP there re-enables UDP that
 * does not go through the proxy: a page's own TURN server would read this machine's real address off the
 * packets. So a WebRTC IP the DEFAULT lookup filled is dropped again on an open-build launch through a
 * proxy. With no proxy the real address is the exit anyway; `geoip: true` or an explicit webrtcIp is the
 * caller's choice and is kept.
 */
/**
 * Engine r54+ (pro-r54-06): whether this launch may send real STUN instead of fabricating the WebRTC IP.
 * Only with no proxy at all, and only when the WebRTC IP is this machine's own egress as the lookup
 * found it: a hand-set webrtcIp may name another place on purpose, and real STUN would contradict it.
 */
export function directStunEligible(fp: GeoFillable, applied: GeoipApplied, proxy: unknown): boolean {
  return !proxy && !!applied.ip && fp.webrtcIp === applied.ip;
}

export function dropAutoWebrtcIp(fp: GeoFillable, applied: GeoipApplied, proxy: unknown, licensed: boolean): void {
  if (applied.autoWebrtcIp && proxy && !licensed) delete fp.webrtcIp;
}

/**
 * Resolve geo for the egress (through `proxy` if given, else direct). Never throws — returns null
 * on failure. Uses the geoip-all-in-one offline DB (ip-api.com only with CLEARCOTE_GEOIP_IPAPI=1).
 */
export async function resolveGeo(
  proxy?: string | { server?: string; username?: string; password?: string },
  opts: { quiet?: boolean; timeoutMs?: number } = {}
): Promise<Geo | null> {
  return (await resolveGeoDetailed(proxy, opts)).geo;
}

// country (ISO-3166 alpha-2) -> the OS locale a machine there most plausibly runs. The geoip DB has
// no language data, so this maps the resolved country. ONE tag, not a list: the launch expands it
// the way Chrome does (DE -> de-DE -> UI "de", languages de-DE,de,en-US,en; CA -> en-CA ->
// en-GB,en-US,en; MY -> ms-MY -> Intl "ms" with en-US,en). See ./languages.ts resolveLanguages.
// Multi-language countries take the majority language (BE nl, CH de, CA en, IN en).
export const COUNTRY_LANG: Readonly<Record<string, string>> = {
  US: "en-US", GB: "en-GB", CA: "en-CA", AU: "en-AU", NZ: "en-NZ", IE: "en-IE",
  IN: "en-IN", ZA: "en-ZA", SG: "en-SG", PH: "en-PH",
  DE: "de-DE", AT: "de-AT", CH: "de-CH", FR: "fr-FR", BE: "nl-BE", NL: "nl-NL",
  ES: "es-ES", MX: "es-MX", AR: "es-AR", CL: "es-CL", CO: "es-CO", PE: "es-PE",
  VE: "es-VE", EC: "es-EC", UY: "es-UY", PT: "pt-PT", BR: "pt-BR",
  IT: "it-IT", PL: "pl-PL", RU: "ru-RU", UA: "uk-UA", SE: "sv-SE", NO: "nb-NO",
  DK: "da-DK", FI: "fi-FI", CZ: "cs-CZ", SK: "sk-SK", RO: "ro-RO", HU: "hu-HU",
  GR: "el-GR", BG: "bg-BG", HR: "hr-HR", SI: "sl-SI", RS: "sr-RS", LT: "lt-LT",
  LV: "lv-LV", EE: "et-EE", TR: "tr-TR", IL: "he-IL", SA: "ar-SA", AE: "ar-AE",
  EG: "ar-EG", JP: "ja-JP", KR: "ko-KR", CN: "zh-CN", HK: "zh-HK", TW: "zh-TW",
  TH: "th-TH", VN: "vi-VN", ID: "id-ID", MY: "ms-MY",
};

/**
 * The OS-locale tag for a country code (`DE` -> `de-DE`), en-US when unknown. It is a valid one-tag
 * Accept-Language; launch() turns it into Chrome's own language list.
 */
export function acceptLanguageForCountry(cc?: string): string {
  if (!cc) return "en-US";
  return COUNTRY_LANG[cc.toUpperCase()] || "en-US";
}

/**
 * Thrown by launch() when `geoip: true` was requested and the region could not be resolved.
 *
 * Failing closed is the point: continuing would launch with the host's clock and a default
 * language — UTC + en-US on most servers — which is exactly the mismatch geoip exists to prevent.
 * Pass an explicit `timezone` AND `acceptLanguage` to launch anyway when the lookup is unavailable.
 */
export class GeoipError extends Error {
  code = "GEOIP_UNRESOLVED";
  constructor(message: string) {
    super(message);
    this.name = "GeoipError";
  }
}
